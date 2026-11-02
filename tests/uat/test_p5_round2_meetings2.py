"""Phase 5, second round: each test states what must hold. Synthetic data only, fake SMTP and IMAP, a local mail server."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

import aime.timeutil as tu
from aime.agents.qualification import NEW_POST_TASK, LeadQualificationAgent
from aime.agents.sales_workflow import HAND_TASK, SalesWorkflowAgent
from aime.crm_ops import add_signal, lift_suppression, suppress
from aime.db import kv_get
from aime.models import Activity, Alert, Contact, FollowUp, Lead, Meeting, Opportunity
from aime.outreach import has_live_meeting
from aime.pipeline import pipeline_checks
from tests.test_inbound_sales import TUESDAY_11AM_ET
from tests.test_inbox import contacted
from tests.test_outreach import drafts, lead_of
from tests.test_time_and_alerts import MAIL
from tests.uat import p2_mail_corpus as K
from tests.uat import test_uat_email as U
from tests.uat.test_p5_inbox_reading import PERSONAL, Picky, address_searches, read, skipped, state_of, stored, we_wrote
from tests.uat.test_p5_meetings import A, B, C, DAVID, LINK, ORIGIN, _draft_status, _reply_draft, deliver, live, notice, notices, open_alerts, web
from tests.uat.test_p5_scoring import new_post, person_parks, qualified_lead, site, status_of, tasks

NOW = TUESDAY_11AM_ET
T = "Mon, 02 Nov 2026 10:{:02d}:00 +0000"
OTHER = "ana@nobody-we-know.example"


def apply(ctx, row_id):
    return web(ctx).post(f"/inbox/{row_id}/apply_calendly", headers=ORIGIN, follow_redirects=False)


@pytest.fixture
def ctx(make_ctx):
    return contacted(make_ctx, calendly_verified_only=False, calendly_url=LINK)[0]


def meetings(c):
    with c.db.session() as s:
        return [(m.status, m.starts_at.strftime("%d %H:%M")) for m in s.execute(select(Meeting).order_by(Meeting.id)).scalars()]


def waits(c):
    return [a.message for a in open_alerts(c) if "waits on Replies" in a.message]


# ---------------------------------------------------------------- 1. an applied notice blocks nothing
def test_a_cancellation_for_an_unmatched_invitee_can_be_applied_and_the_meeting_ends_cancelled(make_ctx):
    c = contacted(make_ctx, calendly_verified_only=True, calendly_url=LINK)[0]
    deliver(c, notice("booked", A, who=OTHER, dated=T.format(0)))
    apply(c, notices(c)[0].id)
    assert meetings(c) == [("scheduled", "08 15:00")]
    deliver(c, notice("cancelled", A, who=OTHER, dated=T.format(5)))
    assert waits(c)
    got = apply(c, notices(c)[1].id)
    assert got.status_code == 303
    assert meetings(c) == [("cancelled", "08 15:00")], "the meeting must end cancelled"
    assert not waits(c), "what was applied waits no longer"


def test_a_notice_nobody_applied_still_has_to_go_first(make_ctx):
    c = contacted(make_ctx, calendly_verified_only=True, calendly_url=LINK)[0]
    deliver(c, notice("booked", A, who=OTHER, dated=T.format(0)), notice("cancelled", A, who=OTHER, dated=T.format(5)))
    apply(c, notices(c)[1].id)
    assert meetings(c) == []


# ---------------------------------------------------------------- 2. each notice's own date
def test_a_late_copy_of_a_reschedule_does_not_move_the_meeting_back(ctx):
    deliver(ctx, notice("booked", A, dated=T.format(0)), notice("moved", B, old=A, dated=T.format(5)), notice("moved", C, old=B, dated=T.format(10)))
    assert live(ctx) == ["10 17:00"]
    deliver(ctx, notice("moved", B, old=A, dated=T.format(5)))
    assert live(ctx) == ["10 17:00"]
    row = notices(ctx)[-1]
    assert row.status == "needs_review" and "out of date" in row.action_note and waits(ctx), "a person is told"


def test_two_moves_read_the_wrong_way_round_end_on_the_newer_time(ctx):
    deliver(ctx, notice("booked", A, dated=T.format(0)), notice("moved", C, old=A, dated=T.format(10)), notice("moved", B, old=A, dated=T.format(5)))
    assert live(ctx) == ["10 17:00"]
    assert notices(ctx)[-1].status == "needs_review"


def test_a_booking_read_last_brings_no_cancelled_meeting_back(ctx):
    deliver(ctx, notice("moved", B, old=A, dated=T.format(5)), notice("cancelled", B, dated=T.format(10)), notice("booked", A, dated=T.format(0)))
    assert live(ctx) == [], "no phantom"
    assert len(meetings(ctx)) == 1
    assert lead_of(ctx).outreach_status != "meeting"
    assert notices(ctx)[-1].status == "needs_review" and waits(ctx)
    with ctx.db.session() as s:
        assert not any("prepare" in (o.next_step or "") for o in s.execute(select(Opportunity)).scalars())


def test_an_old_cancellation_read_late_leaves_the_rebooking_alone(ctx):
    deliver(ctx, notice("booked", A, dated=T.format(0)), notice("cancelled", A, dated=T.format(5)), notice("booked", A, dated=T.format(10)))
    assert live(ctx) == ["08 15:00"]
    deliver(ctx, notice("cancelled", A, dated=T.format(5)))
    assert live(ctx) == ["08 15:00"]
    assert notices(ctx)[-1].status == "needs_review"


def test_in_order_nothing_changes(ctx):
    deliver(ctx, notice("booked", A, dated=T.format(0)), notice("moved", B, old=A, dated=T.format(5)), notice("cancelled", A, dated=T.format(5)),
            notice("cancelled", B, dated=T.format(10)), notice("booked", C, dated=T.format(15)))
    assert live(ctx) == ["10 17:00"]
    assert not waits(ctx)


def test_another_event_type_is_not_compared(ctx):
    other = notice("booked", B, dated=T.format(9)).replace(b"30 Minute Meeting", b"Intro Call")
    deliver(ctx, other, notice("booked", A, dated=T.format(0)))
    assert sorted(live(ctx)) == ["08 15:00", "09 16:00"]


def test_a_person_applying_an_out_of_date_notice_changes_nothing(make_ctx):
    c = contacted(make_ctx, calendly_verified_only=False, calendly_url=LINK)[0]
    deliver(c, notice("moved", B, old=A, dated=T.format(5)), notice("cancelled", B, dated=T.format(10)), notice("booked", A, dated=T.format(0)))
    apply(c, notices(c)[-1].id)
    assert live(c) == []


# ---------------------------------------------------------------- 3. a live meeting stops the sequence
def _lost_and_booked(make_ctx):
    c, sender, sent = contacted(make_ctx, calendly_verified_only=False, calendly_url=LINK)
    U.stamp_sent(c, sent.id, NOW)
    with c.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        s.add(Opportunity(lead_id=ld.id, company_id=ld.company_id, stage="lost", probability=0, name="Deal", lost_reason="No budget"))
    deliver(c, notice("booked", A))
    return c, sender


def test_a_booking_on_a_lost_deal_stops_step_2_and_the_deal_stays_lost(make_ctx):
    c, sender = _lost_and_booked(make_ctx)
    SalesWorkflowAgent(c, sender=sender).run({"now": (NOW + timedelta(days=6)).isoformat()})
    assert [f.step for f in drafts(c) if f.status != "sent"] == [], "nothing drafted for someone with a live meeting"
    with c.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        assert has_live_meeting(s, ld) and ld.outreach_status == "contacted"
        assert s.execute(select(Opportunity)).scalars().one().stage == "lost"
        assert not s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending", FollowUp.subject.startswith("Review "))).first()


def test_once_that_meeting_is_cancelled_the_stop_ends(make_ctx):
    c, sender = _lost_and_booked(make_ctx)
    deliver(c, notice("cancelled", A))
    SalesWorkflowAgent(c, sender=sender).run({"now": (NOW + timedelta(days=6)).isoformat()})
    assert [(f.step, f.status) for f in drafts(c) if f.status != "sent"] == [(2, "awaiting_approval")]


# ---------------------------------------------------------------- 4. a booking link wherever it is hosted
OWN_LINK = "https://cal.example/owner/30min"


def test_a_draft_with_our_own_link_is_marked_and_stops_at_a_booking(make_ctx):
    c, _sender, fid = _reply_draft(make_ctx, "Sounds good, can we schedule a call?", OWN_LINK)
    with c.db.session() as s:
        assert s.get(FollowUp, fid).purpose == "booking_link"
    deliver(c, notice("booked", A))
    assert _draft_status(c, fid) == "cancelled"


def test_an_older_draft_with_our_own_link_and_no_mark_stops_too(make_ctx):
    c, _sender, fid = _reply_draft(make_ctx, "Sounds good, can we schedule a call?", OWN_LINK)
    with c.db.session() as s:
        s.get(FollowUp, fid).purpose = None
    deliver(c, notice("booked", A))
    assert _draft_status(c, fid) == "cancelled"


# ---------------------------------------------------------------- 5. the search for addresses
class Passing(Picky):
    """Says NO for a passing reason to the first `no_times` searches for addresses."""

    def __init__(self, *a, **k):
        self.no_times = 0
        super().__init__(*a, **k)

    def _answer(self, conn, line):
        tag, _, rest = line.partition(" ")
        if self.no_times and rest.upper().startswith("UID SEARCH") and "@" in rest:
            self.no_times -= 1
            self.commands.append(rest)
            conn.sendall(f"{tag} NO [UNAVAILABLE] Temporary System Error\r\n".encode())
            return False
        return super()._answer(conn, line)


@pytest.fixture
def server():
    made = []

    def make(folders):
        s = Passing({name: dict(mails) for name, mails in folders.items()})
        made.append(s)
        return s
    yield make
    for s in made:
        s.stop()


@pytest.fixture
def clock():
    before = tu.CLOCK_OFFSET
    yield before
    tu.CLOCK_OFFSET = before


def stop_mail(v, tag):
    return K.msg(PERSONAL, "Please stop writing to me", f"Please take {v.to} off.", tag=tag)


def off_alerts(c):
    with c.db.session() as s:
        return [a for a in s.execute(select(Alert).where(Alert.resolved_at.is_(None))).scalars() if "search for the addresses" in a.message]


def test_one_passing_no_switches_nothing_off_and_the_mail_is_still_found(make_ctx, server):
    c = make_ctx(**MAIL)
    _, v, _ = we_wrote(c)
    srv = server({"INBOX": {1: stop_mail(v, "t1")}})
    srv.no_times = 1
    assert read(c, srv).ok, "the read is tried again at once"
    assert not state_of(c).get("no_address_search") and not off_alerts(c)
    assert len(stored(c)) == 1 and skipped(c) == [], "the opt-out is read, not passed"


def test_a_repeated_no_switches_it_off_with_an_alert_and_it_is_asked_again_after_a_while(make_ctx, server, clock):
    c = make_ctx(**MAIL)
    _, v, _ = we_wrote(c)
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Hello there", "Hi.", tag="t2")}})
    srv.no_times = 99
    assert not read(c, srv).ok
    assert read(c, srv).ok, "given up on: the mail still comes in"
    assert state_of(c).get("no_address_search") is True and len(off_alerts(c)) == 1
    srv.no_times = 0
    srv.folders["INBOX"][2] = K.msg(PERSONAL, "Hello again", "Hi.", tag="t3")
    srv.commands.clear()
    assert read(c, srv).ok and address_searches(srv) == [], "not asked again at once"
    tu.CLOCK_OFFSET = clock + timedelta(days=1, hours=1)
    srv.folders["INBOX"][3] = stop_mail(v, "t4")
    assert read(c, srv).ok and len(address_searches(srv)) == 1
    state = state_of(c)
    assert not state.get("no_address_search") and "address_search_off_at" not in state and "address_search_drops" not in state
    assert off_alerts(c) == [], "the alert closes when the server answers"
    assert "Please stop writing to me" not in skipped(c)


def test_a_server_that_says_it_cannot_is_switched_off_at_once_and_stays_alerted_when_asked_again(make_ctx, server, clock):
    c = make_ctx(**MAIL)
    we_wrote(c)
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Hello there", "Hi.", tag="t5")}})
    srv.on_addresses = "no"
    assert read(c, srv).ok
    first = state_of(c)["address_search_off_at"]
    assert first and len(off_alerts(c)) == 1
    tu.CLOCK_OFFSET = clock + timedelta(days=1, hours=1)
    srv.folders["INBOX"][2] = K.msg(PERSONAL, "Hello again", "Hi.", tag="t6")
    srv.commands.clear()
    assert read(c, srv).ok and len(address_searches(srv)) == 1
    assert state_of(c)["address_search_off_at"] > first and len(off_alerts(c)) == 1


def test_a_search_switched_off_for_good_by_an_earlier_version_is_asked_again(make_ctx, server):
    from aime.db import kv_set

    c = make_ctx(**MAIL)
    _, v, _ = we_wrote(c)
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Hello there", "Hi.", tag="t7")}})
    assert read(c, srv).ok
    with c.db.session() as s:
        kv_set(s, "inbox.state", {**kv_get(s, "inbox.state"), "no_address_search": True})
    srv.folders["INBOX"][2] = stop_mail(v, "t8")
    srv.commands.clear()
    assert read(c, srv).ok and len(address_searches(srv)) == 1 and not state_of(c).get("no_address_search")


# ---------------------------------------------------------------- 6. parked with nobody to write to
def _repoint(c, lead_id):
    with c.db.session() as s:
        lead = s.get(Lead, lead_id)
        old = lead.contact
        new = Contact(company_id=lead.company_id, email="other.person@brightline-logistics.example", first_name="Robin", source=old.source,
                      email_source_url=old.email_source_url, email_status=old.email_status, role_category="person", consent_status=old.consent_status)
        s.add(new)
        s.flush()
        lead.contact_id = new.id


def _age_sent(c, days):
    with c.db.session() as s:
        for f in s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalars():
            f.sent_at = NOW - timedelta(days=days)


def _hand_tasks(c):
    with c.db.session() as s:
        return [t.status for t in s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.subject.startswith(HAND_TASK))).scalars()]


def test_a_lead_parked_with_nobody_to_write_to_gets_its_task_on_the_next_run(make_ctx):
    c, sender, _ = contacted(make_ctx)
    _age_sent(c, 8)
    lead_id = lead_of(c).id
    _repoint(c, lead_id)
    with c.db.session() as s:
        pipeline_checks(s, c.settings, NOW, "test")
    assert (lead_of(c).status, lead_of(c).outreach_status) == ("nurture", "no_response")
    agent = SalesWorkflowAgent(c, sender=sender)
    agent.run({"now": (NOW + timedelta(days=1)).isoformat()})
    assert _hand_tasks(c) == ["pending"]
    agent.run({"now": (NOW + timedelta(days=2)).isoformat()})
    assert _hand_tasks(c) == ["pending"], "one task"


def test_approved_step_2_then_a_new_contact_after_the_wait_leaves_a_task(make_ctx):
    c, sender, _ = contacted(make_ctx)
    agent = SalesWorkflowAgent(c, sender=sender)
    _age_sent(c, 5)
    agent.run({"now": NOW.isoformat()})
    step2 = [f for f in drafts(c) if f.step == 2]
    with c.db.session() as s:
        s.get(FollowUp, step2[0].id).status, s.get(FollowUp, step2[0].id).approved_by, s.get(FollowUp, step2[0].id).approved_at = "approved", "p", NOW
    _age_sent(c, 8)
    _repoint(c, lead_of(c).id)
    agent.run({"now": NOW.isoformat()})
    agent.run({"now": (NOW + timedelta(days=1)).isoformat()})
    assert _hand_tasks(c) == ["pending"]


def test_a_lead_a_person_parked_gets_no_hand_task_from_this(make_ctx):
    c, sender, _ = contacted(make_ctx)
    _age_sent(c, 8)
    lead_id = lead_of(c).id
    _repoint(c, lead_id)
    with c.db.session() as s:
        s.get(Lead, lead_id).status = "nurture"
        pipeline_checks(s, c.settings, NOW, "test")
    SalesWorkflowAgent(c, sender=sender).run({"now": (NOW + timedelta(days=1)).isoformat()})
    assert _hand_tasks(c) == []


# ---------------------------------------------------------------- 7. what the new-post task says, and Q4(b)
def _post_body(c, lead_id):
    with c.db.session() as s:
        return s.execute(select(FollowUp.body).where(FollowUp.lead_id == lead_id, FollowUp.subject.startswith(NEW_POST_TASK))).scalar_one()


def _parked(make_ctx, clock):
    c = make_ctx(handler=site, polite_delay=0)
    lead_id = qualified_lead(c, post_age=5, activity_age=1)
    person_parks(c, lead_id)
    tu.CLOCK_OFFSET = clock + timedelta(hours=1)
    return c, lead_id


def _signal(c, lead_id, kind, text):
    with c.db.session() as s:
        add_signal(s, s.get(Lead, lead_id).company, kind, text, kind, f"{kind}:1", tu.utcnow())


def test_a_filing_beside_a_new_post_does_not_undo_a_persons_park(make_ctx, clock):
    c, lead_id = _parked(make_ctx, clock)
    new_post(c, lead_id)
    _signal(c, lead_id, "filing", "Annual report filed.")
    LeadQualificationAgent(c).run({"rescore_all": True})
    assert status_of(c, lead_id) == "nurture"
    assert "It stays in nurture" in _post_body(c, lead_id)


def test_a_filing_alone_does_not_undo_a_persons_park(make_ctx, clock):
    c, lead_id = _parked(make_ctx, clock)
    _signal(c, lead_id, "filing", "Annual report filed.")
    LeadQualificationAgent(c).run({"rescore_all": True})
    assert status_of(c, lead_id) == "nurture"
    assert tasks(c, lead_id, NEW_POST_TASK) == []


def test_when_the_company_also_wrote_the_task_says_the_lead_came_back(make_ctx, clock):
    c, lead_id = _parked(make_ctx, clock)
    new_post(c, lead_id)
    _signal(c, lead_id, "inbound", "Can you help us with our back office?")
    LeadQualificationAgent(c).run({"rescore_all": True})
    assert status_of(c, lead_id) == "qualified"
    body = _post_body(c, lead_id)
    assert "back in qualified" in body and "stays in nurture" not in body


def test_a_task_already_open_is_corrected_when_the_company_then_writes(make_ctx, clock):
    c, lead_id = _parked(make_ctx, clock)
    new_post(c, lead_id)
    LeadQualificationAgent(c).run({"rescore_all": True})
    assert "stays in nurture" in _post_body(c, lead_id)
    tu.CLOCK_OFFSET = clock + timedelta(hours=2)
    _signal(c, lead_id, "inbound", "Can you help us with our back office?")
    LeadQualificationAgent(c).run({"rescore_all": True})
    assert status_of(c, lead_id) == "qualified"
    assert "back in qualified" in _post_body(c, lead_id)


# ---------------------------------------------------------------- 8. what a lift says
def test_a_lift_says_what_follows_and_the_draft_still_waits_for_a_person(make_ctx):
    c, sender, _ = contacted(make_ctx)
    with c.db.session() as s:
        suppress(s, DAVID, "asked by hand")
    with c.db.session() as s:
        assert lift_suppression(s, DAVID, "owner", "entered by mistake") is None
    with c.db.session() as s:
        said = s.execute(select(Activity.subject).where(Activity.subject.startswith("Do-not-contact entry lifted"))).scalars().one()
    assert "No email follows" not in said and "a person still approves" in said and "a closed lead stays closed" in said
    _age_sent(c, 8)
    SalesWorkflowAgent(c, sender=sender).run({"now": NOW.isoformat()})
    assert all(f.status in ("sent", "awaiting_approval") for f in drafts(c))


