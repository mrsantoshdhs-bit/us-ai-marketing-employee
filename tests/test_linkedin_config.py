"""A misplaced secret in LINKEDIN_AUTHOR_URN must be rejected, never displayed, and never logged."""

import logging
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from aime.agents import AuthorizedPublishingAgent
from aime.doctor import run_checks
from aime.integrations.linkedin import configured_author_urn, get_auth_status
from aime.models import ContentItem, OAuthToken
from aime.redact import RedactingFilter, redact
from aime.timeutil import utcnow
from aime.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
# Assembled at runtime so this file never contains the secret pattern itself.
FAKE_SECRET = "WPL_AP" + "1." + "Abcdefgh12345678" + "." + "xyzxyz=="
AUTH = dict(linkedin_client_id="cid", linkedin_client_secret="sec", publishing_enabled=True)


def add_token(ctx, scope="openid profile w_member_social"):
    with ctx.db.session() as s:
        s.add(OAuthToken(provider="linkedin", access_token="tok", scope=scope, subject="member1",
                         subject_name="Sample Person", expires_at=utcnow() + timedelta(days=50)))


def test_configured_author_urn_validation(make_ctx):
    assert configured_author_urn(make_ctx().settings) == (None, True)  # empty: default to the member
    assert configured_author_urn(make_ctx(linkedin_author_urn="urn:li:organization:12345").settings) == (
        "urn:li:organization:12345", True)
    assert configured_author_urn(make_ctx(linkedin_author_urn="urn:li:person:Ab-12_x").settings) == (
        "urn:li:person:Ab-12_x", True)
    for bad in (FAKE_SECRET, "urn:li:company:1", "urn:li:organization:", "12345", "urn:li:person:a b"):
        assert configured_author_urn(make_ctx(linkedin_author_urn=bad).settings) == (None, False)


def test_invalid_author_blocks_publishing_and_is_never_shown(make_ctx):
    ctx = make_ctx(linkedin_author_urn=FAKE_SECRET, **AUTH)
    add_token(ctx)
    with ctx.db.session() as s:
        s.add(ContentItem(service="ai_agents", industry="saas", title="T", body="Body?", hashtags=[],
                          scheduled_for=utcnow() - timedelta(minutes=5), status="approved", generator="template",
                          fingerprint="fp", approved_at=utcnow()))
        status = get_auth_status(ctx.settings, s)
    assert not status.can_publish and status.author_urn is None
    assert "LINKEDIN_AUTHOR_URN" in status.reason and FAKE_SECRET not in status.reason

    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert out.published == [] and out.blocked and FAKE_SECRET not in out.blocked[0].reason

    client = TestClient(create_app(ctx))
    for path in ("/integrations", "/api/health", "/", "/content"):
        assert FAKE_SECRET not in client.get(path).text, path

    check = {c["name"]: c for c in run_checks(ctx)}["LinkedIn author setting valid"]
    assert not check["ok"] and FAKE_SECRET not in check["detail"]


def test_valid_organization_author_still_works(make_ctx):
    ctx = make_ctx(linkedin_author_urn="urn:li:organization:12345", **AUTH)
    add_token(ctx, scope="openid profile w_organization_social")
    with ctx.db.session() as s:
        status = get_auth_status(ctx.settings, s)
    assert status.can_publish and status.author_urn == "urn:li:organization:12345"


def test_redaction_masks_linkedin_client_secret():
    assert FAKE_SECRET not in redact(f"exchange failed for secret {FAKE_SECRET} at step 2")
    record = logging.LogRecord("aime", logging.WARNING, __file__, 1, "value=%s", (FAKE_SECRET,), None)
    RedactingFilter().filter(record)
    assert FAKE_SECRET not in record.getMessage()


def test_secret_scan_blocks_linkedin_client_secret(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "secret_scan.py").write_text(
        (ROOT / "scripts" / "secret_scan.py").read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "config.txt").write_text(f"LINKEDIN_AUTHOR_URN={FAKE_SECRET}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    res = subprocess.run([sys.executable, "scripts/secret_scan.py", "--staged"], cwd=repo, capture_output=True, text=True)
    assert res.returncode == 1 and "LinkedIn client secret" in res.stdout
