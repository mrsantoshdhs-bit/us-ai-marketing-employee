"""Publishing and operations safety: no late posts, and manual job runs respect the maintenance window."""

from datetime import timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import maintenance
from aime.agents import AuthorizedPublishingAgent
from aime.models import Activity, Alert, ContentItem, JobRun, OAuthToken
from aime.scheduler.runner import run_job
from aime.timeutil import utcnow
from aime.web.app import create_app

AUTH = dict(linkedin_client_id="cid", linkedin_client_secret="secret", publishing_enabled=True)
ORIGIN = {"origin": "http://testserver"}


def _recording(calls):
    def handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"})
    return handler


def _late_post(ctx, hours_late=30):
    with ctx.db.session() as s:
        s.add(OAuthToken(provider="linkedin", access_token="tok", scope="openid profile w_member_social",
                         subject="abc123", expires_at=utcnow() + timedelta(days=30)))
        item = ContentItem(service="ai_agents", industry="saas", title="T", body="Body?", hashtags=[],
                           scheduled_for=utcnow() - timedelta(hours=hours_late), status="approved",
                           generator="template", fingerprint="fp", approved_by="test", approved_at=utcnow())
        s.add(item)
        s.flush()
        return item.id


def test_overdue_post_without_free_slot_is_held_not_published(make_ctx, monkeypatch):
    calls = []
    ctx = make_ctx(handler=_recording(calls), **AUTH)
    cid = _late_post(ctx)
    monkeypatch.setattr("aime.agents.publishing.free_slots", lambda *a, **k: [])
    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert out.published == [] and calls == []
    with ctx.db.session() as s:
        item = s.get(ContentItem, cid)
        assert item.status == "awaiting_approval" and item.approved_at is None
        assert "approve it again" in item.publish_blocked_reason
        assert s.execute(select(Alert).where(Alert.message.like(f"Post #{cid} missed its slot%"))).first()
        assert s.execute(select(Activity).where(Activity.subject.like(f"Post #{cid} held%"))).first()


def test_overdue_post_with_free_slot_is_rescheduled_and_reported(make_ctx):
    calls = []
    ctx = make_ctx(handler=_recording(calls), **AUTH)
    cid = _late_post(ctx)
    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert out.rescheduled == [cid] and calls == []
    with ctx.db.session() as s:
        item = s.get(ContentItem, cid)
        assert item.status == "approved" and item.scheduled_for > utcnow()
        assert s.execute(select(Alert).where(Alert.message.like(f"Post #{cid} missed its slot and was moved%"))).first()


def test_post_inside_overdue_window_still_publishes(make_ctx):
    calls = []
    ctx = make_ctx(handler=_recording(calls), **AUTH)
    cid = _late_post(ctx, hours_late=1)
    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert out.published == [cid]


def test_manual_job_run_skipped_during_maintenance(ctx):
    maintenance.start(ctx.settings.data_dir, 1, "test deploy")
    result = run_job(ctx, "monitor")
    assert result["status"] == "skipped" and "maintenance" in result["reason"]
    with ctx.db.session() as s:
        assert s.execute(select(JobRun)).first() is None
    assert run_job(ctx, "monitor", allow_during_maintenance=True)["status"] == "success"


def test_dashboard_refuses_job_runs_during_maintenance(ctx):
    maintenance.start(ctx.settings.data_dir, 1, "test deploy")
    client = TestClient(create_app(ctx))
    for path in ("/jobs/monitor/run", "/inbound/process"):
        r = client.post(path, headers=ORIGIN, follow_redirects=False)
        assert r.status_code == 303 and "Maintenance" in r.headers["location"]
    assert "Maintenance in progress" in client.get("/").text
    with ctx.db.session() as s:
        assert s.execute(select(JobRun)).first() is None


def test_manual_run_recovers_abandoned_runs(ctx):
    with ctx.db.session() as s:
        s.add(JobRun(job="research", trigger="scheduler", status="running", started_at=utcnow() - timedelta(hours=3)))
    run_job(ctx, "monitor")
    with ctx.db.session() as s:
        stale = s.execute(select(JobRun).where(JobRun.job == "research")).scalar_one()
        assert stale.status == "abandoned"
