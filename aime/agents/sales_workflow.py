"""Sales Workflow Agent: opportunities, permitted follow-ups, human tasks, and approved sends.

Rules:
- Email is drafted only for people who contacted us (consent "inquiry") or opted in.
- Companies found through research never receive automated messages; a human task is created instead.
- Every email needs human approval, outbound email must be enabled and configured, the address must
  not be suppressed, and sends happen only inside the configured business-hours window.
"""

from __future__ import annotations

import smtplib
import socket
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent, RetryPolicy
from aime.agents.contracts import BlockedItem
from aime.crm_ops import is_suppressed, log_activity
from aime.email_templates import draft_reply
from aime.integrations.email_smtp import EmailNotReady, EmailSender
from aime.knowledge.services import service_name
from aime.models import Alert, FollowUp, InboundInquiry, Lead, Opportunity, Signal
from aime.redact import redact
from aime.timeutil import as_aware_utc, utcnow

CONSENTED = ("inquiry", "opt_in")
OPEN_FOLLOWUP = ("pending", "awaiting_approval", "approved", "sending", "uncertain")


class SalesInput(BaseModel):
    now: datetime | None = None
    lead_ids: list[int] | None = None


class SalesOutput(BaseModel):
    opportunities_created: list[int] = Field(default_factory=list)
    tasks_created: int = 0
    drafts_created: int = 0
    emails_sent: int = 0
    cancelled: int = 0
    blocked: list[BlockedItem] = Field(default_factory=list)


class SalesWorkflowAgent(BaseAgent[SalesInput, SalesOutput]):
    name = "sales_workflow"
    title = "Sales Workflow Agent"
    responsibilities = [
        "Open opportunities for qualified leads who contacted us",
        "Draft reply sequences (max touches, spaced intervals) that wait for human approval",
        "Create human review tasks for research leads instead of contacting them",
        "Send only approved emails, within business hours, to non-suppressed consenting contacts",
    ]
    input_model = SalesInput
    output_model = SalesOutput
    retry_policy = RetryPolicy(max_attempts=1)  # sending email is not idempotent

    def __init__(self, ctx, sender: EmailSender | None = None):
        super().__init__(ctx)
        self.sender = sender or EmailSender(ctx.settings)

    def execute(self, inp: SalesInput, session: Session) -> SalesOutput:
        now = inp.now or utcnow()
        cfg = self.settings.section("followup")
        out = SalesOutput()
        self._recover_stale_sends(session, now)
        stmt = select(Lead).where(Lead.status == "qualified")
        if inp.lead_ids:
            stmt = stmt.where(Lead.id.in_(inp.lead_ids))
        for lead in session.execute(stmt).scalars().all():
            contact = lead.contact
            if contact and contact.consent_status in CONSENTED and contact.email:
                self._ensure_opportunity(session, lead, out)
                self._ensure_sequence(session, lead, now, cfg, out)
            else:
                self._ensure_task(session, lead, out)
        self._progress_sequences(session, now, cfg, out)
        self._send_approved(session, now, cfg, out)
        return out

    # ------------------------------------------------------------------
    def _ensure_opportunity(self, session: Session, lead: Lead, out: SalesOutput) -> None:
        exists = session.execute(select(Opportunity.id).where(Opportunity.lead_id == lead.id)).first()
        if exists:
            return
        opp = Opportunity(lead_id=lead.id, company_id=lead.company_id,
                          name=f"{lead.company.name} — {service_name(lead.primary_service)}",
                          service=lead.primary_service, stage="new", probability=10,
                          next_step="Review the drafted reply and send once approved")
        session.add(opp)
        session.flush()
        out.opportunities_created.append(opp.id)
        log_activity(session, f"Opportunity created: {opp.name}", self.name, type_="opportunity",
                     company_id=lead.company_id, lead_id=lead.id, opportunity_id=opp.id)

    def _inquiry_excerpt(self, session: Session, lead: Lead) -> str | None:
        inq = session.execute(select(InboundInquiry).where(InboundInquiry.lead_id == lead.id)
                              .order_by(InboundInquiry.id.desc())).scalars().first()
        return (inq.payload.get("message") if inq else None) or None

    def _new_draft(self, session: Session, lead: Lead, step: int, due: datetime, out: SalesOutput) -> None:
        subject, body = draft_reply(self.settings, step, lead.contact.first_name, lead.primary_service,
                                    self._inquiry_excerpt(session, lead) if step == 1 else None)
        status = "awaiting_approval" if self.settings.require_human_approval else "approved"
        session.add(FollowUp(lead_id=lead.id, contact_id=lead.contact_id, company_id=lead.company_id,
                             channel="email", step=step, due_at=due, status=status, subject=subject, body=body))
        out.drafts_created += 1
        log_activity(session, f"Drafted follow-up email #{step} (awaiting approval)", self.name, type_="email_draft",
                     company_id=lead.company_id, lead_id=lead.id)

    def _ensure_sequence(self, session: Session, lead: Lead, now: datetime, cfg: dict, out: SalesOutput) -> None:
        exists = session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email")).first()
        if not exists:
            self._new_draft(session, lead, 1, now, out)

    def _progress_sequences(self, session: Session, now: datetime, cfg: dict, out: SalesOutput) -> None:
        intervals = list(cfg.get("intervals_days", [0, 3, 7]))
        max_touches = min(int(cfg.get("max_touches", 3)), len(intervals))
        sent = session.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "sent")).scalars().all()
        latest: dict[int, FollowUp] = {}
        for f in sent:
            if f.lead_id not in latest or f.step > latest[f.lead_id].step:
                latest[f.lead_id] = f
        for lead_id, last in latest.items():
            lead = session.get(Lead, lead_id)
            if lead is None or lead.status != "qualified" or last.step >= max_touches:
                continue
            if not lead.contact or lead.contact.consent_status not in CONSENTED:
                continue
            has_open = session.execute(select(FollowUp.id).where(
                FollowUp.lead_id == lead_id, FollowUp.channel == "email", FollowUp.status.in_(OPEN_FOLLOWUP))).first()
            if has_open:
                continue
            gap = timedelta(days=intervals[last.step] - intervals[last.step - 1])
            if last.sent_at and now >= last.sent_at + gap:
                self._new_draft(session, lead, last.step + 1, now, out)

    def _ensure_task(self, session: Session, lead: Lead, out: SalesOutput) -> None:
        # A cancelled task (lead dropped below the threshold) does not block a fresh one if it re-qualifies.
        exists = session.execute(select(FollowUp.id).where(
            FollowUp.lead_id == lead.id, FollowUp.channel == "task", FollowUp.status.in_(("pending", "done")))).first()
        if exists:
            return
        evidence = session.execute(select(Signal).where(Signal.company_id == lead.company_id)
                                   .order_by(Signal.observed_at.desc())).scalars().first()
        body = (
            f"Qualified research lead (score {lead.score}, grade {lead.grade}).\n"
            f"Best-fit service: {service_name(lead.primary_service)}.\n"
            f"Evidence: {evidence.source_url if evidence else 'n/a'}\n\n"
            "This company has not contacted us, so nothing is sent automatically. Review the evidence and, "
            "if it is a fit, choose a permitted channel yourself (their published contact form, an event, "
            "or a warm introduction)."
        )
        session.add(FollowUp(lead_id=lead.id, company_id=lead.company_id, channel="task", step=1, due_at=utcnow(),
                             status="pending", subject=f"Review {lead.company.name}", body=body))
        out.tasks_created += 1

    def _in_send_window(self, now: datetime, cfg: dict) -> bool:
        tz = ZoneInfo(cfg.get("send_window_timezone", "America/New_York"))
        local = as_aware_utc(now).astimezone(tz)
        if local.weekday() >= 5:
            return False
        start = time.fromisoformat(cfg.get("send_window_start", "08:00"))
        end = time.fromisoformat(cfg.get("send_window_end", "18:00"))
        return start <= local.time() <= end

    def _send_approved(self, session: Session, now: datetime, cfg: dict, out: SalesOutput) -> None:
        approved = session.execute(select(FollowUp).where(
            FollowUp.channel == "email", FollowUp.status == "approved", FollowUp.due_at <= now)).scalars().all()
        if not approved:
            return
        ready, reason = self.sender.status()
        in_window = self._in_send_window(now, cfg)
        for f in approved:
            contact = f.contact
            lead = f.lead
            if (not contact or not contact.email or contact.consent_status not in CONSENTED
                    or is_suppressed(session, contact.email)):
                f.status = "cancelled"
                f.blocked_reason = "No consent or contact suppressed"
                out.cancelled += 1
                continue
            if lead.status != "qualified":
                f.status = "cancelled"
                f.blocked_reason = f"Lead is {lead.status}"
                out.cancelled += 1
                continue
            if not ready:
                f.blocked_reason = reason
                out.blocked.append(BlockedItem(id=f.id, reason=reason))
                continue
            if not in_window:
                f.blocked_reason = "Waiting for the business-hours send window"
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                continue
            # Durable claim before talking to the mail server: at most one send per follow-up.
            claimed = session.execute(update(FollowUp).where(FollowUp.id == f.id, FollowUp.status == "approved")
                                      .values(status="sending", send_started_at=utcnow())).rowcount
            session.commit()
            if claimed != 1:
                continue
            session.refresh(f)
            try:
                msg_id = self.sender.send(contact.email, f.subject, f.body or "")
            except (EmailNotReady, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                    smtplib.SMTPConnectError, smtplib.SMTPAuthenticationError, ConnectionRefusedError,
                    socket.gaierror) as exc:
                # Rejected or never connected: nothing was delivered, safe to try again later.
                f.status, f.send_started_at = "approved", None
                f.blocked_reason = redact(f"Send failed (not delivered): {exc}")[:300]
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                session.commit()
                continue
            except (OSError, smtplib.SMTPException) as exc:
                # Timeout or disconnect mid-conversation: the message may have been delivered.
                f.status = "uncertain"
                f.blocked_reason = redact(f"Delivery unknown ({type(exc).__name__}); check your Sent folder, "
                                          "then mark sent or requeue")[:300]
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                session.add(Alert(level="error", source=self.name, message=f"Follow-up #{f.id}: {f.blocked_reason}"))
                session.commit()
                continue
            f.status = "sent"
            f.sent_at = utcnow()
            f.message_id = msg_id
            f.blocked_reason = None
            lead.last_activity_at = f.sent_at
            out.emails_sent += 1
            log_activity(session, f"Email sent: {f.subject}", self.name, type_="email_sent", body=f.body,
                         company_id=f.company_id, lead_id=f.lead_id)
            session.commit()

    def _recover_stale_sends(self, session: Session, now: datetime) -> None:
        """A follow-up stuck in 'sending' means the process died mid-send: never resend automatically."""
        cutoff = now - timedelta(minutes=10)
        for f in session.execute(select(FollowUp).where(FollowUp.status == "sending")).scalars():
            if f.send_started_at and f.send_started_at < cutoff:
                f.status = "uncertain"
                f.blocked_reason = "Sending was interrupted; check your Sent folder, then mark sent or requeue"
                session.add(Alert(level="error", source=self.name, message=f"Follow-up #{f.id}: {f.blocked_reason}"))
        session.commit()
