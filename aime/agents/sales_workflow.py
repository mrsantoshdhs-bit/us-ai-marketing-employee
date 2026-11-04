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
from aime.crm_ops import WROTE_AGAIN, canonical_address, canonical_sql, is_suppressed, later_reply_waiting, log_activity, suppress
from aime.email_templates import draft_reply
from aime.integrations.email_smtp import EmailNotReady, EmailSender, MailServerNotReached, compliance_problem, new_message_id
from aime.knowledge.content_bank import BRAND_LINES
from aime.knowledge.services import service_name
from aime.models import (Activity, Alert, Contact, FollowUp, InboundEmail, InboundInquiry, Lead, Meeting, MeetingProposal, Opportunity,
                         ResearchSnapshot, Signal)
from aime.monitoring import alert_once, resolve_alerts
from aime.outreach import (ALREADY_CUSTOMER, COMPANY_OPTED_OUT, MEETING_BOOKED, OPEN, REACHED_PROSPECT, RUNNING_STAGES, STOP_STATUSES, TEST_SENT, TOPICS,
                           _emailed_address_closed, _merged_family, company_opted_out, draft_outreach, draft_reengage, eligible, gather_evidence, has_live_meeting, sent_earlier, record_delivery,
                           set_status, stop_sequence)
from aime.people import looks_like_name
from aime.pipeline import NOBODY_TO_WRITE_TO, pipeline_checks, reached_address
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
UNREACHABLE_ALERT = "mail:unreachable"
REVIEW_MARK = "Why no email was drafted:"  # in every review task written since this note was added
HAND_TASK = "Decide by hand: "  # the one task for a lead whose next email has nobody it may go to; never "Review ", whose bodies get rewritten
# no_response is how the lead of such a task gets parked, unanswered: only an answer or a stop ends the task.
HAND_TASK_STOPS = tuple(s for s in STOP_STATUSES if s != "no_response")
NEVER_REACHED = "this address never received the earlier emails of this sequence"
# Between two looks in one send run a slow mail server takes seconds. More than this: the PC slept, or its clock was changed.
SEND_RUN_JUMP = timedelta(minutes=5)
TIME_JUMPED = ("Waiting: the PC slept or its clock changed during the send run, so the rest of that run stopped. It goes out on "
               "the next run, after a new read of the mailbox")
LEFT_THE_WINDOW = "Waiting for the business-hours send window"


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
        self.read_hold = None  # the followups job's: asked before each email, why it must wait for a new read of the mailbox

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
        self.run_began = utcnow()  # `now` may be a run's own time: what has passed since is read from the real clock
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
        self._close_tasks_of_stopped_leads(session)
        stmt = select(Lead).where(Lead.status == "qualified").order_by(Lead.score.desc(), Lead.id)
        if inp.lead_ids:
            stmt = stmt.where(Lead.id.in_(inp.lead_ids))
        budget = outreach_policy(self.settings)["drafts_per_run"]
        for lead in session.execute(stmt).scalars().all():
            contact = lead.contact
            if contact and contact.consent_status in CONSENTED and contact.email and contact.email_status != "bounced":
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

    @staticmethod
    def _may_answer(session: Session, lead: Lead) -> bool:
        """Whether the inquiry sequence may write to this lead: a qualified one, or a person who asked us (an
        inquiry, a LinkedIn form or event registration, with consent) whom only the score put in nurture. Never a
        disqualified lead, nor one that a person, a "not interested" reply or the stale-lead rule parked."""
        contact = lead.contact
        if contact and contact.email_status == "bounced":  # a bounce leaves their consent as it was, and nobody is answered at a dead address
            return False
        if lead.status == "qualified":
            return True
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
        if self._already_emailed_elsewhere(session, lead):
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

    def _already_emailed_elsewhere(self, session: Session, lead: Lead) -> bool:
        """This person really got an email on another lead (a merge, or two records of one company): no second first email.
        A person is told once on the lead's history; a task would only invite them to write by hand."""
        contact = lead.contact
        other = session.execute(select(FollowUp.lead_id).where(
            FollowUp.channel == "email", FollowUp.lead_id != lead.id, FollowUp.status.in_(COUNTS_AS_SENT), REACHED_PROSPECT,
            or_(FollowUp.contact_id == contact.id, canonical_sql(FollowUp.to_email) == canonical_address(contact.email)))).scalars().first()
        if other is None:
            return False
        note = f"No first email drafted: this address was already emailed on lead {other}"
        if not session.execute(select(Activity.id).where(Activity.lead_id == lead.id, Activity.subject == note)).first():
            log_activity(session, note, self.name, type_="outreach", company_id=lead.company_id, lead_id=lead.id)
        return True

    def _wrote_to(self, session: Session, lead: Lead, to: str | None) -> bool:
        """Whether a follow-up to this address follows anything: an earlier email on this lead really reached it. While
        test mode is on the email goes to our own address, so a rehearsal walks on."""
        return bool(self.sender.settings.test_recipients) or reached_address(session, lead.id, to)

    def _leave_to_a_person(self, session: Session, lead: Lead, out: SalesOutput) -> None:
        """Once per lead, and it stays closed once closed. The stage stays as it is: nothing restarts by itself."""
        if self._no_hand_task_because(session, lead):
            return
        if session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "task",
                                                     FollowUp.subject.startswith(HAND_TASK, autoescape=True))).first():
            return
        to = lead.contact.email if lead.contact else None
        session.add(FollowUp(lead_id=lead.id, company_id=lead.company_id, channel="task", step=1, due_at=utcnow(), status="pending",
                             subject=f"{HAND_TASK}{lead.company.name}"[:300],
                             body=(f"No further email is drafted for {lead.company.name}: {to or 'its current contact'} never received the "
                                   "earlier emails of this sequence. They went to another address, or only to our own test address "
                                   "while test mode was on. Nothing goes out by itself: write by hand if you think it right, or leave it.")))
        log_activity(session, "No further email drafted: the current address never received the earlier ones; a task asks a person",
                     self.name, type_="outreach", company_id=lead.company_id, lead_id=lead.id)
        out.tasks_created += 1

    def _parked_with_nobody_to_write_to(self, session: Session, lead: Lead) -> bool:
        """pipeline_checks parked it because its next step had nobody to go to. Nothing else looks at it until the re-engagement date, months on."""
        if lead.status != "nurture" or lead.outreach_status != "no_response" or not eligible(session, self.settings, lead, reengage=True)[0]:
            return False
        return not self._wrote_to(session, lead, lead.contact.email) and session.execute(select(Activity.id).where(
            Activity.lead_id == lead.id, Activity.type == "outreach", Activity.subject.contains(NOBODY_TO_WRITE_TO, autoescape=True)).limit(1)).first() is not None

    def _new_outreach(self, session: Session, lead: Lead, step: int, due: datetime, evidence_dicts: list[dict],
                      evidence, out: SalesOutput, in_reply_to: str | None = None) -> None:
        contact = lead.contact
        earlier = sent_earlier(session, lead.id, in_reply_to)
        subject, body = draft_outreach(self.settings, step, lead, contact, evidence, first_subject=earlier[0].subject if earlier else None)
        suppressed = is_suppressed(session, contact.email, lead.company.domain)
        qc = run_email_checks(subject, body, evidence_dicts, contact, suppressed, step=step, sent_subjects=[e.subject for e in earlier])
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
                    and_(FollowUp.channel == "email", FollowUp.status.in_(OPEN_FOLLOWUP)),
                    and_(FollowUp.kind == "outreach", FollowUp.status == "rejected")))).first():
                continue  # re-engaged once already, an email is still open, or a person said no to a step of this sequence
            if not eligible(session, self.settings, lead, reengage=True)[0]:
                continue  # opted out, suppressed, bounced, competitor, or the address is no longer published
            contact = lead.contact
            if not self._wrote_to(session, lead, contact.email):  # "A few months ago I wrote" would not be true
                self._leave_to_a_person(session, lead, out)
                continue
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
        if not exists and not has_live_meeting(session, lead):
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
            if lead is not None and outreach and self._parked_with_nobody_to_write_to(session, lead):
                self._leave_to_a_person(session, lead, out)
                continue
            intervals = list(pol["intervals"] if outreach else cfg.get("intervals_days", [0, 3, 7]))
            max_touches = min(pol["max_touches"] if outreach else int(cfg.get("max_touches", 3)), len(intervals))
            if lead is None or last.step >= max_touches:
                if (lead is not None and outreach and lead.outreach_status == TEST_SENT and eligible(session, self.settings, lead, running=True)[0]
                        and not self._wrote_to(session, lead, lead.contact.email)):  # the whole sequence was a rehearsal, and that has ended
                    self._leave_to_a_person(session, lead, out)
                continue
            if outreach:
                if (lead.status != "qualified" or lead.outreach_status not in RUNNING_STAGES
                        or not eligible(session, self.settings, lead, running=True)[0]):
                    continue  # replied, unsubscribed, bounced, stopped, or no longer reachable
            elif (not self._may_answer(session, lead) or not lead.contact
                    or lead.contact.consent_status not in CONSENTED or lead.outreach_status in STOP_STATUSES):
                continue  # they replied, booked, unsubscribed, bounced, or a person stopped the sequence
            elif self._deal_lost(session, lead_id):
                continue  # "lost" is a person's word on the deal: nobody is chased after it
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
                if outreach and not self._wrote_to(session, lead, lead.contact.email):
                    self._leave_to_a_person(session, lead, out)
                elif outreach:
                    self._new_outreach(session, lead, last.step + 1, now, list(last.evidence or []), [], out,
                                       in_reply_to=last.message_id)
                else:
                    self._new_draft(session, lead, last.step + 1, now, out, in_reply_to=last.message_id)

    @staticmethod
    def _deal_lost(session: Session, lead_id: int) -> bool:
        """A lost deal and no open one."""
        stages = set(session.execute(select(Opportunity.stage).where(Opportunity.lead_id == lead_id)).scalars())
        return "lost" in stages and not stages - {"lost", "won"}

    def _no_task_because(self, session: Session, lead: Lead, stages: tuple[str, ...] = STOP_STATUSES) -> str | None:
        """Why a person must not be told to write to this lead by hand: it answered or said no, or it is closed for us."""
        contact = lead.contact
        if lead.outreach_status in stages:
            return f"outreach stopped ({lead.outreach_status})"
        if has_live_meeting(session, lead):
            return MEETING_BOOKED
        if (contact and contact.consent_status == "opted_out") or is_suppressed(session, contact.email if contact else None, lead.company.domain):
            return "on the do-not-contact list"
        if company_opted_out(session, lead.company):
            return COMPANY_OPTED_OUT
        # Asked here, not of eligible(): it answers "no published business email" or the grade first, the usual reasons for a task.
        if session.execute(select(Lead.id).where(Lead.company_id.in_(_merged_family(session, lead.company)), Lead.id != lead.id,
                                                 Lead.status == "converted").limit(1)).first():
            return ALREADY_CUSTOMER
        return None

    def _no_hand_task_because(self, session: Session, lead: Lead) -> str | None:
        """As above, and its emails went to an earlier address, which counts too."""
        if lead.status not in ("qualified", "nurture"):
            return f"lead is {lead.status}"
        if _emailed_address_closed(session, lead):
            return "an address this lead was emailed at is on the do-not-contact list"
        return self._no_task_because(session, lead, HAND_TASK_STOPS)

    def _close_tasks_of_stopped_leads(self, session: Session) -> None:
        """Those in nurture or converted never reach the qualified loop. Cancelled with a reason, never done: a task closed
        with no reason reads as a person's, blocks a fresh one for good and counts as work by hand."""
        tasks = session.execute(select(FollowUp, Lead.outreach_status).join(Lead, FollowUp.lead_id == Lead.id).where(
            FollowUp.channel == "task", FollowUp.status == "pending", FollowUp.subject.startswith("Review "),
            Lead.outreach_status.in_(STOP_STATUSES))).all()
        for task, stage in tasks:
            task.status, task.blocked_reason = "cancelled", f"outreach stopped ({stage})"
        # Any kind of lead: a hand task invites an email, so what closes the lead's address closes it too.
        for task in session.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending",
                                                           FollowUp.subject.startswith(HAND_TASK, autoescape=True))).scalars().all():
            why = self._no_hand_task_because(session, task.lead)
            if why:
                task.status, task.blocked_reason = "cancelled", why[:300]

    def _ensure_task(self, session: Session, lead: Lead, out: SalesOutput) -> None:
        stop = self._no_task_because(session, lead)
        if stop:
            for task in session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "task", FollowUp.status == "pending",
                                                               FollowUp.subject.startswith("Review "))).scalars():
                task.status, task.blocked_reason = "cancelled", stop[:300]
            return
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
        if any(t.status == "pending" or not t.blocked_reason for t in tasks):
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
            in_window = self._in_send_window(now, cfg)  # outside it nothing could leave anyway, whatever a read says
            for f in held:
                f.blocked_reason = hold[:300] if in_window else LEFT_THE_WINDOW
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
            if held and in_window:
                alert_once(session, "error", self.name, f"{len(held)} approved email(s) are waiting. {hold.removeprefix('Held: ')}",
                           self.settings, key=MAILBOX_ALERT)
            return
        resolve_alerts(session, (MAILBOX_ALERT,), set())
        # Hybrid/Automatic mode: an active campaign authorization may approve waiting emails, at send time only
        # only inside the send window: an approval that has to wait would still count against the campaign's limits
        by_campaign = (ready and not self.settings.require_human_approval and automation.get_mode(session) != "approval"
                       and self._in_send_window(now, cfg) and bool(automation.active_authorizations(session, now)))
        if not by_campaign and not session.execute(due.limit(1)).first():
            resolve_alerts(session, (UNREACHABLE_ALERT,), set())  # nothing is waiting on the mail server any more
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
            self._clear_interrupted(session, f)  # what this run knows is better: it did not leave
            raise
        self._clear_interrupted(session, f)
        return msg_id

    @staticmethod
    def _clear_interrupted(session: Session, f: FollowUp) -> None:
        """The PC may have slept inside the mail conversation: another run then called it interrupted, with an alert to check."""
        session.refresh(f)
        if f.status == "uncertain":
            session.execute(update(Alert).where(Alert.message.like(f"Follow-up #{f.id}:%"), Alert.acknowledged.is_(False)).values(
                acknowledged=True, resolved_at=utcnow()))

    def _why_stop_the_run(self, last_look: datetime, now: datetime, cfg: dict, in_window: bool) -> str | None:
        """Judged again before each email, because the process can freeze for hours between two of them (a laptop lid). Only
        ever stops a run: what was out of the window or held when the run began is dealt with as before."""
        real = utcnow()
        if abs(real - last_look) > SEND_RUN_JUMP:
            return TIME_JUMPED
        if in_window and not self._in_send_window(now + (real - self.run_began), cfg):
            return LEFT_THE_WINDOW
        return self.read_hold() if self.read_hold else None

    @staticmethod
    def _stopped_since(session: Session) -> str | None:
        """Paused, stopped or the emergency stop pressed in the Automation Control Center while this run was sending: the
        rest of the run stays approved. Read again before each email, as the run-start check reads it once."""
        state = automation.get_state(session)
        if state == "running":
            return None
        return f"Not sent: automation {automation.STATES[state].split(':')[0].lower()} in the Automation Control Center"

    def _stop_before_handing_over(self, last_look: datetime, now: datetime, cfg: dict, in_window: bool) -> None:
        """The same judgement at the last moment: the PC can sleep between the claim and the mail server too."""
        stop = self._why_stop_the_run(last_look, now, cfg, in_window)
        if stop:
            raise EmailNotReady(stop)

    def _send_locked(self, session: Session, approved: list[FollowUp], now: datetime, cfg: dict,
                     out: SalesOutput) -> None:
        ready, reason = self.sender.status()
        in_window = self._in_send_window(now + (utcnow() - self.run_began), cfg)
        limits = self._outreach_limits(session, now, cfg)
        replies_sent, replies_per_run = 0, int(cfg.get("replies_per_run", 10))
        sign_in_failed = unreachable = False
        try:
            journal = send_journal.read(self.settings.data_dir)  # once: this run's own sends are other emails
        except OSError as exc:
            for f in approved:
                f.blocked_reason = f"Waiting: the record of sent emails cannot be read just now ({type(exc).__name__})"
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
            return
        # someone is waiting on a meeting's time: those notes go first, and no limit on replies holds them
        order = sorted(approved, key=lambda f: f.purpose not in automation.MEETING_NOTES)
        last_look = utcnow()
        for turn, f in enumerate(order):
            stop = self._why_stop_the_run(last_look, now, cfg, in_window) or self._stopped_since(session)
            if stop:  # the rest stay approved, for the next proper run
                self.log.warning("Send run stopped with %s email(s) left: %s", len(order) - turn, stop)
                for rest in order[turn:]:
                    rest.blocked_reason = stop[:300]
                    out.blocked.append(BlockedItem(id=rest.id, reason=rest.blocked_reason))
                break
            last_look = utcnow()
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
            if self._meeting_time_passed(session, f):  # a person decides: a note about a past call says nothing true
                automation.return_to_person(f, "the meeting time has passed")
                out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                continue
            if f.kind == "response":
                why = self._response_problem(session, f)
                if why:
                    f.status, f.blocked_reason = "cancelled", f"Not sent: {why}"[:300]
                    out.cancelled += 1
                    continue
                if lead.company.is_competitor and not lead.company.competitor_cleared_by:  # flagged since approval: not lost, and not sent
                    automation.return_to_person(f, "this company was flagged a possible competitor; a person clears the flag or rejects this")
                    out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                    continue
                if later_reply_waiting(session, f):  # held, not cancelled: it goes out once a person has read what came after it
                    f.blocked_reason = WROTE_AGAIN
                    out.blocked.append(BlockedItem(id=f.id, reason=WROTE_AGAIN))
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
                    if ok and (f.step > 1 or f.purpose == "reengage") and not self._wrote_to(session, lead, f.to_email):
                        ok, why = False, NEVER_REACHED  # cancelled: approving it again would meet this same check
                    if ok and f.step == 1:
                        twin = self._same_address_on_another_lead(session, f)
                        if twin:  # both may be open: the older one goes first, and a person decides about this one
                            automation.return_to_person(f, f"this address also has an email on lead {twin}")
                            out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                            continue
                    if not ok and why.startswith("B2B outreach is turned off"):  # a pause, not a decision about this email
                        f.blocked_reason = f"Held: {why}; it goes out when outreach is turned on again"[:300]
                        out.blocked.append(BlockedItem(id=f.id, reason=f.blocked_reason))
                        continue
                if not ok:
                    f.status, f.blocked_reason = "cancelled", f"Not sent: {why}"[:300]
                    out.cancelled += 1
                    if why.startswith(("grade ", "the contact or address changed")) or why == NEVER_REACHED:
                        self._say_approval_fell_away(session, f, lead, why)
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
                f.status, f.blocked_reason = ("cancelled", "Not sent: the sequence was stopped" if lead.outreach_status == "stopped"
                                              else f"Not sent: the sequence stopped ({lead.outreach_status})")
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
            limited_reply = f.kind != "outreach" and f.purpose not in automation.MEETING_NOTES
            if limited_reply and replies_sent >= replies_per_run:  # still approved: only its turn comes later
                f.blocked_reason = (f"Waiting: at most {replies_per_run} replies leave in one run, so that they do not all go "
                                    "at once. Nothing is lost: it goes out on the next run")
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
                                       message_id=f.message_id, references=self._references(session, f),
                                       last_look=lambda at=last_look: self._stop_before_handing_over(at, now, cfg, in_window))
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
                if isinstance(exc, MailServerNotReached):
                    # Each email would wait its minute for the same answer. The rest of the run still passes the gates, so an opt-out still cancels.
                    unreachable, ready, reason = True, False, f.blocked_reason
                    alert_once(session, "warning", self.name, "The mail server could not be reached, so approved emails are waiting; nothing "
                               f"was delivered and they go out on a later run: {f.blocked_reason}"[:500], self.settings, key=UNREACHABLE_ALERT)
                elif isinstance(exc, smtplib.SMTPAuthenticationError) or (isinstance(exc, EmailNotReady) and "oauth" in str(exc).lower()):
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
            replies_sent += limited_reply
            if f.kind == "outreach":
                limits["sent_today"] += 1
                limits["run"] += 1
                domain = (f.to_email or "").split("@")[-1].lower()
                limits["domains"][domain] = limits["domains"].get(domain, 0) + 1
            out.emails_sent += 1
            log_activity(session, f"Email sent: {f.subject}", self.name, type_="email_sent", body=f.body,
                         company_id=f.company_id, lead_id=f.lead_id)
            session.commit()
        if out.emails_sent and not sign_in_failed:
            resolve_alerts(session, (SIGN_IN_ALERT,), set())
        if out.emails_sent and not unreachable:
            resolve_alerts(session, (UNREACHABLE_ALERT,), set())

    def _say_approval_fell_away(self, session: Session, f: FollowUp, lead: Lead, why: str) -> None:
        """The gate is right to stop it, but a person approved this email and a cancelled one leaves the open list."""
        said = f"Follow-up #{f.id} on lead {lead.id} ({lead.company.name}) was approved and not sent: {why}. The lead's grade is now {lead.grade or 'none'}."
        alert_once(session, "warning", self.name, said[:500], self.settings)
        log_activity(session, said[:300], self.name, type_="outreach", company_id=lead.company_id, lead_id=lead.id)

    @staticmethod
    def _same_address_on_another_lead(session: Session, f: FollowUp) -> int | None:
        """Another lead's outreach that reached this address, may have, or is open and older than this one. A rehearsal went to our own."""
        return session.execute(select(FollowUp.lead_id).where(
            FollowUp.kind == "outreach", FollowUp.lead_id != f.lead_id, canonical_sql(FollowUp.to_email) == canonical_address(f.to_email or ""),
            or_(and_(FollowUp.status.in_(COUNTS_AS_SENT), REACHED_PROSPECT), and_(FollowUp.status.in_(OPEN_FOLLOWUP), FollowUp.id < f.id)))).scalars().first()

    @staticmethod
    def _meeting_time_passed(session: Session, f: FollowUp) -> bool:
        """A note about a meeting whose time has gone with no later one on the lead, or an offer whose times have all gone.
        A cancelled meeting is no later meeting, except to its own cancel note. Read from the real clock: `now` may be a run's own time."""
        if f.purpose in automation.MEETING_NOTES:
            meetings = session.execute(select(Meeting.starts_at, Meeting.status).where(Meeting.lead_id == f.lead_id)).all()
            times = [m.starts_at for m in meetings]
            later = [m.starts_at for m in meetings if f.purpose == "meeting_cancel" or m.status != "cancelled"]
            offered = [datetime.fromisoformat(slot) for p in session.execute(select(MeetingProposal).where(
                MeetingProposal.lead_id == f.lead_id, MeetingProposal.status.in_(("draft", "offered")))).scalars() for slot in p.slots or []]
            times += offered
            later += offered
        elif f.purpose == "meeting_offer":
            times = [datetime.fromisoformat(slot) for p in session.execute(select(MeetingProposal).where(
                MeetingProposal.offer_followup_id == f.id)).scalars() for slot in p.slots or []]
            later = times
        else:
            return False
        clock = as_aware_utc(utcnow())
        return any(as_aware_utc(t) <= clock for t in times) and not any(as_aware_utc(t) > clock for t in later)

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
            suppress(session, to, "Bounced (refused by the mail server)", bounce=True)
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
            d = (addr or "").split("@")[-1].lower()
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
        domain = (f.to_email or "").split("@")[-1].lower()
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
        if f.contact and f.contact.consent_status == "opted_out":  # lifting a domain's entry leaves its contacts' marks, and a requeue skips the approve route
            return "this contact opted out"
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
