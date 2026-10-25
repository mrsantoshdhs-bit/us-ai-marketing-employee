"""Append-only, fsync'd record of confirmed publications, independent of the database.

Written immediately after LinkedIn confirms a post and before the database is updated, so a crash or
database failure in between can be reconciled without re-posting.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from aime.timeutil import utcnow


def journal_path(data_dir: Path) -> Path:
    return Path(data_dir) / "publish_journal.jsonl"


def record_success(data_dir: Path, attempt_id: str, content_id: int, external_id: str) -> None:
    path = journal_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"attempt_id": attempt_id, "content_id": content_id, "external_id": external_id,
                       "at": utcnow().isoformat() + "Z"})
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def confirmed(data_dir: Path) -> dict[str, dict]:
    """attempt_id -> record for every confirmed publication."""
    path = journal_path(data_dir)
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a torn last line from a crash is ignored
        if rec.get("attempt_id") and rec.get("external_id"):
            out[rec["attempt_id"]] = rec
    return out
