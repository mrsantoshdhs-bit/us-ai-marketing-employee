"""End to end, with a mock contact at a reserved .example domain and simulated mail (fake SMTP and IMAP servers;
nothing leaves the test): discovery keeps its US evidence, a researched lead's outreach waits until a rule-based
authorization covers it, goes out under the rule, a pricing reply and a meeting request come back, Manager Today
shows only what needs a person, and the stop switches hold everything."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import automation as am
from aime.crm_ops import upsert_company
from aime.models import AuditEvent, CampaignAuthorization, FollowUp
from aime.scheduler.runner import run_job
from aime.sources.base import CompanyCandidate
from aime.web.app import create_app, manager_today
from tests.test_automation import NOW, drafted, followup, run
from tests.test_inbound_sales import FakeSMTP
from tests.test_inbox import DAVID, MAIL, FakeIMAP, latest_reply, mail, run_inbox
from tests.test_rule_authorizations import rule, shape


def needs(ctx):
    with ctx.db.session() as s:
        return [(n["kind"], n["who"], n["why"]) for n in manager_today(ctx, s)["needs"]]


def test_a_lead_goes_from_discovery_to_a_meeting_request_with_people_only_for_exceptions(make_ctx):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=False, **MAIL)
    FakeIMAP.messages = {}

    # discovery: where a company's US location comes from is kept with it
    with ctx.db.session() as s:
        co, _ = upsert_company(s, CompanyCandidate(name="Filed Example Co", source="sec_edgar", source_url="https://sec.example/f",
                                                   domain="filed-co.example", state="DE", is_us=True))
        assert co.location_evidence == "US state DE (SEC filing business address)"

    # the drafted outreach waits for a person, and Manager Today says why
    assert followup(ctx, fid).status == "awaiting_approval" and FakeSMTP.sent == []
    [(kind, who, why)] = [n for n in needs(ctx) if n[0] == "Email"]
    assert who == DAVID and why

    # a rule-based authorization does nothing while it is a draft
    shape(ctx, fid)  # qualified, grade B, verified website, the company's published address
    aid = rule(ctx, activate=False)
    run(ctx, sender)
    assert followup(ctx, fid).status == "awaiting_approval" and FakeSMTP.sent == []

    # once an administrator activates it, the matching outreach goes out under the rule (to the fake server)
    with ctx.db.session() as s:
        am.change_authorization(s, s.get(CampaignAuthorization, aid), "activate", "admin")
    run(ctx, sender)
    sent = followup(ctx, fid)
    assert sent.status == "sent" and sent.approved_by == f"campaign #{aid}"
    assert [m["To"] for m in FakeSMTP.sent] == [DAVID] and not [n for n in needs(ctx) if n[0] == "Email"]

    # a pricing reply always waits for a person, and its answer is not covered by an outreach rule
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Interested. How much does this cost?",
                                 in_reply_to=sent.message_id)}
    run_inbox(ctx)
    pricing = latest_reply(ctx)
    assert pricing.intent == "pricing_request" and pricing.status == "needs_review"
    assert any(k == "Reply" and "pricing" in w for k, _who, w in needs(ctx))
    run(ctx, sender)
    assert followup(ctx, pricing.suggested_followup_id).status == "awaiting_approval" and len(FakeSMTP.sent) == 1

    # a meeting request is listed for the manager until a meeting is booked
    FakeIMAP.messages[2] = mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds good. Can we talk Thursday?",
                                in_reply_to=sent.message_id, msg_id="<thursday@mail.example>")
    run_inbox(ctx)
    request = latest_reply(ctx)
    assert (request.category, request.intent) == ("interested", "meeting_request")
    with ctx.db.session() as s:
        view = manager_today(ctx, s)
        assert [r.id for r in view["requests"]] == [request.id] and request.id in [r.id for r in view["interested"]]
        assert view["health"]["authorizations"] == 1
    page = TestClient(create_app(ctx)).get("/manager").text
    assert "<b>1</b><span>meeting requests not yet booked</span>" in page and "No open meeting requests" not in page

    # pause holds sending; an emergency stop stops jobs and every campaign authorization
    with ctx.db.session() as s:
        f = s.get(FollowUp, pricing.suggested_followup_id)
        f.status, f.approved_by, f.approved_at = "approved", "manager", NOW
        am.change_state(s, "pause", "manager")
    run(ctx, sender)
    assert followup(ctx, pricing.suggested_followup_id).status == "approved" and len(FakeSMTP.sent) == 1
    with ctx.db.session() as s:
        am.change_state(s, "emergency_stop", "manager", "end of test")
    assert run_job(ctx, "followups")["status"] == "skipped"
    with ctx.db.session() as s:
        assert s.get(CampaignAuthorization, aid).status == "stopped"
        actions = [e.action for e in s.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars()]
    for action in ("authorization_created", "authorization_activate", "auto_approved"):
        assert action in actions, action
