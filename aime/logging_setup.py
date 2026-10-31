"""Logging: rotating text log plus a JSON-lines log of agent runs."""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from aime.redact import RedactingFilter

_CONFIGURED: set[str] = set()
ROTATE_RETRY_SECONDS = 60.0


class KeepingRotatingHandler(RotatingFileHandler):
    """The standard handler moves the older logs along first and only then finds that another process holds the current one: on
    Windows the line is lost and the oldest log with it, again at every line. Here the current file moves aside first (see
    `rotate`): while it is held nothing moves, the older logs stay, and lines keep going onto the end of it."""

    _next_try = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> int:
        if time.monotonic() < self._next_try:  # it was held a moment ago: not at every line
            return 0
        return super().shouldRollover(record)

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        if not rotate(Path(self.baseFilename), 0, self.backupCount):  # it is due: the handler has judged the size
            self._next_try = time.monotonic() + ROTATE_RETRY_SECONDS
        if not self.delay:
            self.stream = self._open()


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    key = str(logs_dir.resolve())
    if key in _CONFIGURED:
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aime")
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = KeepingRotatingHandler(logs_dir / "aime.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
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
    """Past max_bytes: path -> path.1 -> ... -> path.<keep>; the oldest goes. Logs only, never business data. The current file
    moves aside first: while a reader holds it nothing moves, so a held file costs no older copy. If an older copy is held, the
    file that moved aside waits and goes first next time."""
    moving = path.with_name(f"{path.name}.rotating")
    try:
        if moving.exists():
            _shift_older_copies(path, moving, keep)
        if not path.exists() or path.stat().st_size < max_bytes:
            return False
        path.replace(moving)
    except OSError:
        return False  # another process holds it: try again next time
    try:
        _shift_older_copies(path, moving, keep)
    except OSError:
        pass
    return True


def _shift_older_copies(path: Path, moving: Path, keep: int) -> None:
    for i in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{i}")
        if older.exists():
            older.replace(path.with_name(f"{path.name}.{i + 1}"))
    moving.replace(path.with_name(f"{path.name}.1"))


_UNWRITABLE: set[str] = set()


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """A log that cannot be written (held by Excel, a full disk) must not stop the work it records: said once, in words."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        _UNWRITABLE.discard(str(path))
    except OSError as exc:
        if str(path) not in _UNWRITABLE:
            _UNWRITABLE.add(str(path))
            logging.getLogger("aime.logging").warning(
                "The log file %s cannot be written (%s): the work goes on, but its lines there are missing until another program "
                "lets go of the file or the disk has room again", path, type(exc).__name__)


DASHBOARD_LOG_MAX_BYTES = 5_000_000


class AppendingLog:
    """stdout and stderr of a dashboard with no console (pythonw). Each write opens the file, appends and closes it, so
    nothing holds the file between writes: on Windows it can then be rotated while the dashboard runs, and `aime scrub-logs`
    can put a cleaned copy in its place. A write that fails is dropped: logging must never stop the dashboard."""

    encoding = "utf-8"

    def __init__(self, path: Path, max_bytes: int = DASHBOARD_LOG_MAX_BYTES, keep: int = JSONL_KEEP):
        self.path, self.max_bytes, self.keep = path, max_bytes, keep
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(text)
            except OSError:
                pass
            if text.endswith("\n"):  # print() writes a line's end apart from the line: rotation keeps lines whole
                rotate(self.path, self.max_bytes, self.keep)
        return len(text)

    def writable(self) -> bool:
        return True

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


# An access line's request: uvicorn writes `"GET /path?query HTTP/1.1" 200 OK`.
# A query can hold anything a client sent, a quotation mark too, so it runs to the line's last version mark.
_REQUEST_WITH_QUERY = re.compile(rb'("(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) [^? \r\n]*)\?[^\r\n]*( HTTP/\d(?:\.\d)?")')
SCRUB_TRIES, SCRUB_PAUSE = 5, 0.2


def scrub_access_log(path: Path, sleep=time.sleep) -> dict[str, int | str]:
    """Take the query strings out of a log's access lines: they can hold sign-in codes and one-time links. Every other byte
    stays. Safe to run twice: the second run finds nothing to do and leaves the file alone. The cleaned copy goes to a temp
    file beside the log and replaces it whole, so a failure leaves the log as it was.

    While something appends: what arrived during the work is copied at the end, just before the replace. A process that
    holds the file open (a dashboard started by an older version) makes Windows refuse the replace: after a few tries the log
    is left as it was and the result says "in use". On Linux a holder cannot be seen, so stop it first there. What remains is
    a line written in the instant between the last copy and the replace. A temp file left by a run that was killed goes at the
    start of the next one. Returns counts only, never a line's content."""
    result: dict[str, int | str] = {"lines": 0, "changed": 0, "state": "clean"}
    for left in path.parent.glob(f"{glob.escape(path.name)}.scrub.*.tmp"):
        try:
            left.unlink(missing_ok=True)
        except OSError:  # a run still working holds it
            pass
    if not path.exists():
        result["state"] = "no file"
        return result
    temp = path.with_name(f"{path.name}.scrub.{os.getpid()}.tmp")
    try:
        with path.open("rb") as src, temp.open("wb") as dst:
            for line in src:
                cleaned, n = _REQUEST_WITH_QUERY.subn(rb"\1\2", line)
                result["lines"] += 1
                result["changed"] += bool(n)
                dst.write(cleaned)
            copied = src.tell()
        if not result["changed"]:
            return result
        for _ in range(SCRUB_TRIES):
            if path.stat().st_size < copied:
                result["state"], result["changed"] = "not done (the log was rotated meanwhile; run this again)", 0
                return result
            with path.open("rb") as src, temp.open("ab") as dst:
                src.seek(copied)
                rest = src.read()
                copied += len(rest)
                cleaned, n = _REQUEST_WITH_QUERY.subn(rb"\1\2", rest)
                result["lines"] += rest.count(b"\n")
                result["changed"] += n
                dst.write(cleaned)
                dst.flush()
                os.fsync(dst.fileno())
            try:
                temp.replace(path)
                result["state"] = "cleaned"
                return result
            except PermissionError:
                sleep(SCRUB_PAUSE)
        result["state"], result["changed"] = "in use", 0
    except OSError as exc:
        result["state"], result["changed"] = f"not done ({type(exc).__name__})", 0
    finally:
        temp.unlink(missing_ok=True)
    return result
