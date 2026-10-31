"""Inbox Agent: reads the sales mailbox (read-only), classifies replies, updates the CRM, suggests responses.

Without IMAP settings it still processes simulated replies (clearly labelled) so the workflow can be
tested; real mailbox reading starts once IMAP_HOST and the credentials are set.
"""

from __future__ import annotations

import hashlib
import imaplib
import re
from datetime import timedelta

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent, RetryPolicy
from aime.db import kv_get, kv_set
from aime.inbox import HEADER_LIMIT, NO_ADDRESS, Mailbox, ParsedEmail, new_text, parse_message
from aime.mail_auth import sender_signed
from aime.meetings import check_double_bookings
from aime.models import Alert, Contact, FollowUp, InboundEmail, SkippedMail
from aime.monitoring import alert_once, resolve_alerts
from aime.reply_intel import (BOOKING_NOTICE_DAYS, CALENDLY_NOTICE, booking_links_without_notice, company_leads,
                              forget_old_skipped_mail, process_pending, remembered_lead_id, sent_with_our_subject)
from aime.timeutil import utcnow


NOTICE_ALERT = "calendly:no_notices"
READ_TRIES = 3  # an email that every run dies on is met this many times, then set aside so the mail behind it is read


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
                                                                  imap_factory=mail_accounts.imap_factory_for(a, self.imap_factory)),
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

    def _authentication(self, raw: bytes, p: ParsedEmail) -> str | None:
        """Who vouches for this email, as stored for the Calendly check and for "was the sender proven" (aime.mail_auth).
        AIME DKIM-verifies what it keeps ("aime.dkim; ..."); a server's own Authentication-Results line is kept only when
        that server is named in TRUSTED_AUTHSERV_IDS. A header written by the sender is never stored as trusted."""
        from aime.mail_auth import authserv_id, trusted_authserv_ids

        if p.auth_results and authserv_id(p.auth_results) in trusted_authserv_ids(self.settings):
            return p.auth_results
        from aime.dkim_verify import auth_results

        return auth_results(raw)

    def _store(self, session: Session, key: str, raw: bytes, own: set[str], out: InboxOutput) -> None:
        p = parse_message(raw, own)
        # Our own mail is no reply, but a Calendly notice forwarded by hand from this mailbox is a notice.
        if p.from_email in own and not (CALENDLY_NOTICE.match(p.subject or "") and looks_like_calendly(p)):
            out.skipped_own += 1
            return
        if p.message_id and session.execute(select(InboundEmail.id).where(
                InboundEmail.message_id == p.message_id)).first():
            return
        if self.settings.inbox_scope != "all" and not sales_related(session, p):
            out.skipped_unrelated += 1  # read once (the mailbox position moves on): only who, what about and when is kept
            if not session.execute(select(SkippedMail.id).where(SkippedMail.message_id == p.message_id)).first():
                session.add(SkippedMail(mailbox_uid=key, message_id=p.message_id, from_email=p.from_email,
                                        subject=p.subject, received_at=p.received_at))
            return
        authentication = self._authentication(raw, p)
        session.add(InboundEmail(
            mailbox_uid=key, message_id=p.message_id,
            in_reply_to=p.bounced_message_id if p.is_bounce and p.bounced_message_id else p.in_reply_to,
            references=p.references, from_email=p.from_email, from_name=p.from_name, to_email=p.to_email,
            subject=p.subject, received_at=p.received_at, body_text=p.body_text, new_text=new_text(p.body_text),
            auto_submitted=p.auto_submitted, bounced_addresses=p.bounced_addresses, status="new",
            auth_results=authentication, sender_verified=sender_signed(p.from_email, authentication),
            mail_kind=p.kind, mail_note=p.note[:400] if p.note else None))
        out.stored += 1

    def _keep_unreadable(self, session: Session, key: str, raw: bytes, own: set[str], exc: Exception,
                         out: InboxOutput) -> None:
        """An email that could not be read must neither stop the others nor disappear: it is kept without its text, so
        it is handled like an empty reply (the sequence of a known sender stops) and a person opens it in the mailbox."""
        sender = _sender_of_unreadable(raw)
        if sender in own:
            out.skipped_own += 1
            return
        stand_in = f"<unreadable-{hashlib.sha256(raw).hexdigest()[:32]}@aime.invalid>"
        if session.execute(select(InboundEmail.id).where(InboundEmail.message_id == stand_in)).first():
            return
        why = str(exc) if isinstance(exc, _StoppedTheJob) else type(exc).__name__
        self.log.warning("Email %s could not be read (%s)", key, why)
        session.add(InboundEmail(mailbox_uid=key, message_id=stand_in, from_email=sender, status="new", new_text="",
                                 subject=f"(could not be read: open it in the mailbox, {key})"[:300], body_text=""))
        session.add(Alert(level="warning", source=self.name,
                          message=f"An email from {sender} could not be read ({why}): please open it in "
                                  f"the mailbox ({key}). It is on Replies."[:500]))
        out.stored += 1

    def _read_mailbox(self, session: Session, box: Mailbox, state_key: str, token, own: set[str], out: InboxOutput) -> None:
        """The position is saved before each email, with a mark that it is being read. A run killed on one email (the
        scheduler's limit, a reboot, out of memory) therefore loses nothing, and after READ_TRIES such runs that email is
        set aside with an alert, so an opt-out behind it is still read."""
        before = kv_get(session, state_key)
        raws, state = box.fetch_new(before, token=token)
        out.fetched += len(raws)
        same_numbering = bool(before) and before.get("uidvalidity") == state.get("uidvalidity")
        if before and not same_numbering:
            session.add(Alert(level="warning", source=self.name, message=f"The mailbox was moved or rebuilt ({state_key}): its "
                              "numbering changed, so AIME's reading position was reset and recent mail is being read again. "
                              "Please check the mailbox for older mail that was still unanswered."))
        for uid in state.get("skipped_uids") or []:  # the server would not give this one after several tries
            if uid not in ((before or {}).get("skipped_uids") or []):
                session.add(Alert(level="warning", source=self.name, message=f"An email (UID {uid}) could not be fetched "
                                  f"after several tries and was skipped ({state_key}): please look for it in the mailbox."))
        position = int(before.get("last_uid", 0)) if same_numbering else 0
        reading = dict(state.pop("reading", None) or {})
        for key, raw in raws:
            uid = int(key.rsplit(":", 1)[1])
            tries = int(reading.get(str(uid), 0))
            if tries >= READ_TRIES:
                self._keep_unreadable(session, key, raw, own, _StoppedTheJob(f"the job stopped on it {tries} times, so it was set aside"), out)
            else:
                kv_set(session, state_key, {**state, "last_uid": position, "reading": {str(uid): tries + 1}})
                session.commit()
                try:
                    self._store(session, key, raw, own, out)
                    session.commit()
                except SQLAlchemyError:  # the database, not the email: the position stays, and it is read again next run
                    session.rollback()
                    self._undo_mark(session, state_key, {**state, "last_uid": position})
                    raise
                except Exception as exc:  # noqa: BLE001 -- whatever a stranger's email does, the good ones are still handled
                    session.rollback()
                    self._keep_unreadable(session, key, raw, own, exc, out)
                    session.commit()
            position = uid
            session.commit()
        kv_set(session, state_key, state)
        session.commit()

    @staticmethod
    def _undo_mark(session: Session, state_key: str, state: dict) -> None:
        """A database failure must not count against the email being read."""
        try:
            kv_set(session, state_key, state)
            session.commit()
        except SQLAlchemyError:  # still failing: the run stops here all the same
            session.rollback()

    def _watch_booking_notices(self, session: Session) -> None:
        """Booking links went out days ago and no Calendly notice was ever read: bookings are not being recorded."""
        count, oldest = booking_links_without_notice(session)
        active = set()
        if oldest is not None and utcnow() - oldest >= timedelta(days=BOOKING_NOTICE_DAYS):
            active.add(NOTICE_ALERT)
            alert_once(session, "warning", self.name, f"{count} booking link(s) sent, but no Calendly notice has ever "
                       "reached the monitored mailbox, so bookings are not recorded. Forward Calendly's notifications to "
                       "it (docs/SALES_SETUP.md section 6).", self.settings, key=NOTICE_ALERT)
        resolve_alerts(session, (NOTICE_ALERT,), active)

    def execute(self, inp: InboxInput, session: Session) -> InboxOutput:
        out = InboxOutput()
        from aime.models import MailAccount
        own = {a.lower() for a in (self.settings.smtp_from, self.settings.imap_credentials[0],
                                    self.settings.unsubscribe_email) if a}
        own |= {e.lower() for e in session.execute(select(MailAccount.email)).scalars()}  # our own mail is not a reply
        # What an earlier run stored and could not finish (it died on the email behind them) is handled first.
        categories = process_pending(session, self.settings, self.name, llm=self.ctx.llm)
        forget_old_skipped_mail(session)
        sources = self._sources(session) if self.settings.network_enabled else []
        if sources:
            out.mailbox = "; ".join(name for name, *_ in sources) + " (read-only)"
        failed = []  # a mailbox that cannot be read fails the run, but only after the others' mail has been handled
        for name, box, state_key, token in sources:
            try:
                self._read_mailbox(session, box, state_key, token, own, out)
            except Exception as exc:  # noqa: BLE001 -- raised again below
                session.rollback()
                self.log.warning("Mailbox %s could not be read (%s)", name, type(exc).__name__)
                failed.append(exc)
        for category, count in process_pending(session, self.settings, self.name, llm=self.ctx.llm).items():
            categories[category] = categories.get(category, 0) + count
        out.categories = categories
        check_double_bookings(session, self.settings)  # after the whole batch: a reschedule's two notices are one change
        self._watch_booking_notices(session)
        self._meetings(session, out)
        out.needs_review = len(session.execute(select(InboundEmail.id).where(InboundEmail.status == "needs_review")).all())
        if failed:
            session.commit()
            raise failed[0]
        return out


CALENDLY_SUBJECT = re.compile(r"\b(new event|canceled|cancelled|rescheduled|invitee)\b", re.I)
REPLY_SUBJECT = re.compile(r"\s*(re|aw|sv|antw|fwd?|fw|wg|tr|rv)\s*:", re.I)
OUR_FOOTER_QUOTED = re.compile(r"to stop receiving emails from us", re.I)  # the line every email of ours ends with


def looks_like_calendly(p: ParsedEmail) -> bool:
    """A Calendly booking notice, sent by Calendly or forwarded (its subject and a calendly.com mention)."""
    sender_domain = p.from_email.rsplit("@", 1)[-1]
    return bool((sender_domain.endswith("calendly.com") or "calendly.com" in (p.body_text or "").lower())
                and (CALENDLY_SUBJECT.search(p.subject or "") or CALENDLY_NOTICE.match(p.subject or "")))


class _StoppedTheJob(Exception):
    """Runs were killed while reading this email, again and again."""


def _sender_of_unreadable(raw: bytes) -> str:
    """The sender of an email that could not be parsed, read from the raw header lines. Only the start of the email and of
    the line is looked at: no pattern runs over a stranger's whole line."""
    found = re.search(rb"(?im)^from:([^\r\n]{0,%d})" % HEADER_LIMIT, raw[:256 * 1024])
    address = re.search(rb"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", found.group(1)) if found else None
    return address.group(0).decode("ascii", "replace").lower() if address else NO_ADDRESS


def sales_related(session: Session, p: ParsedEmail) -> bool:
    """Is this email part of the sales work? A reply in one of our threads, a bounce, a Calendly booking notice (also
    forwarded), mail from a known contact, a lead's company domain or someone who answered in our thread before, or an
    answer that carries the subject of an email we sent or quotes our footer (helpdesks and personal addresses write
    without our thread headers). Everything else in a shared mailbox, Calendly's own newsletters included, is left alone."""
    if p.is_bounce or p.bounced_addresses:
        return True
    if looks_like_calendly(p):
        return True
    ids = {i.strip() for i in [p.in_reply_to or "", *(p.references or "").split()] if i.strip()}
    if ids and session.execute(select(FollowUp.id).where(FollowUp.message_id.in_(ids))).first():
        return True
    if session.execute(select(Contact.id).where(func.lower(Contact.email) == p.from_email)).first():
        return True
    if company_leads(session, p.from_email) or remembered_lead_id(session, p.from_email):
        return True
    if sent_with_our_subject(session, p.subject):
        return True
    return bool(REPLY_SUBJECT.match(p.subject or "") and OUR_FOOTER_QUOTED.search(p.body_text or ""))
