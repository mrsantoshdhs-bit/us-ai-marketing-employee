"""Online SQLite backups, verification, encrypted off-machine bundles, restore drills, WAL checkpoints.

Local backups (data/backups/aime-*.db) are verified right after they are written. An optional off-machine
copy is a zip bundle (database snapshot, publish journal, post images, config, manifest with SHA-256 per
file) encrypted with AES-256-GCM under a key derived from BACKUP_PASSPHRASE (scrypt). The restore drill
rebuilds a backup in a temporary folder and checks it; it never touches the live data folder.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from aime.db import Database

MAGIC = b"AIMEBAK1"
OFFSITE_PREFIX = "aime-offsite-"
OFFSITE_KEEP = 30
MIN_PASSPHRASE = 12


class BackupError(Exception):
    pass


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
        journal = settings.data_dir / "publish_journal.jsonl"
        if journal.exists():
            members.append(("publish_journal.jsonl", journal))
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


def prune_offsite(folder: Path, keep: int = OFFSITE_KEEP) -> int:
    """Remove only this tool's own older encrypted copies; any other file in the folder is left alone."""
    files = sorted(folder.glob(f"{OFFSITE_PREFIX}*.aimebak"))
    removed = 0
    for old in files[:-keep] if keep > 0 else []:
        old.unlink()
        removed += 1
    return removed


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

    True: inside a folder a signed-in OneDrive account syncs. False: a OneDrive path that no signed-in account
    syncs, so the copies stay on this PC. None: another kind of folder (USB drive, network share) that this
    check cannot judge.
    """
    path = Path(folder).expanduser().resolve()
    for synced_root in onedrive_folders() if synced is None else synced:
        synced_root = synced_root.expanduser().resolve()
        if path == synced_root or synced_root in path.parents:
            return True, f"synced by OneDrive ({synced_root})"
    if any("onedrive" in part.lower() for part in path.parts):
        return False, ("OneDrive is not signed in for this folder, so copies would stay on this PC: sign in to "
                       "OneDrive (cloud icon in the taskbar) and check the folder shows as synced")
    return None, "not a OneDrive folder: check yourself that it is on another device (USB drive, network share)"


def write_offsite(settings, db_path: Path) -> dict:
    """Encrypted bundle into BACKUP_OFFSITE_DIR, then decrypt it again to prove it can be restored."""
    ready, reason = offsite_status(settings)
    if not ready:
        return {"status": "not_configured", "reason": reason}
    folder = Path(settings.backup_offsite_dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aime-offsite-") as tmp:
        bundle = create_bundle(settings, db_path, Path(tmp))
        data = bundle.read_bytes()
        target = folder / f"{OFFSITE_PREFIX}{datetime.now():%Y%m%d-%H%M%S}.aimebak"
        partial = target.with_suffix(".partial")
        try:
            partial.write_bytes(encrypt_bytes(data, settings.backup_passphrase))
            if _sha256(decrypt_bytes(partial.read_bytes(), settings.backup_passphrase)) != _sha256(data):
                raise BackupError("encrypted copy did not decrypt to the same bundle")
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    return {"status": "ok", "file": target.name, "bytes": target.stat().st_size, "pruned": prune_offsite(folder)}


# ---------------------------------------------------------------------- restore drill
def latest_local_backup(backup_dir: Path) -> Path | None:
    files = sorted((p for p in backup_dir.glob("aime-*.db") if p.stat().st_size > 0), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


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
    source = source or latest_local_backup(settings.backups_dir)
    if source is None:
        return {"ok": False, "source": None, "detail": "no backup found"}
    with tempfile.TemporaryDirectory(prefix="aime-restore-drill-") as tmp:
        work = Path(tmp)
        expected = None
        if source.suffix in (".aimebak", ".zip"):
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
    return {"ok": ok, "source": source.name, "detail": detail, "tables": check["tables"], "rows": check["rows"],
            "seconds": round(time.perf_counter() - t0, 2)}
