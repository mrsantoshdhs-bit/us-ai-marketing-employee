"""Entry point for Windows Task Scheduler (run with pythonw.exe: no console window)."""

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
DATA = Path(os.environ.get("AIME_DATA_DIR") or ROOT / "data")

try:
    from aime.process_priority import use_normal_priority

    use_normal_priority()  # before the slow imports: Task Scheduler starts tasks at low priority

    # Checked before importing the app, so an upgrade in progress cannot crash scheduled runs.
    from aime import maintenance

    window = maintenance.active(DATA)
    if window:
        from aime.session_info import session_kind

        beat = DATA / "heartbeat.json"
        try:
            previous = json.loads(beat.read_text(encoding="utf-8")).get("last_background_at")
        except (OSError, ValueError, AttributeError):  # cut short by a power cut, or not the shape it should be
            previous = None
        at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
        session = session_kind()
        try:
            beat.write_text(json.dumps({
                "at": at, "session": session, "trigger": "scheduler", "pid": os.getpid(), "state": "maintenance",
                "maintenance": window, "last_background_at": at if session == "background" else previous,
            }), encoding="utf-8")
        except OSError:  # read-only, or a full disk: the maintenance window still stands
            pass
        sys.exit(0)

    from aime.cli import main

    code = main(["tick", "--trigger", "scheduler", "--quiet"])
except SystemExit:
    raise
except Exception:  # pythonw swallows output, so record crashes on disk
    log = DATA / "logs" / "scheduler_crash.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n[{datetime.now().isoformat()}]\n{traceback.format_exc()}")
    except OSError:  # a full disk: the watchdog notices that ticks stopped
        pass
    code = 1
sys.exit(code)
