"""Scheduler watchdog (Windows Task Scheduler, hourly, while you are logged on).

Reads data/heartbeat.json and shows a desktop notification if scheduled ticks have stopped or the dashboard
is not answering on its port. It changes nothing (but its own small note of a wake it allowed for) and does not import the
app (only the small notify/redact helpers), so it works even when the app itself is broken. Findings are appended to
data/logs/watchdog.log.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = Path(os.environ.get("AIME_DATA_DIR") or ROOT / "data")
STALE_AFTER = timedelta(minutes=45)  # three missed 15-minute ticks
STALE_WHILE_RUNNING = timedelta(minutes=75)  # a tick writes its beat as each job starts, and Task Scheduler ends it after an hour
# At sign-in this task and the Dashboard task both start 2 minutes after logon: give a dashboard that is still
# starting this long to answer before reporting it as not running.
DASHBOARD_GRACE_SECONDS = 180
DASHBOARD_RECHECK_SECONDS = 10
# After the PC wakes this task and the Tick task are both due (StartWhenAvailable), and Windows starts them within minutes, in
# either order: this one read the beat from before the sleep, 6 minutes after a wake on 29 and 30 Oct. Half an hour after a wake
# is the tick's to run in; this task's next run, an hour on, reports a real stop.
WAKE_GRACE_SECONDS = 30 * 60
# A PC whose awake stretches stay short would be just awake at every run: however it sleeps, one silence is excused this long at most.
WAKE_GRACE_AT_MOST = timedelta(hours=2)
WAKE_GRACE_NOTE = "watchdog_wake_grace.json"


def seconds_since_wake() -> float | None:
    """Since this PC last woke from sleep, by the Windows event written at each wake (modern standby and hibernate too; the
    power API stays at zero through modern standby's kind of sleep). None where that cannot be told."""
    if os.name != "nt":
        return None
    import re
    import subprocess

    query = "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]"
    try:
        found = subprocess.run(["wevtutil", "qe", "System", f"/q:{query}", "/c:1", "/rd:true", "/f:xml"], capture_output=True, text=True,
                               timeout=20, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        at = re.search(r"SystemTime=['\"](\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", found)
        woke = datetime.fromisoformat(at.group(1)).replace(tzinfo=timezone.utc)
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):  # no event, or the log cannot be read
        return None
    return (datetime.now(timezone.utc) - woke).total_seconds()


def _grace_left(data_dir: Path, last_beat: str, now: datetime) -> bool:
    """May a wake still excuse the silence since this beat? Counted from the first time it did. A note that cannot be written
    excuses nothing: then a stop could be hidden for ever."""
    note = data_dir / WAKE_GRACE_NOTE
    try:
        saved = json.loads(note.read_text(encoding="utf-8"))
        if saved["beat"] == last_beat:
            return timedelta(0) <= now - datetime.fromisoformat(saved["first"]) <= WAKE_GRACE_AT_MOST
    except (OSError, ValueError, KeyError, TypeError, AttributeError):  # none yet, or unreadable: a new count
        pass
    try:
        note.write_text(json.dumps({"beat": last_beat, "first": now.isoformat()}), encoding="utf-8")
    except OSError:
        return False
    return True


def check(data_dir: Path, now: datetime | None = None, since_wake=None) -> list[str]:
    """Problems worth a notification; empty when ticks are fresh, a maintenance window is active, or the PC has only just woken."""
    from aime import maintenance

    own_clock = now is None  # a time given to this check is not this PC's: its wake says nothing about that
    now = now or datetime.now(timezone.utc)
    if maintenance.active(data_dir):
        return []
    path = data_dir / "heartbeat.json"
    if not (data_dir / "aime.db").exists() and any((data_dir / "backups").glob("aime-*.db")):
        return ["The database is missing and backups exist: nothing is running. Run `python -m aime doctor` for the next step."]
    if not path.exists():
        return ["No scheduler heartbeat yet: the AIMarketingEmployee-Tick task may not be installed."]
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(beat["at"].rstrip("Z")).replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ["Scheduler heartbeat file is unreadable."]
    if beat.get("state") == "clock_hold":
        return ["No work is being done because the PC's clock looks wrong. Correct the clock, or run `python -m aime confirm-clock`."]
    age = now - at
    if age > (STALE_WHILE_RUNNING if beat.get("state") == "running" else STALE_AFTER):
        woke = (since_wake or seconds_since_wake)() if since_wake or own_clock else None
        if (woke is not None and 0 <= woke < WAKE_GRACE_SECONDS and age.total_seconds() > woke  # the silence began before the wake
                and _grace_left(data_dir, beat["at"], now)):
            return []
        return [f"Scheduled ticks stopped: last tick {int(age.total_seconds() // 60)} min ago. "
                "Check Task Scheduler (AIMarketingEmployee-Tick) and data/logs/scheduler_crash.log."]
    return []


def dashboard_problem(host: str = "127.0.0.1", port: int | None = None, wait_seconds: float = 0,
                      sleep=time.sleep, clock=time.monotonic) -> str | None:
    """None when the dashboard gives a page. With `wait_seconds`, a dashboard that does not answer yet is checked again
    every few seconds until then (it may still be starting) before it is reported. A port that accepts the connection
    and gives no page is a hung dashboard or another program: it is reported as that, not as all well. So is a web program that
    answers but is not the dashboard."""
    import socket

    from dotenv import dotenv_values

    from aime.dashboard_probe import PAGE_TIMEOUT, page_owner

    port = port or int(os.environ.get("DASHBOARD_PORT") or dotenv_values(ROOT / ".env").get("DASHBOARD_PORT") or 8765)
    deadline = clock() + wait_seconds
    while True:
        accepted = True
        try:
            with socket.create_connection((host, port), timeout=2):
                pass
        except OSError:
            accepted = False
        owner = page_owner(host, port) if accepted else None
        if owner == "dashboard":
            return None
        if clock() >= deadline:
            break
        sleep(DASHBOARD_RECHECK_SECONDS)
    waited = f" after waiting {int(wait_seconds)} s for it to start" if wait_seconds else ""
    if owner == "other":
        return (f"Another web program is using port {port}: it answers, but it is not the dashboard{waited}, so the dashboard cannot "
                "start. Close that program, or give the dashboard another port.")
    if accepted:
        return (f"The dashboard is not answering: something holds port {port} but gave no page in {int(PAGE_TIMEOUT)} s{waited}. "
                "A hung dashboard ends itself within minutes and the Dashboard task starts a new one; if this stays, another "
                "program has the port, or end the AIMarketingEmployee-Dashboard task in Task Scheduler.")
    return (f"The dashboard is not running (nothing on port {port}{waited}). Start it with scripts/start_dashboard.ps1, "
            "or install `python -m aime scheduler install-dashboard` so it restarts by itself.")


def free_space_problem(data_dir: Path) -> str | None:
    """A nearly full disk stops backups, then the database, then ticks. Stdlib only, like the rest of this script."""
    import shutil

    try:
        free = shutil.disk_usage(data_dir).free
    except OSError:
        return None
    if free >= 2 * 2**30:
        return None
    return f"Only {free / 2**30:.1f} GB is free on the disk holding {data_dir}: free some space, or backups and then the database fail."


def main() -> int:
    from aime import maintenance

    problems = check(DATA)
    if not maintenance.active(DATA):
        problems += [p for p in [dashboard_problem(wait_seconds=DASHBOARD_GRACE_SECONDS)] if p]
    problems += [p for p in [free_space_problem(DATA)] if p]
    if problems:
        from aime.notify import TITLE, toast

        toast(f"{TITLE}: scheduler", problems[0])  # first: on a full disk it is the one thing that can still tell the owner
        log = DATA / "logs" / "watchdog.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                for p in problems:
                    fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {p}\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
