"""The reference-style dashboard and the pages added with it: every section renders from records, search finds
records, the approval queue lists what waits, settings never show a secret."""

from fastapi.testclient import TestClient

from aime.web.app import change, create_app
from aime.web.charts import combo_chart, daily_counts, sparkline
from tests.test_outreach import SENDING, researched


def test_dashboard_has_every_reference_section_and_real_numbers(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    page = TestClient(create_app(ctx)).get("/").text
    for text in ("AI Activity", "Sales Performance", "Sales Pipeline", "Upcoming Meetings", "AI Insights", "/static/robot.svg",
                 "Companies discovered", "Verified contacts", "Emails sent", "Meetings booked", 'id="command-center"',
                 'action="/search"', 'href="/approvals"', 'href="/settings"', "Automation Control"):
        assert text in page, text
    assert "No upcoming meetings" in page  # an honest empty state, not sample data
    assert TestClient(create_app(ctx)).get("/?range=30").status_code == 200


def test_search_approvals_and_settings(make_ctx):
    from tests.test_outreach import drafts
    from aime.agents.sales_workflow import SalesWorkflowAgent
    ctx = researched(make_ctx, **{**SENDING, "smtp_password": "never-shown-secret-123"})
    SalesWorkflowAgent(ctx).run({})
    client = TestClient(create_app(ctx))
    found = client.get("/search?q=bright").text
    assert "Brightline" in found and "result" in found
    assert "Type at least two letters" in client.get("/search?q=a").text
    queue = client.get("/approvals").text
    [draft] = [f for f in drafts(ctx) if f.status == "awaiting_approval"]
    assert f"/followups#f{draft.id}" in queue and draft.subject in queue
    settings = client.get("/settings").text
    assert "OUTBOUND_EMAIL_ENABLED" in settings and "never-shown-secret-123" not in settings


def test_change_against_the_previous_period():
    assert change(12, 10) == ("+20%", "up") and change(5, 10) == ("-50%", "down") and change(3, 3) == ("0%", "flat")
    assert change(4, 0) == ("new", "up") and change(0, 0) == ("—", "flat")


def test_small_charts_draw_only_real_numbers():
    from datetime import date
    assert daily_counts([date(2026, 10, 27), date(2026, 10, 27), date(2026, 10, 20)], 3, today=date(2026, 10, 28)) == [0, 2, 0]
    assert 'class="spark blue"' in sparkline([0, 0, 0], "blue", "x")
    assert "No data" in combo_chart(["Mon"], [("Emails", [0])], ("Meetings", [0]), "t", "No data")
    drawn = combo_chart(["Mon", "Tue"], [("Emails", [1, 2])], ("Meetings", [0, 1]), "t", "No data")
    assert drawn.count("<rect") == 2 and "trend-dot" in drawn
