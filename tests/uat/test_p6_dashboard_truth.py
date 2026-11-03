"""Phase 6: what the dashboard says is what the records hold. Each test builds its own synthetic records and reads the
figure on the real page, or from the view that page renders."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aime.agents.analytics import sales_kpis
from aime.agents.linkedin_content import LinkedInContentAgent
from aime.db import kv_set
from aime.models import (AgentRun, Company, ContentItem, FollowUp, InboundEmail, JobRun, Lead, Meeting, Opportunity)
from aime.performance import build
from aime.timeutil import as_aware_utc, to_naive_utc, utcnow
from aime.web.app import NO_FULL_PRIOR, ai_employee, create_app, dashboard_view, funnel_detail, health_strip, manager_today, system_status


def add_lead(s, name, **fields):
    co = Company(name=name, normalized_name=name.lower(), domain=f"{name.lower().replace(' ', '')}.example", source="test")
    s.add(co)
    s.flush()
    lead = Lead(company_id=co.id, source="research", **{"status": "qualified", **fields})
    s.add(lead)
    s.flush()
    return lead


def reply(s, lead, category, simulated=False, status="reviewed", when=None):
    when = when or utcnow() - timedelta(hours=3)
    s.add(InboundEmail(message_id=f"<{lead.id}{category}{simulated}@x.example>", from_email="ann@x.example", subject="Re: hello",
                       category=category, status=status, lead_id=lead.id, company_id=lead.company_id, is_simulated=simulated,
                       received_at=when, processed_at=when, confidence="high"))


def meeting(s, when):
    s.add(Meeting(invitee_email="ann@x.example", title="Call", starts_at=when, ends_at=when + timedelta(hours=1), source="manual",
                  status="completed", created_at=when))


def sent(s, lead, delivered_to="ann@x.example", step=1, when=None):
    s.add(FollowUp(lead_id=lead.id, channel="email", kind="outreach", status="sent", step=step, subject="Hello", body="b",
                   to_email="ann@x.example", delivered_to=delivered_to, sent_at=when or utcnow() - timedelta(hours=1)))


def deal(s, lead, stage="won", value=1000.0):
    now = utcnow()
    s.add(Opportunity(lead_id=lead.id, company_id=lead.company_id, name=f"Deal {lead.id}", service="custom_crm", stage=stage,
                      est_value=value, probability=10, origin="ai", created_at=now, closed_at=now if stage in ("won", "lost") else None))


def agent_run(s, agent, output, status="success", job_run_id=None, error=None):
    at = utcnow() - timedelta(minutes=30)
    s.add(AgentRun(run_id=f"{agent}{status}{job_run_id}"[:32], agent=agent, job=agent, status=status, started_at=at, finished_at=at,
                   output_summary=output, job_run_id=job_run_id, error=error))


def view(ctx, days=7):
    with ctx.db.session() as s:
        return dashboard_view(ctx, s, days)


def stage(v, label):
    return next(x for x in v["pipeline_stages"] if x["label"] == label)


def card(v, label):
    return next(x for x in v["cards"] if x["label"] == label)


def series(v, name):
    return next(total for p in v["performance"] for n, total, _ in p["series"] if n == name)


def page(ctx, path="/"):
    return TestClient(create_app(ctx)).get(path).text


# 1 ---------------------------------------------------------------------------------------------------------------------
def test_insight_is_not_all_quiet_when_a_real_question_came_in(make_ctx):
    ctx = make_ctx()
    assert "All quiet" in page(ctx)
    with ctx.db.session() as s:
        reply(s, add_lead(s, "Asker"), "question")
    html = page(ctx)
    assert "All quiet" not in html and "Replies came in" in html and 'class="insight-btn" href="/inbox?show=all"' in html


def test_insight_is_not_all_quiet_with_a_booking_level_with_last_week(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        meeting(s, utcnow() - timedelta(days=2))
        meeting(s, utcnow() - timedelta(days=9))
    html = page(ctx)
    assert "All quiet" not in html and "1 meeting booking in the last 7 days, against 1" in html


def test_insight_does_not_say_work_carries_on_while_stopped(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        kv_set(s, "automation.state", "stopped")
    html = page(ctx)
    assert "All quiet" not in html and "carry on in the background" not in html and "Automation is stopped" in html


def test_simulated_replies_keep_the_insight_quiet(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        reply(s, add_lead(s, "Rehearsal"), "question", simulated=True)
    assert "All quiet" in page(ctx)


# 2, 15 -----------------------------------------------------------------------------------------------------------------
def test_simulated_wins_are_not_counted(make_ctx):
    ctx = make_ctx()
    now = utcnow()
    with ctx.db.session() as s:
        deal(s, add_lead(s, "Real Deal"))
        rehearsal = add_lead(s, "Rehearsal Deal")
        reply(s, rehearsal, "interested", simulated=True)
        deal(s, rehearsal, value=99999.0)
    v = view(ctx)
    assert stage(v, "Closed won")["value"] == 1
    assert series(v, "Opportunities opened") == 1  # was "Qualified opportunities", counting every deal
    with ctx.db.session() as s:
        assert dict((a, b) for a, b, _ in funnel_detail(s))["Deals won"] == 1
        report = build(s, now - timedelta(days=1), now + timedelta(days=1))
    assert report.total.won == 1 and report.total.won_value == 1000.0 and report.total.deals_opened == 1
    assert "Qualified opportunities" not in page(ctx)


# 3 ---------------------------------------------------------------------------------------------------------------------
def test_a_run_that_wrote_nothing_is_not_completed_and_says_what_it_did(make_ctx):
    ctx = make_ctx(publishing_enabled=False)
    ctx.settings.business["content"]["max_pending_items"] = 1
    LinkedInContentAgent(ctx).run({"research_topics": False})
    with ctx.db.session() as s:
        for post in s.execute(select(ContentItem)).scalars():
            post.status = "approved"
    run =LinkedInContentAgent(ctx).run({"research_topics": False}).output
    assert run.created == [] and "Queue full" in run.skipped_reason and "0 waiting for your approval" in run.skipped_reason
    v = view(ctx)
    row = next(f for f in v["feed"] if f["href"] and "/agents/runs/" in f["href"])
    assert row["status"] == "Skipped" and "Queue full" in row["sub"]
    with ctx.db.session() as s:
        team = {a["agent"]: a for a in ai_employee(ctx, s)["team"]}
    assert team["linkedin_content"]["status"] == "IDLE" and "Queue full" in team["linkedin_content"]["note"]
    assert "approve or reject pending posts" not in page(ctx, "/command-center")


def test_publishing_with_every_post_blocked_is_not_completed(make_ctx):
    ctx = make_ctx(publishing_enabled=True)
    blocked = [{"id": i, "reason": "LinkedIn is not connected"} for i in range(4)]
    with ctx.db.session() as s:
        agent_run(s, "publishing", {"published": [], "blocked": blocked})
    row = next(f for f in view(ctx)["feed"] if "/agents/runs/" in (f["href"] or ""))
    assert row["status"] == "Blocked" and row["sub"] == "4 blocked: LinkedIn is not connected" and row["tone"] == "orange"
    with ctx.db.session() as s:
        team = {a["agent"]: a for a in ai_employee(ctx, s)["team"]}
    assert team["publishing"]["status"] == "BLOCKED" and "4 blocked" in team["publishing"]["note"]


def test_a_run_that_published_something_is_still_completed(make_ctx):
    ctx = make_ctx(publishing_enabled=True)
    with ctx.db.session() as s:
        agent_run(s, "publishing", {"published": [1], "blocked": [{"id": 2, "reason": "x"}]})
    row = next(f for f in view(ctx)["feed"] if "/agents/runs/" in (f["href"] or ""))
    assert row["status"] == "Completed"


# 4 ---------------------------------------------------------------------------------------------------------------------
def test_test_recipient_deliveries_are_not_emails_sent_or_contacted(make_ctx):
    ctx = make_ctx()
    now = utcnow()
    with ctx.db.session() as s:
        sent(s, add_lead(s, "Rehearsed"), delivered_to="me@us.example")
        sent(s, add_lead(s, "Reached"))
    v = view(ctx)
    assert card(v, "Emails sent")["value"] == 1 and card(v, "Emails sent")["period"] == 1
    assert series(v, "Emails sent") == 1 and stage(v, "Contacted")["value"] == 1
    with ctx.db.session() as s:
        count = lambda m, *c: s.execute(select(func.count(m.id)).where(*c)).scalar_one()  # noqa: E731
        kpis = sales_kpis(count, now - timedelta(days=7))
        report = build(s, now - timedelta(days=1), now + timedelta(days=1))
        team = ai_employee(ctx, s)["employee"]["did"]
    assert kpis["outreach_sent_total"] == 1 and kpis["outreach_sent_period"] == 1
    assert report.total.emails_sent == 1
    assert next(n for label, n, _ in team if label == "emails sent") == 1


# 5 ---------------------------------------------------------------------------------------------------------------------
def test_open_leads_means_one_thing_and_is_not_called_all_leads(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        for i, status in enumerate(("new", "qualified", "nurture", "converted", "merged", "disqualified")):
            add_lead(s, f"Co {i}", status=status)
    row = stage(view(ctx), "Open leads")
    assert row["value"] == 3 and row["href"] == "/leads?group=open"
    assert "all leads" not in page(ctx)


# 7 ---------------------------------------------------------------------------------------------------------------------
def test_one_failure_is_counted_once(make_ctx):
    ctx = make_ctx()
    at = utcnow() - timedelta(hours=1)
    with ctx.db.session() as s:
        job = JobRun(job="research", trigger="scheduler", status="failed", started_at=at, finished_at=at, error="boom")
        s.add(job)
        s.flush()
        agent_run(s, "research", {}, status="failed", job_run_id=job.id, error="boom")
    with ctx.db.session() as s:
        assert len(ai_employee(ctx, s)["employee"]["live"]["problems"]) == 1
    assert "1 failed in 24 h" in page(ctx)
    with ctx.db.session() as s:  # a manual run's failure belongs to no job, so it still shows
        agent_run(s, "crm", {}, status="failed", error="other")
    with ctx.db.session() as s:
        assert len(ai_employee(ctx, s)["employee"]["live"]["problems"]) == 2


# 8 ---------------------------------------------------------------------------------------------------------------------
def test_a_merged_lead_is_not_hot(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        add_lead(s, "Merged Away", status="merged", grade="A")
        add_lead(s, "Really Hot", grade="A")
    with ctx.db.session() as s:
        assert [h["company"] for h in manager_today(ctx, s)["hot"]] == ["Really Hot"]


# 9 ---------------------------------------------------------------------------------------------------------------------
def test_waiting_links_each_count_to_the_page_that_lists_it(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        reply(s, add_lead(s, "Waiting"), "question", status="needs_review")
    html = page(ctx)
    assert '<a href="/approvals">0 approvals</a>' in html and '<a href="/inbox">1 reply to review</a>' in html


# 10 --------------------------------------------------------------------------------------------------------------------
def test_the_first_bar_holds_one_whole_owner_day(make_ctx):
    from aime.web.app import owner_tz
    ctx = make_ctx()
    midnight = as_aware_utc(utcnow()).astimezone(owner_tz(ctx.settings)).replace(hour=0, minute=0, second=0, microsecond=0)
    first = to_naive_utc(midnight - timedelta(days=6))
    with ctx.db.session() as s:
        add_lead(s, "Older").created_at = first - timedelta(minutes=1)
        add_lead(s, "First Bar").created_at = first + timedelta(minutes=1)
    v = view(ctx)
    assert series(v, "Leads generated") == 1
    assert v["leads_badge"]["count"] == 1  # the card, the chart and the period's first day agree
    assert f"{midnight - timedelta(days=6):%b %d}" in v["period_label"]


# 11 --------------------------------------------------------------------------------------------------------------------
def test_no_comparison_when_the_prior_period_began_before_the_first_record(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        sent(s, add_lead(s, "Fresh"), when=utcnow() - timedelta(days=8))  # inside the prior period, but not a full one
        sent(s, add_lead(s, "Fresh Two"))
    v = view(ctx)
    assert card(v, "Emails sent")["change"] == NO_FULL_PRIOR and card(v, "Emails sent")["trend"] == "flat"
    assert NO_FULL_PRIOR in page(ctx)


def test_a_full_prior_period_is_compared(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        lead = add_lead(s, "Old Hand")
        sent(s, lead, when=utcnow() - timedelta(days=40))
        sent(s, lead, when=utcnow() - timedelta(days=9))
        sent(s, add_lead(s, "Another"))
        sent(s, add_lead(s, "Third"))
    assert card(view(ctx), "Emails sent")["change"] == "+1"


# 13 --------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("approval_on, shown, hidden", [(True, "Approval Mode", "Automatic Mode"), (False, "Automatic Mode", "Approval Mode")])
def test_the_mode_shown_is_the_mode_in_effect(make_ctx, approval_on, shown, hidden):
    ctx = make_ctx(require_human_approval=approval_on)
    with ctx.db.session() as s:
        kv_set(s, "automation.mode", "automatic")
    html = page(ctx)
    assert shown in html and hidden not in html
    assert ("with every email approved by you" in html) is approval_on


# 14 --------------------------------------------------------------------------------------------------------------------
def test_step_two_emails_are_follow_ups(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        lead = add_lead(s, "Sequence")
        for step in (1, 2):
            s.add(FollowUp(lead_id=lead.id, channel="email", kind="outreach", status="awaiting_approval", step=step,
                           subject="Hi", body="b", to_email="ann@x.example"))
    with ctx.db.session() as s:
        assert manager_today(ctx, s)["counts"]["followups"] == 1


# 16 --------------------------------------------------------------------------------------------------------------------
def test_pipeline_says_it_is_all_time(make_ctx):
    assert 'Sales Pipeline <small class="small muted">all time</small>' in page(make_ctx())


# LOW -------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("hours, board", [(28, "ACTIVE"), (31, "FAILED")])
def test_backup_health_uses_one_number(make_ctx, hours, board):
    ctx = make_ctx()
    now = utcnow()
    with ctx.db.session() as s:
        kv_set(s, "backup.last_verified", {"at": (now - timedelta(hours=hours)).isoformat()})
    with ctx.db.session() as s:
        rows = system_status(ctx, s, ai_employee(ctx, s)["team"], (True, ""), now)
        strip = health_strip(ctx, s)
    assert next(r for r in rows if r["name"] == "Backups")["status"] == board
    assert next(r for r in strip if r[0] == "Backup")[1] == ("ok" if board == "ACTIVE" else "bad")
