"""Online SQLite backups, integrity checks, and WAL checkpoints."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from aime.db import Database


def backup_sqlite_file(db_path: Path, backup_dir: Path, label: str = "auto") -> Path:
    """Consistent copy of a live database using SQLite's online backup API (safe while in use)."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"aime-{label}-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def prune_backups(backup_dir: Path, label: str = "auto", keep: int = 14) -> int:
    files = sorted(backup_dir.glob(f"aime-{label}-*.db"))
    removed = 0
    for old in files[:-keep] if keep > 0 else files:
        old.unlink()
        removed += 1
    return removed


def integrity_check(db: Database) -> dict:
    with db.engine.connect() as conn:
        integrity = conn.execute(text("PRAGMA integrity_check")).scalar()
        fk = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
    return {"integrity": integrity, "foreign_key_violations": len(fk), "ok": integrity == "ok" and not fk}


def checkpoint(db: Database) -> None:
    with db.engine.connect() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
