"""Option B reliability, end to end through the real jobs and the scheduler: a PC restart (a new process on the same data),
interrupted scheduler runs (a job lock held or stale), a locked database during a send and a report claim, a network
outage (every web call and the mailbox down), the local model refused or timing out, a source answering 429, one agent
failing inside a job, the same event arriving twice (an IMAP message, a Calendly notice, an inbound form), two replies at
once, and the emergency stop pressed in the middle of a sending run. `sweep` checks the safety invariants after each."""

from __future__ import annotations

import os
import sqlite3
import threading
import time as _time
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from aime import automation, maintenance
from aime import manager_report as mr
from aime.agents.qualification import LeadQualificationAgent
from aime.models import (Alert, AuditEvent, Company, Contact, FollowUp, InboundEmail, InboundInquiry, JobRun, Lead, Meeting,
                         ReportDelivery)
from aime.scheduler import runner
from aime.timeutil import utcnow
from aime.workflows import JobOutcome
from tests.e2e_ob.helpers import (DAVID, MX, OTHER, ORIGIN, SITE, FakeSMTP, OBIMAP, World, at, drafted_and_approved, forward,
                                  recording, second_prospect, sends_to, sweep)
from tests.e2e_ob.test_full_workflow import booked_notice, through_first_send
from tests.test_inbox import FakeIMAP, mail

def job_runs(ctx) -> dict[str, int]:
    with ctx.db.session() as s:
        out: dict[str, int] = {}
        for r in s.execute(select(JobRun)).scalars():
            out[r.job] = out.get(r.job, 0) + 1
        return out


def no_windows_tasks(monkeypatch):
    """Nothing here may reach Task Scheduler or OneDrive."""
    import subprocess

    from aime import backup
    from aime.scheduler import windows
    blocked = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="blocked in e2e_ob")
    monkeypatch.setattr(windows, "_schtasks", lambda *a, **k: blocked)
    monkeypatch.setattr(windows, "_schtasks_interactive", lambda *a, **k: blocked)
    monkeypatch.setattr(backup, "onedrive_folders", lambda: [])


# --------------------------------------------------------------------------------------------------- restarts

def test_a_pc_restart_resumes_the_ticks_without_running_a_slot_twice_and_honours_maintenance(make_ctx, monkeypatch,
                                                                                            network_attempts):
    no_windows_tasks(monkeypatch)
    w = World(make_ctx, monkeypatch)
    for name in list(runner.JOBS):  # the scheduler's bookkeeping, not the jobs' work
        monkeypatch.setitem(runner.JOBS, name, lambda ctx, job=name: JobOutcome())
    t0 = at(datetime(2026, 10, 27, 14, 0))
    first = runner.tick(w.ctx, now=t0)
    assert first["status"] == "ok" and first["ran"], first
    ran_first = job_runs(w.ctx)
    w.restart()  # the PC restarts a minute later: a new process, the same data
    forward(timedelta(minutes=1))
    again = runner.tick(w.ctx, now=utcnow())
    assert again["status"] == "ok"
    assert job_runs(w.ctx) == ran_first, "a job ran twice in the same slot after the restart"
    # a maintenance window (a deploy): the tick and a manual run both stand aside, then everything resumes
    maintenance.start(w.ctx.settings.data_dir, 1, "deploy rehearsal")
    assert runner.tick(w.ctx)["status"] == "skipped"
    assert runner.run_job(w.ctx, "followups")["status"] == "skipped"
    assert job_runs(w.ctx) == ran_first
    maintenance.stop(w.ctx.settings.data_dir)
    forward(timedelta(hours=3))
    resumed = runner.tick(w.ctx, now=utcnow())
    assert resumed["status"] == "ok" and resumed["ran"]
    with w.ctx.db.session() as s:
        assert s.execute(select(func.count(JobRun.id)).where(JobRun.status == "running")).scalar_one() == 0
    sweep(w, network_attempts)


def test_a_job_lock_held_by_a_live_run_skips_and_a_dead_owners_lock_is_taken_over(make_ctx, monkeypatch, network_attempts):
    w = World(make_ctx, monkeypatch)
    draft = drafted_and_approved(w)
    w.job("inbox")
    lock = runner.FileLock(w.ctx.settings.locks_dir / "job_followups.lock")
    assert lock.acquire()  # a run of this job is alive in this very process
    try:
        assert runner.run_job(w.ctx, "followups") == {"job": "followups", "status": "skipped", "reason": "already running"}
        assert FakeSMTP.sent == [] and w.followup(draft.id).status == "approved"
    finally:
        lock.release()
    # a run killed hard: its lock names a process that is gone, and its run row says "running"
    (w.ctx.settings.locks_dir / "job_followups.lock").write_text(f"999999 {datetime.now().isoformat()}", encoding="utf-8")
    with w.ctx.db.session() as s:
        s.add(JobRun(job="followups", trigger="scheduler", status="running", started_at=utcnow() - timedelta(minutes=30)))
    res = w.send()
    assert res["status"] == "success" and sends_to(DAVID) == 1
    with w.ctx.db.session() as s:
        statuses = [r.status for r in s.execute(select(JobRun).where(JobRun.job == "followups")).scalars()]
    assert "running" not in statuses and "abandoned" in statuses, statuses
    sweep(w, network_attempts)


# --------------------------------------------------------------------------------------------------- the database

class Holder:
    """Another process holding SQLite's write lock (BEGIN IMMEDIATE) for `seconds`."""

    def __init__(self, path, seconds: float):
        self.path, self.seconds = path, seconds
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self):
        con = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        con.execute("BEGIN IMMEDIATE")
        self.ready.set()
        _time.sleep(self.seconds)
        con.execute("ROLLBACK")
        con.close()

    def __enter__(self):
        self.thread.start()
        self.ready.wait(5)
        return self

    def __exit__(self, *a):
        self.thread.join(30)


def db_path(ctx) -> str:
    return ctx.db.engine.url.database


def test_a_briefly_locked_database_during_a_send_waits_and_sends_once(make_ctx, monkeypatch, network_attempts):
    w = World(make_ctx, monkeypatch)
    draft = drafted_and_approved(w)
    w.job("inbox")
    with Holder(db_path(w.ctx), 2.0):  # a dashboard click holding the write lock for two seconds
        res = w.job("followups", ("success", "partial", "failed"))
    assert res["status"] == "success", res
    w.send()
    assert sends_to(DAVID) == 1 and w.followup(draft.id).status == "sent"
    sweep(w, network_attempts)


def test_a_database_locked_past_the_busy_timeout_sends_nothing_and_loses_nothing(make_ctx, monkeypatch, network_attempts):
    """The busy timeout is cut to one second for the test; the lock is held for three. The send claim cannot be written,
    so nothing may leave; the email stays approved and goes once the database is free."""
    w = World(make_ctx, monkeypatch)
    draft = drafted_and_approved(w)
    w.job("inbox")
    from sqlalchemy import event

    @event.listens_for(w.ctx.db.engine, "connect")
    def _short(dbapi_con, _):
        dbapi_con.execute("PRAGMA busy_timeout=1000")
    w.ctx.db.engine.dispose()
    with Holder(db_path(w.ctx), 3.0):
        res = runner.run_job(w.ctx, "followups")
    assert res["status"] == "failed" and "database is locked" in (res["error"] or ""), res
    assert FakeSMTP.sent == [] and w.followup(draft.id).status == "approved"  # never "sending" with nobody to finish it
    forward(timedelta(minutes=2))
    w.send()
    assert sends_to(DAVID) == 1 and w.followup(draft.id).status == "sent"
    sweep(w, network_attempts)


def test_a_locked_database_during_the_report_claim_never_sends_the_report_twice(make_ctx, monkeypatch, network_attempts):
    w = World(make_ctx, monkeypatch, outbound_email_enabled=True, smtp_from="reports@us.example")
    with w.ctx.db.session() as s:
        mr.save(s, w.ctx.settings, "owner", {"recipients": "manager@us.example", "email_enabled": True})
        mr.authorize(s, w.ctx.settings, "owner")
    at(datetime(2026, 10, 27, 23, 30))
    with Holder(db_path(w.ctx), 2.0):
        first = w.job("manager_report", ("success", "failed", "partial"))
    w.job("manager_report", ("success", "failed", "partial"))
    w.job("manager_report", ("success", "failed", "partial"))
    with w.ctx.db.session() as s:
        deliveries = s.execute(select(ReportDelivery)).scalars().all()
        sent = [d for d in deliveries if d.status == "sent"]
        keys = [(d.kind, d.period_start) for d in deliveries]
    assert len(keys) == len(set(keys)), keys  # one claim per report and period
    assert sent and len(FakeSMTP.sent) == len(sent), (first, [(d.kind, d.status) for d in deliveries], len(FakeSMTP.sent))
    sweep(w, network_attempts)


# --------------------------------------------------------------------------------------------------- outages

def test_a_network_outage_degrades_with_alerts_loses_nothing_and_recovers(make_ctx, monkeypatch, network_attempts):
    w = World(make_ctx, monkeypatch)
    draft = drafted_and_approved(w)
    w.job("inbox")
    good = w.handler

    def down(request):
        raise httpx.ConnectError("network unreachable (fake outage)", request=request)
    w.handler = down
    w.ctx.http_transport = httpx.MockTransport(recording(down, w.hosts))
    OBIMAP.down = True
    forward(timedelta(minutes=30))  # the last good read is too old now: approved mail must wait for a read
    FakeIMAP.messages[1] = mail(f"David Chen <{DAVID}>", "Re: note", "Please remove me from your list.", msg_id="<optout@x.example>")
    results = {name: runner.run_job(w.ctx, name)["status"] for name in ("research", "enrich", "inbox", "followups")}
    assert results["inbox"] == "failed", results
    assert FakeSMTP.sent == [] and w.followup(draft.id).status == "approved"  # held: an opt-out may be waiting unread
    with w.ctx.db.session() as s:
        alerts = [a.message for a in s.execute(select(Alert).where(Alert.resolved_at.is_(None))).scalars()]
    assert alerts, "an outage with no alert"
    # the network comes back: the opt-out waiting in the mailbox is read before anything goes
    OBIMAP.down = False
    w.ctx.http_transport = httpx.MockTransport(recording(good, w.hosts))
    w.send()
    with w.ctx.db.session() as s:
        from aime.crm_ops import is_suppressed
        assert is_suppressed(s, DAVID)
    assert FakeSMTP.sent == [], "an approved email went to someone whose opt-out was waiting in the mailbox"
    sweep(w, network_attempts)


@pytest.mark.parametrize("failure", ["refused", "timeout"])
def test_an_unavailable_model_leaves_the_rules_carrying_on(make_ctx, monkeypatch, network_attempts, failure):
    """The real Ollama provider, on this PC's address, whose server refuses the connection or never answers."""
    from aime.llm.ollama import OllamaProvider

    def ollama(request):
        if failure == "refused":
            raise httpx.ConnectError("[WinError 10061] No connection could be made", request=request)
        raise httpx.ReadTimeout("timed out", request=request)

    w = World(make_ctx, monkeypatch, brain="model")
    w.conversation_mode("drafts")
    through_first_send(w)
    w.ctx.conversation_llm = OllamaProvider("http://127.0.0.1:11434", "qwen2.5:7b", timeout=2, transport=httpx.MockTransport(ollama))
    row = w.say("This sounds interesting. We are looking to automate how we enter supplier invoices.")
    assert row.category == "interested" and row.status == "actioned"
    f = w.followup(row.suggested_followup_id)
    assert f.status == "awaiting_approval"
    from aime.models import ConversationFact
    with w.ctx.db.session() as s:
        facts = s.execute(select(ConversationFact).where(ConversationFact.evidence_inbound_id == row.id)).scalars().all()
    assert {x.source for x in facts} <= {"rule"} and any(x.kind == "requirement" for x in facts), [(x.kind, x.source) for x in facts]
    w.answer(row)
    sweep(w, network_attempts)


def test_a_429_from_a_contact_source_cools_down_and_is_not_hammered(make_ctx, monkeypatch, network_attempts):
    """The UK register (Companies House) answers 429 with Retry-After while the enrich job researches a UK company: the
    job carries on, and the next runs inside the wait do not call the register again."""
    from tests.test_contact_sources import CH_HOST, KEY, ch_handler, enable_gb, set_adapter, uk_company
    from aime.contact_sources import budget

    log: list = []
    ch = ch_handler(log, 429, {"Retry-After": "120"})
    w = World(make_ctx, monkeypatch, companies_house_api_key=KEY)
    site = w.handler
    w.ctx.http_transport = httpx.MockTransport(recording(lambda r: ch(r) if r.url.host == CH_HOST else site(r), w.hosts))
    enable_gb(w.ctx)
    set_adapter(w.ctx, "companies_house", enabled=True, countries=["GB"])
    with w.ctx.db.session() as s:
        co = uk_company(s, website="https://thames-freight.example", domain="thames-freight.example", website_status="verified")
        s.add(Lead(company_id=co.id, source="research", status="qualified", score=70, grade="B", primary_service="bpa"))
    for _ in range(3):
        w.job("enrich", ("success", "partial"))
        forward(timedelta(seconds=20))
    calls = [r for r in log if r.url.host == CH_HOST]
    assert len(calls) == 1, f"{len(calls)} calls to a source that said wait 120 seconds"
    with w.ctx.db.session() as s:
        assert budget.cooldown_until(s, "companies_house") is not None
    forward(timedelta(minutes=3))  # the wait is over: one more call, not a burst
    w.job("enrich", ("success", "partial"))
    assert len([r for r in log if r.url.host == CH_HOST]) <= 2
    sweep(w, network_attempts)


def test_one_agent_failing_inside_a_job_leaves_the_others_running(make_ctx, monkeypatch, network_attempts):
    w = World(make_ctx, monkeypatch)
    w.import_list()
    w.job("plan")

    def broken(self, inp, session):
        raise RuntimeError("scoring blew up (fake)")
    monkeypatch.setattr(LeadQualificationAgent, "execute", broken)
    res = runner.run_job(w.ctx, "research")
    assert res["status"] == "partial" and "qualification" in (res["error"] or ""), res
    with w.ctx.db.session() as s:
        assert s.execute(select(Company).where(Company.domain == SITE)).scalar_one()  # discovery kept its work
        run = s.get(JobRun, res["run_id"])
        assert run.status == "partial"
    monkeypatch.undo()
    w2 = World(make_ctx, monkeypatch, data_dir=w.ctx.settings.data_dir)  # fakes back on, the same data
    w2.job("research")
    w2.job("enrich")
    sweep(w2, network_attempts)


# --------------------------------------------------------------------------------------------------- the same thing twice

def test_the_same_imap_message_twice_is_one_reply(make_ctx, monkeypatch, network_attempts):
    w = World(make_ctx, monkeypatch)
    w.conversation_mode("drafts")
    through_first_send(w)
    raw = mail(f"David Chen <{DAVID}>", "Re: note", "Sounds interesting. Could we talk Thursday?", in_reply_to=w.thread,
               msg_id="<same@brightline-logistics.example>")
    FakeIMAP.messages[1] = raw
    w.job("inbox")
    FakeIMAP.messages[2] = raw  # the server shows it again under a new number (moved between folders)
    w.job("inbox")
    w.restart()
    FakeIMAP.messages[3] = raw
    w.job("inbox")
    with w.ctx.db.session() as s:
        rows = s.execute(select(InboundEmail).where(InboundEmail.from_email == DAVID)).scalars().all()
        answers = s.execute(select(FollowUp).where(FollowUp.kind == "response")).scalars().all()
    assert len(rows) == 1 and len(answers) == 1
    sweep(w, network_attempts)


def test_the_same_calendly_notice_twice_is_one_meeting(make_ctx, monkeypatch, network_attempts):
    w = World(make_ctx, monkeypatch)
    through_first_send(w)
    signed, records, _ = booked_notice()
    w.calendly(signed, monkeypatch, records)
    w.calendly(signed, monkeypatch, records)
    w.restart()
    w.calendly(signed, monkeypatch, records)
    with w.ctx.db.session() as s:
        assert s.execute(select(func.count(Meeting.id))).scalar_one() == 1
        assert s.execute(select(func.count(InboundEmail.id)).where(InboundEmail.from_email == "notifications@calendly.com")).scalar_one() == 1
    sweep(w, network_attempts)


def test_the_same_inbound_form_twice_is_one_lead(make_ctx, monkeypatch, network_attempts):
    w = World(make_ctx, monkeypatch, inbound_api_token="t" * 32)
    MX["quarry-labs.example"] = True
    form = {"email": "riley@quarry-labs.example", "name": "Riley Park", "company": "Quarry Labs",
            "website": "https://quarry-labs.example", "message": "We want to automate our invoice entry. Can we talk?"}
    hdr = {"x-inbound-token": "t" * 32, **ORIGIN}
    r1 = w.client.post("/api/inbound", json=form, headers=hdr)
    r2 = w.client.post("/api/inbound", json=form, headers=hdr)
    assert (r1.status_code, r2.status_code) == (201, 200) and r1.json()["id"] == r2.json()["id"]
    w.job("inbound", ("success", "partial"))
    w.client.post("/api/inbound", json=form, headers=hdr)
    w.job("inbound", ("success", "partial"))
    with w.ctx.db.session() as s:
        assert s.execute(select(func.count(InboundInquiry.id))).scalar_one() == 1
        riley = s.execute(select(Contact).where(Contact.email == "riley@quarry-labs.example")).scalars().all()
        assert len(riley) == 1
        assert s.execute(select(func.count(Lead.id)).where(Lead.contact_id == riley[0].id)).scalar_one() == 1
    sweep(w, network_attempts)


def test_two_replies_in_a_row_leave_one_answer_waiting(make_ctx, monkeypatch, network_attempts):
    w = World(make_ctx, monkeypatch)
    w.conversation_mode("drafts")
    through_first_send(w)
    FakeIMAP.messages[1] = mail(f"David Chen <{DAVID}>", "Re: note", "Sounds interesting.", in_reply_to=w.thread,
                                msg_id="<r1@brightline-logistics.example>")
    FakeIMAP.messages[2] = mail(f"David Chen <{DAVID}>", "Re: note", "Also: could we talk Thursday?", in_reply_to=w.thread,
                                msg_id="<r2@brightline-logistics.example>")
    w.job("inbox")
    waiting = w.followups(to_email=DAVID, status="awaiting_approval")
    assert len(waiting) == 1, [(f.id, f.kind, f.subject) for f in waiting]
    for f in w.followups(to_email=DAVID, status="awaiting_approval"):
        w.approve(f.id)
    forward(timedelta(minutes=5))
    w.send()
    assert sends_to(DAVID) == 2
    sweep(w, network_attempts)


# --------------------------------------------------------------------------------------------------- the kill switch

def test_the_emergency_stop_pressed_during_a_sending_run_stops_the_rest_of_it(make_ctx, monkeypatch, network_attempts):
    w = World(make_ctx, monkeypatch)
    drafted_and_approved(w)
    other = second_prospect(w)
    w.job("inbox")
    real = FakeSMTP.send_message
    pressed = {"done": False}

    def press_after_first(self, msg):
        real(self, msg)
        if not pressed["done"]:
            pressed["done"] = True
            with w.ctx.db.session() as s:  # the owner presses Emergency stop while the run is between two emails
                automation.change_state(s, "emergency_stop", "owner", "e2e")
    monkeypatch.setattr(FakeSMTP, "send_message", press_after_first)
    w.job("followups", ("success", "partial"))
    assert pressed["done"] and len(FakeSMTP.sent) == 1, [m["To"] for m in FakeSMTP.sent]
    left = [f for f in w.followups(status="approved")]
    assert len(left) == 1 and left[0].to_email in (DAVID, OTHER) and other  # the second stays approved, unsent
    assert runner.run_job(w.ctx, "followups")["status"] == "skipped"
    with w.ctx.db.session() as s:
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "state_emergency_stop")).scalar_one()
    sweep(w, network_attempts)


def test_pid_helpers_see_this_process():
    assert runner.pid_alive(os.getpid())
