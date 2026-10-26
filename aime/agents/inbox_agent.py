"""Inbox Agent: reads the sales mailbox (read-only), classifies replies, updates the CRM, suggests responses.

Without IMAP settings it still processes simulated replies (clearly labelled) so the workflow can be
tested; real mailbox reading starts once IMAP_HOST and the credentials are set.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent, RetryPolicy
from aime.db import kv_get, kv_set
from aime.inbox import Mailbox, new_text, parse_message
from aime.models import InboundEmail
from aime.reply_intel import process_pending


class InboxInput(BaseModel):
    pass


class InboxOutput(BaseModel):
    mailbox: str = "not configured"
    fetched: int = 0
    stored: int = 0
    skipped_own: int = 0
    categories: dict[str, int] = Field(default_factory=dict)
    needs_review: int = 0


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
    retry_policy = RetryPolicy(max_attempts=2)  # a dropped IMAP connection is retried once

    def __init__(self, ctx, mailbox: Mailbox | None = None):
        super().__init__(ctx)
        self.mailbox = mailbox or (Mailbox(ctx.settings) if ctx.settings.imap_configured else None)

    def execute(self, inp: InboxInput, session: Session) -> InboxOutput:
        out = InboxOutput()
        own = {a.lower() for a in (self.settings.smtp_from, self.settings.imap_credentials[0]) if a}
        if self.mailbox is not None and self.settings.network_enabled:
            out.mailbox = f"{self.settings.imap_host} / {self.settings.imap_folder} (read-only)"
            raws, state = self.mailbox.fetch_new(kv_get(session, "inbox.state"))
            out.fetched = len(raws)
            for key, raw in raws:
                p = parse_message(raw)
                if p.from_email in own:
                    out.skipped_own += 1
                    continue
                if p.message_id and session.execute(select(InboundEmail.id).where(
                        InboundEmail.message_id == p.message_id)).first():
                    continue
                session.add(InboundEmail(
                    mailbox_uid=key, message_id=p.message_id,
                    in_reply_to=p.bounced_message_id if p.is_bounce and p.bounced_message_id else p.in_reply_to,
                    references=p.references, from_email=p.from_email, from_name=p.from_name, to_email=p.to_email,
                    subject=p.subject, received_at=p.received_at, body_text=p.body_text, new_text=new_text(p.body_text),
                    auto_submitted=p.auto_submitted, bounced_addresses=p.bounced_addresses, status="new"))
                out.stored += 1
            kv_set(session, "inbox.state", state)
            session.commit()
        out.categories = process_pending(session, self.settings, self.name)
        out.needs_review = len(session.execute(select(InboundEmail.id).where(InboundEmail.status == "needs_review")).all())
        return out
