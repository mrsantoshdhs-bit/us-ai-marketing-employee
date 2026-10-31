"""Phase 4, a home PC: lost network, a PC asleep inside a send run, a broken off-machine folder, held logs, another program on
the port. Each test named test_defect_ failed on release #74. Synthetic data only.
"""

from __future__ import annotations

import ctypes
import http.server
import importlib.util
import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

import aime.config
import aime.timeutil
from aime import logging_setup
from aime.agents.crm import CRMManagementAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.backup import encrypt_bytes, prune_offsite, write_offsite
from aime.db import kv_set
from aime.doctor import run_checks
from aime.integrations.email_smtp import EmailSender
from aime.models import Alert, Company, FollowUp, JobRun, Lead
from aime.scheduler.runner import run_job
from aime.timeutil import utcnow
from aime.web.app import health_strip
from aime import workflows
from aime.workflows import MAILBOX_READ, job_crm_maintenance, job_followups
from tests.test_inbound_sales import FakeSMTP
from tests.test_time_and_alerts import FOOTER, MAIL, add_lead, add_mail

PASS = "a-test-passphrase-long-enough"


def gone_folder(tmp_path):
    """A folder that cannot be made: a drive that is not there; elsewhere, a folder under a file."""
    if sys.platform == "win32":
        return r"Z:\NoSuchDrive\AIME"
    (tmp_path / "a-file").write_text("x")
    return str(tmp_path / "a-file" / "AIME")


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET)

    def set_to(when):
        aime.timeutil.CLOCK_OFFSET = when - datetime.now(timezone.utc).replace(tzinfo=None)
        return when

    return set_to


def offline(request):
    raise httpx.ConnectError("no network: the Wi-Fi is not back yet")


def held(path):
    """A handle like a sync client's or Excel's: reading shared, delete, rename and write refused. Only Windows has these."""
    if sys.platform != "win32":
        pytest.skip("a file held open blocks delete and rename only on Windows")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = ctypes.c_void_p
    k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    assert h not in (None, ctypes.c_void_p(-1).value)
    return lambda: k.CloseHandle(h)


# ---------------------------------------------------------------------------------- the network is down when a daily job runs
def verified_site_due_for_recheck(ctx):
    lead_id = add_lead(ctx, status="qualified")
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        lead.source = "research"
        co = s.get(Company, lead.company_id)
        co.website_status, co.website_checked_at = "verified", utcnow() - timedelta(days=31)
        return lead_id, co.id


def test_defect_a_lost_network_does_not_call_a_verified_website_unreachable_for_30_days(make_ctx):
    ctx = make_ctx(handler=offline, **MAIL)
    _, company = verified_site_due_for_recheck(ctx)
    job_crm_maintenance(ctx)
    with ctx.db.session() as s:
        assert s.get(Company, company).website_status == "verified"  # nothing was learned about that site


def test_defect_a_lost_network_does_not_move_a_qualified_lead_to_nurture_or_cancel_its_draft(make_ctx):
    ctx = make_ctx(handler=offline, **MAIL)
    lead_id, company = verified_site_due_for_recheck(ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        f = FollowUp(lead_id=lead_id, company_id=company, contact_id=lead.contact_id, channel="email", step=1, status="awaiting_approval",
                     kind="outreach", to_email="x@acme.example", subject="Hello", body=FOOTER, due_at=utcnow())
        s.add(f)
        s.flush()
        draft = f.id
    # the lead's score with the site verified, one point over the line: as lead numbers 24 and 37 on the copy
    with ctx.db.session() as s:
        s.get(Company, company).website_status = "verified"
        s.get(Company, company).website_checked_at = utcnow()
    ctx.settings.business["qualification"]["grade_b"] = 0  # this first scoring is only to read the score: it must not cancel the draft
    LeadQualificationAgent(ctx).run({}, job="test")
    with ctx.db.session() as s:
        score = s.get(Lead, lead_id).score
        s.get(Company, company).website_checked_at = utcnow() - timedelta(days=31)
    ctx.settings.business["qualification"]["grade_b"] = score - 1
    ctx.settings.business["qualification"]["grade_a"] = score + 50
    LeadQualificationAgent(ctx).run({}, job="test")
    with ctx.db.session() as s:
        s.get(Lead, lead_id).status = "qualified"
    job_crm_maintenance(ctx)
    with ctx.db.session() as s:
        assert (s.get(Lead, lead_id).status, s.get(FollowUp, draft).status, s.get(FollowUp, draft).blocked_reason) == ("qualified", "awaiting_approval", None)


def sites_due(ctx, *domains):
    ids = []
    with ctx.db.session() as s:
        for d in domains:
            co = Company(name=d.split(".")[0].title(), normalized_name=d.split(".")[0], source="test", domain=d, website=f"https://{d}",
                         website_status="verified", website_checked_at=utcnow() - timedelta(days=31))
            s.add(co)
            s.flush()
            ids.append(co.id)
    return ids


def check_sites(ctx, **more):
    return CRMManagementAgent(ctx).run({"operations": ["verify_websites"], **more}, job="test")


def web(answers):
    """Sites named answer as given; every other request fails like a site that is not there."""
    def handler(request):
        site = request.url.host
        if site not in answers:
            raise httpx.ConnectError("no answer")
        code, body = answers[site]
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(code, headers={"location": body}) if code == 301 else httpx.Response(code, text=body)
    return handler


def status_of(ctx, company):
    with ctx.db.session() as s:
        return s.get(Company, company).website_status


def open_alerts(ctx):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert).where(Alert.resolved_at.is_(None))).scalars() if a.key == "website_check:outage"]


def test_a_site_that_never_answers_while_others_do_becomes_unreachable_only_after_days(make_ctx, clock):
    start = clock(datetime(2026, 11, 3, 1, 0))
    ctx = make_ctx(handler=web({"alive.example": (200, "<title>Alive</title>")}))
    dead, alive = sites_due(ctx, "dead.example", "alive.example")
    check_sites(ctx)
    assert status_of(ctx, dead) == "verified" and not open_alerts(ctx)
    check_sites(ctx)  # the same day: not due again, and it does not count again
    with ctx.db.session() as s:
        assert s.get(Company, dead).website_no_answer == 1
    seen = []
    for hours in (21, 42, 63):
        clock(start + timedelta(hours=hours))
        check_sites(ctx)
        seen.append(status_of(ctx, dead))
    assert seen == ["verified", "verified", "unreachable"]
    assert status_of(ctx, alive) == "verified"
    with ctx.db.session() as s:
        co = s.get(Company, dead)
        assert co.website_no_answer is None and co.website_retry_at is None
        assert co.website_checked_at is not None and (utcnow() - co.website_checked_at) < timedelta(minutes=1)  # next look in 30 days


def test_a_real_answer_still_counts_at_once(make_ctx):
    for code, body, status in ((404, "", "unreachable"), (410, "", "unreachable"), (200, "buy this domain", "parked"),
                               (301, "https://elsewhere.example/", "mismatch")):
        ctx = make_ctx(handler=web({"acme.example": (code, body), "elsewhere.example": (200, "<title>Unrelated</title>")}))
        company = sites_due(ctx, "acme.example")[0]
        check_sites(ctx)
        assert status_of(ctx, company) == status


def test_an_answer_after_silence_clears_the_count(make_ctx, clock):
    start = clock(datetime(2026, 11, 3, 1, 0))
    answers = {}
    ctx = make_ctx(handler=web(answers))
    company = sites_due(ctx, "acme.example")[0]
    check_sites(ctx)
    answers["acme.example"] = (200, "<title>Acme</title>")
    clock(start + timedelta(hours=21))
    check_sites(ctx)
    with ctx.db.session() as s:
        co = s.get(Company, company)
        assert (co.website_status, co.website_no_answer, co.website_retry_at) == ("verified", None, None)


def test_an_outage_is_said_in_words_counts_nothing_and_ends_with_the_network(make_ctx, clock):
    start = clock(datetime(2026, 11, 3, 1, 0))
    answers = {}
    ctx = make_ctx(handler=web(answers))
    witness = sites_due(ctx, "witness.example")[0]
    with ctx.db.session() as s:
        s.get(Company, witness).website_checked_at = utcnow()  # checked lately: not due, it can only witness
    due = sites_due(ctx, *[f"site{i}.example" for i in range(8)])
    for hours in (0, 2, 4):
        clock(start + timedelta(hours=hours))
        check_sites(ctx)
    with ctx.db.session() as s:
        rows = [s.get(Company, c) for c in due]
        assert {c.website_status for c in rows} == {"verified"} and not any(c.website_no_answer for c in rows)
        assert all((c.website_retry_at or utcnow()) <= utcnow() + timedelta(hours=1) for c in rows)  # soon, not in 30 days
    said = open_alerts(ctx)
    assert len(said) == 1 and "internet connection looks down" in said[0] and "website_check" not in said[0]
    answers.update({f"site{i}.example": (200, f"<title>Site{i}</title>") for i in range(8)})
    answers["witness.example"] = (200, "<title>Witness</title>")
    clock(start + timedelta(hours=6))
    check_sites(ctx)
    assert not open_alerts(ctx)


def test_a_run_of_dead_sites_is_not_taken_for_an_outage_when_a_site_that_answered_before_still_answers(make_ctx):
    ctx = make_ctx(handler=web({"witness.example": (404, "")}))
    witness = sites_due(ctx, "witness.example")[0]
    with ctx.db.session() as s:
        s.get(Company, witness).website_checked_at = utcnow()
    dead = sites_due(ctx, "a.example", "b.example", "c.example")
    check_sites(ctx)
    assert not open_alerts(ctx)
    with ctx.db.session() as s:
        assert all(s.get(Company, c).website_no_answer == 1 for c in dead)


def test_an_outage_does_not_wait_out_every_timeout_of_a_long_list(make_ctx):
    ctx = make_ctx(handler=offline)
    sites_due(ctx, *[f"site{i}.example" for i in range(20)])
    assert check_sites(ctx).output.websites_checked == 5


def test_a_run_with_nothing_due_does_not_end_an_outage_it_cannot_see(make_ctx, clock):
    start = clock(datetime(2026, 11, 3, 1, 0))
    ctx = make_ctx(handler=offline)
    witness = sites_due(ctx, "witness.example")[0]
    with ctx.db.session() as s:
        s.get(Company, witness).website_checked_at = utcnow()
    sites_due(ctx, "a.example", "b.example", "c.example")
    check_sites(ctx)
    assert open_alerts(ctx)
    clock(start + timedelta(minutes=30))  # all three look again only after an hour
    check_sites(ctx)
    assert open_alerts(ctx)


def test_a_site_never_checked_stays_unchecked_when_nothing_answers(make_ctx):
    ctx = make_ctx(handler=offline)
    with ctx.db.session() as s:
        co = Company(name="New Co", normalized_name="new co", source="test", domain="new.example")
        s.add(co)
        s.flush()
        company = co.id
    check_sites(ctx)
    with ctx.db.session() as s:
        co = s.get(Company, company)
        assert co.website_status is None and co.website_checked_at is None and co.website_retry_at is not None


# ---------------------------------------------------------------------------------- the PC sleeps inside a send run
def two_approved(ctx):
    ids = []
    for _ in range(2):
        ids.append(add_mail(ctx, add_lead(ctx, stage="approved")))
    return ids


def test_defect_the_rest_of_a_run_does_not_go_out_at_night_after_the_pc_slept_between_two_emails(make_ctx, clock):
    ctx = make_ctx(**MAIL)
    start = clock(datetime(2026, 11, 3, 22, 59))  # Tuesday 17:59 New York
    two_approved(ctx)
    sent_at = []

    class Sleeps(FakeSMTP):
        def send_message(self, msg, *a, **k):
            sent_at.append(utcnow())
            clock(start + timedelta(hours=10))  # the laptop lid closes; the process carries on when it opens, 03:59 New York
            return {}

    SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=Sleeps)).run({"now": utcnow().isoformat()})
    assert len(sent_at) == 1, f"{len(sent_at)} emails left; the later ones at {sent_at[1:]} UTC, outside the send window"


def smtp_recording(sent, also=None):
    class Recording(FakeSMTP):
        def send_message(self, msg, *a, **k):
            sent.append(msg["To"])
            if also:
                also(len(sent))
            return {}
    return Recording


def followups_job_with(monkeypatch, smtp):
    """The real job, with a mail server that is not real."""
    monkeypatch.setattr(workflows, "SalesWorkflowAgent", lambda c: SalesWorkflowAgent(c, sender=EmailSender(c.settings, smtp_factory=smtp)))


def mailbox_read_now(ctx):
    with ctx.db.session() as s:
        kv_set(s, MAILBOX_READ, utcnow().isoformat())


def rows(ctx, ids):
    with ctx.db.session() as s:
        return [(s.get(FollowUp, i).status, s.get(FollowUp, i).blocked_reason or "") for i in ids]


def test_what_a_sleep_stopped_goes_out_once_on_the_next_proper_run_and_nothing_twice(make_ctx, clock, monkeypatch):
    ctx = make_ctx(**MAIL)
    start = clock(datetime(2026, 11, 3, 22, 59))  # Tuesday 17:59 New York
    ids = two_approved(ctx)
    sent = []

    def lid_closes(n):
        if n == 1:
            clock(start + timedelta(hours=10))

    followups_job_with(monkeypatch, smtp_recording(sent, lid_closes))
    mailbox_read_now(ctx)
    job_followups(ctx)
    assert len(sent) == 1
    states = rows(ctx, ids)
    assert sorted(s for s, _ in states) == ["approved", "sent"]
    assert any("slept" in why for _, why in states)

    clock(start + timedelta(hours=10, minutes=30))  # still 04:29 New York: a new read does not open the window
    mailbox_read_now(ctx)
    job_followups(ctx)
    assert len(sent) == 1

    clock(start + timedelta(hours=16))  # 09:59 New York
    mailbox_read_now(ctx)
    job_followups(ctx)
    assert sorted(sent) == sorted(set(sent)) and len(sent) == 2
    assert [s for s, _ in rows(ctx, ids)] == ["sent", "sent"]
    mailbox_read_now(ctx)
    job_followups(ctx)  # and once more: nothing is left to send
    assert len(sent) == 2


def test_a_read_that_goes_bad_during_a_run_stops_the_rest_even_inside_the_window(make_ctx, clock, monkeypatch):
    ctx = make_ctx(**MAIL)
    clock(datetime(2026, 11, 4, 15, 0))  # Wednesday 10:00 New York
    ids = two_approved(ctx)
    sent = []

    def read_fails_meanwhile(n):
        with ctx.db.session() as s:
            kv_set(s, MAILBOX_READ, None)

    followups_job_with(monkeypatch, smtp_recording(sent, read_fails_meanwhile))
    mailbox_read_now(ctx)
    job_followups(ctx)
    assert len(sent) == 1
    assert any(s == "approved" and why.startswith("Held:") for s, why in rows(ctx, ids))


def test_a_nap_of_half_an_hour_inside_the_window_stops_the_run_too(make_ctx, clock, monkeypatch):
    ctx = make_ctx(**MAIL)
    start = clock(datetime(2026, 11, 4, 15, 0))
    two_approved(ctx)
    sent = []
    followups_job_with(monkeypatch, smtp_recording(sent, lambda n: clock(start + timedelta(minutes=30))))
    mailbox_read_now(ctx)
    job_followups(ctx)
    assert len(sent) == 1


def test_a_clock_set_back_inside_a_run_stops_it(make_ctx, clock, monkeypatch):
    ctx = make_ctx(**MAIL)
    start = clock(datetime(2026, 11, 4, 15, 0))
    two_approved(ctx)
    sent = []
    followups_job_with(monkeypatch, smtp_recording(sent, lambda n: clock(start - timedelta(hours=1))))
    mailbox_read_now(ctx)
    job_followups(ctx)
    assert len(sent) == 1


def test_a_slow_mail_server_does_not_stop_a_run(make_ctx, clock, monkeypatch):
    ctx = make_ctx(**MAIL)
    start = clock(datetime(2026, 11, 4, 15, 0))
    ids = two_approved(ctx)
    sent = []
    followups_job_with(monkeypatch, smtp_recording(sent, lambda n: clock(start + timedelta(minutes=2 * n))))
    mailbox_read_now(ctx)
    job_followups(ctx)
    assert len(sent) == 2 and [s for s, _ in rows(ctx, ids)] == ["sent", "sent"]


def test_a_run_that_leaves_the_window_while_awake_stops_at_the_edge(make_ctx, clock, monkeypatch):
    ctx = make_ctx(**MAIL)
    start = clock(datetime(2026, 11, 3, 22, 59))  # 17:59 New York; two minutes later the window has closed, with no jump
    ids = two_approved(ctx)
    sent = []
    followups_job_with(monkeypatch, smtp_recording(sent, lambda n: clock(start + timedelta(minutes=2))))
    mailbox_read_now(ctx)
    job_followups(ctx)
    assert len(sent) == 1
    assert any(s == "approved" and "send window" in why for s, why in rows(ctx, ids))


def test_a_run_begun_outside_the_window_or_on_a_stale_read_sends_nothing_as_before(make_ctx, clock, monkeypatch):
    ctx = make_ctx(**MAIL)
    clock(datetime(2026, 11, 4, 3, 0))  # 22:00 New York
    ids = two_approved(ctx)
    sent = []
    followups_job_with(monkeypatch, smtp_recording(sent))
    mailbox_read_now(ctx)
    job_followups(ctx)
    assert not sent and all(s == "approved" and "send window" in why for s, why in rows(ctx, ids))
    clock(datetime(2026, 11, 4, 15, 0))  # in the window, the read over 20 minutes old
    job_followups(ctx)
    assert not sent and all(s == "approved" and why.startswith("Held:") for s, why in rows(ctx, ids))


# ---------------------------------------------------------------------------------- the off-machine folder
def offsite_ctx(make_ctx, tmp_path, **more):
    return make_ctx(backup_offsite_dir=str(tmp_path / "off"), backup_passphrase=PASS, **more)


def test_defect_an_old_copy_a_sync_client_holds_does_not_turn_todays_good_copy_into_a_failure(make_ctx, tmp_path):
    ctx = offsite_ctx(make_ctx, tmp_path)
    folder = tmp_path / "off"
    folder.mkdir()
    for i in range(31):
        (folder / f"aime-offsite-202609{i % 28 + 1:02d}-0{i // 28}0000.aimebak").write_bytes(b"old")
    close = held(sorted(folder.glob("*.aimebak"))[0])
    try:
        result = run_job(ctx, "db_maintenance", trigger="test")
        checks = {c["name"]: c for c in run_checks(ctx)}
    finally:
        close()
    written = [p for p in folder.glob("*.aimebak") if p.read_bytes() != b"old"]
    assert written, "today's copy should be there"
    assert result["status"] == "success", result["error"]
    assert "no copy written" not in checks["Off-machine encrypted backup"]["detail"]


def test_defect_prune_keeps_the_last_copy_that_can_still_be_read(tmp_path):
    good = tmp_path / "aime-offsite-20260901-000000.aimebak"
    good.write_bytes(encrypt_bytes(b"not a real bundle", PASS))
    for i in range(30):
        (tmp_path / f"aime-offsite-202610{i + 1:02d}-000000.aimebak").write_bytes(b"cut short by a sync client")
    prune_offsite(tmp_path)
    assert good.exists()


def test_defect_a_half_written_copy_left_by_a_power_cut_does_not_stay_in_the_synced_folder_for_ever(make_ctx, tmp_path):
    ctx = offsite_ctx(make_ctx, tmp_path)
    folder = tmp_path / "off"
    folder.mkdir()
    left = folder / "aime-offsite-20261030-061500.partial"
    left.write_bytes(b"half of an encrypted database")
    write_offsite(ctx.settings, Path(ctx.db.engine.url.database))
    assert not left.exists()


def test_defect_doctor_does_not_pass_a_copy_that_is_no_longer_in_the_folder(make_ctx, tmp_path):
    ctx = offsite_ctx(make_ctx, tmp_path)
    run_job(ctx, "db_maintenance", trigger="test")
    for p in (tmp_path / "off").glob("*.aimebak"):
        p.unlink()
    check = next(c for c in run_checks(ctx) if c["name"] == "Off-machine encrypted backup")
    assert not check["ok"], check["detail"]


def test_defect_the_home_page_does_not_say_ok_while_the_off_machine_copy_keeps_failing(make_ctx, tmp_path):
    ctx = make_ctx(backup_offsite_dir=gone_folder(tmp_path), backup_passphrase=PASS)
    result = run_job(ctx, "db_maintenance", trigger="test")
    assert result["status"] != "success"
    with ctx.db.session() as s:
        row = next(r for r in health_strip(ctx, s) if r[0] == "Off-machine backup")
    assert row[1] != "ok", row


def test_defect_the_alert_says_the_off_machine_copy_failed_and_why_not_that_the_backup_job_failed(make_ctx, clock, tmp_path):
    ctx = make_ctx(backup_offsite_dir=gone_folder(tmp_path), backup_passphrase=PASS)
    start = clock(datetime(2026, 11, 3, 1, 0))
    run_job(ctx, "db_maintenance", trigger="test")
    clock(start + timedelta(minutes=45))
    run_job(ctx, "db_maintenance", trigger="test")
    run_job(ctx, "monitor", trigger="test")
    with ctx.db.session() as s:
        said = " ".join(a.message for a in s.execute(select(Alert).where(Alert.level == "error")).scalars())
    assert "failed 2 times in a row" not in said or "off-machine" in said.lower().split("failed 2 times in a row")[0], said
    assert "folder" in said.lower() and "local" in said.lower(), said


def test_defect_a_failing_off_machine_folder_does_not_eat_the_local_history(make_ctx, tmp_path):
    ctx = make_ctx(backup_offsite_dir=gone_folder(tmp_path), backup_passphrase=PASS)
    for _ in range(3):  # the job's first try and two retries, for one day's slot
        run_job(ctx, "db_maintenance", trigger="test")
        time.sleep(1.05)
    copies = list(ctx.settings.backups_dir.glob("aime-auto-*.db"))
    assert len(copies) == 1, f"{len(copies)} local copies for one day: 14 kept then reach back {14 // len(copies)} days, not 14"


def test_defect_a_held_old_local_copy_does_not_fail_the_whole_backup_job(make_ctx, tmp_path):
    ctx = offsite_ctx(make_ctx, tmp_path)
    backups = ctx.settings.backups_dir
    backups.mkdir(parents=True, exist_ok=True)
    import sqlite3
    for i in range(15):
        con = sqlite3.connect(backups / f"aime-auto-202609{i + 1:02d}-061500.db")
        con.execute("CREATE TABLE t (a)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
        con.close()
    close = held(backups / "aime-auto-20260901-061500.db")
    try:
        result = run_job(ctx, "db_maintenance", trigger="test")
    finally:
        close()
    assert result["status"] == "success", (result.get("error") or "")[:200]
    assert list((tmp_path / "off").glob("*.aimebak")), "the off-machine copy never ran"


# ---------------------------------------------------------------------------------- logs
def test_defect_a_tick_keeps_its_log_lines_and_the_old_logs_while_the_dashboard_holds_the_log(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    main = logs / "aime.log"
    main.write_text("x\n", encoding="utf-8")
    for i in range(1, 6):
        (logs / f"aime.log.{i}").write_text(f"history {i}\n", encoding="utf-8")
    logger = logging.getLogger("aime")
    before = list(logger.handlers)
    dashboard = open(main, "a", encoding="utf-8")  # what the dashboard's own RotatingFileHandler keeps open all day
    with main.open("a", encoding="utf-8") as fh:
        fh.write("old line\n" * 230000)
    raise_exc, logging.raiseExceptions = logging.raiseExceptions, False
    try:
        logging_setup.setup_logging(logs)
        for i in range(6):
            logging.getLogger("aime.scheduler").warning("TICK-%d", i)
        for h in logger.handlers:
            h.flush()
    finally:
        logging.raiseExceptions = raise_exc
        dashboard.close()
        for h in [h for h in logger.handlers if h not in before]:
            h.close()
            logger.removeHandler(h)
        logging_setup._CONFIGURED.discard(str(logs.resolve()))
    every = "".join(p.read_text(encoding="utf-8", errors="replace") for p in logs.glob("aime.log*"))
    assert "TICK-5" in every, "the tick's log lines are lost"
    assert all(f"history {i}" in every for i in range(1, 5)), "the older logs were pushed out, one per lost line"


def test_defect_a_log_file_that_cannot_be_written_does_not_stop_the_work(make_ctx):
    ctx = make_ctx()
    path = ctx.settings.logs_dir / "agent_runs.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.touch()
    close = held(path)  # Excel showing it, or a full log folder
    try:
        result = run_job(ctx, "daily_report", trigger="test")
    finally:
        close()
    assert result["status"] == "success", (result.get("error") or "")[:160]


# ---------------------------------------------------------------------------------- the port
def test_defect_another_web_program_on_the_port_is_not_taken_for_the_dashboard():
    watchdog = load_script("watchdog")

    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"some other program")

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert watchdog.dashboard_problem(port=server.server_address[1]) is not None
    finally:
        server.shutdown()
        server.server_close()


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"aime_{name}", Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_sign_in_page_really_carries_the_mark_the_probes_look_for(make_ctx):
    from fastapi.testclient import TestClient

    from aime.dashboard_probe import PAGE, PAGE_MARK
    from aime.web.app import create_app

    with TestClient(create_app(make_ctx())) as client:
        page = client.get(PAGE)
    assert page.status_code == 200 and PAGE_MARK in page.content


def test_the_dashboard_task_says_who_holds_the_port_in_its_log(make_ctx, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the script moves to the project folder when it loads: undone with this
    task = load_script("dashboard_task")
    ctx = make_ctx()
    monkeypatch.setattr(aime.config.Settings, "load", classmethod(lambda cls, *a, **k: ctx.settings))
    monkeypatch.setattr(task, "still_running", lambda h, p: True)
    monkeypatch.setattr("aime.dashboard_probe.page_owner", lambda h, p: "other")
    assert task.run() == 0
    assert "Another web program" in (ctx.settings.logs_dir / "dashboard.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------- the watchdog after a wake
def old_beat(data_dir, minutes=120, state="idle"):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "aime.db").touch()
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(tzinfo=None).isoformat() + "Z"
    (data_dir / "heartbeat.json").write_text(json.dumps({"at": at, "state": state}), encoding="utf-8")


def test_the_watchdog_gives_ticks_their_turn_right_after_a_wake_and_still_reports_a_real_stop(tmp_path):
    wd = load_script("watchdog")
    old_beat(tmp_path)
    assert wd.check(tmp_path, since_wake=lambda: 30.0) == []  # woke half a minute ago: the Tick task starts now too
    assert wd.check(tmp_path, since_wake=lambda: wd.WAKE_GRACE_SECONDS + 1.0)  # ticks have had their time since
    assert wd.check(tmp_path, since_wake=lambda: None)  # never slept
    old_beat(tmp_path, minutes=50)
    assert wd.check(tmp_path, since_wake=lambda: 3600.0)
    assert wd.check(tmp_path, since_wake=lambda: -5.0)  # nonsense from the clock hides nothing


def test_the_time_since_a_wake_is_read_without_error_on_this_pc():
    wd = load_script("watchdog")
    woke = wd.seconds_since_wake()
    assert woke is None or woke >= 0


# ---------------------------------------------------------------------------------- logs, again
def test_a_log_that_can_rotate_still_rotates_and_keeps_the_history(tmp_path):
    main = tmp_path / "aime.log"
    handler = logging_setup.KeepingRotatingHandler(main, maxBytes=200, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        for i in range(60):
            handler.emit(logging.LogRecord("t", 20, "", 0, f"line {i:03d} " + "x" * 30, None, None))
    finally:
        handler.close()
    assert (tmp_path / "aime.log.1").exists() and (tmp_path / "aime.log.2").exists() and not (tmp_path / "aime.log.3").exists()
    assert "line 059" in main.read_text(encoding="utf-8")


def test_while_held_the_rotation_is_not_tried_at_every_line(tmp_path, monkeypatch):
    main = tmp_path / "aime.log"
    main.write_text("x" * 300, encoding="utf-8")
    tries = []
    monkeypatch.setattr(logging_setup, "rotate", lambda *a: tries.append(a) or False)
    handler = logging_setup.KeepingRotatingHandler(main, maxBytes=200, backupCount=2, encoding="utf-8")
    try:
        for i in range(20):
            handler.emit(logging.LogRecord("t", 20, "", 0, f"line {i}", None, None))
    finally:
        handler.close()
    assert len(tries) == 1
    assert "line 19" in main.read_text(encoding="utf-8")


def test_an_unwritable_run_log_is_said_once_in_words_and_comes_back(tmp_path, monkeypatch, caplog):
    path = tmp_path / "agent_runs.jsonl"
    real_open = Path.open

    def refuse(self, *a, **k):
        if self == path:
            raise PermissionError(13, "denied", str(self))
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", refuse)
    with caplog.at_level(logging.WARNING, logger="aime.logging"):
        for i in range(3):
            logging_setup.append_jsonl(path, {"n": i})
    said = [r.getMessage() for r in caplog.records if r.name == "aime.logging"]
    assert len(said) == 1 and "the work goes on" in said[0]
    monkeypatch.undo()
    logging_setup.append_jsonl(path, {"n": 9})
    assert '"n": 9' in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------- the off-machine folder, again
def job_steps(ctx):
    with ctx.db.session() as s:
        row = s.execute(select(JobRun).where(JobRun.job == "db_maintenance").order_by(JobRun.id.desc())).scalars().first()
        return {st["agent"]: st for st in row.detail["steps"]}, row.error


def test_the_cause_is_said_in_words_everywhere_not_as_a_raw_windows_error(make_ctx, tmp_path, capsys):
    ctx = make_ctx(backup_offsite_dir=gone_folder(tmp_path), backup_passphrase=PASS)
    run_job(ctx, "db_maintenance", trigger="test")
    steps, error = job_steps(ctx)
    check = next(c for c in run_checks(ctx) if c["name"] == "Off-machine encrypted backup")
    for words in (steps["offsite_backup"]["error"], error, check["detail"]):
        assert "WinError" not in words and "Errno" not in words and "cannot be reached" in words, words
    assert not check["ok"] and "local backup is fine" in check["detail"]  # at once, not after 30 hours


def test_a_good_copy_ends_the_trouble_everywhere(make_ctx, tmp_path, monkeypatch):
    ctx = make_ctx(backup_offsite_dir=gone_folder(tmp_path), backup_passphrase=PASS)
    run_job(ctx, "db_maintenance", trigger="test")
    ctx.settings.backup_offsite_dir = str(tmp_path / "off")
    assert run_job(ctx, "db_maintenance", trigger="test")["status"] == "success"
    with ctx.db.session() as s:
        row = next(r for r in health_strip(ctx, s) if r[0] == "Off-machine backup")
    assert row[1] == "ok"
    assert next(c for c in run_checks(ctx) if c["name"] == "Off-machine encrypted backup")["ok"]


def test_when_the_local_backup_fails_too_the_job_is_not_called_fine(make_ctx, tmp_path, monkeypatch):
    ctx = make_ctx(backup_offsite_dir=gone_folder(tmp_path), backup_passphrase=PASS)
    monkeypatch.setattr(workflows, "verify_backup", lambda p: {"ok": False, "integrity": "disk error", "tables": 0, "rows": 0})
    run_job(ctx, "db_maintenance", trigger="test")
    run_job(ctx, "db_maintenance", trigger="test")
    run_job(ctx, "monitor", trigger="test")
    with ctx.db.session() as s:
        said = " ".join(a.message for a in s.execute(select(Alert)).scalars())
    assert "local backup is fine" not in said


def test_a_retry_reuses_the_good_copy_only_for_a_failed_try_of_the_same_day(make_ctx, tmp_path, clock):
    ctx = make_ctx()
    start = clock(datetime(2026, 11, 3, 1, 0))
    run_job(ctx, "db_maintenance", trigger="test")  # all well: a run by hand then writes its own copy
    time.sleep(1.05)
    run_job(ctx, "db_maintenance", trigger="test")
    assert len(list(ctx.settings.backups_dir.glob("aime-auto-*.db"))) == 2
    bad = gone_folder(tmp_path)
    ctx.settings.backup_offsite_dir, ctx.settings.backup_passphrase = bad, PASS
    time.sleep(1.05)
    run_job(ctx, "db_maintenance", trigger="test")
    assert len(list(ctx.settings.backups_dir.glob("aime-auto-*.db"))) == 3
    time.sleep(1.05)
    run_job(ctx, "db_maintenance", trigger="test")  # a retry
    assert len(list(ctx.settings.backups_dir.glob("aime-auto-*.db"))) == 3
    assert job_steps(ctx)[0]["db_maintenance"]["output"]["backup_verified"]
    clock(start + timedelta(days=1))
    time.sleep(1.05)
    run_job(ctx, "db_maintenance", trigger="test")  # the next day, still failing: that day's own copy
    assert len(list(ctx.settings.backups_dir.glob("aime-auto-*.db"))) == 4


def test_a_retry_writes_a_new_copy_when_the_first_tries_copy_has_gone_bad(make_ctx, tmp_path):
    ctx = make_ctx(backup_offsite_dir=gone_folder(tmp_path), backup_passphrase=PASS)
    run_job(ctx, "db_maintenance", trigger="test")
    only = next(ctx.settings.backups_dir.glob("aime-auto-*.db"))
    only.write_bytes(b"damaged")
    time.sleep(1.05)
    run_job(ctx, "db_maintenance", trigger="test")
    good = [p for p in ctx.settings.backups_dir.glob("aime-auto-*.db") if p != only]
    assert len(good) == 1


def test_only_this_tools_own_old_half_written_files_are_cleared(make_ctx, tmp_path):
    ctx = offsite_ctx(make_ctx, tmp_path)
    folder = tmp_path / "off"
    folder.mkdir()
    now = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    stays = [folder / f"aime-offsite-{now}.partial", folder / "family-photos.partial", folder / "notes.txt"]
    for p in stays:
        p.write_bytes(b"x")
    write_offsite(ctx.settings, Path(ctx.db.engine.url.database))
    assert all(p.exists() for p in stays)
    backups = ctx.settings.backups_dir
    backups.mkdir(parents=True, exist_ok=True)
    old = [backups / "partial-1234-aime-auto-20200101-061500.db", backups / "partial-1234-aime-auto-20200101-061500.db-journal"]
    young = backups / f"partial-1234-aime-auto-{now}.db"
    mine = backups / "partial-notes.db"
    for p in (*old, young, mine):
        p.write_bytes(b"x")
    run_job(ctx, "db_maintenance", trigger="test")
    assert not any(p.exists() for p in old) and young.exists() and mine.exists()


def test_a_held_new_half_written_copy_is_reported_and_not_a_second_error(make_ctx, tmp_path, monkeypatch):
    ctx = offsite_ctx(make_ctx, tmp_path)
    real = Path.replace

    def refused(self, target):
        if self.suffix == ".partial":
            raise PermissionError(13, "held", str(self))
        return real(self, target)

    monkeypatch.setattr(Path, "replace", refused)
    real_unlink = Path.unlink

    def no_unlink(self, missing_ok=False):
        if self.suffix == ".partial":
            raise PermissionError(13, "held", str(self))
        return real_unlink(self, missing_ok)

    monkeypatch.setattr(Path, "unlink", no_unlink)
    result = run_job(ctx, "db_maintenance", trigger="test")
    monkeypatch.undo()
    assert result["status"] == "partial"
    assert "another program" in job_steps(ctx)[0]["offsite_backup"]["error"]


def test_prune_goes_on_past_a_copy_it_may_not_remove(tmp_path, monkeypatch):
    for i in range(1, 9):
        (tmp_path / f"aime-offsite-2026100{i}-000000.aimebak").write_bytes(encrypt_bytes(b"x", PASS))
    oldest = tmp_path / "aime-offsite-20261001-000000.aimebak"
    real = Path.unlink

    def refused(self, missing_ok=False):
        if self == oldest:
            raise PermissionError(13, "held", str(self))
        return real(self, missing_ok)

    monkeypatch.setattr(Path, "unlink", refused)
    assert prune_offsite(tmp_path, keep=2, passphrase=PASS) == 5
    assert oldest.exists() and len(list(tmp_path.glob("*.aimebak"))) == 3


def test_a_held_old_local_copy_does_not_stop_the_prune_anywhere(tmp_path, monkeypatch):
    import sqlite3

    from aime.backup import prune_backups

    for i in range(1, 7):
        con = sqlite3.connect(tmp_path / f"aime-auto-2026100{i}-061500.db")
        con.execute("CREATE TABLE t (a)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
        con.close()
    oldest = tmp_path / "aime-auto-20261001-061500.db"
    real = Path.unlink

    def refused(self, missing_ok=False):
        if self == oldest:
            raise PermissionError(13, "held", str(self))
        return real(self, missing_ok)

    monkeypatch.setattr(Path, "unlink", refused)
    assert prune_backups(tmp_path, keep=2) == 3
    assert oldest.exists()


def test_prune_decrypts_when_it_can_and_a_changed_passphrase_keeps_the_last_copy_that_opens(tmp_path):
    good = tmp_path / "aime-offsite-20260901-000000.aimebak"
    good.write_bytes(encrypt_bytes(b"x", PASS))
    for i in range(1, 4):  # whole, but under another passphrase
        (tmp_path / f"aime-offsite-2026100{i}-000000.aimebak").write_bytes(encrypt_bytes(b"x", "another-passphrase-here"))
    assert prune_offsite(tmp_path, keep=3, passphrase=PASS) == 0 and good.exists()
    assert prune_offsite(tmp_path, keep=3, passphrase="another-passphrase-here") == 1 and not good.exists()


def test_a_cloud_only_copy_is_never_opened_or_downloaded(tmp_path, monkeypatch):
    import aime.backup as backup

    good = tmp_path / "aime-offsite-20260901-000000.aimebak"
    good.write_bytes(encrypt_bytes(b"x", PASS))
    new = tmp_path / "aime-offsite-20261001-000000.aimebak"
    new.write_bytes(encrypt_bytes(b"x", PASS))
    monkeypatch.setattr(backup, "_cloud_only", lambda p: True)
    real_open, real_read = Path.open, Path.read_bytes
    opened = []
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: opened.append(self) or real_open(self, *a, **k))
    monkeypatch.setattr(Path, "read_bytes", lambda self: opened.append(self) or real_read(self))
    assert backup.copy_opens(good, PASS) is None and backup.copy_opens(good) is None
    assert backup.prune_offsite(tmp_path, keep=1, passphrase=PASS) == 0  # nothing can be proven: it stays
    assert not opened


def test_a_log_held_when_a_process_starts_does_not_stop_it(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    main = logs / "aime.log"
    main.write_text("old\n", encoding="utf-8")
    close = held(main)
    logger = logging.getLogger("aime")
    before = list(logger.handlers)
    raise_exc, logging.raiseExceptions = logging.raiseExceptions, False
    try:
        logging_setup.setup_logging(logs)  # used to raise here, ending the tick before any work
        logging.getLogger("aime.scheduler").warning("a line with nowhere to go")
    finally:
        logging.raiseExceptions = raise_exc
        close()
        for h in [h for h in logger.handlers if h not in before]:
            h.close()
            logger.removeHandler(h)
        logging_setup._CONFIGURED.discard(str(logs.resolve()))


def test_doctor_says_when_the_newest_copy_is_cut_short_or_no_longer_opens(make_ctx, tmp_path):
    ctx = offsite_ctx(make_ctx, tmp_path)
    run_job(ctx, "db_maintenance", trigger="test")
    copy = next((tmp_path / "off").glob("*.aimebak"))
    whole = copy.read_bytes()
    copy.write_bytes(whole[:len(whole) // 2])
    check = next(c for c in run_checks(ctx) if c["name"] == "Off-machine encrypted backup")
    assert not check["ok"] and "cut short" in check["detail"]
    copy.write_bytes(whole)
    assert next(c for c in run_checks(ctx) if c["name"] == "Off-machine encrypted backup")["ok"]
    ctx.settings.backup_passphrase = "a-new-passphrase-typed-wrong"
    check = next(c for c in run_checks(ctx) if c["name"] == "Off-machine encrypted backup")
    assert not check["ok"] and "passphrase" in check["detail"]


def test_after_a_changed_passphrase_doctor_keeps_saying_so_while_older_copies_are_there(make_ctx, tmp_path):
    ctx = offsite_ctx(make_ctx, tmp_path)
    run_job(ctx, "db_maintenance", trigger="test")
    time.sleep(1.05)
    ctx.settings.backup_passphrase = "a-new-passphrase-typed-wrong"
    assert run_job(ctx, "db_maintenance", trigger="test")["status"] == "success"
    checks = {c["name"]: c for c in run_checks(ctx)}
    assert checks["Off-machine encrypted backup"]["ok"]
    assert not checks["Older off-machine copies open"]["ok"] and "passphrase" in checks["Older off-machine copies open"]["detail"]
    for p in sorted((tmp_path / "off").glob("*.aimebak"))[:-1]:
        p.unlink()  # aged out
    assert "Older off-machine copies open" not in {c["name"] for c in run_checks(ctx)}


def test_the_weekly_drill_also_restores_the_newest_off_machine_copy(make_ctx, tmp_path):
    ctx = offsite_ctx(make_ctx, tmp_path)
    run_job(ctx, "db_maintenance", trigger="test")
    steps = job_steps(ctx)[0]
    assert steps["offsite_drill"]["status"] == "success" and steps["offsite_drill"]["output"]["ok"]
    time.sleep(1.05)
    run_job(ctx, "db_maintenance", trigger="test")
    assert "offsite_drill" not in job_steps(ctx)[0]  # once a week


def test_the_weekly_drill_does_not_download_a_cloud_only_copy(make_ctx, tmp_path, monkeypatch):
    import aime.backup as backup

    ctx = offsite_ctx(make_ctx, tmp_path)
    (tmp_path / "off").mkdir()
    (tmp_path / "off" / "aime-offsite-20261001-000000.aimebak").write_bytes(b"not here")
    monkeypatch.setattr(backup, "_cloud_only", lambda p: True)
    assert backup.weekly_offsite_drill(ctx) is None


def test_a_bad_folder_by_hand_prints_words_and_no_traceback(make_ctx, tmp_path, capsys):
    from argparse import Namespace

    from aime.cli import cmd_backup

    ctx = make_ctx(backup_offsite_dir=gone_folder(tmp_path), backup_passphrase=PASS)
    assert cmd_backup(Namespace(label="manual", offsite=True), ctx) == 1
    said = capsys.readouterr().out
    assert "not written" in said and "cannot be reached" in said and "WinError" not in said


def test_the_words_for_a_folder_beside_the_onedrive_root_and_a_missing_drive(tmp_path):
    from aime.backup import offsite_sync

    root = tmp_path / "OneDrive"
    leaves, why = offsite_sync(tmp_path / "OneDrive-Backups" / "AIME", synced=[root])
    assert leaves is False and "not signed in" not in why and "outside" in why and str(root) in why
    if sys.platform == "win32":
        leaves, why = offsite_sync(r"Z:\OneDrive\AIME", synced=[])
        assert leaves is False and "not signed in" not in why and "not connected" in why
        leaves, why = offsite_sync(r"Z:\Backups\AIME", synced=[])
        assert leaves is False and "not connected" in why
