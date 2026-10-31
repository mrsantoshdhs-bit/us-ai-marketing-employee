"""Phase 2, area "truth": are the numbers true, and can real people do their real day? Synthetic data only.

Every test_defect_* asserts the CORRECT behaviour and fails on today's code; each was first seen on an isolated copy of
production (report_truth.md). test_ok_* pins behaviour confirmed right. Nothing here needs a production copy."""

from __future__ import annotations

import imaplib
import json
import re
import smtplib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import aime.timeutil
from aime.agents import AGENTS
from aime.agents.analytics import AnalyticsReportingAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.cli import build_parser, cmd_user
from aime.integrations.email_smtp import EmailSender
from aime.models import (Alert, Company, Contact, ContentItem, FollowUp, InboundEmail, JobRun, Lead, Meeting, Opportunity,
                         Quote, Team, User)
from aime.monitoring import heartbeat_path
from aime.performance import build
from aime.reply_intel import process_pending
from aime.scheduler.runner import run_job
from aime.timeutil import utcnow
from aime.web.app import ai_employee, create_app, dashboard_view, funnel_detail, health_strip, manager_today
from aime.web.auth import COOKIE, hash_password, make_cookie, session_secret
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import MAIL, contacted
from tests.test_outreach import approve_all, researched

ORIGIN = {"origin": "http://testserver"}
PW = "p2 truth only " + "test passphrase"  # synthetic, assembled so a secret scanner stays quiet
HASH = hash_password(PW)
IST = "Asia/Kolkata"
DOCS = Path(__file__).resolve().parents[2] / "docs"


@pytest.fixture(autouse=True)
def _no_task_scheduler(monkeypatch):
    from aime.scheduler import windows
    monkeypatch.setattr(windows, "query", lambda *a, **k: {"installed": False, "error": "uat"})


@pytest.fixture
def org(make_ctx):
    """The real organisation's shape: one owner who sees everything, two Sales Managers in one team."""
    ctx = make_ctx(dashboard_auth=True)
    with ctx.db.session() as s:
        team = Team(name="US Sales", manager=None)
        s.add(team)
        s.flush()
        s.add(User(username="owner", password_hash=HASH, role="super_admin"))
        s.add(User(username="sam", password_hash=HASH, role="sales_manager", team_id=team.id))
        s.add(User(username="kim", password_hash=HASH, role="sales_manager", team_id=team.id, manager="sam"))
    return ctx


def client(ctx, username=None, redirects=True):
    c = TestClient(create_app(ctx), raise_server_exceptions=False, follow_redirects=redirects, headers=ORIGIN)
    if username:
        with ctx.db.session() as s:
            user = s.execute(select(User).where(User.username == username)).scalar_one()
            c.cookies.set(COOKIE, make_cookie(session_secret(ctx.settings.data_dir), user))
    return c


def words(html: str) -> str:
    """The page as a person reads it, without the tag soup, the menu or the top bar."""
    html = re.sub(r"<(script|style|title)[^>]*>.*?</\1>", " ", html, flags=re.S)
    html = html[html.find("Sign out"):] if "Sign out" in html else html
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def set_clock(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", when - datetime.now(timezone.utc).replace(tzinfo=None))


N = iter(range(1, 100000))


def add_lead(ctx, name=None, owner=None, status="qualified", outreach=None, usable=True, first="Dana", last="Lee", grade="B"):
    i = next(N)
    name = name or f"Acme {i}"
    with ctx.db.session() as s:
        co = Company(name=name, normalized_name=name.lower(), source="test", domain=f"acme{i}.example")
        s.add(co)
        s.flush()
        ct = Contact(company_id=co.id, first_name=first, last_name=last, email=f"info@acme{i}.example", source="website",
                     email_status="published" if usable else None, email_source_url=f"https://acme{i}.example/team",
                     title="CEO")
        s.add(ct)
        s.flush()
        lead = Lead(company_id=co.id, contact_id=ct.id, source="research", status=status, owner=owner, score=70, grade=grade,
                    outreach_status=outreach, primary_service="ai_agents")
        s.add(lead)
        s.flush()
        return lead.id


def add_email(ctx, lead_id, status="sent", kind="outreach", step=1, sent_at=None, subject="Hello"):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        f = FollowUp(lead_id=lead_id, company_id=lead.company_id, contact_id=lead.contact_id, channel="email", kind=kind,
                     step=step, status=status, subject=subject, body="b", to_email=lead.contact.email, due_at=utcnow(),
                     sent_at=sent_at if status == "sent" else None, message_id=f"<m{next(N)}@us.example>", evidence=[])
        if status == "sent" and sent_at is None:
            f.sent_at = utcnow() - timedelta(days=1)
        s.add(f)
        s.flush()
        return f.id


def add_reply(ctx, lead_id, category="other", simulated=False, when=None, status="actioned", sender=None):
    with ctx.db.session() as s:
        when = when or utcnow() - timedelta(hours=2)
        s.add(InboundEmail(message_id=f"<r{next(N)}@x.example>", from_email=sender or "someone@x.example", subject="Re: Hello",
                           body_text="x", new_text="x", received_at=when, processed_at=when, category=category,
                           confidence="high", status=status, lead_id=lead_id, is_simulated=simulated))


def add_meeting(ctx, lead_id, status="scheduled", simulated=False, start=None, created=None):
    with ctx.db.session() as s:
        start = start or utcnow() + timedelta(hours=5)
        s.add(Meeting(lead_id=lead_id, invitee_email="p@x.example", invitee_name="Pat", title="Intro", starts_at=start,
                      ends_at=start + timedelta(minutes=30), source="manual", status=status, is_simulated=simulated,
                      created_at=created or utcnow() - timedelta(hours=1)))


def add_deal(ctx, lead_id, stage="proposal", value=None, owner="sam", closed=None):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        o = Opportunity(lead_id=lead_id, company_id=lead.company_id, name=f"Deal {lead_id}", stage=stage, est_value=value,
                        owner=owner, closed_at=closed, won_by=owner if stage == "won" else None, origin="ai")
        s.add(o)
        s.flush()
        return o.id


def add_post(ctx, status="approved", due_days_ago=2, title="Post"):
    with ctx.db.session() as s:
        when = utcnow() - timedelta(days=due_days_ago)
        p = ContentItem(service="ai_agents", industry="saas", title=f"{title} {next(N)}", body="b", generator="template",
                        fingerprint=f"fp-{next(N)}", status=status, scheduled_for=when, approved_at=when - timedelta(days=1), approved_by="owner")
        s.add(p)
        s.flush()
        return p.id


def open_alerts(ctx, source=None) -> list[str]:
    with ctx.db.session() as s:
        stmt = select(Alert).where(Alert.acknowledged.is_(False))
        if source:
            stmt = stmt.where(Alert.source == source)
        return [a.message for a in s.execute(stmt.order_by(Alert.id)).scalars()]


def ack_all(ctx) -> None:
    with ctx.db.session() as s:
        for a in s.execute(select(Alert).where(Alert.acknowledged.is_(False))).scalars():
            a.acknowledged = True


def beat(ctx, minutes_ago: int, state="idle") -> None:
    at = utcnow() - timedelta(minutes=minutes_ago)
    heartbeat_path(ctx.settings).write_text(json.dumps({"at": at.isoformat() + "Z", "state": state, "session": "background",
                                                        "trigger": "scheduler"}), encoding="utf-8")


def report(ctx, kind="daily") -> str:
    res = AnalyticsReportingAgent(ctx).run({"kind": kind, "import_metrics": False})
    assert res.ok, res.error
    return Path(res.output.path_md).read_text(encoding="utf-8")


def rate(md: str, label: str) -> int | None:
    m = re.search(rf"{label}[^|]*\|[^|]*\| (\d+)%", md)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ number truth: rates and denominators
def test_defect_t01_ready_for_outreach_counts_leads_already_contacted(org):
    """Production today: "5 qualified leads ready for outreach" on Manager Today, and those 5 are the 5 already emailed."""
    for _ in range(3):
        add_email(org, add_lead(org, owner="sam", outreach="contacted"))
    with org.db.session() as s:
        assert manager_today(org, s)["counts"]["outreach_ready"] == 0


def test_ok_t01_a_lead_not_yet_contacted_is_ready(org):
    add_lead(org)
    with org.db.session() as s:
        assert manager_today(org, s)["counts"]["outreach_ready"] == 1


def test_defect_t02_reply_rate_divides_messages_and_strangers_by_emails(org):
    """Two prospects emailed, one answered three times, plus one mail nobody could match: one of two replied (50%)."""
    a, b = add_lead(org, outreach="contacted"), add_lead(org, outreach="contacted")
    add_email(org, a)
    add_email(org, b)
    for _ in range(3):
        add_reply(org, a, "question")
    add_reply(org, None, "other", sender="stranger@elsewhere.example")
    assert rate(report(org), "Reply rate") == 50


def test_defect_t02_meeting_rate_can_exceed_100_percent(org):
    """Seen on the copy: "Meeting rate (meetings / interested) 300% (3/1)"."""
    leads = [add_lead(org) for _ in range(3)]
    add_reply(org, leads[0], "interested")
    for lead in leads:
        add_meeting(org, lead)
    assert rate(report(org), "Meeting rate") <= 100


def test_defect_t03_home_kpi_shows_all_time_total_under_the_period_it_is_labelled_with(org):
    """Under "Oct 26 - Nov 02, Last 7 days": "Companies discovered 306" while the chart below says 106, and "Meetings
    booked 3" next to 1 in the chart. Choosing "Last 30 days" does not move the big number."""
    with org.db.session() as s:
        for i in range(5):
            s.add(Company(name=f"Old {i}", normalized_name=f"old {i}", source="test", created_at=utcnow() - timedelta(days=60)))
    add_lead(org, "New one")
    html = client(org, "owner").get("/?range=7").text
    card = re.search(r'kpi-label">Companies discovered</span><b class="kpi-value">([\d,]+)</b>.*?</a>', html, re.S)
    assert int(card.group(1)) == 1 or re.search(r"all[- ]time|total", card.group(0), re.I)


def test_defect_t04_new_leads_stage_counts_merged_and_disqualified_leads(org):
    add_lead(org)
    add_lead(org, status="disqualified")
    add_lead(org, status="merged")
    with org.db.session() as s:
        stages = dashboard_view(org, s, 7)["pipeline_stages"]
    assert stages[0]["label"] != "New leads" or stages[0]["value"] == 1


def test_defect_t05_cancelled_meetings_count_on_sales_performance_only(org):
    """Performance says 1 meeting; the home page, the funnel and the reports all leave the cancelled one out."""
    add_meeting(org, add_lead(org, owner="sam"), status="cancelled")
    with org.db.session() as s:
        total = build(s, utcnow() - timedelta(days=30), utcnow() + timedelta(days=1)).total.meetings
        funnel = dict((label, n) for label, n, _ in funnel_detail(s))["Meetings booked"]
    assert total == funnel == 0


def test_defect_t06_won_deal_in_another_currency_silently_missing_from_revenue(org):
    """A won deal whose accepted quote is EUR 9,000 has no USD value: "Won 2, Won value $2,500" with no word about it."""
    add_deal(org, add_lead(org, owner="sam"), "won", 2500.0, closed=utcnow() - timedelta(days=1))
    oid = add_deal(org, add_lead(org, owner="sam"), "won", None, closed=utcnow() - timedelta(days=1))
    with org.db.session() as s:
        s.add(Quote(opportunity_id=oid, amount=9000.0, currency="EUR", status="accepted", created_by="sam"))
    page = words(client(org, "owner").get("/performance").text)
    assert re.search(r"EUR|without a (USD )?value|no value", page), "the report must say one won deal adds nothing to revenue"


def test_ok_t06_usd_revenue_adds_up_by_owner_and_month_window(org):
    add_deal(org, add_lead(org, owner="sam"), "won", 1000.0, closed=utcnow() - timedelta(days=1))
    add_deal(org, add_lead(org, owner="kim"), "won", 500.0, closed=utcnow() - timedelta(days=2))
    add_deal(org, add_lead(org, owner="kim"), "won", 7000.0, closed=utcnow() - timedelta(days=45))
    add_deal(org, add_lead(org, owner="kim"), "lost", 900.0, closed=utcnow() - timedelta(days=2))
    with org.db.session() as s:
        r = build(s, utcnow() - timedelta(days=30), utcnow() + timedelta(days=1))
    assert (r.total.won, r.total.lost, r.total.won_value) == (2, 1, 1500.0)


def test_defect_t07_my_work_and_performance_disagree_on_the_last_30_days(org):
    """/my "Last 30 days" is a rolling 30 x 24 h; /performance starts at local midnight 30 days ago."""
    lead = add_lead(org, owner="sam")
    add_email(org, lead, sent_at=utcnow() - timedelta(days=30, minutes=30))
    with org.db.session() as s:  # the clock of these tests is never near midnight in this window
        assert s.get(Lead, lead)
    c = client(org, "sam")
    mine = re.search(r"<td>sam</td><td>(\d+)</td>", c.get("/my").text).group(1)
    perf = re.search(r"sam</a></td><td>\d+</td><td>\d+</td><td>(\d+)</td>", c.get("/performance").text).group(1)
    assert mine == perf


def test_defect_t08_system_replies_to_approve_counts_outreach(org):
    add_email(org, add_lead(org), status="awaiting_approval")
    page = words(client(org, "owner").get("/system").text)
    assert "0 Replies to approve" in page or "Replies to approve" not in page


def test_defect_t09_reports_meetings_chart_label_says_it_includes_cancelled_but_it_does_not(org):
    add_meeting(org, add_lead(org), status="cancelled")
    html = client(org, "owner").get("/reports").text
    assert not ("incl. later cancelled" in html and "No meetings booked in the last 8 weeks" in html)


# ------------------------------------------------------------------ simulated and test records
def simulated_interest(ctx):
    lead = add_lead(ctx, owner="sam", outreach="contacted")
    add_email(ctx, lead)
    with ctx.db.session() as s:
        contact_email = s.get(Lead, lead).contact.email
        s.add(InboundEmail(message_id=f"<sim{next(N)}@aime.local>", from_email=contact_email, subject="Re: Hello",
                           body_text="Yes interested, let's schedule a call.", new_text="Yes interested, let's schedule a call.",
                           is_simulated=True, status="new"))
        s.commit()
        process_pending(s, ctx.settings, "simulate-reply")
    return lead


def test_defect_t10_simulated_reply_counts_as_interested_and_opens_a_real_looking_deal(org):
    """OPERATIONS.md: simulated replies are "never counted". The funnel, the pipeline and Hot leads count them."""
    simulated_interest(org)
    with org.db.session() as s:
        funnel = dict((label, n) for label, n, _ in funnel_detail(s))
    assert (funnel["Interested"], funnel["Open opportunities"]) == (0, 0)


def test_defect_t10_draft_from_a_simulated_reply_is_not_marked_on_manager_today(org):
    simulated_interest(org)
    with org.db.session() as s:
        rows = [n for n in manager_today(org, s)["needs"] if n["kind"] == "Email"]
    assert not rows or all("SIMULATED" in (n["text"] + n["why"]) for n in rows)


def test_defect_t11_simulated_meeting_counts_on_my_work_and_in_the_ai_employee_panel(org):
    add_meeting(org, add_lead(org, owner="sam"), simulated=True)
    assert "Upcoming meetings: 0" in words(client(org, "sam").get("/my").text)
    with org.db.session() as s:
        waiting = {label: n for label, n, _ in ai_employee(org, s)["employee"]["waiting"]}
    assert waiting["meetings in 24 h"] == 0


def test_ok_t11_simulated_records_stay_out_of_the_report_and_performance(org):
    lead = add_lead(org, owner="sam")
    add_reply(org, lead, "interested", simulated=True)
    add_meeting(org, lead, simulated=True)
    md = report(org)
    assert "SIMULATED test activity" in md and rate(md, "Reply rate") is None
    with org.db.session() as s:
        r = build(s, utcnow() - timedelta(days=30), utcnow() + timedelta(days=1)).total
    assert (r.positive_replies, r.meetings) == (0, 0)


def test_defect_t12_test_mode_email_makes_the_prospect_contacted(make_ctx):
    """OUTBOUND_TEST_RECIPIENTS: the email went only to our own address, yet the lead reads "contacted" and the funnel adds it."""
    ctx, _sender, sent = contacted(make_ctx, outbound_test_recipients="me@us.example")
    assert sent.delivered_to == "me@us.example"
    with ctx.db.session() as s:
        funnel = dict((label, n) for label, n, _ in funnel_detail(s))
    assert funnel["Outreach emails sent"] == 0


# ------------------------------------------------------------------ "today", "this week" and time zones
def test_defect_t13_analytics_weeks_are_utc_weeks_not_the_owners(org, monkeypatch):
    """Mon 2 Nov 00:15 IST (Sun 18:45 UTC): Manager Today and the home chart say Monday; Sales Analytics files it in the
    week of Oct 26."""
    org.settings.schedule["timezone"] = IST
    set_clock(monkeypatch, datetime(2026, 11, 1, 19, 0))
    add_reply(org, add_lead(org), "question", when=datetime(2026, 11, 1, 18, 45))
    assert "Replies, week of Nov 02: 1" in client(org, "owner").get("/reports").text


def test_ok_t13_manager_today_starts_at_the_owners_midnight(org, monkeypatch):
    org.settings.schedule["timezone"] = IST
    set_clock(monkeypatch, datetime(2026, 11, 1, 19, 0))
    with org.db.session() as s:
        s.add(Company(name="Just after midnight", normalized_name="a", source="test", created_at=datetime(2026, 11, 1, 18, 45)))
        s.add(Company(name="Just before midnight", normalized_name="b", source="test", created_at=datetime(2026, 11, 1, 18, 15)))
    with org.db.session() as s:
        view = manager_today(org, s)
    assert view["counts"]["discovered"] == 1 and "Monday 02 November" in view["day_label"]


def test_defect_t14_home_says_tomorrow_for_a_meeting_this_evening_in_the_owners_day(org, monkeypatch):
    """Mon 09:00 IST, Manager Today reads "Monday 02 November" and lists the meeting; the home page says "Tomorrow"
    because it is still Sunday in New York."""
    org.settings.schedule["timezone"] = IST
    set_clock(monkeypatch, datetime(2026, 11, 2, 3, 30))
    add_meeting(org, add_lead(org), start=datetime(2026, 11, 2, 15, 0))
    with org.db.session() as s:
        assert dashboard_view(org, s, 7)["upcoming_list"][0]["day"] != "Tomorrow"


def test_defect_t15_my_work_marks_overdue_by_the_utc_date(org, monkeypatch):
    org.settings.schedule["timezone"] = IST
    set_clock(monkeypatch, datetime(2026, 11, 1, 19, 0))  # Mon 2 Nov 00:30 IST
    oid = add_deal(org, add_lead(org, owner="sam"))
    with org.db.session() as s:
        s.get(Opportunity, oid).expected_close = date(2026, 11, 1)
    assert '<td class="bad">2026-11-01</td>' in client(org, "sam").get("/my").text


def test_defect_t16_dates_on_my_work_and_follow_ups_have_no_time_zone(org):
    """"due 2026-10-28 07:08" next to "Mon Nov 02, 10:00 AM ET" and alert texts in UTC: a reader cannot tell which clock."""
    with org.db.session() as s:
        lead_id = add_lead(org, owner="sam")
        lead = s.get(Lead, lead_id)
        s.add(FollowUp(lead_id=lead_id, company_id=lead.company_id, channel="task", step=1, status="pending",
                       subject="Review Acme", body="x", due_at=utcnow()))
    c = client(org, "sam")
    for path in ("/my", "/followups"):
        m = re.search(r"due \d{4}-\d\d-\d\d \d\d:\d\d( [A-Z]{2,4}\b)?", words(c.get(path).text))
        assert m and m.group(1), f"{path}: {m and m.group(0)}"


# ------------------------------------------------------------------ statuses that say more than is true
def test_defect_t17_working_now_for_two_hours_after_the_pc_went_to_sleep(org):
    """A tick that started 90 minutes ago and never ended (sleep): "Working now · AI is working for you!" with no job open,
    next to "Now: Idle, no job running" on the same page."""
    beat(org, 90, state="running")
    page = client(org, "owner").get("/").text
    assert "Working now" not in page


def test_defect_t18_zero_scheduled_runs_reads_all_successful(org):
    """A weekend with the PC asleep: "Scheduled runs, 24 h 0, all successful"."""
    beat(org, 60 * 30)
    page = words(client(org, "owner").get("/manager").text)
    assert "0, all successful" not in page


def test_defect_t19_command_center_says_jobs_fine_when_a_job_was_abandoned(org):
    """Manager Today: "1 not successful". System health on the Command Center: "0 failed or partial", green."""
    with org.db.session() as s:
        s.add(JobRun(job="enrich", trigger="scheduler", status="abandoned", started_at=utcnow() - timedelta(hours=2),
                     finished_at=utcnow() - timedelta(hours=1), error="Process ended"))
    with org.db.session() as s:
        jobs = next(r for r in health_strip(org, s) if r[0] == "Jobs (24 h)")
    assert jobs[1] != "ok", jobs


class RefusingSMTP(FakeSMTP):
    def login(self, *a):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")


def test_defect_t20_email_sending_active_while_the_mailbox_refuses_sign_in(make_ctx):
    """The error alert says nothing is being sent; the board on Manager Today keeps saying ACTIVE, "approved emails go out"."""
    ctx = researched(make_ctx, **MAIL)
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=RefusingSMTP)).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert any("refused the sign-in" in m for m in open_alerts(ctx))
    with ctx.db.session() as s:
        row = next(b for b in manager_today(ctx, s)["board"] if b["name"] == "Email sending")
    assert row["status"] != "ACTIVE", row


def test_ok_t21_mailbox_reading_failed_shows_on_the_board(make_ctx, monkeypatch):
    ctx = make_ctx(**MAIL)

    class Refuses:
        def __init__(self, *a, **k):
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    monkeypatch.setattr("aime.inbox.imaplib.IMAP4_SSL", Refuses)
    run_job(ctx, "inbox", "scheduler")
    with ctx.db.session() as s:
        row = next(b for b in manager_today(ctx, s)["board"] if b["name"] == "Mailbox reading")
    assert row["status"] == "FAILED"


# ------------------------------------------------------------------ lead #43
def stale_competitor(ctx):
    lead_id = add_lead(ctx, outreach="drafted")
    fid = add_email(ctx, lead_id, status="awaiting_approval")
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        lead.company.is_competitor = True
        s.add(FollowUp(lead_id=lead_id, company_id=lead.company_id, channel="task", step=1, status="done",  # as on lead #43:
                       subject="Review", body="x", due_at=utcnow(), blocked_reason="Outreach email drafted"))  # closed by the draft
    AGENTS["qualification"](ctx).run({})
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).status == "disqualified" and s.get(FollowUp, fid).status == "cancelled"
    return lead_id


def test_defect_t22_disqualified_lead_stays_drafted_in_the_pipeline(org):
    """Production lead #43: disqualified, its draft cancelled, and /pipeline still says "drafted: 1" with 0 emails waiting."""
    stale_competitor(org)
    assert "drafted: 0" in words(client(org, "owner").get("/pipeline").text)


def test_defect_t23_clearing_a_false_competitor_flag_leaves_the_lead_with_no_way_forward(org):
    """Reproduced in full on the copy with lead #43: "Not a competitor" -> qualified again -> no draft and no task, ever,
    because the cancelled email counts as "already in a sequence"; the lead page keeps saying "drafted"."""
    lead_id = stale_competitor(org)
    with org.db.session() as s:
        company_id = s.get(Lead, lead_id).company_id
    client(org, "owner").post(f"/companies/{company_id}/not_competitor")
    with org.db.session() as s:
        s.get(Lead, lead_id).status = "qualified"  # what qualification does on its next run for this lead's score
        s.get(Lead, lead_id).last_scored_at = utcnow()
    SalesWorkflowAgent(org).run({"now": utcnow().isoformat()})
    with org.db.session() as s:
        open_work = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.status.in_(
            ("awaiting_approval", "pending")))).scalars().all()
    assert open_work, "a qualified lead with a usable address must get a draft or a task"


# ------------------------------------------------------------------ first day of a salesperson
def test_ok_t24_first_sign_in_path_from_the_command_line(org):
    """`user set-password`, then the sign-in form, lands on My work with the leads of the team."""
    add_lead(org, owner="sam", outreach="contacted")

    class Args:
        action, username, role, data_dir = "set-password", "kim", None, None

    new = "first day " + "passphrase for kim"
    assert cmd_user(Args, org, prompt_fn=lambda *_: new) == 0
    c = client(org)
    r = c.post("/login", data={"username": "kim", "password": new, "next": "/"})
    assert r.status_code == 200 and str(r.url).endswith("/my") and "Leads: 1" in words(r.text)


def test_defect_t25_my_work_does_not_say_which_of_your_leads_were_contacted(org):
    """sales1's five leads show "B · 68 · sales1" and nothing else: no stage, no last contact, no next step."""
    add_email(org, add_lead(org, "Contacted Co", owner="sam", outreach="contacted"))
    html = client(org, "sam").get("/my").text
    row = re.search(r'<div class="row-item"><a href="/leads/\d+">Contacted Co</a>.*?</div>', html, re.S).group(0)
    assert "contacted" in row


def test_defect_t26_lead_page_does_not_say_when_the_next_email_is_due(org):
    lead_id = add_lead(org, owner="sam", outreach="contacted")
    add_email(org, lead_id)
    page = words(client(org, "sam").get(f"/leads/{lead_id}").text)
    assert re.search(r"(?i)next (email|follow-?up|step)", page)


def test_defect_t27_unassigned_pill_opens_a_list_with_another_number(org):
    """Production: "Unassigned qualified leads: 37" opens "Leads 42 matching": 37 plus sales1's five."""
    add_lead(org, owner="sam")
    add_lead(org)
    add_lead(org)
    c = client(org, "sam")
    html = c.get("/my").text
    pill = re.search(r'<a class="pill" href="([^"]+)">Unassigned qualified leads: (\d+)', html)
    assert pill.group(2) == "2"
    assert re.search(r"(\d+) matching", words(c.get(pill.group(1).replace("&amp;", "&")).text)).group(1) == "2"


def test_defect_t28_nobody_can_list_their_own_leads(org):
    """My work shows 12 leads and no link; /leads has no owner filter, so past 12 a salesperson cannot see their own list."""
    add_lead(org, owner="kim")
    add_lead(org, owner="sam")
    add_lead(org)
    assert re.search(r"(\d+) matching", words(client(org, "kim").get("/leads?owner=kim").text)).group(1) == "1"


def test_defect_t29_tasks_past_ten_vanish_from_my_work_without_a_count(org):
    lead_id = add_lead(org, owner="sam")
    with org.db.session() as s:
        lead = s.get(Lead, lead_id)
        for i in range(12):
            s.add(FollowUp(lead_id=lead_id, company_id=lead.company_id, channel="task", step=1, status="pending",
                           subject=f"Task {i}", body="x", due_at=utcnow()))
    html = client(org, "sam").get("/my").text
    tasks = html[html.find("<h2>Tasks"):html.find("<h2>Meetings")]
    assert re.search(r"\b12\b", re.sub(r"Task \d+", "", re.sub(r"<[^>]+>", " ", tasks).split("due")[0])) or "/followups" in tasks


def test_defect_t30_admin_my_work_calls_everyone_elses_leads_yours(org):
    add_lead(org, owner="sam")
    page = words(client(org, "owner").get("/my").text)
    assert "Leads: 0" in page or "on you" not in page


def test_defect_t31_menu_links_a_salesperson_is_shown_silently_bounce_to_my_work(org):
    """Bell, Audit log, Automation Control, View activity, Authorizations and limits, System health: each click comes
    back to My work with no word of why. Either hide the link or say why."""
    c = client(org, "sam", redirects=False)
    links = set()
    for page in ("/my", "/settings"):
        links |= set(re.findall(r'href="(/[^"#]*)', c.get(page).text))
    bounced = [link for link in sorted(links) if link != "/" and c.get(link).status_code == 303
               and c.get(link).headers["location"] == "/my"]
    assert not bounced, bounced


def test_ok_t31_every_link_the_owner_sees_opens(org):
    lead_id = add_lead(org, owner="sam", outreach="contacted")
    add_email(org, lead_id)
    add_deal(org, lead_id)
    c = client(org, "owner", redirects=False)
    links = set()
    for page in ("/", "/my", "/manager", "/reports", "/performance", "/pipeline", "/inbox", "/followups", "/command-center",
                 "/system", "/settings", "/approvals", f"/leads/{lead_id}"):
        links |= set(re.findall(r'href="(/[^"#]*)', c.get(page).text))
    bad = [link for link in sorted(links) if "/connect" not in link and "linkedin" not in link and not link.startswith("/static")
           and c.get(link.replace("&amp;", "&")).status_code != 200]
    assert not bad, bad


def test_defect_t32_docs_never_tell_a_salesperson_how_to_get_a_password(org):
    """`user set-password` exists only as a line on the sign-in page; HANDOVER.md says `user create`, which refuses an
    existing account such as sales1."""
    docs = "\n".join(p.read_text(encoding="utf-8") for p in DOCS.glob("*.md"))
    assert "set-password" in docs


def test_defect_t33_mailbox_admin_command_in_the_docs_does_not_parse():
    doc = (DOCS / "MAILBOX_ADMIN.md").read_text(encoding="utf-8")
    assert "user set-role <" in doc  # the literal form the doc gives
    try:
        build_parser().parse_args(["user", "set-role", "someone", "--role", "super_admin"])
    except SystemExit:
        pytest.fail("docs/MAILBOX_ADMIN.md: `aime user set-role <name> --role super_admin` is refused by the parser")


def test_defect_t34_runbook_says_acknowledge_alerts_on_today_but_it_has_no_alerts(org):
    """OPERATIONS.md, Daily routine: "Today → acknowledge alerts", health strip, alerts, funnel, companies to review."""
    with org.db.session() as s:
        s.add(Alert(level="warning", source="monitor", message="Something to acknowledge"))
    assert "/ack" in client(org, "owner").get("/").text


# ------------------------------------------------------------------ a real Monday
def test_defect_t35_interested_prospect_is_not_the_first_thing_on_manager_today(org):
    """The Monday list runs replies, then emails by due date; the answer to an interested prospect comes after an unknown
    sender and its reason reads "every email needs a person while REQUIRE_HUMAN_APPROVAL=true"."""
    add_reply(org, None, "question", status="needs_review", sender="stranger@elsewhere.example")
    add_email(org, add_lead(org, owner="sam", outreach="contacted"), status="awaiting_approval", step=2, subject="Step two")
    hot = add_lead(org, owner="sam", outreach="interested")
    add_reply(org, hot, "interested")
    add_email(org, hot, status="awaiting_approval", kind="response", subject="Answer to the interested one")
    with org.db.session() as s:
        needs = manager_today(org, s)["needs"]
    assert needs[0]["text"] == "Answer to the interested one" and "interested" in needs[0]["why"], needs[0]


def test_defect_t36_needs_you_count_stops_at_fifteen_emails(org):
    lead = add_lead(org, owner="sam")
    for i in range(20):
        add_email(org, lead, status="awaiting_approval", subject=f"Draft {i}")
    assert "20 need you" in words(client(org, "owner").get("/manager").text)


def test_defect_t37_the_page_the_bell_opens_hides_warnings_behind_info_notices(org):
    """Production: bell 7, /system lists 10 of 12 alerts: 5 notices and 5 warnings, the doctor warning is not among them."""
    with org.db.session() as s:
        s.add(Alert(level="warning", source="doctor", message="doctor: old warning one"))
        s.add(Alert(level="warning", source="doctor", message="doctor: old warning two"))
        for i in range(10):
            s.add(Alert(level="info", source="handover", message=f"Assigned lead #{i}"))
    page = client(org, "owner").get("/system").text
    assert "old warning one" in page and "old warning two" in page


def test_defect_t37_the_bell_points_at_an_anchor_that_does_not_exist(org):
    page = client(org, "owner").get("/system").text
    assert 'href="/system#alerts"' in page and 'id="alerts"' in page


def test_defect_t38_acknowledge_throws_you_to_the_home_page(org):
    """Twelve alerts means twelve trips: click on /system, land on / (no alerts there), go back. No "acknowledge all" either."""
    with org.db.session() as s:
        a = Alert(level="warning", source="monitor", message="x")
        s.add(a)
        s.flush()
        aid = a.id
    r = client(org, "owner", redirects=False).post(f"/alerts/{aid}/ack", headers={"referer": "http://testserver/system"})
    assert r.headers["location"].startswith("/system")


def test_ok_t38_acknowledging_lowers_the_bell(org):
    with org.db.session() as s:
        a = Alert(level="warning", source="monitor", message="x")
        s.add(a)
        s.flush()
        aid = a.id
    c = client(org, "owner")
    assert re.search(r'class="badge">1<', c.get("/my").text)
    c.post(f"/alerts/{aid}/ack")
    assert 'class="badge"' not in re.search(r'<a class="bell".*?</a>', c.get("/my").text, re.S).group(0)


def test_defect_t39_publishing_off_warning_comes_back_every_day_after_acknowledging(org, monkeypatch):
    """Publishing is off by the owner's decision, approved posts stay due: a warning (and a desktop toast) every day for ever."""
    add_post(org)
    AGENTS["publishing"](org).run({})
    assert len(open_alerts(org, "publishing")) == 1
    ack_all(org)
    set_clock(monkeypatch, utcnow() + timedelta(hours=25))
    AGENTS["publishing"](org).run({})
    assert open_alerts(org, "publishing") == []


def test_defect_t40_each_new_count_of_due_posts_is_another_open_alert(org):
    """Production: open warnings "1 approved post(s)", "2", "3", "3", "4": one condition, five alerts, contrary to
    OPERATIONS.md ("kept as one alert per condition")."""
    add_post(org)
    AGENTS["publishing"](org).run({})
    add_post(org)
    AGENTS["publishing"](org).run({})
    assert len(open_alerts(org, "publishing")) == 1


def test_defect_t41_publishing_warning_stays_after_the_condition_is_gone(org):
    pid = add_post(org)
    AGENTS["publishing"](org).run({})
    with org.db.session() as s:
        s.get(ContentItem, pid).status = "rejected"
    AGENTS["publishing"](org).run({})
    assert open_alerts(org, "publishing") == []


def test_defect_t42_a_reassignment_done_by_hand_stays_an_open_alert(org):
    """Five "Assigned lead #N from admin to sales1 ... (by admin)" notices: the owner did that himself, and they stay on
    the alert list until each is acknowledged, while the person it concerns (sales1) cannot see alerts at all."""
    lead_id = add_lead(org)
    client(org, "owner").post(f"/leads/{lead_id}/assign", data={"owner": "sam", "reason": "by hand"})
    assert open_alerts(org) == []


def test_ok_t43_condition_alerts_close_themselves(org):
    from aime.monitoring import alert_once, resolve_alerts
    with org.db.session() as s:
        alert_once(s, "warning", "doctor", "check: bad", org.settings, key="doctor:check")
    assert open_alerts(org) == ["check: bad"]
    with org.db.session() as s:
        resolve_alerts(s, ("doctor:",), set())
    assert open_alerts(org) == []


def test_ok_t44_sidebar_counts_agree_with_the_pages(org):
    lead = add_lead(org, owner="sam", outreach="contacted")
    add_email(org, lead, status="awaiting_approval")
    add_email(org, add_lead(org, owner="kim"), status="awaiting_approval")
    add_reply(org, lead, "question", status="needs_review")
    add_post(org, status="awaiting_approval")
    c = client(org, "owner")
    html = c.get("/manager").text
    assert re.search(r'href="/approvals".*?(\d+)\s*</span>', html, re.S).group(1) == "3"
    assert "3 need you" in words(html) and "1 LinkedIn post" in words(html)
    assert words(c.get("/approvals").text).count("Review") >= 3


# ------------------------------------------------------------------ independent totals on a realistic mix
def test_ok_t45_totals_match_independent_counts_on_a_mixed_book(org):
    """The figures of the home page, the funnel and /system against counts made here by hand from what was seeded."""
    leads = [add_lead(org, owner="sam", outreach="contacted") for _ in range(4)]
    add_lead(org, status="nurture")
    add_lead(org, status="disqualified")
    for lead in leads:
        add_email(org, lead)
    add_reply(org, leads[0], "interested")
    add_reply(org, leads[1], "not_interested")
    add_reply(org, leads[2], "bounce")
    add_reply(org, leads[3], "unsubscribe")
    add_meeting(org, leads[0])
    add_meeting(org, leads[1], status="cancelled")
    add_deal(org, leads[0], "won", 1000.0, closed=utcnow())
    add_deal(org, leads[1], "lost", closed=utcnow())
    add_deal(org, leads[2], "proposal", 400.0)
    with org.db.session() as s:
        funnel = dict((label, n) for label, n, _ in funnel_detail(s))
    assert funnel["Companies found"] == 6
    assert funnel["Outreach emails sent"] == 4
    assert funnel["Replies (real)"] == 2  # bounces and opt-outs are not replies
    assert funnel["Meetings booked"] == 1 and funnel["Deals won"] == 1 and funnel["Open opportunities"] == 1
    page = words(client(org, "owner").get("/system").text)
    assert "6 Companies" in page and "5 Open leads" in page and "1 Open opportunities" in page


# ------------------------------------------------------------------ wording, long lists, search
@pytest.mark.parametrize("path", ["/followups", "/manager", "/settings"])
def test_defect_t46_pages_show_raw_setting_names(org, path):
    """"Outbound email: DISABLED DISABLED: OUTBOUND_EMAIL_ENABLED=false", "...while REQUIRE_HUMAN_APPROVAL=true", "(A3)"."""
    add_email(org, add_lead(org), status="awaiting_approval")
    page = words(client(org, "owner").get(path).text)
    assert not re.findall(r"\b[A-Z]+(?:_[A-Z]+)+\b|\(A[1-4]\)", page)


def test_defect_t47_companies_past_300_cannot_be_reached(org):
    """Production has 298 and research adds up to 40 a day: "300 shown (latest 300)", no total, no next page."""
    with org.db.session() as s:
        for i in range(305):
            s.add(Company(name=f"Co {i:03d}", normalized_name=f"co {i:03d}", source="test"))
    html = client(org, "owner").get("/companies").text
    assert "305" in words(html) or "page=2" in html


def test_defect_t48_search_misses_a_person_by_full_name(org):
    add_lead(org, first="Priya", last="Nair")
    page = words(client(org, "owner").get("/search", params={"q": "Priya Nair"}).text)
    assert not page.startswith("0 results") and "0 results" not in page


def test_defect_t49_search_cannot_find_a_lead_by_its_number(org):
    """Alerts and pages speak of "lead #43"; searching that finds nothing."""
    lead_id = add_lead(org, "Numbered Co")
    assert f"/leads/{lead_id}" in client(org, "owner").get("/search", params={"q": f"lead #{lead_id}"}).text


def test_ok_t49_search_finds_company_domain_address_and_part_of_a_name(org):
    lead_id = add_lead(org, "Findable Widgets", first="Priya", last="Nair")
    c = client(org, "owner")
    assert f"/leads/{lead_id}" in c.get("/search", params={"q": "findable"}).text
    with org.db.session() as s:
        lead = s.get(Lead, lead_id)
        domain, email = lead.company.domain, lead.contact.email
    assert f"/leads/{lead_id}" in c.get("/search", params={"q": domain}).text
    assert email in c.get("/search", params={"q": email}).text
    assert "Priya" in words(c.get("/search", params={"q": "nair"}).text)
