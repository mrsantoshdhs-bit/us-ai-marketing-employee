"""Maintenance window: pauses scheduled ticks until a deadline (stdlib only, safe to import first).

The window always expires on its own, so an interrupted upgrade can never leave automation off.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

FILENAME = "MAINTENANCE.json"


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILENAME


def start(data_dir: Path, hours: float, reason: str) -> dict:
    until = datetime.now(timezone.utc) + timedelta(hours=max(0.1, min(hours, 24)))
    info = {"until": until.isoformat(), "reason": reason[:200]}
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info), encoding="utf-8")
    return info


def stop(data_dir: Path) -> bool:
    try:
        _path(data_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def active(data_dir: Path) -> dict | None:
    """Return the window if it is still in force; expired or unreadable windows are cleared."""
    path = _path(data_dir)
    if not path.exists():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        until = datetime.fromisoformat(info["until"])
    except (OSError, ValueError, KeyError):
        stop(data_dir)
        return None
    if until <= datetime.now(timezone.utc):
        stop(data_dir)
        return None
    return info
