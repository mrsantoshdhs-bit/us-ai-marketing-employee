"""Meetings from Calendly emails, pipeline automation, meetings dashboard, sales reporting."""

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aime.agents.analytics import AnalyticsReportingAgent
from aime.meetings import parse_calendly
from aime.models import Alert, FollowUp, InboundEmail, Lead, Meeting, Opportunity
from aime.pipeline import pipeline_checks
from aime.web.app import create_app
from tests.test_company_research import SITE
from tests.test_inbox import FakeIMAP, contacted, latest_reply, mail, run_inbox
from tests.test_outreach import lead_of

ORIGIN = {"origin": "http://testserver"}
DAVID = f"david.chen@{SITE}"
BOOKED_SUBJECT = "New Event: David Chen - 10:00am Thu, Oct 29, 2026 - 20 Minute Meeting"
BOOKED_BODY = (f"Hi Sales,\nA new event has been scheduled.\nEvent Type: 20 Minute Meeting\nInvitee: David Chen\n"
               f"Invitee Email: {DAVID}\nEvent Date/Time: 10:00am - 10:20am (Eastern Time - US & Canada) Thursday, "
               "October 29, 2026\nLocation: Google Meet\nView event in Calendly")


def calendly(subject=BOOKED_SUBJECT, body=BOOKED_BODY):
    return mail("Calendly <notifications@calendly.com>", subject, body)


def test_parse_calendly_converts_time_zones():
    info = parse_calendly(BOOKED_SUBJECT, BOOKED_BODY, {"sales@us.example"})
    assert info["kind"] == "booked" and info["invitee_email"] == DAVID and info["invitee_name"] == "David Chen"
    assert info["starts_at"] == datetime(2026, 10, 29, 14, 0) and info["ends_at"] == datetime(2026, 10, 29, 14, 20)  # EDT
    winter = parse_calendly("New Event: Ana - 9:30am Tue, Dec 1, 2026 - Intro",
                            "Invitee: Ana Lima Invitee Email: ana@x.example Event Date/Time: 9:30am - 10:00am "
                            "(Pacific Time - US & Canada) Tuesday, December 1, 2026", set())
    assert winter["starts_at"] == datetime(2026, 12, 1, 17, 30)  # PST, UTC-8
    assert parse_calendly("Re: hello", BOOKED_BODY, set()) is None
    assert parse_calendly(BOOKED_SUBJECT, "Invitee: David Chen (no time given)", set()) is None


def test_booking_and_cancellation_move_the_pipeline(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: calendly()}
    run_inbox(ctx)
    r = latest_reply(ctx)
    assert r.category == "calendly" and r.status == "actioned" and "meeting recorded" in r.action_note
    with ctx.db.session() as s:
        m = s.execute(select(Meeting)).scalar_one()
        assert m.status == "scheduled" and m.lead_id and m.source == "calendly_email" and not m.is_simulated
        opp = s.get(Opportunity, m.opportunity_id)
        assert opp.stage == "discovery" and opp.probability == 30 and "Meeting 2026-10-29" in opp.next_step
        assert s.execute(select(Alert).where(Alert.source == "meetings")).scalars().first()
    assert lead_of(ctx).outreach_status == "meeting"
    FakeIMAP.messages[2] = calendly(BOOKED_SUBJECT.replace("New Event", "Canceled"), BOOKED_BODY)
    run_inbox(ctx)
    with ctx.db.session() as s:
        m = s.execute(select(Meeting)).scalar_one()
        assert m.status == "cancelled" and "cancelled" in s.get(Opportunity, m.opportunity_id).next_step
    assert lead_of(ctx).outreach_status == "interested"


def test_unreadable_or_unmatched_bookings_go_to_review(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    FakeIMAP.messages = {1: calendly(body="A new event was scheduled. Open Calendly for details."),
                         2: calendly("New Event: Zoe - 11:00am Fri, Oct 30, 2026 - Intro",
                                     "Invitee: Zoe Park Invitee Email: zoe@elsewhere.example Event Date/Time: 11:00am - "
                                     "11:30am (Eastern Time - US & Canada) Friday, October 30, 2026")}
    run_inbox(ctx)
    with ctx.db.session() as s:
        rows = s.execute(select(InboundEmail).order_by(InboundEmail.id)).scalars().all()
        assert [r.status for r in rows] == ["needs_review", "needs_review"]
        assert "could not be read" in rows[0].action_note and "not matched" in rows[1].action_note
        assert s.execute(select(Meeting)).scalar_one().lead_id is None


def test_pipeline_automation_creates_each_task_once(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: calendly()}
    run_inbox(ctx)
    later = datetime(2026, 11, 20, 12, 0)
    with ctx.db.session() as s:
        stats = pipeline_checks(s, ctx.settings, later, "test")
        again = pipeline_checks(s, ctx.settings, later, "test")
        subjects = [f.subject for f in s.execute(select(FollowUp).where(FollowUp.channel == "task",
                                                                       FollowUp.status == "pending")).scalars()]
    assert stats["stalled_tasks"] == 1 and stats["meeting_tasks"] == 1 and again == {"stalled_tasks": 0, "no_response": 0,
                                                                                       "meeting_tasks": 0}
    assert any(s.startswith("Stalled:") for s in subjects) and any(s.startswith("Record the outcome") for s in subjects)


def test_no_response_after_the_whole_sequence(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        for step in (2, 3):
            s.add(FollowUp(lead_id=sent.lead_id, contact_id=sent.contact_id, company_id=sent.company_id, channel="email",
                           kind="outreach", step=step, status="sent", subject=f"step {step}", body="...",
                           to_email=DAVID, sent_at=datetime(2026, 11, 6, 15, 0), approved_at=datetime(2026, 11, 6, 14, 0)))
    with ctx.db.session() as s:
        assert pipeline_checks(s, ctx.settings, datetime(2026, 11, 10), "test")["no_response"] == 0  # still waiting
        assert pipeline_checks(s, ctx.settings, datetime(2026, 11, 14), "test")["no_response"] == 1
    lead = lead_of(ctx)
    assert lead.outreach_status == "no_response" and lead.status == "nurture"


def test_meetings_page_actions_manual_logging_and_stop_outreach(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    client = TestClient(create_app(ctx))
    lead = lead_of(ctx)
    client.post(f"/leads/{lead.id}/meeting", data={"date": "2026-11-03", "time_et": "15:00", "minutes": "30",
                                                   "title": "Intro call"}, headers=ORIGIN)
    with ctx.db.session() as s:
        m = s.execute(select(Meeting)).scalar_one()
        assert m.source == "manual" and m.starts_at == datetime(2026, 11, 3, 20, 0)  # 15:00 EST = 20:00 UTC
    assert "Intro call" in client.get("/meetings").text
    client.post(f"/meetings/{m.id}/held", data={"notes": "Wants a proposal"}, headers=ORIGIN)
    with ctx.db.session() as s:
        m = s.get(Meeting, m.id)
        assert m.status == "completed" and s.get(Opportunity, m.opportunity_id).probability == 40
    ctx2, _, _ = contacted(make_ctx)
    lead2 = lead_of(ctx2)
    with ctx2.db.session() as s:  # a follow-up step waiting for approval
        s.add(FollowUp(lead_id=lead2.id, channel="email", kind="outreach", step=2, status="awaiting_approval",
                       subject="x", body="y"))
    TestClient(create_app(ctx2)).post(f"/leads/{lead2.id}/stop_outreach", headers=ORIGIN)
    assert lead_of(ctx2).outreach_status == "stopped"
    with ctx2.db.session() as s:
        assert s.execute(select(func.count(FollowUp.id)).where(FollowUp.status == "awaiting_approval")).scalar_one() == 0


def test_reports_count_real_activity_and_keep_simulations_apart(make_ctx):
    from argparse import Namespace

    from aime.cli import cmd_simulate_reply
    ctx, _, _ = contacted(make_ctx)
    cmd_simulate_reply(Namespace(lead=lead_of(ctx).id, text="Yes, sounds interesting, let's talk.", from_email=None), ctx)
    res = AnalyticsReportingAgent(ctx).run({"kind": "daily"})
    assert res.ok, res.error
    k = res.output.kpis
    assert k["outreach_sent_total"] == 1 and k["replies_total"] == 0 and k["simulated_replies_total"] == 1
    with open(res.output.path_md, encoding="utf-8") as fh:
        md = fh.read()
    assert "Outreach sent (approved)" in md and "SIMULATED test activity" in md and "Reply rate" in md


def test_today_shows_meetings_and_sales_funnel(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    FakeIMAP.messages = {1: calendly()}
    run_inbox(ctx)
    page = TestClient(create_app(ctx)).get("/").text
    assert "Upcoming meetings" in page and "Meetings booked" in page and "Outreach emails sent" in page
    with ctx.db.session() as s:
        assert s.execute(select(Lead)).scalar_one().outreach_status == "meeting"


def test_today_separates_posts_waiting_for_approval_from_scheduled_ones(make_ctx):
    from aime.models import ContentItem
    ctx, _, _ = contacted(make_ctx)
    with ctx.db.session() as s:
        s.add(ContentItem(service="bpa", industry="logistics", title="Draft", body="Text", status="awaiting_approval",
                          generator="template", fingerprint="tile-test"))
    page = TestClient(create_app(ctx)).get("/").text
    assert "Posts awaiting approval" in page and "Posts scheduled" in page and "Emails awaiting approval" in page
