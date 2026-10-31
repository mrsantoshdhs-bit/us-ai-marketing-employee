"""What the dashboard says the AI employee is doing, read from real execution records: only an open run counts as
running (never a past completion), the next run follows the scheduler's own rules, failures and blocks are listed
with their reasons, Sales Performance draws ten real series with honest empty views, small periods show plain
differences instead of percentages, every agent has a details page, and an expired authorization approves nothing."""

from datetime import UTC, datetime, time, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aime import automation as am
from aime.models import AgentRun, AuditEvent, CampaignAuthorization, FollowUp, JobRun, JobState
from aime.scheduler.runner import JobSpec, next_run
from aime.timeutil import utcnow
from aime.web.app import ai_employee, change, create_app, dashboard_view, running_agent_runs
from tests.test_automation import authorize, drafted, followup, run
from tests.test_inbound_sales import FakeSMTP
from tests.test_outreach import SENDING, researched


def test_next_run_follows_the_scheduler_rules():
    now = datetime(2026, 10, 28, 12, 0)  # naive UTC, a Wednesday
    every = JobSpec("inbox", 15, None, "")
    assert next_run(every, None, now, UTC) == now  # never run: at the next check-in
    assert next_run(every, JobState(job="inbox", last_attempt_at=now - timedelta(minutes=5), enabled=True), now, UTC) \
        == now + timedelta(minutes=10)
    daily = JobSpec("plan", None, time(6, 45), "")
    done = JobState(job="plan", last_success_at=datetime(2026, 10, 28, 6, 46), enabled=True)
    assert next_run(daily, done, now, UTC) == datetime(2026, 10, 29, 6, 45)
    assert next_run(daily, JobState(job="plan", enabled=True), now, UTC) == now  # today's slot is still pending
    weekly = JobSpec("weekly_report", None, time(8, 0), "", weekdays=(0,))
    monday = JobState(job="weekly_report", last_success_at=datetime(2026, 10, 26, 8, 1), enabled=True)
    assert next_run(weekly, monday, now, UTC) == datetime(2026, 11, 2, 8, 0)
    assert next_run(JobSpec("x", 15, None, "", enabled=False), None, now, UTC) is None
    assert next_run(every, JobState(job="inbox", enabled=False), now, UTC) is None


def test_only_open_runs_count_as_running(make_ctx):
    ctx = make_ctx()
    now = utcnow()
    with ctx.db.session() as s:
        finished = JobRun(job="inbox", trigger="scheduler", status="success", started_at=now - timedelta(minutes=20),
                          finished_at=now - timedelta(minutes=19))
        still_open = JobRun(job="enrich", trigger="scheduler", status="running", started_at=now - timedelta(minutes=3))
        s.add_all([finished, still_open])
        s.flush()
        s.add_all([
            # a leftover "running" row of a job that has finished: not running
            AgentRun(run_id="a", agent="crm", job="inbox", job_run_id=finished.id, status="running",
                     started_at=now - timedelta(minutes=20)),
            AgentRun(run_id="b", agent="company_research", job="enrich", job_run_id=still_open.id, status="running",
                     started_at=now - timedelta(minutes=3)),
            AgentRun(run_id="c", agent="research", job="research", status="success", started_at=now - timedelta(minutes=10),
                     finished_at=now - timedelta(minutes=9)),
        ])
    with ctx.db.session() as s:
        assert [r.agent for r in running_agent_runs(s, now)] == ["company_research"]
        view = ai_employee(ctx, s)
    team = {a["agent"]: a for a in view["team"]}
    assert team["company_research"]["status"] == "RUNNING" and team["company_research"]["current"]["job"] == "enrich"
    assert team["research"]["status"] == "COMPLETED" and team["research"]["current"] is None
    assert team["crm"]["status"] != "RUNNING" and team["crm"]["current"] is None
    assert [r["agent"] for r in view["employee"]["live"]["running"]] == ["Company research"]


def test_live_workspace_reports_failures_blocks_and_approvals(make_ctx):
    ctx = make_ctx()
    now = utcnow()
    with ctx.db.session() as s:
        s.add(JobRun(job="research", trigger="scheduler", status="failed", started_at=now - timedelta(hours=1),
                     finished_at=now - timedelta(minutes=59), error="SEC EDGAR returned 503"))
        s.add(AgentRun(run_id="d", agent="analytics", job="daily_report", status="failed", started_at=now - timedelta(hours=2),
                       finished_at=now - timedelta(hours=2), error="report folder not writable"))
    with ctx.db.session() as s:
        view = ai_employee(ctx, s)
    live = view["employee"]["live"]
    assert any("SEC EDGAR returned 503" in p["why"] for p in live["problems"])
    assert any("report folder not writable" in p["why"] for p in live["problems"])
    # publishing and the mailbox are switched off in tests: blocked with the reason, not failed
    assert {b["what"] for b in live["blocked"]} == {"Publishing blocked", "Reply intelligence blocked"}
    assert {a["agent"]: a["status"] for a in view["team"]}["analytics"] == "FAILED"
    assert live["last_job"]["status"] == "failed" and live["running"] == []
    assert live["approvals"] == 0  # meetings are not approvals
    page = TestClient(create_app(ctx)).get("/command-center").text
    assert "SEC EDGAR returned 503" in page and "Failed and blocked" in page


def test_sales_performance_has_ten_real_series_and_honest_empty_views(make_ctx):
    from aime.agents.sales_workflow import SalesWorkflowAgent
    ctx = researched(make_ctx, **SENDING)
    SalesWorkflowAgent(ctx).run({})
    with ctx.db.session() as s:
        view = dashboard_view(ctx, s, 7)
        drafts_made = s.execute(select(func.count(FollowUp.id)).where(FollowUp.kind == "outreach")).scalar_one()
    names = [name for v in view["performance"] for name, _, _ in v["series"]]
    assert names == ["Companies discovered", "Leads generated", "Usable contacts", "Outreach drafts created",
                     "Emails sent", "Follow-ups completed", "Replies received", "Positive replies", "Meetings booked",
                     "Qualified opportunities"]
    totals = {name: total for v in view["performance"] for name, total, _ in v["series"]}
    assert totals["Outreach drafts created"] == drafts_made >= 1 and totals["Emails sent"] == 0
    by_key = {v["key"]: v for v in view["performance"]}
    assert f"Outreach drafts created {drafts_made}" in by_key["outreach"]["chart"]  # the chart's totals, same records
    assert "No replies, meetings or opportunities recorded in the last 7 days." in by_key["responses"]["chart"]
    page = TestClient(create_app(ctx)).get("/?range=30").text
    assert "Prospecting, the last 30 days" in page and 'id="perf-outreach"' in page


def test_research_reviews_closed_by_a_draft_are_not_follow_ups(make_ctx):
    ctx = make_ctx()
    now = utcnow()
    with ctx.db.session() as s:
        from aime.models import Company, Lead
        co = Company(name="Acme", normalized_name="acme", domain="acme.example", source="test")
        s.add(co)
        s.flush()
        lead = Lead(company_id=co.id, source="research", status="qualified")
        s.add(lead)
        s.flush()
        s.add_all([FollowUp(lead_id=lead.id, channel="task", status="done", subject="Review Acme", updated_at=now),
                   FollowUp(lead_id=lead.id, channel="task", status="done", subject="Rebook the meeting with Ann",
                            updated_at=now)])
    with ctx.db.session() as s:
        view = dashboard_view(ctx, s, 7)
    totals = {name: total for v in view["performance"] for name, total, _ in v["series"]}
    assert totals["Follow-ups completed"] == 1


def test_small_previous_periods_show_plain_differences_not_percentages():
    assert change(3, 1) == ("+2", "up") and change(1, 3) == ("-2", "down") and change(2, 2) == ("no change", "flat")
    assert change(12, 10) == ("+20%", "up") and change(4, 0) == ("new", "up") and change(0, 0) == ("—", "flat")


def test_every_agent_has_a_details_page(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    client = TestClient(create_app(ctx))
    page = client.get("/command-center/agents/research").text
    assert "Company discovery" in page and "Recent runs" in page and "Last successful run" in page
    assert client.get("/command-center/agents/not-an-agent").status_code == 404
    assert 'href="/command-center/agents/publishing"' in client.get("/").text


def test_an_expired_authorization_approves_nothing(make_ctx):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=False)
    aid = authorize(ctx)
    with ctx.db.session() as s:
        s.get(CampaignAuthorization, aid).expires_at = utcnow() - timedelta(minutes=1)
    with ctx.db.session() as s:
        am.expire_authorizations(s)
    with ctx.db.session() as s:
        assert s.get(CampaignAuthorization, aid).status == "expired"
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "authorization_expired")).scalars().first()
    run(ctx, sender)
    f = followup(ctx, fid)
    assert f.status == "awaiting_approval" and f.authorization_id is None and FakeSMTP.sent == []
