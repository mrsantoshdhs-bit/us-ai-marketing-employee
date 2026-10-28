"""Inbox Agent: reads the sales mailbox (read-only), classifies replies, updates the CRM, suggests responses.

Without IMAP settings it still processes simulated replies (clearly labelled) so the workflow can be
tested; real mailbox reading starts once IMAP_HOST and the credentials are set.
"""

from __future__ import annotations

import imaplib
import re

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent, RetryPolicy
from aime.db import kv_get, kv_set
from aime.inbox import Mailbox, ParsedEmail, new_text, parse_message
from aime.meetings import check_double_bookings
from aime.models import Alert, Company, Contact, FollowUp, InboundEmail, Lead
from aime.reply_intel import CALENDLY_NOTICE, process_pending


class InboxInput(BaseModel):
    pass


class InboxOutput(BaseModel):
    mailbox: str = "not configured"
    fetched: int = 0
    stored: int = 0
    skipped_own: int = 0
    skipped_unrelated: int = 0  # INBOX_SCOPE=related: not sales mail, so not stored
    categories: dict[str, int] = Field(default_factory=dict)
    needs_review: int = 0
    proposals: int = 0  # A4: meeting-time proposals drafted for approval
    booked: int = 0  # meetings booked on their own inside an approved campaign


class InboxAgent(BaseAgent[InboxInput, InboxOutput]):
    name = "inbox_monitor"
    title = "Inbox Agent"
    responsibilities = [
        "Read new emails in the sales mailbox without changing it (read-only IMAP)",
        "Match replies to leads by thread and address; classify interest, objections, unsubscribes, bounces",
        "Stop sequences on any human reply; suppress unsubscribes and bounces; open opportunities when interested",
        "Draft suggested responses for approval and send uncertain or sensitive replies to review",
    ]
    input_model = InboxInput
    output_model = InboxOutput
    # a dropped connection or a server hiccup (socket, TLS or IMAP protocol error) is retried once in the run
    retry_policy = RetryPolicy(max_attempts=2, retry_on=(*RetryPolicy().retry_on, OSError, imaplib.IMAP4.error))

    def __init__(self, ctx, mailbox: Mailbox | None = None, imap_factory=None):
        super().__init__(ctx)
        self.injected = mailbox is not None
        self.imap_factory = imap_factory or imaplib.IMAP4_SSL  # for panel mailboxes (tests pass a fake server)
        self.mailbox = mailbox or (Mailbox(ctx.settings) if ctx.settings.imap_configured else None)

    def _sources(self, session: Session) -> list[tuple[str, Mailbox, str, object]]:
        """(name, mailbox, state key, token) for each mailbox to read: the .env one (or one a test injects) and every
        enabled Super Admin panel mailbox with "read replies" on. Each keeps its own position."""
        out = []
        if self.mailbox is not None:
            out.append((f"{self.settings.imap_host} / {self.settings.imap_folder}", self.mailbox, "inbox.state",
                        self._oauth_token(session)))
        if self.injected:
            return out
        from aime import mail_accounts
        from aime.integrations import oauth

        for a in mail_accounts.readable(session):
            token = None
            if a.auth == "oauth":
                def token(account=a.id) -> str:
                    with self.ctx.http() as http:
                        return oauth.access_token(session, self.settings, http, "microsoft", "mail", account=account)
            out.append((f"{a.email} / {a.imap_folder}", Mailbox(mail_accounts.settings_for(self.settings, a),
                                                                  imap_factory=self.imap_factory),
                        f"inbox.state.mailbox{a.id}", token))
        return out

    def _oauth_token(self, session: Session):
        """With MAIL_AUTH=oauth: a callable giving a fresh access token for the connected account."""
        if self.settings.mail_auth != "oauth":
            return None
        from aime.integrations import oauth

        def token() -> str:
            with self.ctx.http() as http:
                return oauth.access_token(session, self.settings, http, self.settings.mail_oauth_provider, "mail")
        return token

    def _meetings(self, session: Session, out) -> None:
        """A4: offer free slots for new meeting requests and raise reminders, when booking is switched on."""
        from aime import booking

        booking.reminders(session, self.settings)
        booking.sync_offered(session)
        ok, _why = booking.ready(session, self.settings)
        if not ok:
            return
        from aime.integrations import oauth
        from aime.integrations.calendar import CalendarError, calendar_for

        with self.ctx.http() as http:
            provider = self.settings.calendar_provider.lower()
            cal = calendar_for(self.settings, http,
                               lambda: oauth.access_token(session, self.settings, http, provider, "calendar"))
            try:
                out.proposals = booking.propose_pending(session, self.settings, cal, self.name)
                out.booked = booking.act_on_answers(session, self.settings, cal, self.name)
            except (CalendarError, oauth.OAuthError) as exc:
                session.add(Alert(level="warning", source=self.name, message=f"Meeting times not proposed: {exc}"))

    def execute(self, inp: InboxInput, session: Session) -> InboxOutput:
        out = InboxOutput()
        from aime.models import MailAccount
        own = {a.lower() for a in (self.settings.smtp_from, self.settings.imap_credentials[0],
                                    self.settings.unsubscribe_email) if a}
        own |= {e.lower() for e in session.execute(select(MailAccount.email)).scalars()}  # our own mail is not a reply
        sources = self._sources(session) if self.settings.network_enabled else []
        if sources:
            out.mailbox = "; ".join(name for name, *_ in sources) + " (read-only)"
        for _name, box, state_key, token in sources:
            raws, state = box.fetch_new(kv_get(session, state_key), token=token)
            out.fetched += len(raws)
            for key, raw in raws:
                p = parse_message(raw, own)
                if p.from_email in own:
                    out.skipped_own += 1
                    continue
                if p.message_id and session.execute(select(InboundEmail.id).where(
                        InboundEmail.message_id == p.message_id)).first():
                    continue
                if self.settings.inbox_scope != "all" and not sales_related(session, p):
                    out.skipped_unrelated += 1  # read once (the mailbox position moves on), never stored
                    continue
                session.add(InboundEmail(
                    mailbox_uid=key, message_id=p.message_id,
                    in_reply_to=p.bounced_message_id if p.is_bounce and p.bounced_message_id else p.in_reply_to,
                    references=p.references, from_email=p.from_email, from_name=p.from_name, to_email=p.to_email,
                    subject=p.subject, received_at=p.received_at, body_text=p.body_text, new_text=new_text(p.body_text),
                    auto_submitted=p.auto_submitted, bounced_addresses=p.bounced_addresses, status="new"))
                out.stored += 1
            kv_set(session, state_key, state)
            session.commit()
        out.categories = process_pending(session, self.settings, self.name, llm=self.ctx.llm)
        check_double_bookings(session, self.settings)  # after the whole batch: a reschedule's two notices are one change
        self._meetings(session, out)
        out.needs_review = len(session.execute(select(InboundEmail.id).where(InboundEmail.status == "needs_review")).all())
        return out


CALENDLY_SUBJECT = re.compile(r"\b(new event|canceled|cancelled|rescheduled|invitee)\b", re.I)


def sales_related(session: Session, p: ParsedEmail) -> bool:
    """Is this email part of the sales work? A reply in one of our threads, a bounce, a Calendly booking notice
    (also forwarded), or mail from a known contact or a lead's company domain. Everything else in a shared mailbox,
    Calendly's own newsletters included, is left alone."""
    if p.is_bounce or p.bounced_addresses:
        return True
    sender_domain = p.from_email.rsplit("@", 1)[-1]
    if (sender_domain.endswith("calendly.com") or "calendly.com" in (p.body_text or "").lower()) \
            and (CALENDLY_SUBJECT.search(p.subject or "") or CALENDLY_NOTICE.match(p.subject or "")):
        return True
    ids = {i.strip() for i in [p.in_reply_to or "", *(p.references or "").split()] if i.strip()}
    if ids and session.execute(select(FollowUp.id).where(FollowUp.message_id.in_(ids))).first():
        return True
    if session.execute(select(Contact.id).where(func.lower(Contact.email) == p.from_email)).first():
        return True
    return bool(session.execute(select(Lead.id).join(Company, Company.id == Lead.company_id).where(
        func.lower(Company.domain) == sender_domain, Company.merged_into_id.is_(None))).first())
