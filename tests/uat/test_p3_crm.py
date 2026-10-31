"""Phase 3, CRM, dashboard and PC: reopening a stopped lead (the owner: "Do not reopen opted-out leads or restart outreach
without explicit approval"), correcting a company's name, a mailbox that signs in again, the limit on replies per run, the
clock against web servers, usable contacts, and the logs. Synthetic data only; runs on Linux CI with no production copy."""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from aime import clock_check, logging_setup
from aime import mail_accounts as ma
from aime.agents.analytics import sales_kpis
from aime.agents.crm import CRMManagementAgent
from aime.agents.marketing_manager import MarketingManagerAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.cli import build_parser, cmd_scrub_logs
from aime.crm_ops import merge_companies, upsert_company
from aime.design import render as render_mod
from aime.integrations.email_smtp import EmailSender, compliance_footer
from aime.models import (Activity, Alert, AuditEvent, Company, Contact, ContentItem, FollowUp, InboundEmail, Lead, MailAccount,
                         MarketingPlan, ResearchTopic, Suppression, User)
from aime.qc.checks import check_spelling
from aime.reply_intel import process_pending
from aime.scheduler import runner
from aime.sources.base import CompanyCandidate
from aime.timeutil import as_aware_utc, utcnow
from aime.web.app import USABLE_EMAIL, dashboard_view, manager_today
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import MAIL, contacted
from tests.test_mailbox_admin import RecordingIMAP, RecordingSMTP, RefusingIMAP, add
from tests.test_outreach import POSTAL, drafts, researched
from tests.uat.test_p2_truth import HASH, _no_task_scheduler, add_email, add_lead, add_reply, client, org, set_clock, words  # noqa: F401


@pytest.fixture(autouse=True)
def _scheduler(_no_task_scheduler):  # noqa: F811
    yield


@pytest.fixture
def people(org):  # noqa: F811
    """The owner's organisation, plus those who may not reopen anything."""
    with org.db.session() as s:
        s.add_all([User(username="eva", password_hash=HASH, role="sales_executive"),
                   User(username="ann", password_hash=HASH, role="analyst"),
                   User(username="max", password_hash=HASH, role="sales_manager")])
    return org


REOPEN = "/leads/{}/reopen_outreach"


def state(ctx, lead_id) -> tuple:
    """Everything a refused or forbidden reopen must leave as it was."""
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        return (lead.outreach_status, lead.status, lead.outreach_updated_at,
                [(f.id, f.status, f.blocked_reason, f.approved_at) for f in s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id))
                 .scalars()],
                s.execute(select(func.count(Activity.id)).where(Activity.lead_id == lead_id)).scalar_one(),
                s.execute(select(func.count(AuditEvent.id)).where(AuditEvent.action == "outreach_reopen")).scalar_one())


def post_reopen(ctx, lead_id, who="sam", reason="The prospect asked us to carry on"):
    return client(ctx, who, redirects=False).post(REOPEN.format(lead_id), data={"reason": reason})


def page(ctx, lead_id, who="sam") -> str:
    r = client(ctx, who).get(f"/leads/{lead_id}")
    assert r.status_code == 200
    return r.text


# ----------------------------------------------------------------------------------------------- 1. reopening: allowed
def test_reopen_with_a_sent_email_returns_to_contacted_and_says_who_and_why(people):
    lead = add_lead(people, owner="sam", outreach="stopped")
    add_email(people, lead)
    r = post_reopen(people, lead)
    assert r.status_code == 303 and "Nothing%20was%20drafted" in r.headers["location"].replace("+", "%20")
    with people.db.session() as s:
        row = s.get(Lead, lead)
        assert row.outreach_status == "contacted"
        said = s.execute(select(Activity.subject).where(Activity.lead_id == lead, Activity.type == "outreach")).scalars().all()
        assert any("reopened by sam: The prospect asked us to carry on" in x for x in said)
        event = s.execute(select(AuditEvent).where(AuditEvent.action == "outreach_reopen")).scalar_one()
        assert event.actor == "sam" and f"lead #{lead}" in event.target and "The prospect asked us to carry on" in event.detail
    assert "Why: reopened by sam" in words(page(people, lead))


def test_reopen_with_nothing_sent_returns_to_no_stage(people):
    lead = add_lead(people, owner="sam", outreach="stopped")
    add_email(people, lead, status="cancelled")
    assert post_reopen(people, lead).status_code == 303
    with people.db.session() as s:
        assert s.get(Lead, lead).outreach_status is None


def test_reopen_after_a_test_mode_send_does_not_claim_the_prospect_was_contacted(people):
    lead = add_lead(people, owner="sam", outreach="stopped")
    fid = add_email(people, lead)
    with people.db.session() as s:
        s.get(FollowUp, fid).delivered_to = "own-test-address@us.example"
    post_reopen(people, lead)
    with people.db.session() as s:
        assert s.get(Lead, lead).outreach_status == "test_sent"


def test_reopen_approves_drafts_and_sends_nothing_and_leaves_cancelled_emails_alone(people):
    lead = add_lead(people, owner="sam", outreach="stopped")
    add_email(people, lead)
    cancelled = add_email(people, lead, status="cancelled", step=2)
    with people.db.session() as s:
        before = [(f.id, f.status, f.blocked_reason, f.approved_at, f.sent_at, f.body) for f in
                  s.execute(select(FollowUp).where(FollowUp.lead_id == lead).order_by(FollowUp.id)).scalars()]
    assert post_reopen(people, lead).status_code == 303
    with people.db.session() as s:
        after = [(f.id, f.status, f.blocked_reason, f.approved_at, f.sent_at, f.body) for f in
                 s.execute(select(FollowUp).where(FollowUp.lead_id == lead).order_by(FollowUp.id)).scalars()]
        assert s.get(FollowUp, cancelled).status == "cancelled"
    assert after == before


def test_after_a_reopen_the_next_email_comes_from_the_normal_workflow_and_waits_for_a_person(make_ctx):
    ctx, *_ = contacted(make_ctx)
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    later = TUESDAY_11AM_ET + timedelta(days=5)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": later.isoformat()})
    step2 = next(f for f in drafts(ctx) if f.step == 2)
    assert step2.status == "awaiting_approval"
    with ctx.db.session() as s:
        lead_id = s.execute(select(Lead.id)).scalar_one()
    c = client(ctx, redirects=False)
    assert c.post(f"/leads/{lead_id}/stop_outreach").status_code == 303
    assert next(f for f in drafts(ctx) if f.id == step2.id).status == "cancelled"
    SalesWorkflowAgent(ctx, sender=sender).run({"now": later.isoformat()})  # stopped: nothing new while it stays stopped
    assert [f.id for f in drafts(ctx) if f.step == 2] == [step2.id]

    assert c.post(REOPEN.format(lead_id), data={"reason": "They called us"}).status_code == 303
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).outreach_status == "contacted"
    assert next(f for f in drafts(ctx) if f.id == step2.id).status == "cancelled"
    sent_before = len(FakeSMTP.sent)
    out = SalesWorkflowAgent(ctx, sender=sender).run({"now": later.isoformat()}).output
    assert out.emails_sent == 0 and len(FakeSMTP.sent) == sent_before
    new = [f for f in drafts(ctx) if f.step == 2 and f.id != step2.id]
    assert len(new) == 1 and new[0].status == "awaiting_approval" and new[0].approved_at is None
    assert next(f for f in drafts(ctx) if f.id == step2.id).status == "cancelled"  # the old one is not brought back


def test_after_a_reopen_of_a_lead_never_emailed_a_first_email_is_drafted_and_waits(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    SalesWorkflowAgent(ctx).run({})
    with ctx.db.session() as s:
        lead_id = s.execute(select(Lead.id)).scalar_one()
    c = client(ctx, redirects=False)
    c.post(f"/leads/{lead_id}/stop_outreach")
    assert [f.status for f in drafts(ctx)] == ["cancelled"]
    SalesWorkflowAgent(ctx).run({})
    assert [f.status for f in drafts(ctx)] == ["cancelled"]
    c.post(REOPEN.format(lead_id), data={"reason": "Stopped by mistake"})
    out = SalesWorkflowAgent(ctx).run({}).output
    assert out.emails_sent == 0 and sorted(f.status for f in drafts(ctx)) == ["awaiting_approval", "cancelled"]


# ----------------------------------------------------------------------------------------------- 1. reopening: refused
def _opted_out(s, lead):
    lead.contact.consent_status = "opted_out"


def _colleague_opted_out(s, lead):
    s.add(Contact(company_id=lead.company_id, email=f"other@{lead.company.domain}", source="website", consent_status="opted_out"))


def _address_suppressed(s, lead):
    s.add(Suppression(value=lead.contact.email.lower(), kind="email", reason="test"))


def _domain_suppressed(s, lead):
    s.add(Suppression(value=lead.company.domain, kind="domain", reason="test"))


def _a_sent_address_suppressed(s, lead):
    s.add(FollowUp(lead_id=lead.id, company_id=lead.company_id, channel="email", step=1, status="sent", subject="x", body="x",
                   to_email="earlier@elsewhere.example", sent_at=utcnow()))
    s.add(Suppression(value="earlier@elsewhere.example", kind="email", reason="test"))


def _stage(name):
    def set_it(s, lead):
        lead.outreach_status = name
    return set_it


def _contact_bounced(s, lead):
    lead.contact.email_status = "bounced"


def _unsubscribe_on_the_lead(s, lead):
    s.add(InboundEmail(message_id="<u1@x.example>", from_email="someone@else.example", subject="Remove me", body_text="x", new_text="x",
                       received_at=utcnow(), category="unsubscribe", status="actioned", lead_id=lead.id, is_simulated=False))


def _unsubscribe_tied_to_another_lead_of_the_company(s, lead):
    s.add(InboundEmail(message_id="<u2@x.example>", from_email=f"boss@{lead.company.domain}", subject="Remove us", body_text="x",
                       new_text="x", received_at=utcnow(), category="unsubscribe", status="actioned", is_simulated=False))


def _stop_said_someone_asked(s, lead):
    s.add(Activity(subject="Outreach: stopped (someone@x.example at this company asked not to be contacted)", actor="inbox", type="outreach",
                   lead_id=lead.id, company_id=lead.company_id))


def _disqualified(s, lead):
    lead.status = "disqualified"


def _merged(s, lead):
    lead.status = "merged"


def _competitor(s, lead):
    lead.company.is_competitor = True


def _holds_sending(s, lead):
    s.add(InboundEmail(message_id="<h1@x.example>", from_email="stranger@else.example", subject="About them", body_text="x", new_text="x",
                       received_at=utcnow(), category="other", status="needs_review", holds_sending=True, lead_id=lead.id,
                       company_id=lead.company_id, is_simulated=False))


def _delivery_report_unread(s, lead):
    s.add(InboundEmail(message_id="<b1@x.example>", from_email="mailer-daemon@x.example", subject="Undeliverable", body_text="x", new_text="x",
                       received_at=utcnow(), category="bounce", status="needs_review", lead_id=lead.id, is_simulated=False))


REFUSALS = {
    "the contact opted out": (_opted_out, "opted out"),
    "a colleague opted out": (_colleague_opted_out, "opted out"),
    "the address is suppressed": (_address_suppressed, "do-not-contact"),
    "the domain is suppressed": (_domain_suppressed, "do-not-contact"),
    "an address written to is suppressed": (_a_sent_address_suppressed, "do-not-contact"),
    "stage unsubscribed": (_stage("unsubscribed"), "unsubscribed"),
    "stage bounced": (_stage("bounced"), "bounced"),
    "the contact's address bounced": (_contact_bounced, "bounced"),
    "an unsubscribe on the lead": (_unsubscribe_on_the_lead, "asked not to be contacted"),
    "an unsubscribe held by a colleague's lead": (_unsubscribe_tied_to_another_lead_of_the_company, "asked not to be contacted"),
    "the stop says someone asked": (_stop_said_someone_asked, "asked not to be contacted"),
    "disqualified": (_disqualified, "disqualified"),
    "merged": (_merged, "merged"),
    "a competitor": (_competitor, "competitor"),
    "an email waiting for review holds sending": (_holds_sending, "holds sending"),
    "a delivery failure report nobody has read": (_delivery_report_unread, "delivery failure"),
    "a stage still running": (_stage("contacted"), "only a lead whose outreach was stopped"),
    "no stage at all": (_stage(None), "only a lead whose outreach was stopped"),
    "a prospect who said no": (_stage("not_interested"), "only a lead whose outreach was stopped"),
}


@pytest.mark.parametrize("case", REFUSALS)
def test_reopen_is_refused_with_a_plain_reason_and_changes_nothing(people, case):
    set_up, said = REFUSALS[case]
    lead_id = add_lead(people, owner="sam", outreach="stopped")
    add_email(people, lead_id)
    with people.db.session() as s:
        set_up(s, s.get(Lead, lead_id))
    before = state(people, lead_id)
    r = post_reopen(people, lead_id)
    assert r.status_code == 303
    target = client(people, "sam").get(r.headers["location"])
    assert "Not reopened" in target.text and said in target.text.replace("&#39;", "'"), case
    assert state(people, lead_id) == before
    assert REOPEN.format(lead_id) not in page(people, lead_id), "the button is only there when it is allowed"


def test_a_colleagues_opt_out_through_the_real_mail_path_refuses_every_lead_of_the_company(people):
    first = add_lead(people, owner="sam", outreach="contacted")
    with people.db.session() as s:
        one = s.get(Lead, first)
        domain = one.company.domain
        second = Lead(company_id=one.company_id, source="manual", status="qualified", outreach_status="contacted", owner="sam", score=70, grade="B")
        s.add(second)
        s.flush()
        second_id = second.id
        s.add(InboundEmail(message_id="<real@x.example>", from_email=f"legal@{domain}", subject="Please remove us",
                           body_text="Please remove us from your mailing list and do not contact us again.",
                           new_text="Please remove us from your mailing list and do not contact us again.",
                           received_at=utcnow(), status="new", is_simulated=False))
    with people.db.session() as s:
        process_pending(s, people.settings, "inbox_monitor")
    with people.db.session() as s:
        assert s.get(Lead, first).outreach_status == "stopped" and s.get(Lead, second_id).outreach_status == "stopped"
    for lead_id in (first, second_id):
        before = state(people, lead_id)
        r = post_reopen(people, lead_id)
        assert "asked not to be contacted" in client(people, "sam").get(r.headers["location"]).text
        assert state(people, lead_id) == before


def test_a_lead_a_person_stopped_first_stays_refused_after_a_colleague_asks(people):
    """The stage is already "stopped", so that stop writes no line of its own: the email itself still counts."""
    lead_id = add_lead(people, owner="sam", outreach="stopped")
    with people.db.session() as s:
        domain = s.get(Lead, lead_id).company.domain
        s.add(InboundEmail(message_id="<real2@x.example>", from_email=f"legal@{domain}", subject="Please remove us",
                           body_text="Please remove us from your mailing list.", new_text="Please remove us from your mailing list.",
                           received_at=utcnow(), status="new", is_simulated=False))
    with people.db.session() as s:
        process_pending(s, people.settings, "inbox_monitor")
    before = state(people, lead_id)
    post_reopen(people, lead_id)
    assert state(people, lead_id) == before


def test_a_reason_is_required(people):
    lead_id = add_lead(people, owner="sam", outreach="stopped")
    before = state(people, lead_id)
    for reason in ("", "   \n  "):
        r = post_reopen(people, lead_id, reason=reason)
        assert r.status_code == 303 and "give%20the%20reason" in r.headers["location"].replace("+", "%20")
    assert state(people, lead_id) == before


# ----------------------------------------------------------------------------------------------- 1. reopening: who
@pytest.mark.parametrize("who", ["eva", "ann"])
def test_those_who_may_not_approve_never_see_it_and_the_server_refuses_them(people, who):
    lead_id = add_lead(people, owner="eva", outreach="stopped")
    assert REOPEN.format(lead_id) not in page(people, lead_id, who)
    before = state(people, lead_id)
    assert post_reopen(people, lead_id, who).status_code == 403
    assert state(people, lead_id) == before


def test_a_manager_who_cannot_see_the_lead_cannot_reopen_it(people):
    lead_id = add_lead(people, owner="sam", outreach="stopped")
    before = state(people, lead_id)
    assert post_reopen(people, lead_id, "max").status_code == 404
    assert state(people, lead_id) == before


def test_the_button_and_its_reason_box_show_for_an_approver_only_when_allowed(people):
    lead_id = add_lead(people, owner="sam", outreach="stopped")
    text = page(people, lead_id)
    assert REOPEN.format(lead_id) in text and 'name="reason"' in text and "required" in text
    with people.db.session() as s:
        s.get(Lead, lead_id).contact.consent_status = "opted_out"
    text = page(people, lead_id)
    assert REOPEN.format(lead_id) not in text and "cannot be reopened: someone at this company has opted out" in text


def test_only_the_dashboard_route_can_reopen(people):
    """Nothing may run by itself in a way that restarts outreach: no job, agent or command reaches it."""
    root = Path(__file__).resolve().parents[2] / "aime"
    callers = [str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*.py") if re.search(r"\breopen_stopped\(", p.read_text(encoding="utf-8"))]
    assert sorted(callers) == ["outreach.py", "web/app.py"]
    assert re.search(r"\breopen_stopped\(", (root / "outreach.py").read_text(encoding="utf-8").replace("def reopen_stopped(", "")) is None
    app_text = (root / "web" / "app.py").read_text(encoding="utf-8")
    assert app_text.count("reopen_stopped(") == 1


def test_the_stage_reason_shows_on_the_lead_page(people):
    lead_id = add_lead(people, owner="sam", outreach="contacted")
    client(people, "sam", redirects=False).post(f"/leads/{lead_id}/stop_outreach")
    assert "Why: Stopped by sam" in words(page(people, lead_id))


def test_a_lead_with_no_reason_on_record_shows_none(people):
    lead_id = add_lead(people, owner="sam", outreach="contacted")
    assert "Why:" not in words(page(people, lead_id))


# ----------------------------------------------------------------------------------------------- 2. the company's name
def _company_of(ctx, lead_id) -> int:
    with ctx.db.session() as s:
        return s.get(Lead, lead_id).company_id


def test_a_person_corrects_the_name_and_activity_says_so_and_a_waiting_draft_keeps_its_words(people):
    lead_id = add_lead(people, "Old Name", owner="eva")
    fid = add_email(people, lead_id, status="awaiting_approval", subject="About Old Name")
    cid = _company_of(people, lead_id)
    with people.db.session() as s:
        s.get(Company, cid).normalized_name = "old name"
    r = client(people, "eva", redirects=False).post(f"/companies/{cid}/name", data={"name": "  New   Name "})
    assert r.status_code == 303 and r.headers["location"].startswith(f"/leads/{lead_id}")
    with people.db.session() as s:
        co = s.get(Company, cid)
        assert (co.name, co.name_set_by, co.normalized_name) == ("New Name", "eva", "old name")
        assert "Old Name → New Name" in s.execute(select(Activity.subject).where(Activity.lead_id == lead_id, Activity.type == "override")).scalar_one()
        assert s.get(FollowUp, fid).subject == "About Old Name" and s.get(FollowUp, fid).status == "awaiting_approval"
    assert "corrected by eva" in page(people, lead_id, "eva")


def test_an_empty_name_changes_nothing_and_those_who_cannot_edit_are_refused(people):
    lead_id = add_lead(people, "Keep Name", owner="eva")
    cid = _company_of(people, lead_id)
    client(people, "eva", redirects=False).post(f"/companies/{cid}/name", data={"name": " "})
    assert client(people, "ann", redirects=False).post(f"/companies/{cid}/name", data={"name": "Bad"}).status_code == 403
    assert client(people, "max", redirects=False).post(f"/companies/{cid}/name", data={"name": "Bad"}).status_code == 404
    with people.db.session() as s:
        assert s.get(Company, cid).name == "Keep Name" and s.get(Company, cid).name_set_by is None
    assert f"/companies/{cid}/name" not in page(people, lead_id, "ann")


def test_clean_ups_and_website_checks_leave_a_persons_name_alone(make_ctx):
    def site(request):
        return httpx.Response(200, html="<html><head><title>Zentro | Great software</title></head><body>" + "words " * 200 + "</body></html>")
    ctx = make_ctx(handler=site)
    with ctx.db.session() as s:
        mine = Company(name="zentro.io", normalized_name="zentro io", domain="zentro.io", source="test", name_set_by="sam")
        linked = Company(name="Linked https://linked.example", normalized_name="linked", source="test", name_set_by="sam")
        theirs = Company(name="other.io", normalized_name="other io", domain="other.io", source="test")
        s.add_all([mine, linked, theirs])
        s.flush()
        ids = mine.id, linked.id
        theirs_id = theirs.id
    assert CRMManagementAgent(ctx).run({"operations": ["verify_websites", "hygiene", "dedupe"]}).ok
    with ctx.db.session() as s:
        assert s.get(Company, ids[0]).name == "zentro.io"
        assert s.get(Company, ids[1]).name == "Linked https://linked.example"
        assert s.get(Company, theirs_id).website_checked_at is not None


def test_dedupe_finds_the_company_by_its_old_and_its_new_name_and_a_merge_keeps_the_correction(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="New Brand", normalized_name="old brand", state="TX", source="sec_edgar", name_set_by="sam")
        s.add(co)
        s.flush()
        for name in ("Old Brand Inc", "New Brand LLC"):
            found, created = upsert_company(s, CompanyCandidate(name=name, source="sec_edgar", source_url="https://sec.example/1", state="TX"))
            assert not created and found.id == co.id and found.name == "New Brand"
        other, created = upsert_company(s, CompanyCandidate(name="New Brand LLC", source="sec_edgar", source_url="https://sec.example/2", state="NY"))
        assert created and other.id != co.id  # the same rule as ever: a name alone matches only in the same state
        keeper = Company(name="Kept", normalized_name="kept", source="test")
        s.add(keeper)
        s.flush()
        merge_companies(s, keeper, co, "sam")
        assert (keeper.name, keeper.name_set_by) == ("New Brand", "sam")


# ----------------------------------------------------------------------------------------------- 3. T20
def _refused_primary(make_ctx):
    ctx = make_ctx(outbound_email_enabled=True, company_postal_address="1 Sample St, Austin, TX 78701")
    aid = add(ctx)
    with ctx.db.session() as s:
        ma.change(s, "owner", aid, "enable")
        ma.change(s, "owner", aid, "primary")
        s.add(Alert(level="error", source="sales_workflow", message="refused", key="mail:sign_in", acknowledged=True))
    return ctx, aid


def _board(ctx):
    with ctx.db.session() as s:
        return next(b for b in manager_today(ctx, s)["board"] if b["name"] == "Email sending")


def _test(ctx, aid, imap=RecordingIMAP):
    with ctx.db.session() as s, ctx.http() as http:
        return ma.test_connection(s, ctx.settings, s.get(MailAccount, aid), http, "owner", imap_factory=imap, smtp_factory=RecordingSMTP)[0]


def test_a_corrected_mailbox_stops_reading_failed_once_test_connection_passes(make_ctx):
    ctx, aid = _refused_primary(make_ctx)
    FakeSMTP.sent = []
    assert _board(ctx)["status"] == "FAILED"
    assert _test(ctx, aid)
    row = _board(ctx)
    assert row["status"] != "FAILED" and "refused" not in row["detail"]
    assert FakeSMTP.sent == []  # the test signs in and sends nothing


def test_a_failed_test_or_another_mailbox_passing_does_not_end_it(make_ctx):
    ctx, aid = _refused_primary(make_ctx)
    assert not _test(ctx, aid, imap=RefusingIMAP)
    assert _board(ctx)["status"] == "FAILED"
    other = add(ctx, {"provider": "gmail", "email": "two@us.example", "secret": "x" * 16})
    with ctx.db.session() as s:
        ma.change(s, "owner", other, "enable")
    assert _test(ctx, other)
    assert _board(ctx)["status"] == "FAILED"


# ----------------------------------------------------------------------------------------------- 4. replies per run
def _approved_reply(ctx, i, *, purpose=None, kind=None, status="approved"):
    with ctx.db.session() as s:
        co = Company(name=f"Asker {i}", normalized_name=f"asker {i}", source="test", domain=f"asker{i}.example")
        s.add(co)
        s.flush()
        ct = Contact(company_id=co.id, email=f"person@asker{i}.example", source="inbound", consent_status="inquiry", first_name="Pat")
        s.add(ct)
        s.flush()
        lead = Lead(company_id=co.id, contact_id=ct.id, source="inbound", status="qualified", score=70, grade="B", outreach_status="replied")
        s.add(lead)
        s.flush()
        in_reply_to = None
        if kind == "response":
            in_reply_to = f"<asked{i}@asker{i}.example>"
            s.add(InboundEmail(message_id=in_reply_to, from_email=ct.email, subject="Question", body_text="x", new_text="x",
                               received_at=utcnow() - timedelta(hours=3), status="actioned", category="question", lead_id=lead.id, is_simulated=False))
        f = FollowUp(lead_id=lead.id, company_id=co.id, contact_id=ct.id, channel="email", kind=kind, purpose=purpose, step=1, status=status,
                     subject=f"Reply {i}", body="Thanks for writing." + compliance_footer(ctx.settings), to_email=ct.email,
                     due_at=TUESDAY_11AM_ET - timedelta(hours=1), in_reply_to=in_reply_to,
                     approved_at=utcnow() - timedelta(hours=1) if status == "approved" else None,
                     approved_by="sam" if status == "approved" else None)
        s.add(f)
        s.flush()
        return f.id


def _run_sends(ctx):
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    return SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()}).output


def test_replies_leave_at_most_the_limit_per_run_and_the_rest_wait_for_the_next(make_ctx):
    ctx = make_ctx(**MAIL)
    FakeSMTP.sent = []
    ids = [_approved_reply(ctx, i) for i in range(13)]
    waiting = _approved_reply(ctx, 99, status="awaiting_approval")
    out = _run_sends(ctx)
    assert out.emails_sent == 10 and len(FakeSMTP.sent) == 10
    with ctx.db.session() as s:
        rows = [s.get(FollowUp, i) for i in ids]
        later = [f for f in rows if f.status == "approved"]
        assert len(later) == 3 and all("Nothing is lost" in f.blocked_reason and "next run" in f.blocked_reason for f in later)
        assert all(f.approved_at for f in later)
    assert _run_sends(ctx).emails_sent == 3 and len(FakeSMTP.sent) == 13
    assert len({m["To"] for m in FakeSMTP.sent}) == 13  # none twice
    with ctx.db.session() as s:
        assert all(s.get(FollowUp, i).status == "sent" and s.get(FollowUp, i).blocked_reason is None for i in ids)
        assert s.get(FollowUp, waiting).status == "awaiting_approval"  # never skips the approval


def test_meeting_notes_go_first_and_are_not_held_by_the_limit(make_ctx):
    ctx = make_ctx(**MAIL)
    ctx.settings.business["followup"]["replies_per_run"] = 2
    FakeSMTP.sent = []
    answers = [_approved_reply(ctx, i, kind="response") for i in range(4)]
    notes = [_approved_reply(ctx, 50 + i, kind="response", purpose="meeting_confirmation") for i in range(3)]
    assert _run_sends(ctx).emails_sent == 5
    assert [m["To"] for m in FakeSMTP.sent[:3]] == [f"person@asker{50 + i}.example" for i in range(3)]
    with ctx.db.session() as s:
        assert all(s.get(FollowUp, i).status == "sent" for i in notes)
        assert sorted(s.get(FollowUp, i).status for i in answers) == ["approved", "approved", "sent", "sent"]


def test_the_limit_does_not_touch_outreach_or_a_stop(make_ctx):
    """A reply cancelled for the do-not-contact list is cancelled at once, whatever the limit says."""
    ctx = make_ctx(**MAIL)
    ctx.settings.business["followup"]["replies_per_run"] = 1
    FakeSMTP.sent = []
    fids = [_approved_reply(ctx, i) for i in range(3)]
    with ctx.db.session() as s:
        s.add(Suppression(value="person@asker2.example", kind="email", reason="test"))
    assert _run_sends(ctx).emails_sent == 1
    with ctx.db.session() as s:
        assert s.get(FollowUp, fids[2]).status == "cancelled"


# ----------------------------------------------------------------------------------------------- 5. the clock and web servers
@pytest.fixture
def no_servers_heard():
    clock_check._servers.clear()
    yield
    clock_check._servers.clear()


def _date(gap: timedelta) -> str:
    """What a server says when this PC's clock is ahead of it by `gap`."""
    return format_datetime(as_aware_utc(utcnow() - gap), usegmt=True)


def _ask(make_ctx, gaps: dict[str, timedelta], extra=None):
    asked = []

    def server(request):
        asked.append(request.url.host)
        headers = {"date": _date(gaps[request.url.host])}
        headers.update((extra or {}).get(request.url.host, {}))
        return httpx.Response(200, headers=headers, text="ok")
    ctx = make_ctx(handler=server)
    with ctx.http() as http:
        for host in gaps:
            http.get(f"https://{host}/")
    assert asked == list(gaps)  # exactly the requests made: the check adds none
    return ctx


def _clock_alerts(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Alert).where(Alert.key == clock_check.SERVERS_ALERT_KEY)).scalars().all()


def _judge(ctx):
    with ctx.db.session() as s:
        clock_check.check_servers(s, ctx.settings)


H3 = timedelta(hours=3)
OK = timedelta(seconds=2)


def test_two_servers_off_by_the_same_amount_raise_one_warning_naming_the_clock(make_ctx, no_servers_heard):
    ctx = _ask(make_ctx, {"a.example": H3, "b.example": H3 + OK})
    _judge(ctx)
    (alert,) = _clock_alerts(ctx)
    assert alert.level == "warning" and "clock" in alert.message and "3.0 hours ahead" in alert.message and clock_check.ADVICE in alert.message
    with ctx.http() as http:
        http.get("https://a.example/"), http.get("https://b.example/")
    _judge(ctx)
    assert len(_clock_alerts(ctx)) == 1 and not _clock_alerts(ctx)[0].acknowledged


def test_a_clock_behind_the_servers_says_behind(make_ctx, no_servers_heard):
    ctx = _ask(make_ctx, {"a.example": -H3, "b.example": -H3})
    _judge(ctx)
    assert "behind" in _clock_alerts(ctx)[0].message


@pytest.mark.parametrize("gaps", [
    {"a.example": H3},  # a single wrong server
    {"a.example": H3, "b.example": OK},
    {"a.example": H3, "b.example": H3, "c.example": OK, "d.example": OK},  # most servers agree with this PC
    {"a.example": H3, "b.example": timedelta(hours=30)},  # wrong each in its own way: the servers, not the clock
    {"a.example": H3, "www.a.example": H3},  # one site twice
    {"a.example": OK, "b.example": OK},
])
def test_no_warning_without_several_servers_agreeing_against_the_clock(make_ctx, no_servers_heard, gaps):
    ctx = _ask(make_ctx, gaps)
    _judge(ctx)
    assert _clock_alerts(ctx) == []


def test_a_page_kept_by_a_cache_is_no_evidence(make_ctx, no_servers_heard):
    age = {"age": str(int(H3.total_seconds()))}
    ctx = _ask(make_ctx, {"a.example": H3, "b.example": H3}, extra={"a.example": age, "b.example": age})
    _judge(ctx)
    assert _clock_alerts(ctx) == []


def test_a_server_on_this_pc_or_an_unreadable_date_counts_for_nothing(make_ctx, no_servers_heard):
    def server(request):
        return httpx.Response(200, headers={"date": "not a date" if request.url.host == "bad.example" else _date(H3)})
    ctx = make_ctx(handler=server)
    with ctx.http() as http:
        for url in ("http://localhost:11434/", "http://127.0.0.1:8765/", "https://bad.example/", "https://one.example/"):
            http.get(url)
    assert list(clock_check._servers) == ["one.example"]


def test_it_closes_when_the_servers_agree_again(make_ctx, no_servers_heard):
    ctx = _ask(make_ctx, {"a.example": H3, "b.example": H3})
    _judge(ctx)
    assert _clock_alerts(ctx)[0].resolved_at is None
    clock_check.note_server_date("a.example", _date(OK))
    _judge(ctx)
    assert _clock_alerts(ctx)[0].resolved_at is None  # one server says nothing either way
    clock_check.note_server_date("a.example", _date(OK))
    clock_check.note_server_date("b.example", _date(OK))
    _judge(ctx)
    assert _clock_alerts(ctx)[0].resolved_at is not None


def test_a_person_who_confirmed_the_clock_is_not_told_again_that_day(make_ctx, no_servers_heard):
    ctx = _ask(make_ctx, {"a.example": H3, "b.example": H3})
    _judge(ctx)
    clock_check.confirm(ctx, None)
    clock_check.note_server_date("a.example", _date(H3))
    clock_check.note_server_date("b.example", _date(H3))
    _judge(ctx)
    assert _clock_alerts(ctx)[0].resolved_at is not None and len(_clock_alerts(ctx)) == 1


def test_a_tick_compares_after_its_jobs_and_the_warning_stops_nothing(make_ctx, no_servers_heard, monkeypatch):
    ctx = make_ctx()
    clock_check.note_server_date("a.example", _date(H3))
    clock_check.note_server_date("b.example", _date(H3))
    monkeypatch.setattr(runner, "due_jobs", lambda *a, **k: [])
    assert runner.tick(ctx)["status"] == "ok"
    assert len(_clock_alerts(ctx)) == 1
    assert runner.tick(ctx)["status"] == "ok"  # the next tick works as usual


def test_a_failure_while_comparing_never_stops_the_tick(make_ctx, no_servers_heard, monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(clock_check, "check_servers", lambda *a: 1 / 0)
    monkeypatch.setattr(runner, "due_jobs", lambda *a, **k: [])
    assert runner.tick(ctx)["status"] == "ok"


# ----------------------------------------------------------------------------------------------- 6. usable contacts
def _contact(s, name, email, company_domain=None, **fields):
    co = Company(name=name, normalized_name=name.lower(), source="test", domain=company_domain)
    s.add(co)
    s.flush()
    s.add(Contact(company_id=co.id, email=email, source="website", email_status="published", first_name="Sam", last_name="Lee",
                  discovered_at=utcnow() - timedelta(hours=2), **fields))
    return co.id


def _usable_everywhere(ctx) -> dict:
    with ctx.db.session() as s:
        since = utcnow() - timedelta(days=1)
        count = lambda model, *where: s.execute(select(func.count(model.id)).where(*where)).scalar_one()  # noqa: E731
        card = next(c for c in dashboard_view(ctx, s, 7)["cards"] if c["label"] == "Usable contacts")
        return {"query": count(Contact, *USABLE_EMAIL), "home": card["value"], "report": sales_kpis(count, since)["usable_emails_total"]}


def test_a_contact_suppressed_by_domain_is_not_counted_as_usable(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        _contact(s, "Fine", "a@fine.example", "fine.example")
        _contact(s, "Found later", "b@listed.example", "listed.example")  # found after the domain went on the list
        _contact(s, "Elsewhere", "c@other.example", "listed2.example")  # its company's domain is on the list, not the address's
        _contact(s, "Person", "D@person.example", "person.example")  # the address itself, written with a capital
        s.add_all([Suppression(value="listed.example", kind="domain", reason="t"), Suppression(value="listed2.example", kind="domain", reason="t"),
                   Suppression(value="d@person.example", kind="email", reason="t")])
    seen = _usable_everywhere(ctx)
    assert seen == {"query": 1, "home": 1, "report": 1}, seen
    assert "a@fine.example" in client(ctx).get("/contacts?reachable=1").text
    assert "b@listed.example" not in client(ctx).get("/contacts?reachable=1").text


def test_usable_contacts_are_unchanged_without_a_suppression(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        _contact(s, "One", "a@one.example", "one.example")
        _contact(s, "Two", "b@two.example")
        s.add(Contact(email="c@three.example", source="website", email_status="published", first_name="N", last_name="C"))  # no company
    assert _usable_everywhere(ctx) == {"query": 3, "home": 3, "report": 3}


# ----------------------------------------------------------------------------------------------- 7. spelling
BODY = "Our team keeps your data clean and your reports on time."


def test_tech_is_a_word_and_real_mistakes_still_fail():
    assert check_spelling("Tech teams need clean data", BODY).status == "pass"
    for wrong, word in (("teh", "teh"), ("Teck teams", "teck"), ("Managment of data", "managment"), ("Recieve a report", "recieve")):
        result = check_spelling(wrong, BODY)
        assert result.status == "fail" and word in result.detail, wrong


# ----------------------------------------------------------------------------------------------- 8. the marketing plan's words
def _plan(ctx):
    assert MarketingManagerAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        plan = s.execute(select(MarketingPlan).order_by(MarketingPlan.id.desc())).scalars().first()
        return plan.rationale, [b["service"] for b in plan.briefs]


def _topics(ctx, service, n=3):
    with ctx.db.session() as s:
        for i in range(n):
            s.add(ResearchTopic(topic=f"story {service} {i}", service=service, source="test", source_url=f"https://x.example/{service}/{i}"))


def test_the_plan_says_trend_favours_a_service_only_when_it_decided_a_brief(make_ctx):
    ctx = make_ctx()
    services = list(ctx.settings.section("targeting").get("services"))
    ctx.settings.business["content"]["posts_per_week"] = 3
    newest = services[-1]
    _topics(ctx, newest)
    rationale, briefs = _plan(ctx)  # nothing posted yet: all equally due, so the trend breaks the tie
    assert briefs[0] == newest and any(f"favours {newest}" in r for r in rationale)


def test_the_plan_is_silent_about_a_trend_that_changed_nothing(make_ctx):
    ctx = make_ctx()
    services = list(ctx.settings.section("targeting").get("services"))
    ctx.settings.business["content"]["posts_per_week"] = 3
    with ctx.db.session() as s:
        for i, svc in enumerate(services):  # each last featured on its own day: the order is decided before the trend counts
            s.add(ContentItem(service=svc, industry="saas", title=f"T{i}", body="b", hashtags=[], status="draft", generator="template",
                              fingerprint=f"fp{i}", created_at=utcnow() - timedelta(days=len(services) - i)))
    _topics(ctx, services[-1])
    rationale, briefs = _plan(ctx)
    assert services[-1] not in briefs and not any("favours" in r for r in rationale)
    assert briefs == services[:3]


# ----------------------------------------------------------------------------------------------- 9. scrub-logs
LINES = [
    b'INFO:     127.0.0.1:50000 - "GET /integrations/oauth/callback?code=SECRETCODE&state=SECRETSTATE HTTP/1.1" 303 See Other\r\n',
    b'INFO:     127.0.0.1:50001 - "GET /leads/7 HTTP/1.1" 200 OK\r\n',
    b'INFO:     127.0.0.1:50002 - "POST /setup?token=ONETIME HTTP/1.1" 200 OK\n',
    b'Traceback mentions /leads?x=1 and "a quoted?thing" outside any request\n',
    b'\xff\xfe bytes that are not text "GET /x?y=1" with no version\n',
    b'[2026-10-31 10:00:00] Port 8765 accepts connections but gives no page\n',
]
CLEAN = [
    b'INFO:     127.0.0.1:50000 - "GET /integrations/oauth/callback HTTP/1.1" 303 See Other\r\n',
    LINES[1],
    b'INFO:     127.0.0.1:50002 - "POST /setup HTTP/1.1" 200 OK\n',
    *LINES[3:],
]


def _log(tmp_path, lines=LINES) -> Path:
    path = tmp_path / "dashboard.log"
    path.write_bytes(b"".join(lines))
    return path


def test_scrub_removes_query_strings_and_keeps_every_other_byte(tmp_path):
    path = _log(tmp_path)
    assert logging_setup.scrub_access_log(path) == {"lines": 6, "changed": 2, "state": "cleaned"}
    assert path.read_bytes() == b"".join(CLEAN)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dashboard.log"]  # no temp file left


def test_scrub_is_safe_to_run_twice_and_leaves_a_clean_file_alone(tmp_path):
    path = _log(tmp_path)
    logging_setup.scrub_access_log(path)
    stamp = path.stat().st_mtime_ns
    assert logging_setup.scrub_access_log(path) == {"lines": 6, "changed": 0, "state": "clean"}
    assert path.read_bytes() == b"".join(CLEAN) and path.stat().st_mtime_ns == stamp


def test_scrub_keeps_what_was_appended_while_it_worked(tmp_path, monkeypatch):
    path = _log(tmp_path)
    real = Path.replace
    tries = []

    def replace(self, target):
        if not tries:  # the dashboard was writing just then
            tries.append(1)
            with path.open("ab") as fh:
                fh.write(b'INFO:     127.0.0.1:1 - "GET /late?secret=1 HTTP/1.1" 200 OK\n')
            raise PermissionError("in use")
        return real(self, target)
    monkeypatch.setattr(Path, "replace", replace)
    done = logging_setup.scrub_access_log(path, sleep=lambda _: None)
    assert done["state"] == "cleaned" and done["changed"] == 3
    assert path.read_bytes() == b"".join(CLEAN) + b'INFO:     127.0.0.1:1 - "GET /late HTTP/1.1" 200 OK\n'


def test_scrub_leaves_a_log_held_open_as_it_was(tmp_path, monkeypatch):
    path = _log(tmp_path)

    def refuse(self, target):
        raise PermissionError("in use")
    monkeypatch.setattr(Path, "replace", refuse)
    done = logging_setup.scrub_access_log(path, sleep=lambda _: None)
    assert done == {"lines": 6, "changed": 0, "state": "in use"}
    assert path.read_bytes() == b"".join(LINES) and sorted(p.name for p in tmp_path.iterdir()) == ["dashboard.log"]


@pytest.mark.skipif(os.name != "nt", reason="Windows refuses to replace a file another handle holds; Linux does not")
def test_scrub_with_a_real_handle_held_open(tmp_path):
    path = _log(tmp_path)
    with path.open("a", encoding="utf-8") as holder:
        done = logging_setup.scrub_access_log(path, sleep=lambda _: None)
        assert done["state"] == "in use" and path.read_bytes() == b"".join(LINES)
        holder.write("still writing\n")
    assert b"still writing" in path.read_bytes()
    assert logging_setup.scrub_access_log(path)["state"] == "cleaned"


def test_scrub_stops_if_the_log_was_rotated_under_it(tmp_path, monkeypatch):
    path = _log(tmp_path)
    real_stat = Path.stat
    calls = []

    def stat(self, **kw):
        if self == path:
            calls.append(1)
            return SimpleNamespace(st_size=0)
        return real_stat(self, **kw)
    monkeypatch.setattr(Path, "stat", stat)
    assert "rotated" in logging_setup.scrub_access_log(path)["state"]
    monkeypatch.undo()
    assert path.read_bytes() == b"".join(LINES)


def test_the_command_prints_counts_only_and_cleans_older_copies(tmp_path, capsys):
    (tmp_path / "dashboard.log").write_bytes(b"".join(LINES))
    (tmp_path / "dashboard.log.1").write_bytes(LINES[0])
    (tmp_path / "dashboard.err.log").write_bytes(LINES[0])
    args = build_parser().parse_args(["scrub-logs"])
    assert cmd_scrub_logs(args, SimpleNamespace(settings=SimpleNamespace(logs_dir=tmp_path))) == 0
    printed = capsys.readouterr().out
    assert "dashboard.log: cleaned: 6 line(s) read, 2 cleaned" in printed and "dashboard.log.1: cleaned" in printed
    assert not re.search(r"SECRET|ONETIME|oauth|setup|/leads", printed)
    assert b"SECRETCODE" not in (tmp_path / "dashboard.log").read_bytes() + (tmp_path / "dashboard.log.1").read_bytes()
    assert b"SECRETCODE" in (tmp_path / "dashboard.err.log").read_bytes()  # not asked: named files only, with --file


def test_the_command_can_be_pointed_at_a_named_file_and_says_nothing_of_a_missing_one(tmp_path, capsys):
    other = tmp_path / "other.log"
    other.write_bytes(LINES[0])
    args = build_parser().parse_args(["scrub-logs", "--file", str(other), "--file", str(tmp_path / "gone.log")])
    assert cmd_scrub_logs(args, SimpleNamespace(settings=SimpleNamespace(logs_dir=tmp_path))) == 0
    assert b"SECRETCODE" not in other.read_bytes() and "gone.log: no file" in capsys.readouterr().out


def test_the_dashboards_output_rotates_by_size_and_holds_no_file_between_writes(tmp_path):
    path = tmp_path / "logs" / "dashboard.log"
    path.parent.mkdir()
    log = logging_setup.AppendingLog(path, max_bytes=100, keep=2)
    for i in range(30):
        print(f"line {i:04d} of the dashboard's output", file=log)
    names = sorted(p.name for p in path.parent.iterdir())
    assert names[-2:] == ["dashboard.log.1", "dashboard.log.2"]
    assert all(p.stat().st_size <= 200 for p in path.parent.iterdir())
    assert "line 0029" in (path.parent / "dashboard.log.1").read_text(encoding="utf-8")
    assert all(text.endswith(("\n",)) for text in (p.read_text(encoding="utf-8") for p in path.parent.iterdir()))  # whole lines
    log.write("one more\n")
    path.replace(tmp_path / "moved.log")  # on Windows this fails if anything still holds the file
    assert not log.isatty()


def test_a_write_that_fails_does_not_stop_the_dashboard(tmp_path):
    log = logging_setup.AppendingLog(tmp_path)  # a folder: the write fails, and nothing else happens
    assert log.write("x\n") == 2


# ----------------------------------------------------------------------------------------------- 10. the browser's saved answer
ANSWER = {"os_password_blank": False, "os_password_last_changed": "134"}


def test_the_answer_lives_where_only_this_user_can_change_it_and_is_shared_by_every_render():
    if os.environ.get("LOCALAPPDATA"):
        assert render_mod.OS_PASSWORD_STATE.is_relative_to(os.environ["LOCALAPPDATA"])
        assert render_mod.OS_PASSWORD_STATE.parent.name == "aime" and "Temp" not in render_mod.OS_PASSWORD_STATE.parts


def test_the_old_answer_comes_along_so_the_move_costs_no_sign_in(tmp_path, monkeypatch):
    old, new = tmp_path / "old.json", tmp_path / "private" / "new.json"
    monkeypatch.setattr(render_mod, "OLD_OS_PASSWORD_STATE", old)
    monkeypatch.setattr(render_mod, "OS_PASSWORD_STATE", new)
    render_mod._bring_over_old_answer()
    assert not new.exists()  # nothing known yet: nothing written
    old.write_text(render_mod.json.dumps({**ANSWER, "intruder": "value"}), encoding="utf-8")
    render_mod._bring_over_old_answer()
    assert render_mod.json.loads(new.read_text(encoding="utf-8")) == ANSWER and old.exists()
    render_mod.seed_profile(tmp_path / "p1")
    assert render_mod.json.loads((tmp_path / "p1" / "Local State").read_text(encoding="utf-8")) == {"password_manager": ANSWER}
    old.write_text(render_mod.json.dumps({"os_password_blank": True}), encoding="utf-8")
    render_mod._bring_over_old_answer()
    assert render_mod.json.loads(new.read_text(encoding="utf-8")) == ANSWER  # the private copy wins from then on


def test_a_changed_answer_file_cannot_put_anything_else_into_a_browser_profile(tmp_path, monkeypatch):
    state = tmp_path / "answer.json"
    monkeypatch.setattr(render_mod, "OS_PASSWORD_STATE", state)
    state.write_text(render_mod.json.dumps({"os_password_blank": False, "profile.exit_type": "x", "os_password_x": {"deep": 1}}), encoding="utf-8")
    render_mod.seed_profile(tmp_path / "p1")
    assert render_mod.json.loads((tmp_path / "p1" / "Local State").read_text(encoding="utf-8")) == {"password_manager": {"os_password_blank": False}}
    state.write_text(render_mod.json.dumps(["not", "a", "dict"]), encoding="utf-8")
    render_mod.seed_profile(tmp_path / "p2")
    assert not (tmp_path / "p2").exists()


def test_saving_the_answer_makes_its_folder(tmp_path, monkeypatch):
    new = tmp_path / "private" / "new.json"
    monkeypatch.setattr(render_mod, "OS_PASSWORD_STATE", new)
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "Local State").write_text(render_mod.json.dumps({"password_manager": {**ANSWER, "other": 1}}), encoding="utf-8")
    render_mod.keep_answer(tmp_path / "p")
    assert render_mod.json.loads(new.read_text(encoding="utf-8")) == ANSWER


# ----------------------------------------------------------------------------------------------- 11. the "Scoring moved" notice
def _moved(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Alert).where(Alert.key == "leads:moved")).scalars().all()


def test_the_notice_keeps_the_days_running_count_across_runs_and_does_not_count_twice_in_a_run(make_ctx):
    ctx = make_ctx()
    agent = LeadQualificationAgent(ctx)
    with ctx.db.session() as s:
        one, told = Counter(nurture=2), Counter()
        agent._say_moved(s, one, told)
        one.update(nurture=1)  # the same run, a batch later: its total so far
        agent._say_moved(s, one, told)
        agent._say_moved(s, one, told)
    (alert,) = _moved(ctx)
    assert "moved 3 lead(s) today: 3 to nurture" in alert.message
    with ctx.db.session() as s:  # a later run
        agent._say_moved(s, Counter(nurture=1, qualified=2), Counter())
    (alert,) = _moved(ctx)
    assert "moved 6 lead(s) today: 4 to nurture, 2 to qualified" in alert.message


def test_the_notice_starts_again_on_a_new_day_and_a_run_that_moved_nothing_changes_nothing(make_ctx, monkeypatch):
    ctx = make_ctx()
    agent = LeadQualificationAgent(ctx)
    with ctx.db.session() as s:
        agent._say_moved(s, Counter(nurture=5), Counter())
        agent._say_moved(s, Counter(), Counter())
    assert "moved 5 lead(s) today" in _moved(ctx)[0].message
    set_clock(monkeypatch, utcnow() + timedelta(days=1))
    with ctx.db.session() as s:
        agent._say_moved(s, Counter(qualified=1), Counter())
    assert "moved 1 lead(s) today: 1 to qualified" in _moved(ctx)[-1].message


# ----------------------------------------------------------------------------------------------- 13. the docs say it
def test_the_operations_guide_says_how_to_reopen_the_replies_limit_and_scrub_logs():
    text = (Path(__file__).resolve().parents[2] / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "Reopen outreach" in text and "replies_per_run" in text and "scrub-logs" in text
