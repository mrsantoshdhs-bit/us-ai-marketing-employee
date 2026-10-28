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
    spammy = f.body.replace("Would a 30-minute call", "Act now for a FREE call, we guarantee results. Would a 30-minute call")
    assert spammy != f.body
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


def test_no_override_sends_an_email_without_its_compliance_footer(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    FakeSMTP.sent = []
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    f = drafts(ctx)[0]
    client = TestClient(create_app(ctx))
    no_footer = f.body.split("\n\n---\n")[0]
    r = client.post(f"/followups/{f.id}/approve", data={"subject": f.subject, "body": no_footer, "qc_override": "yes"},
                    headers=ORIGIN, follow_redirects=False)
    location = r.headers["location"]
    assert r.status_code == 303 and location.startswith("/followups?msg=Cannot%20approve") and location.endswith(f"#f{f.id}")
    assert drafts(ctx)[0].status == "awaiting_approval"
    with ctx.db.session() as s:  # approved behind the dashboard's back: the send-time check still refuses it
        row = s.get(FollowUp, f.id)
        row.status, row.approved_by, row.approved_at = "approved", "someone", row.created_at
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    f = drafts(ctx)[0]
    assert FakeSMTP.sent == [] and f.status == "awaiting_approval" and f.approved_at is None
    assert "opt-out line" in f.blocked_reason


def test_draft_written_before_the_postal_address_gets_it_on_approval(make_ctx):
    ctx = researched(make_ctx)  # drafted with no postal address configured
    SalesWorkflowAgent(ctx).run({})
    assert "[postal address required" in drafts(ctx)[0].body
    ctx.settings.company_postal_address = POSTAL["company_postal_address"]
    f = drafts(ctx)[0]
    TestClient(create_app(ctx)).post(f"/followups/{f.id}/approve", headers=ORIGIN)
    f = drafts(ctx)[0]
    assert f.status == "approved" and POSTAL["company_postal_address"] in f.body and "[postal" not in f.body


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


def _second_lead_at_same_domain(ctx, status="approved", started_at=None):
    """Another qualified lead whose published contact uses the same email domain as David."""
    from aime.models import Company
    from aime.timeutil import utcnow
    with ctx.db.session() as s:
        f1 = s.execute(select(FollowUp).where(FollowUp.kind == "outreach")).scalar_one()
        c1 = f1.contact
        co2 = Company(name="Second Co", normalized_name="second co", domain="second-co.example",
                      website="https://second-co.example", website_status="verified", source="hn_hiring")
        s.add(co2)
        s.flush()
        c2 = Contact(company_id=co2.id, email=f"maria@{SITE}", first_name="Maria", source=c1.source,
                     email_source_url=c1.email_source_url, email_status=c1.email_status,
                     role_category=c1.role_category, consent_status=c1.consent_status)
        s.add(c2)
        s.flush()
        l2 = Lead(company_id=co2.id, contact_id=c2.id, source="research", status="qualified", score=70, grade="B",
                  primary_service="bpa", outreach_status="approved")
        s.add(l2)
        s.flush()
        s.add(FollowUp(lead_id=l2.id, contact_id=c2.id, company_id=co2.id, channel="email", kind="outreach", step=1,
                       due_at=f1.due_at, status=status, approved_by="tester", approved_at=utcnow(),
                       send_started_at=started_at, subject=f1.subject, body=f1.body,
                       to_email=c2.email, evidence=f1.evidence, quality=f1.quality))


def test_an_email_of_unknown_outcome_counts_toward_the_per_domain_limit(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    FakeSMTP.sent = []
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    _second_lead_at_same_domain(ctx, status="uncertain", started_at=TUESDAY_11AM_ET - timedelta(hours=1))  # may have arrived
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    david = next(f for f in drafts(ctx) if f.to_email == f"david.chen@{SITE}")
    assert FakeSMTP.sent == [] and david.status == "approved" and "Already emailed" in david.blocked_reason


def test_overlapping_runs_share_one_send_lock_so_limits_hold(make_ctx):
    from aime.agents.sales_workflow import SEND_BUSY
    from aime.scheduler.runner import FileLock
    ctx = researched(make_ctx, **SENDING)
    FakeSMTP.sent = []
    SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run(
        {"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    _second_lead_at_same_domain(ctx)
    other = FileLock(ctx.settings.locks_dir / "send.lock")  # another job's run is sending
    assert other.acquire()
    out = SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run(
        {"now": TUESDAY_11AM_ET.isoformat()}).output
    assert FakeSMTP.sent == [] and {b.reason for b in out.blocked} == {SEND_BUSY}
    other.release()

    class OverlappingSMTP(FakeSMTP):  # a second run starts while the first is talking to the mail server
        def send_message(self, msg):
            if not getattr(OverlappingSMTP, "nested", False):
                OverlappingSMTP.nested = True
                SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run(
                    {"now": TUESDAY_11AM_ET.isoformat()})
            super().send_message(msg)

    SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=OverlappingSMTP)).run(
        {"now": TUESDAY_11AM_ET.isoformat()})
    assert len(FakeSMTP.sent) == 1  # one email a day to that domain, however the runs overlap
    assert not (ctx.settings.locks_dir / "send.lock").exists()


def test_approval_rechecks_the_do_not_contact_list_even_without_edits(make_ctx):
    from aime.models import Suppression
    ctx = researched(make_ctx, **SENDING)
    SalesWorkflowAgent(ctx).run({"now": TUESDAY_11AM_ET.isoformat()})
    f = drafts(ctx)[0]
    with ctx.db.session() as s:  # e.g. an imported do-not-contact list, which does not cancel drafts itself
        s.add(Suppression(value=SITE, kind="domain", reason="Imported do-not-contact list"))
    r = TestClient(create_app(ctx)).post(f"/followups/{f.id}/approve", headers=ORIGIN, follow_redirects=False)
    assert "do-not-contact" in r.headers["location"].replace("%20", " ") and drafts(ctx)[0].status == "awaiting_approval"


def test_an_email_confirmed_by_mark_sent_continues_the_sequence_and_keeps_its_message_id(make_ctx):
    class TimeoutSMTP(FakeSMTP):
        def send_message(self, msg):
            TimeoutSMTP.message_id = msg["Message-ID"]
            raise TimeoutError("timed out")

    ctx = researched(make_ctx, **SENDING)
    SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run(
        {"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=TimeoutSMTP)).run(
        {"now": TUESDAY_11AM_ET.isoformat()})
    f = drafts(ctx)[0]
    assert f.status == "uncertain" and f.message_id == TimeoutSMTP.message_id  # stored before the send failed
    TestClient(create_app(ctx)).post(f"/followups/{f.id}/mark_sent", headers=ORIGIN)
    f = drafts(ctx)[0]
    assert f.status == "sent" and f.delivered_to == f"david.chen@{SITE}" and lead_of(ctx).outreach_status == "contacted"
    with ctx.db.session() as s:
        s.get(FollowUp, f.id).sent_at = TUESDAY_11AM_ET - timedelta(days=11)
    out = SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run(
        {"now": TUESDAY_11AM_ET.isoformat()}).output
    assert out.outreach_drafts == 1 and drafts(ctx)[-1].step == 2


@pytest.mark.parametrize("step", [2, 3])
def test_every_follow_up_template_passes_the_quality_checks(make_ctx, step):
    from aime.outreach import draft_outreach
    from aime.qc.email_checks import run_email_checks
    ctx = researched(make_ctx, **SENDING)
    lead = lead_of(ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead.id)
        subject, body = draft_outreach(ctx.settings, step, lead, lead.contact, [])
        qc = run_email_checks(subject, body, [], lead.contact, False, step=step)
    assert qc["status"] == "passed", qc  # a template that always fails could only be sent with an override


def test_follow_up_steps_reply_in_the_same_thread(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    FakeSMTP.sent = []
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    day = TUESDAY_11AM_ET
    SalesWorkflowAgent(ctx, sender=sender).run({"now": day.isoformat()})
    ids = []
    for step, wait in ((1, 0), (2, 7), (3, 7)):  # whole weeks: every send lands on a Tuesday, inside the window
        day = day + timedelta(days=wait, minutes=5)
        SalesWorkflowAgent(ctx, sender=sender).run({"now": day.isoformat()})  # drafts the next step when due
        approve_all(ctx)
        SalesWorkflowAgent(ctx, sender=sender).run({"now": day.isoformat()})
        assert len(FakeSMTP.sent) == step, [(f.step, f.status, f.blocked_reason) for f in drafts(ctx)]
        msg = FakeSMTP.sent[-1]
        if step == 1:
            assert msg["In-Reply-To"] is None
        else:
            assert msg["In-Reply-To"] == ids[-1] and msg["References"].split() == ids
        ids.append(msg["Message-ID"])
    assert [f.message_id for f in drafts(ctx)] == ids
