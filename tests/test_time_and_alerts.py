"""What the passage of time must not do: send before the mailbox is read, send on a holiday, lose a day of the report, end a
sequence by score, or nag. Synthetic data on the real agents and jobs."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

import aime.timeutil
import aime.workflows as workflows
from aime.agents.analytics import AnalyticsReportingAgent
from aime.agents.inbox_agent import InboxAgent
from aime.agents.marketing_manager import MarketingManagerAgent
from aime.agents.publishing import AuthorizedPublishingAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.calendar_slots import holiday_on, us_holidays
from aime.config import DEFAULT_BUSINESS
from aime.crm_ops import log_activity
from aime.inbox import Mailbox
from aime.integrations.email_smtp import EmailSender
from aime.models import (Activity, AgentRun, Alert, Company, Contact, ContentItem, FollowUp, InboundEmail, JobRun, Lead, MarketingPlan,
                         Report, ResearchSnapshot, Signal)
from aime.scheduler import runner
from aime.timeutil import utcnow
from aime.workflows import JobOutcome
from tests.test_inbound_sales import FakeSMTP
from tests.test_inbox import FakeIMAP
from tests.test_publishing import AUTH, add_token, linkedin_api

MAIL = dict(company_postal_address="1 Sample St, Austin, TX 78701", outbound_email_enabled=True, smtp_host="smtp.example",
            smtp_from="hello@us.example", smtp_username="hello@us.example", smtp_password="x", imap_host="imap.example",
            imap_username="hello@us.example", imap_password="x")
IN_WINDOW = datetime(2026, 11, 4, 15, 0)  # Wednesday 10:00 New York
FOOTER = "Hello.\n\nTo opt out, reply unsubscribe.\n1 Sample St, Austin, TX 78701"
N = iter(range(1, 100000))


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET)

    def set_to(when: datetime) -> datetime:
        aime.timeutil.CLOCK_OFFSET = when - datetime.now(timezone.utc).replace(tzinfo=None)
        return when

    return set_to


def add_lead(ctx, *, status="qualified", stage=None, email=True, grade="B", score=60) -> int:
    i = next(N)
    with ctx.db.session() as s:
        co = Company(name=f"Acme {i}", normalized_name=f"acme {i}", domain=f"acme{i}.example", source="hn_hiring",
                     website=f"https://acme{i}.example", website_status="verified", state="TX", country="US")
        s.add(co)
        s.flush()
        ct = None
        if email:
            ct = Contact(company_id=co.id, email=f"info@acme{i}.example", source="website", role_category="inbox",
                         email_status="role_address", email_source_url=f"https://acme{i}.example/contact", consent_status="none")
            s.add(ct)
            s.flush()
        lead = Lead(company_id=co.id, contact_id=ct.id if ct else None, source="research", status=status, grade=grade, score=score,
                    primary_service="ai_agents", outreach_status=stage, last_scored_at=utcnow())
        s.add(lead)
        s.flush()
        return lead.id


def add_mail(ctx, lead_id, step=1, status="approved", by="sales1", created=None, approved=None, **kw) -> int:
    now = utcnow()
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        f = FollowUp(lead_id=lead_id, contact_id=lead.contact_id, company_id=lead.company_id, channel="email", kind="outreach", step=step,
                     status=status, subject=f"Step {step}", body=FOOTER, to_email=lead.contact.email, due_at=now,
                     approved_by=by if status == "approved" else None, approved_at=(approved or now) if status == "approved" else None,
                     created_at=created or now, **kw)
        s.add(f)
        s.flush()
        return f.id


def status_of(ctx, fid) -> tuple:
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        return f.status, f.blocked_reason


def mail_world(make_ctx, monkeypatch, imap=FakeIMAP):
    ctx = make_ctx(**MAIL)
    FakeIMAP.messages, FakeSMTP.sent = {}, []
    monkeypatch.setattr(workflows, "InboxAgent", lambda c: InboxAgent(c, mailbox=Mailbox(c.settings, imap_factory=imap)))
    monkeypatch.setattr(workflows, "SalesWorkflowAgent", lambda c: SalesWorkflowAgent(c, sender=EmailSender(c.settings, smtp_factory=FakeSMTP)))
    return ctx


class DownIMAP(FakeIMAP):
    def __init__(self, *a, **k):
        raise OSError("connection refused")


# ------------------------------------------------------------------ read first, send second
def test_only_the_followups_job_sends(make_ctx, monkeypatch, clock):
    ctx = mail_world(make_ctx, monkeypatch)
    clock(IN_WINDOW)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    monkeypatch.setattr(workflows, "USBusinessResearchAgent", lambda c: SalesWorkflowAgent(c))  # any "ok" research step
    assert workflows.SalesWorkflowAgent(ctx).run({"send": False}).ok
    assert FakeSMTP.sent == [] and status_of(ctx, fid)[0] == "approved"
    workflows.job_inbox(ctx)
    workflows.job_followups(ctx)
    assert len(FakeSMTP.sent) == 1 and status_of(ctx, fid)[0] == "sent"


def test_mailbox_down_holds_every_send_with_a_clear_reason_until_it_is_read(make_ctx, monkeypatch, clock):
    ctx = mail_world(make_ctx, monkeypatch, imap=DownIMAP)
    clock(IN_WINDOW)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    assert workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    state, why = status_of(ctx, fid)
    assert FakeSMTP.sent == [] and state == "approved" and "could not be read" in why
    with ctx.db.session() as s:
        assert [a.key for a in s.execute(select(Alert).where(Alert.key == "mail:not_read")).scalars()] == ["mail:not_read"]
    monkeypatch.setattr(workflows, "InboxAgent", lambda c: InboxAgent(c, mailbox=Mailbox(c.settings, imap_factory=FakeIMAP)))
    assert not workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    assert len(FakeSMTP.sent) == 1 and status_of(ctx, fid)[0] == "sent"
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.key == "mail:not_read")).scalars().one().acknowledged


def test_an_old_read_does_not_count(make_ctx, monkeypatch, clock):
    ctx = mail_world(make_ctx, monkeypatch)
    clock(IN_WINDOW - timedelta(minutes=30))
    workflows.job_inbox(ctx)
    clock(IN_WINDOW)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    workflows.job_followups(ctx)
    assert FakeSMTP.sent == [] and "could not be read" in status_of(ctx, fid)[1]


def test_no_mailbox_set_up_has_nothing_to_read_first(make_ctx):
    assert workflows.mailbox_hold(make_ctx()) is None


def test_waking_sends_at_most_the_per_run_limit(make_ctx, monkeypatch, clock):
    """Research and followups both used to send on waking: 10 in a minute against a limit of 5."""
    ctx = mail_world(make_ctx, monkeypatch)
    for name in list(runner.JOBS):
        if name not in ("research", "inbox", "followups"):
            monkeypatch.setitem(runner.JOBS, name, lambda c, job=name: JobOutcome())
    import aime.agents.research as research_module
    monkeypatch.setattr(research_module, "get_sources", lambda c, names=None: [])
    ctx.settings.schedule["timezone"] = "Asia/Kolkata"
    clock(IN_WINDOW)
    for _ in range(8):
        add_mail(ctx, add_lead(ctx, stage="approved"))
    assert runner.tick(ctx, trigger="scheduler")["status"] == "ok"
    assert len(FakeSMTP.sent) == 5


# ------------------------------------------------------------------ the holiday rule
def test_holiday_dates_follow_the_calendar():
    assert us_holidays(2026)["thanksgiving"][1] == date(2026, 11, 26)
    assert us_holidays(2026)["independence_day"][1] == date(2026, 7, 3)  # 4 July 2026 is a Saturday
    assert us_holidays(2026)["memorial_day"][1] == date(2026, 5, 25)
    assert us_holidays(2027)["christmas"][1] == date(2027, 12, 24)  # a Saturday
    assert holiday_on(date(2027, 12, 31), ["new_years_day"]) == "New Year's Day"  # 1 Jan 2028 is a Saturday
    assert holiday_on(date(2026, 11, 11), DEFAULT_BUSINESS["followup"]["holidays"]) is None  # most businesses work
    assert holiday_on(date(2026, 12, 24), ["2026-12-24"]) and holiday_on(date(2026, 12, 23), ["2026-12-24"]) is None


def test_nothing_goes_on_thanksgiving_and_it_goes_the_next_working_day(make_ctx, clock):
    ctx = make_ctx(**MAIL)
    FakeSMTP.sent = []
    agent = SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP))
    clock(datetime(2026, 11, 26, 15, 0))
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    agent.run({})
    assert FakeSMTP.sent == [] and "Thanksgiving" in status_of(ctx, fid)[1]
    clock(datetime(2026, 11, 30, 15, 0))  # Monday: the Friday after is on the list too
    agent.run({})
    assert len(FakeSMTP.sent) == 1


# ------------------------------------------------------------------ a person's sequence and the score
def test_a_downgrade_done_before_this_rule_existed_is_undone_for_a_running_sequence(make_ctx, clock):
    ctx = make_ctx()
    lead_id = add_lead(ctx, status="nurture", stage="contacted", grade="C", score=40)
    add_mail(ctx, lead_id, status="sent", sent_at=utcnow() - timedelta(days=3))
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        log_activity(s, "Lead qualified → nurture (score 40, grade C)", "qualification", type_="status_change", lead_id=lead_id,
                     company_id=lead.company_id)
    assert LeadQualificationAgent(ctx).run({"lead_ids": [lead_id]}).ok
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).status == "qualified"


def test_a_person_who_parked_a_contacted_lead_is_not_overruled(make_ctx):
    ctx = make_ctx()
    lead_id = add_lead(ctx, status="nurture", stage="contacted", grade="C", score=40)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        log_activity(s, "Status qualified → nurture (manual)", "sales1", type_="status_change", lead_id=lead_id, company_id=lead.company_id)
    assert LeadQualificationAgent(ctx).run({"lead_ids": [lead_id]}).ok
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).status == "nurture"


def test_agents_do_not_count_as_a_person_touching_a_lead(make_ctx):
    ctx = make_ctx()
    lead_id = add_lead(ctx, grade="C", score=10)  # no evidence: the score alone would move it
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        for actor in ("sales_workflow", "qualification", "research", "company_research", "command line"):
            log_activity(s, "Something", actor, lead_id=lead_id, company_id=lead.company_id)
    assert LeadQualificationAgent(ctx).run({"lead_ids": [lead_id]}).ok
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).status == "nurture"


def test_a_running_sequence_still_stops_for_a_competitor_or_an_opt_out(make_ctx, clock):
    """Only the grade stops deciding; a suppressed address or a competitor still ends it."""
    ctx = make_ctx(**MAIL)
    FakeSMTP.sent = []
    clock(IN_WINDOW)
    lead_id = add_lead(ctx, stage="contacted", grade="C", score=40)
    add_mail(ctx, lead_id, status="sent", sent_at=utcnow() - timedelta(days=5))
    fid = add_mail(ctx, lead_id, step=2)
    with ctx.db.session() as s:
        s.get(Lead, lead_id).company.is_competitor = True
    SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run({})
    assert FakeSMTP.sent == [] and status_of(ctx, fid)[0] == "cancelled"


def test_grade_c_lead_with_a_sent_email_gets_no_review_task(make_ctx):
    ctx = make_ctx()
    lead_id = add_lead(ctx, stage="contacted", grade="C", score=40)
    add_mail(ctx, lead_id, status="sent", sent_at=utcnow())
    SalesWorkflowAgent(ctx).run({})
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.channel == "task")).first() is None


def test_stuck_drafted_stage_is_corrected_and_logged(make_ctx):
    """Production's disqualified lead with a cancelled email: the job closes it on its next run, on the record."""
    ctx = make_ctx()
    lead_id = add_lead(ctx, status="disqualified", stage="drafted")
    add_mail(ctx, lead_id, status="cancelled")
    SalesWorkflowAgent(ctx).run({})
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).outreach_status is None
        assert s.execute(select(Activity).where(Activity.lead_id == lead_id, Activity.type == "outreach",
                                                Activity.subject.like("%stage closed%"))).first()


def test_rejected_first_email_is_not_drafted_again_and_gets_no_new_task(make_ctx):
    ctx = make_ctx()
    lead_id = add_lead(ctx, stage="stopped")
    add_mail(ctx, lead_id, status="rejected")
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(FollowUp(lead_id=lead_id, company_id=lead.company_id, channel="task", step=1, status="done", subject="Review",
                       body="x", due_at=utcnow(), blocked_reason="Outreach email drafted; it is waiting for your approval"))
    SalesWorkflowAgent(ctx).run({})
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp.status).where(FollowUp.lead_id == lead_id, FollowUp.status.in_(("pending", "awaiting_approval")))).first() is None


def test_task_a_person_closed_is_not_recreated(make_ctx):
    ctx = make_ctx()
    lead_id = add_lead(ctx, email=False)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(FollowUp(lead_id=lead_id, company_id=lead.company_id, channel="task", step=1, status="done", subject="Review", body="x", due_at=utcnow()))
    SalesWorkflowAgent(ctx).run({})
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.status == "pending")).first() is None


def test_review_task_is_ready_to_use_by_hand_and_guesses_nothing(make_ctx):
    ctx = make_ctx()
    lead_id = add_lead(ctx, email=False)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(ResearchSnapshot(company_id=lead.company_id, status="done", fetched_at=utcnow(), people=[
            {"name": "Dana Lee", "title": "CEO", "url": "https://acme.example/team"}],
            pages=[{"url": "https://acme.example/contact", "kind": "contact", "status": "ok"}, {"url": "https://acme.example", "kind": "home", "status": "ok"}]))
        s.add(Signal(company_id=lead.company_id, signal_type="hiring", text="Hiring", source="hn_hiring", source_url="https://news.example/1", fingerprint=f"s{lead_id}",
                     observed_at=utcnow(), strength=1.0, matched_services=["ai_agents"], keywords=[]))
    SalesWorkflowAgent(ctx).run({})
    with ctx.db.session() as s:
        body = s.execute(select(FollowUp.body).where(FollowUp.lead_id == lead_id, FollowUp.channel == "task")).scalar_one()
    for part in ("no published business email", "2 page(s)", "Dana Lee, CEO", "https://acme.example/contact", "Hi Dana,", "no address is guessed"):
        assert part in body, part
    assert "@" not in body and "[outreach]" not in body


def test_an_open_review_task_written_earlier_is_brought_up_to_date_once(make_ctx):
    ctx = make_ctx()
    lead_id = add_lead(ctx, email=False)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(FollowUp(lead_id=lead_id, company_id=lead.company_id, channel="task", step=1, status="pending", subject=f"Review {lead.company.name}",
                       body="Qualified research lead.", due_at=utcnow()))
    assert SalesWorkflowAgent(ctx).run({}).output.tasks_created == 0
    with ctx.db.session() as s:
        tasks = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.channel == "task")).scalars().all()
        assert len(tasks) == 1 and "no published business email" in tasks[0].body


# ------------------------------------------------------------------ emails waiting for a person
def test_one_reminder_while_drafts_wait_and_it_closes_when_they_are_gone(make_ctx, clock):
    ctx = make_ctx()
    clock(IN_WINDOW)
    lead_id = add_lead(ctx, stage="contacted")
    fid = add_mail(ctx, lead_id, step=2, status="awaiting_approval")
    fresh = add_mail(ctx, add_lead(ctx, stage="drafted"), status="awaiting_approval")
    agent = SalesWorkflowAgent(ctx)
    agent.run({})

    def open_reminders():
        with ctx.db.session() as s:
            return [a.message for a in s.execute(select(Alert).where(Alert.key == "drafts:waiting", Alert.acknowledged.is_(False))).scalars()]

    assert open_reminders() == []
    clock(IN_WINDOW + timedelta(days=3))
    agent.run({})
    agent.run({})
    assert len(open_reminders()) == 1 and "2 email(s)" in open_reminders()[0]
    with ctx.db.session() as s:
        s.get(FollowUp, fid).status = "rejected"
        s.get(FollowUp, fresh).status = "rejected"
    agent.run({})
    assert open_reminders() == []


def test_old_draft_asks_to_be_read_again_but_is_not_cancelled_and_a_fresh_approval_sends_it(make_ctx, clock):
    ctx = make_ctx(**MAIL)
    FakeSMTP.sent = []
    clock(IN_WINDOW)
    lead_id = add_lead(ctx, stage="drafted")
    fid = add_mail(ctx, lead_id, status="awaiting_approval")
    clock(IN_WINDOW + timedelta(days=20))
    agent = SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP))
    agent.run({})
    state, why = status_of(ctx, fid)
    assert state == "awaiting_approval" and why.startswith("Written 20 days ago") and FakeSMTP.sent == []
    with ctx.db.session() as s:  # a person looks at it today and decides: that decision stands
        f = s.get(FollowUp, fid)
        f.status, f.approved_by, f.approved_at = "approved", "sales1", utcnow()
    agent.run({})
    assert len(FakeSMTP.sent) == 1 and status_of(ctx, fid)[0] == "sent"


def test_approval_that_never_went_out_goes_back_to_a_person_after_two_weeks(make_ctx, clock):
    ctx = make_ctx(**MAIL)
    FakeSMTP.sent = []
    clock(IN_WINDOW)
    lead_id = add_lead(ctx, stage="approved")
    fid = add_mail(ctx, lead_id, approved=utcnow() - timedelta(days=15))
    SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run({})
    state, why = status_of(ctx, fid)
    assert FakeSMTP.sent == [] and state == "awaiting_approval" and "15 days ago" in why


# ------------------------------------------------------------------ the daily report
def daily(ctx, when: datetime, monkeypatch) -> tuple[Report, str]:
    import aime.agents.analytics as analytics_module

    class SimDT(datetime):
        @classmethod
        def now(cls, tz=None):
            real = datetime.now(timezone.utc) + aime.timeutil.CLOCK_OFFSET
            return real.astimezone(tz) if tz else real.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(analytics_module, "datetime", SimDT)
    aime.timeutil.CLOCK_OFFSET = when - datetime.now(timezone.utc).replace(tzinfo=None)
    res = AnalyticsReportingAgent(ctx).run({"kind": "daily", "import_metrics": False})
    assert res.ok, res.error
    with ctx.db.session() as s:
        row = s.get(Report, res.output.report_id)
        s.expunge(row)
    return row, Path(res.output.path_md).read_text(encoding="utf-8")


def add_company_at(ctx, when: datetime) -> None:
    with ctx.db.session() as s:
        i = next(N)
        s.add(Company(name=f"Seen {i}", normalized_name=f"seen {i}", source="test", created_at=when))


def test_report_windows_meet_with_no_gap_and_no_overlap_across_a_sleep(make_ctx, clock, monkeypatch):
    """Tue 20:30, asleep over Wed 20:00, awake Thu 03:30, Thu 20:30, all on this PC: each company is counted exactly once."""
    ctx = make_ctx()
    ctx.settings.schedule["timezone"] = "Asia/Kolkata"
    tuesday, catch_up, thursday = datetime(2026, 11, 3, 15, 0), datetime(2026, 11, 4, 22, 0), datetime(2026, 11, 5, 15, 0)
    clock(tuesday)
    first, _ = daily(ctx, tuesday, monkeypatch)
    add_company_at(ctx, tuesday + timedelta(hours=2))  # inside what a 24 h window from the catch-up would miss
    second, md = daily(ctx, catch_up, monkeypatch)
    add_company_at(ctx, catch_up + timedelta(hours=1))
    third, _ = daily(ctx, thursday, monkeypatch)
    assert [str(r.report_date) for r in (first, second, third)] == ["2026-11-03", "2026-11-04", "2026-11-05"]
    assert second.summary["kpis"]["companies_new_period"] == 1 and third.summary["kpis"]["companies_new_period"] == 1
    assert second.summary["window"]["from"] == first.summary["window"]["to"]
    assert third.summary["window"]["from"] == second.summary["window"]["to"]
    assert "31 hours ago" in md


def test_report_written_again_for_its_day_keeps_its_start(make_ctx, clock, monkeypatch):
    ctx = make_ctx()
    ctx.settings.schedule["timezone"] = "UTC"
    at = datetime(2026, 11, 3, 21, 0)
    clock(at)
    first, _ = daily(ctx, at, monkeypatch)
    again, _ = daily(ctx, at + timedelta(hours=1), monkeypatch)
    assert first.id == again.id and first.summary["window"]["from"] == again.summary["window"]["from"]


def test_report_counts_reach_and_shows_the_qualified_trend(make_ctx, clock, monkeypatch):
    ctx = make_ctx()
    ctx.settings.schedule["timezone"] = "UTC"
    add_lead(ctx, email=False)
    closed = add_lead(ctx, email=False)
    reachable = add_lead(ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, closed)
        s.add(FollowUp(lead_id=closed, company_id=lead.company_id, channel="task", step=1, status="done", subject="Review", body="x", due_at=utcnow()))
    at = datetime(2026, 11, 3, 21, 0)
    clock(at)
    daily(ctx, at, monkeypatch)
    with ctx.db.session() as s:
        s.get(Lead, reachable).status = "nurture"
    _, md = daily(ctx, at + timedelta(days=1), monkeypatch)
    assert "2 of 2 qualified leads have no usable address" in md
    assert "1 qualified lead(s) have nothing open" in md
    assert "Qualified leads, report by report: 3, 2." in md


# ------------------------------------------------------------------ what piles up
def test_run_history_older_than_90_days_goes_and_nothing_else(make_ctx):
    ctx = make_ctx()
    old, now = utcnow() - timedelta(days=100), utcnow()
    with ctx.db.session() as s:
        s.add_all([JobRun(job="inbox", trigger="scheduler", status="success", started_at=old),
                   JobRun(job="inbox", trigger="scheduler", status="success", started_at=old + timedelta(hours=1)),  # newest ok of old ones
                   JobRun(job="inbox", trigger="scheduler", status="success", started_at=now),
                   JobRun(job="plan", trigger="scheduler", status="success", started_at=old),  # the job's only run: kept
                   JobRun(job="inbox", trigger="scheduler", status="running", started_at=old)])  # not finished: kept
        for i, when in enumerate((old, now)):
            s.add(AgentRun(run_id=f"r{i}", agent="crm", job="crm_maintenance", status="success", started_at=when))
        lead_activity = Activity(type="note", subject="Old note", actor="sales1", created_at=old)
        s.add(lead_activity)
    assert workflows.prune_run_history(ctx) == 3
    with ctx.db.session() as s:
        assert sorted(j for j in s.execute(select(JobRun.job)).scalars()) == ["inbox", "inbox", "plan"]
        assert len(s.execute(select(AgentRun.id)).all()) == 1
        assert s.execute(select(Activity).where(Activity.subject == "Old note")).first()


def test_the_daily_maintenance_job_prunes(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        for hours in (0, 1):
            s.add(JobRun(job="inbox", trigger="scheduler", status="success", started_at=utcnow() - timedelta(days=100, hours=hours)))
        s.add(JobRun(job="inbox", trigger="scheduler", status="success", started_at=utcnow()))
    out = workflows.job_db_maintenance(ctx)
    assert out.steps[0]["output"]["history_pruned"] == 2


def test_publishing_alert_closes_older_copies_of_itself(make_ctx):
    """Production has five open: "1 approved post(s)...", "2", "3", "3", "4". The next run leaves one."""
    ctx = make_ctx(publishing_enabled=False)
    with ctx.db.session() as s:
        for n in (1, 2, 3):
            s.add(Alert(level="warning", source="publishing", message=f"{n} approved post(s) are due but cannot be published: off"))
        s.add(ContentItem(title="A post", body="Text", service="custom_crm", industry="saas", generator="template", fingerprint="p1", status="approved",
                          approved_by="admin", approved_at=utcnow(), scheduled_for=utcnow() - timedelta(hours=1)))
    assert AuthorizedPublishingAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        still = s.execute(select(Alert).where(Alert.source == "publishing", Alert.acknowledged.is_(False))).scalars().all()
        assert len(still) == 1 and still[0].message.startswith("1 approved post(s)") and still[0].key == "publishing:due"


def test_old_hand_reassignment_notices_close(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        s.add(Alert(level="info", source="handover", message="Assigned lead #5 from admin to sales1: x (by admin)"))
        s.add(Alert(level="warning", source="handover", message="Unassigned deal: Acme: nobody is available"))
    SalesWorkflowAgent(ctx).run({})
    with ctx.db.session() as s:
        assert [a.message[:10] for a in s.execute(select(Alert).where(Alert.source == "handover", Alert.acknowledged.is_(False))).scalars()] == ["Unassigned"]


# ------------------------------------------------------------------ switching publishing on
def test_late_posts_keep_their_approval_and_their_order_and_drafts_give_way(make_ctx):
    ctx = make_ctx(handler=linkedin_api([]), **AUTH)
    add_token(ctx)
    from aime.calendar_slots import free_slots

    with ctx.db.session() as s:
        slots = free_slots(s, ctx.settings)
        def post(name, status, when):
            item = ContentItem(service="ai_agents", industry="saas", title=name, body="Hello", hashtags=[], status=status, generator="template",
                               fingerprint=name, scheduled_for=when, approved_at=utcnow() if status == "approved" else None,
                               approved_by="admin" if status == "approved" else None)
            s.add(item)
            s.flush()
            return item.id
        first = post("first", "approved", utcnow() - timedelta(days=5))
        second = post("second", "approved", utcnow() - timedelta(days=4))
        later = post("later", "approved", slots[1])  # after the late ones come in, this one must follow them
        draft = post("draft", "awaiting_approval", slots[0])
    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert out.published == []
    with ctx.db.session() as s:
        got = {i: s.get(ContentItem, i) for i in (first, second, later, draft)}
        assert all(got[i].status == "approved" for i in (first, second, later)) and got[draft].status == "awaiting_approval"
        assert utcnow() < got[first].scheduled_for < got[second].scheduled_for < got[later].scheduled_for
        assert got[draft].scheduled_for not in {got[i].scheduled_for for i in (first, second, later)}


def test_approved_post_on_time_is_never_moved_earlier(make_ctx):
    ctx = make_ctx(handler=linkedin_api([]), **AUTH)
    add_token(ctx)
    from aime.calendar_slots import free_slots

    with ctx.db.session() as s:
        slots = free_slots(s, ctx.settings)
        far = slots[-1]
        late = ContentItem(service="ai_agents", industry="saas", title="late", body="Hello", hashtags=[], status="approved", generator="template",
                           fingerprint="late", scheduled_for=utcnow() - timedelta(days=5), approved_at=utcnow(), approved_by="admin")
        keep = ContentItem(service="ai_agents", industry="saas", title="keep", body="Hello", hashtags=[], status="approved", generator="template",
                           fingerprint="keep", scheduled_for=far, approved_at=utcnow(), approved_by="admin")
        s.add_all([late, keep])
        s.flush()
        keep_id = keep.id
    assert AuthorizedPublishingAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        assert s.get(ContentItem, keep_id).scheduled_for == far


# ------------------------------------------------------------------ the plan uses results
def test_plan_prefers_the_industry_where_prospects_answered(make_ctx):
    ctx = make_ctx()
    ctx.settings.business.setdefault("targeting", {})["industries"] = ["saas", "real_estate", "ecommerce", "smb"]
    ctx.settings.business["research"]["industries_per_run"] = 2
    with ctx.db.session() as s:
        for ind, score in (("real_estate", 90), ("ecommerce", 50)):
            for i in range(3):
                co = Company(name=f"{ind} {i}", normalized_name=f"{ind} {i}", industry=ind, source="test")
                s.add(co)
                s.flush()
                lead = Lead(company_id=co.id, source="research", status="qualified", score=score, grade="B", last_scored_at=utcnow())
                s.add(lead)
                s.flush()
                if ind == "ecommerce" and i == 0:
                    s.add(InboundEmail(message_id="<a@x.example>", from_email="a@x.example", subject="Re", body_text="x", new_text="x",
                                       received_at=utcnow(), processed_at=utcnow(), category="interested", confidence="high", status="actioned",
                                       lead_id=lead.id, is_simulated=False))
    assert MarketingManagerAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        plan = s.execute(select(MarketingPlan)).scalars().one()
    assert plan.focus_industries == ["saas", "ecommerce"]
