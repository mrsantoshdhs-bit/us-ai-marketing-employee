"""Put a backup back in place of data/aime.db, with every step a person could get wrong (`aime restore`).

Done by hand it goes wrong in two ways: with the dashboard still running, the old -wal cannot be deleted on Windows while
the copy succeeds, and it lies over the restored file and damages it; and the restored database has forgotten what it sent
after the backup. So this refuses while anything holds the file, keeps a copy of what it replaces, and holds sending
until a person has confirmed (aime.send_journal). It sends nothing, and it never writes into the backup it reads.
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from aime import maintenance, send_journal
from aime.backup import BackupError, backup_sqlite_file, bring_back_documents, integrity_of_file, newest_good_backup, restore_drill, verify_backup

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
    return _stored_in(path, "db_role")


def _stored_in(path: Path, key: str) -> str | None:
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        row = con.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row:
        return None
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


def _journals_beside_the_bundle(settings, bundle: Path, new_pc: bool) -> None:
    """A bundle also holds the journals. What is already here is newer and stays: only what is missing (a new PC) is taken
    from the bundle. A sent record that has seen nothing, from a command run before this one, counts as missing."""
    if new_pc and (bundle / "sent_journal.jsonl").exists():
        shutil.copyfile(bundle / "sent_journal.jsonl", send_journal.journal_path(settings.data_dir))
    if (bundle / "publish_journal.jsonl").exists() and not (settings.data_dir / "publish_journal.jsonl").exists():
        shutil.copyfile(bundle / "publish_journal.jsonl", settings.data_dir / "publish_journal.jsonl")


def _files_beside_the_bundle(settings, bundle: Path) -> None:
    """Post images and report files, again only what is missing."""
    for held, here in ((bundle / "assets", settings.assets_dir), (bundle / "reports", settings.reports_dir)):
        if held.is_dir():
            here.mkdir(parents=True, exist_ok=True)
            for item in held.iterdir():
                if not (here / item.name).exists():
                    shutil.copyfile(item, here / item.name)


def _approved_in(path: Path) -> list[tuple]:
    """(id, address, message id, approved at) of every approved email in a closed database. A copy with no such table has no
    such email; a copy this cannot be read from goes no further, the present database not having been replaced."""
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'followups'").fetchone():
            return []
        return con.execute("SELECT f.id, COALESCE(f.to_email, c.email), f.message_id, f.approved_at FROM followups f "
                           "LEFT JOIN contacts c ON c.id = f.contact_id WHERE f.channel = 'email' AND f.status = 'approved'").fetchall()
    finally:
        con.close()


def _carry_the_moment(incoming: Path, at: str) -> None:
    """The database and the copies it makes from now on say which journal they lived with. An old copy with no such table has
    nowhere to say it; start() gives it the key."""
    con = sqlite3.connect(incoming)
    try:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kv_store'").fetchone():
            with con:
                con.execute("INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)",
                            (send_journal.NEW_PC_KEY, json.dumps({"v": at}), at.replace("T", " ")))
    finally:
        con.close()


def _hold_back_what_may_have_gone(settings, incoming: Path, new_pc: bool) -> tuple[bool, str | None]:
    """Where the copy was made on another PC (nothing here has seen an email, or the copy does not carry this journal's last
    new_pc moment), nothing can tell whether an email approved in it went out after the copy was made, and its dates prove
    nothing: every one goes to a person. Written before the database is replaced, so a crash half way loses none of it. A copy
    made here keeps to that moment: what was approved here since is known and is left alone. Returns whether drafts get the
    note, and the moment they must be older than (None for all of them)."""
    view = send_journal.read(settings.data_dir)
    if new_pc or _stored_in(incoming, send_journal.NEW_PC_KEY) != view.new_pc_at:
        at = send_journal.record_new_pc(settings.data_dir)
        _carry_the_moment(incoming, at)
        send_journal.record_unknown(settings.data_dir, send_journal.approved_before(view, None, _approved_in(incoming)))
        return True, None
    if view.new_pc_at:
        send_journal.record_unknown(settings.data_dir, send_journal.approved_before(view, view.new_pc_at, _approved_in(incoming)))
    return view.new_pc_at is not None, view.new_pc_at


def restore_database(settings, source: Path) -> dict:
    """Returns what was done. Raises RestoreRefused with nothing changed, unless the message says otherwise."""
    from aime.context import create_context
    from aime.crm_ops import log_activity
    from aime.doctor import run_checks
    from aime.models import Attachment, FollowUp

    db = _db_path(settings)
    if source.resolve() == db.resolve():
        raise RestoreRefused("That is the live database itself: choose a backup")
    new_pc = send_journal.looks_new(settings.data_dir)  # before anything here writes to it
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
        replaced = False
        try:
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
            if source.suffix in (".aimebak", ".zip"):
                _journals_beside_the_bundle(settings, Path(tmp) / "bundle", new_pc)
            note_drafts, drafts_before = _hold_back_what_may_have_gone(settings, incoming, new_pc)
            for side in SIDE_FILES:
                db.with_name(db.name + side).unlink(missing_ok=True)
            incoming.replace(db)
            replaced = True
            for side in SIDE_FILES:  # a copy taken from a live folder may have come with its own
                db.with_name(db.name + side).unlink(missing_ok=True)
            if source.suffix in (".aimebak", ".zip"):
                _files_beside_the_bundle(settings, Path(tmp) / "bundle")
        except (OSError, sqlite3.Error) as exc:
            incoming = db.with_name(db.name + ".restoring")
            try:
                incoming.unlink(missing_ok=True)
            except OSError:
                pass
            state = (f"The backup {source.name} IS in place, but a later step did not finish" if replaced else
                     "The present database was not replaced")
            copy = f" The database as it was is kept as {kept}." if kept else ""
            raise RestoreRefused(f"The restore stopped ({exc}). {state}.{copy} The maintenance window is on and sending is on hold "
                                 "(if that could be written): nothing runs or goes out. Free the disk or close what holds the "
                                 "file, then run this again.") from exc
    ctx = create_context(settings)  # applies any newer schema, with its own backup first
    try:
        with ctx.db.session() as s:
            view = send_journal.read(settings.data_dir)
            marked = send_journal.review_rows(s, view, "restore")
            unknown = [i for i in marked if send_journal.claim_for(view, s.get(FollowUp, i)).get("unknown")]
            already = [i for i in marked if i not in unknown]
            if note_drafts:
                send_journal.note_drafts(s, view, drafts_before)
            documents = s.execute(select(Attachment.stored_as, Attachment.sha256, Attachment.filename)).all()
            back, lost = bring_back_documents(settings, documents, source.parent)
            waiting = s.execute(select(func.count(FollowUp.id)).where(FollowUp.channel == "email", FollowUp.status == "approved")).scalar_one()
            log_activity(s, f"Database restored from {source.name}; sending held until a person confirms", "command line", type_="security")
        checks = [c for c in run_checks(ctx) if not c["ok"] and c["severity"] == "error"]
    finally:
        ctx.db.dispose()
    return {"source": source.name, "kept": kept, "already_sent": already, "unknown_sent": unknown, "approved_waiting": waiting,
            "failing": checks, "documents_back": back, "documents_missing": lost}
