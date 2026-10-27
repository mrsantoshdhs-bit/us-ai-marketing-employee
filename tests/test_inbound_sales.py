import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from aime.agents import CRMManagementAgent, LeadQualificationAgent, SalesWorkflowAgent, USBusinessResearchAgent
from aime.crm_ops import suppress
from aime.inbound import record_inquiry
from aime.integrations.email_smtp import EmailSender
from aime.models import Company, Contact, FollowUp, InboundInquiry, Lead, Opportunity
from aime.sample_data import SAMPLE_INBOUND
from aime.timeutil import utcnow

ET = ZoneInfo("America/New_York")
TUESDAY_11AM_ET = datetime(2026, 10, 27, 11, 0, tzinfo=ET).astimezone(timezone.utc).replace(tzinfo=None)
SATURDAY_ET = datetime(2026, 10, 31, 11, 0, tzinfo=ET).astimezone(timezone.utc).replace(tzinfo=None)


class FakeSMTP:
    sent = []
    tls_contexts: list = []

    def __init__(self, host, port, timeout=30):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        FakeSMTP.tls_contexts.append(context)

    def login(self, u, p):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def ingest(ctx):
    with ctx.db.session() as s:
        for rec in SAMPLE_INBOUND:
            record_inquiry(s, rec, "web_form")
    crm = CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]})
    assert crm.ok, crm.error
    return crm.output


def test_inbound_intake_validates_and_creates_records(ctx):
    out = ingest(ctx)
    assert out.inbound_processed == 3 and out.inbound_rejected == 1
    with ctx.db.session() as s:
        spam = s.execute(select(InboundInquiry).where(InboundInquiry.status == "rejected")).scalars().one()
        assert "honeypot" in spam.error
        dana = s.execute(select(Contact).where(Contact.email == "dana@brightpath-realty.example")).scalars().one()
        assert dana.consent_status == "inquiry" and dana.is_sample
        riley = s.execute(select(Contact).where(Contact.email == "riley@pixelpine.example")).scalars().one()
        assert riley.consent_status == "opt_in"
        pixel = s.execute(select(Company).where(Company.name == "Pixel & Pine Agency")).scalars().one()
        assert pixel.state == "OR" and pixel.industry == "marketing_agencies"
    # duplicates are ignored
    with ctx.db.session() as s:
        _, created = record_inquiry(s, SAMPLE_INBOUND[0], "api")
        assert not created


def test_qualification_and_sales_workflow_drafts_need_approval(ctx):
    out = ingest(ctx)
    q = LeadQualificationAgent(ctx).run({"lead_ids": out.inbound_lead_ids})
    assert q.ok
    with ctx.db.session() as s:
        statuses = {l.company.name: (l.status, l.grade, l.primary_service) for l in s.execute(select(Lead)).scalars()}
    assert statuses["Brightpath Realty Group"][0] == "qualified"
    assert statuses["Brightpath Realty Group"][2] == "custom_crm"  # explicit request ranks first
    assert statuses["Maple North Consulting"][0] == "disqualified"  # outside the US

    res = SalesWorkflowAgent(ctx).run({})
    assert res.ok and res.output.emails_sent == 0
    with ctx.db.session() as s:
        drafts = s.execute(select(FollowUp).where(FollowUp.channel == "email")).scalars().all()
        assert drafts and all(d.status == "awaiting_approval" for d in drafts)
        assert all("unsubscribe" in d.body.lower() for d in drafts)
        assert s.execute(select(Opportunity)).scalars().all()
    # running again does not duplicate
    again = SalesWorkflowAgent(ctx).run({}).output
    assert again.drafts_created == 0 and again.opportunities_created == []


def test_research_leads_get_tasks_never_emails(ctx):
    USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring"]})
    LeadQualificationAgent(ctx).run({})
    SalesWorkflowAgent(ctx).run({})
    with ctx.db.session() as s:
        research_leads = s.execute(select(Lead).where(Lead.source == "research")).scalars().all()
        ids = [l.id for l in research_leads]
        fups = s.execute(select(FollowUp).where(FollowUp.lead_id.in_(ids))).scalars().all()
        assert all(f.channel == "task" for f in fups)
        assert not s.execute(select(Opportunity).where(Opportunity.lead_id.in_(ids))).scalars().all()


def approve_all(ctx):
    with ctx.db.session() as s:
        for f in s.execute(select(FollowUp).where(FollowUp.status == "awaiting_approval")).scalars():
            f.status, f.approved_by, f.approved_at = "approved", "test", utcnow()


def test_sending_requires_enabled_configured_email(ctx):
    out = ingest(ctx)
    LeadQualificationAgent(ctx).run({"lead_ids": out.inbound_lead_ids})
    SalesWorkflowAgent(ctx).run({})
    approve_all(ctx)
    res = SalesWorkflowAgent(ctx).run({"now": TUESDAY_11AM_ET.isoformat()}).output
    assert res.emails_sent == 0 and res.blocked and "DISABLED" in res.blocked[0].reason


def test_approved_email_sent_in_window_and_sequence_continues(make_ctx):
    ctx = make_ctx(outbound_email_enabled=True, smtp_host="smtp.example", smtp_from="hello@us.example",
                   company_postal_address="1 Sample St, Austin, TX 78701")
    FakeSMTP.sent = []
    out = ingest(ctx)
    LeadQualificationAgent(ctx).run({"lead_ids": out.inbound_lead_ids})
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)

    weekend = SalesWorkflowAgent(ctx, sender=sender).run({"now": SATURDAY_ET.isoformat()}).output
    assert weekend.emails_sent == 0 and "send window" in weekend.blocked[0].reason

    sent = SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()}).output
    assert sent.emails_sent == len(FakeSMTP.sent) >= 1
    msg = FakeSMTP.sent[0]
    assert msg["List-Unsubscribe"] and "1 Sample St" in msg.get_content()

    with ctx.db.session() as s:
        for f in s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalars():
            f.sent_at = TUESDAY_11AM_ET - timedelta(days=4)
    later = SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(minutes=5)).isoformat()}).output
    assert later.drafts_created == sent.emails_sent  # step 2 drafted, awaiting approval
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp).where(FollowUp.step == 2)).scalars().first().status == "awaiting_approval"


def test_suppression_cancels_queued_messages(make_ctx):
    ctx = make_ctx(outbound_email_enabled=True, smtp_host="smtp.example", smtp_from="hello@us.example",
                   company_postal_address="1 Sample St")
    FakeSMTP.sent = []
    out = ingest(ctx)
    LeadQualificationAgent(ctx).run({"lead_ids": out.inbound_lead_ids})
    SalesWorkflowAgent(ctx).run({})
    approve_all(ctx)
    with ctx.db.session() as s:
        suppress(s, "brightpath-realty.example", "test")
    SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run(
        {"now": TUESDAY_11AM_ET.isoformat()})
    assert all("brightpath" not in m["To"] for m in FakeSMTP.sent)
    with ctx.db.session() as s:
        c = s.execute(select(Contact).where(Contact.email == "dana@brightpath-realty.example")).scalars().one()
        assert c.consent_status == "opted_out"


def test_inbox_folder_files_are_ingested(ctx):
    (ctx.settings.inbox_dir / "form.json").write_text(json.dumps(SAMPLE_INBOUND[0]), encoding="utf-8")
    (ctx.settings.inbox_dir / "batch.csv").write_text(
        "email,company,state,message,services_interested\n"
        "sam@sample-co.example,Sample Co,FL,We want to automate invoice data entry,bpa\n", encoding="utf-8")
    out = CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]}).output
    assert out.inbound_files_loaded == 2 and out.inbound_processed == 2
    assert not list(ctx.settings.inbox_dir.glob("*.json")) and list((ctx.settings.inbox_dir / "processed").iterdir())


def test_crm_maintenance_merges_duplicates_and_sets_lifecycle(ctx):
    with ctx.db.session() as s:
        a = Company(name="Dup Co", normalized_name="dup co", domain="dup.example", source="t", state="TX")
        b = Company(name="Dup Co LLC", normalized_name="dup co llc", domain="dup.example", source="t", state="TX")
        s.add_all([a, b])
        s.flush()
        s.add_all([Lead(company_id=a.id, source="research", status="qualified"),
                   Lead(company_id=b.id, source="research", status="new")])
    out = CRMManagementAgent(ctx).run({}).output
    assert out.merged == 1 and out.hygiene["companies"] == 1 and out.lifecycle_updates >= 1
    with ctx.db.session() as s:
        merged = s.execute(select(Company).where(Company.merged_into_id.is_not(None))).scalars().one()
        assert merged.lifecycle_stage == "merged"
        assert s.execute(select(Lead).where(Lead.status == "merged")).scalars().one()


def test_inquiry_sequence_stops_once_the_person_replied_or_booked(make_ctx):
    ctx = make_ctx(outbound_email_enabled=True, smtp_host="smtp.example", smtp_from="hello@us.example",
                   company_postal_address="1 Sample St, Austin, TX 78701")
    FakeSMTP.sent = []
    out = ingest(ctx)
    LeadQualificationAgent(ctx).run({"lead_ids": out.inbound_lead_ids})
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    assert SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()}).output.emails_sent >= 1
    with ctx.db.session() as s:
        first = s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalars().first()
        lead_id = first.lead_id
        for f in s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalars():
            f.sent_at = TUESDAY_11AM_ET - timedelta(days=4)
        s.get(Lead, lead_id).outreach_status = "meeting"  # they replied and booked a call
    later = TUESDAY_11AM_ET + timedelta(minutes=5)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": later.isoformat()})
    with ctx.db.session() as s:
        assert not s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.step == 2)).first()
        # an already-approved later step of that sequence is not sent either
        s.add(FollowUp(lead_id=lead_id, contact_id=first.contact_id, company_id=first.company_id, channel="email",
                       step=2, due_at=later, status="approved", approved_by="test", approved_at=utcnow(),
                       subject="Following up", body=first.body))
    FakeSMTP.sent = []
    SalesWorkflowAgent(ctx, sender=sender).run({"now": later.isoformat()})
    with ctx.db.session() as s:
        step2 = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.step == 2)).scalar_one()
        assert step2.status == "cancelled" and "sequence stopped (meeting)" in step2.blocked_reason
    assert FakeSMTP.sent == []


def test_replies_to_inquiries_get_the_postal_address_and_keep_their_footer(make_ctx):
    from fastapi.testclient import TestClient

    from aime.web.app import create_app
    origin = {"origin": "http://testserver"}
    ctx = make_ctx(outbound_email_enabled=True, smtp_host="smtp.example", smtp_from="hello@us.example")  # no address yet
    FakeSMTP.sent = []
    out = ingest(ctx)
    LeadQualificationAgent(ctx).run({"lead_ids": out.inbound_lead_ids})
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    ctx.settings.company_postal_address = "1 Sample St, Austin, TX 78701"  # configured afterwards
    with ctx.db.session() as s:
        (a_id, a_body), (b_id, b_body) = [(f.id, f.body) for f in s.execute(
            select(FollowUp).where(FollowUp.channel == "email").order_by(FollowUp.id)).scalars()][:2]
    assert "[postal address required" in a_body
    client = TestClient(create_app(ctx))
    client.post(f"/followups/{a_id}/approve", headers=origin)
    r = client.post(f"/followups/{b_id}/approve", data={"body": b_body.split("\n\n---\n")[0]}, headers=origin,
                    follow_redirects=False)
    assert "Cannot%20approve" in r.headers["location"]  # the opt-out footer was edited out
    with ctx.db.session() as s:
        a, b = s.get(FollowUp, a_id), s.get(FollowUp, b_id)
        assert a.status == "approved" and "1 Sample St" in a.body and "[postal" not in a.body
        assert b.status == "awaiting_approval"
        b.body, b.status, b.approved_by, b.approved_at = b_body.split("\n\n---\n")[0], "approved", "x", utcnow()
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert [m["To"] for m in FakeSMTP.sent] and all("unsubscribe" in m.get_content().lower() for m in FakeSMTP.sent)
    with ctx.db.session() as s:  # approved behind the dashboard's back without a footer: refused at send time
        b = s.get(FollowUp, b_id)
        assert b.status == "awaiting_approval" and "opt-out line" in b.blocked_reason


def test_first_answer_to_a_new_inquiry_is_sent_even_after_earlier_outreach_stopped(make_ctx):
    ctx = make_ctx(outbound_email_enabled=True, smtp_host="smtp.example", smtp_from="hello@us.example",
                   company_postal_address="1 Sample St, Austin, TX 78701")
    FakeSMTP.sent = []
    out = ingest(ctx)
    LeadQualificationAgent(ctx).run({"lead_ids": out.inbound_lead_ids})
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    with ctx.db.session() as s:  # we contacted them earlier and they said no; now they send an inquiry
        first = s.execute(select(FollowUp).where(FollowUp.status == "approved", FollowUp.step == 1)).scalars().first()
        lead_id, to = first.lead_id, first.contact.email
        s.get(Lead, lead_id).outreach_status = "not_interested"
        s.add(FollowUp(lead_id=lead_id, contact_id=first.contact_id, company_id=first.company_id, channel="email",
                       step=2, due_at=first.due_at, status="approved", approved_by="test", approved_at=utcnow(),
                       subject="Following up", body=first.body))
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert to in [m["To"] for m in FakeSMTP.sent] and "Following up" not in [m["Subject"] for m in FakeSMTP.sent]
    with ctx.db.session() as s:
        steps = {f.step: f for f in s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id,
                                                                     FollowUp.channel == "email")).scalars()}
        assert steps[1].status == "sent"
        assert steps[2].status == "cancelled" and "sequence stopped (not_interested)" in steps[2].blocked_reason
