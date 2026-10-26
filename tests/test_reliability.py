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
    script = notify.toast_script("T", "It's <b>&</b>")
    # XML-escaped (quotes too), so the single-quoted PowerShell string can't be broken out of
    assert "It&#x27;s" in script and "&lt;b&gt;&amp;&lt;/b&gt;" in script and "It's" not in script


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


def test_watchdog_flags_stale_ticks_but_not_fresh_or_maintenance(tmp_path):
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
