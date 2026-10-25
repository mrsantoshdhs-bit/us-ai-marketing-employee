"""Entry point for Windows Task Scheduler (run with pythonw.exe: no console window)."""

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

try:
    from aime.cli import main

    code = main(["tick", "--trigger", "scheduler", "--quiet"])
except Exception:  # pythonw swallows output, so record crashes on disk
    log = ROOT / "data" / "logs" / "scheduler_crash.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n[{datetime.now().isoformat()}]\n{traceback.format_exc()}")
    code = 1
sys.exit(code)
