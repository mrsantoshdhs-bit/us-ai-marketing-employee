"""A campaign authorization's daily and total limits always hold: approvals that wait count against them, campaigns
approve only inside the send window, the limit is checked again just before sending, and REQUIRE_HUMAN_APPROVAL
switched back on takes back every approval a campaign gave. Fake SMTP server only; nothing leaves the test."""

from datetime import timedelta

from sqlalchemy import select

from aime import automation as am
from aime.models import CampaignAuthorization, FollowUp
from tests.test_automation import NOW, authorize, drafted, followup, run
from tests.test_inbound_sales import FakeSMTP

NIGHT = NOW + timedelta(hours=12)  # 23:00 US Eastern on the same Tuesday: outside the 08:00-18:00 send window


def twin(ctx, fid) -> int:
    """A second waiting outreach email to the same researched contact (limits are per campaign, not per email)."""
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        copy = FollowUp(lead_id=f.lead_id, contact_id=f.contact_id, company_id=f.company_id, channel="email", step=1,
                        due_at=f.due_at, status="awaiting_approval", subject=f.subject + " (2)", body=f.body,
                        kind=f.kind, to_email=f.to_email, evidence=f.evidence, quality=f.quality)
        s.add(copy)
        s.flush()
        return copy.id


def test_approvals_that_wait_count_against_the_daily_limit(make_ctx):
    ctx, _sender, fid = drafted(make_ctx, require_human_approval=False)
    second = twin(ctx, fid)
    aid = authorize(ctx, daily=1, total=5)
    with ctx.db.session() as s:
        first = s.get(FollowUp, fid)
        first.status, first.approved_at, first.approved_by, first.authorization_id = "approved", NOW, f"campaign #{aid}", aid
    with ctx.db.session() as s:
        covered, why = am.campaign_for(s, s.get(FollowUp, second), "hybrid", NOW)
        assert covered is None and "limit is reached" in why  # the waiting approval already uses today's one
        assert am.committed_counts(s, s.get(CampaignAuthorization, aid), NOW) == (1, 1)
        assert am.sent_counts(s, s.get(CampaignAuthorization, aid), NOW) == (0, 0)  # the page still shows real sends


def test_campaigns_approve_nothing_outside_the_send_window(make_ctx):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=False)
    authorize(ctx)
    run(ctx, sender, NIGHT)
    assert followup(ctx, fid).status == "awaiting_approval" and followup(ctx, fid).authorization_id is None
    assert FakeSMTP.sent == []
    run(ctx, sender, NOW + timedelta(days=1))  # Wednesday 11:00 ET
    assert followup(ctx, fid).status == "sent" and len(FakeSMTP.sent) == 1


def test_the_limit_is_checked_again_just_before_sending(make_ctx):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=False)
    second = twin(ctx, fid)
    aid = authorize(ctx, daily=1, total=5)
    with ctx.db.session() as s:  # one already sent today under the campaign, and one approved by older code
        sent = s.get(FollowUp, fid)
        sent.status, sent.sent_at, sent.authorization_id = "sent", NOW - timedelta(hours=1), aid
        sent.approved_at, sent.approved_by = NOW - timedelta(hours=2), f"campaign #{aid}"
        extra = s.get(FollowUp, second)
        extra.status, extra.approved_at, extra.approved_by, extra.authorization_id = "approved", NOW, f"campaign #{aid}", aid
    FakeSMTP.sent = []
    run(ctx, sender)
    f = followup(ctx, second)
    assert FakeSMTP.sent == [] and f.status == "awaiting_approval" and "limit is reached" in f.blocked_reason


def test_require_human_approval_switched_back_on_takes_back_campaign_approvals(make_ctx):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=False)
    aid = authorize(ctx)
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        f.status, f.approved_at, f.approved_by, f.authorization_id = "approved", NOW, f"campaign #{aid}", aid
    ctx.settings.require_human_approval = True
    FakeSMTP.sent = []
    run(ctx, sender)
    f = followup(ctx, fid)
    assert FakeSMTP.sent == [] and f.status == "awaiting_approval" and "REQUIRE_HUMAN_APPROVAL=true" in f.blocked_reason
    assert f.approved_by is None and f.authorization_id is None


def test_a_campaign_never_sends_more_than_its_total_over_several_days(make_ctx):
    ctx, sender, fid = drafted(make_ctx, require_human_approval=False)
    ids = [fid] + [twin(ctx, fid) for _ in range(3)]
    aid = authorize(ctx, daily=5, total=2)
    for day in range(4):
        run(ctx, sender, NOW + timedelta(days=day))
    with ctx.db.session() as s:
        statuses = [s.get(FollowUp, i).status for i in ids]
        assert statuses.count("sent") == 2 and am.sent_counts(s, s.get(CampaignAuthorization, aid), NOW + timedelta(days=4))[1] == 2
        assert not s.execute(select(FollowUp).where(FollowUp.authorization_id == aid, FollowUp.status == "approved")).first()
    assert len(FakeSMTP.sent) == 2
