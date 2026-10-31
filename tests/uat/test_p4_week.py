"""A real week in the dashboard: what a person sees after replies, notes, meetings and a corrected name. Synthetic data only."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import unquote

import pytest
from sqlalchemy import select

from aime.agents.analytics import AnalyticsReportingAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.assignment import assign_new
from aime.crm_ops import log_activity, suppress
from aime.integrations import calendar as calendar_module
from aime.models import Activity, Assignment, Company, Contact, FollowUp, InboundEmail, Lead, Meeting, Opportunity, User
from aime.pipeline import NO_REPLY, REPLY_SENT, REPLY_WAITING, settle_next_steps
from aime.permissions import permissions_of
from aime.timeutil import utcnow
from aime.visibility import can_see_lead, scope_for
from aime.web.app import activity_words
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID, FakeIMAP, contacted, mail, run_inbox
from tests.test_outreach import lead_of
from tests.uat.test_uat_email import ORIGIN, web
from tests.uat.test_uat_sales_journey import TOKEN, add_people, client, lead_row, make_deal, make_draft, make_lead, opp_row, post


@pytest.fixture
def org(make_ctx):
    ctx = make_ctx(dashboard_auth=True, inbound_api_token=TOKEN)
    add_people(ctx)
    return ctx


def answer_of(ctx, lead_id):
    with ctx.db.session() as s:
        return [(f.id, f.status) for f in s.execute(select(FollowUp).where(
            FollowUp.lead_id == lead_id, FollowUp.kind == "response")).scalars()]


def interested(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds good, can we talk on Thursday?",
                                 in_reply_to=sent.message_id)}
    run_inbox(ctx)
    lead = lead_of(ctx)
    assert lead.outreach_status == "interested" and [s for _i, s in answer_of(ctx, lead.id)] == ["awaiting_approval"]
    return ctx, sent, lead.id


def test_they_replied_does_not_cancel_an_answer_waiting_for_them(make_ctx):
    """The button says it stops follow-ups. The drafted answer to their own email is not a follow-up."""
    ctx, _sent, lead_id = interested(make_ctx)
    web(ctx).post(f"/leads/{lead_id}/replied", headers=ORIGIN, follow_redirects=False)
    assert "awaiting_approval" in [s for _i, s in answer_of(ctx, lead_id)]


def test_a_note_can_be_read_after_it_is_saved(make_ctx):
    ctx, _sent, lead_id = interested(make_ctx)
    client = web(ctx)
    client.post(f"/leads/{lead_id}/note", data={"note": "Reception says write to the assistant"}, headers=ORIGIN, follow_redirects=False)
    assert "Reception says write to the assistant" in client.get(f"/leads/{lead_id}").text


def test_a_meeting_that_has_ended_can_still_be_marked_held(make_ctx):
    """The page and the 'Record the outcome' task both say to mark it after the meeting."""
    ctx, _sent, lead_id = interested(make_ctx)
    start = utcnow() - timedelta(days=1)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        m = Meeting(lead_id=lead_id, company_id=lead.company_id, invitee_email=DAVID, starts_at=start, ends_at=start + timedelta(minutes=30),
                    status="scheduled", source="manual", title="Intro call")
        s.add(m)
        s.flush()
        mid = m.id
    assert f"/meetings/{mid}/held" in web(ctx).get("/meetings").text


def test_a_corrected_company_name_reaches_its_deal(make_ctx):
    ctx, _sent, lead_id = interested(make_ctx)
    with ctx.db.session() as s:
        company_id = s.get(Lead, lead_id).company_id
    client = web(ctx)
    client.post(f"/companies/{company_id}/name", data={"name": "Corrected Name Inc"}, headers=ORIGIN, follow_redirects=False)
    with ctx.db.session() as s:
        assert s.get(Company, company_id).name == "Corrected Name Inc"
        assert "Corrected Name Inc" in s.execute(select(Opportunity).where(Opportunity.lead_id == lead_id)).scalars().one().name


def test_a_contact_found_by_search_opens_something_about_that_contact(make_ctx):
    ctx, _sent, lead_id = interested(make_ctx)
    text = web(ctx).get("/search", params={"q": DAVID.split("@")[0]}).text
    assert f'href="/leads/{lead_id}"' in text


# ------------------------------------------------------------------ notes
def test_a_note_is_shown_escaped_on_the_deal_page_and_the_feeds_too(make_ctx):
    ctx, _sent, lead_id = interested(make_ctx)
    client = web(ctx)
    client.post(f"/leads/{lead_id}/note", data={"note": "Call <b>after</b> 3pm"}, headers=ORIGIN, follow_redirects=False)
    with ctx.db.session() as s:
        opp_id = s.execute(select(Opportunity.id).where(Opportunity.lead_id == lead_id)).scalar_one()
    for page in (f"/leads/{lead_id}", f"/opportunities/{opp_id}", "/", "/command-center"):
        text = client.get(page).text
        assert "Call &lt;b&gt;after&lt;/b&gt; 3pm" in text and "Call <b>after" not in text, page


def test_ok_a_note_stays_with_who_may_open_its_lead(org):
    lid = make_lead(org, owner="ed", email="dana@acme.example")
    oid = make_deal(org, lid, owner="eva")
    assert post(client(org, "ed"), f"/leads/{lid}/note", note="PRIVATE-WORDS").status_code == 303
    assert "PRIVATE-WORDS" in client(org, "ed").get(f"/leads/{lid}").text
    with org.db.session() as s:
        sees = can_see_lead(s, scope_for(s, {"username": "eva", "perms": permissions_of(s, "sales_executive")}), lid)
    assert ("PRIVATE-WORDS" in client(org, "eva").get(f"/opportunities/{oid}").text) == sees


def test_ok_the_full_email_of_a_sent_message_is_not_repeated_in_activity(make_ctx):
    ctx, sent, lead_id = interested(make_ctx)
    with ctx.db.session() as s:
        body = s.get(FollowUp, sent.id).body
    page = web(ctx).get(f"/leads/{lead_id}").text
    assert page.split("<h2>Activity</h2>")[1].count(body[:40]) == 0


# ------------------------------------------------------------------ meetings
def ended_meeting(ctx, lead_id, opp_id=None):
    start = utcnow() - timedelta(days=1)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        m = Meeting(lead_id=lead_id, company_id=lead.company_id, invitee_email="dana@acme.example", starts_at=start,
                    ends_at=start + timedelta(minutes=30), status="scheduled", source="manual", title="Intro call", opportunity_id=opp_id)
        s.add(m)
        s.flush()
        return m.id


def test_an_ended_meeting_marked_held_reaches_the_deal_and_the_report(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid, stage="discovery")
    mid = ended_meeting(org, lid, oid)
    eva = client(org, "eva")
    text = eva.get("/meetings").text
    for action in ("held", "no_show", "cancel"):
        assert f"/meetings/{mid}/{action}" in text
    assert post(eva, f"/meetings/{mid}/held", notes="Went well").status_code == 303
    with org.db.session() as s:
        assert s.get(Meeting, mid).status == "completed" and s.get(Opportunity, oid).next_step.startswith("Meeting held")
    assert f"/meetings/{mid}/held" not in eva.get("/meetings").text
    assert post(eva, f"/meetings/{mid}/held").status_code == 400  # once only
    res = AnalyticsReportingAgent(org).run({"kind": "daily"})
    assert res.ok and res.output.kpis["meetings_held_total"] == 1


def test_ok_an_ended_meeting_keeps_the_same_permissions(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    mid = ended_meeting(org, lid)
    ana, ed = client(org, "ana"), client(org, "ed")
    assert f"/meetings/{mid}/held" not in ana.get("/meetings").text  # read-only: sees it, cannot mark it
    assert post(ana, f"/meetings/{mid}/held").status_code == 403
    assert f"/meetings/{mid}" not in ed.get("/meetings").text and post(ed, f"/meetings/{mid}/held").status_code == 404
    with org.db.session() as s:
        assert s.get(Meeting, mid).status == "scheduled"


# ------------------------------------------------------------------ organisation pages
def test_a_sales_manager_is_told_why_an_organisation_page_does_not_open(org):
    mia = client(org, "mia")
    for path in ("/manager", "/reports"):
        r = mia.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/my?msg=")
        text = mia.get(path).text
        assert "your role sees" in text and 'href="/performance"' in text
    assert mia.get("/performance").status_code == 200
    assert mia.get("/", follow_redirects=False).headers["location"] == "/my"  # home is not a refusal
    assert "your role sees" not in mia.get("/").text
    assert client(org, "ana").get("/manager", follow_redirects=False).status_code == 200  # who sees what is unchanged
    assert client(org, "eva").get("/my", params={"msg": "your role sees"}).text.count("your role sees") == 0  # only our own words


# ------------------------------------------------------------------ they replied
def test_they_replied_says_the_answer_stays_and_keeps_the_stage(make_ctx):
    ctx, _sent, lead_id = interested(make_ctx)
    r = web(ctx).post(f"/leads/{lead_id}/replied", headers=ORIGIN, follow_redirects=False)
    assert "still waits" in unquote(r.headers["location"])
    assert lead_of(ctx).outreach_status == "interested"


def test_they_replied_still_cancels_the_next_step_of_the_sequence(make_ctx):
    ctx, sent, lead_id = interested(make_ctx)
    with ctx.db.session() as s:
        s.add(FollowUp(lead_id=lead_id, company_id=sent.company_id, contact_id=sent.contact_id, channel="email", kind="outreach", step=2,
                       status="awaiting_approval", subject="Following up", body="x", to_email=sent.to_email))
    web(ctx).post(f"/leads/{lead_id}/replied", headers=ORIGIN, follow_redirects=False)
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp.status).where(FollowUp.lead_id == lead_id, FollowUp.step == 2)).scalar_one() == "cancelled"
    assert "awaiting_approval" in [st for _i, st in answer_of(ctx, lead_id)]


@pytest.mark.parametrize("closed", ["unsubscribed", "bounced", "not_interested", "stopped"])
def test_they_replied_never_keeps_an_answer_on_a_closed_stage_and_never_reopens_it(make_ctx, closed):
    ctx, _sent, lead_id = interested(make_ctx)
    with ctx.db.session() as s:
        s.get(Lead, lead_id).outreach_status = closed
    web(ctx).post(f"/leads/{lead_id}/replied", headers=ORIGIN, follow_redirects=False)
    assert [st for _i, st in answer_of(ctx, lead_id)] == ["cancelled"]
    assert lead_of(ctx).outreach_status == closed


def test_they_replied_does_not_keep_an_answer_to_a_suppressed_address(make_ctx):
    ctx, _sent, lead_id = interested(make_ctx)
    with ctx.db.session() as s:
        suppress(s, DAVID, "unsubscribe")
    web(ctx).post(f"/leads/{lead_id}/replied", headers=ORIGIN, follow_redirects=False)
    assert [st for _i, st in answer_of(ctx, lead_id)] == ["cancelled"]


def test_they_replied_does_not_keep_an_answer_after_an_opt_out_in_the_thread(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Please unsubscribe me.", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    lead = lead_of(ctx)
    assert lead.outreach_status == "unsubscribed"
    web(ctx).post(f"/leads/{lead.id}/replied", headers=ORIGIN, follow_redirects=False)
    assert "awaiting_approval" not in [st for _i, st in answer_of(ctx, lead.id)]
    assert lead_of(ctx).outreach_status == "unsubscribed"


def test_the_they_replied_button_shows_only_after_something_was_sent(org, make_ctx):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    make_draft(org, lid)
    eva = client(org, "eva")
    assert f"/leads/{lid}/replied" not in eva.get(f"/leads/{lid}").text
    ctx, _sent, lead_id = interested(make_ctx)
    assert f"/leads/{lead_id}/replied" in web(ctx).get(f"/leads/{lead_id}").text


# ------------------------------------------------------------------ search
def test_a_contact_without_a_lead_found_by_search_opens_that_contact_only(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    with org.db.session() as s:
        co = Company(name="Quiet Co", normalized_name="quiet co", source="test")
        s.add(co)
        s.flush()
        quiet = Contact(company_id=co.id, first_name="Quinn", last_name="Fay", email="quinn@quiet.example", source="inbound")
        s.add(quiet)
        s.flush()
        quiet_id = quiet.id
    boss = client(org, "boss")
    assert f'href="/contacts?contact={quiet_id}"' in boss.get("/search", params={"q": "quinn"}).text
    page = boss.get(f"/contacts?contact={quiet_id}").text
    assert "quinn@quiet.example" in page and "dana@acme.example" not in page and 'href="/contacts"' in page
    assert "own website" not in page.split("<table", 1)[1] and "None" not in page.split("<table", 1)[1]
    assert f'href="/leads/{lid}"' in boss.get("/search", params={"q": "dana"}).text
    assert "whole" not in boss.get("/contacts?contact=abc").text and boss.get("/contacts?contact=abc").status_code == 200


def test_ok_search_and_the_contact_filter_respect_who_may_see_what(org):
    make_lead(org, owner="eva", email="dana@acme.example")
    with org.db.session() as s:
        dana = s.execute(select(Contact.id).where(Contact.email == "dana@acme.example")).scalar_one()
    ed = client(org, "ed")
    assert "dana@acme.example" not in ed.get("/search", params={"q": "dana"}).text.split("<main", 1)[-1]
    assert "dana@acme.example" not in ed.get(f"/contacts?contact={dana}").text.split("<main", 1)[-1]


# ------------------------------------------------------------------ a corrected name
def test_a_corrected_name_logs_the_deal_and_leaves_what_a_person_named_or_closed(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example", company="Acme")
    follows = make_deal(org, lid, stage="discovery", name="Acme — CRM")
    exact = make_deal(org, lid, stage="discovery", name="Acme")
    named = make_deal(org, lid, stage="discovery", name="Big renewal for Acme")
    longer = make_deal(org, lid, stage="discovery", name="Acme2 pilot")
    already = make_deal(org, lid, stage="discovery", name="Acme Inc — CRM")
    other = make_deal(org, lid, stage="discovery", name="Acme Robotics pilot")
    closed = make_deal(org, lid, stage="won", name="Acme — old project")
    with org.db.session() as s:
        company_id = s.get(Lead, lid).company_id
    assert post(client(org, "eva"), f"/companies/{company_id}/name", name="Acme Inc").status_code == 303
    assert opp_row(org, follows).name == "Acme Inc — CRM" and opp_row(org, exact).name == "Acme Inc"
    assert opp_row(org, named).name == "Big renewal for Acme" and opp_row(org, longer).name == "Acme2 pilot"
    assert opp_row(org, closed).name == "Acme — old project"
    assert opp_row(org, already).name == "Acme Inc — CRM" and opp_row(org, other).name == "Acme Robotics pilot"
    with org.db.session() as s:
        logged = s.execute(select(Activity.subject).where(Activity.opportunity_id == follows)).scalars().all()
    assert "Deal renamed with its company: Acme — CRM → Acme Inc — CRM" in logged


# ------------------------------------------------------------------ the next step
def test_a_deals_next_step_follows_the_reply(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds good, can we talk on Thursday?",
                                 in_reply_to=sent.message_id)}
    run_inbox(ctx)
    lead_id = lead_of(ctx).id
    with ctx.db.session() as s:
        opp = s.execute(select(Opportunity).where(Opportunity.lead_id == lead_id)).scalars().one()
        oid, touched = opp.id, opp.updated_at
    assert "approve the suggested reply" in opp_row(ctx, oid).next_step
    fid = answer_of(ctx, lead_id)[0][0]
    client = web(ctx)
    assert client.post(f"/followups/{fid}/approve", headers=ORIGIN, follow_redirects=False).status_code == 303
    assert "approve the suggested reply" in opp_row(ctx, oid).next_step  # approved, not gone yet: still true enough until it is sent
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(days=1)).isoformat()})
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "sent"
    deal = opp_row(ctx, oid)
    assert deal.next_step == REPLY_SENT and deal.updated_at == touched  # no activity on the deal: the stalled check still counts
    assert REPLY_SENT in client.get("/pipeline").text and "approve the suggested reply" not in client.get("/pipeline").text


def test_a_rejected_reply_says_so_and_a_typed_next_step_stays(make_ctx):
    ctx, _sent, lead_id = interested(make_ctx)
    fid = answer_of(ctx, lead_id)[0][0]
    with ctx.db.session() as s:
        opp = s.execute(select(Opportunity).where(Opportunity.lead_id == lead_id)).scalars().one()
        oid = opp.id
    web(ctx).post(f"/followups/{fid}/reject", headers=ORIGIN, follow_redirects=False)
    assert opp_row(ctx, oid).next_step == NO_REPLY
    with ctx.db.session() as s:
        s.get(Opportunity, oid).next_step = "Call Dana on Friday"
        settle_next_steps(s)
    assert opp_row(ctx, oid).next_step == "Call Dana on Friday"


def test_a_new_reply_waiting_brings_the_next_step_back(make_ctx):
    ctx, sent, lead_id = interested(make_ctx)
    with ctx.db.session() as s:
        opp = s.execute(select(Opportunity).where(Opportunity.lead_id == lead_id)).scalars().one()
        oid = opp.id
        opp.next_step = REPLY_SENT
        settle_next_steps(s)
    assert opp_row(ctx, oid).next_step == REPLY_WAITING


# ------------------------------------------------------------------ leave
def test_marking_someone_unavailable_says_how_much_work_they_hold(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    make_deal(org, lid)
    make_draft(org, lid)
    with org.db.session() as s:
        uid = s.execute(select(User.id).where(User.username == "eva")).scalar_one()
    boss = client(org, "boss")
    r = post(boss, f"/admin/users/{uid}/edit", role="sales_executive", available="")
    said = unquote(r.headers["location"])
    assert "1 open lead, 1 open deal, 1 email waiting for approval" in said and "reassign" in said
    again = post(boss, f"/admin/users/{uid}/edit", role="sales_executive", available="")  # already away: nothing new to say
    assert "open lead" not in unquote(again.headers["location"])
    with org.db.session() as s:
        assert s.get(Lead, lid).owner == "eva"  # nothing moves by itself


def test_the_owner_list_marks_who_is_away_and_offers_no_read_only_analyst(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid)
    with org.db.session() as s:
        s.execute(select(User).where(User.username == "ed")).scalar_one().available = False
    boss = client(org, "boss")
    for page in (f"/leads/{lid}", f"/opportunities/{oid}"):
        text = boss.get(page).text
        assert '<option value="ed" >ed (away)</option>' in text and '<option value="ana"' not in text
        assert '<option value="eva" selected>eva</option>' in text


def test_ok_a_read_only_owner_stays_in_the_list_so_save_does_not_unassign(org):
    lid = make_lead(org, owner="ana", email="dana@acme.example")
    assert '<option value="ana" selected>ana</option>' in client(org, "boss").get(f"/leads/{lid}").text


def new_deal_on(ctx, lead_id):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        opp = Opportunity(company_id=lead.company_id, lead_id=lead.id, name="Deal", stage="discovery")
        s.add(opp)
        s.flush()
        return assign_new(s, ctx.settings, opp, "test"), opp.id


def test_a_deal_on_an_on_leave_persons_lead_takes_the_lead_with_it_on_record(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    with org.db.session() as s:
        s.execute(select(User).where(User.username == "eva")).scalar_one().available = False
    owner, oid = new_deal_on(org, lid)
    assert owner != "eva" and lead_row(org, lid).owner == owner
    with org.db.session() as s:
        moved = s.execute(select(Assignment).where(Assignment.lead_id == lid, Assignment.opportunity_id.is_(None))).scalars().one()
        assert (moved.from_owner, moved.to_owner) == ("eva", owner)
        assert s.execute(select(Activity.id).where(Activity.lead_id == lid, Activity.subject.like("Lead moved from eva%"))).first()


def test_ok_nobody_free_leaves_the_lead_where_it_is_and_the_deal_unassigned(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    with org.db.session() as s:
        for u in s.execute(select(User)).scalars():
            u.available = False
    owner, _oid = new_deal_on(org, lid)
    assert owner is None and lead_row(org, lid).owner == "eva"


def test_ok_a_lead_owner_who_is_merely_full_keeps_the_lead(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    make_deal(org, lid, owner="eva", stage="discovery")
    with org.db.session() as s:
        s.execute(select(User).where(User.username == "eva")).scalar_one().capacity = 1
    owner, _oid = new_deal_on(org, lid)
    assert owner != "eva" and lead_row(org, lid).owner == "eva"


def test_ok_a_lead_owner_outside_the_configured_pool_keeps_the_lead(org):
    org.settings.business.setdefault("handover", {})["sales_manager"] = ["mia"]
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    owner, _oid = new_deal_on(org, lid)
    assert owner == "mia" and lead_row(org, lid).owner == "eva"


# ------------------------------------------------------------------ wording and pages
def test_a_stage_reason_keeps_its_closing_bracket(make_ctx):
    ctx = contacted(make_ctx)[0]
    lead = lead_of(ctx)
    with ctx.db.session() as s:
        log_activity(s, "Outreach: replied (Reply received (other))", "test", type_="outreach", lead_id=lead.id, company_id=lead.company_id)
        s.get(Lead, lead.id).outreach_status = "replied"
    assert "Why: Reply received (other)<" in web(ctx).get(f"/leads/{lead.id}").text


def test_activity_reads_rejected_not_reject_and_keeps_what_the_journal_reads(make_ctx):
    ctx, _sent, lead_id = interested(make_ctx)
    fid = answer_of(ctx, lead_id)[0][0]
    client = web(ctx)
    client.post(f"/followups/{fid}/reject", headers=ORIGIN, follow_redirects=False)
    text = client.get(f"/leads/{lead_id}").text
    assert f"Follow-up #{fid} rejected" in text and f"Follow-up #{fid} reject<" not in text
    assert activity_words("Follow-up #7 requeue") == "Follow-up #7 put back in the queue"
    assert activity_words("Company name corrected") == "Company name corrected"


def test_assigning_with_no_reason_leaves_no_dangling_colon(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid, owner="eva")
    boss = client(org, "boss")
    assert post(boss, f"/leads/{lid}/assign", owner="ed").status_code == 303
    with org.db.session() as s:
        row = s.execute(select(Assignment).where(Assignment.opportunity_id == oid)).scalars().first()
        assert row.reason == "with its lead"
        row.reason = "with its lead: "  # an older row, as production holds them
    assert "with its lead: )" not in boss.get(f"/leads/{lid}").text and "(manual: with its lead)" in boss.get(f"/opportunities/{oid}").text


def test_a_deal_card_calls_its_owner_the_owner(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    make_deal(org, lid, stage="discovery")
    text = client(org, "boss").get("/pipeline").text
    assert "Owner: eva" in text and "Sales Manager: eva" not in text


def test_pages_do_not_show_setting_or_category_names(make_ctx):
    ctx = contacted(make_ctx)[0]
    client = web(ctx)
    assert "CALENDAR_PROVIDER" not in client.get("/meetings").text and "no calendar is connected." in client.get("/meetings").text
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", "Re: your note", "Sounds good, but no thanks for now, maybe later?")}
    run_inbox(ctx)
    with ctx.db.session() as s:
        s.execute(select(InboundEmail)).scalars().first().status = "needs_review"
    text = client.get("/inbox?show=all").text
    assert 'value="not_interested"' in text and ">not interested</option>" in text and ">not_interested<" not in text
    assert '<option value="out_of_office" >out of office</option>' in text


def test_a_report_page_has_a_way_back_plain_words_and_fits_a_phone(make_ctx):
    ctx = contacted(make_ctx)[0]
    res = AnalyticsReportingAgent(ctx).run({"kind": "weekly"})
    assert res.ok, res.error
    with open(res.output.path_html, encoding="utf-8") as stored:
        assert "PUBLISHING_ENABLED" in stored.read()  # as written when it ran: the page says it in words
    text = web(ctx).get(f"/reports/{res.output.report_id}").text
    assert "PUBLISHING_ENABLED" not in text and "Marketing Report" not in text and "<h1>Weekly Sales Report" in text
    assert '<a class="report-back" href="/reports">' in text and "overflow-x:auto" in text


def test_an_approve_refused_for_a_removed_footer_shows_what_to_put_back(make_ctx):
    ctx, _sent, lead_id = interested(make_ctx)
    fid = answer_of(ctx, lead_id)[0][0]
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        f.body = f.body.replace("[Write", "Write").split("---")[0]
    r = web(ctx).post(f"/followups/{fid}/approve", headers=ORIGIN, data={}, follow_redirects=False)
    said = unquote(r.headers["location"])
    assert "Cannot approve" in said and 'reply with "unsubscribe"' in said and ctx.settings.company_postal_address in said
    assert [st for i, st in answer_of(ctx, lead_id) if i == fid] == ["awaiting_approval"]


# ------------------------------------------------------------------ the independent review
def test_they_replied_cancels_an_approved_answer_and_nothing_is_sent(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds good, can we talk on Thursday?",
                                 in_reply_to=sent.message_id)}
    run_inbox(ctx)
    lead_id = lead_of(ctx).id
    fid = answer_of(ctx, lead_id)[0][0]
    client = web(ctx)
    assert client.post(f"/followups/{fid}/approve", headers=ORIGIN, follow_redirects=False).status_code == 303
    r = client.post(f"/leads/{lead_id}/replied", headers=ORIGIN, follow_redirects=False)
    said = unquote(r.headers["location"])
    assert "still waits" not in said and "will not go out" in said
    assert answer_of(ctx, lead_id) == [(fid, "cancelled")]
    FakeSMTP.sent = []
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(days=1)).isoformat()})
    assert FakeSMTP.sent == [] and answer_of(ctx, lead_id) == [(fid, "cancelled")]


def test_a_stopped_lead_with_a_logged_reply_cannot_be_reopened(make_ctx):
    ctx = contacted(make_ctx)[0]
    client = web(ctx)
    lead_id = lead_of(ctx).id
    client.post(f"/leads/{lead_id}/stop_outreach", headers=ORIGIN, follow_redirects=False)
    client.post(f"/leads/{lead_id}/replied", headers=ORIGIN, follow_redirects=False)
    assert lead_of(ctx).outreach_status == "stopped"
    page = client.get(f"/leads/{lead_id}").text
    assert "/reopen_outreach" not in page and "a person logged that this company replied" in page
    r = client.post(f"/leads/{lead_id}/reopen_outreach", data={"reason": "try again"}, headers=ORIGIN, follow_redirects=False)
    assert "Not reopened" in unquote(r.headers["location"]) and lead_of(ctx).outreach_status == "stopped"


class FakeCalendar:
    name = "google"
    cancelled: list[str] = []

    def cancel_event(self, event_id):
        FakeCalendar.cancelled.append(event_id)


def calendar_meeting(ctx, lead_id, start, event_id):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        m = Meeting(lead_id=lead_id, company_id=lead.company_id, invitee_email=DAVID, starts_at=start, ends_at=start + timedelta(minutes=30),
                    status="scheduled", source="calendar", title="Intro call", event_id=event_id, provider="google")
        s.add(m)
        s.flush()
        return m.id


def test_cancelled_on_an_ended_calendar_meeting_changes_the_crm_only(make_ctx, monkeypatch):
    ctx, _sent, lead_id = interested(make_ctx)
    monkeypatch.setattr(calendar_module, "calendar_for", lambda *a, **k: FakeCalendar())
    FakeCalendar.cancelled = []
    ended = calendar_meeting(ctx, lead_id, utcnow() - timedelta(days=1), "ev-ended")
    client = web(ctx)
    assert client.post(f"/meetings/{ended}/cancel", headers=ORIGIN, data={"notes": ""}, follow_redirects=False).status_code == 303
    with ctx.db.session() as s:
        assert s.get(Meeting, ended).status == "cancelled"
        assert not s.execute(select(FollowUp.id).where(FollowUp.lead_id == lead_id, FollowUp.purpose == "meeting_cancel")).first()
    assert FakeCalendar.cancelled == []
    ahead = calendar_meeting(ctx, lead_id, utcnow() + timedelta(days=2), "ev-ahead")
    assert client.post(f"/meetings/{ahead}/cancel", headers=ORIGIN, data={"notes": ""}, follow_redirects=False).status_code == 303
    assert FakeCalendar.cancelled == ["ev-ahead"]  # one still to come is cancelled in the calendar, as before
