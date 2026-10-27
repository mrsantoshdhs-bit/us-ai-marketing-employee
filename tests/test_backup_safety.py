"""Backups are verified, off-machine copies are encrypted and restorable, and drills never touch live data."""

import hashlib
import io
import zipfile

import pytest

from aime.backup import (BackupError, backup_sqlite_file, create_bundle, decrypt_bytes, encrypt_bytes, offsite_sync,
                         prune_offsite, restore_drill, verify_backup, write_offsite)
from aime.config import Settings
from aime.db import kv_get
from aime.doctor import run_checks
from aime.models import Company
from aime.workflows import job_db_maintenance

pytest.importorskip("cryptography")
PASSPHRASE = "test-only passphrase 42"


def _db_path(ctx):
    from pathlib import Path
    return Path(ctx.db.engine.url.database)


def _seed(ctx, n=3):
    with ctx.db.session() as s:
        for i in range(n):
            s.add(Company(name=f"Sample {i}", normalized_name=f"sample {i}", source="test", state="TX"))


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_backup_is_verified_and_recorded(ctx):
    _seed(ctx)
    out = job_db_maintenance(ctx)
    assert not out.failed and out.steps[0]["output"]["backup_verified"]
    with ctx.db.session() as s:
        assert kv_get(s, "backup.last_verified")["rows"] > 0
    check = next(c for c in run_checks(ctx) if c["name"] == "Recent verified backup")
    assert check["ok"]


def test_corrupt_backup_is_detected(ctx, tmp_path):
    _seed(ctx)
    good = backup_sqlite_file(_db_path(ctx), tmp_path / "b")
    assert verify_backup(good)["ok"]
    bad = tmp_path / "bad.db"
    bad.write_bytes(good.read_bytes()[: len(good.read_bytes()) // 3])
    assert not verify_backup(bad)["ok"]
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert not verify_backup(empty)["ok"]


def test_bundle_has_db_journal_assets_and_manifest_but_no_env(ctx, tmp_path):
    _seed(ctx)
    ctx.settings.assets_dir.mkdir(parents=True, exist_ok=True)
    (ctx.settings.assets_dir / "post_1.png").write_bytes(b"\x89PNG fake")
    (ctx.settings.data_dir / "publish_journal.jsonl").write_text('{"content_id": 1}\n', encoding="utf-8")
    bundle = create_bundle(ctx.settings, _db_path(ctx), tmp_path / "out")
    with zipfile.ZipFile(bundle) as zf:
        names = set(zf.namelist())
        assert {"aime.db", "publish_journal.jsonl", "assets/post_1.png", "manifest.json"} <= names
        assert not any(n.endswith(".env") for n in names)


def test_encryption_roundtrip_and_wrong_passphrase(tmp_path):
    data = b"backup bytes" * 100
    blob = encrypt_bytes(data, PASSPHRASE)
    assert data not in blob and decrypt_bytes(blob, PASSPHRASE) == data
    with pytest.raises(BackupError):
        decrypt_bytes(blob, "another passphrase!!")
    with pytest.raises(BackupError):
        decrypt_bytes(blob[:-5] + b"xxxxx", PASSPHRASE)


def test_offsite_copy_is_encrypted_restorable_and_has_no_secrets(make_ctx, tmp_path):
    offsite = tmp_path / "onedrive" / "AIME-Backups"
    fake_secret = "WPL_AP" + "1." + "not-a-real-secret-" + "0000"  # assembled so the secret scanner stays quiet
    ctx = make_ctx(backup_offsite_dir=str(offsite), backup_passphrase=PASSPHRASE, linkedin_client_secret=fake_secret)
    _seed(ctx)
    out = job_db_maintenance(ctx)
    assert not out.failed and out.steps[1]["output"]["status"] == "ok"
    copies = list(offsite.glob("aime-offsite-*.aimebak"))
    assert len(copies) == 1
    raw = copies[0].read_bytes()
    assert b"Sample 0" not in raw and b"SQLite format" not in raw
    plain = decrypt_bytes(raw, PASSPHRASE)
    with zipfile.ZipFile(io.BytesIO(plain)) as zf:
        assert all(b"not-a-real-secret" not in zf.read(n) for n in zf.namelist())
    drill = restore_drill(ctx.settings, copies[0])
    assert drill["ok"], drill
    check = next(c for c in run_checks(ctx) if c["name"] == "Off-machine encrypted backup")
    assert check["ok"]


def test_offsite_not_configured_is_a_warning_not_a_failure(ctx):
    out = job_db_maintenance(ctx)
    assert not out.failed and out.steps[1]["output"]["status"] == "not_configured"
    check = next(c for c in run_checks(ctx) if c["name"] == "Off-machine encrypted backup")
    assert not check["ok"] and check["severity"] == "warning"


def test_short_passphrase_is_refused(make_ctx, tmp_path):
    ctx = make_ctx(backup_offsite_dir=str(tmp_path / "off"), backup_passphrase="short")
    result = write_offsite(ctx.settings, _db_path(ctx))
    assert result["status"] == "not_configured" and "12 characters" in result["reason"]


def test_restore_drill_never_touches_production(ctx):
    _seed(ctx, 5)
    backup_sqlite_file(_db_path(ctx), ctx.settings.backups_dir, label="auto")
    live = _db_path(ctx)
    before = (_hash(live), live.stat().st_mtime)
    data_files = sorted(p.name for p in ctx.settings.data_dir.iterdir())
    drill = restore_drill(ctx.settings)
    assert drill["ok"] and drill["rows"] >= 5
    assert (_hash(live), live.stat().st_mtime) == before
    assert sorted(p.name for p in ctx.settings.data_dir.iterdir()) == data_files


def test_offsite_prune_only_removes_own_files(tmp_path):
    for i in range(5):
        (tmp_path / f"aime-offsite-2026010{i}-000000.aimebak").write_bytes(b"x")
    (tmp_path / "family-photos.zip").write_bytes(b"keep me")
    assert prune_offsite(tmp_path, keep=2) == 3
    assert (tmp_path / "family-photos.zip").exists()
    assert len(list(tmp_path.glob("aime-offsite-*"))) == 2


def test_settings_repr_hides_secrets(tmp_path):
    names = ("backup_passphrase", "linkedin_client_secret", "smtp_password", "inbound_api_token", "census_api_key")
    fakes = {name: f"{name.replace('_', '-')}-fake-value" for name in names}
    s = Settings.load(env_file=tmp_path / "none.env", data_dir=tmp_path, **fakes)
    text = repr(s)
    assert not any(v in text for v in fakes.values())


def test_restore_drill_extracts_only_outside_the_data_folder(ctx, tmp_path):
    _seed(ctx)
    backup_sqlite_file(_db_path(ctx), ctx.settings.backups_dir, label="auto")
    with pytest.raises(BackupError):
        restore_drill(ctx.settings, extract_to=ctx.settings.data_dir / "restore")
    target = tmp_path / "restore-check"
    drill = restore_drill(ctx.settings, extract_to=target)
    assert drill["ok"] and verify_backup(target / "aime.db")["ok"]
    with pytest.raises(BackupError):
        restore_drill(ctx.settings, extract_to=target)  # never overwrites an existing folder


def test_offsite_folder_counts_as_off_machine_only_when_a_signed_in_onedrive_syncs_it(tmp_path):
    synced = tmp_path / "OneDrive"
    assert offsite_sync(synced / "AIME-Backups", synced=[synced])[0] is True
    leaves, why = offsite_sync(tmp_path / "OneDrive" / "AIME-Backups", synced=[])
    assert leaves is False and "not signed in" in why
    assert offsite_sync(tmp_path / "usb" / "backups", synced=[])[0] is None


def test_doctor_warns_when_the_offsite_folder_does_not_leave_the_pc(make_ctx, tmp_path, monkeypatch):
    import aime.backup
    monkeypatch.setattr(aime.backup, "onedrive_folders", lambda: [])
    ctx = make_ctx(backup_offsite_dir=str(tmp_path / "OneDrive" / "AIME-Backups"), backup_passphrase=PASSPHRASE)
    check = next(c for c in run_checks(ctx) if c["name"] == "Off-machine folder leaves this PC")
    assert not check["ok"] and check["severity"] == "warning" and "sign in to OneDrive" in check["detail"]
    monkeypatch.setattr(aime.backup, "onedrive_folders", lambda: [tmp_path / "OneDrive"])
    assert next(c for c in run_checks(ctx) if c["name"] == "Off-machine folder leaves this PC")["ok"]
