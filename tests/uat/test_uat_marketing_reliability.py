"""UAT as a Marketing Manager and a Business Owner: marketing pipeline, dashboards, scheduled jobs, database recovery.

test_ok_*: behaviour verified. test_defect_*: asserts the correct behaviour and FAILS on the current code.
Synthetic data only (make_ctx: temp database, mocked HTTP, no .env). No Windows task is touched: schtasks is stubbed.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import OperationalError

import aime.timeutil
from aime import automation as am
from aime import backup, maintenance, workflows
from aime.agents import (AnalyticsReportingAgent, AuthorizedPublishingAgent, LinkedInContentAgent, MarketingManagerAgent,
                         USBusinessResearchAgent)
from aime.agents.publishing import MAX_ATTEMPTS
from aime.audiences import describe, matching, parse
from aime.calendar_slots import candidate_slots
from aime.content import add_brand_line, compose_template, quality_check
from aime.db import kv_set
from aime.doctor import run_checks
from aime.integrations.status import _LLM_PROBE
from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES
from aime.models import (AgentRun, Alert, Campaign, Company, Contact, ContentItem, ContentMetric, FollowUp,
                         InboundEmail, InboundInquiry, JobRun, JobState, Lead, MarketingPlan, Meeting, OAuthToken,
                         Opportunity, PublishAttempt, Report, User)
from aime.monitoring import alert_once, check_health, read_heartbeat, write_heartbeat
from aime.performance import build
from aime.policy import evaluate_post, evaluate_shadow, policy_config
from aime.publish_journal import record_success
from aime.qc.checks import run_quality_checks
from aime.scheduler import runner, windows
from aime.scheduler.runner import (FileLock, JobSpec, due_jobs, is_due, load_job_specs, next_run, recover, run_job, tick)
from aime.timeutil import utcnow
from aime.web.app import (dashboard_view, employee_duty, funnel_detail, health_strip, system_status, create_app)
from aime.workflows import JOBS, JobOutcome
from scripts import watchdog
from tests.conftest import FakeLLM, hn_handler
from tests.test_linkedin_leads import FakeLinkedIn, lead as li_lead
from tests.test_linkedin_leads import leads_ctx

ORIGIN = {"origin": "http://testserver"}
AUTH = dict(linkedin_client_id="cid", linkedin_client_secret="secret", publishing_enabled=True)
READ_SCOPE = "openid profile w_member_social r_member_social"
SERVICE = next(iter(SERVICES))
INDUSTRY = next(iter(INDUSTRIES))
_seq = {"n": 0}


# ------------------------------------------------------------------------------------------------- shared helpers
@pytest.fixture(autouse=True)
def _never_touch_windows_tasks(monkeypatch):
    """/jobs and others ask Task Scheduler; nothing in this file may reach schtasks."""
    blocked = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="blocked in UAT")
    monkeypatch.setattr(windows, "_schtasks", lambda *a, **k: blocked)
    monkeypatch.setattr(windows, "_schtasks_interactive", lambda *a, **k: blocked)
    monkeypatch.setattr(backup, "onedrive_folders", lambda: [])
    _LLM_PROBE.clear()


@pytest.fixture
def clock(monkeypatch):
    """Move the code's clock (aime.timeutil.utcnow) to a simulated instant; it keeps running from there."""
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET)

    def set_to(when: datetime) -> datetime:
        aime.timeutil.CLOCK_OFFSET = when - datetime.now(timezone.utc).replace(tzinfo=None)
        return when

    def forward(delta: timedelta) -> datetime:
        return set_to(utcnow() + delta)

    set_to.forward = forward
    return set_to


def client_for(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def add_post(ctx, status="approved", slot_delta=timedelta(minutes=-5), title=None, body=None, image_path=None, **kw):
    _seq["n"] += 1
    n = _seq["n"]
    with ctx.db.session() as s:
        item = ContentItem(service=SERVICE, industry=INDUSTRY, title=title or f"UAT post {n}",
                           body=body or f"Synthetic post number {n}: does your team track this (yet)?",
                           hashtags=["#AIAgents"], scheduled_for=utcnow() + slot_delta if slot_delta is not None else None,
                           status=status, generator="template", fingerprint=f"uat{n}", image_path=image_path,
                           approved_by="owner" if status in ("approved", "published", "publishing", "uncertain") else None,
                           approved_at=utcnow() if status in ("approved", "published", "publishing", "uncertain") else None,
                           **kw)
        s.add(item)
        s.flush()
        return item.id


def post(ctx, cid) -> ContentItem:
    with ctx.db.session() as s:
        return s.get(ContentItem, cid)


def add_token(ctx, scope="openid profile w_member_social", expired=False):
    with ctx.db.session() as s:
        s.add(OAuthToken(provider="linkedin", access_token="tok", scope=scope, subject="abc123",
                         expires_at=utcnow() + (timedelta(days=-1) if expired else timedelta(days=50))))


def alerts(ctx, like="%") -> list[Alert]:
    with ctx.db.session() as s:
        return s.execute(select(Alert).where(Alert.message.like(like))).scalars().all()


class FakeLI:
    """LinkedIn's Posts and Images APIs. `script` is what each POST /rest/posts does: 'ok', 'lost' (created, but the
    answer never arrives), an HTTP status, or an exception. The feed is what GET /rest/posts lists."""

    def __init__(self, script=(), image_status=200):
        self.script, self.feed, self.post_calls, self.image_status = list(script), [], 0, image_status

    def _created(self, request) -> str:
        urn = f"urn:li:share:{1000 + len(self.feed)}"
        self.feed.append({"id": urn, "commentary": json.loads(request.content)["commentary"]})
        return urn

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/images":
            if self.image_status != 200:
                return httpx.Response(self.image_status, json={"message": "image refused"})
            return httpx.Response(200, json={"value": {"uploadUrl": "https://upload.linkedin.example/put",
                                                       "image": "urn:li:image:IMG1"}})
        if request.url.host == "upload.linkedin.example":
            return httpx.Response(201)
        if request.url.path != "/rest/posts":
            return hn_handler(request)
        if request.method == "GET":
            return httpx.Response(200, json={"elements": list(self.feed)})
        self.post_calls += 1
        step = self.script.pop(0) if self.script else "ok"
        if step == "ok":
            return httpx.Response(201, headers={"x-restli-id": self._created(request)}, json={})
        if step == "lost":
            self._created(request)
            raise httpx.ReadTimeout("the answer never arrived", request=request)
        if isinstance(step, Exception):
            raise step
        return httpx.Response(step, json={"message": f"status {step}"})


def li_ctx(make_ctx, fake, scope="openid profile w_member_social", **over):
    ctx = make_ctx(handler=fake, **{**AUTH, **over})
    add_token(ctx, scope=scope)
    return ctx


def publish(ctx, **payload):
    res = AuthorizedPublishingAgent(ctx).run(payload)
    assert res.ok, res.error
    return res.output


def png(ctx, name="uat.png", size=(1080, 1350)) -> str:
    ctx.settings.assets_dir.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, (20, 40, 80))
    for x in range(0, size[0], 90):  # a few bars, so the image hash is not all zeros
        img.paste((200, x % 255, 90), (x, 0, x + 40, size[1]))
    img.save(ctx.settings.assets_dir / name)
    return name


def good_draft(ctx, angle=0, pain=0):
    d = compose_template(SERVICE, INDUSTRY, angle, pain, 0)
    return d.title, add_brand_line(d.body, ctx.settings.company_name, SERVICE)


def qc_of(ctx, title, body, image=None, hashtags=("#AIAgents",)):
    with ctx.db.session() as s:
        path = ctx.settings.assets_dir / image if image else None
        return run_quality_checks(s, ctx.settings, title=title, body=body, hashtags=list(hashtags), image_path=path,
                                  image_info={"renderer": "pillow"})


def qc_check(report, name):
    return next(c for c in report["checks"] if c["name"] == name)


def seed_email(ctx, to="pat@acme.example", kind="outreach", grade="A"):
    with ctx.db.session() as s:
        co = Company(name="Acme Dev Shop", normalized_name=f"acme {to}", domain=to.split("@")[1], source="manual")
        s.add(co)
        s.flush()
        ct = Contact(company_id=co.id, first_name="Pat", last_name="Lee", email=to, source="website", consent_status="none",
                     email_status="published", email_source_url="https://acme.example/team", evidence="Pat Lee, CEO",
                     evidence_url="https://acme.example/team", discovered_at=utcnow())
        s.add(ct)
        s.flush()
        ld = Lead(company_id=co.id, contact_id=ct.id, source="research", status="qualified", grade=grade, score=85)
        s.add(ld)
        s.flush()
        f = FollowUp(lead_id=ld.id, contact_id=ct.id, company_id=co.id, channel="email", kind=kind, step=1, to_email=to,
                     status="awaiting_approval", subject="Your CRM", body="Saw your post. Worth a talk?",
                     due_at=utcnow() - timedelta(hours=1))
        s.add(f)
        s.flush()
        return f.id


def fast_jobs(monkeypatch, keep=()):
    """Every job but `keep` is a no-op, so scheduler logic can be tested without doing the jobs' work."""
    for name in list(runner.JOBS):
        if name not in keep:
            monkeypatch.setitem(runner.JOBS, name, lambda ctx, job=name: JobOutcome())


def job_runs(ctx) -> dict[str, int]:
    with ctx.db.session() as s:
        out: dict[str, int] = {}
        for r in s.execute(select(JobRun)).scalars():
            out[r.job] = out.get(r.job, 0) + 1
        return out


def heartbeat_at(ctx, age: timedelta, state="idle"):
    (ctx.settings.data_dir / "heartbeat.json").write_text(json.dumps(
        {"at": (utcnow() - age).isoformat() + "Z", "state": state, "session": "interactive", "trigger": "scheduler"}),
        encoding="utf-8")


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# ================================================================================================= Marketing planning
def test_ok_plan_job_produces_plan_campaign_and_briefs(ctx):
    assert run_job(ctx, "plan")["status"] == "success"
    with ctx.db.session() as s:
        plan = s.execute(select(MarketingPlan)).scalars().one()
        camp = s.execute(select(Campaign)).scalars().one()
        assert len(plan.focus_states) == len(set(plan.focus_states)) >= 1 and plan.focus_industries
        assert plan.briefs and all(b["service"] in SERVICES and b["industry"] in INDUSTRIES for b in plan.briefs)
        assert camp.status == "active" and camp.start_date <= plan.plan_date <= camp.end_date
        assert camp.id == plan.campaign_id


def test_ok_plan_rotates_states_and_keeps_one_campaign(ctx):
    first = MarketingManagerAgent(ctx).run({"plan_date": date(2026, 10, 27)}).output
    second = MarketingManagerAgent(ctx).run({"plan_date": date(2026, 10, 28)}).output
    assert set(first.focus_states).isdisjoint(second.focus_states)
    assert first.campaign_id == second.campaign_id  # same month: the running campaign, not a second one


def test_ok_plan_closes_expired_campaign_at_month_end(ctx):
    MarketingManagerAgent(ctx).run({"plan_date": date(2026, 10, 31)})
    out = MarketingManagerAgent(ctx).run({"plan_date": date(2026, 11, 1)}).output
    with ctx.db.session() as s:
        camps = s.execute(select(Campaign).order_by(Campaign.id)).scalars().all()
        assert [c.status for c in camps] == ["completed", "active"]
        assert camps[1].start_date == date(2026, 11, 1) and camps[1].end_date == date(2026, 11, 30)
        assert out.campaign_id == camps[1].id


# ================================================================================================= Content and QC
def test_ok_content_job_only_queues_posts_for_approval(ctx):
    assert run_job(ctx, "content")["status"] == "success"
    with ctx.db.session() as s:
        items = s.execute(select(ContentItem)).scalars().all()
        assert items and all(i.status == "awaiting_approval" and i.approved_at is None for i in items)
        slots = [i.scheduled_for for i in items]
        assert len(set(slots)) == len(slots) and all(sl > utcnow() for sl in slots)
        assert all(i.qc.get("status") in ("passed", "held") for i in items)
        assert all((ctx.settings.assets_dir / i.image_path).exists() for i in items)


def test_ok_hallucinated_llm_draft_is_replaced_by_template(ctx):
    bad = ("Companies that adopt AI agents grow 73% faster. According to Gartner, our clients saved $2M.\n\n"
           "We helped a leading bank.\n\nWhat would you do?")
    ctx._llm, ctx._llm_checked = FakeLLM([bad]), True
    out = LinkedInContentAgent(ctx).run({"count": 1, "research_topics": False}).output
    assert out.llm_rejections >= 1 and len(out.created) == 1
    item = post(ctx, out.created[0])
    assert "73%" not in item.body and "Gartner" not in item.body and item.generator == "template"


@pytest.mark.parametrize("claim,reason", [
    ("Our platform improves conversion by 40%.", "percentage"), ("We saved a firm $20000.", "dollar"),
    ("Results are guaranteed.", "guaranteed"), ("Studies show this works.", "research"),
    ("We helped twelve firms.", "past results"), ("We are an industry-leading firm.", "superlative"),
])
def test_ok_qc_gate_rejects_unsupported_claims(ctx, claim, reason):
    title, body = good_draft(ctx)
    assert quality_check(body)["passed"]
    q = quality_check(f"{claim}\n\n{body}")
    assert not q["passed"] and any(reason in p for p in q["problems"])


def test_ok_qc_holds_any_number_or_named_company(ctx):
    title, body = good_draft(ctx)
    image = png(ctx)
    assert qc_of(ctx, title, body, image)["decision"] == "pass"
    assert qc_of(ctx, title, f"Three of 12 teams said this.\n\n{body}", image)["decision"] == "hold"
    assert qc_of(ctx, title, f"Salesforce teams said this.\n\n{body}", image)["decision"] == "hold"


def test_defect_qc_spelled_out_statistic_passes_as_clean(ctx):
    """OPERATIONS.md: statistics fail, "any number" is held. A figure written in words gets through as "passed"."""
    title, body = good_draft(ctx)
    image = png(ctx)
    claim = "Forty percent of small firms lose a deal each week to slow replies."
    assert quality_check(f"{claim}\n\n{body}")["passed"]  # the gate does not see it
    report = qc_of(ctx, title, f"{claim}\n\n{body}", image)
    assert report["decision"] != "pass", "a statistic in words must not pass as verified: " + qc_check(report, "factual claims")["detail"]


def test_defect_policy_hold_terms_miss_plural_forms(ctx):
    """OPERATIONS.md: no sensitive terms "(pricing, guarantees, client stories…)". \\bterm\\b never matches a plural."""
    cfg = policy_config(ctx.settings)
    assert "discount" in cfg["hold_terms"] and "price" in cfg["hold_terms"]
    cid = add_post(ctx, status="awaiting_approval", slot_delta=timedelta(days=2),
                   body="Our discounts and prices are the best of the season. Does your team track them?")
    with ctx.db.session() as s:
        rules = evaluate_post(s.get(ContentItem, cid), cfg, ctx.settings, utcnow(), [])
    sensitive = next(r for r in rules if r["rule"] == "sensitive_terms")
    assert not sensitive["ok"], "a post about discounts and prices must be held"


def test_ok_policy_hold_term_and_stale_draft_are_held_and_nothing_changes(ctx):
    cfg = policy_config(ctx.settings)
    cid = add_post(ctx, status="awaiting_approval", slot_delta=timedelta(days=2), body="Our pricing is new. Agree?")
    stale = add_post(ctx, status="awaiting_approval", slot_delta=timedelta(days=3), created_at=utcnow() - timedelta(days=20))
    with ctx.db.session() as s:
        rules = {r["rule"]: r for r in evaluate_post(s.get(ContentItem, cid), cfg, ctx.settings, utcnow(), [])}
        assert not rules["sensitive_terms"]["ok"]
        rules = {r["rule"]: r for r in evaluate_post(s.get(ContentItem, stale), cfg, ctx.settings, utcnow(), [])}
        assert not rules["draft_age"]["ok"]
        out = evaluate_shadow(s, ctx.settings)
    assert set(out["would_hold"]) >= {cid, stale}
    assert post(ctx, cid).status == "awaiting_approval" and post(ctx, stale).status == "awaiting_approval"


def test_ok_qc_duplicates_fail_and_rejected_posts_do_not_count(ctx):
    title, body = good_draft(ctx)
    cid = add_post(ctx, status="awaiting_approval", title=title, body=body)
    assert qc_check(qc_of(ctx, title, "Another text entirely. Does it matter?"), "duplicates")["status"] == "fail"
    assert qc_check(qc_of(ctx, "Fresh title", body), "duplicates")["status"] == "fail"
    with ctx.db.session() as s:
        s.get(ContentItem, cid).status = "rejected"
    assert qc_check(qc_of(ctx, title, body), "duplicates")["status"] == "pass"


def test_ok_queue_full_creates_nothing_and_says_why(ctx):
    for i in range(9):
        add_post(ctx, status="awaiting_approval", slot_delta=timedelta(days=i + 1))
    out = LinkedInContentAgent(ctx).run({"research_topics": False}).output
    assert out.created == [] and "Queue full" in out.skipped_reason and out.pending_total == 9


def test_ok_stale_unapproved_post_is_never_published_and_approval_moves_it(make_ctx):
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx, status="awaiting_approval", slot_delta=timedelta(days=-5))
    publish(ctx)
    assert fake.post_calls == 0 and post(ctx, cid).status == "awaiting_approval"
    r = client_for(ctx).post(f"/content/{cid}/approve", headers=ORIGIN, data={"qc_override": "yes"}, follow_redirects=False)
    assert r.status_code == 303
    out = publish(ctx)  # 5 days past its slot: moved to the next free slot, not posted late
    assert out.rescheduled == [cid] and fake.post_calls == 0 and post(ctx, cid).scheduled_for > utcnow()


def test_ok_approval_needs_a_person_and_held_posts_need_the_tick(make_ctx):
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake)
    title, body = good_draft(ctx)
    cid = add_post(ctx, status="awaiting_approval", title=title, body=body, quality={"qc": {"status": "held"}})
    client = client_for(ctx)
    client.post(f"/content/{cid}/approve", headers=ORIGIN, data={}, follow_redirects=False)
    assert post(ctx, cid).status == "awaiting_approval"
    with ctx.db.session() as s:  # "approved" set directly in the database is no approval
        s.get(ContentItem, cid).status = "approved"
    out = publish(ctx)
    assert fake.post_calls == 0 and out.blocked[0].id == cid and post(ctx, cid).status == "awaiting_approval"


# ================================================================================================= Publishing, mocked API
def test_ok_publish_success_records_everything(make_ctx):
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx)
    assert publish(ctx).published == [cid]
    item = post(ctx, cid)
    assert item.status == "published" and item.external_id == "urn:li:share:1000" and item.published_at
    assert publish(ctx).published == [] and fake.post_calls == 1  # the next run does not post it again
    with ctx.db.session() as s:
        assert s.execute(select(PublishAttempt)).scalars().one().outcome == "success"


def test_ok_publishing_switch_off_makes_no_call(make_ctx):
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake, publishing_enabled=False)
    cid = add_post(ctx)
    out = publish(ctx)
    assert fake.post_calls == 0 and out.published == [] and not out.can_publish
    assert "PUBLISHING_ENABLED" in post(ctx, cid).publish_blocked_reason and post(ctx, cid).status == "approved"
    assert alerts(ctx, "%cannot be published%")


def test_ok_expired_token_makes_no_call_and_alerts(make_ctx):
    fake = FakeLI()
    ctx = make_ctx(handler=fake, **AUTH)
    add_token(ctx, expired=True)
    cid = add_post(ctx)
    publish(ctx)
    assert fake.post_calls == 0 and "expired" in post(ctx, cid).publish_blocked_reason
    assert alerts(ctx, "%cannot be published%")
    assert next(r for r in health_strip_rows(ctx) if r[0] == "LinkedIn connection")[1] == "bad"


def health_strip_rows(ctx):
    with ctx.db.session() as s:
        return health_strip(ctx, s)


def test_ok_rate_limit_429_keeps_post_queued_without_double_post(make_ctx):
    fake = FakeLI([429, "ok"])
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx)
    out = publish(ctx)
    assert out.failed and post(ctx, cid).status == "approved" and "429" in post(ctx, cid).last_publish_error
    assert publish(ctx).published == [cid] and fake.post_calls == 2 and len(fake.feed) == 1


def test_ok_server_error_500_is_uncertain_and_never_retried_automatically(make_ctx):
    fake = FakeLI([500])
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx)
    out = publish(ctx)
    assert [u.id for u in out.uncertain] == [cid] and post(ctx, cid).status == "uncertain"
    assert alerts(ctx, f"Post #{cid}%")
    publish(ctx)
    assert fake.post_calls == 1 and post(ctx, cid).status == "uncertain"


def test_ok_read_timeout_is_uncertain_connect_timeout_is_a_clean_retry(make_ctx):
    fake = FakeLI([httpx.ReadTimeout("slow"), httpx.ConnectTimeout("no route")])
    ctx = li_ctx(make_ctx, fake)
    a = add_post(ctx, slot_delta=timedelta(minutes=-10))
    publish(ctx)
    assert post(ctx, a).status == "uncertain"
    b = add_post(ctx, slot_delta=timedelta(minutes=-5))
    publish(ctx)
    assert post(ctx, b).status == "approved" and "connect" in post(ctx, b).last_publish_error.lower()


def test_ok_uncertain_post_reconciled_from_linkedin_is_not_posted_twice(make_ctx, clock):
    fake = FakeLI(["lost"])
    ctx = li_ctx(make_ctx, fake, scope=READ_SCOPE)
    cid = add_post(ctx, body="Hello (world) #1 *bold*: what do you think?")
    assert [u.id for u in publish(ctx).uncertain] == [cid]
    clock.forward(timedelta(minutes=30))
    out = publish(ctx)
    assert out.reconciled == [cid] and fake.post_calls == 1
    item = post(ctx, cid)
    assert item.status == "published" and item.reconciled_via == "api_lookup" and item.external_id == fake.feed[0]["id"]


def test_ok_uncertain_post_absent_from_linkedin_is_requeued_only_after_30_min(make_ctx, clock):
    fake = FakeLI([500, "ok"])
    ctx = li_ctx(make_ctx, fake, scope=READ_SCOPE)
    cid = add_post(ctx)
    publish(ctx)
    clock.forward(timedelta(minutes=10))
    assert publish(ctx).requeued == [] and post(ctx, cid).status == "uncertain" and fake.post_calls == 1
    clock.forward(timedelta(minutes=25))
    out = publish(ctx)
    assert out.requeued == [cid] and out.published == [cid] and fake.post_calls == 2 and len(fake.feed) == 1
    assert post(ctx, cid).status == "published"


def test_ok_uncertain_without_read_permission_waits_for_a_person(make_ctx, clock):
    fake = FakeLI([500])
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx)
    publish(ctx)
    clock.forward(timedelta(hours=3))
    publish(ctx)
    assert post(ctx, cid).status == "uncertain" and fake.post_calls == 1
    client = client_for(ctx)
    assert client.post(f"/content/{cid}/requeue", headers=ORIGIN, data={}).status_code == 400  # must tick "I checked"
    assert client.post(f"/content/{cid}/approve", headers=ORIGIN, data={}).status_code == 400
    r = client.post(f"/content/{cid}/confirm_published", headers=ORIGIN, data={"post_ref": "urn:li:share:555"},
                    follow_redirects=False)
    assert r.status_code == 303 and post(ctx, cid).status == "published" and post(ctx, cid).external_id == "urn:li:share:555"


def test_ok_crash_after_201_is_repaired_from_the_journal(make_ctx, clock):
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx, status="publishing", publish_attempt_id="att1", publish_started_at=utcnow(), publish_attempts=1)
    with ctx.db.session() as s:
        s.add(PublishAttempt(attempt_id="att1", content_id=cid, started_at=utcnow(), outcome="pending"))
    record_success(ctx.settings.data_dir, "att1", cid, "urn:li:share:777")
    out = publish(ctx)
    assert out.reconciled == [cid] and fake.post_calls == 0 and post(ctx, cid).external_id == "urn:li:share:777"


def test_ok_crash_before_answer_claim_becomes_uncertain_after_ten_minutes(make_ctx, clock):
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx, status="publishing", publish_attempt_id="att2", publish_started_at=utcnow(), publish_attempts=1)
    publish(ctx)
    assert post(ctx, cid).status == "publishing" and fake.post_calls == 0  # may still be in flight in another process
    clock.forward(timedelta(minutes=11))
    publish(ctx)
    assert post(ctx, cid).status == "uncertain" and fake.post_calls == 0


def test_ok_two_processes_cannot_claim_one_post(make_ctx):
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx)
    agent = AuthorizedPublishingAgent(ctx)
    with ctx.db.session() as s1, ctx.db.session() as s2:
        auth = _auth(ctx)
        item1, item2 = s1.get(ContentItem, cid), s2.get(ContentItem, cid)
        assert agent._claim(s1, item1, auth) is not None
        assert agent._claim(s2, item2, auth) is None


def _auth(ctx):
    from aime.integrations.linkedin import get_auth_status
    with ctx.db.session() as s:
        return get_auth_status(ctx.settings, s)


def test_ok_image_upload_failure_is_clean_retry_not_uncertain(make_ctx):
    fake = FakeLI(image_status=500)
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx, image_path=png(ctx))
    out = publish(ctx)
    assert out.failed and post(ctx, cid).status == "approved" and fake.post_calls == 0


def test_ok_overdue_post_moves_and_keeps_its_approval(make_ctx):
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake)
    late = add_post(ctx, slot_delta=timedelta(hours=-30))
    out = publish(ctx)
    assert out.rescheduled == [late] and fake.post_calls == 0 and post(ctx, late).scheduled_for > utcnow()
    with ctx.db.session() as s:  # every slot in the horizon held by drafts: an approved post goes first, still approved
        from aime.calendar_slots import free_slots
        for sl in free_slots(s, ctx.settings):
            s.add(ContentItem(service=SERVICE, industry=INDUSTRY, title=f"f{sl}", body="b", hashtags=[], status="draft",
                              generator="template", fingerprint=f"f{sl}", scheduled_for=sl))
    again = add_post(ctx, slot_delta=timedelta(hours=-30))
    publish(ctx)
    item = post(ctx, again)
    assert item.status == "approved" and item.approved_at is not None and item.scheduled_for > utcnow() and fake.post_calls == 0


def test_ok_paused_automation_holds_publishing(make_ctx):
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx)
    with ctx.db.session() as s:
        am.change_state(s, "pause", "owner")
    publish(ctx)
    assert fake.post_calls == 0 and "paused" in post(ctx, cid).publish_blocked_reason.lower()


def test_defect_token_revoked_401_raises_no_alert(make_ctx):
    """A token LinkedIn stops accepting (revoked, not expired): the post is retried every 30 minutes for as long as
    its slot lasts and the job reports success. No alert, and the health strip still says connected."""
    fake = FakeLI([401])
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx)
    assert run_job(ctx, "publish")["status"] == "success"
    item = post(ctx, cid)
    assert item.status == "approved" and "reconnect" in item.publish_blocked_reason.lower()
    assert any("reconnect" in a.message.lower() or "linkedin" in a.message.lower() for a in alerts(ctx)), \
        "the owner must be told LinkedIn refused the connection"


def test_defect_post_giving_up_after_repeated_rejection_raises_no_alert(make_ctx):
    """After MAX_ATTEMPTS rejected calls (400, 429) the post stops at 'failed' with its slot gone: nobody is told."""
    fake = FakeLI([400] * MAX_ATTEMPTS)
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx)
    for _ in range(MAX_ATTEMPTS):
        publish(ctx)
    assert post(ctx, cid).status == "failed"
    assert alerts(ctx, f"%#{cid}%"), "a post that will never go out by itself must raise an alert"


def test_ok_revoked_token_alert_closes_after_the_next_accepted_post(make_ctx):
    fake = FakeLI([401, "ok"])
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx)
    publish(ctx)
    assert len(alerts(ctx, "%reconnect LinkedIn%")) == 1 and not alerts(ctx, "%reconnect LinkedIn%")[0].resolved_at
    assert publish(ctx).published == [cid]
    assert alerts(ctx, "%reconnect LinkedIn%")[0].resolved_at


def test_ok_missing_image_withdraws_the_approval_and_alerts(make_ctx):
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx, image_path="gone.png")
    out = publish(ctx)
    item = post(ctx, cid)
    assert [b.id for b in out.blocked] == [cid] and fake.post_calls == 0
    assert item.status == "awaiting_approval" and item.approved_at is None and item.publish_attempts == 0
    assert alerts(ctx, f"Post #{cid}%image file is missing%")


def test_defect_approved_image_missing_publishes_text_only(make_ctx):
    """A person approved post + image. If the file is gone at publish time the post silently goes out without it."""
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake)
    cid = add_post(ctx, image_path="gone.png")
    publish(ctx)
    item = post(ctx, cid)
    assert fake.post_calls == 0 and item.status != "published", "what is published must be what was approved"


# ================================================================================================= Campaign authorizations
def _authorize(ctx, to, kinds=("outreach",), daily=5, total=10, days=7):
    with ctx.db.session() as s:
        a = am.create_authorization(s, "owner", "UAT campaign", to, list(kinds), daily, total, days)
        am.change_authorization(s, a, "activate", "owner")
        return a.id


def test_ok_campaign_cannot_approve_while_human_approval_required(make_ctx):
    ctx = make_ctx(require_human_approval=True, outbound_email_enabled=True)
    fid = seed_email(ctx)
    _authorize(ctx, "pat@acme.example")
    with ctx.db.session() as s:
        assert am.apply_campaign_approvals(s, ctx.settings, utcnow(), "system") == []
        assert any("REQUIRE_HUMAN_APPROVAL" in b for b in am.blockers(s, ctx.settings, True))
        f = s.get(FollowUp, fid)
        assert f.status == "awaiting_approval" and f.approved_at is None and f.authorization_id is None
        assert am.autonomous_authorization(s, ctx.settings, f.lead, f.contact, "pat@acme.example") is None


def test_ok_campaign_approves_only_when_every_switch_allows_and_is_taken_back(make_ctx):
    ctx = make_ctx(require_human_approval=False, outbound_email_enabled=True)
    fid = seed_email(ctx)
    other = seed_email(ctx, to="sam@other.example")
    _authorize(ctx, "pat@acme.example")
    with ctx.db.session() as s:
        assert am.apply_campaign_approvals(s, ctx.settings, utcnow(), "system") == [fid]  # the named address only
        assert s.get(FollowUp, other).status == "awaiting_approval"
    ctx.settings.require_human_approval = True  # switched back on: every approval a campaign gave is taken back
    with ctx.db.session() as s:
        assert "REQUIRE_HUMAN_APPROVAL" in am.campaign_approval_problem(s, s.get(FollowUp, fid), utcnow(), ctx.settings)


def test_ok_campaign_limits_expiry_and_emergency_stop(make_ctx, clock):
    ctx = make_ctx(require_human_approval=False, outbound_email_enabled=True)
    a_id = _authorize(ctx, "a@one.example b@two.example", daily=1)
    first, second = seed_email(ctx, to="a@one.example"), seed_email(ctx, to="b@two.example")
    with ctx.db.session() as s:  # daily limit 1: only one of the two
        assert am.apply_campaign_approvals(s, ctx.settings, utcnow(), "system") == [first]
        assert s.get(FollowUp, second).status == "awaiting_approval"
    clock.forward(timedelta(days=8))  # authorization expired
    with ctx.db.session() as s:
        assert am.apply_campaign_approvals(s, ctx.settings, utcnow(), "system") == []
        am.expire_authorizations(s)
    with ctx.db.session() as s:
        assert s.get(am.CampaignAuthorization, a_id).status == "expired"
    b_id = _authorize(ctx, "c@three.example")
    with ctx.db.session() as s:
        am.change_state(s, "emergency_stop", "owner")
    with ctx.db.session() as s:
        assert s.get(am.CampaignAuthorization, b_id).status == "stopped"


def test_ok_authorization_rejects_wildcards_and_oversize_limits(ctx):
    with ctx.db.session() as s:
        with pytest.raises(ValueError):
            am.create_authorization(s, "o", "c", "*@acme.example", ["outreach"], 1, 1, 1)
        with pytest.raises(ValueError):
            am.create_authorization(s, "o", "c", "a@acme.example", ["outreach"], 1, 1, 31)


# ================================================================================================= Audiences
def _lead(s, name, status="new", grade="A", score=80, industry=INDUSTRY, state="TX", source="research", **kw):
    co = Company(name=name, normalized_name=name.lower(), domain=f"{name.lower()}.example", source="manual",
                 industry=industry, state=state)
    s.add(co)
    s.flush()
    ld = Lead(company_id=co.id, source=source, status=status, grade=grade, score=score, primary_service=SERVICE, **kw)
    s.add(ld)
    s.flush()
    return ld


def test_ok_audience_filter_counts_and_attach_changes_only_attribution(ctx):
    with ctx.db.session() as s:
        hit = _lead(s, "Hit").id
        _lead(s, "Wrongstate", state="CA")
        _lead(s, "Lowscore", score=10)
        _lead(s, "Dropped", status="disqualified")
        camp = Campaign(name="C", objective="o", start_date=date(2026, 10, 1), end_date=date(2026, 10, 31))
        s.add(camp)
        s.flush()
        cid = camp.id
    client = client_for(ctx)
    r = client.post(f"/campaigns/{cid}/audience", headers=ORIGIN, follow_redirects=False,
                    data={"states": "tx", "min_score": "50", "industries": f"{INDUSTRY},nonsense", "attach": "yes"})
    assert r.status_code == 303 and "1+matching" in r.headers["location"].replace("%20", "+")
    with ctx.db.session() as s:
        ld = s.get(Lead, hit)
        assert ld.campaign_id == cid and ld.status == "new" and ld.outreach_status is None
        assert s.get(Campaign, cid).audience["industries"] == [INDUSTRY]
        assert s.execute(select(FollowUp)).first() is None


def test_ok_audience_parser_survives_bad_input(ctx):
    a = parse({"states": "tx, 123, c", "min_score": "abc", "grades": "z,a"})
    assert a["states"] == ["TX"] and a["min_score"] == 0 and a["grades"] == ["A"]
    assert describe({}) == "every open lead"


def test_defect_audience_every_open_lead_counts_converted_customers(ctx):
    """The Campaigns page describes an empty audience as "every open lead"; a customer already won is no open lead,
    and "attach" credits the new campaign with it."""
    with ctx.db.session() as s:
        _lead(s, "Open")
        _lead(s, "Customer", status="converted")
        names = [ld.company.name for ld in matching(s, {})]
    assert names == ["Open"], f"{describe({})!r} must not include converted leads: {names}"


def test_defect_campaign_end_before_start_is_accepted(ctx):
    r = client_for(ctx).post("/campaigns", headers=ORIGIN, follow_redirects=False, data={
        "name": "Backwards", "objective": "o", "start_date": "2026-11-30", "end_date": "2026-11-01"})
    assert r.status_code == 303
    with ctx.db.session() as s:
        assert s.execute(select(Campaign)).first() is None, "a campaign that ends before it starts must be refused"


# ================================================================================================= LinkedIn Lead Sync
def test_ok_lead_sync_consent_duplicates_and_test_leads(make_ctx):
    from aime.workflows import job_inbound
    fake = FakeLinkedIn([li_lead("a1", email="yes@brightpath-realty.example", consent=True),
                         li_lead("a1", email="yes@brightpath-realty.example", consent=True),
                         li_lead("a2", email="no@pixelpine.example", consent=False),
                         li_lead("a3", email="test@pixelpine.example", test=True),
                         li_lead("a4", email="")])
    ctx = leads_ctx(make_ctx, fake)
    job_inbound(ctx)
    job_inbound(ctx)  # the second run finds the same leads again
    with ctx.db.session() as s:
        rows = s.execute(select(InboundInquiry)).scalars().all()
        assert len(rows) == 3  # a1 once, a2, and a4 kept as rejected (no way to reply); a3 is a test lead
        assert sorted(r.status for r in rows) == ["processed", "processed", "rejected"]
        consent = {c.email: c.consent_status for c in s.execute(select(Contact)).scalars()}
    assert consent.get("yes@brightpath-realty.example") == "opt_in"
    assert consent.get("no@pixelpine.example") == "inquiry"  # no marketing opt-in without the tick
    assert "test@pixelpine.example" not in consent


def test_ok_lead_sync_off_and_401_is_reported(make_ctx):
    from aime.workflows import job_inbound
    fake = FakeLinkedIn([li_lead("a1")])
    ctx = leads_ctx(make_ctx, fake, enabled=False)
    before = len(fake.calls)
    assert job_inbound(ctx)
    assert len(fake.calls) == before
    fake2 = FakeLinkedIn([li_lead("a1")])
    ctx2 = leads_ctx(make_ctx, fake2)
    fake2.fail_responses = 401
    job_inbound(ctx2)
    assert any("401" in a.message or "connect" in a.message.lower() for a in alerts(ctx2))


# ================================================================================================= CSV import
def _csv(ctx, name, text, encoding="utf-8"):
    ctx.settings.imports_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.settings.imports_dir / name
    path.write_bytes(text.encode(encoding) if isinstance(text, str) else text)
    return path


def _import(ctx):
    return USBusinessResearchAgent(ctx).run({"industries": [], "states": [], "sources": ["csv_import"]})


def test_ok_csv_import_dedupes_and_each_file_runs_once(ctx):
    _csv(ctx, "list.csv", "name,website,state\nAcme Dev Shop,https://acme-devshop.example,TX\n"
                          "Acme Dev Shop LLC,https://www.acme-devshop.example/,TX\nNorthwind,https://northwind-analytics.example,NY\n")
    res = _import(ctx)
    assert res.ok, res.error
    with ctx.db.session() as s:
        assert sorted(c.domain for c in s.execute(select(Company)).scalars()) == ["acme-devshop.example", "northwind-analytics.example"]
        assert s.execute(select(Contact)).first() is None  # a company list creates no contact, so no one to email
    assert not list(ctx.settings.imports_dir.glob("*.csv")) and list((ctx.settings.imports_dir / "processed").glob("*list.csv"))
    _csv(ctx, "copy.csv", "name,website,state\nAcme Dev Shop,https://acme-devshop.example,TX\n")
    assert _import(ctx).ok
    with ctx.db.session() as s:
        assert len(s.execute(select(Company)).scalars().all()) == 2


def test_ok_csv_unreadable_file_is_rejected_with_an_alert(ctx):
    _csv(ctx, "bad.csv", b"name,website\n\xff\xfe\x00garbage\xff\n")
    res = _import(ctx)
    assert res.ok and res.output.import_files_rejected
    assert list((ctx.settings.imports_dir / "rejected").glob("*bad.csv")) and alerts(ctx, "Import file not imported%")


def test_defect_csv_row_with_stray_comma_kills_the_research_run(ctx):
    """An unquoted comma ("Acme, Inc") gives a row more fields than headers: DictReader's overflow list hits .strip().
    AttributeError is not caught, so the whole research agent fails, today and at every run, as the file stays."""
    _csv(ctx, "list.csv", "name,website,state\nAcme, Inc,https://acme-devshop.example,TX\nNorthwind,https://northwind-analytics.example,NY\n")
    res = _import(ctx)
    assert res.ok, f"one bad row must not fail the research job: {res.error}"


def test_ok_csv_bad_rows_keep_the_good_rows_and_the_owner_is_told(ctx):
    _csv(ctx, "list.csv", "name,website,state\nAcme, Inc,https://acme-devshop.example,TX\nNorthwind,https://northwind-analytics.example,NY\n"
                          "Nostate,https://pixelpine.example,\n,https://brightpath-realty.example,TX\n")
    res = _import(ctx)
    assert res.ok, res.error
    with ctx.db.session() as s:
        assert [c.name for c in s.execute(select(Company)).scalars()] == ["Northwind"]
    assert list((ctx.settings.imports_dir / "processed").glob("*list.csv"))
    notice = alerts(ctx, "Import file partly imported:%")[0].message
    assert "3 of 4" in notice and "row 2" in notice and "row 5" in notice and "without a US state" in notice


def test_ok_csv_with_only_unusable_rows_goes_to_rejected_and_a_corrected_copy_imports(ctx):
    _csv(ctx, "list.csv", "name,website\nNorthwind,https://northwind-analytics.example\n")
    assert _import(ctx).ok
    assert list((ctx.settings.imports_dir / "rejected").glob("*list.csv"))
    assert not list((ctx.settings.imports_dir / "processed").glob("*list.csv"))
    assert alerts(ctx, "Import file not imported:%without a US state%")
    _csv(ctx, "list.csv", "name,website,state\nNorthwind,https://northwind-analytics.example,NY\n")
    assert _import(ctx).ok
    with ctx.db.session() as s:
        assert s.execute(select(Company)).scalars().one().name == "Northwind"


def test_ok_csv_is_imported_while_every_public_source_is_down(make_ctx):
    def down(request):
        raise httpx.ConnectError("no route", request=request)
    ctx = make_ctx(handler=down, network_enabled=True)
    _csv(ctx, "list.csv", "name,website,state\nNorthwind,https://northwind-analytics.example,NY\n")
    res = USBusinessResearchAgent(ctx).run({})
    assert res.ok and res.output.companies_created == 1, res.error  # it found something, and says what it could not read
    assert alerts(ctx, "Research source(s) not read%")


def test_defect_csv_without_state_is_archived_with_nothing_imported_and_no_notice(ctx):
    """csv_import.py documents only `name` as required. Rows without a state are skipped as unknown location, the file
    moves to processed/ and no alert says so: the owner's list disappears."""
    _csv(ctx, "list.csv", "name,website\nNorthwind,https://northwind-analytics.example\n")
    res = _import(ctx)
    assert res.ok
    with ctx.db.session() as s:
        imported = s.execute(select(Company)).first() is not None
    assert imported or alerts(ctx) or res.output.import_files_rejected, "nothing imported and nothing said about it"


def test_defect_csv_with_unknown_headers_is_archived_as_if_imported(ctx):
    _csv(ctx, "list.csv", "Organisation,Homepage,Region\nNorthwind,https://northwind-analytics.example,NY\n")
    res = _import(ctx)
    assert res.ok
    assert alerts(ctx) or res.output.import_files_rejected, "a file with no usable column must be reported, not archived silently"


# ================================================================================================= Metrics CSV
def _metrics(ctx, name, text):
    folder = ctx.settings.imports_dir / "linkedin_metrics"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def _report(ctx, kind="daily", **kw):
    return AnalyticsReportingAgent(ctx).run({"kind": kind, **kw})


def test_ok_metrics_csv_attaches_and_bad_file_is_rejected(ctx):
    cid = add_post(ctx, status="published", external_id="urn:li:share:9", external_url="https://www.linkedin.com/feed/update/urn:li:share:9/")
    _metrics(ctx, "a.csv", "Post URL,Impressions,Reactions,Comments\nhttps://www.linkedin.com/feed/update/urn:li:share:9/,\"1,200\",7,x\n")
    (ctx.settings.imports_dir / "linkedin_metrics" / "b.csv").write_bytes(b"post url,impressions\n\xff\xfe\n")
    res = _report(ctx)
    assert res.ok and res.output.metrics_imported == 1
    with ctx.db.session() as s:
        m = s.execute(select(ContentMetric)).scalars().one()
        assert (m.content_id, m.impressions, m.reactions, m.comments) == (cid, 1200, 7, 0)


def test_defect_metrics_csv_with_infinite_number_blocks_every_report(ctx):
    """_num catches ValueError only: float("inf") or "1e999" raises OverflowError out of int(). The transaction rolls
    back, the file stays, and every later daily and weekly report fails on it."""
    _metrics(ctx, "a.csv", "post url,impressions\nhttps://www.linkedin.com/feed/update/urn:li:share:9/,1e999\n")
    res = _report(ctx)
    assert res.ok, f"one bad figure must not stop reports: {res.error}"


def test_defect_reexported_metrics_are_added_up(ctx):
    """LinkedIn exports hold each post's running totals. A second export a week later is added to the first (no
    snapshot logic), so the report's "Impressions" for the post is double-counted."""
    add_post(ctx, status="published", external_id="urn:li:share:9", external_url="https://www.linkedin.com/feed/update/urn:li:share:9/",
             title="Metric post")
    url = "https://www.linkedin.com/feed/update/urn:li:share:9/"
    _metrics(ctx, "week1.csv", f"post url,impressions,reactions\n{url},100,5\n")
    _report(ctx)
    _metrics(ctx, "week2.csv", f"post url,impressions,reactions\n{url},150,6\n")
    res = _report(ctx)
    md = Path(res.output.path_md).read_text(encoding="utf-8")
    row = next(ln for ln in md.splitlines() if ln.startswith("| Metric post"))
    assert "| 150 |" in row, f"the post has 150 impressions in total, the report says: {row}"


# ================================================================================================= Dashboards vs database
@pytest.fixture
def scenario(make_ctx):
    """A known set of records; every figure below is counted by hand from this list."""
    ctx = make_ctx(llm_cost_per_1k_tokens=0.01)
    now = utcnow()
    day, ago = timedelta(days=1), lambda d: now - timedelta(days=d)
    with ctx.db.session() as s:
        s.add(User(username="alice", password_hash="x", role="sales_manager"))
        cos = []
        for i, (created, extra) in enumerate([(ago(0.5), {}), (ago(0.5), {}), (ago(2), {}), (ago(3), {}), (ago(10), {}), (ago(10), {}),
                                              (ago(40), {})]):
            c = Company(name=f"Co {i}", normalized_name=f"co {i}", domain=f"co{i}.example", source="manual", created_at=created,
                        website_status="verified" if i < 2 else None, **extra)
            s.add(c)
            cos.append(c)
        s.flush()
        s.add(Company(name="Merged", normalized_name="merged", domain="merged.example", source="manual", created_at=ago(0.5),
                      merged_into_id=cos[0].id))
        s.flush()
        for i in range(3):  # usable published addresses
            s.add(Contact(company_id=cos[i].id, first_name="A", last_name=f"B{i}", email=f"a{i}@co{i}.example", source="website",
                          email_status="published", email_source_url="https://x.example", evidence="e", evidence_url="https://x.example",
                          discovered_at=ago(2)))
        s.add(Contact(company_id=cos[3].id, first_name="O", last_name="Out", email="out@co3.example", source="website",
                      email_status="published", consent_status="opted_out", email_source_url="https://x.example", evidence="e",
                      evidence_url="https://x.example", discovered_at=ago(2)))
        s.add(Contact(company_id=cos[4].id, email="asker@co4.example", source="inbound", consent_status="inquiry"))
        s.flush()
        statuses = ["qualified", "qualified", "new", "nurture", "disqualified", "converted"]
        leads = []
        for i, st in enumerate(statuses):
            ld = Lead(company_id=cos[i].id, source="research", status=st, grade="A" if i < 2 else "C", score=80 - i, created_at=ago(2),
                      owner="alice" if i < 2 else None)
            s.add(ld)
            leads.append(ld)
        s.flush()
        leads[0].outreach_status, leads[1].outreach_status = "interested", "contacted"
        sent = dict(channel="email", kind="outreach", status="sent", subject="s", body="b", sent_at=ago(0.5), approved_at=ago(2), approved_by="alice")
        s.add(FollowUp(lead_id=leads[0].id, company_id=cos[0].id, step=1, **sent))
        s.add(FollowUp(lead_id=leads[1].id, company_id=cos[1].id, step=1, **sent))
        s.add(FollowUp(lead_id=leads[1].id, company_id=cos[1].id, step=2, **sent))
        for i in (2, 3):
            s.add(FollowUp(lead_id=leads[i].id, channel="email", kind="outreach", status="awaiting_approval", subject="w", body="b", step=1))
        s.add(FollowUp(lead_id=leads[0].id, channel="task", status="pending", subject="Do this", step=1))
        s.add(FollowUp(lead_id=leads[0].id, channel="email", kind="outreach", status="uncertain", subject="u", body="b", step=1, approved_at=ago(0.5)))
        meet = dict(invitee_email="m@co0.example", starts_at=now + day, ends_at=now + day, source="manual", created_at=ago(0.5))
        s.add(Meeting(lead_id=leads[0].id, status="scheduled", **meet))
        s.add(Meeting(lead_id=leads[0].id, status="completed", **meet))
        s.add(Meeting(lead_id=leads[1].id, status="cancelled", **meet))
        s.add(Meeting(lead_id=leads[1].id, status="scheduled", is_simulated=True, **meet))
        mail = dict(from_email="p@co0.example", received_at=ago(0.5), processed_at=ago(0.5), status="actioned")
        s.add(InboundEmail(lead_id=leads[0].id, category="interested", message_id="m1", **mail))
        s.add(InboundEmail(lead_id=leads[1].id, category="interested", message_id="m2", **mail))
        s.add(InboundEmail(lead_id=leads[1].id, category="not_interested", message_id="m3", **mail))
        s.add(InboundEmail(lead_id=leads[1].id, category="unsubscribe", message_id="m4", **mail))
        s.add(InboundEmail(lead_id=leads[1].id, category="bounce", message_id="m5", **mail))
        s.add(InboundEmail(lead_id=leads[1].id, category="interested", is_simulated=True, message_id="m6", **mail))
        opp = dict(company_id=cos[0].id, lead_id=leads[0].id, created_at=ago(2))
        s.add(Opportunity(name="O new", stage="new", **opp))
        s.add(Opportunity(name="O disc", stage="discovery", owner="alice", **opp))
        s.add(Opportunity(name="O won", stage="won", est_value=5000.0, owner="alice", won_by="alice", origin="ai", closed_at=ago(0.5), **opp))
        s.add(Opportunity(name="O lost", stage="lost", owner="alice", closed_at=ago(0.5), **opp))
        for st, n in (("awaiting_approval", 2), ("approved", 1), ("published", 2), ("uncertain", 1), ("failed", 1), ("rejected", 1)):
            for j in range(n):
                extra = {"external_id": f"urn:li:share:{j}", "published_at": ago(0.5)} if st == "published" else {}
                s.add(ContentItem(service=SERVICE, industry=INDUSTRY, title=f"{st} {j}", body="b?", hashtags=[], status=st,
                                  generator="template", fingerprint=f"{st}{j}", scheduled_for=now + timedelta(days=j + 1),
                                  approved_at=ago(0.5) if st in ("approved", "published", "uncertain") else None, **extra))
        run = dict(agent="linkedin_content", run_id="r", started_at=ago(0.5), finished_at=ago(0.5), duration_ms=1000)
        s.add(AgentRun(status="success", llm_calls=2, llm_tokens=300, llm_ms=500, llm_cost_usd=0.003, **run))
        s.add(AgentRun(status="success", llm_calls=1, llm_tokens=200, llm_ms=250, llm_cost_usd=0.002, **run))
        s.add(AgentRun(status="failed", error="boom", **run))
        s.add(AgentRun(status="success", **{**run, "agent": "analytics"}))
    return ctx


def _cards(ctx, days=7):
    with ctx.db.session() as s:
        view = dashboard_view(ctx, s, days)
    return {c["label"]: c for c in view["cards"]}, view


def test_ok_home_cards_match_records(scenario):
    cards, view = _cards(scenario)
    assert {k: c["value"] for k, c in cards.items()} == {"Companies discovered": 7, "Usable contacts": 3, "Emails sent": 3,
                                                         "Meetings booked": 2}
    assert cards["Companies discovered"]["period"] == 4 and cards["Companies discovered"]["change"] == "+2"
    assert cards["Usable contacts"]["period"] == 3 and cards["Emails sent"]["period"] == 3
    assert view["leads_badge"]["count"] == 6
    html = client_for(scenario).get("/").text
    # the big number is the chosen period (T03); the total stays beside it
    assert re.findall(r'class="kpi-value">([\d,]+)<', html) == ["4", "3", "3", "2"]
    assert re.findall(r'class="kpi-total">Last 7 days · ([\d,]+) in total<', html) == ["7", "3", "3", "2"]
    stages = {st["label"]: st["value"] for st in view["pipeline_stages"]}
    # the disqualified lead is not among the new leads (T04)
    assert stages == {"New leads": 5, "Contacted": 2, "Interested": 1, "Meeting booked": 2, "Closed won": 1}


def test_ok_funnel_tiles_and_api_stats_match_records(scenario):
    with scenario.db.session() as s:
        tiles = {label: n for label, n, _ in funnel_detail(s)}
    assert tiles == {"Companies found": 7, "Websites verified": 2, "Companies with a decision-maker": 4,
                     "Companies with a usable email": 3, "Qualified companies": 2, "Emails awaiting approval": 2,
                     "Posts awaiting approval": 2, "Posts scheduled": 1, "Posts published": 2, "Open opportunities": 2,
                     "Outreach emails sent": 3, "Replies (real)": 3, "Interested": 1, "Meetings booked": 2, "Deals won": 1}
    client = client_for(scenario)
    stats = client.get("/api/stats").json()
    assert stats["companies"] == 7 and stats["content_by_status"]["awaiting_approval"] == 2
    assert stats["opportunities_by_stage"] == {"new": 1, "discovery": 1, "won": 1, "lost": 1}
    reports = text_of(client.get("/reports").text)
    assert "3 Outreach emails sent" in reports and "3 Replies (real)" in reports and "2 Meetings booked" in reports


def test_ok_system_page_kpis_match_records(scenario):
    text = text_of(client_for(scenario).get("/system").text)
    for label, n in (("Companies", 7), ("Open leads", 4), ("Qualified", 2), ("Grade A", 2), ("Open opportunities", 2),
                     ("Emails to approve", 2), ("Posts to approve", 2), ("Posts published", 2)):
        assert f"{n} {label}" in text or f"{label} {n}" in text, label


def test_ok_pipeline_page_board_matches_records(scenario):
    text = text_of(client_for(scenario).get("/pipeline").text)
    assert re.search(r"New \( ?1 ?\)", text) and re.search(r"Won \( ?1 ?\)", text) and re.search(r"Lost \( ?1 ?\)", text)
    assert "contacted: 1" in text and "interested: 1" in text
    assert "$5,000" in text


def test_ok_performance_page_matches_records(scenario):
    with scenario.db.session() as s:
        now = utcnow()
        r = build(s, now - timedelta(days=30), now + timedelta(days=1))
    alice = next(x for x in r.rows if x.username == "alice")
    # leads 0 and 1 belong to alice: 3 sent, 2 interested, 2 negative, 2 non-simulated... meetings created in the period
    assert (alice.leads_owned, alice.emails_sent, alice.positive_replies, alice.negative_replies) == (2, 3, 2, 2)
    # a cancelled meeting is not counted, as on the home page and the funnel (T05)
    assert (alice.meetings, alice.won, alice.lost, alice.won_value, alice.won_value_ai_sourced) == (2, 1, 1, 5000.0, 5000.0)
    assert alice.conversion == 0.5 and r.total.won_value == 5000.0 and r.ai["won_value_human_sourced"] == 0
    html = client_for(scenario).get("/performance").text
    assert "50%" in html and "$5,000" in html


def test_ok_agents_page_ai_usage_totals_match_runs(scenario):
    text = text_of(client_for(scenario).get("/agents").text)
    assert "3 calls · 500 tokens" in text and "$0.0050" in text


def test_ok_real_llm_usage_is_recorded_per_run_and_priced(make_ctx):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "uat:latest"}]})
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"response": "too short", "prompt_eval_count": 100, "eval_count": 50})
        return hn_handler(request)
    ctx = make_ctx(handler=handler, llm_provider="ollama", ollama_model="uat:latest", llm_cost_per_1k_tokens=0.01)
    res = LinkedInContentAgent(ctx).run({"count": 2, "research_topics": False})
    assert res.ok and len(res.output.created) == 2 and res.output.llm_rejections == 2
    with ctx.db.session() as s:
        run = s.execute(select(AgentRun).where(AgentRun.agent == "linkedin_content")).scalars().one()
        assert (run.llm_calls, run.llm_tokens) == (4, 600) and run.llm_cost_usd == pytest.approx(0.006)
    assert "4 calls · 600 tokens" in text_of(client_for(ctx).get("/agents").text)


def test_defect_meetings_card_period_counts_cancelled_bookings(scenario):
    """The card's total leaves cancelled meetings out, its 7-day figure and trend line count them (app.py window call)."""
    with scenario.db.session() as s:
        for m in s.execute(select(Meeting).where(Meeting.status != "cancelled")).scalars():
            m.status = "cancelled"
    cards, _ = _cards(scenario)
    c = cards["Meetings booked"]
    assert c["value"] == 0
    assert c["period"] == 0, f"0 meetings booked in total, yet the card says {c['period']} this period ({c['change']})"


def test_defect_meetings_per_week_chart_counts_cancelled_bookings(scenario):
    """/reports "Meetings booked per week" includes cancelled and disagrees with the funnel right next to it."""
    with scenario.db.session() as s:
        for m in s.execute(select(Meeting).where(Meeting.status != "cancelled")).scalars():
            m.status = "cancelled"
    html = client_for(scenario).get("/reports").text
    assert "No meetings booked in the last 8 weeks" in html


def test_defect_home_chart_total_disagrees_with_card_near_the_period_edge(make_ctx, clock):
    """The card counts a rolling 7x24 h; the chart buckets by calendar day and drops the part of the oldest day."""
    clock(datetime(2026, 10, 27, 14, 0))
    ctx = make_ctx()
    with ctx.db.session() as s:
        s.add(Company(name="Edge", normalized_name="edge", domain="edge.example", source="manual",
                      created_at=utcnow() - timedelta(days=6, hours=23)))
    cards, view = _cards(ctx)
    series = {name: total for name, total, _ in view["performance"][0]["series"]}
    assert cards["Companies discovered"]["period"] == 1
    assert series["Companies discovered"] == 1, "same period, same page, two different numbers"


def test_defect_conversion_rate_on_home_can_exceed_100_percent(ctx):
    """The funnel divides meetings (records) by interested leads (people): one lead with two meetings shows 200%."""
    with ctx.db.session() as s:
        ld = _lead(s, "Twice", outreach_status="interested")
        for _i in range(2):
            s.add(Meeting(lead_id=ld.id, invitee_email="x@twice.example", starts_at=utcnow(), ends_at=utcnow(), source="manual"))
    _, view = _cards(ctx)
    rate = next(st["rate"] for st in view["pipeline_stages"] if st["label"] == "Meeting booked")
    assert int(rate.split("%")[0]) <= 100, f"a conversion rate of {rate}"


def test_defect_home_day_labels_use_utc_not_the_owners_day(make_ctx, clock):
    """manager_today: "Today starts at local midnight". The home chart's last day is the UTC date: from 00:00 to 05:30
    IST the owner sees yesterday as today, with the night's work under the wrong day."""
    clock(datetime(2026, 10, 30, 20, 0))  # Saturday 31 Oct, 01:30 in India
    ctx = make_ctx()
    ctx.settings.schedule["timezone"] = "Asia/Kolkata"
    with ctx.db.session() as s:
        s.add(Company(name="Night", normalized_name="night", domain="night.example", source="manual"))
    _, view = _cards(ctx)
    chart = str(view["performance"][0]["chart"])
    days = re.findall(r"<g class=\"col\"><title>(\w+):", chart)
    assert days[-1] == "Sat", f"it is Saturday for the owner; the chart's last day is {days[-1]}"


def test_ok_empty_database_has_no_zero_division(ctx):
    cards, view = _cards(ctx)
    assert all(c["value"] == 0 and c["change"] == "—" for c in cards.values())
    assert [st["rate"] for st in view["pipeline_stages"]] == ["all leads", "", "", "", ""]
    res = _report(ctx)
    assert res.ok, res.error
    md = Path(res.output.path_md).read_text(encoding="utf-8")
    assert "Reply rate (leads who replied / leads contacted) | — | — |" in md
    with ctx.db.session() as s:
        assert build(s, utcnow() - timedelta(days=1), utcnow()).total.conversion is None


PAGES = ["/", "/?range=30", "/?range=abc", "/manager", "/command-center", "/system", "/leads", "/leads?page=2", "/contacts", "/companies",
         "/pipeline", "/meetings", "/followups", "/followups?show=all", "/content", "/content?status=published", "/policy",
         "/campaigns", "/inbound", "/agents", "/agents?agent=none", "/search?q=acme", "/search?q=%25", "/approvals", "/settings",
         "/automation", "/audit", "/jobs", "/reports", "/integrations", "/inbox", "/inbox?show=all", "/performance",
         "/performance?start=2026-01-01&end=2026-12-31", "/performance?start=2026-12-31&end=2026-01-01", "/performance?source=none",
         "/my", "/admin/users", "/admin/teams", "/admin/roles", "/admin/settings", "/admin/mailboxes", "/api/health", "/api/stats",
         "/api/leads", "/login", "/setup"]


@pytest.mark.parametrize("path", PAGES)
def test_ok_page_on_empty_database(ctx, path):
    assert client_for(ctx).get(path, follow_redirects=False).status_code < 500


def test_ok_every_page_with_scenario_data(scenario):
    client = client_for(scenario)
    bad = {p: client.get(p, follow_redirects=False).status_code for p in PAGES if p != "/manager"}  # see the defect below
    assert {p: c for p, c in bad.items() if c >= 500} == {}
    with scenario.db.session() as s:
        cid = s.execute(select(ContentItem.id)).scalars().first()
        lid = s.execute(select(Lead.id)).scalars().first()
        oid = s.execute(select(Opportunity.id)).scalars().first()
        run = s.execute(select(AgentRun.id)).scalars().first()
    for path in (f"/content/{cid}", f"/leads/{lid}", f"/opportunities/{oid}", f"/agents/runs/{run}",
                 "/command-center/agents/linkedin_content"):
        assert client.get(path).status_code == 200, path


def test_defect_manager_page_500_once_a_meeting_is_booked(ctx):
    """manager_today returns Meeting rows after its session closed and manager.html:82 reads m.lead: a lazy load on a
    detached row (DetachedInstanceError). Any upcoming real meeting with a lead makes Manager Today unavailable."""
    with ctx.db.session() as s:
        ld = _lead(s, "Booked")
        s.add(Meeting(lead_id=ld.id, company_id=ld.company_id, invitee_email="pat@booked.example", source="calendly_email",
                      status="scheduled", starts_at=utcnow() + timedelta(days=1), ends_at=utcnow() + timedelta(days=1, hours=1)))
    assert client_for(ctx).get("/manager").status_code == 200


def test_ok_manager_page_figures_and_local_day_start(scenario, clock):
    """Everything on Manager Today except the meeting list (past meetings here). "Today" starts at local midnight."""
    with scenario.db.session() as s:
        for m in s.execute(select(Meeting)).scalars():
            m.starts_at, m.ends_at = utcnow() - timedelta(days=1), utcnow() - timedelta(days=1)
    assert client_for(scenario).get("/manager").status_code == 200
    scenario.settings.schedule["timezone"] = "Asia/Kolkata"
    now = clock(datetime(2026, 10, 30, 19, 0))  # Saturday 31 Oct 00:30 in India
    with scenario.db.session() as s:
        s.add(Company(name="Before", normalized_name="before", domain="before.example", source="manual", created_at=datetime(2026, 10, 30, 18, 29)))
        s.add(Company(name="After", normalized_name="after", domain="after.example", source="manual", created_at=datetime(2026, 10, 30, 18, 31)))
    from aime.web.app import manager_today
    with scenario.db.session() as s:
        view = manager_today(scenario, s)
    assert view["counts"]["discovered"] == 1 and "Saturday 31 October 2026" in view["day_label"] and now


def test_ok_performance_date_filter_includes_the_whole_end_day(ctx):
    with ctx.db.session() as s:
        ld = _lead(s, "Edge", owner=None)
        for when in (datetime(2026, 3, 1, 0, 0), datetime(2026, 3, 31, 23, 59), datetime(2026, 4, 1, 0, 0), datetime(2026, 2, 28, 23, 59)):
            s.add(FollowUp(lead_id=ld.id, channel="email", status="sent", subject="s", sent_at=when, step=1))
        s.flush()
        r = build(s, datetime(2026, 3, 1), datetime(2026, 4, 1))
    assert r.total.emails_sent == 2


def test_ok_chart_buckets_on_week_and_day_boundaries():
    from aime.web.charts import daily_counts, weekly_buckets
    today = date(2026, 11, 1)  # a Sunday
    weeks = weekly_buckets([datetime(2026, 10, 26, 0, 0), datetime(2026, 11, 1, 23, 59), datetime(2026, 10, 25, 23, 59)], today=today)
    assert weeks[-1] == ("Oct 26", 2) and weeks[-2] == ("Oct 19", 1)
    assert daily_counts([datetime(2026, 11, 1, 0, 0), datetime(2026, 10, 31, 23, 59), datetime(2026, 10, 26, 0, 0), None], 7, today=today) == [1, 0, 0, 0, 0, 1, 1]


def test_defect_performance_page_500_on_last_representable_date(ctx):
    r = client_for(ctx).get("/performance?end=9999-12-31", follow_redirects=False)
    assert r.status_code < 500, "a date typed into the filter must not crash the page"


# ================================================================================================= Reports
def test_ok_daily_report_figures_match_records_and_rerun_keeps_one_row(scenario):
    res = _report(scenario, import_metrics=False)
    assert res.ok, res.error
    k = res.output.kpis
    assert (k["companies_total"], k["companies_new_period"], k["leads_open"], k["leads_qualified"]) == (7, 2, 4, 2)
    assert (k["emails_sent_period"], k["outreach_sent_total"], k["outreach_drafted_total"]) == (3, 3, 6)
    assert (k["replies_total"], k["interested_total"], k["bounces_total"], k["unsubscribes_total"]) == (3, 2, 1, 1)
    assert (k["meetings_booked_total"], k["meetings_held_total"], k["simulated_replies_total"], k["simulated_meetings_total"]) == (2, 1, 1, 1)
    assert (k["content_published_total"], k["content_awaiting_approval"]) == (2, 2)
    md = Path(res.output.path_md).read_text(encoding="utf-8")
    assert "| Reply rate (leads who replied / leads contacted) | — | 100% (2/2) |" in md
    assert "| Interest rate (interested leads / leads who replied) | — | 100% (2/2) |" in md
    assert "SIMULATED test activity" in md
    assert _report(scenario, import_metrics=False).ok
    client = client_for(scenario)
    with scenario.db.session() as s:
        rows = s.execute(select(Report)).scalars().all()
        assert len(rows) == 1
        rid = rows[0].id
    assert client.get(f"/reports/{rid}").status_code == 200


def test_defect_daily_report_labels_outreach_as_approved_replies(scenario):
    """The record holds three outreach emails and no reply to anyone, yet "Emails sent (approved replies)" says 3, next
    to "Outreach sent (approved)" also 3: one email reads as two sent, and no reply was sent."""
    res = _report(scenario, import_metrics=False)
    md = Path(res.output.path_md).read_text(encoding="utf-8")
    row = next(ln for ln in md.splitlines() if ln.startswith("| Emails sent (approved replies)"))
    assert row == "| Emails sent (approved replies) | 0 | 0 |", row


def test_ok_weekly_report_compares_periods_and_year_end_file_name(scenario):
    res = _report(scenario, kind="weekly", import_metrics=False, report_date="2027-01-01")
    assert res.ok, res.error
    assert res.output.kpis["companies_new_period"] == 4
    assert Path(res.output.path_md).name == "weekly_2026-W53.md"
    md = Path(res.output.path_md).read_text(encoding="utf-8")
    assert "| Companies discovered | 4 | 2 |" in md and "## Segments (open leads)" in md


def test_ok_report_file_missing_is_404_not_500(ctx):
    res = _report(ctx)
    Path(res.output.path_html).unlink()
    assert client_for(ctx).get(f"/reports/{res.output.report_id}").status_code == 404


def test_ok_report_escapes_public_company_name(ctx):
    with ctx.db.session() as s:
        ld = _lead(s, "Evil")
        ld.company.name = "<script>x</script>|co"
        ld.last_scored_at = utcnow()
    res = _report(ctx)
    assert "<script>x" not in Path(res.output.path_html).read_text(encoding="utf-8")
    assert "<script>x" not in Path(res.output.path_md).read_text(encoding="utf-8")


# ================================================================================================= Every scheduled job
def test_ok_every_configured_job_is_registered(ctx):
    names = {sp.name for sp in load_job_specs(ctx.settings)}
    assert names == set(JOBS) and len(names) == 13


@pytest.mark.parametrize("name", list(JOBS))
def test_ok_job_runs_through_runner(ctx, name):
    res = run_job(ctx, name, trigger="scheduler")
    assert res["status"] == "success", res["error"]
    with ctx.db.session() as s:
        run = s.get(JobRun, res["run_id"])
        assert run.status == "success" and run.finished_at and run.duration_ms is not None
        st = s.get(JobState, name)
        assert st.last_status == "success" and st.consecutive_failures == 0 and st.last_success_at
    assert not (ctx.settings.locks_dir / f"job_{name}.lock").exists()


def _state(**kw):
    return JobState(job="x", consecutive_failures=kw.pop("failures", 0), enabled=kw.pop("enabled", True), **kw)


def test_ok_due_logic_interval_and_slack():
    tz = ZoneInfo("Asia/Kolkata")
    spec = JobSpec("publish", 30, None, "")
    t = datetime(2026, 10, 30, 6, 0)
    assert is_due(spec, None, t, tz)[0]
    assert not is_due(spec, _state(last_attempt_at=t - timedelta(minutes=10)), t, tz)[0]
    assert is_due(spec, _state(last_attempt_at=t - timedelta(minutes=23)), t, tz)[0]  # a late tick does not lose the slot
    assert not is_due(spec, _state(last_attempt_at=t - timedelta(hours=1), enabled=False), t, tz)[0]


def test_ok_due_logic_daily_weekly_catch_up_once():
    tz = ZoneInfo("Asia/Kolkata")
    daily = JobSpec("plan", None, datetime(2026, 1, 1, 6, 45).time(), "")
    # 06:45 IST = 01:15 UTC. Asleep for three days: one catch-up run, for the latest slot only
    wake = datetime(2026, 10, 30, 9, 0)
    assert is_due(daily, _state(last_success_at=datetime(2026, 10, 27, 1, 15), last_attempt_at=datetime(2026, 10, 27, 1, 15)), wake, tz)[0]
    assert not is_due(daily, _state(last_success_at=wake, last_attempt_at=wake), wake + timedelta(minutes=15), tz)[0]
    assert not is_due(daily, _state(last_success_at=datetime(2026, 10, 30, 1, 20), last_attempt_at=datetime(2026, 10, 30, 1, 20)), datetime(2026, 10, 31, 1, 0), tz)[0]
    assert is_due(daily, _state(last_success_at=datetime(2026, 10, 30, 1, 20), last_attempt_at=datetime(2026, 10, 30, 1, 20)), datetime(2026, 10, 31, 1, 16), tz)[0]
    weekly = JobSpec("weekly_report", None, datetime(2026, 1, 1, 8, 0).time(), "", weekdays=(0,))
    monday_run = datetime(2026, 10, 26, 3, 0)
    assert not is_due(weekly, _state(last_success_at=monday_run, last_attempt_at=monday_run), datetime(2026, 11, 1, 3, 0), tz)[0]
    assert is_due(weekly, _state(last_success_at=monday_run, last_attempt_at=monday_run), datetime(2026, 11, 2, 3, 0), tz)[0]


def test_ok_retry_backoff_30_then_60_then_waits_for_next_slot(ctx, monkeypatch, clock):
    clock(datetime(2026, 10, 27, 14, 0))
    ctx.settings.schedule["timezone"] = "UTC"
    fast_jobs(monkeypatch)

    def boom(ctx, job="research"):
        raise RuntimeError("source down")
    monkeypatch.setitem(runner.JOBS, "research", boom)

    def due():
        return next(d for sp, d, _ in due_jobs(ctx) if sp.name == "research")

    assert due() and run_job(ctx, "research", "scheduler")["status"] == "failed"
    clock.forward(timedelta(minutes=29))
    assert not due()
    clock.forward(timedelta(minutes=2))
    assert due() and run_job(ctx, "research", "scheduler")["status"] == "failed"
    clock.forward(timedelta(minutes=59))
    assert not due()
    clock.forward(timedelta(minutes=2))
    assert due() and run_job(ctx, "research", "scheduler")["status"] == "failed"
    clock.forward(timedelta(hours=3))
    assert not due(), "three attempts per slot"
    clock(datetime(2026, 10, 28, 7, 30))
    assert due(), "the next day's slot starts over"
    assert run_job(ctx, "research", "scheduler")["status"] == "failed"
    with ctx.db.session() as s:
        assert s.get(JobState, "research").consecutive_failures == 1


def test_ok_next_run_shows_the_backoff_of_a_failed_job(ctx):
    tz = ZoneInfo("UTC")
    spec = JobSpec("research", None, datetime(2026, 1, 1, 7, 0).time(), "")
    now = datetime(2026, 10, 27, 10, 0)
    waiting = JobState(job="research", last_attempt_at=now - timedelta(minutes=10), last_status="failed", consecutive_failures=1, enabled=True)
    assert next_run(spec, waiting, now, tz) == now + timedelta(minutes=20)
    ready = JobState(job="research", last_attempt_at=now - timedelta(hours=1), last_status="failed", consecutive_failures=1, enabled=True)
    assert next_run(spec, ready, now, tz) == now
    wrong_clock = JobState(job="research", last_attempt_at=now + timedelta(days=30), last_success_at=now + timedelta(days=30), consecutive_failures=0, enabled=True)
    assert is_due(spec, wrong_clock, now, tz)[0] and next_run(spec, wrong_clock, now, tz) == now


def test_ok_failure_alert_raised_then_closed_when_job_recovers(ctx, monkeypatch):
    def boom(ctx, job="inbound"):
        raise RuntimeError("down: password=hunter2")
    monkeypatch.setitem(runner.JOBS, "inbound", boom)
    assert run_job(ctx, "inbound")["status"] == "failed"
    assert run_job(ctx, "monitor")["status"] == "success"
    assert not alerts(ctx, "Job 'inbound' failed%")
    res = run_job(ctx, "inbound")
    assert "hunter2" not in (res["error"] or "")  # secrets are redacted from what is stored
    run_job(ctx, "monitor")
    open_alert = [a for a in alerts(ctx, "Job 'inbound' failed%") if not a.acknowledged]
    assert len(open_alert) == 1 and open_alert[0].level == "error"
    assert client_for(ctx).get("/").status_code == 200
    monkeypatch.setitem(runner.JOBS, "inbound", lambda ctx, job="inbound": JobOutcome())
    assert run_job(ctx, "inbound")["status"] == "success"
    run_job(ctx, "monitor")
    closed = alerts(ctx, "Job 'inbound' failed%")
    assert closed and all(a.resolved_at for a in closed)


def test_ok_stale_job_alert_raised_and_resolved(ctx, clock):
    with ctx.db.session() as s:
        s.add(JobState(job="plan", last_success_at=utcnow() - timedelta(hours=30), last_attempt_at=utcnow() - timedelta(hours=30),
                       consecutive_failures=0, enabled=True))
        check_health(s, ctx.settings)
    assert alerts(ctx, "Job 'plan' has not succeeded%")
    with ctx.db.session() as s:
        s.get(JobState, "plan").last_success_at = utcnow()
    with ctx.db.session() as s:
        assert check_health(s, ctx.settings)["alerts_resolved"] == 1


def test_ok_crash_mid_job_lock_released_and_run_abandoned(ctx, monkeypatch, clock):
    def die(ctx, job="plan"):
        raise SystemExit("killed mid-job")
    monkeypatch.setitem(runner.JOBS, "plan", die)
    with pytest.raises(SystemExit):
        run_job(ctx, "plan")
    assert not (ctx.settings.locks_dir / "job_plan.lock").exists()
    with ctx.db.session() as s:
        run = s.execute(select(JobRun).where(JobRun.job == "plan")).scalars().one()
        assert run.status == "running"
    clock.forward(timedelta(minutes=11))
    assert recover(ctx)["abandoned_jobs"] == 1
    with ctx.db.session() as s:
        assert s.get(JobRun, run.id).status == "abandoned"
        st = s.get(JobState, "plan")
        assert st.consecutive_failures == 1 and st.last_status == "abandoned"
    assert client_for(ctx).get("/").status_code == 200


def test_ok_running_job_with_live_owner_is_not_abandoned_early(ctx, clock):
    with ctx.db.session() as s:
        s.add(JobRun(job="plan", trigger="scheduler", status="running", started_at=utcnow() - timedelta(minutes=30)))
    (ctx.settings.locks_dir / "job_plan.lock").parent.mkdir(parents=True, exist_ok=True)
    (ctx.settings.locks_dir / "job_plan.lock").write_text(f"{os.getpid()} x", encoding="utf-8")  # a live owner
    assert recover(ctx)["abandoned_jobs"] == 0
    clock.forward(timedelta(hours=2))  # hours later, abandoned whatever the lock says
    assert recover(ctx)["abandoned_jobs"] == 1


def test_defect_abandoned_run_uses_up_todays_retries_with_yesterdays_failures(ctx, clock):
    """Runbook: "failures from earlier days don't use up today's retries". _record honours that; recover() does not:
    it adds 1 to yesterday's count, so one crash today (a sleep mid-job) means no retry until tomorrow."""
    clock(datetime(2026, 10, 27, 14, 0))
    ctx.settings.schedule["timezone"] = "UTC"
    with ctx.db.session() as s:
        s.add(JobState(job="research", last_attempt_at=utcnow() - timedelta(hours=24), last_status="failed",
                       consecutive_failures=3, enabled=True))
        s.add(JobRun(job="research", trigger="scheduler", status="running", started_at=utcnow() - timedelta(minutes=11)))
    assert recover(ctx)["abandoned_jobs"] == 1
    clock.forward(timedelta(minutes=45))
    due, why = next((d, w) for sp, d, w in due_jobs(ctx) if sp.name == "research")
    assert due, f"the crash was today's first failure, a retry is due; scheduler says: {why}"


def test_ok_tick_and_job_locks_block_overlap_and_recover_from_dead_owner(ctx, monkeypatch):
    fast_jobs(monkeypatch)
    lock = FileLock(ctx.settings.locks_dir / "tick.lock")
    assert lock.acquire()
    try:
        assert tick(ctx)["status"] == "skipped"
    finally:
        lock.release()
    job_lock = FileLock(ctx.settings.locks_dir / "job_plan.lock")
    assert job_lock.acquire()
    try:
        assert run_job(ctx, "plan")["reason"] == "already running"
    finally:
        job_lock.release()
    # a crashed owner: a PID that no longer exists
    proc = subprocess.Popen(["cmd", "/c", "exit"], stdout=subprocess.DEVNULL) if os.name == "nt" else subprocess.Popen(["true"])
    proc.wait()
    (ctx.settings.locks_dir / "tick.lock").write_text(f"{proc.pid} 2026", encoding="utf-8")
    (ctx.settings.locks_dir / "job_plan.lock").write_text(f"{proc.pid} 2026", encoding="utf-8")
    assert tick(ctx)["status"] == "ok"
    assert not (ctx.settings.locks_dir / "tick.lock").exists()


def test_ok_stale_or_unreadable_lock_recovers_by_age(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text("", encoding="utf-8")
    assert not FileLock(path).acquire()  # no owner on record: the age limit decides
    old = datetime.now().timestamp() - 7300
    os.utime(path, (old, old))
    lock = FileLock(path)
    assert lock.acquire()
    lock.release()


def test_ok_only_one_of_many_threads_gets_a_lock(tmp_path):
    path = tmp_path / "x.lock"
    results = []
    barrier = threading.Barrier(8)

    def grab():
        barrier.wait()
        results.append(FileLock(path).acquire())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1


def test_ok_maintenance_window_stops_tick_and_jobs_and_expires(ctx, monkeypatch):
    fast_jobs(monkeypatch)
    maintenance.start(ctx.settings.data_dir, 1, "upgrade")
    assert tick(ctx)["status"] == "skipped" and read_heartbeat(ctx.settings)["state"] == "maintenance"
    assert run_job(ctx, "plan")["status"] == "skipped"
    assert run_job(ctx, "plan", allow_during_maintenance=True)["status"] == "success"
    assert client_for(ctx).post("/jobs/plan/run", headers=ORIGIN, follow_redirects=False).status_code == 303
    path = ctx.settings.data_dir / maintenance.FILENAME
    path.write_text(json.dumps({"until": "2020-01-01T00:00:00+00:00", "reason": "old"}), encoding="utf-8")
    assert maintenance.active(ctx.settings.data_dir) is None and not path.exists()
    path.write_text("not json", encoding="utf-8")
    assert maintenance.active(ctx.settings.data_dir) is None
    assert tick(ctx)["status"] == "ok"


def test_ok_stopped_automation_runs_only_health_and_housekeeping(ctx, monkeypatch):
    fast_jobs(monkeypatch)
    with ctx.db.session() as s:
        am.change_state(s, "stop", "owner")
    ran = {r["job"]: r["status"] for r in tick(ctx)["ran"]}
    assert ran["monitor"] == "success" and ran["db_maintenance"] == "success" and ran["publish"] == "skipped" and ran["research"] == "skipped"


def test_ok_job_over_budget_warns(ctx, monkeypatch):
    fast_jobs(monkeypatch)
    ctx.settings.schedule["job_budget_minutes"] = -1
    run_job(ctx, "plan")
    assert alerts(ctx, "Job 'plan' took%")


def test_ok_waking_after_days_runs_each_job_once_with_no_burst(ctx, clock):
    """A real tick: every job with its real work, asleep for three days and a bit, then awake."""
    clock(datetime(2026, 10, 27, 14, 0))
    first = tick(ctx, trigger="scheduler")
    assert sorted(r["job"] for r in first["ran"]) == sorted(JOBS) and all(r["status"] == "success" for r in first["ran"]), first
    clock.forward(timedelta(days=3, hours=5))
    second = tick(ctx, trigger="scheduler")
    jobs = [r["job"] for r in second["ran"]]
    assert len(jobs) == len(set(jobs)) and "weekly_report" not in jobs and "monitor" in jobs and "plan" in jobs
    assert all(r["status"] == "success" for r in second["ran"]), second
    third = tick(ctx, trigger="scheduler")
    assert third["ran"] == []
    counts = job_runs(ctx)
    assert max(counts.values()) == 2 and counts["weekly_report"] == 1


def test_ok_waking_publishes_one_post_not_the_backlog(make_ctx, monkeypatch):
    fake = FakeLI()
    ctx = li_ctx(make_ctx, fake)
    fast_jobs(monkeypatch, keep=("publish",))
    for i in range(4):
        add_post(ctx, slot_delta=timedelta(hours=-(i + 1)))
    tick(ctx)
    assert fake.post_calls == 1, "max_posts_per_run applies after a sleep too"


def test_ok_failing_job_does_not_stop_the_rest_of_the_tick(ctx, monkeypatch):
    fast_jobs(monkeypatch)
    monkeypatch.setitem(runner.JOBS, "db_maintenance", lambda ctx, job="x": (_ for _ in ()).throw(RuntimeError("disk")))
    ran = {r["job"]: r["status"] for r in tick(ctx)["ran"]}
    assert ran["db_maintenance"] == "failed" and ran["monitor"] == "success" and len(ran) == 13


def test_defect_next_run_ignores_retry_backoff_and_giving_up(ctx):
    """next_run: "read from the same rules as is_due". While a failed daily job waits for its backoff, or has given up
    until tomorrow, the home page and /command-center say "due at the next check-in"."""
    tz = ZoneInfo("UTC")
    spec = JobSpec("research", None, datetime(2026, 1, 1, 7, 0).time(), "")
    now = datetime(2026, 10, 27, 10, 0)
    gave_up = JobState(job="research", last_attempt_at=now - timedelta(hours=1), last_status="failed", consecutive_failures=3, enabled=True)
    assert is_due(spec, gave_up, now, tz)[0] is False
    assert next_run(spec, gave_up, now, tz) == datetime(2026, 10, 28, 7, 0), "it runs tomorrow, not at the next check-in"


# ------------------------------------------------------------------ DST and clock changes
def _simulate(specs, tz, start, end, tick_minutes=15):
    """Hourly-accurate walk of the scheduler's own is_due over simulated time. Every due job succeeds at once."""
    states = {sp.name: _state(last_attempt_at=start, last_success_at=start) for sp in specs}  # all up to date at the start
    runs = {sp.name: [] for sp in specs}
    now = start
    while now <= end:
        for sp in specs:
            due, _ = is_due(sp, states.get(sp.name), now, tz, tick_minutes)
            if due:
                states[sp.name] = _state(last_attempt_at=now, last_success_at=now)
                runs[sp.name].append(now)
        now += timedelta(minutes=tick_minutes)
    return runs


def test_ok_dst_1_nov_2026_each_daily_job_runs_once_per_local_day(ctx):
    tz = ZoneInfo("America/New_York")
    specs = load_job_specs(ctx.settings)
    runs = _simulate(specs, tz, datetime(2026, 10, 30, 4, 0), datetime(2026, 11, 3, 4, 45))
    for sp in specs:
        if sp.every_minutes:
            hours = (datetime(2026, 11, 3, 4, 45) - datetime(2026, 10, 30, 4, 0)).total_seconds() / 3600
            assert len(runs[sp.name]) <= hours * 60 / sp.every_minutes + 1, sp.name
            continue
        local_days = [r.replace(tzinfo=timezone.utc).astimezone(tz).date() for r in runs[sp.name]]
        assert len(local_days) == len(set(local_days)), f"{sp.name} ran twice on one day: {runs[sp.name]}"
        if not sp.weekdays:
            assert date(2026, 11, 1) in local_days and date(2026, 11, 2) in local_days, sp.name
    # 06:15 local is 10:15 UTC before the change and 11:15 UTC after it
    backups = [r for r in runs["db_maintenance"]]
    assert datetime(2026, 10, 31, 10, 15) in backups and datetime(2026, 11, 1, 11, 15) in backups
    weekly = [r for r in runs["weekly_report"] if r >= datetime(2026, 11, 2)]
    assert weekly == [datetime(2026, 11, 2, 13, 0)]


def test_ok_dst_ambiguous_hour_slot_runs_once(ctx):
    tz = ZoneInfo("America/New_York")
    spec = JobSpec("x", None, datetime(2026, 1, 1, 1, 30).time(), "")
    runs = _simulate([spec], tz, datetime(2026, 11, 1, 3, 0), datetime(2026, 11, 2, 3, 0))
    assert runs["x"] == [datetime(2026, 11, 1, 5, 30)]  # 01:30 EDT, and not again at 01:30 EST


def test_ok_next_run_and_post_slots_follow_dst(ctx):
    tz = ZoneInfo("America/New_York")
    spec = next(sp for sp in load_job_specs(ctx.settings) if sp.name == "db_maintenance")
    done = _state(last_attempt_at=datetime(2026, 10, 31, 10, 15), last_success_at=datetime(2026, 10, 31, 10, 15))
    assert next_run(spec, done, datetime(2026, 10, 31, 12, 0), tz) == datetime(2026, 11, 1, 11, 15)
    slots = candidate_slots(ctx.settings, datetime(2026, 10, 28, 0, 0), 14)
    assert datetime(2026, 10, 29, 13, 30) in slots and datetime(2026, 11, 3, 14, 30) in slots  # 09:30 ET both sides


def test_ok_india_time_zone_has_each_job_once_a_day(ctx):
    tz = ZoneInfo("Asia/Kolkata")
    specs = [sp for sp in load_job_specs(ctx.settings) if sp.daily_at and not sp.weekdays]
    runs = _simulate(specs, tz, datetime(2026, 10, 30, 0, 0), datetime(2026, 11, 3, 0, 0))
    for sp in specs:
        days = [r.replace(tzinfo=timezone.utc).astimezone(tz).date() for r in runs[sp.name]]
        assert len(days) == len(set(days)) and len(days) >= 4


def test_ok_clock_moved_forward_gives_one_catch_up(ctx, monkeypatch, clock):
    fast_jobs(monkeypatch)
    clock(datetime(2026, 10, 27, 14, 0))
    tick(ctx)
    clock.forward(timedelta(days=30))
    assert sorted(r["job"] for r in tick(ctx)["ran"]) == sorted(JOBS)  # a weekly slot passed as well
    assert tick(ctx)["ran"] == []


def test_defect_clock_set_back_stops_all_jobs_silently(ctx, monkeypatch, clock):
    """A wrong clock (dead battery, bad sync) corrected backwards leaves last_attempt in the future. is_due sees a
    negative elapsed time: no job, the monitor included, runs until the clock catches up. Ticks keep running, so the
    heartbeat stays fresh and nothing alerts."""
    fast_jobs(monkeypatch)
    clock(datetime(2026, 12, 1, 12, 0))
    tick(ctx)
    clock(datetime(2026, 10, 27, 14, 0))
    ran = [r["job"] for r in tick(ctx)["ran"]]
    assert "monitor" in ran and "publish" in ran, f"five weeks without any job and no alert; this tick ran only {ran}"


# ------------------------------------------------------------------ ticks stopped: watchdog, monitor, dashboard
def test_ok_watchdog_reports_stopped_ticks(ctx):
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    data = ctx.settings.data_dir
    beat = data / "heartbeat.json"
    assert "No scheduler heartbeat" in watchdog.check(data, now)[0]
    beat.write_text(json.dumps({"at": "2026-10-30T11:45:00Z"}), encoding="utf-8")
    assert watchdog.check(data, now) == []
    beat.write_text(json.dumps({"at": "2026-10-30T11:00:00Z"}), encoding="utf-8")
    assert "stopped" in watchdog.check(data, now)[0] and "60 min" in watchdog.check(data, now)[0]
    beat.write_text("{not json", encoding="utf-8")
    assert "unreadable" in watchdog.check(data, now)[0]
    beat.write_text(json.dumps({"at": "2026-10-30T09:00:00Z", "state": "maintenance"}), encoding="utf-8")
    assert "stopped" in watchdog.check(data, now)[0]  # an old maintenance beat does not hide it once the window ended
    maintenance.start(data, 1, "x")
    assert watchdog.check(data, now) == []


def test_ok_monitor_run_by_hand_alerts_on_stale_heartbeat_and_clears_when_ticks_return(ctx, monkeypatch, clock):
    heartbeat_at(ctx, timedelta(hours=8))
    assert run_job(ctx, "monitor")["status"] == "success"
    found = [a for a in alerts(ctx, "Scheduler heartbeat fresh%") if not a.acknowledged]
    assert len(found) == 1 and "min ago" in found[0].message
    assert 'state-chip warn' in client_for(ctx).get("/").text
    text = text_of(client_for(ctx).get("/command-center").text)
    assert "Scheduler heartbeat fresh" in text
    fast_jobs(monkeypatch, keep=("monitor",))
    clock.forward(timedelta(minutes=31))
    tick(ctx)  # ticks return: the heartbeat is fresh and the monitor closes its own alert
    assert all(a.resolved_at for a in alerts(ctx, "Scheduler heartbeat fresh%"))


def test_ok_dashboard_shows_off_duty_when_ticks_stopped(ctx):
    heartbeat_at(ctx, timedelta(hours=8))
    assert employee_duty(ctx.settings, "running")[0] == "Off duty"
    html = client_for(ctx).get("/").text
    assert "off duty" in html.lower() and "the PC may be asleep or the scheduler stopped" in html
    assert next(r for r in health_strip_rows(ctx) if r[0] == "Scheduler")[1] == "bad"
    with ctx.db.session() as s:
        board = system_status(ctx, s, [], (False, "x"), utcnow())
    assert next(r for r in board if r["name"] == "Scheduled jobs")["status"] == "FAILED"
    assert "last check-in 8 h ago" in client_for(ctx).get("/manager").text


def test_defect_laptop_sleep_mid_tick_shows_working_now_forever(ctx):
    """The tick writes state=running first. Sleep or a crash before the idle beat leaves that state, and employee_duty
    trusts it without looking at the age. Docstring: "Never claims work that is not happening"."""
    heartbeat_at(ctx, timedelta(days=2), state="running")
    duty = employee_duty(ctx.settings, "running")
    assert duty[0] == "Off duty", f"two days after its last check-in the dashboard says {duty[0]!r}: {duty[3]!r}"


def test_defect_ticks_stopped_after_maintenance_window_show_as_healthy_everywhere_but_the_watchdog(ctx):
    """The last beat before the outage was written during a maintenance window, which ended days ago. Doctor passes,
    the health strip is green and the board says PAUSED, each trusting the old state and ignoring the expired window."""
    heartbeat_at(ctx, timedelta(days=3), state="maintenance")
    assert maintenance.active(ctx.settings.data_dir) is None
    check = next(c for c in run_checks(ctx) if c["name"] == "Scheduler heartbeat fresh")
    strip = next(r for r in health_strip_rows(ctx) if r[0] == "Scheduler")
    with ctx.db.session() as s:
        board = next(r for r in system_status(ctx, s, [], (False, "x"), utcnow()) if r["name"] == "Scheduled jobs")
    assert (check["ok"], strip[1], board["status"]) == (False, "bad", "FAILED")


def test_defect_api_health_says_ok_while_ticks_are_stopped(ctx):
    heartbeat_at(ctx, timedelta(days=3))
    body = client_for(ctx).get("/api/health").json()
    stale = next(r for r in body["integrations"] if r["name"] == "Scheduler heartbeat")
    assert stale["state"] == "STALE"
    assert body["ok"] is False, "a health check must not say ok while nothing has run for three days"


def test_ok_outage_record_is_quiet_and_only_the_latest_stays_open(ctx, monkeypatch, clock):
    fast_jobs(monkeypatch)
    clock(datetime(2026, 10, 27, 14, 0))
    tick(ctx)
    clock.forward(timedelta(minutes=30))
    tick(ctx)  # an ordinary check-in is no outage
    assert not alerts(ctx, "No scheduled check-in%")
    for hours in (14, 9):
        clock.forward(timedelta(hours=hours))
        tick(ctx)
    found = alerts(ctx, "No scheduled check-in%")
    assert [a.level for a in found] == ["info", "info"] and "9.0 h" in found[1].message
    assert found[0].resolved_at and not found[1].resolved_at


def test_defect_outage_leaves_no_alert_once_ticks_resume(ctx, monkeypatch, clock):
    """Monitor and doctor only run inside a tick, which refreshes the heartbeat first, so the stale check can never
    fire on schedule. After a night's outage (lockout, sleep) nothing on record says work stopped. By design the live
    "Off duty" status and the watchdog toast cover the outage itself; once ticks are back the owner has no trace."""
    fast_jobs(monkeypatch, keep=("monitor",))
    clock(datetime(2026, 10, 27, 14, 0))
    tick(ctx)
    clock.forward(timedelta(hours=14))
    tick(ctx)
    words = ("tick", "scheduler", "heartbeat", "stopped", "gap", "asleep")
    assert any(any(w in a.message.lower() for w in words) for a in alerts(ctx)), "14 hours without a tick left no record"


# ------------------------------------------------------------------ alerts
def test_ok_alert_dismissal_and_episode_rules(ctx):
    with ctx.db.session() as s:
        assert alert_once(s, "warning", "monitor", "first", ctx.settings, key="k:1")
    with ctx.db.session() as s:
        assert not alert_once(s, "warning", "monitor", "second", ctx.settings, key="k:1")
    row = alerts(ctx)[0]
    assert row.message == "second"  # one alert per condition, latest details
    with ctx.db.session() as s:
        s.get(Alert, row.id).acknowledged = True
    with ctx.db.session() as s:
        assert not alert_once(s, "warning", "monitor", "third", ctx.settings, key="k:1")


def test_ok_dismissed_alert_can_return_24_hours_after_the_dismissal(ctx, clock):
    with ctx.db.session() as s:
        alert_once(s, "warning", "monitor", "old", ctx.settings, key="k:3")
    clock.forward(timedelta(hours=23))
    with ctx.db.session() as s:
        s.execute(select(Alert)).scalars().one().acknowledged = True
    clock.forward(timedelta(hours=25))
    with ctx.db.session() as s:
        assert alert_once(s, "warning", "monitor", "again", ctx.settings, key="k:3")


def test_defect_dismissed_alert_returns_before_24_hours(ctx, clock):
    """OPERATIONS.md: "Dismissing one keeps it quiet for 24 hours". It counts from when the alert was created, so an
    alert dismissed the morning after comes straight back."""
    with ctx.db.session() as s:
        alert_once(s, "warning", "monitor", "old", ctx.settings, key="k:2")
    clock.forward(timedelta(hours=23))
    with ctx.db.session() as s:
        s.execute(select(Alert)).scalars().one().acknowledged = True
    clock.forward(timedelta(hours=2))
    with ctx.db.session() as s:
        assert not alert_once(s, "warning", "monitor", "again", ctx.settings, key="k:2"), "dismissed 2 hours ago"


# ================================================================================================= Database integrity
def _db_path(ctx) -> Path:
    return Path(ctx.db.engine.url.database)


def _garbage(path: Path) -> Path:
    path.write_bytes(b"this is not a database" * 500)
    return path


def test_ok_db_maintenance_backs_up_verifies_and_records(ctx):
    with ctx.db.session() as s:
        s.add(Alert(level="info", source="uat", message="row"))
    assert run_job(ctx, "db_maintenance")["status"] == "success"
    files = list(ctx.settings.backups_dir.glob("aime-auto-*.db"))
    assert len(files) == 1
    check = backup.verify_backup(files[0])
    assert check["ok"] and check["counts"]["alerts"] >= 1
    assert next(c for c in run_checks(ctx) if c["name"] == "Recent verified backup")["ok"]


def test_ok_retention_keeps_14_auto_and_never_manual(ctx):
    d = ctx.settings.backups_dir
    d.mkdir(parents=True, exist_ok=True)
    for i in range(20):
        (d / f"aime-auto-202601{i + 1:02d}-000000.db").write_bytes(b"x")
    (d / "aime-pre-deploy-20260101-000000.db").write_bytes(b"x")
    (d / "aime-manual-20260101-000000.db").write_bytes(b"x")
    assert backup.prune_backups(d, keep=14) == 6
    assert len(list(d.glob("aime-auto-*.db"))) == 14
    assert (d / "aime-pre-deploy-20260101-000000.db").exists() and (d / "aime-manual-20260101-000000.db").exists()
    assert backup.prune_manual_backups(d, 0) == []


def test_defect_retention_deletes_the_only_good_backup(ctx, monkeypatch):
    """job_db_maintenance prunes before it acts on verification, and prune_backups counts every file. After 14 bad
    backups (a failing disk, a corrupt live database) the last good copy is the one deleted."""
    d = ctx.settings.backups_dir
    d.mkdir(parents=True, exist_ok=True)
    good = d / "aime-auto-20260101-000000.db"
    shutil.copy2(backup.backup_sqlite_file(_db_path(ctx), d, label="seed"), good)
    assert backup.verify_backup(good)["ok"]
    for i in range(13):
        _garbage(d / f"aime-auto-202601{i + 2:02d}-000000.db")
    monkeypatch.setattr(workflows, "verify_backup", lambda p: {"ok": False, "integrity": "disk error", "tables": 0, "rows": 0})
    res = run_job(ctx, "db_maintenance")
    assert res["status"] in ("failed", "partial")  # partial: the off-machine step, not configured here, counts as done
    assert good.exists(), "the only good backup was pruned to make room for a backup that failed verification"


def test_ok_prune_keeps_the_last_good_backup_when_every_newer_one_is_damaged(ctx):
    d = ctx.settings.backups_dir
    d.mkdir(parents=True, exist_ok=True)
    good = d / "aime-auto-20260101-000000.db"
    shutil.copy2(backup.backup_sqlite_file(_db_path(ctx), d, label="seed"), good)
    _garbage(d / "aime-auto-20251231-000000.db")  # older than the good copy: goes
    for i in range(3):
        _garbage(d / f"aime-auto-202601{i + 2:02d}-000000.db")
    assert backup.prune_backups(d, keep=2) == 2
    assert good.exists() and len(list(d.glob("aime-auto-*.db"))) == 3


def test_ok_verify_and_drill_reject_bad_backups(ctx):
    d = ctx.settings.backups_dir
    d.mkdir(parents=True, exist_ok=True)
    good = backup.backup_sqlite_file(_db_path(ctx), d, label="manual")
    data = good.read_bytes()
    truncated = d / "aime-trunc-1.db"
    truncated.write_bytes(data[: len(data) // 2])
    empty = d / "aime-empty-1.db"
    empty.write_bytes(b"")
    garbage = _garbage(d / "aime-garbage-1.db")
    for bad in (truncated, empty, garbage, d / "missing.db"):
        assert not backup.verify_backup(bad)["ok"], bad.name
    assert not backup.restore_drill(ctx.settings, garbage)["ok"]
    assert not backup.restore_drill(ctx.settings, truncated)["ok"]
    assert backup.restore_drill(ctx.settings, good)["ok"]


def test_ok_restore_drill_latest_is_truthful_and_never_touches_live(ctx):
    before = _db_path(ctx).read_bytes()
    assert backup.restore_drill(ctx.settings) == {"ok": False, "source": None, "detail": "no backup found"}
    d = ctx.settings.backups_dir
    d.mkdir(parents=True, exist_ok=True)
    good = backup.backup_sqlite_file(_db_path(ctx), d, label="manual")
    assert backup.restore_drill(ctx.settings)["ok"]
    bad = _garbage(d / "aime-zzz-1.db")
    os.utime(bad, (good.stat().st_mtime + 10, good.stat().st_mtime + 10))
    assert not backup.restore_drill(ctx.settings)["ok"]  # the latest copy is damaged: it says so
    assert _db_path(ctx).read_bytes() == before
    with pytest.raises(backup.BackupError):
        backup.restore_drill(ctx.settings, good, extract_to=ctx.settings.data_dir / "x")


def _encrypted(make_ctx, tmp_path, **kw):
    ctx = make_ctx(backup_offsite_dir=str(tmp_path / "offsite"), backup_passphrase="uat-passphrase-long", **kw)
    return ctx, tmp_path / "offsite"


def test_ok_offsite_copy_is_encrypted_and_restores(make_ctx, tmp_path):
    ctx, folder = _encrypted(make_ctx, tmp_path)
    with ctx.db.session() as s:
        s.add(Alert(level="info", source="uat", message="SECRET-ROW-MARKER"))
    assert run_job(ctx, "db_maintenance")["status"] == "success"
    copies = list(folder.glob("aime-offsite-*.aimebak"))
    assert len(copies) == 1 and b"SECRET-ROW-MARKER" not in copies[0].read_bytes() and copies[0].read_bytes().startswith(b"AIMEBAK1")
    assert not list(folder.glob("*.partial"))
    out = backup.restore_drill(ctx.settings, copies[0], extract_to=tmp_path / "restored")
    assert out["ok"] and (tmp_path / "restored" / "aime.db").exists() and (tmp_path / "restored" / "manifest.json").exists()
    names = {p.name for p in (tmp_path / "restored").rglob("*")}
    assert ".env" not in names
    con = sqlite3.connect(tmp_path / "restored" / "aime.db")
    assert con.execute("select count(*) from alerts where message='SECRET-ROW-MARKER'").fetchone()[0] == 1
    con.close()


def test_ok_offsite_damage_wrong_passphrase_and_tampering_are_rejected(make_ctx, tmp_path, monkeypatch):
    ctx, folder = _encrypted(make_ctx, tmp_path)
    assert backup.write_offsite(ctx.settings, _db_path(ctx))["status"] == "ok"
    copy = next(folder.glob("*.aimebak"))
    blob = bytearray(copy.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    damaged = tmp_path / "damaged.aimebak"
    damaged.write_bytes(bytes(blob))
    with pytest.raises(backup.BackupError):
        backup.restore_drill(ctx.settings, damaged)
    ctx.settings.backup_passphrase = "another-passphrase"
    with pytest.raises(backup.BackupError):
        backup.restore_drill(ctx.settings, copy)
    ctx.settings.backup_passphrase = "uat-passphrase-long"
    data = backup.decrypt_bytes(copy.read_bytes(), ctx.settings.backup_passphrase)  # a file changed inside the bundle
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for n in source.namelist():
            zf.writestr(n, b"tampered" if n.startswith("config/") else source.read(n))
    tampered = tmp_path / "tampered.aimebak"
    tampered.write_bytes(backup.encrypt_bytes(out.getvalue(), ctx.settings.backup_passphrase))
    res = backup.restore_drill(ctx.settings, tampered)
    assert not res["ok"] and "checksum mismatch" in res["detail"]


def test_defect_restore_drill_crashes_on_a_corrupt_zip_bundle(ctx, tmp_path, capsys):
    """`restore-drill --source x.zip` is documented for bundles. A damaged zip raises zipfile.BadZipFile, which
    cmd_restore_drill does not catch: a traceback instead of "Restore drill FAILED"."""
    from aime.cli import cmd_restore_drill

    bad = tmp_path / "damaged.zip"
    bad.write_bytes(b"PK damaged bundle")

    class Args:
        source, extract_to = str(bad), None

    assert cmd_restore_drill(Args, ctx) == 1
    assert "FAILED" in capsys.readouterr().out


def test_ok_offsite_prune_keeps_30_and_foreign_files_and_failure_is_reported(make_ctx, tmp_path):
    ctx, folder = _encrypted(make_ctx, tmp_path)
    folder.mkdir(parents=True)
    for i in range(35):
        (folder / f"aime-offsite-202601{i % 28 + 1:02d}-{i:06d}.aimebak").write_bytes(b"x")
    (folder / "family-photos.zip").write_bytes(b"keep me")
    assert backup.write_offsite(ctx.settings, _db_path(ctx))["pruned"] == 6
    assert len(list(folder.glob("aime-offsite-*.aimebak"))) == 30 and (folder / "family-photos.zip").exists()
    ctx2 = make_ctx(backup_offsite_dir=str(tmp_path / "afile"), backup_passphrase="uat-passphrase-long")
    (tmp_path / "afile").write_text("not a folder", encoding="utf-8")
    assert run_job(ctx2, "db_maintenance")["status"] == "partial"  # the local backup stands, the copy failed
    assert backup.offsite_status(make_ctx(backup_offsite_dir="x", backup_passphrase="short").settings)[0] is False


def test_ok_runbook_restore_brings_data_back(make_ctx, tmp_path):
    ctx = make_ctx(data_dir=tmp_path / "live")
    with ctx.db.session() as s:
        s.add(Alert(level="info", source="uat", message="kept"))
    bak = backup.backup_sqlite_file(_db_path(ctx), ctx.settings.backups_dir, label="manual")
    with ctx.db.session() as s:
        s.add(Alert(level="info", source="uat", message="after the backup"))
    ctx.db.dispose()
    live = _db_path(ctx)
    for side in ("aime.db-wal", "aime.db-shm"):
        (live.parent / side).unlink(missing_ok=True)
    shutil.copy2(bak, live)
    restored = make_ctx(data_dir=tmp_path / "live")
    assert [a.message for a in alerts(restored)] == ["kept"]
    assert next(c for c in run_checks(restored) if c["name"] == "SQLite integrity")["ok"]


# ------------------------------------------------------------------ migrations
OLD_SCHEMA = [("content_items", "reconciled_via"), ("alerts", "key"), ("alerts", "resolved_at"), ("agent_runs", "llm_tokens"),
              ("agent_runs", "llm_cost_usd"), ("followups", "purpose"), ("job_state", "enabled")]


def _make_old(path: Path, drop=OLD_SCHEMA):
    con = sqlite3.connect(path)
    for table, col in drop:
        con.execute(f'ALTER TABLE "{table}" DROP COLUMN "{col}"')
    con.commit()
    con.close()


def test_ok_migration_on_old_schema_keeps_data_and_backs_up_first(make_ctx, tmp_path):
    ctx = make_ctx(data_dir=tmp_path / "old")
    with ctx.db.session() as s:
        s.add(Company(name="Keep", normalized_name="keep", domain="keep.example", source="manual"))
        s.add(Alert(level="info", source="uat", message="old alert"))
        s.add(AgentRun(agent="crm", run_id="r", status="success", started_at=utcnow()))
        s.add(JobState(job="plan", consecutive_failures=0, enabled=True, last_success_at=utcnow()))
    ctx.db.dispose()
    _make_old(_db_path(ctx))
    again = make_ctx(data_dir=tmp_path / "old")
    insp = inspect(again.db.engine)
    for table, col in OLD_SCHEMA:
        assert col in {c["name"] for c in insp.get_columns(table)}, col
    with again.db.session() as s:
        assert s.execute(select(Company)).scalars().one().name == "Keep"
        assert s.execute(select(AgentRun)).scalars().one().llm_tokens == 0
        assert s.get(JobState, "plan").enabled is True
    backups = list(again.settings.backups_dir.glob("aime-pre-migration-*.db"))
    assert len(backups) == 1 and backup.verify_backup(backups[0])["ok"]
    client = client_for(again)
    assert client.get("/").status_code == 200 and client.get("/agents").status_code == 200
    assert run_job(again, "monitor")["status"] == "success"
    next_start = make_ctx(data_dir=tmp_path / "old")  # idempotent: no second backup
    assert len(list(next_start.settings.backups_dir.glob("aime-pre-migration-*.db"))) == 1


def test_defect_migration_takes_no_backup_when_only_companies_table_is_empty(make_ctx, tmp_path):
    """migrations.py: "A backup is taken before any ALTER runs against a database that already holds data." _has_data
    only looks at companies: a database with posts, tokens and history but no company gets no backup."""
    ctx = make_ctx(data_dir=tmp_path / "old")
    add_post(ctx, status="published", external_id="urn:li:share:1")
    ctx.db.dispose()
    _make_old(_db_path(ctx), [("content_items", "reconciled_via")])
    again = make_ctx(data_dir=tmp_path / "old")
    assert list(again.settings.backups_dir.glob("aime-pre-migration-*.db")), "a database holding posts was altered without a backup"


# ------------------------------------------------------------------ doctor
def _published_twice(ctx):
    cid = add_post(ctx, status="published", external_id="urn:li:share:1")
    with ctx.db.session() as s:
        for att in ("a", "b"):
            s.add(PublishAttempt(attempt_id=att, content_id=cid, outcome="success"))


def _unapproved_published(ctx):
    cid = add_post(ctx, status="published", external_id="urn:li:share:2")
    with ctx.db.session() as s:
        s.get(ContentItem, cid).approved_at = None


def _stuck_claim(ctx):
    add_post(ctx, status="publishing", publish_started_at=utcnow() - timedelta(minutes=30))


def _email_without_approval(ctx):
    fid = seed_email(ctx)
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        f.status, f.sent_at, f.approved_at = "sent", utcnow(), None


def _email_after_opt_out(ctx):
    fid = seed_email(ctx)
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        f.status, f.sent_at, f.approved_at, f.approved_by = "sent", utcnow(), utcnow(), "o"
        f.contact.consent_status, f.contact.consent_at = "opted_out", utcnow() - timedelta(days=1)


def _plain_token(ctx):
    add_token(ctx)


def _backup_stale(ctx):
    with ctx.db.session() as s:
        kv_set(s, "backup.last_verified", {"file": "x.db", "at": (utcnow() - timedelta(hours=31)).isoformat(), "tables": 1, "rows": 1})


def _foreign_key_broken(ctx):
    con = sqlite3.connect(_db_path(ctx))
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO meetings(lead_id, invitee_email, title, starts_at, ends_at, source, status, is_simulated, created_at, updated_at) "
                "VALUES (999, 'x@y.z', 't', '2026-01-01', '2026-01-01', 'manual', 'scheduled', 0, '2026-01-01', '2026-01-01')")
    con.commit()
    con.close()


def _no_heartbeat_fresh(ctx):
    heartbeat_at(ctx, timedelta(hours=2))


def _scraped_contact(ctx):
    with ctx.db.session() as s:
        s.add(Contact(email="x@y.example", source="scraped"))


@pytest.mark.parametrize("setup,check", [
    (lambda c: add_post(c, status="published"), "Every published post has a LinkedIn id"),
    (_unapproved_published, "Nothing published without approval"),
    (_published_twice, "No post published twice"),
    (_stuck_claim, "No stuck publishing claims"),
    (_email_without_approval, "No email sent without approval"),
    (_email_after_opt_out, "Emails only to consenting contacts or approved B2B outreach"),
    (_plain_token, "OAuth tokens encrypted at rest"),
    (_backup_stale, "Recent verified backup"),
    (_foreign_key_broken, "SQLite integrity"),
    (_no_heartbeat_fresh, "Scheduler heartbeat fresh"),
    (_scraped_contact, "Contacts only from approved sources"),
    (lambda c: add_post(c, status="uncertain"), "No uncertain publications/sends awaiting review"),
])
def test_ok_doctor_fails_each_known_bad_state(ctx, setup, check):
    setup(ctx)
    row = next(c for c in run_checks(ctx) if c["name"] == check)
    assert not row["ok"], row


def test_ok_doctor_is_clean_on_a_healthy_database_and_exit_code_follows_errors(ctx, capsys):
    from aime.cli import cmd_doctor
    run_job(ctx, "db_maintenance")
    write_heartbeat(ctx.settings, {"state": "idle"})
    errors = [c for c in run_checks(ctx) if not c["ok"] and c["severity"] == "error"]
    assert errors == [] and cmd_doctor(None, ctx) == 0
    add_post(ctx, status="published")
    assert cmd_doctor(None, ctx) == 1


def test_ok_doctor_catches_sample_data_in_production(make_ctx):
    ctx = make_ctx(data_role="production")
    with ctx.db.session() as s:
        s.add(Company(name="S", normalized_name="s", domain="s.example", source="manual", is_sample=True))
    assert not next(c for c in run_checks(ctx) if c["name"] == "No sample data in production")["ok"]


def test_defect_doctor_passes_recent_backup_when_the_backup_file_is_gone(ctx):
    """The check reads the record of the last verification, never the folder. Delete the backups (or lose the folder)
    and doctor and the dashboard keep reporting a good recent backup."""
    assert run_job(ctx, "db_maintenance")["status"] == "success"
    for f in ctx.settings.backups_dir.glob("aime-*.db"):
        f.unlink()
    row = next(c for c in run_checks(ctx) if c["name"] == "Recent verified backup")
    assert not row["ok"], f"no backup file exists, doctor says: {row['detail']}"


def test_defect_doctor_crashes_instead_of_reporting_a_corrupt_database(ctx):
    """On a damaged file run_checks raises out of the first query, so `doctor` prints a traceback and the monitor job
    fails without naming the problem. It must report "SQLite integrity" as failed."""
    with ctx.db.session() as s:
        for i in range(400):
            s.add(Alert(level="info", source="uat", message=f"row {i}" * 10))
    ctx.db.dispose()
    path = _db_path(ctx)
    (path.parent / "aime.db-wal").unlink(missing_ok=True)
    (path.parent / "aime.db-shm").unlink(missing_ok=True)
    data = bytearray(path.read_bytes())
    for offset in range(4096, len(data), 4096):  # every page after the header
        data[offset:offset + 200] = b"\xab" * 200
    path.write_bytes(bytes(data))
    checks = run_checks(ctx)  # raises today
    assert not next(c for c in checks if c["name"] == "SQLite integrity")["ok"]


# ------------------------------------------------------------------ database is locked
def test_ok_writes_from_many_threads_all_land(ctx):
    errors = []

    def writer(n):
        try:
            for i in range(10):
                with ctx.db.session() as s:
                    s.add(Alert(level="info", source=f"t{n}", message=f"{i}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and len(alerts(ctx)) == 60


def test_ok_job_waits_out_a_short_write_lock(ctx):
    con = sqlite3.connect(_db_path(ctx), check_same_thread=False, timeout=1)
    con.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(1.5, lambda: (con.commit(), con.close()))
    timer.start()
    res = run_job(ctx, "monitor")
    timer.join()
    assert res["status"] == "success"


def test_ok_job_result_survives_two_locked_writes_and_third_failure_does_not_stop_the_tick(ctx, monkeypatch):
    fast_jobs(monkeypatch)
    monkeypatch.setattr(runner._time, "sleep", lambda _s: None)
    real, fails = runner._record, {"n": 0}

    def flaky(*a, **k):
        if fails["n"] < 2:
            fails["n"] += 1
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        return real(*a, **k)

    monkeypatch.setattr(runner, "_record", flaky)
    assert run_job(ctx, "plan")["status"] == "success"
    with ctx.db.session() as s:
        assert s.get(JobState, "plan").last_status == "success"


def _short_busy_timeout(ctx):
    """Keep the 30 s busy timeout out of the test: the same lock error, sooner."""
    event.listen(ctx.db.engine, "connect", lambda con, _r: con.execute("PRAGMA busy_timeout=150"))
    ctx.db.engine.dispose()


def test_ok_pages_read_fine_while_a_writer_holds_the_lock(ctx):
    _short_busy_timeout(ctx)
    client = client_for(ctx)
    con = sqlite3.connect(_db_path(ctx), check_same_thread=False)
    con.execute("BEGIN IMMEDIATE")
    try:
        for path in ("/", "/content", "/agents", "/reports", "/pipeline"):
            assert client.get(path).status_code == 200, path
    finally:
        con.rollback()
        con.close()


def test_defect_approval_while_database_busy_is_an_error_500(ctx):
    """The app has no handler for OperationalError: when a long job holds the write lock past the 30 s timeout, the
    owner's click gets a bare "Internal Server Error" instead of "busy, try again"."""
    cid = add_post(ctx, status="awaiting_approval", slot_delta=timedelta(days=1))
    _short_busy_timeout(ctx)
    client = client_for(ctx)
    con = sqlite3.connect(_db_path(ctx), check_same_thread=False)
    con.execute("BEGIN IMMEDIATE")
    try:
        r = client.post(f"/content/{cid}/approve", headers=ORIGIN, data={"qc_override": "yes"}, follow_redirects=False)
    finally:
        con.rollback()
        con.close()
    assert r.status_code != 500, "database busy must not be an Internal Server Error"


# ================================================================================================= Error handling
def test_ok_missing_image_file_pages_and_asset(ctx):
    cid = add_post(ctx, status="awaiting_approval", image_path="gone.png", slot_delta=timedelta(days=1))
    client = client_for(ctx)
    assert client.get(f"/content/{cid}").status_code == 200 and client.get("/content").status_code == 200
    assert client.get("/assets/gone.png").status_code == 404 and client.get("/assets/..%2F..%2Fsecret.png").status_code == 404
    r = client.post(f"/content/{cid}/save", headers=ORIGIN, data={"title": "T2", "body": "B2 body?"}, follow_redirects=False)
    assert r.status_code == 303
    assert qc_check(post(ctx, cid).qc, "image")["status"] == "uncertain"  # "no image file" is reported, nothing crashes


def test_defect_saving_post_with_damaged_image_file_is_a_500(ctx):
    """check_image and image_hash open the file with no error handling: a half-written or damaged image makes every
    save of that post an Internal Server Error, so the owner cannot even edit it."""
    ctx.settings.assets_dir.mkdir(parents=True, exist_ok=True)
    (ctx.settings.assets_dir / "broken.png").write_bytes(b"PNG? no")
    cid = add_post(ctx, status="awaiting_approval", image_path="broken.png", slot_delta=timedelta(days=1))
    r = client_for(ctx).post(f"/content/{cid}/save", headers=ORIGIN, data={"title": "T2", "body": "B2 body?"}, follow_redirects=False)
    assert r.status_code < 500


def _ollama(generate):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "uat:latest"}]})
        if request.url.path == "/api/generate":
            return generate(request)
        return hn_handler(request)
    return handler


def test_ok_llm_down_uses_template_and_job_succeeds(make_ctx):
    down = lambda r: httpx.Response(500, text="model crashed")  # noqa: E731
    ctx = make_ctx(handler=_ollama(down), llm_provider="ollama", ollama_model="uat:latest")
    res = LinkedInContentAgent(ctx).run({"count": 1, "research_topics": False})
    assert res.ok and post(ctx, res.output.created[0]).generator == "template"

    def unreachable(request):
        raise httpx.ConnectError("refused", request=request)
    ctx2 = make_ctx(handler=unreachable, llm_provider="ollama", ollama_model="uat:latest")
    assert ctx2.llm is None
    assert LinkedInContentAgent(ctx2).run({"count": 1, "research_topics": False}).ok
    rows = health_strip_rows(ctx2)
    assert next(r for r in rows if r[0] == "Local AI model")[1] == "warn"
    assert client_for(ctx2).get("/").status_code == 200


def test_ok_llm_timeout_uses_template(make_ctx):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)
    ctx = make_ctx(handler=_ollama(slow), llm_provider="ollama", ollama_model="uat:latest")
    res = LinkedInContentAgent(ctx).run({"count": 1, "research_topics": False})
    assert res.ok and post(ctx, res.output.created[0]).generator == "template"


def test_defect_llm_returning_non_json_fails_the_content_job(make_ctx):
    """OllamaProvider.generate catches httpx.HTTPError only: a 200 with a non-JSON body (a proxy page, a half-started
    server) raises JSONDecodeError past write_post's LLMError handler, and no post is written at all."""
    html = lambda r: httpx.Response(200, text="<html>starting up</html>")  # noqa: E731
    ctx = make_ctx(handler=_ollama(html), llm_provider="ollama", ollama_model="uat:latest")
    res = LinkedInContentAgent(ctx).run({"count": 1, "research_topics": False})
    assert res.ok, f"the template writer is the documented fallback: {res.error}"


def test_ok_monitor_raises_and_clears_llm_alert_without_network(make_ctx, monkeypatch):
    ctx = make_ctx(llm_provider="ollama", ollama_model="uat:latest")
    with ctx.db.session() as s:
        check_health(s, ctx.settings, llm_probe=lambda: False)
    with ctx.db.session() as s:
        check_health(s, ctx.settings, llm_probe=lambda: False)
    assert [a for a in alerts(ctx, "Local AI model%") if not a.acknowledged]
    with ctx.db.session() as s:
        check_health(s, ctx.settings, llm_probe=lambda: True)
    assert all(a.resolved_at for a in alerts(ctx, "Local AI model%"))


def test_ok_corrupt_heartbeat_file_does_not_break_pages(ctx):
    (ctx.settings.data_dir / "heartbeat.json").write_text("{half a fi", encoding="utf-8")
    client = client_for(ctx)
    for path in ("/", "/manager", "/system", "/jobs", "/api/health"):
        assert client.get(path).status_code == 200, path
    assert read_heartbeat(ctx.settings) is None
