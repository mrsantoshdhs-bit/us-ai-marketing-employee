"""Logging: rotating text log plus a JSON-lines log of agent runs."""

from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from aime.redact import RedactingFilter

_CONFIGURED: set[str] = set()


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    key = str(logs_dir.resolve())
    if key in _CONFIGURED:
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aime")
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(logs_dir / "aime.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(RedactingFilter())
    logger.addHandler(file_handler)
    # pythonw (Task Scheduler) has no console streams
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        stream.setLevel(logging.WARNING)
        stream.addFilter(RedactingFilter())
        logger.addHandler(stream)
    _CONFIGURED.add(key)


JSONL_MAX_BYTES, JSONL_KEEP = 10_000_000, 5


def rotate(path: Path, max_bytes: int = JSONL_MAX_BYTES, keep: int = JSONL_KEEP) -> bool:
    """Past max_bytes: path -> path.1 -> ... -> path.<keep>; the oldest goes. Logs only, never business data."""
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return False
        for i in range(keep - 1, 0, -1):
            older = path.with_name(f"{path.name}.{i}")
            if older.exists():
                older.replace(path.with_name(f"{path.name}.{i + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
        return True
    except OSError:
        return False  # another process holds it: try again next time


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
