"""A1, running while the owner is signed out: each check-in records the Windows session it ran in, nothing tries to
show a desktop notification in the background session, the read-only preflight explains this PC's settings, the
on-demand test task touches no production task, and Manager Today shows one honest status per part of the
automation. Nothing here installs or changes a real scheduled task: schtasks and PowerShell are replaced."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aime import automation as am
from aime import maintenance, monitoring, notify, session_info
from aime.db import kv_set
from aime.models import AgentRun, JobState
from aime.scheduler import windows
from aime.timeutil import utcnow
from aime.web.app import ai_employee, create_app, manager_today

ROOT = Path(__file__).resolve().parent.parent
PY, SCRIPT, WORK = Path(r"C:\p\pythonw.exe"), Path(r"C:\p\scripts\run_tick.py"), Path(r"C:\p")


def xml(**kw):
    return windows.build_task_xml(PY, SCRIPT, WORK, 15, "PC\\me", start=datetime(2026, 10, 28, 8, 0), **kw)


# --------------------------------------------------------------------------- the session each run happened in

def test_session_zero_is_the_background_session(monkeypatch):
    assert session_info.session_kind(0) == "background" and session_info.session_kind(1) == "interactive"
    monkeypatch.setattr(session_info, "session_id", lambda pid=None: None)
    assert session_info.session_kind() == "unknown" and session_info.has_desktop()
    monkeypatch.setattr(session_info, "session_id", lambda pid=None: 0)
    assert not session_info.has_desktop()


def test_each_check_in_records_its_session_and_keeps_the_last_background_run(make_ctx, monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(monitoring, "session_kind", lambda: "background")
    monitoring.write_heartbeat(ctx.settings, {"state": "idle"})
    first = monitoring.read_heartbeat(ctx.settings)
    assert first["session"] == "background" and first["last_background_at"] == first["at"]
    monkeypatch.setattr(monitoring, "session_kind", lambda: "interactive")
    monitoring.write_heartbeat(ctx.settings, {"state": "idle"})
    second = monitoring.read_heartbeat(ctx.settings)
    assert second["session"] == "interactive" and second["last_background_at"] == first["at"]


def test_the_scheduler_entry_point_keeps_the_last_background_run_during_maintenance(tmp_path):
    (tmp_path / "heartbeat.json").write_text(json.dumps({"at": "x", "last_background_at": "2026-10-28T03:00:00Z"}))
    maintenance.start(tmp_path, 1, "test")
    env = {**os.environ, "AIME_DATA_DIR": str(tmp_path), "AIME_DATA_ROLE": "test"}
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_tick.py")], env=env, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    beat = json.loads((tmp_path / "heartbeat.json").read_text())
    assert beat["state"] == "maintenance" and beat["last_background_at"] == "2026-10-28T03:00:00Z"
    assert beat["session"] in ("interactive", "unknown", "background")


def test_no_desktop_notification_is_attempted_in_the_background_session(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "has_desktop", lambda: False)
    assert notify.toast("t", "m", popen=lambda *a, **k: calls.append(a)) is False and calls == []


# --------------------------------------------------------------------------- task definitions

def test_an_on_demand_task_has_no_triggers_and_never_contains_a_password():
    body = xml(logon_type="Password", on_demand_only=True, task_name=windows.TEST_TASK)
    assert "<Triggers>" not in body and "<LogonType>Password</LogonType>" in body and "Password>" not in body.replace(
        "<LogonType>Password</LogonType>", "")
    assert "<Triggers>" in xml() and "<BootTrigger>" in xml(logon_type="Password")


def test_readiness_lists_settings_that_would_stop_or_duplicate_runs():
    bad = (xml().replace("<DisallowStartIfOnBatteries>false", "<DisallowStartIfOnBatteries>true")
           .replace("<StartWhenAvailable>true", "<StartWhenAvailable>false")
           .replace("<MultipleInstancesPolicy>IgnoreNew", "<MultipleInstancesPolicy>Parallel"))
    lines = windows.unattended_readiness({"Tick": bad})
    assert "Tick: does not start, or stops, on battery power." in lines
    assert "Tick: a run missed while the PC was off or asleep is not made up." in lines
    assert "Tick: may start a second copy while one is running (Parallel)." in lines
    good = windows.task_conditions(xml())
    assert good == {"on_batteries": True, "keeps_running_on_batteries": True, "catches_up": True, "needs_network": False,
                    "needs_idle": False, "multiple_instances": "IgnoreNew", "time_limit": "PT1H", "restarts": 3}


# --------------------------------------------------------------------------- read-only preflight

THIS_PC = {"account_source": "Local", "password_set": True, "admin_member": True, "limit_blank_password": 1,
           "wake_timers_ac": 1, "wake_timers_dc": 0, "sleep_ac": 0, "sleep_dc": 0, "modern_standby": True,
           "fast_startup": 1, "on_wifi": True, "wifi_all_users": True, "task_history": False, "battery": True}


def test_preflight_explains_this_pcs_settings():
    found = dict((text.split(":")[0].split(".")[0], level) for level, text in windows.evaluate_preflight(THIS_PC))
    levels = [level for level, _ in windows.evaluate_preflight(THIS_PC)]
    assert "FAIL" not in levels and levels.count("WARN") == 1  # only: no wake timers on battery
    texts = " ".join(text for _, text in windows.evaluate_preflight(THIS_PC))
    for phrase in ("blank passwords work only at the console", "saved for all users", "Modern Standby",
                   "Wake timers are off on battery", "never goes to sleep", "cannot guarantee 24/7",
                   "Task Scheduler history is off"):
        assert phrase in texts, phrase
    assert found


@pytest.mark.parametrize("change,level,phrase", [
    (dict(password_set=False), "FAIL", "No Windows password is set"),
    (dict(admin_member=False), "WARN", "Log on as a batch job"),
    (dict(wifi_all_users=False), "WARN", "saved for your account only"),
    (dict(wake_timers_ac=0), "WARN", "Wake timers are off on mains power"),
    (dict(account_source="MicrosoftAccount"), "INFO", "Microsoft account password (not the PIN)"),
])
def test_preflight_flags_what_would_stop_unattended_runs(change, level, phrase):
    assert any(lv == level and phrase in text for lv, text in windows.evaluate_preflight({**THIS_PC, **change}))


def test_preflight_fails_plainly_without_facts_or_files():
    assert windows.evaluate_preflight({})[0][0] == "FAIL"
    assert windows.evaluate_preflight(THIS_PC, files_ok=False)[0] == (
        "FAIL", "The app's pythonw.exe or scripts/run_tick.py is missing: the task would have nothing to run.")


def test_preflight_facts_are_read_from_powershell_json(monkeypatch):
    monkeypatch.setattr(windows.os, "name", "nt")
    out = "WARNING: noise\n" + json.dumps(THIS_PC) + "\n"
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(stdout=out, returncode=0)

    assert windows.collect_preflight_facts(fake_run) == THIS_PC
    assert seen["cmd"][:3] == ["powershell", "-NoProfile", "-NonInteractive"]
    for word in ("Set-", "New-", "Remove-", "/change", "/setacvalueindex", "/Create", "/Delete"):
        assert word not in windows.PREFLIGHT_SCRIPT, word  # read-only


# --------------------------------------------------------------------------- the on-demand test task

class FakeSchtasks:
    def __init__(self, probe_path, probe=None, create_ok=True, run_ok=True):
        self.calls, self.probe_path, self.probe = [], probe_path, probe
        self.create_ok, self.run_ok = create_ok, run_ok

    def plain(self, *args):
        self.calls.append(args)
        if args[0] == "/Run" and self.run_ok and self.probe is not None:
            self.probe_path.write_text(json.dumps(self.probe), encoding="utf-8")
        ok = self.run_ok if args[0] == "/Run" else True
        return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="" if ok else "refused")

    def interactive(self, *args):
        self.calls.append(args)
        return SimpleNamespace(returncode=0 if self.create_ok else 1)


def install_fake(monkeypatch, ctx, **kw):
    fake = FakeSchtasks(ctx.settings.data_dir / "a1_probe.json", **kw)
    monkeypatch.setattr(windows, "_schtasks", fake.plain)
    monkeypatch.setattr(windows, "_schtasks_interactive", fake.interactive)
    return fake


GOOD_PROBE = {"session": "background", "session_id": 0, "dpapi": True, "database": True, "network": True, "errors": []}


def test_the_test_task_proves_a_background_run_and_is_deleted_again(make_ctx, monkeypatch):
    ctx = make_ctx()
    fake = install_fake(monkeypatch, ctx, probe=GOOD_PROBE)
    res = windows.test_unattended(ctx.settings, wait_seconds=2, sleep=lambda s: None)
    assert res["ok"] and res["probe"]["session"] == "background"
    names = {a[a.index("/TN") + 1] for a in fake.calls}
    assert names == {windows.TEST_TASK}  # the production tasks are never touched
    create = next(a for a in fake.calls if a[0] == "/Create")
    assert create[create.index("/RP") + 1] == "*" and "/RU" in create  # Windows asks the owner for the password
    assert [a[0] for a in fake.calls] == ["/Create", "/Run", "/Delete"]
    assert "<Triggers>" not in (ctx.settings.data_dir / "a1_test_task.xml").read_text(encoding="utf-16")


@pytest.mark.parametrize("probe", [dict(GOOD_PROBE, session="interactive"), dict(GOOD_PROBE, dpapi=False),
                                   dict(GOOD_PROBE, database=False)])
def test_the_test_task_fails_honestly_when_a_check_fails(make_ctx, monkeypatch, probe):
    ctx = make_ctx()
    fake = install_fake(monkeypatch, ctx, probe=probe)
    assert windows.test_unattended(ctx.settings, wait_seconds=2, sleep=lambda s: None)["ok"] is False
    assert fake.calls[-1][0] == "/Delete"


def test_the_test_task_is_deleted_even_when_it_does_not_report(make_ctx, monkeypatch):
    ctx = make_ctx()
    fake = install_fake(monkeypatch, ctx, probe=None)
    res = windows.test_unattended(ctx.settings, wait_seconds=3, sleep=lambda s: None)
    assert not res["ok"] and "did not report" in res["output"] and fake.calls[-1][0] == "/Delete"
    refused = install_fake(monkeypatch, ctx, create_ok=False)
    assert not windows.test_unattended(ctx.settings, wait_seconds=1, sleep=lambda s: None)["ok"]
    assert [a[0] for a in refused.calls] == ["/Create"]  # nothing to run or delete


def test_the_probe_checks_session_encryption_and_database(tmp_path):
    import sqlite3

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import unattended_probe
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    sqlite3.connect(tmp_path / "aime.db").execute("CREATE TABLE t (x)").connection.close()
    out = unattended_probe.probe(tmp_path)
    assert out["database"] is True and out["session"] in ("interactive", "background", "unknown")
    if os.name == "nt":
        assert out["dpapi"] is True
    assert set(out) >= {"at", "session_id", "session", "user", "dpapi", "database", "network", "errors"}


# --------------------------------------------------------------------------- Manager Today and the agents

def board(ctx):
    with ctx.db.session() as s:
        return {b["name"]: (b["status"], b["detail"]) for b in manager_today(ctx, s)["board"]}


def beat(ctx, minutes_ago=2, **info):
    at = (utcnow() - timedelta(minutes=minutes_ago)).isoformat() + "Z"
    monitoring.heartbeat_path(ctx.settings).write_text(json.dumps({"at": at, "state": "idle", **info}))
    return at


def test_the_board_says_what_is_waiting_for_authorization_or_not_configured(make_ctx):
    ctx = make_ctx(outbound_email_enabled=False, publishing_enabled=False)
    b = board(ctx)
    assert b["Scheduled jobs"][0] == "NOT CONFIGURED"
    assert b["Running while signed out"][0] == "WAITING FOR AUTHORIZATION" and "needs your approval" in b["Running while signed out"][1]
    assert b["Email sending"] == ("WAITING FOR AUTHORIZATION", "sending is switched off: emails are drafted, never "
                                  "sent; sending needs your approval")
    assert b["Mailbox reading"][0] == "NOT CONFIGURED" and "needs your approval" in b["Mailbox reading"][1]
    assert b["Meeting booking"][0] == "NOT CONFIGURED" and "nothing is booked automatically" in b["Meeting booking"][1]
    assert b["LinkedIn publishing"][0] == "WAITING FOR AUTHORIZATION" and b["Backups"][0] == "NOT CONFIGURED"
    page = TestClient(create_app(ctx)).get("/manager").text
    for text in ("WAITING FOR AUTHORIZATION", "NOT CONFIGURED", "Next scheduled check-in", "companies discovered today",
                 "contacts with a usable email", "qualified leads ready for outreach", "follow-ups scheduled"):
        assert text in page, text


def test_the_scheduler_status_follows_the_evidence(make_ctx):
    ctx = make_ctx()
    beat(ctx, session="interactive")
    b = board(ctx)
    assert b["Scheduled jobs"][0] == "ACTIVE" and b["Running while signed out"][0] == "WAITING FOR AUTHORIZATION"
    at = beat(ctx, session="background", last_background_at=None)
    beat(ctx, session="background", last_background_at=at)
    assert board(ctx)["Running while signed out"][0] == "ACTIVE"
    beat(ctx, session="interactive", last_background_at=(utcnow() - timedelta(hours=3)).isoformat() + "Z")
    assert board(ctx)["Running while signed out"][0] == "BLOCKED"
    beat(ctx, minutes_ago=90, session="interactive")
    assert board(ctx)["Scheduled jobs"][0] == "FAILED"
    maintenance.start(ctx.settings.data_dir, 1, "test")
    beat(ctx, state="maintenance")
    assert board(ctx)["Scheduled jobs"][0] == "PAUSED"
    maintenance.stop(ctx.settings.data_dir)
    beat(ctx, minutes_ago=90, state="maintenance")  # the window is over and nothing has run since: not "paused"
    assert board(ctx)["Scheduled jobs"][0] == "FAILED"
    beat(ctx)
    with ctx.db.session() as s:
        am.change_state(s, "stop", "admin")
    b = board(ctx)
    assert b["Scheduled jobs"][0] == "PAUSED" and b["AI agents"][0] == "PAUSED"


def test_sending_switched_on_without_a_password_is_blocked_not_active(make_ctx):
    from tests.test_outreach import SENDING

    ctx = make_ctx(**{**SENDING, "smtp_username": "sales@us.example", "smtp_password": ""})
    status, detail = board(ctx)["Email sending"]
    assert status == "BLOCKED" and "mailbox password is missing" in detail
    assert board(make_ctx(**{**SENDING, "smtp_username": "sales@us.example", "smtp_password": "x" * 8}))["Email sending"][0] == "ACTIVE"


def test_backups_are_active_only_when_a_recent_one_was_verified(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        kv_set(s, "backup.last_verified", {"file": "b.db", "at": utcnow().isoformat()})
    assert board(ctx)["Backups"][0] == "ACTIVE"
    with ctx.db.session() as s:
        kv_set(s, "backup.last_verified", {"file": "b.db", "at": (utcnow() - timedelta(days=3)).isoformat()})
    assert board(ctx)["Backups"][0] == "FAILED"


def test_each_agent_shows_its_pending_retries(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        s.add(AgentRun(run_id="a1-test", agent="research", job="research", status="failed", started_at=utcnow() - timedelta(hours=1),
                       finished_at=utcnow() - timedelta(hours=1), error="SEC EDGAR returned 503"))
        s.add(JobState(job="research", consecutive_failures=2, last_status="failed"))
    with ctx.db.session() as s:
        team = {a["agent"]: a for a in ai_employee(ctx, s)["team"]}
    assert team["research"]["retries"] == 2 and team["crm"]["retries"] == 0
    page = TestClient(create_app(ctx)).get("/command-center/agents/research").text
    assert "Retries pending" in page and "2 failed attempts in a row" in page
    assert board(ctx)["AI agents"][0] == "FAILED"


# --------------------------------------------------------------------------- no overlap, retries, deliberate stops

def test_a_second_tick_never_overlaps_a_running_one_and_a_dead_ones_lock_is_recovered(make_ctx):
    from aime.scheduler.runner import tick

    ctx = make_ctx()
    lock = ctx.settings.locks_dir / "tick.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()} now")  # a live owner: this process
    assert tick(ctx) == {"status": "skipped", "reason": "another tick is running", "ran": []}
    lock.write_text("999999999 long-gone")  # an owner that no longer exists (crash, sleep, kill)
    with ctx.db.session() as s:
        am.change_state(s, "stop", "admin")  # keep the recovered tick from running real jobs
    assert tick(ctx)["status"] == "ok" and not lock.exists()


def test_failed_daily_jobs_retry_with_a_growing_wait_then_stop_until_the_next_slot():
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    from aime.scheduler.runner import MAX_DAILY_RETRIES, JobSpec, is_due, retry_backoff

    spec = JobSpec("research", None, dtime(7, 0), "Research")
    now = datetime(2026, 10, 28, 9, 0)  # 09:00 UTC, after the 07:00 UTC slot
    state = JobState(job="research", enabled=True, last_attempt_at=now - timedelta(minutes=1), consecutive_failures=1)
    assert is_due(spec, state, now, ZoneInfo("UTC"))[1].startswith("retry backoff")
    state.last_attempt_at = now - retry_backoff(1) - timedelta(minutes=1)
    assert is_due(spec, state, now, ZoneInfo("UTC"))[0] is True
    assert retry_backoff(2) == 2 * retry_backoff(1)
    state.consecutive_failures = MAX_DAILY_RETRIES
    assert is_due(spec, state, now, ZoneInfo("UTC")) == (False, "failed repeatedly; waiting for the next slot")


def test_a_deliberate_stop_survives_a_restart_and_only_an_administrator_starts_again(make_ctx):
    from aime.scheduler.runner import run_job

    ctx = make_ctx()
    with ctx.db.session() as s:
        am.change_state(s, "emergency_stop", "admin", "test")
    assert run_job(ctx, "followups")["status"] == "skipped"  # a start-up or catch-up run does not undo the stop
    with ctx.db.session() as s:
        assert am.get_state(s) == "emergency_stop" and "start" in am.ADMIN_ACTIONS
