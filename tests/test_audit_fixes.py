"""Defects found by the 28 Oct 2026 audit: pausing automation holds LinkedIn publishing too, NETWORK_ENABLED is
read from the environment, and a person's approval decisions reach the audit log."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import automation as am
from aime.agents import AuthorizedPublishingAgent
from aime.config import Settings
from aime.models import AuditEvent, ContentItem
from aime.web.app import create_app
from tests.test_automation import drafted
from tests.test_publishing_safety import AUTH, _late_post, _recording

ORIGIN = {"origin": "http://testserver"}


def test_pausing_automation_holds_linkedin_publishing(make_ctx):
    calls = []
    ctx = make_ctx(handler=_recording(calls), **AUTH)
    cid = _late_post(ctx, hours_late=1)
    with ctx.db.session() as s:
        am.change_state(s, "pause", "admin")
    assert AuthorizedPublishingAgent(ctx).run({}).output.published == [] and calls == []
    with ctx.db.session() as s:
        assert "paused" in s.get(ContentItem, cid).publish_blocked_reason
        am.change_state(s, "resume", "admin")
    assert AuthorizedPublishingAgent(ctx).run({}).output.published == [cid]


def test_network_enabled_is_read_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NETWORK_ENABLED", "false")
    assert Settings.load(env_file=tmp_path / "none.env", data_dir=tmp_path, data_role="test").network_enabled is False


def test_a_persons_approval_decisions_reach_the_audit_log(make_ctx):
    ctx, _sender, fid = drafted(make_ctx)
    client = TestClient(create_app(ctx))
    client.post(f"/followups/{fid}/approve", headers=ORIGIN, data={"qc_override": "yes"})
    with ctx.db.session() as s:
        [event] = s.execute(select(AuditEvent).where(AuditEvent.action == "email_approve")).scalars().all()
        assert event.target == f"follow-up #{fid}" and "outreach to" in event.detail
