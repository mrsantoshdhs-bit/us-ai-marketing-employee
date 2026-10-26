"""B2B outreach: evidence-quoted drafts, quality checks, approval, sequences, stop rules, controlled sending."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.agents.company_research import CompanyResearchAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.crm_ops import suppress
from aime.doctor import run_checks
from aime.integrations.email_smtp import EmailSender
from aime.models import Activity, Contact, FollowUp, Lead, Signal
from aime.web.app import create_app
from tests.test_company_research import SITE, add_company, make_handler
from tests.test_contact_validation import FakeChecker
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP

ORIGIN = {"origin": "http://testserver"}
POST = ("We are hiring an operations analyst. Our dispatchers re-key orders from email into spreadsheets every day, "
        "and we want to automate that manual data entry.")
POSTAL = {"company_postal_address": "1 Sample St, Austin, TX 78701"}
SENDING = {**POSTAL, "outbound_email_enabled": True, "smtp_host": "smtp.example", "smtp_from": "hello@us.example"}


def researched(make_ctx, hiring=True, checker=None, **overrides):
    requests: list[str] = []
    ctx = make_ctx(handler=make_handler(requests), **overrides)
    cid = add_company(ctx)
    if hiring:
        with ctx.db.session() as s:
            s.add(Signal(company_id=cid, signal_type="hiring", text=POST, source="hn_hiring",
                         source_url="https://news.ycombinator.com/item?id=123", fingerprint="post-1",
                         keywords=["manual data entry", "spreadsheets"], strength=0.8))
    assert CompanyResearchAgent(ctx, mail_checker=checker or FakeChecker({})).run({"company_ids": [cid]}).ok
    return ctx


def drafts(ctx):
    with ctx.db.session() as s:
        return s.execute(select(FollowUp).where(FollowUp.channel == "email").order_by(FollowUp.id)).scalars().all()


def lead_of(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Lead)).scalar_one()


def test_first_touch_quotes_only_published_evidence(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    out = SalesWorkflowAgent(ctx).run({}).output
    assert out.outreach_drafts == 1 and out.emails_sent == 0
    (f,) = drafts(ctx)
    assert f.kind == "outreach" and f.step == 1 and f.status == "awaiting_approval" and f.approved_at is None
    assert f.to_email == f"david.chen@{SITE}" and f.body.startswith("Hi David,")
    quote = f.body.split('"')[1].rstrip("…")
    assert quote in POST and f.evidence[0]["url"] == "https://news.ycombinator.com/item?id=123"
    assert "publishes this address as a business contact" in f.body and "unsubscribe" in f.body
    assert '.".' not in f.body  # no doubled full stop after a quoted sentence
    assert "1 Sample St" in f.body and f.quality["qc"]["status"] == "passed", f.quality["qc"]
    assert lead_of(ctx).outreach_status == "drafted"


def test_no_quotable_evidence_means_no_email(make_ctx):
    ctx = researched(make_ctx, hiring=False)
    with ctx.db.session() as s:  # remove what the website said, leaving a named person with an email only
        from aime.models import ResearchSnapshot
        for snap in s.execute(select(ResearchSnapshot)).scalars():
            snap.needs, snap.description = [], None
    out = SalesWorkflowAgent(ctx).run({}).output
    assert out.outreach_drafts == 0 and out.tasks_created == 1 and drafts(ctx) == []


@pytest.mark.parametrize("change", ["policy_off", "no_mail_server", "suppressed", "grade_c", "competitor"])
def test_ineligible_leads_get_a_task_not_an_email(make_ctx, change):
    ctx = researched(make_ctx, checker=FakeChecker({SITE: False}) if change == "no_mail_server" else None)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        if change == "policy_off":
            ctx.settings.business["outreach"]["b2b_enabled"] = False
        elif change == "suppressed":
            suppress(s, SITE, "Asked not to be contacted")
        elif change == "grade_c":
            lead.grade = "C"
        elif change == "competitor":
            lead.company.is_competitor = True
    out = SalesWorkflowAgent(ctx).run({}).output
    assert out.outreach_drafts == 0 and drafts(ctx) == []


def test_drafts_per_run_cap(make_ctx):
    ctx = researched(make_ctx)
    ctx.settings.business["outreach"]["drafts_per_run"] = 0
    assert SalesWorkflowAgent(ctx).run({}).output.outreach_drafts == 0
    ctx.settings.business["outreach"]["drafts_per_run"] = 5
    assert SalesWorkflowAgent(ctx).run({}).output.outreach_drafts == 1
    assert SalesWorkflowAgent(ctx).run({}).output.outreach_drafts == 0  # one sequence per lead


def test_held_draft_needs_an_explicit_override(make_ctx):
    ctx = researched(make_ctx)  # no postal address configured: the compliance check holds the draft
    SalesWorkflowAgent(ctx).run({})
    (f,) = drafts(ctx)
    assert f.quality["qc"]["status"] == "held"
    client = TestClient(create_app(ctx))
    r = client.post(f"/followups/{f.id}/approve", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "tick" in r.headers["location"]
    assert drafts(ctx)[0].status == "awaiting_approval"
    client.post(f"/followups/{f.id}/approve", data={"qc_override": "yes"}, headers=ORIGIN)
    f = drafts(ctx)[0]
    assert f.status == "approved" and f.approved_by and f.approved_at
    lead = lead_of(ctx)
    assert lead.outreach_status == "approved" and lead.owner == f.approved_by
    with ctx.db.session() as s:
        assert s.execute(select(Activity).where(Activity.type == "override")).scalars().first()


def test_edits_are_checked_again(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    SalesWorkflowAgent(ctx).run({})
    f = drafts(ctx)[0]
    client = TestClient(create_app(ctx))
    spammy = f.body.replace("Would a 20-minute call", "Act now for a FREE call, we guarantee results. Would a 20-minute call")
    client.post(f"/followups/{f.id}/save", data={"subject": f.subject, "body": spammy}, headers=ORIGIN)
    f = drafts(ctx)[0]
    assert f.quality["qc"]["status"] == "failed" and f.status == "awaiting_approval"
    tampered = f.body.replace(f.body.split('"')[1], "we are desperate for a new CRM vendor")
    client.post(f"/followups/{f.id}/save", data={"subject": f.subject, "body": tampered}, headers=ORIGIN)
    checks = {c["name"]: c for c in drafts(ctx)[0].quality["qc"]["checks"]}
    assert checks["personalization"]["status"] == "fail" and "not found in the evidence" in checks["personalization"]["detail"]


def test_rejecting_the_first_email_stops_outreach(make_ctx):
    ctx = researched(make_ctx, **POSTAL)
    SalesWorkflowAgent(ctx).run({})
    TestClient(create_app(ctx)).post(f"/followups/{drafts(ctx)[0].id}/reject", headers=ORIGIN)
    assert lead_of(ctx).outreach_status == "stopped"
    assert SalesWorkflowAgent(ctx).run({}).output.outreach_drafts == 0 and len(drafts(ctx)) == 1


def approve_all(ctx):
    client = TestClient(create_app(ctx))
    for f in drafts(ctx):
        if f.status == "awaiting_approval":
            client.post(f"/followups/{f.id}/approve", headers=ORIGIN)


def test_controlled_send_then_follow_up_then_stop_on_reply(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    FakeSMTP.sent = []
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    out = SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()}).output
    assert out.emails_sent == 1 and FakeSMTP.sent[0]["To"] == f"david.chen@{SITE}"
    assert FakeSMTP.sent[0]["List-Unsubscribe"]
    assert lead_of(ctx).outreach_status == "contacted"
    assert SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(days=1)).isoformat()}).output.outreach_drafts == 0
    later = TUESDAY_11AM_ET + timedelta(days=4, minutes=5)
    assert SalesWorkflowAgent(ctx, sender=sender).run({"now": later.isoformat()}).output.outreach_drafts == 1
    step2 = drafts(ctx)[-1]
    assert step2.step == 2 and step2.status == "awaiting_approval" and step2.subject.startswith("Following up")
    assert step2.quality["qc"]["status"] == "passed", step2.quality["qc"]
    lead = lead_of(ctx)
    TestClient(create_app(ctx)).post(f"/leads/{lead.id}/replied", headers=ORIGIN)
    assert lead_of(ctx).outreach_status == "replied" and drafts(ctx)[-1].status == "cancelled"
    checks = {c["name"]: c for c in run_checks(ctx)}
    assert checks["Emails only to consenting contacts or approved B2B outreach"]["ok"]


@pytest.mark.parametrize("change", ["address_changed", "suppressed_after_approval"])
def test_send_time_recheck_cancels(make_ctx, change):
    ctx = researched(make_ctx, **SENDING)
    FakeSMTP.sent = []
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    with ctx.db.session() as s:
        if change == "address_changed":
            c = s.execute(select(Contact).where(Contact.email == f"david.chen@{SITE}")).scalar_one()
            c.email = f"d.chen@{SITE}"
        else:
            suppress(s, f"david.chen@{SITE}", "Unsubscribed by phone")
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert FakeSMTP.sent == [] and drafts(ctx)[0].status == "cancelled"
    # suppressing cancels queued emails at once; an address change is caught by the send-time re-check
    assert drafts(ctx)[0].blocked_reason.startswith("Not sent:" if change == "address_changed" else "Contact unsubscribed")
