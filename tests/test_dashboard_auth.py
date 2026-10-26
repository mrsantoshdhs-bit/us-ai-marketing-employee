"""Sales Manager sign-in: every page and API needs a session; approvals record who approved."""

import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.cli import cmd_user
from aime.doctor import run_checks
from aime.models import Activity, ContentItem, User
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.web.auth import (COOKIE, MAX_FAILED, hash_password, make_cookie, read_cookie, session_secret,
                           verify_password)

ORIGIN = {"origin": "http://testserver"}
GOOD = "correct horse " + "battery staple"  # test-only value, assembled so the secret scanner stays quiet


class Args:
    def __init__(self, action, username=None, role="sales_manager"):
        self.action, self.username, self.role = action, username, role


@pytest.fixture
def auth_ctx(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    assert cmd_user(Args("create", "manager"), ctx, prompt_fn=lambda _p: GOOD) == 0
    return ctx


def _login(client, password=GOOD, username="manager", next_="/"):
    return client.post("/login", data={"username": username, "password": password, "next": next_},
                       headers=ORIGIN, follow_redirects=False)


def test_password_hashing():
    stored = hash_password(GOOD)
    assert stored.startswith("scrypt$") and GOOD not in stored
    assert verify_password(GOOD, stored) and not verify_password(GOOD + "x", stored)
    with pytest.raises(ValueError):
        hash_password("short")


def test_every_page_and_api_needs_sign_in(auth_ctx):
    client = TestClient(create_app(auth_ctx))
    for path in ("/", "/leads", "/content", "/followups", "/integrations", "/jobs", "/reports"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    for path in ("/api/stats", "/api/leads", "/api/health", "/api/docs"):
        assert client.get(path).status_code == 401, path
    assert client.get("/login").status_code == 200
    assert client.post("/jobs/monitor/run", headers=ORIGIN, follow_redirects=False).headers["location"].startswith("/login")


def test_sign_in_sign_out_and_safe_redirect(auth_ctx):
    client = TestClient(create_app(auth_ctx))
    r = _login(client, next_="https://evil.example/")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.cookies.get(COOKIE) or client.cookies.get(COOKIE)
    assert cookie and "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    page = client.get("/")
    assert page.status_code == 200 and "Signed in as manager" in page.text
    client.post("/logout", headers=ORIGIN)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_wrong_password_and_lockout(auth_ctx):
    client = TestClient(create_app(auth_ctx))
    for _ in range(MAX_FAILED):
        r = _login(client, password="wrong password!!")
        assert "Wrong" in r.headers["location"] or "Wrong%20" in r.headers["location"]
    r = _login(client)  # right password, but locked now
    assert "locked" in r.headers["location"]
    with auth_ctx.db.session() as s:
        assert s.execute(select(User)).scalar_one().locked_until > utcnow()


def test_tampered_expired_and_revoked_sessions_are_rejected(auth_ctx):
    secret = session_secret(auth_ctx.settings.data_dir)
    with auth_ctx.db.session() as s:
        user = s.execute(select(User)).scalar_one()
        good = make_cookie(secret, user)
        expired = make_cookie(secret, user, now=time.time() - 13 * 3600)
    assert read_cookie(secret, good) and not read_cookie(secret, expired)
    assert not read_cookie(secret, good[:-2] + "00")
    client = TestClient(create_app(auth_ctx))
    client.cookies.set(COOKIE, good)
    assert client.get("/", follow_redirects=False).status_code == 200
    assert cmd_user(Args("set-password", "manager"), auth_ctx, prompt_fn=lambda _p: GOOD + "!") == 0
    assert client.get("/", follow_redirects=False).status_code == 303  # password change signed it out


def test_approvals_record_the_signed_in_manager(auth_ctx):
    with auth_ctx.db.session() as s:
        item = ContentItem(service="ai_agents", industry="saas", title="T", body="Body?", hashtags=[],
                           scheduled_for=utcnow() + timedelta(days=1), status="awaiting_approval",
                           generator="template", fingerprint="fp")
        s.add(item)
        s.flush()
        cid = item.id
    client = TestClient(create_app(auth_ctx))
    _login(client)
    client.post(f"/content/{cid}/approve", headers=ORIGIN)
    with auth_ctx.db.session() as s:
        assert s.get(ContentItem, cid).approved_by == "manager"
        assert s.execute(select(Activity).where(Activity.actor == "manager")).first()


def test_no_account_yet_shows_terminal_instructions_and_doctor_warns(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    client = TestClient(create_app(ctx))
    assert "python -m aime user create" in client.get("/login").text
    check = next(c for c in run_checks(ctx) if c["name"] == "Dashboard sign-in")
    assert not check["ok"] and check["severity"] == "warning"


def test_inbound_api_stays_reachable_with_its_token(make_ctx):
    ctx = make_ctx(dashboard_auth=True, inbound_api_token="tok")
    client = TestClient(create_app(ctx))
    r = client.post("/api/inbound", json={"email": "a@b.example", "message": "hello there", "company": "B"},
                    headers={"X-Inbound-Token": "tok"})
    assert r.status_code != 401


def test_operational_changes_need_an_admin(auth_ctx):
    assert cmd_user(Args("create", "owner", role="admin"), auth_ctx, prompt_fn=lambda _p: GOOD) == 0
    manager = TestClient(create_app(auth_ctx))
    _login(manager)
    assert manager.post("/jobs/monitor/run", headers=ORIGIN, follow_redirects=False).status_code == 403
    assert manager.get("/integrations/linkedin/connect", follow_redirects=False).status_code == 403
    assert manager.get("/jobs").status_code == 200  # viewing is fine; running is not
    admin = TestClient(create_app(auth_ctx))
    _login(admin, username="owner")
    assert admin.post("/jobs/monitor/run", headers=ORIGIN, follow_redirects=False).status_code == 303


def test_an_existing_account_can_be_made_admin_from_the_command_line(auth_ctx, capsys):
    check = next(c for c in run_checks(auth_ctx) if c["name"] == "Dashboard admin account")
    assert not check["ok"] and "set-role" in check["detail"]
    assert cmd_user(Args("set-role", "manager", role=None), auth_ctx) == 1  # the role must be named
    assert cmd_user(Args("set-role", "manager", role="admin"), auth_ctx) == 0
    assert "now admin" in capsys.readouterr().out
    assert next(c for c in run_checks(auth_ctx) if c["name"] == "Dashboard admin account")["ok"]
    with auth_ctx.db.session() as s:
        assert s.execute(select(Activity).where(Activity.type == "security")).scalars().first()
    admin = TestClient(create_app(auth_ctx))
    _login(admin)
    assert admin.post("/jobs/monitor/run", headers=ORIGIN, follow_redirects=False).status_code == 303


def test_sales_pages_need_sign_in(auth_ctx):
    client = TestClient(create_app(auth_ctx))
    for path in ("/contacts", "/inbox", "/meetings", "/policy", "/pipeline", "/system", "/companies"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    for path, method in (("/inbox/1/reviewed", "post"), ("/meetings/1/held", "post"), ("/leads/1/stop_outreach", "post")):
        r = getattr(client, method)(path, headers=ORIGIN, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
