"""Phase 5, score stability: a parked lead comes back only on real new evidence, a person's park holds, and the 30-day rule counts what
really happened. Synthetic data only, fake web and fake mail only."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import aime.agents.company_research as research_agent
import aime.timeutil as tu
from aime.agents.company_research import CompanyResearchAgent
from aime.agents.crm import CRMManagementAgent
from aime.agents.inbox_agent import InboxAgent
from aime.agents.qualification import NEW_POST_NOTE, NEW_POST_TASK, LeadQualificationAgent
from aime.agents.sales_workflow import REVIEW_MARK, SalesWorkflowAgent
from aime.crm_ops import add_signal
from aime.db import kv_set
from aime.inbox import Mailbox
from aime.models import Activity, Alert, Company, FollowUp, Lead, Opportunity, ResearchSnapshot, Signal
from aime.people import PersonFinding
from aime.scoring import LeadFacts, score_lead, signal_fact
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.workflows import MAILBOX_READ
from tests.test_inbox import MAIL as IMAP_MAIL
from tests.test_inbox import FakeIMAP, mail
from tests.test_time_and_alerts import add_lead
from tests.uat.test_uat_email import ORIGIN, web
from tests.uat.test_uat_sales_journey import make_deal, make_lead

POST = "Acme Tools | Software Engineer | Austin, TX. We have paying customers and want to automate our back office."
# keeps grade B for a month more on a post a month old: only such a lead can come back, so only it shows what brings it back
LASTING_POST = POST + " We are looking for contractors or an outside development partner."


@pytest.fixture
def clock():
    before = tu.CLOCK_OFFSET
    yield before
    tu.CLOCK_OFFSET = before


def site(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/":
        return httpx.Response(200, text="<html><head><title>Acme Tools</title></head><body><h1>Acme Tools</h1></body></html>",
                              headers={"content-type": "text/html"})
    return httpx.Response(404)


def qualified_lead(ctx, post_age=31, activity_age=31, researched_age=31, post=LASTING_POST):
    """A qualified lead whose review task a person already ticked off, on a verified site read a month ago: nothing left open."""
    now = tu.utcnow()
    with ctx.db.session() as s:
        c = Company(name="Acme Tools", normalized_name="acme tools", domain="acme.example", website="https://acme.example", state="TX", country="US",
                    source="hn_hiring", website_status="verified", website_checked_at=now - timedelta(days=1), employee_range="11-50", location_text="Austin, TX",
                    industry="saas", researched_at=now - timedelta(days=researched_age), research_status="no_people")
        s.add(c)
        s.flush()
        when = now - timedelta(days=post_age)
        s.add(Signal(company_id=c.id, signal_type="hiring", text=post, source="hn_hiring", source_url="https://example.test/1", fingerprint="p5", observed_at=when, created_at=when))
        lead = Lead(company_id=c.id, source="research", status="qualified", last_activity_at=now - timedelta(days=activity_age), created_at=now - timedelta(days=60))
        s.add(lead)
        s.flush()
        s.add(FollowUp(lead_id=lead.id, company_id=c.id, channel="task", step=1, status="done", due_at=now - timedelta(days=30), subject="Review Acme Tools", body="x"))
        return lead.id


def status_of(ctx, lead_id):
    with ctx.db.session() as s:
        return s.get(Lead, lead_id).status


def moves(ctx, lead_id):
    with ctx.db.session() as s:
        return [a.subject for a in s.execute(select(Activity).where(Activity.lead_id == lead_id, Activity.type == "status_change").order_by(Activity.id)).scalars()]


def morning(ctx, clock, day):
    tu.CLOCK_OFFSET = clock + timedelta(days=day)
    CRMManagementAgent(ctx).run({"operations": ["stale"]})
    LeadQualificationAgent(ctx).run({"rescore_all": True})


def read_site(ctx):
    return CompanyResearchAgent(ctx, mail_checker=lambda d: True).run({"limit": 5})


def reread_same_site(ctx):
    res = read_site(ctx)
    assert res.ok and res.output.companies_researched == 1


def read_again(ctx, lead_id):
    """Whether or not the monthly read is due, so the post on file is as strong as it was at the move."""
    with ctx.db.session() as s:
        company_id = s.get(Lead, lead_id).company_id
    res = CompanyResearchAgent(ctx, mail_checker=lambda d: True).run({"company_ids": [company_id]})
    assert res.ok and res.output.companies_researched == 1
    LeadQualificationAgent(ctx).run({"lead_ids": [lead_id]})
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        assert lead.grade == "B", (lead.grade, lead.score)
        return lead.status


def parked_lead(ctx, clock, **kw):
    """Parked by the stale rule on a lasting grade B."""
    lead_id = qualified_lead(ctx, **kw)
    morning(ctx, clock, 0)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        # a month on, a post a month old has lost 8 points: this is still grade B then, so anything counted as evidence brings it back
        assert (lead.status, lead.grade) == ("nurture", "B") and lead.score - 8 >= 55, (lead.status, lead.grade, lead.score)
    return lead_id


def new_post(ctx, lead_id, tag="2"):
    with ctx.db.session() as s:
        add_signal(s, s.get(Lead, lead_id).company, "hiring", POST + f" Opening {tag}.", "hn_hiring", f"https://example.test/{tag}", observed_at=tu.utcnow())


def person_parks(ctx, lead_id):
    client = TestClient(create_app(ctx), base_url="http://127.0.0.1", follow_redirects=False, headers={"origin": "http://127.0.0.1"})
    r = client.post(f"/leads/{lead_id}/status", data={"status": "nurture", "note": "wrong fit, spoke to the owner"})
    assert r.status_code == 303 and status_of(ctx, lead_id) == "nurture"


def tasks(ctx, lead_id, subject):
    with ctx.db.session() as s:
        return [(f.status, f.blocked_reason) for f in s.execute(select(FollowUp).where(
            FollowUp.lead_id == lead_id, FollowUp.channel == "task", FollowUp.subject.startswith(subject)).order_by(FollowUp.id)).scalars()]


def tick(ctx, lead_id, subject):
    with ctx.db.session() as s:
        for f in s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.status == "pending", FollowUp.subject.startswith(subject))).scalars():
            f.status = "done"


# ------------------------------------------------------------------------------------- 1. what counts as evidence after the stale rule's move
def test_the_monthly_read_of_an_unchanged_site_does_not_bring_back_a_lead_the_stale_rule_parked(make_ctx, clock):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = parked_lead(ctx, clock)
    reread_same_site(ctx)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert status_of(ctx, lead_id) == "nurture", "qualified again on the same site, no new evidence"
    morning(ctx, clock, 1)
    assert len(moves(ctx, lead_id)) == 1, moves(ctx, lead_id)


def test_reads_that_failed_are_no_evidence_for_requalifying(make_ctx, clock):
    def broken(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(500)
    ctx = make_ctx(handler=broken, polite_delay=0)
    lead_id = parked_lead(ctx, clock)
    for hours in (1, 2, 3):
        tu.CLOCK_OFFSET = clock + timedelta(hours=hours)
        read_site(ctx)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert status_of(ctx, lead_id) == "nurture", "nothing was read, yet the lead came back as if on new research"


@pytest.mark.parametrize("code", [403, 429, 503, 401, 500])
def test_one_blocked_read_is_no_evidence(make_ctx, clock, code):
    def blocked(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(code)
    ctx = make_ctx(handler=blocked, polite_delay=0)
    lead_id = parked_lead(ctx, clock)
    tu.CLOCK_OFFSET = clock + timedelta(hours=1)
    read_site(ctx)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).company.research_status == ("failed" if code == 500 else "blocked")
    assert status_of(ctx, lead_id) == "nurture"


def test_a_redirected_read_is_no_evidence(make_ctx, clock):
    def moved_away(request):
        if request.url.host == "acme.example" and request.url.path == "/":
            return httpx.Response(301, headers={"location": "https://elsewhere.example/"})
        return site(request)
    ctx = make_ctx(handler=moved_away, polite_delay=0)
    lead_id = parked_lead(ctx, clock)
    tu.CLOCK_OFFSET = clock + timedelta(hours=1)
    read_site(ctx)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).company.research_status == "redirected"
    assert status_of(ctx, lead_id) == "nurture"


class Contacts:
    """A home page and a contact page that publishes these addresses, or answers 503 during an outage."""

    def __init__(self, *addresses):
        self.addresses, self.down = list(addresses), False

    def __call__(self, request):
        if request.url.path == "/":
            return httpx.Response(200, text='<html><head><title>Acme Tools</title></head><body><h1>Acme Tools</h1><a href="/about">About</a> <a href="/contact">Contact</a></body></html>',
                                  headers={"content-type": "text/html"})
        if request.url.path == "/about":
            return httpx.Response(200, text="<html><head><title>About</title></head><body><h1>About us</h1></body></html>", headers={"content-type": "text/html"})
        if request.url.path == "/contact":
            if self.down:
                return httpx.Response(503)
            links = "".join(f'<a href="mailto:{a}">{a}</a>' for a in self.addresses)
            return httpx.Response(200, text=f"<html><head><title>Contact</title></head><body><h1>Contact</h1>{links}</body></html>", headers={"content-type": "text/html"})
        return httpx.Response(404)


def test_a_partial_outage_and_the_full_read_after_it_are_no_evidence(make_ctx, clock):
    web_site = Contacts("hello@acme.example")
    ctx = make_ctx(handler=web_site, polite_delay=0)
    lead_id = qualified_lead(ctx)
    reread_same_site(ctx)  # what was on file before the move
    tu.CLOCK_OFFSET = clock + timedelta(hours=1)
    CRMManagementAgent(ctx).run({"operations": ["stale"]})
    assert status_of(ctx, lead_id) == "nurture"
    for day, is_down in ((1, True), (2, False)):
        web_site.down = is_down
        tu.CLOCK_OFFSET = clock + timedelta(days=day)
        assert read_again(ctx, lead_id) == "nurture", f"day {day}: fewer people, or the same people again, are not new"
    with ctx.db.session() as s:
        reads = s.execute(select(ResearchSnapshot.status, ResearchSnapshot.emails).where(ResearchSnapshot.company_id == s.get(Lead, lead_id).company_id)
                          .order_by(ResearchSnapshot.id)).all()
    assert [len(emails) for _, emails in reads] == [1, 0, 1], reads


def test_a_new_address_on_the_site_brings_back_a_lead_the_stale_rule_parked(make_ctx, clock):
    web_site = Contacts("hello@acme.example")
    ctx = make_ctx(handler=web_site, polite_delay=0)
    lead_id = qualified_lead(ctx)
    reread_same_site(ctx)
    tu.CLOCK_OFFSET = clock + timedelta(hours=1)
    CRMManagementAgent(ctx).run({"operations": ["stale"]})
    assert status_of(ctx, lead_id) == "nurture"
    web_site.addresses.append("sales@acme.example")
    tu.CLOCK_OFFSET = clock + timedelta(days=2)
    assert read_again(ctx, lead_id) == "qualified"


def test_a_new_person_on_the_site_brings_back_a_lead_the_stale_rule_parked(make_ctx, clock, monkeypatch):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = parked_lead(ctx, clock)
    tu.CLOCK_OFFSET = clock + timedelta(hours=1)
    assert read_again(ctx, lead_id) == "nurture"
    monkeypatch.setattr(research_agent, "extract_people", lambda pages, name: [PersonFinding(
        "Pat Testcase", "Chief Executive Officer", "executive", "high", "named as CEO on its team page", "https://acme.example/", "website")])
    tu.CLOCK_OFFSET = clock + timedelta(days=2)
    assert read_again(ctx, lead_id) == "qualified"


# ------------------------------------------------------------------------------------- 2. a person's park
def test_the_monthly_read_of_an_unchanged_site_does_not_undo_a_persons_nurture(make_ctx, clock):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=5, activity_age=1)
    person_parks(ctx, lead_id)
    tu.CLOCK_OFFSET = clock + timedelta(days=3)
    reread_same_site(ctx)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert status_of(ctx, lead_id) == "nurture", "a person parked it with a reason; a routine read of the same page put it back"
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).grade == "B", "the score is still refreshed: only the status holds"


def test_a_new_person_on_the_site_does_not_undo_a_persons_nurture(make_ctx, clock, monkeypatch):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=5, activity_age=1)
    person_parks(ctx, lead_id)
    monkeypatch.setattr(research_agent, "extract_people", lambda pages, name: [PersonFinding(
        "Pat Testcase", "Chief Executive Officer", "executive", "high", "named as CEO on its team page", "https://acme.example/", "website")])
    tu.CLOCK_OFFSET = clock + timedelta(days=3)
    reread_same_site(ctx)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert status_of(ctx, lead_id) == "nurture"


def test_a_new_post_on_a_lead_a_person_parked_opens_a_task_and_the_lead_stays_parked(make_ctx, clock):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=5, activity_age=1)
    person_parks(ctx, lead_id)
    tu.CLOCK_OFFSET = clock + timedelta(hours=1)
    new_post(ctx, lead_id)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert status_of(ctx, lead_id) == "nurture"
    assert tasks(ctx, lead_id, NEW_POST_TASK) == [("pending", NEW_POST_NOTE)]
    with ctx.db.session() as s:
        body = s.execute(select(FollowUp.body).where(FollowUp.lead_id == lead_id, FollowUp.subject.startswith(NEW_POST_TASK))).scalar_one()
        assert "https://example.test/2" in body and "nothing is drafted" in body
    # still open, or ticked off: the same post is told once
    morning(ctx, clock, 1)
    assert tasks(ctx, lead_id, NEW_POST_TASK) == [("pending", NEW_POST_NOTE)]
    tick(ctx, lead_id, NEW_POST_TASK)
    morning(ctx, clock, 2)
    assert tasks(ctx, lead_id, NEW_POST_TASK) == [("done", NEW_POST_NOTE)]
    # and a later post is news again
    tu.CLOCK_OFFSET = clock + timedelta(days=3)
    new_post(ctx, lead_id, "3")
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert tasks(ctx, lead_id, NEW_POST_TASK) == [("done", NEW_POST_NOTE), ("pending", NEW_POST_NOTE)]
    assert status_of(ctx, lead_id) == "nurture"
    SalesWorkflowAgent(ctx).run({"send": False})
    with ctx.db.session() as s:
        assert not s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.channel == "email")).first()


def test_the_company_writing_to_us_still_brings_back_a_lead_a_person_parked(make_ctx, clock):
    """The owner decided about posts. Any other new signal does as it did."""
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=5, activity_age=1)
    person_parks(ctx, lead_id)
    tu.CLOCK_OFFSET = clock + timedelta(hours=1)
    with ctx.db.session() as s:
        add_signal(s, s.get(Lead, lead_id).company, "inbound", "Can you help us with our back office?", "inbound", "inbound:1", tu.utcnow())
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert status_of(ctx, lead_id) == "qualified"
    assert tasks(ctx, lead_id, NEW_POST_TASK) == []


def test_a_person_disqualifying_a_lead_is_untouched_by_a_new_post(make_ctx, clock):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=5, activity_age=1)
    client = TestClient(create_app(ctx), base_url="http://127.0.0.1", follow_redirects=False, headers={"origin": "http://127.0.0.1"})
    assert client.post(f"/leads/{lead_id}/status", data={"status": "disqualified", "note": "not for us"}).status_code == 303
    tu.CLOCK_OFFSET = clock + timedelta(hours=1)
    new_post(ctx, lead_id)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert status_of(ctx, lead_id) == "disqualified"
    assert tasks(ctx, lead_id, NEW_POST_TASK) == []


# ------------------------------------------------------------------------------------- 3. a post brings back what a machine parked, for a month
def test_a_new_post_that_requalifies_a_parked_lead_is_not_undone_next_morning(make_ctx, clock):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=40, researched_age=1, post=POST)
    morning(ctx, clock, 0)
    assert status_of(ctx, lead_id) == "nurture"
    tu.CLOCK_OFFSET = clock + timedelta(hours=2)
    new_post(ctx, lead_id)
    LeadQualificationAgent(ctx).run({})
    assert status_of(ctx, lead_id) == "qualified"
    morning(ctx, clock, 1)
    assert status_of(ctx, lead_id) == "qualified", "fresh evidence; the 30-day hold promises a month, the stale rule gave it a day"


def test_a_lead_a_post_brought_back_holds_on_day_30_and_parks_on_day_31(make_ctx, clock):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=40, researched_age=1, post=POST)
    morning(ctx, clock, 0)
    tu.CLOCK_OFFSET = clock + timedelta(hours=2)
    new_post(ctx, lead_id)
    LeadQualificationAgent(ctx).run({})
    tick(ctx, lead_id, "Review ")
    for day in (1, 15, 29, 30):
        tu.CLOCK_OFFSET = clock + timedelta(days=day)
        CRMManagementAgent(ctx).run({"operations": ["stale"]})
        assert status_of(ctx, lead_id) == "qualified", day
    tu.CLOCK_OFFSET = clock + timedelta(days=31)
    CRMManagementAgent(ctx).run({"operations": ["stale"]})
    assert status_of(ctx, lead_id) == "nurture"


def test_a_new_post_counts_as_activity_for_the_30_day_rule(make_ctx, clock):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=29, activity_age=45, researched_age=1)
    CRMManagementAgent(ctx).run({"operations": ["stale"]})
    assert status_of(ctx, lead_id) == "qualified", "its newest post is 29 days old"
    tu.CLOCK_OFFSET = clock + timedelta(days=2)
    CRMManagementAgent(ctx).run({"operations": ["stale"]})
    assert status_of(ctx, lead_id) == "nurture", "its newest post is 31 days old"


def test_a_filing_is_no_activity_for_the_30_day_rule(make_ctx, clock):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=45, activity_age=45, researched_age=1)
    with ctx.db.session() as s:
        add_signal(s, s.get(Lead, lead_id).company, "filing", "Annual report", "sec_edgar", "https://example.test/f", tu.utcnow())
    CRMManagementAgent(ctx).run({"operations": ["stale"]})
    assert status_of(ctx, lead_id) == "nurture"


def test_a_new_post_on_a_reviewed_lead_opens_a_new_review_the_normal_way(make_ctx, clock):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=40, researched_age=1, post=POST)
    morning(ctx, clock, 0)
    tu.CLOCK_OFFSET = clock + timedelta(hours=2)
    SalesWorkflowAgent(ctx).run({"send": False})
    assert [t[0] for t in tasks(ctx, lead_id, "Review ")] == ["done"], "parked: nothing new"
    new_post(ctx, lead_id)
    LeadQualificationAgent(ctx).run({})
    SalesWorkflowAgent(ctx).run({"send": False})
    assert [t[0] for t in tasks(ctx, lead_id, "Review ")] == ["done", "pending"]
    with ctx.db.session() as s:
        review = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.subject.startswith("Review "), FollowUp.status == "pending")).scalar_one()
        assert REVIEW_MARK in review.body
    # no second one while it is open, and a person's tick closes it for good
    SalesWorkflowAgent(ctx).run({"send": False})
    assert [t[0] for t in tasks(ctx, lead_id, "Review ")] == ["done", "pending"]
    tick(ctx, lead_id, "Review ")
    tu.CLOCK_OFFSET = clock + timedelta(days=1)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    SalesWorkflowAgent(ctx).run({"send": False})
    assert [t[0] for t in tasks(ctx, lead_id, "Review ")] == ["done", "done"]


def person_brings_back(ctx, lead_id):
    client = TestClient(create_app(ctx), base_url="http://127.0.0.1", follow_redirects=False, headers={"origin": "http://127.0.0.1"})
    assert client.post(f"/leads/{lead_id}/status", data={"status": "qualified"}).status_code == 303
    assert status_of(ctx, lead_id) == "qualified"


def test_a_ticked_post_task_does_not_stop_the_review_of_a_lead_a_machine_parked_later(make_ctx, clock):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=5, activity_age=1, researched_age=1, post=LASTING_POST)
    person_parks(ctx, lead_id)
    tu.CLOCK_OFFSET = clock + timedelta(hours=1)
    new_post(ctx, lead_id)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    tick(ctx, lead_id, NEW_POST_TASK)
    person_brings_back(ctx, lead_id)
    morning(ctx, clock, 32)  # no activity for a month: the stale rule parks it
    assert status_of(ctx, lead_id) == "nurture"
    tu.CLOCK_OFFSET = clock + timedelta(days=33)
    new_post(ctx, lead_id, "3")
    LeadQualificationAgent(ctx).run({})
    assert status_of(ctx, lead_id) == "qualified"
    SalesWorkflowAgent(ctx).run({"send": False})
    assert [t[0] for t in tasks(ctx, lead_id, "Review ")] == ["done", "pending"]


@pytest.mark.parametrize("ticked_first", [True, False])
def test_a_lead_a_person_brought_back_gets_its_first_review_once_the_post_task_is_closed(make_ctx, clock, ticked_first):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=5, activity_age=1, post=LASTING_POST)
    with ctx.db.session() as s:  # never reviewed
        s.delete(s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id)).scalar_one())
    person_parks(ctx, lead_id)
    tu.CLOCK_OFFSET = clock + timedelta(hours=1)
    new_post(ctx, lead_id)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    if ticked_first:
        tick(ctx, lead_id, NEW_POST_TASK)
    person_brings_back(ctx, lead_id)
    SalesWorkflowAgent(ctx).run({"send": False})
    if not ticked_first:  # the open task is the next step, and closing it does not end the road
        assert tasks(ctx, lead_id, "Review ") == []
        tick(ctx, lead_id, NEW_POST_TASK)
        SalesWorkflowAgent(ctx).run({"send": False})
    assert [t[0] for t in tasks(ctx, lead_id, "Review ")] == ["pending"]


def test_a_new_review_falls_away_by_the_same_rule_when_the_score_drops(make_ctx, clock):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=40, researched_age=1, post=POST)
    morning(ctx, clock, 0)
    tu.CLOCK_OFFSET = clock + timedelta(hours=2)
    new_post(ctx, lead_id)
    LeadQualificationAgent(ctx).run({})
    SalesWorkflowAgent(ctx).run({"send": False})
    assert [t[0] for t in tasks(ctx, lead_id, "Review ")] == ["done", "pending"]
    with ctx.db.session() as s:
        s.get(Lead, lead_id).company.website_status = "parked"
        s.get(Lead, lead_id).company.employee_range = None
        for sig in s.execute(select(Signal)).scalars():
            sig.observed_at = tu.utcnow() - timedelta(days=200)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert status_of(ctx, lead_id) == "nurture"
    assert [t[0] for t in tasks(ctx, lead_id, "Review ")] == ["done", "cancelled"]


# ------------------------------------------------------------------------------------- 4. the stale rule tells, and leaves what is being worked
def test_a_stale_move_names_the_lead_in_the_scoring_notice(make_ctx):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx)
    assert CRMManagementAgent(ctx).run({"operations": ["stale"]}).output.stale_to_nurture == 1
    with ctx.db.session() as s:
        said = s.execute(select(Alert).where(Alert.source == "qualification", Alert.key == "leads:moved", Alert.acknowledged.is_(False))).scalars().one()
        assert said.message.startswith("Scoring moved 1 lead(s)") and f"#{lead_id}" in said.message


def test_a_lead_whose_deal_is_being_worked_is_not_parked_as_inactive(make_ctx):
    from datetime import datetime
    ctx = make_ctx()
    lid = make_lead(ctx, owner="eva", email="dana@acme.example")
    oid = make_deal(ctx, lid, owner="eva", stage="proposal", value=1000.0)
    with ctx.db.session() as s:
        s.get(Lead, lid).last_activity_at = datetime(2026, 1, 1)
        s.get(Lead, lid).created_at = datetime(2026, 1, 1)
    c = web(ctx)
    c.post(f"/opportunities/{oid}", data={"stage": "negotiation", "shown_stage": "proposal", "next_step": "call Tuesday"}, headers=ORIGIN)
    c.post(f"/opportunities/{oid}/quotes", data={"amount": "1000"}, headers=ORIGIN)
    with ctx.db.session() as s:
        s.get(Lead, lid).last_activity_at = datetime(2026, 1, 1)
    CRMManagementAgent(ctx).run({"operations": ["stale"]})
    with ctx.db.session() as s:
        assert s.get(Lead, lid).status == "qualified"


@pytest.mark.parametrize("stage,parks", [("won", True), ("lost", True), ("new", False), ("discovery", False), ("proposal", False), ("negotiation", False)])
def test_only_an_open_deal_holds_a_lead_back_from_the_stale_rule(make_ctx, stage, parks):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(Opportunity(lead_id=lead_id, company_id=lead.company_id, name="Acme deal", stage=stage))
    CRMManagementAgent(ctx).run({"operations": ["stale"]})
    assert status_of(ctx, lead_id) == ("nurture" if parks else "qualified")


def replied_lead(ctx, last_days):
    lead_id = add_lead(ctx, stage="contacted")
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        lead.last_activity_at = lead.created_at = utcnow() - timedelta(days=last_days)
        s.add(FollowUp(lead_id=lead_id, company_id=lead.company_id, contact_id=lead.contact_id, channel="email", step=3, status="sent", kind="outreach", approved_by="x",
                       approved_at=utcnow() - timedelta(days=last_days + 1), due_at=utcnow() - timedelta(days=last_days), sent_at=utcnow() - timedelta(days=last_days),
                       to_email=lead.contact.email, subject="Hello there", body="Hello\n\nunsubscribe", message_id=f"<sent{lead_id}@us.example>"))
        return lead_id, lead.contact.email, f"<sent{lead_id}@us.example>"


@pytest.mark.parametrize("last_read", ["failed", "stale"])
def test_a_reply_waiting_in_a_mailbox_nobody_has_just_read_keeps_the_lead_qualified(make_ctx, last_read):
    """Behaviour, not the order of the jobs: last activity 31 days ago, an interested reply waiting, and the morning run comes first."""
    ctx = make_ctx(**IMAP_MAIL)
    lead_id, address, sent_id = replied_lead(ctx, 31)
    FakeIMAP.messages = {1: mail(address, "Re: Hello there", "Yes please, tell me more, can we talk next week?", in_reply_to=sent_id, msg_id="<back@x>")}
    with ctx.db.session() as s:
        kv_set(s, MAILBOX_READ, None if last_read == "failed" else (utcnow() - timedelta(hours=9)).isoformat())
    assert CRMManagementAgent(ctx).run({"operations": ["stale"]}).output.stale_to_nurture == 0
    assert status_of(ctx, lead_id) == "qualified"
    assert InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=FakeIMAP)).run({}).ok
    with ctx.db.session() as s:
        kv_set(s, MAILBOX_READ, utcnow().isoformat())
    assert CRMManagementAgent(ctx).run({"operations": ["stale"]}).output.stale_to_nurture == 0
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        assert (lead.status, lead.outreach_status) == ("qualified", "interested")


def test_the_move_only_waits_for_a_read_and_comes_with_a_fresh_one(make_ctx):
    ctx = make_ctx(**IMAP_MAIL)
    lead_id, *_ = replied_lead(ctx, 31)
    assert CRMManagementAgent(ctx).run({"operations": ["stale"]}).output.stale_to_nurture == 0, "never read yet"
    with ctx.db.session() as s:
        kv_set(s, MAILBOX_READ, utcnow().isoformat())
    assert CRMManagementAgent(ctx).run({"operations": ["stale"]}).output.stale_to_nurture == 1
    assert status_of(ctx, lead_id) == "nurture"


# ------------------------------------------------------------------------------------- 5. a filled post, a refused name, the domain's word
def post_signal(text):
    return SimpleNamespace(signal_type="hiring", text=text, matched_services=["ai_agents"], strength=0.9, observed_at=utcnow(), source_url="https://example.test/1")


@pytest.mark.parametrize("words", ["Position Filled", "position filled", "ROLE FILLED", "Role  filled.", "------ Position Filled -------", "Software Engineer: role filled"])
def test_a_hiring_post_marked_as_filled_earns_nothing(words):
    fact = signal_fact(post_signal(f"{POST} We need engineers to build AI agents for our CRM. {words}"))
    assert fact.matched_services == [] and fact.strength == 0.0 and fact.hires_builders is False


@pytest.mark.parametrize("words", ["Position open", "We have filled seats", "The role is open", "Role: filled with variety is not this phrase",
                                   "We need this role filled by March.", "We need this role filled.", "Looking to get this position filled quickly, engineers apply.",
                                   "Backend Engineer: position filled. Frontend Engineer: open now."])
def test_a_post_that_does_not_say_so_is_read_as_before(words):
    fact = signal_fact(post_signal(f"{POST} We need engineers to build AI agents for our CRM. {words}"))
    assert fact.hires_builders is True and fact.matched_services


def test_a_filled_post_keeps_what_is_stored():
    signal = post_signal(f"{POST} Position Filled")
    signal_fact(signal)
    assert signal.matched_services == ["ai_agents"] and signal.strength == 0.9


def name_lead(ctx, name, set_by=None, status="nurture"):
    with ctx.db.session() as s:
        c = Company(name=name, normalized_name=name.lower(), source="hn_hiring", state="TX", country="US", name_set_by=set_by)
        s.add(c)
        s.flush()
        lead = Lead(company_id=c.id, source="research", status=status)
        s.add(lead)
        s.flush()
        return lead.id


def names_alerts(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Alert).where(Alert.key == "leads:names_refused").order_by(Alert.id)).scalars().all()


def test_open_leads_with_a_refused_name_are_listed_once_and_left_as_they_are(make_ctx):
    ctx = make_ctx()
    job_title = name_lead(ctx, "Senior Engineers")
    place = name_lead(ctx, "Austin", status="qualified")
    corrected = name_lead(ctx, "Remote", set_by="owner")
    closed = name_lead(ctx, "Hiring", status="disqualified")
    fine = name_lead(ctx, "Acme Tools")
    for _ in range(3):
        LeadQualificationAgent(ctx).run({"rescore_all": True})
    alerts = names_alerts(ctx)
    assert len(alerts) == 1 and not alerts[0].acknowledged
    message = alerts[0].message
    assert f"#{job_title}" in message and f"#{place}" in message
    assert not {f"#{corrected}", f"#{closed}", f"#{fine}"} & set(message.replace(",", " ").replace(".", " ").split())
    with ctx.db.session() as s:
        assert s.get(Lead, job_title).status == "nurture" and s.get(Lead, closed).status == "disqualified"
    # dismissed: not raised again; a run for one lead does not even look
    with ctx.db.session() as s:
        s.get(Alert, alerts[0].id).acknowledged = True
    tu_before = tu.CLOCK_OFFSET
    tu.CLOCK_OFFSET = tu_before + timedelta(days=3)
    try:
        LeadQualificationAgent(ctx).run({"rescore_all": True})
        assert len(names_alerts(ctx)) == 1 and names_alerts(ctx)[0].acknowledged
    finally:
        tu.CLOCK_OFFSET = tu_before


def test_the_refused_names_notice_closes_once_a_person_corrected_them(make_ctx):
    ctx = make_ctx()
    lead_id = name_lead(ctx, "Senior Engineers")
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert not names_alerts(ctx)[0].resolved_at
    with ctx.db.session() as s:
        s.get(Lead, lead_id).company.name_set_by = "owner"
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    assert names_alerts(ctx)[0].resolved_at


def facts(**kw):
    return LeadFacts(source="research", industry="saas", target_industries=["saas"], state="TX", country="US", has_website=True, employee_range=None, verified=False,
                     is_competitor=False, suppressed=False, contact_consent=None, website_status="verified", now=utcnow(), **kw)


def test_the_reason_says_when_only_the_domain_spells_the_name_and_the_points_stay():
    named, spelled = score_lead(facts()), score_lead(facts(page_names_it=False))
    assert "+2 data: website confirmed live and naming the company" in named.reasons
    assert any("the domain spells the name" in r and "the page does not confirm it" in r for r in spelled.reasons)
    assert not any("naming the company" in r for r in spelled.reasons)
    assert (named.total, named.fit, named.data) == (spelled.total, spelled.fit, spelled.data)


def test_the_qualification_run_compares_the_name_with_the_title_on_file(make_ctx):
    ctx = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(ctx, post_age=5, activity_age=1)
    def reasons():
        LeadQualificationAgent(ctx).run({"rescore_all": True})
        with ctx.db.session() as s:
            return " ".join(s.get(Lead, lead_id).score_breakdown)
    with ctx.db.session() as s:
        s.get(Lead, lead_id).company.website_title = "Acme Tools | Back office software"
    assert "naming the company" in reasons()
    with ctx.db.session() as s:
        s.get(Lead, lead_id).company.website_title = "Welcome"
    assert "the page does not confirm it" in reasons()
    with ctx.db.session() as s:
        s.get(Lead, lead_id).company.website_title = None
    assert "the page does not confirm it" in reasons()
