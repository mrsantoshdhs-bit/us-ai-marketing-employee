"""Emails and LinkedIn posts never go out without an approval record, whatever the settings say."""

from datetime import timedelta

import httpx
from sqlalchemy import select

from aime.agents import AuthorizedPublishingAgent, LeadQualificationAgent, LinkedInContentAgent, SalesWorkflowAgent
from aime.doctor import run_checks
from aime.integrations.email_smtp import EmailSender
from aime.models import Company, Contact, ContentItem, FollowUp, Lead, OAuthToken
from aime.timeutil import utcnow
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP, ingest

EMAIL_READY = dict(outbound_email_enabled=True, smtp_host="smtp.example", smtp_from="hello@us.example",
                   company_postal_address="1 Sample St, Austin, TX 78701")
LINKEDIN_READY = dict(linkedin_client_id="cid", linkedin_client_secret="secret", publishing_enabled=True)


def _no_calls(calls):
    def handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"})
    return handler


def test_email_drafts_need_approval_even_if_setting_is_false(make_ctx):
    ctx = make_ctx(require_human_approval=False, **EMAIL_READY)
    out = ingest(ctx)
    LeadQualificationAgent(ctx).run({"lead_ids": out.inbound_lead_ids})
    FakeSMTP.sent = []
    SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run(
        {"now": TUESDAY_11AM_ET.isoformat()})
    with ctx.db.session() as s:
        emails = s.execute(select(FollowUp).where(FollowUp.channel == "email")).scalars().all()
    assert emails and all(f.status == "awaiting_approval" for f in emails)
    assert FakeSMTP.sent == []


def test_posts_need_approval_even_if_setting_is_false(make_ctx):
    ctx = make_ctx(require_human_approval=False)
    res = LinkedInContentAgent(ctx).run({"research_topics": False})
    assert res.ok and res.output.created
    with ctx.db.session() as s:
        assert {i.status for i in s.execute(select(ContentItem)).scalars()} == {"awaiting_approval"}


def _approved_email_without_record(ctx):
    with ctx.db.session() as s:
        co = Company(name="Sample Co", normalized_name="sample co", source="test", state="TX")
        s.add(co)
        s.flush()
        ct = Contact(company_id=co.id, email="sam@sample.example", source="inbound", consent_status="inquiry")
        s.add(ct)
        s.flush()
        lead = Lead(company_id=co.id, contact_id=ct.id, source="inbound", status="qualified")
        s.add(lead)
        s.flush()
        f = FollowUp(lead_id=lead.id, contact_id=ct.id, company_id=co.id, channel="email", step=1,
                     due_at=utcnow() - timedelta(minutes=1), status="approved", subject="Hi", body="Body")
        s.add(f)
        s.flush()
        return f.id


def test_email_marked_approved_without_record_is_not_sent(make_ctx):
    ctx = make_ctx(**EMAIL_READY)
    fid = _approved_email_without_record(ctx)
    FakeSMTP.sent = []
    out = SalesWorkflowAgent(ctx, sender=EmailSender(ctx.settings, smtp_factory=FakeSMTP)).run(
        {"now": TUESDAY_11AM_ET.isoformat()}).output
    assert out.emails_sent == 0 and FakeSMTP.sent == []
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        assert f.status == "awaiting_approval" and "approval" in f.blocked_reason


def test_post_marked_approved_without_record_is_not_published(make_ctx):
    calls = []
    ctx = make_ctx(handler=_no_calls(calls), **LINKEDIN_READY)
    with ctx.db.session() as s:
        s.add(OAuthToken(provider="linkedin", access_token="tok", scope="openid profile w_member_social",
                         subject="abc123", expires_at=utcnow() + timedelta(days=30)))
        item = ContentItem(service="ai_agents", industry="saas", title="T", body="Body?", hashtags=[],
                           scheduled_for=utcnow() - timedelta(minutes=5), status="approved", generator="template",
                           fingerprint="fp")
        s.add(item)
        s.flush()
        cid = item.id
    out = AuthorizedPublishingAgent(ctx).run({}).output
    assert out.published == [] and calls == []
    with ctx.db.session() as s:
        assert s.get(ContentItem, cid).status == "awaiting_approval"


def test_doctor_flags_email_sent_without_approval(ctx):
    fid = _approved_email_without_record(ctx)
    with ctx.db.session() as s:
        s.get(FollowUp, fid).status = "sent"
    check = next(c for c in run_checks(ctx) if c["name"] == "No email sent without approval")
    assert not check["ok"] and check["severity"] == "error"


def test_doctor_warns_when_approval_setting_is_false(make_ctx):
    ctx = make_ctx(require_human_approval=False)
    check = next(c for c in run_checks(ctx) if c["name"] == "Human approval setting")
    assert not check["ok"] and check["severity"] == "warning" and "campaign authorizations" in check["detail"]
