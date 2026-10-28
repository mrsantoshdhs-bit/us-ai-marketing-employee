"""Rule-based campaign authorizations: an administrator can authorize outreach to qualified leads that meet a rule
(grade, which published address, verified website) within daily and total limits, instead of listing addresses.
They are off until created and activated, and every existing send-time gate still applies."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import automation as am
from aime.models import AuditEvent, CampaignAuthorization, FollowUp
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.test_automation import NOW, ORIGIN, drafted, followup, run
from tests.test_inbound_sales import FakeSMTP
from tests.test_inbox import DAVID


def shape(ctx, fid, grade="B", status="qualified", website="verified", email_status="published"):
    """Set the lead, company and contact of the draft to the case under test."""
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        f.lead.grade, f.lead.status = grade, status
        f.lead.company.website_status = website
        f.contact.email_status, f.contact.source = email_status, "website"


def rule(ctx, min_grade="B", addresses="role_or_published", verified_website=True, activate=True):
    with ctx.db.session() as s:
        a = am.create_rule_authorization(s, "admin", "Qualified outreach", min_grade, addresses, verified_website, 5, 20, 7)
        a.starts_at, a.expires_at = NOW - timedelta(days=1), NOW + timedelta(days=6)  # around the simulated clock
        if activate:
            am.change_authorization(s, a, "activate", "admin")
        return a.id


def test_matching_outreach_goes_out_under_the_rule_without_a_person(make_ctx):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=False)
    shape(ctx, fid)
    aid = rule(ctx)
    run(ctx, sender)
    f = followup(ctx, fid)
    assert f.status == "sent" and f.approved_by == f"campaign #{aid}" and f.authorization_id == aid
    assert [m["To"] for m in FakeSMTP.sent] == [DAVID]
    with ctx.db.session() as s:
        [e] = s.execute(select(AuditEvent).where(AuditEvent.action == "auto_approved")).scalars().all()
        assert DAVID in e.detail


@pytest.mark.parametrize("case,expected", [
    (dict(grade="C"), "graded C, below B"),
    (dict(website="unreachable"), "website is not verified"),
    (dict(status="nurture"), "not qualified"),
    (dict(email_status="role_address"), "not one the rule allows"),
])
def test_outreach_outside_the_rule_waits_for_a_person(make_ctx, case, expected):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=False)
    shape(ctx, fid, **case)
    rule(ctx, addresses="published")
    with ctx.db.session() as s:
        covered, why = am.campaign_for(s, s.get(FollowUp, fid), "hybrid", utcnow())
    assert covered is None and expected in why
    run(ctx, sender)
    assert followup(ctx, fid).status in ("awaiting_approval", "cancelled") and FakeSMTP.sent == []


@pytest.mark.parametrize("gate", ["approval_required", "draft_only", "approval_mode"])
def test_every_existing_gate_still_holds_a_rule(make_ctx, gate):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=gate == "approval_required")
    shape(ctx, fid)
    rule(ctx, activate=gate != "draft_only")
    if gate == "approval_mode":
        with ctx.db.session() as s:
            am.set_mode(s, "approval", "admin")
    run(ctx, sender)
    assert followup(ctx, fid).status == "awaiting_approval" and FakeSMTP.sent == []


def test_the_form_saves_a_draft_rule_and_logs_it(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    r = client.post("/automation/authorizations/rule", headers=ORIGIN, follow_redirects=False,
                    data={"name": "Grade B outreach", "min_grade": "B", "addresses": "role_or_published",
                          "verified_website": "yes", "daily_limit": "3", "total_limit": "10", "days": "5"})
    assert r.status_code == 303
    with ctx.db.session() as s:
        [a] = s.execute(select(CampaignAuthorization)).scalars().all()
        assert a.status == "draft" and a.recipients == [] and a.kinds == ["outreach"]
        assert a.rule == {"min_grade": "B", "addresses": "role_or_published", "verified_website": True}
        [e] = s.execute(select(AuditEvent).where(AuditEvent.action == "authorization_created")).scalars().all()
        assert e.detail.startswith("rule: qualified leads graded B or better")
    bad = client.post("/automation/authorizations/rule", headers=ORIGIN, follow_redirects=False,
                      data={"name": "x", "min_grade": "C"})
    assert "grade" in bad.headers["location"]
    assert "New rule-based authorization" in client.get("/automation").text
