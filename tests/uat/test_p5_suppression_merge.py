"""Phase 5, do-not-contact list and company merges: each test states what must hold. Synthetic data only."""
from __future__ import annotations

import smtplib
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from aime.agents.crm import CRMManagementAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.contact_validation import recheck_emails
from aime.doctor import run_checks
from aime.crm_ops import CONTACT_NOT_SUPPRESSED, canonical_address, erase_person, is_suppressed, lift_suppression, log_activity, merge_companies, suppress, upsert_company
from aime.integrations.email_smtp import EmailSender
from aime.meetings import reopen_after_cancel
from aime.migrations import migrate, pending_columns
from aime.models import Activity, Alert, AuditEvent, Company, Contact, FollowUp, InboundEmail, Lead, Meeting, Opportunity, ResearchSnapshot, Signal, Suppression
from aime.outreach import eligible, reopen_refusal, reopen_stopped, stop_sequence
from aime.pipeline import ASKS_FOR_APPROVAL, REPLY_SENT, REPLY_WAITING, settle_next_steps
from aime.sources.base import CompanyCandidate
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.test_automation import drafted
from tests.test_company_research import SITE
from tests.test_contact_validation import FakeChecker
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID, FakeIMAP, bounce_for, contacted, mail, run_inbox
from tests.test_outreach import lead_of
from tests.uat.test_uat_email import ScriptedSMTP, run, sender_for

ORIGIN = {"origin": "http://testserver"}
SENDING = {"company_postal_address": "1 Sample St, Austin, TX 78701", "outbound_email_enabled": True, "smtp_host": "smtp.example", "smtp_from": "hello@us.example"}
WORDS = "Fractional CTO and custom AI development for growing regional manufacturers across the United States."
POST = "Alpha | Backend Engineer | Austin, TX. We are a software development agency building custom software for clients."


def add_lead(ctx, tag, email, domain=None, **lead):
    with ctx.db.session() as s:
        co = Company(name=f"Firm {tag}", normalized_name=f"firm {tag}", source="test", domain=domain)
        s.add(co)
        s.flush()
        s.add(ResearchSnapshot(company_id=co.id, status="done", description="Firm builds reliable tools for modern operations teams.", description_url="https://firm.example/"))
        c = Contact(company_id=co.id, email=email, source="website", email_source_url="https://firm.example/c", email_status="role_address", role_category="inbox", consent_status="none")
        s.add(c)
        s.flush()
        row = Lead(company_id=co.id, contact_id=c.id, source="research", status="qualified", score=70, grade="B", primary_service="ai_agents", **lead)
        s.add(row)
        s.flush()
        return row.id


def work(ctx, when=TUESDAY_11AM_ET, **kw):
    return SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run({"now": when.isoformat(), **kw}).output


def approve_all(ctx):
    client = TestClient(create_app(ctx))
    with ctx.db.session() as s:
        ids = [f.id for f in s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "awaiting_approval")).scalars()]
    for i in ids:
        client.post(f"/followups/{i}/approve", headers=ORIGIN, follow_redirects=False)
    return ids


def usable(ctx, email):
    """The dashboard's own rule for an address that can be written to."""
    with ctx.db.session() as s:
        return s.execute(select(func.count(Contact.id)).where(Contact.email == email, CONTACT_NOT_SUPPRESSED)).scalar_one() == 1


def open_emails(ctx):
    with ctx.db.session() as s:
        return s.execute(select(func.count(FollowUp.id)).where(FollowUp.channel == "email", FollowUp.status.in_(("pending", "awaiting_approval", "approved")))).scalar_one()


def alerts(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Alert.message)).scalars().all()


@pytest.fixture(autouse=True)
def _empty_mailbox():
    FakeSMTP.sent = []
    FakeIMAP.messages = {}


# ------------------------------------------------------------------ A. what an entry covers

@pytest.mark.parametrize("listed, written", [("pat@tagged.example", "pat+news@tagged.example"), ("pat+news@tagged.example", "pat@tagged.example"),
                                             ("pat+old@tagged.example", "pat+new@tagged.example")])
def test_a_plus_tag_of_an_opted_out_address_is_refused(make_ctx, listed, written):
    ctx = make_ctx(**SENDING)
    add_lead(ctx, "tag", written, "tagged.example")
    TestClient(create_app(ctx)).post("/suppress", headers=ORIGIN, data={"value": listed, "reason": "asked"})
    work(ctx)
    approve_all(ctx)
    work(ctx)
    assert FakeSMTP.sent == []
    assert not usable(ctx, written)


def test_a_subdomain_of_a_suppressed_domain_is_refused(make_ctx):
    ctx = make_ctx(**SENDING)
    add_lead(ctx, "sub", "pat@mail.closed.example")
    TestClient(create_app(ctx)).post("/suppress", headers=ORIGIN, data={"value": "closed.example", "reason": "asked"})
    work(ctx)
    approve_all(ctx)
    work(ctx)
    assert FakeSMTP.sent == []
    assert not usable(ctx, "pat@mail.closed.example")


VALUES = ["pat@acme.example", "pat+x@acme.example", "+x@acme.example", "acme.example", "mail.acme.example", "co.uk", "acme.co.uk", "sub.acme.co.uk",
          "github.io", "shop.github.io", "example", "a_b.example", "Pat@Acme.Example"]
ADDRESSES = ["pat@acme.example", "PAT@acme.example", "pat+x@acme.example", "pat+y@acme.example", "patrick@acme.example", "+x@acme.example", "x@acme.example",
             "pat@mail.acme.example", "pat@deep.mail.acme.example", "pat@notacme.example", "pat@acme.example.evil.example", "pat@foo.co.uk", "pat@acme.co.uk",
             "pat@sub.acme.co.uk", "pat@deep.sub.acme.co.uk", "pat@a.github.io", "pat@deep.shop.github.io", "pat@shop.github.io", "pat@other.example", "pat@a_b.example", "pat@axb.example"]


def _as_of_release_75(session, email):
    """is_suppressed as it read before this change: whatever it refused must stay refused."""
    values = {email.lower(), email.rsplit("@", 1)[-1].lower()}
    return session.execute(select(Suppression.id).where(Suppression.value.in_(values))).first() is not None


def test_the_gate_and_the_dashboard_agree_and_only_ever_refuse_more(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        s.add_all(Contact(email=a, source="website") for a in ADDRESSES)
        s.flush()
        for value in VALUES:
            s.execute(text("DELETE FROM suppressions"))
            s.add(Suppression(value=value, kind="email" if "@" in value else "domain", reason="test"))
            s.flush()
            for address in ADDRESSES:
                gate = is_suppressed(s, address)
                listed = s.execute(select(func.count(Contact.id)).where(Contact.email == address, CONTACT_NOT_SUPPRESSED)).scalar_one() == 0
                assert gate == listed, (value, address)
                assert gate or not _as_of_release_75(s, address), (value, address)


def test_an_entry_with_a_trailing_dot_still_blocks_the_same_spelling(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        s.add(Contact(email="pat@acme.example.", source="website"))
        s.add(Suppression(value="acme.example.", kind="domain", reason="test"))
        s.flush()
        assert _as_of_release_75(s, "pat@acme.example.") and is_suppressed(s, "pat@acme.example.")


@pytest.mark.parametrize("value, address", [("co.uk", "pat@foo.co.uk"), ("github.io", "pat@shop.github.io"), ("example", "pat@acme.example"), ("uk", "pat@foo.co.uk"),
                                            ("acme.example", "pat@notacme.example"), ("acme.example", "pat@acme.example.evil.example"), ("pat@acme.example", "patrick@acme.example")])
def test_a_bare_suffix_or_a_lookalike_is_never_matched(make_ctx, value, address):
    ctx = make_ctx()
    with ctx.db.session() as s:
        c = Contact(email=address, source="website", consent_status="none")
        s.add(c)
        suppress(s, value, "test")
        assert not is_suppressed(s, address)
        assert c.consent_status == "none"


def test_suppressing_marks_the_tagged_and_the_subdomain_contact_and_stops_their_mail(make_ctx):
    ctx = make_ctx(**SENDING)
    tag = add_lead(ctx, "tag", "pat+news@tagged.example", "tagged.example")
    sub = add_lead(ctx, "sub", "pat@mail.closed.example", "closed.example")
    work(ctx)
    assert open_emails(ctx) == 2
    with ctx.db.session() as s:
        suppress(s, "pat@tagged.example", "asked")
        suppress(s, "closed.example", "asked")
    assert open_emails(ctx) == 0
    with ctx.db.session() as s:
        assert {s.get(Lead, i).contact.consent_status for i in (tag, sub)} == {"opted_out"}


@pytest.mark.parametrize("given, canonical", [("Pat+News@X.example", "pat@x.example"), ("pat@x.example", "pat@x.example"), ("+x@x.example", "+x@x.example"), ("x.example", "x.example")])
def test_the_canonical_form_of_an_address(given, canonical):
    assert canonical_address(given) == canonical


# ------------------------------------------------------------------ B. a bounce is not an opt-out

def _bounced(ctx, lead_id):
    with ctx.db.session() as s:
        suppress(s, s.get(Lead, lead_id).contact.email, "Bounced (delivery failure report)", bounce=True)


def test_a_bounce_blocks_the_address_at_every_gate_without_recording_an_opt_out(make_ctx):
    ctx = make_ctx(**SENDING)
    lead_id = add_lead(ctx, "dead", "pat@dead.example", "dead.example")
    with ctx.db.session() as s:
        other = s.get(Lead, lead_id)
        s.add(FollowUp(lead_id=other.id, company_id=other.company_id, contact_id=other.contact_id, channel="email", step=1, status="awaiting_approval", kind="response",
                       due_at=TUESDAY_11AM_ET, to_email="colleague@dead.example", subject="Re: a question", body="Hello."))
        s.add(FollowUp(lead_id=other.id, company_id=other.company_id, contact_id=other.contact_id, channel="email", step=1, status="approved", kind="outreach",
                       due_at=TUESDAY_11AM_ET, to_email="pat@dead.example", subject="Hello", body="Hello."))
        s.add(FollowUp(lead_id=other.id, company_id=other.company_id, contact_id=other.contact_id, channel="email", step=2, status="awaiting_approval", kind="response",
                       due_at=TUESDAY_11AM_ET, subject="Re: a question", body="Hello."))  # no to_email: it goes to the contact, so to this very address
    _bounced(ctx, lead_id)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        assert lead.contact.consent_status == "none" and lead.contact.email_status == "bounced"
        assert s.execute(select(Suppression.reason).where(Suppression.value == "pat@dead.example")).scalar_one().startswith("Bounced")
        assert is_suppressed(s, "pat@dead.example") and not eligible(s, ctx.settings, lead)[0]
        assert {f.to_email: f.status for f in s.execute(select(FollowUp)).scalars()} == {"colleague@dead.example": "awaiting_approval", "pat@dead.example": "cancelled", None: "cancelled"}
    assert not usable(ctx, "pat@dead.example")
    work(ctx)
    assert FakeSMTP.sent == []


def test_opt_out_then_bounce_still_blocks_and_still_reads_opted_out(make_ctx):
    ctx = make_ctx()
    lead_id = add_lead(ctx, "both", "pat@both.example", "both.example")
    with ctx.db.session() as s:
        suppress(s, "pat@both.example", "Unsubscribe request by email")
    _bounced(ctx, lead_id)
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).contact.consent_status == "opted_out" and is_suppressed(s, "pat@both.example")
        assert s.execute(select(Suppression.reason)).scalar_one() == "Unsubscribe request by email"


def test_bounce_then_a_real_opt_out_sets_the_contact_to_opted_out(make_ctx):
    ctx = make_ctx()
    lead_id = add_lead(ctx, "both", "pat@both.example", "both.example")
    _bounced(ctx, lead_id)
    with ctx.db.session() as s:
        suppress(s, "pat@both.example", "Unsubscribe request by email")
    with ctx.db.session() as s:
        contact = s.get(Lead, lead_id).contact
        assert contact.consent_status == "opted_out" and contact.consent_at is not None and is_suppressed(s, "pat@both.example")
        assert s.execute(select(Suppression.reason)).scalar_one() == "Unsubscribe request by email"


def test_a_mail_server_refusal_is_a_dead_address_not_an_opt_out(make_ctx):
    ctx, _, fid = drafted(make_ctx)
    ScriptedSMTP.reset()
    TestClient(create_app(ctx)).post(f"/followups/{fid}/approve", headers=ORIGIN, follow_redirects=False)
    ScriptedSMTP.script = [smtplib.SMTPRecipientsRefused({DAVID: (550, b"5.1.1 no such user")})]
    run(ctx, sender_for(ctx, ScriptedSMTP))
    with ctx.db.session() as s:
        contact = s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one()
        assert contact.consent_status != "opted_out" and contact.email_status == "bounced" and is_suppressed(s, DAVID)
    assert lead_of(ctx).outreach_status == "bounced"


@pytest.mark.parametrize("sent_before", [False, True])
def test_a_person_who_asked_us_and_bounced_gets_no_further_draft(make_ctx, sent_before):
    """They keep their inquiry consent now, so the drafting rules must not read that as a reason to write to a dead address."""
    ctx = make_ctx(**SENDING)
    with ctx.db.session() as s:
        co = Company(name="Asker", normalized_name="asker", source="inbound", domain="asker.example")
        s.add(co)
        s.flush()
        c = Contact(company_id=co.id, email="pat@asker.example", source="inbound", consent_status="inquiry", first_name="Pat")
        s.add(c)
        s.flush()
        lead = Lead(company_id=co.id, contact_id=c.id, source="inbound", status="qualified", score=80, grade="A", primary_service="ai_agents")
        s.add(lead)
        s.flush()
        if sent_before:
            s.add(FollowUp(lead_id=lead.id, company_id=co.id, contact_id=c.id, channel="email", step=1, status="sent", due_at=TUESDAY_11AM_ET - timedelta(days=30),
                           sent_at=TUESDAY_11AM_ET - timedelta(days=30), to_email=c.email, subject="Thanks", body="Thanks."))
        lead_id = lead.id
    _bounced(ctx, lead_id)
    work(ctx, send=False)
    assert open_emails(ctx) == 0


@pytest.mark.parametrize("dead, drafts", [(False, 1), (True, 0)])
def test_a_person_who_asked_us_parked_by_the_score_and_bounced_gets_no_draft(make_ctx, dead, drafts):
    """Parked inbound leads have their own way in: a draft here could never be approved, and would sit in the queue for good."""
    ctx = make_ctx(**SENDING)
    with ctx.db.session() as s:
        co = Company(name="Parked", normalized_name="parked", source="inbound", domain="parked.example")
        s.add(co)
        s.flush()
        c = Contact(company_id=co.id, email="pat@parked.example", source="inbound", consent_status="inquiry", first_name="Pat")
        s.add(c)
        s.flush()
        lead = Lead(company_id=co.id, contact_id=c.id, source="inbound", status="nurture", score=30, grade="C", primary_service="ai_agents")
        s.add(lead)
        s.flush()
        s.add(Activity(lead_id=lead.id, company_id=co.id, type="status_change", subject="Status new → nurture", actor=LeadQualificationAgent.name))
        lead_id = lead.id
    if dead:
        _bounced(ctx, lead_id)
    work(ctx, send=False)
    work(ctx, when=TUESDAY_11AM_ET + timedelta(days=1), send=False)
    assert open_emails(ctx) == drafts


# ------------------------------------------------------------------ C. merges: which lead goes on

def _keeper(s, **kw):
    k = Company(name="Keeper Co", normalized_name="keeper co", source="test", domain="keeper.example", state="TX", country="US", **kw)
    s.add(k)
    s.flush()
    lead = Lead(company_id=k.id, source="research", status="qualified", score=70, grade="B", primary_service="bpa")
    s.add(lead)
    s.flush()
    return k, lead


def test_after_a_merge_the_keeper_does_not_send_a_second_first_email_to_the_same_address(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    dup_co = lead_of(ctx).company_id
    with ctx.db.session() as s:
        keeper, _ = _keeper(s)
        kid = keeper.id
        merge_companies(s, keeper, s.get(Company, dup_co), "crm")
        contact = s.execute(select(Contact).where(Contact.email == sent.to_email)).scalars().first()
        s.execute(select(Lead).where(Lead.company_id == kid, Lead.status == "qualified")).scalars().first().contact_id = contact.id
    assert SalesWorkflowAgent(ctx).run({"send": False}).ok
    with ctx.db.session() as s:
        assert not s.execute(select(FollowUp).where(FollowUp.kind == "outreach", FollowUp.step == 1, FollowUp.status == "awaiting_approval",
                                                    FollowUp.to_email == sent.to_email)).scalars().all()


def test_the_lead_already_emailed_goes_on_and_the_untouched_one_is_merged(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    emailed = lead_of(ctx)
    with ctx.db.session() as s:
        keeper, untouched = _keeper(s)
        s.add(FollowUp(lead_id=untouched.id, company_id=keeper.id, channel="task", step=0, status="pending", subject="Review Keeper Co", body="Look", due_at=utcnow()))
        s.flush()
        untouched_id = untouched.id
        merge_companies(s, keeper, s.get(Company, emailed.company_id), "crm")
    with ctx.db.session() as s:
        goes_on, gone = s.get(Lead, emailed.id), s.get(Lead, untouched_id)
        assert (goes_on.status, goes_on.outreach_status) == ("qualified", "contacted") and goes_on.company_id == gone.company_id
        assert gone.status == "merged" and gone.disqualify_reason == f"Merged into lead #{emailed.id}"
        task = s.execute(select(FollowUp).where(FollowUp.lead_id == untouched_id, FollowUp.channel == "task")).scalar_one()
        assert task.status == "cancelled" and task.blocked_reason


@pytest.mark.parametrize("touched", ["sent", "draft", "stage", "deal"])
def test_a_keeper_lead_that_was_touched_stays_the_lead(make_ctx, touched):
    ctx, _s, _sent = contacted(make_ctx)
    emailed = lead_of(ctx)
    with ctx.db.session() as s:
        keeper, own = _keeper(s)
        if touched in ("sent", "draft"):
            s.add(FollowUp(lead_id=own.id, company_id=keeper.id, channel="email", step=1, kind="outreach", due_at=utcnow(), to_email="pat@keeper.example", subject="Hi", body="Hi",
                           status="sent" if touched == "sent" else "awaiting_approval"))
        elif touched == "stage":
            own.outreach_status = "stopped"
        else:
            s.add(Opportunity(lead_id=own.id, company_id=keeper.id, name="A deal", stage="discovery", probability=20))
        s.flush()
        own_id = own.id
        merge_companies(s, keeper, s.get(Company, emailed.company_id), "crm")
    with ctx.db.session() as s:
        assert s.get(Lead, emailed.id).status == "merged" and s.get(Lead, emailed.id).disqualify_reason == f"Merged into lead #{own_id}"
        assert s.get(Lead, own_id).status == "qualified"


def test_a_flagged_contacted_duplicate_stays_merged_and_gets_no_next_step(make_ctx):
    ctx, sender, _sent = contacted(make_ctx)
    emailed = lead_of(ctx)
    with ctx.db.session() as s:
        d = s.get(Lead, emailed.id).company
        d.is_competitor = True
        keeper, _ = _keeper(s)
        merge_companies(s, keeper, d, "crm")
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(days=40)).isoformat(), "send": False})
    with ctx.db.session() as s:
        assert s.get(Lead, emailed.id).status == "merged"
        assert not s.execute(select(FollowUp).where(FollowUp.lead_id == emailed.id, FollowUp.step > 1)).first()


def _keeper_with_contact(s, sent=False, words="Keeper builds reliable tools for modern operations teams."):
    k = Company(name="Keeper Co", normalized_name="keeper co", source="test", domain="keeper.example", state="TX", country="US", website="https://keeper.example", website_status="verified")
    s.add(k)
    s.flush()
    c = Contact(company_id=k.id, email="pat@keeper.example", source="website", email_source_url="https://keeper.example/c", email_status="role_address", role_category="inbox", consent_status="none")
    s.add(c)
    s.flush()
    if words:
        s.add(ResearchSnapshot(company_id=k.id, status="done", description=words, description_url="https://keeper.example/"))
    lead = Lead(company_id=k.id, contact_id=c.id, source="research", status="qualified", score=70, grade="B", primary_service="ai_agents")
    s.add(lead)
    s.flush()
    if sent:
        s.add(FollowUp(lead_id=lead.id, company_id=k.id, contact_id=c.id, channel="email", step=1, kind="outreach", due_at=utcnow(), sent_at=utcnow(), to_email=c.email, subject="Hi", body="Hi",
                       status="sent", message_id="<keeper-first@us.example>"))
        lead.outreach_status = "contacted"
        s.flush()
    return k, lead


def _deal_then_merge_with_an_emailed_keeper(make_ctx, stage="discovery"):
    """Both leads were emailed, so the duplicate's lead is merged, and its thread goes on arriving there."""
    ctx, sender, sent = contacted(make_ctx)
    merged = lead_of(ctx)
    with ctx.db.session() as s:
        s.add(Opportunity(lead_id=merged.id, company_id=merged.company_id, name="A deal", stage=stage, probability=20))
        keeper, own = _keeper_with_contact(s, sent=True)
        own_id = own.id
        merge_companies(s, keeper, s.get(Company, merged.company_id), "crm")
    with ctx.db.session() as s:
        assert s.get(Lead, merged.id).status == "merged" and s.execute(select(Opportunity)).scalar_one().lead_id == own_id
    return ctx, sender, sent, merged.id, own_id


def _reply(ctx, sent, words):
    FakeIMAP.messages = {1: mail(DAVID, f"Re: {sent.subject}", words, in_reply_to=sent.message_id)}
    run_inbox(ctx)


def _deals(ctx):
    with ctx.db.session() as s:
        return [(o.lead_id, o.stage, o.next_step) for o in s.execute(select(Opportunity).order_by(Opportunity.id)).scalars()]


def test_an_interested_reply_in_a_merged_leads_thread_opens_no_second_deal(make_ctx):
    ctx, _, sent, merged_id, own_id = _deal_then_merge_with_an_emailed_keeper(make_ctx)
    _reply(ctx, sent, "Yes, sounds good. Let's set up a call next week.")
    with ctx.db.session() as s:
        assert s.get(Lead, merged_id).outreach_status == "interested"
    assert [(lead, stage) for lead, stage, _ in _deals(ctx)] == [(own_id, "discovery")]


def test_a_no_in_a_merged_leads_thread_still_reaches_the_deal(make_ctx):
    ctx, _, sent, _, own_id = _deal_then_merge_with_an_emailed_keeper(make_ctx)
    _reply(ctx, sent, "Not interested, thanks.")
    assert _deals(ctx) == [(own_id, "discovery", "They replied: not interested. Close it as lost or keep it open")]


def test_an_opt_out_in_a_merged_leads_thread_still_closes_the_deal(make_ctx):
    ctx, _, sent, _, own_id = _deal_then_merge_with_an_emailed_keeper(make_ctx)
    _reply(ctx, sent, "Please remove me from your list.")
    assert [(lead, stage) for lead, stage, _ in _deals(ctx)] == [(own_id, "lost")]
    with ctx.db.session() as s:
        assert is_suppressed(s, DAVID)
        closed = s.execute(select(Activity).where(Activity.subject.like("Opportunity stage discovery%lost%"))).scalar_one()
        assert closed.lead_id == own_id


def test_a_deal_a_person_took_further_stays_with_them_after_an_opt_out_in_a_merged_thread(make_ctx):
    ctx, _, sent, _, own_id = _deal_then_merge_with_an_emailed_keeper(make_ctx, stage="proposal")
    _reply(ctx, sent, "Please remove me from your list.")
    (lead, stage, next_step), = _deals(ctx)
    assert (lead, stage) == (own_id, "proposal") and next_step.startswith("They replied")


def test_a_no_in_the_thread_of_the_lead_that_went_on_closes_the_moved_deal(make_ctx):
    ctx, _, _, _, own_id = _deal_then_merge_with_an_emailed_keeper(make_ctx)
    FakeIMAP.messages = {1: mail("pat@keeper.example", "Re: Hi", "Please remove me from your list.", in_reply_to="<keeper-first@us.example>")}
    run_inbox(ctx)
    assert [(lead, stage) for lead, stage, _ in _deals(ctx)] == [(own_id, "lost")]


def test_a_merged_leads_first_interested_reply_opens_the_deal_on_the_lead_that_went_on(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    merged_id = lead_of(ctx).id
    with ctx.db.session() as s:
        keeper, own = _keeper_with_contact(s, sent=True)
        own_id = own.id
        merge_companies(s, keeper, s.get(Lead, merged_id).company, "crm")
    _reply(ctx, sent, "Yes, sounds good. Let's set up a call next week.")
    assert [(lead, stage) for lead, stage, _ in _deals(ctx)] == [(own_id, "discovery")]
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(days=1)).isoformat(), "send": False})
    assert len(_deals(ctx)) == 1


def test_a_deal_left_on_a_lead_merged_by_an_older_build_is_still_found(make_ctx):
    ctx, _, sent, merged_id, _ = _deal_then_merge_with_an_emailed_keeper(make_ctx)
    with ctx.db.session() as s:
        s.execute(select(Opportunity)).scalar_one().lead_id = merged_id
    _reply(ctx, sent, "Please remove me from your list.")
    assert [(lead, stage) for lead, stage, _ in _deals(ctx)] == [(merged_id, "lost")]


def test_the_deals_next_step_reads_the_answer_waiting_on_the_merged_lead(make_ctx):
    ctx, _, sent, merged_id, own_id = _deal_then_merge_with_an_emailed_keeper(make_ctx)
    with ctx.db.session() as s:
        s.get(Lead, own_id).outreach_status = None  # its own mail has no say: this is about the answer to the merged thread
        s.execute(select(Opportunity)).scalar_one().next_step = ASKS_FOR_APPROVAL[0]
        s.add(FollowUp(lead_id=merged_id, company_id=s.get(Lead, merged_id).company_id, channel="email", kind="response", step=1, status="awaiting_approval", due_at=utcnow(), to_email=DAVID, subject="Re", body="Hi"))
    for lead_id in (None, merged_id, own_id):
        with ctx.db.session() as s:
            settle_next_steps(s, lead_id)
        assert _deals(ctx)[0][2] == ASKS_FOR_APPROVAL[0]
    with ctx.db.session() as s:
        s.execute(select(FollowUp).where(FollowUp.kind == "response")).scalar_one().status = "sent"
    with ctx.db.session() as s:
        settle_next_steps(s, merged_id)
    assert _deals(ctx)[0][2] == REPLY_SENT


def test_a_meeting_cancelled_on_a_merged_lead_still_updates_its_deal(make_ctx):
    ctx, _, _, merged_id, _ = _deal_then_merge_with_an_emailed_keeper(make_ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, merged_id)
        when = utcnow() + timedelta(days=3)
        meeting = Meeting(lead_id=merged_id, company_id=lead.company_id, invitee_email=DAVID, starts_at=when, ends_at=when + timedelta(minutes=30), source="manual", status="cancelled")
        s.add(meeting)
        s.flush()
        reopen_after_cancel(s, meeting, "owner")
    assert "was cancelled" in _deals(ctx)[0][2]


YES, NO, STOP = "Yes, sounds good. Let's set up a call next week.", "Not interested, thanks.", "Please remove me from your list."


def _both_leads_have_a_deal(make_ctx, keeper_stage, dup_stage="new"):
    """Two people, one deal each. The duplicate's person writes into the thread of the merged lead."""
    ctx, _, sent = contacted(make_ctx)
    merged = lead_of(ctx)
    with ctx.db.session() as s:
        keeper, own = _keeper_with_contact(s, sent=True)
        own_id = own.id
        s.add(Opportunity(lead_id=own.id, company_id=keeper.id, name="Keeper's deal", stage=keeper_stage, probability=20, lost_reason="Not interested" if keeper_stage == "lost" else None))
        s.flush()
        s.add(Opportunity(lead_id=merged.id, company_id=merged.company_id, name="Duplicate's deal", stage=dup_stage, probability=10))
        s.flush()
        merge_companies(s, keeper, s.get(Company, merged.company_id), "crm")
    return ctx, sent, merged.id, own_id


def _deal_stages(ctx):
    with ctx.db.session() as s:
        return {o.name: (o.lead_id, o.stage) for o in s.execute(select(Opportunity)).scalars()}


@pytest.mark.parametrize("keeper_stage", ["lost", "won"])
def test_an_interested_reply_moves_the_writers_deal_not_the_closed_one_beside_it(make_ctx, keeper_stage):
    ctx, sent, _, own_id = _both_leads_have_a_deal(make_ctx, keeper_stage)
    _reply(ctx, sent, YES)
    assert _deal_stages(ctx) == {"Keeper's deal": (own_id, keeper_stage), "Duplicate's deal": (own_id, "discovery")}


def test_an_opt_out_closes_the_writers_deal_and_not_another_persons(make_ctx):
    ctx, sent, merged_id, own_id = _both_leads_have_a_deal(make_ctx, "discovery")
    _reply(ctx, sent, STOP)
    assert _deal_stages(ctx) == {"Keeper's deal": (own_id, "discovery"), "Duplicate's deal": (merged_id, "lost")}
    with ctx.db.session() as s:
        assert is_suppressed(s, DAVID)


def test_a_no_writes_the_next_step_on_the_writers_deal_only(make_ctx):
    ctx, sent, *_ = _both_leads_have_a_deal(make_ctx, "discovery")
    with ctx.db.session() as s:
        s.execute(select(Opportunity).where(Opportunity.name == "Keeper's deal")).scalar_one().next_step = "Theirs"
        s.execute(select(Opportunity).where(Opportunity.name == "Duplicate's deal")).scalar_one().stage = "proposal"  # a person's: the no does not close it
    _reply(ctx, sent, NO)
    with ctx.db.session() as s:
        steps = {o.name: o.next_step for o in s.execute(select(Opportunity)).scalars()}
    assert steps["Keeper's deal"] == "Theirs" and steps["Duplicate's deal"].startswith("They replied")


def test_two_open_deals_each_stay_with_their_person_after_an_interested_reply(make_ctx):
    ctx, sent, merged_id, own_id = _both_leads_have_a_deal(make_ctx, "proposal")
    _reply(ctx, sent, YES)
    assert _deal_stages(ctx) == {"Keeper's deal": (own_id, "proposal"), "Duplicate's deal": (merged_id, "discovery")}


def test_a_reply_in_the_keepers_thread_leaves_the_duplicates_open_deal_alone(make_ctx):
    ctx, _, merged_id, own_id = _both_leads_have_a_deal(make_ctx, "discovery")
    FakeIMAP.messages = {1: mail("pat@keeper.example", "Re: Hi", STOP, in_reply_to="<keeper-first@us.example>")}
    run_inbox(ctx)
    assert _deal_stages(ctx) == {"Keeper's deal": (own_id, "lost"), "Duplicate's deal": (merged_id, "new")}


def test_a_closed_deal_stays_with_its_person_beside_a_closed_one(make_ctx):
    """Nothing is live to show on the open lead, and which deal is whose is all a later reply has to go on."""
    ctx, sent, merged_id, own_id = _both_leads_have_a_deal(make_ctx, "won", dup_stage="lost")
    assert _deal_stages(ctx) == {"Keeper's deal": (own_id, "won"), "Duplicate's deal": (merged_id, "lost")}
    _reply(ctx, sent, YES)
    assert _deal_stages(ctx) == {"Keeper's deal": (own_id, "won"), "Duplicate's deal": (merged_id, "discovery")}


def test_a_next_step_counts_only_the_mail_of_leads_that_reach_the_deal(make_ctx):
    ctx, _, merged_id, own_id = _both_leads_have_a_deal(make_ctx, "discovery")
    with ctx.db.session() as s:
        for opp in s.execute(select(Opportunity)).scalars():
            opp.next_step = REPLY_SENT
        s.add(FollowUp(lead_id=own_id, company_id=s.get(Lead, own_id).company_id, channel="email", kind="response", step=1, status="sent", due_at=utcnow(), sent_at=utcnow(), to_email="pat@keeper.example", subject="Re", body="Hi"))
        s.add(FollowUp(lead_id=merged_id, company_id=s.get(Lead, merged_id).company_id, channel="email", kind="response", step=1, status="awaiting_approval", due_at=utcnow(), to_email=DAVID, subject="Re", body="Hi"))
    with ctx.db.session() as s:
        settle_next_steps(s)
    with ctx.db.session() as s:
        steps = {o.name: o.next_step for o in s.execute(select(Opportunity)).scalars()}
    assert steps == {"Keeper's deal": REPLY_SENT, "Duplicate's deal": REPLY_WAITING}


def test_a_meeting_cancelled_on_a_merged_lead_touches_its_own_deal_only(make_ctx):
    ctx, _, merged_id, _ = _both_leads_have_a_deal(make_ctx, "discovery")
    with ctx.db.session() as s:
        s.execute(select(Opportunity).where(Opportunity.name == "Keeper's deal")).scalar_one().next_step = "Theirs"
        when = utcnow() + timedelta(days=1)
        meeting = Meeting(lead_id=merged_id, invitee_email=DAVID, starts_at=when, ends_at=when + timedelta(minutes=30), source="manual", status="cancelled")
        s.add(meeting)
        s.flush()
        reopen_after_cancel(s, meeting, "crm")
    with ctx.db.session() as s:
        steps = {o.name: o.next_step for o in s.execute(select(Opportunity)).scalars()}
    assert steps["Keeper's deal"] == "Theirs" and "cancelled" in steps["Duplicate's deal"]


@pytest.mark.parametrize("words, stage, next_step", [(STOP, "lost", None), (NO, "discovery", "They replied"), (YES, "discovery", None)])
def test_a_merged_threads_reply_follows_the_merge_record_when_another_lead_opened_since(make_ctx, words, stage, next_step):
    """The lead the deal moved to was closed and a new inquiry is the company's primary lead by now: the deal is still where it moved to."""
    ctx, _, sent, _, own_id = _deal_then_merge_with_an_emailed_keeper(make_ctx)
    with ctx.db.session() as s:
        own = s.get(Lead, own_id)
        c = Contact(company_id=own.company_id, email="sam@keeper.example", source="inbound", consent_status="inquiry")
        s.add(c)
        s.flush()
        s.add(Lead(company_id=own.company_id, contact_id=c.id, source="inbound", status="qualified", score=80, grade="A", primary_service="ai_agents"))
        own.status = "disqualified"
    _reply(ctx, sent, words)
    (lead, found, step), = _deals(ctx)  # one deal: none opened on the new inquiry's lead
    assert (lead, found) == (own_id, stage)
    if next_step:
        assert step.startswith(next_step)


def test_a_reply_follows_two_merges_to_the_deal(make_ctx):
    ctx, _, sent, _, own_id = _deal_then_merge_with_an_emailed_keeper(make_ctx)
    with ctx.db.session() as s:
        first = s.get(Lead, own_id)
        third = Company(name="Third Co", normalized_name="third co", source="test", domain="third.example")
        s.add(third)
        s.flush()
        goes_on = Lead(company_id=third.id, source="research", status="qualified", score=70, grade="B", primary_service="ai_agents", outreach_status="contacted")
        s.add(goes_on)
        s.flush()
        goes_on_id = goes_on.id
        merge_companies(s, third, first.company, "crm")
    with ctx.db.session() as s:
        assert s.get(Lead, own_id).status == "merged"
    _reply(ctx, sent, STOP)
    assert [(lead, stage) for lead, stage, _ in _deals(ctx)] == [(goes_on_id, "lost")]


def test_a_lead_merged_by_hand_has_no_record_and_goes_by_the_companys_primary_lead(make_ctx):
    ctx, _, sent, merged_id, own_id = _deal_then_merge_with_an_emailed_keeper(make_ctx)
    with ctx.db.session() as s:
        s.get(Lead, merged_id).disqualify_reason = None
    _reply(ctx, sent, STOP)
    assert [(lead, stage) for lead, stage, _ in _deals(ctx)] == [(own_id, "lost")]


def _dead_duplicate_then_merge(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    dup = lead_of(ctx)
    FakeIMAP.messages = {1: bounce_for(DAVID, sent.message_id)}
    run_inbox(ctx)
    assert lead_of(ctx).outreach_status == "bounced"
    with ctx.db.session() as s:
        keeper, own = _keeper_with_contact(s)
        s.add(FollowUp(lead_id=own.id, company_id=keeper.id, channel="task", step=0, status="pending", subject="Review Keeper Co", body="Look", due_at=utcnow()))
        s.flush()
        own_id = own.id
        merge_companies(s, keeper, s.get(Company, dup.company_id), "crm")
    return ctx, sender, dup.id, own_id


def test_a_bounced_duplicate_does_not_take_over_from_a_good_untouched_lead(make_ctx):
    ctx, sender, dup_id, own_id = _dead_duplicate_then_merge(make_ctx)
    with ctx.db.session() as s:
        assert s.get(Lead, dup_id).status == "merged" and s.get(Lead, own_id).status == "qualified"
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(days=1)).isoformat(), "send": False})
    with ctx.db.session() as s:
        drafted_now = s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "awaiting_approval")).scalars().all()
        assert [(f.lead_id, f.step, f.to_email) for f in drafted_now] == [(own_id, 1, "pat@keeper.example")]
        assert is_suppressed(s, DAVID)  # the dead address stays blocked all the same


def _say(ctx, how, sent):
    if how == "stopped":
        with ctx.db.session() as s:
            stop_sequence(s, s.get(Lead, lead_of(ctx).id), "stopped", "Stopped by owner", "owner")
    else:
        _reply(ctx, sent, {"unsubscribed": "Please remove me from your list.", "not_interested": "Not interested, thanks."}[how])


@pytest.mark.parametrize("stage", ["unsubscribed", "not_interested", "stopped"])
def test_a_closed_duplicate_goes_on_and_a_person_is_told_nothing_goes_to_the_keepers_contact(make_ctx, stage):
    ctx, sender, sent = contacted(make_ctx)
    dup = lead_of(ctx)
    _say(ctx, stage, sent)
    assert lead_of(ctx).outreach_status == stage
    with ctx.db.session() as s:
        keeper, own = _keeper_with_contact(s)
        own_id = own.id
        merge_companies(s, keeper, s.get(Company, dup.company_id), "crm")
    told = [a for a in alerts(ctx) if f"lead #{own_id} was closed in favour of lead #{dup.id}" in a]
    assert len(told) == 1 and stage.replace("_", " ") in told[0]
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(days=1)).isoformat(), "send": False})
    with ctx.db.session() as s:
        assert s.get(Lead, own_id).status == "merged"
        assert not s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "awaiting_approval")).first()


def test_a_running_duplicate_going_on_raises_no_alert(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    dup = lead_of(ctx)
    with ctx.db.session() as s:
        keeper, own = _keeper_with_contact(s)
        own_id = own.id
        merge_companies(s, keeper, s.get(Company, dup.company_id), "crm")
    with ctx.db.session() as s:
        assert s.get(Lead, own_id).status == "merged"
    assert not [a for a in alerts(ctx) if "was closed in favour" in a]


@pytest.mark.parametrize("where", ["site", "post", "title"])
def test_a_keeper_that_reads_as_a_seller_is_not_passed_over_by_the_emailed_lead(make_ctx, where):
    ctx, sender, _ = contacted(make_ctx)
    dup = lead_of(ctx)
    with ctx.db.session() as s:
        keeper, own = _keeper_with_contact(s, words=WORDS if where == "site" else None)
        if where == "post":
            s.add(Signal(company_id=keeper.id, signal_type="hiring", text=POST, source="hn_hiring", source_url="https://n.example/k", fingerprint="k", observed_at=utcnow()))
        elif where == "title":
            keeper.website_title = WORDS
        own_id = own.id
        merge_companies(s, keeper, s.get(Company, dup.company_id), "crm")
    assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).ok
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok
    with ctx.db.session() as s:
        assert s.get(Lead, dup.id).status == "merged" and s.get(Lead, own_id).status == "disqualified"
        assert s.get(Lead, own_id).company.is_competitor
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(days=9)).isoformat(), "send": False})
    with ctx.db.session() as s:
        assert not s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "awaiting_approval")).first()


def test_a_keeper_a_person_cleared_is_not_read_as_a_seller_at_the_merge(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    dup = lead_of(ctx)
    with ctx.db.session() as s:
        keeper, own = _keeper_with_contact(s, words=WORDS)
        keeper.competitor_cleared_by = "owner"
        own_id = own.id
        merge_companies(s, keeper, s.get(Company, dup.company_id), "crm")
    with ctx.db.session() as s:
        assert s.get(Lead, own_id).status == "merged" and s.get(Lead, dup.id).status == "qualified"


# ------------------------------------------------------------------ C. merges: a person's clear and the flag

def site(request):
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    if request.url.host == "alpha-old.example":
        return httpx.Response(301, headers={"location": "https://alpha-new.example/"})
    return httpx.Response(200, text="<html><head><title>Alpha</title></head><body><h1>Alpha</h1></body></html>", headers={"content-type": "text/html"})


def on_file(ctx, name, dom, flag=False, post=None, desc=None, stage=None, cleared_by=None):
    with ctx.db.session() as s:
        co = Company(name=name, normalized_name=name.lower(), source="hn_hiring", domain=dom, website=f"https://{dom}", website_status="verified", website_checked_at=utcnow(),
                     state="TX", country="US", is_competitor=flag, location_text="Austin, TX", competitor_cleared_by=cleared_by)
        s.add(co)
        s.flush()
        ct = Contact(company_id=co.id, email=f"p@{dom}", first_name="Pat", consent_status="unknown", source="website", email_status="valid")
        s.add(ct)
        s.flush()
        s.add(Signal(company_id=co.id, signal_type="hiring", text=post or "We are hiring engineers.", source="hn_hiring", source_url=f"https://n.example/{name}", fingerprint=name,
                     observed_at=utcnow() - timedelta(days=3)))
        if desc:
            s.add(ResearchSnapshot(company_id=co.id, status="done", description=desc, description_url=f"https://{dom}/"))
        lead = Lead(company_id=co.id, contact_id=ct.id, source="research", status="qualified", score=70, grade="B", primary_service="ai_agents", outreach_status=stage)
        s.add(lead)
        s.flush()
        return co.id, lead.id


def clear(ctx, company_id):
    TestClient(create_app(ctx)).post(f"/companies/{company_id}/not_competitor", headers=ORIGIN)
    with ctx.db.session() as s:
        return s.get(Company, company_id).competitor_cleared_by


def company(ctx, cid):
    with ctx.db.session() as s:
        return s.get(Company, cid)


def lead_row(ctx, lid):
    with ctx.db.session() as s:
        return s.get(Lead, lid)


def test_a_persons_not_a_competitor_survives_a_merge(make_ctx):
    ctx = make_ctx(handler=site)
    keep, kept = on_file(ctx, "Keeper", "keeper.example", desc=WORDS)
    dup, _ = on_file(ctx, "Dupe", "dupe.example", desc=WORDS, flag=True)
    with ctx.db.session() as s:
        s.get(Company, keep).domain = s.get(Company, dup).domain = "same.example"
    clear(ctx, dup)
    assert CRMManagementAgent(ctx).run({"operations": ["dedupe", "competitors"]}).ok
    assert company(ctx, keep).competitor_cleared_by and not company(ctx, keep).is_competitor
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok
    assert lead_row(ctx, kept).status != "disqualified"


def test_the_clear_outlives_the_merge_of_a_record_that_learned_its_domain_later(make_ctx):
    ctx = make_ctx(handler=site)
    with ctx.db.session() as s:
        old, _ = upsert_company(s, CompanyCandidate(name="Gamma Widgets", source="hn_hiring", source_url="https://n.example/1", source_record_id="r1", location_text="Remote", is_us=True))
        new, _ = upsert_company(s, CompanyCandidate(name="Gamma Widgets", source="sec_edgar", source_url="https://n.example/2", source_record_id="r2", domain="gamma.example",
                                                    website="https://gamma.example", state="TX", is_us=True))
        oid, nid = old.id, new.id
        new.is_competitor = True
        ct = Contact(company_id=nid, email="p@gamma.example", first_name="Pat", consent_status="unknown", source="website", email_status="valid")
        s.add(ct)
        s.flush()
        s.add(Signal(company_id=nid, signal_type="hiring", text=POST, source="hn_hiring", source_url="https://n.example/3", fingerprint="g", observed_at=utcnow()))
        lead = Lead(company_id=nid, contact_id=ct.id, source="research", status="qualified", score=70, grade="B", primary_service="ai_agents")
        s.add(lead)
        s.flush()
        lid = lead.id
    who = clear(ctx, nid)
    with ctx.db.session() as s:
        upsert_company(s, CompanyCandidate(name="Gamma Widgets", source="hn_hiring", source_url="https://n.example/4", source_record_id="r1", domain="gamma.example",
                                           website="https://gamma.example", location_text="Remote", is_us=True))
    assert CRMManagementAgent(ctx).run({"operations": ["dedupe", "competitors"]}).ok
    assert company(ctx, oid).competitor_cleared_by == who and not company(ctx, oid).is_competitor
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok
    assert lead_row(ctx, lid).status != "disqualified"


def test_a_redirect_into_a_seller_does_not_inherit_the_clear(make_ctx):
    ctx = make_ctx(handler=site)
    b, lb = on_file(ctx, "Alpha Holdings", "alpha-new.example", desc=WORDS)
    a, _ = on_file(ctx, "Alpha", "alpha-old.example", flag=True)
    who = clear(ctx, a)
    with ctx.db.session() as s:
        s.get(Company, a).website_checked_at = None
    assert CRMManagementAgent(ctx).run({"operations": ["verify_websites", "dedupe", "competitors"], "company_ids": [a]}).ok
    assert company(ctx, a).merged_into_id == b
    assert company(ctx, b).competitor_cleared_by is None and company(ctx, b).is_competitor
    assert any("Alpha Holdings" in m and who in m for m in alerts(ctx))
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok
    assert lead_row(ctx, lb).status == "disqualified"


def test_a_flagged_keeper_stays_flagged_and_a_person_is_told(make_ctx):
    ctx = make_ctx(handler=site)
    keep, _ = on_file(ctx, "Keeper", "same.example", flag=True)
    dup, _ = on_file(ctx, "Dupe", "dupe.example", flag=True)
    with ctx.db.session() as s:
        s.get(Company, dup).domain = "same.example"
    who = clear(ctx, dup)
    with ctx.db.session() as s:
        merge_companies(s, s.get(Company, keep), s.get(Company, dup), "crm")
    assert company(ctx, keep).is_competitor and company(ctx, keep).competitor_cleared_by is None
    assert any("Keeper" in m and who in m for m in alerts(ctx))


def test_an_uncleared_duplicates_flag_survives_a_merge_and_later_competitor_runs(make_ctx):
    ctx = make_ctx(handler=site)
    k, lk = on_file(ctx, "Kept", "same.example")
    d, _ = on_file(ctx, "Dupe", "same2.example", flag=True, desc=WORDS)
    with ctx.db.session() as s:
        s.get(Company, d).domain = "same.example"
        s.add(Activity(company_id=d, type="data_quality", subject='Marked as a possible competitor: it says "custom AI development"', actor="crm"))
    assert CRMManagementAgent(ctx).run({"operations": ["dedupe", "competitors"]}).ok
    assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).ok
    assert company(ctx, k).is_competitor
    with ctx.db.session() as s:
        line = s.execute(select(Activity.subject).where(Activity.company_id == k, Activity.subject.like("%carried over%"))).scalar_one()
    assert line.startswith(f"Possible-competitor flag carried over from merged duplicate #{d}") and "custom AI development" in line
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok
    assert lead_row(ctx, lk).status == "disqualified"


def test_a_carried_flag_outlives_an_old_mark_on_the_keeper_itself(make_ctx):
    """The keeper once carried a flag of AIME's that was removed since: that old line must not get the new flag removed."""
    ctx = make_ctx(handler=site)
    k, _ = on_file(ctx, "Kept", "same.example")
    d, _ = on_file(ctx, "Dupe", "same2.example", flag=True, desc=WORDS)
    with ctx.db.session() as s:
        s.get(Company, d).domain = "same.example"
        s.add(Activity(company_id=k, type="data_quality", subject='Marked as a possible competitor: it says "old words"', actor=CRMManagementAgent.name))
    assert CRMManagementAgent(ctx).run({"operations": ["dedupe", "competitors"]}).ok
    assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).ok
    assert company(ctx, k).is_competitor


def test_an_emailed_keeper_keeps_its_sequence_and_draft_and_a_person_is_told(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lead = lead_of(ctx)
    d, _ = on_file(ctx, "Dupe", "dupe.example", flag=True, desc=WORDS)
    with ctx.db.session() as s:
        s.add(FollowUp(lead_id=lead.id, company_id=lead.company_id, contact_id=lead.contact_id, channel="email", step=2, kind="outreach", status="awaiting_approval",
                       due_at=TUESDAY_11AM_ET + timedelta(days=4), to_email=DAVID, subject="Again", body="Hello again."))
        keeper = s.get(Company, lead.company_id)
        merge_companies(s, keeper, s.get(Company, d), "crm")
    assert not company(ctx, lead.company_id).is_competitor
    assert any("possible competitor" in m for m in alerts(ctx))
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok
    with ctx.db.session() as s:
        after = s.get(Lead, lead.id)
        assert after.status != "disqualified" and after.outreach_status == "contacted"
        assert s.execute(select(FollowUp.status).where(FollowUp.step == 2)).scalar_one() == "awaiting_approval"


def test_a_cleared_keeper_keeps_its_clear_and_a_person_is_told(make_ctx):
    ctx = make_ctx(handler=site)
    k, _ = on_file(ctx, "Kept", "kept.example", cleared_by="owner")
    d, _ = on_file(ctx, "Dupe", "dupe.example", flag=True, desc=WORDS)
    with ctx.db.session() as s:
        merge_companies(s, s.get(Company, k), s.get(Company, d), "crm")
    assert not company(ctx, k).is_competitor and company(ctx, k).competitor_cleared_by == "owner"
    assert any("Kept" in m and "owner" in m for m in alerts(ctx))


# ------------------------------------------------------------------ the owner's lift

def lift(ctx, value, actor="owner", reason="They wrote and asked to hear from us"):
    with ctx.db.session() as s:
        return lift_suppression(s, value, actor, reason)


def _unsubscribed(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(DAVID, f"Re: {sent.subject}", "Please remove me from your list.", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    assert lead_of(ctx).outreach_status == "unsubscribed"
    with ctx.db.session() as s:
        s.execute(select(InboundEmail)).scalar_one().received_at = utcnow() - timedelta(days=9)  # the fixture's Date header is later than this test's clock
    return ctx, sender, sent


def refusal(ctx):
    with ctx.db.session() as s:
        return reopen_refusal(s, s.get(Lead, lead_of(ctx).id))


def test_a_lift_opens_the_gates_for_that_address_only(make_ctx):
    ctx = make_ctx(**SENDING)
    one = add_lead(ctx, "one", "one@firm-a.example", "firm-a.example")
    with ctx.db.session() as s:
        company_id = s.get(Lead, one).company_id
        c = Contact(company_id=company_id, email="two@firm-a.example", source="website", email_source_url="https://firm.example/c", email_status="role_address", role_category="inbox", consent_status="none")
        s.add(c)
        s.flush()
        s.add(Lead(company_id=company_id, contact_id=c.id, source="research", status="qualified", score=70, grade="B", primary_service="ai_agents"))
        suppress(s, "one@firm-a.example", "Unsubscribe request by email")
        suppress(s, "two@firm-a.example", "Unsubscribe request by email")
    assert lift(ctx, "one@firm-a.example") is None
    with ctx.db.session() as s:
        assert not is_suppressed(s, "one@firm-a.example") and is_suppressed(s, "two@firm-a.example")
        contacts = {c.email: c.consent_status for c in s.execute(select(Contact)).scalars()}
    assert contacts == {"one@firm-a.example": "none", "two@firm-a.example": "opted_out"}
    assert usable(ctx, "one@firm-a.example") and not usable(ctx, "two@firm-a.example")
    work(ctx)
    approve_all(ctx)
    work(ctx)
    assert [m["To"] for m in FakeSMTP.sent] == ["one@firm-a.example"]


def test_a_lift_sends_nothing_drafts_nothing_and_reopens_nothing(make_ctx):
    ctx, _, _ = _unsubscribed(make_ctx)
    with ctx.db.session() as s:
        emails = s.execute(select(func.count(FollowUp.id))).scalar_one()
    FakeSMTP.sent = []
    assert lift(ctx, DAVID) is None
    with ctx.db.session() as s:
        row = s.execute(select(Suppression)).scalar_one()
        assert row.lifted_at and row.lifted_by == "owner" and row.lift_reason and row.value == DAVID  # kept for good
        assert s.execute(select(func.count(FollowUp.id))).scalar_one() == emails
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "suppression_lifted")).scalar_one().actor == "owner"
        assert s.execute(select(Activity).where(Activity.subject.like("Do-not-contact entry lifted%"))).scalars().first().body
    lead = lead_of(ctx)
    assert lead.outreach_status == "unsubscribed" and FakeSMTP.sent == [] and open_emails(ctx) == 0
    work(ctx, when=TUESDAY_11AM_ET + timedelta(days=30))
    assert FakeSMTP.sent == [] and open_emails(ctx) == 0
    with ctx.db.session() as s:
        assert not eligible(s, ctx.settings, s.get(Lead, lead.id))[0]  # the stage still closes it, until a person reopens it


def test_a_closed_lead_may_be_reopened_once_its_address_is_lifted(make_ctx):
    ctx, _, _ = _unsubscribed(make_ctx)
    assert "stays closed" in refusal(ctx)
    assert lift(ctx, DAVID) is None
    assert refusal(ctx) is None  # the old mail and the old activity no longer count
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_of(ctx).id)
        assert reopen_stopped(s, lead, "owner", "They asked to hear from us") is None
        assert lead.outreach_status == "contacted"
    assert open_emails(ctx) == 0


def test_a_closed_lead_stays_closed_while_a_domain_entry_covers_its_address(make_ctx):
    ctx, _, _ = _unsubscribed(make_ctx)
    with ctx.db.session() as s:
        suppress(s, SITE, "The whole company")
    assert lift(ctx, DAVID) is None
    assert "stays closed" in refusal(ctx)
    assert lift(ctx, SITE) is None
    assert "stays closed" not in refusal(ctx)  # what the domain entry marked on the other contacts still refuses: a person looks at those


def test_a_new_opt_out_after_a_lift_blocks_again_at_every_gate(make_ctx):
    ctx, _, sent = _unsubscribed(make_ctx)
    assert lift(ctx, DAVID) is None
    FakeIMAP.messages[2] = mail(DAVID, f"Re: {sent.subject}", "Stop writing to me, unsubscribe.", in_reply_to=sent.message_id, msg_id="<again@mail.example>")
    run_inbox(ctx)
    with ctx.db.session() as s:
        row = s.execute(select(Suppression)).scalar_one()
        assert row.lifted_at is None and row.lifted_by is None and is_suppressed(s, DAVID)
        assert s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one().consent_status == "opted_out"
        audit = {e.action: e.detail for e in s.execute(select(AuditEvent).where(AuditEvent.action.like("suppression_%"))).scalars()}
        assert "suppression_lifted" in audit and "They wrote and asked" in audit["suppression_relisted"]  # the old lift stays on record
    assert "stays closed" in refusal(ctx) and not usable(ctx, DAVID)


def test_an_opt_out_written_after_the_lift_counts_though_nothing_listed_it_again(make_ctx):
    ctx, _, _ = _unsubscribed(make_ctx)
    assert lift(ctx, DAVID) is None
    assert refusal(ctx) is None
    with ctx.db.session() as s:
        s.add(InboundEmail(mailbox_uid="u9", message_id="<later@mail.example>", from_email=DAVID, status="actioned", category="unsubscribe", subject="Stop", body_text="Stop", new_text="Stop"))
    assert "asked not to be contacted" in refusal(ctx)


def test_an_old_opt_out_from_someone_whose_entry_was_not_lifted_still_refuses(make_ctx):
    ctx, _, _ = _unsubscribed(make_ctx)
    with ctx.db.session() as s:
        s.add(InboundEmail(mailbox_uid="u9", message_id="<c@mail.example>", from_email=f"colleague@{SITE}", status="actioned", category="unsubscribe", lead_id=lead_of(ctx).id,
                           received_at=utcnow() - timedelta(days=9), subject="Stop", body_text="Stop", new_text="Stop"))
    assert lift(ctx, DAVID) is None
    assert "asked not to be contacted" in refusal(ctx)


def _colleague_stopped_the_lead(make_ctx):
    """A colleague writes outside the thread: the lead stops, and only the colleague's address goes on the list."""
    ctx, _, _ = contacted(make_ctx)
    colleague = f"colleague@{SITE}"
    FakeIMAP.messages = {1: mail(colleague, "Remove us", "Please remove me from your list. Unsubscribe.")}
    run_inbox(ctx)
    lead = lead_of(ctx)
    assert lead.outreach_status == "stopped"
    with ctx.db.session() as s:
        for row in s.execute(select(InboundEmail)).scalars():
            row.status, row.received_at = "actioned", utcnow() - timedelta(days=9)  # a person read it
        for a in s.execute(select(Activity).where(Activity.lead_id == lead.id)).scalars():
            a.created_at = utcnow() - timedelta(days=9)
    return ctx, colleague


def test_lifting_a_colleagues_opt_out_lets_a_person_reopen_the_lead_it_stopped(make_ctx):
    ctx, colleague = _colleague_stopped_the_lead(make_ctx)
    assert "do-not-contact" in refusal(ctx)
    assert lift(ctx, colleague) is None
    assert refusal(ctx) is None
    assert open_emails(ctx) == 0


def test_a_new_stop_named_after_the_colleagues_lift_refuses_again(make_ctx):
    ctx, colleague = _colleague_stopped_the_lead(make_ctx)
    assert lift(ctx, colleague) is None
    with ctx.db.session() as s:
        log_activity(s, f"Outreach: stopped ({colleague} at this company asked not to be contacted)", "inbox", type_="outreach", lead_id=lead_of(ctx).id, company_id=lead_of(ctx).company_id)
    assert "asked not to be contacted" in refusal(ctx)


def test_one_colleagues_lift_does_not_clear_what_someone_else_said(make_ctx):
    ctx, colleague = _colleague_stopped_the_lead(make_ctx)
    lead = lead_of(ctx)
    with ctx.db.session() as s:
        said = log_activity(s, f"Outreach: stopped (other@{SITE} at this company asked not to be contacted)", "inbox", type_="outreach", lead_id=lead.id, company_id=lead.company_id)
        said.created_at = utcnow() - timedelta(days=9)
        unnamed = log_activity(s, "Outreach: unsubscribed (Asked not to be contacted)", "inbox", type_="outreach", lead_id=lead.id, company_id=lead.company_id)
        unnamed.created_at = utcnow() - timedelta(days=9)
    assert lift(ctx, colleague) is None
    assert "asked not to be contacted" in refusal(ctx)
    with ctx.db.session() as s:
        s.delete(s.execute(select(Activity).where(Activity.subject.like("%other@%"))).scalar_one())
    assert "asked not to be contacted" in refusal(ctx)  # who said it is not on record, and the lead's own address was never lifted


@pytest.mark.parametrize("listed, on_file", [("pat+news@firm-t.example", "pat@firm-t.example"), ("pat@firm-t.example", "pat+news@firm-t.example")])
def test_lifting_a_tag_entry_clears_the_contact_it_marked(make_ctx, listed, on_file):
    ctx = make_ctx(**SENDING)
    lead_id = add_lead(ctx, "tag", on_file, "firm-t.example")
    with ctx.db.session() as s:
        suppress(s, listed, "Unsubscribe request by email")
        assert s.get(Lead, lead_id).contact.consent_status == "opted_out"
    assert lift(ctx, listed) is None
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        assert lead.contact.consent_status == "none" and not is_suppressed(s, on_file) and eligible(s, ctx.settings, lead)[0]


def test_lifting_one_spelling_keeps_the_contact_opted_out_until_the_last_is_lifted(make_ctx):
    ctx = make_ctx(**SENDING)
    lead_id = add_lead(ctx, "tag", "pat@firm-t.example", "firm-t.example")
    with ctx.db.session() as s:
        suppress(s, "pat+news@firm-t.example", "asked")
        suppress(s, "pat@firm-t.example", "asked")
    assert lift(ctx, "pat+news@firm-t.example") is None
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        assert lead.contact.consent_status == "opted_out" and is_suppressed(s, "pat@firm-t.example") and not eligible(s, ctx.settings, lead)[0]
    assert lift(ctx, "pat@firm-t.example") is None
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).contact.consent_status == "none" and not is_suppressed(s, "pat@firm-t.example")


def test_lifting_a_tag_entry_touches_no_colleague_and_no_lookalike(make_ctx):
    ctx = make_ctx(**SENDING)
    lead_id = add_lead(ctx, "tag", "pat@firm-t.example", "firm-t.example")
    with ctx.db.session() as s:
        company_id = s.get(Lead, lead_id).company_id
        for address in ("sam@firm-t.example", "pat@other-t.example"):
            s.add(Contact(company_id=company_id, email=address, source="website", consent_status="none"))
        s.flush()
        suppress(s, "pat+news@firm-t.example", "asked")
        suppress(s, "sam@firm-t.example", "asked")
        suppress(s, "pat@other-t.example", "asked")
    assert lift(ctx, "pat+news@firm-t.example") is None
    with ctx.db.session() as s:
        consent = {c.email: c.consent_status for c in s.execute(select(Contact)).scalars()}
    assert consent == {"pat@firm-t.example": "none", "sam@firm-t.example": "opted_out", "pat@other-t.example": "opted_out"}


def test_a_tag_entrys_lift_does_not_make_earlier_mail_look_sent_without_consent(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="Asker", normalized_name="asker", source="inbound", domain="asker.example")
        s.add(co)
        s.flush()
        c = Contact(company_id=co.id, email="pat@asker.example", source="inbound", consent_status="inquiry")
        s.add(c)
        s.flush()
        lead = Lead(company_id=co.id, contact_id=c.id, source="inbound", status="qualified")
        s.add(lead)
        s.flush()
        s.add(FollowUp(lead_id=lead.id, company_id=co.id, contact_id=c.id, channel="email", step=1, status="sent", due_at=utcnow(), sent_at=utcnow() - timedelta(days=5),
                       to_email=c.email, subject="Thanks", body="Thanks."))
        suppress(s, "pat+news@asker.example", "Unsubscribe request by email")
    assert lift(ctx, "pat+news@asker.example") is None
    check = next(c for c in run_checks(ctx) if c["name"].startswith("Emails only to consenting"))
    assert check["ok"], check


def test_a_bounce_lift_behaves_as_an_opt_out_lift(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: bounce_for(DAVID, sent.message_id)}
    run_inbox(ctx)
    assert lead_of(ctx).outreach_status == "bounced"
    assert "stays closed" in refusal(ctx)
    assert lift(ctx, DAVID) is None
    with ctx.db.session() as s:
        contact = s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one()
        assert contact.email_status == "unchecked" and contact.consent_status != "opted_out" and not is_suppressed(s, DAVID)
        assert not eligible(s, ctx.settings, s.get(Lead, lead_of(ctx).id))[0]
        recheck_emails(s, contact.company, FakeChecker({}), only=("unchecked",))
        assert contact.email_status in ("published", "role_address")  # the mail-server check reads it again
    assert refusal(ctx) is None and open_emails(ctx) == 0


def test_lifting_a_domain_entry_leaves_each_address_of_its_own_on_the_list(make_ctx):
    ctx = make_ctx()
    lead_id = add_lead(ctx, "dom", "pat@firm-b.example", "firm-b.example")
    with ctx.db.session() as s:
        suppress(s, "firm-b.example", "The whole company")
        suppress(s, "own@firm-b.example", "Unsubscribe request by email")
        assert is_suppressed(s, "new@firm-b.example") and is_suppressed(s, "x@mail.firm-b.example")
    assert lift(ctx, "firm-b.example") is None
    with ctx.db.session() as s:
        assert not is_suppressed(s, "new@firm-b.example") and not is_suppressed(s, domain="firm-b.example") and not is_suppressed(s, "x@mail.firm-b.example")
        assert is_suppressed(s, "own@firm-b.example")
        assert s.get(Lead, lead_id).contact.consent_status == "opted_out"  # it may have said no itself: a domain's lift speaks for nobody in person
        s.add(Contact(company_id=s.get(Lead, lead_id).company_id, email="found@firm-b.example", source="website"))
    assert usable(ctx, "found@firm-b.example") and not usable(ctx, "own@firm-b.example")


def test_a_lift_does_not_make_earlier_mail_look_sent_without_consent(make_ctx):
    def consent():
        return next(c for c in run_checks(ctx) if c["name"].startswith("Emails only to consenting"))

    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="Asker", normalized_name="asker", source="inbound", domain="asker.example")
        s.add(co)
        s.flush()
        c = Contact(company_id=co.id, email="pat@asker.example", source="inbound", consent_status="inquiry")
        s.add(c)
        s.flush()
        lead = Lead(company_id=co.id, contact_id=c.id, source="inbound", status="qualified")
        s.add(lead)
        s.flush()
        s.add(FollowUp(lead_id=lead.id, company_id=co.id, contact_id=c.id, channel="email", step=1, status="sent", due_at=utcnow(), sent_at=utcnow() - timedelta(days=5),
                       to_email=c.email, subject="Thanks", body="Thanks."))
        suppress(s, c.email, "Unsubscribe request by email")
    assert lift(ctx, "pat@asker.example") is None
    assert consent()["ok"], consent()
    with ctx.db.session() as s:  # mail after the lift has no such excuse
        f = s.execute(select(FollowUp)).scalar_one()
        f.sent_at = utcnow() + timedelta(days=1)
    assert not consent()["ok"]


def test_a_lift_of_one_spelling_leaves_the_other_blocking(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        suppress(s, "pat@firm-c.example", "asked")
        suppress(s, "pat+news@firm-c.example", "asked")
    assert lift(ctx, "pat@firm-c.example") is None
    with ctx.db.session() as s:
        assert is_suppressed(s, "pat@firm-c.example") and is_suppressed(s, "pat+news@firm-c.example")


@pytest.mark.parametrize("listed_first", [False, True])
def test_a_lift_is_refused_on_an_erasure(make_ctx, listed_first):
    ctx = make_ctx()
    with ctx.db.session() as s:
        if listed_first:  # erase_person finds the address on the list already
            suppress(s, "gone@firm-d.example", "Unsubscribe request by email")
        s.add(Contact(email="gone@firm-d.example", source="website"))
        s.flush()
        erase_person(s, "gone@firm-d.example", "owner")
    refused = lift(ctx, "gone@firm-d.example")
    assert refused and "erased" in refused
    with ctx.db.session() as s:
        assert s.execute(select(Suppression.lifted_at)).scalar_one() is None and is_suppressed(s, "gone@firm-d.example")


def test_a_lifted_entry_that_is_then_erased_is_never_lifted_again(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        suppress(s, "gone@firm-d.example", "Unsubscribe request by email")
    assert lift(ctx, "gone@firm-d.example") is None
    with ctx.db.session() as s:
        erase_person(s, "gone@firm-d.example", "owner")
        assert is_suppressed(s, "gone@firm-d.example")
    assert "erased" in lift(ctx, "gone@firm-d.example")


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_a_lift_without_a_reason_is_refused(make_ctx, reason):
    ctx = make_ctx()
    with ctx.db.session() as s:
        suppress(s, "pat@firm-e.example", "asked")
    assert lift(ctx, "pat@firm-e.example", reason=reason) == "give the reason"
    with ctx.db.session() as s:
        assert is_suppressed(s, "pat@firm-e.example")


def test_a_second_lift_and_an_unknown_value_are_refused(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        suppress(s, "pat@firm-e.example", "asked")
    assert lift(ctx, "pat@firm-e.example") is None
    assert "already" in lift(ctx, "pat@firm-e.example")
    assert "not on" in lift(ctx, "other@firm-e.example")
    with ctx.db.session() as s:
        row = s.execute(select(Suppression)).scalar_one()
        assert lift_suppression(s, row, "owner", "Again") and row.lift_reason == "They wrote and asked to hear from us"


def test_an_older_build_that_does_not_read_the_lift_still_blocks(make_ctx):
    """The rollback case: release #75 compares values only, and the row is still there."""
    ctx = make_ctx()
    with ctx.db.session() as s:
        suppress(s, "pat@firm-f.example", "asked")
    assert lift(ctx, "pat@firm-f.example") is None
    with ctx.db.session() as s:
        assert _as_of_release_75(s, "pat@firm-f.example")
        assert s.execute(text("SELECT count(*) FROM suppressions WHERE value = 'pat@firm-f.example'")).scalar_one() == 1


def test_the_lift_columns_are_added_to_a_database_that_has_entries_and_nothing_is_lifted_by_that(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        suppress(s, "pat@firm-g.example", "asked")
    engine = ctx.db.engine
    with engine.begin() as conn:
        for column in ("lifted_at", "lifted_by", "lift_reason"):
            conn.execute(text(f'ALTER TABLE suppressions DROP COLUMN "{column}"'))
    assert {c.name for t, c in pending_columns(engine) if t == "suppressions"} == {"lifted_at", "lifted_by", "lift_reason"}
    migrate(engine)
    assert not pending_columns(engine)
    with ctx.db.session() as s:
        assert s.execute(select(Suppression.lifted_at)).scalar_one() is None and is_suppressed(s, "pat@firm-g.example")
