"""The autonomous sales flow inside an approved campaign: a rule campaign may also answer its own prospects; in
Automatic mode the AI employee offers meeting times, books the time the prospect clearly chose and confirms it on its
own; anything unclear, sensitive or outside the campaign goes to a person. Interested prospects and booked meetings
are handed over to the configured Sales Manager. Discovery follows the configured country, and the Super Admin sets
targeting, limits, sequence, meeting hours and the Sales Manager from the dashboard. Fakes only: no real mailbox,
email, calendar or meeting."""

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import admin_settings
from aime import automation as am
from aime.agents.research import USBusinessResearchAgent
from aime.booking import chosen_slot
from aime.cli import cmd_user
from aime.config import Settings
from aime.context import create_context
from aime.integrations.email_smtp import EmailSender
from aime.models import Alert, AuditEvent, FollowUp, InboundEmail, Meeting, MeetingProposal, Opportunity
from aime.web.app import create_app
from tests.test_a2_mailbox import GOOGLE, sign_in
from tests.test_a4_meetings import ORIGIN, FakeGoogle
from tests.test_automation import NOW, authorize, followup, run
from tests.test_dashboard_auth import GOOD, Args, _login
from tests.test_inbound_sales import FakeSMTP
from tests.test_inbox import DAVID, FakeIMAP, contacted, mail, run_inbox

SLOTS = [datetime(2026, 10, 28, 14, 0), datetime(2026, 10, 29, 13, 0), datetime(2026, 10, 30, 13, 0)]  # Wed 10, Thu 9, Fri 9 EDT


# --------------------------------------------------------------------------- reading the chosen time

@pytest.mark.parametrize("text,index", [
    ("The second one works for me, thanks", 1),
    ("Thursday works", 1),
    ("10am Wednesday is great", 0),
    ("Option 3 please", 2),
    ("#1", 0),
])
def test_a_clearly_chosen_time_is_read(text, index):
    assert chosen_slot(text, SLOTS, "America/New_York")[0] == index


@pytest.mark.parametrize("text,why", [
    ("Sounds good!", "does not name"),
    ("Thursday or Friday", "exactly one"),
    ("Friday at 10am", "exactly one"),  # Friday was offered at 9am
    ("None of these work, sorry", "another time"),
    ("Thursday doesn't work, can we do next week?", "another time"),
])
def test_anything_unclear_is_left_to_a_person(text, why):
    index, reason = chosen_slot(text, SLOTS, "America/New_York")
    assert index is None and why in reason


# --------------------------------------------------------------------------- who may act on its own

def test_the_ai_acts_on_its_own_only_inside_an_automatic_campaign_that_covers_answers(make_ctx):
    ctx, _sender, _sent = contacted(make_ctx)
    with ctx.db.session() as s:
        lead_contact = s.execute(select(FollowUp)).scalars().first()
        lead, contact = lead_contact.lead, lead_contact.contact
        assert am.autonomous_authorization(s, ctx.settings, lead, contact, DAVID) is None  # needs a person
    ctx.settings.require_human_approval = False
    authorize(ctx, kinds=("outreach",))
    with ctx.db.session() as s:
        am.set_mode(s, "automatic", "admin")
        lead, contact = s.merge(lead), s.merge(contact)
        assert am.autonomous_authorization(s, ctx.settings, lead, contact, DAVID, NOW) is None  # no answers covered
    aid = authorize(ctx, kinds=("outreach", "response"))
    with ctx.db.session() as s:
        lead, contact = s.merge(lead), s.merge(contact)
        assert am.autonomous_authorization(s, ctx.settings, lead, contact, DAVID, NOW).id == aid
        assert am.autonomous_authorization(s, ctx.settings, lead, contact, "someone@else.example", NOW) is None
        am.change_state(s, "pause", "manager")
        assert am.autonomous_authorization(s, ctx.settings, lead, contact, DAVID, NOW) is None


def test_a_rule_campaign_can_also_answer_its_own_prospects(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    for answer in ("", "yes"):
        client.post("/automation/authorizations/rule", headers=ORIGIN, follow_redirects=False,
                    data={"name": f"Rule {answer or 'no'}", "min_grade": "B", "addresses": "role_or_published",
                          "verified_website": "yes", "daily_limit": "3", "total_limit": "9", "days": "5",
                          "answer_replies": answer})
    with ctx.db.session() as s:
        made = [a.kinds for a in s.execute(select(am.CampaignAuthorization).order_by(am.CampaignAuthorization.id)).scalars()]
        assert made == [["outreach"], ["outreach", "response"]]
        assert "meeting offers, booking and meeting notes" in s.execute(
            select(AuditEvent).order_by(AuditEvent.id.desc())).scalars().first().detail
    assert "Also answer these prospects on its own" in client.get("/automation").text


# --------------------------------------------------------------------------- the autonomous chain

def autonomous_ctx(make_ctx, mode="automatic"):
    ctx, _sender, first = contacted(make_ctx, **GOOGLE, calendar_provider="google", meeting_booking_enabled=True)
    ctx.settings.business.setdefault("handover", {})["sales_manager"] = "manager"
    cal = FakeGoogle()
    ctx.http_transport = httpx.MockTransport(cal)
    sign_in(ctx, purpose="calendar")
    ctx.settings.require_human_approval = False
    aid = authorize(ctx, kinds=("outreach", "response"))
    with ctx.db.session() as s:
        am.set_mode(s, mode, "admin")
    FakeIMAP.messages = {}
    return ctx, EmailSender(ctx.settings, smtp_factory=FakeSMTP), first, cal, aid


def reply(ctx, text, in_reply_to, msg_id):
    uid = max(FakeIMAP.messages or {0: None}) + 1
    FakeIMAP.messages[uid] = mail(f"David Chen <{DAVID}>", "Re: call", text, in_reply_to=in_reply_to, msg_id=msg_id)
    run_inbox(ctx)


def test_offer_book_and_confirm_on_their_own_and_hand_over_to_the_sales_manager(make_ctx):
    ctx, sender, first, cal, aid = autonomous_ctx(make_ctx)
    FakeSMTP.sent = []
    reply(ctx, "Sounds interesting. Can we talk Thursday?", first.message_id, "<ask@mail.example>")
    with ctx.db.session() as s:
        p = s.execute(select(MeetingProposal)).scalar_one()
        opp = s.execute(select(Opportunity)).scalar_one()
        assert opp.owner == "manager"  # handed over on the interested reply
        assert s.execute(select(Alert).where(Alert.source == "handover")).scalars().first().message.startswith(
            "Handed over to manager")
        pid, offer_id = p.id, p.offer_followup_id
    assert "Sales Manager: manager" in TestClient(create_app(ctx)).get("/pipeline").text
    run(ctx, sender, NOW + timedelta(hours=1))  # the offer goes out under the campaign
    offer = followup(ctx, offer_id)
    assert offer.status == "sent" and offer.approved_by == f"campaign #{aid}" and len(FakeSMTP.sent) == 1
    reply(ctx, "The second one works for me, thanks.", offer.message_id, "<pick@mail.example>")
    with ctx.db.session() as s:
        p = s.get(MeetingProposal, pid)
        m = s.execute(select(Meeting)).scalar_one()
        assert p.status == "booked" and m.starts_at == datetime.fromisoformat(p.slots[1]) and m.event_id in cal.events
        pick = s.execute(select(InboundEmail).where(InboundEmail.message_id == "<pick@mail.example>")).scalar_one()
        assert pick.status == "actioned" and pick.action_note.startswith("Booked on its own: option 2")
        booked = s.execute(select(AuditEvent).where(AuditEvent.action == "meeting_booked")).scalar_one()
        assert booked.actor == f"AI employee (campaign #{aid})"
        confirm = s.execute(select(FollowUp).where(FollowUp.purpose == "meeting_confirmation")).scalar_one()
        handovers = [a.message for a in s.execute(select(Alert).where(Alert.source == "handover")).scalars()]
        assert any("meeting booked for" in h for h in handovers)
    run(ctx, sender, NOW + timedelta(hours=2))  # the confirmation goes out under the campaign too
    assert followup(ctx, confirm.id).status == "sent" and len(FakeSMTP.sent) == 2
    with ctx.db.session() as s:  # nothing else left for a person to send by mistake
        assert [(f.purpose, f.status) for f in s.execute(select(FollowUp).where(
            FollowUp.status.in_(("awaiting_approval", "approved", "draft")))).scalars()] == []


@pytest.mark.parametrize("mode,text,expect", [
    ("automatic", "Thursday or Friday, whichever", "Which time?"),
    ("automatic", "Option 1 please. Also, what would this cost roughly?", "wrote more that needs an answer"),
    ("hybrid", "The second one works for me", "They chose option 2"),
])
def test_unclear_answers_questions_and_hybrid_mode_go_to_a_person(make_ctx, mode, text, expect):
    ctx, sender, first, cal, _aid = autonomous_ctx(make_ctx, mode=mode)
    reply(ctx, "Sounds interesting. Can we talk Thursday?", first.message_id, "<ask@mail.example>")
    with ctx.db.session() as s:
        p = s.execute(select(MeetingProposal)).scalar_one()
        offer = s.get(FollowUp, p.offer_followup_id)
        offer.status, offer.sent_at, offer.message_id = "sent", NOW, "<offer@us.example>"  # sent (by a person, in Hybrid)
        pid = p.id
    run_inbox(ctx)  # the proposal shows as offered
    reply(ctx, text, "<offer@us.example>", "<pick@mail.example>")
    with ctx.db.session() as s:
        assert s.execute(select(Meeting)).first() is None and cal.events == {}
        pick = s.execute(select(InboundEmail).where(InboundEmail.message_id == "<pick@mail.example>")).scalar_one()
        assert pick.status == "needs_review" and expect in pick.action_note
        assert s.get(MeetingProposal, pid).status == "offered"  # a person can still book it on the Meetings page
        if pick.suggested_followup_id:  # the drafted answer to their question is kept for the person
            assert s.get(FollowUp, pick.suggested_followup_id).status == "awaiting_approval"


# --------------------------------------------------------------------------- discovery follows the country

def test_discovery_follows_the_configured_country(make_ctx):
    ctx = make_ctx()
    ctx.settings.business["targeting"]["country"] = "CA"
    out = USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring", "sec_edgar"]}).output
    reports = {r.name: r.message for r in out.sources}
    assert "no CA coverage" in reports["hn_hiring"] and "no CA coverage" in reports["sec_edgar"]
    assert out.companies_created == 0


# --------------------------------------------------------------------------- Super Admin sales settings

def form(ctx, **changes):
    values = admin_settings.current(ctx.settings)
    data = {k: (", ".join(map(str, v)) if isinstance(v, list) and not k.endswith(("industries", "services")) else v)
            for k, v in values.items()}
    data.update(changes)
    pairs = []
    for k, v in data.items():
        if isinstance(v, list):
            pairs += [(k, x) for x in v]
        elif isinstance(v, bool):
            if v:
                pairs.append((k, "yes"))
        elif v is not None:
            pairs.append((k, str(v)))
    return pairs


class Form:
    def __init__(self, pairs):
        self.pairs = pairs

    def get(self, key, default=None):
        return next((v for k, v in self.pairs if k == key), default)

    def getlist(self, key):
        return [v for k, v in self.pairs if k == key]


def test_sales_settings_are_validated_stored_applied_audited_and_reset(make_ctx):
    ctx = make_ctx()
    assert cmd_user(Args("create", "manager"), ctx, prompt_fn=lambda _p: GOOD) == 0
    with ctx.db.session() as s:
        changes = admin_settings.save(s, ctx.settings, "boss", Form(form(
            ctx, **{"outreach.daily_send_limit": "12", "targeting.states": "ca, tx", "handover.sales_manager": "manager",
                    "targeting.industries": ["saas", "real_estate"]})))
    assert ctx.settings.section("outreach")["daily_send_limit"] == 12 and ctx.settings.section("targeting")["states"] == ["CA", "TX"]
    assert any("outreach.daily_send_limit: 20 -> 12" in c for c in changes)
    with ctx.db.session() as s:
        event = s.execute(select(AuditEvent).where(AuditEvent.action == "settings_changed")).scalar_one()
        assert event.actor == "boss" and "handover.sales_manager" in event.detail
    fresh = create_context(Settings.load(env_file=ctx.settings.data_dir / "none.env", data_dir=ctx.settings.data_dir,
                                         database_url="", llm_provider="template", data_role="test"))
    assert fresh.settings.section("outreach")["daily_send_limit"] == 12  # the scheduled jobs see it on their next run
    fresh.db.dispose()
    for bad, message in ((dict(**{"qualification.grade_b": "90"}), "Grades must go down"),
                         (dict(**{"outreach.intervals_days": "0, 4"}), "one day number for each email"),
                         (dict(**{"meetings.owner_hours": "late"}), "09:00-17:00"),
                         (dict(**{"followup.send_window_timezone": "Mars/Base"}), "time zone"),
                         (dict(**{"handover.sales_manager": "ghost"}), "active dashboard user")):
        with ctx.db.session() as s, pytest.raises(ValueError, match=message):
            admin_settings.save(s, ctx.settings, "boss", Form(form(ctx, **bad)))
    assert ctx.settings.section("outreach")["daily_send_limit"] == 12  # nothing half-saved
    with ctx.db.session() as s:
        admin_settings.reset(s, ctx.settings, "boss")
    assert ctx.settings.section("outreach")["daily_send_limit"] == 20 and ctx.settings.section("targeting")["states"] == "ALL"


def test_only_a_super_admin_sees_the_sales_settings(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    for name, role in (("owner", "admin"), ("boss", "super_admin")):
        assert cmd_user(Args("create", name, role=role), ctx, prompt_fn=lambda _p: GOOD) == 0
    owner = TestClient(create_app(ctx))
    _login(owner, username="owner")
    assert owner.get("/admin/settings", follow_redirects=False).status_code == 403
    boss = TestClient(create_app(ctx))
    _login(boss, username="boss")
    page = boss.get("/admin/settings").text
    assert "Sales settings" in page and "Target country" in page and "Hacker News hiring posts" in page
    data = {}
    for k, v in form(ctx, **{"outreach.sends_per_run": "4"}):
        data.setdefault(k, []).append(v)
    r = boss.post("/admin/settings", data=data, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "Saved" in r.headers["location"] and ctx.settings.section("outreach")["sends_per_run"] == 4
