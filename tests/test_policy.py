from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

import aime.policy as policy
from aime.doctor import run_checks
from aime.models import Activity, ContentItem, PolicyDecision, PublishAttempt
from aime.policy import agreement, evaluate_shadow
from aime.qc import build_report, CheckResult
from aime.scheduler.runner import run_job
from aime.timeutil import utcnow
from aime.web.app import create_app

ORIGIN = {"origin": "http://testserver"}


def make_post(ctx, s, n, *, slot_hours=24.0, renderer="browser", qc="pass", body=None, title=None):
    title = title or f"Post {n}: fewer handoffs between sales and support"
    body = body or ("When a support ticket names a sales account, most teams copy it by hand.\n\n"
                    "A CRM that links both removes that step.\n\nWhere does your team copy data today?")
    tags = ["#CRM"]
    image = f"policy-{n}.png"
    ctx.settings.assets_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1080, 1350), "white").save(ctx.settings.assets_dir / image)
    report = build_report([CheckResult("spelling", qc)], title, body, tags)
    item = ContentItem(service="custom_crm", industry="saas", title=title, body=body, hashtags=tags,
                       image_path=image, scheduled_for=utcnow() + timedelta(hours=slot_hours), generator="test",
                       quality={"qc": report, "image": {"renderer": renderer, "style": "editorial"}},
                       fingerprint=f"fp-{n}", status="awaiting_approval")
    s.add(item)
    s.flush()
    return item


def snapshot(s):
    cols = [c.name for c in ContentItem.__table__.columns]
    return [tuple(getattr(i, c) for c in cols) for i in s.execute(select(ContentItem).order_by(ContentItem.id)).scalars()]


def test_shadow_never_changes_posts_or_calls_out(ctx, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("the shadow policy must not make network calls")
    monkeypatch.setattr(httpx.Client, "send", no_network)
    with ctx.db.session() as s:
        for n in range(3):
            make_post(ctx, s, n, slot_hours=24 * (n + 1))
        before = snapshot(s)
        out = evaluate_shadow(s, ctx.settings)
        assert out["mode"] == "shadow" and out["evaluated"] == 3
        assert snapshot(s) == before
        assert s.execute(select(func.count(PublishAttempt.id))).scalar_one() == 0
        assert all(i.status == "awaiting_approval" and i.approved_at is None
                   for i in s.execute(select(ContentItem)).scalars())
    assert not hasattr(policy, "LinkedInClient")


def test_post_passing_every_rule_would_publish(ctx):
    with ctx.db.session() as s:
        item = make_post(ctx, s, 1)
        out = evaluate_shadow(s, ctx.settings)
        assert out["would_publish"] == [item.id]
        d = s.execute(select(PolicyDecision)).scalar_one()
        assert d.decision == "would_publish" and d.reasons == [] and d.mode == "shadow"
        assert "PUBLISHING_ENABLED=false" in d.blockers
        assert "shadow mode: the policy never publishes" in d.blockers


@pytest.mark.parametrize("change,rule", [
    (dict(qc="uncertain"), "quality_checks"),
    (dict(qc="fail"), "quality_checks"),
    (dict(renderer="pillow"), "image"),
    (dict(slot_hours=-2), "slot"),
    (dict(body="We guarantee results for every client.\n\nWhat would you try first?"), "sensitive_terms"),
])
def test_rules_hold_posts(ctx, change, rule):
    with ctx.db.session() as s:
        item = make_post(ctx, s, 1, **change)
        out = evaluate_shadow(s, ctx.settings)
        assert out["would_hold"] == [item.id]
        d = s.execute(select(PolicyDecision)).scalar_one()
        assert any(r.startswith(rule + ":") for r in d.reasons), d.reasons


def test_edited_after_checks_is_held(ctx):
    with ctx.db.session() as s:
        item = make_post(ctx, s, 1)
        item.body += "\n\nOne more line added by hand."
        evaluate_shadow(s, ctx.settings)
        d = s.execute(select(PolicyDecision)).scalar_one()
        assert d.decision == "would_hold"
        assert "checked_text_is_current: post was edited after its checks" in d.reasons


def test_weekly_cap_and_spacing(ctx):
    with ctx.db.session() as s:
        ids = [make_post(ctx, s, n, slot_hours=24 * (n + 1)).id for n in range(4)]
        close = make_post(ctx, s, 9, slot_hours=24 + 2).id
        out = evaluate_shadow(s, ctx.settings)
        assert out["would_publish"] == ids[:3]
        latest = policy.latest_decisions(s)
        assert any(r.startswith("spacing:") for r in latest[close].reasons)
        assert any(r.startswith("weekly_cap:") for r in latest[ids[3]].reasons)


def test_decisions_recorded_once_and_again_when_content_changes(ctx):
    with ctx.db.session() as s:
        item = make_post(ctx, s, 1)
        assert evaluate_shadow(s, ctx.settings)["recorded"] == 1
        assert evaluate_shadow(s, ctx.settings)["recorded"] == 0
        item.body += "\n\nEdited."
        assert evaluate_shadow(s, ctx.settings)["recorded"] == 1
        acts = s.execute(select(Activity).where(Activity.type == "policy_shadow")).scalars().all()
        assert len(acts) == 2 and acts[0].actor == "publish_policy"
        assert "would publish" in acts[0].subject and "would hold" in acts[1].subject


def test_agreement_with_the_person(ctx):
    with ctx.db.session() as s:
        a, b = make_post(ctx, s, 1, slot_hours=24), make_post(ctx, s, 2, slot_hours=72)
        c = make_post(ctx, s, 3, slot_hours=120, qc="uncertain")
        d = make_post(ctx, s, 4, slot_hours=168)
        evaluate_shadow(s, ctx.settings)
        a.status, a.approved_at, a.approved_by = "approved", utcnow(), "owner"
        b.status = "rejected"
        c.status, c.approved_at, c.approved_by = "approved", utcnow(), "owner"
        s.flush()
        rep = agreement(s)
    verdicts = {r["content_id"]: r["verdict"] for r in rep["rows"]}
    assert verdicts == {a.id: "agree", b.id: "too_permissive", c.id: "too_strict", d.id: "pending"}
    assert rep["counts"]["agreement_rate"] == 33


def test_unsupported_mode_stays_shadow(ctx):
    ctx.settings.business["publish_policy"]["mode"] = "live"
    with ctx.db.session() as s:
        make_post(ctx, s, 1)
        out = evaluate_shadow(s, ctx.settings)
        assert out["mode"] == "shadow"
        assert any("not supported" in b for b in out["blockers"])
        assert s.execute(select(ContentItem)).scalar_one().status == "awaiting_approval"
    check = next(c for c in run_checks(ctx) if c["name"] == "Auto-publish policy is shadow-only")
    assert not check["ok"] and check["severity"] == "warning"


def test_jobs_record_policy_step_without_publishing(ctx):
    res = run_job(ctx, "content")
    assert res["status"] == "success"
    assert run_job(ctx, "publish")["status"] == "success"
    with ctx.db.session() as s:
        assert s.execute(select(func.count(PolicyDecision.id))).scalar_one() > 0
        assert s.execute(select(func.count(ContentItem.id)).where(ContentItem.status == "published")).scalar_one() == 0
        assert s.execute(select(func.count(PublishAttempt.id))).scalar_one() == 0


def test_policy_pages(ctx):
    with ctx.db.session() as s:
        cid = make_post(ctx, s, 1).id
    client = TestClient(create_app(ctx))
    r = client.post("/policy/evaluate", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "Nothing+was+approved+or+published" in r.headers["location"].replace("%20", "+")
    page = client.get("/policy").text
    assert "shadow mode" in page and "would publish" in page
    assert "Auto-publish policy" in client.get(f"/content/{cid}").text
    assert "Auto-publish policy" in client.get("/").text


def test_failed_content_job_stays_failed(ctx, monkeypatch):
    from aime.agents.linkedin_content import LinkedInContentAgent

    def boom(self, inp, session):
        raise RuntimeError("simulated outage")
    monkeypatch.setattr(LinkedInContentAgent, "execute", boom)
    res = run_job(ctx, "content")
    assert res["status"] == "failed" and "publish_policy" not in (res["error"] or "")
    with ctx.db.session() as s:
        assert s.execute(select(func.count(PolicyDecision.id))).scalar_one() == 0
