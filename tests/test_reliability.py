"""Ollama auto-start and health alerts, desktop notifications, scheduler task modes, watchdog, job budget."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from aime import maintenance, notify
from aime.db import kv_get
from aime.integrations.status import _LLM_PROBE, llm_reachable
from aime.llm.ollama import OllamaProvider, start_local_server
from aime.models import Alert
from aime.monitoring import alert_once, check_health
from aime.scheduler import runner, windows
from scripts import watchdog


def _alerts(ctx, like="%"):
    with ctx.db.session() as s:
        return s.execute(select(Alert).where(Alert.message.like(like))).scalars().all()


# ---------------------------------------------------------------- local model health
def test_llm_alert_after_two_failed_probes_and_recovery(make_ctx):
    ctx = make_ctx(llm_provider="auto")
    with ctx.db.session() as s:
        check_health(s, ctx.settings, llm_probe=lambda: False)
    assert _alerts(ctx, "Local AI model%") == []  # one failure is not enough
    with ctx.db.session() as s:
        summary = check_health(s, ctx.settings, llm_probe=lambda: False)
    assert any("unreachable" in i for i in summary["issues"])
    assert len(_alerts(ctx, "Local AI model%")) == 1
    with ctx.db.session() as s:
        check_health(s, ctx.settings, llm_probe=lambda: True)
        assert kv_get(s, "llm.health")["ok"] is True and kv_get(s, "llm.health")["failures"] == 0


def test_no_llm_probe_when_template_writer_is_configured(ctx):
    calls = []
    with ctx.db.session() as s:
        check_health(s, ctx.settings, llm_probe=lambda: calls.append(1) or False)
    assert calls == [] and _alerts(ctx, "Local AI model%") == []


class _Provider(OllamaProvider):
    def __init__(self, url, states):
        super().__init__(url, "llama3.2:latest")
        self.states = list(states)

    def available(self):
        return self.states.pop(0) if self.states else True


def test_autostart_starts_server_once_and_waits(tmp_path):
    exe = tmp_path / "ollama.exe"
    exe.write_bytes(b"")
    launched = []
    provider = _Provider("http://127.0.0.1:11434", [False, False, True])
    ok = start_local_server(provider, exe, wait=5, popen=lambda args, **k: launched.append(args), sleep=lambda s: None)
    assert ok and launched == [[str(exe), "serve"]]


def test_autostart_never_touches_remote_or_running_servers(tmp_path):
    exe = tmp_path / "ollama.exe"
    exe.write_bytes(b"")
    launched = []
    remote = _Provider("http://gpu-box.example:11434", [False])
    assert not start_local_server(remote, exe, popen=lambda a, **k: launched.append(a), sleep=lambda s: None)
    running = _Provider("http://localhost:11434", [True])
    assert start_local_server(running, exe, popen=lambda a, **k: launched.append(a), sleep=lambda s: None)
    missing = _Provider("http://127.0.0.1:11434", [False])
    assert not start_local_server(missing, tmp_path / "nope.exe", popen=lambda a, **k: launched.append(a))
    assert launched == []


def test_status_page_reprobes_the_model(make_ctx):
    up = {"value": False}

    def handler(request: httpx.Request):
        if request.url.path == "/api/tags" and up["value"]:
            return httpx.Response(200, json={"models": [{"name": "llama3.2:latest"}]})
        return httpx.Response(503)
    ctx = make_ctx(handler=handler, llm_provider="auto")
    _LLM_PROBE.clear()
    assert llm_reachable(ctx) is False
    up["value"] = True
    assert llm_reachable(ctx) is False  # cached for a minute
    assert llm_reachable(ctx, max_age=0) is True


# ---------------------------------------------------------------- notifications
def test_new_alert_sends_one_redacted_toast(make_ctx, monkeypatch):
    ctx = make_ctx(data_role="demo")
    sent = []
    monkeypatch.setattr(notify, "toast", lambda title, msg: sent.append((title, msg)) or True)
    monkeypatch.setattr("aime.monitoring.notify_alert",
                        lambda settings, level, message: notify.notify_alert(settings, level, message,
                                                                             sender=notify.toast))
    with ctx.db.session() as s:
        alert_once(s, "error", "monitor", "Job 'research' failed; contact ops@example.com", ctx.settings)
        alert_once(s, "error", "monitor", "Job 'research' failed; contact ops@example.com", ctx.settings)
        alert_once(s, "info", "monitor", "FYI only", ctx.settings)
    assert len(sent) == 1 and "error" in sent[0][0]


def test_toasts_never_fire_in_tests_or_when_disabled(make_ctx):
    sent = []
    ctx = make_ctx()  # data_role=test
    assert not notify.notify_alert(ctx.settings, "error", "x", sender=lambda *a: sent.append(a))
    ctx2 = make_ctx(data_role="demo", alert_toast=False)
    assert not notify.notify_alert(ctx2.settings, "error", "x", sender=lambda *a: sent.append(a))
    assert sent == []


def test_toast_script_escapes_text():
    import base64
    import re

    script = notify.toast_script("T", "It's <b>&</b>")
    # the XML (escaped) reaches the script only as base64, and decodes back to exactly the escaped text
    [xml, app] = [base64.b64decode(b).decode("utf-8") for b in re.findall(r"FromBase64String\('([A-Za-z0-9+/=]*)'\)", script)]
    assert "It&#x27;s" in xml and "&lt;b&gt;&amp;&lt;/b&gt;" in xml and app == notify.APP_ID
    assert "It" not in script.split("FromBase64String")[1]


def test_no_text_can_break_out_of_the_toast_script():
    """PowerShell ends a single-quoted string at ' and at the typographic quotes U+2018-U+201B too: an email could
    carry `’;Start-Process calc;’` in a Calendly invitee name. None of the text reaches the script as text."""
    import re

    payloads = ["x’; Start-Process calc; ’", "x‘;calc;‘", "x‚;calc;‛", "x'; calc; '", 'x"; calc; "', "$(calc)", "`calc`"]
    for p in payloads:
        script = notify.toast_script("T", p)
        quoted = re.findall(r"'([^']*)'", script)
        assert quoted and all(re.fullmatch(r"[A-Za-z0-9+/=]*", q) for q in quoted), p  # only base64 between quotes
        assert not any(ch in script for ch in "‘’‚‛") and "calc" not in script, p


# ---------------------------------------------------------------- scheduler task
def test_password_task_has_boot_trigger_and_no_secret():
    xml = windows.build_task_xml(Path("C:/py/pythonw.exe"), Path("C:/app/run_tick.py"), Path("C:/app"), 15,
                                 "PC\\user", logon_type="Password")
    assert "<LogonType>Password</LogonType>" in xml and "<BootTrigger>" in xml
    assert "Password>" not in xml.replace("<LogonType>Password</LogonType>", "")
    plain = windows.build_task_xml(Path("p"), Path("s"), Path("w"), 15, "PC\\user")
    assert "<LogonType>InteractiveToken</LogonType>" in plain and "<BootTrigger>" not in plain


def test_logged_off_install_lets_windows_prompt_for_the_password(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(windows, "_schtasks_interactive", lambda *a: calls.append(a) or type("P", (), {"returncode": 0})())
    monkeypatch.setattr(windows, "_schtasks", lambda *a: type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    res = windows.install(ctx.settings, 15, run_when_logged_off=True)
    assert res["ok"] and res["logon_type"] == "Password"
    args = calls[0]
    assert args[args.index("/RP") + 1] == "*"


def test_previous_task_definition_is_saved_before_replacing(ctx, monkeypatch):
    old = "<?xml version='1.0'?><Task><Principals><LogonType>InteractiveToken</LogonType></Principals></Task>"

    def fake(*a):
        out = old if "/XML" in a and "/Query" in a else ""
        return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()
    monkeypatch.setattr(windows, "_schtasks", fake)
    res = windows.install(ctx.settings, 15)
    assert res["previous_saved"] and Path(res["previous_saved"]).exists()
    assert windows.restore_previous(ctx.settings)["ok"]


# ---------------------------------------------------------------- watchdog and job budget
def _heartbeat(data_dir, minutes_ago):
    data_dir.mkdir(parents=True, exist_ok=True)
    at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
    (data_dir / "heartbeat.json").write_text(json.dumps({"at": at.isoformat() + "Z", "state": "idle"}), encoding="utf-8")


def test_watchdog_flags_stale_ticks_but_not_fresh_or_maintenance(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "seconds_since_wake", lambda: None)  # however lately this PC really woke
    _heartbeat(tmp_path, 5)
    assert watchdog.check(tmp_path) == []
    _heartbeat(tmp_path, 90)
    assert "stopped" in watchdog.check(tmp_path)[0]
    maintenance.start(tmp_path, 1, "upgrade")
    assert watchdog.check(tmp_path) == []


def test_slow_job_raises_budget_alert_without_failing(make_ctx, monkeypatch):
    ctx = make_ctx()
    ctx.settings.schedule["job_budget_minutes"] = 0.0001
    monkeypatch.setitem(runner.JOBS, "monitor", lambda c, job: __import__("time").sleep(0.05) or runner.JOBS["db_maintenance"](c))
    result = runner.run_job(ctx, "monitor")
    assert result["status"] == "success"
    assert _alerts(ctx, "Job 'monitor' took%")


# ---------------------------------------------------------------- alerts resolve themselves
def _open(ctx):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert).where(Alert.acknowledged.is_(False))).scalars()]


def test_alerts_close_once_the_problem_is_gone_and_do_not_repeat_with_new_numbers(make_ctx):
    from aime.cli import cmd_user
    from aime.workflows import job_monitor
    ctx = make_ctx(llm_provider="auto", dashboard_auth=True)
    for _ in range(3):  # model down three checks in a row: one alert
        with ctx.db.session() as s:
            check_health(s, ctx.settings, llm_probe=lambda: False)
    ctx.settings.llm_provider = "template"  # the monitor job below must not probe a real local model
    job_monitor(ctx)
    job_monitor(ctx)  # same problems again: no second copy
    msgs = _open(ctx)
    assert sum(m.startswith("Local AI model") for m in msgs) == 1
    assert sum(m.startswith("Dashboard sign-in") for m in msgs) == 1
    ctx.settings.llm_provider = "auto"
    with ctx.db.session() as s:
        check_health(s, ctx.settings, llm_probe=lambda: True)

    class Args:
        action, username, role = "create", "manager", "admin"
    assert cmd_user(Args(), ctx, prompt_fn=lambda _p: "correct horse " + "battery staple") == 0
    job_monitor(ctx)
    msgs = _open(ctx)
    assert not any(m.startswith(("Local AI model", "Dashboard sign-in")) for m in msgs), msgs
    with ctx.db.session() as s:
        closed = s.execute(select(Alert).where(Alert.resolved_at.is_not(None))).scalars().all()
        assert {a.key for a in closed} >= {"llm:down", "doctor:Dashboard sign-in"}


def test_a_changing_detail_updates_the_open_alert_instead_of_adding_one(ctx):
    with ctx.db.session() as s:
        assert alert_once(s, "warning", "doctor", "Recent verified backup: verified 31 h ago", key="doctor:Recent verified backup")
    with ctx.db.session() as s:
        assert not alert_once(s, "warning", "doctor", "Recent verified backup: verified 32 h ago",
                              key="doctor:Recent verified backup")
    assert _open(ctx) == ["Recent verified backup: verified 32 h ago"]


def test_alerts_from_before_keys_existed_are_adopted_and_resolved(ctx):
    from aime.monitoring import resolve_alerts
    with ctx.db.session() as s:
        s.add(Alert(level="warning", source="doctor", message="Dashboard sign-in: No account yet: run ..."))
        s.add(Alert(level="warning", source="monitor", message="Job 'monitor' has not succeeded since 2026-10-26 02:21 UTC"))
        s.add(Alert(level="warning", source="monitor", message="Job 'research' took 40 min (budget 30 min)"))
    with ctx.db.session() as s:
        assert not alert_once(s, "warning", "doctor", "Dashboard sign-in: still none", key="doctor:Dashboard sign-in")
        assert resolve_alerts(s, ("doctor:", "job_stale:"), set()) == 2
    assert _open(ctx) == ["Job 'research' took 40 min (budget 30 min)"]  # one-off notices stay for a person


def test_a_dismissed_alert_stays_quiet_for_a_day(ctx):
    with ctx.db.session() as s:
        alert_once(s, "warning", "doctor", "Off-machine encrypted backup: not configured", key="doctor:Off-machine")
    with ctx.db.session() as s:
        s.execute(select(Alert)).scalars().first().acknowledged = True  # the Sales Manager dismissed it
    with ctx.db.session() as s:
        assert not alert_once(s, "warning", "doctor", "Off-machine encrypted backup: not configured",
                              key="doctor:Off-machine")


def test_monitor_never_reports_itself_as_stale(ctx):
    from aime.models import JobState
    from aime.timeutil import utcnow
    with ctx.db.session() as s:
        s.add(JobState(job="monitor", last_success_at=utcnow() - timedelta(hours=10), consecutive_failures=0,
                       enabled=True))
    with ctx.db.session() as s:
        summary = check_health(s, ctx.settings)
    assert not any("'monitor'" in i for i in summary["issues"])


# ---------------------------------------------------------------- killed runs
def test_a_killed_run_is_abandoned_once_its_lock_owner_is_gone_and_counts_as_a_failure(ctx):
    from aime.models import JobRun, JobState
    from aime.timeutil import utcnow
    now = utcnow()
    with ctx.db.session() as s:
        s.add(JobRun(job="inbox", trigger="scheduler", status="running", started_at=now - timedelta(minutes=15)))
        s.add(JobRun(job="enrich", trigger="scheduler", status="running", started_at=now - timedelta(minutes=3)))
        s.add(JobRun(job="research", trigger="scheduler", status="running", started_at=now - timedelta(minutes=15)))
    lock = runner.FileLock(ctx.settings.locks_dir / "job_research.lock")
    assert lock.acquire()  # research is still running in a live process (this one)
    try:
        assert runner.recover(ctx, now)["abandoned_jobs"] == 1
    finally:
        lock.release()
    with ctx.db.session() as s:
        status = {r.job: r.status for r in s.execute(select(JobRun)).scalars()}
        assert status == {"inbox": "abandoned", "enrich": "running", "research": "running"}
        st = s.get(JobState, "inbox")
        assert st.last_status == "abandoned" and st.consecutive_failures == 1


def test_the_open_alert_is_updated_even_when_an_older_one_was_dismissed(ctx):
    with ctx.db.session() as s:
        s.add(Alert(level="warning", source="doctor", message="Off-machine encrypted backup: folder empty", acknowledged=True))
        s.add(Alert(level="warning", source="doctor", message="Off-machine encrypted backup: passphrase too short"))
    with ctx.db.session() as s:
        assert not alert_once(s, "warning", "doctor", "Off-machine encrypted backup: passphrase missing",
                              key="doctor:Off-machine encrypted backup")
    assert _open(ctx) == ["Off-machine encrypted backup: passphrase missing"]


def test_a_new_outage_alerts_again_after_an_earlier_dismissed_one_cleared(make_ctx):
    from aime.monitoring import resolve_alerts
    ctx = make_ctx(llm_provider="auto")
    with ctx.db.session() as s:  # an earlier outage, dismissed by the Sales Manager
        s.add(Alert(level="warning", source="monitor", acknowledged=True,
                    message="Local AI model (Ollama) unreachable since 2026-10-26 09:51 UTC; new posts use the template"))
    for _ in range(2):  # a second outage ...
        with ctx.db.session() as s:
            check_health(s, ctx.settings, llm_probe=lambda: False)
    with ctx.db.session() as s:  # ... that clears
        check_health(s, ctx.settings, llm_probe=lambda: True)
        assert resolve_alerts(s, ("llm:",), set()) == 0
    for _ in range(2):  # a third outage must be reported, not silenced by the old dismissal
        with ctx.db.session() as s:
            check_health(s, ctx.settings, llm_probe=lambda: False)
    with ctx.db.session() as s:
        open_ = s.execute(select(Alert).where(Alert.acknowledged.is_(False))).scalars().all()
    assert [a.key for a in open_] == ["llm:down"]
