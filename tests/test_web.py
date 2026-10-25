from urllib.parse import parse_qs, urlparse

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
        fid = s.execute(select(FollowUp.id).where(FollowUp.status == "awaiting_approval")).scalars().first()
    client.post(f"/followups/{fid}/approve", headers=ORIGIN, data={"subject": "Hi", "body": "Edited"})
    with seeded.db.session() as s:
        f = s.get(FollowUp, fid)
        assert f.status == "approved" and f.body == "Edited"


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
