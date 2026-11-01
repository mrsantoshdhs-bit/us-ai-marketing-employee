"""Phase 5, names and quotes: an email writes to a company by the right name, a job post's requirements and pay are not quoted back at it, and
a website that could not be read does not hide what it said before. Synthetic data only, fake web and fake mail only."""
from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from sqlalchemy import func, select

import aime.agents.research as research_mod
from aime.agents.research import USBusinessResearchAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.crm_ops import upsert_company
from aime.dedupe import company_to_address, describes_rather_than_names, find_duplicate, normalize_name
from aime.models import Company, Contact, FollowUp, Lead, ResearchSnapshot
from aime.outreach import gather_evidence, short_quote, website_quote
from aime.qc.email_checks import run_email_checks
from aime.sources.base import CompanyCandidate
from aime.validation import validate_candidate
from tests.test_inbox import DAVID, FakeIMAP, contacted, mail, run_inbox
from tests.test_outreach import POSTAL, drafts, lead_of, researched
from tests.test_research import filings
from tests.uat.test_uat_email import ORIGIN, web


def add_lead(ctx, description="Firm builds reliable tools for modern operations teams."):
    with ctx.db.session() as s:
        co = Company(name="Firm", normalized_name="firm", source="test", domain="firm.example")
        s.add(co)
        s.flush()
        s.add(ResearchSnapshot(company_id=co.id, status="done", description=description, description_url="https://firm.example/"))
        c = Contact(company_id=co.id, email="info@firm.example", source="website", email_source_url="https://firm.example/c", email_status="role_address",
                    role_category="inbox", consent_status="none")
        s.add(c)
        s.flush()
        lead = Lead(company_id=co.id, contact_id=c.id, source="research", status="qualified", score=70, grade="B", primary_service="ai_agents")
        s.add(lead)
        s.flush()
        return lead.id


# ------------------------------------------------------------------ the description on record
@pytest.mark.parametrize("status", ["failed", "blocked", "redirected"])
def test_a_failed_read_of_the_website_keeps_the_description_on_record(make_ctx, status):
    ctx = make_ctx(**POSTAL)
    lead_id = add_lead(ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(ResearchSnapshot(company_id=lead.company_id, status=status, error="timeout"))
        s.flush()
        ev = gather_evidence(s, lead)
        assert ev and ev[0].kind == "description"


def test_a_newer_description_still_wins_over_an_older_one(make_ctx):
    ctx = make_ctx(**POSTAL)
    lead_id = add_lead(ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(ResearchSnapshot(company_id=lead.company_id, status="done", description="Firm now builds careful tools for field service teams.",
                               description_url="https://firm.example/"))
        s.flush()
        assert gather_evidence(s, lead)[0].fact == "Firm now builds careful tools for field service teams."


def test_a_site_that_never_gave_a_description_still_gives_its_newest_read_for_the_topics(make_ctx):
    ctx = make_ctx(**POSTAL)
    lead_id = add_lead(ctx, description=None)
    need = {"service": "ai_agents", "keywords": ["ai agents"], "quote": "We want to use AI agents in our support desk this year.", "url": "https://firm.example/a"}
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(ResearchSnapshot(company_id=lead.company_id, status="no_people", needs=[need]))
        s.flush()
        assert [e.kind for e in gather_evidence(s, lead)] == ["website"]


NEED = {"service": "ai_agents", "keywords": ["ai agents"], "quote": "We want to use AI agents in our support desk this year.", "url": "https://firm.example/a"}
GOOD = "Firm builds reliable tools for modern operations teams."


@pytest.mark.parametrize("older,newer,kinds", [
    (dict(description="Home"), dict(description=None, needs=[NEED]), ["website"]),  # an older line unfit to quote does not hide the latest read
    (dict(description=GOOD), dict(description=None, needs=[NEED]), ["description", "website"]),  # a finding of the latest read is kept
    (dict(description=GOOD, needs=[NEED]), dict(description=None, needs=[]), ["description"]),  # a topic the latest read no longer found is not quoted
    (dict(description=GOOD, needs=[NEED]), dict(description=None, needs=[], status="no_people"), ["description"]),
    (dict(description=GOOD, needs=[NEED]), dict(description=None, needs=None, status="failed"), ["description", "website"]),  # no read: the last read stands
])
def test_the_topics_come_from_the_latest_real_read(make_ctx, older, newer, kinds):
    ctx = make_ctx(**POSTAL)
    lead_id = add_lead(ctx, description=None)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        for row in (older, newer):
            s.add(ResearchSnapshot(**{"company_id": lead.company_id, "status": "done", "description_url": "https://firm.example/", **row}))
            s.flush()
        assert [e.kind for e in gather_evidence(s, lead)] == kinds


# ------------------------------------------------------------------ what is not quoted from a job post
@pytest.mark.parametrize("line", ["No AI text please, keep it short, shorter emails are more likely to get a response.",
                                  "React and TypeScript at depth, Node or Python behind it.",
                                  "Data Science Background heavily preferred.",
                                  "But no requirement to work in the office.",
                                  "All other options are not possible, like H1B or OPT visas, for us.",
                                  "The base pay range for this post is 180,000 to 220,000 USD per year.",
                                  "Partner with eng on the surfaces that bring in new users.",
                                  "Flask and Python deeply internalized; solid JS skills and able to handle Vue/React.",
                                  "Base is 90,000 to 120,000 dollars with stock on top.",
                                  "Strong SQL skills and an eye for detail are key."])
def test_how_to_apply_requirements_and_pay_are_not_quoted_from_a_job_post(line):
    assert short_quote(line) == ""


@pytest.mark.parametrize("line", ["Firm is re-imagining the trillion-dollar freight market, with a focus on the country's 500,000 fleets and 40 million trucks.",
                                  "Firm is a CRM for small and midsize sales teams (11,000+ paying customers).",
                                  "We build software for regional carriers and the people who plan their routes.",
                                  "Firm is automating document processing and income qualification for loan originators.",
                                  "Firm is a search extension for Postgres used by thousands of teams."])
def test_what_a_company_says_about_its_work_is_still_quoted_from_a_job_post(line):
    assert short_quote(line) == line


def test_a_pay_range_is_no_quote_from_a_website_either_but_a_large_figure_is():
    assert website_quote("Expect to earn 90,000 to 120,000 dollars a year with us.") == ""
    fine = "Firm moves 90,000 pallets a year for 120 regional grocers."
    assert website_quote(fine) == fine


# ------------------------------------------------------------------ the name to write
def test_a_renamed_company_is_written_to_by_its_new_name():
    assert company_to_address("Oldname", "Newname (formerly Oldname) | Gateway for teams", "newname.example") == "Newname"
    assert company_to_address("Oldname", "Newname Labs (previously Oldname) - Gateway", "newnamelabs.example") == "Newname Labs"
    assert company_to_address("Oldname Inc.", "Newname (Formerly known as OLDNAME) | Gateway", "newname.example") == "Newname"


@pytest.mark.parametrize("title", ["Newname (formerly Othername) | Gateway for teams",  # another company's old name
                                   "Newname | Gateway for teams, formerly Oldname",
                                   "Welcome to Newname (formerly Oldname)",  # no name on its own: not guessed from
                                   "Hiring Now (formerly Oldname)",
                                   None])
def test_no_wider_guess_at_a_new_name(title):
    assert company_to_address("Oldname", title, "newname.example") == "Oldname"


@pytest.mark.parametrize("title,domain", [("Welcome To Newname (formerly Oldname)", "newname.example"),  # capitals do not make it a name
                                          ("Home (formerly Oldname)", "old.example"),
                                          ("Careers (formerly Oldname)", "old.example"),
                                          ("Acquired By Bigco (previously Oldname)", "old.example"),
                                          ("Buy Cheap Stuff Now (formerly Oldname)", "old.example"),
                                          ("X (formerly Oldname)", "old.example"),
                                          ("Newname (formerly Oldname) | Gateway", "oldname.example"),  # still on its old domain: the old name is safe
                                          ("Newname (formerly Oldname) | Gateway", None)])
def test_a_new_name_must_spell_the_domain_of_its_site(title, domain):
    assert company_to_address("Oldname", title, domain) == "Oldname"


def test_a_new_name_may_follow_a_domain_prefix():
    assert company_to_address("Oldname", "Newname (formerly Oldname)", "getnewname.example") == "Newname"


def test_a_name_a_person_set_stays_whatever_the_title_says():
    title = "Newname (formerly Oldname) | Gateway for teams"
    assert company_to_address("Oldname", title, "newname.example", name_set_by="eva") == "Oldname"
    assert company_to_address("ACME WIDGETS", "Widget tools | Acme Widgets", "acmewidgets.example", name_set_by="eva") == "ACME WIDGETS"
    assert company_to_address("We Are Widgets", None, None, name_set_by="eva") == "We Are Widgets"
    assert company_to_address("zentro.example.com", "Zentro | Tools", "zentro.example", name_set_by="eva") == "zentro.example.com"


def test_a_company_shouted_in_a_job_post_header_gets_the_spelling_of_its_own_site():
    assert company_to_address("ACME WIDGETS", "Widget tools | Acme Widgets", "acmewidgets.example") == "Acme Widgets"


@pytest.mark.parametrize("name,title,domain", [("ACMEX", "ACMEX: tools for teams | Sign in", "tryacmex.example"),  # a real all-capitals brand's title
                                               ("ACMEX", None, "acmex.example"),
                                               ("ACMEX", "Welcome To ACMEX", "acmexlearn.example"),
                                               ("ACME WIDGETS INC", "Acme Widgets | Tools", "acmewidgets.example")])  # not the same letters
def test_capitals_stay_unless_the_site_spells_the_same_name(name, title, domain):
    assert company_to_address(name, title, domain) == name


def test_a_placeholder_name_gives_way_to_the_name_its_site_gives():
    assert company_to_address("AI x Logistics Startup", "Billing for carriers | Acme Freight", "acmefreight.example") == "Acme Freight"
    assert company_to_address("AI x Logistics Startup", "Billing for carriers", "acmefreight.example") is None
    assert company_to_address("AI x Logistics Startup") is None


def test_a_draft_uses_the_new_name_and_the_stored_name_is_not_touched(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    cid = lead_of(ctx).company_id
    with ctx.db.session() as s:
        s.get(Company, cid).website_title = "Newname (formerly Brightline Logistics) | Freight software"
        s.get(Company, cid).domain = "newname.example"
    SalesWorkflowAgent(ctx).run({"send": False})
    (f,) = drafts(ctx)
    assert "Newname" in f.subject and "Brightline" not in f.subject
    with ctx.db.session() as s:
        co = s.get(Company, cid)
        assert (co.name, co.normalized_name, co.name_set_by) == ("Brightline Logistics", "brightline logistics", None)


def correct_name(ctx, name):
    web(ctx).post(f"/companies/{lead_of(ctx).company_id}/name", data={"name": name}, headers=ORIGIN)


def test_a_name_a_person_corrected_is_the_name_written_in_emails(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    correct_name(ctx, "We Are Widgets")
    SalesWorkflowAgent(ctx).run({"send": False})
    (f,) = drafts(ctx)
    assert "We Are Widgets" in f.subject


def test_the_quality_check_reads_the_name_a_person_set_as_the_draft_does(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    correct_name(ctx, "We Are Widgets")
    SalesWorkflowAgent(ctx).run({"send": False})
    (f,) = drafts(ctx)
    with ctx.db.session() as s:
        contact = s.get(Contact, f.contact_id)
        named = lambda subject, body: {c["name"]: c["status"] for c in run_email_checks(subject, body, f.evidence or [], contact, False)["checks"]}["company name"]  # noqa: E731
        assert named(f.subject, f.body) == "pass"
        assert named("Question about AI agents", "Hello team,\n\nA note.") == "uncertain", "a draft that leaves the name out is still held"


def test_an_answer_to_a_reply_uses_the_name_a_person_set(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    correct_name(ctx, "We Are Widgets")
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds interesting. Could we talk Thursday afternoon?", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    with ctx.db.session() as s:
        answer = s.execute(select(FollowUp).where(FollowUp.kind == "response").order_by(FollowUp.id.desc())).scalars().first()
        assert "We Are Widgets" in answer.body


def test_a_job_post_header_is_no_company_name():
    assert company_to_address("Role: UX Designer 1099 CONTRACT APPLY") is None
    assert company_to_address("Company: acme.example Location: Austin TX ONSITE") is None
    assert company_to_address("acme.example Location: Austin TX") is None


@pytest.mark.parametrize("name", ["reapplyit", "Apply Widgets", "Applied Widgets", "Location Widgets", "Geolocate Widgets Inc", "Job Widgets", "Company Widgets", "Rolepath Widgets", "Title Widgets",
                                  "Acme: Role Models"])
def test_a_real_name_with_the_same_words_is_still_a_name(name):
    assert not describes_rather_than_names(name) and company_to_address(name) == name


def test_a_post_header_is_refused_at_the_door_and_a_name_with_the_same_words_is_not():
    def candidate(name):
        return CompanyCandidate(name=name, source="hn_hiring", source_url="https://news.example/item?id=1", is_us=True, signal_type="hiring",
                                signal_text="A company builds reliable tools for modern operations teams.")
    assert "name is a description, not a company" in validate_candidate(candidate("Role: UX Designer 1099 CONTRACT"))[0]
    assert validate_candidate(candidate("Location Widgets"))[0] == []


# ------------------------------------------------------------------ one company under its legal forms
def test_spaced_legal_suffixes_normalize_like_the_short_ones():
    assert normalize_name("ACME L.L.C.") == normalize_name("ACME LLC") == "acme"
    assert normalize_name("ACME L P") == normalize_name("ACME LP") == "acme"
    assert normalize_name("Acme, L.P.") == "acme"
    assert normalize_name("Acme Realty OP LP") == normalize_name("Acme Realty Op, L.P.") == normalize_name("Acme Realty Inc.") == "acme realty"


@pytest.mark.parametrize("name,key", [("A L P", "a l p"), ("J.L.P.", "j l p"), ("L P", "l p"), ("L.L.C.", "l l c"),  # initials, no name left to keep
                                      ("L P Foods", "l p foods"), ("Shop LP", "shop"), ("Acme Op", "acme op"), ("Op LP", "op"),
                                      ("Farmers Co-Op LP", "farmers co op"), ("Farmers Co-Op L.P.", "farmers co op"), ("Stop & Go Widgets", "stop and go widgets"),
                                      ("Alp", "alp"), ("The Acme Company", "acme"), ("Acme GmbH", "acme"), ("Acme (formerly Zed) LLC", "acme")])
def test_other_names_keep_their_key(name, key):
    assert normalize_name(name) == key


def candidate(name, record, source="csv_import", state="IN"):
    return CompanyCandidate(name=name, source=source, source_url="https://list.example/1", source_record_id=record, state=state, is_us=True, signal_type="hiring",
                            observed_at=datetime(2026, 10, 30), signal_text=f"{name} builds software for property managers and the people they serve.")


def test_a_row_stored_under_its_old_key_is_still_found_and_never_rewritten(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        s.add(Company(name="Simple Group L P", normalized_name="simple group l p", source="csv_import", state="IN"))
        s.add(Company(name="Other Realty OP LP", normalized_name="other realty op", source="csv_import", state="IN"))
        s.add(Company(name="Other Op", normalized_name="other op", source="csv_import", state="IN"))
        s.flush()
        for name in ("Simple Group L P", "Simple Group Inc."):
            _company, created = upsert_company(s, candidate(name, f"r:{name}"))
            assert not created
        assert not find_duplicate(s, "Other", None, "IN"), "a name that only looks like an old key is another company: Other Op is no Other OP LP"
        assert find_duplicate(s, "Other Realty", None, "IN").name == "Other Realty OP LP"
        assert s.execute(select(func.count(Company.id))).scalar_one() == 3
        assert s.execute(select(Company.normalized_name).where(Company.name == "Simple Group L P")).scalar_one() == "simple group l p"


def test_a_public_company_and_its_operating_partnership_are_one_company_and_a_filing_makes_no_lead(make_ctx):
    """Ported from discovery: it counted one lead. Since the owner's decision of 1 Nov 2026 a filing is evidence and makes no lead, so the two filers
    are one company on file and no lead at all."""
    feed = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><content><CIK>0000000001</CIK></content></entry>'
            "<entry><content><CIK>0000000002</CIK></content></entry></feed>")
    names = {"0000000001": "SIMPLE PROPERTY GROUP INC.", "0000000002": "SIMPLE PROPERTY GROUP L P"}

    def handler(request):
        if request.url.host == "www.sec.gov":
            return httpx.Response(200, text=feed)
        if request.url.host == "data.sec.gov":
            cik = request.url.path.split("CIK")[-1].split(".")[0]
            return httpx.Response(200, json={"name": names[cik], "sicDescription": "Real Estate Investment Trusts", "filings": filings(90),
                                             "addresses": {"business": {"stateOrCountry": "IN", "city": "INDIANAPOLIS"}}})
        return httpx.Response(404)
    ctx = make_ctx(handler=handler, sec_user_agent="Test test@example.com", polite_delay=0.0)
    assert USBusinessResearchAgent(ctx).run({"sources": ["sec_edgar"], "industries": ["real_estate"], "states": ["IN"]}).ok
    with ctx.db.session() as s:
        assert s.execute(select(func.count(Company.id))).scalar_one() == 1
        assert s.execute(select(func.count(Lead.id))).scalar_one() == 0


def test_a_listing_under_both_legal_forms_is_one_lead(make_ctx):
    class Listed:
        name, countries = "csv_import", ("US",)

        def configured(self):
            return True, "ok"

        def fetch(self, focus):
            return [candidate("Simple Property Group Inc.", "r:1"), candidate("Simple Property Group L.P.", "r:2")]
    ctx = make_ctx()
    keep = research_mod.get_sources
    research_mod.get_sources = lambda _c, _n=None: [Listed()]
    try:
        assert USBusinessResearchAgent(ctx).run({"sources": ["csv_import"]}).ok
    finally:
        research_mod.get_sources = keep
    with ctx.db.session() as s:
        assert s.execute(select(func.count(Lead.id))).scalar_one() == 1
