"""Phase 5, drafting and sending: who may be written to, what a person is told when an approved email cannot go, and what a
mail server that never answered means. Synthetic data only, fake SMTP only."""

from __future__ import annotations

import smtplib
import ssl
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import aime
import aime.timeutil
from aime import workflows
from aime.agents.base import AgentResult
from aime.agents.company_research import CompanyResearchAgent
from aime.agents.inbox_agent import InboxOutput
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.calendar_slots import holiday_on
from aime.crm_ops import merge_companies, suppress
from aime.db import kv_set
from aime.integrations.email_smtp import EmailSender, MailServerNotReached
from aime.models import (Activity, Alert, Company, Contact, FollowUp, InboundEmail, Lead, Meeting, MeetingProposal, Opportunity,
                         ResearchSnapshot, Signal)
from aime.pipeline import pipeline_checks
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.workflows import MAILBOX_READ, job_followups, job_inbox
from tests.test_company_research import CONTACT, HOME, add_company, make_handler
from tests.test_contact_validation import FakeChecker
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID, MAIL as INBOX_MAIL, FakeIMAP, bounce_for, contacted, mail, run_inbox
from tests.test_outreach import ORIGIN, POST, POSTAL, SENDING, approve_all, drafts, lead_of, researched
from tests.test_time_and_alerts import FOOTER, MAIL, add_lead, add_mail
from tests.uat.test_uat_email import web
from tests.uat.test_uat_sales_journey import make_lead

NOW = TUESDAY_11AM_ET
IN_WINDOW = datetime(2026, 11, 4, 15, 0)  # Wednesday 10:00 New York
SUNDAY_NIGHT = datetime(2026, 11, 1, 4, 9)  # no email can leave until Monday 08:00 New York
INBOX = "info@brightline-logistics.example"
TEAM_NO_ADDRESS = "<html><body><h2>Our Team</h2><div class='card'><h3>David Chen</h3><p>Chief Operating Officer</p></div></body></html>"
TEAM_WITH_ADDRESS = ("<html><body><h2>Our Team</h2><div class='card'><h3>David Chen</h3><p>Chief Operating Officer</p></div>"
                     f"<p>David Chen</p><p>Email: <a href='mailto:{DAVID}'>{DAVID}</a></p></body></html>")
PASTE = "A message you can paste"
SEND = {"company_postal_address": "1 Sample St, Austin, TX 78701", "outbound_email_enabled": True, "smtp_host": "smtp.example", "smtp_from": "hello@us.example"}


@pytest.fixture(autouse=True)
def _empty_mailbox():
    FakeSMTP.sent = []


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET)

    def set_to(when):
        aime.timeutil.CLOCK_OFFSET = when - datetime.now(timezone.utc).replace(tzinfo=None)
        return when
    return set_to


def run(ctx, when=NOW, sender=None, **kw):
    return SalesWorkflowAgent(ctx, sender=sender or EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run({"now": when.isoformat(), **kw}).output


def tasks(ctx, like="%", status="pending"):
    with ctx.db.session() as s:
        return s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == status, FollowUp.subject.like(like))).scalars().all()


def row(ctx, fid):
    with ctx.db.session() as s:
        return s.get(FollowUp, fid)


def click(ctx, f, action):
    assert TestClient(create_app(ctx)).post(f"/followups/{f.id}/{action}", headers=ORIGIN, follow_redirects=False).status_code == 303


def open_alerts(ctx, key):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert).where(Alert.key == key, Alert.resolved_at.is_(None))).scalars()]


def lead_with_inbox(ctx, tag, email, domain=None):
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


def emailed_the_company_inbox(make_ctx):
    """A lead whose company published only info@ when it was first researched, and whose first email went there."""
    requests: list[str] = []
    pages = {"/team": ("text/html", TEAM_NO_ADDRESS), "/": ("text/html", HOME), "/contact": ("text/html", CONTACT)}
    ctx = make_ctx(handler=make_handler(requests, pages), **INBOX_MAIL)
    cid = add_company(ctx)
    with ctx.db.session() as s:
        s.add(Signal(company_id=cid, signal_type="hiring", text=POST, source="hn_hiring", source_url="https://news.ycombinator.com/item?id=123",
                     fingerprint="post-1", keywords=["manual data entry", "spreadsheets"], strength=0.8))
    assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]}).ok
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    run(ctx, NOW, sender)
    approve_all(ctx)
    run(ctx, NOW, sender)
    assert FakeSMTP.sent[0]["To"] == INBOX and lead_of(ctx).outreach_status == "contacted"

    def site_now_names_a_person():  # the normal re-check after recheck_days reads the changed team page
        pages["/team"] = ("text/html", TEAM_WITH_ADDRESS)
        ctx.http_transport = httpx.MockTransport(make_handler(requests, pages))
        assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]}).ok

    with ctx.db.session() as s:
        s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalars().first().sent_at = NOW - timedelta(days=5)
    return ctx, sender, site_now_names_a_person


def move_contact(ctx, lead_id, email="dana@swap.example"):
    """What no ordinary path does now that research keeps an emailed lead's contact: the checks must not depend on that alone."""
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        new = Contact(company_id=lead.company_id, email=email, first_name="Dana", source="website", email_source_url="https://swap.example/team",
                      email_status="role_address", role_category="executive", consent_status="none")
        s.add(new)
        s.flush()
        lead.contact_id = new.id


# ------------------------------------------------------------------ 1. the review task
@pytest.mark.parametrize("what", ["opted_out", "bounced", "answered", "first_email_rejected", "do_not_contact_list"])
def test_nobody_is_told_to_write_by_hand_to_someone_who_said_no_or_is_in_conversation(make_ctx, what):
    if what == "first_email_rejected":
        ctx = researched(make_ctx, **SENDING)
        sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
        run(ctx, NOW, sender)
        click(ctx, drafts(ctx)[0], "reject")
    else:
        ctx, sender, sent = contacted(make_ctx)
        if what == "opted_out":
            FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Please unsubscribe me from this list.", in_reply_to=sent.message_id)}
            run_inbox(ctx)
        elif what == "bounced":
            FakeIMAP.messages = {1: bounce_for(DAVID, sent.message_id)}
            run_inbox(ctx)
        elif what == "answered":
            FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds interesting. Could we talk Thursday afternoon?", in_reply_to=sent.message_id)}
            run_inbox(ctx)
            approve_all(ctx)
            assert run(ctx, NOW, sender).emails_sent == 1
        else:
            with ctx.db.session() as s:
                suppress(s, DAVID, "Asked by phone")
    run(ctx, NOW + timedelta(hours=1), sender)
    said = [t.body for t in tasks(ctx) if PASTE in (t.body or "")]
    assert not said, f"stage {lead_of(ctx).outreach_status}: {said[0][:200]}"


@pytest.mark.parametrize("stage", ["replied", "interested", "meeting", "not_interested", "bounced", "no_response", "stopped"])
def test_a_lead_that_answered_gets_no_task_saying_it_has_not_contacted_us(make_ctx, stage):
    ctx, _sender, _sent = contacted(make_ctx)
    lead = lead_of(ctx)
    with ctx.db.session() as s:
        s.get(Lead, lead.id).outreach_status = stage
    assert SalesWorkflowAgent(ctx).run({"send": False, "lead_ids": [lead.id]}).ok
    assert tasks(ctx, "Review %") == []


def test_an_opted_out_company_gets_no_review_task_with_a_message_to_paste(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Please unsubscribe me.", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    lead = lead_of(ctx)
    assert lead.outreach_status == "unsubscribed"
    assert SalesWorkflowAgent(ctx).run({"send": False, "lead_ids": [lead.id]}).ok
    assert tasks(ctx, "Review %") == []


def test_an_interested_reply_whose_answer_was_rejected_gets_no_review_task(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sure, happy to chat", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    assert lead_of(ctx).outreach_status == "interested"
    with ctx.db.session() as s:
        answer = s.execute(select(FollowUp).where(FollowUp.kind == "response")).scalars().one()
    click(ctx, answer, "reject")
    run(ctx, NOW + timedelta(hours=1), sender)
    assert tasks(ctx, "Review %") == []


def test_a_never_contacted_lead_on_a_listed_domain_gets_no_review_task(make_ctx):
    ctx = make_ctx(**SEND)
    lead_id = lead_with_inbox(ctx, "listed", "info@listed.example", "listed.example")
    with ctx.db.session() as s:
        suppress(s, "listed.example", "Imported list")
    run(ctx)
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).outreach_status is None
    assert tasks(ctx, "Review %") == []


def pending_review_task(ctx, lead_id):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        t = FollowUp(lead_id=lead_id, company_id=lead.company_id, channel="task", step=1, due_at=utcnow(), status="pending",
                     subject=f"Review {lead.company.name}", body=f"Why no email was drafted: x. {PASTE}")
        s.add(t)
        s.flush()
        return t.id


@pytest.mark.parametrize("task_first", [True, False])
def test_a_do_not_contact_entry_and_a_review_task_in_either_order_leave_no_open_task(make_ctx, task_first):
    ctx = make_ctx(**SEND)
    lead_id = lead_with_inbox(ctx, "order", "info@order.example", "order.example")
    with ctx.db.session() as s:
        s.get(Lead, lead_id).grade = "D"  # falls to a task, not a draft
    if task_first:
        run(ctx)
        assert len(tasks(ctx, "Review %")) == 1
    with ctx.db.session() as s:
        suppress(s, "order.example", "Asked by phone")
    run(ctx)
    assert tasks(ctx, "Review %") == []
    closed = tasks(ctx, "Review %", "cancelled")
    assert len(closed) == (1 if task_first else 0)
    assert all(t.blocked_reason for t in closed)  # never "done" with no reason: that blocks fresh tasks and counts as by hand


@pytest.mark.parametrize("status", ["converted", "nurture"])
def test_a_review_task_closes_when_a_lead_outside_the_qualified_loop_has_stopped(make_ctx, status):
    ctx, _sender, _sent = contacted(make_ctx)
    lead = lead_of(ctx)
    tid = pending_review_task(ctx, lead.id)
    with ctx.db.session() as s:
        row_ = s.get(Lead, lead.id)
        row_.status, row_.outreach_status = status, "meeting"
    assert SalesWorkflowAgent(ctx).run({"send": False}).ok
    t = row(ctx, tid)
    assert t.status == "cancelled" and "meeting" in t.blocked_reason


def test_a_task_a_person_or_another_rule_opened_is_not_closed_with_the_review_task(make_ctx):
    ctx, _sender, _sent = contacted(make_ctx)
    lead = lead_of(ctx)
    with ctx.db.session() as s:
        s.get(Lead, lead.id).outreach_status = "meeting"
        t = FollowUp(lead_id=lead.id, company_id=lead.company_id, channel="task", step=1, due_at=utcnow(), status="pending", subject="Record the outcome of the meeting with Pat", body="x")
        s.add(t)
        s.flush()
        tid = t.id
    assert SalesWorkflowAgent(ctx).run({"send": False}).ok
    assert row(ctx, tid).status == "pending"


def test_a_lead_nobody_stopped_still_gets_its_review_task(make_ctx):
    ctx = make_ctx(**SEND)
    lead_id = lead_with_inbox(ctx, "open", "info@open.example", "open.example")
    with ctx.db.session() as s:
        s.get(Lead, lead_id).grade = "D"
    run(ctx)
    assert [PASTE in t.body for t in tasks(ctx, "Review %")] == [True]


# ------------------------------------------------------------------ 2. the do-not-contact list and the stage
def test_a_person_added_to_the_do_not_contact_list_does_not_stay_contacted(make_ctx):
    ctx, sender, _sent = contacted(make_ctx)
    with ctx.db.session() as s:
        suppress(s, DAVID, "Asked by phone")
    run(ctx, NOW + timedelta(days=40), sender)
    assert lead_of(ctx).outreach_status == "stopped"  # not "unsubscribed": no opt-out was received


def test_a_listed_domain_stops_a_rehearsal_too(make_ctx):
    ctx = make_ctx(outbound_test_recipients="me@us.example", **SEND)
    lead_id = lead_with_inbox(ctx, "rehearse", "pat@rehearse.example", "rehearse.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).outreach_status == "test_sent"
        suppress(s, "rehearse.example", "Asked by phone")
    run(ctx, NOW + timedelta(hours=1))
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).outreach_status == "stopped"


def test_stopping_for_the_list_leaves_an_answer_to_a_colleague_who_wrote_to_us(make_ctx):
    ctx, sender, _sent = contacted(make_ctx)
    lead = lead_of(ctx)
    with ctx.db.session() as s:
        answer = FollowUp(lead_id=lead.id, company_id=lead.company_id, channel="email", kind="response", step=1, status="awaiting_approval",
                          to_email="colleague@brightline-logistics.example", subject="Re: q", body="x", due_at=NOW)
        queued = FollowUp(lead_id=lead.id, company_id=lead.company_id, channel="email", kind="outreach", step=2, status="awaiting_approval",
                          to_email=DAVID, subject="s", body="x", due_at=NOW)
        s.add_all([answer, queued])
        s.flush()
        ids = answer.id, queued.id
        suppress(s, DAVID, "Asked by phone")
    run(ctx, NOW + timedelta(hours=1), sender, send=False)
    assert row(ctx, ids[0]).status == "awaiting_approval"
    assert row(ctx, ids[1]).status == "cancelled"


@pytest.mark.parametrize("order", ["entry_run_recheck", "entry_recheck_run", "recheck_entry_run"])
def test_a_do_not_contact_entry_on_the_inbox_is_not_undone_by_a_re_check_in_any_order(make_ctx, order):
    ctx, sender, site_now_names_a_person = emailed_the_company_inbox(make_ctx)

    def entry():
        with ctx.db.session() as s:
            suppress(s, INBOX, "Asked by phone")

    steps = {"entry_run_recheck": (entry, lambda: run(ctx, NOW, sender), site_now_names_a_person),
             "entry_recheck_run": (entry, site_now_names_a_person),
             "recheck_entry_run": (site_now_names_a_person, entry)}[order]
    for step in steps:
        step()
    run(ctx, NOW + timedelta(hours=1), sender)
    run(ctx, NOW + timedelta(days=40), sender)
    assert [f for f in drafts(ctx) if f.step > 1] == []
    assert len(FakeSMTP.sent) == 1


# ------------------------------------------------------------------ 3. who was written to
def test_step_2_does_not_follow_up_to_a_person_who_never_had_step_1_after_research_changed_the_contact(make_ctx):
    ctx = make_ctx(**SEND)
    lead_id = lead_with_inbox(ctx, "swap", "info@swap.example", "swap.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    move_contact(ctx, lead_id)
    run(ctx, NOW + timedelta(days=4, minutes=5))
    with ctx.db.session() as s:
        second = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.channel == "email", FollowUp.step == 2)).scalars().first()
    assert second is None or second.to_email == "info@swap.example"
    (task,) = tasks(ctx, "Decide by hand%")
    assert "write by hand" in task.body and PASTE not in task.body
    run(ctx, NOW + timedelta(days=4, hours=2))
    assert len(tasks(ctx, "Decide by hand%")) == 1
    with ctx.db.session() as s:
        assert s.execute(select(func.count(Activity.id)).where(Activity.lead_id == lead_id, Activity.subject.like("No further email drafted%"))).scalar_one() == 1


def test_a_closed_hand_task_stays_closed_and_the_lead_is_parked_not_left_contacted(make_ctx):
    ctx = make_ctx(**SEND)
    lead_id = lead_with_inbox(ctx, "park", "info@park.example", "park.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    move_contact(ctx, lead_id)
    run(ctx, NOW + timedelta(days=4, minutes=5))
    (task,) = tasks(ctx, "Decide by hand%")
    with ctx.db.session() as s:
        s.get(FollowUp, task.id).status = "done"
    run(ctx, NOW + timedelta(days=8))
    assert tasks(ctx, "Decide by hand%") == []
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        assert (lead.status, lead.outreach_status) == ("nurture", "no_response")


def test_the_one_check_in_is_not_drafted_to_someone_never_written_to(make_ctx):
    ctx = make_ctx(**SEND)
    ctx.settings.business["outreach"]["reengage_after_days"] = 90
    lead_id = lead_with_inbox(ctx, "check", "info@check.example", "check.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    move_contact(ctx, lead_id)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        lead.status, lead.outreach_status = "nurture", "no_response"
    for days in (91, 92):
        run(ctx, NOW + timedelta(days=days))
    assert [f for f in drafts(ctx) if f.purpose == "reengage"] == []
    assert len(tasks(ctx, "Decide by hand%")) == 1
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).outreach_status == "no_response"  # no set_status: nothing restarted


def test_the_check_in_still_goes_to_the_address_that_was_written_to(make_ctx):
    ctx, sender, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    ctx.settings.business["outreach"]["reengage_after_days"] = 90
    site_now_names_a_person()
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        lead.status, lead.outreach_status = "nurture", "no_response"
    run(ctx, NOW + timedelta(days=86), sender)
    (f,) = [x for x in drafts(ctx) if x.purpose == "reengage"]
    assert f.to_email == INBOX


def test_a_different_spelling_of_the_address_written_to_is_the_same_address(make_ctx):
    ctx = make_ctx(**SEND)
    lead_id = lead_with_inbox(ctx, "case", "info@case.example", "case.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    with ctx.db.session() as s:
        s.get(Lead, lead_id).contact.email = "INFO@Case.Example"
    run(ctx, NOW + timedelta(days=4, minutes=5))
    assert [f.step for f in drafts(ctx) if f.status == "awaiting_approval"] == [2]
    assert tasks(ctx, "Decide by hand%") == []


def test_step_2_does_not_reach_a_person_who_never_had_step_1_after_test_mode(make_ctx):
    ctx = make_ctx(outbound_test_recipients="me@us.example", **SEND)
    lead_id = lead_with_inbox(ctx, "rehearse", "pat@rehearse.example", "rehearse.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    run(ctx, NOW + timedelta(days=6, minutes=5))
    approve_all(ctx)
    ctx.settings.outbound_test_recipients = ""  # the owner ends the rehearsal
    FakeSMTP.sent = []
    run(ctx, NOW + timedelta(days=6, minutes=10))
    assert FakeSMTP.sent == []
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        step2 = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.channel == "email", FollowUp.step == 2)).scalars().one()
        assert lead.outreach_status != "contacted"
        assert step2.status == "cancelled" and "never received" in step2.blocked_reason  # not back to a person: approving again meets the same check
    run(ctx, NOW + timedelta(days=6, minutes=20))
    assert len(tasks(ctx, "Decide by hand%")) == 1  # a rehearsed lead gets a task and no restart
    assert [f for f in drafts(ctx) if f.status == "awaiting_approval"] == []


def test_a_rehearsal_still_walks_the_sequence_while_test_mode_is_on(make_ctx):
    ctx = make_ctx(outbound_test_recipients="me@us.example", **SEND)
    lead_with_inbox(ctx, "walk", "pat@walk.example", "walk.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    run(ctx, NOW + timedelta(days=6, minutes=5))
    approve_all(ctx)
    run(ctx, NOW + timedelta(days=6, minutes=10))
    assert len(FakeSMTP.sent) == 2 and all(m["To"] == "me@us.example" for m in FakeSMTP.sent)
    assert tasks(ctx, "Decide by hand%") == []


def test_a_step_2_with_no_step_1_sent_does_not_go(make_ctx):
    ctx = make_ctx(**SEND)
    lead_id = lead_with_inbox(ctx, "bare", "pat@bare.example", "bare.example")
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        lead.outreach_status = "contacted"
        f = FollowUp(lead_id=lead_id, contact_id=lead.contact_id, company_id=lead.company_id, channel="email", kind="outreach", step=2, due_at=NOW, status="approved",
                     approved_at=utcnow(), approved_by="sales1", to_email="pat@bare.example", subject="s", body=FOOTER)
        s.add(f)
        s.flush()
        fid = f.id
    run(ctx)
    assert FakeSMTP.sent == [] and row(ctx, fid).status == "cancelled"


# ------------------------------------------------------------------ 4. one address, one first email
def _merge_into_keeper(ctx, sent):
    dup_co = lead_of(ctx).company_id
    with ctx.db.session() as s:
        keeper = Company(name="Keeper Co", normalized_name="keeper co", source="test", domain="keeper.example", state="TX", country="US")
        s.add(keeper)
        s.flush()
        s.add(Lead(company_id=keeper.id, source="research", status="qualified", score=70, grade="B", primary_service="bpa"))
        s.flush()
        merge_companies(s, keeper, s.get(Company, dup_co), "crm")
        contact = s.execute(select(Contact).where(Contact.email == sent.to_email)).scalars().first()
        k = s.execute(select(Lead).where(Lead.company_id == keeper.id, Lead.status == "qualified")).scalars().first()
        k.contact_id = contact.id  # what the next site read does
        return k.id


def test_after_a_merge_the_keeper_does_not_send_a_second_first_email_to_the_same_address(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    _merge_into_keeper(ctx, sent)
    for _ in range(2):
        assert SalesWorkflowAgent(ctx).run({"send": False}).ok
    with ctx.db.session() as s:
        again = s.execute(select(FollowUp).where(FollowUp.kind == "outreach", FollowUp.step == 1, FollowUp.status == "awaiting_approval",
                                                 FollowUp.to_email == sent.to_email)).scalars().all()
    assert not again


def test_an_address_emailed_on_another_lead_is_said_once_with_no_review_task(make_ctx):
    ctx = make_ctx(**SEND)
    first = lead_with_inbox(ctx, "first", "pat@first.example", "first.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    second = lead_with_inbox(ctx, "second", "PAT@first.example", "second.example")
    run(ctx, NOW + timedelta(minutes=1))
    run(ctx, NOW + timedelta(minutes=2))
    with ctx.db.session() as s:
        said = s.execute(select(Activity.subject).where(Activity.lead_id == second, Activity.subject.like("No first email%"))).scalars().all()
        emails = s.execute(select(func.count(FollowUp.id)).where(FollowUp.lead_id == second, FollowUp.channel == "email")).scalar_one()
    assert said == [f"No first email drafted: this address was already emailed on lead {first}"]
    assert emails == 0 and tasks(ctx, "Review %") == []


def test_a_rehearsal_on_another_lead_does_not_count_as_written_to(make_ctx):
    ctx = make_ctx(outbound_test_recipients="me@us.example", **SEND)
    lead_with_inbox(ctx, "first", "pat@first.example", "first.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    ctx.settings.outbound_test_recipients = ""
    second = lead_with_inbox(ctx, "second", "pat@first.example", "second.example")
    run(ctx, NOW + timedelta(minutes=1), send=False)
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp.step).where(FollowUp.lead_id == second, FollowUp.channel == "email")).scalars().all() == [1]


def test_one_address_on_two_leads_gets_one_first_email(make_ctx):
    ctx = make_ctx(**SEND)
    add_two = [lead_with_inbox(ctx, "one", "pat@shared.example", "one.example"), lead_with_inbox(ctx, "two", "pat@shared.example", "two.example")]
    run(ctx)
    approve_all(ctx)
    run(ctx)
    run(ctx, NOW + timedelta(days=1))
    assert [m["To"] for m in FakeSMTP.sent].count("pat@shared.example") == 1
    with ctx.db.session() as s:
        second = s.execute(select(FollowUp).where(FollowUp.lead_id == add_two[1], FollowUp.channel == "email")).scalars().one()
    assert second.status == "awaiting_approval" and f"lead {add_two[0]}" in second.blocked_reason  # not lost, and a person knows why


def test_one_address_in_two_spellings_on_one_day_is_held(make_ctx):
    ctx = make_ctx(**SEND)
    lead_with_inbox(ctx, "one", "pat@shared.example", "one.example")
    lead_with_inbox(ctx, "two", "PAT@Shared.Example", "two.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    assert len(FakeSMTP.sent) == 1


def test_the_domain_limit_ignores_the_spelling_of_the_domain(make_ctx):
    ctx = make_ctx(**SEND)
    lead_with_inbox(ctx, "one", "pat@shared.example", "one.example")
    two = lead_with_inbox(ctx, "two", "kim@SHARED.Example", "two.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    assert len(FakeSMTP.sent) == 1
    with ctx.db.session() as s:
        waiting = s.execute(select(FollowUp).where(FollowUp.lead_id == two, FollowUp.channel == "email")).scalars().one()
    assert waiting.status == "approved" and "Already emailed" in waiting.blocked_reason


# ------------------------------------------------------------------ 5. a flagged company's answer
def _approved_answer(ctx, lead_id, to, now):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(InboundEmail(mailbox_uid="u1", message_id="<q@x.example>", from_email=to, status="actioned", subject="A question", body_text="A question", new_text="A question"))
        f = FollowUp(lead_id=lead_id, company_id=lead.company_id, contact_id=lead.contact_id, channel="email", step=1, status="approved", kind="response", approved_at=now,
                     approved_by="sales1", due_at=now - timedelta(hours=1), to_email=to, in_reply_to="<q@x.example>", subject="Re: A question", body=FOOTER)
        s.add(f)
        s.flush()
        return f.id


@pytest.mark.parametrize("flagged, cleared, status", [(True, None, "nurture"), (True, None, "qualified"), (False, None, "qualified"), (False, "owner", "nurture")])
def test_an_answer_to_a_flagged_company_goes_back_to_a_person(make_ctx, clock, flagged, cleared, status):
    ctx = make_ctx(**MAIL)
    now = clock(IN_WINDOW)
    lead_id = add_lead(ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        lead.status = status
        lead.company.is_competitor, lead.company.competitor_cleared_by = flagged, cleared
        to = lead.contact.email
    fid = _approved_answer(ctx, lead_id, to, now)
    run(ctx, utcnow())
    f = row(ctx, fid)
    if flagged:
        assert f.status == "awaiting_approval" and f.approved_at is None and "competitor" in f.blocked_reason and FakeSMTP.sent == []
    else:
        assert f.status == "sent"  # an answer to a nurture lead who wrote to us still goes


# ------------------------------------------------------------------ 6. an approval that falls away
def test_an_approved_first_email_is_stopped_with_a_notice_when_the_morning_rescoring_lowers_the_grade(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    run(ctx, NOW, sender)
    click(ctx, drafts(ctx)[0], "approve")
    assert LeadQualificationAgent(ctx).run({}).ok
    assert lead_of(ctx).grade > "B" and lead_of(ctx).status == "qualified"
    run(ctx, NOW, sender)
    f = drafts(ctx)[0]
    lead = lead_of(ctx)
    with ctx.db.session() as s:
        told = [a.message for a in s.execute(select(Alert).where(Alert.level == "warning")).scalars() if "grade" in (a.message or "")]
        history = s.execute(select(Activity.subject).where(Activity.lead_id == lead.id, Activity.subject.like(f"Follow-up #{f.id} on lead%"))).scalars().all()
    assert f.status == "cancelled" and FakeSMTP.sent == []
    assert len(told) == 1 and f"#{f.id}" in told[0] and f"lead {lead.id}" in told[0] and f"grade is now {lead.grade}" in told[0]
    assert len(history) == 1


def test_an_approved_email_to_a_changed_contact_is_stopped_with_a_notice(make_ctx):
    ctx = make_ctx(**SEND)
    lead_id = lead_with_inbox(ctx, "moved", "info@moved.example", "moved.example")
    run(ctx)
    approve_all(ctx)
    move_contact(ctx, lead_id)
    run(ctx)
    (f,) = [x for x in drafts(ctx)]
    with ctx.db.session() as s:
        told = [a.message for a in s.execute(select(Alert).where(Alert.level == "warning")).scalars() if f"#{f.id}" in (a.message or "")]
    assert f.status == "cancelled" and FakeSMTP.sent == [] and told


# ------------------------------------------------------------------ 7. a lost deal
def _inquiry_lead_with_step_1_sent(ctx):
    lid = make_lead(ctx, owner="eva", email="dana@acme.example")
    assert SalesWorkflowAgent(ctx).run({"send": False}).ok
    with ctx.db.session() as s:
        f = s.execute(select(FollowUp).where(FollowUp.lead_id == lid, FollowUp.channel == "email")).scalars().one()
        when = datetime(2026, 10, 1)
        f.status, f.sent_at, f.message_id, f.approved_at, f.approved_by = "sent", when, "<a@b.example>", when, "eva"
        oid = s.execute(select(Opportunity.id).where(Opportunity.lead_id == lid)).scalar_one()
    return lid, oid


def _second_steps(ctx, lid):
    with ctx.db.session() as s:
        return s.execute(select(func.count(FollowUp.id)).where(FollowUp.lead_id == lid, FollowUp.step == 2)).scalar_one()


def test_losing_a_deal_stops_the_inquiry_follow_ups(make_ctx):
    ctx = make_ctx(**POSTAL)
    lid, oid = _inquiry_lead_with_step_1_sent(ctx)
    web(ctx).post(f"/opportunities/{oid}", data={"stage": "lost", "lost_reason": "no budget, do not chase", "shown_stage": "new"}, headers=ORIGIN)
    SalesWorkflowAgent(ctx).run({"send": False})
    assert _second_steps(ctx, lid) == 0


def test_a_lost_deal_beside_an_open_one_does_not_stop_the_inquiry_follow_ups(make_ctx):
    ctx = make_ctx(**POSTAL)
    lid, oid = _inquiry_lead_with_step_1_sent(ctx)
    with ctx.db.session() as s:
        open_deal = s.get(Opportunity, oid)
        s.add(Opportunity(lead_id=lid, company_id=open_deal.company_id, name="Lost one", stage="lost", probability=0))
    SalesWorkflowAgent(ctx).run({"send": False})
    assert _second_steps(ctx, lid) == 1


# ------------------------------------------------------------------ 8. notes about a meeting
def _note(ctx, lead_id, now, purpose="meeting_reminder"):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        to = lead.contact.email
        s.add(InboundEmail(mailbox_uid="1", message_id="<q@x>", from_email=to, status="actioned", subject="A call?", body_text="", new_text=""))
        f = FollowUp(lead_id=lead_id, company_id=lead.company_id, contact_id=lead.contact_id, channel="email", kind="response", step=1, status="approved",
                     purpose=purpose, approved_by="sales1", approved_at=now - timedelta(days=2), due_at=now - timedelta(days=2), to_email=to,
                     in_reply_to="<q@x>", subject="About our call", body=FOOTER)
        s.add(f)
        s.flush()
        return f.id


def _meeting(ctx, lead_id, start, status="scheduled"):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(Meeting(lead_id=lead_id, company_id=lead.company_id, contact_id=lead.contact_id, invitee_email=lead.contact.email, title="Call", timezone="America/New_York",
                      starts_at=start, ends_at=start + timedelta(minutes=30), source="calendar", status=status))


@pytest.mark.parametrize("purpose", ["meeting_reminder", "meeting_confirmation", "meeting_move", "meeting_cancel"])
def test_a_note_about_a_meeting_is_not_sent_after_the_meeting(make_ctx, clock, purpose):
    ctx = make_ctx(**MAIL)
    now = clock(IN_WINDOW)
    lead_id = add_lead(ctx, stage="approved")
    _meeting(ctx, lead_id, now - timedelta(days=1))
    fid = _note(ctx, lead_id, now, purpose)
    run(ctx, utcnow())
    f = row(ctx, fid)
    assert f.status == "awaiting_approval" and "meeting time has passed" in f.blocked_reason and FakeSMTP.sent == []


@pytest.mark.parametrize("hours", [24, None])
def test_a_note_goes_when_a_later_meeting_is_on_the_lead_or_none_is_known(make_ctx, clock, hours):
    ctx = make_ctx(**MAIL)
    now = clock(IN_WINDOW)
    lead_id = add_lead(ctx, stage="approved")
    if hours:
        _meeting(ctx, lead_id, now - timedelta(days=1), "completed")
        _meeting(ctx, lead_id, now + timedelta(hours=hours))
    fid = _note(ctx, lead_id, now)
    run(ctx, utcnow())
    assert row(ctx, fid).status == "sent"


@pytest.mark.parametrize("offsets, goes", [((-48, -24), False), ((-24, 24), True)])
def test_an_offer_of_times_waits_for_a_person_once_every_time_has_passed(make_ctx, clock, offsets, goes):
    ctx = make_ctx(**MAIL)
    now = clock(IN_WINDOW)
    lead_id = add_lead(ctx, stage="approved")
    fid = _note(ctx, lead_id, now, "meeting_offer")
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(MeetingProposal(lead_id=lead_id, prospect_email=lead.contact.email, prospect_tz="America/New_York", status="draft", offer_followup_id=fid,
                              slots=[(now + timedelta(hours=h)).isoformat() for h in offsets]))
    run(ctx, utcnow())
    assert row(ctx, fid).status == ("sent" if goes else "awaiting_approval")


# ------------------------------------------------------------------ 9. a mail server never reached
class RefusesTLS:
    """What real smtplib meets: STARTTLS refused, and then the QUIT of a `with` refused too, which would replace the first answer."""
    closed = 0

    def __init__(self, host, port, timeout=30):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        raise smtplib.SMTPResponseException(500, b"not now")

    def starttls(self, context=None):
        raise self.failure

    def close(self):
        type(self).closed += 1

    failure: Exception = smtplib.SMTPResponseException(454, b"TLS not available")


class LostMidMessage(FakeSMTP):
    def send_message(self, msg):
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")


def _statuses(ctx, ids):
    return [row(ctx, i).status for i in ids]


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), OSError(10051, "network unreachable"), ConnectionResetError(10054, "reset"),
                                     smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), ConnectionRefusedError(10061, "refused")])
def test_a_mail_server_never_reached_is_not_delivery_unknown(make_ctx, clock, failure):
    ctx = make_ctx(**MAIL)
    clock(IN_WINDOW)
    ids = [add_mail(ctx, add_lead(ctx, stage="approved")) for _ in range(2)]
    tried = []

    def cannot_connect(host, port, timeout=30):
        tried.append(host)
        raise failure

    run(ctx, utcnow(), EmailSender(ctx.settings, smtp_factory=cannot_connect))
    assert _statuses(ctx, ids) == ["approved", "approved"]
    assert len(tried) == 1  # the run stops after the first: each email would wait its minute for the same answer
    assert all(row(ctx, i).blocked_reason for i in ids)
    assert open_alerts(ctx, "mail:unreachable")
    with ctx.db.session() as s:
        assert not s.execute(select(Alert).where(Alert.message.like("Follow-up #%"))).first()
        assert not open_alerts(ctx, "mail:sign_in")


@pytest.mark.parametrize("failure", [smtplib.SMTPResponseException(454, b"TLS not available"), ssl.SSLCertVerificationError("certificate verify failed")])
def test_starttls_refused_or_a_bad_certificate_is_not_delivered(make_ctx, clock, failure):
    ctx = make_ctx(**MAIL)
    clock(IN_WINDOW)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    RefusesTLS.failure, RefusesTLS.closed = failure, 0
    run(ctx, utcnow(), EmailSender(ctx.settings, smtp_factory=RefusesTLS))
    f = row(ctx, fid)
    assert f.status == "approved" and RefusesTLS.closed == 1 and type(failure).__name__ in f.blocked_reason


def test_the_sender_says_which_kind_of_failure_it_was(make_ctx):
    ctx = make_ctx(**MAIL)

    def cannot_connect(host, port, timeout=30):
        raise TimeoutError("oauth server timed out")

    with pytest.raises(MailServerNotReached):
        EmailSender(ctx.settings, smtp_factory=cannot_connect).send("pat@x.example", "s", "b")


def test_text_from_the_server_is_no_false_sign_in_alert(make_ctx, clock):
    ctx = make_ctx(**MAIL)
    clock(IN_WINDOW)
    add_mail(ctx, add_lead(ctx, stage="approved"))

    def cannot_connect(host, port, timeout=30):
        raise OSError("OAuth gateway unreachable")

    run(ctx, utcnow(), EmailSender(ctx.settings, smtp_factory=cannot_connect))
    assert not open_alerts(ctx, "mail:sign_in") and open_alerts(ctx, "mail:unreachable")


def test_a_refused_sign_in_keeps_its_own_alert(make_ctx, clock):
    ctx = make_ctx(**MAIL)
    clock(IN_WINDOW)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))

    class Refuses(FakeSMTP):
        def login(self, *a):
            raise smtplib.SMTPAuthenticationError(535, b"no")

    run(ctx, utcnow(), EmailSender(ctx.settings, smtp_factory=Refuses))
    assert row(ctx, fid).status == "approved" and open_alerts(ctx, "mail:sign_in") and not open_alerts(ctx, "mail:unreachable")


def test_a_failure_from_the_message_onward_stays_delivery_unknown(make_ctx, clock):
    ctx = make_ctx(**MAIL)
    clock(IN_WINDOW)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    run(ctx, utcnow(), EmailSender(ctx.settings, smtp_factory=LostMidMessage))
    assert row(ctx, fid).status == "uncertain"


def test_after_the_server_was_not_reached_an_opt_out_still_cancels_a_later_email(make_ctx, clock):
    ctx = make_ctx(**MAIL)
    clock(IN_WINDOW)
    first = add_mail(ctx, add_lead(ctx, stage="approved"))
    second_lead = add_lead(ctx, stage="approved")
    second = add_mail(ctx, second_lead)
    with ctx.db.session() as s:
        suppress(s, s.get(Lead, second_lead).contact.email, "Asked by phone")
        s.get(FollowUp, second).status = "approved"  # a stop that came after approval, on a row the list's own cancel missed

    def cannot_connect(host, port, timeout=30):
        raise TimeoutError("timed out")

    run(ctx, utcnow(), EmailSender(ctx.settings, smtp_factory=cannot_connect))
    assert _statuses(ctx, [first, second]) == ["approved", "cancelled"]


def test_the_unreachable_alert_closes_after_a_good_send(make_ctx, clock):
    ctx = make_ctx(**MAIL)
    clock(IN_WINDOW)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))

    def cannot_connect(host, port, timeout=30):
        raise TimeoutError("timed out")

    run(ctx, utcnow(), EmailSender(ctx.settings, smtp_factory=cannot_connect))
    assert open_alerts(ctx, "mail:unreachable")
    run(ctx, utcnow())
    assert row(ctx, fid).status == "sent" and not open_alerts(ctx, "mail:unreachable")


# ------------------------------------------------------------------ 10. the Date header
def test_every_email_says_when_it_was_written(make_ctx):
    _ctx, _sender, _sent = contacted(make_ctx)
    assert FakeSMTP.sent[0]["Date"]


# ------------------------------------------------------------------ 11. the held-mail alert
class ReadOK:
    to_end = True

    def __init__(self, ctx):
        pass

    def run(self, inp, job=None):
        return AgentResult(agent="inbox_monitor", run_id="x", status="success", attempts=1, duration_ms=1, output=InboxOutput(mailbox="fake", read_to_end=self.to_end))


def failed_read(ctx):
    with ctx.db.session() as s:
        kv_set(s, MAILBOX_READ, None)


def test_the_held_mail_alert_closes_at_the_next_good_read(make_ctx, clock, monkeypatch):
    """Inside the send window, where the alert is raised at all (the tester's Sunday clock is the case of the next test)."""
    ctx = make_ctx(**MAIL)
    clock(IN_WINDOW)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    failed_read(ctx)
    job_followups(ctx)
    assert open_alerts(ctx, "mail:not_read")
    assert row(ctx, fid).blocked_reason.startswith("Held:")
    clock(IN_WINDOW + timedelta(minutes=15))
    monkeypatch.setattr(workflows, "InboxAgent", ReadOK)
    job_inbox(ctx)
    assert not open_alerts(ctx, "mail:not_read")
    assert row(ctx, fid).blocked_reason is None and row(ctx, fid).status == "approved"


def test_a_read_that_stopped_short_keeps_the_alert_and_the_reason(make_ctx, clock, monkeypatch):
    ctx = make_ctx(**MAIL)
    clock(IN_WINDOW)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    failed_read(ctx)
    job_followups(ctx)
    monkeypatch.setattr(workflows, "InboxAgent", type("Short", (ReadOK,), {"to_end": False}))
    job_inbox(ctx)
    assert open_alerts(ctx, "mail:not_read") and row(ctx, fid).blocked_reason.startswith("Held:")


def test_a_good_read_leaves_other_reasons_alone(make_ctx, clock, monkeypatch):
    ctx = make_ctx(**MAIL)
    clock(IN_WINDOW)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    why = "Held: an email about this lead from an unknown address waits on Replies for a person"
    with ctx.db.session() as s:
        s.get(FollowUp, fid).blocked_reason = why
    monkeypatch.setattr(workflows, "InboxAgent", ReadOK)
    job_inbox(ctx)
    assert row(ctx, fid).blocked_reason == why


def test_outside_the_send_window_a_failed_read_is_not_said_to_be_what_holds_the_email(make_ctx, clock):
    ctx = make_ctx(**MAIL)
    clock(SUNDAY_NIGHT)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    failed_read(ctx)
    job_followups(ctx)
    assert open_alerts(ctx, "mail:not_read") == []
    assert row(ctx, fid).blocked_reason == "Waiting for the business-hours send window"
    assert FakeSMTP.sent == []


# ------------------------------------------------------------------ the owner's decisions of 1 Nov 2026
def test_nothing_is_sent_on_veterans_day_by_the_owners_list():
    config = tomllib.loads((Path(aime.__file__).parents[1] / "config" / "business.toml").read_text(encoding="utf-8"))
    holidays = config["followup"]["holidays"]
    assert holiday_on(date(2026, 11, 11), holidays) == "Veterans Day"
    assert holiday_on(date(2026, 11, 12), holidays) is None
    assert holiday_on(date(2026, 10, 12), holidays) is None  # Columbus Day stays a working day


def test_a_rejected_follow_up_blocks_the_check_in(make_ctx):
    ctx = make_ctx(**SEND)
    ctx.settings.business["outreach"]["reengage_after_days"] = 90
    lead_id = lead_with_inbox(ctx, "no", "info@no.example", "no.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    run(ctx, NOW + timedelta(days=4, minutes=5))
    click(ctx, [f for f in drafts(ctx) if f.step == 2][0], "reject")
    run(ctx, NOW + timedelta(days=8))
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).outreach_status == "no_response"
    run(ctx, NOW + timedelta(days=95))
    assert [f for f in drafts(ctx) if f.purpose == "reengage"] == []
    assert tasks(ctx, "Decide by hand%") == []


def test_pipeline_checks_alone_stop_a_listed_lead(make_ctx):
    ctx, _sender, _sent = contacted(make_ctx)
    with ctx.db.session() as s:
        suppress(s, "brightline-logistics.example", "Asked by phone")
        pipeline_checks(s, ctx.settings, NOW, "test")
        assert s.execute(select(Lead)).scalar_one().outreach_status == "stopped"


# ------------------------------------------------------------------ second look: the "Decide by hand" task closes when the lead stops
HAND = "Decide by hand%"
SHARED = "pat@first.example"


def hand_task_open(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    move_contact(ctx, lead_of(ctx).id)
    run(ctx, NOW + timedelta(days=4, minutes=5), sender)
    assert len(tasks(ctx, HAND)) == 1
    return ctx, sender, sent


def add_converted_sibling(ctx, lead_id):
    with ctx.db.session() as s:
        s.add(Lead(company_id=s.get(Lead, lead_id).company_id, source="research", status="converted", score=70, grade="B"))


@pytest.mark.parametrize("what", ["unsubscribe", "interested", "bounce", "new_address_listed", "domain_listed", "old_address_listed", "lead_converted", "sibling_converted"])
def test_a_hand_task_closes_with_its_reason_when_the_lead_stops(make_ctx, what):
    ctx, sender, sent = hand_task_open(make_ctx)
    lead_id = lead_of(ctx).id
    if what in ("unsubscribe", "interested"):
        said = "Please unsubscribe me from this list." if what == "unsubscribe" else "Sounds interesting. Could we talk Thursday afternoon?"
        FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", said, in_reply_to=sent.message_id)}
        run_inbox(ctx)
    elif what == "bounce":
        FakeIMAP.messages = {1: bounce_for(DAVID, sent.message_id)}
        run_inbox(ctx)
    elif what == "lead_converted":
        with ctx.db.session() as s:
            s.get(Lead, lead_id).status = "converted"
    elif what == "sibling_converted":
        add_converted_sibling(ctx, lead_id)
    else:
        with ctx.db.session() as s:
            domain = s.get(Lead, lead_id).company.domain
            suppress(s, {"new_address_listed": "dana@swap.example", "domain_listed": domain, "old_address_listed": DAVID}[what], "Asked by phone")
    run(ctx, NOW + timedelta(days=4, minutes=10), sender)
    assert tasks(ctx, HAND) == []
    (closed,) = tasks(ctx, HAND, "cancelled")
    assert closed.blocked_reason  # never "done" with no reason: that reads as a person's work
    run(ctx, NOW + timedelta(days=5), sender)
    assert tasks(ctx, HAND) == []  # and nothing opens it again


def test_a_hand_task_stays_open_when_the_unanswered_lead_is_parked(make_ctx):
    """no_response is what the sequence ending does, not an answer: it is the very lead the task is about."""
    ctx, sender, _sent = hand_task_open(make_ctx)
    run(ctx, NOW + timedelta(days=9), sender)
    run(ctx, NOW + timedelta(days=9, minutes=5), sender)
    lead = lead_of(ctx)
    assert (lead.status, lead.outreach_status) == ("nurture", "no_response")
    assert len(tasks(ctx, HAND)) == 1


def test_no_hand_task_opens_for_a_company_that_is_already_a_customer(make_ctx):
    ctx, sender, _sent = contacted(make_ctx)
    lead_id = lead_of(ctx).id
    move_contact(ctx, lead_id)
    add_converted_sibling(ctx, lead_id)
    run(ctx, NOW + timedelta(days=4, minutes=5), sender)
    assert tasks(ctx, HAND) == [] and tasks(ctx, HAND, "cancelled") == []


# ------------------------------------------------------------------ second look: a rehearsal on another lead
def test_an_approved_first_email_goes_when_the_other_leads_email_was_only_a_rehearsal(make_ctx):
    ctx = make_ctx(outbound_test_recipients="me@us.example", **SEND)
    lead_with_inbox(ctx, "first", SHARED, "first.example")
    run(ctx)
    approve_all(ctx)
    run(ctx)
    assert [m["To"] for m in FakeSMTP.sent] == ["me@us.example"]
    ctx.settings.outbound_test_recipients = ""
    second = lead_with_inbox(ctx, "second", SHARED, "second.example")
    later = NOW + timedelta(days=1)
    run(ctx, later, send=False)
    approve_all(ctx)
    FakeSMTP.sent = []
    run(ctx, later + timedelta(minutes=1))
    assert [m["To"] for m in FakeSMTP.sent] == [SHARED]
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp.status).where(FollowUp.lead_id == second, FollowUp.step == 1, FollowUp.channel == "email")).scalars().all() == ["sent"]


@pytest.mark.parametrize("status, delivered_to, blocks", [
    ("sent", SHARED, True), ("sent", SHARED.upper(), True), ("sent", None, True), ("sending", None, True), ("uncertain", None, True),
    ("awaiting_approval", None, True), ("approved", None, True), ("sent", "me@us.example", False), ("rejected", None, False), ("cancelled", None, False)])
def test_what_on_another_lead_holds_a_first_email(make_ctx, status, delivered_to, blocks):
    """Anything that may really have reached the address still holds it; only a rehearsal, which went to our own, does not."""
    ctx = make_ctx(**SEND)
    other, this = lead_with_inbox(ctx, "other", SHARED, "other.example"), lead_with_inbox(ctx, "this", SHARED, "this.example")
    with ctx.db.session() as s:
        s.add(FollowUp(lead_id=other, channel="email", kind="outreach", step=1, status=status, to_email=SHARED, delivered_to=delivered_to, subject="x", body="x", due_at=NOW))
        s.flush()
        f = FollowUp(lead_id=this, channel="email", kind="outreach", step=1, status="approved", to_email=SHARED, subject="x", body="x", due_at=NOW)
        s.add(f)
        s.flush()
        assert SalesWorkflowAgent._same_address_on_another_lead(s, f) == (other if blocks else None)


# ------------------------------------------------------------------ second look: a cancelled meeting
@pytest.mark.parametrize("purpose, meetings, goes", [
    ("meeting_confirmation", [(-24, "scheduled"), (72, "cancelled")], False),
    ("meeting_reminder", [(-24, "scheduled"), (72, "cancelled")], False),
    ("meeting_move", [(-24, "scheduled"), (72, "cancelled")], False),
    ("meeting_reminder", [(-24, "cancelled")], False),  # no live meeting left does not make it "none known"
    ("meeting_reminder", [(-24, "cancelled"), (72, "cancelled")], False),
    ("meeting_reminder", [(72, "cancelled")], True),  # its time has not passed: the rule about past meetings says nothing here
    ("meeting_reminder", [(-24, "cancelled"), (72, "scheduled")], True),
    ("meeting_cancel", [(72, "cancelled")], True),  # a cancel note's own meeting is the cancelled one
    ("meeting_cancel", [(-24, "scheduled"), (72, "cancelled")], True),
    ("meeting_cancel", [(-24, "cancelled")], False)])
def test_a_cancelled_meeting_is_no_later_meeting_except_for_its_own_cancel_note(make_ctx, clock, purpose, meetings, goes):
    ctx = make_ctx(**MAIL)
    now = clock(IN_WINDOW)
    lead_id = add_lead(ctx, stage="approved")
    for hours, status in meetings:
        _meeting(ctx, lead_id, now + timedelta(hours=hours), status)
    fid = _note(ctx, lead_id, now, purpose)
    run(ctx, utcnow())
    f = row(ctx, fid)
    if goes:
        assert f.status == "sent"
    else:
        assert f.status == "awaiting_approval" and "meeting time has passed" in f.blocked_reason and FakeSMTP.sent == []


# ------------------------------------------------------------------ second look: already a customer
@pytest.mark.parametrize("task_first", [True, False])
@pytest.mark.parametrize("why", ["no_contact", "low_grade"])
def test_a_customer_company_gets_no_review_task_whatever_else_is_missing(make_ctx, why, task_first):
    ctx = make_ctx(**SEND)
    lead_id = lead_with_inbox(ctx, "cust", "info@cust.example", "cust.example")
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        if why == "no_contact":
            lead.contact_id = None
        else:
            lead.grade = "D"
    if task_first:
        run(ctx)
        assert len(tasks(ctx, "Review %")) == 1
    add_converted_sibling(ctx, lead_id)
    run(ctx)
    assert tasks(ctx, "Review %") == []
    closed = tasks(ctx, "Review %", "cancelled")
    assert len(closed) == (1 if task_first else 0) and all(t.blocked_reason for t in closed)


def test_a_hand_task_on_a_parked_lead_still_closes_when_the_address_is_listed(make_ctx):
    ctx, sender, _sent = hand_task_open(make_ctx)
    run(ctx, NOW + timedelta(days=9), sender)
    assert lead_of(ctx).outreach_status == "no_response"
    with ctx.db.session() as s:
        suppress(s, "dana@swap.example", "Asked by phone")
    run(ctx, NOW + timedelta(days=9, minutes=5), sender)
    assert tasks(ctx, HAND) == [] and len(tasks(ctx, HAND, "cancelled")) == 1
