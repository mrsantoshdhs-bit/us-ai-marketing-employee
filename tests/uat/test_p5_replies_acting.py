"""Phase 5: what a reply does once it is read. Each test states what a person would expect. Synthetic data only.

Never a real email: FakeSMTP and FakeIMAP. Every gate here only tightens: a person approves every email, and nothing is lost of an opt-out."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.agents.crm import CRMManagementAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.crm_ops import is_suppressed, merge_companies, suppress
from aime.inbound import record_inquiry
from aime.meetings import next_meeting
from aime.models import Activity, Alert, Company, Contact, FollowUp, InboundEmail, Lead, Meeting, Opportunity
from aime.outreach import reopen_refusal
from aime.pipeline import ASKS_FOR_APPROVAL, READ_THEIR_REPLY, settle_next_steps
from aime.reply_intel import BOOK_A_MEETING, OPTED_OUT_WROTE_AGAIN, QUESTION_PLACEHOLDER, booking_links_without_notice, lead_on_hold, process_pending
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.test_company_research import SITE
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID, FakeIMAP, bounce_for, contacted, latest_reply, mail, run_inbox
from tests.test_outreach import lead_of
from tests.uat.test_p5_suppression_merge import _keeper_with_contact

ORIGIN = {"origin": "http://testserver"}
SAM = f"sam@{SITE}"
HOME = "jordan.synth@home.example"
A = ("8:30pm", "Tuesday, 8 December 2026", "Tue, 8 Dec 2026")
_n = [100]


def web(ctx):
    return TestClient(create_app(ctx))


def deliver(ctx, *raws):
    start = max(FakeIMAP.messages, default=0) + 1
    for i, raw in enumerate(raws):
        FakeIMAP.messages[start + i] = raw
    return run_inbox(ctx)


def reply(ctx, sent, text, frm=None, subject=None, thread=True):
    _n[0] += 1
    return deliver(ctx, mail(frm or f"David Chen <{DAVID}>", subject or f"Re: {sent.subject}", text, in_reply_to=sent.message_id if thread else None,
                             msg_id=f"<r{_n[0]}@mail.example>"))


def calendly_notice():
    _n[0] += 1
    body = (f"Calendly\n\nEvent Type:\n30 Minute Meeting\n\nInvitee:\nDavid Chen\n\nInvitee Email:\n{DAVID}\n\nEvent Date/Time:\n{A[0]} - {A[1]} (India Standard Time)\n\n"
            "Location:\nGoogle Meet\n\nInvitee Time Zone:\nEastern Time - US & Canada\n\nView event in Calendly\nhttps://calendly.com/app\n")
    return mail("Calendly <notifications@calendly.com>", f"New Event: David Chen - {A[0]} {A[2]} - 30 Minute Meeting", body, msg_id=f"<c{_n[0]}@calendly.example>")


def responses(ctx, lead_id=None):
    with ctx.db.session() as s:
        q = select(FollowUp).where(FollowUp.kind == "response")
        if lead_id:
            q = q.where(FollowUp.lead_id == lead_id)
        return s.execute(q.order_by(FollowUp.id)).scalars().all()


def deals(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Opportunity).order_by(Opportunity.id)).scalars().all()


def alerts(ctx):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert)).scalars()]


def book_meeting(ctx, lead_id, source="calendly_email", days=3):
    start = utcnow() + timedelta(days=days)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        lead.outreach_status = "meeting"
        s.add(Meeting(lead_id=lead_id, company_id=lead.company_id, invitee_email=DAVID, starts_at=start, ends_at=start + timedelta(minutes=30),
                      status="scheduled", source=source, title="30 Minute Meeting"))
    return start


def queue_approved_step(ctx, lead_id):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        f = FollowUp(lead_id=lead.id, company_id=lead.company_id, contact_id=lead.contact_id, channel="email", kind="outreach", step=2, status="approved", due_at=utcnow(),
                     to_email=lead.contact.email if lead.contact else None, subject="Second step", body="Hello again", approved_by="test", approved_at=utcnow())
        s.add(f)
        s.flush()
        return f.id


def status_of(ctx, fid):
    with ctx.db.session() as s:
        return s.get(FollowUp, fid).status


def try_to_send(ctx, sender):
    FakeSMTP.sent = []
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(days=7)).isoformat()})
    return list(FakeSMTP.sent)


# ------------------------------------------------------------------ the owner's Q2 and Q4(d)
def test_not_until_a_date_stops_the_sequence_suppresses_nothing_and_sets_a_reminder(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, "Don't email me until April.")
    with ctx.db.session() as s:
        assert not is_suppressed(s, DAVID)
        assert s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending", FollowUp.subject.like("Check back%"))).scalars().first() is not None
    assert lead_of(ctx).outreach_status != "contacted"


def park(ctx):
    with ctx.db.session() as s:
        s.execute(select(Lead)).scalars().first().status = "nurture"


def test_a_clear_interested_reply_brings_a_nurture_lead_back(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    park(ctx)
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?")
    assert lead_of(ctx).status == "qualified"
    with ctx.db.session() as s:
        assert s.execute(select(Activity).where(Activity.subject.like("Lead nurture → qualified%"))).scalars().first() is not None
    LeadQualificationAgent(ctx).run({})
    assert lead_of(ctx).status == "qualified"


@pytest.mark.parametrize("why", ["low", "opted_out", "suppressed", "held"])
def test_a_nurture_lead_stays_when_the_reading_is_unsure_or_it_opted_out_or_is_held(make_ctx, why):
    ctx, _s, sent = contacted(make_ctx)
    park(ctx)
    text = "Sounds interesting, can we talk Thursday?"
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalars().first()
        if why == "opted_out":
            lead.contact.consent_status = "opted_out"
        elif why == "suppressed":
            suppress(s, DAVID, "asked")
        elif why == "held":
            s.add(InboundEmail(mailbox_uid="h1", message_id="<h1@mail.example>", from_email=HOME, status="needs_review", lead_id=lead.id, holds_sending=True))
    if why == "low":
        text = "Sounds interesting but I hate this, not interested, call me maybe."
    reply(ctx, sent, text)
    assert lead_of(ctx).status == "nurture"


# ------------------------------------------------------------------ one opt-out and the company's other leads
def second_lead_by_inquiry(ctx):
    """The real path: an inquiry from a colleague makes a lead beside the research lead."""
    first = lead_of(ctx)
    with ctx.db.session() as s:
        name = s.get(Company, first.company_id).name
        record_inquiry(s, {"email": SAM, "company": name, "message": "We need help with our data entry", "name": "Sam Synth"}, "web_form")
    assert CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]}).ok
    with ctx.db.session() as s:
        other = s.execute(select(Lead).where(Lead.company_id == first.company_id, Lead.id != first.id)).scalars().one()
        return first.id, other.id


@pytest.mark.parametrize("text", ["Please remove our company from your list.", "Please remove me from your list."])
def test_an_opt_out_from_a_known_colleague_stops_another_leads_approved_email(make_ctx, text):
    ctx, sender, sent = contacted(make_ctx)
    first, _inquiry = second_lead_by_inquiry(ctx)
    step = queue_approved_step(ctx, first)
    reply(ctx, sent, text, frm=SAM, subject="Your email", thread=False)
    r = latest_reply(ctx)
    assert r.category == "unsubscribe" and r.status == "needs_review" and r.lead_id == _inquiry
    assert status_of(ctx, step) == "cancelled"
    assert try_to_send(ctx, sender) == []
    with ctx.db.session() as s:
        lead = s.get(Lead, first)
        assert lead.outreach_status == "stopped" and lead.contact.consent_status != "opted_out" and not is_suppressed(s, DAVID) and is_suppressed(s, SAM)
        assert reopen_refusal(s, lead)
    assert "reopen" not in r.action_note.lower()


def test_an_opt_out_from_a_personal_address_in_the_thread_stops_the_companys_other_leads(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    first, other = second_lead_by_inquiry(ctx)
    step = queue_approved_step(ctx, other)
    reply(ctx, sent, "Please remove me from your list.", frm=HOME)
    assert status_of(ctx, step) == "cancelled"
    assert try_to_send(ctx, sender) == []
    r = latest_reply(ctx)
    assert r.lead_id == first and r.status == "needs_review"
    with ctx.db.session() as s:
        assert s.get(Lead, other).outreach_status == "stopped"
        assert is_suppressed(s, HOME) and is_suppressed(s, DAVID) and not is_suppressed(s, SAM)


def test_a_quoted_opt_out_is_nobodys_and_stops_no_other_lead(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    _first, other = second_lead_by_inquiry(ctx)
    step = queue_approved_step(ctx, other)
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?\n\nOn Tue, Oct 27, Pat wrote:\n> Please remove me from your list.")
    assert status_of(ctx, step) == "approved"


@pytest.mark.parametrize("stage", ["interested", "meeting"])
def test_another_lead_in_a_live_conversation_keeps_its_stage_and_a_person_is_told(make_ctx, stage):
    ctx, _s, sent = contacted(make_ctx)
    first, other = second_lead_by_inquiry(ctx)
    step = queue_approved_step(ctx, other)
    with ctx.db.session() as s:
        lead = s.get(Lead, other)
        lead.outreach_status = stage
        answer = FollowUp(lead_id=other, company_id=lead.company_id, channel="email", kind="response", step=1, status="approved", due_at=utcnow(), to_email=SAM, subject="Re: x", body="An answer")
        s.add(answer)
        s.flush()
        answer_id = answer.id
    reply(ctx, sent, "Please remove me from your list.")
    r = latest_reply(ctx)
    assert r.status == "needs_review" and r.lead_id == first
    with ctx.db.session() as s:
        assert s.get(Lead, other).outreach_status == stage
    assert status_of(ctx, step) == "cancelled"
    assert status_of(ctx, answer_id) == "awaiting_approval", "an approved answer does not go on its own after this"
    assert any(f"#{other} ({stage})" in a for a in alerts(ctx))


def test_a_deal_on_another_lead_is_told_and_promises_no_reopening(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    _first, other = second_lead_by_inquiry(ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, other)
        s.add(Opportunity(lead_id=other, company_id=lead.company_id, name="Colleague deal", stage="proposal", probability=50, next_step="Send it"))
    reply(ctx, sent, "Please remove me from your list.")
    step = [o for o in deals(ctx) if o.lead_id == other][0].next_step
    assert "opted out" in step and "reopen" not in step.lower()


# ------------------------------------------------------------------ a reply that reaches a merged lead
def merged_scene(make_ctx, keeper_contact_is_sender=False):
    ctx, sender, sent = contacted(make_ctx)
    merged = lead_of(ctx).id
    with ctx.db.session() as s:
        keeper, own = _keeper_with_contact(s)
        own_id = own.id
        if keeper_contact_is_sender:
            own.contact.email = DAVID
        first = FollowUp(lead_id=own.id, company_id=keeper.id, contact_id=own.contact_id, channel="email", kind="outreach", step=1, status="approved", due_at=utcnow(),
                         to_email=own.contact.email, subject="Hi", body="Hi", approved_by="test", approved_at=utcnow())
        s.add(first)
        s.flush()
        first_id = first.id
        merge_companies(s, keeper, s.get(Company, s.get(Lead, merged).company_id), "crm")
    with ctx.db.session() as s:
        assert s.get(Lead, merged).status == "merged", "the emailed lead must be the merged one for this scene"
    return ctx, sender, sent, merged, own_id, first_id


def force_merged(make_ctx):
    """The emailed lead merged, as a person can set it by hand, beside a keeper not yet emailed."""
    ctx, sender, sent = contacted(make_ctx)
    merged = lead_of(ctx).id
    with ctx.db.session() as s:
        lead = s.get(Lead, merged)
        lead.status = "merged"
        keeper = Lead(company_id=lead.company_id, source="research", status="qualified", score=70, grade="B")
        other = Contact(company_id=lead.company_id, email=f"lee@{SITE}", source="website", email_source_url=f"https://{SITE}/c", email_status="role_address", consent_status="none")
        s.add(other)
        s.flush()
        keeper.contact_id = other.id
        s.add(keeper)
        s.flush()
        first = FollowUp(lead_id=keeper.id, company_id=lead.company_id, contact_id=other.id, channel="email", kind="outreach", step=1, status="approved", due_at=utcnow(),
                         to_email=other.email, subject="Hi", body="Hi", approved_by="test", approved_at=utcnow())
        s.add(first)
        s.flush()
        return ctx, sender, sent, merged, keeper.id, first.id


def test_an_interested_reply_on_a_merged_lead_shows_on_the_open_lead_and_stops_its_first_email(make_ctx):
    ctx, sender, sent, merged, keeper, first = force_merged(make_ctx)
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?")
    r = latest_reply(ctx)
    assert r.lead_id == keeper and r.followup_id == sent.id
    assert status_of(ctx, first) == "cancelled"
    with ctx.db.session() as s:
        assert s.get(Lead, keeper).outreach_status == "stopped", "the keeper's contact did not write"
    assert len(deals(ctx)) == 1
    assert try_to_send(ctx, sender) == []


def test_the_open_leads_own_contact_writing_gives_the_open_lead_the_replys_stage(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    merged = lead_of(ctx).id
    with ctx.db.session() as s:
        lead = s.get(Lead, merged)
        lead.status = "merged"
        keeper = Lead(company_id=lead.company_id, contact_id=lead.contact_id, source="inbound", status="qualified", score=70)
        s.add(keeper)
        s.flush()
        keeper_id = keeper.id
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?")
    with ctx.db.session() as s:
        assert s.get(Lead, keeper_id).outreach_status == "interested"
    assert latest_reply(ctx).lead_id == keeper_id


def test_a_merged_leads_opt_out_stops_the_open_lead_as_stopped_when_it_is_someone_else(make_ctx):
    ctx, sender, sent, merged, keeper, first = force_merged(make_ctx)
    reply(ctx, sent, "Please remove me from your list.")
    r = latest_reply(ctx)
    assert r.lead_id == keeper and r.status == "needs_review"
    assert status_of(ctx, first) == "cancelled"
    with ctx.db.session() as s:
        assert s.get(Lead, merged).outreach_status == "unsubscribed" and s.get(Lead, keeper).outreach_status == "stopped"
        assert is_suppressed(s, DAVID) and not is_suppressed(s, f"lee@{SITE}")
    assert try_to_send(ctx, sender) == []


def test_a_merged_leads_opt_out_closes_the_open_lead_when_it_is_the_same_address(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    merged = lead_of(ctx).id
    with ctx.db.session() as s:
        lead = s.get(Lead, merged)
        lead.status = "merged"
        keeper = Lead(company_id=lead.company_id, contact_id=lead.contact_id, source="inbound", status="qualified", score=70)
        s.add(keeper)
        s.flush()
        keeper_id = keeper.id
    reply(ctx, sent, "Please remove me from your list.")
    with ctx.db.session() as s:
        assert s.get(Lead, keeper_id).outreach_status == "unsubscribed"


def test_a_bounce_on_a_merged_lead_stays_on_the_merged_lead(make_ctx):
    ctx, _s, sent, merged, keeper, first = force_merged(make_ctx)
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    assert latest_reply(ctx).lead_id == merged
    assert status_of(ctx, first) == "approved"
    with ctx.db.session() as s:
        assert s.get(Lead, keeper).outreach_status is None


def test_a_merged_lead_with_no_open_lead_beside_it_stays_for_a_person(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    merged = lead_of(ctx).id
    with ctx.db.session() as s:
        s.get(Lead, merged).status = "merged"
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?")
    r = latest_reply(ctx)
    assert r.status == "needs_review" and r.lead_id == merged and "merged" in r.action_note


# ------------------------------------------------------------------ writing again after an opt-out
UNSUB = "Please unsubscribe me."
CALL = "Sorry, I hit reply too fast earlier. Actually please call me, we are interested."


def one_neutral_alert(ctx):
    told = alerts(ctx)
    assert sum("opted out earlier and wrote again" in a for a in told) == 1 and not any("Interested reply" in a for a in told)


def test_a_opt_out_then_interest_leaves_no_draft_no_deal_and_no_good_news(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, UNSUB)
    reply(ctx, sent, CALL)
    r = latest_reply(ctx)
    assert r.status == "needs_review" and "opted out earlier" in r.action_note and "owner decides" in r.recommended_status
    assert responses(ctx) == [] and deals(ctx) == [] and lead_of(ctx).outreach_status == "unsubscribed"
    one_neutral_alert(ctx)


def test_b_a_deal_lost_by_the_opt_out_stays_lost(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?")
    reply(ctx, sent, UNSUB)
    assert deals(ctx)[0].stage == "lost"
    reply(ctx, sent, CALL)
    assert [(o.stage, o.lost_reason) for o in deals(ctx)] == [("lost", "Unsubscribed")]
    assert not [d for d in responses(ctx) if d.status == "awaiting_approval"]


def test_an_open_deal_only_gets_a_new_next_step(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?")
    with ctx.db.session() as s:
        s.execute(select(Opportunity)).scalars().one().stage = "proposal"
    reply(ctx, sent, UNSUB)
    reply(ctx, sent, CALL)
    (deal,) = deals(ctx)
    assert deal.stage == "proposal" and deal.next_step == OPTED_OUT_WROTE_AGAIN


def test_c_a_lead_with_no_stage_goes_to_a_person_and_does_not_read_interested(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalars().first()
        lead.outreach_status = None
        lead.contact.consent_status = "opted_out"
        suppress(s, DAVID, "Unsubscribe request by email")
    reply(ctx, sent, CALL)
    r = latest_reply(ctx)
    assert r.status == "needs_review" and "opted out earlier" in r.action_note
    assert lead_of(ctx).outreach_status != "interested" and responses(ctx) == [] and deals(ctx) == []


def test_d_a_colleague_on_file_keeps_an_approvable_draft_and_a_true_note(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, UNSUB)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalars().first()
        s.add(Contact(company_id=lead.company_id, email=SAM, source="manual", consent_status="none"))
    reply(ctx, sent, "We are interested, can we talk Friday?", frm=SAM, subject="Hello there", thread=False)
    r = latest_reply(ctx)
    assert r.status == "needs_review" and "this sender is not on the list" in r.action_note and "they opted out earlier" not in r.action_note
    (draft,) = [d for d in responses(ctx) if d.status == "awaiting_approval"]
    assert draft.to_email == SAM


def test_g_a_colleague_in_our_thread_after_the_opt_out_has_the_gate_decide(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, UNSUB)
    reply(ctx, sent, "We are interested, can we talk Friday?", frm=SAM)
    r = latest_reply(ctx)
    assert r.status == "needs_review" and "opted out earlier" in r.action_note
    assert not [d for d in responses(ctx) if d.status == "awaiting_approval"], "the draft's contact opted out: the gate could not take it"


@pytest.mark.parametrize("text", ["We use another vendor already, maybe next year.", "How much does this cost?", "Who else should I ask about this?"])
def test_i_no_draft_for_any_kind_of_reply_after_an_opt_out(make_ctx, text):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, UNSUB)
    reply(ctx, sent, text)
    assert latest_reply(ctx).status == "needs_review" and responses(ctx) == []


def test_a_person_reclassifying_a_false_opt_out_gets_no_dead_draft_or_deal(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, UNSUB)
    rid = latest_reply(ctx).id
    r = web(ctx).post(f"/inbox/{rid}/reclassify", headers=ORIGIN, data={"category": "interested"}, follow_redirects=False)
    assert r.status_code == 303
    assert responses(ctx) == [] and deals(ctx) == []
    with ctx.db.session() as s:
        assert is_suppressed(s, DAVID)


# ------------------------------------------------------------------ a delivery report after someone replied
def test_a_bounce_of_the_first_address_keeps_the_answer_to_a_colleague_who_replied(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    deliver(ctx, mail(f"Sam <{SAM}>", f"Re: {sent.subject}", "Sounds interesting. Can we talk Thursday?", in_reply_to=sent.message_id))
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    (draft,) = responses(ctx)
    assert draft.to_email == SAM and draft.status == "awaiting_approval"
    r = latest_reply(ctx)
    assert r.category == "bounce" and r.status == "needs_review" and SAM in r.action_note
    assert lead_of(ctx).outreach_status == "interested"
    with ctx.db.session() as s:
        contact = s.execute(select(Lead)).scalars().first().contact
        assert is_suppressed(s, DAVID) and contact.email_status == "bounced" and contact.consent_status != "opted_out"
    assert web(ctx).post(f"/followups/{draft.id}/approve", headers=ORIGIN, follow_redirects=False).status_code == 303
    assert status_of(ctx, draft.id) == "approved"


def test_a_bounce_keeps_the_check_back_task_their_reply_asked_for(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    deliver(ctx, mail(f"Sam <{SAM}>", f"Re: {sent.subject}", "Interesting, but please check back in March.", in_reply_to=sent.message_id))
    assert [d.to_email for d in responses(ctx) if d.status == "awaiting_approval"] == [SAM]
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending", FollowUp.subject.like("Check back%"))).scalars().first() is not None


def test_an_approved_answer_goes_back_to_a_person_after_the_report(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    deliver(ctx, mail(f"Sam <{SAM}>", f"Re: {sent.subject}", "Sounds interesting. Can we talk Thursday?", in_reply_to=sent.message_id))
    (draft,) = responses(ctx)
    with ctx.db.session() as s:
        d = s.get(FollowUp, draft.id)
        d.status, d.approved_by, d.approved_at = "approved", "test", utcnow()
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    assert status_of(ctx, draft.id) == "awaiting_approval"
    assert try_to_send(ctx, sender) == []


def test_the_same_address_failing_cancels_its_answer_and_shows_the_reply(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    deliver(ctx, mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds interesting. Can we talk Thursday?", in_reply_to=sent.message_id))
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    (draft,) = responses(ctx)
    assert draft.status == "cancelled" and lead_of(ctx).outreach_status == "bounced"
    r = latest_reply(ctx)
    assert r.status == "needs_review" and DAVID in r.action_note


def test_a_report_with_no_reply_on_the_lead_is_actioned_as_before(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    assert latest_reply(ctx).status == "actioned" and lead_of(ctx).outreach_status == "bounced"


def test_an_alias_targets_report_keeps_the_colleagues_draft(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    deliver(ctx, mail(f"Sam <{SAM}>", f"Re: {sent.subject}", "Sounds interesting. Can we talk Thursday?", in_reply_to=sent.message_id))
    target = f"gone@{SITE}"
    deliver(ctx, bounce_for(target, sent.message_id))
    r = latest_reply(ctx)
    assert r.status == "needs_review"
    (draft,) = responses(ctx)
    assert draft.status == "awaiting_approval" and lead_of(ctx).outreach_status == "interested"
    with ctx.db.session() as s:
        assert not is_suppressed(s, target) and not is_suppressed(s, DAVID)


def test_a_real_opt_out_then_a_bounce_still_blocks_the_colleagues_answer(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, UNSUB)
    deliver(ctx, mail(f"Sam <{SAM}>", f"Re: {sent.subject}", "We are interested. Can we talk Thursday?", in_reply_to=sent.message_id))
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    assert not [d for d in responses(ctx) if d.status in ("awaiting_approval", "approved")]
    with ctx.db.session() as s:
        assert s.execute(select(Lead)).scalars().first().contact.consent_status == "opted_out"


def test_a_bounce_then_a_real_opt_out_still_blocks_the_colleagues_answer(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    deliver(ctx, mail(f"Sam <{SAM}>", f"Re: {sent.subject}", "Sounds interesting. Can we talk Thursday?", in_reply_to=sent.message_id))
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    reply(ctx, sent, UNSUB)
    assert not [d for d in responses(ctx) if d.status in ("awaiting_approval", "approved")]
    with ctx.db.session() as s:
        contact = s.execute(select(Lead)).scalars().first().contact
        assert contact.consent_status == "opted_out" and is_suppressed(s, DAVID)


# ------------------------------------------------------------------ a body that names who we wrote to
def paste(to, words):
    return f"{words}\n\n________________________________\nFrom: Sales <sales@us.example>\nSent: Friday, October 30, 2026 1:09 PM\nTo: Pat Lead <{to}>\nSubject: A question\n\nHello"


@pytest.mark.parametrize("words", ["Sounds interesting, can we talk Friday?", "Please stop writing to my work address and take me off your list."])
def test_an_outlook_paste_from_a_personal_address_holds_step_2_and_nothing_else(make_ctx, words):
    ctx, sender, sent = contacted(make_ctx)
    lead = lead_of(ctx).id
    step = queue_approved_step(ctx, lead)
    reply(ctx, sent, paste(DAVID, words), frm=HOME, subject="your email to my work address", thread=False)
    r = latest_reply(ctx)
    assert r.lead_id == lead and r.holds_sending is True and r.status == "needs_review"
    assert status_of(ctx, step) == "approved"
    assert try_to_send(ctx, sender) == []
    assert deals(ctx) == [] and responses(ctx) == [] and lead_of(ctx).outreach_status == "contacted"
    with ctx.db.session() as s:
        assert not is_suppressed(s, DAVID) and lead_on_hold(s, lead)


def test_mark_reviewed_releases_the_hold(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, paste(DAVID, "Sounds interesting."), frm=HOME, subject="your email", thread=False)
    r = latest_reply(ctx)
    web(ctx).post(f"/inbox/{r.id}/reviewed", headers=ORIGIN, follow_redirects=False)
    with ctx.db.session() as s:
        assert not lead_on_hold(s, r.lead_id)


def test_a_person_confirming_the_opt_out_stops_the_lead_it_held(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, paste(DAVID, "What is this about?"), frm=HOME, subject="your email", thread=False)
    r = latest_reply(ctx)
    assert r.holds_sending is True
    web(ctx).post(f"/inbox/{r.id}/reclassify", headers=ORIGIN, data={"category": "unsubscribe"}, follow_redirects=False)
    assert lead_of(ctx).outreach_status == "unsubscribed"
    with ctx.db.session() as s:
        assert is_suppressed(s, DAVID)


def already_kept(ctx, body):
    """The mail as it stands once the inbox has kept it, whatever made it do so."""
    _n[0] += 1
    with ctx.db.session() as s:
        s.add(InboundEmail(mailbox_uid=f"k{_n[0]}", message_id=f"<k{_n[0]}@home.example>", from_email=HOME, subject="your email", status="new", body_text=body,
                           new_text="Sounds interesting.", received_at=utcnow(), auto_submitted=False, bounced_addresses=[]))
    with ctx.db.session() as s:
        process_pending(s, ctx.settings, "inbox")
    return latest_reply(ctx)


def test_naming_two_leads_holds_neither_a_known_gap_a_person_reads_it(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    with ctx.db.session() as s:
        _keeper_with_contact(s, sent=True)
    r = already_kept(ctx, paste(DAVID, "Sounds interesting.") + "\n" + paste("pat@keeper.example", "x"))
    assert r.status == "needs_review" and not r.holds_sending and r.lead_id is None


@pytest.mark.parametrize("address", [f"x{DAVID}", f"{DAVID}.evil.example"])
def test_an_address_inside_a_longer_one_holds_nothing(make_ctx, address):
    ctx, _s, _sent = contacted(make_ctx)
    assert not already_kept(ctx, paste(address, "Sounds interesting.")).holds_sending


def test_a_newsletter_or_a_robot_holds_nothing(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    _n[0] += 1
    deliver(ctx, mail(HOME, "Your weekly digest", paste(DAVID, "Sounds interesting."), msg_id=f"<r{_n[0]}@mail.example>", headers={"Auto-Submitted": "auto-generated"}))
    deliver(ctx, mail("no-reply@home.example", "A notice", paste(DAVID, "Sounds interesting."), msg_id="<r0@mail.example>"))
    with ctx.db.session() as s:
        assert not lead_on_hold(s, lead_of(ctx).id)


def test_a_forged_calendly_notice_in_junk_naming_the_address_holds_nothing(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        s.add(InboundEmail(mailbox_uid="j1", message_id="<j1@evil.example>", from_email="someone@evil.example", subject="New Event: nobody", status="new", in_junk=True,
                           body_text=f"see calendly.com\nInvitee Email:\n{DAVID}\n", new_text="see calendly.com", received_at=utcnow(), auto_submitted=False, bounced_addresses=[]))
    with ctx.db.session() as s:
        process_pending(s, ctx.settings, "inbox")
    r = latest_reply(ctx)
    assert r.in_junk and not r.holds_sending


# ------------------------------------------------------------------ a booked meeting, then an email about it
def test_an_email_asking_to_move_the_meeting_reaches_a_person_and_is_not_answered_i_look_forward_to_it(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    lead = lead_of(ctx)
    book_meeting(ctx, lead.id)
    reply(ctx, sent, "Something came up. Can we move our call to Friday morning instead?")
    r = latest_reply(ctx)
    assert r.status == "needs_review" and "meeting is booked" in r.action_note
    assert not any("I look forward to it" in (d.body or "") for d in responses(ctx, lead.id))
    assert sum("while a meeting is booked" in a for a in alerts(ctx)) == 1


@pytest.mark.parametrize("words", ["Can we push the meeting to Monday at 10?", "Friday at 3 works better for us, can we change the call?",
                                   "Need to reschedule. Are you available next week?", "Please cancel the meeting, Tuesday at 2 no longer works.",
                                   "Sounds good but we have to move the call to next week.", "Can we do Wednesday at 4?", "Tuesday morning is a holiday here. Wednesday at 4?",
                                   "I won't be able to attend. Are you free next week?", "We have a conflict, are you available Thursday?"])
def test_no_wording_about_changing_it_gets_the_confirmation(make_ctx, words):
    ctx, _sender, sent = contacted(make_ctx)
    lead = lead_of(ctx)
    book_meeting(ctx, lead.id)
    reply(ctx, sent, words)
    assert latest_reply(ctx).status == "needs_review"
    drafts = responses(ctx, lead.id)
    assert all("I look forward to it" not in (d.body or "") for d in drafts)
    assert all(QUESTION_PLACEHOLDER in d.body and d.blocked_reason for d in drafts)


@pytest.mark.parametrize("words", ["Something came up. Can we move our call to Thursday instead?", "We need to cancel our call tomorrow.",
                                   "Please reschedule our meeting, I am travelling that day."])
def test_a_scheduled_meeting_keeps_the_stage_meeting_until_a_person_changes_it(make_ctx, words):
    ctx, _sender, sent = contacted(make_ctx)
    lead = lead_of(ctx)
    book_meeting(ctx, lead.id)
    reply(ctx, sent, words)
    assert lead_of(ctx).outreach_status == "meeting"


def test_a_plain_thanks_still_gets_the_confirmation_and_the_deal_keeps_its_meeting(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    lead = lead_of(ctx)
    book_meeting(ctx, lead.id)
    reply(ctx, sent, "Great, sounds good. Looking forward to our call, we are interested.")
    (draft,) = responses(ctx, lead.id)
    assert "I look forward to it" in draft.body and QUESTION_PLACEHOLDER not in draft.body
    assert latest_reply(ctx).status == "needs_review"
    (deal,) = deals(ctx)
    assert deal.next_step != BOOK_A_MEETING and "Meeting" in deal.next_step


@pytest.mark.parametrize("words", ["Please cancel our call, no need to talk. Not interested.", "Please cancel the meeting and remove me from your list."])
def test_a_no_or_an_opt_out_with_a_meeting_booked_reaches_a_person_and_the_meeting_stands(make_ctx, words):
    ctx, _sender, sent = contacted(make_ctx)
    lead = lead_of(ctx)
    book_meeting(ctx, lead.id)
    reply(ctx, sent, words)
    r = latest_reply(ctx)
    assert r.status == "needs_review" and "meeting is booked" in r.action_note
    assert sum("while a meeting is booked" in a for a in alerts(ctx)) == 1
    with ctx.db.session() as s:
        assert next_meeting(s, lead.id) is not None
        if r.category == "unsubscribe":
            assert is_suppressed(s, DAVID) and s.get(Lead, lead.id).outreach_status == "unsubscribed"


def test_a_meeting_logged_by_hand_does_not_promise_a_calendly_confirmation(make_ctx):
    ctx, _s, sent = contacted(make_ctx, calendly_verified_only=False, calendly_url="https://calendly.com/owner-example/30min")
    lid = lead_of(ctx).id
    web(ctx).post(f"/leads/{lid}/meeting", headers=ORIGIN, data={"date": "2026-12-08", "time_et": "10:00", "minutes": 30, "invitee_email": DAVID}, follow_redirects=False)
    reply(ctx, sent, "Sounds good, I am interested. Let's talk.")
    (draft,) = responses(ctx)
    assert "calendly" not in draft.body.lower() and "just reply to this email" in draft.body


# ------------------------------------------------------------------ pricing
def pricing_then(make_ctx, second):
    ctx, _sender, sent = contacted(make_ctx)
    reply(ctx, sent, "Thanks. What does this usually cost for a team of ten?")
    reply(ctx, sent, second)
    return ctx


def test_a_budget_question_after_a_pricing_question_leaves_a_place_for_the_answer(make_ctx):
    ctx = pricing_then(make_ctx, "One more thing: our budget is under 10k. Is that realistic?")
    with ctx.db.session() as s:
        row = s.execute(select(InboundEmail).order_by(InboundEmail.id.desc())).scalars().first()
        draft = s.get(FollowUp, row.suggested_followup_id)
        assert "a person writes the answer" in row.action_note
        assert QUESTION_PLACEHOLDER in draft.body


def test_a_first_budget_question_is_held_at_approval_and_has_no_link(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, "Our budget is 5k, can you do it for that?")
    r = latest_reply(ctx)
    assert r.category == "objection" and "a person writes the answer" in r.action_note
    (draft,) = responses(ctx)
    assert QUESTION_PLACEHOLDER in draft.body and "calendly" not in draft.body.split("---")[0].lower() and "Thank you for getting back to me." in draft.body
    web(ctx).post(f"/followups/{draft.id}/approve", headers=ORIGIN, follow_redirects=False)
    assert status_of(ctx, draft.id) == "awaiting_approval"


@pytest.mark.parametrize(("words", "kept"), [
    ("Our budget for next year is already committed. Please check back in March.", "check back"),
    ("I forwarded this to our CFO, who handles the budget.", "passing my note along"),
    ("We already use a vendor at a lower cost.", "honest answer"),
])
def test_a_plain_statement_about_money_keeps_its_template_and_a_true_note(make_ctx, words, kept):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, words)
    r = latest_reply(ctx)
    (draft,) = responses(ctx)
    assert kept in draft.body and QUESTION_PLACEHOLDER not in draft.body and "calendly" not in draft.body.split("---")[0].lower()
    assert "a person writes the answer" not in (r.action_note or "")
    if r.intent == "pricing_request":
        assert "mentions pricing: a person checks the draft" in r.action_note


# ------------------------------------------------------------------ what the deal's next step says
def test_the_deals_next_step_stops_saying_the_answer_waits_once_it_went(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    reply(ctx, sent, "Thanks. What does this usually cost? Can you send pricing for a small team of about ten people?")
    lead = lead_of(ctx)
    (deal,) = deals(ctx)
    assert deal.next_step in ASKS_FOR_APPROVAL, "named, so it settles"
    with ctx.db.session() as s:
        f = s.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.kind == "response")).scalars().one()
        f.status, f.sent_at = "sent", utcnow()
    with ctx.db.session() as s:
        settle_next_steps(s, lead.id)
    assert "it waits in the suggested reply" not in deals(ctx)[0].next_step


def test_the_answer_stays_the_next_step_while_the_draft_waits(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    reply(ctx, sent, "Thanks. What does this usually cost? Can you send pricing for a small team of about ten people?")
    before = deals(ctx)[0].next_step
    with ctx.db.session() as s:
        settle_next_steps(s, lead_of(ctx).id)
    assert deals(ctx)[0].next_step == before


def test_an_unread_reply_is_the_next_step_not_no_reply_is_waiting(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    reply(ctx, sent, "Sounds interesting. Can we talk Thursday?")
    reply(ctx, sent, "Also, one more thing about our setup.")
    with ctx.db.session() as s:
        for f in s.execute(select(FollowUp).where(FollowUp.kind == "response")).scalars():
            f.status = "cancelled"
        s.flush()
        settle_next_steps(s, lead_of(ctx).id)
    assert deals(ctx)[0].next_step == READ_THEIR_REPLY


def test_a_next_step_a_person_typed_stays(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    reply(ctx, sent, "Sounds interesting. Can we talk Thursday?")
    with ctx.db.session() as s:
        s.execute(select(Opportunity)).scalars().one().next_step = "Call them on Monday"
    with ctx.db.session() as s:
        settle_next_steps(s, lead_of(ctx).id)
    assert deals(ctx)[0].next_step == "Call them on Monday"


# ------------------------------------------------------------------ meetings wording and the no-notices warning
def test_a_booking_stops_the_draft_that_asks_for_times(make_ctx):
    ctx, _s, sent = contacted(make_ctx, calendly_verified_only=False, calendly_url="")
    reply(ctx, sent, "Sounds good, can we schedule a call?")
    (draft,) = responses(ctx)
    assert draft.purpose == "ask_times"
    deliver(ctx, calendly_notice())
    assert responses(ctx)[0].status == "cancelled"


def test_only_the_plain_request_for_times_is_marked(make_ctx):
    ctx, _s, sent = contacted(make_ctx, calendly_verified_only=False, calendly_url="")
    reply(ctx, sent, "Sounds interesting. Can you send more information?")
    assert all(d.purpose is None for d in responses(ctx))


def test_the_deal_does_not_mention_a_booking_link_when_none_is_configured(make_ctx):
    ctx, _s, sent = contacted(make_ctx, calendly_verified_only=False, calendly_url="")
    reply(ctx, sent, "Sounds good, can we schedule a call?")
    (deal,) = deals(ctx)
    assert "booking link" not in deal.next_step and deal.next_step in ASKS_FOR_APPROVAL


def test_an_unproven_notice_does_not_silence_the_no_notices_warning(make_ctx):
    ctx, _s, _sent = contacted(make_ctx, calendly_verified_only=True, calendly_url="https://calendly.com/owner-example/30min")
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        s.add(FollowUp(lead_id=ld.id, company_id=ld.company_id, channel="email", kind="response", step=1, status="sent", purpose="booking_link",
                       to_email=DAVID, subject="Re: x", body="https://calendly.com/owner-example/30min", sent_at=utcnow() - timedelta(days=5)))
    run_inbox(ctx)
    deliver(ctx, mail("Stranger <someone@evil.example>", "New Event: nobody", "see calendly.com", msg_id="<x1@evil.example>"))
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.key == "calendly:no_notices", Alert.resolved_at.is_(None))).first() is not None
        assert booking_links_without_notice(s)[0] == 1


def test_a_notice_that_was_applied_ends_the_warning(make_ctx):
    ctx, _s, _sent = contacted(make_ctx, calendly_verified_only=False, calendly_url="https://calendly.com/owner-example/30min")
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        s.add(FollowUp(lead_id=ld.id, company_id=ld.company_id, channel="email", kind="response", step=1, status="sent", purpose="booking_link",
                       to_email=DAVID, subject="Re: x", body="https://calendly.com/owner-example/30min", sent_at=datetime(2026, 10, 1)))
    deliver(ctx, calendly_notice())
    assert latest_reply(ctx).status == "actioned"
    with ctx.db.session() as s:
        assert booking_links_without_notice(s) == (0, None)
