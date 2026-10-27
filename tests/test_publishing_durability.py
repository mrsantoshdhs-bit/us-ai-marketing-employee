"""Failure simulations: no LinkedIn post or reply email may ever be sent twice."""

import json
import smtplib
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from aime.agents import AuthorizedPublishingAgent, SalesWorkflowAgent
from aime.agents.publishing import PublishOutput
from aime.doctor import run_checks
from aime.integrations.email_smtp import EmailSender, compliance_footer
from aime.integrations.linkedin import format_commentary
from aime.models import Alert, Company, Contact, ContentItem, FollowUp, Lead, OAuthToken, PublishAttempt
from aime.publish_journal import confirmed
from aime.timeutil import utcnow
from aime.web.app import create_app

AUTH = dict(linkedin_client_id="cid", linkedin_client_secret="secret", publishing_enabled=True)
BODY = "Hello (world) — a test post?"
TAGS = ["#AIAgents"]


def add_post(ctx, status="approved", **kw):
    with ctx.db.session() as s:
        item = ContentItem(service="ai_agents", industry="saas", title="T", body=BODY, hashtags=TAGS,
                           scheduled_for=utcnow() - timedelta(minutes=5), status=status, generator="template",
                           fingerprint="fp", approved_at=utcnow(), approved_by="test", **kw)
        s.add(item)
        s.flush()
        return item.id


def add_token(ctx, scope="openid profile w_member_social"):
    with ctx.db.session() as s:
        s.add(OAuthToken(provider="linkedin", access_token="tok", scope=scope, subject="abc123",
                         expires_at=utcnow() + timedelta(days=50)))


def get(ctx, cid):
    with ctx.db.session() as s:
        return s.get(ContentItem, cid)


class Api:
    """Scripted LinkedIn API: each POST /rest/posts pops the next behaviour."""

    def __init__(self, *behaviours, recent=None):
        self.behaviours = list(behaviours)
        self.posts = 0
        self.recent = recent or []

    def __call__(self, request: httpx.Request):
        if request.url.path == "/rest/posts" and request.method == "GET":
            return httpx.Response(200, json={"elements": self.recent})
        if request.url.path == "/rest/posts":
            self.posts += 1
            b = self.behaviours.pop(0) if self.behaviours else "ok"
            if b == "ok":
                return httpx.Response(201, headers={"x-restli-id": f"urn:li:share:{100 + self.posts}"})
            if b == "read_timeout":
                raise httpx.ReadTimeout("timed out after sending", request=request)
            if b == "connect_error":
                raise httpx.ConnectError("connection refused", request=request)
            if b == "duplicate":
                return httpx.Response(422, json={"message": "Content is a duplicate of urn:li:share:555"})
        return httpx.Response(404)


def test_read_timeout_is_uncertain_and_never_retried(make_ctx):
    api = Api("read_timeout")
    ctx = make_ctx(handler=api, **AUTH)
    add_token(ctx)
    cid = add_post(ctx)
    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert [u.id for u in out.uncertain] == [cid]
    for _ in range(3):
        AuthorizedPublishingAgent(ctx).run({})
    assert api.posts == 1 and get(ctx, cid).status == "uncertain"
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.level == "error")).scalars().first()
        assert s.execute(select(PublishAttempt)).scalars().one().outcome == "uncertain"


def test_connect_error_is_safe_to_retry(make_ctx):
    api = Api("connect_error", "ok")
    ctx = make_ctx(handler=api, **AUTH)
    add_token(ctx)
    cid = add_post(ctx)
    AuthorizedPublishingAgent(ctx).run({})
    assert get(ctx, cid).status == "approved"  # request never reached LinkedIn
    AuthorizedPublishingAgent(ctx).run({})
    assert get(ctx, cid).status == "published" and api.posts == 2
    with ctx.db.session() as s:
        outcomes = sorted(a.outcome for a in s.execute(select(PublishAttempt)).scalars())
    assert outcomes == ["failed", "success"]


def test_crash_mid_publish_becomes_uncertain_without_reposting(make_ctx):
    api = Api()
    ctx = make_ctx(handler=api, **AUTH)
    add_token(ctx)
    cid = add_post(ctx, status="publishing", publish_attempt_id="dead1",
                   publish_started_at=utcnow() - timedelta(minutes=20))
    AuthorizedPublishingAgent(ctx).run({})
    assert get(ctx, cid).status == "uncertain" and api.posts == 0


def test_fresh_claim_by_another_process_is_left_alone(make_ctx):
    api = Api()
    ctx = make_ctx(handler=api, **AUTH)
    add_token(ctx)
    cid = add_post(ctx, status="publishing", publish_attempt_id="live1", publish_started_at=utcnow())
    AuthorizedPublishingAgent(ctx).run({})
    assert get(ctx, cid).status == "publishing" and api.posts == 0


def test_database_failure_after_success_recovers_from_journal(make_ctx, monkeypatch):
    api = Api("ok")
    ctx = make_ctx(handler=api, **AUTH)
    add_token(ctx)
    cid = add_post(ctx)
    real = AuthorizedPublishingAgent._mark_published
    calls = {"n": 0}

    def flaky(self, session, item, attempt, urn, via, out):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("UPDATE content_items", {}, Exception("disk I/O error"))
        return real(self, session, item, attempt, urn, via, out)

    monkeypatch.setattr(AuthorizedPublishingAgent, "_mark_published", flaky)
    first = AuthorizedPublishingAgent(ctx).run({})
    assert not first.ok and get(ctx, cid).status == "publishing"
    assert any(r["external_id"] == "urn:li:share:101" for r in confirmed(ctx.settings.data_dir).values())

    with ctx.db.session() as s:  # simulate time passing beyond the stale-claim window
        s.get(ContentItem, cid).publish_started_at = utcnow() - timedelta(minutes=30)
    out = AuthorizedPublishingAgent(ctx).run({}).output
    item = get(ctx, cid)
    assert item.status == "published" and item.external_id == "urn:li:share:101" and item.reconciled_via == "journal"
    assert out.reconciled == [cid] and api.posts == 1


def test_duplicate_response_marks_published(make_ctx):
    ctx = make_ctx(handler=Api("duplicate"), **AUTH)
    add_token(ctx)
    cid = add_post(ctx)
    AuthorizedPublishingAgent(ctx).run({})
    item = get(ctx, cid)
    assert item.status == "published" and item.external_id == "urn:li:share:555"
    assert item.reconciled_via == "duplicate_response"


def test_only_one_process_can_claim_a_post(make_ctx):
    ctx = make_ctx(handler=Api(), **AUTH)
    add_token(ctx)
    cid = add_post(ctx)
    a, b = AuthorizedPublishingAgent(ctx), AuthorizedPublishingAgent(ctx)
    with ctx.db.session() as s1, ctx.db.session() as s2:
        from aime.integrations.linkedin import get_auth_status
        auth = get_auth_status(ctx.settings, s1)
        first = a._claim(s1, s1.get(ContentItem, cid), auth)
        second = b._claim(s2, s2.get(ContentItem, cid), auth)
    assert first is not None and second is None


def test_read_scope_reconciles_uncertain_posts(make_ctx):
    commentary = format_commentary(BODY, TAGS)
    api = Api(recent=[{"id": "urn:li:share:777", "commentary": commentary}])
    ctx = make_ctx(handler=api, **AUTH)
    add_token(ctx, scope="openid profile w_member_social r_member_social")
    cid = add_post(ctx, status="uncertain", publish_attempt_id="u1", publish_started_at=utcnow() - timedelta(minutes=5))
    AuthorizedPublishingAgent(ctx).run({})
    item = get(ctx, cid)
    assert item.status == "published" and item.external_id == "urn:li:share:777" and item.reconciled_via == "api_lookup"
    assert api.posts == 0


def test_read_scope_confirms_absence_then_requeues(make_ctx):
    api = Api(recent=[])
    ctx = make_ctx(handler=api, **AUTH)
    add_token(ctx, scope="openid profile w_member_social r_member_social")
    cid = add_post(ctx, status="uncertain", publish_attempt_id="u2", publish_started_at=utcnow() - timedelta(minutes=45))
    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert out.requeued == [cid]
    assert get(ctx, cid).status == "published" and api.posts == 1  # requeued, then published in the same run


def test_dashboard_resolves_uncertain_posts(make_ctx):
    ctx = make_ctx(**AUTH)
    a = add_post(ctx, status="uncertain", publish_attempt_id="x1")
    b = add_post(ctx, status="uncertain", publish_attempt_id="x2")
    busy = add_post(ctx, status="publishing", publish_attempt_id="x3", publish_started_at=utcnow())
    c = TestClient(create_app(ctx))
    h = {"origin": "http://testserver"}
    assert c.post(f"/content/{a}/confirm_published", data={"post_ref": "not a url"}, headers=h).status_code == 400
    c.post(f"/content/{a}/confirm_published", data={"post_ref": "urn:li:share:42"}, headers=h)
    assert get(ctx, a).status == "published" and get(ctx, a).reconciled_via == "manual"
    assert c.post(f"/content/{b}/requeue", data={}, headers=h).status_code == 400  # must confirm the check
    c.post(f"/content/{b}/requeue", data={"checked": "yes"}, headers=h)
    assert get(ctx, b).status == "approved"
    assert c.post(f"/content/{busy}/save", data={"title": "x"}, headers=h).status_code == 409


def test_doctor_reports_uncertain_and_double_publication(make_ctx):
    ctx = make_ctx()
    cid = add_post(ctx, status="uncertain")
    with ctx.db.session() as s:
        s.add_all([PublishAttempt(attempt_id="a", content_id=cid, outcome="success"),
                   PublishAttempt(attempt_id="b", content_id=cid, outcome="success")])
    checks = {c["name"]: c for c in run_checks(ctx)}
    assert not checks["No post published twice"]["ok"]
    assert not checks["No uncertain publications/sends awaiting review"]["ok"]


# ---------------------------------------------------------------- reply emails
class TimeoutSMTP:
    attempts = 0

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        TimeoutSMTP.attempts += 1
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed after DATA")


def _approved_followup(ctx):
    with ctx.db.session() as s:
        co = Company(name="Sample Co", normalized_name="sample co", source="test", state="TX")
        s.add(co)
        s.flush()
        ct = Contact(company_id=co.id, email="sam@sample.example", source="inbound", consent_status="inquiry")
        s.add(ct)
        s.flush()
        lead = Lead(company_id=co.id, contact_id=ct.id, source="inbound", status="qualified")
        s.add(lead)
        s.flush()
        f = FollowUp(lead_id=lead.id, contact_id=ct.id, company_id=co.id, channel="email", step=1,
                     due_at=utcnow() - timedelta(minutes=1), status="approved", subject="Hi",
                     body="Body" + compliance_footer(ctx.settings),
                     approved_by="test", approved_at=utcnow())
        s.add(f)
        s.flush()
        return f.id


def test_smtp_disconnect_is_uncertain_and_not_resent(make_ctx):
    ctx = make_ctx(outbound_email_enabled=True, smtp_host="smtp.example", smtp_from="hi@us.example",
                   company_postal_address="1 Sample St")
    fid = _approved_followup(ctx)
    TimeoutSMTP.attempts = 0
    sender = EmailSender(ctx.settings, smtp_factory=TimeoutSMTP)
    tuesday_noon_et = utcnow().replace(year=2026, month=10, day=27, hour=16, minute=0)
    for _ in range(3):
        SalesWorkflowAgent(ctx, sender=sender).run({"now": tuesday_noon_et.isoformat()})
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "uncertain"
    assert TimeoutSMTP.attempts == 1


def test_interrupted_send_becomes_uncertain(make_ctx):
    ctx = make_ctx()
    fid = _approved_followup(ctx)
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        f.status, f.send_started_at = "sending", utcnow() - timedelta(minutes=30)
    SalesWorkflowAgent(ctx).run({})
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "uncertain"


def test_publish_output_contract_has_reconciliation_fields():
    fields = PublishOutput.model_fields
    assert {"reconciled", "requeued", "uncertain"} <= set(fields)
    assert json.loads(PublishOutput(can_publish=False, authorization="x").model_dump_json())["uncertain"] == []
