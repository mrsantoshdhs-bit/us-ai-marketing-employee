"""Re-engagement: a lead whose whole outreach sequence went unanswered gets ONE short check-in months later, waiting
for a person's approval (never a campaign's), and never anyone who said no, unsubscribed or bounced."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.automation import campaign_for
from aime.crm_ops import suppress
from aime.integrations.email_smtp import EmailSender
from aime.models import FollowUp, Lead
from aime.web.app import create_app
from tests.test_company_research import SITE
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_outreach import ORIGIN, POSTAL, SENDING, approve_all, drafts, lead_of, researched

LATER = TUESDAY_11AM_ET  # a Tuesday, 11:00 in New York (inside the send window), before today: a send stamps the clock
LAST = LATER - timedelta(days=98)  # the third and last email of the sequence


def parked(make_ctx, **overrides):
    """A researched lead whose 3 emails went unanswered: outreach stage no_response, the lead in nurture."""
    ctx = researched(make_ctx, **overrides)
    ctx.settings.business["outreach"]["reengage_after_days"] = 90
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        lead.status, lead.outreach_status = "nurture", "no_response"
        for step in (1, 2, 3):
            s.add(FollowUp(lead_id=lead.id, contact_id=lead.contact_id, company_id=lead.company_id, channel="email",
                           kind="outreach", step=step, status="sent", subject=f"Step {step}", body="...",
                           to_email=lead.contact.email, sent_at=LAST - timedelta(days=10 - 4 * step),
                           message_id=f"<m{step}@us.example>", delivery_status="sent"))
    return ctx


def run(ctx, when, sender=None):
    return SalesWorkflowAgent(ctx, sender=sender).run({"now": when.isoformat()}).output


def test_one_check_in_months_after_an_unanswered_sequence_then_never_again(make_ctx):
    ctx = parked(make_ctx, **SENDING)
    FakeSMTP.sent = []
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    assert run(ctx, LAST + timedelta(days=89), sender).reengage_drafts == 0  # too soon
    out = run(ctx, LATER, sender)
    assert out.reengage_drafts == 1 and out.emails_sent == 0  # it waits for a person
    f = drafts(ctx)[-1]
    assert (f.kind, f.purpose, f.status, f.step, f.to_email) == ("outreach", "reengage", "awaiting_approval", 4,
                                                                 f"david.chen@{SITE}")
    assert f.subject.startswith("Checking back in") and f.body.startswith("Hi David,")
    assert "I will not write again" in f.body and "unsubscribe" in f.body and "1 Sample St" in f.body
    assert f.in_reply_to == "<m3@us.example>" and f.quality["qc"]["status"] == "passed", f.quality["qc"]
    assert (lead_of(ctx).status, lead_of(ctx).outreach_status) == ("nurture", "reengage")
    assert run(ctx, LATER, sender).reengage_drafts == 0  # no second draft while one is open
    assert "re-engagement · once" in TestClient(create_app(ctx)).get("/followups").text

    approve_all(ctx)
    out = run(ctx, LATER, sender)
    assert out.emails_sent == 1 and FakeSMTP.sent[0]["To"] == f"david.chen@{SITE}"
    assert FakeSMTP.sent[0]["In-Reply-To"] == "<m3@us.example>" and FakeSMTP.sent[0]["List-Unsubscribe"]
    assert lead_of(ctx).outreach_status == "contacted"
    with ctx.db.session() as s:  # the send stamped today's clock; put it on the run's date
        s.execute(select(FollowUp).where(FollowUp.purpose == "reengage")).scalar_one().sent_at = LATER
    run(ctx, LATER + timedelta(days=8), sender)  # no answer again: the pipeline check parks it again
    assert (lead_of(ctx).status, lead_of(ctx).outreach_status) == ("nurture", "no_response")
    assert run(ctx, LATER + timedelta(days=400), sender).reengage_drafts == 0  # never a second time
    assert len(FakeSMTP.sent) == 1


@pytest.mark.parametrize("change", ["said_no", "unsubscribed", "bounced", "suppressed", "turned_off", "policy_off",
                                    "competitor", "already_reengaged"])
def test_nobody_who_said_no_or_cannot_be_reached_is_reengaged(make_ctx, change):
    ctx = parked(make_ctx, **POSTAL)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        if change in ("said_no", "unsubscribed", "bounced"):
            lead.outreach_status = {"said_no": "not_interested"}.get(change, change)
        elif change == "suppressed":
            suppress(s, SITE, "Asked not to be contacted")
        elif change == "turned_off":
            ctx.settings.business["outreach"]["reengage_after_days"] = 0
        elif change == "policy_off":
            ctx.settings.business["outreach"]["b2b_enabled"] = False
        elif change == "competitor":
            lead.company.is_competitor = True
        else:
            s.add(FollowUp(lead_id=lead.id, contact_id=lead.contact_id, company_id=lead.company_id, channel="email",
                           kind="outreach", purpose="reengage", step=4, status="rejected", subject="x", body="x"))
    assert run(ctx, LATER).reengage_drafts == 0
    assert not [f for f in drafts(ctx) if f.status == "awaiting_approval"]


def test_rejecting_it_parks_the_lead_again_and_no_campaign_ever_approves_it(make_ctx):
    ctx = parked(make_ctx, **POSTAL)
    run(ctx, LATER)
    f = drafts(ctx)[-1]
    with ctx.db.session() as s:
        a, why = campaign_for(s, s.get(FollowUp, f.id), "automatic", LATER)
        assert a is None and "always needs a person" in why
    TestClient(create_app(ctx)).post(f"/followups/{f.id}/reject", headers=ORIGIN)
    assert drafts(ctx)[-1].status == "rejected" and lead_of(ctx).outreach_status == "no_response"
    assert run(ctx, LATER + timedelta(days=200)).reengage_drafts == 0


def test_a_reply_before_it_goes_out_cancels_it(make_ctx):
    ctx = parked(make_ctx, **POSTAL)
    run(ctx, LATER)
    lead = lead_of(ctx)
    TestClient(create_app(ctx)).post(f"/leads/{lead.id}/replied", headers=ORIGIN)
    assert drafts(ctx)[-1].status == "cancelled" and lead_of(ctx).outreach_status == "replied"
