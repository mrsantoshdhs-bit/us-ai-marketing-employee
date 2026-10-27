"""Opt-out replies in everyday wording: recognised, suppressed at once, and nothing more is sent (CAN-SPAM)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.models import Contact, FollowUp, InboundEmail, Suppression
from aime.reply_intel import classify
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID, FakeIMAP, contacted, latest_reply, mail, run_inbox
from tests.test_outreach import approve_all, lead_of


def _classify(text):
    return classify(InboundEmail(from_email="x@y.example", subject="Re: hi", body_text=text, new_text=text))


@pytest.mark.parametrize("text", [
    "Remove my email from your list.", "Please take my email off your list", "Take us off your mailing list.",
    "Please remove us from your mailing list", "Delete me from your list", "Remove me", "Unsubscribe", "unsub",
    "Unsubcribe", "unsuscribe me", "un-subscribe", "No more emails please.", "Stop the emails.", "Do not email me again.",
    "Please don't contact us.", "Never contact me again", "Opt me out", "Leave me alone.",
])
def test_opt_outs_in_everyday_wording(text):
    assert _classify(text)[:2] == ("unsubscribe", "high"), _classify(text)


@pytest.mark.parametrize("text", [
    "Can we talk Thursday?", "Stop by our booth next week, let's talk.", "Our list of vendors is full right now.",
    "Can you remove the second paragraph?", "Please add my colleague to the thread.", "It was an unsuspecting mistake.",
    "Those are unsubstantiated claims.",
])
def test_ordinary_replies_are_not_opt_outs(text):
    assert _classify(text)[0] != "unsubscribe", _classify(text)


def test_an_opt_out_suppresses_the_address_cancels_the_approved_next_email_and_nothing_more_is_sent(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    later = TUESDAY_11AM_ET + timedelta(days=6)
    assert SalesWorkflowAgent(ctx, sender=sender).run({"now": later.isoformat()}).output.outreach_drafts == 1
    approve_all(ctx)  # step 2 is approved and would go out on the next run
    FakeIMAP.messages = {1: mail(DAVID, f"Re: {sent.subject}", "Remove my email from your list.", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    r = latest_reply(ctx)
    assert (r.category, r.confidence, r.status) == ("unsubscribe", "high", "actioned")
    before = len(FakeSMTP.sent)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (later + timedelta(hours=1)).isoformat()})
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (later + timedelta(days=7)).isoformat()})
    assert len(FakeSMTP.sent) == before  # nothing after the opt-out
    with ctx.db.session() as s:
        assert s.execute(select(Suppression).where(Suppression.value == DAVID)).scalar_one()
        assert s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one().consent_status == "opted_out"
        step2 = s.execute(select(FollowUp).where(FollowUp.step == 2, FollowUp.kind == "outreach")).scalar_one()
        assert step2.status == "cancelled"
        assert not s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status.in_(
            ("pending", "awaiting_approval", "approved")))).first()
    assert lead_of(ctx).outreach_status == "unsubscribed"


def test_an_opt_out_from_another_address_in_the_thread_also_protects_the_address_we_wrote_to(make_ctx):
    from fastapi.testclient import TestClient

    from aime.web.app import create_app
    ctx, sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail("assistant@elsewhere.example", f"Re: {sent.subject}", "Please take us off your mailing list.",
                                 in_reply_to=sent.message_id)}
    run_inbox(ctx)
    with ctx.db.session() as s:
        values = {x.value for x in s.execute(select(Suppression)).scalars()}
        contact = s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one()
        f = FollowUp(lead_id=lead_of(ctx).id, contact_id=contact.id, company_id=contact.company_id, channel="email",
                     kind="outreach", step=2, status="awaiting_approval", subject="Following up", body=sent.body,
                     to_email=DAVID, evidence=sent.evidence, quality=sent.quality)
        s.add(f)
        s.commit()
        fid = f.id
    assert {"assistant@elsewhere.example", DAVID} <= values and contact.consent_status == "opted_out"
    TestClient(create_app(ctx)).post(f"/followups/{fid}/approve", headers={"origin": "http://testserver"})
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "awaiting_approval"  # approval refused: do-not-contact


def test_approval_is_refused_for_a_contact_who_opted_out_without_a_suppression_entry(make_ctx):
    from fastapi.testclient import TestClient

    from aime.web.app import create_app
    ctx, sender, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        contact = s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one()
        contact.consent_status = "opted_out"  # e.g. recorded by hand after a phone call
        f = FollowUp(lead_id=lead_of(ctx).id, contact_id=contact.id, company_id=contact.company_id, channel="email",
                     kind="outreach", step=2, status="awaiting_approval", subject="Following up", body=sent.body,
                     to_email=DAVID, evidence=sent.evidence, quality=sent.quality)
        s.add(f)
        s.commit()
        fid = f.id
    TestClient(create_app(ctx)).post(f"/followups/{fid}/approve", headers={"origin": "http://testserver"})
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "awaiting_approval"
