"""Phase 5, what a website check may conclude: each test states the outcome a sales director expects. Synthetic data only."""
from __future__ import annotations

import time
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

import aime.timeutil as tu
from aime.agents.company_research import CompanyResearchAgent
from aime.agents.crm import HELD_RUNS_AT_MOST, HELD_SINCE_KEY, CRMManagementAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.db import kv_get
from aime.dedupe import is_job_board
from aime.models import Activity, Alert, Company, FollowUp, Lead, ResearchSnapshot, Signal
from aime.timeutil import utcnow
from aime.website_check import check_website, name_matches, redirect_names_company
from aime.workflows import job_research
from tests.test_time_and_alerts import MAIL, add_lead

HTML = {"content-type": "text/html"}
HEADER = "Senior Engineer | Columbus, OH | ONSITE | Full-time"
FILLER = ("We are a growing company and we still copy orders from email into spreadsheets before invoicing. "
          "We want to automate that manual data entry and build internal tools for our operations team.")


# ------------------------------------------------------------------ a small fake web
def page(body, status=200, ctype="text/html; charset=utf-8", headers=None):
    return dict(status=status, ctype=ctype, body=body.encode("utf-8") if isinstance(body, str) else body, headers=headers or {})


def redirect(to, status=301):
    return dict(status=status, ctype="text/html", body=b"", headers={"location": to})


def shell(title, body, meta=""):
    return f"<!doctype html><html><head><title>{title}</title>{meta}</head><body>{body}</body></html>"


def robots_ok():
    return page("User-agent: *\nAllow: /\n", ctype="text/plain")


class Web:
    """hosts: {host: {path: page}}, with www. stripped. An unknown host fails like a name not found."""

    def __init__(self):
        self.hosts, self.hn, self.requests = {}, {}, []

    def handle(self, req):
        host = req.url.host.lower().removeprefix("www.")
        self.requests.append(str(req.url))
        if host == "hn.algolia.com":
            if req.url.path.endswith("/search_by_date"):
                return httpx.Response(200, json={"hits": [{"objectID": k, "title": v["title"]} for k, v in self.hn.items()]})
            sid = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"children": self.hn[sid]["children"]}) if sid in self.hn else httpx.Response(404)
        if host in ("www.sec.gov", "sec.gov", "data.sec.gov"):
            return httpx.Response(200, text='<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>')
        site = self.hosts.get(host)
        if site is None:
            raise httpx.ConnectError(f"name not found {host}", request=req)
        p = site.get(req.url.path) or site.get(req.url.path.rstrip("/"))
        if p is None:
            return httpx.Response(404, text="not found", headers=HTML)
        return httpx.Response(p["status"], content=p["body"], headers={"content-type": p["ctype"], **p["headers"]})


def post(item_id, header, links=()):
    hrefs = " ".join(f'<a href="{u}" rel="nofollow">{u}</a>' for u in links)
    return {"id": item_id, "type": "comment", "author": "someone", "created_at": "2026-10-26T15:00:00Z",
            "text": f"{header}{(' | ' + hrefs) if hrefs else ''}<p>{FILLER}"}


def thread(web, story_id, items):
    web.hn[str(story_id)] = {"title": "Ask HN: Who is hiring? (November 2026)", "children": items}


def store(name, dom):
    home = shell(f"{name} | Online store", f"""<nav><a href="/team">Team</a><a href="/contact">Contact</a></nav><h1>{name}</h1>
      <p>{name} sells home goods through its own online store and ships to every state from a warehouse in Ohio since 2009.</p>
      <a href="mailto:info@{dom}">info@{dom}</a>""",
                 f'<meta name="description" content="{name} sells home goods through its own online store and ships to every state from a warehouse in Ohio.">')
    team = shell("Team", f'<div><h3>Rhea Stone</h3><p>Chief Executive Officer</p></div><div><h3>Cole Vance</h3><p>VP of Sales</p><p><a href="mailto:cole.vance@{dom}">cole.vance@{dom}</a></p></div>')
    return {"/robots.txt": robots_ok(), "/": page(home), "/team": page(team), "/contact": page(shell("Contact", "<p>Write to us.</p>"))}


def world(make_ctx, companies):
    web = Web()
    web.hosts = {d: store(n, d) for n, d in companies.items()}
    thread(web, 777001, [post(9_000_000 + i, f"{n} | {HEADER}", links=[f"https://{d}/"]) for i, (n, d) in enumerate(companies.items())])
    return make_ctx(handler=web.handle, polite_delay=0.0), web


def full_day(ctx):
    job_research(ctx)
    CompanyResearchAgent(ctx, mail_checker=lambda d: True).run({"limit": 15})
    LeadQualificationAgent(ctx).run({})
    SalesWorkflowAgent(ctx).run({"send": False})


def by_name(s, name):
    return s.execute(select(Company).where(Company.normalized_name == name.lower())).scalars().first()


# ------------------------------------------------------------------ a redirect to another company
HELMSMAN = shell("Helmsman Shipping | Home", """<nav><a href="/team">Team</a></nav><h1>Helmsman Shipping</h1>
    <p>Helmsman Shipping moves containers between the Gulf Coast and Central America for industrial shippers.</p>
    <a href="mailto:info@helmsman.example">info@helmsman.example</a>""")


def redirecting(make_ctx, existing=False):
    ctx, web = world(make_ctx, {"Elm Retail": "elmretail.example"})
    web.hosts["elmretail.example"] = {"/robots.txt": robots_ok(), "/": redirect("https://helmsman.example/")}
    web.hosts["helmsman.example"] = {"/robots.txt": robots_ok(), "/": page(HELMSMAN), "/team": page(shell("Team", "<div><h3>Rory Vance</h3><p>Chief Executive Officer</p></div>"))}
    if existing:
        with ctx.db.session() as s:
            s.add(Company(name="Helmsman Shipping", normalized_name="helmsman shipping", domain="helmsman.example", source="manual", website_status="verified"))
    return ctx


def test_a_redirect_to_another_company_does_not_take_its_inbox(make_ctx):
    """"Elm" is inside "Helmsman"."""
    ctx = redirecting(make_ctx)
    full_day(ctx)
    with ctx.db.session() as s:
        c = by_name(s, "Elm Retail")
        tos = s.execute(select(FollowUp.to_email).where(FollowUp.company_id == c.id, FollowUp.channel == "email")).scalars().all()
    assert c.domain == "elmretail.example" and "info@helmsman.example" not in tos, (c.domain, tos)
    assert c.website_status == "mismatch"


def test_a_redirect_does_not_merge_a_company_into_an_unrelated_one(make_ctx):
    ctx = redirecting(make_ctx, existing=True)
    job_research(ctx)
    with ctx.db.session() as s:
        assert by_name(s, "Elm Retail").merged_into_id is None


@pytest.mark.parametrize("name,title,domain", [
    ("Elm Retail", "Helmsman Shipping | Home", "helmsman.example"),
    ("Elm", "Helmsman Shipping | Home", "helmsman.example"),
    ("Acme Health", "Bright Health | Home", "brighthealth.example"),
    ("AI Health", "Bright Health | Home", "brighthealth.example"),
    ("Summit Software", "Acme Software", "acmesoftware.example"),
    ("Blue Data", "Big Data Analytics", "bigdata.example"),
    ("Elm Retail", "Helm Retailers | Home", "helmretailers.example"),
    ("Net One", "Welcome: please sign in to use the internet", "isp-portal.example"),
    ("HP", "HP Laptops", "laptops.example"),  # a name too short for the old match: the redirect check is only ever narrower
])
def test_what_does_not_count_as_a_redirect_to_the_same_company(name, title, domain):
    assert not redirect_names_company(name, title, None, domain)


@pytest.mark.parametrize("name,title,domain", [
    ("Acme Robotics", "Welcome", "acme-robotics.example"),
    ("Acme Robotics Inc", "Welcome", "acme-robotics.example"),
    ("Acme Robotics", "Acme Robotics Inc", "acmerobotics.example"),
    ("Acme Labs", "Acme | Home", "acme-new.example"),  # the first word is the whole name: "labs" names nothing
    ("Smith & Jones", "Smith & Jones | Home", "newname.example"),
    ("AI Health", "AI Health is now Bright", "bright.example"),
])
def test_what_still_counts_as_a_redirect_to_the_same_company(name, title, domain):
    assert redirect_names_company(name, title, None, domain)


def test_the_same_domain_check_keeps_its_looser_match():
    """146 production companies rest on it: only the redirect got stricter."""
    assert name_matches("Acme Health", "Bright Health | Home") and name_matches("Elm Retail", "Helmsman Shipping")


def due_company(ctx, name, domain, status="verified"):
    with ctx.db.session() as s:
        c = Company(name=name, normalized_name=name.lower(), domain=domain, website=f"https://{domain}", source="test", state="TX", country="US",
                    website_status=status, website_checked_at=utcnow() - timedelta(days=31) if status else None)
        s.add(c)
        s.flush()
        return c.id


def fresh_witness(ctx, name, domain):
    company_id = due_company(ctx, name, domain)
    with ctx.db.session() as s:
        s.get(Company, company_id).website_checked_at = utcnow()
    return company_id


def verify(ctx, **more):
    return CRMManagementAgent(ctx).run({"operations": ["verify_websites"], **more}, job="test")


def kv_held(ctx):
    with ctx.db.session() as s:
        return kv_get(s, HELD_SINCE_KEY)


def outage_alerts(ctx):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert).where(Alert.key == "website_check:outage", Alert.resolved_at.is_(None))).scalars()]


def test_a_first_token_rebrand_stays_redirected_with_the_new_domain(make_ctx):
    web = Web()
    web.hosts["acmeold.example"] = {"/robots.txt": robots_ok(), "/": redirect("https://acme-new.example/")}
    web.hosts["acme-new.example"] = {"/robots.txt": robots_ok(), "/": page(shell("Acme | Home", "<h1>Acme</h1>"))}
    ctx = make_ctx(handler=web.handle)
    cid = due_company(ctx, "Acme Labs", "acmeold.example")
    verify(ctx)
    with ctx.db.session() as s:
        c = s.get(Company, cid)
        assert (c.website_status, c.domain, c.website) == ("redirected", "acme-new.example", "https://acme-new.example")


@pytest.mark.parametrize("old_name,owner_name,merges", [("Acme Robotics", "Acme Robotics Inc", True), ("Acme Robotics", "Bright Parent", False)])
def test_a_merge_also_needs_the_names_to_agree(make_ctx, old_name, owner_name, merges):
    web = Web()
    web.hosts["acmeold.example"] = {"/robots.txt": robots_ok(), "/": redirect("https://parent.example/")}
    web.hosts["parent.example"] = {"/robots.txt": robots_ok(), "/": page(shell("Acme Robotics is now part of the family", "<h1>Welcome</h1>"))}
    ctx = make_ctx(handler=web.handle)
    cid = due_company(ctx, old_name, "acmeold.example")
    owner = fresh_witness(ctx, owner_name, "parent.example")
    verify(ctx)
    with ctx.db.session() as s:
        c = s.get(Company, cid)
        assert c.merged_into_id == (owner if merges else None)
        if not merges:
            assert c.domain == "acmeold.example"
            assert any("not merged" in a.subject for a in s.execute(select(Activity).where(Activity.company_id == cid)).scalars())


# ------------------------------------------------------------------ a sign-in page in front of the internet
PORTAL = "<html><head><title>Welcome: please sign in to use the internet</title></head><body>Please sign in. Your data pack has ended.</body></html>"


def portal_302(req):
    if req.url.scheme == "https":
        raise httpx.ConnectError("certificate verify failed", request=req)
    if req.url.host == "login.isp-portal.example":
        return httpx.Response(200, text=PORTAL, headers=HTML, request=req)
    return httpx.Response(302, headers={"location": "http://login.isp-portal.example/"}, request=req)


def portal_inline(req):
    if req.url.scheme == "https":
        raise httpx.ConnectError("certificate verify failed", request=req)
    return httpx.Response(200, text=PORTAL, headers=HTML, request=req)


def portal_code(code):
    def handler(req):
        if req.url.scheme == "https":
            raise httpx.ConnectError("certificate verify failed", request=req)
        return httpx.Response(code, text=PORTAL, headers=HTML, request=req)
    handler.__name__ = f"portal_{code}"
    return handler


SHAPES = [portal_302, portal_inline, portal_code(511), portal_code(403)]


def leads_on_sites(ctx, names):
    ids = []
    for name in names:
        lead_id = add_lead(ctx)
        with ctx.db.session() as s:
            co = s.get(Company, s.get(Lead, lead_id).company_id)
            co.name, co.normalized_name, co.name_set_by = name, name.lower(), None
            co.website_checked_at = utcnow() - timedelta(days=31)
            ids.append((co.id, co.domain))
    return ids


def snapshot(ctx, ids):
    with ctx.db.session() as s:
        return [(s.get(Company, i).website_status, s.get(Company, i).domain, s.get(Company, i).merged_into_id, s.get(Company, i).website_title,
                 s.get(Company, i).validation) for i, _ in ids]


def test_a_sign_in_page_on_public_wifi_does_not_change_what_is_known_of_the_sites(make_ctx):
    ctx = make_ctx(handler=portal_302, **MAIL)
    ids = []
    for _ in range(4):
        lead_id = add_lead(ctx)
        with ctx.db.session() as s:
            co = s.get(Company, s.get(Lead, lead_id).company_id)
            co.website_checked_at = utcnow() - timedelta(days=31)
            ids.append(co.id)
    verify(ctx)
    with ctx.db.session() as s:
        assert [s.get(Company, i).website_status for i in ids] == ["verified"] * 4


@pytest.mark.parametrize("handler", SHAPES, ids=lambda h: h.__name__)
@pytest.mark.parametrize("names", [("Zebra Quill", "Mango Vortex", "Plum Corp"), ("Net One", "Net Two", "Plum Corp"), ("Mango Internet", "Bright Internet", "Plum Corp")],
                         ids=["no-overlap", "substring-overlap", "word-overlap"])
def test_every_shape_of_sign_in_page_changes_nothing_and_says_so(make_ctx, handler, names):
    ctx = make_ctx(handler=handler, **MAIL)
    ids = leads_on_sites(ctx, names)
    before = snapshot(ctx, ids)
    verify(ctx)
    assert snapshot(ctx, ids) == before
    with ctx.db.session() as s:
        rows = [s.get(Company, i) for i, _ in ids]
        assert all(c.website_retry_at and c.website_retry_at <= utcnow() + timedelta(hours=1) for c in rows)
        assert all(c.website_checked_at < utcnow() - timedelta(days=30) for c in rows)
        assert not any(c.website_no_answer for c in rows)
    said = outage_alerts(ctx)
    assert len(said) == 1 and "sign-in page" in said[0] and "internet connection looks down" in said[0]


@pytest.mark.parametrize("handler", SHAPES, ids=lambda h: h.__name__)
def test_a_small_run_on_a_sign_in_page_asks_a_witness_and_changes_nothing(make_ctx, handler):
    """A run of one new company (the research job passes only those), and known sites outside it to ask."""
    ctx = make_ctx(handler=handler, **MAIL)
    fresh_witness(ctx, "Known One", "knownone.example")
    fresh_witness(ctx, "Known Two", "knowntwo.example")
    cid = due_company(ctx, "Plum Corp", "plumcorp.example", status=None)
    verify(ctx, company_ids=[cid])
    with ctx.db.session() as s:
        c = s.get(Company, cid)
        assert (c.website_status, c.website_checked_at, c.domain) == (None, None, "plumcorp.example") and c.website_retry_at
    assert len(outage_alerts(ctx)) == 1


def test_a_sign_in_page_at_the_sites_own_address_does_not_prove_the_network_good(make_ctx):
    """The domain spells the name, so this reads "verified", yet it is the sign-in page: it must not wave the others through or replace a title."""
    ctx = make_ctx(handler=portal_inline, **MAIL)
    fresh_witness(ctx, "Known One", "knownone.example")
    fresh_witness(ctx, "Known Two", "knowntwo.example")
    spelled = due_company(ctx, "Spelled Name", "spelledname.example")
    other = due_company(ctx, "Plum Corp", "bright-fruit.example")
    with ctx.db.session() as s:
        s.get(Company, spelled).website_title = "Spelled Name | Technology Services & Consulting"
    verify(ctx)
    with ctx.db.session() as s:
        assert s.get(Company, spelled).website_title == "Spelled Name | Technology Services & Consulting"
        assert s.get(Company, other).website_status == "verified"
    assert outage_alerts(ctx)


def test_verdicts_come_in_once_the_sign_in_page_is_gone(make_ctx):
    answers = {"mode": "portal"}
    web = Web()
    web.hosts["moved.example"] = {"/robots.txt": robots_ok(), "/": page(shell("Totally different store", "<h1>Hello</h1>"))}
    web.hosts["knownone.example"] = {"/robots.txt": robots_ok(), "/": page(shell("Known One", "<h1>Known One</h1>"))}

    def handler(req):
        return portal_302(req) if answers["mode"] == "portal" else web.handle(req)
    ctx = make_ctx(handler=handler, **MAIL)
    fresh_witness(ctx, "Known One", "knownone.example")
    cid = due_company(ctx, "Plum Corp", "moved.example")
    verify(ctx)
    with ctx.db.session() as s:
        assert s.get(Company, cid).website_status == "verified"
    assert outage_alerts(ctx)
    answers["mode"] = "back"
    before = tu.CLOCK_OFFSET
    try:
        tu.CLOCK_OFFSET = before + timedelta(hours=2)
        verify(ctx)
        with ctx.db.session() as s:
            c = s.get(Company, cid)
            assert (c.website_status, c.website_retry_at) == ("mismatch", None)
        assert not outage_alerts(ctx)
    finally:
        tu.CLOCK_OFFSET = before


def test_witnesses_that_really_changed_cannot_hold_verdicts_forever(make_ctx):
    web = Web()
    web.hosts["moved.example"] = {"/robots.txt": robots_ok(), "/": page(shell("Totally different store", "<h1>Hello</h1>"))}
    web.hosts["changed.example"] = {"/robots.txt": robots_ok(), "/": page(shell("Someone else entirely", "<h1>Hello</h1>"))}
    ctx = make_ctx(handler=web.handle)
    fresh_witness(ctx, "Plain Corp", "changed.example")
    cid = due_company(ctx, "Plum Corp", "moved.example")
    before = tu.CLOCK_OFFSET
    seen = []
    try:
        for hours in range(HELD_RUNS_AT_MOST):  # the cap is counted in runs, each an hour apart here, so a PC that was shut uses none of it
            tu.CLOCK_OFFSET = before + timedelta(hours=hours)
            verify(ctx)
            with ctx.db.session() as s:
                seen.append(s.get(Company, cid).website_status)
    finally:
        tu.CLOCK_OFFSET = before
    assert seen == ["verified"] * (HELD_RUNS_AT_MOST - 1) + ["mismatch"]


@pytest.mark.parametrize("handler", [portal_302, portal_inline], ids=lambda h: h.__name__)
@pytest.mark.parametrize("witnesses", [0, 2], ids=["run-is-its-own-witness", "witnesses-outside-the-run"])
def test_a_second_sign_in_page_long_after_a_full_recovery_changes_nothing(make_ctx, handler, witnesses):
    """The first sign-in page's timer ends with the recovery, even when nothing was left waiting: the next one starts its own."""
    answers = {"mode": "portal"}
    web = Web()
    names = {"Zebra Quill": "bright-one.example", "Mango Vortex": "bright-two.example", "Plum Corp": "bright-three.example"}
    for name, domain in names.items():
        web.hosts[domain] = {"/robots.txt": robots_ok(), "/": page(shell(name, f"<h1>{name}</h1>"))}
    for i in range(witnesses):
        web.hosts[f"known-{i}.example"] = {"/robots.txt": robots_ok(), "/": page(shell(f"Known {i}x", f"<h1>Known {i}x</h1>"))}

    def serve(req):
        return handler(req) if answers["mode"] == "portal" else web.handle(req)
    ctx = make_ctx(handler=serve, **MAIL)
    ids = [(due_company(ctx, name, domain), domain) for name, domain in names.items()]
    known = [fresh_witness(ctx, f"Known {i}x", f"known-{i}.example") for i in range(witnesses)]
    before = tu.CLOCK_OFFSET
    try:
        verify(ctx)
        assert kv_held(ctx) and len(outage_alerts(ctx)) == 1
        answers["mode"] = "back"
        tu.CLOCK_OFFSET = before + timedelta(hours=2)
        verify(ctx)
        assert not kv_held(ctx) and not outage_alerts(ctx)
        answers["mode"] = "portal"
        tu.CLOCK_OFFSET = before + timedelta(days=40)
        with ctx.db.session() as s:
            for company_id in known:
                s.get(Company, company_id).website_checked_at = utcnow()
        held = snapshot(ctx, ids)
        verify(ctx)
        assert snapshot(ctx, ids) == held
        assert len(outage_alerts(ctx)) == 1
    finally:
        tu.CLOCK_OFFSET = before


def test_a_run_with_no_proof_leaves_a_running_hold_as_it_is(make_ctx):
    """Silence proves nothing: a hold that is still running is not restarted by a run in which no site answered."""
    answers = {"mode": "portal"}

    def serve(req):
        if answers["mode"] == "portal":
            return portal_302(req)
        raise httpx.ConnectError("no network", request=req)
    ctx = make_ctx(handler=serve, **MAIL)
    for i, name in enumerate(("Zebra Quill", "Mango Vortex", "Plum Corp")):
        due_company(ctx, name, f"bright-{i}.example")
    before = tu.CLOCK_OFFSET
    try:
        verify(ctx)
        started = kv_held(ctx)
        assert started
        answers["mode"] = "silent"
        tu.CLOCK_OFFSET = before + timedelta(hours=2)
        verify(ctx)
        assert kv_held(ctx) == started
    finally:
        tu.CLOCK_OFFSET = before


def test_a_real_same_parent_pair_ends_as_mismatch_with_no_alert(make_ctx):
    def handler(req):
        if req.url.host == "parent-holdings.example":
            return httpx.Response(200, text="<title>Parent Holdings</title>", headers=HTML, request=req)
        if req.url.host == "knownone.example":
            return httpx.Response(200, text="<title>Known One</title>", headers=HTML, request=req)
        if req.url.path == "/robots.txt":
            return httpx.Response(404, request=req)
        return httpx.Response(301, headers={"location": "https://parent-holdings.example/"}, request=req)
    for witness in (False, True):
        ctx = make_ctx(handler=handler, **MAIL)
        if witness:
            fresh_witness(ctx, "Known One", "knownone.example")
        ids = leads_on_sites(ctx, ("Zebra Quill", "Mango Vortex"))
        verify(ctx)
        with ctx.db.session() as s:
            assert [(s.get(Company, i).website_status, s.get(Company, i).domain) for i, _ in ids] == [("mismatch", d) for _, d in ids]
        assert not outage_alerts(ctx)


def test_a_lone_real_redirect_with_a_healthy_witness_applies(make_ctx):
    web = Web()
    web.hosts["acmeold.example"] = {"/robots.txt": robots_ok(), "/": redirect("https://acmenew.example/")}
    web.hosts["acmenew.example"] = {"/robots.txt": robots_ok(), "/": page(shell("Acme Widgets", "<h1>Acme Widgets</h1>"))}
    web.hosts["knownone.example"] = {"/robots.txt": robots_ok(), "/": page(shell("Known One", "<h1>Known One</h1>"))}
    ctx = make_ctx(handler=web.handle)
    fresh_witness(ctx, "Known One", "knownone.example")
    cid = due_company(ctx, "Acme Widgets", "acmeold.example")
    verify(ctx)
    with ctx.db.session() as s:
        assert (s.get(Company, cid).website_status, s.get(Company, cid).domain) == ("redirected", "acmenew.example")
    assert not outage_alerts(ctx)


def test_a_run_with_a_site_proven_good_does_not_ask_witnesses(make_ctx):
    web = Web()
    web.hosts["good.example"] = {"/robots.txt": robots_ok(), "/": page(shell("Good Co", "<h1>Good Co</h1>"))}
    web.hosts["wrong.example"] = {"/robots.txt": robots_ok(), "/": page(shell("Something else", "<h1>Hi</h1>"))}
    web.hosts["knownone.example"] = {"/robots.txt": robots_ok(), "/": page(shell("Known One", "<h1>Known One</h1>"))}
    ctx = make_ctx(handler=web.handle)
    fresh_witness(ctx, "Known One", "knownone.example")
    due_company(ctx, "Good Co", "good.example")
    wrong = due_company(ctx, "Plum Corp", "wrong.example")
    verify(ctx)
    assert not [u for u in web.requests if "knownone" in u]
    with ctx.db.session() as s:
        assert s.get(Company, wrong).website_status == "mismatch"


def test_a_redirect_over_plain_http_moves_nothing_but_a_real_https_one_does(make_ctx):
    def handler(req):
        if req.url.host == "acmewidgets.example":
            return httpx.Response(200, text="<title>Acme Widgets</title>", headers=HTML, request=req)
        if req.url.host == "knownone.example":
            return httpx.Response(200, text="<title>Known One</title>", headers=HTML, request=req)
        if req.url.host == "httponly.example" and req.url.scheme == "https":
            raise httpx.ConnectError("no https here", request=req)
        return httpx.Response(301, headers={"location": "https://acmewidgets.example/"}, request=req)
    ctx = make_ctx(handler=handler)
    fresh_witness(ctx, "Known One", "knownone.example")
    plain = due_company(ctx, "Acme Widgets", "httponly.example")
    with ctx.db.session() as s:
        title = s.get(Company, plain).website_title
    verify(ctx)
    with ctx.db.session() as s:
        c = s.get(Company, plain)
        assert (c.domain, c.website, c.website_status, c.website_title, c.merged_into_id) == ("httponly.example", "https://httponly.example", "verified", title, None)
        assert c.website_checked_at > utcnow() - timedelta(minutes=1) and "not confirmed over HTTPS" in c.validation["website"]
    https = due_company(ctx, "Acme Widgets B", "acme-widgets-b.example")
    verify(ctx)
    with ctx.db.session() as s:
        c = s.get(Company, https)
        assert c.website_status == "mismatch" and c.domain == "acme-widgets-b.example"  # the page names "Acme Widgets", not this one
    real = due_company(ctx, "Acme Widgets", "acmeoldname.example")
    verify(ctx)
    with ctx.db.session() as s:
        assert s.get(Company, real).website_status == "redirected" and s.get(Company, real).domain == "acmewidgets.example"


# ------------------------------------------------------------------ one error answer
POST = "Acme Tools | Software Engineer | Austin, TX. We have paying customers and want to automate our back office."


def faulty(kind):
    """The site is fine except for one kind of answer on the monthly check."""
    def handler(request):
        if kind == "robots500" and request.url.path == "/robots.txt":
            return httpx.Response(500)
        if kind == "robots429" and request.url.path == "/robots.txt":
            return httpx.Response(429)
        if kind == "disallow" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /", headers={"content-type": "text/plain"})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if kind.startswith("home"):
            return httpx.Response(int(kind[4:]))
        if kind == "notitle":
            return httpx.Response(200, text="<html><body><h1>Hello</h1></body></html>", headers=HTML)
        if kind == "challenge":
            return httpx.Response(200, text="<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing the site.</body></html>", headers=HTML)
        return httpx.Response(200, text="<html><head><title>Acme Tools</title></head><body><h1>Acme Tools</h1></body></html>", headers=HTML)
    return handler


def qualified_lead(ctx, post_age=50, activity_age=1, researched_age=1):
    now = tu.utcnow()
    with ctx.db.session() as s:
        c = Company(name="Acme Tools", normalized_name="acme tools", domain="acme.example", website="https://acme.example", state="TX", country="US",
                    source="hn_hiring", website_status="verified", website_checked_at=now - timedelta(days=1), employee_range="11-50", location_text="Austin, TX",
                    industry="saas", researched_at=now - timedelta(days=researched_age), research_status="no_people")
        s.add(c)
        s.flush()
        when = now - timedelta(days=post_age)
        s.add(Signal(company_id=c.id, signal_type="hiring", text=POST, source="hn_hiring", source_url="https://example.test/1", fingerprint="p5", observed_at=when, created_at=when))
        lead = Lead(company_id=c.id, source="research", status="qualified", last_activity_at=now - timedelta(days=activity_age), created_at=now - timedelta(days=60))
        s.add(lead)
        s.flush()
        s.add(FollowUp(lead_id=lead.id, company_id=c.id, channel="task", step=1, status="done", due_at=now - timedelta(days=30), subject="Review Acme Tools", body="x"))
        return lead.id


@pytest.mark.parametrize("kind", ["robots500", "robots429", "home503", "home500", "home429", "home403", "home401", "home404", "challenge"])
def test_one_error_answer_on_the_monthly_check_does_not_drop_a_qualified_lead_or_its_draft(make_ctx, kind):
    ctx = make_ctx(handler=faulty(kind), polite_delay=0)
    lead_id = qualified_lead(ctx)
    now = tu.utcnow()
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        lead.company.website_checked_at = now - timedelta(days=31)
        s.add(FollowUp(lead_id=lead_id, company_id=lead.company_id, channel="email", kind="outreach", step=1, status="awaiting_approval", due_at=now, subject="Hello", body="x"))
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).status == "qualified"
    verify(ctx)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        draft = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.channel == "email")).scalars().one()
        got = (lead.status, lead.company.website_status, draft.status, lead.score)
        assert lead.company.website_no_answer == 1 and lead.company.website_retry_at
    assert got[:3] == ("qualified", "verified", "awaiting_approval"), f"one error answer on the one check a month: {got}"


@pytest.mark.parametrize("kind,status", [("robots500", "blocked"), ("home503", "blocked"), ("home403", "blocked"), ("home404", "unreachable"), ("home500", "unreachable")])
def test_the_real_answer_counts_only_after_repeats_and_is_not_called_silence(make_ctx, kind, status):
    ctx = make_ctx(handler=faulty(kind), polite_delay=0)
    cid = due_company(ctx, "Acme Tools", "acme.example")
    before, seen = tu.CLOCK_OFFSET, []
    try:
        for hours in (0, 21, 42, 63):
            tu.CLOCK_OFFSET = before + timedelta(hours=hours)
            verify(ctx)
            with ctx.db.session() as s:
                seen.append(s.get(Company, cid).website_status)
    finally:
        tu.CLOCK_OFFSET = before
    assert seen == ["verified"] * 3 + [status]
    with ctx.db.session() as s:
        c = s.get(Company, cid)
        assert c.website_no_answer is None and c.website_retry_at is None
        subjects = [a.subject for a in s.execute(select(Activity).where(Activity.company_id == cid)).scalars()]
    assert not any("gave no answer" in x for x in subjects) and any(f"now marked {status}" in x for x in subjects)
    assert "No answer" not in c.validation["website"]


@pytest.mark.parametrize("kind,status", [("robots500", "blocked"), ("home503", "blocked"), ("home403", "blocked"), ("home404", "unreachable")])
def test_a_first_check_stays_as_it_answered(make_ctx, kind, status):
    ctx = make_ctx(handler=faulty(kind), polite_delay=0)
    cid = due_company(ctx, "Acme Tools", "acme.example", status=None)
    verify(ctx)
    with ctx.db.session() as s:
        c = s.get(Company, cid)
        assert c.website_status == status and c.website_checked_at is not None and c.website_retry_at is None


@pytest.mark.parametrize("kind", ["robots500", "home503"])
def test_robots_error_still_means_do_not_crawl_and_the_status_it_gives_is_blocked(make_ctx, kind):
    seen = []
    handler = faulty(kind)

    def spying(request):
        seen.append(request.url.path)
        return handler(request)
    ctx = make_ctx(handler=spying)
    result = check_website(ctx, "acme.example", "Acme Tools")
    assert result.status == "blocked" and result.no_answer and result.error_answer and result.title is None
    if kind == "robots500":
        assert seen == ["/robots.txt"]


@pytest.mark.parametrize("kind", ["home503", "home500", "home403", "disallow", "robots503", "notitle", "challenge"])
def test_an_answer_without_a_title_does_not_lift_a_competitor_flag_shown_by_the_title(make_ctx, kind):
    handler = faulty("robots500" if kind == "robots503" else kind)
    ctx = make_ctx(handler=handler, polite_delay=0)
    now = tu.utcnow()
    with ctx.db.session() as s:
        c = Company(name="Globex", normalized_name="globex", domain="globex.example", website="https://globex.example", state="TX", country="US", source="hn_hiring",
                    website_status="verified", website_checked_at=now - timedelta(days=31), website_title="Globex | Technology Services & Consulting Company",
                    location_text="Austin, TX", is_competitor=True)
        s.add(c)
        s.flush()
        s.add(Signal(company_id=c.id, signal_type="hiring", text="Globex | Engineers | Austin, TX. We have customers and are hiring.", source="hn_hiring",
                     source_url="https://example.test/g", fingerprint="g", observed_at=now - timedelta(days=5), created_at=now - timedelta(days=5)))
        lead = Lead(company_id=c.id, source="research", status="disqualified", disqualify_reason="Company appears to sell software or AI work to clients (possible competitor)")
        s.add(lead)
        s.flush()
        s.add(Activity(company_id=c.id, lead_id=lead.id, actor="crm", type="data_quality", subject='Marked as a possible competitor: it says "Technology Services & Consulting"'))
        lead_id, company_id = lead.id, c.id
    CRMManagementAgent(ctx).run({"operations": ["verify_websites", "competitors"]})
    with ctx.db.session() as s:
        got = (s.get(Company, company_id).is_competitor, s.get(Lead, lead_id).status, s.get(Company, company_id).website_title)
    assert got[0] is True and got[1] == "disqualified" and got[2], f"nothing says it stopped selling such work: {got}"


def test_a_real_new_title_still_replaces_the_old_one(make_ctx):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<title>Acme Tools | Hand tools</title><h1>Acme Tools</h1>", headers=HTML)
    ctx = make_ctx(handler=handler)
    cid = due_company(ctx, "Acme Tools", "acme.example")
    with ctx.db.session() as s:
        s.get(Company, cid).website_title = "Old words"
    verify(ctx)
    with ctx.db.session() as s:
        assert s.get(Company, cid).website_title == "Acme Tools | Hand tools"


# ------------------------------------------------------------------ what kind of page this is
PARKED_LANDER = shell("birchdental.example", """<div id="sponsored"><h2>Related searches</h2>
    <ul><li><a href="/search?q=dental">Dental Insurance Plans</a></li><li><a href="/search?q=implants">Cheap Implants</a></li></ul>
    <p>This webpage was generated by the domain owner using a sponsored listings service. The Sponsored Listings displayed above are served automatically by a third party.</p></div>""")
EXPIRED = shell("Website Expired", "<h1>This website has expired</h1><p>The owner of crestsupply.example needs to renew this site's hosting plan. Account suspended.</p>")
COMING_SOON = shell("Coming Soon", "<h1>Coming soon</h1><p>Our new website is under construction. Powered by a website builder.</p>")
CHALLENGE = shell("Just a moment...", "<h1>Checking your browser before accessing the site.</h1><noscript>Enable JavaScript and cookies to continue</noscript>")
NOTION_JOB = shell("Notion", '<div id="notion-app"></div><script src="/app.js"></script>')


def homepage(make_ctx, host, body):
    web = Web()
    web.hosts[host] = {"/robots.txt": robots_ok(), "/": page(body)}
    return make_ctx(handler=web.handle, polite_delay=0.0)


@pytest.mark.parametrize("body,status", [(PARKED_LANDER, "parked"), (EXPIRED, "parked"), (COMING_SOON, "mismatch"), (CHALLENGE, "blocked")],
                         ids=["lander", "expired", "soon", "waf"])
def test_a_page_that_does_not_show_the_company_is_not_verified(make_ctx, body, status):
    result = check_website(homepage(make_ctx, "crestsupply.example", body), "crestsupply.example", "Crest Supply")
    assert result.status == status, result.note


def test_a_browser_check_gives_no_title(make_ctx):
    assert check_website(homepage(make_ctx, "crestsupply.example", CHALLENGE), "crestsupply.example", "Crest Supply").title is None


def test_a_page_hosted_on_a_listed_board_is_not_the_companys_own_site(make_ctx):
    host = "crestsupply.notion.site"
    result = check_website(homepage(make_ctx, host, NOTION_JOB), host, "Crest Supply")
    assert result.status == "mismatch", result.note


@pytest.mark.parametrize("domain,expected", [("notion.site", True), ("crestsupply.notion.site", True), ("a.b.notion.site", True), ("jobs.lever.co", True),
                                             ("greenhouse.io", True), ("netflix.com", False), ("crestsupply.example", False), ("x.com", True),
                                             ("nox.com", False), ("", False), (None, False)])
def test_is_job_board_matches_a_listed_board_and_its_subdomains_only(domain, expected):
    assert is_job_board(domain) is expected


def test_a_real_page_that_also_says_coming_soon_stays_verified(make_ctx):
    words = " ".join(["Crest Supply sells industrial fasteners and packaging to manufacturers across the Midwest from two warehouses in Ohio."] * 4)
    body = shell("Crest Supply", f"<h1>Crest Supply</h1><p>{words}</p><p>Our new online catalog is coming soon.</p>")
    assert check_website(homepage(make_ctx, "crestsupply.example", body), "crestsupply.example", "Crest Supply").status == "verified"


REAL_TEXT = " ".join(["Crest Supply sells industrial fasteners and packaging to manufacturers across the Midwest from two warehouses in Ohio."] * 20)


@pytest.mark.parametrize("body", [
    shell("Crest Supply", f"<p>{REAL_TEXT}</p><p>Help: account suspended? Call billing.</p>"),
    shell("Crest Supply", f"<p>{REAL_TEXT}</p><p>We email you when your domain has expired.</p>"),
    shell("Crest Supply", f"<p>{REAL_TEXT}</p><p>Compare sponsored listings displayed above the results.</p>"),
    shell("Crest Supply", "<div id='root'></div><script>window.i18n={e:'Account suspended'}</script>"),
    shell("Crest Supply", "<div id='root'></div><script>window.i18n={e:'Sponsored listings service'}</script>"),
    shell("Crest Supply", "<div id='root'></div><script>window.i18n={badge:'Coming soon'}</script>"),
    shell("Crest Supply", "<div id='root'></div><script src='/app.js'></script>", "<meta name=description content='New catalog coming soon'>"),
    shell("Crest Supply", "<div id='root'></div><!-- <p>under construction</p> -->"),
    shell("Crest Supply", "<style>.x:after{content:'website expired'}</style><div id='root'></div>"),
], ids=["help-text", "email-text", "lander-words-in-long-text", "script-suspended", "script-lander", "script-soon", "meta-soon", "comment-soon", "style-expired"])
def test_words_in_passing_or_out_of_sight_do_not_cost_a_live_company_its_status(make_ctx, body):
    result = check_website(homepage(make_ctx, "crestsupply.example", body), "crestsupply.example", "Crest Supply")
    assert result.status == "verified", result.note


@pytest.mark.parametrize("body,status", [
    (shell("Account Suspended", f"<p>{REAL_TEXT}</p>"), "parked"),
    (shell("Crest Supply", "<script>" + "var word = 1; " * 500 + "</script><h1>Account  suspended</h1><p>Contact your host.</p>"), "parked"),
    (shell("crestsupply.example", "<p>This <b>domain</b> has expired.</p>"), "parked"),
    (shell("Crest Supply", "<h1>This <b>domain has expired</b></h1>"), "parked"),
    (shell("crestsupply.example", "<p>" + "Related searches here. " * 25 + "The sponsored listings displayed above are served by a third party.</p>"), "parked"),
    (shell("Crest Supply", "<h1>Coming soon</h1><script>" + "var word = 1; " * 500 + "</script>"), "mismatch"),
    # the older markers were always read from the whole page, scripts too, and stay so
    (shell("Crest Supply", f"<p>{REAL_TEXT}</p><script>window.ad='this domain is for sale'</script>"), "parked"),
], ids=["title", "short-page-past-a-script", "words-split-by-a-tag", "marker-in-a-tag", "lander-with-long-disclaimer", "soon-past-a-script", "older-marker"])
def test_a_page_that_really_is_gone_is_still_seen(make_ctx, body, status):
    result = check_website(homepage(make_ctx, "crestsupply.example", body), "crestsupply.example", "Crest Supply")
    assert result.status == status, result.note


def test_a_coming_soon_page_with_a_big_script_is_still_a_coming_soon_page(make_ctx):
    body = shell("Coming Soon", "<script>" + "var word = 1; " * 500 + "</script><h1>Coming soon</h1>")
    assert check_website(homepage(make_ctx, "crestsupply.example", body), "crestsupply.example", "Crest Supply").status == "mismatch"


@pytest.mark.parametrize("body", ["coming soon " + "<script" * 40_000, "coming soon " + "<" * 290_000, "coming soon " + "<style>" * 40_000, "coming soon " + "<!--" * 70_000],
                         ids=["script", "angle", "style", "comment"])
def test_a_page_built_against_the_coming_soon_check_costs_one_pass(make_ctx, body):
    started = time.perf_counter()
    check_website(homepage(make_ctx, "crestsupply.example", body), "crestsupply.example", "Crest Supply")
    assert time.perf_counter() - started < 3


def test_the_note_says_what_matched(make_ctx):
    named = check_website(homepage(make_ctx, "crestsupply.example", shell("Crest Supply | Home", "<h1>Hi</h1>")), "crestsupply.example", "Crest Supply")
    assert (named.status, named.note) == ("verified", "Homepage names the company")
    spelled = check_website(homepage(make_ctx, "crestsupply.example", shell("Loading...", "<div id=root></div>")), "crestsupply.example", "Crest Supply")
    assert spelled.status == "verified" and "the page does not confirm it" in spelled.note and "the domain spells the name" in spelled.note.lower()


# ------------------------------------------------------------------ an older encoding
LATIN1_HOME = ('<html><head><meta charset="iso-8859-1"><title>Hogar Café Ñandú</title>'
               '<meta name="description" content="Café Ñandú vende café de especialidad en México y Estados Unidos desde hace treinta años, con tostado propio."></head>'
               '<body><h1>Café Ñandú</h1><p>Somos una empresa familiar de café de especialidad, con tostado propio y envíos a todo México.</p>'
               '<a href="mailto:hola@hogarcafe.example">hola@hogarcafe.example</a></body></html>').encode("latin-1")


def test_a_page_in_a_declared_older_encoding_is_not_garbled(make_ctx):
    web = Web()
    web.hosts["hogarcafe.example"] = {"/robots.txt": robots_ok(), "/": page(LATIN1_HOME, ctype="text/html")}
    thread(web, 1, [post(1, f"Hogar Cafe | {HEADER}", links=["https://hogarcafe.example/"])])
    ctx = make_ctx(handler=web.handle, polite_delay=0.0)
    full_day(ctx)
    with ctx.db.session() as s:
        d = s.execute(select(ResearchSnapshot.description).where(ResearchSnapshot.description.is_not(None))).scalars().first()
    assert d and "�" not in d and "Ñandú" in d, d


@pytest.mark.parametrize("charset,raw", [("nonsense-charset", b"caf\xe9"), ("utf-16", b"caf\xc3\xa9"), ("zlib", b"caf\xc3\xa9"), ("idna", b"caf\xc3\xa9")])
def test_a_strange_declared_charset_does_not_break_the_read(make_ctx, charset, raw):
    body = f'<html><head><meta charset="{charset}"><title>Crest Supply</title></head><body>'.encode() + raw + b"</body></html>"
    assert check_website(homepage(make_ctx, "crestsupply.example", page(body, ctype="text/html")["body"]), "crestsupply.example", "Crest Supply").status == "verified"


def test_a_header_charset_still_wins_over_the_page(make_ctx):
    body = '<html><head><meta charset="iso-8859-1"><title>Crest Café</title></head></html>'.encode()
    web = Web()
    web.hosts["crestcafe.example"] = {"/robots.txt": robots_ok(), "/": page(body, ctype="text/html; charset=utf-8")}
    assert check_website(make_ctx(handler=web.handle), "crestcafe.example", "Crest Cafe").title == "Crest Café"
