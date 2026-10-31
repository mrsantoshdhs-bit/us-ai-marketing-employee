"""Put a backup back in place of data/aime.db, with every step a person could get wrong (`aime restore`).

Done by hand it goes wrong in two ways: with the dashboard still running, the old -wal cannot be deleted on Windows while
the copy succeeds, and it lies over the restored file and damages it; and the restored database has forgotten what it sent
after the backup. So this refuses while anything holds the file, keeps a copy of what it replaces, and holds sending
until a person has confirmed (aime.send_journal). It sends nothing, and it never writes into the backup it reads.
"""

from __future__ import annotations

import shutil
import socket
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from aime import maintenance, send_journal
from aime.backup import BackupError, backup_sqlite_file, integrity_of_file, newest_good_backup, restore_drill, verify_backup

SIDE_FILES = ("-wal", "-shm")


class RestoreRefused(Exception):
    """Nothing was changed; the message says why and what to do."""


def _db_path(settings) -> Path:
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise RestoreRefused("Only a SQLite database can be restored with this command")
    return Path(url.database)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _files_held(db: Path) -> list[str]:
    """Which of the database's files another process has open. On Windows SQLite's own handles refuse a rename; elsewhere
    nothing refuses, and the lock files and the port are the check."""
    held = []
    for path in (db, *(db.with_name(db.name + s) for s in SIDE_FILES)):
        if not path.exists():
            continue
        probe = path.with_name(path.name + ".restore-check")
        try:
            path.replace(probe)
            probe.replace(path)
        except OSError:
            held.append(path.name)
    return held


def what_holds_it(settings, db: Path) -> list[str]:
    from aime.scheduler.runner import lock_owner_gone

    found = []
    if _port_open(settings.dashboard_host, settings.dashboard_port):
        found.append(f"the dashboard is running on port {settings.dashboard_port} (stop it: end the AIMarketingEmployee-Dashboard "
                     "task in Task Scheduler, or close its window)")
    for lock in sorted(settings.locks_dir.glob("*.lock")):
        try:
            if not lock_owner_gone(lock):
                found.append(f"a run is working now ({lock.stem}): wait for it to finish")
        except FileNotFoundError:
            continue
    return found


def _source_files(settings, source: Path, work: Path) -> Path:
    """The checked database to put in place, from a plain copy or an encrypted or zipped bundle. Raises RestoreRefused."""
    if not source.exists():
        raise RestoreRefused(f"{source} does not exist")
    if source.suffix in (".aimebak", ".zip"):
        try:
            result = restore_drill(settings, source, work / "bundle")
        except BackupError as exc:
            raise RestoreRefused(f"{source.name} cannot be used: {exc}") from exc
        if not result["ok"]:
            raise RestoreRefused(f"{source.name} failed its check: {result['detail']}")
        return work / "bundle" / "aime.db"
    check = verify_backup(source)
    if not check["ok"]:
        better = newest_good_backup(settings.backups_dir, skip={source.name})
        raise RestoreRefused(f"{source.name} failed its check ({check['integrity']})"
                             + (f". The newest good copy is {better.name}" if better else ""))
    return source


def _role_of(path: Path) -> str | None:
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        row = con.execute("SELECT value FROM kv_store WHERE key = 'db_role'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row:
        return None
    import json

    try:
        return json.loads(row[0]).get("v")
    except (ValueError, AttributeError):
        return None


def _keep_what_is_there(settings, db: Path) -> str | None:
    """A copy of the database being replaced, so a mistaken restore can be undone. A damaged one is kept under a name no
    backup rule treats as a backup. Raises RestoreRefused if there is no room: nothing has been changed yet."""
    if not db.exists() or db.stat().st_size == 0:
        return None
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    try:
        copy = backup_sqlite_file(db, settings.backups_dir, label="pre-restore")
        if verify_backup(copy)["ok"]:
            return copy.name
        damaged = copy.with_name(f"damaged-{datetime.now():%Y%m%d-%H%M%S}.db")
        copy.replace(damaged)
        return damaged.name
    except sqlite3.Error:
        damaged = settings.backups_dir / f"damaged-{datetime.now():%Y%m%d-%H%M%S}.db"
        try:
            shutil.copyfile(db, damaged)
            wal = db.with_name(db.name + "-wal")
            if wal.exists():
                shutil.copyfile(wal, damaged.with_name(damaged.name + "-wal"))
        except OSError as exc:
            raise RestoreRefused(f"No copy of the present database could be kept ({exc}); nothing was changed") from exc
        return damaged.name
    except OSError as exc:
        raise RestoreRefused(f"No copy of the present database could be kept ({exc}); nothing was changed") from exc


def _files_beside_the_bundle(settings, bundle: Path) -> None:
    """A bundle also holds the journals and post images. What is already here is newer and stays: only what is missing
    (a new PC) is taken from the bundle."""
    for name in ("sent_journal.jsonl", "publish_journal.jsonl"):
        if (bundle / name).exists() and not (settings.data_dir / name).exists():
            shutil.copyfile(bundle / name, settings.data_dir / name)
    if (bundle / "assets").is_dir():
        settings.assets_dir.mkdir(parents=True, exist_ok=True)
        for image in (bundle / "assets").iterdir():
            if not (settings.assets_dir / image.name).exists():
                shutil.copyfile(image, settings.assets_dir / image.name)


def restore_database(settings, source: Path) -> dict:
    """Returns what was done. Raises RestoreRefused with nothing changed, unless the message says otherwise."""
    from aime.context import create_context
    from aime.crm_ops import log_activity
    from aime.doctor import run_checks
    from aime.models import FollowUp

    db = _db_path(settings)
    if source.resolve() == db.resolve():
        raise RestoreRefused("That is the live database itself: choose a backup")
    with tempfile.TemporaryDirectory(prefix="aime-restore-") as tmp:
        checked = _source_files(settings, source, Path(tmp))
        role = _role_of(checked)
        if role and role != settings.data_role:
            raise RestoreRefused(f"{source.name} is a '{role}' database and this folder is '{settings.data_role}': refusing to mix them")
        problems = what_holds_it(settings, db)
        if problems:
            raise RestoreRefused("Something is using the database: " + "; ".join(problems) + ". Nothing was changed.")
        window_was_on = maintenance.active(settings.data_dir) is not None
        maintenance.start(settings.data_dir, 2, f"restore from {source.name}")  # the tasks start nothing from here on
        try:
            problems = what_holds_it(settings, db)
            held = _files_held(db)
            if problems or held:
                raise RestoreRefused("Something is using the database: " + "; ".join([*problems, *(f"another program has {n} open" for n in held)])
                                     + ". Close it and run this again. Nothing was changed.")
            kept = _keep_what_is_there(settings, db)
        except RestoreRefused:
            if not window_was_on:
                maintenance.stop(settings.data_dir)
            raise
        # From here on the database changes: sending is held first, so a crash half way sends nothing either.
        hold_was_on = send_journal.hold(settings.data_dir) is not None
        send_journal.set_hold(settings.data_dir, "restored", f"the database was restored from {source.name} and has forgotten "
                              "what it sent after that backup: check the Sent folder")
        incoming = db.with_name(db.name + ".restoring")
        shutil.copyfile(checked, incoming)
        check = integrity_of_file(incoming)
        if check != "ok":
            incoming.unlink(missing_ok=True)
            if not window_was_on:
                maintenance.stop(settings.data_dir)
            if not hold_was_on:
                send_journal.clear_hold(settings.data_dir)
            raise RestoreRefused(f"The copy of {source.name} failed its check ({check}). The present database was not replaced.")
        for side in SIDE_FILES:
            db.with_name(db.name + side).unlink(missing_ok=True)
        incoming.replace(db)
        for side in SIDE_FILES:  # a copy taken from a live folder may have come with its own
            db.with_name(db.name + side).unlink(missing_ok=True)
        if source.suffix in (".aimebak", ".zip"):
            _files_beside_the_bundle(settings, Path(tmp) / "bundle")
    ctx = create_context(settings)  # applies any newer schema, with its own backup first
    try:
        with ctx.db.session() as s:
            view = send_journal.read(settings.data_dir)
            already = send_journal.review_rows(s, view, "restore")
            waiting = s.execute(select(func.count(FollowUp.id)).where(FollowUp.channel == "email", FollowUp.status == "approved")).scalar_one()
            log_activity(s, f"Database restored from {source.name}; sending held until a person confirms", "command line", type_="security")
        checks = [c for c in run_checks(ctx) if not c["ok"] and c["severity"] == "error"]
    finally:
        ctx.db.dispose()
    return {"source": source.name, "kept": kept, "already_sent": already, "approved_waiting": waiting, "failing": checks}
