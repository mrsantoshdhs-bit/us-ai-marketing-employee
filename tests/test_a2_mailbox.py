"""A2, mailbox reading: OAuth sign-in to Google or Microsoft 365 (PKCE, one-time state, encrypted tokens, refresh),
XOAUTH2 for IMAP, failed fetches tried again, IMAP errors retried in the run, unsubscribes by subject alone, and a
sentiment plus a recommended lead status for every reply. Mock token servers and a fake IMAP server only: no real
account, mailbox or network."""

import base64
import hashlib
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.agents.inbox_agent import InboxAgent
from aime.db import kv_get
from aime.inbox import FETCH_TRIES, Mailbox
from aime.integrations import oauth
from aime.models import InboundEmail, OAuthToken
from aime.reply_intel import classify, sentiment_and_recommendation
from aime.secure_store import unprotect
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.web.auth import admin_only
from tests.test_inbox import DAVID, MAIL, FakeIMAP, contacted, latest_reply, mail, run_inbox

GOOGLE = dict(google_client_id="cid.apps.example", google_client_secret="x" * 12)
TOKENS = []  # requests the mock token server received


def token_server(request: httpx.Request) -> httpx.Response:
    if request.url.host in ("oauth2.googleapis.com", "login.microsoftonline.com"):
        form = parse_qs(request.content.decode())
        TOKENS.append({k: v[0] for k, v in form.items()} | {"url": str(request.url)})
        if form.get("code", [""])[0] == "bad":
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code"})
        body = {"access_token": f"at-{len(TOKENS)}", "expires_in": 3600, "token_type": "Bearer"}
        if form["grant_type"][0] == "authorization_code":
            body["refresh_token"] = "rt-original"
        return httpx.Response(200, json=body)
    return httpx.Response(404)


@pytest.fixture(autouse=True)
def _clear():
    TOKENS.clear()


def sign_in(ctx, provider="google", purpose="mail", code="good"):
    with ctx.db.session() as s:
        url = oauth.start(s, ctx.settings, provider, purpose)
    q = parse_qs(urlparse(url).query)
    with ctx.db.session() as s, ctx.http() as http:
        oauth.finish(s, ctx.settings, http, q["state"][0], code)
    return url, q


# --------------------------------------------------------------------------- OAuth

def test_sign_in_uses_pkce_and_stores_tokens_encrypted(make_ctx):
    ctx = make_ctx(handler=token_server, **GOOGLE)
    url, q = sign_in(ctx)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["scope"] == ["https://mail.google.com/"] and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == [f"http://127.0.0.1:{ctx.settings.dashboard_port}/integrations/oauth/callback"]
    sent = TOKENS[0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"][0]  # the verifier matches the challenge sent to the sign-in page
    with ctx.db.session() as s:
        row = s.execute(select(OAuthToken).where(OAuthToken.provider == "google:mail")).scalar_one()
        assert row.access_token != "at-1" and row.refresh_token != "rt-original"  # never stored in the clear
        assert unprotect(row.access_token) == "at-1" and unprotect(row.refresh_token) == "rt-original"
        assert kv_get(s, "oauth.pending") is None  # the state is used once


def test_a_state_works_once_and_expires(make_ctx):
    ctx = make_ctx(handler=token_server, **GOOGLE)
    with ctx.db.session() as s:
        state = parse_qs(urlparse(oauth.start(s, ctx.settings, "google", "mail")).query)["state"][0]
    with ctx.db.session() as s, ctx.http() as http:
        oauth.finish(s, ctx.settings, http, state, "good")
    with ctx.db.session() as s, ctx.http() as http, pytest.raises(oauth.OAuthError, match="Invalid or expired"):
        oauth.finish(s, ctx.settings, http, state, "good")
    with ctx.db.session() as s:
        state = parse_qs(urlparse(oauth.start(s, ctx.settings, "google", "mail")).query)["state"][0]
        pending = kv_get(s, "oauth.pending")
        pending["at"] = (utcnow() - timedelta(minutes=20)).isoformat()
        from aime.db import kv_set
        kv_set(s, "oauth.pending", pending)
    with ctx.db.session() as s, ctx.http() as http, pytest.raises(oauth.OAuthError, match="Invalid or expired"):
        oauth.finish(s, ctx.settings, http, state, "good")
    with ctx.db.session() as s:
        state = parse_qs(urlparse(oauth.start(s, ctx.settings, "google", "mail")).query)["state"][0]
    with ctx.db.session() as s, ctx.http() as http, pytest.raises(oauth.OAuthError, match="Bad code"):
        oauth.finish(s, ctx.settings, http, state, "bad")


def test_access_tokens_are_renewed_before_they_expire(make_ctx):
    ctx = make_ctx(handler=token_server, **GOOGLE)
    sign_in(ctx)
    with ctx.db.session() as s, ctx.http() as http:
        assert oauth.access_token(s, ctx.settings, http, "google", "mail") == "at-1"  # still valid: no request
        assert len(TOKENS) == 1
        later = utcnow() + timedelta(minutes=59)  # within the refresh margin
        assert oauth.access_token(s, ctx.settings, http, "google", "mail", now=later) == "at-2"
        assert TOKENS[-1]["grant_type"] == "refresh_token" and TOKENS[-1]["refresh_token"] == "rt-original"
        row = oauth.connected(s, "google", "mail")
        assert unprotect(row.refresh_token) == "rt-original"  # kept when the refresh returns none
        with pytest.raises(oauth.OAuthError, match="not connected"):
            oauth.access_token(s, ctx.settings, http, "google", "calendar")


def test_microsoft_uses_the_tenant_and_names_scopes_on_refresh(make_ctx):
    ctx = make_ctx(handler=token_server, microsoft_client_id="ms-app", microsoft_tenant="contoso.example")
    url, q = sign_in(ctx, provider="microsoft", purpose="calendar")
    assert url.startswith("https://login.microsoftonline.com/contoso.example/oauth2/v2.0/authorize?")
    assert "Calendars.ReadWrite" in q["scope"][0] and "offline_access" in q["scope"][0]
    assert "client_secret" not in TOKENS[0]  # a public client sends no secret
    with ctx.db.session() as s, ctx.http() as http:
        oauth.access_token(s, ctx.settings, http, "microsoft", "calendar", now=utcnow() + timedelta(hours=2))
    assert "Calendars.ReadWrite" in TOKENS[-1]["scope"]


def test_sign_in_needs_a_registered_client():
    from aime.config import Settings
    with pytest.raises(oauth.OAuthError, match="GOOGLE_CLIENT_ID"):
        oauth.start(None, Settings(), "google", "mail")
    assert oauth.xoauth2("sales@us.example", "tok") == "user=sales@us.example\x01auth=Bearer tok\x01\x01"


# --------------------------------------------------------------------------- the mailbox

class OAuthIMAP(FakeIMAP):
    authenticated = None

    def login(self, user, password):
        raise AssertionError("password sign-in must not be used with MAIL_AUTH=oauth")

    def authenticate(self, mechanism, callback):
        OAuthIMAP.authenticated = (mechanism, callback(b""))
        return "OK", [b"authenticated"]


def test_the_mailbox_signs_in_with_xoauth2_and_stays_read_only(make_ctx):
    ctx = make_ctx(handler=token_server, **{**MAIL, **GOOGLE, "mail_auth": "oauth", "imap_password": "",
                                            "smtp_password": ""})
    assert ctx.settings.imap_configured
    sign_in(ctx)
    OAuthIMAP.messages = {1: mail(f"David Chen <{DAVID}>", "Hello", "Thanks, interested.")}
    out = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=OAuthIMAP)).run({})
    assert out.ok, out.error
    mechanism, sasl = OAuthIMAP.authenticated
    assert mechanism == "XOAUTH2" and sasl == f"user={ctx.settings.imap_credentials[0]}\x01auth=Bearer at-1\x01\x01".encode()
    assert OAuthIMAP.readonly is True


def test_without_a_connected_account_the_run_says_so(make_ctx):
    ctx = make_ctx(handler=token_server, **{**MAIL, **GOOGLE, "mail_auth": "oauth"})
    res = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=OAuthIMAP)).run({})
    assert not res.ok and "not connected" in res.error


class FlakyFetch(FakeIMAP):
    broken: set[int] = set()

    def uid(self, command, *args):
        if command == "FETCH" and int(args[0]) in self.broken:
            return "NO", [None]
        return super().uid(command, *args)


def test_a_message_that_fails_to_fetch_is_tried_again_then_skipped(make_ctx):
    ctx = make_ctx(**MAIL)
    FlakyFetch.messages = {1: mail("a@x.example", "one", "1"), 2: mail("b@x.example", "two", "2"),
                           3: mail("c@x.example", "three", "3")}
    FlakyFetch.broken = {2}
    box = Mailbox(ctx.settings, imap_factory=FlakyFetch)
    got, state = box.fetch_new({"uidvalidity": "777", "last_uid": 0})
    assert [k for k, _ in got] == ["777:1"] and state["last_uid"] == 1 and state["failed"] == {"2": 1}
    for _ in range(FETCH_TRIES - 2):
        got, state = box.fetch_new(state)
        assert got == [] and state["last_uid"] == 1
    got, state = box.fetch_new(state)  # the last try: skipped, and the next message is read
    assert [k for k, _ in got] == ["777:3"] and state["skipped_uids"] == [2] and state["failed"] == {}


class DropsOnce(FakeIMAP):
    connects = 0

    def __init__(self, *a, **k):
        DropsOnce.connects += 1
        if DropsOnce.connects == 1:
            raise ConnectionResetError("connection dropped")
        super().__init__(*a, **k)


def test_a_dropped_connection_is_retried_in_the_same_run(make_ctx):
    ctx = make_ctx(**MAIL)
    DropsOnce.connects, DropsOnce.messages = 0, {}
    res = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=DropsOnce)).run({})
    assert res.ok and DropsOnce.connects == 2


# --------------------------------------------------------------------------- classification

def test_an_unsubscribe_by_subject_alone_suppresses_the_address(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", "unsubscribe", "", in_reply_to=None)}
    run_inbox(ctx)
    r = latest_reply(ctx)
    assert (r.category, r.confidence) == ("unsubscribe", "high")
    assert r.sentiment == "negative" and r.recommended_status == "Do not contact: address suppressed"
    from aime.crm_ops import is_suppressed
    with ctx.db.session() as s:
        assert is_suppressed(s, DAVID)
    assert classify(InboundEmail(from_email="x@y.example", subject="Re: our call", body_text="", new_text=""))[0] == "other"


@pytest.mark.parametrize("category,intent,sentiment,words", [
    ("interested", "meeting_request", "positive", "book the meeting"),
    ("question", "pricing_request", "positive", "prices always need a person"),
    ("not_interested", None, "negative", "nurture later"),
    ("out_of_office", None, "neutral", "until they are back"),
    ("bounce", None, "neutral", "find another contact"),
])
def test_every_reply_gets_a_sentiment_and_a_recommended_status(category, intent, sentiment, words):
    s, rec = sentiment_and_recommendation(category, intent)
    assert s == sentiment and words in rec


def test_replies_store_sentiment_and_show_it_with_the_conversation(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds good. Can we talk Thursday?",
                                 in_reply_to=sent.message_id)}
    run_inbox(ctx)
    r = latest_reply(ctx)
    assert r.sentiment == "positive" and "book the meeting" in r.recommended_status
    client = TestClient(create_app(ctx))
    assert "Recommended: Opportunity: book the meeting" in client.get("/inbox?show=all").text
    page = client.get(f"/leads/{r.lead_id}").text
    assert "Conversation" in page and "we wrote to" in page and "Can we talk Thursday" in page


# --------------------------------------------------------------------------- dashboard

def test_connecting_an_account_is_for_administrators_and_audited(make_ctx):
    assert admin_only("GET", "/integrations/oauth/google/mail/connect")
    assert admin_only("GET", "/integrations/oauth/callback") and admin_only("POST", "/integrations/oauth/google/mail/disconnect")
    ctx = make_ctx(handler=token_server, **GOOGLE)
    client = TestClient(create_app(ctx))
    page = client.get("/integrations").text
    assert "Mail and calendar accounts (OAuth)" in page and "/integrations/oauth/google/mail/connect" in page
    assert "/integrations/oauth/microsoft/mail/connect" not in page  # no Microsoft client registered
    r = client.get("/integrations/oauth/google/calendar/connect", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com/")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    done = client.get(f"/integrations/oauth/callback?state={state}&code=good", follow_redirects=False)
    assert done.status_code == 303 and "connected" in done.headers["location"]
    from aime.models import AuditEvent
    with ctx.db.session() as s:
        assert oauth.connected(s, "google", "calendar") is not None
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "account_connected")).scalars().first()
    gone = client.post("/integrations/oauth/google/calendar/disconnect", headers={"origin": "http://testserver"},
                       follow_redirects=False)
    assert gone.status_code == 303
    with ctx.db.session() as s:
        assert oauth.connected(s, "google", "calendar") is None
    assert json.dumps(TOKENS)  # the mock saw the exchange; nothing reached a real server
