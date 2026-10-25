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
    # Checked before importing the app, so an upgrade in progress cannot crash scheduled runs.
    from aime import maintenance

    window = maintenance.active(DATA)
    if window:
        (DATA / "heartbeat.json").write_text(json.dumps({
            "at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            "trigger": "scheduler", "pid": os.getpid(), "state": "maintenance", "maintenance": window,
        }), encoding="utf-8")
        sys.exit(0)

    from aime.cli import main

    code = main(["tick", "--trigger", "scheduler", "--quiet"])
except SystemExit:
    raise
except Exception:  # pythonw swallows output, so record crashes on disk
    log = DATA / "logs" / "scheduler_crash.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n[{datetime.now().isoformat()}]\n{traceback.format_exc()}")
    code = 1
sys.exit(code)
