"""Phase 5, what a re-read of a company's site may change: the contact of a lead someone was written to, someone erased, people on an
empty page, a sentence read as a title, a hosting platform's blurb. Synthetic data only, fake web and fake mail only."""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.agents.company_research import CompanyResearchAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.company_research import has_real_text, save_contact, withdraw_missing
from aime.contact_validation import assign_published_emails
from aime.crm_ops import erase_person, suppress
from aime.integrations.email_smtp import EmailSender
from aime.migrations import pending_columns
from aime.models import Company, Contact, FollowUp, Lead, ResearchSnapshot, Signal
from aime.outreach import stop_sequence
from aime.people import PersonFinding, extract_people, is_title, name_hash
from aime.site_reader import Page, parse_page
from aime.web.app import create_app
from tests.test_company_research import CONTACT, HOME, SITE, add_company, make_handler
from tests.test_contact_validation import FakeChecker
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID, MAIL, FakeIMAP, contacted, mail, run_inbox
from tests.test_outreach import ORIGIN, POST, SENDING, approve_all, drafts, lead_of, researched

NOW = TUESDAY_11AM_ET
INBOX = f"info@{SITE}"
MARIA = f"maria.lopez@{SITE}"
TEAM_NO_ADDRESS = "<html><body><h2>Our Team</h2><div class='card'><h3>David Chen</h3><p>Chief Operating Officer</p></div></body></html>"
TEAM_WITH_ADDRESS = ("<html><body><h2>Our Team</h2><div class='card'><h3>David Chen</h3><p>Chief Operating Officer</p></div>"
                     f"<p>David Chen</p><p>Email: <a href='mailto:{DAVID}'>{DAVID}</a></p></body></html>")
TEAM_TWO_ADDRESSES = TEAM_WITH_ADDRESS.replace("</body>", "<div class='card'><h3>Maria Lopez</h3><p>Chief Executive Officer</p></div>"
                                               f"<p>Maria Lopez</p><p>Email: <a href='mailto:{MARIA}'>{MARIA}</a></p></body>")
JS_SHELL = "<!doctype html><html><head><title>Brightline</title></head><body><div id='root'></div><script src='/main.js'></script><noscript>You need to enable JavaScript to run this app.</noscript></body></html>"


def run(ctx, when=NOW, sender=None):
    return SalesWorkflowAgent(ctx, sender=sender).run({"now": when.isoformat()}).output


def click(ctx, f, action):
    assert TestClient(create_app(ctx)).post(f"/followups/{f.id}/{action}", headers=ORIGIN, follow_redirects=False).status_code == 303


def emailed_the_company_inbox(make_ctx, **overrides):
    """A lead whose company published only info@ when it was first researched, and whose first email went there."""
    requests: list[str] = []
    pages = {"/team": ("text/html", TEAM_NO_ADDRESS), "/": ("text/html", HOME), "/contact": ("text/html", CONTACT)}
    ctx = make_ctx(handler=make_handler(requests, pages), **{**MAIL, **overrides})
    cid = add_company(ctx)
    with ctx.db.session() as s:
        s.add(Signal(company_id=cid, signal_type="hiring", text=POST, source="hn_hiring", source_url="https://news.ycombinator.com/item?id=123",
                     fingerprint="post-1", keywords=["manual data entry", "spreadsheets"], strength=0.8))
    assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]}).ok
    FakeSMTP.sent = []
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    run(ctx, NOW, sender)
    approve_all(ctx)
    run(ctx, NOW, sender)
    assert FakeSMTP.sent[0]["To"] == INBOX and lead_of(ctx).outreach_status == "contacted"

    def site_now_names_a_person(team=TEAM_WITH_ADDRESS):  # the normal re-check after recheck_days reads the changed team page
        pages["/team"] = ("text/html", team)
        ctx.http_transport = httpx.MockTransport(make_handler(requests, pages))
        assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]}).ok
        assert lead_of(ctx).contact_id is not None

    with ctx.db.session() as s:
        s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalars().first().sent_at = NOW - timedelta(days=5)
    return ctx, sender, site_now_names_a_person


def step2(ctx):
    return [f for f in drafts(ctx) if f.step == 2 and f.status != "cancelled"]


def contact_of_lead(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Lead)).scalar_one().contact.email


def maria_would_rank_first(ctx):
    """For sales work a Chief Executive fits a little better than a Chief Operating Officer, with the same evidence."""
    with ctx.db.session() as s:
        s.execute(select(Lead)).scalar_one().primary_service = "sales_automation"


def read_again(ctx, **pages):
    requests: list[str] = []
    overrides = {"/" + k.replace("_", "-"): ("text/html", v) for k, v in pages.items() if k != "home"}
    if "home" in pages:
        overrides["/"] = ("text/html", pages["home"])
    ctx.http_transport = httpx.MockTransport(make_handler(requests, overrides))
    with ctx.db.session() as s:
        cid = s.execute(select(Company.id)).scalars().first()
    assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]}).ok


# ------------------------------------------------------------------ once written to, the lead stays with the address
def test_the_follow_up_goes_to_the_address_the_first_email_went_to(make_ctx):
    ctx, sender, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    site_now_names_a_person()
    run(ctx, NOW, sender)
    assert [f.to_email for f in step2(ctx)] == [INBOX]


def test_a_new_person_on_the_site_does_not_cancel_an_approved_follow_up_unseen(make_ctx):
    ctx, sender, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    run(ctx, NOW, sender)
    assert [f.to_email for f in step2(ctx)] == [INBOX]
    site_now_names_a_person()
    approve_all(ctx)  # the person approved the email to the company inbox
    run(ctx, NOW, sender)
    first = [f for f in drafts(ctx) if f.step == 2][0]
    assert first.status == "sent" and FakeSMTP.sent[-1]["To"] == INBOX, f"{first.status}: {first.blocked_reason}"


def test_the_one_check_in_goes_to_the_address_that_was_written_to(make_ctx):
    ctx, sender, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    ctx.settings.business["outreach"]["reengage_after_days"] = 90
    site_now_names_a_person()
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        lead.status, lead.outreach_status = "nurture", "no_response"
    run(ctx, NOW + timedelta(days=86), sender)
    (f,) = [x for x in drafts(ctx) if x.purpose == "reengage"]
    assert f.to_email == INBOX


def test_end_to_end_three_sends_the_pipeline_a_re_check_and_ninety_days_all_reach_one_address(make_ctx):
    """No status set by hand: the real sends, the real sequence end, a re-read that finds a person, and the one check-in."""
    ctx, sender, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    ctx.settings.business["outreach"]["reengage_after_days"] = 90
    with ctx.db.session() as s:
        s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalars().first().sent_at = NOW
    for day in (6, 13):
        run(ctx, NOW + timedelta(days=day), sender)
        approve_all(ctx)
        run(ctx, NOW + timedelta(days=day), sender)
    run(ctx, NOW + timedelta(days=21), sender)
    assert [m["To"] for m in FakeSMTP.sent] == [INBOX] * 3
    site_now_names_a_person()
    run(ctx, NOW + timedelta(days=105), sender)
    (f,) = [x for x in drafts(ctx) if x.purpose == "reengage"]
    assert f.to_email == INBOX and contact_of_lead(ctx) == INBOX
    approve_all(ctx)
    run(ctx, NOW + timedelta(days=105), sender)
    assert [m["To"] for m in FakeSMTP.sent] == [INBOX] * 4


def test_a_named_person_written_to_is_not_displaced_by_a_better_named_person(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    assert sent.to_email == DAVID
    maria_would_rank_first(ctx)
    read_again(ctx, team=TEAM_TWO_ADDRESSES)
    assert contact_of_lead(ctx) == DAVID
    with ctx.db.session() as s:
        assert s.execute(select(Contact.id).where(Contact.email == MARIA)).first(), "she stays on record, as a contact of the company"


# ------------------------------------------------------------------ a do-not-contact entry on the inbox, in every order
def test_do_not_contact_then_a_sales_run_then_a_re_check_writes_to_nobody(make_ctx):
    ctx, sender, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    with ctx.db.session() as s:
        suppress(s, INBOX, "Asked by phone: stop writing")
    run(ctx, NOW, sender)
    assert not step2(ctx)
    site_now_names_a_person()
    run(ctx, NOW + timedelta(hours=1), sender)
    assert not step2(ctx) and contact_of_lead(ctx) == INBOX


def test_do_not_contact_then_a_re_check_before_any_sales_run_writes_to_nobody(make_ctx):
    ctx, sender, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    with ctx.db.session() as s:
        suppress(s, INBOX, "Asked by phone: stop writing")
    site_now_names_a_person()
    run(ctx, NOW + timedelta(hours=1), sender)
    assert not step2(ctx) and contact_of_lead(ctx) == INBOX


def test_a_re_check_then_do_not_contact_writes_to_nobody(make_ctx):
    ctx, sender, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    site_now_names_a_person()
    with ctx.db.session() as s:
        suppress(s, INBOX, "Asked by phone: stop writing")
    run(ctx, NOW + timedelta(hours=1), sender)
    assert not step2(ctx) and contact_of_lead(ctx) == INBOX


def test_the_scheduled_re_check_forty_days_on_does_not_revive_a_stopped_sequence(make_ctx):
    """The ordinary run, with no company ids: the company comes up because its read has aged."""
    ctx, sender, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    with ctx.db.session() as s:
        suppress(s, INBOX, "Asked by phone: stop writing")
    run(ctx, NOW, sender)
    run(ctx, NOW + timedelta(days=20), sender)
    with ctx.db.session() as s:
        c = s.execute(select(Company)).scalars().first()
        c.researched_at = c.researched_at - timedelta(days=40)
    requests: list[str] = []
    ctx.http_transport = httpx.MockTransport(make_handler(requests, {"/team": ("text/html", TEAM_WITH_ADDRESS), "/": ("text/html", HOME), "/contact": ("text/html", CONTACT)}))
    assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({}).ok and requests
    run(ctx, NOW + timedelta(days=40), sender)
    assert not step2(ctx) and contact_of_lead(ctx) == INBOX


# ------------------------------------------------------------------ when the contact may still change, and when not
def test_a_lead_nobody_wrote_to_still_moves_to_the_named_person(make_ctx):
    requests: list[str] = []
    pages = {"/team": ("text/html", TEAM_NO_ADDRESS), "/": ("text/html", HOME), "/contact": ("text/html", CONTACT)}
    ctx = make_ctx(handler=make_handler(requests, pages), **MAIL)
    add_company(ctx)
    read_again(ctx, team=TEAM_NO_ADDRESS, home=HOME, contact=CONTACT)
    assert contact_of_lead(ctx) == INBOX
    read_again(ctx, team=TEAM_WITH_ADDRESS, home=HOME, contact=CONTACT)
    assert contact_of_lead(ctx) == DAVID


def test_a_rehearsal_in_test_mode_does_not_hold_the_lead_to_its_address(make_ctx):
    """It went to our own address, so the prospect has heard nothing from us."""
    ctx = researched(make_ctx, **SENDING, outbound_test_recipients=["qa@us.example"])
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        s.add(FollowUp(lead_id=lead.id, contact_id=lead.contact_id, company_id=lead.company_id, channel="email", kind="outreach", step=1, status="sent",
                       subject="Rehearsal", body="x", to_email=lead.contact.email, delivered_to="qa@us.example", sent_at=NOW))
        lead.outreach_status = "test_sent"
    maria_would_rank_first(ctx)
    read_again(ctx, team=TEAM_TWO_ADDRESSES)
    assert contact_of_lead(ctx) == MARIA


@pytest.mark.parametrize("status", ["sending", "uncertain"])
def test_an_email_that_may_have_gone_holds_the_lead_too(make_ctx, status):
    ctx = researched(make_ctx, **SENDING)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        s.add(FollowUp(lead_id=lead.id, contact_id=lead.contact_id, company_id=lead.company_id, channel="email", kind="outreach", step=1, status=status,
                       subject="Under way", body="x", to_email=lead.contact.email))
    maria_would_rank_first(ctx)
    read_again(ctx, team=TEAM_TWO_ADDRESSES)
    assert contact_of_lead(ctx) == DAVID


def test_an_opted_out_contact_stays_on_a_closed_lead(make_ctx):
    """The inbox said no and the site then names a person. The closed lead keeps the record of who said no."""
    ctx, sender, _site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        suppress(s, INBOX, "Unsubscribe request by email")
        stop_sequence(s, lead, "unsubscribed", "Asked not to be contacted", "inbox_monitor")
    assert LeadQualificationAgent(ctx).run({}).ok
    assert lead_of(ctx).status == "disqualified"
    read_again(ctx, team=TEAM_WITH_ADDRESS, home=HOME, contact=CONTACT)
    assert lead_of(ctx).status == "disqualified" and contact_of_lead(ctx) == INBOX


def test_a_real_opt_out_reply_before_a_re_check_keeps_the_contact_and_stops_the_mail(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Please unsubscribe me from this list.", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    maria_would_rank_first(ctx)
    read_again(ctx, team=TEAM_TWO_ADDRESSES)
    run(ctx, NOW + timedelta(days=5), sender)
    assert contact_of_lead(ctx) == DAVID and lead_of(ctx).outreach_status == "unsubscribed" and not step2(ctx)


@pytest.mark.parametrize("change", ["opted_out", "unsubscribed", "bounced", "disqualified", "converted", "merged"])
def test_nothing_moves_a_contact_off_a_lead_nobody_wrote_to_when_it_is_closed_or_stopped(make_ctx, change):
    ctx = researched(make_ctx, **SENDING)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        if change == "opted_out":
            lead.contact.consent_status = "opted_out"
        elif change in ("unsubscribed", "bounced"):
            lead.outreach_status = change
        else:
            lead.status = change
    maria_would_rank_first(ctx)
    read_again(ctx, team=TEAM_TWO_ADDRESSES)
    assert contact_of_lead(ctx) == DAVID


def test_a_withdrawn_contact_is_cleared_only_while_nobody_was_written_to(make_ctx):
    def withdrawn_with_nobody_left(make):
        ctx = researched(make, **SENDING)
        with ctx.db.session() as s:
            lead = s.execute(select(Lead)).scalar_one()
            for c in s.execute(select(Contact)).scalars():
                c.source = "manual"  # none researched is left to move to
            lead.contact.source = "website_withdrawn"
        return ctx
    ctx = withdrawn_with_nobody_left(make_ctx)
    read_again(ctx, home="<html><body><p>Nothing here names anyone at all, and this is the whole of the page, which is rather short.</p></body></html>", team=404, about_us=404, contact=404)
    assert lead_of(ctx).contact_id is None

    ctx = withdrawn_with_nobody_left(make_ctx)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        s.add(FollowUp(lead_id=lead.id, contact_id=lead.contact_id, company_id=lead.company_id, channel="email", kind="outreach", step=1, status="sent",
                       subject="Sent", body="x", to_email=lead.contact.email, sent_at=NOW))
    read_again(ctx, home="<html><body><p>Nothing here names anyone at all, and this is the whole of the page, which is rather short.</p></body></html>", team=404, about_us=404, contact=404)
    assert lead_of(ctx).contact_id is not None


def test_an_open_lead_with_no_contact_can_still_be_given_one(make_ctx):
    ctx = make_ctx(handler=make_handler([], {"/team": ("text/html", TEAM_WITH_ADDRESS), "/": ("text/html", HOME), "/contact": ("text/html", CONTACT)}), **MAIL)
    add_company(ctx)
    assert lead_of(ctx).contact_id is None
    read_again(ctx, team=TEAM_WITH_ADDRESS, home=HOME, contact=CONTACT)
    assert contact_of_lead(ctx) == DAVID


def test_a_closed_lead_with_no_contact_is_not_given_one(make_ctx):
    ctx = make_ctx(handler=make_handler([], {"/team": ("text/html", TEAM_WITH_ADDRESS), "/": ("text/html", HOME), "/contact": ("text/html", CONTACT)}), **MAIL)
    add_company(ctx)
    with ctx.db.session() as s:
        s.execute(select(Lead)).scalar_one().status = "disqualified"
    read_again(ctx, team=TEAM_WITH_ADDRESS, home=HOME, contact=CONTACT)
    assert lead_of(ctx).contact_id is None


# ------------------------------------------------------------------ erased
def erased_david(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    with ctx.db.session() as s:
        assert s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one().last_name == "Chen"
        erase_person(s, DAVID, "owner")
    return ctx


def test_a_person_who_asked_to_be_erased_is_not_collected_again(make_ctx):
    ctx = erased_david(make_ctx)
    read_again(ctx)  # the page still prints the name and the address
    with ctx.db.session() as s:
        assert not s.execute(select(Contact.id).where(Contact.email == DAVID)).first()
        assert not s.execute(select(Contact.id).where(Contact.last_name == "Chen")).first()
        snap = s.execute(select(ResearchSnapshot).order_by(ResearchSnapshot.id.desc())).scalars().first()
        assert "David Chen" not in str(snap.people) and "David Chen" not in (snap.summary or "") and DAVID not in str(snap.emails)
        assert s.execute(select(Contact.id).where(Contact.last_name == "Lopez")).first(), "the others on the page are still collected"


def test_an_erased_row_keeps_no_name_only_a_one_way_hash(make_ctx):
    ctx = erased_david(make_ctx)
    with ctx.db.session() as s:
        row = s.execute(select(Contact).where(Contact.consent_source == "[erased on request]")).scalar_one()
        assert row.first_name is None and row.last_name is None and row.email is None
        assert row.erased_name_hash == name_hash("David Chen") and "chen" not in row.erased_name_hash


def test_an_erased_person_is_known_with_or_without_a_middle_initial(make_ctx):
    ctx = erased_david(make_ctx)
    with ctx.db.session() as s:
        company = s.execute(select(Company)).scalars().first()
        for spelling in ("David Chen", "David A. Chen", "DAVID CHEN"):
            finding = PersonFinding(spelling, "COO", "operations", "high", "card", "https://x.example/team")
            assert save_contact(s, company, finding, None) == (None, False)
        finding = PersonFinding("Dana Chen", "COO", "operations", "high", "card", "https://x.example/team")
        contact, created = save_contact(s, company, finding, None)
        assert created and contact.last_name == "Chen"


def test_the_hash_of_an_erased_person_counts_only_at_their_company(make_ctx):
    ctx = erased_david(make_ctx)
    with ctx.db.session() as s:
        other = Company(name="Other Co", normalized_name="other co", domain="other.example", source="manual")
        s.add(other)
        s.flush()
        _, created = save_contact(s, other, PersonFinding("David Chen", "COO", "operations", "high", "card", "https://other.example/team"), None)
        assert created


def fir_retail(s):
    company = Company(name="Fir Retail", normalized_name="fir retail", domain="fir.example", source="manual")
    s.add(company)
    s.flush()
    return company


FIR_INBOX = [{"email": "info@fir.example", "url": "https://fir.example/contact", "near": None, "on_domain": True}]


@pytest.mark.parametrize("spelling", ["info@fir.example", "info+news@fir.example", "INFO@fir.example"])
def test_an_erased_address_is_not_attached_again(make_ctx, spelling):
    ctx = make_ctx(**SENDING)
    with ctx.db.session() as s:
        company = fir_retail(s)
        s.add(Contact(company_id=company.id, email="info@fir.example", source="website", role_category="inbox", consent_status="none"))
        s.flush()
        erase_person(s, spelling.lower().replace("+news", ""), "owner")
        published = [{**FIR_INBOX[0], "email": spelling}]
        assert assign_published_emails(s, company, published, checker=lambda d: True) == {"person_emails": 0, "inboxes": 0, "skipped": 1}
        assert not s.execute(select(Contact.id).where(Contact.email.is_not(None))).first()


def test_an_address_listed_before_it_was_erased_is_not_attached_again(make_ctx):
    ctx = make_ctx(**SENDING)
    with ctx.db.session() as s:
        company = fir_retail(s)
        suppress(s, "info@fir.example", "Asked by phone")
        erase_person(s, "info@fir.example", "owner")
        assert assign_published_emails(s, company, FIR_INBOX, checker=lambda d: True)["skipped"] == 1


@pytest.mark.parametrize("value", ["info@fir.example", "fir.example"])
def test_an_address_on_the_list_for_another_reason_stays_on_file_where_it_closes_the_lead(make_ctx, value):
    """Skipping every listed address would reopen things: with the address off file a lead its entry closed
    stays open, and an entry at a sub-domain of the company stops counting for the company."""
    ctx = make_ctx(**SENDING)
    with ctx.db.session() as s:
        company = fir_retail(s)
        suppress(s, value, "Asked by phone")
        assert assign_published_emails(s, company, FIR_INBOX, checker=lambda d: True)["inboxes"] == 1
        assert s.execute(select(Contact.id).where(Contact.email == "info@fir.example")).first()


def test_a_do_not_contact_entry_before_the_first_read_still_closes_the_lead(make_ctx):
    requests: list[str] = []
    ctx = make_ctx(handler=make_handler(requests, {"/team": ("text/html", TEAM_NO_ADDRESS), "/": ("text/html", HOME), "/contact": ("text/html", CONTACT)}), **MAIL)
    cid = add_company(ctx)
    with ctx.db.session() as s:
        suppress(s, INBOX, "Asked by phone")
    assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]}).ok
    assert LeadQualificationAgent(ctx).run({}).ok
    assert lead_of(ctx).status == "disqualified" and contact_of_lead(ctx) == INBOX


def test_a_database_from_before_this_gets_the_column_on_migration(make_ctx):
    ctx = make_ctx()
    assert not pending_columns(ctx.db.engine), "a new database has it"
    assert "erased_name_hash" in Contact.__table__.columns and Contact.__table__.columns["erased_name_hash"].nullable


# ------------------------------------------------------------------ an empty page reads nobody out
def test_an_empty_javascript_page_on_a_re_read_withdraws_nobody(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    read_again(ctx, home=JS_SHELL, team=JS_SHELL, about_us=JS_SHELL, contact=JS_SHELL)
    with ctx.db.session() as s:
        assert set(s.execute(select(Contact.source).where(Contact.last_name == "Chen")).scalars()) == {"website"}


def test_a_team_page_gone_empty_withdraws_nobody_while_the_home_page_reads_well(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    read_again(ctx, team=JS_SHELL)
    with ctx.db.session() as s:
        assert set(s.execute(select(Contact.source).where(Contact.last_name == "Chen")).scalars()) == {"website"}


def test_a_team_page_with_words_on_it_still_withdraws_who_left(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    with ctx.db.session() as s:
        assert s.execute(select(Contact.source).where(Contact.last_name == "Baker")).scalar_one() == "website"
    read_again(ctx, team="<html><body><h2>Our Team</h2><div class='card'><h3>David Chen</h3><p>Chief Operating Officer</p></div></body></html>")
    with ctx.db.session() as s:
        assert s.execute(select(Contact.source).where(Contact.last_name == "Baker")).scalar_one() == "website_withdrawn"
        assert s.execute(select(Contact.source).where(Contact.last_name == "Chen")).scalar_one() == "website"


def test_withdraw_missing_counts_only_pages_with_text(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        company = Company(name="Fir", normalized_name="fir", domain="fir.example", source="manual")
        s.add(company)
        s.flush()
        s.add(Contact(company_id=company.id, first_name="Pat", last_name="Reed", source="website", evidence_url="https://fir.example/team"))
        s.flush()
        empty = Page("https://fir.example/team", "team", "ok")
        assert withdraw_missing(s, company, [], [empty]) == []
        read = Page("https://fir.example/team", "team", "ok", blocks=[("p", "x" * 200)], content_blocks=[("p", "x" * 200)])
        assert [c.last_name for c in withdraw_missing(s, company, [], [read])] == ["Reed"]


def test_has_real_text():
    assert not has_real_text(Page("u", "home", "http_500", blocks=[("p", "x" * 500)]))
    assert not has_real_text(Page("u", "home", "ok"))
    assert not has_real_text(Page("u", "home", "ok", blocks=[("p", "x" * 199)]))
    assert has_real_text(Page("u", "home", "ok", blocks=[("p", "x" * 200)]))
    assert has_real_text(Page("u", "home", "ok", blocks=[("p", "a"), ("p", "b"), ("p", "c")]))
    # Only a menu around an empty app: its links are no words of the page's own.
    assert not has_real_text(Page("u", "home", "ok", blocks=[("li", "Home"), ("li", "About"), ("li", "Team")], content_blocks=[]))


# ------------------------------------------------------------------ what passes as a person with a title
ABOUT_WITH_VALUES = ("<html><body><div><h3>Owners Mindset</h3><p>We believe that each team member is an owner.</p></div>"
                     "<div><h3>Rhea Stone</h3><p>Chief Executive Officer</p></div></body></html>")


def test_a_sentence_on_a_values_card_is_not_a_person(make_ctx):
    assert is_title("We believe that each team member is an owner.")  # why decision_maker_title is needed as well
    ctx = make_ctx(handler=make_handler([], {"/about-us": ("text/html", ABOUT_WITH_VALUES), "/team": ("text/html", TEAM_NO_ADDRESS)}), **SENDING)
    add_company(ctx)
    read_again(ctx, about_us=ABOUT_WITH_VALUES, team=TEAM_NO_ADDRESS)
    with ctx.db.session() as s:
        names = [c.display_name for c in s.execute(select(Contact)).scalars()]
    assert "Owners Mindset" not in names and "Rhea Stone" in names and "David Chen" in names


def test_the_same_sentence_on_one_line_is_not_a_person():
    page = parse_page("https://x.example/about", "about", "<html><body><p>Owners Mindset, We believe that each team member is an owner.</p><p>Rhea Stone, Founder and CEO</p></body></html>")
    assert [(p.full_name, p.title) for p in extract_people([page])] == [("Rhea Stone", "Founder and CEO")]


@pytest.mark.parametrize("title", ["Chief Executive Officer", "CEO", "Founder & CEO", "Co-Founder", "President", "VP of Sales", "Head of Engineering", "Managing Partner",
                                   "Director of Operations", "General Manager", "Owner", "Business Owner", "Sales Manager", "Principal", "Chief Technology Officer"])
def test_real_titles_still_pass(title):
    page = parse_page("https://x.example/team", "team", f"<html><body><h3>Rhea Stone</h3><p>{title.replace('&', '&amp;')}</p><p>Cole Vance, {title.replace('&', '&amp;')}</p></body></html>")
    assert {(p.full_name, p.title) for p in extract_people([page])} == {("Rhea Stone", title), ("Cole Vance", title)}


# ------------------------------------------------------------------ a hosting platform's blurb is not the company's words
NOTION = ('<html><head><title>Fir Retail</title><meta name="description" content="Made in Notion, the connected workspace where better, faster work happens for every team.">'
          "</head><body><div id='notion-app'></div></body></html>")


def test_a_hosting_platforms_blurb_is_not_stored_as_the_description(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    read_again(ctx, home=NOTION)
    with ctx.db.session() as s:
        snap = s.execute(select(ResearchSnapshot).order_by(ResearchSnapshot.id.desc())).scalars().first()
    assert not (snap.description or "").startswith("Made in") and "Made in" not in (snap.summary or "")


def test_a_description_falls_to_the_next_candidate_past_the_blurb(make_ctx):
    home = NOTION.replace("<div id='notion-app'></div>", "<p>Fir Retail sells home goods through its own online store and ships to every state from a warehouse in Ohio.</p>")
    ctx = researched(make_ctx, **SENDING)
    read_again(ctx, home=home)
    with ctx.db.session() as s:
        snap = s.execute(select(ResearchSnapshot).order_by(ResearchSnapshot.id.desc())).scalars().first()
    assert snap.description.startswith("Fir Retail sells")
