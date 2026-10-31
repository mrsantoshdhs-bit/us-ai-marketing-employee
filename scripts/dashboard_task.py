"""Keep the dashboard running (Windows Task Scheduler: at logon and every 15 minutes).

Starts `aime serve` unless the dashboard already answers on its port or a maintenance window is active
(deploys stop the dashboard on purpose). Each run either exits at once or becomes the dashboard process.
The task runs pythonw.exe, which has no console: the web server's output goes to data/logs/dashboard.log
(without somewhere to write, uvicorn stops at startup).
"""

import os
import socket
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

CHECKS, PAUSE = 3, 2.0  # a running dashboard answers every time; one that is stopping answers once, then is gone


def listening(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def still_running(host: str, port: int) -> bool:
    """A restart (End, then Run at once) can find the old dashboard still answering for a moment: this run would
    then exit and leave no dashboard until the next trigger. Only a dashboard that keeps answering counts."""
    for i in range(CHECKS):
        if not listening(host, port):
            return False
        if i < CHECKS - 1:
            time.sleep(PAUSE)
    return True


def ensure_output(log_path: Path) -> None:
    """Give a console-less process (pythonw.exe) a stdout and stderr: the dashboard log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    from aime.logging_setup import AppendingLog

    log = AppendingLog(log_path)
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


def note_problem(log_path: Path, text: str) -> None:
    """Under pythonw nothing else can show this. A log that cannot be written leaves nothing more to try."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] {text}\n")
    except OSError:
        pass


def main() -> int:
    try:
        return run()
    except Exception:  # before the settings are read nothing has given this process a place to write
        note_problem(Path(os.environ.get("AIME_DATA_DIR") or ROOT / "data") / "logs" / "dashboard.log", traceback.format_exc())
        raise


def run() -> int:
    from aime.process_priority import use_normal_priority

    use_normal_priority()  # Task Scheduler starts tasks at low priority; the dashboard answers people
    from aime import maintenance
    from aime.config import Settings

    settings = Settings.load()
    if maintenance.active(settings.data_dir):
        return 0
    if still_running(settings.dashboard_host, settings.dashboard_port):
        from aime.dashboard_probe import page_owner

        owner = page_owner(settings.dashboard_host, settings.dashboard_port)
        if owner == "other":  # a second copy could not take the port
            note_problem(settings.data_dir / "logs" / "dashboard.log",
                         f"Another web program is using port {settings.dashboard_port}: it answers, but it is not the dashboard. "
                         "Nothing was started: close that program, or give the dashboard another port.")
        elif owner is None:
            note_problem(settings.data_dir / "logs" / "dashboard.log",
                         f"Port {settings.dashboard_port} accepts connections but gives no page: another program holds it, or "
                         "a dashboard that was not started by this task has hung. Nothing was started.")
        return 0
    ensure_output(settings.data_dir / "logs" / "dashboard.log")
    from aime.cli import main as cli_main

    return cli_main(["serve"])


if __name__ == "__main__":
    sys.exit(main())
