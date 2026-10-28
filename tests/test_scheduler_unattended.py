"""Readiness to run while the owner is signed out or the PC sleeps, tested on generated task definitions only:
nothing here installs or changes a real scheduled task (schtasks is replaced by a recorder)."""

from datetime import datetime
from pathlib import Path

from aime.scheduler import windows

PY, SCRIPT, WORK = Path(r"C:\p\pythonw.exe"), Path(r"C:\p\scripts\run_tick.py"), Path(r"C:\p")


def xml(**kw):
    return windows.build_task_xml(PY, SCRIPT, WORK, 15, "PC\\me", start=datetime(2026, 10, 28, 8, 0), **kw)


def test_wake_to_run_and_logged_off_settings_are_read_back_from_the_task_definition():
    assert windows.task_settings(xml()) == {"logon_type": "InteractiveToken", "wake_to_run": False, "boot_trigger": False}
    unattended = xml(logon_type="Password", wake_to_run=True)
    assert windows.task_settings(unattended) == {"logon_type": "Password", "wake_to_run": True, "boot_trigger": True}
    assert "<Password>" not in unattended and "RP" not in unattended  # Windows asks for the password; the XML never has it


def test_readiness_says_plainly_what_stops_when_the_owner_signs_out():
    lines = windows.unattended_readiness({"Tick": xml(), "Watchdog": None})
    assert "Tick: runs only while you are logged on; it stops when you sign out." in lines
    assert "Tick: does not wake the PC: nothing runs while it sleeps." in lines
    assert "Watchdog: not installed." in lines
    ready = windows.unattended_readiness({"Tick": xml(logon_type="Password", wake_to_run=True)})
    assert ready == ["Tick: runs while you are signed out (Password logon); also starts after a reboot before you log in.",
                     "Tick: wakes the PC from sleep to run."]


def test_install_passes_wake_and_lets_windows_ask_for_the_password(make_ctx, monkeypatch):
    ctx = make_ctx()
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(windows, "_schtasks", lambda *a: calls.append(("plain", a)) or Done())
    monkeypatch.setattr(windows, "_schtasks_interactive", lambda *a: calls.append(("console", a)) or Done())
    res = windows.install(ctx.settings, run_when_logged_off=True, wake=True)
    assert res["ok"] and res["logon_type"] == "Password" and res["wake_to_run"] is True
    created = [a for kind, a in calls if a[0] == "/Create"]
    assert created and created[0][-3:] == ("/RP", "*", "/F")  # the password prompt belongs to Windows
    saved = Path(res["xml"]).read_text(encoding="utf-16")
    assert windows.task_settings(saved)["wake_to_run"] is True
