from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.content import add_brand_line
from aime.db import kv_set
from aime.models import ContentItem
from aime.monitoring import write_heartbeat
from aime.scheduler.runner import run_job
from aime.timeutil import utcnow
from aime.web.app import create_app, health_strip


def test_brand_line_goes_before_the_closing_question():
    body = "Copy-paste between tools costs hours.\n\nWhich task would you automate first?"
    out = add_brand_line(body, "Acme Labs", "bpa")
    parts = out.split("\n\n")
    assert parts[-1] == "Which task would you automate first?"
    assert parts[-2].startswith("At Acme Labs, we build automation")


def test_brand_line_added_once_and_only_for_known_services():
    body = "Acme Labs builds CRMs.\n\nWhat would you change?"
    assert add_brand_line(body, "Acme Labs", "custom_crm") == body
    assert add_brand_line("No question here.", "Acme Labs", "unknown_service") == "No question here."
    appended = add_brand_line("No question here.", "Acme Labs", "ai_agents")
    assert appended.endswith("with human oversight.")


def _client(ctx):
    return TestClient(create_app(ctx))


def test_today_shows_sales_manager_view(ctx):
    for job in ("plan", "research", "content"):
        assert run_job(ctx, job)["status"] == "success"
    with ctx.db.session() as s:
        titles = s.execute(select(ContentItem.title).where(ContentItem.status == "awaiting_approval")).scalars().all()
    html = _client(ctx).get("/").text
    assert "Needs you" in html and "Companies to review" in html
    assert "Companies with a decision-maker" in html and "not built yet" not in html
    assert "off: nothing is posted" in html
    assert titles and all(t.replace("&", "&amp;").replace("'", "&#39;") in html for t in titles[:3])


def test_today_lists_exceptions(ctx):
    assert run_job(ctx, "content")["status"] == "success"
    with ctx.db.session() as s:
        post = s.execute(select(ContentItem)).scalars().first()
        post.status = "uncertain"
    assert "Post outcome unknown" in _client(ctx).get("/").text


def test_system_page_keeps_technical_details(ctx):
    r = _client(ctx).get("/system")
    assert r.status_code == 200
    assert "<h1>System" in r.text and "Recent job runs" in r.text


def test_health_strip_flags_stale_scheduler_and_backup(ctx):
    with ctx.db.session() as s:
        rows = {label: (state, detail) for label, state, detail in health_strip(ctx, s)}
    assert rows["Scheduler"][0] == "bad"
    assert rows["Backup"][0] == "warn"
    assert rows["Publishing"] == ("ok", "off: nothing is posted")

    write_heartbeat(ctx.settings, {"trigger": "tick", "state": "ok"})
    with ctx.db.session() as s:
        kv_set(s, "backup.last_verified", {"file": "x.db", "at": (utcnow() - timedelta(hours=40)).isoformat()})
    with ctx.db.session() as s:
        rows = {label: (state, detail) for label, state, detail in health_strip(ctx, s)}
    assert rows["Scheduler"][0] == "ok"
    assert rows["Backup"] == ("bad", "verified 40 h ago")
