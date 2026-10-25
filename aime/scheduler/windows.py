"""Windows Task Scheduler integration (per-user task, no admin rights, no stored password).

The task runs `scripts/run_tick.py` with pythonw.exe (no console window) every N minutes while
the user is logged on, and catches up after sleep/boot (StartWhenAvailable).
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
                   start: datetime | None = None) -> str:
    start = (start or datetime.now() + timedelta(minutes=1)).replace(second=0, microsecond=0)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Autonomous US AI Marketing Employee: runs due agent jobs (research, content, inbound, follow-ups, publishing, reports).</Description>
    <URI>\\{TASK_NAME}</URI>
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
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user_id)}</UserId>
      <LogonType>InteractiveToken</LogonType>
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
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
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


def install(settings: Settings, interval_minutes: int | None = None) -> dict:
    interval = interval_minutes or int(settings.schedule.get("tick_minutes", 15))
    script = settings.base_dir / "scripts" / "run_tick.py"
    xml = build_task_xml(python_for_task(settings), script, settings.base_dir, interval, current_user())
    xml_path = settings.data_dir / "scheduler_task.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(xml, encoding="utf-16")
    proc = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F")
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip(), "xml": str(xml_path),
            "interval_minutes": interval}


def uninstall() -> dict:
    proc = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip()}


def run_now() -> dict:
    proc = _schtasks("/Run", "/TN", TASK_NAME)
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip()}


def query() -> dict:
    """Parse `schtasks /Query /V /FO LIST` into a dict. {'installed': False} when absent."""
    if os.name != "nt":
        return {"installed": False, "error": "Not Windows"}
    proc = _schtasks("/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST")
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
