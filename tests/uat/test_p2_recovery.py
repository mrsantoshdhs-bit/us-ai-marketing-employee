"""Phase 2, recovery: things failing on a real PC. Power loss, a real restore, damaged files, a wrong clock, the watchdog.

test_defect_*: asserts the correct behaviour and FAILS on the current code. test_ok_*: verified, and not covered elsewhere.
Synthetic data only; every mail server is a fake. Real kills use a child process, so they run on Linux as well.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

import aime.timeutil
from aime import cli as aime_cli
from aime import mail_accounts, monitoring
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.backup import backup_sqlite_file, verify_backup
from aime.config import Settings
from aime.context import create_context
from aime.doctor import run_checks
from aime.integrations.email_smtp import EmailSender
from aime.models import Alert, Company, Contact, FollowUp, InboundEmail, Lead, MailAccount, Suppression
from aime.scheduler import runner
from aime.scheduler.runner import FileLock, run_job, tick
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.workflows import JobOutcome

ROOT = Path(__file__).resolve().parents[2]
POSTAL = "1 Test Way, Testville, TX 75001"
MAIL = dict(outbound_email_enabled=True, smtp_host="fake.invalid", smtp_from="sales@us.example", company_postal_address=POSTAL,
            smtp_username="", require_human_approval=True, alert_toast=False)


def script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    cwd = os.getcwd()
    spec.loader.exec_module(module)
    os.chdir(cwd)  # dashboard_task changes to the project folder on import
    return module


class FakeSMTP:
    sent: list = []
    on_connect = None
    on_send = None

    def __init__(self, host, port, timeout=30):
        if FakeSMTP.on_connect:
            FakeSMTP.on_connect()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        return 220, b"ok"

    def send_message(self, msg, *a, **k):
        if FakeSMTP.on_send:
            FakeSMTP.on_send()
        FakeSMTP.sent.append(msg["To"])
        return {}


@pytest.fixture(autouse=True)
def _fresh_mail_and_no_task_scheduler(monkeypatch):
    from aime import notify
    from aime.scheduler import windows

    monkeypatch.setattr(windows, "query", lambda *a, **k: {"installed": False, "error": "uat"})
    monkeypatch.setattr(notify, "has_desktop", lambda: False)  # no real notification on the PC running the tests
    FakeSMTP.sent, FakeSMTP.on_connect, FakeSMTP.on_send = [], None, None


def approved_email(ctx, tag="a") -> int:
    with ctx.db.session() as s:
        co = Company(name=f"Synth {tag}", normalized_name=f"synth {tag}", source="test", domain=f"{tag}.example")
        s.add(co)
        s.flush()
        ct = Contact(company_id=co.id, email=f"p@{tag}.example", first_name="Pat", consent_status="unknown", source="manual")
        s.add(ct)
        s.flush()
        ld = Lead(company_id=co.id, contact_id=ct.id, source="inbound", status="qualified", score=70)
        s.add(ld)
        s.flush()
        mid = f"<q-{tag}@{tag}.example>"
        s.add(InboundEmail(mailbox_uid=tag, message_id=mid, from_email=f"p@{tag}.example", status="actioned", subject="Q",
                           body_text="Q", new_text="Q"))
        f = FollowUp(lead_id=ld.id, company_id=co.id, contact_id=ct.id, channel="email", step=1, status="approved", approved_by="owner",
                     approved_at=utcnow(), due_at=utcnow() - timedelta(hours=1), kind="response", to_email=f"p@{tag}.example",
                     in_reply_to=mid, subject=f"Re: {tag}", body=f"Hello.\n\nTo opt out, reply unsubscribe.\n{POSTAL}")
        s.add(f)
        s.flush()
        return f.id


def send_run(ctx):
    return SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run({"now": utcnow().isoformat()})


def literal_restore(ctx, backup: Path) -> None:
    """docs/OPERATIONS.md, Backups and restore: delete -wal and -shm, copy the backup over data/aime.db."""
    data = ctx.settings.data_dir
    ctx.db.dispose()
    for side in ("aime.db-wal", "aime.db-shm"):
        (data / side).unlink(missing_ok=True)
    shutil.copyfile(backup, data / "aime.db")


def stub_jobs(monkeypatch) -> list[str]:
    calls: list[str] = []

    def job(name):
        return lambda ctx, job=name: (calls.append(name), JobOutcome())[1]
    monkeypatch.setattr(runner, "JOBS", {k: job(k) for k in runner.JOBS})
    return calls


def restore_section() -> str:
    text = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    return text[text.index("## Backups and restore"):text.index("## Scheduler recovery")]


# ---------------------------------------------------------------------------------------- a real restore
def test_defect_an_email_sent_after_the_backup_is_not_sent_again_after_a_restore(make_ctx, tmp_path):
    """R1. Approve, nightly backup, send, the disk dies, the documented restore, the next tick: the customer gets it twice."""
    ctx = make_ctx(**MAIL)
    send_run(ctx)  # sending has been on all along
    approved_email(ctx)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), tmp_path / "keep", label="nightly")
    send_run(ctx)
    assert FakeSMTP.sent == ["p@a.example"]

    literal_restore(ctx, backup)
    restored = make_ctx(**MAIL, data_dir=ctx.settings.data_dir)
    send_run(restored)
    assert FakeSMTP.sent == ["p@a.example"], "the restored 'approved' row went out a second time, with no warning to anyone"


def test_defect_the_restore_procedure_says_to_stop_the_dashboard():
    """R2. With the dashboard running, the -wal cannot be deleted on Windows but the copy succeeds: the old WAL lies over the
    restored database and the integrity check fails. The runbook never mentions the dashboard."""
    assert re.search(r"(stop|end|close|quit)\b.{0,60}dashboard|dashboard.{0,60}\b(stop|end|close)", restore_section(), re.I | re.S)


def test_defect_the_restore_procedure_warns_about_emails_and_mailbox_passwords():
    """R1, R6. A person restoring must know what comes back to life, and what a new PC cannot read."""
    section = restore_section()
    assert re.search(r"approved|sent again|re-?send|twice", section, re.I), "nothing warns that approved emails go out again"
    assert re.search(r"dpapi|another pc|new pc|mailbox password|re-?enter", section, re.I), "nothing about secrets on another PC"


@pytest.mark.parametrize("args", [["maintenance", "on", "--hours", "1", "--reason", "restore"], ["doctor"], ["restore-drill"]],
                         ids=["maintenance_on", "doctor", "restore_drill"])
def test_defect_the_restore_commands_run_when_the_database_is_damaged(args, tmp_path, monkeypatch, capsys):
    """R3. The restore starts with `maintenance on`; all commands open the live database first and die with a traceback."""
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    good = sqlite3.connect(data / "backups" / "aime-auto-20261030-061500.db")
    good.execute("CREATE TABLE t(x)")
    good.execute("INSERT INTO t VALUES (1)")
    good.commit()
    good.close()
    (data / "aime.db").write_bytes(os.urandom(8192))
    monkeypatch.setenv("AIME_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.setenv("AIME_DATA_DIR", str(data))
    monkeypatch.setenv("AIME_DATA_ROLE", "test")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code = aime_cli.main(args)  # must not raise
    assert isinstance(code, int)
    if args[0] == "maintenance":
        assert (data / "MAINTENANCE.json").exists()
    if args[0] == "doctor":
        assert code == 1 and re.search(r"damaged|integrity|not a database", capsys.readouterr().out, re.I)


def test_defect_a_missing_database_is_not_silently_replaced_by_an_empty_one(make_ctx):
    """R5. aime.db gone, backups present: doctor exits 0, ticks run, /login offers to create the first account, the do-not-contact
    list is gone and research starts over. It must refuse or fail loudly."""
    ctx = make_ctx(data_role="production")
    with ctx.db.session() as s:
        s.add(Suppression(value="gone@example.com", kind="email", reason="unsubscribed"))
    backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="auto")
    ctx.db.dispose()
    data = ctx.settings.data_dir
    for name in ("aime.db", "aime.db-wal", "aime.db-shm"):
        (data / name).unlink(missing_ok=True)
    settings = Settings.load(env_file=data / "no.env", data_dir=data, database_url="", data_role="production", llm_provider="template")
    try:
        again = create_context(settings)
    except Exception:
        return  # refusing is right
    try:
        failures = [c for c in run_checks(again) if not c["ok"] and c["severity"] == "error"]
        assert failures, "doctor passes on an empty production database while backups exist"
    finally:
        again.db.dispose()


def test_defect_a_mailbox_secret_from_another_pc_does_not_break_the_pages(make_ctx, monkeypatch):
    """R6. Restored on a new PC (or after a Windows password reset) DPAPI cannot read the secret: Follow-ups, Integrations, System and
    the Mailboxes page, where it would be fixed, answer 500."""
    ctx = make_ctx(**MAIL)
    with ctx.db.session() as s:
        s.add(MailAccount(label="Synth", provider="gmail", auth="app_password", email="s@us.example", username="s@us.example",
                          imap_host="fake.invalid", smtp_host="fake.invalid", secret="dpapi:AAAA", active=True, read_replies=True,
                          is_primary=True))

    def cannot_read(_stored):
        raise OSError("[WinError -2146893813] Key not valid for use in specified state")
    monkeypatch.setattr(mail_accounts, "unprotect", cannot_read)
    client = TestClient(create_app(ctx), raise_server_exceptions=False, base_url="http://testserver")
    codes = {p: client.get(p).status_code for p in ("/followups", "/integrations", "/system", "/admin/mailboxes")}
    assert all(c < 500 for c in codes.values()), codes


def test_defect_a_mailbox_secret_from_another_pc_tells_a_person_what_to_do(make_ctx, monkeypatch):
    """R6. The job dies with "OSError: [WinError ...]"; no alert says the mailbox password must be entered again."""
    ctx = make_ctx(**MAIL)
    with ctx.db.session() as s:
        s.add(MailAccount(label="Synth", provider="gmail", auth="app_password", email="s@us.example", username="s@us.example",
                          imap_host="fake.invalid", smtp_host="fake.invalid", secret="dpapi:AAAA", active=True, read_replies=True,
                          is_primary=True))

    def cannot_read(_stored):
        raise OSError("[WinError -2146893813] Key not valid for use in specified state")
    monkeypatch.setattr(mail_accounts, "unprotect", cannot_read)
    run_job(ctx, "followups")
    with ctx.db.session() as s:
        said = [a.message for a in s.execute(select(Alert)).scalars()]
    assert any(re.search(r"mailbox.*(password|sign|enter)", m, re.I) for m in said), said


# ---------------------------------------------------------------------------------------- a damaged database
def damage_table(ctx, table: str) -> None:
    db = Path(ctx.db.engine.url.database)
    ctx.db.dispose()
    con = sqlite3.connect(db)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    root = con.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    size = con.execute("PRAGMA page_size").fetchone()[0]
    con.close()
    with open(db, "r+b") as fh:
        fh.seek((root - 1) * size)
        fh.write(b"\xff" * size)


def test_defect_nothing_is_sent_from_a_database_that_failed_its_integrity_check(make_ctx):
    """R4. Interior damage: doctor says FAIL, and the next run still sends. The records behind a send (opt-outs, what was sent) cannot
    be trusted until a person has looked."""
    ctx = make_ctx(**MAIL)
    send_run(ctx)
    approved_email(ctx)
    damage_table(ctx, "signals")
    assert any(c["name"] == "SQLite integrity" and not c["ok"] for c in run_checks(ctx))
    run_job(ctx, "monitor")
    send_run(ctx)
    assert FakeSMTP.sent == []


def test_defect_doctor_notices_that_the_newest_backup_has_gone_bad(make_ctx):
    """R13. "Recent verified backup" reads the record of yesterday's check, and that the file exists. A file damaged since passes."""
    ctx = make_ctx()
    assert run_job(ctx, "db_maintenance")["status"] == "success"
    newest = sorted(ctx.settings.backups_dir.glob("aime-auto-*.db"))[-1]
    with open(newest, "r+b") as fh:
        fh.seek(4096)
        fh.write(os.urandom(4096 * 4))
    assert not verify_backup(newest)["ok"]
    check = next(c for c in run_checks(ctx) if c["name"] == "Recent verified backup")
    assert not check["ok"], check


# ---------------------------------------------------------------------------------------- locks left behind
@pytest.fixture
def live_stranger():
    """A running program that has nothing to do with AIME, started after the lock was written: a PID Windows gave out again."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield p.pid
    p.kill()
    p.wait()


def old_lock(path: Path, text: str, minutes: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - minutes * 60
    os.utime(path, (stamp, stamp))


def test_defect_a_reused_pid_does_not_stop_ticks_for_90_minutes(make_ctx, monkeypatch, live_stranger):
    """R7. After a power cut, tick.lock names a PID now used by another program: every tick is skipped until the lock is 90 minutes
    old, and the skipped tick writes no heartbeat and no reason. The lock's own time is older than that program."""
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    time.sleep(0.2)
    from datetime import datetime
    written = (datetime.now() - timedelta(minutes=5)).isoformat()
    old_lock(ctx.settings.locks_dir / "tick.lock", f"{live_stranger} {written}")
    result = tick(ctx, trigger="scheduler")
    assert result["status"] == "ok" and calls


def test_defect_a_reused_pid_does_not_stop_the_inbox_for_two_hours(make_ctx, monkeypatch, live_stranger):
    """R7. job_inbox.lock with a reused PID: replies and opt-outs go unread for two hours, recorded nowhere."""
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    from datetime import datetime
    written = (datetime.now() - timedelta(minutes=5)).isoformat()
    old_lock(ctx.settings.locks_dir / "job_inbox.lock", f"{live_stranger} {written}")
    assert run_job(ctx, "inbox")["status"] == "success" and calls == ["inbox"]


@pytest.mark.parametrize("content", ["", "\0" * 32], ids=["empty", "nul_bytes"])
def test_defect_a_lock_file_emptied_by_a_power_cut_does_not_stop_ticks(make_ctx, monkeypatch, content):
    """R7. A power cut between creating the lock and writing it (or NTFS zero-filling it): it counts as "owned" for 90 minutes."""
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    old_lock(ctx.settings.locks_dir / "tick.lock", content)
    assert tick(ctx, trigger="scheduler")["status"] == "ok" and calls


# ---------------------------------------------------------------------------------------- files that cannot be written
def test_defect_a_heartbeat_that_cannot_be_written_does_not_stop_the_work(make_ctx, monkeypatch):
    """R8. Read-only or held by another program (antivirus, a sync tool), or a full disk: the tick dies before its first job."""
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    real = Path.write_text

    def refuse(self, *a, **k):
        if self.name == "heartbeat.json":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", refuse)
    assert tick(ctx, trigger="scheduler")["status"] == "ok" and calls


def test_defect_the_watchdog_still_notifies_when_its_log_cannot_be_written(tmp_path, monkeypatch):
    """R9. On a full disk the log line comes first, raises, and the notification, the one thing still able to tell the owner, never
    shows. Ticks are stopped by then (R8), so nobody hears of a full disk at all."""
    watchdog = script("watchdog")
    shown = []
    from aime import notify
    monkeypatch.setattr(notify, "toast", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(watchdog, "DATA", tmp_path)
    monkeypatch.setattr(watchdog, "dashboard_problem", lambda **k: None)
    (tmp_path / "logs" / "watchdog.log").mkdir(parents=True)  # open() for append fails, as it does on a full disk
    try:
        watchdog.main()
    except OSError:
        pass
    assert shown, "no notification"


def test_defect_a_backup_that_failed_on_a_full_disk_leaves_no_file_behind(make_ctx, monkeypatch):
    """R10. Each failed attempt (three a day) leaves a file named like a backup, on a disk that is already full."""
    ctx = make_ctx()
    import aime.backup as b
    real = sqlite3.connect

    def small_disk(path, *a, **k):
        con = real(path, *a, **k)
        if "backups" in str(path):
            con.execute("PRAGMA max_page_count=1")
        return con
    monkeypatch.setattr(b.sqlite3, "connect", small_disk)
    with pytest.raises(sqlite3.OperationalError):
        backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="auto")
    assert list(ctx.settings.backups_dir.glob("aime-auto-*")) == []


def test_defect_a_locked_database_at_the_start_of_a_tick_does_not_lose_the_tick(make_ctx, monkeypatch):
    """R11. recover() always writes. A lock held past the 30 s timeout at that moment ends the whole tick with no job run, although
    jobs later in the same tick are protected from exactly this."""
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)

    def locked(*a, **k):
        raise OperationalError("UPDATE agent_runs", {}, sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(runner, "recover", locked)
    assert tick(ctx, trigger="scheduler")["status"] == "ok" and calls


@pytest.mark.parametrize("content", ['{"at": "garbage"}', '{"state": "idle"}', "[1, 2]", '{"at": 5}'],
                         ids=["bad_date", "no_at", "a_list", "a_number"])
def test_defect_a_heartbeat_of_the_wrong_shape_breaks_nothing(make_ctx, content):
    """R14. Today, System, Integrations and Jobs answer 500, doctor raises (so the monitor job fails), the watchdog raises."""
    ctx = make_ctx()
    (ctx.settings.data_dir / "heartbeat.json").write_text(content, encoding="utf-8")
    assert run_checks(ctx)
    assert isinstance(script("watchdog").check(ctx.settings.data_dir), list)
    client = TestClient(create_app(ctx), raise_server_exceptions=False, base_url="http://testserver")
    codes = {p: client.get(p).status_code for p in ("/", "/system", "/integrations", "/jobs")}
    assert all(c < 500 for c in codes.values()), codes


# ---------------------------------------------------------------------------------------- the clock
def test_defect_a_clock_a_year_ahead_is_noticed_before_the_work_runs(make_ctx, monkeypatch):
    """R12. On a copy of real data, one tick with the PC's clock a year ahead moved 37 of 40 qualified leads to nurture. Correcting the
    clock did not bring them back, no alert named the clock, and doctor passed."""
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    assert tick(ctx, trigger="scheduler")["status"] == "ok"
    calls.clear()
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET + timedelta(days=365))
    tick(ctx, trigger="scheduler")
    with ctx.db.session() as s:
        said = [a.message for a in s.execute(select(Alert)).scalars() if re.search(r"\bclock\b", a.message, re.I)]
    assert said, "nobody is told; the only record says the PC may have been asleep for 8760 h"
    assert calls == [], "the jobs ran on the wrong date"


def test_ok_the_clock_going_back_an_hour_sends_nothing_twice(make_ctx, monkeypatch):
    ctx = make_ctx(**MAIL)
    send_run(ctx)
    approved_email(ctx)
    send_run(ctx)
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET - timedelta(hours=1))
    send_run(ctx)
    assert FakeSMTP.sent == ["p@a.example"]


def test_defect_an_email_that_finished_sending_after_a_sleep_keeps_no_false_warning(make_ctx, monkeypatch):
    """R15. The PC sleeps inside the SMTP conversation; another run marks the email uncertain; on waking the send completes and it
    becomes sent, still carrying "Sending was interrupted; check your Sent folder"."""
    ctx = make_ctx(**MAIL)
    send_run(ctx)
    fid = approved_email(ctx)

    def slept():  # at the handing over: a sleep before it now stops the send (see test_p4_pc)
        FakeSMTP.on_send = None
        monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET + timedelta(minutes=30))
        send_run(ctx)
    FakeSMTP.on_send = slept
    send_run(ctx)
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        assert f.status == "sent" and FakeSMTP.sent == ["p@a.example"]
        assert not f.blocked_reason, f.blocked_reason


# ---------------------------------------------------------------------------------------- watchdog and dashboard start
def test_defect_the_watchdog_sees_a_port_held_by_something_that_never_answers(tmp_path):
    """R16. Another program on the port, or a hung dashboard: TCP is accepted, no page ever comes. dashboard_problem says all is well,
    dashboard_task exits quietly every time, so the dashboard stays down with no notification."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    try:
        assert script("watchdog").dashboard_problem(port=server.getsockname()[1]) is not None
    finally:
        server.close()


def test_defect_a_dashboard_that_cannot_read_its_settings_leaves_a_trace(tmp_path, monkeypatch):
    """R17. Under pythonw the output goes to the log only after the settings have loaded: a bad value in .env is written nowhere, so
    the task fails every 15 minutes with nothing to read. run_tick.py writes scheduler_crash.log in this case."""
    task = script("dashboard_task")
    monkeypatch.setenv("AIME_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(task, "ROOT", tmp_path)
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, *a, **k: (_ for _ in ()).throw(ValueError("bad value in .env"))))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    try:
        task.main()
    except Exception:
        pass
    finally:
        monkeypatch.undo()
    logs = [p for p in tmp_path.rglob("*.log")]
    assert any("bad value" in p.read_text(encoding="utf-8", errors="replace") for p in logs), logs


def test_defect_two_processes_starting_on_a_pending_migration_both_survive(make_ctx, monkeypatch):
    """R18. After a deploy the tick and the dashboard start together. On a copy the loser failed 5 times out of 5 with
    "duplicate column name" and exited; here the other process's ALTER lands between the check and this one's."""
    from aime import migrations
    ctx = make_ctx()
    real = migrations.pending_columns
    from aime.models import FollowUp as F
    col = F.__table__.columns["send_started_at"]
    monkeypatch.setattr(migrations, "pending_columns", lambda engine: [("followups", col)] if real(engine) == [] else real(engine))
    migrations.migrate(ctx.db.engine, ctx.settings.backups_dir, has_data=False)


# ---------------------------------------------------------------------------------------- volume
def test_defect_requalifying_every_lead_does_not_hold_the_write_lock_for_the_whole_run(make_ctx):
    """R19. Every lead is rescored in one transaction. Measured on real concurrent processes: longest lock 1.6 s at 297 leads, 27 s at
    6,000, 137 s at 30,000. From about 6,500 leads every click during crm_maintenance waits 30 s and gets "The database is busy"."""
    from sqlalchemy import event

    from aime.agents.qualification import LeadQualificationAgent

    ctx = make_ctx()
    n = 120
    with ctx.db.session() as s:
        for i in range(n):
            co = Company(name=f"Synth {i}", normalized_name=f"synth {i}", source="test", domain=f"s{i}.example")
            s.add(co)
            s.flush()
            s.add(Lead(company_id=co.id, source="inbound", status="new", score=0))
    in_tx, most = {"n": 0}, {"n": 0}

    def count(conn, cursor, statement, params, context, many):
        if statement.startswith("UPDATE leads"):
            in_tx["n"] += 1
            most["n"] = max(most["n"], in_tx["n"])

    def committed(conn):
        in_tx["n"] = 0
    event.listen(ctx.db.engine, "before_cursor_execute", count)
    event.listen(ctx.db.engine, "commit", committed)
    assert LeadQualificationAgent(ctx).run({}).ok
    assert most["n"] > 0
    assert most["n"] < n, f"{most['n']} leads updated in one transaction"


# ---------------------------------------------------------------------------------------- real kills
CHILD = textwrap.dedent("""
    import json, os, sys
    from datetime import datetime, timezone
    from pathlib import Path
    import aime.timeutil
    now = datetime.fromisoformat(sys.argv[3])
    aime.timeutil.CLOCK_OFFSET = now - datetime.now(timezone.utc).replace(tzinfo=None)
    from aime.config import Settings
    from aime.context import create_context
    from aime.integrations.email_smtp import EmailSender
    from aime.agents.sales_workflow import SalesWorkflowAgent
    data, point = Path(sys.argv[1]), sys.argv[2]
    st = Settings.load(env_file=data / "no.env", data_dir=data, database_url="", data_role="test", llm_provider="template",
                       outbound_email_enabled=True, smtp_host="fake.invalid", smtp_from="sales@us.example", smtp_username="",
                       company_postal_address="1 Test Way, Testville, TX 75001", alert_toast=False)
    ctx = create_context(st)
    class SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None): return 220, b"ok"
        def send_message(self, msg, *a, **k):
            with open(data / "world.txt", "a") as fh:
                fh.write(msg["To"] + "\\n"); fh.flush(); os.fsync(fh.fileno())
            if point == "after_data": os._exit(9)
            return {}
    if point == "before_commit":
        import aime.agents.sales_workflow as sw
        sw.record_delivery = lambda *a, **k: os._exit(9)
    SalesWorkflowAgent(ctx, sender=EmailSender(st, smtp_factory=SMTP)).run({"now": now.isoformat()})
""")


@pytest.mark.parametrize("point", ["after_data", "before_commit"])
def test_ok_a_process_killed_during_a_send_never_sends_it_again_and_a_person_is_told(make_ctx, tmp_path, point):
    ctx = make_ctx(**MAIL)
    fid = approved_email(ctx, tag="k1")
    other = approved_email(ctx, tag="k2")
    data = ctx.settings.data_dir
    ctx.db.dispose()
    child = tmp_path / "child.py"
    child.write_text(CHILD, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    start = utcnow()

    def run(p, minutes):
        return subprocess.run([sys.executable, str(child), str(data), p, (start + timedelta(minutes=minutes)).isoformat()], env=env,
                              capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert run(point, 0).returncode == 9
    world = (data / "world.txt").read_text().split()
    assert len(world) == 1
    with sqlite3.connect(data / "aime.db") as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT status FROM followups WHERE id=?", (fid,)).fetchone()[0] in ("sending", "approved")
    assert run("none", 5).returncode == 0
    assert run("none", 15).returncode == 0
    assert sorted((data / "world.txt").read_text().split()) == sorted({*world, "p@k1.example", "p@k2.example"})
    with sqlite3.connect(data / "aime.db") as con:
        assert con.execute("SELECT status FROM followups WHERE id=?", (other,)).fetchone()[0] in ("sent", "uncertain")
        first = con.execute("SELECT status, blocked_reason FROM followups WHERE id=?", (fid,)).fetchone()
        assert first[0] == "uncertain" and "Sent folder" in first[1]
        assert con.execute("SELECT count(*) FROM alerts WHERE message LIKE ?", (f"Follow-up #{fid}:%",)).fetchone()[0] == 1


def test_ok_a_migration_stopped_between_two_columns_finishes_on_the_next_start(make_ctx):
    from sqlalchemy import event

    ctx = make_ctx()
    db = Path(ctx.db.engine.url.database)
    ctx.db.dispose()
    con = sqlite3.connect(db)
    for table, col in (("followups", "send_started_at"), ("activities", "body"), ("contacts", "email_source_url")):
        con.execute(f'ALTER TABLE "{table}" DROP COLUMN "{col}"')
    con.commit()
    con.close()
    from sqlalchemy.engine import Engine
    seen = {"n": 0}

    class Killed(BaseException):
        pass

    def stop(conn, cursor, statement, params, context, many):
        if statement.startswith("ALTER TABLE"):
            seen["n"] += 1
            if seen["n"] == 2:
                raise Killed
    event.listen(Engine, "before_cursor_execute", stop)
    settings = Settings.load(env_file=db.parent / "no.env", data_dir=db.parent, database_url="", data_role="test", llm_provider="template")
    try:
        with pytest.raises(Killed):
            create_context(settings)
    finally:
        event.remove(Engine, "before_cursor_execute", stop)
    again = create_context(settings)
    try:
        with again.db.session() as s:
            assert s.execute(select(func.count(FollowUp.id))).scalar_one() == 0
    finally:
        again.db.dispose()
    with sqlite3.connect(db) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert "send_started_at" in [r[1] for r in con.execute("PRAGMA table_info(followups)")]


def test_ok_a_heartbeat_cut_short_by_a_power_cut_does_no_harm(make_ctx, monkeypatch):
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    for content in (b"", b"\0" * 300, b'{"at": "2026-10-3'):
        (ctx.settings.data_dir / "heartbeat.json").write_bytes(content)
        assert run_checks(ctx)
        assert "unreadable" in script("watchdog").check(ctx.settings.data_dir)[0]
        assert tick(ctx, trigger="scheduler")["status"] == "ok"
        assert json.loads((ctx.settings.data_dir / "heartbeat.json").read_text(encoding="utf-8"))["state"] == "idle"
    assert calls


def test_ok_a_lock_of_a_dead_process_is_taken_over_at_once(make_ctx, monkeypatch):
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    p = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    old_lock(ctx.settings.locks_dir / "tick.lock", f"{int(p.stdout)} 2026-10-31T00:00:00", minutes=0)
    assert tick(ctx, trigger="scheduler")["status"] == "ok" and calls
    assert isinstance(monitoring.read_heartbeat(ctx.settings), dict) and FileLock(ctx.settings.locks_dir / "tick.lock").acquire()
