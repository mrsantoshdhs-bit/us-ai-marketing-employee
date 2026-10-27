"""Windows Task Scheduler integration.

Tasks:
- `AIMarketingEmployee-Tick` runs `scripts/run_tick.py` with pythonw.exe every N minutes and catches up after
  sleep or boot (StartWhenAvailable). By default it runs only while you are logged on (no stored password).
  With `install(..., run_when_logged_off=True)` it also runs while you are signed out and after a reboot
  before you log in: Windows asks YOU for your password in the console (`/RP *`); this app never sees,
  stores or logs it. Your Windows profile is loaded, so DPAPI-encrypted tokens keep working.
- `AIMarketingEmployee-Watchdog` (optional, interactive) runs `scripts/watchdog.py` hourly and shows a
  desktop notification if ticks have stopped or the dashboard is not running.
- `AIMarketingEmployee-Dashboard` (optional, interactive) runs `scripts/dashboard_task.py` at logon and every
  15 minutes: it starts the dashboard unless it is already running or a maintenance window is active, so the
  dashboard comes back by itself after a crash, a reboot or a closed window.
Before a task is replaced, its current definition is saved to data/ so it can be restored.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from aime.config import Settings

TASK_NAME = "AIMarketingEmployee-Tick"
WATCHDOG_TASK = "AIMarketingEmployee-Watchdog"
DASHBOARD_TASK = "AIMarketingEmployee-Dashboard"
LOGON_TYPES = ("InteractiveToken", "Password")


def python_for_task(settings: Settings) -> Path:
    venv = settings.base_dir / ".venv" / "Scripts" / "pythonw.exe"
    if venv.exists():
        return venv
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def build_task_xml(python_exe: Path, script: Path, workdir: Path, interval_minutes: int, user_id: str,
                   start: datetime | None = None, logon_type: str = "InteractiveToken",
                   task_name: str = TASK_NAME, description: str | None = None, time_limit: str = "PT1H") -> str:
    """Task definition. `Password` logon adds a boot trigger; the XML never contains a password.

    `time_limit` is how long one run may take (PT0S: no limit, for the long-running dashboard)."""
    if logon_type not in LOGON_TYPES:
        raise ValueError(f"logon_type must be one of {LOGON_TYPES}")
    start = (start or datetime.now() + timedelta(minutes=1)).replace(second=0, microsecond=0)
    description = description or ("Autonomous US AI Marketing Employee: runs due agent jobs (research, content, "
                                  "inbound, follow-ups, publishing, reports).")
    boot = """
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT3M</Delay>
    </BootTrigger>""" if logon_type == "Password" else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
    <URI>\\{escape(task_name)}</URI>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{int(interval_minutes)}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start:%Y-%m-%dT%H:%M:%S}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user_id)}</UserId>
      <Delay>PT2M</Delay>
    </LogonTrigger>{boot}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user_id)}</UserId>
      <LogonType>{logon_type}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <RestartOnFailure>
      <Interval>PT5M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{escape(time_limit)}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python_exe))}</Command>
      <Arguments>"{escape(str(script))}"</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, errors="replace")


def _schtasks_interactive(*args: str) -> subprocess.CompletedProcess:
    """Run schtasks attached to this console so Windows itself can prompt for the password (`/RP *`)."""
    return subprocess.run(["schtasks", *args])


def save_current_definition(settings: Settings, task_name: str = TASK_NAME) -> Path | None:
    """Keep the installed task's XML before it is replaced, for `restore_previous`."""
    proc = _schtasks("/Query", "/TN", task_name, "/XML")
    if proc.returncode != 0 or "<Task" not in proc.stdout:
        return None
    path = settings.data_dir / f"scheduler_task.backup-{datetime.now():%Y%m%d-%H%M%S}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(proc.stdout, encoding="utf-16")
    return path


def install(settings: Settings, interval_minutes: int | None = None, run_when_logged_off: bool = False) -> dict:
    interval = interval_minutes or int(settings.schedule.get("tick_minutes", 15))
    script = settings.base_dir / "scripts" / "run_tick.py"
    logon = "Password" if run_when_logged_off else "InteractiveToken"
    user = current_user()
    xml = build_task_xml(python_for_task(settings), script, settings.base_dir, interval, user, logon_type=logon)
    xml_path = settings.data_dir / "scheduler_task.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(xml, encoding="utf-16")
    previous = save_current_definition(settings)
    if run_when_logged_off:
        print(f"Windows will now ask for the password of {user} so the task can run while you are signed out.")
        print("It is stored by Windows Task Scheduler only; this app never sees it.")
        proc = _schtasks_interactive("/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/RU", user, "/RP", "*", "/F")
        output = ""
    else:
        proc = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F")
        output = (proc.stdout + proc.stderr).strip()
    return {"ok": proc.returncode == 0, "output": output, "xml": str(xml_path), "interval_minutes": interval,
            "logon_type": logon, "previous_saved": str(previous) if previous else None}


def restore_previous(settings: Settings) -> dict:
    """Re-register the most recently saved task definition (undo the last install)."""
    saved = sorted(settings.data_dir.glob("scheduler_task.backup-*.xml"))
    if not saved:
        return {"ok": False, "output": "No saved task definition found"}
    xml = saved[-1]
    needs_password = "<LogonType>Password</LogonType>" in xml.read_text(encoding="utf-16", errors="replace")
    if needs_password:
        proc = _schtasks_interactive("/Create", "/TN", TASK_NAME, "/XML", str(xml), "/RU", current_user(),
                                     "/RP", "*", "/F")
        return {"ok": proc.returncode == 0, "output": "", "restored": xml.name}
    proc = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(xml), "/F")
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip(), "restored": xml.name}


def install_watchdog(settings: Settings) -> dict:
    script = settings.base_dir / "scripts" / "watchdog.py"
    xml = build_task_xml(python_for_task(settings), script, settings.base_dir, 60, current_user(),
                         task_name=WATCHDOG_TASK,
                         description="AI Marketing Employee watchdog: notifies you if scheduled ticks have stopped.")
    xml_path = settings.data_dir / "watchdog_task.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(xml, encoding="utf-16")
    proc = _schtasks("/Create", "/TN", WATCHDOG_TASK, "/XML", str(xml_path), "/F")
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip(), "xml": str(xml_path)}


def install_dashboard(settings: Settings) -> dict:
    script = settings.base_dir / "scripts" / "dashboard_task.py"
    xml = build_task_xml(python_for_task(settings), script, settings.base_dir, 15, current_user(),
                         task_name=DASHBOARD_TASK, time_limit="PT0S",
                         description="AI Marketing Employee dashboard: starts it at logon and restarts it if it "
                                     "has stopped (never during a maintenance window).")
    xml_path = settings.data_dir / "dashboard_task.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(xml, encoding="utf-16")
    proc = _schtasks("/Create", "/TN", DASHBOARD_TASK, "/XML", str(xml_path), "/F")
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip(), "xml": str(xml_path)}


def uninstall(task_name: str = TASK_NAME) -> dict:
    proc = _schtasks("/Delete", "/TN", task_name, "/F")
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip()}


def run_now() -> dict:
    proc = _schtasks("/Run", "/TN", TASK_NAME)
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip()}


def query(task_name: str = TASK_NAME) -> dict:
    """Parse `schtasks /Query /V /FO LIST` into a dict. {'installed': False} when absent."""
    if os.name != "nt":
        return {"installed": False, "error": "Not Windows"}
    proc = _schtasks("/Query", "/TN", task_name, "/V", "/FO", "LIST")
    if proc.returncode != 0:
        return {"installed": False, "error": (proc.stderr or proc.stdout).strip()}
    info: dict = {"installed": True}
    for line in proc.stdout.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            if key and key not in info:
                info[key] = value.strip()
    return info
