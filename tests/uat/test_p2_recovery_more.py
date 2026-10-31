"""Phase 2, recovery: what the fixes must also do, and above all what they must not do on an ordinary day.

Synthetic data only; every mail server is a fake. The safety question for each gate: can it ever stop an ordinary first send?
"""

from __future__ import annotations

import json
import os
import shutil
import smtplib
import socket
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import aime.timeutil
from aime import cli as aime_cli
from aime import clock_check, mail_accounts, maintenance, send_journal
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.backup import backup_sqlite_file, create_bundle, prune_manual_backups, restore_drill, weekly_restore_drill
from aime.crm_ops import erase_person
from aime.doctor import run_checks
from aime.integrations.email_smtp import EmailSender
from aime.models import Alert, FollowUp, MailAccount
from aime.restore import RestoreRefused, restore_database
from aime.scheduler import runner
from aime.scheduler.runner import run_job, tick
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.uat.test_p2_recovery import MAIL, FakeSMTP, approved_email, damage_table, literal_restore, script, send_run, stub_jobs


@pytest.fixture(autouse=True)
def _no_task_scheduler_and_a_clean_server(monkeypatch):
    from aime import notify
    from aime.scheduler import windows

    monkeypatch.setattr(windows, "query", lambda *a, **k: {"installed": False, "error": "uat"})
    monkeypatch.setattr(notify, "has_desktop", lambda: False)
    FakeSMTP.sent, FakeSMTP.on_connect = [], None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Refusing(FakeSMTP):
    error: Exception = smtplib.SMTPSenderRefused(550, b"no", "sales@us.example")

    def send_message(self, msg, *a, **k):
        raise Refusing.error


def run_with(ctx, smtp):
    return SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=smtp)).run({"now": utcnow().isoformat()})


def status_of(ctx, fid):
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        return f.status, f.blocked_reason


# ---------------------------------------------------------------------------------------- an ordinary day
def test_a_first_send_goes_out_and_nothing_is_held(make_ctx):
    ctx = make_ctx(**MAIL)
    fid = approved_email(ctx)
    send_run(ctx)
    assert FakeSMTP.sent == ["p@a.example"] and status_of(ctx, fid)[0] == "sent"
    assert send_journal.hold(ctx.settings.data_dir) is None
    second = approved_email(ctx, tag="b")
    make_ctx(**MAIL, data_dir=ctx.settings.data_dir)  # a new process on the same day
    send_run(ctx)
    assert FakeSMTP.sent == ["p@a.example", "p@b.example"] and status_of(ctx, second)[0] == "sent"
    assert send_journal.hold(ctx.settings.data_dir) is None


@pytest.mark.parametrize("error", [smtplib.SMTPSenderRefused(550, b"no", "x"), smtplib.SMTPConnectError(421, b"busy"),
                                   smtplib.SMTPAuthenticationError(535, b"no"), ConnectionRefusedError(),
                                   smtplib.SMTPRecipientsRefused({"p@a.example": (450, b"later")})],
                         ids=["sender_refused", "connect_error", "sign_in", "refused", "recipient_4xx"])
def test_a_send_that_delivered_nothing_is_retried_and_holds_nothing(make_ctx, error):
    ctx = make_ctx(**MAIL)
    fid = approved_email(ctx)
    Refusing.error = error
    run_with(ctx, Refusing)
    assert status_of(ctx, fid)[0] == "approved" and send_journal.hold(ctx.settings.data_dir) is None
    send_run(ctx)
    assert FakeSMTP.sent == ["p@a.example"] and status_of(ctx, fid)[0] == "sent"
    assert send_journal.hold(ctx.settings.data_dir) is None


def test_an_email_of_unknown_outcome_is_no_sign_of_a_restore_and_a_person_can_requeue_it(make_ctx):
    ctx = make_ctx(**MAIL)
    fid = approved_email(ctx)
    Refusing.error = TimeoutError("timed out")
    run_with(ctx, Refusing)
    assert status_of(ctx, fid)[0] == "uncertain"
    send_run(ctx)
    assert FakeSMTP.sent == [] and send_journal.hold(ctx.settings.data_dir) is None
    client = TestClient(create_app(ctx), follow_redirects=False)
    assert client.post(f"/followups/{fid}/requeue", headers={"Origin": "http://testserver"}).status_code < 400
    assert status_of(ctx, fid)[0] == "approved"
    send_run(ctx)
    assert FakeSMTP.sent == ["p@a.example"] and status_of(ctx, fid)[0] == "sent"
    assert send_journal.hold(ctx.settings.data_dir) is None


def test_an_erased_person_and_their_sent_email_stop_nothing(make_ctx):
    ctx = make_ctx(**MAIL)
    approved_email(ctx)
    send_run(ctx)
    with ctx.db.session() as s:
        erase_person(s, "p@a.example", "owner")
    other = approved_email(ctx, tag="b")
    send_run(ctx)
    assert status_of(ctx, other)[0] == "sent" and send_journal.hold(ctx.settings.data_dir) is None
    assert "p@a.example" not in send_journal.journal_path(ctx.settings.data_dir).read_text(encoding="utf-8")


def test_a_journal_that_cannot_be_written_stops_that_send_and_leaves_it_approved(make_ctx, monkeypatch):
    ctx = make_ctx(**MAIL)
    fid = approved_email(ctx)

    def refuse(*a, **k):
        raise PermissionError(13, "held by another program")
    monkeypatch.setattr(send_journal, "record_claim", refuse)
    send_run(ctx)
    assert FakeSMTP.sent == []
    status, why = status_of(ctx, fid)
    assert status == "approved" and "record of sent emails" in why
    monkeypatch.undo()
    send_run(ctx)
    assert FakeSMTP.sent == ["p@a.example"]


def test_a_journal_cut_short_by_a_power_cut_loses_only_that_line(make_ctx):
    ctx = make_ctx(**MAIL)
    approved_email(ctx)
    send_run(ctx)
    path = send_journal.journal_path(ctx.settings.data_dir)
    with path.open("ab") as fh:
        fh.write(b'{"event": "claim", "followup_id": 99')
    second = approved_email(ctx, tag="b")
    send_run(ctx)
    assert status_of(ctx, second)[0] == "sent"
    assert (second, send_journal._mark("p@b.example")) in send_journal.read(ctx.settings.data_dir).claims


# ---------------------------------------------------------------------------------------- after a restore
def restored(make_ctx, tmp_path, tags=("a",)):
    ctx = make_ctx(**MAIL)
    send_run(ctx)
    ids = [approved_email(ctx, tag=t) for t in tags]
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), tmp_path / "keep", label="nightly")
    send_run(ctx)
    assert sorted(FakeSMTP.sent) == sorted(f"p@{t}.example" for t in tags)
    literal_restore(ctx, backup)
    return make_ctx(**MAIL, data_dir=ctx.settings.data_dir), ids


def test_a_restore_marks_the_email_for_a_person_and_holds_the_rest_until_confirmed(make_ctx, tmp_path):
    again, (fid,) = restored(make_ctx, tmp_path)
    fresh = approved_email(again, tag="c")  # approved before the backup in real life, or since: either way it waits
    send_run(again)
    assert FakeSMTP.sent == ["p@a.example"]
    status, why = status_of(again, fid)
    assert status == "uncertain" and "Sent folder" in why
    assert status_of(again, fresh)[0] == "approved" and "confirm-sending" in status_of(again, fresh)[1]
    with again.db.session() as s:
        said = [a.message for a in s.execute(select(Alert)).scalars()]
    assert sum("ON HOLD" in m for m in said) == 1 and sum(f"Follow-up #{fid}" in m for m in said) == 1
    assert any(c["name"] == "Sending is not on hold" and not c["ok"] for c in run_checks(again))
    send_run(again)  # still held, however many runs
    assert FakeSMTP.sent == ["p@a.example"]

    assert aime_cli.cmd_confirm_sending(type("A", (), {"who": "owner"})(), again) == 0
    assert send_journal.hold(again.settings.data_dir) is None
    send_run(again)
    assert FakeSMTP.sent == ["p@a.example", "p@c.example"], "the confirmed one goes; the one that went before does not"
    assert status_of(again, fid)[0] == "uncertain"
    with again.db.session() as s:
        assert not s.execute(select(Alert).where(Alert.message.contains("ON HOLD"), Alert.acknowledged.is_(False))).first()


def test_the_journal_lives_outside_the_database_file(make_ctx, tmp_path):
    again, _ = restored(make_ctx, tmp_path)
    assert send_journal.journal_path(again.settings.data_dir).exists()
    assert send_journal.read(again.settings.data_dir).claims, "copying a backup over aime.db left the journal alone"


def test_an_email_created_and_sent_after_the_backup_is_how_a_restore_is_found_too(make_ctx, tmp_path):
    ctx = make_ctx(**MAIL)
    send_run(ctx)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), tmp_path / "keep", label="nightly")
    approved_email(ctx)  # the row does not exist in the backup at all
    send_run(ctx)
    literal_restore(ctx, backup)
    again = make_ctx(**MAIL, data_dir=ctx.settings.data_dir)
    send_run(again)
    assert send_journal.hold(again.settings.data_dir), "the journal names a row beyond the newest one"


def test_a_row_number_used_again_after_a_restore_is_not_mistaken_for_the_email_that_went(make_ctx, tmp_path):
    ctx = make_ctx(**MAIL)
    send_run(ctx)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), tmp_path / "keep", label="nightly")
    old = approved_email(ctx, tag="a")
    send_run(ctx)
    literal_restore(ctx, backup)
    again = make_ctx(**MAIL, data_dir=ctx.settings.data_dir)
    send_run(again)
    assert aime_cli.cmd_confirm_sending(type("A", (), {"who": "owner"})(), again) == 0
    new = approved_email(again, tag="z")  # a different person, on the same row number
    assert new == old
    send_run(again)
    assert FakeSMTP.sent == ["p@a.example", "p@z.example"]


def test_a_requeue_is_not_undone_by_restoring_the_same_backup_again(make_ctx, tmp_path):
    ctx = make_ctx(**MAIL)
    send_run(ctx)
    fid = approved_email(ctx)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), tmp_path / "keep", label="nightly")
    send_journal.record_claim(ctx.settings.data_dir, fid, "p@a.example", None)  # it went
    send_journal.record_released(ctx.settings.data_dir, fid, "p@a.example")  # a person said it did not arrive
    send_journal.record_claim(ctx.settings.data_dir, fid, "p@a.example", None)  # and it went again
    literal_restore(ctx, backup)
    again = make_ctx(**MAIL, data_dir=ctx.settings.data_dir)
    send_run(again)
    assert FakeSMTP.sent == [] and status_of(again, fid)[0] == "uncertain"


def test_emails_sent_before_this_code_existed_are_protected_too(make_ctx, tmp_path):
    ctx = make_ctx(**MAIL)
    send_run(ctx)
    fid = approved_email(ctx)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), tmp_path / "keep", label="nightly")
    send_run(ctx)
    data = ctx.settings.data_dir
    send_journal.journal_path(data).unlink()  # as if the first run of this code came after that send
    with ctx.db.session() as s:
        from aime.models import KeyValue
        s.delete(s.get(KeyValue, send_journal.STARTED_KEY))
    ctx.db.dispose()
    again = make_ctx(**MAIL, data_dir=data)  # first start: the journal starts from what the database says was sent
    assert send_journal.hold(data) is None and send_journal.read(data).claims
    literal_restore(again, backup)
    third = make_ctx(**MAIL, data_dir=data)
    send_run(third)
    assert FakeSMTP.sent == ["p@a.example"] and status_of(third, fid)[0] == "uncertain"


def test_a_database_that_arrives_without_its_journal_holds_sending(make_ctx):
    ctx = make_ctx(**MAIL)
    approved_email(ctx)
    send_run(ctx)
    data = ctx.settings.data_dir
    ctx.db.dispose()
    send_journal.journal_path(data).unlink()  # a new PC, a copied folder
    again = make_ctx(**MAIL, data_dir=data)
    assert "missing" in send_journal.hold(data)["reason"]
    approved_email(again, tag="b")
    send_run(again)
    assert FakeSMTP.sent == ["p@a.example"]


def test_no_post_goes_out_on_hold(make_ctx):
    from aime.agents.publishing import AuthorizedPublishingAgent

    ctx = make_ctx()
    send_journal.set_hold(ctx.settings.data_dir, "restored", "test")
    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert out.can_publish is False and "confirm-sending" in out.authorization


# ---------------------------------------------------------------------------------------- the restore command
def restore_settings(make_ctx):
    return make_ctx(**MAIL, dashboard_port=free_port())


def test_the_restore_command_does_every_step(make_ctx, capsys):
    ctx = restore_settings(make_ctx)
    send_run(ctx)
    fid = approved_email(ctx)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="nightly")
    send_run(ctx)
    ctx.db.dispose()
    done = restore_database(ctx.settings, backup)
    assert done["already_sent"] == [fid] and done["kept"].startswith("aime-pre-restore-")
    assert (ctx.settings.backups_dir / done["kept"]).exists()
    assert maintenance.active(ctx.settings.data_dir)["reason"].startswith("restore from")
    assert send_journal.hold(ctx.settings.data_dir)
    assert not (ctx.settings.data_dir / "aime.db-wal").exists() or (ctx.settings.data_dir / "aime.db-wal").stat().st_size == 0
    assert sqlite3.connect(ctx.settings.data_dir / "aime.db").execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert run_job(ctx, "followups")["status"] == "skipped"  # the window: nothing runs until the person ends it
    maintenance.stop(ctx.settings.data_dir)
    send_run(ctx)
    assert FakeSMTP.sent == ["p@a.example"]


def test_the_restore_command_refuses_while_the_dashboard_answers_and_changes_nothing(make_ctx):
    ctx = restore_settings(make_ctx)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="nightly")
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", ctx.settings.dashboard_port))
        srv.listen()
        with pytest.raises(RestoreRefused, match="dashboard"):
            restore_database(ctx.settings, backup)
    assert not maintenance.active(ctx.settings.data_dir) and send_journal.hold(ctx.settings.data_dir) is None


def test_the_restore_command_refuses_while_a_run_is_working(make_ctx):
    ctx = restore_settings(make_ctx)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="nightly")
    lock = runner.FileLock(ctx.settings.locks_dir / "tick.lock")
    assert lock.acquire()
    try:
        with pytest.raises(RestoreRefused, match="tick"):
            restore_database(ctx.settings, backup)
    finally:
        lock.release()
    assert send_journal.hold(ctx.settings.data_dir) is None


def test_the_restore_command_refuses_a_bad_copy_and_names_a_good_one(make_ctx):
    ctx = restore_settings(make_ctx)
    good = backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="auto")
    bad = ctx.settings.backups_dir / "aime-auto-29990101-000000.db"
    bad.write_bytes(os.urandom(8192))
    with pytest.raises(RestoreRefused, match=good.name):
        restore_database(ctx.settings, bad)
    assert send_journal.hold(ctx.settings.data_dir) is None


def test_the_restore_command_works_on_a_damaged_database_and_keeps_it(make_ctx, monkeypatch, capsys):
    ctx = restore_settings(make_ctx)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="auto")
    data = ctx.settings.data_dir
    ctx.db.dispose()
    (data / "aime.db").write_bytes(os.urandom(8192))
    done = restore_database(ctx.settings, backup)
    assert done["kept"].startswith("damaged-") and (ctx.settings.backups_dir / done["kept"]).exists()
    assert not list(ctx.settings.backups_dir.glob("partial-*"))


def test_the_restore_command_takes_an_encrypted_copy_and_leaves_the_newer_journal_alone(make_ctx):
    pytest.importorskip("cryptography")
    ctx = restore_settings(make_ctx)
    bundle = create_bundle(ctx.settings, Path(ctx.db.engine.url.database), ctx.settings.data_dir / "bundles")
    outside = ctx.settings.data_dir.parent / "bundle.zip"
    shutil.copyfile(bundle, outside)
    journal = send_journal.journal_path(ctx.settings.data_dir)
    send_journal.record_claim(ctx.settings.data_dir, 99, "x@example.com", None)
    before = journal.read_text(encoding="utf-8")
    ctx.db.dispose()
    restore_database(ctx.settings, outside)
    assert journal.read_text(encoding="utf-8").startswith(before)


def test_the_cli_asks_for_confirm_before_replacing_anything(make_ctx, tmp_path, monkeypatch, capsys):
    ctx = restore_settings(make_ctx)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="auto")
    args = type("A", (), {"source": str(backup), "confirm": False})()
    assert aime_cli.cmd_restore(args, ctx.settings) == 2
    assert send_journal.hold(ctx.settings.data_dir) is None


# ---------------------------------------------------------------------------------------- a damaged database
def test_a_person_who_has_looked_can_let_sending_go_on_and_new_damage_holds_it_again(make_ctx):
    ctx = make_ctx(**MAIL)
    send_run(ctx)
    approved_email(ctx)
    damage_table(ctx, "signals")
    send_run(ctx)
    assert FakeSMTP.sent == [] and "integrity" in send_journal.hold(ctx.settings.data_dir)["reason"]
    assert aime_cli.cmd_confirm_sending(type("A", (), {"who": "owner"})(), ctx) == 0
    send_run(ctx)
    assert FakeSMTP.sent == ["p@a.example"], "the same result, already looked at, does not hold it again"


def test_a_clean_database_checks_nothing_when_nothing_is_due(make_ctx, monkeypatch):
    import aime.backup as backup

    ctx = make_ctx(**MAIL)
    calls = []
    real = backup.integrity_check
    monkeypatch.setattr(backup, "integrity_check", lambda db: (calls.append(1), real(db))[1])
    send_run(ctx)
    assert calls == []
    approved_email(ctx)
    send_run(ctx)
    assert calls == [1] and FakeSMTP.sent == ["p@a.example"]


def test_a_missing_database_on_a_new_install_still_starts(make_ctx):
    ctx = make_ctx(data_role="production")
    assert (ctx.settings.data_dir / "aime.db").exists()


def test_a_missing_production_database_is_said_by_doctor_with_the_next_step(make_ctx, tmp_path, monkeypatch, capsys):
    ctx = make_ctx(data_role="production")
    good = backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="auto")
    data = ctx.settings.data_dir
    ctx.db.dispose()
    for name in ("aime.db", "aime.db-wal", "aime.db-shm"):
        (data / name).unlink(missing_ok=True)
    monkeypatch.setenv("AIME_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.setenv("AIME_DATA_DIR", str(data))
    monkeypatch.setenv("AIME_DATA_ROLE", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert aime_cli.main(["doctor"]) == 1
    said = capsys.readouterr().out
    assert "missing" in said and good.name in said and not (data / "aime.db").exists()
    assert aime_cli.main(["tick", "--quiet"]) == 2 and not (data / "aime.db").exists()
    assert "missing" in (data / "logs" / "scheduler_crash.log").read_text(encoding="utf-8")
    assert aime_cli.main(["maintenance", "on", "--hours", "1"]) == 0
    assert aime_cli.main(["status"]) == 1


@pytest.mark.parametrize("cmd", [["backup-verify", "nothing.db"], ["status"]], ids=["backup_verify", "status"])
def test_more_commands_run_when_the_database_is_damaged(cmd, tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / "aime.db").write_bytes(os.urandom(8192))
    monkeypatch.setenv("AIME_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.setenv("AIME_DATA_DIR", str(data))
    monkeypatch.setenv("AIME_DATA_ROLE", "test")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert isinstance(aime_cli.main(cmd), int)


def test_a_failed_drill_on_the_newest_copy_names_the_next_good_one(make_ctx):
    ctx = make_ctx()
    good = backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="auto")
    bad = ctx.settings.backups_dir / "aime-auto-29990101-000000.db"
    bad.write_bytes(os.urandom(8192))
    result = restore_drill(ctx.settings)
    assert not result["ok"] and good.name in result["detail"]


def test_the_weekly_drill_runs_once_a_week_and_reports_a_bad_copy(make_ctx, monkeypatch):
    ctx = make_ctx()
    backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="auto")
    assert weekly_restore_drill(ctx)["status"] == "success"
    assert weekly_restore_drill(ctx) is None
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET + timedelta(days=8))
    newest = sorted(ctx.settings.backups_dir.glob("aime-*.db"))[-1]
    newest.write_bytes(os.urandom(8192))
    assert weekly_restore_drill(ctx)["status"] == "failed"


# ---------------------------------------------------------------------------------------- backups kept
def test_old_manual_copies_go_by_age_keeping_the_newest_predeploy_and_the_last_good(tmp_path):
    day = 86400
    now = time.time()

    def copy(name, age_days, good=True):
        p = tmp_path / name
        if good:
            con = sqlite3.connect(p)
            con.execute("CREATE TABLE t(x)")
            con.execute("INSERT INTO t VALUES (1)")
            con.commit()
            con.close()
        else:
            p.write_bytes(os.urandom(8192))
        os.utime(p, (now - age_days * day, now - age_days * day))
        return p

    names = [copy(f"aime-predeploy-{i}.db", 100 - i) for i in range(8)]
    labelled = copy("aime-before-team-setup.db", 200)
    young = copy("aime-pre-migration-1.db", 3)
    removed = prune_manual_backups(tmp_path, keep=2, keep_predeploy=5, now=now)
    assert labelled.name in removed and young.exists()
    assert all(p.exists() for p in names[-5:]) and not names[0].exists()

    (tmp_path / "x").mkdir()
    only = [copy(f"x/aime-predeploy-{i}.db", 100 - i, good=i == 0) for i in range(3)]
    prune_manual_backups(tmp_path / "x", keep=1, now=now)
    assert only[0].exists(), "the only copy that still verifies is never removed, however old"


def test_a_failed_backup_leaves_no_partial_file(make_ctx, monkeypatch):
    ctx = make_ctx()
    import aime.backup as b

    real = sqlite3.connect

    def small(path, *a, **k):
        con = real(path, *a, **k)
        if "backups" in str(path):
            con.execute("PRAGMA max_page_count=1")
        return con
    monkeypatch.setattr(b.sqlite3, "connect", small)
    with pytest.raises(sqlite3.OperationalError):
        backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="auto")
    assert list(ctx.settings.backups_dir.iterdir()) == []


# ---------------------------------------------------------------------------------------- the clock
def test_a_real_72_hour_sleep_and_a_week_away_are_normal(make_ctx, monkeypatch):
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    assert tick(ctx, trigger="scheduler")["status"] == "ok"
    for hours in (72, 24 * 9):
        calls.clear()
        monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET + timedelta(hours=hours))
        assert tick(ctx, trigger="scheduler")["status"] == "ok" and calls


def stopped_a_year_ahead(make_ctx, monkeypatch):
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    assert tick(ctx, trigger="scheduler")["status"] == "ok"
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET + timedelta(days=365))
    calls.clear()
    assert tick(ctx, trigger="scheduler")["status"] == "skipped"
    return ctx, calls


def test_the_stop_lasts_while_the_clock_is_wrong_with_one_alert_and_doctor_says_so(make_ctx, monkeypatch):
    ctx, calls = stopped_a_year_ahead(make_ctx, monkeypatch)
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET + timedelta(minutes=15))
    assert tick(ctx, trigger="scheduler")["status"] == "skipped" and calls == []
    with ctx.db.session() as s:
        assert len(s.execute(select(Alert).where(Alert.key == clock_check.ALERT_KEY)).scalars().all()) == 1
    assert any(c["name"] == "Clock looks right" and not c["ok"] for c in run_checks(ctx))
    assert "clock" in script("watchdog").check(ctx.settings.data_dir)[0]


def test_correcting_the_clock_ends_the_stop_by_itself(make_ctx, monkeypatch):
    ctx, calls = stopped_a_year_ahead(make_ctx, monkeypatch)
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET - timedelta(days=365) + timedelta(minutes=30))
    assert tick(ctx, trigger="scheduler")["status"] == "ok" and calls
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.key == clock_check.ALERT_KEY)).scalars().one().resolved_at


def test_a_person_can_confirm_a_long_absence_so_the_work_is_not_stuck(make_ctx, monkeypatch):
    ctx, calls = stopped_a_year_ahead(make_ctx, monkeypatch)
    assert aime_cli.cmd_confirm_clock(None, ctx) == 0
    assert tick(ctx, trigger="scheduler")["status"] == "ok" and calls
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET + timedelta(days=20))
    calls.clear()
    assert tick(ctx, trigger="scheduler")["status"] == "skipped" and calls == [], "the confirmation covered that one gap only"


def test_a_clock_earlier_than_the_newest_record_stops_and_a_small_correction_does_not(make_ctx, monkeypatch):
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    assert tick(ctx, trigger="scheduler")["status"] == "ok"
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET - timedelta(minutes=3))
    assert tick(ctx, trigger="scheduler")["status"] == "ok"
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET - timedelta(days=400))
    calls.clear()
    assert tick(ctx, trigger="scheduler")["status"] == "skipped" and calls == []
    with ctx.db.session() as s:
        assert "clock" in s.execute(select(Alert).where(Alert.key == clock_check.ALERT_KEY)).scalars().one().message
    aime_cli.cmd_confirm_clock(None, ctx)
    assert tick(ctx, trigger="scheduler")["status"] == "ok" and calls, "a clock that used to be wrong does not stay stuck"
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET + timedelta(days=30))
    # nearly a month later, on a clock still behind those old records: the beats since then are what count
    calls.clear()
    assert tick(ctx, trigger="scheduler")["status"] == "skipped", "more than 14 days since the last check-in"


def test_a_first_tick_on_a_new_install_is_never_stopped(make_ctx, monkeypatch):
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    assert tick(ctx, trigger="scheduler")["status"] == "ok" and calls


def test_a_tick_given_its_own_time_is_not_judged_by_it(make_ctx, monkeypatch):
    ctx = make_ctx()
    stub_jobs(monkeypatch)
    assert tick(ctx, trigger="scheduler")["status"] == "ok"
    assert tick(ctx, trigger="scheduler", now=utcnow() + timedelta(days=100))["status"] == "ok"


# ---------------------------------------------------------------------------------------- locks
def test_a_lock_of_a_live_process_that_wrote_it_still_holds(make_ctx, tmp_path):
    import subprocess
    import sys

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.5)
        lock = tmp_path / "x.lock"
        lock.write_text(f"{p.pid} {datetime.now().isoformat()}", encoding="utf-8")
        assert not runner.FileLock(lock).acquire(), "a running tick keeps its lock"
    finally:
        p.kill()
        p.wait()


def test_a_lock_just_created_and_not_yet_written_still_holds(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("", encoding="utf-8")
    assert not runner.FileLock(lock).acquire()


def test_a_process_start_time_is_told(tmp_path):
    started = runner.pid_started(os.getpid())
    assert started is None or abs(started - time.time()) < 24 * 3600 * 30
    if os.name == "nt":
        assert started is not None and started <= time.time()


# ---------------------------------------------------------------------------------------- mailbox secrets
def box(ctx, **over):
    with ctx.db.session() as s:
        s.add(MailAccount(**{**dict(label="Synth", provider="gmail", auth="app_password", email="s@us.example", username="s@us.example",
                                    imap_host="fake.invalid", smtp_host="fake.invalid", secret="dpapi:AAAA", active=True, read_replies=True,
                                    is_primary=True), **over}))


def cannot_read(_stored):
    raise OSError("[WinError -2146893813] Key not valid for use in specified state")


def test_an_unreadable_password_means_no_sign_in_one_alert_and_the_run_goes_on(make_ctx, monkeypatch):
    ctx = make_ctx(**MAIL)
    box(ctx)
    monkeypatch.setattr(mail_accounts, "unprotect", cannot_read)
    fid = approved_email(ctx)
    connected = []
    monkeypatch.setattr(FakeSMTP, "on_connect", lambda: connected.append(1))
    run_job(ctx, "followups")
    run_job(ctx, "monitor")
    assert connected == [] and status_of(ctx, fid)[0] == "approved"
    with ctx.db.session() as s:
        said = [a.message for a in s.execute(select(Alert).where(Alert.resolved_at.is_(None))).scalars() if "password" in a.message]
    assert len(said) == 1 and "enter it again" in said[0], said
    with ctx.db.session() as s:
        ready, reason = mail_accounts.sending_status(s, ctx.settings)
    assert not ready and "enter it again" in reason


def test_the_alert_closes_when_the_password_can_be_read_again(make_ctx, monkeypatch):
    ctx = make_ctx(**MAIL)
    box(ctx)
    monkeypatch.setattr(mail_accounts, "unprotect", cannot_read)
    run_job(ctx, "followups")
    monkeypatch.setattr(mail_accounts, "unprotect", lambda _s: "readable")
    run_job(ctx, "followups")
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.key == mail_accounts.UNREADABLE_ALERT)).scalars().one().resolved_at


def test_an_unreadable_mailbox_is_not_signed_in_to_and_the_inbox_job_fails_plainly(make_ctx, monkeypatch):
    from aime.agents.inbox_agent import InboxAgent

    ctx = make_ctx(**MAIL)
    box(ctx)
    monkeypatch.setattr(mail_accounts, "unprotect", cannot_read)
    signed_in = []
    result = InboxAgent(ctx, imap_factory=lambda *a, **k: signed_in.append(1)).run({})
    assert signed_in == [] and not result.ok and "enter it again" in result.error


def test_the_mailboxes_page_says_it_plainly(make_ctx, monkeypatch):
    ctx = make_ctx(**MAIL)
    box(ctx)
    monkeypatch.setattr(mail_accounts, "unprotect", cannot_read)
    with ctx.db.session() as s:
        shown = mail_accounts.public(s.execute(select(MailAccount)).scalars().one())
    assert "enter it again" in shown["last_test_detail"] and shown["last_test_ok"] is False
    assert "dpapi" not in json.dumps(shown, default=str)


def test_a_sign_in_key_from_another_pc_only_signs_everyone_out(tmp_path, monkeypatch):
    from aime.secure_store import SecretUnreadable
    from aime.web import auth

    def not_decryptable(_stored):
        raise SecretUnreadable("another PC")
    (tmp_path / "session.key").write_text("dpapi:AAAA", encoding="utf-8")
    monkeypatch.setattr(auth, "unprotect", not_decryptable)
    monkeypatch.setattr(auth, "protect", lambda v: "plain:" + v)
    assert len(auth.session_secret(tmp_path)) == 32
    assert (tmp_path / "session.key").read_text(encoding="utf-8").startswith("plain:")


def test_a_sign_in_key_emptied_by_a_power_cut_is_replaced_not_used(tmp_path, monkeypatch):
    from aime.web import auth

    monkeypatch.setattr(auth, "protect", lambda v: "plain:" + v)
    for short in ("", "plain:QUJD"):  # nothing, and three bytes
        auth._SECRETS.clear()
        (tmp_path / "session.key").write_text(short, encoding="utf-8")
        assert len(auth.session_secret(tmp_path)) == 32
    assert not list(tmp_path.glob("*.partial"))


def test_a_passing_failure_to_read_the_sign_in_key_keeps_the_key(tmp_path, monkeypatch):
    from aime.web import auth

    auth._SECRETS.clear()
    (tmp_path / "session.key").write_text("dpapi:AAAA", encoding="utf-8")

    def passing(_stored):
        raise OSError(5, "held for a moment")
    monkeypatch.setattr(auth, "unprotect", passing)
    assert len(auth.session_secret(tmp_path)) == 32
    assert (tmp_path / "session.key").read_text(encoding="utf-8") == "dpapi:AAAA", "the key file stays"
    assert not auth._SECRETS, "and the next request reads it again"


@pytest.mark.skipif(os.name != "nt", reason="DPAPI")
def test_only_a_definite_dpapi_failure_counts_as_unreadable(monkeypatch):
    import aime.secure_store as ss

    def fails_with(code):
        def dpapi(*_a):
            error = OSError(22, "x")
            error.winerror = code
            raise error
        return dpapi
    monkeypatch.setattr(ss, "_dpapi", fails_with(-2146893813))
    with pytest.raises(ss.SecretUnreadable):
        ss.unprotect("dpapi:QUJD")
    monkeypatch.setattr(ss, "_dpapi", fails_with(1450))  # ERROR_NO_SYSTEM_RESOURCES: it passes
    with pytest.raises(OSError) as passing:
        ss.unprotect("dpapi:QUJD")
    assert not isinstance(passing.value, ss.SecretUnreadable)


# ---------------------------------------------------------------------------------------- the dashboard looking after itself
def test_a_hung_dashboard_ends_itself_only_after_it_has_answered_once(tmp_path):
    from aime import dashboard_probe as dp

    ended, answers = [], iter([True] + [False] * 5)
    dp.self_check("h", 1, tmp_path, probe=lambda *a, **k: next(answers), sleep=lambda _s: None, end=ended.append, rounds=6)
    assert ended == [3]
    ended.clear()
    dp.self_check("h", 1, tmp_path, probe=lambda *a, **k: False, sleep=lambda _s: None, end=ended.append, rounds=20)
    assert ended == [], "it has never answered: still starting, or the port is another program's"
    answers = iter([True] + [False] * 4 + [True] + [False] * 4)
    dp.self_check("h", 1, tmp_path, probe=lambda *a, **k: next(answers), sleep=lambda _s: None, end=ended.append, rounds=10)
    assert ended == [], "a slow moment is no hang"
    maintenance.start(tmp_path, 1, "deploy")
    answers = iter([True] + [False] * 6)
    dp.self_check("h", 1, tmp_path, probe=lambda *a, **k: next(answers), sleep=lambda _s: None, end=ended.append, rounds=7)
    assert ended == [], "nothing would start a new one during a maintenance window"


def test_the_self_check_is_left_out_of_the_access_log():
    import logging

    keep = aime_cli.PathOnlyAccessLog().filter
    rec = logging.LogRecord("u", 20, "", 0, "%s %s %s", ("1", "GET", "/login?self_check=1"), None)
    assert keep(rec) is False
    rec = logging.LogRecord("u", 20, "", 0, "%s %s %s", ("1", "GET", "/login?next=/x"), None)
    assert keep(rec) is True and rec.args[2] == "/login"


def test_the_watchdog_warns_of_a_nearly_full_disk(tmp_path, monkeypatch):
    from collections import namedtuple

    wd = script("watchdog")
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12, 10**9))
    assert "GB" in wd.free_space_problem(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 0, 10**12))
    assert wd.free_space_problem(tmp_path) is None


def test_doctor_warns_of_a_nearly_full_disk(make_ctx, monkeypatch):
    from collections import namedtuple

    ctx = make_ctx()
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12, 10**9))
    check = next(c for c in run_checks(ctx) if c["name"] == "Free disk space")
    assert not check["ok"] and check["severity"] == "warning"


def test_a_long_job_is_not_reported_as_stopped_ticks(tmp_path):
    wd = script("watchdog")
    from datetime import timezone

    now = datetime.now(timezone.utc)
    beat = {"at": (now - timedelta(minutes=60)).replace(tzinfo=None).isoformat() + "Z", "state": "running", "job": "enrich"}
    (tmp_path / "heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")
    assert wd.check(tmp_path, now) == []
    beat["at"] = (now - timedelta(minutes=80)).replace(tzinfo=None).isoformat() + "Z"
    (tmp_path / "heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")
    assert "stopped" in wd.check(tmp_path, now)[0]


# ---------------------------------------------------------------------------------------- second review
class _NoConnection:
    def __init__(self, error):
        self.engine = self
        self.error = error

    def connect(self):
        raise self.error


@pytest.mark.parametrize("words", ["database is locked", "disk I/O error", "unable to open database file"])
def test_a_passing_database_error_is_no_finding_about_the_file(words):
    from sqlalchemy.exc import OperationalError

    from aime.backup import integrity_check

    check = integrity_check(_NoConnection(OperationalError("PRAGMA", {}, sqlite3.OperationalError(words))))
    assert not check["ok"] and check["passing"] is True


@pytest.mark.parametrize("words", ["database disk image is malformed", "file is not a database"])
def test_real_damage_is_a_finding(words):
    from sqlalchemy.exc import DatabaseError

    from aime.backup import integrity_check

    check = integrity_check(_NoConnection(DatabaseError("PRAGMA", {}, sqlite3.DatabaseError(words))))
    assert not check["ok"] and "passing" not in check


def test_a_busy_database_waits_this_run_with_no_hold_and_no_alert(make_ctx, monkeypatch):
    import aime.backup as backup

    ctx = make_ctx(**MAIL)
    fid = approved_email(ctx)
    real = backup.integrity_check
    busy = lambda db: {"integrity": "unreadable: database is locked", "foreign_key_violations": 0, "ok": False, "passing": True}  # noqa: E731
    monkeypatch.setattr(backup, "integrity_check", busy)
    monkeypatch.setattr("aime.doctor.integrity_check", busy)
    send_run(ctx)
    assert FakeSMTP.sent == [] and send_journal.hold(ctx.settings.data_dir) is None
    status, why = status_of(ctx, fid)
    assert status == "approved" and why.startswith("Waiting") and "opt-out" not in why
    with ctx.db.session() as s:
        assert not [a for a in s.execute(select(Alert)).scalars() if "ON HOLD" in a.message]
    assert next(c for c in run_checks(ctx) if c["name"] == "SQLite integrity")["severity"] == "warning"
    monkeypatch.setattr(backup, "integrity_check", real)
    send_run(ctx)
    assert FakeSMTP.sent == ["p@a.example"], "the next run goes on by itself"


def test_home_and_manager_today_say_the_clock_stopped_the_work(make_ctx, monkeypatch):
    from aime import automation as am
    from aime.web.app import employee_duty, manager_today

    ctx, _ = stopped_a_year_ahead(make_ctx, monkeypatch)
    with ctx.db.session() as s:
        status, tone, detail, headline = employee_duty(ctx.settings, am.get_state(s), s)
        assert tone == "bad" and "clock" in headline and clock_check.ADVICE in detail
        assert "working for you" not in headline
        board = manager_today(ctx, s)
    assert "clock" in json.dumps(board, default=str).lower()
    assert '"ACTIVE", "last check-in' not in json.dumps(board, default=str)


def test_a_journal_that_stays_unwritable_raises_one_alert_that_closes(make_ctx, monkeypatch):
    ctx = make_ctx(**MAIL)
    approved_email(ctx)

    def refuse(*a, **k):
        raise PermissionError(13, "held")
    monkeypatch.setattr(send_journal, "record_claim", refuse)
    send_run(ctx)
    send_run(ctx)
    with ctx.db.session() as s:
        found = s.execute(select(Alert).where(Alert.key == "sending:journal")).scalars().all()
    assert len(found) == 1 and "record of sent emails" in found[0].message and FakeSMTP.sent == []
    monkeypatch.undo()
    send_run(ctx)
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.key == "sending:journal")).scalars().one().resolved_at
    assert FakeSMTP.sent == ["p@a.example"]


def test_a_locked_journal_does_not_fail_the_monitor_or_give_a_bare_error_on_requeue(make_ctx, monkeypatch):
    ctx = make_ctx(**MAIL)
    fid = approved_email(ctx)
    Refusing.error = TimeoutError("timed out")
    run_with(ctx, Refusing)

    def locked(*a, **k):
        raise PermissionError(13, "locked")
    monkeypatch.setattr(send_journal, "read", locked)
    check = next(c for c in run_checks(ctx) if c["name"] == "Database knows what was sent")
    assert not check["ok"] and check["severity"] == "warning"
    assert any(c["name"] == "No uncertain publications/sends awaiting review" for c in run_checks(ctx)), "the other checks ran"
    assert run_job(ctx, "monitor")["status"] == "success"
    monkeypatch.setattr(send_journal, "record_released", locked)
    client = TestClient(create_app(ctx), follow_redirects=False)
    assert client.post(f"/followups/{fid}/requeue", headers={"Origin": "http://testserver"}).status_code < 500
    assert status_of(ctx, fid)[0] == "uncertain", "nothing changed"


def test_thousands_of_claims_do_not_break_the_check(make_ctx):
    ctx = make_ctx(**MAIL)
    data = ctx.settings.data_dir
    send_journal._append(data, *[send_journal._record("claim", i, f"p{i}@x.example", None) for i in range(1, 40_000)])
    with ctx.db.session() as s:
        found = send_journal.restore_evidence(s, send_journal.read(data))
    assert len(found) == 39_999, "asked for in chunks: one statement cannot name them all"


def test_the_journal_is_read_once_per_send_run(make_ctx, monkeypatch):
    ctx = make_ctx(**MAIL)
    for tag in "abc":
        approved_email(ctx, tag=tag)
    reads = []
    real = send_journal.read
    monkeypatch.setattr(send_journal, "read", lambda d: (reads.append(1), real(d))[1])
    send_run(ctx)
    assert len(FakeSMTP.sent) == 3 and len(reads) <= 2  # the hold check and the sending


def test_two_processes_appending_at_once_lose_nothing(tmp_path):
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    code = ("import sys\nfrom pathlib import Path\nfrom aime import send_journal as j\n"
            "for i in range(300):\n    j.record_claim(Path(sys.argv[1]), int(sys.argv[2]) + i, 'a@b.example', None)\n")
    env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
    kids = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(base)], env=env, cwd=str(tmp_path)) for base in (0, 1000, 2000)]
    for kid in kids:
        assert kid.wait(timeout=120) == 0
    lines = send_journal.journal_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 900 and len({json.loads(line)["followup_id"] for line in lines}) == 900


def test_an_email_requeued_by_older_code_goes_to_a_person_without_a_global_hold(make_ctx):
    from aime.crm_ops import log_activity

    ctx = make_ctx(**MAIL)
    fid = approved_email(ctx)
    Refusing.error = TimeoutError("timed out")
    run_with(ctx, Refusing)
    with ctx.db.session() as s:  # what the code before this journal did: no "released" in the journal
        f = s.get(FollowUp, fid)
        f.status, f.send_started_at, f.blocked_reason = "approved", None, "Requeued after a person confirmed it was not delivered"
        log_activity(s, f"Follow-up #{fid} requeue", "owner", type_="followup")
    send_run(ctx)
    assert FakeSMTP.sent == [] and send_journal.hold(ctx.settings.data_dir) is None
    status, why = status_of(ctx, fid)
    assert status == "uncertain" and "restored" not in why and "Sent folder" in why


def test_a_requeue_before_the_claim_does_not_hide_a_restore(make_ctx, tmp_path):
    from aime.crm_ops import log_activity

    ctx = make_ctx(**MAIL)
    send_run(ctx)
    fid = approved_email(ctx)
    with ctx.db.session() as s:
        log_activity(s, f"Follow-up #{fid} requeue", "owner", type_="followup")
    time.sleep(1.1)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), tmp_path / "keep", label="nightly")
    send_run(ctx)
    literal_restore(ctx, backup)
    again = make_ctx(**MAIL, data_dir=ctx.settings.data_dir)
    send_run(again)
    assert send_journal.hold(again.settings.data_dir), "that requeue is older than the send"
    assert FakeSMTP.sent == ["p@a.example"]


def test_run_now_on_a_wrong_clock_does_nothing_and_says_so(make_ctx, monkeypatch):
    ctx, calls = stopped_a_year_ahead(make_ctx, monkeypatch)
    result = run_job(ctx, "monitor", trigger="manual")
    assert result["status"] == "skipped" and "clock" in result["reason"] and calls == []
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET - timedelta(days=365) + timedelta(minutes=30))
    assert tick(ctx, trigger="scheduler")["status"] == "ok", "no future row was left, so nothing to confirm"


def test_a_maintenance_beat_on_a_wrong_clock_does_not_hide_it(make_ctx, monkeypatch):
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    assert tick(ctx, trigger="scheduler")["status"] == "ok"
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET + timedelta(days=365))
    maintenance.start(ctx.settings.data_dir, 1, "deploy")
    assert tick(ctx, trigger="scheduler")["status"] == "skipped"
    maintenance.stop(ctx.settings.data_dir)
    calls.clear()
    assert tick(ctx, trigger="scheduler")["status"] == "skipped" and calls == []


def test_the_clock_alert_closes_after_a_maintenance_beat_came_between(make_ctx, monkeypatch):
    ctx, calls = stopped_a_year_ahead(make_ctx, monkeypatch)
    maintenance.start(ctx.settings.data_dir, 1, "deploy")
    tick(ctx, trigger="scheduler")
    maintenance.stop(ctx.settings.data_dir)
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET - timedelta(days=365) + timedelta(minutes=30))
    assert tick(ctx, trigger="scheduler")["status"] == "ok"
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.key == clock_check.ALERT_KEY)).scalars().one().resolved_at


def test_ordinary_maintenance_beats_do_not_stop_the_next_tick(make_ctx, monkeypatch):
    ctx = make_ctx()
    stub_jobs(monkeypatch)
    assert tick(ctx, trigger="scheduler")["status"] == "ok"
    maintenance.start(ctx.settings.data_dir, 1, "deploy")
    tick(ctx, trigger="scheduler")
    maintenance.stop(ctx.settings.data_dir)
    assert tick(ctx, trigger="scheduler")["status"] == "ok"


def test_a_lock_that_cannot_be_touched_skips_the_tick_cleanly(make_ctx, monkeypatch):
    ctx = make_ctx()
    calls = stub_jobs(monkeypatch)
    for error in (PermissionError(13, "being deleted"), OSError(28, "No space left on device")):
        def refuse(*a, error=error, **k):
            raise error
        monkeypatch.setattr(runner.os, "open", refuse)
        result = tick(ctx, trigger="scheduler")
        monkeypatch.undo()
        calls = stub_jobs(monkeypatch)
        assert result["status"] == "skipped" and calls == []


def test_a_second_process_cannot_delete_the_lock_the_first_has_just_taken(tmp_path, monkeypatch):
    lock = tmp_path / "x.lock"
    lock.write_text("", encoding="utf-8")
    os.utime(lock, (time.time() - 300, time.time() - 300))
    first, second = runner.FileLock(lock), runner.FileLock(lock)
    real = runner.FileLock._stale
    # the second has judged the file stale; before it acts, the first takes over and holds the lock
    state = {"raced": False}

    def racing(self):
        answer = real(self)
        if self is second and not state["raced"]:
            state["raced"] = True
            assert first.acquire()
        return answer
    monkeypatch.setattr(runner.FileLock, "_stale", racing)
    assert second.acquire() is False, "it judges the file again under the guard, and finds a live owner"
    assert first.held and lock.exists()
    first.release()


def test_a_live_owner_is_not_judged_gone_when_the_clock_stepped_back(tmp_path):
    lock = tmp_path / "x.lock"
    held = runner.FileLock(lock)
    assert held.acquire()
    before = time.time() - 30 * 86400  # as if written with the clock a month behind the process's own start
    os.utime(lock, (before, before))
    if runner.pid_started(os.getpid()) is not None:
        assert runner.lock_owner_gone(lock) is False
    held.release()


def test_status_says_what_is_wrong_on_a_damaged_page_instead_of_a_traceback(make_ctx, tmp_path, monkeypatch, capsys):
    ctx = make_ctx()
    data = ctx.settings.data_dir
    damage_table(ctx, "job_state")
    monkeypatch.setenv("AIME_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.setenv("AIME_DATA_DIR", str(data))
    monkeypatch.setenv("AIME_DATA_ROLE", "test")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert aime_cli.main(["status"]) == 1
    assert "Database:" in capsys.readouterr().out


def test_a_restore_that_fails_half_way_says_what_state_it_left(make_ctx, monkeypatch):
    import aime.restore as restore_module

    ctx = restore_settings(make_ctx)
    backup = backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label="nightly")
    ctx.db.dispose()

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(restore_module.shutil, "copyfile", full)
    with pytest.raises(RestoreRefused) as said:
        restore_database(ctx.settings, backup)
    words = str(said.value)
    assert "was not replaced" in words and "on hold" in words and "aime-pre-restore-" in words
    assert (ctx.settings.data_dir / "aime.db").exists() and send_journal.hold(ctx.settings.data_dir)


def test_a_held_side_file_does_not_fail_the_tidying(tmp_path, monkeypatch):
    (tmp_path / "aime-x.db-wal").write_bytes(b"")
    real = Path.unlink

    def held(self, *a, **k):
        if self.name.endswith("-wal"):
            raise PermissionError(32, "held")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", held)
    assert prune_manual_backups(tmp_path, keep=2) == []
    assert (tmp_path / "aime-x.db-wal").exists()


def test_moves_of_a_failed_scoring_run_still_reach_the_alert(make_ctx, monkeypatch):
    from aime.agents import qualification as q
    from aime.models import Company, Lead

    ctx = make_ctx()
    with ctx.db.session() as s:
        for i in range(80):
            co = Company(name=f"Synth {i}", normalized_name=f"synth {i}", source="test", domain=f"s{i}.example")
            s.add(co)
            s.flush()
            s.add(Lead(company_id=co.id, source="inbound", status="new", score=0))
    real, seen = q.score_lead, {"n": 0}

    def fails_late(*a, **k):
        seen["n"] += 1
        if seen["n"] == 70:
            raise RuntimeError("a bad lead")
        return real(*a, **k)
    monkeypatch.setattr(q, "score_lead", fails_late)
    assert not q.LeadQualificationAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        moved = s.execute(select(Lead).where(Lead.status != "new")).scalars().all()
        said = s.execute(select(Alert).where(Alert.key == "leads:moved")).scalars().all()
    assert moved, "the first batch was saved"
    assert len(said) == 1 and f"moved {len(moved)} lead(s)" in said[0].message
