"""Phase 5, running it on the PC: what the owner is told, and where. A full disk named in the one notification, the network
named as the cause, desktop notices from the process that can show them, and lockouts in the audit log. Synthetic data only.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import threading
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

import aime
from aime import desk_notices, notify
from aime.cli import cmd_serve
from aime.models import AgentRun, Alert, AuditEvent, Company, InboundEmail, JobRun, JobState, Lead, User
from aime.monitoring import check_health
from aime.reply_intel import process_pending
from aime.web.app import create_app
from aime.web.auth import MAX_FAILED, SIGN_IN_FAILED, authenticate, hash_password

ROOT = Path(aime.__file__).parent.parent


def test_on_a_full_disk_the_one_notification_names_the_disk(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("wd", ROOT / "scripts" / "watchdog.py")
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    monkeypatch.setattr(wd, "DATA", tmp_path)
    monkeypatch.setattr(wd, "check", lambda d: ["Scheduled ticks stopped: last tick 74 min ago. Check Task Scheduler"])
    monkeypatch.setattr(wd, "dashboard_problem", lambda **k: None)
    monkeypatch.setattr(wd, "free_space_problem", lambda d: "Only 0.0 GB is free on the disk")
    shown = []
    monkeypatch.setattr("aime.notify.toast", lambda title, message: shown.append(message))
    wd.main()
    assert "free on the disk" in shown[0]


# ---------------------------------------------------------------- the network named as the cause
def failing_job_alert(ctx, *runs: tuple[str, str | None]) -> str:
    """The job's runs, oldest first, as (status, error)."""
    with ctx.db.session() as s:
        s.add(JobState(job="content", consecutive_failures=2, enabled=True))
        for status, error in runs:
            s.add(JobRun(job="content", trigger="scheduler", status=status, error=error))
            s.flush()
    with ctx.db.session() as s:
        check_health(s, ctx.settings)
    with ctx.db.session() as s:
        return s.execute(select(Alert.message).where(Alert.key == "job_failing:content")).scalar_one()


def test_a_job_failing_for_want_of_a_network_says_so(ctx):
    said = failing_job_alert(ctx, ("failed", "inbox: [Errno 11001] getaddrinfo failed for imap.example with password hunter2"))
    assert said == "Job 'content' failed 2 times in a row: no internet or DNS at the last try"


def test_a_part_failed_run_counts_as_the_last_try(ctx):
    said = failing_job_alert(ctx, ("failed", "ValueError: no such template"), ("partial", "content: socket.gaierror: name resolution failed"))
    assert said.endswith("no internet or DNS at the last try")


def test_a_job_failing_for_any_other_reason_keeps_its_words(ctx):
    said = failing_job_alert(ctx, ("failed", "ValueError: no such template"))
    assert said == "Job 'content' failed 2 times in a row"


def test_a_job_with_no_error_recorded_keeps_its_words(ctx):
    assert failing_job_alert(ctx, ("failed", None)) == "Job 'content' failed 2 times in a row"


def test_an_old_network_failure_is_not_blamed_for_a_newer_one_with_another_cause(ctx):
    """A job-level exception is on its run only: last week's DNS failure stays the newest failed agent run."""
    with ctx.db.session() as s:
        s.add(AgentRun(run_id="r1", agent="content", job="content", status="failed", error="socket.gaierror: getaddrinfo failed"))
    disk = "sqlite3.OperationalError: disk I/O error"
    said = failing_job_alert(ctx, ("failed", "socket.gaierror: getaddrinfo failed"), ("failed", disk), ("failed", disk))
    assert said == "Job 'content' failed 2 times in a row"


def test_an_old_network_failure_is_not_blamed_for_a_newer_part_failed_or_abandoned_run(make_ctx):
    network = ("failed", "socket.gaierror: getaddrinfo failed")
    assert "no internet" not in failing_job_alert(make_ctx(), network, ("partial", "content: ValueError: no such template"))
    assert "no internet" not in failing_job_alert(make_ctx(), network, ("abandoned", "Process ended before the job finished"))


def test_a_run_still_going_is_not_the_last_try(ctx):
    network = ("failed", "socket.gaierror: getaddrinfo failed")
    assert failing_job_alert(ctx, network, ("running", None)).endswith("no internet or DNS at the last try")


# ---------------------------------------------------------------- desktop notices
def fake_toasts(monkeypatch):
    shown = []
    monkeypatch.setattr(desk_notices, "notify_alert", lambda settings, level, words: shown.append((level, words)))
    monkeypatch.setattr(desk_notices, "notify_event", lambda settings, words: shown.append(("sales", words)))
    return shown


def add_alert(ctx, level="error", **fields):
    with ctx.db.session() as s:
        s.add(Alert(level=level, source="monitor", message=fields.pop("message", f"a {level} alert"), **fields))


def add_reply(ctx, category="interested", company=None, **fields):
    with ctx.db.session() as s:
        lead_id = None
        if company:
            c = Company(name=company, normalized_name=company.lower(), source="test")
            s.add(c)
            s.flush()
            lead = Lead(company_id=c.id, source="test")
            s.add(lead)
            s.flush()
            lead_id = lead.id
        n = s.query(InboundEmail).count()
        s.add(InboundEmail(message_id=f"<{n}@mail.example>", from_email=f"person{n}@mail.example", subject="Re: hello",
                           category=category, lead_id=lead_id, status="actioned", **fields))


def store_unread_reply(ctx, words="Yes, I am interested, let's talk next week."):
    """What the inbox job's first step leaves: stored, not yet read."""
    with ctx.db.session() as s:
        n = s.query(InboundEmail).count()
        s.add(InboundEmail(message_id=f"<{n}@mail.example>", from_email=f"person{n}@mail.example", subject="Re: hello",
                           status="new", body_text=words, new_text=words))


def read_the_stored_replies(ctx):
    with ctx.db.session() as s:
        process_pending(s, ctx.settings, "test")


def test_the_first_start_shows_nothing_old(ctx, monkeypatch):
    shown = fake_toasts(monkeypatch)
    add_alert(ctx)
    add_reply(ctx)
    assert desk_notices.poll_once(ctx) == 0 and desk_notices.poll_once(ctx) == 0
    assert shown == []


def test_a_person_at_the_desk_is_told_of_an_interested_reply_the_tick_could_not_show(ctx, monkeypatch):
    """The tick's own call shows nothing from the background session; the dashboard's next look does."""
    shown = fake_toasts(monkeypatch)
    desk_notices.poll_once(ctx)
    add_reply(ctx, company="Acme Software")
    assert desk_notices.poll_once(ctx) == 1
    assert shown == [("sales", "Interested reply from person0@mail.example (Acme Software)")]


def test_a_reply_read_after_a_look_still_gets_its_notice_once(ctx, monkeypatch):
    shown = fake_toasts(monkeypatch)
    desk_notices.poll_once(ctx)
    store_unread_reply(ctx)
    assert desk_notices.poll_once(ctx) == 0  # the dashboard's minute falls between storing and reading
    read_the_stored_replies(ctx)
    assert desk_notices.poll_once(ctx) == 1 and desk_notices.poll_once(ctx) == 0
    assert shown == [("sales", "Interested reply from person0@mail.example")]


def test_a_reply_stored_before_the_first_start_and_read_after_it_gets_its_notice(ctx, monkeypatch):
    shown = fake_toasts(monkeypatch)
    store_unread_reply(ctx)
    assert desk_notices.poll_once(ctx) == 0
    read_the_stored_replies(ctx)
    assert desk_notices.poll_once(ctx) == 1 and len(shown) == 1


def test_an_unread_reply_holds_back_later_replies_but_loses_and_repeats_none_and_no_alert_waits(ctx, monkeypatch):
    shown = fake_toasts(monkeypatch)
    desk_notices.poll_once(ctx)
    store_unread_reply(ctx)
    add_reply(ctx)
    add_alert(ctx)
    desk_notices.poll_once(ctx)
    assert shown == [("error", "a error alert")]
    read_the_stored_replies(ctx)
    desk_notices.poll_once(ctx)
    desk_notices.poll_once(ctx)
    assert sorted(w for _, w in shown[1:]) == ["Interested reply from person0@mail.example", "Interested reply from person1@mail.example"]


def test_new_warnings_and_errors_are_shown_once_and_a_restart_repeats_none(ctx, monkeypatch):
    shown = fake_toasts(monkeypatch)
    desk_notices.poll_once(ctx)
    add_alert(ctx, "warning")
    add_alert(ctx, "error")
    desk_notices.poll_once(ctx)
    assert shown == [("error", "a error alert"), ("warning", "a warning alert")]
    desk_notices.poll_once(ctx)  # the same as a new process: what was seen is in the database, not in this module
    assert len(shown) == 2


def test_what_is_not_news_is_not_shown(ctx, monkeypatch):
    shown = fake_toasts(monkeypatch)
    desk_notices.poll_once(ctx)
    add_alert(ctx, "info")
    add_alert(ctx, "error", acknowledged=True)
    add_alert(ctx, "warning", resolved_at=aime.timeutil.utcnow())
    add_reply(ctx, category="not_interested")
    add_reply(ctx, is_simulated=True)
    desk_notices.poll_once(ctx)
    assert shown == []


def test_a_long_gap_is_a_few_notices_and_a_count_with_the_reply_first(ctx, monkeypatch):
    shown = fake_toasts(monkeypatch)
    desk_notices.poll_once(ctx)
    for i in range(5):
        add_alert(ctx, "warning", message=f"w{i}")
    add_alert(ctx, "error", message="e")
    add_reply(ctx)
    desk_notices.poll_once(ctx)
    assert [w for _, w in shown] == ["Interested reply from person0@mail.example", "e", "w0", "4 more on the dashboard"]


def test_looking_changes_no_alert(ctx, monkeypatch):
    fake_toasts(monkeypatch)
    desk_notices.poll_once(ctx)
    add_alert(ctx)
    desk_notices.poll_once(ctx)
    with ctx.db.session() as s:
        a = s.execute(select(Alert)).scalars().one()
        assert not a.acknowledged and a.resolved_at is None and a.message == "a error alert"


def test_a_failed_look_is_logged_and_the_loop_goes_on(ctx, monkeypatch, caplog):
    looks = []
    stop = threading.Event()

    def fails_then_stops(c):
        looks.append(1)
        if len(looks) == 2:
            stop.set()
        raise RuntimeError("database is locked")

    monkeypatch.setattr(desk_notices, "poll_once", fails_then_stops)
    with caplog.at_level(logging.ERROR, logger="aime.desk_notices"):
        desk_notices._loop(ctx, stop, seconds=0)
    assert len(looks) == 2 and "this look failed" in caplog.text


def test_a_toast_that_fails_is_not_shown_again_every_minute(ctx, monkeypatch):
    calls = []

    def fails(settings, level, words):
        calls.append(words)
        raise RuntimeError("no toast")

    monkeypatch.setattr(desk_notices, "notify_alert", fails)
    desk_notices.poll_once(ctx)
    add_alert(ctx)
    try:
        desk_notices.poll_once(ctx)
    except RuntimeError:
        pass
    desk_notices.poll_once(ctx)
    assert len(calls) == 1


def threads_named(name):
    return [t for t in threading.enumerate() if t.name == name]


def test_it_is_off_where_a_toast_cannot_be_shown(make_ctx, monkeypatch):
    ctx = make_ctx(data_role="production")
    silent, testing = make_ctx(data_role="production", alert_toast=False), make_ctx(data_role="test", alert_toast=True)
    monkeypatch.setattr(notify, "has_desktop", lambda: False)  # the background session
    assert not notify.can_show(ctx.settings) and desk_notices.start(ctx) is None
    monkeypatch.setattr(notify, "has_desktop", lambda: True)
    with monkeypatch.context() as on_windows:  # settings already built: no path is made while this says Windows
        on_windows.setattr(notify.os, "name", "nt")
        assert notify.can_show(ctx.settings)
        assert not notify.can_show(silent.settings) and not notify.can_show(testing.settings)


def test_it_starts_only_for_the_real_data(make_ctx, monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, **k):
            self.daemon = k["daemon"]

        def start(self):
            started.append(self.daemon)

    monkeypatch.setattr(desk_notices.threading, "Thread", FakeThread)
    monkeypatch.setattr(desk_notices, "can_show", lambda settings: True)
    assert desk_notices.start(make_ctx(data_role="demo")) is None and started == []
    assert desk_notices.start(make_ctx(data_role="production")) is not None and started == [True]


def test_the_dashboard_start_starts_it_and_the_web_app_alone_does_not(ctx, monkeypatch):
    before = len(threads_named("desk-notices"))
    with TestClient(create_app(ctx)) as client:
        client.get("/")
        assert len(threads_named("desk-notices")) == before
    started = []
    monkeypatch.setattr("aime.desk_notices.start", started.append)
    monkeypatch.setattr("aime.dashboard_probe.start_self_check", lambda *a: None)
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    assert cmd_serve(argparse.Namespace(host=None, port=None), ctx) == 0
    assert started == [ctx]


# ---------------------------------------------------------------- lockouts
def add_user(ctx):
    with ctx.db.session() as s:
        s.add(User(username="sam", password_hash=hash_password("right-horse-battery"), role="admin", active=True))


def test_a_lockout_is_in_the_audit_log_and_the_log_with_no_password(ctx, caplog):
    add_user(ctx)
    wrong = "wrong-secret-words"
    with caplog.at_level(logging.WARNING, logger="aime.web.auth"):
        for _ in range(MAX_FAILED - 1):
            with ctx.db.session() as s:
                assert authenticate(s, "sam", wrong) == (None, SIGN_IN_FAILED)
        with ctx.db.session() as s:
            assert s.execute(select(AuditEvent)).first() is None
            assert authenticate(s, "sam", wrong) == (None, SIGN_IN_FAILED)  # the same words at the lock
        with ctx.db.session() as s:
            assert authenticate(s, "sam", wrong) == (None, SIGN_IN_FAILED)  # already locked: no second row
    with ctx.db.session() as s:
        event = s.execute(select(AuditEvent)).scalars().one()
        assert (event.action, event.target) == ("sign_in_locked", "sam")
        assert wrong not in f"{event.actor} {event.detail}"
    assert "sam" in caplog.text and wrong not in caplog.text


def test_a_name_that_is_no_account_writes_nothing(ctx, caplog):
    """Someone can type a password into the name box: only a real account's own name is ever kept."""
    add_user(ctx)
    with caplog.at_level(logging.WARNING, logger="aime.web.auth"):
        for _ in range(MAX_FAILED + 1):
            with ctx.db.session() as s:
                authenticate(s, "typed-password-here", "x")
    with ctx.db.session() as s:
        assert s.execute(select(AuditEvent)).first() is None
    assert "typed-password-here" not in caplog.text
