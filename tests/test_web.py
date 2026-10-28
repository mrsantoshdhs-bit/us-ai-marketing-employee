import json
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.db import kv_get
from aime.models import ContentItem, FollowUp, InboundInquiry, OAuthToken
from aime.sample_data import SAMPLE_INBOUND
from aime.scheduler.runner import run_job
from aime.web.app import create_app
from tests.conftest import hn_handler

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def seeded(ctx):
    with ctx.db.session() as s:
        from aime.inbound import record_inquiry
        for rec in SAMPLE_INBOUND:
            record_inquiry(s, rec, "web_form")
    for job in ("plan", "research", "inbound", "content", "publish", "daily_report"):
        assert run_job(ctx, job)["status"] == "success"
    return ctx


@pytest.fixture
def client(seeded):
    return TestClient(create_app(seeded))


PAGES = ["/", "/leads", "/leads?grade=A", "/leads?status=qualified&state=TX", "/companies", "/pipeline",
         "/followups", "/followups?show=all", "/content", "/campaigns", "/inbound", "/agents", "/jobs",
         "/reports", "/integrations", "/api/health", "/api/stats", "/api/leads"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]


def test_detail_pages_render(client, seeded):
    with seeded.db.session() as s:
        lead_id = s.execute(select(FollowUp.lead_id)).scalars().first()
        content_id = s.execute(select(ContentItem.id)).scalars().first()
    assert client.get(f"/leads/{lead_id}").status_code == 200
    assert client.get(f"/content/{content_id}").status_code == 200
    assert client.get("/reports/1").status_code == 200
    assert client.get("/jobs/runs/1").status_code == 200
    assert client.get("/agents/runs/1").status_code == 200


def test_unknown_host_header_rejected(client):
    assert client.get("/", headers={"host": "rebind.attacker.example"}).status_code == 400
    assert client.get("/", headers={"host": "127.0.0.1:8765"}).status_code == 200


def test_cross_site_posts_blocked(client):
    assert client.post("/content/1/approve").status_code == 403
    assert client.post("/content/1/approve", headers={"origin": "https://evil.example"}).status_code == 403


def test_referrer_policy_lets_browsers_send_origin(client):
    # Under "no-referrer" browsers send "Origin: null" on form posts, so sign-in and every button got a 403.
    for path in ("/", "/login"):
        assert client.get(path).headers["referrer-policy"] == "same-origin"
    assert client.post("/login", data={"username": "x", "password": "y"}, headers={"origin": "null"}).status_code == 403
    signin = client.post("/login", data={"username": "x", "password": "y"}, headers=ORIGIN, follow_redirects=False)
    assert signin.status_code == 303 and signin.headers["location"].startswith("/login?error=")


def test_content_approval_flow(client, seeded):
    r = client.post("/content/1/approve", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    with seeded.db.session() as s:
        item = s.get(ContentItem, 1)
        assert item.status == "approved" and item.approved_by
    client.post("/content/1/save", headers=ORIGIN, data={"title": "New title", "body": "Edited body?", "hashtags": "AI, CRM"})
    with seeded.db.session() as s:
        item = s.get(ContentItem, 1)
        assert item.status == "awaiting_approval" and item.hashtags == ["#AI", "#CRM"]


def test_followup_approval(client, seeded):
    with seeded.db.session() as s:
        f = s.execute(select(FollowUp).where(FollowUp.status == "awaiting_approval")).scalars().first()
        fid, footer = f.id, "\n\n---\n" + f.body.split("\n\n---\n")[1]
    client.post(f"/followups/{fid}/approve", headers=ORIGIN, data={"subject": "Hi", "body": "Edited"})
    with seeded.db.session() as s:  # the edit dropped the opt-out footer: not approved
        assert s.get(FollowUp, fid).status == "awaiting_approval"
    client.post(f"/followups/{fid}/approve", headers=ORIGIN, data={"subject": "Hi", "body": "Edited" + footer})
    with seeded.db.session() as s:
        f = s.get(FollowUp, fid)
        assert f.status == "approved" and f.body == "Edited" + footer


def test_api_inbound(make_ctx):
    ctx = make_ctx(inbound_api_token="secret-token")
    c = TestClient(create_app(ctx))
    payload = {"email": "lee@sample.example", "company": "Sample LLC", "message": "Need a CRM", "state": "NY"}
    assert c.post("/api/inbound", json=payload).status_code == 401
    r = c.post("/api/inbound", json=payload, headers={"x-inbound-token": "secret-token"})
    assert r.status_code == 201 and r.json()["created"]
    assert c.post("/api/inbound", json=payload, headers={"x-inbound-token": "secret-token"}).json()["created"] is False
    with ctx.db.session() as s:
        assert s.execute(select(InboundInquiry)).scalars().one().channel == "api"


def test_dashboard_inbound_form(client, seeded):
    r = client.post("/inbound", headers=ORIGIN, data={"email": "kim@sample.example", "company": "Kim Co",
                                                      "message": "Automate our quotes", "services_interested": ["bpa"]})
    assert r.status_code == 200
    with seeded.db.session() as s:
        row = s.execute(select(InboundInquiry).where(InboundInquiry.channel == "manual")).scalars().one()
        assert row.payload["services_interested"] == ["bpa"]


def test_asset_path_traversal_blocked(client):
    assert client.get("/assets/..%2F..%2Faime.db").status_code == 404
    assert client.get("/assets/nope.png").status_code == 404


def test_linkedin_oauth_flow(make_ctx):
    def handler(request: httpx.Request):
        if request.url.host == "www.linkedin.com" and request.url.path == "/oauth/v2/accessToken":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 5184000,
                                             "scope": "openid,profile,w_member_social"})
        if request.url.path == "/v2/userinfo":
            return httpx.Response(200, json={"sub": "member42", "name": "Sample Person"})
        return hn_handler(request)

    ctx = make_ctx(handler=handler, linkedin_client_id="cid", linkedin_client_secret="sec")
    c = TestClient(create_app(ctx))
    r = c.get("/integrations/linkedin/connect", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://www.linkedin.com/oauth/v2/authorization")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    bad = c.get("/integrations/linkedin/callback?code=x&state=wrong", follow_redirects=False)
    assert "Invalid" in bad.headers["location"]
    r = c.get("/integrations/linkedin/connect", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    ok = c.get(f"/integrations/linkedin/callback?code=abc&state={state}", follow_redirects=False)
    assert "connected" in ok.headers["location"].lower()
    with ctx.db.session() as s:
        tok = s.execute(select(OAuthToken)).scalars().one()
        assert tok.subject == "member42" and "w_member_social" in tok.scope
        assert kv_get(s, "linkedin.oauth_state") is None
    page = c.get("/integrations").text
    assert "AT" not in page.split("Sample Person")[0][-50:]  # token value never rendered
    assert "PUBLISHING_ENABLED=false" in page


def test_linkedin_connect_requires_configuration(client):
    r = client.get("/integrations/linkedin/connect", follow_redirects=False)
    assert r.status_code == 303 and "LINKEDIN_CLIENT_ID" in r.headers["location"]


def test_inbound_api_is_off_without_a_token_and_takes_only_json(make_ctx):
    payload = {"email": "lee@sample.example", "company": "Sample LLC", "message": "Need a CRM"}
    r = TestClient(create_app(make_ctx())).post("/api/inbound", json=payload)
    assert r.status_code == 403 and "INBOUND_API_TOKEN" in r.json()["detail"]
    ctx = make_ctx(inbound_api_token="secret-token")
    c = TestClient(create_app(ctx))
    token = {"x-inbound-token": "secret-token"}
    # a cross-site page can send text/plain without a preflight; it must not be accepted
    r = c.post("/api/inbound", content=json.dumps(payload), headers={**token, "content-type": "text/plain"})
    assert r.status_code == 415
    with ctx.db.session() as s:
        assert s.execute(select(InboundInquiry)).scalars().first() is None
    assert c.post("/api/inbound", json=payload, headers=token).status_code == 201


def test_followups_page_always_lists_emails_awaiting_approval(ctx):
    from datetime import timedelta

    from aime.models import Company, Lead
    from aime.timeutil import utcnow
    with ctx.db.session() as s:
        co = Company(name="Co", normalized_name="co", domain="co.example", source="test")
        s.add(co)
        s.flush()
        lead = Lead(company_id=co.id, source="research", status="new")
        s.add(lead)
        s.flush()
        for i in range(301):
            s.add(FollowUp(lead_id=lead.id, company_id=co.id, channel="task", status="pending", subject=f"Review {i}",
                           due_at=utcnow() - timedelta(days=10, minutes=i)))
        f = FollowUp(lead_id=lead.id, company_id=co.id, channel="email", kind="response", status="awaiting_approval",
                     subject="Reply to Jane", body="Hi", due_at=utcnow())
        s.add(f)
        s.flush()
        fid = f.id
    page = TestClient(create_app(ctx)).get("/followups").text
    assert f'id="f{fid}"' in page
    assert "1 more not shown" in page


def test_approving_or_requeueing_a_post_clears_old_blocked_reasons(client, seeded):
    with seeded.db.session() as s:
        item = s.get(ContentItem, 1)
        item.status, item.publish_blocked_reason = "failed", "Failed 3 times; review and re-approve"
    client.post("/content/1/approve", headers=ORIGIN)
    with seeded.db.session() as s:
        item = s.get(ContentItem, 1)
        assert item.status == "approved" and item.publish_blocked_reason is None
        item.status, item.publish_attempt_id = "uncertain", "a1"
    client.post("/content/1/requeue", headers=ORIGIN, data={"checked": "yes"})
    with seeded.db.session() as s:
        item = s.get(ContentItem, 1)
        assert item.status == "approved" and item.publish_blocked_reason is None
    assert "Approved post blocked" not in client.get("/command-center").text


def test_expired_linkedin_token_shows_reconnect_not_connected(make_ctx):
    from datetime import timedelta

    from aime.secure_store import protect
    from aime.timeutil import utcnow
    ctx = make_ctx(linkedin_client_id="cid", linkedin_client_secret="sec")
    with ctx.db.session() as s:
        s.add(OAuthToken(provider="linkedin", access_token=protect("AT"), scope="openid profile w_member_social",
                         subject="abc", subject_name="Owner", expires_at=utcnow() - timedelta(days=3)))
    page = TestClient(create_app(ctx)).get("/command-center").text
    strip = page[page.find("LinkedIn connection"):][:200]
    assert "expired" in strip and "days left" not in strip


def test_bad_form_input_and_linkedin_network_errors_give_a_message_not_a_500(make_ctx, client, seeded):
    r = client.post("/content/1/save", headers=ORIGIN, data={"scheduled_et": "next tuesday"}, follow_redirects=False)
    assert r.status_code == 303 and "date" in r.headers["location"].lower()
    r = client.post("/campaigns", headers=ORIGIN, follow_redirects=False,
                    data={"name": "C", "objective": "o", "start_date": "2026-13-01", "end_date": "x"})
    assert r.status_code == 303 and "date" in r.headers["location"].lower()

    def handler(request: httpx.Request):
        raise httpx.ConnectError("offline")

    ctx = make_ctx(handler=handler, linkedin_client_id="cid", linkedin_client_secret="sec")
    c = TestClient(create_app(ctx))
    state = parse_qs(urlparse(c.get("/integrations/linkedin/connect", follow_redirects=False)
                              .headers["location"]).query)["state"][0]
    r = c.get(f"/integrations/linkedin/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 303 and "could not reach linkedin" in unquote(r.headers["location"]).lower()
    with ctx.db.session() as s:
        assert kv_get(s, "linkedin.oauth_state") is None  # the one-time state is used up, not reusable


def test_opportunity_value_that_is_not_a_number_is_reported(ctx):
    from aime.models import Company, Opportunity
    with ctx.db.session() as s:
        co = Company(name="Co", normalized_name="co", source="test")
        s.add(co)
        s.flush()
        o = Opportunity(company_id=co.id, name="Deal")
        s.add(o)
        s.flush()
        oid = o.id
    c = TestClient(create_app(ctx))
    r = c.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "discovery", "est_value": "ten grand"},
               follow_redirects=False)
    assert "not a number" in unquote(r.headers["location"])
    with ctx.db.session() as s:
        assert s.get(Opportunity, oid).est_value is None


def test_testserver_host_is_accepted_only_for_test_data(make_ctx):
    prod = make_ctx(data_role="production")
    assert TestClient(create_app(prod)).get("/login").status_code == 400
    assert TestClient(create_app(prod), base_url="http://127.0.0.1:8765").get("/login").status_code == 200


def test_publisher_cannot_claim_a_post_while_the_dashboard_is_editing_it(client, seeded, monkeypatch):
    import threading

    from sqlalchemy import update

    import aime.web.app as web
    from aime.timeutil import utcnow
    with seeded.db.session() as s:
        item = s.get(ContentItem, 1)
        item.status, item.approved_by, item.approved_at = "approved", "manager", utcnow()
    claimed = []

    def claim():  # what PublishingAgent._claim does
        with seeded.db.session() as other:
            res = other.execute(update(ContentItem).where(ContentItem.id == 1, ContentItem.status == "approved")
                                .values(status="publishing"))
            claimed.append(res.rowcount)

    publisher = threading.Thread(target=claim)
    real = web.run_quality_checks

    def claim_meanwhile(*a, **k):  # the publisher's claim arrives while the dashboard is saving the edit
        publisher.start()
        publisher.join(0.5)
        return real(*a, **k)

    monkeypatch.setattr(web, "run_quality_checks", claim_meanwhile)
    r = client.post("/content/1/save", headers=ORIGIN, data={"body": "Edited text"}, follow_redirects=False)
    publisher.join(30)
    assert r.status_code == 303 and claimed == [0]  # the claim waited, then found the post no longer approved
    with seeded.db.session() as s:
        item = s.get(ContentItem, 1)
        assert item.status == "awaiting_approval" and item.body == "Edited text" and item.approved_at is None
