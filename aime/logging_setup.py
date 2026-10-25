"""Logging: rotating text log plus a JSON-lines log of agent runs."""

from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

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
    logger.addHandler(file_handler)
    # pythonw (Task Scheduler) has no console streams
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        stream.setLevel(logging.WARNING)
        logger.addHandler(stream)
    _CONFIGURED.add(key)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
