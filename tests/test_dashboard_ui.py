"""The reference-style dashboard and the pages around it: the dashboard shows each business figure once, the Sales
Command Center shows how the work runs (no business KPIs repeated), Analytics holds the trends and the funnel,
search finds records, the approval queue lists what waits, settings never show a secret."""

from fastapi.testclient import TestClient

from aime.web.app import change, create_app
from aime.web.charts import combo_chart, daily_counts, nice_ticks, sparkline
from tests.test_outreach import SENDING, researched

ORIGIN = {"origin": "http://testserver"}


def test_dashboard_has_every_reference_section_and_real_numbers(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    page = TestClient(create_app(ctx)).get("/").text
    for text in ("AI Activity", "Sales Performance", "Sales Pipeline", "Upcoming Meetings", "AI Insights", "/static/robot.svg",
                 "Companies discovered", "Verified contacts", "Emails sent", "Meetings booked", "Your AI team at work",
                 'href="/command-center"', 'action="/search"', 'href="/approvals"', 'href="/settings"', "Automation Control"):
        assert text in page, text
    assert "No upcoming meetings" in page  # an honest empty state, not sample data
    assert TestClient(create_app(ctx)).get("/?range=30").status_code == 200


def test_each_figure_has_one_home(make_ctx):
    """The dashboard no longer repeats a second KPI grid, the funnel or the weekly charts; the Command Center shows
    how the work runs without the dashboard's business figures; the weekly trends and funnel live in Analytics."""
    ctx = researched(make_ctx, **SENDING)
    client = TestClient(create_app(ctx))
    dash, center, analytics = client.get("/").text, client.get("/command-center").text, client.get("/reports").text
    for text in ("Needs you", "Sales funnel", "Lead generation", "Funnel detail", "Companies with a usable email"):
        assert text not in dash, text
    for text in ("Companies discovered", "Verified contacts", "Sales Pipeline", "Sales funnel"):
        assert text not in center, text
    for text in ("Lead generation", "Sales funnel", "Funnel detail", "Companies with a usable email"):
        assert text in analytics, text
    assert dash.count('class="kpi-card"') == 4


def test_agents_show_one_clear_status_each(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    page = TestClient(create_app(ctx)).get("/command-center").text
    assert page.count('class="agent-card ') == 10
    for stage in ("Find", "Qualify", "Engage", "Market", "Report"):
        assert f"<b>{stage}</b>" in page
    # publishing is switched off in tests and the mailbox is not set: blocked, with the reason, never "running"
    assert "st-blocked" in page and "publishing is off (PUBLISHING_ENABLED is false)" in page
    assert "mailbox reading is off (mailbox not set up)" in page
    assert "st-completed" in page  # research ran in the fixture


def test_mode_switch_from_the_top_bar_returns_to_the_page(make_ctx):
    from aime import automation as am
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    page = client.get("/command-center").text
    assert 'action="/automation/mode"' in page and 'name="next" value="/command-center"' in page
    r = client.post("/automation/mode", data={"mode": "approval", "next": "/command-center#modes"}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/command-center?msg=")
    with ctx.db.session() as s:
        assert am.get_mode(s) == "approval"
    r = client.post("/automation/mode", data={"mode": "hybrid", "next": "https://evil.example/"}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.headers["location"].startswith("/?msg=")  # never another site


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
    for page in ("/", "/command-center", "/reports"):
        assert "never-shown-secret-123" not in client.get(page).text


def test_change_against_the_previous_period():
    assert change(12, 10) == ("+20%", "up") and change(5, 10) == ("-50%", "down") and change(8, 8) == ("0%", "flat")
    assert change(4, 0) == ("new", "up") and change(0, 0) == ("—", "flat")


def test_small_charts_draw_only_real_numbers():
    from datetime import date
    assert daily_counts([date(2026, 10, 27), date(2026, 10, 27), date(2026, 10, 20)], 3, today=date(2026, 10, 28)) == [0, 2, 0]
    assert 'class="spark blue"' in sparkline([0, 0, 0], "blue", "x")
    assert "No data" in combo_chart(["Mon"], [("Emails", [0])], ("Meetings", [0]), "t", "No data")
    drawn = combo_chart(["Mon", "Tue"], [("Emails", [1, 2])], ("Meetings", [0, 1]), "t", "No data")
    assert drawn.count('class="bar s0"') == 2 and "trend-dot" in drawn and "Mon: Emails 1 · Meetings 0" in drawn
    assert nice_ticks(159) == [0, 50, 100, 150, 200] and nice_ticks(3) == [0, 1, 2, 3] and nice_ticks(0) == [0, 1]
