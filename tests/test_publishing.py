import json
from datetime import timedelta

import httpx
from sqlalchemy import select

from aime.agents import AuthorizedPublishingAgent
from aime.integrations.linkedin import escape_little_text, format_commentary
from aime.models import Alert, ContentItem, OAuthToken
from aime.timeutil import utcnow

AUTH = dict(linkedin_client_id="cid", linkedin_client_secret="secret", publishing_enabled=True)


def add_post(ctx, status="approved", due_delta=timedelta(minutes=-5), image=False):
    with ctx.db.session() as s:
        item = ContentItem(service="ai_agents", industry="saas", title="T", body="Hello (world) #1 *bold*",
                           hashtags=["#AIAgents", "#SaaS"], scheduled_for=utcnow() + due_delta, status=status,
                           generator="template", fingerprint=f"fp{utcnow().timestamp()}",
                           image_path="img.png" if image else None,
                           approved_at=utcnow() if status == "approved" else None)
        s.add(item)
        s.flush()
        return item.id


def add_token(ctx, scope="openid profile w_member_social", expired=False):
    with ctx.db.session() as s:
        s.add(OAuthToken(provider="linkedin", access_token="tok", scope=scope, subject="abc123",
                         expires_at=utcnow() + (timedelta(days=-1) if expired else timedelta(days=50))))


def get(ctx, cid):
    with ctx.db.session() as s:
        return s.get(ContentItem, cid)


def test_escape_little_text():
    assert escape_little_text("a(b)#c") == "a\\(b\\)\\#c"
    assert format_commentary("Hi", ["#AIAgents"]).endswith("{hashtag|\\#|AIAgents}")


def test_without_authorization_posts_stay_queued(ctx):
    cid = add_post(ctx)
    res = AuthorizedPublishingAgent(ctx).run({})
    assert res.ok and res.output.published == [] and not res.output.can_publish
    item = get(ctx, cid)
    assert item.status == "approved" and item.external_id is None
    assert "NOT CONFIGURED" in item.publish_blocked_reason
    with ctx.db.session() as s:
        assert s.execute(select(Alert)).scalars().first() is not None


def test_master_switch_off_blocks_even_with_token(make_ctx):
    ctx = make_ctx(**{**AUTH, "publishing_enabled": False})
    add_token(ctx)
    cid = add_post(ctx)
    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert out.published == [] and "PUBLISHING_ENABLED" in get(ctx, cid).publish_blocked_reason


def test_missing_scope_and_expired_token_block(make_ctx):
    ctx = make_ctx(**AUTH)
    add_token(ctx, scope="openid profile")
    add_post(ctx)
    assert "w_member_social" in AuthorizedPublishingAgent(ctx).run({}).output.authorization
    ctx2 = make_ctx(**AUTH)
    add_token(ctx2, expired=True)
    add_post(ctx2)
    assert "expired" in AuthorizedPublishingAgent(ctx2).run({}).output.authorization


def linkedin_api(calls, post_status=201, post_headers=None):
    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.path == "/rest/images":
            return httpx.Response(200, json={"value": {"uploadUrl": "https://upload.linkedin.example/put",
                                                       "image": "urn:li:image:IMG1"}})
        if request.url.host == "upload.linkedin.example":
            return httpx.Response(201)
        if request.url.path == "/rest/posts":
            return httpx.Response(post_status, headers=post_headers if post_headers is not None
                                  else {"x-restli-id": "urn:li:share:999"}, json={})
        return httpx.Response(404)
    return handler


def test_publishes_only_on_confirmed_201(make_ctx):
    calls = []
    ctx = make_ctx(handler=linkedin_api(calls), **AUTH)
    add_token(ctx)
    (ctx.settings.assets_dir / "img.png").write_bytes(b"\x89PNG fake")
    cid = add_post(ctx, image=True)
    pending = add_post(ctx, status="awaiting_approval")
    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert out.published == [cid]
    item = get(ctx, cid)
    assert item.status == "published" and item.external_id == "urn:li:share:999"
    assert item.external_url.endswith("urn:li:share:999/")
    assert get(ctx, pending).status == "awaiting_approval"  # never published without approval
    post_req = next(r for r in calls if r.url.path == "/rest/posts")
    body = json.loads(post_req.content)
    assert post_req.headers["LinkedIn-Version"] == ctx.settings.linkedin_api_version
    assert body["author"] == "urn:li:person:abc123" and body["content"]["media"]["id"] == "urn:li:image:IMG1"
    assert "\\(world\\)" in body["commentary"] and "{hashtag|\\#|AIAgents}" in body["commentary"]


def test_server_error_is_uncertain_and_never_reposted(make_ctx):
    calls = []
    ctx = make_ctx(handler=linkedin_api(calls, post_status=500), **AUTH)
    add_token(ctx)
    cid = add_post(ctx)
    for _ in range(3):
        AuthorizedPublishingAgent(ctx).run({})
    item = get(ctx, cid)
    assert item.status == "uncertain" and item.external_id is None
    assert sum(1 for r in calls if r.url.path == "/rest/posts") == 1  # a 5xx might have created it: no retry


def test_rejected_request_is_retried_then_failed(make_ctx):
    calls = []
    ctx = make_ctx(handler=linkedin_api(calls, post_status=400), **AUTH)
    add_token(ctx)
    cid = add_post(ctx)
    for _ in range(4):
        AuthorizedPublishingAgent(ctx).run({})
    item = get(ctx, cid)
    assert item.status == "failed" and item.publish_attempts == 3
    assert sum(1 for r in calls if r.url.path == "/rest/posts") == 3


def test_201_without_post_id_is_not_trusted(make_ctx):
    ctx = make_ctx(handler=linkedin_api([], post_headers={}), **AUTH)
    add_token(ctx)
    cid = add_post(ctx)
    AuthorizedPublishingAgent(ctx).run({})
    item = get(ctx, cid)
    assert item.status == "uncertain" and item.external_id is None  # probably created: needs confirmation


def test_unauthorized_response_requests_reconnect(make_ctx):
    ctx = make_ctx(handler=linkedin_api([], post_status=401), **AUTH)
    add_token(ctx)
    cid = add_post(ctx)
    AuthorizedPublishingAgent(ctx).run({})
    assert "reconnect" in get(ctx, cid).publish_blocked_reason


def test_one_post_per_run_and_overdue_rescheduled(make_ctx):
    calls = []
    ctx = make_ctx(handler=linkedin_api(calls), **AUTH)
    add_token(ctx)
    a = add_post(ctx, due_delta=timedelta(minutes=-10))
    b = add_post(ctx, due_delta=timedelta(minutes=-5))
    stale = add_post(ctx, due_delta=timedelta(days=-3))
    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert out.rescheduled == [stale] and get(ctx, stale).scheduled_for > utcnow()
    assert len(out.published) == 1 and out.published[0] in (a, b)
