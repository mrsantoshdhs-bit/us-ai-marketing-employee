"""Company research and decision-maker identification from companies' own pages and SEC filings (mocked)."""

from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aime.agents.company_research import CompanyResearchAgent
from aime.doctor import run_checks
from aime.models import Company, Contact, Lead, ResearchSnapshot
from aime.agents.qualification import LeadQualificationAgent
from aime.models import Activity, FollowUp
from aime.people import extract_people, is_title, looks_like_name, names_other_org
from aime.site_reader import classify_link, parse_page
from aime.sources.sec_officers import display_name
from aime.web.app import create_app

SITE = "brightline-logistics.example"
HOME = """<html><head><title>Brightline Logistics | Freight software</title>
<meta name="description" content="Brightline Logistics helps regional carriers plan routes, dispatch drivers and update customers from one place.">
</head><body><nav><a href="/about-us">About Us</a> <a href="/team">Our Team</a> <a href="/contact">Contact</a>
<a href="/private/staff">Staff portal</a> <a href="/careers">Careers</a> <a href="https://www.linkedin.com/company/x">LinkedIn</a></nav>
<h1>Freight operations, simplified</h1>
<p>Our dispatchers still copy orders from email into spreadsheets before invoicing, and we want to automate that manual data entry.</p>
</body></html>"""
TEAM = """<html><body>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Person", "name": "Maria Lopez", "jobTitle": "Chief Executive Officer"}</script>
<h2>Our Team</h2>
<div class="card"><h3>David Chen</h3><p>Chief Operating Officer</p></div>
<div class="card"><span class="name">Sarah O'Neil</span><span class="role">VP of Sales</span></div>
<div class="card"><h3>Read More</h3><p>Learn about our leadership</p></div>
<div><p>Tom Baker, Head of Engineering</p></div>
<p>David Chen</p><p>Email: <a href="mailto:david.chen@brightline-logistics.example">david.chen@brightline-logistics.example</a></p>
</body></html>"""
ABOUT = """<html><body><h1>About us</h1>
<p>Brightline was founded by James Carter in 2015 to bring modern software to regional freight carriers across Ohio.</p>
</body></html>"""
CONTACT = """<html><body><h1>Contact</h1><p>Write to <a href="mailto:info@brightline-logistics.example">info@brightline-logistics.example</a>
or <a href="tel:+1-614-555-0100">call us</a>.</p></body></html>"""
PRIVATE = "<html><body><p>Secret Person, CEO</p></body></html>"

CIK = "0000123456"
RECENT = (date.today() - timedelta(days=40)).isoformat()
FORM4 = """<ownershipDocument><reportingOwner><reportingOwnerId><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>{d}</isDirector><isOfficer>{o}</isOfficer><officerTitle>{t}</officerTitle>
</reportingOwnerRelationship></reportingOwner></ownershipDocument>"""
SUBMISSIONS = {"filings": {"recent": {
    "form": ["10-K", "4", "4", "3"],
    "accessionNumber": ["0000123456-26-000001", "0000123456-26-000010", "0000123456-26-000011", "0000123456-26-000020"],
    "primaryDocument": ["k.htm", "xslF345X05/form4a.xml", "xslF345X05/form4b.xml", "xslF345X02/form3.xml"],
    "filingDate": [RECENT, RECENT, RECENT, RECENT]}}}
SEC_DOCS = {
    "/Archives/edgar/data/123456/000012345626000010/form4a.xml": FORM4.format(name="SMITH JOHN A", d=0, o=1, t="Chief Executive Officer"),
    "/Archives/edgar/data/123456/000012345626000011/form4b.xml": FORM4.format(name="DOE JANE", d=1, o=0, t=""),
    "/Archives/edgar/data/123456/000012345626000020/form3.xml": FORM4.format(name="BROWN MIKE", d=0, o=1, t="See Remarks"),
}


def make_handler(requests: list[str], overrides: dict | None = None):
    pages = {"/robots.txt": ("text/plain", "User-agent: *\nDisallow: /private/\n"), "/": ("text/html", HOME),
             "/team": ("text/html", TEAM), "/about-us": ("text/html", ABOUT), "/contact": ("text/html", CONTACT),
             "/private/staff": ("text/html", PRIVATE)}
    pages.update(overrides or {})

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == SITE and path in pages:
            ctype, body = pages[path]
            if isinstance(body, int):
                return httpx.Response(body)
            return httpx.Response(200, text=body, headers={"content-type": f"{ctype}; charset=utf-8"})
        if host == "data.sec.gov" and path == f"/submissions/CIK{CIK}.json":
            return httpx.Response(200, json=SUBMISSIONS)
        if host == "www.sec.gov" and path in SEC_DOCS:
            return httpx.Response(200, text=SEC_DOCS[path], headers={"content-type": "application/xml"})
        return httpx.Response(404)
    return handler


def add_company(ctx, name=SITE and "Brightline Logistics", domain=SITE, **kw) -> int:
    with ctx.db.session() as s:
        c = Company(name=name, normalized_name=name.lower(), domain=domain, website=f"https://{domain}" if domain else None,
                    website_status="verified" if domain else None, source=kw.pop("source", "hn_hiring"), **kw)
        s.add(c)
        s.flush()
        s.add(Lead(company_id=c.id, source="research", status="qualified", score=70, grade="B", primary_service="bpa"))
        return c.id


@pytest.fixture
def research_ctx(make_ctx):
    requests: list[str] = []
    ctx = make_ctx(handler=make_handler(requests), sec_user_agent="aime-test qa@example.com")
    ctx.requests = requests
    return ctx


def people_of(ctx, company_id):
    with ctx.db.session() as s:
        return {f"{c.first_name} {c.last_name}": c for c in s.execute(select(Contact).where(
            Contact.company_id == company_id, Contact.last_name.is_not(None))).scalars()}


def test_reads_only_allowed_pages_and_names_people_with_evidence(research_ctx):
    cid = add_company(research_ctx)
    res = CompanyResearchAgent(research_ctx).run({"company_ids": [cid]})
    assert res.ok, res.error
    assert res.output.companies_researched == 1 and res.output.pages_read == 4
    people = people_of(research_ctx, cid)
    assert set(people) == {"Maria Lopez", "David Chen", "Sarah O'Neil", "Tom Baker", "James Carter"}
    assert people["Maria Lopez"].title == "Chief Executive Officer" and "schema.org" in people["Maria Lopez"].evidence
    assert people["David Chen"].title == "Chief Operating Officer" and people["David Chen"].confidence == "high"
    assert people["Sarah O'Neil"].title == "VP of Sales" and people["Sarah O'Neil"].role_category == "sales"
    assert people["James Carter"].title == "Founder" and people["James Carter"].confidence == "medium"  # prose, not a card
    assert "founded by James Carter" in people["James Carter"].evidence
    for c in people.values():
        assert c.source == "website" and c.evidence_url.startswith(f"https://{SITE}/") and c.consent_status == "none"
        assert c.email is None or c.email_source_url  # only emails the company publishes, never guessed
    urls = research_ctx.requests
    assert not any("/private/" in u for u in urls if not u.endswith("robots.txt"))  # robots.txt honoured
    assert not any("careers" in u or "linkedin" in u for u in urls)


def test_snapshot_quotes_the_company_and_keeps_published_contacts(research_ctx):
    cid = add_company(research_ctx)
    CompanyResearchAgent(research_ctx).run({"company_ids": [cid]})
    with research_ctx.db.session() as s:
        snap = s.execute(select(ResearchSnapshot).where(ResearchSnapshot.company_id == cid)).scalar_one()
        company, lead = s.get(Company, cid), s.execute(select(Lead).where(Lead.company_id == cid)).scalar_one()
        primary = lead.contact
        assert snap.status == "done" and company.research_status == "done" and company.researched_at
        assert snap.description.startswith("Brightline Logistics helps regional carriers") and snap.description_url == f"https://{SITE}/"
        assert any(n["service"] == "bpa" and "manual data entry" in n["quote"] for n in snap.needs)
        emails = {e["email"]: e for e in snap.emails}
        assert emails["info@brightline-logistics.example"]["on_domain"] and emails["info@brightline-logistics.example"]["url"].endswith("/contact")
        assert emails["david.chen@brightline-logistics.example"]["near"] == "David Chen"
        assert snap.phones == [{"phone": "+1-614-555-0100", "url": f"https://{SITE}/contact"}]
        assert "David Chen, Chief Operating Officer" in snap.summary
        assert primary.first_name == "David" and primary.last_name == "Chen"  # COO fits automation best
    checks = {c["name"]: c for c in run_checks(research_ctx)}
    assert checks["Contacts only from approved sources"]["ok"] and checks["Researched contacts have evidence"]["ok"]


def test_rerun_refreshes_without_duplicates(research_ctx):
    cid = add_company(research_ctx)
    for _ in range(2):
        assert CompanyResearchAgent(research_ctx).run({"company_ids": [cid]}).ok
    with research_ctx.db.session() as s:
        assert s.execute(select(func.count(Contact.id))).scalar_one() == 6  # 5 people + the published info@ inbox
        assert s.execute(select(func.count(ResearchSnapshot.id))).scalar_one() == 2
    assert CompanyResearchAgent(research_ctx).run({}).output.companies_researched == 0  # nothing due yet


def test_pages_that_name_nobody_are_recorded_as_not_found(make_ctx):
    requests: list[str] = []
    plain = "<html><body><h1>Welcome</h1><p>We move freight across the Midwest with care and speed every day.</p></body></html>"
    ctx = make_ctx(handler=make_handler(requests, {"/team": ("text/html", plain), "/about-us": ("text/html", plain)}))
    cid = add_company(ctx)
    out = CompanyResearchAgent(ctx).run({"company_ids": [cid]}).output
    with ctx.db.session() as s:
        assert s.get(Company, cid).research_status == "no_people" and out.no_people == [cid]
        snap = s.execute(select(ResearchSnapshot)).scalar_one()
        assert "No decision-maker is named" in snap.summary
    lead_id = 1
    page = TestClient(create_app(ctx)).get(f"/leads/{lead_id}").text
    assert "No decision-maker named on the pages read" in page


def test_blocked_site_is_recorded_and_the_batch_continues(make_ctx):
    requests: list[str] = []
    ctx = make_ctx(handler=make_handler(requests, {"/": ("text/html", 403)}))
    blocked = add_company(ctx)
    res = CompanyResearchAgent(ctx).run({})
    assert res.ok and res.output.companies_researched == 1
    with ctx.db.session() as s:
        assert s.get(Company, blocked).research_status == "blocked"


def test_sec_officers_come_from_insider_filings(research_ctx):
    cid = add_company(research_ctx, name="Northwind Freight Inc", domain=None, source="sec_edgar",
                      source_record_id=f"cik:{CIK}")
    out = CompanyResearchAgent(research_ctx).run({"company_ids": [cid]}).output
    people = people_of(research_ctx, cid)
    assert set(people) == {"John A. Smith", "Jane Doe"} and out.sec_officers == 2
    ceo = people["John A. Smith"]
    assert ceo.title == "Chief Executive Officer" and ceo.source == "sec_filing" and ceo.confidence == "high"
    assert '"SMITH JOHN A"' in ceo.evidence and ceo.evidence_url.endswith("0000123456-26-000010-index.htm")
    assert people["Jane Doe"].title == "Director" and people["Jane Doe"].role_category == "board"


def test_network_disabled_skips(make_ctx):
    ctx = make_ctx(network_enabled=False)
    add_company(ctx)
    out = CompanyResearchAgent(ctx).run({}).output
    assert out.companies_researched == 0 and "disabled" in out.skipped


def test_name_and_title_rules():
    for name in ("Jane Doe", "Sarah O'Neil", "Mary-Kate Olsen", "John A. Smith", "Ian McDonald"):
        assert looks_like_name(name), name
    for text in ("Our Team", "Read More", "Chief Executive Officer", "Contact Us", "JOHN SMITH", "jane doe",
                 "Brightline Logistics", "Learn about our leadership", "Click here to view"):
        assert not looks_like_name(text, "Brightline Logistics"), text
    assert is_title("VP of Sales") and is_title("Co-Founder & CEO") and is_title("Managing Partner")
    assert not is_title("We help carriers grow. Contact us today.") and not is_title("Freight software")
    assert display_name("SMITH JOHN A") == "John A. Smith" and display_name("Doe, Jane") == "Jane Doe"
    assert display_name("O'BRIEN PATRICK JR") == "Patrick O'Brien Jr."


def test_title_first_cards_are_paired_by_the_pages_own_order():
    html = """<div><p>Chief Executive Officer</p><h3>Ana Silva</h3></div>
              <div><p>Chief Technology Officer</p><h3>Ben Okafor</h3></div>"""
    found = {f.full_name: f.title for f in extract_people([parse_page("https://x.example/team", "team", html)])}
    assert found == {"Ana Silva": "Chief Executive Officer", "Ben Okafor": "Chief Technology Officer"}


def test_dashboard_shows_decision_makers(research_ctx):
    cid = add_company(research_ctx)
    CompanyResearchAgent(research_ctx).run({"company_ids": [cid]})
    client = TestClient(create_app(research_ctx))
    page = client.get("/contacts").text
    assert "David Chen" in page and "Chief Operating Officer" in page and "own website" in page
    lead = client.get("/leads/1").text
    assert "Website research" in lead and "In its own words" in lead and "primary" in lead
    assert "Companies with a decision-maker" in client.get("/reports").text


def test_homepage_testimonials_and_people_from_other_companies_are_not_decision_makers():
    home = parse_page("https://acme.example/", "home", """
        <h2>What our customers say</h2>
        <div><p>"Acme saved our team hours every week."</p><h4>Marcus Allen</h4><p>Chief Procurement Officer</p></div>
        <p>Laura Simmons, Finance Director</p>""")
    about = parse_page("https://acme.example/about", "about", """
        <div><h3>Lauren DeSouza</h3><p>Co-founder and CEO</p></div>
        <div><h3>Zainab Jangda</h3><p>Founder, Biddify</p></div>
        <div><h3>Aashni Shah</h3><p>Director, Innovative Tech Labs</p></div>
        <div><h3>Domonique Brown</h3><p>Director of Customer Experience</p></div>
        <p>Our advisor Pat Lee, founder of Northwind Labs, joined in 2024.</p>""")
    found = {f.full_name: f for f in extract_people([home, about], "Acme Software")}
    assert set(found) == {"Lauren DeSouza", "Domonique Brown"}
    assert found["Domonique Brown"].role_category == "operations"  # a department director, not the board


def test_product_pages_are_not_team_pages():
    # "management" or "people" inside a product name is a product page with customer quotes, not the team
    for url, text in (("https://x.example/customer-management", "Customer management"),
                      ("https://x.example/product-lifecycle-management", "PLM"),
                      ("https://x.example/people-analytics", "People analytics")):
        assert classify_link(url, text) != "team", url
    for url, text in (("https://x.example/people", "People"), ("https://x.example/about/management", "Management"),
                      ("https://x.example/our-team", "Our team"), ("https://x.example/company", "Meet our people"),
                      ("https://x.example/leadership-team", "Leadership")):
        assert classify_link(url, text) == "team", url


def test_titles_naming_another_organisation_and_place_names():
    for title in ("Founder & CEO, Octopus Energy Services", "Director of Business Solutions, EDF", "CFO at Acme Corp",
                  "Founder, Biddify"):
        assert names_other_org(title, "Kraken Tech"), title
    for title in ("Founder, CEO", "VP and Head of Global Sales", "Chief People Officer & Chief of Staff",
                  "CEO, Kraken for Water & Telco", "Director of Sales Operations, North America", "Head of Product, AI",
                  "Partner, Tax", "Director of Customer Experience"):
        assert not names_other_org(title, "Kraken Tech"), title
    assert not looks_like_name("San Francisco") and not looks_like_name("New York") and not looks_like_name("Salt Lake City")
    assert looks_like_name("Francisco Lopez") and looks_like_name("Georgia Smith")


def recheck_ctx(make_ctx, state):
    requests: list[str] = []
    base = make_handler(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == SITE and request.url.path in state:
            return httpx.Response(200, text=state[request.url.path], headers={"content-type": "text/html; charset=utf-8"})
        return base(request)
    return make_ctx(handler=handler)


def test_recheck_withdraws_people_no_longer_found_but_keeps_anyone_already_emailed(make_ctx, capsys):
    from argparse import Namespace

    from aime.cli import cmd_research_recheck
    state = {"/team": TEAM}
    ctx = recheck_ctx(make_ctx, state)
    cid = add_company(ctx)
    assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).ok
    found = people_of(ctx, cid)
    assert {"Tom Baker", "Sarah O'Neil"} <= set(found)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        s.add(FollowUp(lead_id=lead.id, contact_id=found["Sarah O'Neil"].id, company_id=cid, channel="email",
                       kind="outreach", step=1, status="awaiting_approval", subject="Hi", body="Body"))
    state["/team"] = (TEAM.replace("<div><p>Tom Baker, Head of Engineering</p></div>", "")
                      .replace('<div class="card"><span class="name">Sarah O\'Neil</span><span class="role">VP of Sales</span></div>', ""))
    assert cmd_research_recheck(Namespace(company=[cid]), ctx) == 0
    assert "1 withdrawn" in capsys.readouterr().out
    after = people_of(ctx, cid)
    assert after["Tom Baker"].source == "website_withdrawn" and "Not found when" in after["Tom Baker"].evidence
    assert after["Sarah O'Neil"].source == "website"  # an email is queued for her: a person decides
    client = TestClient(create_app(ctx))
    assert "Tom Baker" not in client.get("/contacts").text
    lead_page = client.get("/leads/1").text
    assert "<b>Tom Baker</b>" not in lead_page and "1 earlier finding(s) withdrawn" in lead_page  # only in Activity
    with ctx.db.session() as s:
        assert s.execute(select(Activity).where(Activity.subject.like("Decision-maker withdrawn: Tom Baker%"))).scalars().first()
    checks = {c["name"]: c for c in run_checks(ctx)}
    assert checks["Contacts only from approved sources"]["ok"]
    state["/team"] = TEAM  # back on the page: restored, not duplicated
    assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).ok
    again = people_of(ctx, cid)
    assert again["Tom Baker"].source == "website" and len(again) == len(found)


def test_companies_that_sell_ai_agents_are_flagged_and_a_person_can_clear_it(make_ctx):
    state = {"/": HOME.replace("Brightline Logistics helps regional carriers plan routes, dispatch drivers and update "
                               "customers from one place.", "Brightline builds AI agents that automate dispatch for "
                               "regional carriers.")}
    ctx = recheck_ctx(make_ctx, state)
    cid = add_company(ctx)
    with ctx.db.session() as s:  # an outreach draft already waiting for approval
        s.add(FollowUp(lead_id=s.execute(select(Lead)).scalar_one().id, company_id=cid, channel="email", kind="outreach",
                       step=1, status="awaiting_approval", subject="Hi", body="Body", to_email=f"info@{SITE}"))
    assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).output.competitors == [cid]
    LeadQualificationAgent(ctx).run({})
    with ctx.db.session() as s:
        assert s.get(Company, cid).is_competitor
        assert s.execute(select(Lead)).scalar_one().status == "disqualified"
        draft = s.execute(select(FollowUp)).scalar_one()
        assert draft.status == "cancelled" and "competitor" in draft.blocked_reason
        note = s.execute(select(Activity).where(Activity.subject.like("Marked as a possible competitor%"))).scalars().one()
        assert "ai agents that" in note.subject.lower()
    client = TestClient(create_app(ctx))
    assert "Not a competitor" in client.get("/leads/1").text
    client.post(f"/companies/{cid}/not_competitor", headers={"origin": "http://testserver"})
    with ctx.db.session() as s:
        assert not s.get(Company, cid).is_competitor and s.get(Company, cid).competitor_cleared_by
        assert s.execute(select(Lead)).scalar_one().status == "new"
    assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).output.competitors == []  # not flagged again
    LeadQualificationAgent(ctx).run({})
    with ctx.db.session() as s:
        assert s.execute(select(Lead)).scalar_one().status != "disqualified"


def test_unreadable_robots_txt_means_do_not_crawl(make_ctx):
    from aime.website_check import check_website
    for code in (503, 500, 429):
        requests: list[str] = []
        ctx = make_ctx(handler=make_handler(requests, {"/robots.txt": ("text/plain", code)}))
        cid = add_company(ctx)
        assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).ok
        with ctx.db.session() as s:
            assert s.get(Company, cid).research_status == "blocked"
        assert [u for u in requests if not u.endswith("/robots.txt")] == []
        assert check_website(ctx, SITE, "Brightline Logistics").status == "blocked"
    requests = []
    ctx = make_ctx(handler=make_handler(requests, {"/robots.txt": ("text/plain", 404)}))  # none at all: allowed
    cid = add_company(ctx)
    assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).ok and people_of(ctx, cid)


def test_a_site_that_redirects_to_another_domain_is_not_read(make_ctx):
    requests: list[str] = []
    other = '<html><body><p>Our founder, John Smith, started Othercorp in 2010 to sell shoes online.</p></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == SITE:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(301, headers={"location": "https://other.example/"})
        return httpx.Response(200, text=other, headers={"content-type": "text/html"})
    ctx = make_ctx(handler=handler)
    cid = add_company(ctx)
    assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).ok
    with ctx.db.session() as s:
        assert s.get(Company, cid).research_status == "redirected"
    assert not people_of(ctx, cid) and not any("other.example" in u for u in requests)


def test_a_page_that_fails_to_load_withdraws_nobody(make_ctx):
    status = {"/team": 200}
    base = make_handler([])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == SITE and status.get(request.url.path, 200) != 200:
            return httpx.Response(status[request.url.path])
        return base(request)
    ctx = make_ctx(handler=handler)
    cid = add_company(ctx)
    assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).ok
    assert people_of(ctx, cid)["Tom Baker"].source == "website"
    status["/team"] = 503  # a bad moment on the re-check
    res = CompanyResearchAgent(ctx).run({"company_ids": [cid]})
    assert res.ok and res.output.contacts_withdrawn == 0
    assert people_of(ctx, cid)["Tom Baker"].source == "website"


def test_research_attaches_contacts_to_the_open_lead_not_an_old_one(make_ctx):
    ctx = make_ctx(handler=make_handler([]))
    with ctx.db.session() as s:
        c = Company(name="Brightline Logistics", normalized_name="brightline logistics", domain=SITE,
                    website=f"https://{SITE}", website_status="verified", source="hn_hiring")
        s.add(c)
        s.flush()
        s.add(Lead(company_id=c.id, source="research", status="merged", score=10, grade="D"))
        s.flush()
        s.add(Lead(company_id=c.id, source="research", status="qualified", score=70, grade="B"))
        cid = c.id
    assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).ok
    with ctx.db.session() as s:
        old, current = s.execute(select(Lead).order_by(Lead.id)).scalars().all()
        assert old.contact_id is None and current.contact_id is not None


def test_menus_headers_and_footers_are_not_team_cards():
    html = """<html><body><header><nav><ul><li><a href="/coaching">Career Coaching</a></li>
    <li><a href="/exec">Executive Coaching</a></li><li><a href="/about">About</a></li></ul></nav></header>
    <h1>About Summit Growth</h1><div>Mary Jones</div><div>Founder</div>
    <article><header><h3>Paul Green</h3><p>Chief Operating Officer</p></header></article>
    <footer><div>Grace Hall</div><div>Office Manager</div></footer></body></html>"""
    found = {f.full_name: f.title for f in extract_people([parse_page("https://summit.example/about", "about", html)],
                                                           "Summit Growth")}
    assert found == {"Mary Jones": "Founder", "Paul Green": "Chief Operating Officer"}


def test_sec_names_issuers_and_middle_initials():
    from aime.people import same_name
    from aime.sources.sec_officers import parse_ownership_xml
    assert display_name("VAN DYKE RICHARD J") == "Richard J. Van Dyke"
    assert display_name("DE LA CRUZ MARIA") == "Maria De La Cruz"
    assert display_name("SMITH JOHN A") == "John A. Smith" and display_name("DEAN JAMES") == "James Dean"
    other_issuer = ("<ownershipDocument><issuer><issuerCik>0000999999</issuerCik></issuer>" +
                    FORM4.format(name="ROE RICHARD", d=1, o=0, t="").removeprefix("<ownershipDocument>"))
    assert parse_ownership_xml(other_issuer, CIK) == []  # this company's stake in someone else
    itself = ("<ownershipDocument><issuer><issuerCik>123456</issuerCik></issuer><reportingOwner><reportingOwnerId>"
              "<rptOwnerCik>0000123456</rptOwnerCik><rptOwnerName>ATLAS CORP</rptOwnerName></reportingOwnerId>"
              "<reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship></reportingOwner>"
              "</ownershipDocument>")
    assert parse_ownership_xml(itself, CIK) == []
    assert [p["filed_name"] for p in parse_ownership_xml(SEC_DOCS[next(iter(SEC_DOCS))], CIK)] == ["SMITH JOHN A"]
    assert same_name("john smith", "john a smith") and not same_name("john a smith", "john b smith")
    assert not same_name("john smith", "jane smith")


def test_the_same_person_from_the_website_and_an_sec_filing_is_one_contact(make_ctx):
    from aime.company_research import save_contact
    from aime.people import PersonFinding
    ctx = make_ctx()
    with ctx.db.session() as s:
        c = Company(name="Smith Holdings", normalized_name="smith holdings", domain=SITE, source="sec_edgar")
        s.add(c)
        s.flush()
        save_contact(s, c, PersonFinding("John Smith", "Chief Executive Officer", "executive", "high", "card",
                                         f"https://{SITE}/team"), None)
        _, created = save_contact(s, c, PersonFinding("John A. Smith", "CEO", "executive", "high", "Form 4",
                                                      "https://www.sec.gov/x", source="sec_filing"), None)
        assert not created and s.execute(select(func.count(Contact.id))).scalar_one() == 1
