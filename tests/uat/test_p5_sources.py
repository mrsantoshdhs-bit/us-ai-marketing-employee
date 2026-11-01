"""Phase 5, public sources: a source that stops working reaches a person, and a link in a post is not taken for the company's own
site on one shared word. Synthetic data, fake HTTP only."""

from __future__ import annotations

import os
import time
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from aime.agents.company_research import CompanyResearchAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.research import USBusinessResearchAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.db import kv_get, kv_set
from aime.doctor import DISCOVERY_QUIET_DAYS, run_checks
from aime.inbound import record_inquiry
from aime.integrations import linkedin_leads as ll
from aime.models import Alert, Company, Contact, FollowUp, InboundInquiry, Lead, OAuthToken, ResearchSnapshot
from aime.secure_store import protect
from aime.sources.base import ResearchFocus
from aime.sources.hn_hiring import HNHiringSource, pick_website
from aime.sources.sec_edgar import SECEdgarSource, parse_company_feed
from aime.sources.sec_officers import sec_officers
from aime.timeutil import utcnow
from aime.workflows import job_inbound, job_monitor, job_research

HEADER = "Senior Engineer | Columbus, OH | ONSITE | Full-time"
PLAN = {"industries": ["saas"], "states": ["CA"]}
BLOCK = "<html><body><h1>Request Rate Threshold Exceeded</h1></body></html>"
ATOM_EMPTY = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
ATOM_ONE = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><content><CIK>0000000001</CIK></content></entry></feed>'
ATOM_TWO = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><content><CIK>0000000001</CIK></content></entry>'
            "<entry><content><CIK>0000000002</CIK></content></entry></feed>")
UA = "Test test@example.com"
NOVEMBER = "Ask HN: Who is hiring? (November 2026)"
OCTOBER = "Ask HN: Who is hiring? (October 2026)"


def post(item_id, header, links=()):
    hrefs = " ".join(f'<a href="{u}" rel="nofollow">{u}</a>' for u in links)
    body = "We copy orders from email into spreadsheets before invoicing, and we want to automate that manual data entry."
    return {"id": item_id, "type": "comment", "author": "someone", "created_at": "2026-10-26T15:00:00Z",
            "text": f"{header}{(' | ' + hrefs) if hrefs else ''}<p>{body}"}


GOOD_POST = post(1, f"Quill Retail | {HEADER}")


def hn_api(threads: dict):
    """{story id: (title, children)}, newest first."""
    def handle(req):
        if req.url.path.endswith("/search_by_date"):
            return httpx.Response(200, json={"hits": [{"objectID": k, "title": v[0]} for k, v in threads.items()]})
        return httpx.Response(200, json={"children": threads[req.url.path.rsplit("/", 1)[-1]][1]})
    return handle


GOOD_HN = hn_api({"1": (NOVEMBER, [GOOD_POST])})


def web(hn=GOOD_HN, sec=None, sites=None):
    def handle(req):
        host = req.url.host.lower().removeprefix("www.")
        if host == "hn.algolia.com":
            return hn(req)
        if host in ("sec.gov", "data.sec.gov"):
            return sec(req) if sec else httpx.Response(200, text=ATOM_EMPTY)
        page = (sites or {}).get(host, {}).get(req.url.path)
        if page is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})
    return handle


def research_alerts(ctx):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert).where(Alert.source == "research", Alert.resolved_at.is_(None))).scalars()]


def report(run, name):
    return next(r for r in run.output.sources if r.name == name).model_dump()


def age_the_cache(ctx, days):
    then = time.time() - days * 86400
    for path in ctx.settings.cache_dir.glob("*.json"):
        os.utime(path, (then, then))


# ------------------------------------------------------------------ what counts as the company's own website
def test_a_link_to_someone_elses_site_is_not_taken_for_the_companys_own(make_ctx):
    """A post by "Oaken Retail" that links a customer whose name merely contains "oaken"."""
    home = ("<!doctype html><html><head><title>Oaken Furniture Land | Solid oak tables</title></head><body><h1>Oak furniture</h1>"
            "<p>Solid oak dining tables and chairs delivered across the Midwest since 1998, with free assembly on every order.</p>"
            '<a href="mailto:sales@oakenfurnitureland.example">sales@oakenfurnitureland.example</a></body></html>')
    hn = hn_api({"1": (NOVEMBER, [post(1, f"Oaken Retail | {HEADER}", links=["https://oakenfurnitureland.example/"])])})
    ctx = make_ctx(handler=web(hn, sites={"oakenfurnitureland.example": {"/": home, "/robots.txt": "User-agent: *\nAllow: /\n"}}))
    job_research(ctx)
    CompanyResearchAgent(ctx, mail_checker=lambda domain: True).run({"limit": 15})
    LeadQualificationAgent(ctx).run({})
    SalesWorkflowAgent(ctx).run({"send": False})
    with ctx.db.session() as s:
        company = s.execute(select(Company).where(Company.normalized_name == "oaken retail")).scalars().one()
        to = s.execute(select(FollowUp.to_email).where(FollowUp.company_id == company.id, FollowUp.channel == "email")).scalars().all()
        assert company.domain is None and company.website_status != "verified" and "sales@oakenfurnitureland.example" not in to


@pytest.mark.parametrize("name,domain,own", [
    ("Oaken Retail", "oakenfurnitureland.example", False),  # one shared word, the defect
    ("Oaken Retail Inc", "oakenretailpartners.example", True),  # the full name still leads
    ("Oaken Retail", "oakenretail.example", True),
    ("Oaken Retail", "oaken-retail.example", True),  # a hyphen is not a difference
    ("Oaken Retail", "getoakenretail.example", True),
    ("Oaken Retail", "oakenretailhq.example", True),
    ("Oaken", "oaken-iq.example", True),
    ("Elm", "helmsman.example", False),  # a short name inside another word
    ("Elm", "elmretail.example", True),
    ("Oaken Retail", "oaken.example", True),  # a shorter form of the name
    ("Oaken Retail", "retail.example", True),  # known gap: a word of the name as the whole label is not told from a shortening
    ("Oaken Retail", "stripe.example", False),
])
def test_a_domain_counts_as_the_companys_own_only_on_its_full_name(name, domain, own):
    raw = f'{name} | <a href="https://{domain}/">link</a><p>About us.'
    assert pick_website(raw, f"{name} | link", name) == (domain if own else None)


def test_a_token_link_in_the_body_is_passed_over_for_the_companys_own_later_in_the_post():
    raw = ('Oaken Retail | Remote<p>Our customer <a href="https://oakenfurnitureland.example">one</a> uses '
           '<a href="https://oakenretail.example">our store</a>.')
    assert pick_website(raw, "Oaken Retail | Remote", "Oaken Retail") == "oakenretail.example"


# ------------------------------------------------------------------ a source stopping must reach a person
@pytest.mark.parametrize("hn,sec", [
    (GOOD_HN, lambda r: httpx.Response(200, text=BLOCK)),
    (GOOD_HN, lambda r: httpx.Response(200, text="<?xml version='1.0'?><companies><c/></companies>")),
    (GOOD_HN, lambda r: httpx.Response(200, text="this is not xml <")),
    (GOOD_HN, lambda r: httpx.Response(200, text=ATOM_ONE) if "browse-edgar" in r.url.path else httpx.Response(403, text=BLOCK)),
    (GOOD_HN, lambda r: httpx.Response(200, text=ATOM_ONE) if "browse-edgar" in r.url.path else httpx.Response(429, text=BLOCK)),
    (GOOD_HN, lambda r: httpx.Response(200, text=ATOM_ONE) if "browse-edgar" in r.url.path else httpx.Response(503, text=BLOCK)),
    (lambda r: httpx.Response(200, json={"hits": []}), lambda r: httpx.Response(200, text=ATOM_EMPTY)),
    (hn_api({"1": (NOVEMBER, [])}), lambda r: httpx.Response(200, text=ATOM_EMPTY)),
    (hn_api({"1": (NOVEMBER, [{"id": 1, "type": "post", "text": "a new shape"}])}), lambda r: httpx.Response(200, text=ATOM_EMPTY)),
    (hn_api({"1": (NOVEMBER, [{"id": 1, "type": "comment", "author": "someone", "text": None}])}), lambda r: httpx.Response(200, text=ATOM_EMPTY)),
], ids=["sec-block-page", "sec-new-shape", "sec-not-xml", "sec-lookups-403", "sec-lookups-429", "sec-lookups-503",
        "hn-no-threads", "hn-empty-thread", "hn-comment-type-changed", "hn-nothing-parses"])
def test_a_source_that_stopped_working_tells_a_person(make_ctx, hn, sec):
    ctx = make_ctx(handler=web(hn, sec), sec_user_agent=UA)
    run = USBusinessResearchAgent(ctx).run(PLAN)
    assert run.status == "success"  # the other source was read, so the run goes on
    assert any("not read this run" in a for a in research_alerts(ctx))


def test_the_alert_says_what_is_wrong(make_ctx):
    ctx = make_ctx(handler=web(hn_api({"1": (NOVEMBER, [])})), sec_user_agent=UA)
    USBusinessResearchAgent(ctx).run(PLAN)
    assert any("hn_hiring" in a and "has a post" in a for a in research_alerts(ctx))


def test_an_empty_newest_thread_keeps_last_months_posts_with_no_alert(make_ctx):
    ctx = make_ctx(handler=web(hn_api({"2": (NOVEMBER, []), "1": (OCTOBER, [GOOD_POST])})))
    run = USBusinessResearchAgent(ctx).run(PLAN)
    assert report(run, "hn_hiring")["ok"] is True and report(run, "hn_hiring")["records"] == 1
    assert research_alerts(ctx) == []
    with ctx.db.session() as s:
        assert s.execute(select(Company.name)).scalars().all() == ["Quill Retail"]


def test_an_empty_answer_does_not_replace_the_saved_lists(make_ctx):
    calls = {"hn": GOOD_HN}
    ctx = make_ctx(handler=web(lambda r: calls["hn"](r)))
    source = HNHiringSource(ctx)
    source.fetch(ResearchFocus([], []))
    saved = {p.name: p.read_text() for p in ctx.settings.cache_dir.glob("hn_hiring_*.json")}
    assert len(saved) == 2
    age_the_cache(ctx, 1)  # past the 12-hour cache, so the thread is asked for again

    calls["hn"] = hn_api({"1": (NOVEMBER, [])})
    with pytest.raises(ValueError):
        source.fetch(ResearchFocus([], []))
    calls["hn"] = lambda r: httpx.Response(200, json={"hits": []})
    with pytest.raises(ValueError):
        source.fetch(ResearchFocus([], []))
    assert {p.name: p.read_text() for p in ctx.settings.cache_dir.glob("hn_hiring_*.json")} == saved

    calls["hn"] = lambda r: httpx.Response(500)  # and the outage that follows still has them to fall back on
    found = source.fetch(ResearchFocus([], []))
    assert [c.name for c in found] == ["Quill Retail"] and source.stale_hours is not None


def test_a_dead_hiring_api_is_not_hidden_by_its_cache(make_ctx):
    calls = {"hn": GOOD_HN}
    ctx = make_ctx(handler=web(lambda r: calls["hn"](r)))
    USBusinessResearchAgent(ctx).run(PLAN)
    assert research_alerts(ctx) == []
    calls["hn"] = lambda r: httpx.Response(500)
    age_the_cache(ctx, 39)
    run = USBusinessResearchAgent(ctx).run(PLAN)
    assert report(run, "hn_hiring")["ok"] is True and "stale" in report(run, "hn_hiring")["message"]  # its records are still used
    assert report(run, "hn_hiring")["records"] == 1
    alerts = research_alerts(ctx)
    assert len(alerts) == 1 and "hn_hiring" in alerts[0] and "39" in alerts[0]

    USBusinessResearchAgent(ctx).run(PLAN)  # the next run does not add a second alert
    assert len(research_alerts(ctx)) == 1
    calls["hn"] = GOOD_HN  # and the alert closes when the API is back
    run = USBusinessResearchAgent(ctx).run(PLAN)
    assert research_alerts(ctx) == [] and "stale" not in report(run, "hn_hiring")["message"]


def test_a_missed_day_says_stale_with_no_alert(make_ctx):
    calls = {"hn": GOOD_HN}
    ctx = make_ctx(handler=web(lambda r: calls["hn"](r)))
    USBusinessResearchAgent(ctx).run(PLAN)
    calls["hn"] = lambda r: httpx.Response(500)
    age_the_cache(ctx, 1)
    run = USBusinessResearchAgent(ctx).run(PLAN)
    assert "stale" in report(run, "hn_hiring")["message"] and research_alerts(ctx) == []


def test_a_fresh_answer_is_not_called_stale(make_ctx):
    ctx = make_ctx(handler=web())
    run = USBusinessResearchAgent(ctx).run(PLAN)
    assert report(run, "hn_hiring")["message"] == "Public API, no key required"


# ------------------------------------------------------------------ SEC answers that are not a stoppage
def test_a_well_formed_feed_with_no_entries_stays_ok(make_ctx):
    assert parse_company_feed(ATOM_EMPTY) == []
    assert parse_company_feed("<feed/>") == []
    ctx = make_ctx(handler=web(sec=lambda r: httpx.Response(200, text=ATOM_EMPTY)), sec_user_agent=UA)
    run = USBusinessResearchAgent(ctx).run(PLAN)
    assert report(run, "sec_edgar")["ok"] is True and research_alerts(ctx) == []


@pytest.mark.parametrize("body", ["", "<html><body>blocked</body></html>", "<?xml version='1.0'?><companies/>", "x" * 5_000_001],
                         ids=["empty", "html", "other-xml", "oversized"])
def test_a_body_that_is_no_atom_feed_raises(body):
    with pytest.raises(ValueError):
        parse_company_feed(body)


def filing(name="Filed Co"):
    return {"name": name, "sicDescription": "Services", "addresses": {"business": {"stateOrCountry": "CA", "city": "OAKLAND"}},
            "filings": {"recent": {"form": ["10-K"], "filingDate": [utcnow().date().isoformat()]}}}


@pytest.mark.parametrize("status", [404, 403])
def test_some_lookups_lost_is_one_lost_company_not_a_stopped_source(make_ctx, status):
    def sec(req):
        if "browse-edgar" in req.url.path:
            return httpx.Response(200, text=ATOM_TWO)
        return httpx.Response(200, json=filing()) if "CIK0000000001" in req.url.path else httpx.Response(status, text=BLOCK)
    ctx = make_ctx(handler=web(sec=sec), sec_user_agent=UA)
    run = USBusinessResearchAgent(ctx).run(PLAN)
    assert report(run, "sec_edgar")["ok"] is True and report(run, "sec_edgar")["records"] >= 1 and research_alerts(ctx) == []


def test_every_lookup_404_is_still_no_stoppage(make_ctx):
    sec = lambda r: httpx.Response(200, text=ATOM_ONE) if "browse-edgar" in r.url.path else httpx.Response(404)  # noqa: E731
    ctx = make_ctx(handler=web(sec=sec), sec_user_agent=UA)
    source = SECEdgarSource(ctx)
    assert source.fetch(ResearchFocus(["saas"], ["CA"])) == []


# ------------------------------------------------------------------ SEC refusing while a company's people are read
def sec_company(ctx):
    with ctx.db.session() as s:
        company = Company(name="Filed Co", normalized_name="filed co", source="sec_edgar", source_record_id="cik:0000000001", state="IN", verified=True)
        s.add(company)
        s.flush()
        s.add(Lead(company_id=company.id, source="research", status="nurture"))
        return company.id


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_sec_refusing_during_research_does_not_mark_the_company_researched(make_ctx, status):
    answer = {"sec": lambda r: httpx.Response(status, text=BLOCK)}
    ctx = make_ctx(handler=web(sec=lambda r: answer["sec"](r)), sec_user_agent=UA)
    cid = sec_company(ctx)
    CompanyResearchAgent(ctx, mail_checker=lambda domain: True).run({"limit": 5})
    with ctx.db.session() as s:
        company = s.get(Company, cid)
        assert company.researched_at is None and company.research_status == "failed"
    answer["sec"] = lambda r: httpx.Response(404)  # the next run reads it again
    CompanyResearchAgent(ctx, mail_checker=lambda domain: True).run({"limit": 5})
    with ctx.db.session() as s:
        company = s.get(Company, cid)
        assert company.researched_at is not None and company.research_status == "no_people"


def test_three_companies_refused_is_a_failed_run_a_person_sees(make_ctx):
    ctx = make_ctx(handler=web(sec=lambda r: httpx.Response(403, text=BLOCK)), sec_user_agent=UA)
    with ctx.db.session() as s:
        for n in (1, 2, 3):
            s.add(Company(name=f"Filed {n}", normalized_name=f"filed {n}", source="sec_edgar", source_record_id=f"cik:000000000{n}", state="IN", verified=True))
    run = CompanyResearchAgent(ctx, mail_checker=lambda domain: True).run({"limit": 5})
    assert run.status == "failed" and "researched" in (run.error or "")


def test_a_filer_sec_does_not_have_is_no_failure(make_ctx):
    ctx = make_ctx(handler=web(sec=lambda r: httpx.Response(404)), sec_user_agent=UA)
    assert sec_officers(ctx, "1") == []
    cid = sec_company(ctx)
    CompanyResearchAgent(ctx, mail_checker=lambda domain: True).run({"limit": 5})
    with ctx.db.session() as s:
        company = s.get(Company, cid)
        assert company.researched_at is not None and company.research_status == "no_people"


# ------------------------------------------------------------------ the same, for a company whose own site was read
SITE_HOME = ("<!doctype html><html><head><title>Filed Co | Parts</title></head><body><h1>Parts for regional fleets</h1>"
             "<p>We supply parts to regional fleets across the Midwest and answer every order on the same working day.</p>"
             '<a href="mailto:sales@filedco.example">sales@filedco.example</a></body></html>')
SITE = {"filedco.example": {"/": SITE_HOME}}


def filer_with_site(ctx):
    with ctx.db.session() as s:
        company = Company(name="Filed Co", normalized_name="filed co", source="sec_edgar", source_record_id="cik:0000000001", state="IN",
                          verified=True, domain="filedco.example", website_status="verified")
        s.add(company)
        s.flush()
        s.add(Lead(company_id=company.id, source="research", status="qualified", score=80))
        return company.id


def research(ctx, cid):
    return CompanyResearchAgent(ctx, mail_checker=lambda domain: True).run({"company_ids": [cid]})


def website_addresses(ctx, cid):
    with ctx.db.session() as s:
        return sorted(a for a in s.execute(select(Contact.email).where(Contact.company_id == cid)).scalars() if a)


def refuse(status):
    return lambda r: httpx.Response(status, text=BLOCK)


def unreachable(req):
    raise httpx.ConnectError("no route", request=req)


@pytest.mark.parametrize("sec", [refuse(403), refuse(429), refuse(503), unreachable])
def test_sec_failing_keeps_what_the_companys_own_site_said(make_ctx, sec):
    ctx = make_ctx(handler=web(sec=sec, sites=SITE), sec_user_agent=UA)
    cid = filer_with_site(ctx)
    run = research(ctx, cid)
    assert website_addresses(ctx, cid) == ["sales@filedco.example"]
    assert run.output.no_people == [] and [f.id for f in run.output.failed] == [cid]  # "no people" is not known yet
    with ctx.db.session() as s:
        company = s.get(Company, cid)
        assert company.research_status == "failed" and company.researched_at is None  # the filings are tried again
        snap = s.execute(select(ResearchSnapshot).where(ResearchSnapshot.company_id == cid)).scalars().one()
        assert snap.status == "no_people" and "SEC" in snap.error  # the site was read: a Review task must not say it was not


def test_sec_coming_back_finishes_the_research_of_a_company_whose_site_was_read(make_ctx):
    today = utcnow().date().isoformat()
    answer = {"sec": refuse(403)}
    ctx = make_ctx(handler=web(sec=lambda r: answer["sec"](r), sites=SITE), sec_user_agent=UA)
    cid = filer_with_site(ctx)
    research(ctx, cid)
    answer["sec"] = lambda r: filings(today) if "submissions" in r.url.path else httpx.Response(200, text=FORM4)
    run = research(ctx, cid)
    assert run.output.failed == [] and run.output.sec_officers == 1
    with ctx.db.session() as s:
        company = s.get(Company, cid)
        assert company.research_status == "done" and company.researched_at is not None
        assert "Vance" in " ".join(c.last_name or "" for c in s.execute(select(Contact).where(Contact.company_id == cid)).scalars())
    assert website_addresses(ctx, cid) == ["sales@filedco.example"]


def test_sec_that_stays_refused_waits_for_the_recheck_with_the_site_kept(make_ctx):
    ctx = make_ctx(handler=web(sec=refuse(403), sites=SITE), sec_user_agent=UA)
    cid = filer_with_site(ctx)
    for _ in range(3):
        research(ctx, cid)
    with ctx.db.session() as s:
        assert s.get(Company, cid).researched_at is not None  # cannot take a place in every batch
    assert website_addresses(ctx, cid) == ["sales@filedco.example"]


def test_sec_refusing_when_the_site_was_not_read_either_saves_nothing(make_ctx):
    ctx = make_ctx(handler=web(sec=refuse(403), sites={}), sec_user_agent=UA)
    cid = filer_with_site(ctx)
    research(ctx, cid)
    with ctx.db.session() as s:
        company = s.get(Company, cid)
        assert company.research_status == "failed" and company.researched_at is None
        assert s.execute(select(ResearchSnapshot.error).where(ResearchSnapshot.company_id == cid)).scalar_one()


FORM4 = """<ownershipDocument><issuer><issuerCik>0000000001</issuerCik></issuer><reportingOwner>
<reportingOwnerId><rptOwnerCik>9</rptOwnerCik><rptOwnerName>VANCE COLE</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
</reportingOwner></ownershipDocument>"""


def filings(today):
    return httpx.Response(200, json={"filings": {"recent": {"form": ["4", "4"], "accessionNumber": ["0-0-1", "0-0-2"],
                                                            "primaryDocument": ["a.xml", "b.xml"], "filingDate": [today, today]}}})


def test_filing_documents_refused_with_nobody_found_is_a_failed_read_too(make_ctx):
    today = utcnow().date().isoformat()
    ctx = make_ctx(handler=web(sec=lambda r: filings(today) if "submissions" in r.url.path else httpx.Response(429)), sec_user_agent=UA)
    with pytest.raises(httpx.HTTPStatusError):
        sec_officers(ctx, "1")


def test_one_filing_document_refused_keeps_the_people_of_the_others(make_ctx):
    today = utcnow().date().isoformat()

    def sec(req):
        if "submissions" in req.url.path:
            return filings(today)
        return httpx.Response(200, text=FORM4) if req.url.path.endswith("a.xml") else httpx.Response(503)
    ctx = make_ctx(handler=web(sec=sec), sec_user_agent=UA)
    assert [p.full_name for p in sec_officers(ctx, "1")] == ["Cole Vance"]


def test_filing_documents_missing_is_no_failure(make_ctx):
    today = utcnow().date().isoformat()
    ctx = make_ctx(handler=web(sec=lambda r: filings(today) if "submissions" in r.url.path else httpx.Response(404)), sec_user_agent=UA)
    assert sec_officers(ctx, "1") == []


# ------------------------------------------------------------------ nothing configured, or nothing new for weeks
def discovery_check(ctx):
    return next((c for c in run_checks(ctx) if c["name"] == "New leads are being found"), None)


def doctor_alerts(ctx):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert).where(Alert.source == "doctor", Alert.acknowledged.is_(False))).scalars()
                if a.message.startswith("New leads are being found")]


def seed_company(ctx, source, age_days):
    with ctx.db.session() as s:
        s.add(Company(name=f"Co {source} {age_days}", normalized_name=f"co {source} {age_days}", source=source,
                      created_at=utcnow() - timedelta(days=age_days)))


def test_doctor_warns_once_when_no_public_source_is_on(make_ctx):
    ctx = make_ctx(data_role="production", hn_source_enabled=False, sec_user_agent="")
    check = discovery_check(ctx)
    assert check["ok"] is False and check["severity"] == "warning" and "switched on" in check["detail"]
    job_monitor(ctx)
    job_monitor(ctx)
    assert len(doctor_alerts(ctx)) == 1


def test_sec_alone_finds_no_leads_for_this_check(make_ctx):
    """The owner's decision of 1 Nov 2026: a filing makes no lead."""
    ctx = make_ctx(data_role="production", hn_source_enabled=False, sec_user_agent=UA)
    assert discovery_check(ctx)["ok"] is False


def test_doctor_warns_when_no_company_has_arrived_for_weeks_and_clears_when_one_does(make_ctx):
    ctx = make_ctx(data_role="production")
    seed_company(ctx, "hn_hiring", DISCOVERY_QUIET_DAYS + 5)
    seed_company(ctx, "sec_edgar", 1)  # a filing is no new lead, and a person's own row is no public source
    seed_company(ctx, "csv_import", 1)
    check = discovery_check(ctx)
    assert check["ok"] is False and f"{DISCOVERY_QUIET_DAYS + 5} day(s)" in check["detail"]
    job_monitor(ctx)
    assert len(doctor_alerts(ctx)) == 1
    seed_company(ctx, "hn_hiring", DISCOVERY_QUIET_DAYS - 1)  # last month's thread: a normal gap
    assert discovery_check(ctx)["ok"] is True
    job_monitor(ctx)
    assert doctor_alerts(ctx) == []


def test_a_new_install_with_no_company_yet_is_not_called_quiet(make_ctx):
    assert discovery_check(make_ctx(data_role="production"))["ok"] is True


def test_a_test_or_sample_database_has_no_such_duty(make_ctx):
    assert discovery_check(make_ctx(hn_source_enabled=False)) is None


# ------------------------------------------------------------------ Lead Sync, if it is ever switched on
def linkedin_on(make_ctx, body):
    def handle(req):
        if req.url.host == "api.linkedin.com":
            return httpx.Response(200, text=body, headers={"content-type": "application/json"})
        return httpx.Response(404)
    ctx = make_ctx(handler=handle, linkedin_leads_enabled=True, linkedin_leads_client_id="id", linkedin_leads_client_secret="x")
    with ctx.db.session() as s:
        s.add(OAuthToken(provider=ll.TOKEN_KEY, access_token=protect("t"), scope=ll.NEEDED_SCOPE))
        kv_set(s, ll.OWNERS_KEY, [{"urn": "urn:li:organization:1", "kind": "page"}])
    return ctx


@pytest.mark.parametrize("body", ["<html>blocked</html>", '{"elements": [1]}'], ids=["not-json", "not-a-lead"])
def test_if_switched_on_garbage_from_linkedin_does_not_stop_website_inquiries(make_ctx, body):
    ctx = linkedin_on(make_ctx, body)
    with ctx.db.session() as s:
        record_inquiry(s, {"name": "Pat Example", "email": "pat@quillretail.example", "company": "Quill Retail",
                           "message": "Please call me about a CRM for our team, we need help."}, "form")
    job_inbound(ctx)
    with ctx.db.session() as s:
        assert s.execute(select(InboundInquiry.status)).scalars().first() == "processed"
        told = s.execute(select(Alert.message).where(Alert.source == "crm")).scalars().all()
        assert not kv_get(s, ll.CURSOR_KEY)
    assert told == ["LinkedIn leads not synced: LinkedIn's answer could not be read"]


@pytest.mark.parametrize("body", ['{"unexpected": true}', '[{"elements": []}]', '{"elements": null}', '{"elements": 3}'])
def test_if_switched_on_an_answer_without_elements_does_not_move_the_cursor(make_ctx, body):
    ctx = linkedin_on(make_ctx, body)
    job_inbound(ctx)
    with ctx.db.session() as s:
        assert not kv_get(s, ll.CURSOR_KEY), "the leads of this window are never asked for again"
        assert any("partly synced" in m for m in s.execute(select(Alert.message).where(Alert.source == "crm")).scalars())


def test_if_switched_on_an_empty_page_is_a_real_answer_and_moves_the_cursor(make_ctx):
    ctx = linkedin_on(make_ctx, '{"elements": []}')
    job_inbound(ctx)
    with ctx.db.session() as s:
        assert kv_get(s, ll.CURSOR_KEY)
        assert s.execute(select(Alert.message).where(Alert.source == "crm")).scalars().all() == []
