"""File intake for inbox inquiries, company CSV imports and LinkedIn metrics exports.

A file is imported exactly once and archived only after its rows are committed:
1. read the file and add its rows to the session
2. record the file's SHA-256 in kv_store in the same transaction (`mark`)
3. commit, then move the file to processed/ (`IntakeBatch.archive`)
If anything fails before the commit, the rows roll back and the file stays where it was. If the move
fails after the commit, the next run recognises the file by its hash and only archives it.
Unreadable files are moved to rejected/ (never deleted) so one bad file can't block the folder.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from aime.db import kv_get, kv_set
from aime.timeutil import utcnow

log = logging.getLogger("aime.intake")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _key(kind: str, sha: str) -> str:
    return f"intake:{kind}:{sha[:48]}"


def seen(session: Session, kind: str, sha: str) -> bool:
    return kv_get(session, _key(kind, sha)) is not None


def mark(session: Session, kind: str, sha: str, name: str, rows: int) -> None:
    kv_set(session, _key(kind, sha), {"file": name, "rows": rows, "at": utcnow().isoformat()})


def move_aside(path: Path, folder: Path) -> Path:
    """Move a file into folder with a timestamp prefix; never overwrites an existing file."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    target = folder / f"{stamp}_{path.name}"
    n = 1
    while target.exists():
        target = folder / f"{stamp}_{n}_{path.name}"
        n += 1
    shutil.move(str(path), target)
    return target


@dataclass
class IntakeBatch:
    """Files read in this run, to be archived once the transaction that holds their rows has committed."""

    folder: Path
    files: list[Path] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    def archive(self) -> int:
        moved = 0
        for path in self.files:
            if not path.exists():
                continue
            try:
                move_aside(path, self.folder / "processed")
                moved += 1
            except OSError as exc:  # rows are committed and the hash is recorded: the next run archives it
                log.warning("Could not archive %s yet: %s", path.name, exc)
        return moved

    def reject(self, path: Path, reason: str) -> None:
        move_aside(path, self.folder / "rejected")
        self.rejected.append(f"{path.name}: {reason}")
