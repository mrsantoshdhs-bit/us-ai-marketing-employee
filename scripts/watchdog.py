"""Scheduler watchdog (Windows Task Scheduler, hourly, while you are logged on).

Reads data/heartbeat.json and shows a desktop notification if scheduled ticks have stopped or the dashboard
is not answering on its port. It changes nothing and does not import the app (only the small notify/redact
helpers), so it works even when the app itself is broken. Findings are appended to data/logs/watchdog.log.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = Path(os.environ.get("AIME_DATA_DIR") or ROOT / "data")
STALE_AFTER = timedelta(minutes=45)  # three missed 15-minute ticks


def check(data_dir: Path, now: datetime | None = None) -> list[str]:
    """Problems worth a notification; empty when ticks are fresh or a maintenance window is active."""
    from aime import maintenance

    now = now or datetime.now(timezone.utc)
    if maintenance.active(data_dir):
        return []
    path = data_dir / "heartbeat.json"
    if not path.exists():
        return ["No scheduler heartbeat yet: the AIMarketingEmployee-Tick task may not be installed."]
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(beat["at"].rstrip("Z")).replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError):
        return ["Scheduler heartbeat file is unreadable."]
    age = now - at
    if age > STALE_AFTER:
        return [f"Scheduled ticks stopped: last tick {int(age.total_seconds() // 60)} min ago. "
                "Check Task Scheduler (AIMarketingEmployee-Tick) and data/logs/scheduler_crash.log."]
    return []


def dashboard_problem(host: str = "127.0.0.1", port: int | None = None) -> str | None:
    import socket

    from dotenv import dotenv_values

    port = port or int(os.environ.get("DASHBOARD_PORT") or dotenv_values(ROOT / ".env").get("DASHBOARD_PORT") or 8765)
    try:
        with socket.create_connection((host, port), timeout=2):
            return None
    except OSError:
        return (f"The dashboard is not running (nothing on port {port}). Start it with scripts/start_dashboard.ps1, "
                "or install `python -m aime scheduler install-dashboard` so it restarts by itself.")


def main() -> int:
    from aime import maintenance

    problems = check(DATA)
    if not maintenance.active(DATA):
        problems += [p for p in [dashboard_problem()] if p]
    if problems:
        from aime.notify import TITLE, toast

        log = DATA / "logs" / "watchdog.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            for p in problems:
                fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {p}\n")
        toast(f"{TITLE}: scheduler", problems[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
