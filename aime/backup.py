"""Online SQLite backups, verification, encrypted off-machine bundles, restore drills, WAL checkpoints.

Local backups (data/backups/aime-*.db) are verified right after they are written. An optional off-machine
copy is a zip bundle (database snapshot, publish and sent journals, post images, config, manifest with SHA-256 per
file) encrypted with AES-256-GCM under a key derived from BACKUP_PASSPHRASE (scrypt). The restore drill
rebuilds a backup in a temporary folder and checks it; it never touches the live data folder.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from aime.db import Database
from aime.redact import redact

MAGIC = b"AIMEBAK1"
OFFSITE_PREFIX = "aime-offsite-"
OFFSITE_KEEP = 30
OFFSITE_PROBLEM = "backup.offsite_problem"  # why the last off-machine copy failed, in words; empty once one is written
OFFSITE_KEY_CHANGED = "backup.offsite_key_changed"  # the last copy that does not open with today's passphrase
MIN_BLOB = len(MAGIC) + 28  # the header, the salt, the nonce and the tag: what any encrypted copy has at least
LEFTOVER_AGE_SECONDS = 3600  # a half-written file this old has no writer left: a copy takes seconds
MIN_PASSPHRASE = 12
MIN_FREE_BYTES = 2 * 2**30
DAMAGE_WORDS = ("malformed", "not a database")  # what SQLite says of a damaged file; anything else may pass by itself
DRILL_EVERY_DAYS = 7
PREDEPLOY_KEPT = 5  # the newest pre-deploy copies, whatever BACKUP_KEEP_MANUAL says: what a rollback goes back to


class BackupError(Exception):
    pass


def _clear_leftovers(folder: Path, *patterns: str) -> list[str]:
    """Half-written files of this tool that a power cut left behind. By name only (nothing else in the folder is ever touched)
    and only old ones, by the time in the name, or the file's own time if it has none: a copy being written now is young."""
    removed = []
    for pattern in patterns:
        for left in folder.glob(pattern):
            stamp = re.search(r"\d{8}-\d{6}", left.name)
            try:
                born = datetime.strptime(stamp.group(), "%Y%m%d-%H%M%S").timestamp() if stamp else left.stat().st_mtime
            except (OSError, ValueError):
                continue
            if abs(time.time() - born) > LEFTOVER_AGE_SECONDS and _removed(left):
                removed.append(left.name)
    return removed


def backup_sqlite_file(db_path: Path, backup_dir: Path, label: str = "auto") -> Path:
    """Consistent copy of a live database using SQLite's online backup API (safe while in use)."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    _clear_leftovers(backup_dir, "partial-[0-9]*-aime-*.db", "partial-[0-9]*-aime-*.db-journal")
    target = backup_dir / f"aime-{label}-{datetime.now():%Y%m%d-%H%M%S}.db"
    # A failed copy (a full disk) must not look like a backup. Two processes can back up in one second (a tick and the
    # dashboard, both starting on a new schema): each writes its own file.
    partial = target.with_name(f"partial-{os.getpid()}-{target.name}")
    try:
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(partial))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        try:
            partial.replace(target)
        except PermissionError:
            if not target.exists():  # the other process's copy of this same second is in place, and open: that one stands
                raise
            partial.unlink()
    except BaseException:
        for leftover in (partial, partial.with_name(partial.name + "-journal")):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return target


def prune_manual_backups(backup_dir: Path, keep: int, min_age_days: int = 30, now=None, keep_predeploy: int = 0) -> list[str]:
    """BACKUP_KEEP_MANUAL > 0: keep the newest `keep` manual, pre-deploy and pre-migration copies, and the newest
    `keep_predeploy` pre-deploy copies among them; never one younger than `min_age_days`, never the automatic ones (they
    have their own rotation), and never the last copy that still verifies. Also clears empty -wal/-shm files left beside
    copies. Returns the removed names."""
    if keep <= 0:
        return []
    removed = []
    for old in _manual_to_remove(backup_dir, keep, min_age_days, now, keep_predeploy):
        for path in (old, old.with_name(old.name + "-wal"), old.with_name(old.name + "-shm")):  # with what lies beside it
            if path.exists() and _removed(path):
                removed.append(path.name)
    return removed + _clear_empty_side_files(backup_dir)


def _removed(path: Path) -> bool:
    """A file another program holds stays until the next run: tidying up must never fail the backup job."""
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _clear_empty_side_files(backup_dir: Path) -> list[str]:
    removed = []
    for side in list(backup_dir.glob("aime-*.db-wal")) + list(backup_dir.glob("aime-*.db-shm")):
        if side.stat().st_size == 0 and _removed(side):
            removed.append(side.name)
    return removed


def _manual_to_remove(backup_dir: Path, keep: int, min_age_days: int, now, keep_predeploy: int) -> list[Path]:
    now = now or time.time()
    manual = sorted((p for p in backup_dir.glob("aime-*.db") if not p.name.startswith("aime-auto-")),
                    key=lambda p: p.stat().st_mtime)
    predeploy = [p for p in manual if p.name.startswith(("aime-pre-deploy-", "aime-predeploy-"))]
    stays = {*manual[-keep:], *(predeploy[-keep_predeploy:] if keep_predeploy > 0 else [])}
    remove = [p for p in manual if p not in stays and now - p.stat().st_mtime >= min_age_days * 86400]
    if remove:  # newest first: normally the first one answers
        others = sorted((p for p in backup_dir.glob("aime-*.db") if p not in remove), key=lambda p: p.stat().st_mtime, reverse=True)
        if not any(verify_backup(p)["ok"] for p in others):
            last_good = next((p for p in reversed(remove) if verify_backup(p)["ok"]), None)
            remove = [p for p in remove if p != last_good]
    return remove


def prune_backups(backup_dir: Path, label: str = "auto", keep: int = 14) -> int:
    """Keep the newest `keep` copies of a label. The last copy that still verifies is never removed, however old:
    after a run of damaged backups (a failing disk, a corrupt database) it is the only way back."""
    files = sorted(backup_dir.glob(f"aime-{label}-*.db"))
    expired, kept = (files[:-keep], files[-keep:]) if keep > 0 else (files, [])
    last_good = None
    if expired and not any(verify_backup(p)["ok"] for p in reversed(kept)):  # newest first: normally the first answers
        last_good = next((p for p in reversed(expired) if verify_backup(p)["ok"]), None)
    removed = 0
    for old in expired:
        if old != last_good and _removed(old):
            removed += 1
    return removed


def integrity_check(db: Database) -> dict:
    """A file too damaged to answer is a failed check, not an exception: that is what this check is there to report. A check
    that could not run just now (locked, an I/O error, cannot be opened) says nothing about the file: "passing" marks it."""
    try:
        with db.engine.connect() as conn:
            integrity = conn.execute(text("PRAGMA integrity_check")).scalar()
            fk = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
    except (DatabaseError, sqlite3.Error) as exc:
        said = getattr(exc, "orig", None) or exc
        result = {"integrity": f"unreadable: {said}", "foreign_key_violations": 0, "ok": False}
        if not any(word in str(said).lower() for word in DAMAGE_WORDS):
            result["passing"] = True
        return result
    return {"integrity": integrity, "foreign_key_violations": len(fk), "ok": integrity == "ok" and not fk}


def checkpoint(db: Database) -> None:
    with db.engine.connect() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))


# ---------------------------------------------------------------------- verification
def verify_backup(path: Path) -> dict:
    """Open a backup read-only (no -wal/-shm side files) and check integrity and table counts."""
    if not path.exists() or path.stat().st_size == 0:
        return {"ok": False, "integrity": "missing or empty file", "tables": 0, "rows": 0, "counts": {}}
    try:
        con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        return {"ok": False, "integrity": f"unreadable: {exc}", "tables": 0, "rows": 0, "counts": {}}
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        # Table names come from the file's own schema; quote them as identifiers (doubling any embedded quote).
        counts = {n: con.execute('SELECT COUNT(*) FROM "{}"'.format(n.replace('"', '""'))).fetchone()[0]  # noqa: S608
                  for n in names}
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "integrity": f"unreadable: {exc}", "tables": 0, "rows": 0, "counts": {}}
    finally:
        con.close()
    return {"ok": integrity == "ok" and bool(counts), "integrity": integrity, "tables": len(counts),
            "rows": sum(counts.values()), "counts": counts}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_commit(base_dir: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=base_dir, capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------------- bundles
def create_bundle(settings, db_path: Path, out_dir: Path) -> Path:
    """Zip a fresh DB snapshot, the publish journal, post images and config, with a SHA-256 manifest.

    .env is never included: secrets stay in your password manager, not in backups.
    """
    from aime.migrations import SCHEMA_VERSION

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aime-bundle-") as tmp:
        snap = backup_sqlite_file(db_path, Path(tmp), label="bundle")
        check = verify_backup(snap)
        if not check["ok"]:
            raise BackupError(f"database snapshot failed verification: {check['integrity']}")
        members: list[tuple[str, Path]] = [("aime.db", snap)]
        for name in ("publish_journal.jsonl", "sent_journal.jsonl"):
            journal = settings.data_dir / name
            if journal.exists():
                members.append((name, journal))
        if settings.assets_dir.exists():
            members += [(f"assets/{p.name}", p) for p in sorted(settings.assets_dir.iterdir())
                        if p.suffix.lower() in (".png", ".jpg", ".jpeg")]
        config_dir = settings.base_dir / "config"
        if config_dir.exists():
            members += [(f"config/{p.name}", p) for p in sorted(config_dir.glob("*.toml"))]
        manifest = {"format": 1, "created_at": datetime.now().isoformat(timespec="seconds"),
                    "git_commit": _git_commit(settings.base_dir), "schema_version": SCHEMA_VERSION,
                    "db": {k: check[k] for k in ("tables", "rows", "counts")}, "files": {}}
        target = out_dir / f"aime-bundle-{datetime.now():%Y%m%d-%H%M%S}.zip"
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for arcname, path in members:
                data = path.read_bytes()
                manifest["files"][arcname] = _sha256(data)
                zf.writestr(arcname, data)
            zf.writestr("manifest.json", json.dumps(manifest, indent=1))
    return target


def _key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024,
                          dklen=32)


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # optional dependency: off-site copies need it, local backups don't
        raise BackupError("encryption library missing: install the 'cryptography' package") from exc
    return AESGCM


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + _aesgcm()(_key(passphrase, salt)).encrypt(nonce, data, MAGIC)


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 28:
        raise BackupError("not an AIME encrypted backup")
    salt, nonce, body = blob[8:24], blob[24:36], blob[36:]
    try:
        return _aesgcm()(_key(passphrase, salt)).decrypt(nonce, body, MAGIC)
    except BackupError:
        raise
    except Exception as exc:  # cryptography.exceptions.InvalidTag: wrong passphrase or damaged file
        raise BackupError("cannot decrypt: wrong passphrase or damaged file") from exc


def _cloud_only(path: Path) -> bool:
    """A file a sync client keeps online only: opening it would download it. Nothing here does that."""
    attributes = getattr(os.stat(path), "st_file_attributes", 0)  # Windows: offline, recall on open, recall on data access
    return bool(attributes & (0x1000 | 0x40000 | 0x400000))


def copy_opens(path: Path, passphrase: str | None = None) -> bool | None:
    """Can this encrypted copy still be read? True or False; None when that cannot be told without a download, or just now.
    With no passphrase only its beginning and size are read; with one, it is decrypted."""
    try:
        if _cloud_only(path):
            return None
        if not passphrase:
            with path.open("rb") as fh:
                return fh.read(len(MAGIC)) == MAGIC and path.stat().st_size >= MIN_BLOB
        decrypt_bytes(path.read_bytes(), passphrase)
        return True
    except BackupError:
        return False
    except OSError:  # gone, or held by another program for a moment
        return None


def prune_offsite(folder: Path, keep: int = OFFSITE_KEEP, passphrase: str | None = None, known_good: Path | None = None) -> int:
    """Remove only this tool's own older encrypted copies; any other file in the folder is left alone. The newest copy that
    really opens is never removed, however old: 30 newer files a sync client cut short are no way back. A copy another program
    holds stays until the next run. Cloud-only copies are not downloaded to judge them: one that might open stays."""
    files = sorted(folder.glob(f"{OFFSITE_PREFIX}*.aimebak"))
    expired, kept = (files[:-keep], files[-keep:]) if keep > 0 else ([], files)
    if not expired:
        return 0
    last_good = None
    if known_good not in kept and not any(copy_opens(p, passphrase) for p in reversed(kept)):  # newest first: the first answers
        last_good = next((p for p in reversed(expired) if copy_opens(p, passphrase) is not False), None)
    return sum(1 for old in expired if old != last_good and _removed(old))


def offsite_status(settings) -> tuple[bool, str]:
    if not settings.backup_offsite_dir:
        return False, "not configured (BACKUP_OFFSITE_DIR is empty)"
    if len(settings.backup_passphrase or "") < MIN_PASSPHRASE:
        return False, f"not configured (BACKUP_PASSPHRASE must be at least {MIN_PASSPHRASE} characters)"
    try:
        _aesgcm()
    except BackupError as exc:
        return False, str(exc)
    return True, "ready"


def onedrive_folders() -> list[Path]:
    """Folders that a signed-in OneDrive account syncs (Windows). Empty elsewhere or when nobody is signed in."""
    try:
        import winreg
    except ImportError:
        return []
    found: list[Path] = []
    try:
        accounts = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\OneDrive\Accounts")
    except OSError:
        return []
    with accounts:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(accounts, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(accounts, name) as key:
                    folder, _ = winreg.QueryValueEx(key, "UserFolder")
            except OSError:
                continue
            if folder:
                found.append(Path(folder))
    return found


def offsite_sync(folder: str | Path, synced: list[Path] | None = None) -> tuple[bool | None, str]:
    """Whether copies written to `folder` actually leave this PC.

    True: inside a folder a signed-in OneDrive account syncs. False: its drive is not connected, or a OneDrive path that no
    signed-in account syncs, so the copies stay on this PC. None: another kind of folder (USB drive, network share) that this
    check cannot judge.
    """
    path = Path(folder).expanduser().resolve()
    roots = [root.expanduser().resolve() for root in (onedrive_folders() if synced is None else synced)]
    for synced_root in roots:
        if path == synced_root or synced_root in path.parents:
            return True, f"synced by OneDrive ({synced_root})"
    if path.anchor and not Path(path.anchor).exists():
        return False, (f"The drive {path.anchor} is not connected, so no copy can be written to this folder: connect the drive, "
                       "or choose a folder that is there")
    if any("onedrive" in part.lower() for part in path.parts):
        if roots:  # signed in, but this folder only sits beside what it syncs
            return False, (f"OneDrive is signed in but syncs only {', '.join(str(r) for r in roots)}. This folder is outside "
                           "that, so copies would stay on this PC: choose a folder inside it")
        return False, ("OneDrive is not signed in for this folder, so copies would stay on this PC: sign in to "
                       "OneDrive (cloud icon in the taskbar) and check the folder shows as synced")
    return None, "not a OneDrive folder: check yourself that it is on another device (USB drive, network share)"


def offsite_trouble(exc: BaseException, folder: str | Path) -> str:
    """Why an off-machine copy could not be written, in words."""
    if isinstance(exc, (FileNotFoundError, NotADirectoryError, FileExistsError)):
        anchor = Path(folder).expanduser().anchor
        if anchor and not Path(anchor).exists():
            return f"the off-machine folder {folder} cannot be reached: the drive {anchor} is not connected"
        return f"the off-machine folder {folder} cannot be reached: it, or a folder above it, is not there"
    if isinstance(exc, PermissionError):
        return (f"the off-machine folder {folder} refused the file: this PC may not write there, or another program "
                "(a sync client) was holding it")
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return f"the off-machine folder {folder} has no room left"
    if isinstance(exc, OSError):
        return f"the off-machine folder {folder} could not be written ({type(exc).__name__})"
    return f"the copy for the off-machine folder {folder} could not be made: {redact(str(exc))}"


def try_write_offsite(settings, db_path: Path) -> dict:
    """write_offsite for a job or a person: a failure comes back as a result, with the reason in words."""
    try:
        return write_offsite(settings, db_path)
    except Exception as exc:  # the local backup stands whatever happens to this copy
        return {"status": "error", "reason": offsite_trouble(exc, settings.backup_offsite_dir)}


def offsite_problem_words(problem: dict, failures: int = 0) -> str:
    """The whole story for a person, from what db_maintenance recorded."""
    times = f" {failures} times in a row" if failures > 1 else ""
    local = "The local backup is fine" if problem.get("local_ok") else "The local backup needs a look too"
    return f"{local}, but the off-machine copy failed{times}: {problem.get('why')}."


def offsite_copy_problem(settings, record: dict | None) -> str | None:
    """What is wrong with the newest copy in the off-machine folder, or None: not only the record of it. It is decrypted, which
    takes a fraction of a second for a local file; a cloud-only file is not downloaded for this."""
    folder = Path(settings.backup_offsite_dir).expanduser()
    files = sorted(folder.glob(f"{OFFSITE_PREFIX}*.aimebak"))
    if not files:
        return f"no copy is left in the off-machine folder {folder}: the last one was written as {(record or {}).get('file')}"
    newest = files[-1]
    if record and record.get("file", "") > newest.name:
        return f"the newest copy, {record['file']}, is no longer in the off-machine folder {folder}"
    try:
        size = newest.stat().st_size
    except OSError:
        return None  # another program has it for a moment
    if size < MIN_BLOB or (record and record.get("file") == newest.name and record.get("bytes") not in (None, size)):
        return f"the newest copy, {newest.name}, is cut short: a sync client or a full disk may have damaged it"
    if copy_opens(newest) is False:
        return f"the newest copy, {newest.name}, is not a whole encrypted backup: a sync client may have damaged it"
    if copy_opens(newest, settings.backup_passphrase) is False:
        return (f"the newest copy, {newest.name}, does not open with the backup passphrase in use now: the passphrase has changed "
                "since it was written, or the file is damaged")
    return None


def _first_copy_the_passphrase_cannot_open(files: list[Path], passphrase: str) -> str | None:
    """The newest of the earlier copies does not open with today's passphrase: it was changed, or that copy is damaged."""
    return files[-1].name if files and copy_opens(files[-1], passphrase) is False else None


def write_offsite(settings, db_path: Path) -> dict:
    """Encrypted bundle into BACKUP_OFFSITE_DIR, then decrypt it again to prove it can be restored. Once the copy is in place
    nothing here can fail it: tidying up comes after, and a file another program holds simply waits."""
    ready, reason = offsite_status(settings)
    if not ready:
        return {"status": "not_configured", "reason": reason}
    folder = Path(settings.backup_offsite_dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    _clear_leftovers(folder, f"{OFFSITE_PREFIX}*.partial")
    earlier = sorted(folder.glob(f"{OFFSITE_PREFIX}*.aimebak"))
    with tempfile.TemporaryDirectory(prefix="aime-offsite-") as tmp:
        bundle = create_bundle(settings, db_path, Path(tmp))
        data = bundle.read_bytes()
        target = folder / f"{OFFSITE_PREFIX}{datetime.now():%Y%m%d-%H%M%S}.aimebak"
        partial = target.with_suffix(".partial")
        try:
            encrypted = encrypt_bytes(data, settings.backup_passphrase)
            partial.write_bytes(encrypted)
            if _sha256(decrypt_bytes(partial.read_bytes(), settings.backup_passphrase)) != _sha256(data):
                raise BackupError("encrypted copy did not decrypt to the same bundle")
            partial.replace(target)
        except BaseException:
            _removed(partial)
            raise
    result = {"status": "ok", "file": target.name, "bytes": len(encrypted), "pruned": 0,
              "key_changed_after": _first_copy_the_passphrase_cannot_open(earlier, settings.backup_passphrase)}
    try:
        result["pruned"] = prune_offsite(folder, passphrase=settings.backup_passphrase, known_good=target)
    except OSError:  # today's copy stands: the old ones go next time
        pass
    return result


# ---------------------------------------------------------------------- restore drill
def latest_local_backup(backup_dir: Path) -> Path | None:
    files = sorted((p for p in backup_dir.glob("aime-*.db") if p.stat().st_size > 0), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def newest_good_backup(backup_dir: Path, skip: set[str] | None = None) -> Path | None:
    """The newest copy that passes its check today. Checked now: a file can go bad after it was verified. What a restore
    set aside is not offered: it is what a person chose to replace."""
    files = sorted((p for p in backup_dir.glob("aime-*.db") if p.stat().st_size > 0 and p.name not in (skip or ())
                    and not p.name.startswith("aime-pre-restore-")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return next((p for p in files if verify_backup(p)["ok"]), None)


def integrity_of_file(path: Path) -> str:
    """The integrity check of a closed database file, as text; "ok" when sound."""
    return verify_backup(path)["integrity"]


def free_space_problem(folder: Path, minimum: int = MIN_FREE_BYTES) -> str | None:
    """Words for the owner when the disk is nearly full (backups, the database and the log files all fail there)."""
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return None
    if free >= minimum:
        return None
    return (f"Only {free / 2**30:.1f} GB is free on the disk holding {folder}. Under {minimum / 2**30:.0f} GB backups and the "
            "database start to fail: free some space.")


def weekly_restore_drill(ctx) -> dict | None:
    """A job step, once a week: the newest local backup is restored into a temporary folder and checked. A file gone bad
    is then found while a good copy is still there. None when it is not time yet."""
    from aime.db import kv_get, kv_set
    from aime.timeutil import utcnow

    with ctx.db.session() as s:
        last = kv_get(s, "backup.last_drill")
    try:
        if last and (utcnow() - datetime.fromisoformat(last)).days < DRILL_EVERY_DAYS:
            return None
    except (TypeError, ValueError):
        pass
    try:
        result = restore_drill(ctx.settings)
    except (BackupError, OSError) as exc:
        result = {"ok": False, "source": None, "detail": str(exc)}
    step = {"agent": "restore_drill", "status": "success" if result["ok"] else "failed", "output": result}
    if result["ok"]:
        with ctx.db.session() as s:
            kv_set(s, "backup.last_drill", utcnow().isoformat())
    else:
        step["error"] = f"The weekly restore drill failed on {result.get('source')}: {result['detail']}"
    return step


def weekly_offsite_drill(ctx) -> dict | None:
    """A job step, once a week: the newest off-machine copy is restored in a temporary folder too. None when it is not time yet,
    the copies are not set up, or the newest is kept online only, which is not downloaded for this."""
    from aime.db import kv_get, kv_set
    from aime.timeutil import utcnow

    if not offsite_status(ctx.settings)[0]:
        return None
    with ctx.db.session() as s:
        last = kv_get(s, "backup.last_offsite_drill")
    try:
        if last and (utcnow() - datetime.fromisoformat(last)).days < DRILL_EVERY_DAYS:
            return None
    except (TypeError, ValueError):
        pass
    try:
        files = sorted(Path(ctx.settings.backup_offsite_dir).expanduser().glob(f"{OFFSITE_PREFIX}*.aimebak"))
        if not files or _cloud_only(files[-1]):
            return None
        result = restore_drill(ctx.settings, source=files[-1])
    except (BackupError, OSError) as exc:
        result = {"ok": False, "source": files[-1].name if files else None, "detail": str(exc)}
    step = {"agent": "offsite_drill", "status": "success" if result["ok"] else "failed", "output": result}
    if result["ok"]:
        with ctx.db.session() as s:
            kv_set(s, "backup.last_offsite_drill", utcnow().isoformat())
    else:
        step["error"] = f"the newest copy, {result.get('source')}, failed the weekly restore check: {result['detail']}"
    return step


def restore_drill(settings, source: Path | None = None, extract_to: Path | None = None) -> dict:
    """Restore a backup into a temporary folder and check it. The live data folder is never written.

    With `extract_to` (a new folder outside data/), a backup that passes the checks is also copied there,
    ready for the documented manual restore.
    """
    t0 = time.perf_counter()
    if extract_to is not None:
        target = extract_to.resolve()
        if target.exists():
            raise BackupError(f"{extract_to} already exists; choose a new folder")
        if target == settings.data_dir.resolve() or settings.data_dir.resolve() in target.parents:
            raise BackupError("extract outside the live data folder")
    chosen = source
    source = source or latest_local_backup(settings.backups_dir)
    if source is None:
        return {"ok": False, "source": None, "detail": "no backup found"}
    with tempfile.TemporaryDirectory(prefix="aime-restore-drill-") as tmp:
        work = Path(tmp)
        expected = None
        if source.suffix in (".aimebak", ".zip"):
            try:
                data = source.read_bytes()
                if source.suffix == ".aimebak":
                    data = decrypt_bytes(data, settings.backup_passphrase)
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    manifest = json.loads(zf.read("manifest.json"))
                    for name, digest in manifest["files"].items():
                        if _sha256(zf.read(name)) != digest:
                            return {"ok": False, "source": source.name, "detail": f"checksum mismatch in {name}"}
                    zf.extractall(work)
                expected = manifest["db"]["counts"]
            except (zipfile.BadZipFile, zlib.error, EOFError, OSError, KeyError, TypeError, ValueError) as exc:
                return {"ok": False, "source": source.name, "detail": f"damaged bundle ({type(exc).__name__}: {exc})"}
            restored = work / "aime.db"
        else:
            restored = work / "aime.db"
            shutil.copy2(source, restored)
        check = verify_backup(restored)
        counts_ok = expected is None or check["counts"] == expected
        ok = check["ok"] and counts_ok
        detail = (f"integrity={check['integrity']}, {check['tables']} tables, {check['rows']} rows"
                  + ("" if counts_ok else ", row counts differ from the manifest"))
        if ok and extract_to is not None:
            shutil.copytree(work, extract_to)
            detail += f"; extracted to {extract_to}"
        if not ok and chosen is None:  # the newest copy has gone bad: say which one to use instead
            better = newest_good_backup(settings.backups_dir, skip={source.name})
            detail += f"; the newest good copy is {better.name}" if better else "; no other copy passes its check"
    return {"ok": ok, "source": source.name, "detail": detail, "tables": check["tables"], "rows": check["rows"],
            "seconds": round(time.perf_counter() - t0, 2)}
