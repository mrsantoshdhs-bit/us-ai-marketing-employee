"""A clear "no" ends the deal the automation opened: a "not interested" or unsubscribe reply closes the lead's early
opportunity as lost with the reason, so the pipeline does not keep counting it. An uncertain reading, or a deal a
person has taken to proposal or negotiation, stays open for a person to decide. An interested reply after a "no"
reopens the deal, and the Pipeline page records why a deal was lost. Fake LinkedIn, SMTP and IMAP servers only."""

from urllib.parse import unquote

from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.models import Activity, Company, Contact, InboundEmail, Lead, Opportunity
from aime.web.app import create_app
from tests.test_inbox import FakeIMAP, mail, run_inbox
from tests.test_pilot_test_mode import ANN, BEN, ORIGIN, first_email, pilot

OWNER = "owner-test@owner.example"


def opp_of(ctx, address):
    with ctx.db.session() as s:
        lead = s.execute(select(Lead).join(Contact, Lead.contact_id == Contact.id).where(Contact.email == address)).scalar_one()
        return s.execute(select(Opportunity).where(Opportunity.lead_id == lead.id)).scalar_one()


def answer(ctx, uid, address, text):
    FakeIMAP.messages[uid] = mail(f"Owner <{OWNER}>", "Re: your email", text,
                                  in_reply_to=first_email(ctx, address).message_id, msg_id=f"<r{uid}@owner.example>")
    run_inbox(ctx)
    with ctx.db.session() as s:
        return s.execute(select(InboundEmail).where(InboundEmail.message_id == f"<r{uid}@owner.example>")).scalar_one()


def test_a_clear_not_interested_closes_the_leads_open_opportunity_as_lost(make_ctx):
    ctx, _ = pilot(make_ctx)
    assert opp_of(ctx, ANN).stage == "new" and opp_of(ctx, BEN).stage == "new"  # opened for both inquiries
    row = answer(ctx, 1, ANN, "No thanks, we're all set.")
    assert row.category == "not_interested" and row.confidence == "high" and row.status == "actioned"
    assert "opportunity closed as lost (Not interested)" in row.action_note
    o = opp_of(ctx, ANN)
    assert (o.stage, o.probability, o.lost_reason, o.next_step) == ("lost", 0, "Not interested", None)
    assert o.closed_at is not None
    assert opp_of(ctx, BEN).stage == "new"  # another lead's deal is untouched
    with ctx.db.session() as s:
        assert s.execute(select(Activity).where(Activity.opportunity_id == o.id,
                                                Activity.subject == "Opportunity stage new → lost (Not interested)")).first()


def test_an_unsubscribe_closes_it_too(make_ctx):
    ctx, _ = pilot(make_ctx)
    row = answer(ctx, 1, ANN, "Please unsubscribe me.")
    assert row.category == "unsubscribe" and "opportunity closed as lost (Unsubscribed)" in row.action_note
    o = opp_of(ctx, ANN)
    assert (o.stage, o.lost_reason) == ("lost", "Unsubscribed")


def test_an_uncertain_no_leaves_the_deal_open_for_a_person(make_ctx):
    ctx, _ = pilot(make_ctx)
    row = answer(ctx, 1, ANN, "Interesting, but not for us.")
    assert (row.category, row.confidence, row.status) == ("not_interested", "low", "needs_review")
    o = opp_of(ctx, ANN)
    assert o.stage == "new" and o.closed_at is None and "Close it as lost or keep it open" in o.next_step
    assert "a person decides" in row.action_note


def test_a_deal_a_person_took_to_proposal_is_theirs_to_close(make_ctx):
    ctx, _ = pilot(make_ctx)
    with ctx.db.session() as s:
        s.get(Opportunity, opp_of(ctx, ANN).id).stage = "proposal"
    answer(ctx, 1, ANN, "No thanks, we're all set.")
    o = opp_of(ctx, ANN)
    assert o.stage == "proposal" and o.lost_reason is None and "not interested" in o.next_step


def test_an_interested_reply_after_a_no_reopens_the_deal(make_ctx):
    ctx, _ = pilot(make_ctx)
    answer(ctx, 1, ANN, "No thanks, we're all set.")
    assert opp_of(ctx, ANN).stage == "lost"
    answer(ctx, 2, ANN, "Actually we changed our minds. Can we talk Thursday?")
    o = opp_of(ctx, ANN)
    assert (o.stage, o.probability, o.closed_at, o.lost_reason) == ("discovery", 20, None, None)
    with ctx.db.session() as s:
        assert s.execute(select(Activity).where(Activity.opportunity_id == o.id,
                                                Activity.subject.like("%reopened by an interested reply%"))).first()


def test_the_pipeline_page_records_why_a_deal_was_lost_and_clears_it_when_reopened(ctx):
    with ctx.db.session() as s:
        co = Company(name="Co", normalized_name="co", source="test")
        s.add(co)
        s.flush()
        o = Opportunity(company_id=co.id, name="Deal", stage="discovery")
        s.add(o)
        s.flush()
        oid = o.id
    c = TestClient(create_app(ctx))
    r = c.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "lost", "lost_reason": "  Chose a  competitor "},
               follow_redirects=False)
    assert "Opportunity updated" in unquote(r.headers["location"])
    with ctx.db.session() as s:
        o = s.get(Opportunity, oid)
        assert (o.stage, o.lost_reason, o.probability) == ("lost", "Chose a competitor", 0) and o.closed_at
        closed = o.closed_at
        assert s.execute(select(Activity).where(Activity.opportunity_id == oid,
                                                Activity.subject == "Opportunity stage discovery → lost (Chose a competitor)")).first()
    assert "Lost: Chose a competitor" in c.get("/pipeline").text
    c.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "lost", "next_step": "Ask again in Q3"})
    with ctx.db.session() as s:  # saving a lost deal again keeps its reason and the day it was closed
        o = s.get(Opportunity, oid)
        assert (o.lost_reason, o.closed_at, o.next_step) == ("Chose a competitor", closed, "Ask again in Q3")
    c.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "discovery"})
    with ctx.db.session() as s:
        o = s.get(Opportunity, oid)
        assert (o.stage, o.lost_reason, o.closed_at, o.probability) == ("discovery", None, None, 20)
    c.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "lost"})
    with ctx.db.session() as s:
        assert s.get(Opportunity, oid).lost_reason == "No reason given"
