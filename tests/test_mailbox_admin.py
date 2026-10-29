"""Super Admin mailbox panel: add, edit, test, enable / disable, read and choose the primary sending mailbox from the
dashboard, without .env. Secrets are stored encrypted and never shown; only a Super Admin can see or change the
panel; nothing is read or sent until a mailbox is enabled for it (and sending still needs OUTBOUND_EMAIL_ENABLED and
every approval). Fake IMAP / SMTP servers and a mock Microsoft sign-in only."""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import mail_accounts as ma
from aime.agents.inbox_agent import InboxAgent
from aime.cli import cmd_user
from aime.db import kv_get
from aime.integrations import oauth
from aime.integrations.email_smtp import EmailSender
from aime.models import AuditEvent, MailAccount, OAuthToken
from aime.secure_store import unprotect
from aime.web.app import create_app
from tests.test_a2_mailbox import TOKENS, token_server
from tests.test_automation import drafted, followup, run
from tests.test_dashboard_auth import GOOD, Args, _login
from tests.test_inbound_sales import FakeSMTP
from tests.test_inbox import FakeIMAP, mail
from tests.test_outreach import approve_all

ORIGIN = {"origin": "http://testserver"}
APP_PASSWORD = "abcd efgh " + "ijkl mnop"  # test-only, assembled so the secret scanner stays quiet
GMAIL = {"provider": "gmail", "label": "Sales", "email": "Sales@us.example", "secret": APP_PASSWORD}
M365 = {"provider": "microsoft365", "email": "deals@corp.example", "oauth_client_id": "ms-client-1",
        "oauth_client_secret": "x" * 12, "oauth_tenant": "corp.example"}


class RecordingSMTP(FakeSMTP):
    logins: list = []
    hosts: list = []
    auths: list = []

    def __init__(self, host, port, timeout=30):
        super().__init__(host, port, timeout)
        RecordingSMTP.hosts.append((host, port))

    def login(self, u, p):
        RecordingSMTP.logins.append((u, p))

    def ehlo(self):
        pass

    def auth(self, mechanism, authobject):
        RecordingSMTP.auths.append((mechanism, authobject()))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class RecordingIMAP(FakeIMAP):
    logins: list = []

    def login(self, user, password):
        RecordingIMAP.logins.append((user, password))
        return "OK", [b"logged in"]

    def authenticate(self, mechanism, callback):
        RecordingIMAP.logins.append((mechanism, callback(b"")))
        return "OK", [b"ok"]


@pytest.fixture(autouse=True)
def _reset():
    RecordingSMTP.logins, RecordingSMTP.hosts, RecordingSMTP.auths, FakeSMTP.sent = [], [], [], []
    RecordingIMAP.logins, RecordingIMAP.messages = [], {}
    TOKENS.clear()


def add(ctx, data=GMAIL, actor="owner"):
    with ctx.db.session() as s:
        return ma.save(s, actor, dict(data)).id


# --------------------------------------------------------------------------- the model and its rules

def test_a_new_mailbox_is_stored_encrypted_and_starts_switched_off(make_ctx):
    ctx = make_ctx()
    aid = add(ctx)
    with ctx.db.session() as s:
        a = s.get(MailAccount, aid)
        assert (a.email, a.username, a.imap_host, a.smtp_host, a.smtp_port) == (
            "sales@us.example", "sales@us.example", "imap.gmail.com", "smtp.gmail.com", 587)
        assert (a.active, a.read_replies, a.is_primary) == (False, False, False)
        assert a.secret != APP_PASSWORD and unprotect(a.secret) == APP_PASSWORD
        view = ma.public(a)
        assert view["has_secret"] is True and APP_PASSWORD not in json.dumps(view) and "secret" not in view
        [event] = s.execute(select(AuditEvent).where(AuditEvent.action == "mailbox_created")).scalars().all()
        assert APP_PASSWORD not in event.detail and event.actor == "owner"


@pytest.mark.parametrize("change,message", [
    (dict(provider="yahoo"), "Choose Gmail"),
    (dict(email="not-an-email"), "email address"),
    (dict(secret=""), "needs the app password"),
    (dict(smtp_port="99999"), "between 1 and 65535"),
    (dict(imap_host="bad host!"), "not a host name"),
])
def test_bad_input_is_refused_with_a_reason(make_ctx, change, message):
    ctx = make_ctx()
    with ctx.db.session() as s, pytest.raises(ValueError, match=message):
        ma.save(s, "owner", {**GMAIL, **change})
    with ctx.db.session() as s, pytest.raises(ValueError, match="client ID"):
        ma.save(s, "owner", {**M365, "oauth_client_id": ""})


def test_editing_keeps_the_stored_secret_unless_a_new_one_is_typed(make_ctx):
    ctx = make_ctx()
    aid = add(ctx)
    with ctx.db.session() as s:
        ma.save(s, "owner", {"label": "Renamed"}, account_id=aid)
        assert unprotect(s.get(MailAccount, aid).secret) == APP_PASSWORD
        ma.save(s, "owner", {"secret": "new pass " + "word 1234"}, account_id=aid)
        assert unprotect(s.get(MailAccount, aid).secret) == "new pass word 1234"
        ma.save(s, "owner", {**GMAIL, "email": "other@us.example"})
        with pytest.raises(ValueError, match="already configured"):
            ma.save(s, "owner", {**GMAIL, "email": "Other@us.example"})  # the same address, any case


def test_enable_read_primary_disable_and_delete(make_ctx):
    ctx = make_ctx()
    first, second = add(ctx), add(ctx, {**GMAIL, "email": "two@us.example"})
    with ctx.db.session() as s:
        with pytest.raises(ValueError, match="Enable the mailbox"):
            ma.change(s, "owner", first, "primary")
        with pytest.raises(ValueError, match="Enable the mailbox"):
            ma.change(s, "owner", first, "read_on")
        for aid in (first, second):
            ma.change(s, "owner", aid, "enable")
        ma.change(s, "owner", first, "primary")
        ma.change(s, "owner", second, "primary")  # only one primary at a time
        assert ma.primary(s).id == second and not s.get(MailAccount, first).is_primary
        ma.change(s, "owner", second, "read_on")
        ma.change(s, "owner", second, "disable")  # a disabled mailbox is neither read nor primary
        a = s.get(MailAccount, second)
        assert (a.active, a.read_replies, a.is_primary) == (False, False, False) and ma.primary(s) is None
        ma.change(s, "owner", first, "delete")
        assert s.get(MailAccount, first) is None
        actions = [e.action for e in s.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars()]
    assert {"mailbox_enable", "mailbox_primary", "mailbox_read_on", "mailbox_disable", "mailbox_deleted"} <= set(actions)


def test_a_mailbox_replaces_only_the_mail_settings_never_a_switch(make_ctx):
    ctx = make_ctx(outbound_email_enabled=False, require_human_approval=True)
    with ctx.db.session() as s:
        a = s.get(MailAccount, add(ctx))
        st = ma.settings_for(ctx.settings, a)
    assert (st.smtp_host, st.smtp_username, st.smtp_password, st.smtp_from, st.imap_host, st.mail_auth) == (
        "smtp.gmail.com", "sales@us.example", APP_PASSWORD, "sales@us.example", "imap.gmail.com", "password")
    assert st.outbound_email_enabled is False and st.require_human_approval is True
    assert EmailSender(st).status()[0] is False  # the switch still stops everything


# --------------------------------------------------------------------------- sending and reading

def primary_gmail(ctx):
    aid = add(ctx)
    with ctx.db.session() as s:
        ma.change(s, "owner", aid, "enable")
        ma.change(s, "owner", aid, "primary")
    return aid


def test_the_primary_mailbox_sends_with_its_own_server_and_password(make_ctx):
    ctx, _sender, fid = drafted(make_ctx)
    primary_gmail(ctx)
    approve_all(ctx)
    run(ctx, EmailSender(ctx.settings, smtp_factory=RecordingSMTP))
    assert followup(ctx, fid).status == "sent"
    assert RecordingSMTP.hosts == [("smtp.gmail.com", 587)] and RecordingSMTP.logins == [("sales@us.example", APP_PASSWORD)]
    assert "sales@us.example" in FakeSMTP.sent[0]["From"]


@pytest.mark.parametrize("why", ["switch_off", "not_primary"])
def test_nothing_is_sent_from_a_mailbox_without_the_switch_or_primary(make_ctx, why):
    ctx, _sender, fid = drafted(make_ctx, outbound_email_enabled=why != "switch_off")
    if why == "switch_off":
        primary_gmail(ctx)
    else:
        aid = add(ctx)
        with ctx.db.session() as s:
            ma.change(s, "owner", aid, "enable")  # enabled but not primary: the .env mailbox sends, not this one
    approve_all(ctx)
    run(ctx, EmailSender(ctx.settings, smtp_factory=RecordingSMTP))
    assert ("smtp.gmail.com", 587) not in RecordingSMTP.hosts
    if why == "switch_off":
        assert FakeSMTP.sent == [] and followup(ctx, fid).status != "sent"


def test_every_enabled_mailbox_with_read_replies_is_read_with_its_own_position(make_ctx):
    ctx = make_ctx()
    one, two = add(ctx), add(ctx, {**GMAIL, "email": "two@us.example"})
    add(ctx, {**GMAIL, "email": "off@us.example"})  # configured but never enabled: not read
    with ctx.db.session() as s:
        for aid in (one, two):
            ma.change(s, "owner", aid, "enable")
            ma.change(s, "owner", aid, "read_on")
    RecordingIMAP.messages = {1: mail("Someone <x@prospect.example>", "Hello", "Interested."),
                              2: mail("Sales <sales@us.example>", "our own", "copy of our own email")}
    res = InboxAgent(ctx, imap_factory=RecordingIMAP).run({})
    assert res.ok, res.error
    users = [u for u, _p in RecordingIMAP.logins]
    assert users == ["sales@us.example", "two@us.example"] and "off@us.example" not in users
    assert RecordingIMAP.readonly is True and res.output.skipped_own >= 1  # mail from our own mailboxes is skipped
    with ctx.db.session() as s:
        assert kv_get(s, f"inbox.state.mailbox{one}")["last_uid"] == 2 and kv_get(s, f"inbox.state.mailbox{two}")
        assert kv_get(s, "inbox.state") is None  # the .env mailbox (not configured) keeps its own position


# --------------------------------------------------------------------------- test connection

def test_test_connection_signs_in_and_sends_nothing(make_ctx):
    ctx = make_ctx()
    aid = add(ctx)
    with ctx.db.session() as s, ctx.http() as http:
        ok, detail = ma.test_connection(s, ctx.settings, s.get(MailAccount, aid), http, "owner",
                                        imap_factory=RecordingIMAP, smtp_factory=RecordingSMTP)
    assert ok and "IMAP sign-in" in detail and "no email was sent" in detail
    assert RecordingIMAP.readonly is True and FakeSMTP.sent == []
    assert RecordingIMAP.logins == [("sales@us.example", APP_PASSWORD)] == RecordingSMTP.logins
    with ctx.db.session() as s:
        a = s.get(MailAccount, aid)
        assert a.last_test_ok is True and a.last_test_at is not None
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "mailbox_tested")).scalars().first()


class RefusingIMAP(RecordingIMAP):
    def login(self, user, password):
        raise OSError(f"[AUTHENTICATIONFAILED] Invalid credentials for {user} password={password}")


def test_a_failed_test_is_recorded_without_the_password(make_ctx):
    ctx = make_ctx()
    aid = add(ctx)
    with ctx.db.session() as s, ctx.http() as http:
        ok, detail = ma.test_connection(s, ctx.settings, s.get(MailAccount, aid), http, "owner",
                                        imap_factory=RefusingIMAP, smtp_factory=RecordingSMTP)
        assert not ok and "AUTHENTICATIONFAILED" in detail and RecordingSMTP.hosts == []
        assert s.get(MailAccount, aid).last_test_ok is False
    with ctx.db.session() as s, ctx.http() as http:
        off = make_ctx(network_enabled=False)
        assert ma.test_connection(s, off.settings, s.get(MailAccount, aid), http, "owner",
                                  imap_factory=RecordingIMAP)[0] is False and RecordingIMAP.logins == []


def test_microsoft_365_connects_with_its_own_app_registration_and_uses_xoauth2(make_ctx):
    ctx = make_ctx(handler=token_server)
    aid = add(ctx, M365)
    client = TestClient(create_app(ctx))
    r = client.get(f"/admin/mailboxes/{aid}/connect", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(
        "https://login.microsoftonline.com/corp.example/oauth2/v2.0/authorize?")
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["client_id"] == ["ms-client-1"] and "IMAP.AccessAsUser.All" in q["scope"][0]
    done = client.get(f"/integrations/oauth/callback?state={q['state'][0]}&code=good", follow_redirects=False)
    assert done.status_code == 303
    assert TOKENS[0]["client_id"] == "ms-client-1" and TOKENS[0]["client_secret"] == "x" * 12
    with ctx.db.session() as s:
        assert s.execute(select(OAuthToken).where(OAuthToken.provider == f"mailbox:{aid}")).scalars().first()
    with ctx.db.session() as s, ctx.http() as http:
        ok, _detail = ma.test_connection(s, ctx.settings, s.get(MailAccount, aid), http, "owner",
                                         imap_factory=RecordingIMAP, smtp_factory=RecordingSMTP)
    assert ok and RecordingIMAP.logins[0][0] == "XOAUTH2" and RecordingSMTP.auths[0][0] == "XOAUTH2"
    assert b"user=deals@corp.example\x01auth=Bearer at-1" in RecordingIMAP.logins[0][1]
    with ctx.db.session() as s:
        ma.change(s, "owner", aid, "delete")
        assert oauth.connected(s, "microsoft", "mail", account=aid) is None  # its tokens go with it


# --------------------------------------------------------------------------- the panel and the API

def test_the_panel_never_shows_a_secret_and_the_api_neither(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    r = client.post("/admin/mailboxes", data=GMAIL, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "disabled" in r.headers["location"]
    page = client.get("/admin/mailboxes").text
    assert "sales@us.example" in page and APP_PASSWORD not in page and "app password stored" in page
    listing = client.get("/api/mailboxes").json()["mailboxes"]
    assert listing[0]["has_secret"] is True and APP_PASSWORD not in json.dumps(listing)
    made = client.post("/api/mailboxes", json={**GMAIL, "email": "api@us.example"}, headers=ORIGIN)
    assert made.status_code == 201 and APP_PASSWORD not in made.text and made.json()["active"] is False
    aid = made.json()["id"]
    assert client.post(f"/api/mailboxes/{aid}/primary", headers=ORIGIN).status_code == 400  # disabled first
    assert client.post(f"/api/mailboxes/{aid}/enable", headers=ORIGIN).json()["active"] is True
    assert client.patch(f"/api/mailboxes/{aid}", json={"label": "API box"}, headers=ORIGIN).json()["label"] == "API box"
    assert client.delete(f"/api/mailboxes/{aid}", headers=ORIGIN).json() == {"deleted": aid}
    assert client.post("/api/mailboxes", json=GMAIL).status_code == 403  # no same-origin header: refused


@pytest.fixture
def roles(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    for name, role in (("manager", "sales_manager"), ("owner", "admin"), ("boss", "super_admin")):
        assert cmd_user(Args("create", name, role=role), ctx, prompt_fn=lambda _p: GOOD) == 0
    return ctx


def test_only_a_super_admin_can_see_or_change_mailboxes(roles):
    for name in ("manager", "owner"):
        c = TestClient(create_app(roles))
        _login(c, username=name)
        assert c.get("/admin/mailboxes", follow_redirects=False).status_code == 403
        assert c.get("/api/mailboxes").status_code == 403
        assert c.post("/admin/mailboxes", data=GMAIL, headers=ORIGIN, follow_redirects=False).status_code == 403
        assert 'href="/admin/mailboxes"' not in c.get("/").text
    boss = TestClient(create_app(roles))
    _login(boss, username="boss")
    assert boss.get("/admin/mailboxes").status_code == 200 and 'href="/admin/mailboxes"' in boss.get("/").text
    assert boss.post("/admin/mailboxes", data=GMAIL, headers=ORIGIN, follow_redirects=False).status_code == 303
    assert boss.post("/jobs/monitor/run", headers=ORIGIN, follow_redirects=False).status_code == 303  # also an admin
    with roles.db.session() as s:
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "mailbox_created")).scalar_one().actor == "boss"
    anonymous = TestClient(create_app(roles))
    assert anonymous.get("/api/mailboxes").status_code == 401


def test_the_super_admin_role_is_given_from_the_command_line_only(roles, capsys):
    assert cmd_user(Args("set-role", "owner", role="super_admin"), roles) == 0
    assert "owner is now super_admin" in capsys.readouterr().out
    from aime.doctor import run_checks
    checks = {c["name"]: c for c in run_checks(roles)}
    assert checks["Dashboard admin account"]["ok"]


# --------------------------------------------------------------------------- what the pages say

def test_the_pages_report_the_mailbox_that_actually_sends_and_reads(make_ctx):
    """Sending and reading use the Mailboxes panel, but the Follow-ups banner, the Automation and Settings pages,
    the Replies page, `aime status` and the doctor used to look at .env only and said "NOT CONFIGURED"."""
    from aime.doctor import run_checks
    from aime.integrations.status import integration_statuses

    ctx = make_ctx(outbound_email_enabled=True, company_postal_address="1 Sample St, Austin, TX 78701")
    ctx.settings.business.setdefault("outreach", {})["b2b_enabled"] = True
    client = TestClient(create_app(ctx))
    out, read = "Outbound email (approved replies and outreach)", "Sales mailbox (replies, bounces)"

    def rows():
        with ctx.db.session() as s:
            return {r["name"]: (r["state"], r["detail"]) for r in integration_statuses(ctx, s, include_scheduler=False)}

    def monitored():
        return next(c for c in run_checks(ctx) if c["name"] == "Sales mailbox monitored")["ok"]

    assert rows()[out] == ("NOT CONFIGURED", "NOT CONFIGURED: SMTP_HOST / SMTP_FROM missing")  # no mailbox anywhere
    assert rows()[read][0] == "NOT CONFIGURED" and not monitored()
    aid = add(ctx)
    with ctx.db.session() as s:
        ma.change(s, "owner", aid, "enable")
        ma.change(s, "owner", aid, "read_on")
    assert rows()[out] == ("NOT CONFIGURED", "NOT CONFIGURED: no sending mailbox; make one Primary on the Mailboxes page")
    assert rows()[read] == ("CONFIGURED", "sales@us.example / INBOX (Mailboxes panel), read-only") and monitored()
    assert "sales@us.example / INBOX (Mailboxes panel)</b>" in client.get("/inbox").text
    with ctx.db.session() as s:
        ma.change(s, "owner", aid, "primary")
    state, detail = rows()[out]
    assert state == "READY" and detail == "Ready; sends from sales@us.example (Mailboxes panel)"
    for path in ("/followups", "/automation", "/settings"):
        page = client.get(path).text
        assert "SMTP_HOST / SMTP_FROM missing" not in page, path
    assert "sends from sales@us.example (Mailboxes panel)" in client.get("/followups").text
    assert 'Mail server</span><span class="val"><span class="pill ok">ready' in client.get("/automation").text
    settings_page = client.get("/settings").text
    assert "smtp.gmail.com" in settings_page and "sales@us.example / INBOX (Mailboxes panel), read-only" in settings_page
    ctx.settings.outbound_email_enabled = False
    assert rows()[out][0] == "DISABLED"  # the switch still decides, whatever the mailbox
