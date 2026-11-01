"""Phase 5, closed companies: what a person closed, a won customer and an opt-out stay that way until a person changes them, and SEC filings
make no lead. Synthetic data only, fake web and fake mail only."""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import aime.agents.research as research_mod
import aime.timeutil
from aime.agents.company_research import CompanyResearchAgent
from aime.agents.crm import CRMManagementAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.research import USBusinessResearchAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.crm_ops import lift_suppression, log_activity, merge_companies, suppress
from aime.inbound import record_inquiry
from aime.integrations.email_smtp import EmailSender
from aime.models import Activity, Alert, Company, Contact, FollowUp, InboundEmail, Lead, Signal, Suppression
from aime.outreach import ALREADY_CUSTOMER, COMPANY_OPTED_OUT, company_opted_out, eligible, reopen_refusal, stop_sequence
from aime.sources.base import CompanyCandidate
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.workflows import job_research
from tests.test_company_research import CONTACT, HOME, SITE, add_company, make_handler
from tests.test_contact_validation import FakeChecker
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID, MAIL, FakeIMAP, bounce_for, contacted, mail, run_inbox
from tests.test_outreach import POST, approve_all, drafts, lead_of
from tests.uat.test_uat_email import ORIGIN, web
from tests.uat.test_uat_sales_journey import make_deal

NOW = TUESDAY_11AM_ET
INBOX = f"info@{SITE}"
MARIA = f"maria.lopez@{SITE}"
PROOF = "https://news.example/item?id="
TEAM_NO_ADDRESS = "<html><body><h2>Our Team</h2><div class='card'><h3>David Chen</h3><p>Chief Operating Officer</p></div></body></html>"
TEAM_WITH_ADDRESS = ("<html><body><h2>Our Team</h2><div class='card'><h3>David Chen</h3><p>Chief Operating Officer</p></div>"
                     f"<p>David Chen</p><p>Email: <a href='mailto:{DAVID}'>{DAVID}</a></p></body></html>")
TEAM_TWO_ADDRESSES = TEAM_WITH_ADDRESS.replace("</body>", "<div class='card'><h3>Maria Lopez</h3><p>Chief Executive Officer</p></div>"
                                               f"<p>Maria Lopez</p><p>Email: <a href='mailto:{MARIA}'>{MARIA}</a></p></body>")
NEED = "We are looking for an agency or outside development partner to build a custom CRM, AI agents and sales automation. Our team still does manual data entry in spreadsheets and follow-ups get missed."


class FakeSource:
    countries = ("US",)

    def __init__(self, name, cands):
        self.name, self.cands = name, cands

    def configured(self):
        return True, "ok"

    def fetch(self, focus):
        return self.cands


def listing(name, domain=None, post=NEED, item=77, source="hn_hiring", state="TX", signal_type="hiring", record=None, description=None):
    return CompanyCandidate(name=name, source=source, source_url=f"{PROOF}{item}", source_record_id=record or f"{source}:{name}", website=f"https://{domain}" if domain else None,
                            domain=domain, state=state, location_text="Austin, TX", is_us=True, signal_type=signal_type, observed_at=datetime(2026, 10, 30),
                            signal_text=f"{name} | Remote US\n\n{post}", description=description)


def read_listing(ctx, *cands, source="hn_hiring", **inp):
    keep = research_mod.get_sources
    research_mod.get_sources = lambda _c, _n=None: [FakeSource(source, list(cands))]
    try:
        res = USBusinessResearchAgent(ctx).run({"sources": [source], **inp})
    finally:
        research_mod.get_sources = keep
    assert res.ok, res.error
    return res.output


def repost(ctx, company_id, item=77):
    """A later hiring thread names the same company: what the research job does with it. The same item again is the morning re-read."""
    with ctx.db.session() as s:
        c = s.get(Company, company_id)
        name, domain, state = c.name, c.domain, c.state
    return read_listing(ctx, listing(name, domain, item=item, state=state, record=f"hn:{item}"))


def next_month_runs(ctx, company_id, mail_checker=None):
    """The site is read again and the jobs run: only agents, nothing patched."""
    with ctx.db.session() as s:
        new = s.execute(select(Lead.id).where(Lead.company_id == company_id).order_by(Lead.id.desc())).scalars().first()
    LeadQualificationAgent(ctx).run({"lead_ids": [new]})
    assert CompanyResearchAgent(ctx, mail_checker=mail_checker or FakeChecker({})).run({"company_ids": [company_id]}).ok
    LeadQualificationAgent(ctx).run({"lead_ids": [new]})
    SalesWorkflowAgent(ctx).run({"send": False})


def waiting_first_emails(ctx):
    with ctx.db.session() as s:
        return s.execute(select(FollowUp).where(FollowUp.kind == "outreach", FollowUp.step == 1, FollowUp.status == "awaiting_approval")).scalars().all()


def company_of(ctx):
    return lead_of(ctx).company_id


def leads_at(ctx, company_id):
    with ctx.db.session() as s:
        return s.execute(select(Lead).where(Lead.company_id == company_id).order_by(Lead.id)).scalars().all()


def alerts_about(ctx, lead_id):
    with ctx.db.session() as s:
        return s.execute(select(Alert).where(Alert.key == f"listing_on_closed:{lead_id}").order_by(Alert.id)).scalars().all()


def close_by_person(ctx, lead_id):
    r = web(ctx).post(f"/leads/{lead_id}/status", data={"status": "disqualified", "note": "not a fit"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303


def win(ctx, lead_id, owner=None):
    oid = make_deal(ctx, lead_id, owner=owner, stage="negotiation", value=1000.0)
    web(ctx).post(f"/opportunities/{oid}", data={"stage": "won", "shown_stage": "negotiation"}, headers=ORIGIN)
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).status == "converted"
    return oid


def opt_out_by_reply(ctx, sent, who=DAVID):
    FakeIMAP.messages = {1: mail(f"Someone <{who}>", f"Re: {sent.subject}", "Please unsubscribe us.", in_reply_to=sent.message_id)}
    run_inbox(ctx)


def site_with(team):
    """An emailed lead whose company publishes `team`, and the means to change the page, as the 30-day re-read finds it."""
    requests: list[str] = []
    pages = {"/team": ("text/html", team), "/": ("text/html", HOME), "/contact": ("text/html", CONTACT)}
    return requests, pages


def emailed(make_ctx, team=TEAM_NO_ADDRESS):
    requests, pages = site_with(team)
    ctx = make_ctx(handler=make_handler(requests, pages), **MAIL)
    cid = add_company(ctx)
    with ctx.db.session() as s:
        s.add(Signal(company_id=cid, signal_type="hiring", text=POST, source="hn_hiring", source_url=f"{PROOF}123",
                     fingerprint="post-1", keywords=["manual data entry", "spreadsheets"], strength=0.8))
    assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]}).ok
    FakeSMTP.sent, FakeIMAP.messages = [], {}
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": NOW.isoformat()})
    approve_all(ctx)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": NOW.isoformat()})
    (sent,) = [f for f in drafts(ctx) if f.status == "sent"]

    def site_now_shows(new_team):
        pages["/team"] = ("text/html", new_team)
        ctx.http_transport = httpx.MockTransport(make_handler(requests, pages))
    return ctx, sender, sent, cid, site_now_shows


# ------------------------------------------------------------------ the testers' cases
def test_a_lead_a_person_disqualified_does_not_come_back_with_a_new_first_email(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    close_by_person(ctx, lid)
    repost(ctx, cid)
    next_month_runs(ctx, cid)
    assert waiting_first_emails(ctx) == []


def test_a_won_customer_is_not_sent_a_cold_first_email_when_it_posts_a_job(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    win(ctx, lid)
    repost(ctx, cid)
    next_month_runs(ctx, cid)
    assert waiting_first_emails(ctx) == []


def test_a_company_that_opted_out_gets_no_first_email_to_a_colleague_after_a_repost(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    cid = company_of(ctx)
    with ctx.db.session() as s:
        s.add(Contact(company_id=cid, email=MARIA, first_name="Maria", last_name="Lopez", source="website", role_category="executive", email_status="published",
                      email_source_url=f"https://{SITE}/team", consent_status="none", confidence="high", email_confidence="high", relevance=999))
    opt_out_by_reply(ctx, sent)
    LeadQualificationAgent(ctx).run({"lead_ids": [lead_of(ctx).id]})
    repost(ctx, cid)
    next_month_runs(ctx, cid)
    assert waiting_first_emails(ctx) == []
    assert len(leads_at(ctx, cid)) == 1


# The discovery tester's cases: a fake Hacker News and fake company sites, and the days pass through the morning job.
def page(body, ctype="text/html; charset=utf-8"):
    return {"body": body, "ctype": ctype}


def shell(title, body, meta=""):
    return f"<!doctype html><html><head><title>{title}</title>{meta}</head><body>{body}</body></html>"


def store(name, dom):
    words = f"{name} sells home goods through its own online store and ships to every state from a warehouse in Ohio since 2009."
    home = shell(f"{name} | Online store", f'<nav><a href="/team">Team</a><a href="/contact">Contact</a></nav><h1>{name}</h1><p>{words}</p><a href="mailto:info@{dom}">info@{dom}</a>',
                 f'<meta name="description" content="{words}">')
    team = shell("Team", '<div><h3>Rhea Stone</h3><p>Chief Executive Officer</p></div>' + f'<div><h3>Cole Vance</h3><p>VP of Sales</p><p><a href="mailto:cole.vance@{dom}">cole.vance@{dom}</a></p></div>')
    return {"/robots.txt": page("User-agent: *\nAllow: /\n", "text/plain"), "/": page(home), "/team": page(team), "/contact": page(shell("Contact", "<p>Write to us.</p>"))}


FILLER = ("We are a growing company and we still copy orders from email into spreadsheets before invoicing. "
          "We want to automate that manual data entry and build internal tools for our operations team.")


def world(make_ctx, name, dom):
    sites = {dom: store(name, dom)}
    href = f"https://{dom}/"
    text = f'{name} | Senior Engineer | Columbus, OH | ONSITE | Full-time | <a href="{href}" rel="nofollow">{href}</a><p>{FILLER}'
    item = {"id": 9_000_000, "type": "comment", "author": "someone", "created_at": "2026-10-26T15:00:00Z", "text": text}

    def handler(req):
        host = req.url.host.lower().removeprefix("www.")
        if host == "hn.algolia.com":
            if req.url.path.endswith("/search_by_date"):
                return httpx.Response(200, json={"hits": [{"objectID": "777001", "title": "Ask HN: Who is hiring? (November 2026)"}]})
            return httpx.Response(200, json={"children": [item]})
        if host in ("www.sec.gov", "sec.gov", "data.sec.gov"):
            return httpx.Response(200, text='<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>', headers={"content-type": "application/atom+xml"})
        if host not in sites:
            raise httpx.ConnectError(f"name not found {host}", request=req)
        got = sites[host].get(req.url.path) or sites[host].get(req.url.path.rstrip("/"))
        if got is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        return httpx.Response(200, content=got["body"].encode(), headers={"content-type": got["ctype"]})
    return make_ctx(handler=handler, polite_delay=0.0)


def full_day(ctx):
    job_research(ctx)
    CompanyResearchAgent(ctx, mail_checker=lambda domain: True).run({"limit": 15})
    LeadQualificationAgent(ctx).run({})
    SalesWorkflowAgent(ctx).run({"send": False})


def days_later(n):
    aime.timeutil.CLOCK_OFFSET += timedelta(days=n)


def named(s, name):
    return s.execute(select(Company).where(Company.normalized_name == name.lower())).scalars().first()


def leads_of(s, name):
    return s.execute(select(Lead).where(Lead.company_id == named(s, name).id).order_by(Lead.id)).scalars().all()


def close_by_hand(ctx, name, status):
    with ctx.db.session() as s:
        lead = leads_of(s, name)[-1]
        old, lead.status = lead.status, status
        log_activity(s, f"Status {old} → {status} (manual)", "owner", type_="status_change", company_id=lead.company_id, lead_id=lead.id)
        for f in s.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.status.in_(("awaiting_approval", "approved", "pending")))).scalars():
            f.status = "cancelled"


@pytest.mark.parametrize("status", ["disqualified", "converted"])
def test_a_closed_or_won_company_is_not_added_back_by_the_next_discovery(make_ctx, status):
    ctx = world(make_ctx, "Alder Retail", "alderretail.example")
    full_day(ctx)
    with ctx.db.session() as s:
        assert leads_of(s, "Alder Retail")[-1].status == "qualified"
    close_by_hand(ctx, "Alder Retail", status)
    days_later(1)
    full_day(ctx)
    with ctx.db.session() as s:
        assert [x.status for x in leads_of(s, "Alder Retail")] == [status]


def test_the_closed_company_gets_no_draft_a_month_later(make_ctx):
    ctx = world(make_ctx, "Alder Retail", "alderretail.example")
    full_day(ctx)
    close_by_hand(ctx, "Alder Retail", "disqualified")
    days_later(1)
    full_day(ctx)
    days_later(31)
    full_day(ctx)
    with ctx.db.session() as s:
        cid = named(s, "Alder Retail").id
        assert not s.execute(select(FollowUp).where(FollowUp.company_id == cid, FollowUp.channel == "email", FollowUp.status == "awaiting_approval")).scalars().all()


def test_an_opted_out_company_is_not_reopened_with_a_message_to_paste(make_ctx):
    ctx = world(make_ctx, "Aspen Retail", "aspenretail.example")
    full_day(ctx)
    with ctx.db.session() as s:
        lead = leads_of(s, "Aspen Retail")[-1]
        suppress(s, lead.contact.email, "Unsubscribe request by email")
        stop_sequence(s, lead, "unsubscribed", "Asked not to be contacted", "inbox_monitor")
    for _ in range(2):  # day 1: scoring closes the lead. Day 2: discovery finds the company with no open lead.
        days_later(1)
        full_day(ctx)
    with ctx.db.session() as s:
        cid = named(s, "Aspen Retail").id
        still_open = [x.id for x in leads_of(s, "Aspen Retail") if x.status in ("new", "qualified", "nurture")]
        tasks = s.execute(select(FollowUp.subject).where(FollowUp.company_id == cid, FollowUp.channel == "task", FollowUp.status == "pending")).scalars().all()
    assert not still_open and not tasks


def test_a_do_not_contact_domain_does_not_grow_one_lead_a_day(make_ctx):
    ctx = world(make_ctx, "Dogwood Retail", "dogwoodretail.example")
    full_day(ctx)
    with ctx.db.session() as s:
        suppress(s, "dogwoodretail.example", "Do not contact")
    LeadQualificationAgent(ctx).run({})
    for _ in range(3):
        days_later(1)
        full_day(ctx)
    with ctx.db.session() as s:
        assert len(leads_of(s, "Dogwood Retail")) == 1


def test_a_morning_with_nothing_new_adds_nothing(make_ctx):
    ctx = world(make_ctx, "Plain Retail", "plainretail.example")
    full_day(ctx)

    def counts():
        with ctx.db.session() as s:
            return [s.execute(select(func.count(t.id))).scalar_one() for t in (Company, Lead, Contact, FollowUp)]
    first = counts()
    full_day(ctx)
    days_later(1)
    full_day(ctx)
    assert counts() == first


# ------------------------------------------------------------------ flagged at discovery
BOILERPLATE = "We sell accounting software to dentists. No agencies, staff augmentation firms or recruiters, please."
TRUE_COMPETITOR = "We are a software development agency building custom software for clients."


def test_a_company_flagged_at_discovery_gets_a_lead_a_reason_and_a_way_to_clear_it(make_ctx):
    ctx = make_ctx()
    read_listing(ctx, listing("Synthprod", "synthprod.example", post=BOILERPLATE))
    with ctx.db.session() as s:
        co = s.execute(select(Company).where(Company.normalized_name == "synthprod")).scalar_one()
        marks = s.execute(select(Activity.subject).where(Activity.company_id == co.id, Activity.subject.like("%competitor%"))).scalars().all()
        leads = s.execute(select(Lead.id).where(Lead.company_id == co.id)).scalars().all()
    assert leads or not co.is_competitor
    assert marks or not co.is_competitor


def test_a_true_competitor_gets_one_closed_lead_its_words_and_no_email(make_ctx):
    ctx = make_ctx()
    read_listing(ctx, listing("Devshop", "devshop.example", post=TRUE_COMPETITOR))
    LeadQualificationAgent(ctx).run({})
    with ctx.db.session() as s:
        co = s.execute(select(Company).where(Company.normalized_name == "devshop")).scalar_one()
        cid = co.id
        (mark,) = s.execute(select(Activity).where(Activity.company_id == cid, Activity.subject.like("Marked as a possible competitor%"))).scalars().all()
    assert co.is_competitor and "software development" in mark.subject.lower() and mark.actor == "research"
    (lead,) = leads_at(ctx, cid)
    assert lead.status == "disqualified" and "competitor" in lead.disqualify_reason
    assert "not_competitor" in web(ctx).get(f"/leads/{lead.id}").text

    for item in (78, 79):  # later posts and re-reads make no second lead and no second record of the words
        repost(ctx, cid, item=item)
        repost(ctx, cid, item=item)
    LeadQualificationAgent(ctx).run({})
    SalesWorkflowAgent(ctx).run({"send": False})
    assert len(leads_at(ctx, cid)) == 1 and waiting_first_emails(ctx) == []
    with ctx.db.session() as s:
        assert s.execute(select(func.count(Activity.id)).where(Activity.company_id == cid, Activity.subject.like("Marked as%"))).scalar_one() == 1

    assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).ok  # the daily review: its own words still say so
    with ctx.db.session() as s:
        assert s.get(Company, cid).is_competitor


def test_a_flag_set_from_an_imported_row_survives_the_daily_review(make_ctx):
    """The review reads hiring posts and the description, so a Marked line written for words it cannot read would lift a true flag."""
    ctx = make_ctx()
    cand = listing("Devshop", "devshop.example", post=TRUE_COMPETITOR, source="csv_import", signal_type="import", description=None)
    cand.extra = {"import_file": "people.csv"}
    read_listing(ctx, cand, source="csv_import")
    assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).ok
    with ctx.db.session() as s:
        co = s.execute(select(Company).where(Company.normalized_name == "devshop")).scalar_one()
        assert co.is_competitor


def test_clearing_a_discovery_flag_says_something_true(make_ctx):
    ctx = make_ctx()
    read_listing(ctx, listing("Synthprod", "synthprod.example", post=BOILERPLATE))
    with ctx.db.session() as s:
        cid = s.execute(select(Company.id).where(Company.normalized_name == "synthprod")).scalar_one()
    LeadQualificationAgent(ctx).run({})
    r = TestClient(create_app(ctx)).post(f"/companies/{cid}/not_competitor", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    (lead,) = leads_at(ctx, cid)
    assert lead.status != "disqualified"


# ------------------------------------------------------------------ what a closed company's new post does
def test_an_identical_re_read_raises_nothing_and_a_new_post_raises_one_notice(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    repost(ctx, cid)  # on file before the close
    close_by_person(ctx, lid)
    out = repost(ctx, cid)
    assert (out.signals_added, out.leads_created) == (0, 0)
    assert alerts_about(ctx, lid) == []

    out = repost(ctx, cid, item=78)
    assert (out.signals_added, out.leads_created) == (1, 0)
    (alert,) = alerts_about(ctx, lid)
    assert f"#{lid}" in alert.message and "Nothing was reopened" in alert.message and "a person decides" in alert.message
    with ctx.db.session() as s:
        assert s.execute(select(func.count(Activity.id)).where(Activity.lead_id == lid, Activity.subject.like("New listing%"))).scalar_one() == 1
    repost(ctx, cid, item=78)
    assert len(alerts_about(ctx, lid)) == 1


def test_a_dismissed_notice_stays_dismissed_on_a_re_read(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    close_by_person(ctx, lid)
    repost(ctx, cid)
    (alert,) = alerts_about(ctx, lid)
    with ctx.db.session() as s:
        row = s.get(Alert, alert.id)
        row.acknowledged, row.acknowledged_at = True, utcnow()
    days_later(3)
    repost(ctx, cid)
    (alert,) = alerts_about(ctx, lid)
    assert alert.acknowledged


def test_a_won_customers_new_post_goes_to_the_deals_owner_as_one_task(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    win(ctx, lid, owner="eva")

    def tasks():
        with ctx.db.session() as s:
            return s.execute(select(FollowUp).where(FollowUp.lead_id == lid, FollowUp.channel == "task", FollowUp.subject.like("New listing%"))).scalars().all()
    repost(ctx, cid)
    (task,) = tasks()
    assert "Deal owner: eva" in task.body and task.status == "pending" and "reopen" in task.body.lower()
    repost(ctx, cid)  # the same post
    repost(ctx, cid, item=78)  # a new post while the task is still open
    assert len(tasks()) == 1
    assert len(leads_at(ctx, cid)) == 1


def test_a_company_closed_by_an_opt_out_is_never_invited_to_reopen(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    opt_out_by_reply(ctx, sent)
    LeadQualificationAgent(ctx).run({"lead_ids": [lid]})
    assert lead_of(ctx).status == "disqualified"
    repost(ctx, cid)
    (alert,) = alerts_about(ctx, lid)
    assert "stays closed" in alert.message and "a person decides" not in alert.message and "reopen it" not in alert.message


def test_a_lead_aime_closed_stays_closed_with_only_a_record(make_ctx):
    ctx = make_ctx()
    read_listing(ctx, listing("Harbor Goods", "harbor.example"))
    with ctx.db.session() as s:
        cid = s.execute(select(Company.id).where(Company.normalized_name == "harbor goods")).scalar_one()
        lead = s.execute(select(Lead).where(Lead.company_id == cid)).scalars().one()
        lead.status, lead.disqualify_reason = "disqualified", "Outside US target market"
        lid = lead.id
        log_activity(s, "Status new → disqualified", "qualification", type_="status_change", company_id=cid, lead_id=lid)
    out = repost(ctx, cid, item=78)
    assert out.leads_created == 0 and alerts_about(ctx, lid) == []
    with ctx.db.session() as s:
        assert s.execute(select(func.count(Activity.id)).where(Activity.lead_id == lid, Activity.subject.like("New listing%"))).scalar_one() == 1


def csv_rows(ctx, *rows, name="people.csv"):
    ctx.settings.imports_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.settings.imports_dir / name
    path.write_text("\n".join(["name,website,state,industry,notes", *rows]) + "\n", encoding="utf-8")
    return path


def test_a_persons_own_csv_row_for_a_closed_company_is_told_and_the_file_is_kept(make_ctx):
    ctx = make_ctx()
    read_listing(ctx, listing("Harbor Goods", "harbor.example"))
    with ctx.db.session() as s:
        cid = s.execute(select(Company.id).where(Company.normalized_name == "harbor goods")).scalar_one()
    (lead,) = leads_at(ctx, cid)
    close_by_person(ctx, lead.id)
    csv_rows(ctx, "Harbor Goods,https://harbor.example,TX,saas,From the trade show list")
    res = USBusinessResearchAgent(ctx).run({"sources": ["csv_import"]})
    assert res.ok and res.output.import_files_rejected == [] and res.output.import_files_archived == 1
    assert res.output.leads_created == 0
    assert any("already closed, reopen it on its lead page" in n for n in res.output.import_notices)
    assert len(leads_at(ctx, cid)) == 1


def test_a_csv_row_for_an_opted_out_company_does_not_invite_a_reopen(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    opt_out_by_reply(ctx, sent)
    LeadQualificationAgent(ctx).run({"lead_ids": [lid]})
    with ctx.db.session() as s:
        co = s.get(Company, cid)
        name, domain = co.name, co.domain
    csv_rows(ctx, f"{name},https://{domain},TX,saas,From the trade show list")
    res = USBusinessResearchAgent(ctx).run({"sources": ["csv_import"]})
    assert res.ok and res.output.leads_created == 0
    (notice,) = res.output.import_notices
    assert "stays closed" in notice and "reopen" not in notice


def test_a_new_post_on_a_day_past_the_company_cap_still_raises_its_notice(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    close_by_person(ctx, lid)
    with ctx.db.session() as s:
        co = s.get(Company, cid)
        name, domain, state = co.name, co.domain, co.state
    out = read_listing(ctx, listing("Brand New Co", "brandnew.example"), listing(name, domain, item=78, state=state, record="hn:78"), limit=1)
    assert out.limit_reached and out.signals_added == 2
    assert len(alerts_about(ctx, lid)) == 1


# ------------------------------------------------------------------ what stays as it was
def test_an_inquiry_after_a_close_still_opens_a_lead(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    close_by_person(ctx, lid)
    with ctx.db.session() as s:
        name, domain = s.get(Company, cid).name, s.get(Company, cid).domain
    with ctx.db.session() as s:
        record_inquiry(s, {"company": name, "website": f"https://{domain}", "name": "Pat Reed", "email": f"pat.reed@{domain}", "state": "TX", "message": "Can you help with a CRM?", "consent": True}, "form")
    assert CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]}).ok
    assert [x.status for x in leads_at(ctx, cid)][0] == "disqualified"
    new = [x for x in leads_at(ctx, cid) if x.id != lid]
    assert len(new) == 1 and new[0].source == "inbound" and new[0].status in ("new", "qualified", "nurture")


def test_a_person_can_restore_the_closed_lead_from_its_page(make_ctx):
    ctx = make_ctx()
    read_listing(ctx, listing("Harbor Goods", "harbor.example"))
    with ctx.db.session() as s:
        cid = s.execute(select(Company.id).where(Company.normalized_name == "harbor goods")).scalar_one()
    (lead,) = leads_at(ctx, cid)
    close_by_person(ctx, lead.id)
    repost(ctx, cid, item=78)
    assert [x.status for x in leads_at(ctx, cid)] == ["disqualified"]
    r = web(ctx).post(f"/leads/{lead.id}/status", data={"status": "new", "note": ""}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert [x.status for x in leads_at(ctx, cid)] == ["new"]
    out = repost(ctx, cid, item=79)  # worked as any open lead again
    assert out.leads_created == 0 and [x.status for x in leads_at(ctx, cid)] == ["new"]


def test_a_company_with_an_open_inbound_lead_is_as_it_was(make_ctx):
    """Open inbound lead: research adds its own lead, as it does today. Nothing reports a closed company."""
    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="Harbor Goods", normalized_name="harbor goods", source="inbound", domain="harbor.example", state="TX", country="US")
        s.add(co)
        s.flush()
        s.add(Lead(company_id=co.id, source="inbound", status="new"))
        s.add(Lead(company_id=co.id, source="research", status="disqualified", disqualify_reason="not a fit"))
        cid = co.id
    out = read_listing(ctx, listing("Harbor Goods", "harbor.example"))
    assert out.leads_created == 1
    assert sorted((x.source, x.status) for x in leads_at(ctx, cid)) == [("inbound", "new"), ("research", "disqualified"), ("research", "new")]
    with ctx.db.session() as s:
        assert s.execute(select(func.count(Alert.id)).where(Alert.key.like("listing_on_closed%"))).scalar_one() == 0


@pytest.mark.parametrize("closed_by", ["win", "opt-out"])
def test_someone_who_writes_in_from_a_won_or_opted_out_company_is_still_answered(make_ctx, closed_by):
    """The inbound branch of the sales run does not ask eligible()."""
    ctx, _s, sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    if closed_by == "win":
        win(ctx, lid)
    else:
        opt_out_by_reply(ctx, sent)
    with ctx.db.session() as s:
        name, domain = s.get(Company, cid).name, s.get(Company, cid).domain
        record_inquiry(s, {"company": name, "website": f"https://{domain}", "name": "Pat Reed", "email": f"pat.reed@{domain}", "state": "TX", "message": "Can you help with a CRM?", "consent": True}, "form")
    assert CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]}).ok
    LeadQualificationAgent(ctx).run({})
    SalesWorkflowAgent(ctx).run({"send": False, "now": NOW.isoformat()})
    with ctx.db.session() as s:
        (answer,) = s.execute(select(FollowUp).where(FollowUp.lead_id != lid, FollowUp.channel == "email")).scalars().all()
    assert answer.kind != "outreach" and answer.status == "awaiting_approval" and "inquiry" in answer.subject


def test_an_emailed_lead_closed_by_a_person_gets_no_new_step_one(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    close_by_person(ctx, lid)
    repost(ctx, cid, item=78)
    next_month_runs(ctx, cid)
    with ctx.db.session() as s:
        ones = s.execute(select(FollowUp).where(FollowUp.company_id == cid, FollowUp.kind == "outreach", FollowUp.step == 1)).scalars().all()
    assert [f.id for f in ones] == [sent.id]
    assert len(leads_at(ctx, cid)) == 1


def test_a_closed_lead_moved_to_a_keeper_by_a_merge_blocks_the_keepers_new_research_lead(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    close_by_person(ctx, lid)
    with ctx.db.session() as s:
        keeper = Company(name="Keeper Co", normalized_name="keeper co", source="test", domain="keeper.example", state="TX", country="US")
        s.add(keeper)
        s.flush()
        kid = keeper.id
        merge_companies(s, keeper, s.get(Company, cid), "crm")
    out = read_listing(ctx, listing("Keeper Co", "keeper.example"))
    assert out.leads_created == 0
    assert [x.status for x in leads_at(ctx, kid) if x.status != "merged"] == ["disqualified"]


# ------------------------------------------------------------------ the address published later, and an opt-out at the company
def test_an_address_published_later_gets_no_first_email_after_an_opt_out(make_ctx):
    """crm-1c by its natural path: nothing hand-made and no raised relevance. The person who opted out and a colleague published a month later."""
    ctx, _s, sent, cid, site_now_shows = emailed(make_ctx, TEAM_WITH_ADDRESS)
    assert sent.to_email == DAVID
    opt_out_by_reply(ctx, sent)
    LeadQualificationAgent(ctx).run({})
    repost(ctx, cid)
    site_now_shows(TEAM_TWO_ADDRESSES)
    next_month_runs(ctx, cid)
    assert waiting_first_emails(ctx) == []
    assert [x.status for x in leads_at(ctx, cid)] == ["disqualified"]
    with ctx.db.session() as s:
        assert company_opted_out(s, s.get(Company, cid))


def test_an_opted_out_inbox_then_a_named_person_gets_no_first_email(make_ctx):
    ctx, _s, sent, cid, site_now_shows = emailed(make_ctx)
    assert sent.to_email == INBOX
    opt_out_by_reply(ctx, sent, who=INBOX)
    LeadQualificationAgent(ctx).run({})
    repost(ctx, cid)
    site_now_shows(TEAM_WITH_ADDRESS)
    next_month_runs(ctx, cid)
    assert waiting_first_emails(ctx) == []


def test_a_bounce_does_not_close_the_company(make_ctx):
    """A dead address is nobody's no: a working colleague can still get a first email, through a new lead as before."""
    ctx, _s, sent, cid, site_now_shows = emailed(make_ctx)
    FakeIMAP.messages = {1: bounce_for(INBOX, sent.message_id)}
    run_inbox(ctx)
    with ctx.db.session() as s:
        contact = s.execute(select(Contact).where(Contact.email == INBOX)).scalars().one()
        assert contact.email_status == "bounced"
        assert s.execute(select(Suppression.reason).where(Suppression.value == INBOX)).scalar_one().startswith("Bounced")
        assert not company_opted_out(s, s.get(Company, cid))
    LeadQualificationAgent(ctx).run({})
    (first,) = leads_at(ctx, cid)
    assert first.status == "disqualified"
    # Nobody else on file: a new lead would only close again the same way.
    assert repost(ctx, cid).leads_created == 0
    site_now_shows(TEAM_WITH_ADDRESS)
    assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]}).ok
    assert repost(ctx, cid, item=78).leads_created == 1
    (_, second) = leads_at(ctx, cid)
    next_month_runs(ctx, cid)
    with ctx.db.session() as s:
        assert s.get(Lead, second.id).contact.email == DAVID
    (draft,) = waiting_first_emails(ctx)
    assert draft.to_email == DAVID
    assert repost(ctx, cid, item=79).leads_created == 0  # an open lead again: no third


def test_an_opt_out_from_someone_with_no_contact_row_counts(make_ctx):
    """Their mail stops the sequence and suppresses only their own address, and it marks no contact."""
    ctx, _s, _sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    stranger = f"pat.reed@{SITE}"
    FakeIMAP.messages = {1: mail(f"Pat Reed <{stranger}>", "Your email", "Please remove us from your list.")}
    run_inbox(ctx)
    with ctx.db.session() as s:
        assert not s.execute(select(Contact).where(Contact.consent_status == "opted_out")).scalars().all()
        assert not s.execute(select(Contact).where(Contact.email == stranger)).scalars().all()
        assert company_opted_out(s, s.get(Company, cid))
        colleague = Contact(company_id=cid, email=MARIA, first_name="Maria", last_name="Lopez", source="website", role_category="executive", email_status="published",
                            email_source_url=f"https://{SITE}/team", consent_status="none", confidence="high", email_confidence="high")
        s.add(colleague)
        s.flush()
        other = Lead(company_id=cid, contact_id=colleague.id, source="research", status="qualified", score=70, grade="B", primary_service="custom_crm")
        s.add(other)
        s.flush()
        assert eligible(s, ctx.settings, other) == (False, COMPANY_OPTED_OUT)
    assert lid


def test_a_person_on_the_do_not_contact_list_found_later_counts_and_a_bounce_entry_does_not(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="Harbor Goods", normalized_name="harbor goods", source="test", domain="harbor.example", state="TX", country="US")
        s.add(co)
        s.flush()
        s.add(Suppression(value="pat@harbor.example", kind="email", reason="Asked by phone"))
        s.add(Suppression(value="dead@harbor.example", kind="email", reason="Bounced (delivery failure report)"))
        s.flush()
        s.add(Contact(company_id=co.id, email="dead@harbor.example", source="website", consent_status="none"))
        s.flush()
        assert not company_opted_out(s, co)
        s.add(Contact(company_id=co.id, email="Pat+news@harbor.example", source="website", consent_status="none"))
        s.flush()
        assert company_opted_out(s, co)


def opted_out_contact_with_a_bounce_entry(ctx, **contact):
    with ctx.db.session() as s:
        co = Company(name="Harbor Goods", normalized_name="harbor goods", source="test", domain="harbor.example", state="TX", country="US")
        s.add(co)
        s.flush()
        s.add(Contact(company_id=co.id, email="dead@harbor.example", source="website", consent_status="opted_out", **contact))
        return co.id


@pytest.mark.parametrize("status, reason, mail_says, counts", [
    ("bounced", "Bounced (delivery failure report)", None, False),
    ("bounced", "Bounced (delivery failure report)", "unsubscribe", True),  # they said no as well
    ("bounced", "Asked by phone", None, True),
    ("bounced", None, None, True),  # no entry to show it was only a bounce: doubt
    ("published", "Bounced (delivery failure report)", None, True),
])
def test_only_a_clear_bounce_is_not_an_opt_out(make_ctx, status, reason, mail_says, counts):
    ctx = make_ctx()
    cid = opted_out_contact_with_a_bounce_entry(ctx, email_status=status)
    with ctx.db.session() as s:
        if reason:
            s.add(Suppression(value="dead@harbor.example", kind="email", reason=reason))
        if mail_says:
            s.add(InboundEmail(message_id="<m1@x.example>", from_email="dead@harbor.example", subject="Re: hello", body_text="Stop", category=mail_says, status="actioned", company_id=cid))
        s.flush()
        assert company_opted_out(s, s.get(Company, cid)) is counts


def two_leads_one_company(ctx, sent, cid):
    """A second, emailed lead at the same company, on a colleague's address: what merges and inbound leads leave behind."""
    with ctx.db.session() as s:
        colleague = Contact(company_id=cid, email=MARIA, first_name="Maria", last_name="Lopez", source="website", role_category="executive", email_status="published",
                            email_source_url=f"https://{SITE}/team", consent_status="none", confidence="high", email_confidence="high")
        s.add(colleague)
        s.flush()
        other = Lead(company_id=cid, contact_id=colleague.id, source="research", status="qualified", score=70, grade="B", primary_service="custom_crm", outreach_status="contacted")
        s.add(other)
        s.flush()
        s.add(FollowUp(lead_id=other.id, company_id=cid, contact_id=colleague.id, channel="email", step=1, kind="outreach", status="sent", to_email=MARIA,
                       subject="Hi", body="Body", sent_at=utcnow() - timedelta(days=5)))
        return other.id


def test_one_opt_out_stops_the_other_sequences_at_the_company_with_no_extra_address_listed(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    cid = company_of(ctx)
    other = two_leads_one_company(ctx, sent, cid)
    with ctx.db.session() as s:
        assert eligible(s, ctx.settings, s.get(Lead, other), running=True) == (True, "")
    opt_out_by_reply(ctx, sent)
    with ctx.db.session() as s:
        row = s.get(Lead, other)
        assert eligible(s, ctx.settings, row, running=True) == (False, COMPANY_OPTED_OUT)
        assert eligible(s, ctx.settings, row, reengage=True) == (False, COMPANY_OPTED_OUT)
        assert s.execute(select(Suppression.id).where(Suppression.value == MARIA)).first() is None
        assert s.get(Contact, row.contact_id).consent_status == "none"


def test_a_bounce_on_one_address_does_not_stop_a_colleagues_sequence(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    cid = company_of(ctx)
    other = two_leads_one_company(ctx, sent, cid)
    FakeIMAP.messages = {1: bounce_for(sent.to_email, sent.message_id)}
    run_inbox(ctx)
    with ctx.db.session() as s:
        assert eligible(s, ctx.settings, s.get(Lead, other), running=True) == (True, "")


# ------------------------------------------------------------------ a sequence cannot move past a do-not-contact entry
@pytest.mark.parametrize("order", ["entry, run, re-check", "entry, re-check", "re-check, entry"])
def test_an_entry_on_the_inbox_stops_the_sequence_whatever_the_order_of_a_re_check(make_ctx, order):
    ctx, sender, sent, cid, site_now_shows = emailed(make_ctx)
    with ctx.db.session() as s:
        s.get(FollowUp, sent.id).sent_at = NOW - timedelta(days=5)

    def entry():
        with ctx.db.session() as s:
            suppress(s, INBOX, "Asked by phone: stop writing")

    def re_check():
        site_now_shows(TEAM_WITH_ADDRESS)
        assert CompanyResearchAgent(ctx, mail_checker=FakeChecker({})).run({"company_ids": [cid]}).ok

    def run_sales():
        SalesWorkflowAgent(ctx, sender=sender).run({"now": NOW.isoformat()})

    for step in {"entry, run, re-check": (entry, run_sales, re_check), "entry, re-check": (entry, re_check), "re-check, entry": (re_check, entry)}[order]:
        step()
    run_sales()
    assert not [f for f in drafts(ctx) if f.step == 2 and f.status != "cancelled"]


def test_an_entry_only_on_an_address_the_lead_was_emailed_at_refuses_the_next_step(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_of(ctx).id)
        colleague = Contact(company_id=lead.company_id, email=f"maria@other-{SITE}", source="website", email_source_url=f"https://{SITE}/team", email_status="published", consent_status="none")
        s.add(colleague)
        s.flush()
        lead.contact_id = colleague.id  # any re-point, now or later
        s.add(Suppression(value=sent.to_email, kind="email", reason="Bounced (delivery failure report)"))
        s.flush()
        ok, why = eligible(s, ctx.settings, lead, running=True)
    assert not ok and "emailed at" in why


# ------------------------------------------------------------------ at send and after a change of mind
def second_first_email(ctx, cid):
    """A second lead of the company with a first email of its own, as an inbound lead and a research lead leave behind."""
    with ctx.db.session() as s:
        colleague = Contact(company_id=cid, email=MARIA, first_name="Maria", last_name="Lopez", source="website", role_category="executive", email_status="published",
                            email_source_url=f"https://{SITE}/team", consent_status="none", confidence="high", email_confidence="high")
        s.add(colleague)
        s.flush()
        lead = Lead(company_id=cid, contact_id=colleague.id, source="research", status="qualified", score=75, grade="B", primary_service="bpa")
        s.add(lead)
        s.flush()
        return lead.id


def test_an_approved_first_email_is_cancelled_at_send_after_a_win(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    other = second_first_email(ctx, cid)
    SalesWorkflowAgent(ctx, sender=sender).run({"send": False, "lead_ids": [other], "now": NOW.isoformat()})
    (draft,) = [f for f in drafts(ctx) if f.lead_id == other]
    assert draft.to_email == MARIA and draft.step == 1
    approve_all(ctx)
    with ctx.db.session() as s:
        assert s.get(FollowUp, draft.id).status == "approved"
    FakeSMTP.sent = []
    win(ctx, lid)
    SalesWorkflowAgent(ctx, sender=sender).run({"lead_ids": [other], "now": NOW.isoformat()})
    with ctx.db.session() as s:
        f = s.get(FollowUp, draft.id)
    assert f.status == "cancelled" and ALREADY_CUSTOMER in f.blocked_reason
    assert FakeSMTP.sent == []


def test_reopening_the_deal_clears_already_customer(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    other = second_first_email(ctx, cid)
    oid = win(ctx, lid)
    with ctx.db.session() as s:
        assert eligible(s, ctx.settings, s.get(Lead, other)) == (False, ALREADY_CUSTOMER)
    web(ctx).post(f"/opportunities/{oid}", data={"stage": "negotiation", "shown_stage": "won"}, headers=ORIGIN)
    with ctx.db.session() as s:
        assert s.get(Lead, lid).status != "converted"
        assert eligible(s, ctx.settings, s.get(Lead, other)) == (True, "")


def test_won_customer_does_not_stop_the_sequence_already_running(make_ctx):
    """First emails only: the owner's rule on a won customer is about cold first emails."""
    ctx, _s, sent = contacted(make_ctx)
    lid, cid = lead_of(ctx).id, company_of(ctx)
    other = two_leads_one_company(ctx, sent, cid)
    win(ctx, lid)
    with ctx.db.session() as s:
        assert eligible(s, ctx.settings, s.get(Lead, other), running=True) == (True, "")


# ------------------------------------------------------------------ the owner's lift
def test_a_lifted_opt_out_no_longer_closes_the_company_and_a_newer_one_does(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    cid = company_of(ctx)
    opt_out_by_reply(ctx, sent)
    with ctx.db.session() as s:
        assert company_opted_out(s, s.get(Company, cid))
    days_later(1)
    with ctx.db.session() as s:
        assert lift_suppression(s, DAVID, "owner", "They asked us to write again") is None
    with ctx.db.session() as s:
        assert s.execute(select(Contact.consent_status).where(Contact.email == DAVID)).scalar_one() == "none"
        assert not company_opted_out(s, s.get(Company, cid))  # the mail and the stop are older than the lift
    days_later(1)
    FakeIMAP.messages = {2: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Please unsubscribe us, for good.", in_reply_to=sent.message_id, msg_id="<again@mail.example>")}
    run_inbox(ctx)
    with ctx.db.session() as s:
        assert company_opted_out(s, s.get(Company, cid))


def test_a_lifted_entry_does_not_answer_for_another_persons_opt_out(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    cid = company_of(ctx)
    stranger = f"pat.reed@{SITE}"
    opt_out_by_reply(ctx, sent)
    FakeIMAP.messages = {2: mail(f"Pat Reed <{stranger}>", "Your email", "Please remove us from your list.", msg_id="<pat@mail.example>")}
    run_inbox(ctx)
    days_later(1)
    with ctx.db.session() as s:
        assert lift_suppression(s, DAVID, "owner", "They asked us to write again") is None
    with ctx.db.session() as s:
        assert company_opted_out(s, s.get(Company, cid))  # Pat's own entry still stands
        assert eligible(s, ctx.settings, lead_of_row(s, cid)) == (False, COMPANY_OPTED_OUT)
    with ctx.db.session() as s:
        assert lift_suppression(s, stranger, "owner", "Pat asked us to write again") is None
    with ctx.db.session() as s:
        assert not company_opted_out(s, s.get(Company, cid))


def lead_of_row(s, company_id):
    row = s.execute(select(Lead).where(Lead.company_id == company_id)).scalars().first()
    row.outreach_status = None  # the stage of the stopped lead would answer first
    return row


def test_a_lifted_domain_entry_leaves_its_contacts_counted(make_ctx):
    """lift_suppression leaves a domain's contacts as they are, since any of them may have said no: doubt counts as a no."""
    ctx, _s, _sent = contacted(make_ctx)
    cid = company_of(ctx)
    with ctx.db.session() as s:
        suppress(s, SITE, "Asked by phone")
    with ctx.db.session() as s:
        assert lift_suppression(s, SITE, "owner", "They asked us to write again") is None
    with ctx.db.session() as s:
        assert company_opted_out(s, s.get(Company, cid))


def test_a_stop_that_names_nobody_needs_the_lift_of_its_own_contact(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="Harbor Goods", normalized_name="harbor goods", source="test", domain="harbor.example", state="TX", country="US")
        s.add(co)
        s.flush()
        c = Contact(company_id=co.id, email="pat@harbor.example", source="website", consent_status="none")
        s.add(c)
        s.flush()
        lead = Lead(company_id=co.id, contact_id=c.id, source="research", status="disqualified")
        s.add(lead)
        s.flush()
        s.add(Activity(lead_id=lead.id, company_id=co.id, type="outreach", actor="inbox_monitor", subject="Outreach: unsubscribed (Asked not to be contacted)", created_at=utcnow()))
        s.flush()
        assert company_opted_out(s, co)
        s.add(Suppression(value="pat@harbor.example", kind="email", reason="Unsubscribe", lifted_at=utcnow() + timedelta(minutes=5), lifted_by="owner", lift_reason="asked"))
        s.flush()
        assert not company_opted_out(s, co)


def test_reopen_refusal_keeps_its_answers(make_ctx):
    ctx, _s, _sent = contacted(make_ctx)
    lid = lead_of(ctx).id
    with ctx.db.session() as s:
        s.get(Lead, lid).outreach_status = "stopped"
        assert reopen_refusal(s, s.get(Lead, lid)) is None
    FakeIMAP.messages = {1: mail(f"Pat Reed <pat.reed@{SITE}>", "Your email", "Please remove us from your list.")}
    run_inbox(ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, lid)
        lead.outreach_status = "stopped"
        assert "do-not-contact" in reopen_refusal(s, lead)
        s.execute(select(Suppression)).scalars().one().lifted_at = utcnow() - timedelta(days=1)  # lifted before the mail: it stands
        s.flush()
        assert "asked not to be contacted" in reopen_refusal(s, lead)


# ------------------------------------------------------------------ SEC filings are evidence only
SEC_TEXT = "Filed its annual report; business address in Texas."


def filing(name, item=91, **over):
    return listing(name, post=SEC_TEXT, item=item, source="sec_edgar", signal_type="filing", record=f"sec:{name}", **over)


def test_a_filing_for_a_new_company_keeps_a_signal_and_makes_no_lead_and_a_hiring_post_then_does(make_ctx):
    ctx = make_ctx()
    out = read_listing(ctx, filing("Filed Example Co"), filing("Other Filed Co", item=92), source="sec_edgar")
    assert (out.companies_created, out.signals_added, out.leads_created, out.sec_kept_as_evidence) == (2, 2, 0, 2)
    with ctx.db.session() as s:
        cid = s.execute(select(Company.id).where(Company.normalized_name == "filed example")).scalar_one()
        assert s.execute(select(func.count(Signal.id))).scalar_one() == 2 and s.execute(select(func.count(Lead.id))).scalar_one() == 0
        assert s.execute(select(func.count(Alert.id))).scalar_one() == 0  # a count in the log, not an alert each
    again = read_listing(ctx, filing("Filed Example Co"), filing("Other Filed Co", item=92), source="sec_edgar")
    assert again.leads_created == 0 and again.sec_kept_as_evidence == 2

    hn = listing("Filed Example Co", record="hn:5", item=5)
    assert read_listing(ctx, hn).leads_created == 1
    (lead,) = leads_at(ctx, cid)
    assert lead.source == "research" and lead.status == "new"
    assert read_listing(ctx, filing("Filed Example Co", item=93), source="sec_edgar").sec_kept_as_evidence == 0  # it has a lead now


def test_an_existing_lead_from_a_filing_stays_and_keeps_being_scored(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="Filed Example Co", normalized_name="filed example", source="sec_edgar", source_record_id="sec:Filed Example Co", state="TX", country="US", location_text="Austin, TX")
        s.add(co)
        s.flush()
        s.add(Lead(company_id=co.id, source="research", status="nurture", score=30, grade="D"))
        cid = co.id
    out = read_listing(ctx, filing("Filed Example Co"), source="sec_edgar")
    assert (out.signals_added, out.leads_created, out.sec_kept_as_evidence) == (1, 0, 0)
    (lead,) = leads_at(ctx, cid)
    assert lead.status == "nurture"
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).output.scored == 1
    (lead,) = leads_at(ctx, cid)
    assert lead.last_scored_at is not None and lead.status == "nurture"


def test_a_filing_adds_no_lead_beside_an_inbound_one_and_no_notice_on_a_closed_company(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        one = Company(name="Inbound Co", normalized_name="inbound", source="inbound", source_record_id="sec:Inbound Co", state="TX", country="US")
        two = Company(name="Closed Co", normalized_name="closed", source="sec_edgar", source_record_id="sec:Closed Co", state="TX", country="US")
        s.add_all([one, two])
        s.flush()
        s.add(Lead(company_id=one.id, source="inbound", status="new"))
        s.add(Lead(company_id=two.id, source="research", status="disqualified", disqualify_reason="not a fit"))
        ids = one.id, two.id
    out = read_listing(ctx, filing("Inbound Co"), filing("Closed Co", item=92), source="sec_edgar")
    assert out.signals_added == 2 and out.leads_created == 0
    assert [len(leads_at(ctx, i)) for i in ids] == [1, 1]
    with ctx.db.session() as s:
        assert s.execute(select(func.count(Alert.id))).scalar_one() == 0


def test_a_flagged_filer_makes_no_lead(make_ctx):
    ctx = make_ctx()
    out = read_listing(ctx, listing("Devshop", post=TRUE_COMPETITOR, source="sec_edgar", signal_type="filing", record="sec:Devshop"), source="sec_edgar")
    assert out.competitors_flagged == 1 and out.leads_created == 0


def test_an_erased_or_unknown_company_with_no_lead_is_read_as_before(make_ctx):
    """A company that is not closed gets its lead exactly as before."""
    ctx = make_ctx()
    out = read_listing(ctx, listing("Harbor Goods", "harbor.example"))
    assert out.leads_created == 1
    assert repost(ctx, leads_at(ctx, 1)[0].company_id).leads_created == 0
