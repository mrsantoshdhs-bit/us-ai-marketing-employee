"""Phase 4, lead decisions: each test fails on release #74 and states the outcome a sales director expects. Synthetic data only."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

import aime.timeutil as tu
from aime.agents.crm import CRMManagementAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.crm_ops import upsert_company
from aime.db import kv_set
from aime.matching import competitor_evidence, competitor_in_post, match_services
from aime.models import Activity, Alert, Company, FollowUp, Lead, ResearchSnapshot, Signal, Suppression
from aime.people import decision_maker_title
from aime.scoring import LeadFacts, SignalFact, lasting_grade, read_company, score_lead
from aime.sources.base import CompanyCandidate
from aime.web.app import create_app


@pytest.mark.parametrize("own_words", [
    "GPU sandboxes for AI agents and persistent instances for developers.",
    "Finds ready-to-buy accounts, automates personalized outreach and shortens every deal.",
    "Platform for food makers. Automate sales, orders and samples.",
    "Cloud infrastructure for your code and agents.",
    "Agentic network operations",
    "The agentic system for clinical data.",
    "Agentic pricing management for B2B: two modules for quotes.",
])
def test_a_company_selling_a_product_is_not_a_competitor(own_words):
    assert competitor_evidence(own_words) is None


@pytest.mark.parametrize("post", [
    "Beyond verification we are building a full agentic compliance platform for banks.",
    "Our agentic orchestration platform makes clinical decisions frictionless.",
])
def test_a_hiring_post_about_its_own_product_is_not_a_competitor(post):
    assert competitor_in_post(post) is None


@pytest.mark.parametrize("post", [
    "We are a boutique tech agency creating on-demand teams and software solutions for long-standing clients.",
    "We're a small IT consulting firm working in federal and regulated environments.",
    "A small R&D company specializing in AI/ML and software development for defense and healthcare.",
])
def test_a_firm_selling_software_work_to_others_is_a_competitor(post):
    assert competitor_in_post(post) is not None


def test_a_consultancy_saying_so_on_its_own_site_is_a_competitor():
    assert competitor_evidence("Consulting-Led and AI-Powered Technology Services & Consulting Company") is not None


@pytest.mark.parametrize("text, service", [
    ("Our AI product helps clinics. Series A, backed by Benchmark, Anthropic and other investors.", "ai_agents"),
    ("We are an AI native team: engineers use copilot and cursor as daily tools for code review.", "ai_agents"),
    ("Experience with workflow orchestration (temporal, airflow, dagster or similar) and data pipelines.", "agent_orchestration"),
])
def test_an_investor_a_coding_tool_or_a_data_scheduler_is_no_need(text, service):
    assert service not in [m.service_id for m in match_services(text)]


def _hn_lead(ctx, days_old: int, status="nurture", competitor=False) -> tuple[int, int]:
    """A hiring-post lead scoring 57, grade B, while its post counts in full, and 54, grade C, the day after it stops."""
    post = ("Acme Tools | Software Engineer | Austin, TX. We have paying customers and want to automate our back office.")
    with ctx.db.session() as s:
        c = Company(name="Acme Tools", normalized_name="acme tools", domain="acme.example", website="https://acme.example",
                    state="TX", country="US", source="hn_hiring", website_status="verified", employee_range="11-50",
                    is_competitor=competitor, location_text="Austin, TX")
        s.add(c)
        s.flush()
        s.add(Signal(company_id=c.id, signal_type="hiring", text=post, source="hn_hiring", source_url="https://example.test/1",
                     fingerprint="p4", observed_at=tu.utcnow() - timedelta(days=days_old), created_at=tu.utcnow() - timedelta(days=days_old)))
        lead = Lead(company_id=c.id, source="research", status=status)
        s.add(lead)
        s.flush()
        return lead.id, c.id


def _status(ctx, lead_id):
    with ctx.db.session() as s:
        x = s.get(Lead, lead_id)
        return x.status, x.grade, x.score


@pytest.fixture
def clock():
    before = tu.CLOCK_OFFSET  # the session's test day: later tests run on it
    yield before
    tu.CLOCK_OFFSET = before


def test_a_lead_is_not_qualified_for_one_day_only(make_ctx, clock):
    ctx = make_ctx()
    lead_id, _ = _hn_lead(ctx, days_old=60)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    first = _status(ctx, lead_id)
    tu.CLOCK_OFFSET = clock + timedelta(days=1)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    second = _status(ctx, lead_id)
    assert first[0] == second[0], f"qualified and dropped again in 24 hours: {first} then {second}"


def test_a_fresh_post_still_takes_a_lead_in_and_someone_who_asked_us_is_not_held_back():
    now = tu.utcnow()
    base = dict(industry="saas", target_industries=["saas"], state="TX", country="US", has_website=False, employee_range=None, verified=False,
                is_competitor=False, suppressed=False, contact_consent=None, now=now)
    fresh = LeadFacts(source="research", signals=[SignalFact("hiring", ["bpa"], 1.0, now - timedelta(days=5), True)], **base)
    stale = LeadFacts(source="research", signals=[SignalFact("hiring", ["bpa"], 1.0, now - timedelta(days=60), True)], **base)
    asked = LeadFacts(source="inbound", signals=[SignalFact("inbound", ["bpa"], 1.0, now - timedelta(days=40), False)], **base)
    limits = {"grade_b": score_lead(fresh).total, "grade_a": 100}
    assert score_lead(fresh, limits).grade == lasting_grade(fresh, limits) == "B"
    limits = {"grade_b": score_lead(stale).total, "grade_a": 100}
    assert score_lead(stale, limits).grade == "B" and lasting_grade(stale, limits) == "C"
    limits = {"grade_b": score_lead(asked).total, "grade_a": 100}
    assert score_lead(asked, limits).grade == lasting_grade(asked, limits) == "B"


def test_a_persons_status_on_a_flagged_lead_stays_or_is_refused(make_ctx):
    ctx = make_ctx()
    lead_id, _ = _hn_lead(ctx, days_old=5, status="disqualified", competitor=True)
    client = TestClient(create_app(ctx), base_url="http://127.0.0.1", follow_redirects=False, headers={"origin": "http://127.0.0.1"})
    client.post(f"/leads/{lead_id}/status", data={"status": "qualified", "note": "real prospect"})
    said = _status(ctx, lead_id)[0]
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert _status(ctx, lead_id)[0] == said, "the page accepted the person's status and the next scoring undid it without a word"


def test_yesterdays_unread_moves_are_not_overwritten_by_todays(make_ctx, clock):
    ctx = make_ctx()
    a, _ = _hn_lead(ctx, days_old=5, status="new")
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    tu.CLOCK_OFFSET = clock + timedelta(days=1)
    with ctx.db.session() as s:
        c = Company(name="Other Co", normalized_name="other co", state="TX", country="ZZ", source="hn_hiring")
        s.add(c)
        s.flush()
        s.add(Lead(company_id=c.id, source="research", status="new"))
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    with ctx.db.session() as s:
        open_ones = [x.message for x in s.query(Alert).filter(Alert.acknowledged.is_(False), Alert.message.like("Scoring moved%"))]
    assert any(" to qualified" in m for m in open_ones), f"nobody read yesterday's notice and it is gone: {open_ones}"


# ------------------------------------------------------------------ stored flags, read again by today's rule
def _flagged_lead(ctx, *, own_words="AI agents that automate claims for hospitals.", post="We are hiring engineers.", actor="company_research",
                  status="disqualified", reason="Company appears to sell similar services (competitor)", outreach=None, cleared=None):
    with ctx.db.session() as s:
        c = Company(name="Flagged Co", normalized_name="flagged co", domain="flagged.example", state="TX", country="US", source="hn_hiring",
                    is_competitor=cleared is None, competitor_cleared_by=cleared, location_text="Austin, TX", website_status="verified")
        s.add(c)
        s.flush()
        s.add(ResearchSnapshot(company_id=c.id, status="done", description=own_words, description_url="https://flagged.example/"))
        s.add(Signal(company_id=c.id, signal_type="hiring", text=post, source="hn_hiring", source_url="https://example.test/2", fingerprint="f",
                     observed_at=tu.utcnow() - timedelta(days=5)))
        lead = Lead(company_id=c.id, source="research", status=status, disqualify_reason=reason, outreach_status=outreach)
        s.add(lead)
        s.flush()
        s.add(Activity(subject='Marked as a possible competitor: its website says "AI agents that"', actor=actor, type="data_quality",
                       company_id=c.id, lead_id=lead.id))
        s.add(Activity(subject="Lead qualified → disqualified (score 60, grade B)", actor="qualification", type="status_change",
                       company_id=c.id, lead_id=lead.id))
        return lead.id, c.id


def _review(ctx):
    assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).ok


def _state(ctx, lead_id, company_id):
    with ctx.db.session() as s:
        lead, c = s.get(Lead, lead_id), s.get(Company, company_id)
        return c.is_competitor, lead.status, lead.disqualify_reason


def test_a_flag_aime_set_on_old_wording_goes_and_the_lead_returns_logged(make_ctx):
    ctx = make_ctx()
    lead_id, cid = _flagged_lead(ctx)
    _review(ctx)
    assert _state(ctx, lead_id, cid) == (False, "new", None)
    with ctx.db.session() as s:
        said = [a.subject for a in s.query(Activity).filter(Activity.company_id == cid, Activity.actor == "crm")]
        assert any("flag removed" in x for x in said) and any("Lead disqualified → new" in x for x in said)
        assert not s.query(FollowUp).count()  # nothing drafted, approved or sent by this step
    _review(ctx)  # and it is not set again
    assert _state(ctx, lead_id, cid)[0] is False


def test_a_flag_still_shown_by_todays_wording_stays(make_ctx):
    ctx = make_ctx()
    a, ca = _flagged_lead(ctx, own_words="Source a custom AI agent for your business.")
    _review(ctx)
    assert _state(ctx, a, ca)[:2] == (True, "disqualified")


def test_a_flag_shown_only_by_its_post_stays(make_ctx):
    ctx = make_ctx()
    a, ca = _flagged_lead(ctx, post="We are a software development company for hire.")
    _review(ctx)
    assert _state(ctx, a, ca)[:2] == (True, "disqualified")


def test_a_flag_a_person_set_is_never_cleared(make_ctx):
    ctx = make_ctx()
    a, ca = _flagged_lead(ctx, actor="owner")
    _review(ctx)
    assert _state(ctx, a, ca)[:2] == (True, "disqualified")


def test_a_flag_with_no_record_of_who_set_it_stays(make_ctx):
    ctx = make_ctx()
    a, ca = _flagged_lead(ctx)
    with ctx.db.session() as s:
        s.query(Activity).filter(Activity.subject.like("Marked%")).delete()
    _review(ctx)
    assert _state(ctx, a, ca)[:2] == (True, "disqualified")


def test_a_flag_a_person_cleared_is_never_set_again(make_ctx):
    ctx = make_ctx()
    a, ca = _flagged_lead(ctx, own_words="Fractional CTO and custom AI development.", status="qualified", reason=None, cleared="owner")
    _review(ctx)
    assert _state(ctx, a, ca)[:2] == (False, "qualified")


def test_a_lead_a_person_closed_stays_closed_when_the_flag_goes(make_ctx):
    ctx = make_ctx()
    a, ca = _flagged_lead(ctx)
    with ctx.db.session() as s:
        s.add(Activity(subject="Status disqualified → disqualified (manual)", actor="owner", type="status_change", company_id=ca, lead_id=a))
    _review(ctx)
    assert _state(ctx, a, ca)[:2] == (False, "disqualified")


def test_an_opted_out_or_already_emailed_lead_does_not_reopen_when_the_flag_goes(make_ctx):
    ctx = make_ctx()
    a, ca = _flagged_lead(ctx, outreach="stopped")
    _review(ctx)
    assert _state(ctx, a, ca)[:2] == (False, "disqualified") and "person decides" in _state(ctx, a, ca)[2]
    ctx2 = make_ctx()
    a, ca = _flagged_lead(ctx2)
    with ctx2.db.session() as s:
        s.add(Suppression(value="flagged.example", kind="domain", reason="opt-out"))
    _review(ctx2)
    assert _state(ctx2, a, ca)[:2] == (False, "disqualified") and "suppression" in _state(ctx2, a, ca)[2]


def test_a_lead_that_returns_gets_at_most_a_draft_and_the_owner_is_told_which(make_ctx):
    ctx = make_ctx()
    lead_id, cid = _flagged_lead(ctx)
    _review(ctx)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    with ctx.db.session() as s:
        assert s.query(FollowUp).filter(FollowUp.status.in_(("approved", "sending", "sent"))).count() == 0
        (alert,) = s.query(Alert).filter(Alert.key == "leads:moved").all()
        assert alert.level == "warning" and f"#{lead_id}" in alert.message


# ------------------------------------------------------------------ the owner is told
def test_the_notice_is_a_warning_that_names_a_capped_list_of_leads(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        LeadQualificationAgent(ctx)._say_moved(s, [("nurture", i) for i in range(1, 101)])
    with ctx.db.session() as s:
        (alert,) = s.query(Alert).filter(Alert.key == "leads:moved").all()
        assert alert.level == "warning" and "#1," in alert.message and "#30" in alert.message and "#31" not in alert.message
        assert "and 70 more" in alert.message and alert.message.endswith("cleared there.")


def test_the_notice_reaches_the_bell_and_the_home_page(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        LeadQualificationAgent(ctx)._say_moved(s, [("nurture", 77)])
    page = TestClient(create_app(ctx), base_url="http://127.0.0.1").get("/command-center").text
    assert "Scoring moved 1 lead(s)" in page and '<span class="badge">1</span>' in page


def test_the_disqualified_list_shows_the_reason(make_ctx):
    ctx = make_ctx()
    _flagged_lead(ctx)
    page = TestClient(create_app(ctx), base_url="http://127.0.0.1").get("/leads?status=disqualified").text
    assert "sell similar services" in page


# ------------------------------------------------------------------ a status the next run would undo
def _set_status(ctx, lead_id, status):
    client = TestClient(create_app(ctx), base_url="http://127.0.0.1", follow_redirects=False, headers={"origin": "http://127.0.0.1"})
    return unquote(client.post(f"/leads/{lead_id}/status", data={"status": status}).headers["location"])


def test_the_page_says_why_a_flagged_lead_cannot_be_reopened_and_points_to_the_button(make_ctx):
    ctx = make_ctx()
    lead_id, _ = _hn_lead(ctx, days_old=5, status="disqualified", competitor=True)
    where = _set_status(ctx, lead_id, "nurture")
    assert "Status not changed" in where and "Not a competitor" in where
    assert _status(ctx, lead_id)[0] == "disqualified"


def test_the_page_says_plainly_why_an_outside_us_lead_cannot_be_reopened(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        c = Company(name="Far Co", normalized_name="far co", country="US", source="hn_hiring", location_text="Far Co | Warsaw, Poland")
        s.add(c)
        s.flush()
        lead = Lead(company_id=c.id, source="research", status="disqualified", disqualify_reason="Outside US target market")
        s.add(lead)
        s.flush()
        lead_id = lead.id
    where = _set_status(ctx, lead_id, "qualified")
    assert "Status not changed" in where and "outside the US" in where and "cannot be changed" in where and "administrator" not in where
    assert _status(ctx, lead_id)[0] == "disqualified"
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert _status(ctx, lead_id)[0] == "disqualified"


def test_a_persons_status_on_an_ordinary_lead_is_still_accepted_and_clears_the_old_reason(make_ctx):
    ctx = make_ctx()
    lead_id, cid = _hn_lead(ctx, days_old=5, status="disqualified")
    with ctx.db.session() as s:
        s.get(Lead, lead_id).disqualify_reason = "Company appears to sell similar services (competitor)"
    assert "Status updated" in _set_status(ctx, lead_id, "nurture")
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        assert (lead.status, lead.disqualify_reason) == ("nurture", None)


# ------------------------------------------------------------------ false points
@pytest.mark.parametrize("text, service", [
    ("We build AI agents with LangGraph for claims handling and need a RAG pipeline.", "ai_agents"),
    ("Our product calls models from OpenAI and Anthropic through their APIs.", "ai_agents"),
    ("Agent orchestration across multi-agent planners, with tool use.", "agent_orchestration"),
    ("Replace our Salesforce CRM and clean the sales pipeline.", "custom_crm"),
])
def test_a_real_need_is_kept(text, service):
    assert service in [m.service_id for m in match_services(text)]


@pytest.mark.parametrize("post", [
    "Prior experience at a software consulting firm is a plus.",
    "We are not a software consulting firm: we sell our own product.",
    "AI and software development for our own platform.",
    "We are not an outsourcing company.",
])
def test_a_post_that_only_mentions_such_firms_is_no_competitor(post):
    assert competitor_in_post(post) is None


def test_a_label_a_post_does_not_support_earns_no_industry_points():
    company = SimpleNamespace(source="hn_hiring", industry="marketing_agencies", state="CA", country="US", location_text="Lab | San Francisco, CA",
                              verified=False)
    post = SimpleNamespace(signal_type="hiring", text="We care a lot about high agency and ownership.")
    assert read_company(company, [post])["industry"] is None
    company.industry = "saas"
    post.text = "We're not a SaaS company, and we make medicines."
    assert read_company(company, [post])["industry"] != "saas"
    company.verified = True  # a registry gave it
    assert read_company(company, [post])["industry"] == "saas"


@pytest.mark.parametrize("place, stated", [("Co | Remote (Global / US/EU/Africa time zones welcome)", False), ("Co | Remote", False),
                                           ("Co | Remote (US)", True), ("Co | US only", True), ("Co | Remote (anywhere in the US)", True), ("Co Global | Remote (US)", True)])
def test_us_points_come_only_from_what_the_post_says(place, stated):
    company = SimpleNamespace(source="hn_hiring", industry=None, state=None, country="US", location_text=place, verified=False)
    assert read_company(company, [])["us_stated"] is stated
    reasons = score_lead(LeadFacts(source="research", industry=None, target_industries=[], state=None, country="US", has_website=False,
                                   employee_range=None, verified=False, is_competitor=False, suppressed=False, contact_consent=None,
                                   us_stated=stated)).reasons
    assert any("US presence stated" in r for r in reasons) is stated


@pytest.mark.parametrize("title, decides", [("Pet Owner Support", False), ("Clinics, Start HerePet Owner Support", False), ("Former CEO", False),
                                            ("We believe that each team member is an owner.", False), ("COO of a $6M family-owned HVAC business.", False),
                                            ("Founder and CEO", True), ("Owner", True), ("Owner and CEO", True), ("Co-Founder, Owner", True), ("CTO", True),
                                            ("Owner CEO", True), ("President, Smith & Co.", True), ("Founder & CEO, Acme Inc.", True),
                                            ("Chairman of the Board, President and Chief Executive Officer of Acme Holdings, Inc.", True),
                                            ("Product Owner", False), ("Process Owner", False), ("Product Owner & Founder", True)])
def test_a_decision_maker_is_someone_whose_title_says_so(title, decides):
    assert decision_maker_title(title) is decides


def test_reasons_read_in_plain_words():
    reasons = score_lead(LeadFacts(source="research", industry="saas", target_industries=["saas"], state="TX", country="US", has_website=False,
                                   employee_range=None, verified=False, is_competitor=False, suppressed=False, contact_consent=None,
                                   explicit_services=[], signals=[SignalFact("hiring", ["agent_orchestration"], 1.0, tu.utcnow(), True)])).reasons
    text = " ".join(reasons)
    assert "AI Agent Orchestration (strong)" in text and "agent_orchestration" not in text and "strength" not in text
    assert "target industry (SaaS)" in text


# ------------------------------------------------------------------ review of this branch, 31 Oct
def test_a_new_post_on_a_company_on_file_never_sets_the_flag_by_itself(make_ctx):
    ctx = make_ctx()
    lead_id, _ = _hn_lead(ctx, days_old=5, status="qualified")
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        lead.outreach_status = "contacted"
        cand = CompanyCandidate(name="Acme Tools", source="hn_hiring", source_url="https://example.test/9", domain="acme.example", state="TX",
                                is_us=True, signal_type="hiring", signal_text="We are a small IT consulting firm.")
        existing, created = upsert_company(s, cand)
        assert not created and not existing.is_competitor
    _review(ctx)
    assert _state(ctx, lead_id, _company_of(ctx, lead_id))[:2] == (False, "qualified")  # already emailed: left alone


def _company_of(ctx, lead_id):
    with ctx.db.session() as s:
        return s.get(Lead, lead_id).company_id


def test_a_new_company_is_still_flagged_from_its_first_post(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        cand = CompanyCandidate(name="Firm Co", source="hn_hiring", source_url="https://example.test/8", domain="firm.example", state="TX",
                                is_us=True, signal_type="hiring", signal_text="We are a small IT consulting firm.")
        company, created = upsert_company(s, cand)
        assert created and company.is_competitor


def test_the_morning_check_flags_a_company_on_file_and_logs_the_phrase(make_ctx):
    ctx = make_ctx()
    lead_id, cid = _hn_lead(ctx, days_old=5, status="qualified")
    with ctx.db.session() as s:
        s.add(Signal(company_id=cid, signal_type="hiring", text="We are a small IT consulting firm.", source="hn_hiring",
                     source_url="https://example.test/7", fingerprint="p4b", observed_at=tu.utcnow()))
    _review(ctx)
    with ctx.db.session() as s:
        assert s.get(Company, cid).is_competitor
        assert s.query(Activity).filter(Activity.subject.like("Marked as a possible competitor%IT consulting firm%")).count() == 1


@pytest.mark.parametrize("post", [
    "Experience in an IT consulting firm is a plus.",
    "Prior work with a software consulting company helps.",
    "Our customer, a leading IT consulting firm, runs on our product.",
    "Our customer is a leading IT consulting firm.",
    "We sell to a technology consulting company.",
    "No agencies, no outstaffing please.",
    "We aren't an outsourcing company.",
    "We replaced an outsourcing firm last year.",
    "10 years of experience in IT services and consulting.",
    "You will lead AI and software development for autonomous drones.",
])
def test_a_post_naming_such_a_firm_is_not_the_firm(post):
    assert competitor_in_post(post) is None


@pytest.mark.parametrize("post", [
    "We are a people-first, boutique tech agency creating teams for clients.",
    "We’re a small IT consulting firm in regulated environments.",
    "Acme is a boutique outsourcing company based abroad.",
    "We are an outstaffing company.",
    "Acme is a small R&D company specializing in AI/ML and software development for defense and healthcare.",
])
def test_a_post_saying_it_is_such_a_firm_still_counts(post):
    assert competitor_in_post(post) is not None


def test_engineering_services_of_another_trade_are_no_competitor():
    assert competitor_evidence("An environmental services and consulting company.") is None


def test_a_flag_shown_only_by_the_homepage_title_stays(make_ctx):
    ctx = make_ctx()
    a, ca = _flagged_lead(ctx, own_words="We help teams.")
    with ctx.db.session() as s:
        snap = s.query(ResearchSnapshot).filter(ResearchSnapshot.company_id == ca).one()
        snap.pages = [{"url": "https://flagged.example/", "kind": "home", "status": 200, "title": "Flagged | Custom AI development"}]
    _review(ctx)
    assert _state(ctx, a, ca)[:2] == (True, "disqualified")


@pytest.mark.parametrize("own_words", [
    "The leading AI automation agency.", "We are an AI consultancy.", "An AI development company.", "AI development services for banks.",
    "An AI agent development company.", "A software consultancy in Boston.", "Custom software development for hospitals and clinics.",
    "IT consulting services.", "AI consulting services for retailers.", "IT Services and IT Consulting",
])
def test_plain_firm_descriptions_on_its_own_site_count(own_words):
    assert competitor_evidence(own_words) is not None


@pytest.mark.parametrize("post", ["We are hiring for AI consulting services work.", "Custom software development for hospitals is what our client wanted."])
def test_those_descriptions_do_not_decide_from_a_post(post):
    assert competitor_in_post(post) is None


def test_we_build_ai_agents_for_someone_stays_unflagged():
    """Decided 31 Oct: alone it could be a product or a service, and a person approves every email."""
    assert competitor_evidence("We build AI agents for insurers.") is None
    assert competitor_in_post("We build AI agents for insurers.") is None


def test_a_persons_note_keeps_the_flag(make_ctx):
    ctx = make_ctx()
    a, ca = _flagged_lead(ctx)
    with ctx.db.session() as s:
        s.add(Activity(subject="Note", actor="owner", type="note", body="Confirmed by phone, do not reopen", company_id=ca, lead_id=a))
    _review(ctx)
    assert _state(ctx, a, ca)[:2] == (True, "disqualified")


def test_a_refused_status_keeps_the_note_the_person_typed(make_ctx):
    ctx = make_ctx()
    lead_id, cid = _hn_lead(ctx, days_old=5, status="disqualified", competitor=True)
    client = TestClient(create_app(ctx), base_url="http://127.0.0.1", follow_redirects=False, headers={"origin": "http://127.0.0.1"})
    where = unquote(client.post(f"/leads/{lead_id}/status", data={"status": "qualified", "note": "They asked us to call"}).headers["location"])
    assert "Your note was saved" in where
    with ctx.db.session() as s:
        assert s.query(Activity).filter(Activity.type == "note", Activity.lead_id == lead_id, Activity.body == "They asked us to call").count() == 1
    assert _status(ctx, lead_id)[0] == "disqualified"


def test_the_suppression_refusal_promises_nothing_that_does_not_exist(make_ctx):
    ctx = make_ctx()
    lead_id, cid = _hn_lead(ctx, days_old=5, status="disqualified")
    with ctx.db.session() as s:
        s.add(Suppression(value="acme.example", kind="domain", reason="opt-out"))
    where = _set_status(ctx, lead_id, "qualified")
    assert "suppression list" in where and "no way to take" in where and "administrator" not in where


@pytest.mark.parametrize("text, services", [
    ("Backed by Benchmark, we help clinics with AI agents built on Anthropic.", ["ai_agents"]),
    ("We raised $5M from Benchmark to replace our CRM with OpenAI tooling.", ["custom_crm", "ai_agents"]),
    ("Funded by NIH to build a RAG chatbot using Claude.", ["ai_agents"]),
    ("LLM workflow orchestration is the core of the job.", ["agent_orchestration"]),
    ("Workflow orchestration of our agents.", ["agent_orchestration"]),
    ("Pipelines and orchestration of AI agents.", ["agent_orchestration"]),
])
def test_real_need_beside_an_investor_or_a_workflow_is_kept(text, services):
    found = [m.service_id for m in match_services(text)]
    assert all(x in found for x in services), found


@pytest.mark.parametrize("text", ["Backed by OpenAI and Anthropic, we make shoes.", "We are ex-Anthropic, METR, OpenAI people making shoes.",
                                  "Investors include Benchmark, Anthropic and others."])
def test_a_vendor_named_only_as_investor_or_past_employer_is_no_need(text):
    assert "ai_agents" not in [m.service_id for m in match_services(text)]


def test_a_stale_parked_lead_returns_parked_and_its_clock_restarts(make_ctx):
    ctx = make_ctx()
    a, ca = _flagged_lead(ctx)
    with ctx.db.session() as s:
        s.query(Activity).filter(Activity.type == "status_change").delete()
        t0 = tu.utcnow() - timedelta(days=9)
        s.add(Activity(subject="No activity for 30+ days: moved to nurture", actor="crm", type="status_change", company_id=ca, lead_id=a, created_at=t0))
        s.add(Activity(subject="Lead nurture → disqualified (score 60, grade B)", actor="qualification", type="status_change", company_id=ca, lead_id=a,
                       created_at=t0 + timedelta(days=1)))
        s.get(Lead, a).last_activity_at = t0
    _review(ctx)
    assert _state(ctx, a, ca) == (False, "nurture", None)
    with ctx.db.session() as s:
        assert s.get(Lead, a).last_activity_at > tu.utcnow() - timedelta(minutes=5)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert _status(ctx, a)[0] == "nurture"


def test_a_lead_that_stays_closed_when_the_flag_goes_is_named_to_someone(make_ctx):
    ctx = make_ctx()
    a, ca = _flagged_lead(ctx, outreach="stopped")
    _review(ctx)
    with ctx.db.session() as s:
        (alert,) = s.query(Alert).filter(Alert.key == f"lead_closed:{a}").all()
        assert alert.level == "warning" and f"#{a}" in alert.message
    assert _state(ctx, a, ca)[1] == "disqualified"


def test_the_notice_counts_leads_not_moves_and_an_old_count_is_not_added_to_the_total(make_ctx):
    ctx = make_ctx()
    agent = LeadQualificationAgent(ctx)
    with ctx.db.session() as s:
        agent._say_moved(s, [("nurture", 1), ("qualified", 1), ("nurture", 2), ("nurture", 2)])
    with ctx.db.session() as s:
        (alert,) = s.query(Alert).filter(Alert.key == "leads:moved").all()
        assert "moved 2 lead(s)" in alert.message and "2 to nurture, 1 to qualified" in alert.message
        s.delete(alert)
    with ctx.db.session() as s:
        s.add(Alert(level="info", source="qualification", key="leads:moved", message="Scoring moved 4 lead(s) today: 4 to nurture."))
        kv_set(s, "qualification.moved_today", {"day": "2026-10-31", "moved": {"nurture": 4}})
    with ctx.db.session() as s:
        agent._say_moved(s, [("nurture", 7)])
    with ctx.db.session() as s:
        (alert,) = s.query(Alert).filter(Alert.key == "leads:moved").all()
        assert "moved 1 lead(s)" in alert.message and "4 earlier move(s)" in alert.message and alert.level == "warning"


def test_setting_new_by_hand_says_the_next_run_will_move_it(make_ctx):
    ctx = make_ctx()
    lead_id, _ = _hn_lead(ctx, days_old=5, status="nurture")
    assert "next scoring run" in _set_status(ctx, lead_id, "new")
    assert _status(ctx, lead_id)[0] == "new"


def test_not_moved_is_not_written_on_a_sequence_that_was_put_back(make_ctx):
    ctx = make_ctx()
    lead_id, cid = _hn_lead(ctx, days_old=60, status="nurture")
    with ctx.db.session() as s:
        s.get(Lead, lead_id).outreach_status = "contacted"
        s.add(Activity(subject="Lead qualified → nurture (score 54, grade C)", actor="qualification", type="status_change", company_id=cid, lead_id=lead_id))
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        assert lead.status == "qualified" and not any("Not moved" in r for r in lead.score_breakdown)
