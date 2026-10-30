"""Windows Task Scheduler integration.

Tasks:
- `AIMarketingEmployee-Tick` runs `scripts/run_tick.py` with pythonw.exe every N minutes and catches up after
  sleep or boot (StartWhenAvailable). By default it runs only while you are logged on (no stored password).
  With `install(..., run_when_logged_off=True)` it also runs while you are signed out and after a reboot
  before you log in: Windows asks YOU for your password in the console (`/RP *`); this app never sees,
  stores or logs it. Your Windows profile is loaded, so DPAPI-encrypted tokens keep working.
  With `wake=True` the task may also wake the PC from sleep to run (Windows power settings must allow wake timers).
  S4U ("do not store password") is deliberately not offered: without the password Windows cannot unlock the
  current-user DPAPI keys that protect the LinkedIn token and the dashboard sessions (aime/secure_store.py).
- `AIMarketingEmployee-Watchdog` (optional, interactive) runs `scripts/watchdog.py` hourly and shows a
  desktop notification if ticks have stopped or the dashboard is not running.
- `AIMarketingEmployee-Dashboard` (optional, interactive) runs `scripts/dashboard_task.py` at logon and every
  15 minutes: it starts the dashboard unless it is already running or a maintenance window is active, so the
  dashboard comes back by itself after a crash, a reboot or a closed window.
Before a task is replaced, its current definition is saved to data/ so it can be restored.

Before the tick task is switched to run while signed out, `preflight()` reads (and changes nothing) what on this PC
decides whether that works: the account and its password, wake timers, standby type, Wi-Fi scope. `test_unattended()`
then proves it on a separate, on-demand task (`AIMarketingEmployee-A1Test`) that runs `scripts/unattended_probe.py`
once in the background session and is deleted again; the production tasks are not touched.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from aime.config import Settings

TASK_NAME = "AIMarketingEmployee-Tick"
WATCHDOG_TASK = "AIMarketingEmployee-Watchdog"
DASHBOARD_TASK = "AIMarketingEmployee-Dashboard"
TEST_TASK = "AIMarketingEmployee-A1Test"  # on demand only; created and deleted by test_unattended()
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
                   task_name: str = TASK_NAME, description: str | None = None, time_limit: str = "PT1H",
                   wake_to_run: bool = False, on_demand_only: bool = False) -> str:
    """Task definition. `Password` logon adds a boot trigger; the XML never contains a password.

    `time_limit` is how long one run may take (PT0S: no limit, for the long-running dashboard); `wake_to_run`
    lets the task wake the PC from sleep; `on_demand_only` leaves out every trigger (it runs only when started)."""
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
    triggers = "" if on_demand_only else f"""
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
  </Triggers>"""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
    <URI>\\{escape(task_name)}</URI>
  </RegistrationInfo>{triggers}
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
    <WakeToRun>{"true" if wake_to_run else "false"}</WakeToRun>
    <ExecutionTimeLimit>{escape(time_limit)}</ExecutionTimeLimit>
    <Priority>6</Priority>
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


def install(settings: Settings, interval_minutes: int | None = None, run_when_logged_off: bool = False,
            wake: bool = False) -> dict:
    interval = interval_minutes or int(settings.schedule.get("tick_minutes", 15))
    script = settings.base_dir / "scripts" / "run_tick.py"
    logon = "Password" if run_when_logged_off else "InteractiveToken"
    user = current_user()
    xml = build_task_xml(python_for_task(settings), script, settings.base_dir, interval, user, logon_type=logon,
                         wake_to_run=wake)
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
            "logon_type": logon, "wake_to_run": wake, "previous_saved": str(previous) if previous else None}


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


def task_settings(xml_text: str) -> dict:
    """What an installed task's XML says about running unattended: logon type, wake-to-run, boot trigger."""
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    body = xml_text.strip()
    if body.startswith("<?xml"):
        body = body[body.index("?>") + 2:]  # the declaration names UTF-16, but this text is already decoded
    # Task Scheduler's own definition of our tasks (schtasks /Query /XML), not untrusted input
    root = ElementTree.fromstring(body)  # noqa: S314
    text = lambda path: (root.findtext(path, default="", namespaces=ns) or "").strip()  # noqa: E731
    return {"logon_type": text("t:Principals/t:Principal/t:LogonType") or "InteractiveToken",
            "wake_to_run": text("t:Settings/t:WakeToRun").lower() == "true",
            "boot_trigger": root.find("t:Triggers/t:BootTrigger", ns) is not None}


def task_conditions(xml_text: str) -> dict:
    """The settings that decide whether a due run starts, catches up, or runs twice."""
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    body = xml_text.strip()
    if body.startswith("<?xml"):
        body = body[body.index("?>") + 2:]
    root = ElementTree.fromstring(body)  # noqa: S314 - Task Scheduler's own definition of our tasks
    text = lambda path, default="": (root.findtext(path, default=default, namespaces=ns) or default).strip()  # noqa: E731
    return {"on_batteries": text("t:Settings/t:DisallowStartIfOnBatteries", "true").lower() != "true",
            "keeps_running_on_batteries": text("t:Settings/t:StopIfGoingOnBatteries", "true").lower() != "true",
            "catches_up": text("t:Settings/t:StartWhenAvailable", "false").lower() == "true",
            "needs_network": text("t:Settings/t:RunOnlyIfNetworkAvailable", "false").lower() == "true",
            "needs_idle": text("t:Settings/t:RunOnlyIfIdle", "false").lower() == "true",
            "multiple_instances": text("t:Settings/t:MultipleInstancesPolicy", "IgnoreNew"),
            "time_limit": text("t:Settings/t:ExecutionTimeLimit", "PT72H"),
            "restarts": int(text("t:Settings/t:RestartOnFailure/t:Count", "0") or 0)}


def unattended_readiness(xml_by_task: dict[str, str | None]) -> list[str]:
    """Plain findings on whether work continues while the owner is signed out or the PC sleeps. Settings that would
    stop or duplicate runs are listed too; settings that are fine are not."""
    findings = []
    for name, xml_text in xml_by_task.items():
        if xml_text is None:
            findings.append(f"{name}: not installed.")
            continue
        t = task_settings(xml_text)
        if t["logon_type"] in ("Password", "S4U"):
            findings.append(f"{name}: runs while you are signed out ({t['logon_type']} logon)"
                            + ("; also starts after a reboot before you log in." if t["boot_trigger"] else "."))
        else:
            findings.append(f"{name}: runs only while you are logged on; it stops when you sign out.")
        findings.append(f"{name}: {'wakes the PC from sleep to run' if t['wake_to_run'] else 'does not wake the PC: nothing runs while it sleeps'}.")
        c = task_conditions(xml_text)
        if not c["on_batteries"] or not c["keeps_running_on_batteries"]:
            findings.append(f"{name}: does not start, or stops, on battery power.")
        if not c["catches_up"]:
            findings.append(f"{name}: a run missed while the PC was off or asleep is not made up.")
        if c["needs_network"] or c["needs_idle"]:
            findings.append(f"{name}: waits for {'a network connection' if c['needs_network'] else 'the PC to be idle'}.")
        if c["multiple_instances"] != "IgnoreNew":
            findings.append(f"{name}: may start a second copy while one is running ({c['multiple_instances']}).")
    return findings


# --------------------------------------------------------------------------- preflight (read-only) and test task

PREFLIGHT_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$u = Get-LocalUser -Name $env:USERNAME
function Idx($sub, $set) { $r = @{}; foreach ($m in (powercfg /q SCHEME_CURRENT $sub $set | Select-String 'Current (AC|DC) Power Setting Index: (0x[0-9a-fA-F]+)')) { $r[$m.Matches[0].Groups[1].Value] = [Convert]::ToInt64($m.Matches[0].Groups[2].Value, 16) }; $r }
$wake = Idx 'SUB_SLEEP' 'RTCWAKE'; $sleep = Idx 'SUB_SLEEP' 'STANDBYIDLE'
$available = ((powercfg /a) -join "`n" -split 'not available')[0]
$line = netsh wlan show interfaces | Select-String '^\s+Profile\s+:' | Select-Object -First 1
$connected = if ($line) { ($line.ToString() -split ':', 2)[1].Trim() } else { $null }
$all = @(netsh wlan show profiles | Select-String 'All User Profile\s+:' | ForEach-Object { ($_.ToString() -split ':', 2)[1].Trim() })
[pscustomobject]@{
  account_source = "$($u.PrincipalSource)"; password_set = [bool]$u.PasswordLastSet
  admin_member = [bool](& "$env:SystemRoot\System32\whoami.exe" /groups | Select-String 'S-1-5-32-544')
  limit_blank_password = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Lsa').LimitBlankPasswordUse
  wake_timers_ac = $wake['AC']; wake_timers_dc = $wake['DC']; sleep_ac = $sleep['AC']; sleep_dc = $sleep['DC']
  modern_standby = [bool]($available -match 'S0 Low Power Idle')
  fast_startup = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power').HiberbootEnabled
  on_wifi = [bool]$connected; wifi_all_users = $(if ($connected) { $all -contains $connected } else { $null })
  task_history = [bool](Get-WinEvent -ListLog 'Microsoft-Windows-TaskScheduler/Operational').IsEnabled
  battery = [bool](Get-CimInstance Win32_Battery)
} | ConvertTo-Json -Compress
"""


def collect_preflight_facts(run=subprocess.run) -> dict:
    """What this PC's settings say about running while signed out (read-only; no names, SSIDs or secrets)."""
    if os.name != "nt":
        return {}
    proc = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PREFLIGHT_SCRIPT], capture_output=True,
               text=True, errors="replace", timeout=120)
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip().startswith("{")]
    return json.loads(lines[-1]) if lines else {}


def evaluate_preflight(facts: dict, files_ok: bool = True) -> list[tuple[str, str]]:
    """(PASS / WARN / FAIL / INFO, finding) for each fact that decides whether the tick can run while signed out."""
    out: list[tuple[str, str]] = []
    if not facts:
        return [("FAIL", "Could not read this PC's settings (not Windows, or PowerShell did not answer).")]
    if not files_ok:
        out.append(("FAIL", "The app's pythonw.exe or scripts/run_tick.py is missing: the task would have nothing to run."))
    source = facts.get("account_source") or "unknown"
    if source == "MicrosoftAccount":
        out.append(("INFO", "Microsoft account: Windows asks for the Microsoft account password (not the PIN)."))
    if not facts.get("password_set"):
        out.append(("FAIL", "No Windows password is set: a task cannot run while signed out without one."))
    else:
        out.append(("PASS", f"{source} account with a password. If that password is blank, Windows refuses the task "
                            "(blank passwords work only at the console)." if facts.get("limit_blank_password") == 1
                    else f"{source} account with a password."))
    out.append(("PASS", "Your account is an administrator, which may run batch jobs (needed to run while signed out).")
               if facts.get("admin_member") else
               ("WARN", "Your account is not an administrator: Windows may refuse 'run whether logged on or not' "
                        "unless the account has the 'Log on as a batch job' right."))
    if facts.get("on_wifi"):
        out.append(("PASS", "The Wi-Fi network in use is saved for all users, so it stays connected while signed out.")
                   if facts.get("wifi_all_users") else
                   ("WARN", "The Wi-Fi network in use is saved for your account only: it may disconnect while you are "
                            "signed out, and jobs that need the internet would wait."))
    if facts.get("modern_standby"):
        out.append(("INFO", "This PC uses Modern Standby: while it sleeps, a task runs only if wake timers are allowed."))
    ac, dc = facts.get("wake_timers_ac"), facts.get("wake_timers_dc")
    out.append(("PASS", "Wake timers are allowed on mains power.") if ac in (1, 2) else
               ("WARN", "Wake timers are off on mains power: a sleeping PC is not woken to run."))
    if facts.get("battery") and dc not in (1, 2):
        out.append(("WARN", "Wake timers are off on battery: on battery, a sleeping PC is not woken to run."))
    if facts.get("sleep_ac") == 0:
        out.append(("PASS", "On mains power the PC never goes to sleep by itself."))
    else:
        out.append(("INFO", f"On mains power the PC sleeps after {int(facts.get('sleep_ac') or 0) // 60} minutes idle."))
    if facts.get("fast_startup"):
        out.append(("INFO", "Fast Startup is on: after a Shut down, nothing runs until the PC is switched on again, "
                            "and Windows may not treat that as a start-up. The 15-minute trigger makes up the missed run "
                            "within minutes of power-on, signed in or not; a Restart always counts as a start-up."))
    out.append(("INFO", "Nothing runs while the PC is shut down, out of power, or without a network: a PC cannot "
                        "guarantee 24/7 operation."))
    if not facts.get("task_history"):
        out.append(("INFO", "Windows Task Scheduler history is off; the app's own job history is the record of each run."))
    return out


def preflight(settings: Settings, run=subprocess.run) -> list[tuple[str, str]]:
    files_ok = python_for_task(settings).exists() and (settings.base_dir / "scripts" / "run_tick.py").exists()
    return evaluate_preflight(collect_preflight_facts(run), files_ok)


def test_unattended(settings: Settings, wait_seconds: int = 120, sleep=time.sleep) -> dict:
    """Prove a background-session run on a separate on-demand task, then delete it. Windows asks the owner for the
    password (`/RP *`); the production tasks are not touched. The probe writes data/a1_probe.json."""
    probe = settings.data_dir / "a1_probe.json"
    probe.unlink(missing_ok=True)
    xml = build_task_xml(python_for_task(settings), settings.base_dir / "scripts" / "unattended_probe.py",
                         settings.base_dir, 15, current_user(), logon_type="Password", task_name=TEST_TASK,
                         time_limit="PT10M", on_demand_only=True,
                         description="AI Marketing Employee A1 test: runs once in the background session, then is deleted.")
    xml_path = settings.data_dir / "a1_test_task.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(xml, encoding="utf-16")
    print(f"Windows will now ask for the password of {current_user()} to register the test task {TEST_TASK}.")
    created = _schtasks_interactive("/Create", "/TN", TEST_TASK, "/XML", str(xml_path), "/RU", current_user(), "/RP", "*", "/F")
    if created.returncode != 0:
        return {"ok": False, "output": "The test task was not registered (wrong password, or Windows refused it)."}
    try:
        started = _schtasks("/Run", "/TN", TEST_TASK)
        if started.returncode != 0:
            return {"ok": False, "output": (started.stdout + started.stderr).strip()}
        for _ in range(int(wait_seconds)):
            if probe.exists():
                break
            sleep(1)
        if not probe.exists():
            return {"ok": False, "output": f"The test task did not report within {wait_seconds} s."}
        result = json.loads(probe.read_text(encoding="utf-8"))
        ok = result.get("session") == "background" and result.get("dpapi") and result.get("database")
        return {"ok": bool(ok), "probe": result}
    finally:
        removed = _schtasks("/Delete", "/TN", TEST_TASK, "/F")
        if removed.returncode != 0:
            print(f"Could not delete {TEST_TASK}: delete it in Task Scheduler.")


def installed_xml(task_name: str) -> str | None:
    """The installed task's definition (read-only), or None."""
    if os.name != "nt":
        return None
    proc = _schtasks("/Query", "/TN", task_name, "/XML")
    return proc.stdout if proc.returncode == 0 and "<Task" in proc.stdout else None


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
