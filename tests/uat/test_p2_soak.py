"""Phase 2, the passage of time: what weeks of real ticks do, found by a clock-travel soak on a production copy and pinned
here on synthetic data. test_defect_* assert the CORRECT behaviour, so they fail until the defect is fixed; test_ok_* are
behaviour the soak confirmed and Phase 1 does not cover. Everything runs on the real agents and the real tick."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import aime.agents.analytics as analytics_module
import aime.agents.research as research_module
import aime.timeutil
import aime.workflows as workflows
from aime.agents.crm import CRMManagementAgent
from aime.agents.inbox_agent import InboxAgent
from aime.agents.publishing import AuthorizedPublishingAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.inbox import Mailbox
from aime.integrations.email_smtp import EmailSender
from aime.models import Activity, Alert, Company, Contact, ContentItem, FollowUp, Lead, Opportunity, Report, Signal, Suppression
from aime.scheduler import runner
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.workflows import JobOutcome
from tests.test_inbound_sales import FakeSMTP
from tests.test_inbox import FakeIMAP, mail

ORIGIN = {"origin": "http://testserver"}
MAIL = dict(company_postal_address="1 Sample St, Austin, TX 78701", outbound_email_enabled=True, smtp_host="smtp.example",
            smtp_from="hello@us.example", smtp_username="hello@us.example", smtp_password="x", imap_host="imap.example",
            imap_username="hello@us.example", imap_password="x")
# The real timeline: first emails on Fri 30 Oct 2026, 13:09 New York. Inside the winter window: 13:00-23:00 UTC.
FIRST_SENT = datetime(2026, 10, 30, 17, 9)
WAKE = datetime(2026, 11, 4, 14, 0)  # Wed 09:00 New York, 19:30 on this PC


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET)

    def set_to(when: datetime) -> datetime:
        aime.timeutil.CLOCK_OFFSET = when - datetime.now(timezone.utc).replace(tzinfo=None)
        return when

    return set_to


@pytest.fixture
def world(make_ctx, monkeypatch):
    """A PC in India running the real jobs, with fake mail servers and nothing new to discover."""
    ctx = make_ctx(**MAIL)
    ctx.settings.schedule["timezone"] = "Asia/Kolkata"
    FakeIMAP.messages, FakeSMTP.sent = {}, []
    monkeypatch.setattr(workflows, "InboxAgent", lambda c: InboxAgent(c, mailbox=Mailbox(c.settings, imap_factory=FakeIMAP)))
    monkeypatch.setattr(workflows, "SalesWorkflowAgent", lambda c: SalesWorkflowAgent(c, sender=EmailSender(c.settings, smtp_factory=FakeSMTP)))
    monkeypatch.setattr(research_module, "get_sources", lambda c, names=None: [])
    return ctx


def only_jobs(monkeypatch, *keep):
    for name in list(runner.JOBS):
        if name not in keep:
            monkeypatch.setitem(runner.JOBS, name, lambda ctx, job=name: JobOutcome())


def seed(ctx, age_days: int, n: int = 1, grade_b_at_current_score: bool = True) -> int:
    """A researched company whose job post is `age_days` old, qualified by the real scoring. With the flag, grade B starts
    exactly at its score (production's contacted leads sit 1-4 points above it, so any lost point drops the grade)."""
    now = utcnow()
    with ctx.db.session() as s:
        c = Company(name=f"Acme Tools {n}", normalized_name=f"acme tools {n}", domain=f"acme{n}.example", website=f"https://acme{n}.example",
                    website_status="verified", website_checked_at=now, source="hn_hiring", state="TX", country="US")
        s.add(c)
        s.flush()
        ct = Contact(company_id=c.id, email=f"info@acme{n}.example", source="website", role_category="inbox", email_status="role_address",
                     email_source_url=f"https://acme{n}.example/contact", evidence="contact page",
                     evidence_url=f"https://acme{n}.example/contact", consent_status="none")
        s.add(ct)
        s.flush()
        lead = Lead(company_id=c.id, contact_id=ct.id, source="research", status="new", last_activity_at=now)
        s.add(lead)
        s.add(Signal(company_id=c.id, signal_type="hiring", text="We are hiring to build internal tools and AI agents for our teams.",
                     source="hn_hiring", source_url=f"https://news.example/{n}", fingerprint=f"post-{n}",
                     observed_at=now - timedelta(days=age_days), strength=1.0, matched_services=["custom_software", "ai_agents"],
                     keywords=["internal tools"]))
        s.flush()
        lead_id = lead.id
    assert LeadQualificationAgent(ctx).run({"lead_ids": [lead_id]}).ok
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        if grade_b_at_current_score:
            ctx.settings.business["qualification"]["grade_b"] = lead.score
            ctx.settings.business["qualification"]["grade_a"] = lead.score + 30
        lead.status = "qualified"  # it entered while its post was fresh: a post this old no longer takes a new lead in
    assert LeadQualificationAgent(ctx).run({"lead_ids": [lead_id]}).ok
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        assert (lead.status, lead.grade) == ("qualified", "B"), (lead.status, lead.grade, lead.score)
    return lead_id


def sent_email(ctx, lead_id: int, when: datetime, step: int = 1, by: str = "sales1") -> int:
    """An outreach email a person approved and the system really sent."""
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        f = FollowUp(lead_id=lead_id, contact_id=lead.contact_id, company_id=lead.company_id, channel="email", kind="outreach", step=step,
                     status="sent", subject=f"Step {step}", body="Hello", to_email=lead.contact.email, approved_by=by, approved_at=when,
                     sent_at=when, due_at=when, message_id=f"<lead{lead_id}-step{step}@us.example>", delivery_status="sent")
        s.add(f)
        lead.outreach_status, lead.owner, lead.last_activity_at = "contacted", by, when
        s.add(Activity(type="outreach", subject="Outreach: approved", actor=by, lead_id=lead_id, company_id=lead.company_id))
        s.flush()
        return f.id


def waiting_email(ctx, lead_id: int, step: int, status: str = "awaiting_approval", by: str | None = None) -> int:
    now = utcnow()
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        f = FollowUp(lead_id=lead_id, contact_id=lead.contact_id, company_id=lead.company_id, channel="email", kind="outreach", step=step,
                     status=status, subject=f"Step {step}", body="Hello.\n\nTo opt out, reply unsubscribe.\n1 Sample St, Austin, TX 78701",
                     to_email=lead.contact.email, due_at=now, approved_by=by, approved_at=now if by else None,
                     in_reply_to=f"<lead{lead_id}-step{step - 1}@us.example>" if step > 1 else None)
        s.add(f)
        s.flush()
        return f.id


def lead_state(ctx, lead_id: int) -> tuple:
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        return lead.status, lead.outreach_status


def followup(ctx, fid: int) -> tuple:
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        return f.status, f.blocked_reason


def drafts_of(ctx, lead_id: int, step: int) -> list[str]:
    with ctx.db.session() as s:
        return list(s.execute(select(FollowUp.status).where(FollowUp.lead_id == lead_id, FollowUp.step == step,
                                                             FollowUp.channel == "email")).scalars())


def sales_run(ctx, when: datetime):
    return SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run({"now": when.isoformat()}).output


# ------------------------------------------------------------------ what time does to a sequence
def test_defect_contacted_lead_drops_out_of_its_sequence_when_its_job_post_turns_60_days_old(world, clock):
    """Three of the five leads emailed on 30 Oct do this on Mon 2 Nov, 06:30 on this PC: their job post was 58 days old when
    they were emailed, and a person's approval does not count as a person having touched the lead (qualification.py:162
    looks for the actor "human"; real actors are user names). Nobody is told, and step 2 is never drafted."""
    clock(FIRST_SENT)
    lead_id = seed(world, age_days=59)
    sent_email(world, lead_id, FIRST_SENT)
    clock(datetime(2026, 11, 2, 1, 0))  # the crm_maintenance job's scoring run
    assert LeadQualificationAgent(world).run({}).ok
    assert lead_state(world, lead_id) == ("qualified", "contacted"), "a person emailed this lead: the score alone must not end the sequence"
    when = clock(datetime(2026, 11, 3, 18, 0))
    sales_run(world, when)
    assert drafts_of(world, lead_id, 2) == ["awaiting_approval"], "step 2 is due 4 days after the first email"


@pytest.mark.parametrize("how", ["disqualified", "cancelled_at_send"])
def test_defect_outreach_stage_stays_open_after_the_email_was_cancelled(world, clock, how):
    """Production's lead #43: disqualified, its only email cancelled, its outreach stage still "drafted" (it counts as drafted on
    the pipeline for ever). The same when an approved email is cancelled at send time because the score fell."""
    clock(datetime(2026, 11, 2, 15, 0))
    lead_id = seed(world, age_days=58)
    with world.db.session() as s:
        s.get(Lead, lead_id).outreach_status = "drafted" if how == "disqualified" else "approved"
    fid = waiting_email(world, lead_id, 1, *(("approved", "sales1") if how == "cancelled_at_send" else ()))
    if how == "disqualified":
        with world.db.session() as s:
            s.get(Lead, lead_id).company.is_competitor = True
        assert LeadQualificationAgent(world).run({"lead_ids": [lead_id]}).ok
    else:
        with world.db.session() as s:
            s.get(Lead, lead_id).status = "nurture"
        sales_run(world, utcnow())
    assert followup(world, fid)[0] == "cancelled"
    assert lead_state(world, lead_id)[1] not in ("drafted", "approved"), "nothing is drafted or approved any more"


def test_defect_rejecting_a_followup_leaves_the_lead_contacted_for_ever(world, clock):
    """Rejecting step 2 or 3 ends the sequence, but only step 1 closes the stage: the lead never reaches no_response, is never
    re-engaged, and after 30 days sits in nurture with the stage "contacted"."""
    clock(FIRST_SENT)
    lead_id = seed(world, age_days=10)
    sent_email(world, lead_id, FIRST_SENT)
    clock(datetime(2026, 11, 4, 10, 0))
    fid = waiting_email(world, lead_id, 2)
    r = TestClient(create_app(world)).post(f"/followups/{fid}/reject", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and followup(world, fid)[0] == "rejected"
    when = clock(datetime(2026, 12, 15, 15, 0))
    assert CRMManagementAgent(world).run({"operations": ["stale"]}).ok
    sales_run(world, when)
    assert lead_state(world, lead_id)[1] != "contacted", "the sequence ended 6 weeks ago, nothing more will go to this lead"


def _whole_sequence(ctx, lead_id: int, clock) -> datetime:
    for step, at in ((1, FIRST_SENT), (2, FIRST_SENT + timedelta(days=4)), (3, FIRST_SENT + timedelta(days=10))):
        clock(at)
        sent_email(ctx, lead_id, at, step=step)
    return FIRST_SENT + timedelta(days=10)


def test_defect_lead_parked_as_no_response_is_qualified_again_the_next_morning(world, clock):
    """pipeline.py sets nurture without a status record, so _may_requalify finds nothing against it: seen on the production copy
    (lead #239 on 18 Nov). It counts as qualified again, and the re-engagement email only looks at nurture leads."""
    clock(FIRST_SENT)
    lead_id = seed(world, age_days=5)
    last = _whole_sequence(world, lead_id, clock)
    when = clock(last + timedelta(days=8))
    sales_run(world, when)
    assert lead_state(world, lead_id) == ("nurture", "no_response")
    clock(when + timedelta(days=1, hours=1))
    assert LeadQualificationAgent(world).run({}).ok
    assert lead_state(world, lead_id) == ("nurture", "no_response")


def test_defect_lead_who_said_not_interested_is_qualified_again_the_next_morning(world, clock):
    """A "not interested" reply parks the lead without a status record (reply_intel.py:549), so the same re-qualification undoes it."""
    clock(FIRST_SENT)
    lead_id = seed(world, age_days=5)
    sent_email(world, lead_id, FIRST_SENT)
    with world.db.session() as s:
        addr = s.get(Lead, lead_id).contact.email
    when = clock(FIRST_SENT + timedelta(days=1))
    FakeIMAP.messages = {1: mail(f"Someone <{addr}>", "Re: Step 1", "Not interested, thanks.", in_reply_to=f"<lead{lead_id}-step1@us.example>")}
    assert InboxAgent(world, mailbox=Mailbox(world.settings, imap_factory=FakeIMAP)).run({}).ok
    assert lead_state(world, lead_id) == ("nurture", "not_interested")
    clock(when + timedelta(days=1, hours=1))
    assert LeadQualificationAgent(world).run({}).ok
    assert lead_state(world, lead_id) == ("nurture", "not_interested")


def test_defect_reengagement_email_is_not_drafted_once_the_evidence_has_aged(world, clock):
    """By design the check-in comes months later, but it demands grade B on a score that loses points as the job post ages. Unless
    research happens to find a newer post, the evidence has aged by then: after 6 weeks 4 of the 5 contacted leads are already below B."""
    clock(FIRST_SENT)
    lead_id = seed(world, age_days=10)
    last = _whole_sequence(world, lead_id, clock)
    clock(last + timedelta(days=8))
    sales_run(world, utcnow())
    assert lead_state(world, lead_id) == ("nurture", "no_response")
    when = clock(last + timedelta(days=91))
    assert LeadQualificationAgent(world).run({}).ok
    with world.db.session() as s:
        s.get(Lead, lead_id).status = "nurture"  # whatever the other defect did: this test is about the grade
    with world.db.session() as s:
        assert s.get(Lead, lead_id).grade == "C"  # the reason: the score fell with the evidence's age
    assert sales_run(world, when).reengage_drafts == 1


# ------------------------------------------------------------------ the PC was asleep
def test_defect_on_waking_an_approved_email_goes_to_someone_whose_opt_out_is_waiting_in_the_mailbox(world, clock, monkeypatch):
    """The tick runs jobs in registry order: research (slot 07:00 on this PC) runs the Sales Workflow Agent, which sends, before
    inbox reads the mailbox. Asleep through the morning and waking inside the New York window: the opt-out received 9 hours ago
    is read seconds after the email to that person left. Reproduced on the production copy (lead #3, follow-up step 2)."""
    only_jobs(monkeypatch, "research", "inbox", "followups")
    clock(datetime(2026, 11, 3, 18, 0))
    lead_id = seed(world, age_days=20)
    sent_email(world, lead_id, FIRST_SENT)
    clock(datetime(2026, 11, 4, 0, 30))  # Tue 19:30 New York: approved after hours, the PC then sleeps at 01:00 UTC
    assert runner.tick(world, trigger="scheduler")["status"] == "ok"
    fid = waiting_email(world, lead_id, 2, "approved", "sales1")
    with world.db.session() as s:
        addr = s.get(Lead, lead_id).contact.email
    FakeIMAP.messages = {1: mail(f"Someone <{addr}>", "Re: Step 1", "Please remove me from your list, do not email again.",
                                 in_reply_to=f"<lead{lead_id}-step1@us.example>")}
    clock(WAKE)
    runner.tick(world, trigger="scheduler")
    with world.db.session() as s:
        assert s.execute(select(Suppression).where(Suppression.value == addr)).scalars().first() is not None  # it was read, too late
    assert [m["To"] for m in FakeSMTP.sent] == [], "nobody gets an email after asking to be removed"
    assert followup(world, fid)[0] == "cancelled"


def test_defect_pc_asleep_at_report_time_loses_that_days_report(world, clock, monkeypatch):
    """A daily report missed at 20:00 is written on waking under the new day's date, and that day's own report overwrites it: an
    8-hour sleep over 20:00 left no report for 4 Nov on the copy, and nothing says so."""
    class SimDT(datetime):
        @classmethod
        def now(cls, tz=None):
            real = datetime.now(timezone.utc) + aime.timeutil.CLOCK_OFFSET
            return real.astimezone(tz) if tz else real.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(analytics_module, "datetime", SimDT)
    only_jobs(monkeypatch, "daily_report")
    clock(datetime(2026, 11, 3, 15, 0))  # Tue 20:30 on this PC: Tuesday's report
    runner.tick(world, trigger="scheduler")
    clock(datetime(2026, 11, 4, 22, 0))  # asleep over Wed 20:00; awake at 03:30 on Thursday
    runner.tick(world, trigger="scheduler")
    clock(datetime(2026, 11, 5, 15, 0))  # Thu 20:30
    runner.tick(world, trigger="scheduler")
    with world.db.session() as s:
        dates = sorted(str(r.report_date) for r in s.execute(select(Report).where(Report.kind == "daily")).scalars())
    assert "2026-11-04" in dates, dates


# ------------------------------------------------------------------ what piles up
def test_defect_one_blocked_publishing_problem_raises_a_new_alert_every_day(make_ctx, clock):
    """Production already has 5 of these open after 2 days; on the copy 48 are open after 6 weeks, none ever closed. One
    standing problem must be one open alert whose text is kept up to date, and it must close when the problem goes."""
    ctx = make_ctx(publishing_enabled=False)
    start = clock(datetime(2026, 11, 2, 2, 0))
    with ctx.db.session() as s:
        s.add(ContentItem(title="A post", body="Text", service="custom_crm", industry="saas", generator="template", fingerprint="post-1", status="approved", approved_by="admin", approved_at=start,
                          scheduled_for=start - timedelta(hours=1)))
    for day in range(7):
        clock(start + timedelta(days=day, minutes=30))
        assert AuthorizedPublishingAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        same = [a for a in s.execute(select(Alert).where(Alert.source == "publishing")).scalars() if not a.acknowledged]
    assert len(same) == 1, f"{len(same)} open alerts about one problem"


# ------------------------------------------------------------------ confirmed
def test_ok_step_2_is_drafted_once_four_days_later_and_waits_for_a_person(world, clock, monkeypatch):
    """Real hourly ticks from the first email: one draft, at the first run after day 4, and nothing sent."""
    only_jobs(monkeypatch, "inbox", "followups")
    clock(FIRST_SENT)
    lead_id = seed(world, age_days=10)
    sent_email(world, lead_id, FIRST_SENT)
    t = FIRST_SENT + timedelta(hours=1)
    seen = None
    while t < FIRST_SENT + timedelta(days=6):
        clock(t)
        runner.tick(world, trigger="scheduler")
        if seen is None and drafts_of(world, lead_id, 2):
            seen = t
        t += timedelta(hours=1)
    assert seen is not None and timedelta(days=4) <= seen - FIRST_SENT <= timedelta(days=4, hours=2)
    assert drafts_of(world, lead_id, 2) == ["awaiting_approval"] and FakeSMTP.sent == []


def test_ok_reply_on_sunday_cancels_the_email_approved_on_saturday(world, clock, monkeypatch):
    only_jobs(monkeypatch, "inbox", "followups")
    clock(datetime(2026, 11, 7, 14, 0))
    lead_id = seed(world, age_days=10)
    sent_email(world, lead_id, FIRST_SENT)
    fid = waiting_email(world, lead_id, 2, "approved", "sales1")
    runner.tick(world, trigger="scheduler")
    assert followup(world, fid)[0] == "approved" and FakeSMTP.sent == []  # Saturday: it waits
    with world.db.session() as s:
        addr = s.get(Lead, lead_id).contact.email
    FakeIMAP.messages = {1: mail(f"Someone <{addr}>", "Re: Step 1", "Thanks, please call me next week.", in_reply_to=f"<lead{lead_id}-step1@us.example>")}
    clock(datetime(2026, 11, 8, 15, 0))
    runner.tick(world, trigger="scheduler")
    assert followup(world, fid)[0] == "cancelled" and lead_state(world, lead_id)[1] == "replied"
    clock(datetime(2026, 11, 9, 14, 0))  # Monday, inside the window
    runner.tick(world, trigger="scheduler")
    assert FakeSMTP.sent == []


def test_ok_approved_late_on_friday_goes_out_at_the_first_run_of_monday_winter_time(world, clock, monkeypatch):
    """Friday 18:30 New York, then Monday: not at 12:00 UTC (08:00 in summer), at 13:00 UTC, once."""
    only_jobs(monkeypatch, "inbox", "followups")
    clock(datetime(2026, 11, 20, 23, 30))
    lead_id = seed(world, age_days=10)
    sent_email(world, lead_id, FIRST_SENT)
    waiting_email(world, lead_id, 2, "approved", "sales1")
    t = datetime(2026, 11, 20, 23, 45)
    while t <= datetime(2026, 11, 23, 15, 0):
        clock(t)
        runner.tick(world, trigger="scheduler")
        if t < datetime(2026, 11, 23, 13, 0):
            assert FakeSMTP.sent == [], t
        t += timedelta(hours=1)
    assert len(FakeSMTP.sent) == 1


def test_ok_mailbox_limit_rolls_over_after_24_hours_across_the_clock_change(world, clock):
    """The first Monday in winter time: the limit counts a rolling 24 hours, so the waiting email goes on Tuesday."""
    world.settings.business["outreach"]["mailbox_daily_limit"] = 1
    clock(datetime(2026, 11, 2, 13, 30))
    a, b = seed(world, age_days=10, n=1), seed(world, age_days=10, n=2)
    for lead_id in (a, b):
        with world.db.session() as s:
            s.get(Lead, lead_id).outreach_status = "approved"
        waiting_email(world, lead_id, 1, "approved", "sales1")
    assert sales_run(world, utcnow()).emails_sent == 1
    clock(datetime(2026, 11, 2, 22, 30))
    assert sales_run(world, utcnow()).emails_sent == 0
    clock(datetime(2026, 11, 3, 13, 45))
    assert sales_run(world, utcnow()).emails_sent == 1


def test_ok_stalled_task_marked_done_stays_done_until_ten_more_silent_days(world, clock):
    clock(datetime(2026, 11, 2, 15, 0))
    lead_id = seed(world, age_days=10)
    sent_email(world, lead_id, FIRST_SENT)
    with world.db.session() as s:
        lead = s.get(Lead, lead_id)
        lead.outreach_status = "interested"  # they replied; the deal is open and then nothing happens for 10 days
        s.add(Opportunity(lead_id=lead_id, company_id=lead.company_id, name="Acme deal", stage="new", probability=10))
    when = clock(datetime(2026, 11, 14, 15, 0))
    assert sales_run(world, when).pipeline["stalled_tasks"] == 1
    with world.db.session() as s:
        fid = s.execute(select(FollowUp.id).where(FollowUp.subject == "Stalled: Acme deal")).scalar_one()
    r = TestClient(create_app(world)).post(f"/followups/{fid}/done", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    when = clock(when + timedelta(hours=1))
    assert sales_run(world, when).pipeline["stalled_tasks"] == 0, "a person dealt with it an hour ago"
    when = clock(when + timedelta(days=11))
    assert sales_run(world, when).pipeline["stalled_tasks"] == 1
