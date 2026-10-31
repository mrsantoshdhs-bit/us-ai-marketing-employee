"""Phase 2, area "quality": is what the agents produce right and useful? Written after reading production's real
records (see report_quality.md); everything here is synthetic, so it runs in CI with no production copy.

test_defect_* : asserts the correct behaviour and FAILS on the current code; each one is a reported defect.
test_ok_*     : important behaviour that was checked on the production copy and holds.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from aime.agents import (AnalyticsReportingAgent, AuthorizedPublishingAgent, LeadQualificationAgent,
                         SalesWorkflowAgent)
from aime.calendar_slots import candidate_slots, free_slots
from aime.crm_ops import log_activity
from aime.dedupe import is_job_board
from aime.integrations.email_smtp import compliance_footer
from aime.knowledge.geo import parse_location
from aime.matching import competitor_evidence, match_services, seeks_external_help
from aime.models import Company, Contact, ContentItem, FollowUp, Lead, Signal
from aime.outreach import draft_outreach, draft_reengage, short_quote
from aime.people import is_title, looks_like_name, relevance, role_category
from aime.qc.checks import check_claims
from aime.qc.email_checks import run_email_checks
from aime.reply_intel import QUESTION_PLACEHOLDER, suggest
from aime.site_reader import SiteReader
from aime.sources.base import CompanyCandidate
from aime.sources.hn_hiring import parse_post
from aime.timeutil import utcnow
from aime.validation import validate_candidate
from tests.test_publishing import AUTH, add_token, linkedin_api


# --------------------------------------------------------------------------- synthetic records

def add_lead(ctx, *, status="qualified", outreach=None, age_days=5, services=("ai_agents",), email="hello@zentro.example",
             name="Zentro") -> int:
    """A researched lead with a company inbox and one hiring post `age_days` old."""
    with ctx.db.session() as s:
        company = Company(name=name, normalized_name=name.lower(), domain="zentro.example", website="https://zentro.example",
                          industry="saas", state="NY", country="US", source="hn_hiring", website_status="verified")
        s.add(company)
        s.flush()
        contact = None
        if email:
            contact = Contact(company_id=company.id, email=email, title="Company inbox", source="website",
                              role_category="inbox", email_status="role_address", consent_status="none",
                              email_source_url="https://zentro.example/contact")
            s.add(contact)
            s.flush()
        s.add(Signal(company_id=company.id, signal_type="hiring", text="Zentro is hiring. We use an LLM for support.",
                     source="hn_hiring", source_url="https://news.ycombinator.com/item?id=1", fingerprint=f"f{company.id}",
                     observed_at=utcnow() - timedelta(days=age_days), matched_services=list(services),
                     keywords=["llm"], strength=1.0))
        lead = Lead(company_id=company.id, contact_id=contact.id if contact else None, source="research", status=status,
                    score=56, grade="B", primary_service=services[0], outreach_status=outreach)
        s.add(lead)
        s.flush()
        return lead.id


def first_email_sent(ctx, lead_id: int, days_ago: float = 5) -> None:
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(FollowUp(lead_id=lead_id, contact_id=lead.contact_id, company_id=lead.company_id, channel="email",
                       kind="outreach", step=1, status="sent", subject="Question", body="Hello",
                       to_email=lead.contact.email, message_id="<first@us.example>", evidence=[],
                       sent_at=utcnow() - timedelta(days=days_ago), due_at=utcnow() - timedelta(days=days_ago)))


def fake_lead(name="Zentro", service="ai_agents"):
    return SimpleNamespace(company=SimpleNamespace(name=name), primary_service=service)


INBOX = SimpleNamespace(role_category="inbox", first_name=None, source="website", email="hello@zentro.example",
                        email_status="role_address")


def check(report: dict, name: str) -> str:
    return next(c["status"] for c in report["checks"] if c["name"] == name)


# --------------------------------------------------------------------------- the sequence and the score over time

def test_defect_contacted_lead_keeps_its_sequence_when_the_hiring_post_ages(make_ctx):
    """Production: leads 219, 232 and 239 got their first email on 30 Oct. Their posts pass 60 days on 1 Nov, the score
    falls below 55, the lead goes to nurture, and follow-up 2 (due 3 Nov) is never drafted. No person is told."""
    ctx = make_ctx()
    lead_id = add_lead(ctx, outreach="contacted", age_days=61)
    first_email_sent(ctx, lead_id)
    assert LeadQualificationAgent(ctx).run({}).ok
    assert SalesWorkflowAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        steps = s.execute(select(FollowUp.step).where(FollowUp.lead_id == lead_id, FollowUp.channel == "email")).scalars().all()
    assert 2 in steps, "the second email of a running sequence was not drafted: the score decay ended it silently"


def test_defect_lead_a_person_worked_on_is_not_downgraded_by_the_score(make_ctx):
    """_human_touched looks for actor "human", but people are recorded by username ("admin"), so it is never true."""
    ctx = make_ctx()
    lead_id = add_lead(ctx, age_days=61)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        log_activity(s, "Follow-up #1 approve", "admin", type_="followup", company_id=lead.company_id, lead_id=lead_id)
    assert LeadQualificationAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).status == "qualified"


def test_defect_lead_closed_as_no_response_is_not_requalified_the_next_day(make_ctx):
    """pipeline_checks parks it without a status record, so _may_requalify sees nothing and the lead flips back to
    qualified on the next score (lead 3 in the 75-day run, 18 to 19 Nov), inflating "qualified" with a closed lead."""
    ctx = make_ctx()
    lead_id = add_lead(ctx, status="nurture", outreach="no_response", services=("ai_agents", "bpa", "custom_software"))
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        log_activity(s, "Lead new → qualified (score 60, grade B)", "qualification", type_="status_change",
                     company_id=lead.company_id, lead_id=lead_id)
    assert LeadQualificationAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).status == "nurture"


# --------------------------------------------------------------------------- what the prospect reads

HEADER_POST = ("Zentro | Senior Software Engineer, Backend | $180-220k | Hybrid NYC | Series A | VISA\n\n"
               "Zentro builds reporting tools for retailers. Our backend is Python.")


def test_defect_quote_is_not_the_job_post_header():
    """All 5 emails sent on 30 Oct, and 38 of 56 rendered first emails, quote the post's pipe-separated header."""
    quote = short_quote(HEADER_POST, ["backend"])
    assert "|" not in quote and "$" not in quote, quote


def test_defect_quote_is_not_cut_at_an_abbreviation():
    """Lead 197 would read: mentions "Eastern/Central Time) | Senior Full Stack Python Developer | U.S." """
    quote = short_quote("Zentro | Remote (U.S. Eastern Time) | Senior Python Developer | U.S. Citizenship Required\n\n"
                        "Zentro builds secure applications.", ["python"])
    assert not quote.endswith("U.S.") and not quote.startswith("Eastern"), quote


@pytest.mark.parametrize("service,wrong", [("ai_agents", "AI agents becomes"), ("custom_software", "internal tools becomes"),
                                           ("sales_automation", "sales follow-ups becomes")])
def test_defect_closing_email_grammar_with_a_plural_topic(make_ctx, service, wrong):
    """40 of 56 rendered closing emails, and all 5 live leads: "If AI agents becomes a priority"."""
    ctx = make_ctx()
    _, body = draft_outreach(ctx.settings, 3, fake_lead(service=service), INBOX, [])
    assert wrong not in body, body


def test_defect_reengage_email_grammar_with_a_plural_topic(make_ctx):
    ctx = make_ctx()
    _, body = draft_reengage(ctx.settings, fake_lead(), INBOX)
    assert "if AI agents is on your list" not in body, body


def test_defect_greeting_does_not_use_a_bare_web_address_as_the_name(make_ctx):
    """Follow-up 38 really went out as "Hello <domain>.com team," and "Question about internal tools at <domain>.com"."""
    ctx = make_ctx()
    ev = [SimpleNamespace(kind="description", fact="Zentro builds reporting tools for retailers.", url="https://z.example")]
    subject, body = draft_outreach(ctx.settings, 1, fake_lead(name="meetzentro.com"), INBOX, ev)
    assert "meetzentro.com" not in body.split("\n", 1)[0] and "meetzentro.com" not in subject


def test_defect_company_in_capitals_is_not_failed_as_shouting():
    """Its own name in capitals: 4 of 56 rendered first emails fail the subject check for this alone."""
    company = SimpleNamespace(name="ZENTRO")
    contact = SimpleNamespace(company=company, email="hello@zentro.example", email_status="role_address")
    body = ("Hello ZENTRO team,\n\n" + "This is a short and plain note about the way your team works today. " * 6
            + "\n---\nreply unsubscribe")
    report = run_email_checks("Question about AI agents at ZENTRO", body, [], contact, False, step=2)
    assert check(report, "subject line") == "pass"


def test_defect_footer_does_not_repeat_the_company_name(make_ctx):
    """Sent emails end "Digital Hub Solution · Digital Hub Solution, 610 E. ...": the postal address starts with the name."""
    ctx = make_ctx(company_postal_address="Digital Hub Solution, 1 Test Road, Phoenix, AZ 85001, USA")
    footer = compliance_footer(ctx.settings, outreach=True)
    assert footer.count(ctx.settings.company_name) == 1, footer


def test_defect_reply_greeting_does_not_use_a_mailbox_name_as_a_first_name(make_ctx):
    """Reproduced in this test only: production has had no replies yet. A role inbox answers under its display name."""
    ctx = make_ctx()
    row = SimpleNamespace(from_name="Sales Team", subject="Question", new_text="Tell me more", body_text="", intent=None)
    _, body = suggest(ctx.settings, "objection", row, None)
    assert not body.startswith("Hi Sales,"), body


def test_ok_price_question_never_gets_a_price(make_ctx):
    """Checked on the production copy for the 5 live leads: a placeholder, which a send refuses, and no figure."""
    ctx = make_ctx()
    row = SimpleNamespace(from_name="Dana Roe", subject="Question", new_text="What does it cost?", body_text="",
                          intent="pricing_request")
    _, body = suggest(ctx.settings, "question", row, None)
    assert QUESTION_PLACEHOLDER in body and "$" not in body.split("\n---\n")[0]


# --------------------------------------------------------------------------- qualification rules

@pytest.mark.parametrize("own_words", [
    "Fractional CTO services and custom AI development for growing startups.",
    "We are an applied-AI advisory firm for large enterprises.",
    "An AI transformation firm building working AI systems for mid-market clients.",
])
def test_defect_competitor_selling_ai_services_is_recognised(own_words):
    """Leads 155 and 287 sell what Digital Hub Solution sells; both are qualified and 287 has a pending task."""
    assert competitor_evidence(own_words)


@pytest.mark.parametrize("text", [
    "We help contractors log their jobs and send invoices.",
    "Small contractors lose their evenings to paperwork.",
    "Estimating software for commercial electrical contractors.",
    "Zentro is a defense prime contractor building a radio system.",
    "You will lead two teams (employees + contractors).",
])
def test_defect_customers_called_contractors_is_not_asking_for_contractors(text):
    """+3 intent. 5 of the 9 real posts this rule matches, and both of the qualified ones, do not ask for outside help."""
    assert not seeks_external_help(text)


@pytest.mark.parametrize("text", [
    "Senior Full-Stack Engineer. Our stack: React, TypeScript, Python, Node.",
    "Inspired by the legacy of her grandfather, she changed the company.",
])
def test_defect_tech_stack_or_chance_word_is_not_a_need_for_custom_software(text):
    """19 of 43 qualified leads have custom_software as best service, mostly from words like these."""
    assert "custom_software" not in [m.service_id for m in match_services(text)]


def test_defect_hiring_post_with_no_industry_evidence_gets_no_target_industry_points():
    """Every post without a keyword becomes tech_startups (+15 fit): a laundry service, a cosmetics group and an art
    studio among them. 210 of 297 leads; none of the 31 qualified ones would reach 55 without those 15 points."""
    cand = parse_post({"id": 1, "author": "x", "created_at": "2026-10-01T00:00:00Z",
                       "text": "Zentro Cleaners | Seattle, WA | Engineer<p>We pick up and deliver dry cleaning to homes.</p>"})
    assert cand is not None and cand.industry != "tech_startups"


@pytest.mark.parametrize("header,check_it", [
    ("Zentro | Engineer | DE Frankfurt a.M., ONSITE | German language skills required", lambda loc: loc.is_us is not True),
    ("Zentro | Fully REMOTE (Poland or Romanian residents only) | US Eastern timezone overlap", lambda loc: loc.is_us is not True),
    ("Zentro | Engineer | REMOTE (US ONLY, LA/SF preferred) | Full-time", lambda loc: loc.state != "LA"),
])
def test_defect_location_is_read_as_written(header, check_it):
    """Lead 95 (Frankfurt) is "Delaware" at score 54 with a published inbox, one point from a first email; lead 220 is
    open to Poland and Romania only; lead 213 "LA" (Los Angeles) became Louisiana."""
    assert check_it(parse_location(header))


@pytest.mark.parametrize("name", ["Looking for a commercial co-founder", "Early-stage Startup", "SaaS Startup", "NYC",
                                  "AI x Healthcare Startup"])
def test_defect_description_is_not_accepted_as_a_company_name(name):
    """7 of 242 Hacker News companies; "Hello AI x Healthcare Startup team," would be the greeting for lead 120."""
    cand = CompanyCandidate(name=name, source="hn_hiring", source_url="https://news.ycombinator.com/item?id=1",
                            signal_type="hiring", signal_text="x" * 60)
    assert validate_candidate(cand)[0]


@pytest.mark.parametrize("domain", ["applytojob.com", "curriculo.me"])
def test_defect_hiring_platform_is_not_the_company_website(domain):
    assert is_job_board(domain)


# --------------------------------------------------------------------------- contacts

@pytest.mark.parametrize("text", ["HomeAbout UsCareersTestimonialsBlogContact", "Founding Engineer"])
def test_defect_menu_text_or_a_job_is_not_a_person(text):
    """Lead 99's primary contact is its site menu, and that +1 data point is what puts it at exactly 55."""
    assert not looks_like_name(text)


def test_defect_former_ceo_is_not_the_top_decision_maker():
    """Lead 145: six contacts, all "Former CEO" or "Former CRO", primary relevance 100, and again +1 to exactly 55."""
    assert relevance(role_category("Former CEO"), "Former CEO", "ai_consulting") < 50


def test_defect_customer_testimonial_is_not_a_job_title():
    """Lead 222's primary contact, relevance 100, is a customer quoted on the site."""
    assert not is_title("COO of a $4M family-owned plumbing business.")


# --------------------------------------------------------------------------- research reads

def test_defect_redirect_with_an_explicit_port_is_not_offsite(make_ctx):
    """Lead 64: its homepage redirects to the www host with ":443", read as another site, so research stopped."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "zentro.example":
            return httpx.Response(301, headers={"location": "https://www.zentro.example:443/"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><title>Zentro</title><p>Hi</p></html>")
    ctx = make_ctx(handler=handler)
    pages = SiteReader(ctx, "zentro.example", max_pages=1, delay=0).read()
    assert pages[0].status == "ok", pages[0].status


# --------------------------------------------------------------------------- dead ends and what a person is told

def test_defect_review_task_says_why_there_is_no_email(make_ctx):
    """38 of 43 qualified leads stop at "no published business email". The task never says so."""
    ctx = make_ctx()
    lead_id = add_lead(ctx, email=None)
    assert SalesWorkflowAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        task = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.channel == "task")).scalars().one()
    assert "no published" in (task.body or "").lower(), task.body


def test_defect_daily_report_counts_qualified_leads_nobody_can_reach(make_ctx):
    """It lists the top leads and "usable emails" (20), never that 38 of 43 qualified leads have no address."""
    ctx = make_ctx()
    add_lead(ctx, email=None)
    result = AnalyticsReportingAgent(ctx).run({"kind": "daily"})
    assert result.ok
    text = Path(result.output.path_md).read_text(encoding="utf-8").lower()
    assert "no published" in text or "cannot be contacted" in text or "no address" in text


# --------------------------------------------------------------------------- LinkedIn

def test_defect_first_hand_claim_in_a_post_is_sent_to_a_person():
    """Post 7, awaiting approval, QC passed: a first-hand experience claim with nothing behind it."""
    assert check_claims("A title", "We've seen firsthand how a well-designed system helps.").status != "pass"


def overdue_posts_and_a_full_calendar(ctx, overdue=3):
    with ctx.db.session() as s:
        ids = []
        for i in range(overdue):
            item = ContentItem(service="ai_agents", industry="saas", title=f"Late {i}", body="Hello", hashtags=[],
                               status="approved", approved_at=utcnow() - timedelta(days=5), generator="template",
                               fingerprint=f"late{i}", scheduled_for=utcnow() - timedelta(days=3 + i))
            s.add(item)
            s.flush()
            ids.append(item.id)
        # drafts nobody has approved yet hold every slot but one, as in production today
        for i, slot in enumerate(free_slots(s, ctx.settings)[1:]):
            s.add(ContentItem(service="bpa", industry="saas", title=f"Draft {i}", body="Hello", hashtags=[],
                              status="awaiting_approval", generator="template", fingerprint=f"draft{i}", scheduled_for=slot))
        return ids


def test_defect_switching_publishing_on_keeps_the_owners_approvals(make_ctx):
    """On the copy, the first run after the switch: post 1 moves to 12 Nov; posts 2, 6 and 3 lose their approval, while
    unapproved drafts keep the earlier slots."""
    ctx = make_ctx(handler=linkedin_api([]), **AUTH)
    add_token(ctx)
    ids = overdue_posts_and_a_full_calendar(ctx)
    assert AuthorizedPublishingAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        states = [s.get(ContentItem, i).status for i in ids]
    assert states == ["approved"] * len(ids), states


def test_ok_overdue_posts_never_go_out_in_a_burst(make_ctx):
    """Checked on the copy with a fake LinkedIn: nothing overdue is published late, and only one post per run."""
    calls: list[httpx.Request] = []
    ctx = make_ctx(handler=linkedin_api(calls), **AUTH)
    add_token(ctx)
    overdue_posts_and_a_full_calendar(ctx)
    assert AuthorizedPublishingAgent(ctx).run({}).output.published == []
    assert not [c for c in calls if c.url.path == "/rest/posts"]


def test_ok_post_time_follows_new_york_across_the_clock_change(make_ctx):
    ctx = make_ctx()
    slots = candidate_slots(ctx.settings, datetime(2026, 10, 27, 0, 0), 14)
    assert datetime(2026, 10, 27, 13, 30) in slots and datetime(2026, 11, 3, 14, 30) in slots
