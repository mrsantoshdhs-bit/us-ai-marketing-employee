"""End-to-end flows for the MVP sprint, offline on fictional data: each test chains real jobs."""

import csv
import secrets
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import aime.monitoring as monitoring
from aime.agents.linkedin_content import LinkedInContentAgent
from aime.backup import restore_drill
from aime.db import kv_get
from aime.models import (Alert, Company, ContentItem, FollowUp, JobRun, JobState, Lead, PolicyDecision,
                         PublishAttempt)
from aime.notify import notify_alert
from aime.policy import agreement
from aime.qc import content_sha256
from aime.scheduler.runner import run_job
from aime.timeutil import utcnow
from aime.web.app import create_app

ORIGIN = {"origin": "http://testserver"}


def _count(s, model, *where):
    return s.execute(select(func.count(model.id)).where(*where)).scalar_one()


def test_discovery_verification_qualification_crm(ctx):
    """Research (public sample feed + a 45-row CSV) -> website checks -> qualification -> CRM review tasks."""
    ctx.settings.imports_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.settings.imports_dir / "event-list.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "website", "state", "industry", "notes"])
        for i in range(45):
            w.writerow([f"Sample Firm {i:02d}", f"https://firm{i:02d}.example", "TX", "saas", "Opted-in event list"])

    assert run_job(ctx, "plan")["status"] == "success"
    assert run_job(ctx, "research")["status"] == "success"
    with ctx.db.session() as s:
        csv_companies = _count(s, Company, Company.name.like("Sample Firm %"))
        total = _count(s, Company, Company.merged_into_id.is_(None))
        competitors = _count(s, Company, Company.is_competitor.is_(True))
        checked = _count(s, Company, Company.website_status.is_not(None))
        leads = _count(s, Lead)
        graded = _count(s, Lead, Lead.grade.is_not(None))
        tasks = _count(s, FollowUp, FollowUp.channel == "task")
        emails = _count(s, FollowUp, FollowUp.channel == "email")
    assert csv_companies == 45  # past the old 40-record research cap
    assert total > 45 and competitors >= 1
    assert leads == total - competitors and graded == leads  # competitors are flagged, never made leads
    assert checked == ctx.settings.section("research").get("website_checks_per_run", 25)  # polite per-run cap
    assert tasks >= 1 and emails == 0  # research leads become review tasks, never outreach
    assert not path.exists() and (ctx.settings.imports_dir / "processed").exists()

    for _ in range(2):  # CRM maintenance works through the remaining homepages, 25 at a time
        assert run_job(ctx, "crm_maintenance")["status"] == "success"
    with ctx.db.session() as s:
        assert _count(s, Company, Company.domain.is_not(None), Company.website_status.is_(None)) == 0
        assert _count(s, Company, Company.website_status == "verified") >= 1  # sample homepages name the company

    assert run_job(ctx, "research")["status"] == "success"  # the archived file is not imported again
    with ctx.db.session() as s:
        assert _count(s, Company, Company.name.like("Sample Firm %")) == 45


def test_content_image_checks_schedule_policy_approval(ctx):
    """Content -> image -> quality checks -> scheduling -> shadow policy -> human approval -> blocked publish."""
    assert run_job(ctx, "plan")["status"] == "success"
    assert run_job(ctx, "content")["status"] == "success"
    with ctx.db.session() as s:
        posts = s.execute(select(ContentItem).order_by(ContentItem.scheduled_for)).scalars().all()
        assert posts
        slots = [p.scheduled_for for p in posts]
        assert len(set(slots)) == len(slots) and min(slots) > utcnow()
        for p in posts:
            assert p.status == "awaiting_approval" and p.approved_at is None
            assert (ctx.settings.assets_dir / p.image_path).exists()
            assert p.qc_status in ("passed", "held")
            assert p.qc["content_sha256"] == content_sha256(p.title, p.body, p.hashtags)
        decisions = {d.content_id: d for d in s.execute(select(PolicyDecision)).scalars()}
        assert set(decisions) == {p.id for p in posts}
        # Tests render simple cards, so the policy holds everything for a person.
        assert all(d.decision == "would_hold" and any(r.startswith("image:") for r in d.reasons)
                   for d in decisions.values())
        first = posts[0]

    client = TestClient(create_app(ctx))
    form = {"qc_override": "yes"} if first.qc_status != "passed" else {}
    assert client.post(f"/content/{first.id}/approve", data=form, headers=ORIGIN,
                       follow_redirects=False).status_code == 303
    assert run_job(ctx, "publish")["status"] == "success"
    with ctx.db.session() as s:
        item = s.get(ContentItem, first.id)
        assert item.status == "approved" and item.approved_by and item.approved_at
        assert _count(s, PublishAttempt) == 0 and _count(s, ContentItem, ContentItem.status == "published") == 0
        counts = agreement(s)["counts"]
    assert counts["too_strict"] == 1 and counts["pending"] == len(posts) - 1


def test_job_failure_raises_alert_and_one_notification(make_ctx, monkeypatch):
    ctx = make_ctx(data_role="qa", alert_toast=True)
    sent = []
    monkeypatch.setattr(monitoring, "notify_alert",
                        lambda settings, level, message: notify_alert(settings, level, message,
                                                                      sender=lambda t, m: sent.append(m) or True))

    def boom(self, inp, session):
        raise RuntimeError("simulated outage")
    monkeypatch.setattr(LinkedInContentAgent, "execute", boom)
    assert run_job(ctx, "content")["status"] == "failed"
    assert run_job(ctx, "content")["status"] == "failed"
    with ctx.db.session() as s:
        assert s.get(JobState, "content").consecutive_failures == 2
    run_job(ctx, "monitor")
    run_job(ctx, "monitor")
    msg = "Job 'content' failed 2 times in a row"
    with ctx.db.session() as s:
        assert _count(s, Alert, Alert.message == msg, Alert.level == "error") == 1
    assert sent.count(msg) == 1


def test_crash_recovery_backup_and_restore(make_ctx, tmp_path):
    offsite = tmp_path / "offsite"
    ctx = make_ctx(backup_offsite_dir=str(offsite), backup_passphrase=secrets.token_hex(16))
    assert run_job(ctx, "plan")["status"] == "success"
    with ctx.db.session() as s:  # a run left behind by a crashed process
        s.add(JobRun(job="research", trigger="schedule", status="running", started_at=utcnow() - timedelta(hours=3)))
    assert run_job(ctx, "db_maintenance")["status"] == "success"
    with ctx.db.session() as s:
        assert _count(s, JobRun, JobRun.status == "abandoned") == 1
        assert kv_get(s, "backup.last_verified") and kv_get(s, "backup.last_offsite")
        live_jobs = _count(s, JobRun)
    bundle = sorted(Path(offsite).glob("aime-offsite-*.aimebak"))[-1]
    result = restore_drill(ctx.settings, bundle)
    assert result["ok"], result
    assert result["rows"] > 0 and live_jobs >= 2
