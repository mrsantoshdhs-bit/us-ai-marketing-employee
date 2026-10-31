"""What earns a lead its score and what its first email quotes: evidence in the company's own words. All data is synthetic."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from aime.agents.crm import CRMManagementAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.dedupe import company_to_address, name_from_title, registrable_domain
from aime.knowledge.geo import parse_location
from aime.matching import competitor_evidence, match_services, seeks_external_help
from aime.models import Activity, Company, Contact, FollowUp, Lead, ResearchSnapshot, Signal
from aime.outreach import draft_outreach, draft_reengage, gather_evidence, short_quote, website_quote
from aime.qc.email_checks import run_email_checks
from aime.scoring import read_company, score_lead, signal_fact
from aime.timeutil import utcnow
from tests.test_scoring import facts

SOFTWARE_POST = ("Acme | Senior Engineer | Austin, TX | Full-time | $150k\n\nAcme sells scheduling software to dental clinics across the country. "
                 "We have paying customers and we are hiring engineers.")


def company(**kw):
    base = dict(source="hn_hiring", industry="tech_startups", state="TX", country="US", location_text="Acme | Austin, TX")
    return SimpleNamespace(**{**base, **kw})


def post(text):
    return SimpleNamespace(signal_type="hiring", text=text, matched_services=["custom_software"], strength=1.0,
                           observed_at=utcnow(), source_url="https://news.example/1")


# ------------------------------------------------------------------ the need
@pytest.mark.parametrize("text,not_found", [
    ("We are developing biological models (not LLMs) to cure disease.", "ai_agents"),
    ("Learn about our agentic coding practices at our blog.", "agent_orchestration"),
    ("We write unit and integration tests. Integration testing matters, and so does test automation.", "bpa"),
    ("Kubernetes orchestration experience is a plus.", "agent_orchestration"),
    ("Great full stack role with no bland rewrites.", "custom_software"),
])
def test_chance_words_are_not_a_need(text, not_found):
    assert not_found not in [m.service_id for m in match_services(text)]


@pytest.mark.parametrize("text,found", [
    ("We want to replace our legacy systems and build internal tools for dispatch.", "custom_software"),
    ("Scaling our ML systems and LLMs to automate operations.", "ai_agents"),
    ("We are looking to automate our invoice data entry.", "bpa"),
])
def test_a_named_need_is_still_found(text, found):
    assert found in [m.service_id for m in match_services(text)]


def test_a_stored_match_the_rules_no_longer_accept_stops_counting_without_rewriting_it():
    signal = post("Great team. Our stack: React, Python, Node, TypeScript. We are hiring engineers.")
    fact = signal_fact(signal)
    assert fact.matched_services == [] and fact.hires_builders
    assert signal.matched_services == ["custom_software"]  # the record stays as it was


def test_what_a_person_stated_stays_as_recorded():
    signal = SimpleNamespace(signal_type="inbound", text="hello", matched_services=["custom_crm"], strength=1.0,
                             observed_at=utcnow(), source_url=None)
    assert signal_fact(signal).matched_services == ["custom_crm"]


def test_hiring_builders_counts_for_less_than_a_named_need_and_names_no_service():
    builders = signal_fact(post("We are hiring engineers to work on the product."))
    named = signal_fact(post("We are hiring engineers to build AI agents with LLMs for our support team."))
    plain, need = score_lead(facts(signals=[builders])), score_lead(facts(signals=[named]))
    assert plain.matched_services == [] and need.matched_services == ["ai_agents"]
    assert plain.need < need.need


# ------------------------------------------------------------------ the industry
def test_a_defaulted_industry_earns_nothing():
    seen = read_company(company(), [post("We pick up and deliver dry cleaning to homes.")])
    assert seen["industry"] is None and not seen["running_business"]
    result = score_lead(facts(**{k: v for k, v in seen.items() if k in ("industry", "running_business")}))
    assert result.fit == 13 and not any("target industry" in r or "running business" in r for r in result.reasons)  # state + website only


def test_evidence_of_startup_or_business_earns_points():
    assert read_company(company(), [post("We raised a Series A to build our product.")])["industry"] == "tech_startups"
    seen = read_company(company(), [post("We deliver dry cleaning to paying customers in ten cities.")])
    assert seen["industry"] is None and seen["running_business"]
    assert any("running business" in r for r in score_lead(facts(industry=None, running_business=True)).reasons)


@pytest.mark.parametrize("source,industry", [("csv_import", "tech_startups"), ("inbound", "saas"), ("sec_edgar", "tech_startups")])
def test_an_industry_a_person_or_registry_gave_stands(source, industry):
    assert read_company(company(source=source, industry=industry), [])["industry"] == industry


def test_rescoring_changes_no_stored_company_or_signal(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        c = Company(name="Acme", normalized_name="acme", domain="acme.example", website="https://acme.example", industry="tech_startups",
                    state="TX", country="US", source="hn_hiring", website_status="verified", location_text="Acme | Austin, TX")
        s.add(c)
        s.flush()
        s.add(Signal(company_id=c.id, signal_type="hiring", text="We deliver dry cleaning.", source="hn_hiring", source_url="https://n.example/1",
                     fingerprint="a", observed_at=utcnow(), matched_services=["custom_software"], keywords=["react"], strength=1.0))
        s.add(Lead(company_id=c.id, source="research", status="new"))
    assert LeadQualificationAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        c = s.execute(select(Company)).scalar_one()
        sig = s.execute(select(Signal)).scalar_one()
        assert c.industry == "tech_startups" and sig.matched_services == ["custom_software"] and sig.keywords == ["react"]
        assert not any("tech_startups" in r for r in s.execute(select(Lead)).scalar_one().score_breakdown)


# ------------------------------------------------------------------ the place
@pytest.mark.parametrize("text,is_us,state", [
    ("Acme | Austin, TX | Engineer", True, "TX"), ("Acme | Remote (NY or NJ)", True, "NY"), ("Acme | TX | Engineer", True, "TX"),
    ("Acme | TX Remote", True, "TX"), ("Acme | New Orleans, LA", True, "LA"), ("Acme | LA | Engineer", None, None),
    ("Acme | Remote (UT or ID residents only)", True, "UT"), ("Acme | Remote (US, Eastern hours)", True, None),
    ("Acme (U.S. Eastern/Central Time) remote", True, None), ("Acme | Toronto | US Eastern hours", False, None),
    ("Acme | London, UK", False, None), ("Acme | Requires a BS/MS degree", None, None),
])
def test_places_old_and_new(text, is_us, state):
    place = parse_location(text)
    assert (place.is_us, place.state) == (is_us, state)


@pytest.mark.parametrize("text,first", [("Acme | Berlin, Freiburg, NYC", True), ("Acme | NYC, London", False),
                                        ("Acme | Remote (US or Canada) or San Francisco", False), ("Acme | Austin, TX", False)])
def test_a_us_office_after_places_abroad_is_only_an_office(text, first):
    assert parse_location(text).foreign_first is first


def test_a_company_outside_the_us_is_disqualified_on_its_own_words():
    seen = read_company(company(location_text="Acme | Fully REMOTE (Poland only) | US Eastern timezone overlap"), [post("Hello there.")])
    assert score_lead(facts(country=seen["country"])).disqualify_reason


# ------------------------------------------------------------------ competitors
@pytest.mark.parametrize("words", ["Agentic pricing management for B2B. Experts and AI agents do the work.", "Give AI agents instant sandboxes.",
                                   "Infrastructure for your code and agents.", "Give every team agents they can trust; every agent has an identity.",
                                   "Finds accounts and automates personalized outreach."])
def test_same_offer_vendors_are_recognised_in_their_own_words(words):
    assert competitor_evidence(words)


@pytest.mark.parametrize("words", ["We sell accounting software.", "Affordable primary care designed around you.", "Wholesale specialty insurance brokerage.",
                                   "The connected workspace where AI agents and teams organize docs.", "Dry cleaning delivered to your door."])
def test_ordinary_companies_are_not_competitors(words):
    assert not competitor_evidence(words)


def lead_with(ctx, name="Acme", description=None, post_text="We are hiring engineers.", outreach=None, cleared=None):
    with ctx.db.session() as s:
        c = Company(name=name, normalized_name=name.lower(), domain=f"{name.lower()}.example", state="TX", country="US", source="hn_hiring",
                    website_status="verified", competitor_cleared_by=cleared)
        s.add(c)
        s.flush()
        s.add(Signal(company_id=c.id, signal_type="hiring", text=post_text, source="hn_hiring", source_url="https://n.example/1", fingerprint=name,
                     observed_at=utcnow() - timedelta(days=3)))
        if description:
            s.add(ResearchSnapshot(company_id=c.id, status="done", description=description, description_url=f"https://{name.lower()}.example/"))
        lead = Lead(company_id=c.id, source="research", status="qualified", outreach_status=outreach)
        s.add(lead)
        s.flush()
        return lead.id, c.id


def flagged(ctx, cid):
    assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).ok
    with ctx.db.session() as s:
        return s.get(Company, cid).is_competitor


def test_a_company_on_file_gets_the_own_words_check_and_it_is_logged(make_ctx):
    ctx = make_ctx()
    lead_id, cid = lead_with(ctx, description="Fractional CTO and custom AI development. Book a call.")
    assert flagged(ctx, cid)
    with ctx.db.session() as s:
        assert "possible competitor" in s.execute(select(Activity).where(Activity.lead_id == lead_id)).scalars().one().subject


def test_an_unread_company_is_checked_on_its_post_but_its_own_words_decide_when_on_record(make_ctx):
    ctx = make_ctx()
    _, unread = lead_with(ctx, "Unread", post_text="We are expanding into a full agentic compliance platform.")
    _, clinic = lead_with(ctx, "Clinic", description="Affordable primary care.", post_text="We are building an agentic platform for our clinic.")
    assert flagged(ctx, unread) and not flagged(ctx, clinic)


def test_a_persons_decision_and_a_running_sequence_are_left_alone(make_ctx):
    ctx = make_ctx()
    words = "Fractional CTO and custom AI development."
    _, cleared = lead_with(ctx, "Cleared", description=words, cleared="owner")
    _, emailed = lead_with(ctx, "Emailed", description=words, outreach="contacted")
    assert not flagged(ctx, cleared) and not flagged(ctx, emailed)


# ------------------------------------------------------------------ outside help
@pytest.mark.parametrize("text", ["Contractors welcome.", "We are hiring contractors for a migration.", "Part-time contract, path to full-time",
                                  "Remote contract role.", "1099 contract, 20 hrs a week", "Contract or Full-time", "looking for a fractional CTO",
                                  "Contract-to-hire", "seeking an implementation partner"])
def test_asking_for_outside_help_still_counts(text):
    assert seeks_external_help(text)


# ------------------------------------------------------------------ the quote
@pytest.mark.parametrize("text", [
    "Acme | Engineer | $180k | VISA\n\nApply at the link.",
    "Acme (Remote US) — Full-Stack Engineer — TypeScript / Postgres / React",
    "We offer competitive salary and visa sponsorship for this great company.",
    "Tech we use: Python, React, Postgres, Docker, Terraform, and a lot more.",
    "You will own the product end to end from day one.",
    "Read more at https://acme.example/about about our company.",
    "Email our founder at jane@acme.example for details.",
])
def test_nothing_unfit_is_quoted_from_a_post(text):
    assert short_quote(text) == ""


def test_the_sentence_about_the_work_is_quoted_verbatim_and_whole():
    quote = short_quote("Acme | Remote (U.S. only)\n\nAcme serves the U.S. Postal Service and retailers with routing tools.\nYou will write code.")
    assert quote == "Acme serves the U.S. Postal Service and retailers with routing tools."


def test_a_long_sentence_is_cut_at_a_clause():
    sentence = "Acme builds scheduling tools for dental clinics, " + "and it also does a great many other things for many others " * 4 + "indeed."
    quote = short_quote(sentence)
    assert quote == "Acme builds scheduling tools for dental clinics…" or quote.endswith("…") and quote.rstrip("…") in sentence


@pytest.mark.parametrize("text", ["Jane was previously the Head of Support at another company, and led it.", "“Acme got me in touch with my customers at last.”",
                                  "Made in Notion, the connected workspace for docs and teams.", "Thank you for checking out our website today."])
def test_a_career_a_customer_or_a_button_is_not_about_the_work(text):
    assert website_quote(text) == ""


def test_a_tagline_with_no_full_stop_can_be_the_description_but_a_need_must_name_the_topic():
    assert website_quote("Scheduling tools for dental clinics everywhere")
    assert website_quote("We automate invoice data entry for clinics.", ["data entry"])
    assert not website_quote("We help dental clinics with their paperwork.", ["data entry"])


def inbox(company_record):
    return SimpleNamespace(company=company_record, role_category="inbox", first_name=None, source="website", email="hello@acme.example",
                           email_status="role_address")


def seeded(ctx, description=None, needs=None, post_text=SOFTWARE_POST):
    lead_id, cid = lead_with(ctx, description=description, post_text=post_text)
    with ctx.db.session() as s:
        s.get(Lead, lead_id).primary_service = "bpa"
        if needs:
            s.execute(select(ResearchSnapshot).where(ResearchSnapshot.company_id == cid)).scalars().one().needs = needs
    return lead_id


def test_website_evidence_comes_first_when_it_is_fit_to_quote(make_ctx):
    ctx = make_ctx()
    lead_id = seeded(ctx, "Scheduling tools for dental clinics.", [{"service": "bpa", "keywords": ["data entry"], "url": "https://acme.example/x",
                                                                     "quote": "Our tools remove manual data entry from the front desk."}])
    with ctx.db.session() as s:
        assert [e.kind for e in gather_evidence(s, s.get(Lead, lead_id))] == ["website", "description", "hiring"]


def test_a_job_post_is_quoted_only_when_nothing_on_the_website_will_do(make_ctx):
    ctx = make_ctx()
    lead_id = seeded(ctx)
    with ctx.db.session() as s:
        (ev,) = gather_evidence(s, s.get(Lead, lead_id))
        assert ev.kind == "hiring" and ev.fact == "Acme sells scheduling software to dental clinics across the country."


def test_no_fit_sentence_at_all_gives_no_evidence(make_ctx):
    ctx = make_ctx()
    lead_id = seeded(ctx, post_text="Acme | Engineer | $180k | VISA")
    with ctx.db.session() as s:
        assert gather_evidence(s, s.get(Lead, lead_id)) == []


# ------------------------------------------------------------------ the name
@pytest.mark.parametrize("name,title,domain,written", [
    ("Acme", None, None, "Acme"), ("Acme Inc.", None, None, "Acme Inc"), ("Zentro.com", None, None, "Zentro"), ("meetzentro.com", None, None, None),
    ("meetzentro.com", "Zentro - feedback in one place", "meetzentro.com", "Zentro"), ("zentro.io", "Home | Zentro", "zentro.io", "Zentro"),
    ("AI x Healthcare Startup", None, None, None), ("Looking for a commercial co-founder", None, None, None), ("Atlas Company", None, None, "Atlas Company"),
    ("A Cloud Guru", None, None, "A Cloud Guru"),
])
def test_the_name_written_to_a_company(name, title, domain, written):
    assert company_to_address(name, title, domain) == written


def test_a_title_only_counts_when_it_spells_the_domain():
    assert name_from_title("Other Brand | Great things", "meetzentro.com") is None
    assert name_from_title("Get | Great things", "get.com") == "Get"


@pytest.mark.parametrize("step", [1, 2, 3])
@pytest.mark.parametrize("name", ["meetzentro.com", "AI x Healthcare Startup"])
def test_no_name_fit_to_write_reads_as_team_and_a_person_looks(make_ctx, step, name):
    ctx = make_ctx()
    record = SimpleNamespace(name=name, website_title=None, domain="zentro.example")
    lead = SimpleNamespace(company=record, primary_service="ai_agents")
    ev = [SimpleNamespace(kind="description", fact="Scheduling tools for dental clinics.", url="https://z.example")]
    subject, body = draft_outreach(ctx.settings, step, lead, inbox(record), ev)
    assert body.startswith("Hello team,") and name not in subject and name not in body
    report = run_email_checks(subject, body, [{"fact": ev[0].fact, "source_text": ev[0].fact}], inbox(record), False, step=step)
    if step < 3:  # the closing email does not name a company
        assert next(c for c in report["checks"] if c["name"] == "company name")["status"] == "uncertain"


def test_a_long_company_name_leaves_the_subject_within_the_limit(make_ctx):
    ctx = make_ctx()
    record = SimpleNamespace(name="Very Long Company Name of Dental Scheduling Tools Holdings", website_title=None, domain=None)
    subject, body = draft_outreach(ctx.settings, 2, SimpleNamespace(company=record, primary_service="custom_crm"), inbox(record), [])
    assert len(subject) <= 70 and record.name in body
    _, again = draft_reengage(ctx.settings, SimpleNamespace(company=record, primary_service="ai_agents"), inbox(record))
    assert "if you are looking at AI agents now" in again


def test_sent_and_edited_emails_are_not_rewritten(make_ctx):
    """Wording changes reach only new drafts: scoring and research leave existing follow-ups as they stand."""
    ctx = make_ctx()
    lead_id, cid = lead_with(ctx, description="Scheduling tools for dental clinics.", outreach="contacted")
    with ctx.db.session() as s:
        contact = Contact(company_id=cid, email="hello@acme.example", source="website", role_category="inbox", email_status="role_address",
                          email_source_url="https://acme.example/contact", consent_status="none")
        s.add(contact)
        s.flush()
        s.get(Lead, lead_id).contact_id = contact.id
        s.add(FollowUp(lead_id=lead_id, contact_id=contact.id, company_id=cid, channel="email", kind="outreach", step=1, status="sent",
                       subject="Old subject", body="If AI agents becomes a priority", to_email=contact.email, sent_at=utcnow(), due_at=utcnow()))
        s.add(FollowUp(lead_id=lead_id, contact_id=contact.id, company_id=cid, channel="email", kind="outreach", step=2, status="awaiting_approval",
                       subject="A person's subject", body="A person's words", to_email=contact.email, due_at=utcnow()))
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok and CRMManagementAgent(ctx).run({"operations": ["competitors"]}).ok
    with ctx.db.session() as s:
        rows = s.execute(select(FollowUp).order_by(FollowUp.step)).scalars().all()
        assert [f.body for f in rows] == ["If AI agents becomes a priority", "A person's words"]


# ------------------------------------------------------------------ hosts with a port
@pytest.mark.parametrize("host,domain", [("www.acme.example:443", "acme.example"), ("acme.example", "acme.example"), ("user@acme.example:8080", "acme.example"),
                                         ("a.b.co.uk", "b.co.uk")])
def test_a_port_or_user_is_no_part_of_a_domain(host, domain):
    assert registrable_domain(host) == domain
