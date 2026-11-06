"""Deliverability safeguards (aime.deliverability): the bounce and complaint auto-pause, the SPF/DKIM/DMARC gate, per-mailbox
accounting and caps, the per-hour cap, send spacing, warm-up and the RFC 8058 one-click header. Fake SMTP and IMAP servers
and fake DNS answers only: nothing is sent, no DNS is looked up and no limit is raised."""

import smtplib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

import aime.timeutil
import aime.workflows as workflows
from aime import deliverability as dv
from aime.agents.inbox_agent import InboxAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.cli import cmd_user
from aime.doctor import run_checks
from aime.inbox import Mailbox
from aime.integrations.email_smtp import EmailSender
from aime.migrations import migrate
from aime.models import Alert, AuditEvent, FollowUp, InboundEmail, MailAccount
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.test_dashboard_auth import GOOD, Args, _login
from tests.test_inbound_sales import FakeSMTP
from tests.test_inbox import FakeIMAP, mail
from tests.test_time_and_alerts import FOOTER, IN_WINDOW, MAIL, add_lead, add_mail, status_of

ORIGIN = {"origin": "http://testserver"}
PASSING = {("us.example", "MX"): ["10 mx.us.example."], ("us.example", "TXT"): ["v=spf1 ip4:192.0.2.10 ~all"],
           ("smtp.example", "A"): ["192.0.2.10"], ("google._domainkey.us.example", "TXT"): ["v=DKIM1; k=rsa; p=MIGf"],
           ("_dmarc.us.example", "TXT"): ["v=DMARC1; p=quarantine"]}
NO_DMARC = {k: v for k, v in PASSING.items() if k[0] != "_dmarc.us.example"}


def resolver(records, calls=None):
    def resolve(name, rtype):
        if calls is not None:
            calls.append((name, rtype))
        return list(records.get((name.rstrip("."), rtype), []))
    return resolve


def dns_down(name, rtype):
    raise TimeoutError(f"DNS lookup of {name} timed out")


class StormSMTP(FakeSMTP):
    """Refuses every recipient in `bad` at RCPT TO, as a server does for an address that does not exist."""
    bad: set = set()
    attempts: list = []

    def send_message(self, msg):
        StormSMTP.attempts.append(msg["To"])
        if msg["To"] in StormSMTP.bad:
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"5.1.1 The email account that you tried to reach does not exist")})
        FakeSMTP.sent.append(msg)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET)

    def set_to(when: datetime) -> datetime:
        aime.timeutil.CLOCK_OFFSET = when - datetime.now(timezone.utc).replace(tzinfo=None)
        return when
    return set_to


@pytest.fixture
def world(make_ctx, monkeypatch, clock):
    """The followups job's world on a Wednesday morning in the send window: fake mailbox, fake mail server."""
    ctx = make_ctx(**MAIL)
    clock(IN_WINDOW)
    FakeIMAP.messages, FakeSMTP.sent, StormSMTP.attempts, StormSMTP.bad = {}, [], [], set()
    monkeypatch.setattr(workflows, "InboxAgent", lambda c: InboxAgent(c, mailbox=Mailbox(c.settings, imap_factory=FakeIMAP)))
    monkeypatch.setattr(workflows, "SalesWorkflowAgent", lambda c: SalesWorkflowAgent(c, sender=EmailSender(c.settings, smtp_factory=StormSMTP)))
    monkeypatch.setattr(dv, "RESOLVER", None)
    return ctx


def followups_run(ctx):
    workflows.job_inbox(ctx)  # read first, send second
    return workflows.job_followups(ctx)


def history(ctx, sent, bounced=0, ago=timedelta(days=2), account=None):
    """Emails that went out earlier from the mailbox; the first `bounced` of them came back as bounces."""
    lead_id = add_lead(ctx, status="disqualified")
    with ctx.db.session() as s:
        for i in range(sent):
            s.add(FollowUp(lead_id=lead_id, channel="email", kind="response", status="sent", subject="Earlier", body=FOOTER,
                           to_email=f"p{i}@old{i}.example", sent_at=utcnow() - ago, mail_account_id=account,
                           delivery_status="bounced" if i < bounced else "sent"))
    return lead_id


def complaints(ctx, n):
    with ctx.db.session() as s:
        for i in range(n):
            s.add(InboundEmail(from_email=f"angry{i}@old{i}.example", subject="Re: hello", category="unsubscribe",
                               confidence="medium", reasons=['complaint: "spam"', "unsubscribe"], status="actioned",
                               received_at=utcnow() - timedelta(days=1), processed_at=utcnow() - timedelta(days=1)))


def approved(ctx, n):
    return [add_mail(ctx, add_lead(ctx, stage="approved")) for _ in range(n)]


def configure(ctx, **values):
    ctx.settings.business["deliverability"] = {**ctx.settings.business.get("deliverability", {}), **values}


def audits(ctx, action):
    with ctx.db.session() as s:
        return s.execute(select(AuditEvent).where(AuditEvent.action == action)).scalars().all()


def open_alert(ctx, key):
    with ctx.db.session() as s:
        return s.execute(select(Alert).where(Alert.key == key, Alert.resolved_at.is_(None))).scalars().first()


# --------------------------------------------------------------------------- the bounce and complaint auto-pause
def test_a_bounce_storm_pauses_sending_within_one_followups_run_and_nothing_goes_out_after(world):
    ctx = world
    history(ctx, 30)  # a clean week
    fids = approved(ctx, 5)
    with ctx.db.session() as s:
        StormSMTP.bad = {f.to_email for f in s.execute(select(FollowUp).where(FollowUp.id.in_(fids))).scalars()}
    followups_run(ctx)
    # three refusals (3 of 33 attempted, 9%) cross the line; the other two are never handed to the mail server
    assert len(StormSMTP.attempts) == 3 and FakeSMTP.sent == []
    states = [status_of(ctx, fid) for fid in fids]
    assert sum(st == "cancelled" for st, _ in states) == 3
    held = [why for st, why in states if st == "approved"]
    assert len(held) == 2 and all(why.startswith("Held: outbound email is paused for deliverability") for why in held)
    [event] = audits(ctx, "deliverability_paused")
    assert event.actor == "deliverability" and "bounced" in event.detail
    assert open_alert(ctx, dv.PAUSE_ALERT).level == "error"
    StormSMTP.bad = set()
    followups_run(ctx)  # still paused: the next run sends nothing either, and reading went on
    assert FakeSMTP.sent == [] and len(StormSMTP.attempts) == 3
    assert [status_of(ctx, fid)[0] for fid in fids].count("approved") == 2
    with ctx.db.session() as s:
        assert dv.pause_state(s)["scope"] == "all mailboxes"


def test_bounces_read_from_the_mailbox_pause_the_run_before_any_email(world):
    ctx = world
    history(ctx, 40, bounced=4)  # 10% of last week's emails came back
    fids = approved(ctx, 2)
    followups_run(ctx)
    assert StormSMTP.attempts == [] and all(status_of(ctx, f)[1].startswith("Held: outbound email is paused") for f in fids)
    assert all(status_of(ctx, f)[0] == "approved" for f in fids)


def test_complaints_pause_sending(world):
    ctx = world
    history(ctx, 30)
    complaints(ctx, 2)
    [fid] = approved(ctx, 1)
    followups_run(ctx)
    status, why = status_of(ctx, fid)
    assert StormSMTP.attempts == [] and status == "approved" and "complaint" in why
    with ctx.db.session() as s:
        assert "complaint" in dv.pause_state(s)["reason"]


def test_complaints_read_from_the_mailbox_pause_sending(world):
    ctx = world
    lead_id = history(ctx, 30)
    with ctx.db.session() as s:
        earlier = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id).order_by(FollowUp.id).limit(2)).scalars().all()
        for i, f in enumerate(earlier):
            f.message_id = f"<earlier{i}@us.example>"
        replies = [(f.to_email, f.message_id) for f in earlier]
    FakeIMAP.messages = {n + 1: mail(f"Pat <{to}>", "Re: Earlier", "This is spam. I am reporting you. Stop emailing me.", in_reply_to=mid)
                         for n, (to, mid) in enumerate(replies)}
    [fid] = approved(ctx, 1)
    followups_run(ctx)
    status, why = status_of(ctx, fid)
    assert StormSMTP.attempts == [] and status == "approved" and "complaint" in why


def test_one_complaint_alone_does_not_pause(world):
    ctx = world
    history(ctx, 30)
    complaints(ctx, 1)
    [fid] = approved(ctx, 1)
    followups_run(ctx)
    assert status_of(ctx, fid)[0] == "sent"


def test_a_clean_history_never_pauses(world):
    ctx = world
    history(ctx, 200)
    history(ctx, 40, ago=timedelta(hours=30))
    fids = approved(ctx, 3)
    followups_run(ctx)
    assert [status_of(ctx, f)[0] for f in fids] == ["sent"] * 3
    with ctx.db.session() as s:
        assert dv.pause_state(s) is None and dv.evaluate(s, ctx.settings) is None
    assert audits(ctx, "deliverability_paused") == [] and open_alert(ctx, dv.PAUSE_ALERT) is None


@pytest.mark.parametrize("sent, bounced, paused", [
    (5, 1, False),  # one bounce in five emails: no pattern
    (5, 3, False),  # under the minimum sample of 20 emails
    (19, 3, False),
    (40, 2, False),  # 5% but fewer than 3 bounces
    (60, 3, True),  # 3 of 60: exactly 5%
    (61, 3, False),  # 4.9%
    (20, 3, True),
])
def test_the_minimum_sample_keeps_a_few_bounces_from_pausing(world, sent, bounced, paused):
    ctx = world
    history(ctx, sent, bounced=bounced)
    with ctx.db.session() as s:
        assert (dv.evaluate(s, ctx.settings) is not None) is paused


def test_old_bounces_outside_the_window_do_not_count(world):
    ctx = world
    history(ctx, 40, bounced=10, ago=timedelta(days=8))
    with ctx.db.session() as s:
        assert dv.evaluate(s, ctx.settings) is None


def test_settings_cannot_shrink_the_minimum_sample_below_its_floor(world):
    ctx = world
    configure(ctx, min_sends=1, min_bounces=1)
    history(ctx, 5, bounced=1)
    with ctx.db.session() as s:
        cfg = dv.config(ctx.settings)
        assert cfg["min_sends"] == dv.MIN_SAMPLE_FLOOR and dv.evaluate(s, ctx.settings) is None


def test_auto_pause_can_be_switched_off(world):
    ctx = world
    configure(ctx, auto_pause=False)
    history(ctx, 40, bounced=10)
    with ctx.db.session() as s:
        assert dv.evaluate(s, ctx.settings) is None


def test_a_bounce_pause_is_per_mailbox_too(world):
    ctx = world
    history(ctx, 200)  # the .env mailbox: clean
    history(ctx, 20, bounced=3, account=7)  # mailbox 7: 15%; all together 3 of 220 (1.4%)
    with ctx.db.session() as s:
        p = dv.evaluate(s, ctx.settings)
        assert p is not None and "mailbox #7" in p["scope"]


# --------------------------------------------------------------------------- clearing the pause
def test_an_admin_clears_the_pause_and_it_is_audited_and_counted_afresh(world):
    ctx = world
    history(ctx, 40, bounced=10)
    with ctx.db.session() as s:
        assert dv.evaluate(s, ctx.settings) is not None
        with pytest.raises(PermissionError):
            dv.clear_pause(s, "manager", {"approve_outbound", "edit_records"})
        assert dv.pause_state(s) is not None
        dv.clear_pause(s, "owner", {"admin_ops"}, "bad list removed")
        assert dv.pause_state(s) is None
        assert dv.evaluate(s, ctx.settings) is None  # the rates count from the clear: no pause again at once
        with pytest.raises(ValueError):
            dv.clear_pause(s, "owner", {"admin_ops"})
    [refused] = audits(ctx, "deliverability_clear_refused")
    [cleared] = audits(ctx, "deliverability_cleared")
    assert refused.actor == "manager" and cleared.actor == "owner" and "bad list removed" in cleared.detail
    assert open_alert(ctx, dv.PAUSE_ALERT) is None


@pytest.fixture
def roles(make_ctx):
    ctx = make_ctx(dashboard_auth=True, **MAIL)
    for name, role in (("manager", "sales_manager"), ("owner", "admin"), ("exec", "sales_executive")):
        assert cmd_user(Args("create", name, role=role), ctx, prompt_fn=lambda _p: GOOD) == 0
    with ctx.db.session() as s:
        dv.pause(s, ctx.settings, "all mailboxes: 10 of 40 emails bounced", "all mailboxes", {"sends": 40, "bounces": 10})
    return ctx


def test_only_an_admin_clears_the_pause_on_the_page(roles):
    manager = TestClient(create_app(roles))
    _login(manager, username="manager")
    page = manager.get("/deliverability")
    assert page.status_code == 200 and "10 of 40 emails bounced" in page.text and "Clear the pause" not in page.text
    r = manager.post("/deliverability/clear", data={"note": "x"}, headers=ORIGIN, follow_redirects=False)
    assert "Only+an+administrator" in r.headers["location"] or "Only%20an%20administrator" in r.headers["location"]
    with roles.db.session() as s:
        assert dv.pause_state(s) is not None
    assert [e.actor for e in audits(roles, "deliverability_clear_refused")] == ["manager"]

    executive = TestClient(create_app(roles))
    _login(executive, username="exec")
    assert executive.get("/deliverability").status_code == 403

    owner = TestClient(create_app(roles))
    _login(owner, username="owner")
    assert "Clear the pause" in owner.get("/deliverability").text
    r = owner.post("/deliverability/clear", data={"note": "checked the list"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    with roles.db.session() as s:
        assert dv.pause_state(s) is None
    [cleared] = audits(roles, "deliverability_cleared")
    assert cleared.actor == "owner" and "checked the list" in cleared.detail
    assert "Not paused" in owner.get("/deliverability").text


def test_the_page_has_no_script(make_ctx):
    ctx = make_ctx(**MAIL)
    page = TestClient(create_app(ctx)).get("/deliverability")
    assert page.status_code == 200 and "<script" not in page.text.lower() and "Sending domain authentication" in page.text


# --------------------------------------------------------------------------- the authentication gate
def test_the_gate_reads_a_kept_check_and_the_monitor_refreshes_it_once_a_day(world, monkeypatch):
    ctx = world
    calls = []
    monkeypatch.setattr(dv, "RESOLVER", resolver(PASSING, calls))
    with ctx.db.session() as s:
        dv.monitor(s, ctx.settings)
        first = len(calls)
        assert first and dv.auth_state(s)["failing"] == [] and dv.auth_state(s)["domain"] == "us.example"
        dv.monitor(s, ctx.settings)  # within the day: no look-up again
        assert len(calls) == first
        dv.monitor(s, ctx.settings, now=utcnow() + timedelta(hours=25))
        assert len(calls) > first
    [fid] = approved(ctx, 1)
    calls.clear()
    followups_run(ctx)  # sending never looks anything up
    assert status_of(ctx, fid)[0] == "sent" and calls == []


def test_gate_off_looks_nothing_up_and_holds_nothing(world, monkeypatch):
    ctx = world
    configure(ctx, auth_gate="off")
    calls = []
    monkeypatch.setattr(dv, "RESOLVER", resolver({}, calls))
    with ctx.db.session() as s:
        dv.monitor(s, ctx.settings)
    [fid] = approved(ctx, 1)
    followups_run(ctx)
    assert calls == [] and status_of(ctx, fid)[0] == "sent"


@pytest.mark.parametrize("dns", ["failing", "down"])
def test_warn_mode_alerts_and_still_sends(world, monkeypatch, dns):
    ctx = world
    monkeypatch.setattr(dv, "RESOLVER", resolver(NO_DMARC) if dns == "failing" else dns_down)
    with ctx.db.session() as s:
        dv.monitor(s, ctx.settings)
    alert = open_alert(ctx, dv.AUTH_ALERT)
    assert alert.level == "warning" and ("DMARC" in alert.message if dns == "failing" else "could not run" in alert.message)
    [fid] = approved(ctx, 1)
    followups_run(ctx)
    assert status_of(ctx, fid)[0] == "sent"  # fail-open in warn mode


@pytest.mark.parametrize("dns", ["failing", "down", "never", "stale"])
def test_block_mode_holds_sending_while_the_domain_does_not_pass(world, monkeypatch, dns):
    ctx = world
    configure(ctx, auth_gate="block")
    if dns != "never":
        monkeypatch.setattr(dv, "RESOLVER", {"failing": resolver(NO_DMARC), "down": dns_down, "stale": resolver(PASSING)}[dns])
        with ctx.db.session() as s:
            dv.refresh_auth(s, ctx.settings, now=utcnow() - timedelta(hours=49) if dns == "stale" else None)
    [fid] = approved(ctx, 1)
    followups_run(ctx)
    status, why = status_of(ctx, fid)
    assert status == "approved" and why.startswith("Held: the sending domain's SPF/DKIM/DMARC check") and StormSMTP.attempts == []
    if dns in ("failing", "down"):
        assert open_alert(ctx, dv.AUTH_ALERT).level == "error"


def test_block_mode_sends_once_the_domain_passes(world, monkeypatch):
    ctx = world
    configure(ctx, auth_gate="block")
    monkeypatch.setattr(dv, "RESOLVER", resolver(PASSING))
    with ctx.db.session() as s:
        dv.refresh_auth(s, ctx.settings)
    [fid] = approved(ctx, 1)
    followups_run(ctx)
    assert status_of(ctx, fid)[0] == "sent" and open_alert(ctx, dv.AUTH_ALERT) is None


def test_no_real_dns_look_up_from_tests_or_without_the_network(make_ctx, monkeypatch):
    monkeypatch.setattr(dv, "RESOLVER", None)
    ctx = make_ctx(**MAIL)
    with ctx.db.session() as s:
        assert dv.refresh_auth(s, ctx.settings) is None and dv.monitor(s, ctx.settings)["issues"] == []
    offline = make_ctx(network_enabled=False, data_role="production", **MAIL)
    assert dv._default_resolver(offline.settings) is None


def test_the_doctor_reports_the_pause_and_the_gate(world, monkeypatch):
    ctx = world
    checks = {c["name"]: c for c in run_checks(ctx)}
    assert checks["Outbound not paused for deliverability"]["ok"]
    assert checks["Sending domain authentication (SPF, DKIM, DMARC)"]["ok"]  # not checked yet, warn mode: no finding
    monkeypatch.setattr(dv, "RESOLVER", resolver(NO_DMARC))
    with ctx.db.session() as s:
        dv.refresh_auth(s, ctx.settings)
        dv.pause(s, ctx.settings, "test", "all mailboxes", {})
    checks = {c["name"]: c for c in run_checks(ctx)}
    auth = checks["Sending domain authentication (SPF, DKIM, DMARC)"]
    assert not auth["ok"] and auth["severity"] == "warning" and "DMARC" in auth["detail"] and auth["own_alert"]
    assert not checks["Outbound not paused for deliverability"]["ok"]
    configure(ctx, auth_gate="block")
    assert {c["name"]: c for c in run_checks(ctx)}["Sending domain authentication (SPF, DKIM, DMARC)"]["severity"] == "error"


# --------------------------------------------------------------------------- per-mailbox accounting
def test_one_mailbox_counts_exactly_as_the_global_count(world):
    ctx = world
    history(ctx, 7, ago=timedelta(hours=3))
    history(ctx, 4, ago=timedelta(hours=30))  # outside the 24 hours
    with ctx.db.session() as s:
        guard = dv.SendGuard(s, ctx.settings, utcnow(), None, "hello@us.example")
        assert guard.box_sent == 7 and guard.cap == 50 and guard.cap_why == "the mailbox limit"
        assert dv.mailbox_sent(s, 3, utcnow() - timedelta(hours=24)) == 7  # a panel mailbox counts the unrecorded ones too


def test_two_mailboxes_count_their_own_and_the_unrecorded(world):
    ctx = world
    history(ctx, 4, ago=timedelta(hours=2), account=1)
    history(ctx, 3, ago=timedelta(hours=2), account=2)
    history(ctx, 2, ago=timedelta(hours=2))
    since = utcnow() - timedelta(hours=24)
    with ctx.db.session() as s:
        assert dv.mailbox_sent(s, 1, since) == 6 and dv.mailbox_sent(s, 2, since) == 5 and dv.mailbox_sent(s, None, since) == 2
        assert dv.panel(s, ctx.settings)["global_sent"] == 9


def test_a_send_records_which_mailbox_sent_it(make_ctx):
    from tests.test_automation import drafted, followup, run
    from tests.test_mailbox_admin import RecordingSMTP, primary_gmail
    from tests.test_outreach import approve_all

    ctx, sender, fid = drafted(make_ctx)
    approve_all(ctx)
    run(ctx, sender)
    assert followup(ctx, fid).status == "sent" and followup(ctx, fid).mail_account_id is None  # the .env mailbox

    RecordingSMTP.logins, RecordingSMTP.hosts, FakeSMTP.sent = [], [], []
    ctx2, _sender, fid2 = drafted(make_ctx)
    aid = primary_gmail(ctx2)
    approve_all(ctx2)
    run(ctx2, EmailSender(ctx2.settings, smtp_factory=RecordingSMTP))
    assert followup(ctx2, fid2).status == "sent" and followup(ctx2, fid2).mail_account_id == aid


def test_a_mailbox_cap_can_only_lower_the_global_one(world):
    ctx = world
    history(ctx, 2, ago=timedelta(hours=1))
    configure(ctx, mailbox_caps={"Hello@us.example": 2})
    [fid] = approved(ctx, 1)
    followups_run(ctx)
    status, why = status_of(ctx, fid)
    assert status == "approved" and "this mailbox's own cap: 2" in why
    configure(ctx, mailbox_caps={"hello@us.example": 500})
    with ctx.db.session() as s:
        assert dv.effective_cap(s, ctx.settings, None, "hello@us.example", utcnow()) == (50, "the mailbox limit")
    followups_run(ctx)
    assert status_of(ctx, fid)[0] == "sent"


def test_the_global_mailbox_limit_still_counts_every_mailbox(world):
    ctx = world
    ctx.settings.business["outreach"]["mailbox_daily_limit"] = 3
    history(ctx, 3, ago=timedelta(hours=1), account=99)  # another mailbox's emails today
    [fid] = approved(ctx, 1)
    followups_run(ctx)
    status, why = status_of(ctx, fid)
    assert status == "approved" and "its limit" in why


# --------------------------------------------------------------------------- per-hour cap and spacing
def test_the_per_hour_cap(world):
    ctx = world
    configure(ctx, hourly_cap=1)
    history(ctx, 1, ago=timedelta(minutes=30))
    [fid] = approved(ctx, 1)
    followups_run(ctx)
    status, why = status_of(ctx, fid)
    assert status == "approved" and "at most 1 emails an hour" in why
    configure(ctx, hourly_cap=2)
    followups_run(ctx)
    assert status_of(ctx, fid)[0] == "sent"


def test_spacing_waits_between_emails_and_never_beyond_its_budget(world, monkeypatch):
    ctx = world
    slept = []
    monkeypatch.setattr(dv, "SLEEP", slept.append)
    monkeypatch.setattr(dv.RANDOM, "uniform", lambda low, high: high)
    configure(ctx, jitter_enabled=True, jitter_min_seconds=10, jitter_max_seconds=20, jitter_run_budget_seconds=45)
    fids = approved(ctx, 5)
    followups_run(ctx)
    assert slept == [20, 20]  # none before the first email; a third wait would pass the 45 seconds
    states = [status_of(ctx, f) for f in fids]
    assert [st for st, _ in states].count("sent") == 3 and len(FakeSMTP.sent) == 3
    assert all("time for spacing out emails is used up" in why for st, why in states if st == "approved")


def test_spacing_is_off_by_default_and_bounded_whatever_the_settings(world, monkeypatch):
    ctx = world
    slept = []
    monkeypatch.setattr(dv, "SLEEP", slept.append)
    approved(ctx, 3)
    followups_run(ctx)
    assert slept == [] and len(FakeSMTP.sent) == 3
    configure(ctx, jitter_enabled=True, jitter_min_seconds=500, jitter_max_seconds=1000, jitter_run_budget_seconds=99999)
    cfg = dv.config(ctx.settings)
    assert cfg["jitter_max_seconds"] == dv.JITTER_MAX_CEILING and cfg["jitter_min_seconds"] <= cfg["jitter_max_seconds"]
    assert cfg["jitter_run_budget_seconds"] == dv.JITTER_BUDGET_CEILING
    assert dv.JITTER_MAX_CEILING * 60 < 300 * 60 and dv.JITTER_MAX_CEILING < 300  # far below the send run's five-minute check


# --------------------------------------------------------------------------- warm-up
def test_warm_up_starts_low_and_rises_with_the_mailbox_age(world):
    ctx = world
    configure(ctx, warmup_enabled=True)
    now = utcnow()
    with ctx.db.session() as s:
        assert dv.warmup_stage(s, ctx.settings, 5, "new@us.example", now) == {"day": 1, "cap": 5, "start": now.date().isoformat(),
                                                                                "complete": False}
        assert dv.effective_cap(s, ctx.settings, 5, "new@us.example", now) == (5, "warm-up day 1")
        configure(ctx, warmup_start={"new@us.example": (now - timedelta(days=10)).date().isoformat()})
        assert dv.effective_cap(s, ctx.settings, 5, "new@us.example", now) == (20, "warm-up day 11")
        configure(ctx, warmup_start={"new@us.example": (now - timedelta(days=60)).date().isoformat()})
        stage = dv.warmup_stage(s, ctx.settings, 5, "new@us.example", now)
        assert stage["complete"] and dv.effective_cap(s, ctx.settings, 5, "new@us.example", now)[0] == 50


def test_warm_up_can_only_lower_a_cap(world):
    ctx = world
    configure(ctx, warmup_enabled=True, warmup_schedule=[[1, 500]])
    ctx.settings.business["outreach"]["mailbox_daily_limit"] = 3
    with ctx.db.session() as s:
        assert dv.effective_cap(s, ctx.settings, 5, "new@us.example", utcnow()) == (3, "the mailbox limit")
    configure(ctx, mailbox_caps={"new@us.example": 2})
    with ctx.db.session() as s:
        assert dv.effective_cap(s, ctx.settings, 5, "new@us.example", utcnow()) == (2, "this mailbox's own cap")


def test_warm_up_holds_an_email_over_the_days_cap(world):
    ctx = world
    configure(ctx, warmup_enabled=True, warmup_schedule=[[1, 1], [2, 5]])
    history(ctx, 1, ago=timedelta(hours=1))  # its first email went out today: day 1
    [fid] = approved(ctx, 1)
    followups_run(ctx)
    status, why = status_of(ctx, fid)
    assert status == "approved" and "warm-up day 1: 1" in why
    page = TestClient(create_app(ctx)).get("/deliverability").text
    assert "day 1: 1 a day" in page


# --------------------------------------------------------------------------- RFC 8058
def test_the_one_click_header_is_added_only_with_an_https_address(make_ctx):
    ctx = make_ctx(**MAIL)
    FakeSMTP.sent = []
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    sender.send("ann@acme.example", "Hello", FOOTER)
    msg = FakeSMTP.sent[-1]
    assert msg["List-Unsubscribe"] == "<mailto:hello@us.example?subject=unsubscribe>" and msg["List-Unsubscribe-Post"] is None
    configure(ctx, one_click_unsubscribe_url="http://us.example/unsub")  # not https: ignored, and the doctor says so
    sender.send("ann@acme.example", "Hello", FOOTER)
    assert FakeSMTP.sent[-1]["List-Unsubscribe-Post"] is None
    assert not {c["name"]: c for c in run_checks(ctx)}["One-click unsubscribe address"]["ok"]
    configure(ctx, one_click_unsubscribe_url="https://us.example/unsubscribe")
    sender.send("ann@acme.example", "Hello", FOOTER)
    msg = FakeSMTP.sent[-1]
    assert msg["List-Unsubscribe"] == "<https://us.example/unsubscribe>, <mailto:hello@us.example?subject=unsubscribe>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


# --------------------------------------------------------------------------- the schema
def test_the_new_column_is_added_to_an_existing_database(make_ctx):
    ctx = make_ctx()
    with ctx.db.engine.begin() as conn:
        conn.execute(text('ALTER TABLE followups DROP COLUMN "mail_account_id"'))
    applied = migrate(ctx.db.engine)
    assert any("mail_account_id" in ddl for ddl in applied)
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp.mail_account_id)).all() == []
        assert s.execute(select(MailAccount.id)).all() == []


def test_a_spacing_wait_that_crosses_the_end_of_the_send_window_sends_nothing_after_it(world, monkeypatch, clock):
    """OB-001: the window is judged before the spacing wait, so the last-moment check must judge it again after it.
    The run starts 15 seconds before 18:00 New York; one 20-second wait later the window is closed."""
    ctx = world
    clock(datetime(2026, 11, 4, 22, 59, 45))  # Wednesday 17:59:45 New York

    def sleep(seconds):  # the wait passes on the application's clock
        aime.timeutil.CLOCK_OFFSET += timedelta(seconds=seconds)

    monkeypatch.setattr(dv, "SLEEP", sleep)
    monkeypatch.setattr(dv.RANDOM, "uniform", lambda low, high: high)
    configure(ctx, jitter_enabled=True, jitter_min_seconds=10, jitter_max_seconds=20, jitter_run_budget_seconds=120)
    fids = approved(ctx, 3)
    followups_run(ctx)
    states = [status_of(ctx, f) for f in fids]
    assert [st for st, _ in states].count("sent") == 1 and len(FakeSMTP.sent) == 1, states
    assert [st for st, _ in states].count("approved") == 2, states  # kept for the next window, never dropped
