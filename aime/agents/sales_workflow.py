"""Sales Workflow Agent: opportunities, permitted follow-ups, B2B outreach drafts, human tasks, approved sends.

Rules:
- Replies are drafted for people who contacted us (consent "inquiry") or opted in.
- Researched leads get a B2B outreach draft (aime.outreach) only when the owner's outreach policy is on and
  the primary contact has a business email the company publishes, with quotable evidence; otherwise a
  human review task is created instead.
- Every email needs human approval, outbound email must be enabled and configured, the address must
  not be suppressed, and sends happen only inside the configured business-hours window.
"""

from __future__ import annotations

import smtplib
import socket
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent, RetryPolicy
from aime import automation
from aime.agents.contracts import BlockedItem
from aime.crm_ops import is_suppressed, log_activity
from aime.email_templates import draft_reply
from aime.integrations.email_smtp import EmailNotReady, EmailSender, compliance_problem, new_message_id
from aime.knowledge.services import service_name
from aime.models import Alert, FollowUp, InboundEmail, InboundInquiry, Lead, Opportunity, Signal
from aime.outreach import STOP_STATUSES, draft_outreach, eligible, gather_evidence, record_delivery, set_status
from aime.pipeline import pipeline_checks
from aime.outreach import policy as outreach_policy
from aime.qc.email_checks import run_email_checks
from aime.reply_intel import QUESTION_PLACEHOLDER
from aime.redact import redact
from aime.timeutil import as_aware_utc, utcnow

CONSENTED = ("inquiry", "opt_in")
OPEN_FOLLOWUP = ("pending", "awaiting_approval", "approved", "sending", "uncertain")
COUNTS_AS_SENT = ("sent", "sending", "uncertain")  # for the daily limits: an uncertain email may have gone out
SEND_BUSY = "Another run is sending emails right now; this one goes out on the next run"


class SalesInput(BaseModel):
    now: datetime | None = None
    lead_ids: list[int] | None = None


class SalesOutput(BaseModel):
    opportunities_created: list[int] = Field(default_factory=list)
    tasks_created: int = 0
    drafts_created: int = 0
    outreach_drafts: int = 0
    emails_sent: int = 0
    cancelled: int = 0
    pipeline: dict[str, int] = Field(default_factory=dict)
    blocked: list[BlockedItem] = Field(default_factory=list)


class SalesWorkflowAgent(BaseAgent[SalesInput, SalesOutput]):
    name = "sales_workflow"
    title = "Sales Workflow Agent"
    responsibilities = [
        "Open opportunities for qualified leads who contacted us",
        "Draft reply sequences (max touches, spaced intervals) that wait for human approval",
        "Draft evidence-based B2B outreach for researched decision-makers with published business emails",
        "Create human review tasks for research leads that cannot be contacted",
        "Send only approved emails, within business hours, to non-suppressed consenting contacts",
    ]
    input_model = SalesInput
    output_model = SalesOutput
    retry_policy = RetryPolicy(max_attempts=1)  # sending email is not idempotent

    def __init__(self, ctx, sender: EmailSender | None = None):
        super().__init__(ctx)
        self.sender = sender or EmailSender(ctx.settings)

    def _use_primary_mailbox(self, session: Session) -> None:
        """A primary mailbox from the Super Admin panel sends instead of the .env mailbox. Every switch and
        approval stays the same: OUTBOUND_EMAIL_ENABLED off still sends nothing."""
        from aime import mail_accounts

        a = mail_accounts.primary(session)
        if a is None:
            return
        self.sender.settings = mail_accounts.settings_for(self.settings, a)
        if a.auth == "oauth":
            from aime.integrations import oauth

            def token() -> str:
                with self.ctx.http() as http:
                    return oauth.access_token(session, self.settings, http, "microsoft", "mail", account=a.id)
            self.sender.token = token

    def _connect_oauth(self, session: Session) -> None:
        """MAIL_AUTH=oauth: give the sender a way to get a fresh access token in this run (never stored on it)."""
        if self.settings.mail_auth != "oauth" or self.sender.token is not None:
            return
        from aime.integrations import oauth

        def token() -> str:
            with self.ctx.http() as http:
                return oauth.access_token(session, self.settings, http, self.settings.mail_oauth_provider, "mail")
        self.sender.token = token

    def execute(self, inp: SalesInput, session: Session) -> SalesOutput:
        self._use_primary_mailbox(session)
        self._connect_oauth(session)
        now = inp.now or utcnow()
        cfg = self.settings.section("followup")
        out = SalesOutput()
        self._recover_stale_sends(session, now)
        stmt = select(Lead).where(Lead.status == "qualified").order_by(Lead.score.desc(), Lead.id)
        if inp.lead_ids:
            stmt = stmt.where(Lead.id.in_(inp.lead_ids))
        budget = outreach_policy(self.settings)["drafts_per_run"]
        for lead in session.execute(stmt).scalars().all():
            contact = lead.contact
            if contact and contact.consent_status in CONSENTED and contact.email:
                self._ensure_opportunity(session, lead, out)
                self._ensure_sequence(session, lead, now, cfg, out)
                continue
            ok, _ = eligible(session, self.settings, lead)
            if ok:
                result = self._ensure_outreach(session, lead, now, out, budget)
                budget -= int(result == "created")
                if result in ("created", "exists", "budget"):
                    continue
            self._ensure_task(session, lead, out)
        self._progress_sequences(session, now, cfg, out)
        self._send_approved(session, now, cfg, out)
        out.pipeline = pipeline_checks(session, self.settings, now, self.name)
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

    def _new_draft(self, session: Session, lead: Lead, step: int, due: datetime, out: SalesOutput,
                   in_reply_to: str | None = None) -> None:
        subject, body = draft_reply(self.settings, step, lead.contact.first_name, lead.primary_service,
                                    self._inquiry_excerpt(session, lead) if step == 1 else None)
        # Every draft waits for approval: a person's, or at send time an active campaign authorization (aime/automation.py).
        session.add(FollowUp(lead_id=lead.id, contact_id=lead.contact_id, company_id=lead.company_id,
                             channel="email", step=step, due_at=due, status="awaiting_approval",
                             subject=subject, body=body, in_reply_to=in_reply_to))
        out.drafts_created += 1
        log_activity(session, f"Drafted follow-up email #{step} (awaiting approval)", self.name, type_="email_draft",
                     company_id=lead.company_id, lead_id=lead.id)

    def _ensure_outreach(self, session: Session, lead: Lead, now: datetime, out: SalesOutput, budget: int) -> str:
        """created | exists (already in a sequence) | budget (cap reached this run) | no_evidence"""
        if session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email")).first():
            return "exists"
        if budget <= 0:
            return "budget"
        evidence = gather_evidence(session, lead)
        if not evidence:
            return "no_evidence"
        self._new_outreach(session, lead, 1, now, [e.as_dict() for e in evidence], evidence, out)
        for task in session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "task",
                                                           FollowUp.status == "pending")).scalars():
            task.status, task.blocked_reason = "done", "Outreach email drafted; it is waiting for your approval"
        set_status(session, lead, "drafted", self.name)
        return "created"

    def _new_outreach(self, session: Session, lead: Lead, step: int, due: datetime, evidence_dicts: list[dict],
                      evidence, out: SalesOutput, in_reply_to: str | None = None) -> None:
        contact = lead.contact
        subject, body = draft_outreach(self.settings, step, lead, contact, evidence)
        suppressed = is_suppressed(session, contact.email, lead.company.domain)
        qc = run_email_checks(subject, body, evidence_dicts, contact, suppressed, step=step)
        # Every draft waits for approval: a person's, or at send time an active campaign authorization (aime/automation.py).
        session.add(FollowUp(lead_id=lead.id, contact_id=contact.id, company_id=lead.company_id, channel="email",
                             kind="outreach", step=step, due_at=due, status="awaiting_approval", subject=subject,
                             body=body, to_email=contact.email, evidence=evidence_dicts, quality={"qc": qc},
                             in_reply_to=in_reply_to))
        out.outreach_drafts += 1
        log_activity(session, f"Drafted outreach email #{step} to {contact.email} (awaiting approval; checks "
                     f"{qc['status']})", self.name, type_="email_draft", company_id=lead.company_id, lead_id=lead.id)

    def _ensure_sequence(self, session: Session, lead: Lead, now: datetime, cfg: dict, out: SalesOutput) -> None:
        exists = session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email")).first()
        if not exists:
            self._new_draft(session, lead, 1, now, out)

    def _progress_sequences(self, session: Session, now: datetime, cfg: dict, out: SalesOutput) -> None:
        pol = outreach_policy(self.settings)
        sent = session.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "sent")).scalars().all()
        latest: dict[int, FollowUp] = {}
        for f in sent:
            if f.lead_id not in latest or f.step > latest[f.lead_id].step:
                latest[f.lead_id] = f
        for lead_id, last in latest.items():
            lead = session.get(Lead, lead_id)
            outreach = last.kind == "outreach"
            intervals = list(pol["intervals"] if outreach else cfg.get("intervals_days", [0, 3, 7]))
            max_touches = min(pol["max_touches"] if outreach else int(cfg.get("max_touches", 3)), len(intervals))
            if lead is None or lead.status != "qualified" or last.step >= max_touches:
                continue
            if outreach:
                if lead.outreach_status != "contacted" or not eligible(session, self.settings, lead)[0]:
                    continue  # replied, unsubscribed, bounced, stopped, or no longer reachable
            elif (not lead.contact or lead.contact.consent_status not in CONSENTED
                    or lead.outreach_status in STOP_STATUSES):
                continue  # they replied, booked, unsubscribed, bounced, or a person stopped the sequence
            has_open = session.execute(select(FollowUp.id).where(
                FollowUp.lead_id == lead_id, FollowUp.channel == "email", FollowUp.status.in_(OPEN_FOLLOWUP))).first()
            if has_open:
                continue
            gap = timedelta(days=intervals[last.step] - intervals[last.step - 1])
            if last.sent_at and now >= last.sent_at + gap:
                # The next step answers the last one we sent, so it stays in the same conversation.
                if outreach:
                    self._new_outreach(session, lead, last.step + 1, now, list(last.evidence or []), [], out,
                                       in_reply_to=last.message_id)
                else:
                    self._new_draft(session, lead, last.step + 1, now, out, in_reply_to=last.message_id)

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
        # "approved" without an approval record (e.g. set directly in the database) is not an approval.
        for f in session.execute(select(FollowUp).where(
                FollowUp.channel == "email", FollowUp.status == "approved", FollowUp.approved_at.is_(None))).scalars():
            f.status = "awaiting_approval"
            f.blocked_reason = "Needs a person's approval before it can be sent"
            out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
        due = select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "approved",
                                     FollowUp.approved_at.is_not(None), FollowUp.due_at <= now)
        ready, _ = self.sender.status()
        automation.observe_sending(session, self.settings.outbound_email_enabled, now)  # the switch, not readiness
        state = automation.get_state(session)
        if state != "running":  # paused or stopped in the Automation Control Center: nothing goes out
            for f in session.execute(due).scalars():
                f.blocked_reason = (f"Not sent: automation {automation.STATES[state].split(':')[0].lower()} "
                                    "in the Automation Control Center")
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
            return
        # Hybrid/Automatic mode: an active campaign authorization may approve waiting emails, at send time only
        # only inside the send window: an approval that has to wait would still count against the campaign's limits
        by_campaign = (ready and not self.settings.require_human_approval and automation.get_mode(session) != "approval"
                       and self._in_send_window(now, cfg) and bool(automation.active_authorizations(session, now)))
        if not by_campaign and not session.execute(due.limit(1)).first():
            return
        from aime.scheduler.runner import FileLock  # here, not at import time: the runner imports the agents

        # Several jobs run this agent (research, inbound, followups), each under its own job lock. One send
        # lock across them keeps the daily and per-domain limits true when two runs overlap.
        lock = FileLock(self.settings.locks_dir / "send.lock", stale_seconds=1800)
        if not lock.acquire():
            for f in session.execute(due).scalars():
                out.blocked.append(BlockedItem(id=f.id, reason=SEND_BUSY))
            return
        try:
            if by_campaign:
                automation.apply_campaign_approvals(session, self.settings, now, self.name)
                session.flush()
            self._send_locked(session, session.execute(due).scalars().all(), now, cfg, out)
        finally:
            lock.release()

    def _send_locked(self, session: Session, approved: list[FollowUp], now: datetime, cfg: dict,
                     out: SalesOutput) -> None:
        ready, reason = self.sender.status()
        in_window = self._in_send_window(now, cfg)
        limits = self._outreach_limits(session, now, cfg)
        for f in approved:
            contact = f.contact
            lead = f.lead
            test_auth = None  # the authorization a given test contact's email goes out under
            gone = automation.campaign_approval_problem(session, f, now, self.settings)  # holds while the campaign does
            if gone:
                automation.return_to_person(f, gone)
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                continue
            if f.kind == "response":
                why = self._response_problem(session, f)
                if why:
                    f.status, f.blocked_reason = "cancelled", f"Not sent: {why}"[:300]
                    out.cancelled += 1
                    continue
            elif f.kind == "outreach":
                if automation.is_given_test_contact(contact):  # an address the owner gave for testing, never published
                    test_auth, test_why = automation.test_contact_authorization(session, f, now)
                    if test_auth is None:  # held, not cancelled: an authorization may still be activated for it
                        f.blocked_reason = f"Not sent: test contact; {test_why}"[:300]
                        out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                        continue
                    ok, why = True, ""
                    if (f.to_email or "").lower() != (contact.email or "").lower():
                        ok, why = False, "the address changed after approval; draft a new email"
                else:
                    ok, why = eligible(session, self.settings, lead, for_send=True)
                    if ok and (lead.contact_id != f.contact_id or (f.to_email or "").lower() != (contact.email or "").lower()):
                        ok, why = False, "the contact or address changed after approval; draft a new email"
                    if not ok and why.startswith("B2B outreach is turned off"):  # a pause, not a decision about this email
                        f.blocked_reason = f"Held: {why}; it goes out when outreach is turned on again"[:300]
                        out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                        continue
                if not ok:
                    f.status, f.blocked_reason = "cancelled", f"Not sent: {why}"[:300]
                    out.cancelled += 1
                    continue
                wait = self._over_limit(f, limits)
                if wait:
                    f.blocked_reason = wait
                    out.blocked.append(BlockedItem(id=f.id, reason=wait))
                    continue
            elif (not contact or not contact.email or contact.consent_status not in CONSENTED
                    or is_suppressed(session, contact.email)):
                f.status = "cancelled"
                f.blocked_reason = "No consent or contact suppressed"
                out.cancelled += 1
                continue
            elif f.step > 1 and lead.outreach_status in STOP_STATUSES:  # step 1 answers a new inquiry: always fine
                f.status, f.blocked_reason = "cancelled", f"Not sent: the sequence stopped ({lead.outreach_status})"
                out.cancelled += 1
                continue
            if lead.status != "qualified" and f.kind != "response" and test_auth is None:
                f.status = "cancelled"
                f.blocked_reason = f"Lead is {lead.status}"
                out.cancelled += 1
                continue
            if not ready:
                f.blocked_reason = reason
                out.blocked.append(BlockedItem(id=f.id, reason=reason))
                continue
            if automation.needs_reconfirmation(session, f):  # approved while sending was off: not released by switching it on
                f.blocked_reason = "Approved before sending was switched on: confirm it again on Follow-ups before it is sent"
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                continue
            problem = compliance_problem(self.settings, f.body or "")  # every kind: outreach, responses, inquiry replies
            if problem:  # e.g. the footer was edited out, or written before the postal address was configured
                f.status, f.approved_at, f.approved_by = "awaiting_approval", None, None
                f.blocked_reason = f"Not sent: {problem}; put the footer back and approve again"[:300]
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                continue
            if not in_window:
                f.blocked_reason = "Waiting for the business-hours send window"
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                continue
            if limits["mailbox_sent"] >= limits["mailbox_daily"]:  # every kind of email counts for the mailbox
                f.blocked_reason = (f"The sales mailbox has sent {limits['mailbox_daily']} emails in 24 hours "
                                    "(its limit); it goes out when the count drops")
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                continue
            if test_auth is not None:  # counts against, and is checked again with, the authorization
                f.authorization_id = test_auth.id
                qc = ((f.quality or {}).get("qc") or {}).get("status", "not checked")
                automation.audit(session, self.name, "test_contact_send", f"follow-up #{f.id}",
                                 f"to {f.to_email} under campaign #{test_auth.id} {test_auth.name}; approved by "
                                 f"{f.approved_by}; quality checks {qc}")
            # Durable claim before talking to the mail server: at most one send per follow-up. The Message-ID is
            # stored with it, so a reply to an email whose delivery was never confirmed still finds its thread.
            claimed = session.execute(update(FollowUp).where(FollowUp.id == f.id, FollowUp.status == "approved")
                                      .values(status="sending", send_started_at=utcnow(),
                                              message_id=f.message_id or new_message_id(self.settings))).rowcount
            session.commit()
            if claimed != 1:
                continue
            session.refresh(f)
            try:
                msg_id = self.sender.send(f.to_email or contact.email, f.subject, f.body or "", in_reply_to=f.in_reply_to,
                                          message_id=f.message_id, references=self._references(session, f))
            except (EmailNotReady, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                    smtplib.SMTPConnectError, smtplib.SMTPAuthenticationError, ConnectionRefusedError,
                    socket.gaierror) as exc:
                # Rejected or never connected: nothing was delivered, safe to try again later.
                f.status, f.send_started_at = "approved", None
                f.blocked_reason = redact(f"Send failed (not delivered): {exc}")[:300]
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                session.commit()
                continue
            except ValueError as exc:
                # The message could not be built (e.g. a malformed header): nothing left this computer.
                f.status, f.send_started_at = "awaiting_approval", None
                f.approved_at, f.approved_by = None, None
                f.blocked_reason = redact(f"Could not be sent: {exc}; edit it and approve again")[:300]
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
            f.message_id = msg_id
            record_delivery(session, f, utcnow(), self.sender.recipient_for(f.to_email or contact.email)[0], self.name)
            limits["mailbox_sent"] += 1
            if f.kind == "outreach":
                limits["sent_today"] += 1
                limits["run"] += 1
                domain = (f.to_email or "").split("@")[-1]
                limits["domains"][domain] = limits["domains"].get(domain, 0) + 1
            out.emails_sent += 1
            log_activity(session, f"Email sent: {f.subject}", self.name, type_="email_sent", body=f.body,
                         company_id=f.company_id, lead_id=f.lead_id)
            session.commit()

    def _outreach_limits(self, session: Session, now: datetime, cfg: dict) -> dict:
        """Outreach already sent today (in the send-window time zone), overall and per recipient domain.
        Emails being sent or of unknown outcome count too: they may have been delivered."""
        pol = self.settings.section("outreach")
        tz = ZoneInfo(cfg.get("send_window_timezone", "America/New_York"))
        local = as_aware_utc(now).astimezone(tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        sent = session.execute(select(FollowUp.to_email).where(
            FollowUp.kind == "outreach", FollowUp.status.in_(COUNTS_AS_SENT),
            func.coalesce(FollowUp.sent_at, FollowUp.send_started_at) >= start)).scalars().all()
        domains: dict[str, int] = {}
        for addr in sent:
            d = (addr or "").split("@")[-1]
            domains[d] = domains.get(d, 0) + 1
        mailbox_sent = session.execute(select(func.count(FollowUp.id)).where(
            FollowUp.channel == "email", FollowUp.status.in_(COUNTS_AS_SENT),
            func.coalesce(FollowUp.sent_at, FollowUp.send_started_at) >= now - timedelta(hours=24))).scalar_one()
        return {"sent_today": len(sent), "domains": domains, "run": 0, "mailbox_sent": mailbox_sent,
                "mailbox_daily": int(pol.get("mailbox_daily_limit", 50)),
                "daily": int(pol.get("daily_send_limit", 20)), "per_domain": int(pol.get("per_domain_daily_limit", 1)),
                "per_run": int(pol.get("sends_per_run", 5))}

    @staticmethod
    def _over_limit(f: FollowUp, limits: dict) -> str | None:
        if limits["sent_today"] >= limits["daily"]:
            return f"Daily outreach limit reached ({limits['daily']}); it will go out on the next business day"
        if limits["run"] >= limits["per_run"]:
            return f"Sending at most {limits['per_run']} outreach emails per run; it will go out on the next run"
        domain = (f.to_email or "").split("@")[-1]
        if limits["domains"].get(domain, 0) >= limits["per_domain"]:
            return f"Already emailed {domain} today (limit {limits['per_domain']}); it will go out tomorrow"
        return None

    @staticmethod
    def _references(session: Session, f: FollowUp) -> str | None:
        """The thread's Message-IDs, oldest first, ending with the email this one answers (References header)."""
        if not f.in_reply_to:
            return None
        if f.kind == "response":
            row = session.execute(select(InboundEmail).where(InboundEmail.message_id == f.in_reply_to)).scalars().first()
            chain = (row.references or "").split() if row else []
        else:  # earlier steps of the same sequence
            same_kind = FollowUp.kind.is_(None) if f.kind is None else FollowUp.kind == f.kind
            chain = [m for m in session.execute(select(FollowUp.message_id).where(
                FollowUp.lead_id == f.lead_id, FollowUp.channel == "email", FollowUp.status == "sent", same_kind,
                FollowUp.step < f.step).order_by(FollowUp.step)).scalars() if m]
        return " ".join(list(dict.fromkeys([*chain, f.in_reply_to]))[-10:])

    @staticmethod
    def _response_problem(session: Session, f: FollowUp) -> str | None:
        """A response may go only to someone whose email we received, in that thread, and never if suppressed."""
        if not f.in_reply_to or not f.to_email:
            return "a response must answer an email we received"
        received = session.execute(select(InboundEmail.id).where(
            InboundEmail.message_id == f.in_reply_to, func.lower(InboundEmail.from_email) == f.to_email.lower())).first()
        if not received:
            return "no email from this address to answer"
        if is_suppressed(session, f.to_email):
            return "on the do-not-contact list"
        if QUESTION_PLACEHOLDER in (f.body or ""):
            return "the answer placeholder was not filled in"
        return None

    def _recover_stale_sends(self, session: Session, now: datetime) -> None:
        """A follow-up stuck in 'sending' means the process died mid-send: never resend automatically."""
        cutoff = now - timedelta(minutes=10)
        for f in session.execute(select(FollowUp).where(FollowUp.status == "sending")).scalars():
            if f.send_started_at and f.send_started_at < cutoff:
                f.status = "uncertain"
                f.blocked_reason = "Sending was interrupted; check your Sent folder, then mark sent or requeue"
                session.add(Alert(level="error", source=self.name, message=f"Follow-up #{f.id}: {f.blocked_reason}"))
        session.commit()
