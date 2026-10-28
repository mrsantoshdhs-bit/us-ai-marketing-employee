"""A1 probe, run once by the on-demand test task `AIMarketingEmployee-A1Test` (`python -m aime scheduler
test-unattended`), never by the production tasks.

It checks what a scheduled tick needs while the owner is signed out, and writes the answers to data/a1_probe.json:
which Windows session it ran in (background = session 0), whether the owner's Windows encryption keys (DPAPI)
work there (round trip of a random value: no real secret is read), whether the database opens (read-only), and
whether names resolve (DNS only; nothing is sent). It changes nothing else.
"""

import json
import os
import secrets
import socket
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def probe(data_dir: Path) -> dict:
    from aime.secure_store import protect, unprotect
    from aime.session_info import session_id, session_kind

    result = {"at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z", "session_id": session_id(),
              "session": session_kind(), "user": os.environ.get("USERNAME"), "errors": []}
    try:
        value = secrets.token_hex(16)
        result["dpapi"] = unprotect(protect(value)) == value
    except Exception as exc:  # the answer is the point: record it
        result["dpapi"], result["errors"] = False, result["errors"] + [f"dpapi: {type(exc).__name__}"]
    try:
        con = sqlite3.connect(f"file:{(data_dir / 'aime.db').as_posix()}?mode=ro", uri=True, timeout=10)
        result["database"] = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        con.close()
    except sqlite3.Error as exc:
        result["database"], result["errors"] = False, result["errors"] + [f"database: {type(exc).__name__}"]
    try:
        socket.getaddrinfo("www.sec.gov", 443)
        result["network"] = True
    except OSError:
        result["network"] = False
    return result


def main() -> int:
    from aime.config import Settings

    data_dir = Settings.load().data_dir
    out = probe(data_dir)
    (data_dir / "a1_probe.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
