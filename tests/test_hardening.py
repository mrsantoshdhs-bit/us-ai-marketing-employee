"""Production-hardening tests: migrations, backups, isolation, recovery, security, compliance."""

import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import maintenance
from aime.agents import AnalyticsReportingAgent, CRMManagementAgent, LeadQualificationAgent, USBusinessResearchAgent
from aime.backup import backup_sqlite_file, integrity_check, prune_backups
from aime.config import Settings
from aime.context import DataRoleMismatch, create_context
from aime.dedupe import find_duplicate
from aime.doctor import run_checks
from aime.inbound import record_inquiry
from aime.integrations.linkedin import LinkedInClient, get_token
from aime.migrations import migrate, pending_columns
from aime.models import AgentRun, Company, InboundInquiry, JobRun, Lead
from aime.redact import RedactingFilter, redact
from aime.scheduler.runner import FileLock, JobSpec, is_due, load_job_specs, recover, tick
from aime.scoring import LeadFacts, SignalFact, categorize, score_lead
from aime.secure_store import is_protected, protect, unprotect
from aime.sources.base import CompanyCandidate
from aime.timeutil import utcnow
from aime.validation import validate_candidate
from aime.web.app import create_app
from aime.website_check import check_website
from tests.conftest import hn_handler

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------- schema and data safety
def test_migration_adds_columns_without_losing_rows(tmp_path):
    db_path = tmp_path / "old.db"
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE companies (id INTEGER PRIMARY KEY, name VARCHAR(200), normalized_name VARCHAR(200),
            domain VARCHAR(200), website VARCHAR(300), industry VARCHAR(50), state VARCHAR(2), city VARCHAR(100),
            country VARCHAR(2), location_text VARCHAR(300), employee_range VARCHAR(40), description TEXT,
            phone VARCHAR(40), source VARCHAR(50), source_url VARCHAR(500), source_record_id VARCHAR(120),
            verified BOOLEAN, is_competitor BOOLEAN, is_sample BOOLEAN, lifecycle_stage VARCHAR(30),
            merged_into_id INTEGER, created_at DATETIME, updated_at DATETIME);
        INSERT INTO companies (id, name, normalized_name, source, verified, is_competitor, is_sample, lifecycle_stage, country)
            VALUES (1, 'Kept Co', 'kept co', 'test', 0, 0, 0, 'prospect', 'US');
    """)
    con.commit()
    con.close()
    settings = Settings.load(env_file=tmp_path / "none.env", data_dir=tmp_path, database_url=f"sqlite:///{db_path.as_posix()}",
                             data_role="test")
    ctx = create_context(settings)
    assert pending_columns(ctx.db.engine) == []
    with ctx.db.session() as s:
        co = s.get(Company, 1)
        assert co.name == "Kept Co" and co.website_status is None
    assert list((tmp_path / "backups").glob("aime-pre-migration-*.db"))  # backup before ALTER
    assert migrate(ctx.db.engine) == []  # idempotent
    ctx.db.dispose()


def test_backup_prune_and_integrity(ctx):
    path = backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="auto")
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    for i in range(3):
        (ctx.settings.backups_dir / f"aime-auto-20200101-00000{i}.db").write_bytes(b"old")
    assert prune_backups(ctx.settings.backups_dir, keep=1) == 3  # newest real backup is kept
    assert path.exists()
    assert integrity_check(ctx.db)["ok"]


def test_production_database_refuses_test_settings(make_ctx, tmp_path):
    ctx = make_ctx()  # role "test"
    settings = Settings.load(env_file=tmp_path / "none.env", data_dir=ctx.settings.data_dir, database_url="",
                             data_role="production")
    with pytest.raises(DataRoleMismatch):
        create_context(settings)


def test_production_rejects_sample_records(make_ctx):
    ctx = make_ctx(data_role="production")
    with ctx.db.session() as s:
        record_inquiry(s, {"email": "a@b.example", "company": "Sample", "message": "Need a CRM please",
                           "is_sample": True}, "api")
    CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]})
    with ctx.db.session() as s:
        inq = s.execute(select(InboundInquiry)).scalars().one()
        assert inq.status == "rejected" and "production" in inq.error
        assert s.execute(select(Company)).scalars().first() is None
    c = TestClient(create_app(ctx))
    r = c.post("/api/inbound", json={"email": "z@y.example", "company": "Z", "message": "hello there", "is_sample": True})
    assert r.status_code == 422


def test_seed_demo_refuses_production_folder():
    from aime.cli import PRODUCTION_DATA_DIR, main
    assert main(["seed-demo", "--data-dir", str(PRODUCTION_DATA_DIR)]) == 2


# ---------------------------------------------------------------- scheduler resilience
def test_maintenance_window_pauses_ticks_and_expires(ctx):
    maintenance.start(ctx.settings.data_dir, 1, "upgrade")
    assert tick(ctx)["status"] == "skipped"
    path = ctx.settings.data_dir / maintenance.FILENAME
    path.write_text('{"until": "2000-01-01T00:00:00+00:00", "reason": "old"}', encoding="utf-8")
    assert maintenance.active(ctx.settings.data_dir) is None and not path.exists()


def test_lock_from_dead_process_is_recovered(tmp_path):
    lock_path = tmp_path / "job.lock"
    lock_path.write_text("999999 2026-10-25T00:00:00", encoding="utf-8")  # PID that does not exist
    lock = FileLock(lock_path)
    assert lock.acquire()  # dead owner: taken over immediately, no 2-hour wait
    lock.release()
    lock_path.write_text(f"{os.getpid()} now", encoding="utf-8")  # a live owner keeps its lock
    assert not FileLock(lock_path).acquire()


def test_abandoned_runs_are_closed(ctx):
    with ctx.db.session() as s:
        s.add(JobRun(job="research", trigger="scheduler", status="running", started_at=utcnow() - timedelta(hours=3)))
        s.add(AgentRun(run_id="r1", agent="research", status="running", started_at=utcnow() - timedelta(hours=3)))
    assert recover(ctx) == {"abandoned_jobs": 1, "abandoned_agent_runs": 1}


def test_weekly_schedule_and_backoff():
    spec = JobSpec("weekly_report", None, time(8, 0), "", weekdays=(0,))  # Monday 08:00
    wed = datetime(2026, 10, 28, 9, 0, tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)
    due, reason = is_due(spec, None, wed, TZ)
    assert due and "2026-10-26 08:00" in reason
    from aime.models import JobState
    st = JobState(job="weekly_report", last_attempt_at=wed - timedelta(minutes=40), consecutive_failures=2,
                  enabled=True, last_success_at=None)
    assert not is_due(spec, st, wed, TZ)[0]  # second retry waits 60 minutes
    assert is_due(spec, st, wed + timedelta(minutes=25), TZ)[0]


def test_weekly_jobs_loaded_from_config(ctx):
    specs = {s.name: s for s in load_job_specs(ctx.settings)}
    assert specs["weekly_report"].weekdays == (0,) and "db_maintenance" in specs


def test_weekly_report(ctx):
    res = AnalyticsReportingAgent(ctx).run({"kind": "weekly"})
    assert res.ok, res.error
    md = Path(res.output.path_md).read_text(encoding="utf-8")
    assert md.startswith("# Weekly Marketing Report") and "Last 7 days" in md and "weekly_" in res.output.path_md


# ---------------------------------------------------------------- research quality
def test_website_verification_outcomes(make_ctx):
    def handler(request):
        host = request.url.host
        if request.url.path == "/robots.txt":
            if host == "private.example":
                return httpx.Response(200, text="User-agent: *\nDisallow: /")
            return httpx.Response(404)
        pages = {
            "acme.example": (200, "<title>Acme Robotics</title>"),
            "wrong.example": (200, "<title>Totally different store</title>"),
            "parked.example": (200, "<title>parked</title> This domain is for sale"),
            "blocked.example": (403, "denied"),
        }
        if host == "old.example":
            return httpx.Response(301, headers={"location": "https://acme-robotics.example/"})
        if host == "acme-robotics.example":
            return httpx.Response(200, text="<title>Acme Robotics Inc</title>")
        if host in pages:
            code, body = pages[host]
            return httpx.Response(code, text=body, headers={"content-type": "text/html"})
        raise httpx.ConnectError("no such host", request=request)

    ctx = make_ctx(handler=handler)
    assert check_website(ctx, "acme.example", "Acme Robotics").status == "verified"
    assert check_website(ctx, "wrong.example", "Acme Robotics").status == "mismatch"
    assert check_website(ctx, "parked.example", "Acme Robotics").status == "parked"
    assert check_website(ctx, "blocked.example", "Acme Robotics").status == "blocked"
    assert check_website(ctx, "private.example", "Acme Robotics").status == "robots_disallowed"
    assert check_website(ctx, "gone.example", "Acme Robotics").status == "unreachable"
    redirect = check_website(ctx, "old.example", "Acme Robotics")
    assert redirect.status == "redirected" and redirect.final_domain == "acme-robotics.example"


def test_research_job_verifies_new_company_websites(ctx):
    from aime.scheduler.runner import run_job
    assert run_job(ctx, "research")["status"] == "success"
    with ctx.db.session() as s:
        nw = s.execute(select(Company).where(Company.domain == "northwind-analytics.example")).scalars().one()
        assert nw.website_status == "verified" and "Northwind" in nw.website_title
        lead = s.execute(select(Lead).where(Lead.company_id == nw.id)).scalars().one()
        assert any("website verified" in r for r in lead.score_breakdown)
        assert lead.category.startswith(("warm", "nurture", "cold"))


def test_candidate_validation_rejects_malformed_records():
    def cand(**kw):
        base = dict(name="Acme", source="hn_hiring", source_url="https://news.ycombinator.com/item?id=1",
                    signal_text="x" * 50, state="TX", is_us=True)
        base.update(kw)
        return CompanyCandidate(**base)

    assert validate_candidate(cand())[0] == []
    assert validate_candidate(cand(name="Senior Backend Engineer"))[0]
    assert validate_candidate(cand(signal_text="short"))[0]
    assert validate_candidate(cand(state="ZZ"))[0]
    c = cand(domain="lever.co", website="https://lever.co")
    problems, fixes = validate_candidate(c)
    assert not problems and fixes and c.domain is None


def test_core_name_dedupe_same_state_only(ctx):
    with ctx.db.session() as s:
        s.add(Company(name="Beacon Labs", normalized_name="beacon labs", state="CA", source="t"))
        s.flush()
        assert find_duplicate(s, "Beacon AI", None, "CA") is not None
        assert find_duplicate(s, "Beacon AI", None, "TX") is None


def test_same_post_is_matched_by_source_record(ctx):
    USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring"]})
    with ctx.db.session() as s:
        co = s.execute(select(Company).where(Company.source_record_id == "hn:90000011")).scalars().one()
        co.name, co.normalized_name, co.domain = "Renamed", "renamed", None
        s.flush()
        assert find_duplicate(s, "Something Else", None, "CO", "hn:90000011").id == co.id


def test_contractor_signal_and_categories():
    sig = SignalFact("hiring", ["custom_crm"], 1.0, utcnow(), True)
    base = dict(source="research", industry="saas", target_industries=["saas"], state="CA", country="US",
                has_website=True, employee_range=None, verified=False, is_competitor=False, suppressed=False,
                contact_consent=None, signals=[sig], website_status="verified")
    plain = score_lead(LeadFacts(**base))
    helped = score_lead(LeadFacts(**base, seeks_external_help=True))
    assert helped.intent == plain.intent + 3
    assert categorize("inbound", "A", "custom_crm", False) == "hot · CRM & Sales"
    assert categorize("research", "B", "ai_agents", False) == "warm · AI & Agents"
    assert categorize("research", "D", None, True).startswith("disqualified")


def test_requalified_lead_gets_a_new_review_task(ctx):
    from aime.agents import SalesWorkflowAgent
    from aime.models import FollowUp
    with ctx.db.session() as s:
        co = Company(name="Back Co", normalized_name="back co", source="hn_hiring", state="CA")
        s.add(co)
        s.flush()
        lead = Lead(company_id=co.id, source="research", status="qualified", score=60, grade="B")
        s.add(lead)
        s.flush()
        s.add(FollowUp(lead_id=lead.id, company_id=co.id, channel="task", step=1, status="cancelled",
                       subject="Review Back Co", blocked_reason="Score fell below the qualification threshold"))
        lead_id = lead.id
    assert SalesWorkflowAgent(ctx).run({}).output.tasks_created == 1
    with ctx.db.session() as s:
        statuses = sorted(f.status for f in s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id)).scalars())
    assert statuses == ["cancelled", "pending"]
    assert SalesWorkflowAgent(ctx).run({}).output.tasks_created == 0  # no duplicates


def test_downgraded_research_lead_returns_to_nurture(ctx):
    with ctx.db.session() as s:
        co = Company(name="Weak Co", normalized_name="weak co", source="hn_hiring", state=None, country="US")
        s.add(co)
        s.flush()
        s.add(Lead(company_id=co.id, source="research", status="qualified"))
    out = LeadQualificationAgent(ctx).run({"rescore_all": True}).output
    assert out.downgraded


# ---------------------------------------------------------------- security
@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_dpapi_roundtrip_and_token_encrypted_at_rest(make_ctx):
    stored = protect("secret-token-value")
    assert stored.startswith("dpapi:") and "secret-token-value" not in stored
    assert unprotect(stored) == "secret-token-value"
    ctx = make_ctx(linkedin_client_id="cid", linkedin_client_secret="sec")
    with ctx.db.session() as s:
        LinkedInClient(ctx.settings).store_token(s, {"access_token": "AQ" + "x" * 70, "expires_in": 60}, {"sub": "m"})
    with ctx.db.session() as s:
        assert is_protected(get_token(s).access_token)
    assert {c["name"]: c for c in run_checks(ctx)}["OAuth tokens encrypted at rest"]["ok"]


def test_redaction_of_secrets_and_emails(ctx, caplog):
    text = "Bearer abcdefghijklmnop access_token=XYZ123456 client_secret: s3cr3tvalue mail jane@corp.com"
    out = redact(text)
    assert "abcdefghijklmnop" not in out and "XYZ123456" not in out and "s3cr3tvalue" not in out
    assert "jane@corp.com" not in out
    record = logging.LogRecord("aime", logging.INFO, __file__, 1, "token=%s", ("supersecretvalue",), None)
    RedactingFilter().filter(record)
    assert "supersecretvalue" not in record.getMessage()


def test_security_headers_and_inbound_limits(ctx):
    c = TestClient(create_app(ctx))
    r = c.get("/")
    assert r.headers["X-Frame-Options"] == "DENY" and "script-src 'none'" in r.headers["Content-Security-Policy"]
    assert c.post("/api/inbound", content=b"{" + b" " * 21000 + b"}",
                  headers={"content-type": "application/json"}).status_code == 413
    assert c.post("/api/inbound", content=b"not json", headers={"content-type": "application/json"}).status_code == 422
    codes = [c.post("/api/inbound", json={"email": f"p{i}@x.example", "company": "X", "message": "hello there"}).status_code
             for i in range(25)]
    assert 429 in codes


def test_secret_scan_blocks_credentials_and_data(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True)  # noqa: E731
    run("git", "init", "-q")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "secret_scan.py").write_text((ROOT / "scripts" / "secret_scan.py").read_text(encoding="utf-8"),
                                                     encoding="utf-8")
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    personal = "someone@" + "gmail.com"
    (repo / "leak.py").write_text('token = "ghp_' + "a" * 36 + f'"\nmail = "{personal}"\n', encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "aime.db").write_bytes(b"SQLite")
    run("git", "add", "-A")
    res = subprocess.run([sys.executable, "scripts/secret_scan.py", "--staged"], cwd=repo, capture_output=True, text=True)
    assert res.returncode == 1
    assert "GitHub token" in res.stdout and "personal email" in res.stdout and "runtime data" in res.stdout


def test_repository_ignores_private_files():
    ignored = subprocess.run(["git", "check-ignore", ".env", "data/aime.db", "data/publish_journal.jsonl",
                              "demo_data/x.db", "data/backups/a.db", "x.log"], cwd=ROOT, capture_output=True, text=True)
    assert len(ignored.stdout.split()) == 6


# ---------------------------------------------------------------- compliance
FORBIDDEN_AUTOMATION = re.compile(r"\b(selenium|playwright|pyppeteer|puppeteer|undetected_chromedriver|pyautogui|"
                                  r"linkedin_api|li_at|2captcha|anticaptcha)\b", re.I)


def test_no_scraping_or_browser_automation_code():
    for path in list((ROOT / "aime").rglob("*.py")) + [ROOT / "requirements.txt"]:
        assert not FORBIDDEN_AUTOMATION.search(path.read_text(encoding="utf-8")), path


def test_linkedin_adapter_only_calls_official_endpoints():
    src = (ROOT / "aime" / "integrations" / "linkedin.py").read_text(encoding="utf-8")
    hosts = set(re.findall(r"https://([a-z0-9.-]+)/", src))
    assert hosts <= {"www.linkedin.com", "api.linkedin.com"}
    endpoints = set(re.findall(r'"(https://[^"]+)"', src))
    assert endpoints == {"https://www.linkedin.com/oauth/v2/authorization", "https://www.linkedin.com/oauth/v2/accessToken",
                         "https://api.linkedin.com/v2/userinfo", "https://api.linkedin.com/rest/posts",
                         "https://api.linkedin.com/rest/images",
                         "https://www.linkedin.com/feed/update/{post_urn}/"}  # display link to a published post
    for forbidden in ("/invitations", "/messages", "/conversations", "/connections", "li_at", "jsessionid"):
        assert forbidden not in src.lower()


def test_publishing_disabled_by_default():
    defaults = Settings()
    assert defaults.publishing_enabled is False and defaults.require_human_approval is True
    assert defaults.outbound_email_enabled is False
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "PUBLISHING_ENABLED=false" in example and "OUTBOUND_EMAIL_ENABLED=false" in example


def test_network_failures_fall_back_to_cached_threads(make_ctx):
    ctx = make_ctx()
    USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring"]})  # fills the cache

    def down(request):
        raise httpx.ConnectError("offline", request=request)

    ctx.http_transport = httpx.MockTransport(down)
    res = USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring"]})
    assert res.ok and res.output.sources[0].ok
    _ = hn_handler
