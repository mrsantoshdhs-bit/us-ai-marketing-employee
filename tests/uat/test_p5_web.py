"""Phase 5, the web routes and their pages: approval cards, statuses, Calendly on Replies and Meetings, the owner's lift. Synthetic data only;
fake SMTP and IMAP; everything through the real routes."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import aime.timeutil
from aime.agents.crm import CRMManagementAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.assignment import escalations
from aime.db import kv_set
from aime.integrations.email_smtp import EmailSender
from aime.models import (Activity, Alert, Assignment, AuditEvent, Company, Contact, FollowUp, InboundEmail, Lead, Meeting, Opportunity, Report, ResearchSnapshot,
                         Suppression, User)
from aime.scheduler import runner
from aime.scoring import OLD_DEFAULT_INDUSTRY, lead_tier
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.workflows import MAILBOX_READ
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID, FakeIMAP, contacted, mail, run_inbox
from tests.test_outreach import POSTAL, approve_all, drafts, lead_of, researched
from tests.uat.test_uat_email import ORIGIN, web
from tests.uat.test_uat_sales_journey import add_people, client, make_deal, make_lead, post, where

SENDING = {"company_postal_address": "1 Sample St, Austin, TX 78701", "outbound_email_enabled": True, "smtp_host": "smtp.example", "smtp_from": "hello@us.example"}
PAGE = {**ORIGIN, "accept": "text/html"}
A = ("8:30pm", "Tuesday, 8 December 2026", "Tue, 8 Dec 2026")     # 15:00 UTC
B = ("9:30pm", "Wednesday, 9 December 2026", "Wed, 9 Dec 2026")   # 16:00 UTC
_n = [0]


def company_of(ctx):
    return lead_of(ctx).company_id


def told(r):
    return where(r) + r.text


# ------------------------------------------------------------------ crm-2: an approved email goes back to draft
def approved_email(make_ctx, **settings):
    ctx = researched(make_ctx, **POSTAL, **settings)
    SalesWorkflowAgent(ctx).run({"send": False})
    approve_all(ctx)
    (f,) = drafts(ctx)
    assert f.status == "approved"
    return ctx, f


def test_an_approved_email_can_be_corrected_after_the_company_name_was_corrected(make_ctx):
    ctx, f = approved_email(make_ctx)
    c = web(ctx)
    said = c.post(f"/companies/{company_of(ctx)}/name", data={"name": "New Name Inc"}, headers=ORIGIN, follow_redirects=False)
    assert "Back to draft" in unquote(said.headers["location"])
    assert f"/followups/{f.id}/back_to_draft" in c.get("/followups").text
    assert c.post(f"/followups/{f.id}/back_to_draft", headers=ORIGIN, follow_redirects=False).status_code == 303
    with ctx.db.session() as s:
        now = s.get(FollowUp, f.id)
        assert (now.status, now.approved_by, now.approved_at, now.authorization_id) == ("awaiting_approval", None, None, None)
        assert s.get(Lead, now.lead_id).outreach_status == "drafted"
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "email_back_to_draft")).first()
    # it cannot go until a person approves it again
    res = SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert res.ok
    with ctx.db.session() as s:
        assert s.get(FollowUp, f.id).status == "awaiting_approval"
    assert c.post(f"/followups/{f.id}/save", data={"subject": "About New Name Inc", "body": f.body}, headers=ORIGIN, follow_redirects=False).status_code == 303
    assert c.post(f"/followups/{f.id}/approve", headers=ORIGIN, data={"qc_override": "yes"}, follow_redirects=False).status_code == 303
    with ctx.db.session() as s:
        now = s.get(FollowUp, f.id)
        assert now.status == "approved" and now.subject == "About New Name Inc" and s.get(Lead, now.lead_id).outreach_status == "approved"


def test_save_from_a_stale_page_does_not_undo_an_approval(make_ctx):
    ctx, f = approved_email(make_ctx)
    assert web(ctx).post(f"/followups/{f.id}/save", data={"subject": "x", "body": f.body}, headers=ORIGIN, follow_redirects=False).status_code == 400
    with ctx.db.session() as s:
        assert s.get(FollowUp, f.id).status == "approved"


@pytest.mark.parametrize("status", ["sending", "uncertain", "awaiting_approval", "sent", "cancelled", "rejected", "pending"])
def test_only_an_approved_email_goes_back_to_draft(make_ctx, status):
    ctx, f = approved_email(make_ctx)
    with ctx.db.session() as s:
        s.get(FollowUp, f.id).status = status
    assert web(ctx).post(f"/followups/{f.id}/back_to_draft", headers=ORIGIN, follow_redirects=False).status_code == 400
    with ctx.db.session() as s:
        assert s.get(FollowUp, f.id).status == status


def test_a_later_step_back_to_draft_keeps_the_stage_contacted(make_ctx):
    ctx, f = approved_email(make_ctx)
    with ctx.db.session() as s:
        now = s.get(FollowUp, f.id)
        now.step = 2
        s.get(Lead, now.lead_id).outreach_status = "contacted"
    web(ctx).post(f"/followups/{f.id}/back_to_draft", headers=ORIGIN, follow_redirects=False)
    assert lead_of(ctx).outreach_status == "contacted"


def test_a_closed_stage_stays_closed_when_an_email_goes_back_to_draft(make_ctx):
    ctx, f = approved_email(make_ctx)
    with ctx.db.session() as s:
        s.get(Lead, s.get(FollowUp, f.id).lead_id).outreach_status = "not_interested"
    web(ctx).post(f"/followups/{f.id}/back_to_draft", headers=ORIGIN, follow_redirects=False)
    assert lead_of(ctx).outreach_status == "not_interested"


def test_who_may_put_an_email_back_to_draft(make_ctx):
    ctx, f = approved_email(make_ctx)
    ctx.settings.dashboard_auth = True
    add_people(ctx)
    with ctx.db.session() as s:
        s.get(FollowUp, f.id).lead.owner = "eva"
    for name, code in (("eva", 403), ("ana", 403)):
        assert post(client(ctx, name), f"/followups/{f.id}/back_to_draft").status_code == code
    with ctx.db.session() as s:
        s.get(FollowUp, f.id).lead.owner = "mia"
    assert post(client(ctx, "mia"), f"/followups/{f.id}/back_to_draft").status_code == 303


def test_a_quality_override_must_be_ticked_again(make_ctx, monkeypatch):
    ctx, f = approved_email(make_ctx)
    monkeypatch.setattr("aime.web.app.run_email_checks", lambda *a, **k: {"status": "held", "checks": []})
    c = web(ctx)
    c.post(f"/followups/{f.id}/back_to_draft", headers=ORIGIN, follow_redirects=False)
    r = c.post(f"/followups/{f.id}/approve", headers=ORIGIN, follow_redirects=False)
    assert "Quality checks held" in where(r)
    with ctx.db.session() as s:
        assert s.get(FollowUp, f.id).status == "awaiting_approval"
    c.post(f"/followups/{f.id}/approve", headers=ORIGIN, data={"qc_override": "yes"}, follow_redirects=False)
    with ctx.db.session() as s:
        assert s.get(FollowUp, f.id).status == "approved"


def test_cancel_on_a_first_email_warns_what_it_ends(make_ctx):
    ctx, _f = approved_email(make_ctx)
    assert "this ends the first email for this lead; use Back to draft to change it" in web(ctx).get("/followups").text


# ------------------------------------------------------------------ crm-16, drafts-4a: a person disqualifies a lead
def add_lead(ctx, tag, email, domain=None):
    with ctx.db.session() as s:
        co = Company(name=f"Firm {tag}", normalized_name=f"firm {tag}", source="test", domain=domain)
        s.add(co)
        s.flush()
        s.add(ResearchSnapshot(company_id=co.id, status="done", description="Firm builds reliable tools for modern operations teams.", description_url="https://firm.example/"))
        c = Contact(company_id=co.id, email=email, source="website", email_source_url="https://firm.example/c", email_status="role_address", role_category="inbox", consent_status="none")
        s.add(c)
        s.flush()
        lead = Lead(company_id=co.id, contact_id=c.id, source="research", status="qualified", score=70, grade="B", primary_service="ai_agents")
        s.add(lead)
        s.flush()
        return lead.id


def answer_draft(ctx, lead_id, to, linked=False, message_id="<q@x.example>"):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        row = InboundEmail(mailbox_uid="u" + message_id, message_id=message_id, from_email=to, status="actioned", subject="A question", body_text="A question from them", new_text="A question from them",
                           category="question", lead_id=lead_id, company_id=lead.company_id)
        s.add(row)
        s.flush()
        f = FollowUp(lead_id=lead_id, company_id=lead.company_id, contact_id=lead.contact_id, channel="email", step=1, status="awaiting_approval", kind="response",
                     due_at=TUESDAY_11AM_ET - timedelta(hours=1), to_email=to, in_reply_to=message_id, subject="Re: A question",
                     body="Hello.\n\nTo opt out, reply unsubscribe.\n1 Sample St, Austin, TX 78701")
        s.add(f)
        s.flush()
        if linked:
            row.suggested_followup_id = f.id
        return f.id


def send_run(ctx, when=TUESDAY_11AM_ET):
    FakeSMTP.sent = []
    return SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run({"now": when.isoformat()}).output


def approve(ctx, fid):
    return TestClient(create_app(ctx)).post(f"/followups/{fid}/approve", headers=ORIGIN, follow_redirects=False)


def test_an_approved_answer_does_not_go_after_the_lead_is_disqualified(make_ctx):
    ctx = make_ctx(**SENDING)
    lead_id = add_lead(ctx, "answer", "pat@answer.example", "answer.example")
    fid = answer_draft(ctx, lead_id, "pat@answer.example")
    assert approve(ctx, fid).status_code == 303
    r = TestClient(create_app(ctx)).post(f"/leads/{lead_id}/status", headers=ORIGIN, data={"status": "disqualified", "note": "not a fit"}, follow_redirects=False)
    assert "1 approved email cancelled" in where(r)
    send_run(ctx)
    assert FakeSMTP.sent == []
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        assert f.status == "cancelled" and "not a fit" in f.blocked_reason and "None" not in f.blocked_reason


def test_the_cancel_reason_never_prints_none(make_ctx):
    ctx = make_ctx(**SENDING)
    lead_id = add_lead(ctx, "answer", "pat@answer.example", "answer.example")
    fid = answer_draft(ctx, lead_id, "pat@answer.example")
    approve(ctx, fid)
    TestClient(create_app(ctx)).post(f"/leads/{lead_id}/status", headers=ORIGIN, data={"status": "disqualified"})
    with ctx.db.session() as s:
        assert "None" not in s.get(FollowUp, fid).blocked_reason


def test_an_answer_approved_after_a_real_reply_on_a_disqualified_lead_still_goes(make_ctx):
    """The owner's order of things: the person disqualifies, they write, a person chooses to answer. Nothing at send stops that."""
    ctx = make_ctx(**SENDING)
    lead_id = add_lead(ctx, "late", "pat@late.example", "late.example")
    TestClient(create_app(ctx)).post(f"/leads/{lead_id}/status", headers=ORIGIN, data={"status": "disqualified"})
    fid = answer_draft(ctx, lead_id, "pat@late.example")
    assert approve(ctx, fid).status_code == 303
    send_run(ctx)
    assert [m["To"] for m in FakeSMTP.sent] == ["pat@late.example"]


def test_a_waiting_answer_to_a_real_email_stays_and_a_first_email_does_not(make_ctx):
    ctx = make_ctx(**SENDING)
    lead_id = add_lead(ctx, "keep", "pat@keep.example", "keep.example")
    kept = answer_draft(ctx, lead_id, "pat@keep.example", linked=True)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        lead.outreach_status = "drafted"
        first = FollowUp(lead_id=lead_id, company_id=lead.company_id, contact_id=lead.contact_id, channel="email", step=1, status="awaiting_approval", kind="outreach",
                         due_at=TUESDAY_11AM_ET, to_email="pat@keep.example", subject="Hi", body="b")
        s.add(first)
        s.flush()
        gone = first.id
    r = TestClient(create_app(ctx)).post(f"/leads/{lead_id}/status", headers=ORIGIN, data={"status": "disqualified"}, follow_redirects=False)
    assert "still waits on Follow-ups" in where(r) and "1 queued email cancelled" in where(r)
    with ctx.db.session() as s:
        assert s.get(FollowUp, kept).status == "awaiting_approval" and s.get(FollowUp, gone).status == "cancelled"
        assert s.get(Lead, lead_id).outreach_status is None


@pytest.mark.parametrize("status", ["sending", "uncertain", "sent"])
def test_a_stage_stays_when_its_email_may_have_gone(make_ctx, status):
    ctx = make_ctx(**SENDING)
    lid = add_lead(ctx, "gone", "pat@gone.example", "gone.example")
    with ctx.db.session() as s:
        lead = s.get(Lead, lid)
        lead.outreach_status = "approved"
        s.add(FollowUp(lead_id=lid, company_id=lead.company_id, channel="email", step=1, kind="outreach", status=status, to_email="pat@gone.example", subject="s", body="b"))
    TestClient(create_app(ctx)).post(f"/leads/{lid}/status", headers=ORIGIN, data={"status": "disqualified"})
    assert lead_of(ctx).outreach_status == "approved"


def test_a_contacted_lead_disqualified_by_a_person_shows_stopped(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lid = lead_of(ctx).id
    web(ctx).post(f"/leads/{lid}/status", data={"status": "disqualified"}, headers=ORIGIN)
    assert lead_of(ctx).outreach_status == "stopped"


@pytest.mark.parametrize("status", ["disqualified", "merged"])
def test_outreach_on_a_closed_lead_cannot_be_approved(make_ctx, status):
    ctx = researched(make_ctx, **POSTAL)
    SalesWorkflowAgent(ctx).run({"send": False})
    (f,) = drafts(ctx)
    with ctx.db.session() as s:
        s.get(FollowUp, f.id).lead.status = status
    r = web(ctx).post(f"/followups/{f.id}/approve", headers=ORIGIN, data={"qc_override": "yes"}, follow_redirects=False)
    assert "Cannot approve" in where(r)
    with ctx.db.session() as s:
        assert s.get(FollowUp, f.id).status == "awaiting_approval"


# ------------------------------------------------------------------ crm-15, 7b, 14, 7a, 10, 11, 12, 5
def test_merged_is_not_a_status_a_person_sets(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    lid = lead_of(ctx).id
    c = web(ctx)
    assert c.post(f"/leads/{lid}/status", data={"status": "merged"}, headers=ORIGIN, follow_redirects=False).status_code == 400
    assert lead_of(ctx).status != "merged"
    page = c.get(f"/leads/{lid}").text
    form = page[page.index(f'action="/leads/{lid}/status"'):page.index('placeholder="Reason (optional)"')]
    assert "merged" not in form
    assert ">merged<" in c.get("/leads").text  # still a filter


def test_saving_the_same_status_writes_no_change_to_the_history(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    lid = lead_of(ctx).id
    status = lead_of(ctx).status
    c = web(ctx)
    r = c.post(f"/leads/{lid}/status", data={"status": status, "note": "typed words"}, headers=ORIGIN, follow_redirects=False)
    assert "Status unchanged" in where(r)
    with ctx.db.session() as s:
        assert not s.execute(select(Activity).where(Activity.lead_id == lid, Activity.subject.like(f"Status {status} → {status}%"))).first()
        assert s.execute(select(Activity).where(Activity.lead_id == lid, Activity.type == "note", Activity.body == "typed words")).first()


def test_a_note_of_spaces_changes_nothing(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    lid = lead_of(ctx).id
    web(ctx).post(f"/leads/{lid}/note", data={"note": "    "}, headers=ORIGIN)
    with ctx.db.session() as s:
        assert not s.execute(select(Activity).where(Activity.lead_id == lid, Activity.type == "note")).first()


def test_a_note_stays_readable_after_many_status_changes(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    lid = lead_of(ctx).id
    c = web(ctx)
    c.post(f"/leads/{lid}/note", data={"note": "Call the assistant in January"}, headers=ORIGIN)
    for x in ["nurture", "qualified"] * 27:
        c.post(f"/leads/{lid}/status", data={"status": x}, headers=ORIGIN)
    page = c.get(f"/leads/{lid}").text
    assert "Call the assistant in January" in page and f"/leads/{lid}?activity=all" in page
    assert c.get(f"/leads/{lid}?activity=all").text.count("(manual)") == 54


def test_the_old_name_still_finds_the_lead_after_a_correction(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    c = web(ctx)
    with ctx.db.session() as s:
        old = s.get(Company, cid).name
    c.post(f"/companies/{cid}/name", data={"name": "Zzz Renamed"}, headers=ORIGIN)
    assert f"/leads/{lid}" in c.get(f"/leads?q={old}").text
    assert "Zzz Renamed" in c.get(f"/companies?q={old}").text
    assert f"/leads/{lid}" in c.get(f"/search?q={old}").text


def test_a_search_for_a_legal_suffix_alone_does_not_list_everything(make_ctx):
    ctx = make_ctx()
    make_lead(ctx, company="Acme")
    assert "/leads/" not in web(ctx).get("/leads?q=Inc").text.split("</form>", 1)[1]


def test_a_sent_email_found_by_search_opens_where_sent_mail_is_listed(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    assert f"/followups?show=sent#f{sent.id}" in web(ctx).get(f"/search?q={DAVID}").text


def test_a_deal_reopened_after_a_correction_takes_the_new_name(make_ctx):
    ctx = make_ctx()
    lid = make_lead(ctx, owner="eva", company="Oldname")
    oid = make_deal(ctx, lid, owner="eva", stage="lost", name="Oldname — CRM")
    other = make_deal(ctx, lid, owner="eva", stage="lost", name="Oldname Partners")
    with ctx.db.session() as s:
        cid = s.get(Lead, lid).company_id
    c = web(ctx)
    c.post(f"/companies/{cid}/name", data={"name": "Newname"}, headers=ORIGIN)
    c.post(f"/opportunities/{oid}", data={"stage": "discovery", "shown_stage": "lost"}, headers=ORIGIN)
    c.post(f"/opportunities/{other}", data={"stage": "discovery", "shown_stage": "lost"}, headers=ORIGIN)
    with ctx.db.session() as s:
        assert s.get(Opportunity, oid).name == "Newname — CRM"
        assert s.get(Opportunity, other).name == "Oldname Partners"  # a person named it differently


@pytest.mark.parametrize("typed, saved", [("1,25,000", 125000.0), ("1,250", 1250.0), ("$1,250.50", 1250.5), ("1250", 1250.0), ("12,50", None), ("1,2", None), ("1,2500", None)])
def test_a_comma_in_an_amount(make_ctx, typed, saved):
    ctx = make_ctx()
    lid = make_lead(ctx, owner="eva")
    oid = make_deal(ctx, lid, owner="eva", stage="proposal")
    c = web(ctx)
    r = c.post(f"/opportunities/{oid}/quotes", data={"amount": typed}, headers=ORIGIN, follow_redirects=False)
    v = c.post(f"/opportunities/{oid}", data={"stage": "proposal", "shown_stage": "proposal", "est_value": typed}, headers=ORIGIN, follow_redirects=False)
    from aime.models import Quote
    with ctx.db.session() as s:
        quote = s.execute(select(Quote.amount)).scalar()
        assert quote == saved and s.get(Opportunity, oid).est_value == saved
    if saved:
        assert f"{saved:,.2f}" in where(r) and f"{saved:,.2f}" in where(v)


def test_a_lead_whose_deal_is_being_worked_is_not_parked_as_inactive(make_ctx):
    ctx = make_ctx()
    lid = make_lead(ctx, owner="eva", email="dana@acme.example")
    oid = make_deal(ctx, lid, owner="eva", stage="proposal", value=1000.0)
    with ctx.db.session() as s:
        s.get(Lead, lid).last_activity_at = datetime(2026, 1, 1)
    c = web(ctx)
    c.post(f"/opportunities/{oid}", data={"stage": "negotiation", "shown_stage": "proposal", "next_step": "call Tuesday"}, headers=ORIGIN)
    with ctx.db.session() as s:
        assert s.get(Lead, lid).last_activity_at > datetime(2026, 2, 1)
        s.get(Lead, lid).last_activity_at = datetime(2026, 1, 1)
    c.post(f"/opportunities/{oid}/quotes", data={"amount": "1000"}, headers=ORIGIN)
    CRMManagementAgent(ctx).run({"operations": ["stale"]})
    with ctx.db.session() as s:
        assert s.get(Lead, lid).status == "qualified"


# ------------------------------------------------------------------ crm-3b, 3c: what a merge left
def two_leads(ctx, company_status="merged"):
    with ctx.db.session() as s:
        co = Company(name="Two Leads", normalized_name="two leads", source="test", domain="twolead.example")
        s.add(co)
        s.flush()
        keeper = Lead(company_id=co.id, source="research", status="qualified", score=70, grade="B")
        other = Lead(company_id=co.id, source="research", status=company_status, score=70, grade="B", owner="ed")
        s.add_all([keeper, other])
        s.flush()
        when = utcnow() - timedelta(days=3)
        s.add(FollowUp(lead_id=other.id, company_id=co.id, channel="email", step=1, kind="outreach", status="sent", to_email="pat@twolead.example", subject="Sent before the merge", body="b", sent_at=when))
        s.add(FollowUp(lead_id=other.id, company_id=co.id, channel="email", step=2, kind="outreach", status="uncertain", to_email="pat@twolead.example", subject="May have gone", body="b", send_started_at=when))
        s.add(InboundEmail(mailbox_uid="r1", message_id="<r1@x.example>", from_email="pat@twolead.example", lead_id=other.id, company_id=co.id, subject="Their answer", status="reviewed", category="question"))
        return keeper.id, other.id


def test_the_keepers_page_shows_mail_from_the_merged_lead(make_ctx):
    ctx = make_ctx()
    keeper, other = two_leads(ctx)
    page = web(ctx).get(f"/leads/{keeper}").text
    assert "Sent before the merge" in page and "May have gone" in page and "Their answer" in page and f'href="/leads/{other}"' in page


def test_a_sibling_lead_that_is_not_merged_is_listed_too(make_ctx):
    ctx = make_ctx()
    keeper, _other = two_leads(ctx, company_status="nurture")
    assert "Sent before the merge" in web(ctx).get(f"/leads/{keeper}").text


def test_a_lead_one_may_not_open_shows_only_a_date(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    add_people(ctx)
    keeper, _other = two_leads(ctx, company_status="nurture")
    with ctx.db.session() as s:
        s.get(Lead, keeper).owner = "zoe"
    page = client(ctx, "zoe").get(f"/leads/{keeper}").text
    assert "Sent before the merge" not in page and "pat@twolead.example" not in page and "an email on a lead you cannot open" in page


def test_the_approval_card_says_the_address_was_already_emailed(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    SalesWorkflowAgent(ctx).run({"send": False})
    (f,) = drafts(ctx)
    other = make_lead(ctx, company="Earlier Co")
    with ctx.db.session() as s:
        s.add(FollowUp(lead_id=other, channel="email", step=1, kind="outreach", status="sent", to_email=f.to_email.upper(), subject="s", body="b", sent_at=datetime(2026, 10, 3, 15)))
    page = web(ctx).get("/followups").text
    assert f"This address was already emailed on 2026-10-03 (lead #{other})" in page
    assert approve(ctx, f.id).status_code == 303  # display only


def test_a_rehearsal_to_our_own_address_is_not_called_emailed(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    SalesWorkflowAgent(ctx).run({"send": False})
    (f,) = drafts(ctx)
    other = make_lead(ctx, company="Earlier Co")
    with ctx.db.session() as s:
        s.add(FollowUp(lead_id=other, channel="email", step=1, kind="outreach", status="sent", to_email=f.to_email, delivered_to="me@us.example", subject="s", body="b", sent_at=datetime(2026, 10, 3, 15)))
    assert "already emailed" not in web(ctx).get("/followups").text


def test_an_interested_merged_lead_is_not_on_the_interested_card(make_ctx):
    from aime.web.app import INTERESTED_LEADS
    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="Merged Co", normalized_name="merged co", source="test")
        s.add(co)
        s.flush()
        s.add(Lead(company_id=co.id, source="research", status="merged", outreach_status="interested"))
        s.flush()
        assert s.execute(select(func.count(Lead.id)).where(*INTERESTED_LEADS)).scalar_one() == 0


def test_a_reply_on_a_merged_lead_with_no_open_sibling_stays_for_a_person(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        s.get(Lead, lead_of(ctx).id).status = "merged"
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds good, I am interested. Let's talk.", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    with ctx.db.session() as s:
        assert s.execute(select(InboundEmail)).scalars().first().status == "needs_review"


# ------------------------------------------------------------------ journey-2a, replies-13, replies-2d: what the card and Replies show
def test_the_draft_card_shows_what_they_wrote(make_ctx):
    ctx = make_ctx(**SENDING)
    lid = add_lead(ctx, "words", "pat@words.example", "words.example")
    answer_draft(ctx, lid, "pat@words.example", linked=True)
    page = web(ctx).get("/followups").text
    assert "What they wrote" in page and "A question from them" in page


def test_the_card_says_they_are_away(make_ctx):
    ctx = make_ctx(**SENDING)
    lid = add_lead(ctx, "away", "pat@away.example", "away.example")
    answer_draft(ctx, lid, "pat@away.example")
    assert "They replied that they are away" not in web(ctx).get("/followups").text
    with ctx.db.session() as s:
        s.add(InboundEmail(mailbox_uid="o1", message_id="<o1@x.example>", from_email="pat@away.example", lead_id=lid, status="actioned", subject="Out of office", category="out_of_office"))
    assert "They replied that they are away" in web(ctx).get("/followups").text


def test_replies_opens_the_whole_email_when_it_holds_more_than_the_new_words(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        s.add(InboundEmail(mailbox_uid="f1", message_id="<f1@x.example>", from_email="p@x.example", status="needs_review", subject="Fwd", category="other",
                           new_text="See below", body_text="See below\n\nFrom: boss\nPlease stop writing to us"))
        s.add(InboundEmail(mailbox_uid="f2", message_id="<f2@x.example>", from_email="q@x.example", status="needs_review", subject="Plain", category="other", new_text="Same", body_text="Same"))
    page = web(ctx).get("/inbox").text
    assert "Please stop writing to us" in page and page.count("The whole email") == 1


# ------------------------------------------------------------------ drafts-9: display only
def test_the_card_warns_when_the_prospect_sells_ai_agents_too(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    SalesWorkflowAgent(ctx).run({"send": False})
    (f,) = drafts(ctx)
    pitch = "At Us, we build AI agents that complete real tasks inside your systems, with human oversight."
    with ctx.db.session() as s:
        s.get(FollowUp, f.id).body = f"Hi\n\n{pitch}\n\nBye"
        s.add(ResearchSnapshot(company_id=f.company_id, status="done", description="We build AI agents for dentists."))
    c = web(ctx)
    assert "This company sells AI agents itself; check the pitch line" in c.get("/followups").text
    with ctx.db.session() as s:
        s.get(FollowUp, f.id).body = "Hi\n\nAt Us, we build CRMs around how a team sells.\n\nBye"
    assert "sells AI agents itself" not in c.get("/followups").text


def test_a_company_that_only_uses_ai_agents_gets_no_warning(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    SalesWorkflowAgent(ctx).run({"send": False})
    (f,) = drafts(ctx)
    with ctx.db.session() as s:
        s.get(FollowUp, f.id).body = "Hi\n\nAt Us, we build AI agents that complete real tasks.\n\nBye"
        s.add(ResearchSnapshot(company_id=f.company_id, status="done", description="We run dental clinics across Texas."))
    assert "sells AI agents itself" not in web(ctx).get("/followups").text


# ------------------------------------------------------------------ journey-3b, 5b, 8b, 8c, 8d, 7
def test_approving_an_answer_a_second_reply_replaced_says_why(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Thanks. What does this usually cost for a team of ten?", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    lead = lead_of(ctx)
    with ctx.db.session() as s:
        first = s.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.kind == "response", FollowUp.status == "awaiting_approval")).scalars().one().id
    FakeIMAP.messages = {2: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "One more thing: our budget is under 10k. Is that realistic?", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    secret = "An answer a person typed."
    r = web(ctx).post(f"/followups/{first}/approve", headers=PAGE, data={"body": f"{secret}\nunsubscribe"}, follow_redirects=False)
    assert r.status_code == 303 and "wrote again" in where(r) and secret not in where(r)
    with ctx.db.session() as s:
        now = s.get(FollowUp, first)
        assert now.status == "cancelled" and secret not in now.body


def test_a_draft_cancelled_for_another_reason_does_not_say_they_wrote(make_ctx):
    ctx = make_ctx(**SENDING)
    lid = add_lead(ctx, "stop", "pat@stop.example", "stop.example")
    fid = answer_draft(ctx, lid, "pat@stop.example")
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        f.status, f.blocked_reason = "cancelled", "Stopped by eva"
    r = web(ctx).post(f"/followups/{fid}/approve", headers=PAGE, follow_redirects=False)
    assert "Stopped by eva" in where(r) and "wrote again" not in where(r)


def test_a_cancelled_draft_still_refuses_a_client_that_asks_for_no_page(make_ctx):
    ctx = make_ctx(**SENDING)
    lid = add_lead(ctx, "api", "pat@api.example", "api.example")
    fid = answer_draft(ctx, lid, "pat@api.example")
    with ctx.db.session() as s:
        s.get(FollowUp, fid).status = "cancelled"
    assert web(ctx).post(f"/followups/{fid}/approve", headers=ORIGIN, follow_redirects=False).status_code == 400


def test_a_won_deal_shows_no_next_step_from_before_it_was_won(make_ctx):
    ctx = make_ctx()
    lid = make_lead(ctx, owner="eva", email="dana@acme.example")
    oid = make_deal(ctx, lid, owner="eva", stage="proposal")
    with ctx.db.session() as s:
        s.get(Opportunity, oid).next_step = "Send the proposal"
    c = web(ctx)
    assert "Next: Send the proposal" in c.get("/pipeline").text
    c.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "won"}, follow_redirects=False)
    assert "Next: Send the proposal" not in c.get("/pipeline").text
    with ctx.db.session() as s:
        assert s.get(Opportunity, oid).next_step == "Send the proposal"  # reopening keeps it


def test_a_linked_notice_says_who_linked_it_whatever_its_status(make_ctx):
    ctx, _s, _f = contacted(make_ctx, calendly_verified_only=False)
    deliver(ctx, notice("booked", A, who="stranger@unknown-co.example"))
    with ctx.db.session() as s:
        row = s.execute(select(InboundEmail).where(InboundEmail.category == "calendly")).scalars().one()
        row.status = "reviewed"
        rid = row.id
        mid, lid = s.execute(select(Meeting.id)).scalar_one(), s.execute(select(Lead.id)).scalars().first()
    web(ctx).post(f"/meetings/{mid}/link", headers=ORIGIN, data={"lead_id": lid}, follow_redirects=False)
    with ctx.db.session() as s:
        row = s.get(InboundEmail, rid)
        assert f"linked by a person to lead #{lid}" in row.action_note and row.status == "reviewed" and row.lead_id == lid


def test_a_rep_is_told_a_manager_approves(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    add_people(ctx)
    lid = make_lead(ctx, owner="eva", email="dana@acme.example")
    with ctx.db.session() as s:
        lead = s.get(Lead, lid)
        s.add(FollowUp(lead_id=lid, company_id=lead.company_id, channel="email", step=1, kind="outreach", status="awaiting_approval", to_email="dana@acme.example", subject="s", body="b"))
    assert "Emails waiting for a manager: 1" in client(ctx, "eva").get("/my").text
    with ctx.db.session() as s:
        s.get(Lead, lid).owner = "mia"
    assert "Emails to approve: 1" in client(ctx, "mia").get("/my").text


def test_a_converted_lead_is_not_hot(make_ctx):
    ctx = make_ctx()
    lid = make_lead(ctx, owner="eva", email="dana@acme.example", company="Customer Co", status="converted")
    with ctx.db.session() as s:
        lead = s.get(Lead, lid)
        lead.outreach_status, lead.grade = "meeting", "A"
        assert lead_tier(lead) != "HOT"
        lead.status = "qualified"
        assert lead_tier(lead) == "HOT"
        lead.status = "converted"
    page = web(ctx).get("/manager").text
    section = page[page.index('id="hot"'):]
    assert f"/leads/{lid}" not in section.split("</section>", 1)[0]


def with_a_mailbox(make_ctx):
    ctx = make_ctx(network_enabled=True, imap_host="imap.example", imap_username="u@us.example", imap_password="x" * 8)
    assert ctx.settings.imap_configured
    return ctx


def test_a_manager_is_told_approved_emails_wait_for_the_mailbox(make_ctx):
    ctx = with_a_mailbox(make_ctx)
    c = web(ctx)
    runner.write_heartbeat(ctx.settings, {"state": "idle"})
    held = "held until the mailbox can be read"
    assert held in c.get("/my").text
    assert held in c.get("/manager").text  # the banner's detail
    with ctx.db.session() as s:
        kv_set(s, MAILBOX_READ, utcnow().isoformat())
    assert held not in c.get("/my").text


def test_a_rep_is_not_told_about_the_mailbox(make_ctx):
    ctx = make_ctx(dashboard_auth=True, network_enabled=True, imap_host="imap.example", imap_username="u@us.example", imap_password="x" * 8)
    add_people(ctx)
    assert "The last read of the mailbox failed" not in client(ctx, "eva").get("/my").text
    assert "The last read of the mailbox failed" in client(ctx, "mia").get("/my").text


def test_no_mailbox_set_up_is_no_hold(make_ctx):
    ctx = make_ctx()
    assert "held until the mailbox can be read" not in web(ctx).get("/my").text


# ------------------------------------------------------------------ meetings
def when(t, zone="India Standard Time"):
    return f"{t[0]} - {t[1]} ({zone})"


def notice(kind, to=A, old=None, who=DAVID, frm="Calendly <notifications@calendly.com>"):
    subject = {"booked": f"New Event: David Chen - {to[0]} {to[2]} - 30 Minute Meeting",
               "moved": f"Updated: David Chen - {to[0]} {to[2]} - 30 Minute Meeting",
               "cancelled": f"Canceled: 30 Minute Meeting with David Chen on {to[1]}"}[kind]
    former = f"Date & Time:\nFormer: {when(old)}\nUpdated: {when(to)}\n\n" if old else ""
    body = (f"Calendly\n\nEvent Type:\n30 Minute Meeting\n\nInvitee:\nDavid Chen\n\nInvitee Email:\n{who}\n\nEvent Date/Time:\n{when(to)}\n\n"
            f"Location:\nGoogle Meet\n\nInvitee Time Zone:\nEastern Time - US & Canada\n\n{former}View event in Calendly\nhttps://calendly.com/app\n")
    _n[0] += 1
    return mail(frm, subject, body, msg_id=f"<c{_n[0]}@calendly.example>")


def deliver(ctx, *raws):
    start = max(FakeIMAP.messages, default=0) + 1
    for i, raw in enumerate(raws):
        FakeIMAP.messages[start + i] = raw
    return run_inbox(ctx)


def live(ctx):
    with ctx.db.session() as s:
        return [m.starts_at.strftime("%d %H:%M") for m in s.execute(select(Meeting).where(Meeting.status == "scheduled")).scalars()]


def notice_ids(ctx):
    with ctx.db.session() as s:
        return [r.id for r in s.execute(select(InboundEmail).where(InboundEmail.category == "calendly").order_by(InboundEmail.id)).scalars()]


@pytest.fixture
def signed_only(make_ctx):
    c, _sender, _sent = contacted(make_ctx, calendly_verified_only=True, calendly_url="https://calendly.com/owner-example/30min")
    return c


@pytest.fixture
def ctx_open(make_ctx):
    c, _sender, _sent = contacted(make_ctx, calendly_verified_only=False, calendly_url="https://calendly.com/owner-example/30min")
    return c


def apply(c, rid):
    return c.post(f"/inbox/{rid}/apply_calendly", headers=ORIGIN, follow_redirects=False)


def statuses(ctx):
    with ctx.db.session() as s:
        return [s.get(InboundEmail, i).status for i in notice_ids(ctx)]


def test_notices_applied_as_replies_lists_them_give_the_same_result_as_in_order(signed_only):
    """Replies lists newest first. The two newer presses are refused and change nothing; in the order required, no live meeting is left."""
    ctx = signed_only
    deliver(ctx, notice("booked", A), notice("moved", B, old=A), notice("cancelled", B))
    booked, moved, cancelled = notice_ids(ctx)
    c = web(ctx)
    for newer, older in ((cancelled, booked), (moved, booked)):
        r = apply(c, newer)
        assert f"Older Calendly notice #{older}" in where(r) and "Apply it first" in where(r)
    assert live(ctx) == [] and statuses(ctx) == ["needs_review"] * 3
    for rid in (booked, moved, cancelled):
        assert "Applied" in where(apply(c, rid))
    assert live(ctx) == []


def test_confirming_an_older_notice_never_applies_a_newer_one(signed_only):
    ctx = signed_only
    deliver(ctx, notice("booked", A), notice("cancelled", A, frm="Calendly <notifications@evil.example>"))
    booked, forged = notice_ids(ctx)
    apply(web(ctx), booked)
    assert live(ctx) == ["08 15:00"]
    with ctx.db.session() as s:
        assert s.get(InboundEmail, forged).status == "needs_review"


def test_a_notice_marked_reviewed_no_longer_blocks_a_newer_one(signed_only):
    ctx = signed_only
    deliver(ctx, notice("booked", A), notice("booked", B))
    forged, real = notice_ids(ctx)
    c = web(ctx)
    c.post(f"/inbox/{forged}/reviewed", headers=ORIGIN, follow_redirects=False)
    assert "Applied" in where(apply(c, real))
    assert live(ctx) == ["09 16:00"]


def test_another_invitees_notice_and_an_unreadable_one_block_nothing(signed_only):
    ctx = signed_only
    deliver(ctx, notice("booked", A, who="other@elsewhere.example"), mail("Calendly <notifications@calendly.com>", "New Event: nobody", "see calendly.com", msg_id="<u1@calendly.example>"), notice("booked", B))
    rid = notice_ids(ctx)[-1]
    assert "Applied" in where(apply(web(ctx), rid))


def test_applying_checks_for_a_double_booking(signed_only):
    ctx = signed_only
    deliver(ctx, notice("booked", A, who="other@elsewhere.example"), notice("booked", A))
    c = web(ctx)
    for rid in notice_ids(ctx):
        apply(c, rid)
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.message.like("%overlaps%"))).first()


def test_a_flash_that_left_the_row_waiting_says_not_applied(signed_only):
    ctx = signed_only
    deliver(ctx, mail("Calendly <notifications@calendly.com>", "New Event: nobody at all", "Invitee Email:\nx@y.example\n\nsee calendly.com", msg_id="<u2@calendly.example>"))
    (rid,) = notice_ids(ctx)
    assert "Not applied" in where(apply(web(ctx), rid))
    with ctx.db.session() as s:
        assert s.get(InboundEmail, rid).status == "needs_review"


def test_mark_reviewed_does_not_take_away_apply(signed_only):
    deliver(signed_only, notice("booked", A))
    (rid,) = notice_ids(signed_only)
    c = web(signed_only)
    c.post(f"/inbox/{rid}/reviewed", headers=ORIGIN, follow_redirects=False)
    assert f"/inbox/{rid}/apply_calendly" in c.get("/inbox?show=all").text
    assert apply(c, rid).status_code == 303
    assert live(signed_only) == ["08 15:00"]


def test_apply_stays_for_those_who_may_approve(make_ctx):
    ctx, _s, _f = contacted(make_ctx, calendly_verified_only=True, calendly_url="https://calendly.com/owner-example/30min")
    ctx.settings.dashboard_auth = True
    add_people(ctx)
    deliver(ctx, notice("booked", A))
    (rid,) = notice_ids(ctx)
    assert post(client(ctx, "eva"), f"/inbox/{rid}/apply_calendly").status_code == 403
    assert live(ctx) == []


def test_a_rep_is_told_a_manager_applies_a_notice(make_ctx):
    ctx, _s, _f = contacted(make_ctx, calendly_verified_only=True, calendly_url="https://calendly.com/owner-example/30min")
    ctx.settings.dashboard_auth = True
    add_people(ctx)
    deliver(ctx, notice("booked", A))
    (rid,) = notice_ids(ctx)
    with ctx.db.session() as s:
        s.get(Lead, s.execute(select(Lead.id)).scalars().first()).owner = "eva"
        s.get(InboundEmail, rid).lead_id = s.execute(select(Lead.id)).scalars().first()
    page = client(ctx, "eva").get("/inbox").text
    assert "A Sales Manager applies this" in page and f"/inbox/{rid}/reviewed" not in page


def test_the_meetings_page_says_a_notice_waits(signed_only):
    deliver(signed_only, notice("booked", A))
    page = web(signed_only).get("/meetings").text
    assert "1 Calendly notice waits on Replies" in page and "waiting" in page.lower()
    apply(web(signed_only), notice_ids(signed_only)[0])
    assert "wait on Replies" not in web(signed_only).get("/meetings").text and "waits on Replies" not in web(signed_only).get("/meetings").text


def test_an_unmatched_booking_that_has_ended_can_still_be_linked(ctx_open):
    ctx = ctx_open
    deliver(ctx, notice("booked", A, who="stranger@unknown-co.example"))
    start = utcnow() - timedelta(days=1)
    with ctx.db.session() as s:
        m = s.execute(select(Meeting)).scalars().first()
        m.starts_at, m.ends_at = start, start + timedelta(minutes=30)
        mid = m.id
    assert f"/meetings/{mid}/link" in web(ctx).get("/meetings").text


def test_reclassifying_a_notice_drops_what_it_recommended(signed_only):
    deliver(signed_only, notice("booked", A))
    (rid,) = notice_ids(signed_only)
    with signed_only.db.session() as s:
        before = s.get(InboundEmail, rid).recommended_status
    assert before and "Apply" in before
    web(signed_only).post(f"/inbox/{rid}/reclassify", headers=ORIGIN, data={"category": "other"}, follow_redirects=False)
    with signed_only.db.session() as s:
        assert s.get(InboundEmail, rid).recommended_status != before


def test_no_rebook_task_on_a_lead_that_opted_out(ctx_open):
    ctx = ctx_open
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        mid = s.execute(select(Meeting.id)).scalar_one()
        lead = s.execute(select(Lead)).scalars().first()
        lead.outreach_status = "unsubscribed"
        lead.contact.consent_status = "opted_out"
    web(ctx).post(f"/meetings/{mid}/no_show", headers=ORIGIN, follow_redirects=False)
    with ctx.db.session() as s:
        assert not s.execute(select(FollowUp).where(FollowUp.subject.like("Rebook%"))).first()
        assert s.get(Meeting, mid).status == "no_show"


def test_a_rebook_task_on_a_lead_being_worked(ctx_open):
    ctx = ctx_open
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        mid = s.execute(select(Meeting.id)).scalar_one()
    web(ctx).post(f"/meetings/{mid}/no_show", headers=ORIGIN, follow_redirects=False)
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp).where(FollowUp.subject.like("Rebook%"))).first()


def test_a_move_of_a_meeting_with_no_deal_on_record(ctx_open):
    ctx = ctx_open
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        s.execute(select(Meeting)).scalars().first().opportunity_id = None
    deliver(ctx, notice("moved", B, old=A))
    assert live(ctx) == ["09 16:00"]


# ------------------------------------------------------------------ crm-4, 8a, 8b, 17, competitor-5, discovery-8a, security, pc-7
def test_a_parked_lead_is_not_promised_its_next_step(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lid = lead_of(ctx).id
    c = web(ctx)
    c.post(f"/leads/{lid}/status", data={"status": "nurture"}, headers=ORIGIN)
    page = c.get(f"/leads/{lid}").text
    assert "step 2 is drafted for approval" not in page and "none while this lead is nurture" in page
    c.post(f"/leads/{lid}/status", data={"status": "converted"}, headers=ORIGIN)
    assert "step 2 is drafted for approval" not in c.get(f"/leads/{lid}").text
    c.post(f"/leads/{lid}/status", data={"status": "qualified"}, headers=ORIGIN)
    assert "step 2 is drafted for approval" in c.get(f"/leads/{lid}").text


def test_the_owner_filter_can_find_what_a_deactivated_person_still_holds(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    add_people(ctx)
    make_lead(ctx, owner="eva")
    boss = client(ctx, "boss")
    with ctx.db.session() as s:
        uid = s.execute(select(User.id).where(User.username == "eva")).scalar_one()
        zoe = s.execute(select(User).where(User.username == "zoe")).scalar_one()
        zoe.active = False  # holds nothing
    post(boss, f"/admin/users/{uid}/deactivate")
    page = boss.get("/leads").text
    assert '<option value="eva" >eva (deactivated)</option>' in page
    assert 'value="zoe"' not in page


def test_an_escalation_is_not_addressed_to_a_deactivated_manager(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    add_people(ctx)
    lid = make_lead(ctx, owner="eva")
    oid = make_deal(ctx, lid, owner="eva")
    with ctx.db.session() as s:
        s.add(Assignment(lead_id=lid, opportunity_id=oid, to_owner="eva", method="manual", reason="", actor="boss"))
        s.flush()
        s.execute(select(User).where(User.username == "mia")).scalar_one().active = False
    with ctx.db.session() as s:
        escalations(s, ctx.settings, datetime(2030, 1, 1), "x")
    with ctx.db.session() as s:
        said = s.execute(select(Alert.message).where(Alert.message.like("Escalated%"))).scalars().one()
    assert "mia" not in said and "the administrators" in said


def test_a_read_only_person_cannot_be_given_a_lead(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    add_people(ctx)
    lid = make_lead(ctx, owner="eva")
    oid = make_deal(ctx, lid, owner="eva")
    boss = client(ctx, "boss")
    assert "read-only" in where(post(boss, f"/leads/{lid}/assign", owner="ana"))
    assert "read-only" in where(post(boss, f"/opportunities/{oid}/assign", owner="ana"))
    with ctx.db.session() as s:
        assert s.get(Lead, lid).owner == "eva"
        s.get(Lead, lid).owner = "ana"
    assert "read-only" not in where(post(boss, f"/leads/{lid}/assign", owner="ana"))  # already theirs: no refusal


def closed_by_flag(ctx, leads=2, outreach=None, opted_out=False):
    with ctx.db.session() as s:
        co = Company(name="Both", normalized_name="both", source="test", domain="both.example", is_competitor=True)
        s.add(co)
        s.flush()
        ct = Contact(company_id=co.id, email="p@both.example", source="website", email_status="valid", consent_status="opted_out" if opted_out else "none")
        s.add(ct)
        s.flush()
        ids = []
        for i in range(leads):
            lead = Lead(company_id=co.id, contact_id=ct.id, source="research", status="disqualified", score=70, grade="B", outreach_status=outreach if i == 0 else None,
                        disqualify_reason="Company appears to sell software or AI work to clients (possible competitor)")
            s.add(lead)
            s.flush()
            ids.append(lead.id)
        return co.id, ids


def status_of(ctx, lid):
    with ctx.db.session() as s:
        return s.get(Lead, lid).status


def test_clearing_a_flag_reopens_every_lead_it_closed(make_ctx):
    ctx = make_ctx()
    cid, ids = closed_by_flag(ctx)
    TestClient(create_app(ctx)).post(f"/companies/{cid}/not_competitor", headers=ORIGIN)
    assert [status_of(ctx, i) for i in ids] == ["new", "new"]
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok


def test_clearing_a_flag_never_reopens_an_opted_out_lead(make_ctx):
    ctx = make_ctx()
    cid, ids = closed_by_flag(ctx, opted_out=True)
    TestClient(create_app(ctx)).post(f"/companies/{cid}/not_competitor", headers=ORIGIN)
    assert [status_of(ctx, i) for i in ids] == ["disqualified", "disqualified"]


def test_clearing_a_flag_never_reopens_a_suppressed_lead(make_ctx):
    from aime.crm_ops import suppress
    ctx = make_ctx()
    cid, ids = closed_by_flag(ctx)
    with ctx.db.session() as s:
        suppress(s, "both.example", "asked")
    TestClient(create_app(ctx)).post(f"/companies/{cid}/not_competitor", headers=ORIGIN)
    assert [status_of(ctx, i) for i in ids] == ["disqualified", "disqualified"]


def test_clearing_a_flag_leaves_a_lead_whose_outreach_began_for_a_person(make_ctx):
    ctx = make_ctx()
    cid, ids = closed_by_flag(ctx, outreach="contacted")
    r = TestClient(create_app(ctx)).post(f"/companies/{cid}/not_competitor", headers=ORIGIN, follow_redirects=False)
    assert status_of(ctx, ids[0]) == "disqualified" and status_of(ctx, ids[1]) == "new"
    assert "1 lead stays closed" in where(r)


def test_the_lead_page_shows_the_industry_scoring_reads(make_ctx):
    ctx = make_ctx()
    lid = make_lead(ctx, company="Hiring Co")
    with ctx.db.session() as s:
        co = s.get(Lead, lid).company
        co.source, co.industry = "hn_hiring", OLD_DEFAULT_INDUSTRY
        cid = co.id
    c = web(ctx)
    assert "not stated" in c.get(f"/leads/{lid}").text
    with ctx.db.session() as s:
        assert s.get(Company, cid).industry == OLD_DEFAULT_INDUSTRY  # nothing stored is rewritten
    assert "not stated" in c.get("/leads").text


def test_a_nul_in_an_asset_name_is_not_found(make_ctx):
    assert client(make_ctx()).get("/assets/x.png%00.db").status_code == 404


def test_a_restored_report_opens_from_this_data_folder(make_ctx):
    ctx = make_ctx()
    ctx.settings.reports_dir.mkdir(parents=True, exist_ok=True)
    (ctx.settings.reports_dir / "r1.html").write_text("<html><body>Report words</body></html>", encoding="utf-8")
    with ctx.db.session() as s:
        r = Report(report_date=date(2026, 10, 1), kind="daily", path_md="r1.md", path_html="D:\\old-pc\\data\\reports\\r1.html")
        s.add(r)
        s.flush()
        rid = r.id
    c = web(ctx)
    assert "Report words" in c.get(f"/reports/{rid}").text


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET)

    def set_to(when):
        aime.timeutil.CLOCK_OFFSET = when - datetime.now(timezone.utc).replace(tzinfo=None)
        return when
    return set_to


IN_WINDOW = datetime(2026, 11, 4, 15, 0)


def test_a_long_absence_is_on_the_home_page_after_the_first_tick(make_ctx, clock):
    """The owner's home for this is Manager Today."""
    ctx = make_ctx()
    now = clock(IN_WINDOW)
    runner._note_gap(ctx, {"at": (now - timedelta(days=3)).isoformat() + "Z"}, now)
    c = TestClient(create_app(ctx), base_url="http://127.0.0.1:8765")
    assert "Nothing ran from" in c.get("/manager").text
    with ctx.db.session() as s:
        assert s.execute(select(Alert.level).where(Alert.key == "scheduler:gap")).scalars().one() == "info"  # no louder than it was


def test_a_night_asleep_is_not_on_the_manager_page(make_ctx, clock):
    ctx = make_ctx()
    now = clock(IN_WINDOW)
    runner._note_gap(ctx, {"at": (now - timedelta(hours=9)).isoformat() + "Z"}, now)
    assert "Nothing ran from" not in TestClient(create_app(ctx), base_url="http://127.0.0.1:8765").get("/manager").text


# ------------------------------------------------------------------ the owner's lift (owner's decision Q2)
def listed(ctx, value="pat@list.example", reason="asked"):
    from aime.crm_ops import suppress
    with ctx.db.session() as s:
        suppress(s, value, reason)
        return s.execute(select(Suppression.id).where(Suppression.value == value)).scalar_one()


@pytest.mark.parametrize("name, code", [("boss", 303), ("olga", 303), ("mia", 403), ("eva", 403), ("ana", 403)])
def test_only_an_administrator_lifts(make_ctx, name, code):
    ctx = make_ctx(dashboard_auth=True)
    add_people(ctx)
    sid = listed(ctx)
    assert post(client(ctx, name), f"/suppressions/{sid}/lift", reason="a mistake").status_code == code
    with ctx.db.session() as s:
        assert bool(s.get(Suppression, sid).lifted_at) is (code == 303)


def test_a_lift_needs_a_reason_and_keeps_the_entry(make_ctx):
    ctx = make_ctx()
    sid = listed(ctx)
    c = web(ctx)
    assert "give the reason" in where(c.post(f"/suppressions/{sid}/lift", headers=ORIGIN, data={"reason": "   "}, follow_redirects=False))
    with ctx.db.session() as s:
        assert s.get(Suppression, sid).lifted_at is None
    r = c.post(f"/suppressions/{sid}/lift", headers=ORIGIN, data={"reason": "added by mistake"}, follow_redirects=False)
    assert "Nothing was drafted or sent" in where(r) and "stays closed" in where(r) and "entry is kept" in where(r)
    with ctx.db.session() as s:
        row = s.get(Suppression, sid)
        assert row.lift_reason == "added by mistake" and s.execute(select(FollowUp)).first() is None


def test_a_lift_needs_the_same_origin(make_ctx):
    ctx = make_ctx()
    sid = listed(ctx)
    assert web(ctx).post(f"/suppressions/{sid}/lift", data={"reason": "x"}, headers={"origin": "http://elsewhere.example"}).status_code == 403


def test_an_erasure_shows_no_button_and_cannot_be_lifted(make_ctx):
    from aime.crm_ops import ERASURE_REASON
    ctx = make_ctx()
    sid = listed(ctx, value="gone@list.example", reason=ERASURE_REASON)
    c = web(ctx)
    page = c.get("/automation").text
    assert f"/suppressions/{sid}/lift" not in page and "gone@list.example" not in page
    assert "Not lifted" in where(c.post(f"/suppressions/{sid}/lift", headers=ORIGIN, data={"reason": "x"}, follow_redirects=False))
    with ctx.db.session() as s:
        assert s.get(Suppression, sid).lifted_at is None


def test_the_list_shows_only_to_an_administrator_and_the_lead_page_links_to_it(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    add_people(ctx)
    lid = make_lead(ctx, owner="mia", email="pat@list.example")
    sid = listed(ctx)
    action = f"/suppressions/{sid}/lift"
    assert action in client(ctx, "boss").get("/automation").text
    assert action not in client(ctx, "mia").get("/automation").text
    assert "/automation#suppressions" in client(ctx, "boss").get(f"/leads/{lid}").text
    assert "/automation#suppressions" not in client(ctx, "mia").get(f"/leads/{lid}").text


# ------------------------------------------------------------------ referral wording (owner's smallest step)
def test_a_referral_says_to_write_from_the_mailbox_yourself(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "You should talk to Sam, she looks after this: sam@elsewhere.example", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    assert "write to them from the mailbox yourself, then log it here" in web(ctx).get("/inbox?show=all").text

