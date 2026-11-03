"""Phase 6: email safety. Each test states the safe outcome. Synthetic data only."""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.agents.company_research import CompanyResearchAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.crm_ops import canonical_address, erase_person, lift_suppression, merge_companies, suppress
from aime.models import Alert, AuditEvent, Company, Contact, FollowUp, Lead, Opportunity, Suppression
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID, FakeIMAP, contacted, mail, run_inbox
from tests.test_outreach import lead_of
from tests.uat.test_p5_round2_web_truth import HTML, SIGN_IN, sign_in_page
from tests.uat.test_p5_suppression_merge import ORIGIN, SENDING, add_lead, approve_all, work


@pytest.fixture(autouse=True)
def _clean():
    FakeSMTP.sent = []
    FakeIMAP.messages = {}


def emails(ctx):
    with ctx.db.session() as s:
        rows = s.execute(select(FollowUp).where(FollowUp.channel == "email").order_by(FollowUp.id)).scalars().all()
        s.expunge_all()
    return rows


def to_list():
    return [m["To"] for m in FakeSMTP.sent]


def lift(ctx, value, reason="owner decided"):
    with ctx.db.session() as s:
        return lift_suppression(s, value, "owner", reason)


# ---- a lift and an approval given before the entry
def test_an_approval_to_another_host_comes_back_to_a_person_when_the_domain_entry_is_lifted(make_ctx):
    ctx = make_ctx(**SENDING)
    add_lead(ctx, "a", "pat@other.example", "closed.example")
    work(ctx)
    approve_all(ctx)
    TestClient(create_app(ctx)).post("/suppress", headers=ORIGIN, data={"value": "closed.example", "reason": "no"}, follow_redirects=False)
    assert emails(ctx)[0].status == "approved"
    assert lift(ctx, "closed.example") is None
    work(ctx)
    f = emails(ctx)[0]
    assert FakeSMTP.sent == [] and f.status == "awaiting_approval" and f.approved_at is None and "approved before" in f.blocked_reason


def test_a_lift_leaves_an_approval_nothing_covered(make_ctx):
    ctx = make_ctx(**SENDING)
    add_lead(ctx, "a", "pat@other.example", "other.example")
    work(ctx)
    approve_all(ctx)
    with ctx.db.session() as s:
        suppress(s, "elsewhere.example", "no")
    assert lift(ctx, "elsewhere.example") is None
    assert emails(ctx)[0].status == "approved"


def test_same_host_is_cancelled_and_stays_cancelled(make_ctx):
    ctx = make_ctx(**SENDING)
    add_lead(ctx, "a", "pat@closed.example", "closed.example")
    work(ctx)
    approve_all(ctx)
    TestClient(create_app(ctx)).post("/suppress", headers=ORIGIN, data={"value": "closed.example", "reason": "no"}, follow_redirects=False)
    lift(ctx, "closed.example")
    work(ctx)
    assert FakeSMTP.sent == [] and emails(ctx)[0].status == "cancelled"


# ---- a reply to someone who opted out
def _approved_response(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    FakeSMTP.sent = []
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds interesting. Could we talk Thursday afternoon?", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    client = TestClient(create_app(ctx))
    r = [f for f in emails(ctx) if f.kind == "response"][0]
    client.post(f"/followups/{r.id}/approve", headers=ORIGIN, follow_redirects=False)
    assert [f for f in emails(ctx) if f.id == r.id][0].status == "approved"
    return ctx, sender, client, r.id


def run_sender(ctx, sender):
    return SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})


def test_the_gate_stops_a_reply_to_an_opted_out_contact_with_no_entry(make_ctx):
    ctx, sender, _client, fid = _approved_response(make_ctx)
    with ctx.db.session() as s:
        c = s.execute(select(Contact).where(Contact.email == DAVID)).scalars().one()
        c.consent_status, c.consent_at = "opted_out", utcnow()
    run_sender(ctx, sender)
    assert FakeSMTP.sent == [] and [f for f in emails(ctx) if f.id == fid][0].status == "cancelled"


def test_uncertain_then_domain_entry_then_lift_then_requeue_sends_nothing(make_ctx):
    ctx, sender, client, fid = _approved_response(make_ctx)
    with ctx.db.session() as s:
        s.get(FollowUp, fid).status = "uncertain"
    host = DAVID.split("@")[1]
    client.post("/suppress", headers=ORIGIN, data={"value": host, "reason": "no"}, follow_redirects=False)
    assert lift(ctx, host) is None
    with ctx.db.session() as s:
        assert s.execute(select(Contact).where(Contact.email == DAVID)).scalars().one().consent_status == "opted_out"
    assert client.post(f"/followups/{fid}/requeue", headers=ORIGIN, follow_redirects=False).status_code == 303
    run_sender(ctx, sender)
    assert FakeSMTP.sent == []


def test_a_reply_to_someone_who_has_not_opted_out_still_goes(make_ctx):
    ctx, sender, _client, _fid = _approved_response(make_ctx)
    run_sender(ctx, sender)
    assert to_list() == [DAVID]


# ---- one person, two spellings
def test_a_plus_tag_spelling_gets_no_second_first_email(make_ctx):
    ctx = make_ctx(**SENDING)
    assert canonical_address("pat+news@twin.example") == "pat@twin.example"
    add_lead(ctx, "a", "pat@twin.example", "twin.example")
    work(ctx)
    approve_all(ctx)
    work(ctx)
    add_lead(ctx, "b", "pat+news@twin.example", "twin-b.example")
    work(ctx, TUESDAY_11AM_ET + timedelta(days=1))
    approve_all(ctx)
    work(ctx, TUESDAY_11AM_ET + timedelta(days=1, minutes=1))
    assert to_list() == ["pat@twin.example"]


def test_the_send_gate_returns_a_draft_for_the_other_spelling_to_a_person(make_ctx):
    """Both open before either went: the older goes, the newer waits for a person."""
    ctx = make_ctx(**SENDING)
    add_lead(ctx, "a", "pat@twin.example", "twin.example")
    add_lead(ctx, "b", "pat+news@twin.example", "twin-b.example")
    work(ctx)
    approve_all(ctx)
    work(ctx)
    work(ctx, TUESDAY_11AM_ET + timedelta(days=1))
    assert to_list() == ["pat@twin.example"]
    assert emails(ctx)[1].status == "awaiting_approval"


# ---- a merge with an open first email
def _dup_sent_then_keeper_open(ctx):
    add_lead(ctx, "a", "bob@dup.example", "dup.example")
    work(ctx)
    approve_all(ctx)
    work(ctx)
    assert to_list() == ["bob@dup.example"]
    keeper_lead = add_lead(ctx, "b", "amy@keeper.example", "keeper.example")
    work(ctx, TUESDAY_11AM_ET + timedelta(minutes=1))
    return keeper_lead


def _merge(ctx, keeper_lead):
    with ctx.db.session() as s:
        keeper = s.get(Lead, keeper_lead).company
        dup = s.execute(select(Company).where(Company.domain == "dup.example")).scalars().one()
        merge_companies(s, keeper, dup, "crm")


def _is_back_with_a_person(ctx):
    f = [x for x in emails(ctx) if x.to_email == "amy@keeper.example"][0]
    assert f.status == "awaiting_approval" and f.approved_at is None and "already emailed" in f.blocked_reason
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.message.like("%went back to a person%"))).scalars().first()
    return f


def test_an_open_first_email_at_the_keeper_goes_back_to_a_person_on_the_merge(make_ctx):
    ctx = make_ctx(**SENDING)
    _merge(ctx, _dup_sent_then_keeper_open(ctx))
    _is_back_with_a_person(ctx)
    work(ctx, TUESDAY_11AM_ET + timedelta(minutes=2))
    assert to_list() == ["bob@dup.example"]


def test_an_approved_first_email_at_the_keeper_loses_its_approval_on_the_merge(make_ctx):
    ctx = make_ctx(**SENDING)
    k = _dup_sent_then_keeper_open(ctx)
    approve_all(ctx)
    _merge(ctx, k)
    _is_back_with_a_person(ctx)
    work(ctx, TUESDAY_11AM_ET + timedelta(minutes=2))
    assert to_list() == ["bob@dup.example"]


def test_a_merge_with_nothing_sent_leaves_an_approval_alone(make_ctx):
    ctx = make_ctx(**SENDING)
    add_lead(ctx, "a", "bob@dup.example", "dup.example")
    k = add_lead(ctx, "b", "amy@keeper.example", "keeper.example")
    with ctx.db.session() as s:  # the duplicate has no email at all
        s.execute(select(Lead).where(Lead.id != k)).scalars().one().status = "nurture"
    work(ctx)
    approve_all(ctx)
    _merge(ctx, k)
    assert [f.status for f in emails(ctx) if f.to_email == "amy@keeper.example"] == ["approved"]


# ---- someone else's opt-out after a merge
def _survivor_with_a_deal(ctx, sent):
    dup_co = lead_of(ctx).company_id
    with ctx.db.session() as s:
        keeper = Company(name="Keeper Co", normalized_name="keeper co", source="test", domain="keeper.example", state="TX", country="US")
        s.add(keeper)
        s.flush()
        amy = Contact(company_id=keeper.id, email="amy@keeper.example", source="inbound", consent_status="inquiry")
        s.add(amy)
        s.flush()
        k = Lead(company_id=keeper.id, contact_id=amy.id, source="research", status="qualified", score=70, grade="B", primary_service="bpa")
        s.add(k)
        s.flush()
        opp = Opportunity(lead_id=k.id, company_id=keeper.id, name="The survivor's deal", stage="new", probability=10)
        s.add(opp)
        s.flush()
        merge_companies(s, keeper, s.get(Company, dup_co), "crm")
        return opp.id


def test_after_a_merge_someone_elses_opt_out_leaves_the_survivors_deal_for_a_person(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    opp_id = _survivor_with_a_deal(ctx, sent)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Please unsubscribe me. Remove me from your list.", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    with ctx.db.session() as s:
        o = s.get(Opportunity, opp_id)
        assert o.stage != "lost" and "Close it as lost or keep it open" in o.next_step
        assert s.get(Lead, sent.lead_id).outreach_status == "unsubscribed"  # the sequence stops all the same
        assert s.execute(select(Suppression).where(Suppression.value == DAVID)).scalars().first()


def test_without_a_merge_their_own_opt_out_still_closes_their_deal(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, sent.lead_id)
        opp = Opportunity(lead_id=lead.id, company_id=lead.company_id, name="Their deal", stage="new", probability=10)
        s.add(opp)
        s.flush()
        opp_id = opp.id
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Please unsubscribe me. Remove me from your list.", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    with ctx.db.session() as s:
        assert s.get(Opportunity, opp_id).stage == "lost"


# ---- a portal that answers for the company's site
def _researched_contact(ctx):
    with ctx.db.session() as s:
        co = Company(name="Firm Alpha", normalized_name="firm alpha", source="test", domain="firm.example", website="https://firm.example", website_status="verified")
        s.add(co)
        s.flush()
        s.add(Contact(company_id=co.id, email="jane@firm.example", first_name="Jane", last_name="Rivers", title="CEO", source="website",
                      evidence_url="https://firm.example/", email_source_url="https://firm.example/", consent_status="none"))
        s.flush()
        return co.id


def _read(ctx, cid):
    res = CompanyResearchAgent(ctx).run({"company_ids": [cid], "limit": 1})
    assert res.ok, res.error
    with ctx.db.session() as s:
        return s.execute(select(Contact).where(Contact.company_id == cid)).scalars().one().source, s.get(Company, cid).is_competitor


def https_page(title, extra=""):
    page = SIGN_IN.replace("Welcome to Harbour Guest Wi-Fi", title)

    def serve(request):
        return httpx.Response(200, text=page % extra, headers=HTML, request=request)
    return serve


def test_a_plain_http_portal_withdraws_nothing(make_ctx):
    ctx = make_ctx(handler=sign_in_page(), **SENDING)
    assert _read(ctx, _researched_contact(ctx)) == ("website", False)


def test_an_https_portal_withdraws_nobody(make_ctx):
    ctx = make_ctx(handler=https_page("Welcome to Harbour Guest Wi-Fi"), **SENDING)
    assert _read(ctx, _researched_contact(ctx))[0] == "website"


def test_the_companys_own_page_still_withdraws_someone_who_left(make_ctx):
    ctx = make_ctx(handler=https_page("Firm Alpha"), **SENDING)
    assert _read(ctx, _researched_contact(ctx))[0] == "website_withdrawn"


def test_an_https_portal_sets_no_competitor_flag(make_ctx):
    ctx = make_ctx(handler=https_page("Welcome to Harbour Guest Wi-Fi", "<p>We are a software development agency building custom software for clients.</p>"), **SENDING)
    assert _read(ctx, _researched_contact(ctx))[1] is False


# ---- what an address leaves behind
def test_suppress_keeps_the_address_out_of_the_redirect(make_ctx):
    ctx = make_ctx(**SENDING)
    r = TestClient(create_app(ctx)).post("/suppress", headers=ORIGIN, data={"value": "pat@x.example", "reason": "no"}, follow_redirects=False)
    assert "pat" not in r.headers["location"] and "x.example" not in r.headers["location"]
    with ctx.db.session() as s:
        assert s.execute(select(Suppression).where(Suppression.value == "pat@x.example")).scalars().one()


def test_an_address_typed_in_a_lift_reason_is_gone_after_erasure(make_ctx):
    ctx = make_ctx(**SENDING)
    with ctx.db.session() as s:
        suppress(s, "firm.example", "no")
    lift(ctx, "firm.example", "Pat@Firm.example asked us to write")
    with ctx.db.session() as s:
        erase_person(s, "pat@firm.example", "owner")
    with ctx.db.session() as s:
        left = [e.detail for e in s.execute(select(AuditEvent)).scalars() if "pat@firm.example" in (e.detail or "").lower()]
        r = s.execute(select(Suppression).where(Suppression.value == "firm.example")).scalars().one()
        assert s.execute(select(Suppression).where(Suppression.value == "pat@firm.example")).scalars().one()  # the list keeps the address itself
    assert not left and "pat@" not in r.lift_reason.lower() and r.lift_reason == "[erased] asked us to write"


def test_the_approval_audit_line_never_holds_the_address(make_ctx):
    ctx = make_ctx(**SENDING)
    add_lead(ctx, "a", "pat@firm.example", "firm.example")
    work(ctx)
    approve_all(ctx)
    with ctx.db.session() as s:
        [event] = s.execute(select(AuditEvent).where(AuditEvent.action == "email_approve")).scalars().all()
        assert "pat@firm.example" not in event.detail and "address #" in event.detail


def test_an_email_with_no_contact_link_is_erased_by_its_address(make_ctx):
    ctx = make_ctx(**SENDING)
    lead_id = add_lead(ctx, "a", "pat@firm.example", "firm.example")
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(FollowUp(lead_id=lead_id, company_id=lead.company_id, channel="email", kind="response", step=1, due_at=utcnow(), status="sent",
                       subject="Re: hello", body="Hi Sam, private words", to_email="sam@firm.example"))
    with ctx.db.session() as s:
        erase_person(s, "sam@firm.example", "owner")
    assert not [f for f in emails(ctx) if f.to_email == "sam@firm.example" or "private" in (f.body or "")]
