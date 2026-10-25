import httpx
from sqlalchemy import func, select

from aime.agents import LeadQualificationAgent, USBusinessResearchAgent
from aime.models import Company, Lead, Signal
from aime.sources.hn_hiring import parse_post
from aime.sources.sec_edgar import parse_company_feed


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
                                             "addresses": {"business": {"stateOrCountry": "CA", "city": "SAN JOSE"}}})
        return httpx.Response(404)

    ctx = make_ctx(handler=handler, sec_user_agent="Test Org ops@test.example")
    res = USBusinessResearchAgent(ctx).run({"sources": ["sec_edgar"], "industries": ["saas"], "states": ["CA"]})
    assert res.ok, res.error
    with ctx.db.session() as s:
        co = s.execute(select(Company)).scalars().one()
        assert co.verified and co.state == "CA" and co.phone == "555-0100" and co.domain == "sample-software.example"
