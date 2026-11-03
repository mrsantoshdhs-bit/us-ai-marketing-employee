"""Performance benchmark on a synthetic, isolated database (Option B Phase 8: measure before scaling).

Seeds a fresh SQLite database in a temporary folder (never data/) at a multiple of today's production volume and times
the operations a tick and a person do: the research job (discovery, website checks, qualification, drafts), a company
research batch, inbox processing of N replies, the Sales Manager's reports, target progress, the funnel summary and the
main dashboard pages. Nothing leaves the PC: every HTTP request goes to a mock transport, DNS answers come from a stub,
and any other socket connection is refused.

    python scripts/perf_bench.py --scales 1,5,25,100 --label before
    python scripts/perf_bench.py --scales 25 --label after --out-dir C:\\...\\AIME-OptionB-Logs

Each scale runs in its own process (a fresh memory baseline). Results are merged into <out-dir>/perf_bench.json under
the label and written as a table to perf_bench.md. Only measured numbers are written.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Today's production volume (Option B Phase 8 brief, 3 Nov 2026). The rows marked "assumed" were not given and are
# kept small and proportional; they are listed in the report.
BASE = {"companies": 341, "leads": 341, "contacts": 372, "activities": 1100, "followups": 71, "job_runs": 2200,
        "agent_runs": 4400, "signals": 341, "snapshots": 270, "inbound_emails": 30, "alerts": 60, "audit_events": 400,
        "opportunities": 8, "suppressions": 10}
ASSUMED = ("agent_runs", "signals", "snapshots", "inbound_emails", "alerts", "audit_events", "opportunities", "suppressions")
DEFAULT_OUT = Path(os.environ.get("AIME_PERF_OUT") or Path.home() / "Documents" / "AIME-OptionB-Logs")
REPLIES = 20  # replies in the inbox run, the same at every volume: what one 15-minute read takes in at most today
# Run after the morning research and enrich jobs, in the scheduler's order (the inbox job is the InboxAgent run above:
# the real one would open an IMAP connection). crm_maintenance, manager_report and db_maintenance run at the end.
INTERVAL_AND_DAILY_JOBS = ("plan", "content", "inbound", "followups", "publish", "daily_report", "weekly_report", "monitor")
PAGES = ("/", "/sales-manager", "/pipeline", "/leads", "/admin/settings/system", "/jobs")
INDUSTRIES = ("saas", "real_estate", "marketing_agencies", "smb", "healthcare", "logistics")
STATES = ("TX", "NY", "CA", "CO", "FL", "WA", "IL", "OH", "GA", "NC")


# ----------------------------------------------------------------------------------------------- offline guard

def offline(setattr_=setattr) -> None:
    """No network in a benchmark: DNS answers "accepts mail" from a stub, every other socket connection is refused.
    `setattr_`: pytest's monkeypatch.setattr in a test, so everything is put back afterwards."""
    import socket

    import dns.resolver

    from aime import contact_validation

    real_getaddrinfo, real_connect = socket.getaddrinfo, socket.socket.connect
    local = ("127.0.0.1", "::1", "localhost")  # the event loop's own socket pair (TestClient) stays possible

    def getaddrinfo(host, *a, **k):
        if host not in local:
            raise OSError(f"perf_bench: network refused ({host})")
        return real_getaddrinfo(host, *a, **k)

    def connect(self, address):
        if not (isinstance(address, tuple) and address[0] in local):
            raise OSError(f"perf_bench: network refused ({address})")
        return real_connect(self, address)

    def no_dns(*_a, **_k):
        raise dns.resolver.NoNameservers("perf_bench: no DNS")

    setattr_(socket, "getaddrinfo", getaddrinfo)
    setattr_(socket.socket, "connect", connect)
    setattr_(dns.resolver, "resolve", no_dns)
    setattr_(dns.resolver.Resolver, "resolve", no_dns)
    setattr_(contact_validation.MailDomainChecker, "_lookup", lambda self, domain: True)


def handler_factory():
    import httpx

    from aime.sample_data import SAMPLE_HN_ITEMS, SAMPLE_HN_THREAD

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "hn.algolia.com":
            if path.endswith("/search_by_date"):
                return httpx.Response(200, json={"hits": [SAMPLE_HN_THREAD]})
            if "/items/" in path:
                return httpx.Response(200, json={"children": [{**i, "type": "comment"} for i in SAMPLE_HN_ITEMS]})
            if path.endswith("/search"):
                return httpx.Response(200, json={"hits": []})
        site = host.removeprefix("www.")
        if site.endswith(".example"):
            name = site.split(".")[0].replace("-", " ").title()
            if path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
            if path == "/team":
                body = (f"<html><body><h2>Our Team</h2><div class='card'><h3>Alex Morgan</h3><p>Chief Executive Officer</p></div>"
                        f"<p>Email: <a href='mailto:alex.morgan@{site}'>alex.morgan@{site}</a></p></body></html>")
            elif path in ("/", ""):
                body = (f"<html><head><title>{name} | Operations software</title><meta name='description' content='{name} helps "
                        "teams replace spreadsheets and manual data entry.'></head><body><nav><a href='/team'>Our Team</a> "
                        "<a href='/contact'>Contact</a></nav><h1>{name}</h1><p>Our staff still copy orders into spreadsheets.</p>"
                        "</body></html>")
            elif path == "/contact":
                body = f"<html><body><a href='mailto:info@{site}'>info@{site}</a></body></html>"
            else:
                return httpx.Response(404)
            return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404)
    return handler


class FakeIMAP:
    """A read-only IMAP server holding the synthetic replies (the same protocol subset as tests/test_inbox.py)."""

    messages: dict[int, bytes] = {}
    validity = "777"

    def __init__(self, host, port, ssl_context=None, timeout=None):
        pass

    def login(self, user, password):
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [self.validity.encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            crit = " ".join(a for a in args if a)
            lo = int(crit.split()[1].split(":")[0]) if crit.startswith("UID ") else 0
            words = crit.split(" BODY ", 1)[1].strip('"').encode() if " BODY " in crit else b""
            return "OK", [" ".join(str(u) for u in sorted(self.messages) if u >= lo and words.lower() in self.messages[u].lower()).encode()]
        uid = int(args[0])
        return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.messages[uid]), b")"]

    def logout(self):
        pass


# ----------------------------------------------------------------------------------------------- seeding

# Test mode, nothing sent or published, and a (fake) sales mailbox so the inbox work runs.
BENCH_SETTINGS = dict(llm_provider="template", retry_base_delay=0.0, polite_delay=0.0, data_role="test", dashboard_auth=False,
                      image_renderer="pillow", calendly_verified_only=False, outbound_email_enabled=False, publishing_enabled=False,
                      imap_host="imap.example", smtp_host="smtp.example", smtp_username="sales@us.example", smtp_password="x" * 8,
                      smtp_from="hello@us.example", company_postal_address="1 Sample St, Austin, TX 78701")


def make_ctx(data_dir: Path):
    import httpx

    from aime.config import Settings
    from aime.context import create_context, set_context

    settings = Settings.load(env_file=data_dir / "no.env", data_dir=data_dir, database_url="", **BENCH_SETTINGS)
    ctx = create_context(settings, http_transport=httpx.MockTransport(handler_factory()))
    set_context(ctx)
    return ctx


def load_replies(ctx, sent_lead_ids: list[int]) -> None:
    """REPLIES replies in the fake mailbox, to emails that were sent: interested, no, opt-out, a question, an absence."""
    from aime.models import Lead

    FakeIMAP.messages = {}
    bodies = ("Sounds interesting. Could we talk Thursday afternoon?", "Not interested, thanks.", "Please unsubscribe me.",
              "What does this cost?", "I am out of the office until Monday.")
    with ctx.db.session() as s:
        for uid, lid in enumerate(sent_lead_ids[:REPLIES], start=1):
            lead = s.get(Lead, lid)
            FakeIMAP.messages[uid] = reply_mail(lead.contact.email, lid, bodies[uid % len(bodies)])


def seed(ctx, scale: int, rnd: random.Random) -> dict:
    """Bulk rows at `scale` x today's volume, spread over the last 90 days, through Core inserts (fast)."""
    from aime import models as m
    from aime.timeutil import utcnow

    n = {k: max(1, round(v * scale)) for k, v in BASE.items()}
    now = utcnow()

    def ago(max_days: float = 90) -> datetime:
        return now - timedelta(seconds=rnd.uniform(0, max_days * 86400))

    lead_status = ["new"] * 40 + ["qualified"] * 20 + ["nurture"] * 20 + ["disqualified"] * 18 + ["converted"] * 2
    with ctx.db.engine.begin() as conn:
        companies = []
        for i in range(1, n["companies"] + 1):
            created = ago()
            researched = rnd.random() < 0.8
            companies.append(dict(
                id=i, name=f"Synthetic Co {i}", normalized_name=f"synthetic co {i}", domain=f"synthetic-co-{i}.example",
                website=f"https://synthetic-co-{i}.example", industry=rnd.choice(INDUSTRIES), state=rnd.choice(STATES),
                city="Austin", country="US", location_evidence="US state (synthetic)", source=rnd.choice(("hn_hiring", "csv_import", "sec_edgar")),
                source_url="https://news.ycombinator.com/item?id=1", verified=True, is_competitor=rnd.random() < 0.03,
                is_sample=False, lifecycle_stage="prospect", website_status="verified" if rnd.random() < 0.85 else None,
                website_checked_at=created, website_title=f"Synthetic Co {i}", validation={},
                research_status="done" if researched else None, researched_at=ago(29) if researched else None,
                created_at=created, updated_at=created))
        conn.execute(m.Company.__table__.insert(), companies)
        conn.execute(m.Signal.__table__.insert(), [dict(
            company_id=rnd.randint(1, n["companies"]), signal_type="hiring", text="Hiring: ops automation, spreadsheets",
            source="hn_hiring", source_url="https://news.ycombinator.com/item?id=1", fingerprint=f"fp-{i}",
            keywords=["spreadsheets"], strength=0.7, observed_at=ago(), created_at=ago()) for i in range(n["signals"])])
        contacts = []
        for i in range(1, n["contacts"] + 1):
            cid = ((i - 1) % n["companies"]) + 1
            person = rnd.random() < 0.4
            contacts.append(dict(
                id=i, company_id=cid, first_name="Alex" if person else None, last_name=f"Person{i}" if person else None,
                email=f"{'alex' if person else 'info'}{i}@synthetic-co-{cid}.example", title="Chief Executive Officer" if person else None,
                source="website", consent_status="none", is_sample=False, role_category="executive" if person else "inbox",
                relevance=80 if person else 20, confidence="high" if person else "medium", email_status="published",
                created_at=ago(), updated_at=now))
        conn.execute(m.Contact.__table__.insert(), contacts)
        leads = []
        for i in range(1, n["leads"] + 1):
            st = rnd.choice(lead_status)
            score = rnd.randint(10, 95)
            leads.append(dict(
                id=i, company_id=((i - 1) % n["companies"]) + 1, contact_id=i if i <= n["contacts"] else None, source="research",
                status=st, score=score, grade="ABCD"[min(3, (100 - score) // 25)], fit_score=score // 3, need_score=score // 3,
                intent_score=0, data_score=score // 3, score_breakdown=[], matched_services=["bpa"], primary_service="bpa",
                category="warm · AI & Agents", outreach_status=None, owner=None, is_sample=False, created_at=ago(),
                updated_at=now, last_scored_at=ago(5)))
        sent_lead_ids = rnd.sample(range(1, min(n["leads"], n["contacts"]) + 1), min(n["followups"], n["leads"]))
        followups = []
        for j, lid in enumerate(sent_lead_ids):
            sent = j % 5 < 2
            when = ago(60)
            followups.append(dict(
                lead_id=lid, contact_id=lid, company_id=leads[lid - 1]["company_id"], channel="email", step=1, due_at=when,
                status="sent" if sent else rnd.choice(("awaiting_approval", "cancelled", "rejected")), subject=f"Question for Synthetic Co {lid}",
                body="Hello", sent_at=when if sent else None, message_id=f"<sent-{lid}@us.example>" if sent else None,
                kind="outreach", to_email=contacts[lid - 1]["email"], delivered_to=contacts[lid - 1]["email"] if sent else None,
                generator="template", evidence=[], quality={}, created_at=when, updated_at=when))
            if sent:
                leads[lid - 1]["outreach_status"] = "contacted"
                leads[lid - 1]["status"] = "qualified"
        conn.execute(m.Lead.__table__.insert(), leads)
        conn.execute(m.FollowUp.__table__.insert(), followups)
        types = ("research", "status_change", "followup", "note", "email")
        conn.execute(m.Activity.__table__.insert(), [dict(
            company_id=rnd.randint(1, n["companies"]), lead_id=rnd.randint(1, n["leads"]), type=rnd.choice(types),
            subject="Company research: 3 page(s) read", body=None, actor=rnd.choice(("company_research", "lead_qualification", "owner")),
            created_at=ago()) for _ in range(n["activities"])])
        conn.execute(m.ResearchSnapshot.__table__.insert(), [dict(
            company_id=rnd.randint(1, n["companies"]), fetched_at=ago(), status="done", pages=[], people=[], emails=[], phones=[],
            services_offered=[], summary="Synthetic research") for _ in range(n["snapshots"])])
        jobs = ("inbox", "inbound", "monitor", "followups", "publish", "research", "enrich", "db_maintenance", "crm_maintenance")
        job_rows = []
        for i in range(1, n["job_runs"] + 1):
            start = ago(85)
            job_rows.append(dict(id=i, job=rnd.choice(jobs), trigger="scheduler", status=rnd.choice(("success",) * 9 + ("failed",)),
                                 started_at=start, finished_at=start + timedelta(seconds=3), duration_ms=3000,
                                 detail={"steps": [{"agent": "inbox_monitor", "status": "success"}]}))
        conn.execute(m.JobRun.__table__.insert(), job_rows)
        conn.execute(m.AgentRun.__table__.insert(), [dict(
            run_id=f"r{i:x}", agent=rnd.choice(("inbox_monitor", "crm_management", "lead_qualification", "sales_workflow")),
            job="inbox", job_run_id=rnd.randint(1, n["job_runs"]), status="success", attempts=1, started_at=ago(85),
            finished_at=now, duration_ms=900, input_summary={}, output_summary={}, llm_calls=0, llm_failures=0, llm_tokens=0,
            llm_ms=0, llm_cost_usd=0.0) for i in range(n["agent_runs"])])
        conn.execute(m.InboundEmail.__table__.insert(), [dict(
            message_id=f"<old-{i}@mail.example>", from_email=f"someone{i}@elsewhere.example", subject="Re: hello",
            received_at=ago(), category="other", status="reviewed", lead_id=rnd.randint(1, n["leads"]), bounced_addresses=[], reasons=[],
            auto_submitted=False, is_simulated=False) for i in range(n["inbound_emails"])])
        conn.execute(m.Alert.__table__.insert(), [dict(
            level="warning", source="monitor", message=f"Synthetic alert {i}", created_at=ago(), acknowledged=True,
            key=f"synthetic:{i}", resolved_at=now) for i in range(n["alerts"])])
        conn.execute(m.AuditEvent.__table__.insert(), [dict(
            at=ago(), actor="owner", action="settings.save", target="settings:retention", detail="") for _ in range(n["audit_events"])])
        conn.execute(m.Opportunity.__table__.insert(), [dict(
            lead_id=rnd.randint(1, n["leads"]), company_id=rnd.randint(1, n["companies"]), name=f"Deal {i}",
            stage=rnd.choice(("new", "discovery", "proposal", "won", "lost")), created_at=ago(), updated_at=now) for i in range(n["opportunities"])])
        conn.execute(m.Suppression.__table__.insert(), [dict(
            value=f"no{i}@synthetic-co-{i + 1}.example", kind="email", reason="unsubscribe", created_at=ago()) for i in range(n["suppressions"])])
    return {**n, "sent_lead_ids": [f["lead_id"] for f in followups if f["status"] == "sent"]}


def reply_mail(frm: str, lead_id: int, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = frm, "sales@us.example", f"Re: Question for Synthetic Co {lead_id}"
    msg["Message-ID"] = f"<reply-{lead_id}@mail.example>"
    msg["In-Reply-To"] = f"<sent-{lead_id}@us.example>"
    msg["Date"] = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(body)
    return bytes(msg)


# ----------------------------------------------------------------------------------------------- operations

def timed(results: dict, name: str, fn):
    t0 = time.perf_counter()
    try:
        value = fn()
        status = "ok"
    except Exception as exc:  # noqa: BLE001 - a benchmark records the failure and goes on
        value, status = None, f"error: {type(exc).__name__}: {exc}"[:300]
    results[name] = {"seconds": round(time.perf_counter() - t0, 3), "status": status}
    return value


def _step_output(ctx, job: dict | None, agent: str, key: str):
    from aime.models import JobRun

    if not job or not job.get("run_id"):
        return None
    with ctx.db.session() as s:
        steps = (s.get(JobRun, job["run_id"]).detail or {}).get("steps", [])
    return next(((st.get("output") or {}).get(key) for st in steps if st.get("agent") == agent), None)


def peak_rss_mb() -> float | None:
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.WinDLL("kernel32")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
            return round(pmc.PeakWorkingSetSize / 2 ** 20, 1)
        import resource

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:  # noqa: BLE001
        return None


def run_scale(scale: int, work: Path) -> dict:
    offline()
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from aime import funnel, manager_report, targets
    from aime.agents.inbox_agent import InboxAgent
    from aime.inbox import Mailbox
    from aime.models import Company
    from aime.scheduler.runner import run_job
    from aime.web.app import create_app

    rnd = random.Random(20261103 + scale)
    data_dir = work / f"x{scale}" / "data"
    if data_dir.parent.exists():
        shutil.rmtree(data_dir.parent)
    data_dir.mkdir(parents=True)
    out: dict = {"scale": scale, "ops": {}}
    ops = out["ops"]
    ctx = timed(ops, "create_context", lambda: make_ctx(data_dir))
    seeded = timed(ops, "seed", lambda: seed(ctx, scale, rnd))
    out["rows"] = {k: v for k, v in seeded.items() if k != "sent_lead_ids"}
    db_file = Path(ctx.db.engine.url.database)
    with ctx.db.engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    out["db_mb_seeded"] = round(db_file.stat().st_size / 2 ** 20, 2)

    # The scheduled work, in the order a morning tick runs it.
    (ctx.settings.imports_dir).mkdir(parents=True, exist_ok=True)
    job = timed(ops, "research_job (discovery+checks+qualification+drafts)", lambda: run_job(ctx, "research"))
    out["research_job_status"] = job and job["status"]
    job = timed(ops, "enrich_job (company research batch)", lambda: run_job(ctx, "enrich"))
    out["enrich_job_status"] = job and job["status"]
    out["enrich_companies_researched"] = _step_output(ctx, job, "company_research", "companies_researched")

    load_replies(ctx, seeded["sent_lead_ids"])
    box = Mailbox(ctx.settings, imap_factory=FakeIMAP)
    res = timed(ops, f"inbox ({len(FakeIMAP.messages)} replies)", lambda: InboxAgent(ctx, mailbox=box).run({}))
    out["inbox_stored"] = getattr(getattr(res, "output", None), "stored", None)
    # The interval jobs (every 15-60 minutes) and the rest of the daily set, as the scheduler runs them.
    for name in INTERVAL_AND_DAILY_JOBS:
        job = timed(ops, f"{name} job", lambda n=name: run_job(ctx, n))
        out.setdefault("job_status", {})[name] = job and job["status"]

    def report(kind: str):
        with ctx.db.session() as s:
            cfg = manager_report.config(s, ctx.settings)
            period = manager_report.latest_period(kind, manager_report.local_today(cfg), cfg)
            return manager_report.build(s, ctx.settings, period)
    for kind in ("daily", "weekly", "monthly"):
        timed(ops, f"manager_report.build {kind}", lambda k=kind: report(k))

    def progress():
        with ctx.db.session() as s:
            return targets.progress(s, ctx.settings)
    timed(ops, "targets.progress", progress)

    def summary():
        with ctx.db.session() as s:
            return funnel.summary(s)
    timed(ops, "funnel.summary", summary)

    client = TestClient(create_app(ctx))
    for path in PAGES:
        def get(p=path):
            r = client.get(p)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r
        timed(ops, f"GET {path} (first)", get)
        timed(ops, f"GET {path}", get)
    timed(ops, "manager_report job (daily, written only)", lambda: run_job(ctx, "manager_report"))
    timed(ops, "crm_maintenance job", lambda: run_job(ctx, "crm_maintenance"))
    timed(ops, "db_maintenance job", lambda: run_job(ctx, "db_maintenance"))

    with ctx.db.session() as s:
        out["companies_after"] = s.execute(select(func.count(Company.id))).scalar_one()
    ctx.db.dispose()
    out["db_mb_final"] = round(db_file.stat().st_size / 2 ** 20, 2)
    out["peak_rss_mb"] = peak_rss_mb()
    return out


# ----------------------------------------------------------------------------------------------- report

def write_markdown(results: dict, path: Path) -> None:
    lines = ["# AIME performance benchmark (synthetic isolated database)", "",
             f"Machine: {results.get('machine', '?')}. Base volume (1x): " + ", ".join(f"{k} {v}" for k, v in BASE.items()) + ".",
             f"Rows not given by the brief and assumed: {', '.join(ASSUMED)}. Inbox run: {REPLIES} replies at every volume.",
             "Every number below was measured by scripts/perf_bench.py; seconds are wall-clock on this PC with other work running.", ""]
    for label, runs in results.get("runs", {}).items():
        scales = sorted(runs, key=int)
        lines += [f"## {label}", "", f"Recorded {runs[scales[0]].get('recorded_at', '?')} at commit {runs[scales[0]].get('commit', '?')}.", ""]
        names = list(dict.fromkeys(name for s in scales for name in runs[s]["ops"]))
        lines.append("| operation | " + " | ".join(f"{s}x" for s in scales) + " |")
        lines.append("|---|" + "---:|" * len(scales))
        for name in names:
            cells = []
            for s in scales:
                op = runs[s]["ops"].get(name)
                cells.append("-" if op is None else (f"{op['seconds']:.2f}" if op["status"] == "ok" else f"{op['seconds']:.2f} ({op['status'][:40]})"))
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
        for key in ("db_mb_seeded", "db_mb_final", "peak_rss_mb"):
            lines.append(f"| {key} | " + " | ".join(str(runs[s].get(key)) for s in scales) + " |")
        lines.append("| companies (seeded) | " + " | ".join(str(runs[s]["rows"].get("companies")) for s in scales) + " |")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scales", default="1,5,25,100")
    ap.add_argument("--label", default="run")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--one", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--work", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.one:
        print("PERF_RESULT=" + json.dumps(run_scale(args.one, Path(args.work))), flush=True)
        return
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    store = out_dir / "perf_bench.json"
    results = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    results["machine"] = f"{platform.processor() or platform.machine()}, {os.cpu_count()} logical CPUs, Python {platform.python_version()}"
    commit = os.environ.get("AIME_PERF_COMMIT") or subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                                                                  capture_output=True, text=True).stdout.strip()
    work = Path(tempfile.mkdtemp(prefix="aime-perf-"))
    env = {**os.environ, "AIME_DATA_ROLE": "test", "OUTBOUND_EMAIL_ENABLED": "false", "PUBLISHING_ENABLED": "false",
           "LLM_PROVIDER": "template", "PYTHONIOENCODING": "utf-8"}
    try:
        for scale in [int(x) for x in args.scales.split(",") if x.strip()]:
            t0 = time.perf_counter()
            proc = subprocess.run([sys.executable, __file__, "--one", str(scale), "--work", str(work)], capture_output=True,
                                  text=True, env=env, encoding="utf-8", errors="replace")
            line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PERF_RESULT=")), None)
            if line is None:
                print(f"scale {scale}x failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}", file=sys.stderr)
                continue
            run = json.loads(line.removeprefix("PERF_RESULT="))
            run.update(recorded_at=datetime.now().isoformat(timespec="seconds"), commit=commit,
                       wall_seconds=round(time.perf_counter() - t0, 1))
            results.setdefault("runs", {}).setdefault(args.label, {})[str(scale)] = run
            store.write_text(json.dumps(results, indent=1), encoding="utf-8")
            write_markdown(results, out_dir / "perf_bench.md")
            print(f"{scale}x done in {run['wall_seconds']} s: " + ", ".join(f"{k}={v['seconds']}" for k, v in run["ops"].items()), flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
