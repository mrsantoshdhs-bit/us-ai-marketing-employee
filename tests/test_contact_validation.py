"""Business contacts from what companies publish: assignment, exclusions, mail-server checks, scoring."""

import dns.resolver
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.agents.company_research import CompanyResearchAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.contact_validation import MailDomainChecker, name_patterns, usable
from aime.doctor import run_checks
from aime.models import Company, Contact, Lead
from aime.web.app import create_app
from tests.test_company_research import SITE, add_company, make_handler

CONTACT = f"""<html><body><h1>Contact</h1>
<p>Sales: <a href="mailto:info@{SITE}">info@{SITE}</a> · <a href="mailto:support@{SITE}">support@{SITE}</a>
· careers: jobs@{SITE} · our agency: hello@otheragency.example · template: you@company.com</p>
<p>Engineering questions go to tbaker@{SITE}.</p><a href="tel:+1-614-555-0100">Call us</a></body></html>"""


class FakeChecker:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, domain):
        self.asked.append(domain)
        return self.answers.get(domain, True)


@pytest.fixture
def run_research(make_ctx):
    def _run(answers=None, contact_page=CONTACT):
        requests: list[str] = []
        ctx = make_ctx(handler=make_handler(requests, {"/contact": ("text/html", contact_page)}))
        cid = add_company(ctx)
        checker = FakeChecker(answers or {})
        res = CompanyResearchAgent(ctx, mail_checker=checker).run({"company_ids": [cid]})
        assert res.ok, res.error
        return ctx, cid, res.output
    return _run


def contacts(ctx, cid):
    with ctx.db.session() as s:
        return {(c.display_name if c.last_name else c.email): c
                for c in s.execute(select(Contact).where(Contact.company_id == cid)).scalars()}


def test_only_published_on_domain_emails_are_attached(run_research):
    ctx, cid, out = run_research()
    people = contacts(ctx, cid)
    david = people["David Chen"]
    assert david.email == f"david.chen@{SITE}" and david.email_confidence == "high"
    assert david.email_status == "published" and david.email_source_url == f"https://{SITE}/team"
    assert 'next to "David Chen"' in david.email_evidence
    tom = people["Tom Baker"]
    assert tom.email == f"tbaker@{SITE}" and tom.email_confidence == "medium" and "matches Tom Baker" in tom.email_evidence
    inbox = people[f"info@{SITE}"]
    assert inbox.role_category == "inbox" and inbox.email_status == "role_address" and inbox.title == "Company inbox"
    assert people["Maria Lopez"].email is None  # nothing published for her: nothing guessed
    all_emails = {c.email for c in people.values() if c.email}
    assert all_emails == {f"david.chen@{SITE}", f"tbaker@{SITE}", f"info@{SITE}"}  # no support/jobs/other/template
    assert out.person_emails == 2 and out.inboxes == 1
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        assert lead.contact.display_name == "David Chen"  # reachable, best role fit for automation
        assert s.get(Company, cid).phone == "+1-614-555-0100"
    checks = {c["name"]: c for c in run_checks(ctx)}
    assert checks["Researched contacts have evidence"]["ok"]


def test_domain_without_mail_server_is_not_usable(run_research):
    ctx, cid, _ = run_research({SITE: False})
    people = contacts(ctx, cid)
    assert people["David Chen"].email_status == "no_mail_server" and people[f"info@{SITE}"].email_status == "no_mail_server"
    with ctx.db.session() as s:
        assert s.execute(select(Lead)).scalar_one().contact.display_name == "David Chen"  # best person, no usable email


def test_unknown_dns_is_recorded_as_unchecked(run_research):
    ctx, cid, _ = run_research({SITE: None})
    assert contacts(ctx, cid)["David Chen"].email_status == "unchecked"


def test_scores_explain_the_contact(run_research):
    ctx, cid, _ = run_research()
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        assert any("decision-maker named by the company, with a business email it publishes" in r
                   for r in lead.score_breakdown)
    ctx2, _, _ = run_research({SITE: False})
    LeadQualificationAgent(ctx2).run({"rescore_all": True})
    with ctx2.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        assert any("decision-maker named by the company (no published email)" in r for r in lead.score_breakdown)


def test_rerun_keeps_one_email_per_address(run_research, make_ctx):
    ctx, cid, _ = run_research()
    CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]})
    emails = [c.email for c in contacts(ctx, cid).values() if c.email]
    assert len(emails) == len(set(emails)) == 3


def test_usable_and_name_patterns():
    assert usable("jane.doe@acme.example") == (True, "")
    for bad in ("support@acme.example", "noreply@acme.example", "jobs@acme.example", "you@company.com",
                "name@domain.com", "not-an-email", "a..b@acme.example"):
        assert not usable(bad)[0], bad
    pats = name_patterns("Jane", "Doe")
    assert {"jane.doe", "jdoe", "jane", "doe", "janed", "doe.jane"} <= pats and "info" not in pats


def test_mail_server_check(monkeypatch):
    class Answer:
        def __init__(self, exchange):
            self.exchange = exchange

    def fake_resolve(self, domain, rtype):
        table = {("mx.example", "MX"): [Answer("mail.mx.example.")], ("nullmx.example", "MX"): [Answer(".")],
                 ("aonly.example", "A"): ["1.2.3.4"]}
        if domain == "gone.example":
            raise dns.resolver.NXDOMAIN
        if domain == "slow.example":
            raise dns.exception.Timeout
        if (domain, rtype) in table:
            return table[(domain, rtype)]
        raise dns.resolver.NoAnswer
    monkeypatch.setattr(dns.resolver.Resolver, "resolve", fake_resolve)
    check = MailDomainChecker()
    assert check("mx.example") is True and check("nullmx.example") is False and check("aonly.example") is True
    assert check("gone.example") is False and check("slow.example") is None and check("noanswer.example") is False


def test_dashboard_shows_emails_and_reachability(run_research):
    ctx, _, _ = run_research()
    client = TestClient(create_app(ctx))
    page = client.get("/contacts?reachable=1").text
    assert f"david.chen@{SITE}" in page and "published" in page and "Company inbox" in page
    assert "Maria Lopez" not in page  # no usable email
    assert "Companies with a usable email" in client.get("/").text




def test_seniority_outranks_function_fit():
    from aime.people import relevance, role_category

    def score(title, service):
        return relevance(role_category(title), title, service)
    # Typify (real site): a co-founder must outrank a training lead for an AI-agents lead.
    assert score("Co-founder and CEO", "ai_agents") > score("Technical Training Lead", "ai_agents")
    assert score("Chief Operating Officer", "bpa") >= score("Chief Executive Officer", "bpa") > score("Director", "bpa")
    assert score("VP of Sales", "custom_crm") > score("Director of Product Development", "custom_crm")
    assert score("Chief Advisory Medical Officer", "bpa") < score("VP of Operations", "bpa")


def test_an_inbox_or_an_address_printed_after_someone_else_is_not_a_persons_email(make_ctx):
    from aime.company_research import published_contacts, save_contact
    from aime.contact_validation import assign_published_emails
    from aime.people import extract_people
    from aime.site_reader import parse_page
    html = (f'<html><body><div><span>Jane Doe</span><span>Managing Partner</span></div>'
            f'<p>Email: <a href="mailto:info@{SITE}">info@{SITE}</a></p>'
            f'<div>John Smith</div><div>Office Manager</div><p>Write to <a href="mailto:jane@{SITE}">jane@{SITE}</a></p>'
            f'<div>Ann Lee</div><div>Chief Financial Officer</div>'
            f'<p><a href="mailto:ann.lee@{SITE}">ann.lee@{SITE}</a></p></body></html>')
    page = parse_page(f"https://{SITE}/team", "team", html)
    emails, _ = published_contacts([page], SITE)
    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="Brightline Logistics", normalized_name="brightline logistics", domain=SITE, source="test")
        s.add(co)
        s.flush()
        for f in extract_people([page], co.name):
            save_contact(s, co, f, None)
        assign_published_emails(s, co, emails, checker=lambda d: True)
        got = {(c.display_name if c.last_name else c.title): c for c in
               s.execute(select(Contact).where(Contact.company_id == co.id)).scalars()}
        assert got["Jane Doe"].email is None  # info@ printed under her name is the company's inbox
        assert got["John Smith"].email is None  # jane@ printed after John is not John's, and not given to anyone
        assert got["Ann Lee"].email == f"ann.lee@{SITE}" and got["Ann Lee"].email_confidence == "high"
        assert got["Company inbox"].email == f"info@{SITE}" and got["Company inbox"].email_status == "role_address"


def test_emails_whose_mail_server_check_failed_are_checked_again(run_research, make_ctx):
    ctx, cid, _ = run_research(answers={SITE: None})  # DNS timed out during research
    assert contacts(ctx, cid)["Tom Baker"].email_status == "unchecked"
    res = CompanyResearchAgent(ctx, mail_checker=lambda d: True).run({})  # nothing due for research
    assert res.ok and res.output.companies_researched == 0 and res.output.emails_rechecked == 1
    assert contacts(ctx, cid)["Tom Baker"].email_status == "published"
