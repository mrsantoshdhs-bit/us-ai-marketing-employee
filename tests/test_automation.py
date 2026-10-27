"""Operating modes, campaign authorizations, stop switches, approvals given while sending was off, reply intents,
lead tiers, the Automation Control Center and the Sales Command Center."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import automation as am
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.integrations.email_smtp import EmailSender
from aime.models import AuditEvent, CampaignAuthorization, FollowUp, InboundEmail
from aime.reply_intel import QUESTION_PLACEHOLDER, classify, intent_of
from aime.scoring import lead_tier
from aime.web.app import create_app
from aime.web.charts import bar_list, column_chart
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID, FakeIMAP, contacted, latest_reply, mail, run_inbox
from tests.test_outreach import SENDING, drafts, researched

ORIGIN = {"origin": "http://testserver"}
NOW = TUESDAY_11AM_ET


def drafted(make_ctx, **overrides):
    """A researched lead with its first outreach email drafted and waiting for approval."""
    ctx = researched(make_ctx, **{**SENDING, **overrides})
    FakeSMTP.sent = []
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": NOW.isoformat()})
    [f] = [f for f in drafts(ctx) if f.kind == "outreach"]
    assert f.status == "awaiting_approval" and f.to_email == DAVID
    return ctx, sender, f.id


def authorize(ctx, recipients=DAVID, kinds=("outreach",), daily=5, total=20, activate=True):
    with ctx.db.session() as s:
        a = am.create_authorization(s, "admin", "Controlled test", recipients, list(kinds), daily, total, 7)
        a.starts_at, a.expires_at = NOW - timedelta(days=1), NOW + timedelta(days=6)  # around the simulated clock
        if activate:
            am.change_authorization(s, a, "activate", "admin")
        return a.id


def run(ctx, sender, now=NOW):
    return SalesWorkflowAgent(ctx, sender=sender).run({"now": now.isoformat()})


def followup(ctx, fid):
    with ctx.db.session() as s:
        return s.get(FollowUp, fid)


# --------------------------------------------------------------------------- modes and states

def test_modes_and_states_are_logged_and_guarded(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        assert (am.get_mode(s), am.get_state(s)) == ("hybrid", "running")
        am.set_mode(s, "approval", "admin")
        with pytest.raises(ValueError):
            am.set_mode(s, "everything", "admin")
        am.change_state(s, "pause", "manager")
        with pytest.raises(ValueError):
            am.change_state(s, "pause", "manager")  # already paused
        am.change_state(s, "resume", "admin")
        a = am.create_authorization(s, "admin", "Test", DAVID, ["outreach"], 1, 1, 1)
        am.change_authorization(s, a, "activate", "admin")
        am.change_state(s, "emergency_stop", "manager", "wrong list")
        assert am.get_state(s) == "emergency_stop" and a.status == "stopped"
        actions = [e.action for e in s.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars()]
    assert actions == ["mode_changed", "state_pause", "state_resume", "authorization_created", "authorization_activate",
                       "state_emergency_stop", "authorization_stopped"]


def test_authorizations_name_exact_people_within_limits(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        for recipients, kinds, err in [("*@company.com", ["outreach"], "exact"), ("company.com", ["outreach"], "exact"),
                                       ("", ["outreach"], "at least one"), (DAVID, [], "Choose"),
                                       (DAVID, ["outreach", "everything"], None)]:
            if err:
                with pytest.raises(ValueError, match=err):
                    am.create_authorization(s, "admin", "x", recipients, kinds, 5, 20, 7)
            else:
                a = am.create_authorization(s, "admin", "x", f"{DAVID.upper()}, {DAVID}", kinds, 5, 20, 7)
                assert a.recipients == [DAVID] and a.kinds == ["outreach"] and a.status == "draft"
        with pytest.raises(ValueError, match="Limits"):
            am.create_authorization(s, "admin", "x", DAVID, ["outreach"], 5, 20, 90)


# --------------------------------------------------------------------------- send-time approval

def test_hybrid_sends_authorized_outreach_without_a_person_and_records_it(make_ctx):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=False)
    authorize(ctx)
    run(ctx, sender)
    f = followup(ctx, fid)
    assert f.status == "sent" and f.approved_by.startswith("campaign #") and f.authorization_id
    assert [m["To"] for m in FakeSMTP.sent] == [DAVID]
    with ctx.db.session() as s:
        [e] = s.execute(select(AuditEvent).where(AuditEvent.action == "auto_approved")).scalars().all()
        assert DAVID in e.detail and "hybrid mode" in e.detail


@pytest.mark.parametrize("change", ["approval_mode", "human_approval_setting", "sending_off", "not_on_list", "draft_only",
                                    "paused_state", "sensitive_words", "limit"])
def test_nothing_goes_out_without_a_person_outside_an_authorization(make_ctx, change):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=change == "human_approval_setting",
                               outbound_email_enabled=change != "sending_off")
    aid = authorize(ctx, recipients="someone.else@example.com" if change == "not_on_list" else DAVID,
                    activate=change != "draft_only", daily=1)
    with ctx.db.session() as s:
        if change == "approval_mode":
            am.set_mode(s, "approval", "admin")
        if change == "paused_state":
            am.change_state(s, "pause", "admin")
        f = s.get(FollowUp, fid)
        if change == "sensitive_words":
            f.body = f.body.replace("\n\n", "\n\nOur pricing starts low.\n\n", 1)
        if change == "limit":  # the authorization already sent its one email today
            s.add(FollowUp(lead_id=f.lead_id, channel="email", kind="outreach", step=1, status="sent", subject="x",
                           to_email=DAVID, authorization_id=aid, sent_at=NOW - timedelta(hours=1), due_at=NOW))
    run(ctx, sender)
    f = followup(ctx, fid)
    assert f.status == "awaiting_approval" and f.authorization_id is None and FakeSMTP.sent == []


def test_switching_sending_on_does_not_release_a_backlog(make_ctx):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=False, outbound_email_enabled=False)
    authorize(ctx)
    run(ctx, sender)  # sending off: the campaign approves nothing ahead of time
    assert followup(ctx, fid).status == "awaiting_approval"


def test_a_campaign_approval_holds_only_while_the_campaign_does(make_ctx):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=False)
    aid = authorize(ctx)
    with ctx.db.session() as s:  # approved by the campaign at send time, then the campaign is paused before sending
        am.apply_campaign_approvals(s, ctx.settings, NOW, "test")
        am.change_authorization(s, s.get(CampaignAuthorization, aid), "pause", "admin")
    assert followup(ctx, fid).status == "approved"
    run(ctx, sender)
    f = followup(ctx, fid)
    assert f.status == "awaiting_approval" and "no longer active" in f.blocked_reason and FakeSMTP.sent == []


def test_automatic_mode_answers_a_clear_meeting_request_with_the_booking_link(make_ctx):
    ctx, sender, sent = contacted(make_ctx, calendly_url="https://calendly.com/owner-example/30min")
    ctx.settings.require_human_approval = False
    authorize(ctx, kinds=("outreach", "response"))
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds interesting. Can we talk Thursday?",
                                 in_reply_to=sent.message_id)}
    run_inbox(ctx)
    reply = latest_reply(ctx)
    assert (reply.category, reply.confidence, reply.intent) == ("interested", "high", "meeting_request")
    with ctx.db.session() as s:
        am.set_mode(s, "hybrid", "admin")
    later = NOW + timedelta(days=1)
    run(ctx, sender, later)
    assert followup(ctx, reply.suggested_followup_id).status == "awaiting_approval"  # Hybrid: replies need a person
    with ctx.db.session() as s:
        am.set_mode(s, "automatic", "admin")
    run(ctx, sender, later)
    f = followup(ctx, reply.suggested_followup_id)
    assert f.status == "sent" and f.approved_by.startswith("campaign") and "calendly.com" in FakeSMTP.sent[-1].get_content()


def test_a_pricing_question_always_waits_for_a_person(make_ctx):
    ctx, sender, sent = contacted(make_ctx, calendly_url="https://calendly.com/owner-example/30min")
    ctx.settings.require_human_approval = False
    authorize(ctx, kinds=("outreach", "response"))
    with ctx.db.session() as s:
        am.set_mode(s, "automatic", "admin")
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}",
                                 "Interested. How much does this cost, roughly?", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    reply = latest_reply(ctx)
    assert reply.intent == "pricing_request" and reply.status == "needs_review" and "pricing" in reply.action_note
    run(ctx, sender, NOW + timedelta(days=1))
    f = followup(ctx, reply.suggested_followup_id)
    assert f.status == "awaiting_approval" and QUESTION_PLACEHOLDER in f.body and "asking about pricing" in f.body


# --------------------------------------------------------------------------- stop switches and old approvals

def test_paused_or_stopped_automation_sends_nothing_and_stopped_jobs_wait(make_ctx):
    from aime.scheduler.runner import run_job
    ctx, sender, fid = drafted(make_ctx)
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        f.status, f.approved_by, f.approved_at = "approved", "manager", NOW
        am.change_state(s, "pause", "manager")
    run(ctx, sender)
    f = followup(ctx, fid)
    assert f.status == "approved" and "paused" in f.blocked_reason and FakeSMTP.sent == []
    with ctx.db.session() as s:
        am.change_state(s, "stop", "manager")
    assert run_job(ctx, "followups")["status"] == "skipped" and run_job(ctx, "monitor")["status"] != "skipped"


def test_approvals_given_while_sending_was_off_need_confirming_again(make_ctx, monkeypatch):
    ctx, sender, fid = drafted(make_ctx, outbound_email_enabled=False)
    client = TestClient(create_app(ctx))
    client.post(f"/followups/{fid}/approve", headers=ORIGIN)
    run(ctx, sender)  # sending off: recorded as off
    ctx.settings.outbound_email_enabled = True  # the owner switches sending on later
    run(ctx, sender)
    f = followup(ctx, fid)
    assert f.status == "approved" and "confirm it again" in f.blocked_reason and FakeSMTP.sent == []
    assert "Confirm again" in client.get("/followups").text
    monkeypatch.setattr(am, "utcnow", lambda: NOW + timedelta(minutes=1))  # confirmed after sending was switched on
    client.post(f"/followups/{fid}/reconfirm", headers=ORIGIN)
    run(ctx, sender, NOW + timedelta(minutes=5))
    assert followup(ctx, fid).status == "sent" and [m["To"] for m in FakeSMTP.sent] == [DAVID]


# --------------------------------------------------------------------------- pages

def test_control_center_changes_go_through_the_pages(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    page = client.get("/automation").text
    assert "Automation Control Center" in page and "REQUIRE_HUMAN_APPROVAL=true" in page
    client.post("/automation/mode", data={"mode": "approval"}, headers=ORIGIN)
    client.post("/automation/authorizations", headers=ORIGIN,
                data={"name": "Two test contacts", "recipients": f"{DAVID}\nana@x.example", "kinds": ["outreach"],
                      "daily_limit": "2", "total_limit": "4", "days": "3"})
    r = client.post("/automation/authorizations", headers=ORIGIN, follow_redirects=False,
                    data={"name": "Bad", "recipients": "*@x.example", "kinds": ["outreach"]})
    assert "Not%20an%20exact" in r.headers["location"]
    with ctx.db.session() as s:
        [a] = s.execute(select(CampaignAuthorization)).scalars().all()
        assert a.recipients == [DAVID, "ana@x.example"] and a.status == "draft"
    client.post(f"/automation/authorizations/{a.id}/activate", headers=ORIGIN)
    client.post("/automation/state/emergency_stop", headers=ORIGIN)
    with ctx.db.session() as s:
        assert am.get_mode(s) == "approval" and am.get_state(s) == "emergency_stop"
        assert s.get(CampaignAuthorization, a.id).status == "stopped"
    audit = client.get("/audit").text
    assert "authorization created" in audit and "state emergency stop" in audit


def test_admin_only_routes_cover_permission_changes_but_not_stopping():
    from aime.web.auth import admin_only
    for path in ("/automation/mode", "/automation/authorizations", "/automation/authorizations/1/activate",
                 "/automation/state/start", "/automation/state/resume"):
        assert admin_only("POST", path), path
    for path in ("/automation/state/pause", "/automation/state/stop", "/automation/state/emergency_stop"):
        assert not admin_only("POST", path), path


def test_command_center_shows_real_counts_and_honest_empty_charts(make_ctx):
    ctx = make_ctx()
    page = TestClient(create_app(ctx)).get("/").text
    assert "Sales Command Center" in page and "No leads created in the last 8 weeks" in page
    assert "Needs you" in page and "Upcoming meetings" in page and "Automation status" in page
    for text in ('href="/automation"', 'href="/audit"', 'class="sidebar"'):
        assert text in page


# --------------------------------------------------------------------------- intents, tiers, charts

@pytest.mark.parametrize("text,intent", [("Can we schedule a call next week?", "meeting_request"),
                                         ("What would this cost us?", "pricing_request"),
                                         ("Interested - could you send me more information?", "more_info"),
                                         ("Thanks, not for us.", None)])
def test_reply_intents(text, intent):
    row = InboundEmail(from_email="a@b.example", subject="Re: hello", body_text=text, new_text=text)
    row.category, row.confidence, _ = classify(row)
    assert intent_of(row) == intent


def test_lead_tiers_explain_engagement_and_grade():
    lead = lambda **kw: SimpleNamespace(**{"status": "qualified", "outreach_status": None, "grade": "B", "source": "research",  # noqa: E731
                                           "score": 60, **kw})
    assert lead_tier(lead(outreach_status="meeting", grade="C")) == "HOT"
    assert lead_tier(lead(grade="A")) == "HOT" and lead_tier(lead(source="inbound")) == "HOT"
    assert lead_tier(lead()) == "WARM" and lead_tier(lead(grade="C")) == "COLD"
    assert lead_tier(lead(grade="D")) == lead_tier(lead(status="disqualified", grade="A")) == "UNQUALIFIED"


def test_charts_escape_and_never_draw_empty_data():
    assert "No data <yet>" not in column_chart(["W1"], [("Leads", [0])], "t", "No data <yet>")
    assert "No data &lt;yet&gt;" in column_chart(["W1"], [("Leads", [0])], "t", "No data <yet>")
    html = bar_list([("<b>Won</b>", 2, "/p?a=1&b=2")], "Funnel", "empty")
    assert "&lt;b&gt;Won" in html and "/p?a=1&amp;b=2" in html and "width:100.0%" in html
