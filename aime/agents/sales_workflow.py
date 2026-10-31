"""Sales Workflow Agent: opportunities, permitted follow-ups, B2B outreach drafts, human tasks, approved sends.

Rules:
- Replies are drafted for people who contacted us (consent "inquiry") or opted in, whatever their grade: one whom
  only the score put in nurture is still answered (that draft waits for a person; rule campaigns cover grade B up).
- Researched leads get a B2B outreach draft (aime.outreach) only when the owner's outreach policy is on and
  the primary contact has a business email the company publishes, with quotable evidence; otherwise a
  human review task is created instead.
- Every email needs human approval, outbound email must be enabled and configured, the address must
  not be suppressed, and sends happen only inside the configured business-hours window.
"""

from __future__ import annotations

import re
import smtplib
import socket
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent, RetryPolicy
from aime import automation, send_journal
from aime.agents.contracts import BlockedItem
from aime.agents.qualification import LeadQualificationAgent
from aime.calendar_slots import holiday_on
from aime.crm_ops import is_suppressed, log_activity, suppress
from aime.email_templates import draft_reply
from aime.integrations.email_smtp import EmailNotReady, EmailSender, compliance_problem, new_message_id
from aime.knowledge.content_bank import BRAND_LINES
from aime.knowledge.services import service_name
from aime.models import (Activity, Alert, Contact, FollowUp, InboundEmail, InboundInquiry, Lead, Opportunity, ResearchSnapshot,
                         Signal)
from aime.monitoring import alert_once, resolve_alerts
from aime.outreach import (OPEN, RUNNING_STAGES, STOP_STATUSES, TOPICS, draft_outreach, draft_reengage, eligible, gather_evidence,
                           record_delivery, set_status, stop_sequence)
from aime.people import looks_like_name
from aime.pipeline import pipeline_checks
from aime.outreach import policy as outreach_policy
from aime.qc.email_checks import run_email_checks
from aime.reply_intel import QUESTION_PLACEHOLDER, lead_on_hold
from aime.redact import redact
from aime.timeutil import as_aware_utc, utcnow

CONSENTED = ("inquiry", "opt_in")
OPEN_FOLLOWUP = ("pending", "awaiting_approval", "approved", "sending", "uncertain")
COUNTS_AS_SENT = ("sent", "sending", "uncertain")  # for the daily limits: an uncertain email may have gone out
SEND_BUSY = "Another run is sending emails right now; this one goes out on the next run"
JOURNAL_ALERT = "sending:journal"
# What certainly delivered nothing: the server refused it, it never connected, or the message could not be built.
NOT_DELIVERED = (smtplib.SMTPRecipientsRefused, EmailNotReady, smtplib.SMTPSenderRefused, smtplib.SMTPConnectError,
                 smtplib.SMTPAuthenticationError, ConnectionRefusedError, socket.gaierror, ValueError)
# A refusal that says the address itself does not exist (RFC 3463 5.1.x, not those about our own sender address).
DEAD_ADDRESS = re.compile(r"\b5\.1\.(0|1|2|3|6|10)\b|no such|user unknown|unknown user|does not exist|address not found|"
                          r"no mailbox|mailbox not found|recipient not found|invalid recipient", re.I)
SIGN_IN_ALERT = "mail:sign_in"
MAILBOX_ALERT = "mail:not_read"
REVIEW_MARK = "Why no email was drafted:"  # in every review task written since this note was added


class SalesInput(BaseModel):
    now: datetime | None = None
    lead_ids: list[int] | None = None
    send: bool = True  # False: drafts and tasks only (research and inbound leave the sending to the followups job)
    hold_sends: str | None = None  # why approved emails must wait this run (the mailbox could not be read)


class SalesOutput(BaseModel):
    opportunities_created: list[int] = Field(default_factory=list)
    tasks_created: int = 0
    drafts_created: int = 0
    outreach_drafts: int = 0
    reengage_drafts: int = 0
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
        # Research, inbound and followups each run this agent under their own job lock: two runs drafting at once would
        # both find "no email yet" and write two first-touch emails for one person. The other run drafts on its next turn.
        from aime.scheduler.runner import FileLock  # here, not at import time: the runner imports the agents

        lock = FileLock(self.settings.locks_dir / "drafts.lock", stale_seconds=3600)
        if lock.acquire():
            try:
                self._draft(session, inp, now, cfg, out)
                session.commit()  # before the lock opens: the next run's "no email yet" check must see these
            finally:
                lock.release()
        else:
            self.log.info("Another run is drafting right now: this one only sends")
        self._send_approved(session, now, cfg, out, inp.send, inp.hold_sends)
        out.pipeline = pipeline_checks(session, self.settings, now, self.name)
        return out

    def _draft(self, session: Session, inp: SalesInput, now: datetime, cfg: dict, out: SalesOutput) -> None:
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
            ok, _ = eligible(session, self.settings, lead, running=lead.outreach_status == "contacted")
            if ok:
                result = self._ensure_outreach(session, lead, now, out, budget)
                budget -= int(result == "created")
                if result in ("created", "exists", "budget"):
                    continue
            self._ensure_task(session, lead, out)
        # People who asked us but scored below the qualification threshold still get an answer.
        parked = select(Lead).where(Lead.status == "nurture", Lead.source == "inbound").order_by(Lead.id)
        if inp.lead_ids:
            parked = parked.where(Lead.id.in_(inp.lead_ids))
        for lead in session.execute(parked).scalars().all():
            if self._may_answer(session, lead):
                self._ensure_sequence(session, lead, now, cfg, out)
        self._reengage(session, now, out, budget, inp.lead_ids)
        self._progress_sequences(session, now, cfg, out)

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
        from aime.stages import opened

        opened(session, opp, self.name, "a qualified lead who contacted us")
        out.opportunities_created.append(opp.id)
        log_activity(session, f"Opportunity created: {opp.name}", self.name, type_="opportunity",
                     company_id=lead.company_id, lead_id=lead.id, opportunity_id=opp.id)

    def _may_answer(self, session: Session, lead: Lead) -> bool:
        """Whether the inquiry sequence may write to this lead: a qualified one, or a person who asked us (an
        inquiry, a LinkedIn form or event registration, with consent) whom only the score put in nurture. Never a
        disqualified lead, nor one that a person, a "not interested" reply or the stale-lead rule parked."""
        if lead.status == "qualified":
            return True
        contact = lead.contact
        if (lead.status != "nurture" or lead.source != "inbound" or not contact or not contact.email
                or contact.consent_status not in CONSENTED):
            return False
        # The last recorded move must be the score's own "new → nurture": a "not interested" reply parks a
        # qualified lead without a record of its own, so its last record still reads "→ qualified".
        last = session.execute(select(Activity).where(Activity.lead_id == lead.id, Activity.type == "status_change")
                               .order_by(Activity.created_at.desc(), Activity.id.desc())).scalars().first()
        return last is not None and last.actor == LeadQualificationAgent.name and "→ nurture" in (last.subject or "")

    def _inquiry(self, session: Session, lead: Lead) -> InboundInquiry | None:
        return session.execute(select(InboundInquiry).where(InboundInquiry.lead_id == lead.id)
                               .order_by(InboundInquiry.id.desc())).scalars().first()

    def _new_draft(self, session: Session, lead: Lead, step: int, due: datetime, out: SalesOutput,
                   in_reply_to: str | None = None) -> None:
        inq = self._inquiry(session, lead)  # its message is quoted in the first reply; its source names an event
        subject, body = draft_reply(self.settings, step, lead.contact.first_name, lead.primary_service,
                                    (inq.payload.get("message") or None) if inq and step == 1 else None,
                                    source_page=inq.payload.get("source_page") if inq else None)
        generator = "template"
        if step == 1 and getattr(self.settings, "ai_email_drafts", False) and inq is not None:
            from aime.ai_drafts import opening

            line = opening(self.ctx.llm, lead.contact.first_name, lead.company.name, service_name(lead.primary_service),
                           inq.payload.get("message"))
            first = body.split("\n\n", 2)  # greeting, the template's opening, the rest
            if line and len(first) == 3:
                body = f"{first[0]}\n\n{line}\n\n{first[2]}"
                generator = f"AI ({self.ctx.llm.label}) opening + template"
        # Every draft waits for approval: a person's, or at send time an active campaign authorization (aime/automation.py).
        session.add(FollowUp(lead_id=lead.id, contact_id=lead.contact_id, company_id=lead.company_id,
                             channel="email", step=step, due_at=due, status="awaiting_approval",
                             subject=subject, body=body, in_reply_to=in_reply_to, generator=generator[:80]))
        out.drafts_created += 1
        log_activity(session, f"Drafted follow-up email #{step} (awaiting approval)", self.name, type_="email_draft",
                     company_id=lead.company_id, lead_id=lead.id)

    def _ensure_outreach(self, session: Session, lead: Lead, now: datetime, out: SalesOutput, budget: int) -> str:
        """created | exists (already in a sequence) | budget (cap reached this run) | no_evidence. An email that was
        cancelled is no sequence: the reason it fell away may be gone (a competitor flag cleared, a new address)."""
        if session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                     FollowUp.status != "cancelled")).first():
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

    def _reengage(self, session: Session, now: datetime, out: SalesOutput, budget: int,
                  lead_ids: list[int] | None = None) -> None:
        """One re-engagement email per lead, ever: only a lead whose whole outreach sequence went unanswered
        (outreach stage no_response, parked in nurture), [outreach] reengage_after_days after the last email, and
        still reachable. It waits for a person like every outreach email; campaigns never approve it."""
        pol = outreach_policy(self.settings)
        days = pol["reengage_after_days"]
        if days <= 0 or budget <= 0:
            return
        stmt = select(Lead).where(Lead.status == "nurture", Lead.outreach_status == "no_response").order_by(Lead.id)
        if lead_ids:
            stmt = stmt.where(Lead.id.in_(lead_ids))
        for lead in session.execute(stmt).scalars().all():
            if out.reengage_drafts >= budget:
                return
            last = session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                          FollowUp.sent_at.is_not(None))
                                   .order_by(FollowUp.sent_at.desc())).scalars().first()
            if last is None or now - last.sent_at < timedelta(days=days):
                continue
            if session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, or_(
                    FollowUp.purpose == "reengage",
                    and_(FollowUp.channel == "email", FollowUp.status.in_(OPEN_FOLLOWUP))))).first():
                continue  # re-engaged once already, or an email is still open
            if not eligible(session, self.settings, lead, reengage=True)[0]:
                continue  # opted out, suppressed, bounced, competitor, or the address is no longer published
            contact = lead.contact
            subject, body = draft_reengage(self.settings, lead, contact)
            step = pol["max_touches"] + 1
            qc = run_email_checks(subject, body, [], contact, is_suppressed(session, contact.email, lead.company.domain),
                                  step=step)
            session.add(FollowUp(lead_id=lead.id, contact_id=contact.id, company_id=lead.company_id, channel="email",
                                 kind="outreach", purpose="reengage", step=step, due_at=now,
                                 status="awaiting_approval", subject=subject, body=body, to_email=contact.email,
                                 evidence=[], quality={"qc": qc}, in_reply_to=last.message_id))
            set_status(session, lead, "reengage", self.name, f"no reply {(now - last.sent_at).days} days after the last email")
            log_activity(session, f"Drafted the re-engagement email to {contact.email} (awaiting approval; checks "
                         f"{qc['status']})", self.name, type_="email_draft", company_id=lead.company_id, lead_id=lead.id)
            out.reengage_drafts += 1

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
            if lead is None or last.step >= max_touches:
                continue
            if outreach:
                if (lead.status != "qualified" or lead.outreach_status not in RUNNING_STAGES
                        or not eligible(session, self.settings, lead, running=True)[0]):
                    continue  # replied, unsubscribed, bounced, stopped, or no longer reachable
            elif (not self._may_answer(session, lead) or not lead.contact
                    or lead.contact.consent_status not in CONSENTED or lead.outreach_status in STOP_STATUSES):
                continue  # they replied, booked, unsubscribed, bounced, or a person stopped the sequence
            has_open = session.execute(select(FollowUp.id).where(
                FollowUp.lead_id == lead_id, FollowUp.channel == "email", FollowUp.status.in_(OPEN_FOLLOWUP))).first()
            if has_open:
                continue
            same_kind = FollowUp.kind == "outreach" if outreach else FollowUp.kind.is_(None)
            if session.execute(select(FollowUp.id).where(
                    FollowUp.lead_id == lead_id, FollowUp.channel == "email", same_kind, FollowUp.step > last.step,
                    FollowUp.status == "rejected")).first():
                continue  # a person said no to the next step: a rejection decides the email, it is not drafted again
            gap = timedelta(days=intervals[last.step] - intervals[last.step - 1])
            if last.sent_at and now >= last.sent_at + gap:
                # The next step answers the last one we sent, so it stays in the same conversation.
                if outreach:
                    self._new_outreach(session, lead, last.step + 1, now, list(last.evidence or []), [], out,
                                       in_reply_to=last.message_id)
                else:
                    self._new_draft(session, lead, last.step + 1, now, out, in_reply_to=last.message_id)

    def _ensure_task(self, session: Session, lead: Lead, out: SalesOutput) -> None:
        if session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                     FollowUp.status.in_(OPEN))).first():
            return  # an email is waiting: that is the next step
        tasks = session.execute(select(FollowUp).where(
            FollowUp.lead_id == lead.id, FollowUp.channel == "task", FollowUp.status.in_(("pending", "done")))).scalars().all()
        for task in tasks:
            if task.status == "pending" and task.subject.startswith("Review ") and REVIEW_MARK not in (task.body or ""):
                task.body = self._review_body(session, lead)  # written before the task said why: bring it up to date
        # A cancelled task (lead dropped below the threshold) does not block a fresh one if it re-qualifies, nor does one
        # the drafting closed, if that email later fell away. One a person closed stays closed.
        if any(t.status == "pending" or not t.blocked_reason or lead.outreach_status in STOP_STATUSES for t in tasks):
            return
        session.add(FollowUp(lead_id=lead.id, company_id=lead.company_id, channel="task", step=1, due_at=utcnow(),
                             status="pending", subject=f"Review {lead.company.name}", body=self._review_body(session, lead)))
        out.tasks_created += 1

    def _review_body(self, session: Session, lead: Lead) -> str:
        """What a person needs to take a dead end further by hand: why no email, what was read, who was found, where to
        write, and a message to paste. No address is guessed, and the person does the sending."""
        contact, company = lead.contact, lead.company
        evidence = session.execute(select(Signal).where(Signal.company_id == lead.company_id)
                                   .order_by(Signal.observed_at.desc())).scalars().first()
        snap = session.execute(select(ResearchSnapshot).where(ResearchSnapshot.company_id == lead.company_id)
                               .order_by(ResearchSnapshot.id.desc())).scalars().first()
        if not contact or not contact.email:
            why = "no published business email"
        else:
            why = eligible(session, self.settings, lead)[1] or "there is nothing quotable on record to write from"
        why = re.sub(r"\s*\(\[[^)]*\)", "", why)  # the setting's name means nothing here
        if snap is None:
            read = "its website has not been read yet"
        elif snap.status in ("done", "no_people"):
            read = f"{len(snap.pages or [])} page(s) of its website on {snap.fetched_at:%d %b %Y}"
        elif snap.status == "no_website":
            read = "nothing: no website is known for this company"
        else:
            read = f"nothing: its website could not be read on {snap.fetched_at:%d %b %Y} ({snap.status})"
        name, title, source = self._person_found(session, lead, snap)
        person = f"{name}{f', {title}' if title else ''}{f' ({source})' if source else ''}" if name else "nobody named"
        page = next((p.get("url") for p in (snap.pages if snap else []) or []
                     if p.get("kind") == "contact" and p.get("status") == "ok" and p.get("url")), None)
        topic = TOPICS.get(lead.primary_service or "", "your operations")
        brand = BRAND_LINES.get(lead.primary_service or "", "At {company}, we build software for teams like yours.")
        greeting = f"Hi {name.split()[0]}," if name else f"Hello {company.name} team,"
        sender = self.settings.sender_name or f"The {self.settings.company_name} team"
        return (
            f"Qualified research lead (score {lead.score}, grade {lead.grade}).\n"
            f"Best-fit service: {service_name(lead.primary_service)}.\n"
            f"Evidence: {evidence.source_url if evidence else 'n/a'}\n\n"
            f"{REVIEW_MARK} {why}.\nRead: {read}.\nPerson found: {person}.\n"
            f"Contact page: {page or 'none found'}.\n\n"
            "This company has not contacted us, so nothing is sent automatically, and no address is guessed. Review the "
            "evidence and, if it is a fit, choose a permitted channel yourself (their published contact form, LinkedIn, an "
            "event, or a warm introduction). A message you can paste:\n\n"
            f"{greeting}\n\nI am writing about {topic} at {company.name}. {brand.format(company=self.settings.company_name)}\n\n"
            "Would a short call next week be useful? If someone else looks after this, I would be grateful for a pointer."
            f"\n\nBest regards,\n{sender}\n{self.settings.company_name}"
        )

    @staticmethod
    def _person_found(session: Session, lead: Lead, snap: ResearchSnapshot | None) -> tuple[str | None, str | None, str | None]:
        """(name, title, where it was published) of the best person on record, from the company's own pages."""
        people = [lead.contact] if lead.contact else []
        people += session.execute(select(Contact).where(Contact.company_id == lead.company_id).order_by(
            Contact.relevance.desc(), Contact.id)).scalars().all()
        company = lead.company.name if lead.company else None
        for c in people:
            if c.last_name and c.source in ("website", "sec_filing") and looks_like_name(c.display_name, company):
                return c.display_name, c.title, c.evidence_url
        found = next((p for p in (snap.people if snap else []) or [] if p.get("name") and looks_like_name(p["name"], company)), None)
        return (found["name"], found.get("title"), found.get("url")) if found else (None, None, None)

    def _in_send_window(self, now: datetime, cfg: dict) -> bool:
        tz = ZoneInfo(cfg.get("send_window_timezone", "America/New_York"))
        local = as_aware_utc(now).astimezone(tz)
        if local.weekday() >= 5 or holiday_on(local.date(), cfg.get("holidays")):
            return False
        start = time.fromisoformat(cfg.get("send_window_start", "08:00"))
        end = time.fromisoformat(cfg.get("send_window_end", "18:00"))
        return start <= local.time() <= end

    def _send_approved(self, session: Session, now: datetime, cfg: dict, out: SalesOutput, send: bool = True,
                       hold: str | None = None) -> None:
        # "approved" without an approval record (e.g. set directly in the database) is not an approval.
        for f in session.execute(select(FollowUp).where(
                FollowUp.channel == "email", FollowUp.status == "approved", FollowUp.approved_at.is_(None))).scalars():
            f.status = "awaiting_approval"
            f.blocked_reason = "Needs a person's approval before it can be sent"
            out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
        due = select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "approved",
                                     FollowUp.approved_at.is_not(None), FollowUp.due_at <= now)
        if self._sending_held(session, due, now, out):
            return
        ready, _ = self.sender.status()
        automation.observe_sending(session, self.settings.outbound_email_enabled, now)  # the switch, not readiness
        if not send:
            return
        state = automation.get_state(session)
        if state != "running":  # paused or stopped in the Automation Control Center: nothing goes out
            for f in session.execute(due).scalars():
                f.blocked_reason = (f"Not sent: automation {automation.STATES[state].split(':')[0].lower()} "
                                    "in the Automation Control Center")
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
            return
        if hold:  # read first, send second: every kind waits, including replies, and no campaign approves anything
            held = session.execute(due).scalars().all()
            for f in held:
                f.blocked_reason = hold[:300]
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
            if held:
                alert_once(session, "error", self.name, f"{len(held)} approved email(s) are waiting. {hold.removeprefix('Held: ')}",
                           self.settings, key=MAILBOX_ALERT)
            return
        resolve_alerts(session, (MAILBOX_ALERT,), set())
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

    def _sending_held(self, session: Session, due, now: datetime, out: SalesOutput) -> bool:
        """A restored or damaged database: nothing goes out, and no campaign approves anything, until a person has looked."""
        something_to_send = session.execute(due.limit(1)).first() or not self.settings.require_human_approval
        why = send_journal.why_held(self.ctx, session, check_integrity=bool(something_to_send), source=self.name)
        if not why:
            return False
        automation.observe_sending(session, self.settings.outbound_email_enabled, now)
        for f in session.execute(due).scalars():
            f.blocked_reason = send_journal.row_message() if why.startswith("ON HOLD") else why[:300]
            out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
        return True

    def _deliver(self, session: Session, f: FollowUp, to: str, *args, **kwargs) -> str:
        """Hand one email to the mail server, with the record outside the database written first. A send that certainly
        delivered nothing takes the record back, so the retry is no repeat; anything else leaves it for a restore to find.
        A record that cannot be written stops this send and leaves the email approved."""
        data = self.settings.data_dir
        try:
            send_journal.record_claim(data, f.id, to, f.message_id)
        except OSError as exc:
            alert_once(session, "error", self.name, "Nothing is being sent: the record of sent emails cannot be written "
                       f"({send_journal.journal_path(data)}, {type(exc).__name__}). Check that the disk has room and that no "
                       "other program holds the file or made it read-only.", self.settings, key=JOURNAL_ALERT)
            raise EmailNotReady(f"the record of sent emails cannot be written ({type(exc).__name__}), so nothing left") from exc
        resolve_alerts(session, (JOURNAL_ALERT,), set())
        try:
            msg_id = self.sender.send(to, *args, **kwargs)
        except NOT_DELIVERED:
            send_journal.record_released(data, f.id, to)  # if this fails too: "delivery unknown", for a person
            raise
        session.refresh(f)  # the PC may have slept inside the mail conversation: another run then called it interrupted
        if f.status == "uncertain":
            session.execute(update(Alert).where(Alert.message.like(f"Follow-up #{f.id}:%"), Alert.acknowledged.is_(False)).values(
                acknowledged=True, resolved_at=utcnow()))
        return msg_id

    def _send_locked(self, session: Session, approved: list[FollowUp], now: datetime, cfg: dict,
                     out: SalesOutput) -> None:
        ready, reason = self.sender.status()
        in_window = self._in_send_window(now, cfg)
        limits = self._outreach_limits(session, now, cfg)
        sign_in_failed = False
        try:
            journal = send_journal.read(self.settings.data_dir)  # once: this run's own sends are other emails
        except OSError as exc:
            for f in approved:
                f.blocked_reason = f"Waiting: the record of sent emails cannot be read just now ({type(exc).__name__})"
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
            return
        for f in approved:
            went_out = send_journal.claim_for(journal, f)
            if went_out:  # the record outside the database says it left already: never again without a person
                send_journal.mark_for_a_person(session, f, went_out, self.name)
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                session.commit()
                continue
            contact = f.contact
            lead = f.lead
            test_auth = None  # the authorization a given test contact's email goes out under
            reengaging = f.purpose == "reengage" and lead.outreach_status == "reengage"  # the one email to a parked lead
            if lead_on_hold(session, f.lead_id):  # someone we do not know wrote about this lead: a person decides first
                f.blocked_reason = "Held: an email about this lead from an unknown address waits on Replies for a person"
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                continue
            gone = automation.campaign_approval_problem(session, f, now, self.settings)  # holds while the campaign does
            if gone:
                automation.return_to_person(f, gone)
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                continue
            aged = (utcnow() - f.approved_at).days  # the real clock: `now` may be a run's own time
            if aged > int(cfg.get("draft_fresh_days", 14)):  # a person looks again: what was true then may not be now
                automation.return_to_person(f, f"it was approved {aged} days ago and never went out; read it again")
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
                    ok, why = eligible(session, self.settings, lead, for_send=True, reengage=reengaging,
                                       running=f.step > 1 and lead.outreach_status == "contacted")
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
            answerable = (lead.status == "qualified" or reengaging
                          or (f.kind is None and self._may_answer(session, lead)))
            if not answerable and f.kind != "response" and test_auth is None:
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
                holiday = holiday_on(as_aware_utc(now).astimezone(ZoneInfo(cfg.get("send_window_timezone", "America/New_York"))).date(),
                                     cfg.get("holidays"))
                f.blocked_reason = (f"Waiting: {holiday} is a US holiday; it goes out on the next business day" if holiday
                                    else "Waiting for the business-hours send window")
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
                msg_id = self._deliver(session, f, f.to_email or contact.email, f.subject, f.body or "", in_reply_to=f.in_reply_to,
                                       message_id=f.message_id, references=self._references(session, f))
            except smtplib.SMTPRecipientsRefused as exc:
                self._recipient_refused(session, f, exc, out)
                session.commit()
                continue
            except (EmailNotReady, smtplib.SMTPSenderRefused,
                    smtplib.SMTPConnectError, smtplib.SMTPAuthenticationError, ConnectionRefusedError,
                    socket.gaierror) as exc:
                # Rejected or never connected: nothing was delivered, safe to try again later.
                f.status, f.send_started_at = "approved", None
                f.blocked_reason = redact(f"Send failed (not delivered): {exc}")[:300]
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                if isinstance(exc, smtplib.SMTPAuthenticationError) or (isinstance(exc, EmailNotReady) and "oauth" in str(exc).lower()):
                    # Every other email would fail the same way, and repeated refused sign-ins can lock the mailbox.
                    sign_in_failed, ready, reason = True, False, f.blocked_reason
                    alert_once(session, "error", self.name, "The sales mailbox refused the sign-in, so approved emails are "
                               f"waiting and nothing is being sent: {f.blocked_reason}"[:500], self.settings,
                               key=SIGN_IN_ALERT)
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
        if out.emails_sent and not sign_in_failed:
            resolve_alerts(session, (SIGN_IN_ALERT,), set())

    def _recipient_refused(self, session: Session, f: FollowUp, exc: smtplib.SMTPRecipientsRefused, out: SalesOutput) -> None:
        """The mail server said no at RCPT TO. Temporary (4xx): tried again later. Permanent (5xx): asking again every run
        cannot help. An address that does not exist is handled as a bounce; any other refusal goes back to a person."""
        to = f.to_email or (f.contact.email if f.contact else "") or ""
        answers = [(code, message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message))
                   for code, message in exc.recipients.values()]
        said = redact("; ".join(f"{code} {message}" for code, message in answers))
        f.send_started_at = None
        if not answers or any(code < 500 for code, _ in answers):
            f.status, f.blocked_reason = "approved", f"Send failed (not delivered): {said}"[:300]
            out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
            return
        if DEAD_ADDRESS.search(said) and not self.sender.recipient_for(to)[1]:  # not the test address standing in for it
            f.status, f.delivery_status = "cancelled", "bounced"
            f.blocked_reason = f"Not sent: the mail server says this address does not exist ({said})"[:300]
            for c in session.execute(select(Contact).where(func.lower(Contact.email) == to.lower())).scalars():
                c.email_status = "bounced"
            suppress(session, to, "Bounced (refused by the mail server)")
            if f.lead and f.lead.outreach_status:
                stop_sequence(session, f.lead, "bounced", "Email refused: the address does not exist", self.name)
            out.cancelled += 1
        else:
            automation.return_to_person(f, f"the mail server refused it ({said})")
            out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
        session.add(Alert(level="warning", source=self.name, message=f"Follow-up #{f.id}: {f.blocked_reason}"[:500]))

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
