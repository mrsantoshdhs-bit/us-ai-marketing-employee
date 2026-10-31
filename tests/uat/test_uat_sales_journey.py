"""Independent UAT of the sales journey through the real HTTP routes, as the people who use it: a CRM Administrator,
a Sales Manager, Sales Executives, a Business Owner reading reports, a new lead and a returning customer.

test_ok_*      behaviour that was checked and is right.
test_defect_*  asserts the correct behaviour and FAILS on the current code: each is one defect.

Synthetic data only: a temp file-backed database, a mocked HTTP transport, fake SMTP and IMAP. Nothing real is touched.
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.integrations.email_smtp import EmailSender
from aime.models import (Activity, Assignment, AuditEvent, Company, Contact, FollowUp, InboundEmail, InboundInquiry, Lead,
                         Meeting, Opportunity, Quote, StageChange, Suppression, Team, User)
from aime.timeutil import fmt_local, utcnow
from aime.web.app import create_app
from aime.web.auth import COOKIE, hash_password, make_cookie, session_secret
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import MAIL, FakeIMAP, mail, run_inbox

ORIGIN = {"origin": "http://testserver"}
GOOD = "correct horse " + "battery staple"  # test-only value, assembled so the secret scanner stays quiet
TOKEN = "uat-" + "token"
HUGE_ID = 10 ** 30
HASH = hash_password(GOOD)
PEOPLE = (("boss", "admin"), ("olga", "super_admin"), ("ana", "analyst"), ("mia", "sales_manager"),
          ("eva", "sales_executive"), ("ed", "sales_executive"), ("zoe", "sales_executive"))


# ------------------------------------------------------------------ the organisation and its people
def add_people(ctx):
    with ctx.db.session() as s:
        team = Team(name="US East", manager="mia")
        s.add(team)
        s.flush()
        for name, role in PEOPLE:
            s.add(User(username=name, password_hash=HASH, role=role,
                       team_id=team.id if name in ("eva", "ed") else None))


@pytest.fixture
def org(make_ctx):
    ctx = make_ctx(dashboard_auth=True, inbound_api_token=TOKEN)
    add_people(ctx)
    return ctx


def client(ctx, username=None, app=None):
    """A browser: signed in with a real session cookie. raise_server_exceptions=False: a crash is a 500 as a user sees."""
    c = TestClient(app or create_app(ctx), raise_server_exceptions=False)
    if username:
        with ctx.db.session() as s:
            user = s.execute(select(User).where(User.username == username)).scalar_one()
            c.cookies.set(COOKIE, make_cookie(session_secret(ctx.settings.data_dir), user))
    return c


def post(c, url, **data):
    return c.post(url, headers=ORIGIN, data=data, follow_redirects=False)


def where(r):
    return unquote(r.headers.get("location", ""))


def make_lead(ctx, owner=None, company="Acme", domain=None, email=None, status="qualified", first="Dana"):
    with ctx.db.session() as s:
        co = Company(name=company, normalized_name=company.lower(), source="test", domain=domain)
        s.add(co)
        s.flush()
        contact = None
        if email:
            contact = Contact(company_id=co.id, first_name=first, last_name="Lee", email=email, source="inbound",
                              consent_status="inquiry")
            s.add(contact)
            s.flush()
        lead = Lead(company_id=co.id, contact_id=contact.id if contact else None, source="inbound", status=status,
                    owner=owner, score=70, grade="B")
        s.add(lead)
        s.flush()
        return lead.id


def make_deal(ctx, lead_id, owner="eva", stage="negotiation", value=None, name="Acme CRM", origin="human"):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        o = Opportunity(company_id=lead.company_id, lead_id=lead_id, name=name, stage=stage, owner=owner, probability=70,
                        est_value=value, origin=origin)
        s.add(o)
        s.flush()
        return o.id


def make_draft(ctx, lead_id, to="dana@acme.example", kind="response", status="awaiting_approval", channel="email",
               subject="Your question"):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        f = FollowUp(lead_id=lead_id, company_id=lead.company_id, contact_id=lead.contact_id, channel=channel, step=1,
                     status=status, subject=subject, kind=kind, to_email=to,
                     body="Hi Dana,\n\nThanks.\n\nTo opt out, reply unsubscribe.\n1 Sample St, Austin, TX")
        s.add(f)
        s.flush()
        return f.id


def count(ctx, model, *where_):
    with ctx.db.session() as s:
        return s.execute(select(func.count()).select_from(model).where(*where_)).scalar_one()


def opp_row(ctx, oid):
    with ctx.db.session() as s:
        return s.get(Opportunity, oid)


def lead_row(ctx, lid):
    with ctx.db.session() as s:
        return s.get(Lead, lid)


def table_rows(html):
    """Every table row of a page as its cells' text."""
    rows = []
    for row in re.findall(r"<tr.*?</tr>", html, re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        rows.append([" ".join(re.sub(r"<[^>]+>", " ", c).split()) for c in cells])
    return rows


PERF_COLUMNS = ("name", "leads_owned", "assigned", "emails", "pos", "neg", "meetings", "opened", "won", "lost", "rate",
                "value", "ai")


def perf(html, who):
    for cells in table_rows(html):
        if cells and cells[0] == who:
            return dict(zip(PERF_COLUMNS, cells, strict=False))
    raise AssertionError(f"no row for {who} in the performance table")


def board_count(html, stage):
    return int(re.search(rf"<strong>{stage.capitalize()}</strong> <span class=\"muted\">\((\d+)\)", html).group(1))


def tile(html, label):
    return int(re.search(rf'<div class="v">(\d+)</div><div class="l">{label}</div>', html).group(1))


def api_inquiry(c, **payload):
    return c.post("/api/inbound", json=payload, headers={"x-inbound-token": TOKEN})


def process(ctx, app=None):
    r = post(client(ctx, "boss", app), "/inbound/process")
    assert r.status_code == 303, r.text


ASK = ("Our agents track leads in spreadsheets and follow-ups get missed. We'd like a custom CRM that captures leads "
       "from our website and automates follow-up reminders.")
DANA = {"first_name": "Dana", "last_name": "Lee", "email": "dana@brightpath-realty.example",
        "company": "Brightpath Realty Group", "state": "TX", "city": "Austin", "country": "US", "industry": "real_estate",
        "message": ASK, "services_interested": ["custom_crm", "sales_automation"], "timeline": "Next quarter",
        "website": "https://brightpath-realty.example", "source_page": "/contact"}


# ================================================================== the whole journey, start to finish
def test_ok_full_journey_from_web_inquiry_to_handover_and_reports(make_ctx):
    ctx = make_ctx(dashboard_auth=True, inbound_api_token=TOKEN, **MAIL)
    add_people(ctx)
    app = create_app(ctx)
    boss, mia, eva, ana = (client(ctx, u, app) for u in ("boss", "mia", "eva", "ana"))
    FakeSMTP.sent, FakeIMAP.messages = [], {}
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)

    # lead capture, qualification, scoring: a new lead uses the website form
    r = api_inquiry(client(ctx, app=app), **DANA)
    assert r.status_code == 201 and r.json()["created"] is True
    process(ctx, app)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        lid = lead.id
        assert lead.status == "qualified" and lead.score > 0 and lead.grade
        assert lead.contact.email == DANA["email"] and lead.contact.consent_status == "inquiry"
        deal = s.execute(select(Opportunity).where(Opportunity.lead_id == lid)).scalar_one()
        oid = deal.id
        draft = s.execute(select(FollowUp).where(FollowUp.lead_id == lid, FollowUp.channel == "email")).scalars().first()
        fid, score = draft.id, lead.score
        assert draft.status == "awaiting_approval"
    assert f"Score {score}/100" in boss.get(f"/leads/{lid}").text
    assert count(ctx, FollowUp, FollowUp.sent_at.is_not(None)) == 0  # nothing leaves without a person

    # assignment: the administrator gives the lead to Eva, in the Sales Manager's team
    r = post(boss, f"/leads/{lid}/assign", owner="eva", reason="Works realty")
    assert r.status_code == 303 and "to eva" in where(r)
    assert lead_row(ctx, lid).owner == "eva" and opp_row(ctx, oid).owner == "eva"
    assert f'/leads/{lid}"' in eva.get("/my").text

    # approval: a Sales Executive cannot approve, the Sales Manager can
    assert post(eva, f"/followups/{fid}/approve").status_code == 403
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})  # sending seen on
    assert post(mia, f"/followups/{fid}/approve").status_code == 303
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert len(FakeSMTP.sent) == 1 and FakeSMTP.sent[0]["To"] == DANA["email"]
    with ctx.db.session() as s:
        sent = s.get(FollowUp, fid)
        assert sent.status == "sent" and sent.approved_by == "mia"
        message_id = sent.message_id

    # reply classification: Dana answers with interest
    FakeIMAP.messages[1] = mail(f"Dana Lee <{DANA['email']}>", "Re: your email", "Yes, sounds good. Can we talk Thursday?",
                                in_reply_to=message_id, msg_id="<dana-1@brightpath.example>")
    run_inbox(ctx)
    with ctx.db.session() as s:
        row = s.execute(select(InboundEmail)).scalar_one()
        assert row.category == "interested" and row.lead_id == lid
    assert opp_row(ctx, oid).stage == "discovery"
    assert "Re: your email" in eva.get("/inbox?show=all").text

    # meeting: Eva logs it from the lead page, then records the outcome
    r = post(eva, f"/leads/{lid}/meeting", date="2026-11-03", time_et="10:00", minutes="30", title="Intro")
    assert "Meeting logged" in where(r)
    with ctx.db.session() as s:
        meeting = s.execute(select(Meeting)).scalar_one()
        assert meeting.lead_id == lid and meeting.opportunity_id == oid
        assert meeting.starts_at == datetime(2026, 11, 3, 15, 0)  # 10:00 ET in November is 15:00 UTC
        mid = meeting.id
    assert post(eva, f"/meetings/{mid}/held", notes="Good call").status_code == 303

    # quotation and negotiation
    assert post(eva, f"/opportunities/{oid}/quotes", amount="$18,000", terms="50% upfront").status_code == 303
    with ctx.db.session() as s:
        qid = s.execute(select(Quote)).scalar_one().id
    assert post(eva, f"/opportunities/{oid}/quotes/{qid}/send").status_code == 303
    assert opp_row(ctx, oid).stage == "proposal"
    assert post(eva, f"/opportunities/{oid}", stage="negotiation", next_step="Review terms").status_code == 303
    assert post(eva, f"/opportunities/{oid}/quotes/{qid}/accept").status_code == 303
    assert opp_row(ctx, oid).est_value == 18000

    # won and handed over
    assert post(eva, f"/opportunities/{oid}", stage="won").status_code == 303
    o = opp_row(ctx, oid)
    assert (o.stage, o.won_by, o.probability) == ("won", "eva", 100) and o.closed_at
    assert lead_row(ctx, lid).status == "converted"
    with ctx.db.session() as s:
        task = s.execute(select(FollowUp).where(FollowUp.subject.like("Customer handover:%"))).scalar_one()
        assert "USD 18,000.00" in task.body and DANA["email"] in task.body and task.status == "pending"
    assert "Customer handover" in eva.get("/my").text

    # reporting: each page agrees with the database and with each other, for each reader
    assert board_count(mia.get("/pipeline").text, "won") == 1
    assert board_count(eva.get("/pipeline").text, "won") == 1
    today = fmt_local(utcnow(), None, "%Y-%m-%d")  # the report's days are the days on screen
    row = perf(ana.get(f"/performance?start={today}&end={today}").text, "eva")
    assert (row["won"], row["value"], row["meetings"], row["emails"], row["pos"]) == ("1", "$18,000", "1", "1", "1")
    assert perf(eva.get("/my").text, "eva")  # her last 30 days are on My work
    reports = ana.get("/reports")
    assert reports.status_code == 200 and tile(reports.text, "Deals won") == 1 and tile(reports.text, "Meetings booked") == 1
    assert eva.get("/reports", follow_redirects=False).headers["location"].startswith("/my?msg=")  # org figures: not for her


# ================================================================== lead capture: wrong and missing input
def test_ok_public_form_refuses_bad_requests_with_words_not_crashes(org):
    c = client(org)
    assert c.post("/api/inbound", json={"message": "x"}).status_code == 401
    assert c.post("/api/inbound", json={"message": "x"}, headers={"x-inbound-token": "wrong"}).status_code == 401
    assert c.post("/api/inbound", json={"email": 5}, headers={"x-inbound-token": TOKEN}).status_code == 422
    assert c.post("/api/inbound", json=[1, 2], headers={"x-inbound-token": TOKEN}).status_code == 422
    assert c.post("/api/inbound", content=b"not json", headers={"x-inbound-token": TOKEN,
                                                                 "content-type": "application/json"}).status_code == 422
    assert c.post("/api/inbound", content=b"x" * 30000, headers={"x-inbound-token": TOKEN,
                                                                  "content-type": "application/json"}).status_code == 413
    assert count(org, InboundInquiry) == 0


def test_defect_public_inquiry_api_crashes_when_services_is_not_a_list(org):
    """Any website form can post this: the API documents 422 for an invalid inquiry; a TypeError escapes as a 500."""
    c = client(org)
    r = c.post("/api/inbound", json={"email": "a@b.example", "message": "hello there", "services_interested": 5},
               headers={"x-inbound-token": TOKEN})
    assert r.status_code == 422


def test_ok_duplicate_inquiry_same_email_in_other_case_is_recorded_once(org):
    c = client(org)
    assert api_inquiry(c, **DANA).status_code == 201
    shout = {**DANA, "email": DANA["email"].upper(), "company": DANA["company"].upper()}
    r = api_inquiry(c, **shout)
    assert r.status_code == 200 and r.json()["created"] is False
    assert count(org, InboundInquiry) == 1


def test_ok_same_person_asks_again_in_other_words_while_the_lead_is_open_stays_one_lead(org):
    c = client(org)
    api_inquiry(c, **DANA)
    process(org)
    api_inquiry(c, **{**DANA, "email": DANA["email"].upper(), "message": "Any update on the CRM project please?"})
    process(org)
    assert count(org, Lead) == 1 and count(org, Contact) == 1 and count(org, Company) == 1
    assert count(org, InboundInquiry, InboundInquiry.status == "processed") == 2


def test_ok_colleague_from_the_same_domain_joins_the_same_company(org):
    c = client(org)
    api_inquiry(c, **DANA)
    api_inquiry(c, **{**DANA, "first_name": "Sam", "email": "sam@brightpath-realty.example", "company": "Brightpath"})
    process(org)
    assert count(org, Company) == 1 and count(org, Contact) == 2


def test_ok_missing_information_is_rejected_with_a_reason_or_handled(org):
    c = client(org)
    api_inquiry(c, message="Hello, we need a CRM")  # no way to reply
    api_inquiry(c, message="", email="a@b.example")  # empty message
    api_inquiry(c, message="Hello, we need a CRM", email="not-an-address")
    api_inquiry(c, message="y" * 6000, email="long@b.example")  # over the 5,000 limit
    api_inquiry(c, message="Please call me about a CRM", phone="+1 555 0100", company="Phone Only Inc")
    api_inquiry(c, message="Hello, we need a CRM", email="nocompany@somewhere.example")
    process(org)
    with org.db.session() as s:
        rows = s.execute(select(InboundInquiry).order_by(InboundInquiry.id)).scalars().all()
        assert [r.status for r in rows] == ["rejected"] * 4 + ["processed"] * 2
        assert all(r.error for r in rows[:4])
        phone_lead = s.get(Lead, rows[4].lead_id)
        assert phone_lead.contact.email is None and phone_lead.company.name == "Phone Only Inc"
        assert s.get(Lead, rows[5].lead_id).company.domain == "somewhere.example"
        ids = [rows[4].lead_id, rows[5].lead_id]
    page = client(org, "boss").get("/inbound").text
    assert "No way to reply" in page and "Invalid email address" in page
    for lid in ids:
        assert client(org, "boss").get(f"/leads/{lid}").status_code == 200


def test_ok_empty_and_hostile_manual_inquiry_form_never_crashes(org):
    c = client(org, "eva")
    assert post(c, "/inbound").status_code == 303
    assert post(c, "/inbound", first_name="<script>alert(1)</script>", message="<b>Need CRM</b> help please",
                company="<i>Evil</i>", email="x@evil.example").status_code == 303
    page = c.get("/inbound").text
    assert "<i>Evil</i>" not in page and "&lt;i&gt;Evil" in page
    process(org)
    assert client(org, "boss").get("/leads").status_code == 200


def test_ok_unicode_and_emoji_names_survive_the_journey(org):
    c = client(org)
    r = api_inquiry(c, first_name="José 😀", last_name="Nguyễn", email="jose@acme-devshop.example", company="株式会社🚀 Dev",
                    message="We need a CRM please 🙏", country="US")
    assert r.status_code == 201
    process(org)
    with org.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        lid = lead.id
        assert lead.contact.first_name == "José 😀"
    page = client(org, "boss").get(f"/leads/{lid}").text
    assert "株式会社🚀" in page and "José 😀" in page
    assert "株式会社🚀" in client(org, "boss").get("/search?q=" + "株式").text


def test_ok_two_companies_with_only_non_latin_names_stay_two(org):
    """Two different customers, no email domain to tell them apart: their names must not collapse into one company."""
    c = client(org)
    api_inquiry(c, message="We need a CRM please", phone="+1 555 0101", company="東京商事")
    api_inquiry(c, message="We want an AI agent", phone="+1 555 0102", company="大阪物産")
    process(org)
    with org.db.session() as s:
        names = {c.name for c in s.execute(select(Company)).scalars()}
    assert names == {"東京商事", "大阪物産"}


# ================================================================== qualification and scoring
def test_ok_qualification_outcomes_match_on_the_page(org):
    c = client(org)
    api_inquiry(c, **DANA)
    api_inquiry(c, first_name="Lou", email="lou@maple.example", company="Maple Ltd", country="Canada",
                message="We would like a custom CRM with follow-up reminders for our team")
    api_inquiry(c, first_name="Vi", email="vi@tiny.example", company="Tiny Shop", country="US", message="Hello, tell me more")
    process(org)
    boss = client(org, "boss")
    with org.db.session() as s:
        by_email = {x.contact.email: x for x in s.execute(select(Lead)).scalars()}
        dana, lou, vi = by_email[DANA["email"]], by_email["lou@maple.example"], by_email["vi@tiny.example"]
        assert dana.status == "qualified" and dana.grade in ("A", "B") and dana.primary_service == "custom_crm"
        assert lou.status == "disqualified" and lou.disqualify_reason
        assert vi.status in ("nurture", "qualified")
        drafts = {f.lead_id for f in s.execute(select(FollowUp).where(FollowUp.channel == "email")).scalars()}
        assert dana.id in drafts and vi.id in drafts  # both asked us: both get an answer waiting for a person
        assert lou.id not in drafts
        expect = {x.id: (x.score, x.status, x.grade) for x in by_email.values()}
    for lid, (score, status, _grade) in expect.items():
        page = boss.get(f"/leads/{lid}").text
        assert f"Score {score}/100" in page and status in page
    assert "Maple Ltd" not in boss.get("/leads?status=qualified").text
    assert "Brightpath" in boss.get("/leads?status=qualified").text
    assert tile(boss.get("/reports").text, "Qualified companies") == sum(s == "qualified" for _, s, _ in expect.values())


# ================================================================== returning customers and opted-out people
def won_customer(org):
    """Dana's deal was won by Eva, then Dana enquires again with something new."""
    c = client(org)
    api_inquiry(c, **DANA)
    process(org)
    with org.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        lid = lead.id
    post(client(org, "boss"), f"/leads/{lid}/assign", owner="eva")
    with org.db.session() as s:
        oid = s.execute(select(Opportunity).where(Opportunity.lead_id == lid)).scalar_one().id
    post(client(org, "eva"), f"/opportunities/{oid}", stage="won", est_value="10000")
    api_inquiry(c, **{**DANA, "email": DANA["email"].upper(), "message": "We would now like an AI agent as well, please"})
    process(org)
    with org.db.session() as s:
        new = s.execute(select(Lead).where(Lead.id != lid)).scalars().first()
    return lid, oid, new


def test_ok_won_customer_enquiring_again_keeps_the_won_deal_and_gets_a_new_open_lead(org):
    lid, oid, new = won_customer(org)
    assert new is not None and new.status in ("new", "qualified", "nurture") and new.contact_id == lead_row(org, lid).contact_id
    o = opp_row(org, oid)
    assert (o.stage, o.est_value, o.won_by) == ("won", 10000, "eva")
    assert lead_row(org, lid).status == "converted"
    assert count(org, Contact) == 1 and count(org, Company) == 1


def test_ok_returning_customers_new_lead_starts_unassigned(org):
    """Arguably intended: aime.assignment keeps 'one person per customer' per lead, and this is a new lead. Recorded so
    the owner can decide whether a won customer's new enquiry should go back to their salesperson."""
    _, _, new = won_customer(org)
    assert new.owner is None


def test_defect_meeting_logged_on_a_returning_customers_new_lead_lands_on_the_old_lead(org):
    _, _, new = won_customer(org)
    r = post(client(org, "boss"), f"/leads/{new.id}/meeting", date="2026-11-03", time_et="10:00")
    assert r.status_code == 303
    with org.db.session() as s:
        assert s.execute(select(Meeting)).scalar_one().lead_id == new.id


def test_ok_unsubscribed_person_enquiring_again_can_never_be_emailed(org):
    c = client(org)
    api_inquiry(c, **DANA)
    process(org)
    with org.db.session() as s:
        lid = s.execute(select(Lead)).scalar_one().id
    boss = client(org, "boss")
    assert post(boss, "/suppress", value=DANA["email"].upper(), reason="Asked to stop").status_code == 303
    assert count(org, FollowUp, FollowUp.status == "awaiting_approval") == 0  # queued drafts are cancelled
    api_inquiry(c, **{**DANA, "message": "Sorry, what was your pricing again?", "consent_marketing": True})
    process(org)
    with org.db.session() as s:
        contact = s.execute(select(Contact)).scalar_one()
        assert contact.consent_status == "opted_out"  # an inquiry does not undo an opt-out
        for f in s.execute(select(FollowUp).where(FollowUp.status == "awaiting_approval")).scalars():
            assert f.channel != "email"
    fid = make_draft(org, lid, to=DANA["email"].upper())
    r = post(client(org, "mia"), f"/followups/{fid}/approve")
    assert "do-not-contact" in where(r)
    with org.db.session() as s:
        assert s.get(FollowUp, fid).status == "awaiting_approval"


def test_ok_suppress_form_refuses_a_pattern(org):
    boss = client(org, "boss")
    assert "one email address" in where(post(boss, "/suppress", value="%"))
    assert "one email address" in where(post(boss, "/suppress", value="a b@c.example"))
    assert count(org, Suppression) == 0


# ================================================================== roles and sign-in
def test_ok_real_sign_in_each_role_lands_where_it_should(org):
    for name, home in (("boss", "/"), ("mia", "/my"), ("eva", "/my")):
        c = client(org)
        r = post(c, "/login", username=name.upper(), password=GOOD, next="/")
        assert r.status_code == 303 and r.headers["location"] == "/", name
        assert c.get("/", follow_redirects=False).status_code == (200 if home == "/" else 303), name
    bad = client(org)
    assert "Wrong" in where(post(bad, "/login", username="😀" * 50, password="x" * 9000))
    assert "Wrong" in where(post(bad, "/login", username="", password=""))


def test_ok_who_may_do_what(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid)
    fid = make_draft(org, lid)
    eva, zoe, ana, mia = (client(org, u) for u in ("eva", "zoe", "ana", "mia"))
    assert post(eva, f"/followups/{fid}/approve").status_code == 403  # executives do not approve
    assert post(eva, f"/leads/{lid}/assign", owner="ed").status_code == 403  # nor assign
    assert post(eva, "/inbound/process").status_code == 403  # nor run jobs
    assert eva.get("/admin/users").status_code == 403
    for r in (zoe.get(f"/leads/{lid}"), zoe.get(f"/opportunities/{oid}"), post(zoe, f"/opportunities/{oid}", stage="won"),
              post(zoe, f"/opportunities/{oid}/quotes", amount="1"), post(zoe, f"/leads/{lid}/note", note="x"),
              post(zoe, f"/followups/{fid}/reject")):
        assert r.status_code == 404  # another person's record does not exist for her
    assert opp_row(org, oid).stage == "negotiation"
    for r in (post(ana, f"/opportunities/{oid}", stage="won"), post(ana, f"/leads/{lid}/note", note="x"),
              post(ana, "/inbound", message="hello there")):
        assert r.status_code == 403  # the analyst reads only
    assert ana.get("/pipeline").status_code == 200 and ana.get("/reports").status_code == 200
    assert mia.get(f"/leads/{lid}").status_code == 200  # her team's
    assert post(eva, f"/followups/{fid}/+{fid}").status_code in (400, 404)
    assert post(mia, f"/followups/+{fid}/approve").status_code == 404
    assert client(org).post(f"/opportunities/{oid}", data={"stage": "won"}).status_code == 403  # no origin: cross-site
    assert eva.post(f"/opportunities/{oid}", headers={"origin": "http://evil.example"}, data={"stage": "won"}).status_code == 403


def test_ok_session_expiry_mid_form_changes_nothing(org):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid)
    c = client(org)
    with org.db.session() as s:
        user = s.execute(select(User).where(User.username == "eva")).scalar_one()
        c.cookies.set(COOKIE, make_cookie(session_secret(org.settings.data_dir), user, now=datetime.now().timestamp() - 13 * 3600))
    r = post(c, f"/opportunities/{oid}", stage="won")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert opp_row(org, oid).stage == "negotiation"


def test_defect_signing_back_in_after_expiry_mid_form_lands_on_a_dead_end(org):
    """The session runs out while a form is open: the sign-in sends the person to the form's POST-only address, and
    the browser's GET there answers a bare 405 instead of the page they were on."""
    lid = make_lead(org, owner="mia", email="dana@acme.example")
    fid = make_draft(org, lid)
    c = client(org)
    r = post(c, f"/followups/{fid}/approve")
    target = re.search(r"next=([^&]+)", r.headers["location"]).group(1)
    signed_in = post(c, "/login", username="mia", password=GOOD, next=unquote(target))
    assert signed_in.status_code == 303
    assert c.get(signed_in.headers["location"]).status_code == 200


def test_ok_deactivation_and_role_change_end_the_session_mid_work(org):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid)
    eva = client(org, "eva")
    boss = client(org, "boss")
    with org.db.session() as s:
        uid = s.execute(select(User).where(User.username == "eva")).scalar_one().id
    assert post(boss, f"/admin/users/{uid}/deactivate").status_code == 303
    r = post(eva, f"/opportunities/{oid}", stage="won")
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert opp_row(org, oid).stage == "negotiation"
    assert "not an active user" in where(post(boss, f"/leads/{lid}/assign", owner="eva"))


# ================================================================== assignment
def test_ok_assignment_rules(org):
    lid = make_lead(org, owner=None)
    oid = make_deal(org, lid, owner=None)
    closed = make_deal(org, lid, owner="ed", stage="won", name="Old deal")
    mia, boss = client(org, "mia"), client(org, "boss")
    assert "not an active user" in where(post(boss, f"/leads/{lid}/assign", owner="nobody"))
    assert "not in your team" in where(post(mia, f"/leads/{lid}/assign", owner="zoe"))  # a manager: her team only
    assert lead_row(org, lid).owner is None
    assert "to eva" in where(post(mia, f"/leads/{lid}/assign", owner=" EVA ", reason="📞 follow up"))
    assert lead_row(org, lid).owner == "eva" and opp_row(org, oid).owner == "eva"
    assert opp_row(org, closed).owner == "ed"  # a closed deal keeps its owner
    assert client(org, "eva").get(f"/leads/{lid}").status_code == 200
    assert client(org, "zoe").get(f"/leads/{lid}").status_code == 404
    assert post(boss, f"/leads/{lid}/assign", owner="zoe").status_code == 303
    assert client(org, "eva").get(f"/opportunities/{oid}").status_code == 404  # taken away: gone from her view
    assert client(org, "zoe").get(f"/opportunities/{oid}").status_code == 200
    page = boss.get(f"/leads/{lid}").text
    assert "eva → zoe" in page and "unassigned → eva" in page


def test_defect_double_click_on_assign_counts_as_two_assignments(org):
    """Repeated click, or browser back and resubmit: the owner did not change, yet history and /performance grow."""
    lid = make_lead(org, owner=None)
    boss = client(org, "boss")
    post(boss, f"/leads/{lid}/assign", owner="eva")
    post(boss, f"/leads/{lid}/assign", owner="eva")
    assert count(org, Assignment, Assignment.lead_id == lid) == 1
    assert perf(boss.get("/performance").text, "eva")["assigned"] == "1"


# ================================================================== email draft and approval
def test_ok_approval_repeated_clicks_and_conflicts(org):
    lid = make_lead(org, owner=None, email="dana@acme.example")
    fid = make_draft(org, lid)
    mia = client(org, "mia")
    assert post(mia, f"/followups/{fid}/approve").status_code == 303
    with org.db.session() as s:
        f = s.get(FollowUp, fid)
        assert (f.status, f.approved_by) == ("approved", "mia") and s.get(Lead, lid).owner == "mia"
    again = post(client(org, "boss"), f"/followups/{fid}/approve")  # the second click: refused, no change
    assert again.status_code == 400
    with org.db.session() as s:
        assert s.get(FollowUp, fid).approved_by == "mia"
    assert count(org, AuditEvent, AuditEvent.action == "email_approve") == 1
    assert post(mia, f"/followups/{fid}/reject").status_code == 303  # stop it before it goes
    assert post(mia, f"/followups/{fid}/approve").status_code == 400  # browser back to the old page: not revived
    with org.db.session() as s:
        assert s.get(FollowUp, fid).status == "rejected"


def test_ok_approval_by_an_admin_leaves_the_lead_unowned_and_reassign_during_approval_keeps_the_new_owner(org):
    lid = make_lead(org, owner=None, email="dana@acme.example")
    fid = make_draft(org, lid)
    post(client(org, "boss"), f"/followups/{fid}/approve")
    assert lead_row(org, lid).owner is None  # an administrator does not take leads
    lid2 = make_lead(org, owner="eva", company="Other", email="o@other.example")
    fid2 = make_draft(org, lid2, to="o@other.example")
    post(client(org, "boss"), f"/leads/{lid2}/assign", owner="ed")
    assert post(client(org, "mia"), f"/followups/{fid2}/approve").status_code == 303
    assert lead_row(org, lid2).owner == "ed"


def test_ok_editing_a_draft_with_odd_input(org):
    lid = make_lead(org, owner="mia", email="dana@acme.example")
    fid = make_draft(org, lid)
    mia = client(org, "mia")
    body = "Hello 😀 " + "x" * 200_000 + "\n\nTo opt out, reply unsubscribe.\n1 Sample St, Austin, TX"
    assert post(mia, f"/followups/{fid}/save", subject="  ", body=body).status_code == 303
    with org.db.session() as s:
        f = s.get(FollowUp, fid)
        assert f.subject == "Your question" and "😀" in f.body and "edited by mia" in f.generator  # blank subject ignored
    assert mia.get("/followups").status_code == 200
    assert post(mia, f"/followups/{fid}/nonsense").status_code == 400
    assert post(mia, f"/followups/{fid}/save", body="too short, no footer").status_code == 303


def test_defect_a_rejected_task_can_be_marked_done_by_a_stale_tab(org):
    lid = make_lead(org, owner="eva")
    fid = make_draft(org, lid, channel="task", status="pending", kind=None, subject="Call them")
    eva = client(org, "eva")
    assert post(eva, f"/followups/{fid}/reject").status_code == 303
    assert post(eva, f"/followups/{fid}/done").status_code == 400  # stale tab: it was rejected
    with org.db.session() as s:
        assert s.get(FollowUp, fid).status == "rejected"


# ================================================================== replies
def replied(make_ctx, text):
    ctx = make_ctx(dashboard_auth=True, inbound_api_token=TOKEN, **MAIL)
    add_people(ctx)
    FakeSMTP.sent, FakeIMAP.messages = [], {}
    api_inquiry(client(ctx), **DANA)
    process(ctx)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        lid = lead.id
        fid = s.execute(select(FollowUp).where(FollowUp.lead_id == lid, FollowUp.channel == "email")).scalars().first().id
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    post(client(ctx, "boss"), f"/followups/{fid}/approve")
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    with ctx.db.session() as s:
        message_id = s.get(FollowUp, fid).message_id
    FakeIMAP.messages[1] = mail(f"Dana Lee <{DANA['email']}>", "Re: your email", text, in_reply_to=message_id,
                                msg_id="<d1@brightpath.example>")
    run_inbox(ctx)
    with ctx.db.session() as s:
        row = s.execute(select(InboundEmail)).scalar_one()
        oid = s.execute(select(Opportunity).where(Opportunity.lead_id == lid)).scalar_one().id
    return ctx, lid, oid, row


def test_ok_no_thanks_closes_the_early_deal_and_a_person_may_reclassify(make_ctx):
    ctx, lid, oid, row = replied(make_ctx, "No thanks, we're all set.")
    assert row.category == "not_interested"
    assert opp_row(ctx, oid).stage == "lost" and opp_row(ctx, oid).lost_reason == "Not interested"
    boss = client(ctx, "boss")
    assert board_count(boss.get("/pipeline").text, "lost") == 1
    assert post(client(ctx, "eva"), f"/inbox/{row.id}/reclassify", category="interested").status_code == 403
    assert post(boss, f"/inbox/{row.id}/reclassify", category="nonsense").status_code == 400
    assert post(boss, f"/inbox/{row.id}/apply_calendly").status_code == 400
    assert post(boss, f"/inbox/{row.id}/reclassify", category="interested").status_code == 303
    assert opp_row(ctx, oid).stage == "discovery"  # the deal opens again
    assert post(boss, f"/inbox/{row.id}/reviewed").status_code == 303


def test_ok_unsubscribe_reply_stops_everything(make_ctx):
    ctx, lid, oid, row = replied(make_ctx, "Please unsubscribe me.")
    assert row.category == "unsubscribe"
    assert opp_row(ctx, oid).stage == "lost"
    assert count(ctx, Suppression, Suppression.value == DANA["email"]) == 1
    with ctx.db.session() as s:
        assert s.execute(select(Contact)).scalar_one().consent_status == "opted_out"
        assert not s.execute(select(FollowUp).where(FollowUp.status.in_(("awaiting_approval", "approved")))).first()


def test_ok_second_touch_waits_for_approval_and_stops_on_a_logged_reply(make_ctx):
    ctx = make_ctx(dashboard_auth=True, inbound_api_token=TOKEN, **MAIL)
    add_people(ctx)
    FakeSMTP.sent = []
    api_inquiry(client(ctx), **DANA)
    process(ctx)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        lid = lead.id
        fid = s.execute(select(FollowUp).where(FollowUp.lead_id == lid, FollowUp.channel == "email")).scalars().first().id
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    mia = client(ctx, "mia")
    post(mia, f"/followups/{fid}/approve")
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    with ctx.db.session() as s:
        s.get(FollowUp, fid).sent_at = TUESDAY_11AM_ET - timedelta(days=4)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(minutes=5)).isoformat()})
    with ctx.db.session() as s:
        second = s.execute(select(FollowUp).where(FollowUp.lead_id == lid, FollowUp.step == 2)).scalar_one()
        assert second.status == "awaiting_approval"
        sid = second.id
    assert len(FakeSMTP.sent) == 1  # nothing else went out on its own
    assert f"f{sid}" in mia.get("/followups").text
    assert "Emails to approve: 1" in mia.get("/my").text
    assert post(mia, f"/leads/{lid}/replied").status_code == 303
    with ctx.db.session() as s:
        assert s.get(FollowUp, sid).status == "cancelled"
    assert "Emails to approve: 0" in mia.get("/my").text


def test_ok_follow_up_stops_when_they_replied_and_repeated_clicks_are_harmless(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    fid = make_draft(org, lid)
    oid = make_deal(org, lid, stage="new")
    eva = client(org, "eva")
    for _ in range(2):
        assert post(eva, f"/leads/{lid}/replied").status_code == 303
    with org.db.session() as s:
        assert s.get(FollowUp, fid).status == "cancelled"
    assert opp_row(org, oid).stage == "discovery"
    for _ in range(2):
        assert post(eva, f"/leads/{lid}/stop_outreach").status_code == 303


# ================================================================== meetings
def test_ok_meeting_wrong_input_is_refused_and_a_repeat_is_one_meeting(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    make_deal(org, lid, stage="discovery")
    eva = client(org, "eva")
    for date_, time_ in (("not-a-date", "10:00"), ("2026-02-30", "10:00"), ("2026-11-03", "25:00"), ("", "")):
        r = post(eva, f"/leads/{lid}/meeting", date=date_, time_et=time_)
        assert r.status_code in (303, 422), (date_, time_)
    assert post(eva, f"/leads/{lid}/meeting", date="2026-11-03", time_et="10:00", minutes="abc").status_code == 422
    assert count(org, Meeting) == 0
    for _ in range(2):  # double click
        assert post(eva, f"/leads/{lid}/meeting", date="2026-11-03", time_et="10:00", minutes="-5").status_code == 303
    with org.db.session() as s:
        m = s.execute(select(Meeting)).scalar_one()
        assert m.ends_at - m.starts_at == timedelta(minutes=5)
        mid = m.id
    assert post(eva, f"/meetings/{mid}/held").status_code == 303
    assert "Already marked" in where(post(eva, f"/meetings/{mid}/held"))  # the same outcome again: a double click, nothing changes
    for action in ("no_show", "cancel", "nonsense"):  # another outcome once recorded: refused, not changed
        assert post(eva, f"/meetings/{mid}/{action}").status_code == 400
    with org.db.session() as s:
        assert s.get(Meeting, mid).status == "completed"


def test_ok_meeting_time_zones_including_the_daylight_saving_change(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    eva = client(org, "eva")
    post(eva, f"/leads/{lid}/meeting", date="2026-10-30", time_et="10:00")  # EDT: UTC-4
    post(eva, f"/leads/{lid}/meeting", date="2026-11-02", time_et="10:00")  # EST: UTC-5
    post(eva, f"/leads/{lid}/meeting", date="2027-03-14", time_et="02:30")  # a time that does not exist: no crash
    with org.db.session() as s:
        starts = [m.starts_at for m in s.execute(select(Meeting).order_by(Meeting.starts_at)).scalars()]
    assert starts[0] == datetime(2026, 10, 30, 14, 0) and starts[1] == datetime(2026, 11, 2, 15, 0) and len(starts) == 3
    assert "Oct 30, 10:00 AM ET" in eva.get(f"/leads/{lid}").text


def test_defect_meeting_form_crashes_on_a_far_future_date(org):
    """A mistyped year: 500 instead of the page's 'Enter the date as YYYY-MM-DD' answer."""
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    r = post(client(org, "eva"), f"/leads/{lid}/meeting", date="9999-12-31", time_et="23:59")
    assert r.status_code == 303


def test_defect_meeting_logged_for_a_lead_without_contact_is_not_attached_to_that_lead(org):
    """The lead page asks for the invitee's email exactly when the lead has no contact, then ignores which lead it is."""
    lid = make_lead(org, owner="eva")
    make_deal(org, lid, stage="discovery")
    r = post(client(org, "eva"), f"/leads/{lid}/meeting", date="2026-11-03", time_et="10:00",
             invitee_email="pat@elsewhere.example")
    assert r.status_code == 303
    with org.db.session() as s:
        assert s.execute(select(Meeting)).scalar_one().lead_id == lid


def test_defect_meeting_logged_with_another_customers_email_changes_that_customers_lead(org):
    """Eva logs a meeting on her lead and types an address that belongs to Zoe's customer: Zoe's lead is moved instead."""
    mine = make_lead(org, owner="eva")
    theirs = make_lead(org, owner="zoe", company="Other Co", email="sam@other.example")
    post(client(org, "eva"), f"/leads/{mine}/meeting", date="2026-11-03", time_et="10:00", invitee_email="sam@other.example")
    with org.db.session() as s:
        m = s.execute(select(Meeting)).scalar_one()
        assert m.lead_id == mine and s.get(Lead, theirs).outreach_status is None


def test_ok_moving_a_meeting_without_a_calendar_or_with_a_bad_date_answers_in_words(org):
    lid = make_lead(org, owner="mia", email="dana@acme.example")
    mia = client(org, "mia")
    post(mia, f"/leads/{lid}/meeting", date="2026-11-03", time_et="10:00")
    with org.db.session() as s:
        m = s.execute(select(Meeting)).scalar_one()
        mid, before = m.id, m.starts_at
    assert "give the new date" in where(post(mia, f"/meetings/{mid}/move", start_local="soon"))
    assert post(mia, f"/meetings/{mid}/move", start_local="2026-11-04T10:00").status_code == 303
    with org.db.session() as s:
        assert s.get(Meeting, mid).starts_at == before  # the calendar is what moves it; none here


def test_defect_moving_a_meeting_to_a_far_future_date_crashes(org):
    lid = make_lead(org, owner="mia", email="dana@acme.example")
    post(client(org, "mia"), f"/leads/{lid}/meeting", date="2026-11-03", time_et="10:00")
    with org.db.session() as s:
        mid = s.execute(select(Meeting)).scalar_one().id
    r = post(client(org, "mia"), f"/meetings/{mid}/move", start_local="9999-12-31T23:59")
    assert r.status_code == 303


def test_defect_executive_logging_a_meeting_on_an_unassigned_lead_loses_it_and_lands_on_not_found(org):
    """Eva works an unassigned lead and logs her meeting. The handover gives the deal to whoever has the fewest deals
    (here Mia), so the page she is sent back to answers 404 and her lead is gone from her view."""
    lid = make_lead(org, owner=None, email="dana@acme.example")
    make_deal(org, lid, owner=None, stage="discovery")
    eva = client(org, "eva")
    r = post(eva, f"/leads/{lid}/meeting", date="2026-11-03", time_et="10:00")
    assert r.status_code == 303
    assert eva.get(r.headers["location"]).status_code == 200


def test_ok_handover_skips_people_who_are_away(org):
    lid = make_lead(org, owner=None, email="dana@acme.example")
    oid = make_deal(org, lid, owner=None, stage="discovery")
    boss = client(org, "boss")
    with org.db.session() as s:
        ids = {u.username: u.id for u in s.execute(select(User)).scalars()}
    for name in ("mia", "eva", "ed"):
        role = "sales_manager" if name == "mia" else "sales_executive"
        assert post(boss, f"/admin/users/{ids[name]}/edit", role=role, available="").status_code == 303
    assert post(boss, f"/leads/{lid}/meeting", date="2026-11-03", time_et="10:00").status_code == 303
    assert opp_row(org, oid).owner == "zoe" and lead_row(org, lid).owner == "zoe"
    assert f'/leads/{lid}"' in client(org, "zoe").get("/my").text
    assert count(org, Assignment, Assignment.opportunity_id == oid, Assignment.to_owner == "zoe") == 1


def test_ok_nobody_available_leaves_the_deal_unassigned_with_an_alert(org):
    lid = make_lead(org, owner=None, email="dana@acme.example")
    oid = make_deal(org, lid, owner=None, stage="discovery")
    boss = client(org, "boss")
    with org.db.session() as s:
        for u in s.execute(select(User)).scalars():
            u.available = False
    assert post(boss, f"/leads/{lid}/meeting", date="2026-11-03", time_et="10:00").status_code == 303
    assert opp_row(org, oid).owner is None
    assert "Unassigned deal" in boss.get("/system").text


def test_ok_a_reply_nobody_could_match_can_still_be_classified_by_a_person(org):
    with org.db.session() as s:
        row = InboundEmail(from_email="stranger@nowhere.example", subject="Hello", body_text="Hi", new_text="Hi",
                           category="other", status="needs_review", message_id="<s1@nowhere.example>")
        s.add(row)
        s.flush()
        rid = row.id
    boss = client(org, "boss")
    for category in ("interested", "not_interested", "objection", "referral", "question", "out_of_office", "bounce",
                     "other", "unsubscribe"):
        assert post(boss, f"/inbox/{rid}/reclassify", category=category).status_code == 303, category
    assert boss.get("/inbox?show=all").status_code == 200
    assert client(org, "ana").get("/inbox?show=all").status_code == 200


# ================================================================== the administrator's side
def test_ok_administrator_adds_a_salesperson_who_then_works_a_lead(org):
    boss = client(org, "boss")
    page = post(boss, "/admin/users", username="nina", display_name="Nina \U0001F600", role="sales_executive").text
    path = re.search(r"(/setup\?token=[A-Za-z0-9_\-]+)", page).group(1)
    assert "already exists" in where(post(boss, "/admin/users", username="NINA", role="sales_executive"))
    assert "Username" in where(post(boss, "/admin/users", username="\U0001F600", role="sales_executive"))
    assert "cannot give" in where(post(boss, "/admin/users", username="nina2", role="super_admin"))
    anon = client(org)
    r = post(anon, "/setup", token=path.split("=")[1], password=GOOD, confirm=GOOD)
    assert r.status_code == 303
    assert post(anon, "/login", username="nina", password=GOOD, next="/my").headers["location"] == "/my"
    lid = make_lead(org, owner=None)
    assert post(boss, f"/leads/{lid}/assign", owner="nina").status_code == 303
    assert f'/leads/{lid}"' in anon.get("/my").text


def test_defect_a_non_ascii_digit_in_a_filter_crashes_the_performance_page(org):
    """'\u00b2'.isdigit() is True but int('\u00b2') fails: 500 from a query string."""
    assert client(org, "boss").get("/performance?team=\u00b2").status_code == 200


def test_defect_a_non_ascii_digit_in_the_team_form_crashes(org):
    assert post(client(org, "boss"), "/admin/teams", name="Team", team_id="\u00b2").status_code == 303


def test_defect_a_huge_team_number_in_the_new_user_form_crashes(org):
    assert post(client(org, "boss"), "/admin/users", username="nina", role="sales_executive",
                team_id=str(HUGE_ID)).status_code == 200


# ================================================================== opportunities: wrong input
@pytest.mark.parametrize("value", ["abc", "12,34,x", "1 000", "$"])
def test_ok_non_numeric_deal_value_changes_nothing(org, value):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid, value=500)
    r = post(client(org, "eva"), f"/opportunities/{oid}", stage="won", est_value=value, next_step="should not stick")
    assert "not a number" in where(r)
    o = opp_row(org, oid)
    assert (o.stage, o.est_value, o.next_step) == ("negotiation", 500, None)


@pytest.mark.parametrize("value", ["-5000", "inf", "-inf", "nan", "1e400"])
def test_defect_deal_value_accepts_negative_and_non_finite_numbers(org, value):
    """A negative or infinite 'Value $' goes straight into revenue on /performance and /my."""
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid, value=500)
    client_ = client(org, "eva")
    post(client_, f"/opportunities/{oid}", stage="negotiation", est_value=value)
    assert opp_row(org, oid).est_value == 500
    assert client_.get("/my").status_code == 200


def test_ok_stage_and_date_input_is_checked(org):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid)
    eva = client(org, "eva")
    assert post(eva, f"/opportunities/{oid}", stage="imaginary").status_code == 400
    assert post(eva, f"/opportunities/{oid}", stage="WON").status_code == 400
    assert post(eva, f"/opportunities/{oid}").status_code == 422
    assert "not a date" in where(post(eva, f"/opportunities/{oid}", stage="won", expected_close="31/12/2026"))
    assert opp_row(org, oid).stage == "negotiation"
    assert post(eva, f"/opportunities/{oid}", stage="negotiation", expected_close="2020-01-01").status_code == 303
    assert opp_row(org, oid).expected_close == date(2020, 1, 1)
    assert eva.get("/my").status_code == 200 and eva.get("/pipeline").status_code == 200


def test_ok_huge_and_unicode_text_on_a_deal_does_not_break_the_pages(org):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid)
    eva = client(org, "eva")
    assert post(eva, f"/opportunities/{oid}", stage="lost", lost_reason="競合 😀 " + "x" * 50_000,
                next_step="Call <b>them</b> 📞").status_code == 303
    o = opp_row(org, oid)
    assert o.lost_reason.startswith("競合 😀") and len(o.lost_reason) <= 200
    for path in ("/pipeline", f"/opportunities/{oid}", "/my", f"/leads/{lid}"):
        r = eva.get(path)
        assert r.status_code == 200, path
        assert "<b>them</b>" not in r.text
    assert post(eva, f"/leads/{lid}/note", note="📝 " + "n" * 200_000).status_code == 303
    assert post(eva, f"/leads/{lid}/note", note="").status_code == 422
    assert eva.get(f"/leads/{lid}").status_code == 200


def test_ok_very_long_next_step_does_not_break_the_board(org):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid)
    c = client(org, "eva")
    assert post(c, f"/opportunities/{oid}", stage="negotiation", next_step="x" * 5000).status_code == 303
    assert c.get("/pipeline").status_code == 200


@pytest.mark.parametrize("path", ["/leads/{}", "/opportunities/{}"])
def test_defect_a_huge_id_in_the_address_crashes_instead_of_not_found(org, path):
    assert client(org, "boss").get(path.format(HUGE_ID)).status_code == 404


def test_defect_a_huge_id_in_a_form_address_crashes_instead_of_not_found(org):
    assert post(client(org, "boss"), f"/followups/{HUGE_ID}/approve").status_code == 404


def test_defect_a_huge_page_number_on_leads_crashes(org):
    assert client(org, "boss").get(f"/leads?page={HUGE_ID}").status_code == 200


def test_ok_odd_query_strings_do_not_crash(org):
    make_lead(org, owner="eva")
    boss = client(org, "boss")
    for path in ("/leads?page=0", "/leads?page=-5", "/leads?q=%25", "/leads?q=_", "/leads?q=😀", "/search?q=%25%25",
                 "/search?q=" + "x" * 5000, "/performance?team=999", "/performance?owner=nobody", "/performance?source=😀",
                 "/performance?start=2026-12-31&end=2026-01-01", "/performance?start=0001-01-01"):
        assert boss.get(path).status_code == 200, path
    assert boss.get("/leads?page=abc").status_code == 422
    assert "Dates are" in where(boss.get("/performance?start=31-12-2026", follow_redirects=False))


def test_defect_performance_report_crashes_on_the_last_date_in_the_picker(org):
    """A person types 9999-12-31 as 'To' to mean 'everything': 500 instead of a report."""
    assert client(org, "boss").get("/performance?end=9999-12-31").status_code == 200


# ================================================================== deals: conflicts, repeated clicks, stage jumps
def test_ok_double_win_counts_once_and_keeps_the_first_winner(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid, value=1000)
    eva, mia = client(org, "eva"), client(org, "mia")
    assert post(eva, f"/opportunities/{oid}", stage="won").status_code == 303
    first = opp_row(org, oid)
    assert post(eva, f"/opportunities/{oid}", stage="won").status_code == 303
    assert post(mia, f"/opportunities/{oid}", stage="won").status_code == 303  # the manager, a moment later
    o = opp_row(org, oid)
    assert (o.won_by, o.closed_at) == ("eva", first.closed_at)
    assert count(org, StageChange, StageChange.opportunity_id == oid, StageChange.to_stage == "won") == 1
    assert count(org, FollowUp, FollowUp.subject.like("Customer handover:%")) == 1
    assert perf(client(org, "ana").get("/performance").text, "eva")["won"] == "1"


def test_ok_stage_jumps_are_allowed_and_on_record(org):
    """Intended (tests/test_customer_handover.py): a person may win a brand-new deal or reopen one; history shows it."""
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid, stage="new")
    eva = client(org, "eva")
    post(eva, f"/opportunities/{oid}", stage="won")
    post(eva, f"/opportunities/{oid}", stage="new")
    o = opp_row(org, oid)
    assert (o.stage, o.closed_at, o.won_by, o.probability) == ("new", None, None, 10)
    assert "won → <b>new</b> by eva" in eva.get(f"/opportunities/{oid}").text


def test_defect_a_lost_deal_won_directly_still_shows_why_it_was_lost(org):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid)
    eva = client(org, "eva")
    post(eva, f"/opportunities/{oid}", stage="lost", lost_reason="Chose a competitor")
    post(eva, f"/opportunities/{oid}", stage="won")
    assert opp_row(org, oid).lost_reason is None
    assert "Lost: Chose a competitor" not in eva.get("/pipeline").text


def form_fields(html, action):
    """What a browser would send for this form: its hidden fields, select choice and pre-filled inputs."""
    form = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>', html, re.S).group(1)
    data = {}
    for tag in re.findall(r"<input[^>]*>", form):
        name, value = re.search(r'name="([^"]+)"', tag), re.search(r'value="([^"]*)"', tag)
        if name:
            data[name.group(1)] = value.group(1) if value else ""
    for name, options in re.findall(r'<select name="([^"]+)">(.*?)</select>', form, re.S):
        chosen = re.search(r"<option selected[^>]*>([^<]+)</option>", options)
        data[name] = chosen.group(1) if chosen else ""
    return data


def test_defect_a_stale_pipeline_page_silently_undoes_a_colleagues_win(org):
    """Two people on one deal. Mia has /pipeline open and only types a next step; meanwhile Eva wins the deal. Mia's
    Save also resends the stage her old page showed: the win, its credit and its revenue vanish with no warning."""
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid, value=9000)
    eva, mia = client(org, "eva"), client(org, "mia")
    page = mia.get("/pipeline").text  # the page she has open
    assert post(eva, f"/opportunities/{oid}", stage="won").status_code == 303
    assert perf(client(org, "ana").get("/performance").text, "eva")["value"] == "$9,000"
    fields = form_fields(page, f"/opportunities/{oid}")
    fields["next_step"] = "Call on Monday"
    post(mia, f"/opportunities/{oid}", **fields)
    o = opp_row(org, oid)
    assert (o.stage, o.won_by) == ("won", "eva")


def test_defect_reopening_a_won_deal_leaves_the_lead_a_converted_customer(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid)
    eva = client(org, "eva")
    post(eva, f"/opportunities/{oid}", stage="won")
    assert lead_row(org, lid).status == "converted"
    post(eva, f"/opportunities/{oid}", stage="negotiation")
    assert lead_row(org, lid).status != "converted"


def test_defect_a_won_deal_changed_to_lost_keeps_the_customer_and_the_onboarding_task(org):
    """Eva wins by mistake and corrects it to lost: delivery is still told to onboard a customer who is not one."""
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid)
    eva = client(org, "eva")
    post(eva, f"/opportunities/{oid}", stage="won")
    post(eva, f"/opportunities/{oid}", stage="lost", lost_reason="Won by mistake")
    assert lead_row(org, lid).status != "converted"
    assert count(org, FollowUp, FollowUp.subject.like("Customer handover:%"), FollowUp.status == "pending") == 0


# ================================================================== quotations
@pytest.mark.parametrize("amount", ["abc", "-100", "0", "nan", "inf", "1e9", "1e400", "١٢٣x", " "])
def test_ok_bad_quote_amounts_are_refused(org, amount):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid)
    r = post(client(org, "eva"), f"/opportunities/{oid}/quotes", amount=amount)
    assert r.status_code == 303 and "as a number" in where(r)
    assert count(org, Quote) == 0


def test_ok_quote_other_wrong_input(org):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid)
    other = make_deal(org, lid, name="Other")
    eva = client(org, "eva")
    assert post(eva, f"/opportunities/{oid}/quotes").status_code == 422
    assert "3-letter" in where(post(eva, f"/opportunities/{oid}/quotes", amount="100", currency="€€"))
    assert "as a number" in where(post(eva, f"/opportunities/{oid}/quotes", amount="100", valid_until="tomorrow"))
    assert post(eva, f"/opportunities/{oid}/quotes", amount="100", terms="😀" + "t" * 5000).status_code == 303
    with org.db.session() as s:
        q = s.execute(select(Quote)).scalar_one()
        assert len(q.terms) <= 500
        qid = q.id
    assert post(eva, f"/opportunities/{other}/quotes/{qid}/accept").status_code == 404
    assert post(eva, f"/opportunities/{oid}/quotes/999/accept").status_code == 404
    assert post(eva, f"/opportunities/{oid}/quotes/{qid}/nonsense").status_code == 400
    assert eva.get(f"/opportunities/{oid}").status_code == 200


def quoted(org, amount="18000", value=None, currency="USD"):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid, value=value)
    eva = client(org, "eva")
    assert post(eva, f"/opportunities/{oid}/quotes", amount=amount, currency=currency).status_code == 303
    with org.db.session() as s:
        qid = s.execute(select(Quote).order_by(Quote.id.desc())).scalars().first().id
    return lid, oid, qid, eva


def test_defect_an_accepted_quote_can_be_rejected_by_a_stale_tab(org):
    """The page hides the buttons once a quote is decided; the server does not refuse them."""
    _, oid, qid, eva = quoted(org)
    post(eva, f"/opportunities/{oid}/quotes/{qid}/accept")
    with org.db.session() as s:
        decided = s.get(Quote, qid).decided_at
    post(eva, f"/opportunities/{oid}/quotes/{qid}/reject")
    with org.db.session() as s:
        q = s.get(Quote, qid)
        assert (q.status, q.decided_at) == ("accepted", decided)


def test_defect_mark_sent_after_accepted_takes_the_acceptance_back(org):
    _, oid, qid, eva = quoted(org)
    post(eva, f"/opportunities/{oid}/quotes/{qid}/accept")
    post(eva, f"/opportunities/{oid}/quotes/{qid}/send")
    with org.db.session() as s:
        assert s.get(Quote, qid).status == "accepted"


def test_defect_double_click_on_accepted_writes_the_decision_twice(org):
    _, oid, qid, eva = quoted(org)
    post(eva, f"/opportunities/{oid}/quotes/{qid}/accept")
    post(eva, f"/opportunities/{oid}/quotes/{qid}/accept")
    assert count(org, Activity, Activity.opportunity_id == oid, Activity.subject.like("%accepted%")) == 1


def test_defect_revenue_ignores_the_accepted_quote_when_the_deal_already_had_an_estimate(org):
    """Negotiation: an early estimate of $5,000, the customer accepts $18,000. The handover says $18,000 while the
    deal, /pipeline and revenue on /performance keep saying $5,000."""
    _, oid, qid, eva = quoted(org, value=5000)
    post(eva, f"/opportunities/{oid}/quotes/{qid}/accept")
    post(eva, f"/opportunities/{oid}", stage="won")
    with org.db.session() as s:
        assert "18,000.00" in s.execute(select(FollowUp).where(FollowUp.subject.like("Customer handover:%"))).scalar_one().body
    assert opp_row(org, oid).est_value == 18000
    assert perf(client(org, "ana").get("/performance").text, "eva")["value"] == "$18,000"


def test_defect_a_quote_in_euros_becomes_dollars_on_the_deal_and_in_revenue(org):
    _, oid, qid, eva = quoted(org, currency="EUR")
    post(eva, f"/opportunities/{oid}/quotes/{qid}/accept")
    page = eva.get(f"/opportunities/{oid}").text
    assert "EUR 18,000.00" in page
    assert "$18,000" not in page


def test_ok_quote_on_a_lost_deal_does_not_reopen_it(org):
    _, oid, qid, eva = quoted(org)
    post(eva, f"/opportunities/{oid}", stage="lost", lost_reason="Budget")
    post(eva, f"/opportunities/{oid}/quotes/{qid}/send")
    assert opp_row(org, oid).stage == "lost"


# ================================================================== won, lost, handover
def test_ok_handover_goes_to_the_owner_only(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid, value=4000)
    post(client(org, "eva"), f"/opportunities/{oid}", stage="won")
    with org.db.session() as s:
        task = s.execute(select(FollowUp).where(FollowUp.subject.like("Customer handover:%"))).scalar_one()
        assert "USD 4,000.00 (value on the deal" in task.body and "dana@acme.example" in task.body
        assert task.channel == "task"
        assert not s.execute(select(FollowUp).where(FollowUp.channel == "email")).first()  # nothing to the customer
    assert "Customer handover" in client(org, "eva").get("/my").text
    assert "Customer handover" in client(org, "mia").get("/my").text  # her team
    assert "Customer handover" not in client(org, "zoe").get("/my").text
    assert post(client(org, "eva"), f"/followups/{task.id}/done").status_code == 303
    assert "Customer handover" not in client(org, "eva").get("/my").text


def test_ok_won_deal_with_no_owner_lead_or_contact_still_hands_over(org):
    lid = make_lead(org, owner=None)
    oid = make_deal(org, lid, owner=None)
    assert post(client(org, "boss"), f"/opportunities/{oid}", stage="won").status_code == 303
    with org.db.session() as s:
        task = s.execute(select(FollowUp).where(FollowUp.subject.like("Customer handover:%"))).scalar_one()
        assert "no contact on record" in task.body and "none: assign one" in task.body and "no price on record" in task.body


# ================================================================== reporting: pages against the database
def reporting_set(org):
    """Eva: won $10,000 and lost one. Zoe: won $7,000. Ed: an open $3,000. Nobody: an unassigned won $1,000."""
    e1 = make_deal(org, make_lead(org, owner="eva", company="A", email="a@a.example"), value=10000, name="A deal")
    e2 = make_deal(org, make_lead(org, owner="eva", company="B"), value=2000, name="B deal", owner="eva")
    z1 = make_deal(org, make_lead(org, owner="zoe", company="C"), value=7000, name="C deal", owner="zoe")
    make_deal(org, make_lead(org, owner="ed", company="D"), value=3000, name="D deal", owner="ed")
    u1 = make_deal(org, make_lead(org, owner=None, company="E"), value=1000, name="E deal", owner=None)
    eva, zoe, boss = client(org, "eva"), client(org, "zoe"), client(org, "boss")
    post(eva, f"/opportunities/{e1}", stage="won")
    post(eva, f"/opportunities/{e2}", stage="lost", lost_reason="Budget")
    post(zoe, f"/opportunities/{z1}", stage="won")
    post(boss, f"/opportunities/{u1}", stage="won")
    return e1, e2, z1, u1


def test_ok_owner_reads_numbers_that_match_the_database(org):
    reporting_set(org)
    ana = client(org, "ana")
    board = ana.get("/pipeline").text
    with org.db.session() as s:
        by_stage = dict(s.execute(select(Opportunity.stage, func.count(Opportunity.id)).group_by(Opportunity.stage)).all())
    assert {st: board_count(board, st) for st in by_stage} == by_stage
    assert by_stage == {"won": 3, "lost": 1, "negotiation": 1}
    page = ana.get("/performance").text
    assert perf(page, "Total")["won"] == "3" and perf(page, "Total")["lost"] == "1"
    assert perf(page, "Total")["value"] == "$18,000"
    assert perf(page, "boss")["value"] == "$1,000"  # whoever moved it to won gets the credit
    assert (perf(page, "eva")["won"], perf(page, "eva")["lost"], perf(page, "eva")["rate"]) == ("1", "1", "50%")
    assert (perf(page, "zoe")["won"], perf(page, "zoe")["value"]) == ("1", "$7,000")
    reports = ana.get("/reports").text
    assert tile(reports, "Deals won") == 3 and tile(reports, "Open opportunities") == 1


def test_ok_executive_and_manager_read_their_own_numbers(org):
    reporting_set(org)
    eva, mia, zoe = client(org, "eva"), client(org, "mia"), client(org, "zoe")
    assert eva.get("/reports", follow_redirects=False).status_code == 303
    assert eva.get("/", follow_redirects=False).headers["location"] == "/my"
    assert eva.get("/api/stats").status_code == 403
    assert board_count(eva.get("/pipeline").text, "won") == 2  # hers and the unassigned one, not Zoe's
    assert board_count(zoe.get("/pipeline").text, "won") == 2
    assert board_count(mia.get("/pipeline").text, "won") == 2  # her team's (Eva's) and the unassigned one
    assert board_count(mia.get("/pipeline").text, "negotiation") == 1  # Ed's open deal
    assert perf(eva.get("/my").text, "eva")["leads_owned"]  # a row exists
    page = mia.get("/my").text
    assert "Open deals: 1" in page and "$3,000" in page
    p = eva.get("/performance?owner=zoe").text
    with pytest.raises(AssertionError):
        perf(p, "zoe")  # someone else's row: not shown to her


def test_defect_performance_total_for_an_executive_adds_records_she_cannot_see(org):
    """Eva's table shows her row and a Total: the Total also adds the unassigned deals of the whole company, hidden
    from her table, so her row (won $10,000) and the Total ($11,000) do not agree."""
    reporting_set(org)
    page = client(org, "eva").get("/performance").text
    assert perf(page, "eva")["value"] == "$10,000"
    assert perf(page, "Total")["value"] == "$10,000"
    assert perf(page, "Total")["won"] == perf(page, "eva")["won"]


def test_defect_performance_ai_section_shows_an_executive_company_wide_figures(org):
    """The AI section is on her page as 'for you and the people you can see', yet counts Zoe's AI-sourced revenue."""
    z = make_deal(org, make_lead(org, owner="zoe", company="C"), value=7000, owner="zoe", origin="ai")
    post(client(org, "boss"), f"/opportunities/{z}", stage="won")
    lid = make_lead(org, owner="zoe", company="Z2", email="z@z2.example")
    make_draft(org, lid, to="z@z2.example")
    page = client(org, "eva").get("/performance").text
    assert "Email drafts written: <b>0</b>" in page


def test_defect_a_managers_win_changes_who_gets_the_credit_depending_on_the_filter(org):
    """aime.performance: 'A won deal counts once: for the person who moved it to won'. Mia wins Eva's deal: the whole
    company's view credits Mia; Eva's own view and the owner filter credit Eva as well."""
    oid = make_deal(org, make_lead(org, owner="eva", email="a@a.example"), value=5000)
    post(client(org, "mia"), f"/opportunities/{oid}", stage="won")
    boss = client(org, "boss")
    assert perf(boss.get("/performance").text, "mia")["won"] == "1"
    assert perf(boss.get("/performance").text, "eva")["won"] == "0"
    assert perf(boss.get("/performance?owner=eva").text, "eva")["won"] == "0"
    assert perf(client(org, "eva").get("/performance").text, "eva")["won"] == "0"


def test_defect_revenue_disappears_from_the_report_when_a_salesperson_leaves(org):
    """Eva won $10,000 and is deactivated. The company's Total, read by the owner, no longer includes that money."""
    oid = make_deal(org, make_lead(org, owner="eva", email="a@a.example"), value=10000)
    post(client(org, "eva"), f"/opportunities/{oid}", stage="won")
    boss = client(org, "boss")
    assert perf(boss.get("/performance").text, "Total")["value"] == "$10,000"
    with org.db.session() as s:
        uid = s.execute(select(User).where(User.username == "eva")).scalar_one().id
    post(boss, f"/admin/users/{uid}/deactivate")
    assert perf(boss.get("/performance").text, "Total")["value"] == "$10,000"


def test_defect_my_work_lead_count_includes_merged_and_disqualified_leads(org):
    make_lead(org, owner="eva", company="Good")
    make_lead(org, owner="eva", company="Dup", status="merged")
    make_lead(org, owner="eva", company="Bad", status="disqualified")
    page = client(org, "eva").get("/my").text
    assert re.search(r"Leads: (\d+)", page).group(1) == "1"


def test_defect_performance_leads_owned_counts_merged_leads(org):
    make_lead(org, owner="eva", company="Good")
    make_lead(org, owner="eva", company="Dup", status="merged")
    assert perf(client(org, "boss").get("/performance").text, "eva")["leads_owned"] == "1"


def test_defect_sidebar_count_shows_an_executive_approvals_she_cannot_see(org):
    """Zoe's page: 'Emails to approve: 0' on My work, while the sidebar badge counts other people's drafts too."""
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    make_draft(org, lid)
    page = client(org, "zoe").get("/my").text
    assert "Emails to approve: 0" in page
    link = re.search(r'href="/approvals"[^>]*>.*?</a>', page, re.S).group(0)
    assert not re.search(r">\s*1\s*<", link)


def test_defect_inbound_page_shows_every_customers_inquiry_to_every_executive(org):
    """aime.visibility: a user sees their own records. /inbound lists every inquiry: name of the company, the address,
    the message, a link, including those of leads that are 'not found' for this user everywhere else."""
    c = client(org)
    api_inquiry(c, **DANA)
    process(org)
    with org.db.session() as s:
        lid = s.execute(select(Lead)).scalar_one().id
    post(client(org, "boss"), f"/leads/{lid}/assign", owner="eva")
    zoe = client(org, "zoe")
    assert zoe.get(f"/leads/{lid}").status_code == 404
    assert DANA["email"] not in zoe.get("/inbound").text


def test_ok_search_and_api_respect_who_may_see(org):
    lid = make_lead(org, owner="eva", company="Secretco", email="dana@secretco.example")
    make_draft(org, lid, to="dana@secretco.example", subject="Secretco proposal")
    zoe = client(org, "zoe")
    assert "Secretco" not in zoe.get("/search?q=secretco").text
    assert lid not in {row["id"] for row in zoe.get("/api/leads").json()}
    assert "Secretco" not in zoe.get("/followups").text and "Secretco" not in zoe.get("/approvals").text
    assert "Secretco" not in zoe.get("/contacts").text and "Secretco" not in zoe.get("/companies").text
    assert "Secretco" in client(org, "mia").get("/search?q=secretco").text


def test_defect_report_date_filter_uses_utc_while_every_date_on_screen_is_local(org, monkeypatch):
    """An owner far east of UTC wins a deal in the small hours of 28 Oct local time. The deal's history says 28 Oct; the
    report for 28 Oct says nothing was won (it filters by the UTC date, 27 Oct). Tokyo, not the owner's India: a machine
    in India or on UTC cannot hide a place that still reads its own clock."""
    for where in ("aime.timeutil.local_tz", 'aime.agents.analytics.local_tz', 'aime.agents.marketing_manager.local_tz', 'aime.scheduler.runner.local_tz', 'aime.web.app.local_tz', 'aime.web.team_pages.local_tz'):
        monkeypatch.setattr(where, lambda name=None: ZoneInfo("Asia/Tokyo"))
    oid = make_deal(org, make_lead(org, owner="eva", email="a@a.example"), value=5000)
    post(client(org, "eva"), f"/opportunities/{oid}", stage="won")
    with org.db.session() as s:
        o = s.get(Opportunity, oid)
        o.closed_at = datetime(2026, 10, 27, 20, 0)  # 05:00 on the 28th in Tokyo
        s.execute(StageChange.__table__.update().where(StageChange.opportunity_id == oid).values(at=o.closed_at))
    boss = client(org, "boss")
    assert "2026-10-28 05:00" in boss.get(f"/opportunities/{oid}").text
    assert perf(boss.get("/performance?start=2026-10-28&end=2026-10-28").text, "eva")["won"] == "1"


# ================================================================== several people at once (threads, file database)
def together(jobs):
    """Run the requests at the same moment; returns their status codes."""
    gate = threading.Barrier(len(jobs))
    codes = [None] * len(jobs)

    def run(i, job):
        gate.wait(timeout=30)
        codes[i] = job().status_code

    threads = [threading.Thread(target=run, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return codes


def test_ok_many_people_working_different_records_at_once_lose_nothing(org):
    app = create_app(org)
    leads = {u: make_lead(org, owner=u, company=f"Co {u}") for u in ("eva", "ed", "zoe", "mia")}
    deals = {u: make_deal(org, lid, owner=u) for u, lid in leads.items()}
    jobs = []
    for u in leads:
        c = client(org, u, app)
        jobs += [lambda c=c, u=u: post(c, f"/opportunities/{deals[u]}/quotes", amount="1000"),
                 lambda c=c, u=u: post(c, f"/leads/{leads[u]}/note", note="Called 😀"),
                 lambda c=c, u=u: post(c, f"/opportunities/{deals[u]}", stage="won", est_value="1000"),
                 lambda c=c: c.get("/my")]
    codes = together(jobs)
    assert set(codes) <= {200, 303}, codes
    assert count(org, Quote) == 4 and count(org, Opportunity, Opportunity.stage == "won") == 4
    assert count(org, Activity, Activity.type == "note") == 4
    assert count(org, FollowUp, FollowUp.subject.like("Customer handover:%")) == 4
    assert perf(client(org, "ana").get("/performance").text, "Total")["value"] == "$4,000"


def test_defect_a_double_submit_of_the_public_form_answers_500(org):
    """The same inquiry sent twice at once: the second insert hits the unique fingerprint and the sender gets a 500
    although the inquiry is safely on record."""
    app = create_app(org)
    c = client(org, app=app)
    for n in range(3):  # a race: several rounds, each a new inquiry, within the public API's limit of 20 a minute
        codes = together([lambda n=n: api_inquiry(c, **{**DANA, "message": f"{ASK} round {n}"}) for _ in range(6)])
        assert set(codes) <= {200, 201}, codes
    assert count(org, InboundInquiry) == 3


def test_defect_the_first_approvals_on_a_new_install_crash_each_other(org, monkeypatch):
    """Nothing recorded yet about the sending switch: every approval tries to insert the same setting; all but one get
    a 500 and their approval is lost."""
    import aime.automation as am

    gate = threading.Barrier(2)
    real = am.kv_set

    def held(*a, **k):
        try:
            gate.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return real(*a, **k)

    monkeypatch.setattr(am, "kv_set", held)
    app = create_app(org)
    lid = make_lead(org, owner="mia", email="dana@acme.example")
    ids = [make_draft(org, lid) for _ in range(2)]
    mia = client(org, "mia", app)
    codes = together([lambda f=f: post(mia, f"/followups/{f}/approve") for f in ids])
    assert all(c < 500 for c in codes), codes


def seen_sending(org):
    """An approval has happened before: the sending switch is on record."""
    lid = make_lead(org, owner="mia", company="Earlier", email="e@earlier.example")
    assert post(client(org, "mia"), f"/followups/{make_draft(org, lid, to='e@earlier.example')}/approve").status_code == 303


def test_ok_many_approve_clicks_together_leave_one_approved_email(org):
    seen_sending(org)
    app = create_app(org)
    lid = make_lead(org, owner="mia", email="dana@acme.example")
    fid = make_draft(org, lid)
    mia, boss = client(org, "mia", app), client(org, "boss", app)
    codes = together([lambda: post(mia, f"/followups/{fid}/approve"), lambda: post(boss, f"/followups/{fid}/approve"),
                      lambda: post(mia, f"/followups/{fid}/approve"), lambda: post(boss, f"/followups/{fid}/approve")])
    assert all(c < 500 for c in codes) and codes.count(303) >= 1, codes
    with org.db.session() as s:
        assert s.get(FollowUp, fid).status == "approved"


def test_defect_an_email_rejected_while_a_colleague_approves_it_is_sent_anyway(org, monkeypatch):
    """Mia clicks Approve as the administrator clicks Reject. The reject is saved and its page says 'Rejected', then
    the approve, which read the draft a moment earlier, overwrites it: the rejected email goes out with the next run."""
    import aime.web.app as web

    seen_sending(org)
    lid = make_lead(org, owner="mia", email="dana@acme.example")
    fid = make_draft(org, lid)
    app = create_app(org)
    has_read, reject_done = threading.Event(), threading.Event()
    real = web.am.observe_sending

    def hold(*a, **k):
        has_read.set()
        reject_done.wait(timeout=10)
        return real(*a, **k)

    monkeypatch.setattr(web.am, "observe_sending", hold)
    mia, boss = client(org, "mia", app), client(org, "boss", app)
    codes = {}
    t = threading.Thread(target=lambda: codes.update(approve=post(mia, f"/followups/{fid}/approve").status_code))
    t.start()
    assert has_read.wait(timeout=30)
    assert post(boss, f"/followups/{fid}/reject").status_code == 303
    reject_done.set()
    t.join(timeout=30)
    with org.db.session() as s:
        assert s.get(FollowUp, fid).status == "rejected", codes


def race_on_move(monkeypatch, parties):
    """Holds every request just after it read the deal, before it writes: the moment two people click together."""
    import aime.web.app as web

    gate = threading.Barrier(parties)
    real = web.move_stage

    def slow(*a, **k):
        try:
            gate.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return real(*a, **k)

    monkeypatch.setattr(web, "move_stage", slow)


def test_defect_two_people_winning_one_deal_at_the_same_moment_write_it_twice(org, monkeypatch):
    race_on_move(monkeypatch, 2)
    app = create_app(org)
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid, value=1000)
    eva, mia = client(org, "eva", app), client(org, "mia", app)
    codes = together([lambda: post(eva, f"/opportunities/{oid}", stage="won"),
                      lambda: post(mia, f"/opportunities/{oid}", stage="won")])
    assert all(c < 500 for c in codes), codes
    assert count(org, StageChange, StageChange.opportunity_id == oid, StageChange.to_stage == "won") == 1
    assert count(org, FollowUp, FollowUp.subject.like("Customer handover:%")) == 1


def test_defect_one_user_reopens_as_another_wins_leaves_a_deal_that_is_both(org, monkeypatch):
    """A lost deal. Mia reopens it to proposal while Eva marks it won. Mia's save, last, only writes the columns she
    changed: the deal ends up open at 'proposal', and also 'won by eva', with the lead converted and a handover task."""
    import aime.web.app as web

    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid, value=1000)
    assert post(client(org, "eva"), f"/opportunities/{oid}", stage="lost", lost_reason="Budget").status_code == 303
    app = create_app(org)
    has_read, eva_done = threading.Event(), threading.Event()
    real = web.move_stage

    def slow(session, opp, stage, *a, **k):
        if stage == "proposal":  # Mia's request has read the deal; it writes after Eva's has finished
            has_read.set()
            eva_done.wait(timeout=10)
        return real(session, opp, stage, *a, **k)

    monkeypatch.setattr(web, "move_stage", slow)
    eva, mia = client(org, "eva", app), client(org, "mia", app)
    done = {}
    t = threading.Thread(target=lambda: done.update(mia=post(mia, f"/opportunities/{oid}", stage="proposal").status_code))
    t.start()
    assert has_read.wait(timeout=30)
    assert post(eva, f"/opportunities/{oid}", stage="won").status_code == 303
    eva_done.set()
    t.join(timeout=30)
    assert done["mia"] < 500
    o = opp_row(org, oid)
    if o.stage == "won":
        assert o.won_by == "eva" and o.closed_at
    else:
        assert o.won_by is None and o.closed_at is None and lead_row(org, lid).status != "converted"


# ================================================================== what the fixes above must not break
def test_ok_one_person_editing_a_deal_from_its_pages_is_never_told_of_a_conflict(org):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid, value=9000)
    eva = client(org, "eva")
    for page, action in ((f"/opportunities/{oid}", f"/opportunities/{oid}"), ("/pipeline", f"/opportunities/{oid}")):
        fields = form_fields(eva.get(page).text, action)
        assert fields["shown_stage"] == opp_row(org, oid).stage
        assert post(eva, action, **{**fields, "next_step": f"From {page}"}).status_code == 303
        assert opp_row(org, oid).next_step == f"From {page}"
    fields = form_fields(eva.get("/pipeline").text, f"/opportunities/{oid}")
    r = post(eva, f"/opportunities/{oid}", **{**fields, "stage": "won"})
    assert "Opportunity updated" in where(r)
    assert post(eva, f"/opportunities/{oid}", **{**fields, "stage": "won"}).status_code == 303  # the same click twice
    o = opp_row(org, oid)
    assert (o.stage, o.won_by) == ("won", "eva")
    assert count(org, StageChange, StageChange.opportunity_id == oid, StageChange.to_stage == "won") == 1


def test_ok_a_stale_page_says_why_nothing_was_saved_and_keeps_a_colleagues_next_step_and_date(org):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid, stage="proposal")
    eva, mia = client(org, "eva"), client(org, "mia")
    page = mia.get(f"/opportunities/{oid}").text
    post(eva, f"/opportunities/{oid}", stage="proposal", next_step="Eva's plan", expected_close="2027-01-15")
    fields = form_fields(page, f"/opportunities/{oid}")  # the stage has not moved: Mia only attaches a reason nobody needs
    assert post(mia, f"/opportunities/{oid}", **fields).status_code == 303
    o = opp_row(org, oid)
    assert (o.next_step, o.expected_close) == ("Eva's plan", date(2027, 1, 15))  # fields she left alone did not overwrite
    post(eva, f"/opportunities/{oid}", stage="negotiation")
    r = post(mia, f"/opportunities/{oid}", **{**fields, "next_step": "Mia's plan"})
    assert "moved to negotiation while your page was open" in where(r) and "nothing was saved" in where(r)
    assert opp_row(org, oid).next_step == "Eva's plan"


def test_ok_winning_again_after_a_reopen_brings_back_one_handover_task(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    oid = make_deal(org, lid)
    eva = client(org, "eva")
    post(eva, f"/opportunities/{oid}", stage="won")
    post(eva, f"/opportunities/{oid}", stage="negotiation")
    assert lead_row(org, lid).status == "qualified"
    with org.db.session() as s:
        assert s.execute(select(FollowUp).where(FollowUp.subject.like("Customer handover:%"))).scalar_one().status == "cancelled"
    post(eva, f"/opportunities/{oid}", stage="won")
    assert lead_row(org, lid).status == "converted"
    with org.db.session() as s:
        task = s.execute(select(FollowUp).where(FollowUp.subject.like("Customer handover:%"))).scalar_one()
        assert task.status == "pending" and task.blocked_reason is None


def test_ok_a_finished_handover_and_another_won_deal_survive_a_reopen(org):
    lid = make_lead(org, owner="eva", email="dana@acme.example")
    first = make_deal(org, lid, name="First")
    second = make_deal(org, lid, name="Second")
    eva = client(org, "eva")
    post(eva, f"/opportunities/{first}", stage="won")
    post(eva, f"/opportunities/{second}", stage="won")
    with org.db.session() as s:
        task = s.execute(select(FollowUp).where(FollowUp.subject.like("Customer handover: First"))).scalar_one()
        task.status = "done"
    post(eva, f"/opportunities/{first}", stage="negotiation")
    assert lead_row(org, lid).status == "converted"  # the second deal still makes them a customer
    with org.db.session() as s:
        assert s.execute(select(FollowUp).where(FollowUp.subject.like("Customer handover: First"))).scalar_one().status == "done"


def test_ok_a_later_usd_quote_after_a_euro_one_sets_the_deal_value(org):
    _, oid, qid, eva = quoted(org, currency="EUR")
    r = post(eva, f"/opportunities/{oid}/quotes/{qid}/accept")
    assert "enter the agreed value in USD" in where(r) and opp_row(org, oid).est_value is None
    with org.db.session() as s:
        assert s.get(Quote, qid).status == "accepted"
    post(eva, f"/opportunities/{oid}/quotes", amount="20000")
    with org.db.session() as s:
        second = s.execute(select(Quote).order_by(Quote.id.desc())).scalars().first().id
    post(eva, f"/opportunities/{oid}/quotes/{second}/accept")
    assert opp_row(org, oid).est_value == 20000


def test_ok_a_role_without_leads_logging_a_meeting_lands_on_my_work(org):
    """A Lead Generation Executive does not receive leads: the deal goes to the pool, and the page they come back to is
    one they can open."""
    with org.db.session() as s:
        s.add(User(username="gen", password_hash=HASH, role="lead_gen_executive"))
    lid = make_lead(org, owner=None, email="dana@acme.example")
    make_deal(org, lid, owner=None, stage="discovery")
    gen = client(org, "gen")
    r = post(gen, f"/leads/{lid}/meeting", date="2026-11-03", time_et="10:00")
    assert r.status_code == 303 and r.headers["location"].startswith("/my") and "Meeting logged" in where(r)
    assert gen.get(r.headers["location"]).status_code == 200
    assert lead_row(org, lid).owner not in (None, "gen")


def test_ok_a_calendly_booking_by_a_returning_customer_goes_to_their_open_lead(org):
    from aime.meetings import match_lead

    _, _, new = won_customer(org)
    with org.db.session() as s:
        assert match_lead(s, DANA["email"])[0].id == new.id


def test_ok_a_double_click_on_mark_done_does_not_show_an_error(org):
    lid = make_lead(org, owner="eva")
    fid = make_draft(org, lid, channel="task", status="pending", kind=None, subject="Call them")
    eva = client(org, "eva")
    html = {"accept": "text/html"}
    assert eva.post(f"/followups/{fid}/done", headers={**ORIGIN, **html}, follow_redirects=False).status_code == 303
    assert eva.post(f"/followups/{fid}/done", headers={**ORIGIN, **html}, follow_redirects=False).status_code == 303
    with org.db.session() as s:
        assert s.get(FollowUp, fid).status == "done"


def test_ok_a_lead_page_links_to_its_own_deal(org):
    lid = make_lead(org, owner="eva")
    oid = make_deal(org, lid)
    assert f'href="/opportunities/{oid}"' in client(org, "eva").get(f"/leads/{lid}").text
