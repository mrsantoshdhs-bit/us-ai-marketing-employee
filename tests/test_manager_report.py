"""The Sales Manager's report (aime/manager_report.py): every figure equals what the records hold, counted by hand
here; role addresses, test-mode deliveries and simulated activity never count; a period with nothing in it says "no
data"; the report is always stored, and emailed only when every gate is open, at most once per period."""

import itertools
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aime import automation as am
from aime import manager_report as mr
from aime.agents.analytics import sales_kpis
from aime.cli import cmd_user
from aime.models import (Activity, AgentRun, AuditEvent, Company, Contact, FollowUp, InboundEmail, JobRun, JobState, Lead,
                         Meeting, Opportunity, Report, ReportDelivery, StageChange, Suppression)
from aime.scheduler.runner import JobSpec, is_due, load_job_specs, next_run, run_job
from aime.web.app import create_app
from tests.test_dashboard_auth import GOOD, ORIGIN, Args, _login

IST = ZoneInfo("Asia/Kolkata")
# 05:30 IST on Tue 27 Oct 2026: the daily report covers Mon 26 Oct IST = 25 Oct 18:30 UTC to 26 Oct 18:30 UTC
NOW = datetime(2026, 10, 27, 0, 0)
IN = datetime(2026, 10, 26, 6, 0)  # inside the period (11:30 IST on the 26th)
BEFORE = datetime(2026, 10, 25, 12, 0)  # the 25th IST: before the period
AFTER = datetime(2026, 10, 26, 20, 0)  # 01:30 IST on the 27th: after it
DAILY = mr.Period("daily", date(2026, 10, 26), date(2026, 10, 27), "Asia/Kolkata")
OURS = "digitalhub.example"
MANAGER = f"manager@{OURS}"
EMAIL_READY = dict(outbound_email_enabled=True, smtp_host="smtp.example", smtp_from=f"reports@{OURS}",
                   company_postal_address="1 Sample St, Austin, TX 78701")


class FakeSMTP:
    sent: list = []
    fail_connect = False
    crash = False

    def __init__(self, host, port, timeout=30):
        if FakeSMTP.fail_connect:
            raise OSError("connection refused")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        if FakeSMTP.crash:
            raise KeyboardInterrupt  # the process dies while the message is being handed over
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def _fresh_smtp():
    FakeSMTP.sent, FakeSMTP.fail_connect, FakeSMTP.crash = [], False, False
    yield


def company(s, name, country="US", industry="saas", created=IN, **kw):
    co = Company(name=name, normalized_name=name.lower(), domain=f"{name.lower()}.example", source="test", country=country,
                 industry=industry, created_at=created, **kw)
    s.add(co)
    s.flush()
    return co


def contact(s, co, email, first=None, last=None, title=None, status="published", source="website", created=IN, **kw):
    c = Contact(company_id=co.id, first_name=first, last_name=last, title=title, email=email, email_status=status,
                source=source, created_at=created, **kw)
    s.add(c)
    s.flush()
    return c


def lead(s, co, **kw):
    ld = Lead(company_id=co.id, source="research", **{"status": "qualified", **kw})
    s.add(ld)
    s.flush()
    return ld


def fu(s, ld, **kw):
    base = dict(lead_id=ld.id, company_id=ld.company_id, channel="email", kind="outreach", step=1, subject="Hello", body="b",
                created_at=IN, due_at=IN)
    f = FollowUp(**{**base, **kw})
    s.add(f)
    s.flush()
    return f


MAIL_IDS = itertools.count(1)


def mail(s, ld, category, when=IN, simulated=False):
    s.add(InboundEmail(message_id=f"<m{next(MAIL_IDS)}@x.example>", from_email="p@x.example", subject="Re", category=category,
                       lead_id=ld.id if ld else None, is_simulated=simulated, received_at=when, processed_at=when))


def seed(ctx) -> dict:
    """A day of records with every kind of trap the report must not fall into; the expected figures are counted by hand."""
    with ctx.db.session() as s:
        a, b, c = company(s, "Acme"), company(s, "Bravo"), company(s, "Chola", country="IN", industry="healthcare")
        company(s, "Sampleco", is_sample=True)  # sample data: not counted
        dup = company(s, "Dupe")
        dup.merged_into_id = a.id  # merged duplicate: not counted
        old = company(s, "Oldco", created=BEFORE)  # before the period
        company(s, "Lateco", created=AFTER)  # after it

        ann = contact(s, a, "ann@acme.example", "Ann", "Lee", "CEO")  # DM, contactable, verified
        contact(s, a, "info@acme.example", status="role_address")  # company inbox: contactable, never verified
        contact(s, b, "info@bravo.example", "Bob", "Ray", "CTO")  # a DM printed with a role address: not verified
        contact(s, b, "cara@bravo.example", "Cara", "Diaz", "Office Manager")  # verified contact, not a decision-maker
        contact(s, c, "dan@chola.example", "Dan", "Fox", None, status="unchecked", source="sec_filing")  # DM, unverified
        contact(s, a, "eve@acme.example", "Eve", "Ng", "CFO", status="bounced")  # DM, not contactable
        contact(s, a, "hal@acme.example", "Hal", "Oz", "COO")  # DM, suppressed below: not contactable
        contact(s, a, "gus@acme.example", "Gus", "Ito", "CEO", created=BEFORE)  # before the period
        s.add(Suppression(value="hal@acme.example", kind="email", reason="asked", created_at=IN))

        la, lb, lc = lead(s, a, contact_id=ann.id), lead(s, b), lead(s, c, status="disqualified")
        for ld, subject, when in ((la, "Lead new → qualified (score 70, grade B)", IN),
                                  (lb, "Status new → qualified (manual)", IN), (lb, "Status nurture → qualified (manual)", IN),
                                  (lc, "Lead new → disqualified (score 10, grade D)", IN),
                                  (lc, "Lead new → qualified (score 70, grade B)", BEFORE)):
            s.add(Activity(lead_id=ld.id, company_id=ld.company_id, type="status_change", subject=subject, actor="t", created_at=when))

        f1 = fu(s, la, status="sent", sent_at=IN, approved_at=IN, to_email="ann@acme.example", delivered_to="ann@acme.example")
        fu(s, lb, status="sent", sent_at=IN, to_email="bob@bravo.example", delivered_to="test@ours.example")  # test mode
        fu(s, la, status="awaiting_approval", step=2,
           blocked_reason="Already emailed acme.example today (limit 1); it will go out tomorrow")
        fu(s, lb, status="cancelled")  # cancelled before any approval: never part of the work
        fu(s, lc, status="approved", approved_at=IN, created_at=BEFORE,
           blocked_reason="Already emailed chola.example today (limit 1); it will go out tomorrow")
        fu(s, la, status="sent", step=2, sent_at=IN, to_email="ann@acme.example", delivered_to="ann@acme.example")
        fu(s, la, kind="response", status="sent", sent_at=IN, to_email="ann@acme.example", delivered_to="ann@acme.example")
        fu(s, la, status="sent", sent_at=AFTER, created_at=AFTER, to_email="ann@acme.example", delivered_to="ann@acme.example")
        fu(s, la, channel="task", kind=None, status="done", subject="Call Ann", updated_at=IN)
        fu(s, la, channel="task", kind=None, status="done", subject="Review Acme", updated_at=IN)  # closes by itself

        ld_sim = lead(s, old)
        mail(s, la, "interested")
        mail(s, lb, "question")
        mail(s, la, "interested", simulated=True)
        mail(s, None, "question")  # nobody's lead: no prospect's reply
        mail(s, la, "bounce")
        mail(s, la, "other", when=BEFORE)
        mail(s, ld_sim, "interested", simulated=True)

        def meet(**kw):
            s.add(Meeting(**{"lead_id": la.id, "company_id": a.id, "invitee_email": "ann@acme.example", "starts_at": AFTER,
                             "ends_at": AFTER + timedelta(hours=1), "source": "manual", "created_at": IN, **kw}))
        meet()
        meet(is_simulated=True)
        meet(status="cancelled")

        o1 = Opportunity(lead_id=la.id, company_id=a.id, name="Acme deal", stage="discovery", created_at=IN)
        o2 = Opportunity(lead_id=lb.id, company_id=b.id, name="Bravo deal", stage="won", est_value=5000.0, created_at=BEFORE, closed_at=IN)
        o3 = Opportunity(lead_id=ld_sim.id, company_id=old.id, name="Simulated deal", stage="new", created_at=IN)
        s.add_all([o1, o2, o3])
        s.flush()
        s.add(StageChange(opportunity_id=o2.id, from_stage="proposal", to_stage="won", at=IN, actor="t"))
        s.add(StageChange(opportunity_id=o3.id, from_stage="new", to_stage="proposal", at=IN, actor="t"))
        s.add_all([AgentRun(run_id="r1", agent="research", status="success", started_at=IN, duration_ms=2000),
                   AgentRun(run_id="r2", agent="research", status="failed", started_at=IN, duration_ms=4000, error="x"),
                   AgentRun(run_id="r3", agent="inbox_monitor", status="success", started_at=IN, duration_ms=1000),
                   AgentRun(run_id="r4", agent="research", status="success", started_at=BEFORE, duration_ms=1000),
                   JobRun(job="research", trigger="scheduler", status="failed", started_at=IN),
                   JobRun(job="inbox", trigger="scheduler", status="success", started_at=IN)])
        return {"f1": f1.id, "ann": ann.id}


def values(data):
    return {k: m["value"] for k, m in data["metrics"].items()}


# ------------------------------------------------------------------------------------------------- the figures
def test_every_funnel_stage_matches_the_records_counted_by_hand(ctx):
    ids = seed(ctx)
    ctx.settings.business["targets"] = {"US": {"daily_companies_discovered": 4}}
    with ctx.db.session() as s:
        data = mr.build(s, ctx.settings, DAILY, NOW)
    v = values(data)
    assert data["has_data"]
    expected = {
        "companies_discovered": 3, "decision_makers": 5, "contactable_prospects": 5, "verified_contacts": 2,
        "verified_decision_makers": 1, "role_addresses": 1, "qualified_leads": 2, "disqualified_leads": 1,
        "outreach_drafted": 4, "outreach_approved": 2, "outreach_sent": 2, "leads_contacted": 1, "emails_sent": 3,
        "test_mode_deliveries": 1, "replies": 2, "interested_replies": 1, "leads_replied": 2, "contacted_and_replied": 1,
        "unsubscribes": 0, "bounces": 1, "simulated_replies": 2, "followup_emails_sent": 1, "answers_sent": 1,
        "tasks_completed": 1, "meetings_booked": 1, "simulated_meetings": 1, "opportunities_opened": 1, "deals_won": 1,
        "deals_lost": 0, "suppressions_added": 1, "failed_jobs": 1, "agent_runs": 3, "agent_failures": 1,
    }
    assert {k: v[k] for k in expected} == expected
    assert data["metrics"]["verification_rate"]["text"] == "40% (2 of 5)"
    assert data["metrics"]["engagement_rate"]["text"] == "100% (1 of 1)"
    assert data["metrics"]["bounce_rate"]["text"] == "33% (1 of 3)"
    assert data["metrics"]["verified_decision_makers"]["ids"] == [ids["ann"]]
    assert ids["f1"] in data["metrics"]["outreach_sent"]["ids"]
    assert "followups" in data["metrics"]["outreach_sent"]["sql"] and "2026-10-25 18:30" in data["metrics"]["outreach_sent"]["sql"]
    assert data["pipeline"]["won_value"] == 5000.0
    assert data["pipeline"]["stage_moves"] == [{"stage": "won", "value": 1, "ids": data["pipeline"]["stage_moves"][0]["ids"]}]
    assert {r["stage"]: r["value"] for r in data["pipeline"]["by_stage_now"]} == {"discovery": 1, "won": 1}
    held = data["blocked"]["held_sends"]
    assert len(held) == 1 and held[0]["value"] == 2  # the same reason for two domains groups together
    research = next(a for a in data["agents"] if a["agent"] == "research")
    assert (research["runs"], research["success"], research["failed"], research["avg_s"]) == (2, 1, 1, 3.0)
    us = next(c for c in data["countries"] if c["country"] == "US")["figures"]
    ind = next(c for c in data["countries"] if c["country"] == "IN")["figures"]
    assert us["companies_discovered"] == {"value": 2, "target": 4.0, "attainment": 50}
    assert ind["companies_discovered"] == {"value": 1, "target": None, "attainment": None}
    assert us["verified_decision_makers"]["value"] == 1 and us["outreach_sent"]["value"] == 2
    assert {i["industry"]: i["figures"]["companies_discovered"] for i in data["industries"]} == {"saas": 2, "healthcare": 1}


def test_the_figures_agree_with_the_dashboards_sales_kpis(ctx):
    """Where the home page's sales_kpis counts the same thing, both say the same number (its "since" figures, taken at
    the period's start minus at its end, are the period's)."""
    seed(ctx)
    with ctx.db.session() as s:
        data = mr.build(s, ctx.settings, DAILY, NOW)

        def count(model, *conds):
            return s.execute(select(func.count(model.id)).where(*conds)).scalar_one()
        at_start, at_end = sales_kpis(count, DAILY.start_utc), sales_kpis(count, DAILY.end_utc)
    period = {k: at_start[k] - at_end[k] for k in at_start if k.endswith("_period")}
    v = values(data)
    assert period["decision_makers_period"] == v["decision_makers"]
    assert period["outreach_drafted_period"] == v["outreach_drafted"]
    assert period["outreach_sent_period"] == v["outreach_sent"]
    assert period["replies_period"] == v["replies"]
    assert period["interested_period"] == v["interested_replies"]
    assert period["meetings_booked_period"] == v["meetings_booked"]
    assert period["bounces_period"] == v["bounces"]


def test_a_role_address_is_never_a_verified_decision_maker(ctx):
    with ctx.db.session() as s:
        co = company(s, "Acme")
        contact(s, co, "sales@acme.example", "Sam", "Hill", "CEO", status="published")  # named, but a company inbox
        contact(s, co, "info@acme.example", "Ivy", "Park", "Founder", status="role_address")
        data = mr.build(s, ctx.settings, DAILY, NOW)
    v = values(data)
    assert v["decision_makers"] == 2 and v["contactable_prospects"] == 2
    assert v["verified_contacts"] == 0 and v["verified_decision_makers"] == 0
    assert data["metrics"]["dm_verification_rate"]["text"] == "0% (0 of 2)"


def test_test_mode_and_simulated_activity_is_never_counted(ctx):
    with ctx.db.session() as s:
        co = company(s, "Acme")
        ld = lead(s, co)
        fu(s, ld, status="sent", sent_at=IN, to_email="ann@acme.example", delivered_to="me@ours.example")
        mail(s, ld, "interested", simulated=True)
        s.add(Meeting(lead_id=ld.id, invitee_email="a@acme.example", starts_at=IN, ends_at=IN, source="manual", is_simulated=True,
                      created_at=IN))
        s.add(Opportunity(lead_id=ld.id, company_id=co.id, name="Sim", stage="new", created_at=IN))
        v = values(mr.build(s, ctx.settings, DAILY, NOW))
    assert (v["outreach_sent"], v["emails_sent"], v["replies"], v["meetings_booked"], v["opportunities_opened"]) == (0, 0, 0, 0, 0)
    assert (v["test_mode_deliveries"], v["simulated_replies"], v["simulated_meetings"]) == (1, 1, 1)


def test_an_empty_database_says_no_data_and_draws_nothing(ctx):
    with ctx.db.session() as s:
        report = mr.store(s, ctx.settings, DAILY, NOW)
        data = report.summary["manager_report"]
        assert not data["has_data"] and report.summary["highlights"] == "no data"
        assert all(c["figures"] for c in data["countries"]) and data["countries"] == []
        assert data["metrics"]["engagement_rate"]["text"] == "no data (nothing to divide by)"
    subject, plain, html = mr.render(data, ctx.settings)
    assert "NO DATA" in plain and "No data" in html and "Daily Sales Manager Report" in subject
    page = TestClient(create_app(ctx)).get("/sales-manager?period=daily").text
    assert "No data" in page and "Nothing recorded in the last 14 days." in page


def test_the_email_body_has_no_remote_content_or_tracking(ctx):
    seed(ctx)
    with ctx.db.session() as s:
        _, plain, html = mr.render(mr.build(s, ctx.settings, DAILY, NOW), ctx.settings)
    low = html.lower()
    assert "<img" not in low and "<script" not in low and "src=" not in low and "http" not in low and "<link" not in low
    assert "Companies discovered" in plain and "Companies discovered" in html


def test_periods_on_the_report_clock():
    cfg = dict(mr.DEFAULTS)
    today = date(2026, 11, 3)  # a Tuesday
    assert mr.latest_period("daily", today, cfg) == mr.Period("daily", date(2026, 11, 2), today, "Asia/Kolkata")
    week = mr.latest_period("weekly", today, cfg)
    assert (week.start, week.end) == (date(2026, 10, 26), date(2026, 11, 2))  # Mon 26 Oct to Sun 1 Nov
    month = mr.latest_period("monthly", today, cfg)
    assert (month.start, month.end) == (date(2026, 10, 1), date(2026, 11, 1))
    assert month.start_utc == datetime(2026, 9, 30, 18, 30)  # IST midnight, no daylight saving time
    assert mr.latest_period("monthly", date(2026, 11, 3), {**cfg, "monthly_day": 5}).start == date(2026, 9, 1)


# ------------------------------------------------------------------------------------------------- the schedule
def test_the_job_runs_at_four_in_the_morning_india_time_whatever_the_pcs_zone():
    spec = JobSpec("manager_report", None, time(4, 0), "", timezone="Asia/Kolkata")
    for pc in ("America/New_York", "Europe/London", "Asia/Kolkata", "Pacific/Auckland"):
        tz = ZoneInfo(pc)
        # 04:00 IST on Sun 1 Nov 2026 is 22:30 UTC on the 31st, across the US and UK clock changes of that weekend
        for slot in (datetime(2026, 10, 24, 22, 30), datetime(2026, 10, 31, 22, 30), datetime(2026, 11, 1, 22, 30)):
            done = JobState(job="manager_report", last_success_at=slot - timedelta(days=1), last_attempt_at=slot - timedelta(days=1),
                            consecutive_failures=0, enabled=True)
            assert not is_due(spec, done, slot - timedelta(minutes=1), tz)[0], (pc, slot)
            assert is_due(spec, done, slot, tz)[0], (pc, slot)
            assert next_run(spec, done, slot - timedelta(hours=3), tz) == slot
            ran = JobState(job="manager_report", last_success_at=slot, last_attempt_at=slot, consecutive_failures=0, enabled=True)
            assert not is_due(spec, ran, slot + timedelta(hours=20), tz)[0]
            assert next_run(spec, ran, slot + timedelta(hours=1), tz) == slot + timedelta(days=1)


def test_the_schedule_entry_carries_its_zone_and_a_bad_zone_keeps_it_off(ctx):
    spec = next(sp for sp in load_job_specs(ctx.settings) if sp.name == "manager_report")
    assert (spec.daily_at, spec.timezone, spec.enabled) == (time(4, 0), "Asia/Kolkata", True)
    assert spec.schedule_label == "daily 04:00 Asia/Kolkata"
    ctx.settings.schedule["jobs"]["manager_report"]["timezone"] = "Asia/Kolkatta"
    assert not next(sp for sp in load_job_specs(ctx.settings) if sp.name == "manager_report").enabled


def test_the_super_admins_time_moves_the_job(ctx):
    with ctx.db.session() as s:
        mr.save(s, ctx.settings, "owner", {"daily_time": "05:15", "timezone": "Asia/Dubai"})
    spec = next(sp for sp in load_job_specs(ctx.settings) if sp.name == "manager_report")
    assert (spec.daily_at, spec.timezone) == (time(5, 15), "Asia/Dubai")
    with ctx.db.session() as s:
        ctx.settings.schedule["jobs"]["manager_report"] = {"daily_at": "04:00", "timezone": "Asia/Kolkata"}
        mr.load_schedule(s, ctx.settings)  # what a new process does at start-up
    assert ctx.settings.schedule["jobs"]["manager_report"]["daily_at"] == "05:15"


# ------------------------------------------------------------------------------------------------- sending
def authorized(make_ctx, **overrides):
    ctx = make_ctx(**{**EMAIL_READY, **overrides})
    with ctx.db.session() as s:
        mr.save(s, ctx.settings, "owner", {"recipients": MANAGER, "email_enabled": True})
        mr.authorize(s, ctx.settings, "owner")
    return ctx


def sent_count(ctx):
    with ctx.db.session() as s:
        return s.execute(select(func.count(ReportDelivery.id)).where(ReportDelivery.status == "sent")).scalar_one()


def test_disabled_by_default_the_job_stores_the_report_and_says_why_it_was_not_sent(ctx):
    res = run_job(ctx, "manager_report", trigger="scheduler")
    assert res["status"] == "success", res["error"]
    with ctx.db.session() as s:
        reports = s.execute(select(Report).where(Report.kind.like("manager_%"))).scalars().all()
        assert reports and {r.kind for r in reports} >= {"manager_daily"}
        reasons = reports[0].summary["delivery"]["reasons"]
        assert not s.execute(select(ReportDelivery)).first()
        run = s.get(JobRun, res["run_id"])
    assert any("switched off" in r for r in reasons) and any("OUTBOUND_EMAIL_ENABLED" in r for r in reasons)
    assert any("authorized" in r for r in reasons) and any("no recipient" in r for r in reasons)
    assert run.detail["steps"][0]["output"]["reports"][0]["status"] == "not_sent"
    assert Path(reports[0].path_html).is_file() and FakeSMTP.sent == []


def test_the_authorized_report_is_emailed_once_per_period_even_when_the_job_runs_twice(make_ctx):
    ctx = authorized(make_ctx)
    seed(ctx)
    first = mr.run(ctx, NOW, smtp_factory=FakeSMTP)
    daily = next(r for r in first["reports"] if r["kind"] == "daily")
    assert daily["status"] == "sent", daily
    n = len(FakeSMTP.sent)
    second = mr.run(ctx, NOW + timedelta(minutes=30), smtp_factory=FakeSMTP)
    assert {r["status"] for r in second["reports"]} == {"already"}
    assert len(FakeSMTP.sent) == n
    msg = next(m for m in FakeSMTP.sent if "Daily" in m["Subject"])
    assert msg["To"] == MANAGER
    assert msg.get_body(("html",)) is not None and "Companies discovered" in msg.get_body(("plain",)).get_content()
    with ctx.db.session() as s:
        report = s.get(Report, daily["report_id"])
        assert report.summary["delivery"]["sent_to"] == [MANAGER]
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "manager_report_emailed")).first()
        # an internal notification: no prospect limit is used, but the mailbox's own limit counts it
        assert s.execute(select(func.count(FollowUp.id)).where(FollowUp.status == "sent")).scalar_one() == 5
        assert mr.mailbox_sends_since(s, NOW - timedelta(hours=1)) == len(FakeSMTP.sent)


def test_a_crash_after_the_claim_never_sends_the_period_again(make_ctx):
    ctx = authorized(make_ctx)
    FakeSMTP.crash = True
    with pytest.raises(KeyboardInterrupt):
        mr.run(ctx, NOW, smtp_factory=FakeSMTP)
    FakeSMTP.crash = False
    again = mr.run(ctx, NOW + timedelta(minutes=2), smtp_factory=FakeSMTP)
    assert next(r for r in again["reports"] if r["kind"] == "daily")["status"] == "already"
    later = mr.run(ctx, NOW + timedelta(minutes=30), smtp_factory=FakeSMTP)
    assert next(r for r in later["reports"] if r["kind"] == "daily")["status"] == "already"
    with ctx.db.session() as s:
        row = s.execute(select(ReportDelivery).where(ReportDelivery.kind == "manager_daily")).scalars().one()
        assert row.status == "uncertain" and "Sent folder" in row.error
    assert not any("Daily" in m["Subject"] for m in FakeSMTP.sent)


def test_a_send_that_certainly_left_nothing_is_tried_again_once(make_ctx):
    ctx = authorized(make_ctx)
    FakeSMTP.fail_connect = True
    first = mr.run(ctx, NOW, smtp_factory=FakeSMTP)
    assert next(r for r in first["reports"] if r["kind"] == "daily")["status"] == "failed" and first["failed"]
    FakeSMTP.fail_connect = False
    retry = mr.run(ctx, NOW + timedelta(minutes=30), smtp_factory=FakeSMTP)
    assert next(r for r in retry["reports"] if r["kind"] == "daily")["status"] == "sent"
    mr.run(ctx, NOW + timedelta(minutes=60), smtp_factory=FakeSMTP)
    assert sum("Daily" in m["Subject"] for m in FakeSMTP.sent) == 1


def _close(gate, ctx):
    with ctx.db.session() as s:
        if gate == "enabled":
            mr.save(s, ctx.settings, "owner", {"email_enabled": False})
        elif gate == "recipients":
            mr.save(s, ctx.settings, "owner", {"recipients": ""})
        elif gate == "authorization":
            mr.revoke(s, "owner")
        elif gate == "outbound":
            ctx.settings.outbound_email_enabled = False
        elif gate == "mailbox":
            ctx.settings.smtp_host = ""
        elif gate == "emergency_stop":
            am.change_state(s, "emergency_stop", "owner")
        elif gate == "prospect":
            company(s, "Partner")
            contact(s, s.execute(select(Company)).scalars().first(), MANAGER, "Man", "Ager", "CEO")
        elif gate == "suppressed":
            s.add(Suppression(value=MANAGER, kind="email", reason="asked"))
        elif gate == "deliverability_pause":  # the mailbox's reputation is shared with outreach
            from aime import deliverability
            deliverability.pause(s, ctx.settings, "a bounce storm", "overall", {}, now=NOW)
        elif gate == "mailbox_limit":
            ctx.settings.business.setdefault("outreach", {})["mailbox_daily_limit"] = 1
            ld = lead(s, company(s, "Busy"))
            fu(s, ld, status="sent", sent_at=NOW - timedelta(hours=1), to_email="x@busy.example")


GATES = {"enabled": "switched off", "recipients": "no recipient", "authorization": "authorized",
         "outbound": "OUTBOUND_EMAIL_ENABLED", "mailbox": "sending mailbox", "emergency_stop": "emergency stop",
         "prospect": "prospect", "suppressed": "do-not-contact", "mailbox_limit": "daily limit",
         "deliverability_pause": "paused for deliverability"}


@pytest.mark.parametrize("gate", list(GATES))
def test_each_gate_alone_keeps_the_report_from_being_emailed(make_ctx, gate):
    ctx = authorized(make_ctx)
    _close(gate, ctx)
    res = mr.run(ctx, NOW, smtp_factory=FakeSMTP)
    daily = next(r for r in res["reports"] if r["kind"] == "daily")
    assert daily["status"] == "not_sent" and GATES[gate] in daily["reason"], daily
    assert FakeSMTP.sent == [] and sent_count(ctx) == 0
    with ctx.db.session() as s:
        assert s.get(Report, daily["report_id"]).summary["delivery"]["reasons"]


def test_the_open_gates_send(make_ctx):
    """The baseline the gate tests close one at a time: with nothing closed, it goes."""
    ctx = authorized(make_ctx)
    assert next(r for r in mr.run(ctx, NOW, smtp_factory=FakeSMTP)["reports"] if r["kind"] == "daily")["status"] == "sent"


def test_changing_the_recipients_withdraws_the_authorization(make_ctx):
    ctx = authorized(make_ctx)
    with ctx.db.session() as s:
        assert mr.authorization(mr.config(s, ctx.settings))[0]
        changes = mr.save(s, ctx.settings, "owner", {"recipients": f"{MANAGER}, director@{OURS}"})
        assert "authorization withdrawn: the recipients changed" in changes
        ok, why = mr.authorization(mr.config(s, ctx.settings))
        assert not ok and "authorize" in why
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "reporting_authorization_withdrawn")).first()
        mr.save(s, ctx.settings, "owner", {"recipients": MANAGER})  # back to what was authorized: still withdrawn
        assert not mr.authorization(mr.config(s, ctx.settings))[0]
    res = mr.run(ctx, NOW, smtp_factory=FakeSMTP)
    assert FakeSMTP.sent == [] and "authorize" in res["reports"][0]["reason"]


def test_a_prospect_a_suppressed_or_an_outside_address_is_refused(make_ctx):
    ctx = make_ctx(**EMAIL_READY)
    with ctx.db.session() as s:
        co = company(s, "Acme")
        contact(s, co, "ann@acme.example", "Ann", "Lee", "CEO")
        s.add(Suppression(value=f"gone@{OURS}", kind="email", reason="asked"))
    for addr, words in (("ann@acme.example", "not an internal"), ("boss@elsewhere.example", "not an internal"),
                        (f"gone@{OURS}", "do-not-contact")):
        with ctx.db.session() as s, pytest.raises(ValueError, match=words):
            mr.save(s, ctx.settings, "owner", {"recipients": addr})
    with ctx.db.session() as s, pytest.raises(ValueError, match="prospect"):  # allow-listed, and still a prospect
        mr.save(s, ctx.settings, "owner", {"recipients": "ann@acme.example", "allowlist": "ann@acme.example"})
    with ctx.db.session() as s, pytest.raises(ValueError, match="prospect company"):
        mr.save(s, ctx.settings, "owner", {"recipients": "someone@acme.example", "allowlist": "acme.example"})


def test_a_public_mail_sending_mailbox_makes_no_address_there_internal(make_ctx):
    ctx = make_ctx(**{**EMAIL_READY, "smtp_from": "owner.sales@fastmail.com"})
    with ctx.db.session() as s:
        with pytest.raises(ValueError, match="public mail service"):
            mr.save(s, ctx.settings, "owner", {"recipients": "stranger@fastmail.com"})
        with pytest.raises(ValueError, match="public mail service"):
            mr.save(s, ctx.settings, "owner", {"recipients": "stranger@fastmail.com", "allowlist": "fastmail.com"})
        mr.save(s, ctx.settings, "owner", {"recipients": "my.manager@fastmail.com", "allowlist": "my.manager@fastmail.com"})
        assert mr.config(s, ctx.settings)["recipients"] == ["my.manager@fastmail.com"]


def test_authorization_is_never_read_from_the_settings_file(make_ctx):
    ctx = make_ctx(**EMAIL_READY)
    ctx.settings.business["reporting"] = {"email_enabled": True, "recipients": [MANAGER], "authorized_by": "file",
                                          "authorized_at": "2026-01-01T00:00:00", "authorized_for": mr.fingerprint([MANAGER], [])}
    with ctx.db.session() as s:
        assert not mr.authorization(mr.config(s, ctx.settings))[0]
    assert mr.run(ctx, NOW, smtp_factory=FakeSMTP)["reports"][0]["status"] == "not_sent" and FakeSMTP.sent == []


def test_the_outreach_mailbox_limit_counts_report_emails(make_ctx):
    from aime.agents.sales_workflow import SalesWorkflowAgent

    ctx = authorized(make_ctx)
    mr.run(ctx, NOW, smtp_factory=FakeSMTP)
    with ctx.db.session() as s:
        limits = SalesWorkflowAgent(ctx)._outreach_limits(s, NOW + timedelta(minutes=5), {})
    assert limits["mailbox_sent"] == len(FakeSMTP.sent) >= 1
    assert limits["sent_today"] == 0  # not outreach


# ------------------------------------------------------------------------------------------------- the pages
@pytest.fixture
def org(make_ctx):
    ctx = make_ctx(dashboard_auth=True, **EMAIL_READY)
    for name, role in (("boss", "super_admin"), ("manager", "sales_manager"), ("director", "sales_director"),
                       ("lead", "team_leader"), ("ana", "analyst"), ("bde", "sales_executive")):
        assert cmd_user(Args("create", name, role=role), ctx, prompt_fn=lambda _p: GOOD) == 0
    return ctx


def client_for(ctx, username):
    client = TestClient(create_app(ctx))
    assert _login(client, username=username).status_code == 303
    return client


def test_the_dashboard_is_for_sales_managers_and_above(org):
    seed(org)
    for name in ("boss", "manager", "director"):
        page = client_for(org, name).get("/sales-manager?period=daily")
        assert page.status_code == 200 and "Sales Manager report" in page.text, name
    for name in ("lead", "ana", "bde"):
        assert client_for(org, name).get("/sales-manager").status_code == 403, name
    assert TestClient(create_app(org)).get("/sales-manager", follow_redirects=False).status_code == 303  # sign in first


def test_record_ids_are_shown_only_to_a_role_that_sees_every_record(org):
    ids = seed(org)
    marker = f"Records (contacts): {ids['ann']}"
    assert marker in client_for(org, "director").get("/sales-manager?period=daily").text
    assert marker not in client_for(org, "manager").get("/sales-manager?period=daily").text


def test_the_reporting_settings_are_super_admin_only_and_audited(org):
    for name in ("manager", "director", "ana"):
        c = client_for(org, name)
        assert c.get("/admin/reporting").status_code == 403, name
        assert c.post("/admin/reporting/authorize", headers=ORIGIN).status_code == 403, name
        assert c.post("/admin/reporting", data={"recipients": MANAGER, "email_enabled": "yes"}, headers=ORIGIN).status_code == 403
    boss = client_for(org, "boss")
    assert boss.get("/admin/reporting").status_code == 200
    saved = boss.post("/admin/reporting", data={"recipients": MANAGER, "email_enabled": "yes", "daily_time": "04:00",
                                                "timezone": "Asia/Kolkata", "weekly_day": "mon", "monthly_day": "1"},
                      headers=ORIGIN, follow_redirects=False)
    assert saved.status_code == 303 and "Saved" in saved.headers["location"]
    assert boss.post("/admin/reporting/authorize", headers=ORIGIN, follow_redirects=False).status_code == 303
    with org.db.session() as s:
        cfg = mr.config(s, org.settings)
        assert cfg["authorized_by"] == "boss" and mr.authorization(cfg)[0]
        actions = set(s.execute(select(AuditEvent.action)).scalars())
    assert {"reporting_settings_changed", "reporting_authorized"} <= actions
    refused = boss.post("/admin/reporting", data={"recipients": "x@elsewhere.example"}, headers=ORIGIN, follow_redirects=False)
    assert "Not%20saved" in refused.headers["location"]
    page = boss.get("/admin/reporting").text
    assert "Authorized" in page and "<script" not in page


def test_the_stored_report_opens_on_the_dashboard(ctx):
    seed(ctx)
    with ctx.db.session() as s:
        rid = mr.store(s, ctx.settings, DAILY, NOW).id
    c = TestClient(create_app(ctx))
    assert "Daily Sales Manager Report" in c.get(f"/reports/{rid}").text
    listing = c.get("/reports").text
    assert "Sales Manager report:" in listing and "3 companies" in listing
    assert "Mon 26 Oct 2026" in c.get("/sales-manager").text
    assert "Daily Sales Manager Report" in c.get(f"/sales-manager/reports/{rid}").text


def test_a_sales_manager_opens_the_stored_report_but_no_other_report(org):
    from aime.agents import AnalyticsReportingAgent

    seed(org)
    with org.db.session() as s:
        rid = mr.store(s, org.settings, DAILY, NOW).id
    other = AnalyticsReportingAgent(org).run({"kind": "daily", "import_metrics": False}).output.report_id
    manager = client_for(org, "manager")
    assert "Daily Sales Manager Report" in manager.get(f"/sales-manager/reports/{rid}").text
    assert manager.get(f"/sales-manager/reports/{other}").status_code == 404
    assert client_for(org, "bde").get(f"/sales-manager/reports/{rid}").status_code == 403


def test_the_ai_section_counts_the_decision_log_by_policy_verdict(make_ctx):
    from aime.models import AIDecision

    ctx = make_ctx()
    period = mr.latest_period("manager_daily", mr.local_today(mr.DEFAULTS), mr.DEFAULTS)
    at = period.start_utc + timedelta(hours=2)
    with ctx.db.session() as s:
        for verdict, esc in (("auto_internal", []), ("human_review", ["pricing"]), ("human_review", []), ("block", ["injection"])):
            s.add(AIDecision(agent="conversation", task="reply", provider="fake", verdict=verdict, escalations=esc, created_at=at))
        s.add(AIDecision(agent="conversation", task="reply", provider="fake", verdict="block", created_at=period.end_utc))
        s.commit()
        got = mr._ai_decisions(s, period)
    assert got["total"] == 4 and got["by"] == {"auto_internal": 1, "human_review": 2, "block": 1}
    assert (got["blocked"], got["human_review"], got["escalated"], got["escalated_or_blocked"]) == (1, 2, 2, 2)


def test_negative_replies_objections_handovers_pending_approvals_and_sources_are_counted_from_the_records(ctx):
    from aime.models import HandoverPackage

    seed(ctx)
    with ctx.db.session() as s:
        a = s.execute(select(Company).where(Company.name == "Acme")).scalar_one()
        a.source = "hn_hiring"
        la = s.execute(select(Lead).where(Lead.company_id == a.id)).scalars().first()
        mail(s, la, "not_interested")
        mail(s, la, "objection")
        mail(s, la, "not_interested", simulated=True)  # simulated: never counted
        mail(s, la, "not_interested", when=BEFORE)  # before the period
        o1 = s.execute(select(Opportunity).where(Opportunity.name == "Acme deal")).scalar_one()
        o3 = s.execute(select(Opportunity).where(Opportunity.name == "Simulated deal")).scalar_one()
        pkg = dict(summary="s", sections={}, conditions=[], content_hash="h", created_by="t")
        s.add_all([HandoverPackage(opportunity_id=o1.id, version=1, status="superseded", created_at=IN, **pkg),
                   HandoverPackage(opportunity_id=o1.id, version=2, status="accepted", created_at=IN, accepted_at=IN,
                                   accepted_by="manager", **pkg),  # a second version of the same deal: one handover
                   HandoverPackage(opportunity_id=o3.id, version=1, status="open", created_at=IN, **pkg)])  # simulated deal
    with ctx.db.session() as s:
        data = mr.build(s, ctx.settings, DAILY, NOW)
    v = values(data)
    assert (v["negative_replies"], v["objection_replies"]) == (1, 1)
    assert (v["handovers"], v["handovers_accepted"], v["handovers_waiting"]) == (1, 1, 0)
    assert v["pending_approvals"] == 1  # the step-2 email still awaiting approval
    assert data["metrics"]["meeting_conversion"]["text"] == "50% (1 of 2)"  # one meeting, two leads replied
    sources = {r["source"]: r["figures"]["companies_discovered"] for r in data["sources"]}
    assert sources == {"hn_hiring": 1, "test": 2}
    subject, plain, html = mr.render(data, ctx.settings)
    for text in ("Negative replies (not interested): 1", "Handovers to the Sales Manager: 1", "Emails waiting for approval: 1",
                 "BY SOURCE", "Meeting conversion: 50% (1 of 2)"):
        assert text in plain, text
    assert "By source" in html and "Sales Manager handovers" in html


def test_a_report_stored_before_the_new_figures_still_renders(ctx):
    seed(ctx)
    with ctx.db.session() as s:
        data = mr.build(s, ctx.settings, DAILY, NOW)
    for key in ("negative_replies", "objection_replies", "handovers", "handovers_accepted", "handovers_waiting",
                "pending_approvals", "meeting_conversion"):
        data["metrics"].pop(key)
    data.pop("sources")
    subject, plain, html = mr.render(data, ctx.settings)
    assert "Replies from prospects" in plain and "BY SOURCE" not in plain and "By source" not in html
