"""Phase 5, round 2: a sign-in page, an unreadable SEC feed, plain sellers, and a person found after the first email. Synthetic data only."""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

import aime.timeutil as tu
from aime.agents.company_research import PERSON_FOUND_TASK, CompanyResearchAgent
from aime.agents.crm import HELD_RUNS_AT_MOST, HELD_RUNS_KEY, OUTAGE_ALERT, CRMManagementAgent
from aime.agents.research import USBusinessResearchAgent
from aime.company_research import WITHDRAWN
from aime.crm_ops import suppress
from aime.db import kv_get
from aime.matching import competitor_evidence, competitor_in_post
from aime.models import Activity, Alert, Company, Contact, FollowUp, Lead, ResearchSnapshot
from aime.sources.base import ResearchFocus
from aime.sources.sec_edgar import SECEdgarSource
from aime.timeutil import utcnow
from tests.test_company_research import CONTACT, HOME, add_company, make_handler
from tests.test_contact_validation import FakeChecker
from tests.test_time_and_alerts import MAIL
from tests.uat.test_p5_research_reads import INBOX, TEAM_NO_ADDRESS, TEAM_WITH_ADDRESS, contact_of_lead, emailed_the_company_inbox, read_again, step2

HTML = {"content-type": "text/html"}
SIGN_IN = ("<html><head><title>Welcome to Harbour Guest Wi-Fi</title></head><body><h1>Harbour Guest Wi-Fi</h1>%s"
           + "".join(f"<p>Please sign in with your room number to use the internet. Term {i}: guests agree to fair use of the network.</p>" for i in range(12))
           + "</body></html>")


def sign_in_page(extra=""):
    """The commonest kind: HTTPS fails the certificate, and plain HTTP answers at the asked-for address."""
    def serve(request):
        if request.url.scheme == "https":
            raise httpx.ConnectError("certificate verify failed: Hostname mismatch", request=request)
        return httpx.Response(200, text=SIGN_IN % extra, headers=HTML, request=request)
    return serve


def sign_in_redirect(scheme):
    def serve(request):
        if request.url.host == "login.isp-portal.example":
            return httpx.Response(200, text=SIGN_IN % "", headers=HTML, request=request)
        if request.url.scheme == "https" and scheme == "http":
            raise httpx.ConnectError("certificate verify failed: Hostname mismatch", request=request)
        return httpx.Response(302, headers={"location": f"{scheme}://login.isp-portal.example/?from=x"}, request=request)
    return serve


def company(ctx, name, domain, *, status="verified", due=True, lead=True):
    with ctx.db.session() as s:
        c = Company(name=name, normalized_name=name.lower(), source="test", domain=domain, website=f"https://{domain}", website_status=status,
                    website_checked_at=utcnow() - timedelta(days=45 if due else 0))
        s.add(c)
        s.flush()
        if lead:
            s.add(Lead(company_id=c.id, source="test", status="qualified", score=70))
        return c.id


def verify(ctx):
    assert CRMManagementAgent(ctx).run({"operations": ["verify_websites"]}, job="test").ok


def statuses(ctx, ids):
    with ctx.db.session() as s:
        return [s.get(Company, i).website_status for i in ids]


def at(before, hours):
    tu.CLOCK_OFFSET = before + timedelta(hours=hours)


def outage_message(ctx):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert).where(Alert.resolved_at.is_(None), Alert.key == OUTAGE_ALERT)).scalars()]


# ------------------------------------------------------------------ pc-1a: the website hold is counted in runs
def test_a_pc_that_was_shut_uses_up_none_of_the_hold(make_ctx):
    ctx = make_ctx(handler=sign_in_page(), **MAIL)
    ids = [company(ctx, name, domain) for name, domain in (("Zebra Quill", "bright-one.example"), ("Mango Vortex", "bright-two.example"), ("Plum Corp", "bright-three.example"))]
    company(ctx, "Known Oneway", "known-1.example", due=False)
    company(ctx, "Known Twoway", "known-2.example", due=False)
    before = tu.CLOCK_OFFSET
    try:
        for hours in (0, 61, 24 * 30):  # shut in between: three sightings, not a month of hold
            at(before, hours)
            verify(ctx)
            with ctx.db.session() as s:
                for i in ids:
                    s.get(Company, i).website_retry_at = None
            assert statuses(ctx, ids) == ["verified"] * 3
    finally:
        tu.CLOCK_OFFSET = before
    with ctx.db.session() as s:
        assert kv_get(s, HELD_RUNS_KEY)["runs"] == 3
    assert any("Nothing changes" in m for m in outage_message(ctx))


def test_runs_close_together_count_once_so_a_night_behind_a_sign_in_page_uses_little(make_ctx):
    ctx = make_ctx(handler=sign_in_page(), **MAIL)
    ids = [company(ctx, name, domain) for name, domain in (("Zebra Quill", "bright-one.example"), ("Mango Vortex", "bright-two.example"), ("Plum Corp", "bright-three.example"))]
    before = tu.CLOCK_OFFSET
    try:
        for quarter in range(8):
            at(before, quarter / 4)
            with ctx.db.session() as s:
                for i in ids:
                    s.get(Company, i).website_retry_at = None
            verify(ctx)
    finally:
        tu.CLOCK_OFFSET = before
    with ctx.db.session() as s:
        assert kv_get(s, HELD_RUNS_KEY)["runs"] == 2


def test_when_the_held_runs_run_out_the_alert_says_what_was_written(make_ctx):
    ctx = make_ctx(handler=sign_in_page(), **MAIL)
    ids = [company(ctx, name, domain) for name, domain in (("Zebra Quill", "bright-one.example"), ("Mango Vortex", "bright-two.example"), ("Plum Corp", "bright-three.example"))]
    before, seen = tu.CLOCK_OFFSET, []
    try:
        for run in range(HELD_RUNS_AT_MOST):
            at(before, run)
            with ctx.db.session() as s:
                for i in ids:
                    s.get(Company, i).website_retry_at = None
            verify(ctx)
            seen.append(statuses(ctx, ids)[0])
    finally:
        tu.CLOCK_OFFSET = before
    assert seen == ["verified"] * (HELD_RUNS_AT_MOST - 1) + ["mismatch"]
    said = outage_message(ctx)
    assert len(said) == 1 and "now recorded as they answered" in said[0] and "Nothing changes" not in said[0]


# ------------------------------------------------------------------ pc-1b
@pytest.mark.parametrize("serve", [sign_in_page(), sign_in_redirect("http"), sign_in_redirect("https")], ids=["inline", "plain-302", "https-302"])
@pytest.mark.parametrize("runs", [1, HELD_RUNS_AT_MOST + 1], ids=["first-sighting", "after-the-hold-ran-out"])
def test_a_sign_in_page_neither_overwrites_a_domain_nor_merges_companies(make_ctx, serve, runs):
    ctx = make_ctx(handler=serve, **MAIL)
    domains = {"Zebra Quill": "bright-one.example", "Mango Vortex": "bright-two.example", "Plum Corp": "bright-three.example"}
    ids = {company(ctx, name, domain): domain for name, domain in domains.items()}
    portal_on_file = company(ctx, "Isp Portal", "isp-portal.example", due=False, lead=False)  # what a merge would pick
    before = tu.CLOCK_OFFSET
    try:
        for run in range(runs):
            at(before, run)
            with ctx.db.session() as s:
                for i in ids:
                    s.get(Company, i).website_retry_at = None
                    s.get(Company, i).website_checked_at = utcnow() - timedelta(days=45)
            verify(ctx)
    finally:
        tu.CLOCK_OFFSET = before
    with ctx.db.session() as s:
        for i, domain in ids.items():
            c = s.get(Company, i)
            assert c.domain == domain and c.website == f"https://{domain}" and c.merged_into_id is None
        assert s.get(Company, portal_on_file).merged_into_id is None
        assert s.execute(select(Lead).where(Lead.status == "merged")).first() is None


# ------------------------------------------------------------------ research behind a sign-in page
def read_once(make_ctx, **over):
    requests: list[str] = []
    ctx = make_ctx(handler=make_handler(requests, {"/team": ("text/html", TEAM_WITH_ADDRESS), "/": ("text/html", HOME), "/contact": ("text/html", CONTACT)}), **{**MAIL, **over})
    cid = add_company(ctx)
    assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]}).ok
    with ctx.db.session() as s:
        c = s.get(Company, cid)
        c.researched_at = utcnow() - timedelta(days=40)
        lead = s.execute(select(Lead).where(Lead.company_id == cid)).scalar_one()
        names = [x.id for x in s.execute(select(Contact).where(Contact.company_id == cid, Contact.source == "website", Contact.last_name.is_not(None))).scalars()]
        assert names
        return ctx, cid, lead.id, names, lead.contact_id, c.researched_at


def witnesses(ctx):
    company(ctx, "Known Oneway", "known-1.example", due=False, lead=False)
    company(ctx, "Known Twoway", "known-2.example", due=False, lead=False)


def research_again(ctx):
    return CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({})


@pytest.mark.parametrize("serve", [sign_in_page(), sign_in_page("<p>This guest network is managed by Harbour Networks, an IT consulting firm. We provide IT consulting and software development services for clients.</p>"), sign_in_redirect("http")],
                         ids=["plain-page", "page-with-selling-words", "plain-redirect"])
@pytest.mark.parametrize("with_witnesses", [True, False], ids=["witnesses", "no-other-site-to-ask"])
def test_research_behind_a_sign_in_page_changes_nothing_and_is_not_stamped(make_ctx, serve, with_witnesses):
    ctx, cid, lid, people, contact_id, stamped = read_once(make_ctx)
    if with_witnesses:
        witnesses(ctx)
    with ctx.db.session() as s:
        snapshots = len(s.execute(select(ResearchSnapshot.id)).all())
    ctx.http_transport = httpx.MockTransport(serve)
    run = research_again(ctx)
    assert run.ok and cid in run.output.held_back and not run.output.companies_researched
    with ctx.db.session() as s:
        c = s.get(Company, cid)
        assert not c.is_competitor and c.researched_at == stamped
        assert s.get(Lead, lid).contact_id == contact_id and s.get(Lead, lid).status == "qualified"
        assert all(s.get(Contact, p).source == "website" for p in people) and not s.execute(select(Contact).where(Contact.source == WITHDRAWN)).first()
        assert len(s.execute(select(ResearchSnapshot.id)).all()) == snapshots
        assert not s.execute(select(Activity.id).where(Activity.subject.like("%competitor%"))).first()
        assert s.execute(select(Alert.id).where(Alert.key == "research:held", Alert.resolved_at.is_(None))).first()


def test_when_the_internet_is_back_research_reads_again_and_the_alert_closes(make_ctx):
    ctx, cid, *_ = read_once(make_ctx)
    witnesses(ctx)
    ctx.http_transport = httpx.MockTransport(sign_in_page())
    research_again(ctx)
    requests: list[str] = []
    ctx.http_transport = httpx.MockTransport(make_handler(requests, {"/team": ("text/html", TEAM_WITH_ADDRESS), "/": ("text/html", HOME), "/contact": ("text/html", CONTACT)}))
    run = research_again(ctx)
    assert run.output.companies_researched and not run.output.held_back
    with ctx.db.session() as s:
        assert s.get(Company, cid).researched_at >= utcnow() - timedelta(minutes=5)
        assert not s.execute(select(Alert.id).where(Alert.key == "research:held", Alert.resolved_at.is_(None))).first()


def test_a_plain_http_site_is_still_read_when_known_sites_prove_good(make_ctx):
    """The network is fine and the site is plain HTTP without its name in the title: this is the old behaviour, unchanged."""
    ctx, cid, *_ = read_once(make_ctx)
    witnesses(ctx)
    page = SIGN_IN % ""

    def serve(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        if "known-" in request.url.host:
            name = "Known Oneway" if "known-1" in request.url.host else "Known Twoway"
            return httpx.Response(200, text=f"<html><head><title>{name}</title></head><body><h1>{name}</h1></body></html>", headers=HTML, request=request)
        if request.url.scheme == "https":
            raise httpx.ConnectError("certificate verify failed", request=request)
        return httpx.Response(200, text=page, headers=HTML, request=request)
    ctx.http_transport = httpx.MockTransport(serve)
    run = research_again(ctx)
    assert run.output.companies_researched and not run.output.held_back


# ------------------------------------------------------------------ SEC
CA = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><content><CIK>0000000001</CIK></content></entry></feed>'
NOT_ATOM = "<html><body><h1>No matching companies.</h1></body></html>"
PLAN = {"industries": ["professional_services"], "states": ["CA", "WY"]}
UA = "copy test test@example.invalid"


def sec_web(feed_for):
    def serve(request):
        host = request.url.host
        if host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": []}, request=request)
        if "cgi-bin/browse-edgar" in str(request.url):
            return httpx.Response(200, text=feed_for(request.url.params.get("State")), request=request)
        if host == "data.sec.gov":
            recent = (utcnow() - timedelta(days=40)).date().isoformat()
            return httpx.Response(200, json={"name": "Good Filer Inc", "website": "https://goodfiler.example", "addresses": {"business": {"stateOrCountry": "CA", "city": "Fresno"}},
                                             "filings": {"recent": {"form": ["10-K"], "filingDate": [recent]}}}, request=request)
        return httpx.Response(404, request=request)
    return serve


def sec_report(run):
    return next(r for r in run.output.sources if r.name == "sec_edgar")


def sec_alerts(ctx):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert).where(Alert.resolved_at.is_(None), Alert.message.like("%sec_edgar%"))).scalars()]


def test_one_unreadable_sec_feed_is_skipped_and_counted_and_the_run_keeps_the_rest(make_ctx):
    ctx = make_ctx(handler=sec_web(lambda state: CA if state == "CA" else NOT_ATOM), sec_user_agent=UA)
    run = USBusinessResearchAgent(ctx).run(PLAN)
    got = sec_report(run)
    assert got.ok is True and got.records >= 1 and "could not be read and were skipped" in got.message and sec_alerts(ctx) == []


def test_sec_fails_only_when_no_feed_could_be_read(make_ctx):
    ctx = make_ctx(handler=sec_web(lambda state: NOT_ATOM), sec_user_agent=UA)
    with pytest.raises(ValueError, match="could not be read"):
        SECEdgarSource(ctx).fetch(ResearchFocus(**PLAN))


def test_the_unreadable_count_starts_again_each_run(make_ctx):
    ctx = make_ctx(handler=sec_web(lambda state: CA), sec_user_agent=UA)
    src = SECEdgarSource(ctx)
    src.unreadable_feeds = 9
    from aime.sources.base import ResearchFocus
    src.fetch(ResearchFocus(**PLAN, limit=5))
    assert src.unreadable_feeds == 0


# ------------------------------------------------------------------ plain sellers are competitors again
@pytest.mark.parametrize("sentence", [
    "As a nearshore partner, we build software for US startups.", "A nearshore company in Mexico.", "Acme, a nearshore company based in Bogota.",
    "An offshore development partner for startups.", "Nearshore AI engineers.", "Nearshore programmers.", "Nearshore dev team.", "Nearshore IT outsourcing.",
    "Leading nearshore provider.", "We do offshore development.", "Offshore development for startups.", "Specialists in offshore development.",
    "A trusted nearshore partner for product teams.", "Home | A nearshore company in Mexico",
])
def test_a_plain_seller_is_flagged_on_a_site_and_in_a_post(sentence):
    assert competitor_evidence(sentence) and competitor_in_post(sentence)


@pytest.mark.parametrize("sentence", [
    "We work with a nearshore partner in Mexico.", "You will coordinate with our nearshore partner.", "We are not a nearshore company.",
    "Experience at an offshore development partner helps.", "Tools for managing nearshore teams.", "A chat app for offshore development teams.",
    "Time tracking for every software engineering company.", "Our offshore development team is in Vietnam.",
    "Nearshore wind farms need better turbine monitoring.", "We finance offshore development of wind projects.",
    "You will coordinate with our nearshore team in Mexico.",
    "We use Acme, a nearshore partner, for mobile work.", "We use Acme as a nearshore partner.", "A nearshore partner builds our mobile app.",
    "An offshore development partner handles our QA.", "Our nearshore dev team is great.", "We hired a nearshore dev team.",
    "We work with specialists in offshore development.", "Offshore development for wind farms is booming.", "We do offshore development of wind farms.",
    "Tools for nearshore AI engineers.", "Tools for a nearshore dev team.", "Our platform helps with offshore development for startups.", "Permits for offshore development for energy companies.",
])
def test_a_company_that_uses_or_serves_such_a_firm_still_is_not_flagged(sentence):
    assert competitor_evidence(sentence) is None


# ------------------------------------------------------------------ the owner's Q6: a person found later gets a task
def person_tasks(ctx):
    with ctx.db.session() as s:
        return list(s.execute(select(FollowUp.status).where(FollowUp.channel == "task", FollowUp.subject.startswith(PERSON_FOUND_TASK))).scalars())


def test_a_named_person_found_after_the_first_email_gets_one_task_and_no_email(make_ctx):
    from tests.test_inbound_sales import FakeSMTP
    ctx, sender, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    sent = len(FakeSMTP.sent)
    site_now_names_a_person()
    assert person_tasks(ctx) == ["pending"] and contact_of_lead(ctx) == INBOX and not step2(ctx)
    site_now_names_a_person()
    site_now_names_a_person()
    assert person_tasks(ctx) == ["pending"]
    with ctx.db.session() as s:
        task = s.execute(select(FollowUp).where(FollowUp.subject.startswith(PERSON_FOUND_TASK))).scalar_one()
        task.status = "done"  # a person ticks it off
        assert task.contact_id and task.blocked_reason and "@" not in task.body
    site_now_names_a_person()
    assert person_tasks(ctx) == ["done"] and len(FakeSMTP.sent) == sent


def test_no_task_on_a_lead_nobody_wrote_to(make_ctx):
    requests: list[str] = []
    ctx = make_ctx(handler=make_handler(requests, {"/team": ("text/html", TEAM_WITH_ADDRESS), "/": ("text/html", HOME), "/contact": ("text/html", CONTACT)}), **MAIL)
    add_company(ctx)
    read_again(ctx, team=TEAM_WITH_ADDRESS, home=HOME, contact=CONTACT)
    assert person_tasks(ctx) == []


def test_no_task_once_the_company_said_no(make_ctx):
    ctx, _, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    with ctx.db.session() as s:
        suppress(s, INBOX, "Asked by phone: stop writing")
        contact = s.execute(select(Lead)).scalar_one().contact
        contact.consent_status = "opted_out"
    site_now_names_a_person()
    assert person_tasks(ctx) == []


def test_someone_already_known_with_no_address_is_no_news(make_ctx):
    ctx, _, site_now_names_a_person = emailed_the_company_inbox(make_ctx)
    with ctx.db.session() as s:
        s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalars().first().sent_at = utcnow() + timedelta(minutes=1)
    site_now_names_a_person(team=TEAM_NO_ADDRESS)
    assert person_tasks(ctx) == []
