from datetime import date, timedelta

import httpx
from sqlalchemy import func, select

from aime.agents import LeadQualificationAgent, USBusinessResearchAgent
from aime.models import Company, Lead, Signal
from aime.sources.hn_hiring import parse_post
from aime.sources.sec_edgar import latest_annual_report, parse_company_feed


def filings(days_ago: int, form: str = "10-K") -> dict:
    """Submissions-API filing history with one annual report filed `days_ago` days before today."""
    recent = (date.today() - timedelta(days=days_ago)).isoformat()
    return {"recent": {"filingDate": [recent, "2001-01-01"], "form": [form, "10-K"]}}


def test_parse_post_extracts_company_facts_without_personal_data():
    item = {"id": 1, "author": "x", "created_at": "2026-10-01T15:00:00.000Z",
            "text": "Acme Robotics | Backend | Austin, TX | <a href=\"https:&#x2F;&#x2F;acme.example\">site</a>"
                    "<p>We automate warehouse workflows. Email jane@acme.example or call (512) 555-0199.</p>"}
    c = parse_post(item)
    assert c.name == "Acme Robotics" and c.state == "TX" and c.domain == "acme.example"
    assert "@" not in c.signal_text and "555-0199" not in c.signal_text
    assert "[email removed]" in c.signal_text


def test_website_prefers_company_domain_over_job_boards_and_media():
    def post(text):
        return parse_post({"id": 2, "author": "x", "created_at": "2026-10-01T15:00:00.000Z", "text": text})

    c = post("Harbor Quant Partners | Engineer | New York, NY | <a href=\"https:&#x2F;&#x2F;grnh.se&#x2F;abc\">apply</a>"
             "<p>More at <a href=\"https:&#x2F;&#x2F;www.harborquant.example\">harborquant.example</a></p>")
    assert c.domain == "harborquant.example"
    c = post("Brightline Metrics | Engineer | San Francisco, CA<p>As covered in "
             "<a href=\"https:&#x2F;&#x2F;www.mercurynews.com&#x2F;story\">the news</a></p>")
    assert c.domain is None  # unknown beats wrong
    assert post("samplestudio.io | Engineer | NYC").domain == "samplestudio.io"
    assert post("etc. | Engineer | Austin, TX") is None


def test_company_names_drop_urls_labels_and_job_fields():
    def name(header):
        c = parse_post({"id": 3, "author": "x", "created_at": "2026-10-01T15:00:00.000Z", "text": header})
        return c and c.name

    assert name("Quillstack https://quillstack.example | Engineer | Remote (US)") == "Quillstack"
    assert name("Company: quillstack.example\nLocation: Austin, TX ONSITE") == "quillstack.example"
    assert name("Role: UX/UI Designer\nAPPLY: https://quillstack.example/careers") is None
    assert name("https://quillstack.example/jobs | Engineer | NYC") == "quillstack.example"


def test_research_run_filters_dedupes_and_is_idempotent(ctx):
    res = USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring", "sec_edgar"]})
    assert res.ok, res.error
    out = res.output
    assert out.candidates_seen == 6
    assert out.non_us_skipped == 1          # London
    assert out.unknown_location_skipped == 1  # "Remote" with no region
    assert out.companies_created == 3       # Northwind, Lumen, Acme Dev Shop
    assert out.companies_matched_existing == 1  # "Northwind Analytics Inc." duplicate post
    assert out.competitors_flagged == 1
    sec = next(s for s in out.sources if s.name == "sec_edgar")
    assert not sec.configured and "NOT CONFIGURED" in sec.message

    with ctx.db.session() as s:
        acme = s.execute(select(Company).where(Company.name == "Acme Dev Shop")).scalars().one()
        assert acme.is_competitor
        assert s.execute(select(func.count(Lead.id)).where(Lead.company_id == acme.id)).scalar_one() == 0
        for sig in s.execute(select(Signal)).scalars():
            assert "@" not in sig.text  # no harvested emails

    again = USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring"]}).output
    assert again.companies_created == 0 and again.signals_added == 0 and again.leads_created == 0


def test_research_leads_get_evidence_based_scores(ctx):
    USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring"]})
    q = LeadQualificationAgent(ctx).run({})
    assert q.ok and q.output.scored == 2
    with ctx.db.session() as s:
        for lead in s.execute(select(Lead)).scalars():
            assert lead.intent_score <= 6  # hiring posts are signals, never buying intent
            assert lead.score_breakdown


def test_network_failure_is_retried_then_reported(make_ctx):
    def down(request):
        raise httpx.ConnectError("offline", request=request)

    ctx = make_ctx(handler=down)
    res = USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring"]})
    assert not res.ok and res.attempts == 3 and "network" in res.error.lower()


def test_sec_edgar_feed_and_detail(make_ctx):
    feed = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>SAMPLE SOFTWARE INC</title><content type="text/xml"><company-info><cik>0000123456</cik>
      <name>SAMPLE SOFTWARE INC</name></company-info></content></entry></feed>"""
    assert parse_company_feed(feed) == [{"cik": "0000123456", "name": "SAMPLE SOFTWARE INC"}]

    def handler(request):
        if request.url.host == "www.sec.gov":
            assert request.headers["user-agent"] == "Test Org ops@test.example"
            return httpx.Response(200, text=feed)
        if request.url.host == "data.sec.gov":
            return httpx.Response(200, json={"name": "Sample Software Inc", "website": "sample-software.example",
                                             "phone": "555-0100", "sicDescription": "Prepackaged software",
                                             "addresses": {"business": {"stateOrCountry": "CA", "city": "SAN JOSE"}},
                                             "filings": filings(90)})
        return httpx.Response(404)

    ctx = make_ctx(handler=handler, sec_user_agent="Test Org ops@test.example")
    res = USBusinessResearchAgent(ctx).run({"sources": ["sec_edgar"], "industries": ["saas"], "states": ["CA"]})
    assert res.ok, res.error
    with ctx.db.session() as s:
        co = s.execute(select(Company)).scalars().one()
        assert co.verified and co.state == "CA" and co.phone == "555-0100" and co.domain == "sample-software.example"


# Shape of SEC's live company-list feed (Oct 2026): names are Perl placeholders, CIKs are real.
REAL_FORMAT_FEED = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <author><email>webmaster@sec.gov</email><name>Webmaster</name></author>
  <entry title="ARRAY(0x56198132cf18)">
    <content type="text/xml">
      <company-info name="ARRAY(0x5619812981b0)">
        <addresses><address type="business"><state>NJ</state></address><address type="mailing" /></addresses>
        <cik>0001355001</cik>
        <irs-number><former-name-0 DATE="2012-10-09" NAME="SAMPLE FORMER NAME" /></irs-number>
        <sic>6531</sic><state>NJ</state>
      </company-info>
    </content>
    <id>urn:tag:www.sec.gov:cik=0001355001</id>
  </entry>
  <entry title="ARRAY(0x1)"><content type="text/xml"><company-info><cik>0000999999</cik></company-info></content></entry>
  <entry title="ARRAY(0x3)"><content type="text/xml"><company-info><cik>0000888888</cik></company-info></content></entry>
</feed>"""


def test_sec_live_feed_format_with_placeholder_names(make_ctx):
    rows = parse_company_feed(REAL_FORMAT_FEED)
    assert rows == [{"cik": "0001355001", "name": None}, {"cik": "0000999999", "name": None},
                    {"cik": "0000888888", "name": None}]

    def handler(request):
        if request.url.host == "www.sec.gov":
            return httpx.Response(200, text=REAL_FORMAT_FEED)
        if request.url.host == "data.sec.gov" and "0001355001" in request.url.path:
            return httpx.Response(200, json={"name": "Sample Realty Group LLC", "website": "", "phone": "555-0101",
                                             "sicDescription": "Real Estate Agents & Managers (For Others)",
                                             "addresses": {"business": {"stateOrCountry": "NJ", "city": "MADISON"}},
                                             "filings": filings(120)})
        if request.url.host == "data.sec.gov" and "0000888888" in request.url.path:  # defunct: last 10-K years ago
            return httpx.Response(200, json={"name": "Old Sample Media Inc", "filings": filings(5000),
                                             "addresses": {"business": {"stateOrCountry": "NJ", "city": "NEWARK"}}})
        if request.url.host == "data.sec.gov":  # no usable name anywhere: must be skipped, not stored
            return httpx.Response(200, json={"name": "ARRAY(0x2)", "addresses": {"business": {"stateOrCountry": "NJ"}},
                                             "filings": filings(30)})
        return httpx.Response(404)

    ctx = make_ctx(handler=handler, sec_user_agent="Test Org ops@test.example")
    res = USBusinessResearchAgent(ctx).run({"sources": ["sec_edgar"], "industries": ["real_estate"], "states": ["NJ"]})
    assert res.ok, res.error
    assert res.output.companies_created == 1
    with ctx.db.session() as s:
        co = s.execute(select(Company)).scalars().one()
        assert co.name == "Sample Realty Group LLC" and co.state == "NJ" and co.city == "Madison" and co.verified
        sig = s.execute(select(Signal)).scalars().one()
        assert sig.signal_type == "filing" and "SEC-registered" in sig.text and sig.matched_services == []


def test_latest_annual_report_detection():
    assert latest_annual_report({"filings": filings(10)}) == date.today() - timedelta(days=10)
    assert latest_annual_report({"filings": {"recent": {"filingDate": ["2026-01-01"], "form": ["8-K"]}}}) is None
    assert latest_annual_report({}) is None
