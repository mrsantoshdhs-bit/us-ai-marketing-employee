"""Phase 5, backups and recovery on a new PC: what a copy holds, and what it cannot know. Synthetic data, fake mail, temp folders."""

from __future__ import annotations

import os
import shutil
import socket
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import make_url

from aime import backup as B
from aime import cli, send_journal
from aime.backup import create_bundle, offsite_copy_problem, restore_drill, weekly_offsite_drill, write_offsite
from aime.context import create_context
from aime.db import kv_set
from aime.doctor import run_checks
from aime.models import Alert, Attachment, Company, FollowUp, Opportunity, Report, Suppression, User
from aime.restore import RestoreRefused, restore_database
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.web.auth import COOKIE, hash_password, make_cookie, session_secret
from tests.conftest import hn_handler, make_ctx  # noqa: F401
from tests.uat.test_p2_recovery import MAIL, FakeSMTP, approved_email, send_run

PASS = "synthetic-passphrase-1"
BASE = "http://127.0.0.1:8765"
COVERS = "Record of sent emails covers every sent email"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def pc(make_ctx, monkeypatch):  # noqa: F811
    from aime import notify, timeutil
    from aime.scheduler import windows

    monkeypatch.setattr(timeutil, "CLOCK_OFFSET", datetime(2026, 11, 3, 16, 0) - datetime.now(timezone.utc).replace(tzinfo=None))  # Tuesday 11:00 ET
    monkeypatch.setattr(windows, "query", lambda *a, **k: {"installed": False, "error": "p5"})
    monkeypatch.setattr(notify, "has_desktop", lambda: False)

    def _new(**over):
        return make_ctx(**{**MAIL, "dashboard_port": free_port(), "backup_passphrase": PASS, **over})
    FakeSMTP.sent, FakeSMTP.on_connect = [], None
    return _new


def status(ctx, fid):
    with ctx.db.session() as s:
        return s.get(FollowUp, fid).status


def confirm(ctx):
    return cli.cmd_confirm_sending(types.SimpleNamespace(who="reviewer"), ctx)


def snapshot(ctx, tmp_path, name):
    p = tmp_path / name
    shutil.copyfile(create_bundle(ctx.settings, Path(ctx.db.engine.url.database), tmp_path / ("b" + name)), p)
    return p


def plain_copy(ctx, tmp_path):
    return B.backup_sqlite_file(Path(ctx.db.engine.url.database), tmp_path / "plain", label="auto")


def bare_pc(pc, **over):
    """A new folder: nothing has run in it."""
    new = pc(**over)
    new.db.dispose()
    shutil.rmtree(new.settings.data_dir)
    return new


def signed_in(ctx):
    with ctx.db.session() as s:
        u = User(username="p5", password_hash=hash_password("p5-synth-" + "value-1234"), role="super_admin")
        s.add(u)
        s.flush()
        ck = make_cookie(session_secret(ctx.settings.data_dir), u)
    client = TestClient(create_app(ctx), base_url=BASE, raise_server_exceptions=False, follow_redirects=False,
                        headers={"origin": BASE})
    client.cookies.set(COOKIE, ck)
    return client


# ---------------------------------------------------------------------------------------- an email the new PC cannot know about
@pytest.mark.parametrize("kind", ["zip", "db"])
def test_on_a_new_pc_an_approved_email_does_not_simply_go_after_confirm(pc, tmp_path, kind):
    """Approved in the copy, sent by the old PC after the copy, old PC gone: it comes back to a person one by one."""
    old = pc()
    fid = approved_email(old)
    copy = snapshot(old, tmp_path, "copy.zip") if kind == "zip" else plain_copy(old, tmp_path)
    send_run(old)
    assert FakeSMTP.sent == ["p@a.example"]
    new = bare_pc(pc)
    done = restore_database(new.settings, copy)
    assert done["unknown_sent"] == [fid] and done["already_sent"] == [] and done["approved_waiting"] == 0
    again = create_context(new.settings)
    assert status(again, fid) == "uncertain"
    with again.db.session() as s:
        row = s.get(FollowUp, fid)
        assert "unknown" in row.blocked_reason and "already went" not in row.blocked_reason
        assert row.sent_at is None and row.send_started_at is None
        assert s.execute(select(Alert).where(Alert.message.like(f"Follow-up #{fid}:%"))).scalars().first()
    send_run(again)
    confirm(again)
    send_run(again)
    assert FakeSMTP.sent == ["p@a.example"]  # the old PC's, only
    assert status(again, fid) == "uncertain"
    again.db.dispose()


def test_the_restore_prints_their_numbers_and_never_says_they_went(pc, tmp_path, capsys):
    old = pc()
    fid = approved_email(old)
    copy = plain_copy(old, tmp_path)
    new = bare_pc(pc)
    assert cli.cmd_restore(types.SimpleNamespace(source=str(copy), confirm=True), new.settings) == 0
    said = capsys.readouterr().out
    line = next(x for x in said.splitlines() if "may have sent" in x)
    assert f"[{fid}]" in line and "will not be sent again" not in line
    assert "only the Sent folder shows it" in said


def test_a_second_restore_on_the_new_pc_is_held_back_too(pc, tmp_path):
    """The journal has real claims by then, so "does it look new" alone would miss the copy chosen second."""
    old = pc()
    first = approved_email(old, "a")
    send_run(old)
    older = snapshot(old, tmp_path, "older.zip")  # holds a real claim
    second = approved_email(old, "b")
    time.sleep(1.1)
    newer = snapshot(old, tmp_path, "newer.zip")
    send_run(old)
    FakeSMTP.sent = []
    new = bare_pc(pc)
    restore_database(new.settings, older)
    assert send_journal.read(new.settings.data_dir).claims  # no longer looks new
    done = restore_database(new.settings, newer)
    assert done["unknown_sent"] == [second]
    again = create_context(new.settings)
    assert status(again, first) == "sent" and status(again, second) == "uncertain"
    confirm(again)
    send_run(again)
    assert FakeSMTP.sent == []
    again.db.dispose()


def test_a_second_restore_with_no_claim_at_all_after_the_first(pc, tmp_path):
    old = pc()
    copy_empty = plain_copy(old, tmp_path)
    fid = approved_email(old)
    time.sleep(1.1)
    copy_full = plain_copy(old, tmp_path)
    new = bare_pc(pc)
    restore_database(new.settings, copy_empty)
    assert restore_database(new.settings, copy_full)["unknown_sent"] == [fid]


def rehearsed_then_old_pc_went_on(pc, tmp_path, monkeypatch):
    """A rehearsal on the new PC, then the old PC approves an email a week later."""
    from aime import timeutil

    old = pc()
    first = approved_email(old, "a")
    rehearsal = snapshot(old, tmp_path, "rehearsal.zip")
    new = bare_pc(pc)
    restore_database(new.settings, rehearsal)
    monkeypatch.setattr(timeutil, "CLOCK_OFFSET", timeutil.CLOCK_OFFSET + timedelta(days=7))
    return old, new, first, approved_email(old, "b")


@pytest.mark.parametrize("kind", ["zip", "db"])
def test_a_rehearsal_does_not_let_a_later_copys_email_go_twice(pc, tmp_path, monkeypatch, kind):
    old, new, first, later = rehearsed_then_old_pc_went_on(pc, tmp_path, monkeypatch)
    copy = snapshot(old, tmp_path, "later.zip") if kind == "zip" else plain_copy(old, tmp_path)
    send_run(old)
    FakeSMTP.sent = []
    done = restore_database(new.settings, copy)
    assert sorted(done["unknown_sent"]) == [first, later] and done["approved_waiting"] == 0
    again = create_context(new.settings)
    assert status(again, later) == "uncertain"
    confirm(again)
    send_run(again)
    assert FakeSMTP.sent == []
    again.db.dispose()


def test_a_copy_from_the_old_pc_placed_by_hand_after_a_rehearsal_is_noticed(pc, tmp_path, monkeypatch):
    old, new, _, later = rehearsed_then_old_pc_went_on(pc, tmp_path, monkeypatch)
    copy = plain_copy(old, tmp_path)
    send_run(old)
    FakeSMTP.sent = []
    shutil.copyfile(copy, make_url(new.settings.database_url).database)
    again = create_context(new.settings)
    assert send_journal.hold(new.settings.data_dir) and status(again, later) == "uncertain"
    confirm(again)
    send_run(again)
    assert FakeSMTP.sent == []
    again.db.dispose()
    last = create_context(new.settings)  # looked at once: it does not fire again on every start
    assert send_journal.hold(new.settings.data_dir) is None
    last.db.dispose()


def test_the_old_pc_restoring_a_copy_the_new_pc_made_holds_back_its_emails(pc, tmp_path, monkeypatch):
    from aime import timeutil

    old = pc()
    approved_email(old, "a")
    rehearsal = plain_copy(old, tmp_path)
    send_run(old)
    new = bare_pc(pc)
    restore_database(new.settings, rehearsal)
    there = create_context(new.settings)
    confirm(there)
    monkeypatch.setattr(timeutil, "CLOCK_OFFSET", timeutil.CLOCK_OFFSET + timedelta(minutes=5))
    fid = approved_email(there, "n")
    back = plain_copy(there, tmp_path)
    there.db.dispose()
    old.db.dispose()
    assert fid in restore_database(old.settings, back)["unknown_sent"]


@pytest.mark.parametrize("kind", ["zip", "db"])
def test_on_a_new_pc_a_clock_behind_the_copy_holds_back_every_approved_email(pc, tmp_path, monkeypatch, kind):
    """A row's date against this PC's clock proves nothing on the first restore."""
    from aime import timeutil

    old = pc()
    fid = approved_email(old, "a")
    draft = approved_email(old, "b")
    with old.db.session() as s:
        s.get(FollowUp, draft).status = "awaiting_approval"
    copy = snapshot(old, tmp_path, "copy.zip") if kind == "zip" else plain_copy(old, tmp_path)
    new = bare_pc(pc)
    monkeypatch.setattr(timeutil, "CLOCK_OFFSET", timeutil.CLOCK_OFFSET - timedelta(minutes=30))
    assert restore_database(new.settings, copy)["unknown_sent"] == [fid]
    again = create_context(new.settings)
    assert status(again, fid) == "uncertain"
    with again.db.session() as s:
        assert s.get(FollowUp, draft).blocked_reason == send_journal.UNKNOWN_DRAFT
    confirm(again)
    FakeSMTP.sent = []
    send_run(again)
    assert FakeSMTP.sent == []
    again.db.dispose()


def test_what_was_approved_on_the_new_pc_is_known_and_left_alone(pc, tmp_path, monkeypatch):
    from aime import timeutil

    old = pc()
    copy = plain_copy(old, tmp_path)
    new = bare_pc(pc)
    restore_database(new.settings, copy)
    again = create_context(new.settings)
    confirm(again)
    monkeypatch.setattr(timeutil, "CLOCK_OFFSET", timeutil.CLOCK_OFFSET + timedelta(minutes=5))
    fid = approved_email(again, "n")
    later = plain_copy(again, tmp_path)
    again.db.dispose()
    done = restore_database(new.settings, later)
    assert done["unknown_sent"] == [] and done["approved_waiting"] == 1
    last = create_context(new.settings)
    assert status(last, fid) == "approved"
    last.db.dispose()


def test_a_database_copied_by_hand_with_no_restore(pc, tmp_path):
    old = pc()
    fid = approved_email(old)
    copy = plain_copy(old, tmp_path)
    send_run(old)
    FakeSMTP.sent = []
    new = pc()
    new.db.dispose()
    d = new.settings.data_dir
    shutil.rmtree(d)
    d.mkdir(parents=True)
    shutil.copyfile(copy, d / "aime.db")
    ctx = create_context(new.settings)
    assert send_journal.hold(d)
    assert status(ctx, fid) == "uncertain"
    confirm(ctx)
    send_run(ctx)
    assert FakeSMTP.sent == []
    ctx.db.dispose()


def test_doctor_first_then_restore(pc, tmp_path):
    old = pc()
    fid = approved_email(old)
    copy = snapshot(old, tmp_path, "copy.zip")
    send_run(old)
    FakeSMTP.sent = []
    new = pc()  # the first command on the new PC: an empty database, and a journal with one "started" line
    assert send_journal.looks_new(new.settings.data_dir)
    run_checks(new)
    new.db.dispose()
    assert restore_database(new.settings, copy)["unknown_sent"] == [fid]


def test_the_first_ever_start_holds_nothing(pc):
    ctx = pc()
    assert send_journal.hold(ctx.settings.data_dir) is None
    fid = approved_email(ctx)
    send_journal.start(ctx.db, ctx.settings)
    assert status(ctx, fid) == "approved"


def test_a_journal_made_by_an_empty_start_does_not_shut_out_the_one_in_the_copy(pc, tmp_path):
    """New PC: `aime doctor` (or a task) ran before the restore. The bundle's journal must still be taken, or a later restore
    from an older copy sends again what the first copy knew had gone."""
    old = pc()
    fid = approved_email(old)
    send_run(old)
    assert status(old, fid) == "sent"
    bundle = snapshot(old, tmp_path, "copy.zip")
    new = pc()
    new.db.dispose()
    restore_database(new.settings, bundle)
    assert fid in {i for i, _ in send_journal.read(new.settings.data_dir).claims}


def test_a_real_local_journal_is_never_replaced_by_the_copys(pc, tmp_path):
    ctx = pc()
    fid = approved_email(ctx)
    bundle = snapshot(ctx, tmp_path, "copy.zip")
    send_run(ctx)
    ctx.db.dispose()
    before = send_journal.journal_path(ctx.settings.data_dir).read_text(encoding="utf-8")
    done = restore_database(ctx.settings, bundle)
    after = send_journal.journal_path(ctx.settings.data_dir).read_text(encoding="utf-8")
    assert after.startswith(before) and done["already_sent"] == [fid] and done["unknown_sent"] == []


def test_on_the_same_pc_an_unclaimed_approved_email_stays_approved(pc, tmp_path):
    ctx = pc()
    first = approved_email(ctx, "a")
    send_run(ctx)  # a real journal
    second = approved_email(ctx, "b")
    copy = plain_copy(ctx, tmp_path)
    ctx.db.dispose()
    done = restore_database(ctx.settings, copy)
    assert done["unknown_sent"] == [] and done["already_sent"] == [] and done["approved_waiting"] == 1
    again = create_context(ctx.settings)
    assert status(again, first) == "sent" and status(again, second) == "approved"
    with again.db.session() as s:
        assert "unknown" not in (s.get(FollowUp, second).blocked_reason or "")
    again.db.dispose()


def test_a_draft_in_the_copy_keeps_its_status_and_gets_the_note(pc, tmp_path):
    old = pc()
    fid = approved_email(old)
    with old.db.session() as s:
        s.get(FollowUp, fid).status = "awaiting_approval"
    copy = plain_copy(old, tmp_path)
    new = bare_pc(pc)
    done = restore_database(new.settings, copy)
    assert done["unknown_sent"] == []
    again = create_context(new.settings)
    with again.db.session() as s:
        row = s.get(FollowUp, fid)
        assert row.status == "awaiting_approval" and "unknown" in row.blocked_reason
    again.db.dispose()


def test_an_opt_out_read_again_leaves_the_row_for_a_person_and_requeue_is_still_stopped(pc, tmp_path):
    """cancel_queued leaves "uncertain" alone. Requeue puts it back to approved, and the send gate cancels it: no opt-out lost."""
    from aime.outreach import cancel_queued

    old = pc()
    fid = approved_email(old)
    copy = plain_copy(old, tmp_path)
    new = bare_pc(pc)
    restore_database(new.settings, copy)
    ctx = create_context(new.settings)
    with ctx.db.session() as s:
        s.add(Suppression(kind="email", value="p@a.example", reason="unsubscribe"))
        row = s.get(FollowUp, fid)
        assert cancel_queued(s, row.lead, "opted out") == 0 and row.status == "uncertain"
    confirm(ctx)
    client = signed_in(ctx)
    assert client.post(f"/followups/{fid}/requeue").status_code in (200, 303)
    assert status(ctx, fid) == "approved" and not send_journal.read(ctx.settings.data_dir).claims
    send_run(ctx)
    assert FakeSMTP.sent == [] and status(ctx, fid) == "cancelled"
    ctx.db.dispose()


def test_marked_sent_by_a_person_does_not_go_after_a_later_restore(pc, tmp_path):
    ctx = pc()
    fid = approved_email(ctx)
    send_run(ctx)  # a real journal
    FakeSMTP.sent = []
    fid2 = approved_email(ctx, "b")
    copy = plain_copy(ctx, tmp_path)
    with ctx.db.session() as s:
        s.get(FollowUp, fid2).status = "uncertain"  # the send never wrote a claim
    client = signed_in(ctx)
    assert client.post(f"/followups/{fid2}/mark_sent").status_code in (200, 303)
    assert status(ctx, fid2) == "sent" and fid2 in {i for i, _ in send_journal.read(ctx.settings.data_dir).claims}
    ctx.db.dispose()
    done = restore_database(ctx.settings, copy)
    assert done["already_sent"] == [fid2]
    again = create_context(ctx.settings)
    confirm(again)
    send_run(again)
    assert FakeSMTP.sent == [] and status(again, fid2) == "uncertain" and status(again, fid) == "sent"
    again.db.dispose()


def test_mark_sent_changes_nothing_when_the_record_cannot_be_written(pc, monkeypatch):
    ctx = pc()
    fid = approved_email(ctx)
    with ctx.db.session() as s:
        s.get(FollowUp, fid).status = "uncertain"
    client = signed_in(ctx)

    def full(*a, **k):
        raise OSError("no room")
    monkeypatch.setattr(send_journal, "record_claim", full)
    client.post(f"/followups/{fid}/mark_sent")
    assert status(ctx, fid) == "uncertain"


# ---------------------------------------------------------------------------------------- a journal that lost lines
def test_a_journal_emptied_while_the_database_knows_of_sent_mail_is_noticed(pc):
    ctx = pc()
    fid = approved_email(ctx)
    send_run(ctx)
    assert status(ctx, fid) == "sent"
    send_journal.journal_path(ctx.settings.data_dir).write_bytes(b"")
    send_journal.start(ctx.db, ctx.settings)  # as on the next start
    assert send_journal.hold(ctx.settings.data_dir)
    assert fid in {i for i, _ in send_journal.read(ctx.settings.data_dir).claims}


def test_a_journal_cut_to_its_first_line_is_noticed_and_healed(pc, tmp_path):
    ctx = pc()
    fid = approved_email(ctx)
    older = plain_copy(ctx, tmp_path)
    send_run(ctx)
    path = send_journal.journal_path(ctx.settings.data_dir)
    path.write_text(path.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    bad = next(c for c in run_checks(ctx) if c["name"] == COVERS)
    assert not bad["ok"] and bad["severity"] == "error" and "record of sent" in bad["detail"].lower()
    ctx.db.dispose()
    again = create_context(ctx.settings)  # the next start
    assert next(c for c in run_checks(again) if c["name"] == COVERS)["ok"]
    with again.db.session() as s:
        assert s.execute(select(Alert).where(Alert.message.like("The record of sent emails%"))).scalars().first()
    again.db.dispose()
    restore_database(ctx.settings, older)
    last = create_context(ctx.settings)
    assert status(last, fid) == "uncertain"
    confirm(last)
    send_run(last)
    assert FakeSMTP.sent == ["p@a.example"]
    last.db.dispose()


def test_a_send_in_hand_and_a_new_pc_row_are_not_called_lost(pc, tmp_path):
    ctx = pc()
    fid = approved_email(ctx, "a")
    with ctx.db.session() as s:
        row = s.get(FollowUp, fid)
        row.status, row.send_started_at = "sending", utcnow()
    assert next(c for c in run_checks(ctx) if c["name"] == COVERS)["ok"]
    with ctx.db.session() as s:
        row = s.get(FollowUp, fid)
        row.status, row.send_started_at, row.blocked_reason = "uncertain", None, send_journal.UNKNOWN_SENT
    assert next(c for c in run_checks(ctx) if c["name"] == COVERS)["ok"]
    with ctx.db.session() as s:
        s.get(FollowUp, fid).blocked_reason = "Sending was interrupted"
    assert not next(c for c in run_checks(ctx) if c["name"] == COVERS)["ok"]


# ---------------------------------------------------------------------------------------- deal documents
def a_deal(ctx):
    with ctx.db.session() as s:
        co = Company(name="Synth deal", normalized_name="synth deal", source="test", domain="deal.example")
        s.add(co)
        s.flush()
        opp = Opportunity(company_id=co.id, name="Synth deal", stage="new")
        s.add(opp)
        s.flush()
        return opp.id


@pytest.mark.parametrize("kind", ["aimebak", "zip"])
def test_the_off_machine_copy_holds_uploaded_deal_documents(pc, tmp_path, kind):
    off = tmp_path / "off"
    body = b"%PDF signed contract"
    old = pc(backup_offsite_dir=str(off))
    opp = a_deal(old)
    client = signed_in(old)
    assert client.post(f"/opportunities/{opp}/attachments", files={"file": ("contract.pdf", body, "application/pdf")}).status_code in (200, 303)
    with old.db.session() as s:
        att = s.execute(select(Attachment)).scalars().one().id
    assert client.get(f"/opportunities/{opp}/attachments/{att}").content == body
    made = write_offsite(old.settings, Path(old.db.engine.url.database))
    assert made["status"] == "ok" and not made["documents_skipped"]
    held = list((off / "attachments").iterdir())
    assert len(held) == 1 and body not in held[0].read_bytes()  # encrypted
    source = off / made["file"] if kind == "aimebak" else snapshot(old, tmp_path, "copy.zip")
    new = bare_pc(pc, backup_offsite_dir=str(off))
    done = restore_database(new.settings, source)
    assert done["documents_back"] == 1 and done["documents_missing"] == []
    again = create_context(new.settings)
    assert signed_in_again(again).get(f"/opportunities/{opp}/attachments/{att}").content == body
    again.db.dispose()


def signed_in_again(ctx):
    with ctx.db.session() as s:
        u = User(username="p5b", password_hash=hash_password("p5-synth-" + "value-1234"), role="super_admin")
        s.add(u)
        s.flush()
        ck = make_cookie(session_secret(ctx.settings.data_dir), u)
    client = TestClient(create_app(ctx), base_url=BASE, raise_server_exceptions=False, follow_redirects=False)
    client.cookies.set(COOKIE, ck)
    return client


def test_each_document_is_written_once_and_the_bundle_does_not_hold_it(pc, tmp_path):
    import zipfile

    off = tmp_path / "off"
    ctx = pc(backup_offsite_dir=str(off))
    folder = ctx.settings.data_dir / "attachments"
    folder.mkdir()
    (folder / "abc.pdf").write_bytes(b"%PDF one")
    first = write_offsite(ctx.settings, Path(ctx.db.engine.url.database))
    copy = off / "attachments" / "abc.pdf.aimebak"
    stamp = copy.read_bytes()
    time.sleep(1.1)
    write_offsite(ctx.settings, Path(ctx.db.engine.url.database))
    assert copy.read_bytes() == stamp
    with zipfile.ZipFile(create_bundle(ctx.settings, Path(ctx.db.engine.url.database), tmp_path / "b")) as zf:
        assert not any("abc.pdf" in n for n in zf.namelist())
    assert first["status"] == "ok"


def test_a_document_that_cannot_be_read_never_fails_the_copy_of_the_database(pc, tmp_path, monkeypatch):
    off = tmp_path / "off"
    ctx = pc(backup_offsite_dir=str(off))
    folder = ctx.settings.data_dir / "attachments"
    folder.mkdir()
    (folder / "held.pdf").write_bytes(b"%PDF held")
    (folder / "fine.pdf").write_bytes(b"%PDF fine")
    (folder / "sub").mkdir()
    real = Path.read_bytes

    def locked(self):
        if self.name == "held.pdf":
            raise PermissionError(13, "held by another program")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", locked)
    result = write_offsite(ctx.settings, Path(ctx.db.engine.url.database))
    assert result["status"] == "ok" and result["documents_skipped"] == ["held.pdf"]
    assert (off / "attachments" / "fine.pdf.aimebak").exists() and not (off / "attachments" / "held.pdf.aimebak").exists()
    with ctx.db.session() as s:
        B.record_offsite(s, result)
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.message.like("%deal document(s) could not%"))).scalars().first()


def test_a_copy_that_does_not_match_its_row_is_not_put_back(pc, tmp_path):
    off = tmp_path / "off"
    ctx = pc(backup_offsite_dir=str(off))
    (off / "attachments").mkdir(parents=True)
    (off / "attachments" / "x.pdf.aimebak").write_bytes(B.encrypt_bytes(b"something else", PASS))
    assert B.bring_back_documents(ctx.settings, [("x.pdf", "0" * 64, "contract.pdf")]) == (0, ["contract.pdf"])
    assert not (ctx.settings.data_dir / "attachments" / "x.pdf").exists()


def test_doctor_names_a_missing_document_by_name_and_size_only(pc, tmp_path):
    off = tmp_path / "off"
    ctx = pc(backup_offsite_dir=str(off))
    opp = a_deal(ctx)
    folder = ctx.settings.data_dir / "attachments"
    folder.mkdir()
    (folder / "a.pdf").write_bytes(b"12345")
    with ctx.db.session() as s:
        s.add(Attachment(opportunity_id=opp, filename="contract.pdf", stored_as="a.pdf", content_type="application/pdf", size=5, sha256="0" * 64, uploaded_by="p5"))

    def check():
        return next(c for c in run_checks(ctx) if c["name"] == "Deal documents are safe")
    assert check()["ok"]  # added today: tomorrow's copy takes it
    with ctx.db.session() as s:
        s.execute(select(Attachment)).scalars().one().created_at = utcnow() - timedelta(hours=31)
    got = check()
    assert not got["ok"] and got["severity"] == "warning" and "no off-machine copy" in got["detail"] and "contract.pdf" in got["detail"]
    (folder / "a.pdf").unlink()
    assert "missing" in check()["detail"]


def test_too_little_memory_in_the_drill_is_recorded_not_lost(pc, tmp_path, monkeypatch):
    off = tmp_path / "off"
    ctx = pc(backup_offsite_dir=str(off))
    write_offsite(ctx.settings, Path(ctx.db.engine.url.database))
    real = Path.read_bytes

    def short(self):
        if self.suffix == ".aimebak":
            raise MemoryError
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", short)
    assert "memory" in restore_drill(ctx.settings, next(off.glob("*.aimebak")))["detail"]
    step = weekly_offsite_drill(ctx)
    assert step["status"] == "failed" and "memory" in step["error"]


# ---------------------------------------------------------------------------------------- report files
def a_report(ctx):
    path = ctx.settings.reports_dir / "weekly.html"
    path.write_text("<html><body>report body</body></html>", encoding="utf-8")
    (ctx.settings.reports_dir / "weekly.md").write_text("report", encoding="utf-8")
    (ctx.settings.reports_dir / "other.txt").write_text("not a report", encoding="utf-8")
    with ctx.db.session() as s:
        r = Report(report_date=utcnow().date(), kind="weekly", path_md=str(path.with_suffix(".md")), path_html=str(path))
        s.add(r)
        s.flush()
        return r.id


def test_the_off_machine_copy_holds_the_report_files(pc, tmp_path):
    old = pc()
    a_report(old)
    bundle = snapshot(old, tmp_path, "copy.zip")
    new = bare_pc(pc)
    restore_database(new.settings, bundle)
    assert (new.settings.reports_dir / "weekly.html").exists() and (new.settings.reports_dir / "weekly.md").exists()
    assert not (new.settings.reports_dir / "other.txt").exists()


def test_a_report_already_here_is_not_overwritten(pc, tmp_path):
    ctx = pc()
    a_report(ctx)
    bundle = snapshot(ctx, tmp_path, "copy.zip")
    (ctx.settings.reports_dir / "weekly.html").write_text("newer", encoding="utf-8")
    ctx.db.dispose()
    restore_database(ctx.settings, bundle)
    assert (ctx.settings.reports_dir / "weekly.html").read_text(encoding="utf-8") == "newer"


@pytest.mark.xfail(strict=False, reason="for the integrator: passes once web-routes resolves the path in report_view")
def test_a_restored_report_opens_on_the_new_pc(pc, tmp_path):
    old = pc()
    rid = a_report(old)
    bundle = snapshot(old, tmp_path, "copy.zip")
    shutil.rmtree(old.settings.reports_dir)  # the old PC is gone: the stored path leads nowhere
    new = bare_pc(pc)
    restore_database(new.settings, bundle)
    again = create_context(new.settings)
    got = signed_in_again(again).get(f"/reports/{rid}")
    again.db.dispose()
    assert got.status_code == 200 and "report body" in got.text


# ---------------------------------------------------------------------------------------- what the wording says
def test_a_copy_another_program_holds_is_not_called_damaged(pc, tmp_path, monkeypatch):
    ctx = pc(backup_offsite_dir=str(tmp_path / "off"))
    made = write_offsite(ctx.settings, Path(ctx.db.engine.url.database))
    held = tmp_path / "off" / made["file"]
    real = Path.read_bytes

    def locked(self):
        if self == held:
            raise PermissionError(13, "held by another program", str(held))
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", locked)
    got = restore_drill(ctx.settings, held)
    assert got["ok"] is False and "damaged" not in got["detail"] and "another program" in got["detail"] and str(tmp_path) not in got["detail"]
    assert weekly_offsite_drill(ctx) is None  # not time yet: no alarm, and tomorrow's run asks again
    with pytest.raises(RestoreRefused):
        restore_database(ctx.settings, held)


@pytest.mark.skipif(os.name != "nt", reason="an exclusive handle is a Windows thing")
def test_a_real_exclusive_handle_is_not_called_damaged(pc, tmp_path):
    import ctypes

    ctx = pc(backup_offsite_dir=str(tmp_path / "off"))
    made = write_offsite(ctx.settings, Path(ctx.db.engine.url.database))
    held = tmp_path / "off" / made["file"]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = ctypes.c_void_p
    k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.CreateFileW(str(held), 0x80000000, 0, None, 3, 0x80, None)
    try:
        got = restore_drill(ctx.settings, held)
        assert got["ok"] is False and got["detail"] == B.HELD_NOW
        assert weekly_offsite_drill(ctx) is None
    finally:
        k.CloseHandle(h)


def test_a_real_damaged_copy_is_still_called_damaged(pc, tmp_path):
    ctx = pc()
    bad = tmp_path / "copy.zip"
    bad.write_bytes(b"not a zip")
    got = restore_drill(ctx.settings, bad)
    assert got["ok"] is False and "damaged" in got["detail"] and "held" not in got


def test_a_copy_replaced_by_an_older_one_is_told_so(pc, tmp_path):
    off = tmp_path / "off"
    ctx = pc(backup_offsite_dir=str(off))
    db = Path(ctx.db.engine.url.database)
    first = write_offsite(ctx.settings, db)
    keep = (off / first["file"]).read_bytes()
    with ctx.db.session() as s:
        for i in range(50):
            s.add(Company(name=f"c{i}", normalized_name=f"c{i}", source="test"))
    time.sleep(1.1)
    second = write_offsite(ctx.settings, db)
    (off / second["file"]).write_bytes(keep)
    assert "cut short or replaced" in offsite_copy_problem(ctx.settings, second)
    renamed = off / "aime-offsite-20200101-000000.aimebak"
    renamed.write_bytes(keep)
    got = restore_drill(ctx.settings, renamed)
    assert got["ok"] is True and "WARNING" in got["detail"] and "not the date in its name" in got["detail"]
    assert "WARNING" not in restore_drill(ctx.settings, off / first["file"])["detail"]


def test_a_synced_folder_is_not_claimed_as_uploaded(tmp_path):
    root = tmp_path / "OneDrive"
    leaves, words = B.offsite_sync(root / "AIME", synced=[root])
    assert leaves is True and "confirm on onedrive.com" in words and "synced by" not in words


def test_doctor_warns_when_the_restore_check_of_the_copy_has_stopped(pc, tmp_path):
    ctx = pc(backup_offsite_dir=str(tmp_path / "off"))

    def check():
        return [c for c in run_checks(ctx) if c["name"] == "Off-machine copy restored recently"]
    assert not check()
    with ctx.db.session() as s:
        kv_set(s, "backup.last_offsite_drill", (utcnow() - timedelta(days=3)).isoformat())
    assert check()[0]["ok"]
    with ctx.db.session() as s:
        kv_set(s, "backup.last_offsite_drill", (utcnow() - timedelta(days=15)).isoformat())
    got = check()[0]
    assert not got["ok"] and got["severity"] == "warning"
