"""Phase 6, the Sales Manager handover (aime.handover_package, aime.handover_notify, aime.assignment.handover_sla): a package
in which every line cites the record it came from and nothing uncited is written; budget and timeline in the prospect's own
words, never invented; each handover rule triggers alone and stays quiet otherwise; one version per change of content; the
internal email off by default, held by each gate alone, sent once when authorized; accept pauses AI drafting and return puts
the conversation back; a package nobody accepts is escalated once; slots in the prospect's zone across countries; double
bookings refused; who may accept, return and see the list. All records are synthetic; mail and calendar are fakes."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aime import automation as am
from aime import booking, settings_store
from aime import handover_notify as hn
from aime import handover_package as hp
from aime.ai.state import check_transition
from aime.assignment import handover_sla
from aime.cli import cmd_user
from aime.handover import handover
from aime.models import (AIDecision, Alert, Assignment, AuditEvent, Company, Contact, Conversation, ConversationFact,
                         FollowUp, HandoverPackage, InboundEmail, Lead, Meeting, Opportunity, ResearchSnapshot, Suppression)
from aime.pipeline import pipeline_checks
from aime.web.app import create_app
from tests.test_dashboard_auth import GOOD, Args, _login
from tests.test_manager_report import FakeSMTP

ORIGIN = {"origin": "http://testserver"}
OURS = "digitalhub.example"
MANAGER_ADDR = f"manager@{OURS}"
EMAIL_READY = dict(outbound_email_enabled=True, smtp_host="smtp.example", smtp_from=f"sales@{OURS}",
                   company_postal_address="1 Sample St, Austin, TX 78701")
NOW = datetime(2026, 11, 2, 9, 0)
REPLY = ("Thanks for the note. We need a support chatbot for our patient portal, in English and Spanish.\n"
         "Our budget is around $40k for the first phase, and we would like to go live before the end of Q1 2027.\n"
         "Our CTO Priya Rao signs off on vendors. Honestly we tried a bot before and it was a mess.")


@pytest.fixture(autouse=True)
def _fresh_smtp():
    FakeSMTP.sent, FakeSMTP.fail_connect, FakeSMTP.crash = [], False, False
    yield


def seed(ctx, *, country="US", state="TX", text=REPLY, intent=None, category="interested", owner="manager", facts=True,
         snapshot=True, score=0) -> dict:
    """A lead with an interested reply, the facts the conversation engine took from it and a deal owned by `owner`."""
    with ctx.db.session() as s:
        co = Company(name="Carewell Clinics", normalized_name="carewell clinics", domain="carewell.example",
                     website="https://carewell.example", industry="healthcare", state=state, city="Austin", country=country,
                     employee_range="51-200", source="test")
        s.add(co)
        s.flush()
        c = Contact(company_id=co.id, first_name="Dana", last_name="Lopez", title="Head of Digital",
                    email="dana@carewell.example", email_status="published", source="website")
        s.add(c)
        s.flush()
        ld = Lead(company_id=co.id, contact_id=c.id, source="research", status="qualified", outreach_status="interested",
                  score=score, owner=owner)
        s.add(ld)
        s.flush()
        row = InboundEmail(message_id=f"<r{ld.id}@carewell.example>", from_email=c.email, subject="Re: chatbots",
                           new_text=text, body_text=text, category=category, intent=intent, lead_id=ld.id,
                           company_id=co.id, contact_id=c.id, received_at=NOW - timedelta(hours=2), status="actioned")
        s.add(row)
        opp = Opportunity(lead_id=ld.id, company_id=co.id, name="Carewell Clinics: AI chatbot", stage="discovery",
                          owner=owner, next_step="Book a meeting")
        s.add(opp)
        s.flush()
        if snapshot:
            s.add(ResearchSnapshot(company_id=co.id, status="done", description="Clinics across Central Texas.",
                                   description_url="https://carewell.example/about", services_offered=["Primary care"],
                                   needs=[{"service": "ai_chatbot", "quote": "book appointments online", "url": "https://carewell.example"}]))
        if facts:
            for kind, value, quote in (
                    ("requirement", "Support chatbot for the patient portal", "support chatbot for our patient portal"),
                    ("budget", "USD 40,000", "around $40k for the first phase"),
                    ("timeline", "Q1 2027", "go live before the end of Q1 2027"),
                    ("stakeholder", "Priya Rao, CTO, approves vendors", "Our CTO Priya Rao signs off on vendors"),
                    ("objection", "A previous bot failed", "we tried a bot before and it was a mess")):
                s.add(ConversationFact(lead_id=ld.id, kind=kind, value=value, evidence_inbound_id=row.id,
                                       evidence_quote=quote, confidence=0.9, source="ai"))
        return {"company": co.id, "contact": c.id, "lead": ld.id, "email": row.id, "opp": opp.id}


def package(ctx, opp_id):
    with ctx.db.session() as s:
        return hp.latest(s, opp_id)


def hand_over(ctx, opp_id, reason="meeting booked", actor="test"):
    with ctx.db.session() as s:
        handover(s, ctx.settings, s.get(Opportunity, opp_id), reason, actor)
    return package(ctx, opp_id)


def all_lines(pkg):
    return [ln for key, sec in pkg.sections.items() if key != "unknown" for ln in sec["lines"]]


# ------------------------------------------------------------------------------------------------- the package

def test_every_line_cites_its_record_and_nothing_uncited_is_written(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx)
    with ctx.db.session() as s:
        other = Lead(company_id=ids["company"], source="research")
        s.add(other)
        s.flush()
        stranger = InboundEmail(message_id="<x@else.example>", from_email="x@else.example", subject="x",
                                new_text="we can spend $1m", lead_id=other.id)
        s.add(stranger)
        s.flush()
        s.add_all([  # none of these can be cited, so none may appear
            ConversationFact(lead_id=ids["lead"], kind="requirement", value="Wants a mobile app", source="ai",
                             evidence_inbound_id=ids["email"], evidence_quote="we need a mobile app"),  # not in the email
            ConversationFact(lead_id=ids["lead"], kind="requirement", value="Wants SAP integration", source="ai"),  # no email
            ConversationFact(lead_id=ids["lead"], kind="budget", value="USD 1,000,000", source="ai",
                             evidence_inbound_id=stranger.id, evidence_quote="we can spend $1m"),  # another lead's email
            ConversationFact(lead_id=ids["lead"], kind="timeline", value="next week", source="ai",
                             evidence_inbound_id=ids["email"]),  # a timeline without their words
            ConversationFact(lead_id=ids["lead"], kind="stakeholder", value="The CEO decides", source="ai",
                             evidence_inbound_id=ids["email"], status="withdrawn"),
            ConversationFact(lead_id=ids["lead"], kind="requirement", value="Spanish too", source="human", status="corrected"),
        ])
    pkg = hand_over(ctx, ids["opp"])
    assert pkg is not None and pkg.version == 1
    lines = all_lines(pkg)
    assert lines and all(hp.cited(ln["source"]) for ln in lines), [ln for ln in lines if not hp.cited(ln["source"])]
    text = pkg.summary
    for absent in ("mobile app", "SAP", "1,000,000", "$1m", "next week", "CEO decides", "Spanish too"):
        assert absent not in text, absent
    # every summary line under a section carries its citation
    body = [ln for ln in text.splitlines() if ln.startswith("- ")]
    known = {f"- {u}" for u in pkg.sections["unknown"]}
    assert all(ln.endswith("]") and "[" in ln for ln in body if ln not in known)
    # what each cited line names exists and belongs to this deal
    with ctx.db.session() as s:
        for ln in lines:
            src = ln["source"]
            if src["type"] == "inbound_email":
                assert s.get(InboundEmail, src["id"]).lead_id == ids["lead"]
                if src.get("quote"):
                    assert hp.quoted_in(src["quote"], s.get(InboundEmail, src["id"]))
            elif src["type"] == "research_snapshot":
                assert s.get(ResearchSnapshot, src["id"]).company_id == ids["company"]
    keys = set(pkg.sections)
    assert {"why", "prospect", "needs", "budget_timeline", "stakeholders", "objections", "risks", "next_step", "links"} <= keys
    assert "Carewell Clinics" in text and "Head of Digital" in text and "Clinics across Central Texas" in text
    assert "Priya Rao" in text and "tried a bot before" in text and "/leads/" in text and "/companies/" in text


def test_budget_and_timeline_are_their_own_words_and_never_invented(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx)
    pkg = hand_over(ctx, ids["opp"])
    bt = [ln["text"] for ln in pkg.sections["budget_timeline"]["lines"]]
    assert bt == ["Budget, in their words: “around $40k for the first phase”",
                  "Timeline, in their words: “go live before the end of Q1 2027”"]
    assert "40,000" not in pkg.summary  # the AI's normalised value is not shown as if they said it
    # nothing stated: nothing shown, and the package says it is not known
    ctx2 = make_ctx()
    ids2 = seed(ctx2, facts=False, intent="pricing_request", text="How much would it cost?")
    pkg2 = hand_over(ctx2, ids2["opp"])
    assert "budget_timeline" not in pkg2.sections
    assert "Budget: not stated in their emails yet" in pkg2.sections["unknown"]
    assert "Timeline: not stated in their emails yet" in pkg2.sections["unknown"]
    assert "$" not in pkg2.summary.split("Not known yet")[0].replace("$40k", "")


def test_a_person_s_recorded_budget_is_cited_as_a_crm_field(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx, facts=False, intent="pricing_request")
    with ctx.db.session() as s:
        f = ConversationFact(lead_id=ids["lead"], kind="budget", value="USD 25k per year (said on the phone)", source="human",
                             status="confirmed", confirmed_by="manager")
        s.add(f)
        s.flush()
        fid = f.id
    pkg = hand_over(ctx, ids["opp"])
    line = pkg.sections["budget_timeline"]["lines"][0]
    assert line["source"] == {"type": "crm", "table": "conversation_facts", "id": fid, "field": "value"}
    assert "recorded by manager" in line["text"]


# ------------------------------------------------------------------------------------------------- the conditions

def _meeting(s, ids, **kw):
    m = Meeting(lead_id=ids["lead"], company_id=ids["company"], opportunity_id=ids["opp"], invitee_email="dana@carewell.example",
                title="Intro call", starts_at=NOW + timedelta(days=2), ends_at=NOW + timedelta(days=2, minutes=30),
                timezone="America/Chicago", source="calendly_email", status="scheduled", **kw)
    s.add(m)
    s.flush()
    return m


CONDITIONS = {
    "interested_requirements_complete": dict(seed={}, off={"facts": False}),
    "meeting_booked": dict(seed={"facts": False}, extra=_meeting),
    "pricing_asked": dict(seed={"facts": False, "intent": "pricing_request", "text": "What would this cost us?"}),
    "human_requested": dict(seed={"facts": False, "text": "Could I speak to a real person on your sales team please?"}),
    "score_with_budget": dict(seed={"score": 80}, rules={"min_score": 70, "interested_requirements_complete": False}),
    "vip_account": dict(seed={"facts": False}, rules={"vip_accounts": ["carewell.example"]}),
    "conversation_escalation": dict(seed={"facts": False},
                                    extra=lambda s, ids: hp.request_handover(s, ids["lead"], "they asked about a contract")),
}


@pytest.mark.parametrize("key", list(CONDITIONS))
def test_each_handover_rule_triggers_alone_and_stays_quiet_when_off(make_ctx, key):
    spec = CONDITIONS[key]
    ctx = make_ctx()
    rules = {**settings_store.get(ctx.settings, hp.SECTION), **spec.get("rules", {})}
    with ctx.db.session() as s:
        settings_store.save(s, ctx.settings, hp.SECTION, rules, "owner")
    ids = seed(ctx, **spec["seed"])
    with ctx.db.session() as s:
        if spec.get("extra"):
            spec["extra"](s, ids)
        found = hp.evaluate(s, ctx.settings, s.get(Opportunity, ids["opp"]))
        assert [c["key"] for c in found] == [key], found
        assert all(hp.cited(c["source"]) for c in found)
    # the rule switched off: nothing triggers, and a handover writes no package
    off = {**rules, **({key: False} if isinstance(rules.get(key), bool) else {})}
    if key == "score_with_budget":
        off["min_score"] = 0
    if key == "vip_account":
        off["vip_accounts"] = []
    with ctx.db.session() as s:
        settings_store.save(s, ctx.settings, hp.SECTION, off, "owner")
        assert hp.evaluate(s, ctx.settings, s.get(Opportunity, ids["opp"])) == []
    assert hand_over(ctx, ids["opp"]) is None


def test_no_rule_no_package_and_the_alert_stays_as_it_was(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx, facts=False, text="Sounds interesting, tell me more.")
    assert hand_over(ctx, ids["opp"], "interested reply from dana@carewell.example") is None
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.source == "handover")).scalar_one().message.startswith("Handed over to manager")
        assert s.execute(select(HandoverPackage)).first() is None
    # the rules off altogether: not even a meeting writes one
    with ctx.db.session() as s:
        settings_store.save(s, ctx.settings, hp.SECTION, {**settings_store.get(ctx.settings, hp.SECTION), "enabled": False}, "owner")
        _meeting(s, ids)
    assert hand_over(ctx, ids["opp"]) is None


def test_a_closed_or_simulated_meeting_does_not_count(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx, facts=False)
    with ctx.db.session() as s:
        _meeting(s, ids, is_simulated=True)
        assert hp.evaluate(s, ctx.settings, s.get(Opportunity, ids["opp"])) == []
        _meeting(s, ids)
        s.get(Opportunity, ids["opp"]).stage = "lost"
        assert hp.evaluate(s, ctx.settings, s.get(Opportunity, ids["opp"])) == []


def test_the_engines_handover_proposal_counts_unless_a_person_rejected_it(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx, facts=False)
    with ctx.db.session() as s:
        d = AIDecision(agent="conversation", task="reply", provider="template", lead_id=ids["lead"], kind="handover",
                       verdict="human_review", proposal={"kind": "handover", "reason": "legal question"})
        s.add(d)
        s.flush()
        found = hp.evaluate(s, ctx.settings, s.get(Opportunity, ids["opp"]))
        assert [c["key"] for c in found] == ["conversation_escalation"] and "legal question" in found[0]["why"]
        d.human_action = "rejected"
        assert hp.evaluate(s, ctx.settings, s.get(Opportunity, ids["opp"])) == []


# ------------------------------------------------------------------------------------------------- versions

def test_a_package_version_is_written_only_when_its_content_changes(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx)
    first = hand_over(ctx, ids["opp"])
    again = hand_over(ctx, ids["opp"], "interested reply again")
    assert first.id == again.id and first.version == 1
    with ctx.db.session() as s:
        assert s.execute(select(func.count(HandoverPackage.id))).scalar_one() == 1
        assert s.execute(select(func.count(AuditEvent.id)).where(AuditEvent.action == "handover_package")).scalar_one() == 1
        _meeting(s, ids)
    second = hand_over(ctx, ids["opp"])
    assert second.version == 2 and "meeting" in second.sections
    with ctx.db.session() as s:
        assert s.get(HandoverPackage, first.id).status == "superseded"
        assert s.get(HandoverPackage, second.id).waiting_since == s.get(HandoverPackage, first.id).waiting_since
        assert [p.version for p in s.execute(select(HandoverPackage).order_by(HandoverPackage.version)).scalars()] == [1, 2]


def test_the_periodic_check_hands_over_the_engines_leads_once(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx)
    with ctx.db.session() as s:
        s.add(Conversation(lead_id=ids["lead"], state="qualified"))
    with ctx.db.session() as s:
        assert pipeline_checks(s, ctx.settings, NOW, "test")["handed_over"] == 1
    with ctx.db.session() as s:
        assert "handed_over" not in pipeline_checks(s, ctx.settings, NOW, "test")
        assert s.execute(select(func.count(HandoverPackage.id))).scalar_one() == 1
        assert s.execute(select(func.count(Alert.id)).where(Alert.source == "handover",
                                                            Alert.message.like("Handed over%"))).scalar_one() == 1


def test_the_periodic_check_leaves_a_lead_that_opted_out(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx)
    with ctx.db.session() as s:
        s.add(Conversation(lead_id=ids["lead"], state="qualified"))
        s.add(Suppression(value="dana@carewell.example", kind="email", reason="asked"))
    with ctx.db.session() as s:
        assert hp.periodic_check(s, ctx.settings, "test", NOW) == 0
        assert s.execute(select(HandoverPackage)).first() is None


def test_a_rescheduled_meeting_updates_the_package_in_both_time_zones(make_ctx):
    ctx = make_ctx()
    ctx.settings.business.setdefault("meetings", {})["owner_timezone"] = "Asia/Kolkata"
    ids = seed(ctx)
    with ctx.db.session() as s:
        s.add(Conversation(lead_id=ids["lead"], state="scheduling"))
        m = _meeting(s, ids)
        m.starts_at, m.ends_at = datetime(2026, 11, 5, 15, 0), datetime(2026, 11, 5, 15, 30)
        mid = m.id
    pkg = hand_over(ctx, ids["opp"])
    line = pkg.sections["meeting"]["lines"][0]["text"]
    assert "9:00 AM CST for the prospect (America/Chicago)" in line and "8:30 PM IST for you (Asia/Kolkata)" in line
    with ctx.db.session() as s:  # Calendly moved it: the next check writes the new time
        m = s.get(Meeting, mid)
        m.rescheduled_from, m.starts_at, m.ends_at = m.starts_at, datetime(2026, 11, 6, 16, 0), datetime(2026, 11, 6, 16, 30)
    with ctx.db.session() as s:
        assert hp.periodic_check(s, ctx.settings, "test", NOW) == 1
    moved = package(ctx, ids["opp"])
    assert moved.version == 2 and "10:00 AM CST" in moved.sections["meeting"]["lines"][0]["text"]
    with ctx.db.session() as s:  # cancelled: the meeting is no longer a reason, the package says none is booked
        s.get(Meeting, mid).status = "cancelled"
    with ctx.db.session() as s:
        hp.periodic_check(s, ctx.settings, "test", NOW)
    last = package(ctx, ids["opp"])
    assert last.version == 3 and "meeting" not in last.sections and "Meeting: none booked yet" in last.sections["unknown"]
    assert "meeting_booked" not in {c["key"] for c in last.conditions}


# ------------------------------------------------------------------------------------------------- accept and return

def test_accept_pauses_ai_drafting_and_return_puts_the_conversation_back(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx)
    with ctx.db.session() as s:
        s.add(Conversation(lead_id=ids["lead"], state="qualified"))
    pkg = hand_over(ctx, ids["opp"])
    with ctx.db.session() as s:
        assert not hp.ai_paused(s, ids["lead"])
        note = hp.accept(s, ctx.settings, s.get(HandoverPackage, pkg.id), "manager")
        assert "paused" in note and hp.ai_paused(s, ids["lead"])
        conv = s.execute(select(Conversation).where(Conversation.lead_id == ids["lead"])).scalar_one()
        assert conv.state == "handed_over"
        # the AI can never move it out again; only a person can
        assert check_transition(conv.state, "discovering", actor="ai")[0] is False
        assert hp.accept(s, ctx.settings, s.get(HandoverPackage, pkg.id), "manager").startswith("Already accepted")
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "handover_accepted")).scalar_one()
    with ctx.db.session() as s:
        with pytest.raises(ValueError, match="say why"):
            hp.return_to_ai(s, ctx.settings, s.get(HandoverPackage, pkg.id), "manager", " ")
        hp.return_to_ai(s, ctx.settings, s.get(HandoverPackage, pkg.id), "manager", "Not ready: they need a budget first")
        conv = s.execute(select(Conversation).where(Conversation.lead_id == ids["lead"])).scalar_one()
        assert conv.state == "qualified" and not hp.ai_paused(s, ids["lead"])
        back = s.get(HandoverPackage, pkg.id)
        assert (back.status, back.returned_by, back.return_reason) == ("returned", "manager", "Not ready: they need a budget first")
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "handover_returned")).scalar_one()


def test_accept_creates_the_conversation_when_the_engine_has_not_yet(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx)
    pkg = hand_over(ctx, ids["opp"])
    with ctx.db.session() as s:
        hp.accept(s, ctx.settings, s.get(HandoverPackage, pkg.id), "manager")
        assert hp.ai_paused(s, ids["lead"])
    with ctx.db.session() as s:  # an update after acceptance stays the manager's: nothing to accept again
        _meeting(s, ids)
    newer = hand_over(ctx, ids["opp"])
    assert newer.version == 2 and newer.status == "accepted" and newer.accepted_by == "manager"
    with ctx.db.session() as s:
        with pytest.raises(ValueError, match="newer version"):
            hp.return_to_ai(s, ctx.settings, s.get(HandoverPackage, pkg.id), "manager", "old tab")


def test_an_opted_out_conversation_cannot_be_handed_over(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx)
    pkg = hand_over(ctx, ids["opp"])
    with ctx.db.session() as s:
        s.add(Conversation(lead_id=ids["lead"], state="opted_out"))
    with ctx.db.session() as s:
        with pytest.raises(ValueError, match="opted"):
            hp.accept(s, ctx.settings, s.get(HandoverPackage, pkg.id), "manager")


# ------------------------------------------------------------------------------------------------- the SLA

def test_a_handover_nobody_accepts_in_time_is_escalated_once(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx)
    with ctx.db.session() as s:
        s.add(Opportunity(company_id=ids["company"], name="Unrelated", owner="manager"))
    pkg = hand_over(ctx, ids["opp"])
    with ctx.db.session() as s:
        assert handover_sla(s, ctx.settings, pkg.waiting_since + timedelta(hours=23), "test") == 0
        assert handover_sla(s, ctx.settings, pkg.waiting_since + timedelta(hours=25), "test") == 1
        assert handover_sla(s, ctx.settings, pkg.waiting_since + timedelta(hours=50), "test") == 0  # once
        alert = s.execute(select(Alert).where(Alert.message.like("Escalated to%"))).scalar_one()
        assert "not accepted by manager in 24 h" in alert.message and "the administrators" in alert.message
        esc = s.execute(select(Assignment).where(Assignment.method == "escalation")).scalar_one()
        assert esc.opportunity_id == ids["opp"] and esc.to_owner == "manager"  # the deal stays with its owner
        assert s.get(HandoverPackage, pkg.id).sla_escalated_at is not None


def test_an_accepted_handover_or_an_sla_of_zero_is_never_escalated(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx)
    pkg = hand_over(ctx, ids["opp"])
    with ctx.db.session() as s:
        settings_store.save(s, ctx.settings, hp.SECTION, {**settings_store.get(ctx.settings, hp.SECTION),
                                                          "accept_within_hours": 0}, "owner")
        assert handover_sla(s, ctx.settings, pkg.waiting_since + timedelta(days=9), "test") == 0
        settings_store.save(s, ctx.settings, hp.SECTION, {**settings_store.get(ctx.settings, hp.SECTION),
                                                          "accept_within_hours": 4}, "owner")
        hp.accept(s, ctx.settings, s.get(HandoverPackage, pkg.id), "manager")
        assert handover_sla(s, ctx.settings, pkg.waiting_since + timedelta(days=9), "test") == 0


# ------------------------------------------------------------------------------------------------- the email

def authorized(make_ctx, **overrides):
    ctx = make_ctx(**{**EMAIL_READY, **overrides})
    with ctx.db.session() as s:
        hn.save(s, ctx.settings, "owner", {"addresses": f"manager = {MANAGER_ADDR}", "email_enabled": True})
        hn.authorize(s, ctx.settings, "owner")
    return ctx


def test_off_by_default_nothing_is_looked_at_or_sent(make_ctx):
    ctx = make_ctx(**EMAIL_READY)
    ids = seed(ctx)
    hand_over(ctx, ids["opp"])
    assert hn.run(ctx, NOW, smtp_factory=FakeSMTP) == {"status": "off", "results": []}
    with ctx.db.session() as s:
        pkg = hp.latest(s, ids["opp"])
        reasons = hn.blockers(s, ctx.settings, pkg, NOW)
        assert pkg.notify_status is None
    assert any("switched off" in r for r in reasons) and any("authorized" in r for r in reasons)
    assert any("no internal address" in r for r in reasons) and FakeSMTP.sent == []


def test_the_authorized_email_goes_once_per_version_to_the_owner_only(make_ctx):
    ctx = authorized(make_ctx)
    ids = seed(ctx)
    pkg = hand_over(ctx, ids["opp"])
    first = hn.run(ctx, NOW, smtp_factory=FakeSMTP)
    assert [r["status"] for r in first["results"]] == ["sent"]
    assert hn.run(ctx, NOW + timedelta(minutes=5), smtp_factory=FakeSMTP)["results"] == []
    assert hn.deliver(ctx, pkg.id, NOW, smtp_factory=FakeSMTP)["status"] == "already"
    assert len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert msg["To"] == MANAGER_ADDR and "Carewell Clinics" in msg["Subject"]
    body = msg.get_body(("plain",)).get_content()
    assert "around $40k for the first phase" in body and f"/opportunities/{ids['opp']}" in body and "[email #" in body
    assert "dana@carewell.example" not in msg["To"]
    with ctx.db.session() as s:
        row = s.get(HandoverPackage, pkg.id)
        assert row.notify_status == "sent" and row.notify_to == MANAGER_ADDR and row.notify_message_id
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "handover_emailed")).scalar_one()
        assert s.execute(select(func.count(FollowUp.id))).scalar_one() == 0  # internal: no outreach record
        assert hn.sends_since(s, NOW - timedelta(hours=1)) == 1
    # a new version is a new email, once
    with ctx.db.session() as s:
        _meeting(s, ids)
    hand_over(ctx, ids["opp"])
    assert [r["status"] for r in hn.run(ctx, NOW, smtp_factory=FakeSMTP)["results"]] == ["sent"]
    assert len(FakeSMTP.sent) == 2


def test_a_crash_after_the_claim_never_sends_that_version_again(make_ctx):
    ctx = authorized(make_ctx)
    ids = seed(ctx)
    pkg = hand_over(ctx, ids["opp"])
    FakeSMTP.crash = True
    with pytest.raises(KeyboardInterrupt):
        hn.run(ctx, NOW, smtp_factory=FakeSMTP)
    FakeSMTP.crash = False
    assert hn.deliver(ctx, pkg.id, NOW + timedelta(minutes=2), smtp_factory=FakeSMTP)["status"] == "already"
    assert hn.deliver(ctx, pkg.id, NOW + timedelta(minutes=30), smtp_factory=FakeSMTP)["status"] == "already"
    with ctx.db.session() as s:
        assert s.get(HandoverPackage, pkg.id).notify_status == "uncertain"
    assert FakeSMTP.sent == []


def test_a_send_that_certainly_left_nothing_is_tried_again(make_ctx):
    ctx = authorized(make_ctx)
    ids = seed(ctx)
    hand_over(ctx, ids["opp"])
    FakeSMTP.fail_connect = True
    assert [r["status"] for r in hn.run(ctx, NOW, smtp_factory=FakeSMTP)["results"]] == ["failed"]
    FakeSMTP.fail_connect = False
    assert [r["status"] for r in hn.run(ctx, NOW + timedelta(minutes=30), smtp_factory=FakeSMTP)["results"]] == ["sent"]
    assert hn.run(ctx, NOW + timedelta(hours=1), smtp_factory=FakeSMTP)["results"] == [] and len(FakeSMTP.sent) == 1


def _close(gate, ctx, ids):
    with ctx.db.session() as s:
        if gate == "enabled":
            hn.save(s, ctx.settings, "owner", {"email_enabled": False})
        elif gate == "address":
            s.get(Opportunity, ids["opp"]).owner = "someone_else"
        elif gate == "owner":
            s.get(Opportunity, ids["opp"]).owner = None
        elif gate == "authorization":
            hn.revoke(s, "owner")
        elif gate == "outbound":
            ctx.settings.outbound_email_enabled = False
        elif gate == "mailbox":
            ctx.settings.smtp_host = ""
        elif gate == "emergency_stop":
            am.change_state(s, "emergency_stop", "owner")
        elif gate == "prospect":
            s.add(Contact(company_id=ids["company"], email=MANAGER_ADDR, source="website"))
        elif gate == "suppressed":
            s.add(Suppression(value=MANAGER_ADDR, kind="email", reason="asked"))
        elif gate == "outside":
            ctx.settings.smtp_from = "sales@another.example"
        elif gate == "deliverability_pause":  # the mailbox's reputation is shared with outreach
            from aime import deliverability
            deliverability.pause(s, ctx.settings, "a bounce storm", "overall", {}, now=NOW)
        elif gate == "mailbox_limit":
            ctx.settings.business.setdefault("outreach", {})["mailbox_daily_limit"] = 1
            s.add(FollowUp(lead_id=ids["lead"], company_id=ids["company"], channel="email", kind="outreach", step=1,
                           subject="Hello", body="b", status="sent", sent_at=NOW - timedelta(hours=1), due_at=NOW,
                           to_email="dana@carewell.example"))


GATES = {"enabled": None, "address": "no internal address", "owner": "no owner", "authorization": "authorized",
         "outbound": "OUTBOUND_EMAIL_ENABLED", "mailbox": "sending mailbox", "emergency_stop": "emergency stop",
         "prospect": "prospect", "suppressed": "do-not-contact", "outside": "not an internal address",
         "mailbox_limit": "daily limit", "deliverability_pause": "paused for deliverability"}


@pytest.mark.parametrize("gate", list(GATES))
def test_each_gate_alone_keeps_the_email_from_going(make_ctx, gate):
    ctx = authorized(make_ctx)
    ids = seed(ctx)
    pkg = hand_over(ctx, ids["opp"])
    _close(gate, ctx, ids)
    res = hn.run(ctx, NOW, smtp_factory=FakeSMTP)
    if gate == "enabled":
        assert res["status"] == "off"
    else:
        assert [r["status"] for r in res["results"]] == ["not_sent"] and GATES[gate] in res["results"][0]["reason"], res
    assert FakeSMTP.sent == []
    with ctx.db.session() as s:
        assert s.get(HandoverPackage, pkg.id).notify_status is None


def test_the_open_gates_send(make_ctx):
    """The baseline the gate tests close one at a time."""
    ctx = authorized(make_ctx)
    ids = seed(ctx)
    hand_over(ctx, ids["opp"])
    assert [r["status"] for r in hn.run(ctx, NOW, smtp_factory=FakeSMTP)["results"]] == ["sent"]


def test_a_prospect_or_outside_address_cannot_be_saved_or_authorized(make_ctx):
    ctx = make_ctx(**EMAIL_READY)
    ids = seed(ctx)
    with ctx.db.session() as s:
        for bad, why in (("dana@carewell.example", "contact in the CRM"), ("boss@" + "gmail.com", "not an internal address"),
                         ("not-an-address", "not an email address")):
            with pytest.raises(ValueError, match=why):
                # the prospect's domain allow-listed by mistake: still refused, being a contact in the CRM
                hn.save(s, ctx.settings, "owner", {"addresses": f"manager = {bad}", "allowlist": "carewell.example"})
        with pytest.raises(ValueError, match="at least one address"):
            hn.authorize(s, ctx.settings, "owner")
        hn.save(s, ctx.settings, "owner", {"addresses": f"manager = {MANAGER_ADDR}", "email_enabled": True})
        hn.authorize(s, ctx.settings, "owner")
        assert hn.authorization(hn.config(s))[0]
        hn.save(s, ctx.settings, "owner", {"addresses": f"manager = {MANAGER_ADDR}\nana = ana@{OURS}"})
        ok, why = hn.authorization(hn.config(s))  # changing the addresses withdraws it, on record
        assert not ok and "no Super Admin has authorized" in why
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "handover_notify_authorization_withdrawn")).scalar_one()
        # an authorization edited behind the page's back (the addresses changed under it) does not apply either
        hn.authorize(s, ctx.settings, "owner")
        stored = dict(hn.kv_get(s, hn.KV_KEY))
        hn.kv_set(s, hn.KV_KEY, {**stored, "addresses": {"manager": f"other@{OURS}"}})
        ok, why = hn.authorization(hn.config(s))
        assert not ok and "changed after the authorization" in why
    assert ids  # the prospect company is in the CRM


def test_the_report_and_the_handover_emails_share_the_mailbox_limit(make_ctx):
    from aime import manager_report as mr

    ctx = authorized(make_ctx)
    ids = seed(ctx)
    hand_over(ctx, ids["opp"])
    hn.run(ctx, NOW, smtp_factory=FakeSMTP)
    with ctx.db.session() as s:
        assert mr.mailbox_sends_since(s, NOW - timedelta(hours=1)) == 1


# ------------------------------------------------------------------------------------------------- time zones and booking

def _owner(ctx, tz, hours):
    m = ctx.settings.business.setdefault("meetings", {})
    m["owner_timezone"], m["owner_hours"] = tz, hours


def test_slots_are_offered_in_the_prospects_zone_across_india_and_the_us(make_ctx):
    from zoneinfo import ZoneInfo

    tuesday = datetime(2026, 10, 27, 14, 0)
    ctx = make_ctx()
    _owner(ctx, "Asia/Kolkata", "08:00-22:00")  # a manager in India, a prospect in New York
    slots = booking.free_slots([], tuesday, "America/New_York", ctx.settings)
    assert len(slots) == 3
    for at in slots:
        ny = booking.as_aware_utc(at).astimezone(ZoneInfo("America/New_York"))
        ist = booking.as_aware_utc(at).astimezone(ZoneInfo("Asia/Kolkata"))
        assert 9 <= ny.hour < 17 and (8, 0) <= (ist.hour, ist.minute) <= (21, 30) and ny.weekday() < 5
    assert "EDT" in booking.label(slots[0], "America/New_York") or "EST" in booking.label(slots[0], "America/New_York")
    _owner(ctx, "Asia/Kolkata", "08:00-22:00")  # a prospect in India: their morning
    india = booking.free_slots([], tuesday, "Asia/Kolkata", ctx.settings)
    assert india and all(booking.as_aware_utc(a).astimezone(ZoneInfo("Asia/Kolkata")).hour >= 9 for a in india)
    assert "IST" in booking.label(india[0], "Asia/Kolkata")
    _owner(ctx, "America/Los_Angeles", "08:00-18:00")  # no overlap with Indian business hours: a person proposes times
    assert booking.free_slots([], tuesday, "Asia/Kolkata", ctx.settings) == []


def test_the_prospects_zone_follows_the_company_country_and_the_saved_registry(make_ctx):
    ctx = make_ctx()
    ids = seed(ctx, country="IN", state=None)
    with ctx.db.session() as s:
        lead = s.get(Lead, ids["lead"])
        assert booking.prospect_zone(lead, settings=ctx.settings) == "Asia/Kolkata"
        lead.company.country = "GB"
        assert booking.prospect_zone(lead, settings=ctx.settings) == "Europe/London"
        lead.company.country, lead.company.state = "US", "CA"
        assert booking.prospect_zone(lead, settings=ctx.settings) == "America/Los_Angeles"
        lead.company.country = "ZZ"
        assert booking.prospect_zone(lead, settings=ctx.settings) == "America/New_York"
        # a registry a person saved decides for the countries it lists
        data = settings_store.get(ctx.settings, "countries")
        data["countries"].append({**data["countries"][0], "code": "AU", "enabled": False, "name": "Australia",
                                  "timezone": "Australia/Perth", "holidays": [], "compliance_regime": "not reviewed"})
        settings_store.save(s, ctx.settings, "countries", data, "owner")
        lead.company.country = "AU"
        assert booking.prospect_zone(lead, settings=ctx.settings) == "Australia/Perth"
        assert booking.prospect_zone(lead) == "Australia/Sydney"  # without the settings: the built-in zone


def test_a_time_the_crm_already_has_a_meeting_at_is_not_booked_twice(make_ctx):
    from tests.test_a4_meetings import booking_ctx, meeting_request, proposal

    ctx, _sender, sent, cal = booking_ctx(make_ctx)
    meeting_request(ctx, sent)
    p = proposal(ctx)
    start = datetime.fromisoformat(p.slots[0])
    with ctx.db.session() as s:  # a Calendly booking with someone else, which the connected calendar does not show
        s.add(Meeting(invitee_email="other@else.example", title="Other call", starts_at=start - timedelta(minutes=15),
                      ends_at=start + timedelta(minutes=15), source="calendly_email", status="scheduled"))
    with ctx.db.session() as s:
        with pytest.raises(ValueError, match="already booked at that time"):
            booking.book(s, ctx.settings, _calendar(ctx), s.get(type(p), p.id), 0, "manager")
    assert cal.events == {}
    with ctx.db.session() as s:
        assert s.execute(select(func.count(Meeting.id))).scalar_one() == 1
        # another slot is free: booked once, and its handover package shows both zones
        m = booking.book(s, ctx.settings, _calendar(ctx), s.get(type(p), p.id), 1, "manager")
        pkg = hp.latest(s, m.opportunity_id)
        assert pkg is not None and "meeting_booked" in {c["key"] for c in pkg.conditions}
        assert "for the prospect" in pkg.sections["meeting"]["lines"][0]["text"]
    assert len(cal.events) == 1


def _calendar(ctx):
    import httpx

    from aime.integrations.calendar import GoogleCalendar

    return GoogleCalendar(httpx.Client(transport=ctx.http_transport), lambda: "token")


# ------------------------------------------------------------------------------------------------- the pages

@pytest.fixture
def org(make_ctx):
    ctx = make_ctx(dashboard_auth=True, **EMAIL_READY)
    for name, role in (("boss", "super_admin"), ("manager", "sales_manager"), ("other", "sales_manager"),
                       ("ana", "analyst"), ("bde", "sales_executive"), ("ops", "admin"), ("director", "sales_director")):
        assert cmd_user(Args("create", name, role=role), ctx, prompt_fn=lambda _p: GOOD) == 0
    return ctx


def client_for(ctx, username):
    client = TestClient(create_app(ctx))
    assert _login(client, username=username).status_code == 303
    return client


def test_the_package_shows_on_the_deal_page_with_accept_for_its_owner_only(org):
    ids = seed(org)
    pkg = hand_over(org, ids["opp"])
    page = client_for(org, "manager").get(f"/opportunities/{ids['opp']}")
    assert page.status_code == 200 and "Handover package" in page.text and "<script" not in page.text
    assert "around $40k for the first phase" in page.text and f"[email #{ids['email']}]" in page.text
    assert "Accept: I take it over" in page.text
    for name in ("other", "bde"):  # people who cannot see the deal: as if it did not exist
        r = client_for(org, name).post(f"/opportunities/{ids['opp']}/handover/accept", data={"package_id": pkg.id},
                                       headers=ORIGIN, follow_redirects=False)
        assert r.status_code == 404, name
    director = client_for(org, "director")  # sees every deal, but neither owns this one nor administers
    assert "Accept: I take it over" not in director.get(f"/opportunities/{ids['opp']}").text
    r = director.post(f"/opportunities/{ids['opp']}/handover/accept", data={"package_id": pkg.id}, headers=ORIGIN,
                      follow_redirects=False)
    assert r.status_code == 403
    r = client_for(org, "ana").post(f"/opportunities/{ids['opp']}/handover/accept", data={"package_id": pkg.id},
                                    headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 403  # read-only
    assert package(org, ids["opp"]).status == "open"
    r = client_for(org, "manager").post(f"/opportunities/{ids['opp']}/handover/accept", data={"package_id": pkg.id},
                                        headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and package(org, ids["opp"]).accepted_by == "manager"
    r = client_for(org, "ops").post(f"/opportunities/{ids['opp']}/handover/return",
                                    data={"package_id": pkg.id, "reason": "Needs more discovery"}, headers=ORIGIN,
                                    follow_redirects=False)
    assert r.status_code == 303 and package(org, ids["opp"]).status == "returned"  # an administrator may


def test_the_handovers_list_is_for_sales_managers_and_lists_what_they_may_see(org):
    ids = seed(org)
    hand_over(org, ids["opp"])
    page = client_for(org, "manager").get("/handovers")
    assert page.status_code == 200 and "Carewell Clinics: AI chatbot" in page.text and "<script" not in page.text
    assert "Email to the Sales Manager" not in page.text  # the email's settings are the Super Admin's
    assert "Carewell" not in client_for(org, "other").get("/handovers").text  # not their team's deal
    assert client_for(org, "bde").get("/handovers").status_code == 403
    boss = client_for(org, "boss")
    assert "Email to the Sales Manager" in boss.get("/handovers").text
    for name in ("manager", "ops"):
        r = client_for(org, name).post("/handovers/notify", data={"addresses": f"manager = {MANAGER_ADDR}",
                                                                  "email_enabled": "yes"}, headers=ORIGIN, follow_redirects=False)
        assert r.status_code == 403, name
        assert client_for(org, name).post("/handovers/notify/authorize", headers=ORIGIN,
                                          follow_redirects=False).status_code == 403
    r = boss.post("/handovers/notify", data={"addresses": f"manager = {MANAGER_ADDR}", "email_enabled": "yes"},
                  headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert boss.post("/handovers/notify/authorize", headers=ORIGIN, follow_redirects=False).status_code == 303
    with org.db.session() as s:
        assert hn.authorization(hn.config(s))[0]
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "handover_notify_authorized")).scalar_one().actor == "boss"
    assert client_for(org, "boss").get("/admin/settings/section/handover_rules").status_code == 200


# ------------------------------------------------------------------------------------------------- end to end

def test_end_to_end_interested_reply_facts_verified_calendly_package_email_accept(make_ctx, monkeypatch):
    """A synthetic prospect: an interested reply, the facts taken from it, a meeting booked through a DKIM-verified Calendly
    notice (a forged one changes nothing), the cited package, the authorized email to the Sales Manager (fake SMTP), and
    the manager accepting it."""
    from tests.test_dkim import MESSAGE, RSA_KEY, dns_record, notice_in_inbox, sign
    from tests.test_inbox import DAVID, FakeIMAP, contacted, latest_reply, mail, run_inbox

    ctx, _sender, sent = contacted(make_ctx, calendly_verified_only=True)
    ctx.settings.business.setdefault("handover", {})["sales_manager"] = "manager"
    with ctx.db.session() as s:  # the owner's rule: what they need and when is enough to hand over
        settings_store.save(s, ctx.settings, hp.SECTION, {**settings_store.get(ctx.settings, hp.SECTION),
                                                          "required_facts": ["requirement", "timeline"]}, "owner")
    text = "Sounds interesting. We need help automating our support inbox, ideally live before March."
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", text, in_reply_to=sent.message_id)}
    run_inbox(ctx)
    reply = latest_reply(ctx)
    assert reply.category == "interested"
    with ctx.db.session() as s:
        opp = s.execute(select(Opportunity).where(Opportunity.lead_id == reply.lead_id)).scalar_one()
        opp_id = opp.id
        assert hp.latest(s, opp_id) is None  # interested, but nothing stated yet that completes the requirements
        for kind, value, quote in (("requirement", "Automate the support inbox", "automating our support inbox"),
                                   ("timeline", "2027-03-01", "ideally live before March")):
            s.add(ConversationFact(lead_id=reply.lead_id, kind=kind, value=value, evidence_inbound_id=reply.id,
                                   evidence_quote=quote, source="ai", confidence=0.8))
    records = {"s1._domainkey.calendly.com": dns_record(RSA_KEY)}
    forged = MESSAGE.replace(b"<n1@calendly.example>", b"<forged@calendly.example>")
    held = notice_in_inbox(ctx, 2, forged, monkeypatch, records)  # unsigned: no meeting, no package
    assert held.status == "needs_review"
    with ctx.db.session() as s:
        assert s.execute(select(Meeting)).first() is None and hp.latest(s, opp_id) is None
    notice = notice_in_inbox(ctx, 3, sign(MESSAGE, "calendly.com"), monkeypatch, records)
    assert notice.status == "actioned" and "dkim=pass header.d=calendly.com" in notice.auth_results
    with ctx.db.session() as s:
        meeting = s.execute(select(Meeting)).scalar_one()
        pkg = hp.latest(s, opp_id)
        assert pkg is not None and pkg.version == 1 and pkg.owner == "manager"
        assert {c["key"] for c in pkg.conditions} >= {"meeting_booked", "interested_requirements_complete"}
        assert all(hp.cited(ln["source"]) for ln in all_lines(pkg))
        assert "“ideally live before March”" in pkg.summary and "2027-03-01" not in pkg.summary
        assert "Budget: not stated in their emails yet" in pkg.sections["unknown"]
        assert f"CRM meetings.starts_at #{meeting.id}" in pkg.summary
        assert s.execute(select(Alert).where(Alert.source == "handover", Alert.message.like("Handed over to manager%"))).first()
        pkg_id = pkg.id
    # the Super Admin authorizes the internal email; the next sending run tells the manager, once
    with ctx.db.session() as s:
        hn.save(s, ctx.settings, "owner", {"addresses": "manager = manager@us.example", "email_enabled": True})
        hn.authorize(s, ctx.settings, "owner")
    assert [r["status"] for r in hn.run(ctx, smtp_factory=FakeSMTP)["results"]] == ["sent"]
    assert hn.run(ctx, smtp_factory=FakeSMTP)["results"] == []
    assert [m["To"] for m in FakeSMTP.sent] == ["manager@us.example"] and DAVID not in FakeSMTP.sent[0]["To"]
    # the manager accepts: AI drafting for David's lead pauses
    client = TestClient(create_app(ctx))
    page = client.get(f"/opportunities/{opp_id}")
    assert "Handover package" in page.text and "ideally live before March" in page.text
    r = client.post(f"/opportunities/{opp_id}/handover/accept", data={"package_id": pkg_id}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303
    with ctx.db.session() as s:
        assert hp.ai_paused(s, reply.lead_id) and s.get(HandoverPackage, pkg_id).status == "accepted"
    with ctx.db.session() as s:
        name = s.get(Opportunity, opp_id).name
    assert name in client.get("/handovers?status=accepted").text and name not in client.get("/handovers").text


def test_the_conversation_engines_own_handover_request_reaches_the_package(make_ctx):
    """Integration of the two Option B teams: aime.ai.conversation.needs_handover is what the engine really calls, and the
    handover package must read it as a conversation escalation; a person's rejection closes it."""
    from aime.ai import conversation
    from aime.ai.decisions import record_human_action

    ctx = make_ctx()
    ids = seed(ctx, facts=False)
    with ctx.db.session() as s:
        rid = conversation.needs_handover(s, ids["lead"], "they asked about contract terms", codes=["contract"], alert=False)
        s.flush()
        found = hp.evaluate(s, ctx.settings, s.get(Opportunity, ids["opp"]))
        assert [c["key"] for c in found] == ["conversation_escalation"] and "contract terms" in found[0]["why"]
        assert conversation.handover_requests(s, ids["lead"])[0].id == rid
        record_human_action(s, rid, "rejected", "manager")
        assert hp.evaluate(s, ctx.settings, s.get(Opportunity, ids["opp"])) == []


def test_a_held_calendly_notice_is_not_listed_as_their_email(make_ctx):
    """A forged or unverified notice was held for review: the package's links must not present it as the prospect's own
    email, nor as a verified booking."""
    ctx = make_ctx()
    ids = seed(ctx)
    with ctx.db.session() as s:
        s.add(InboundEmail(message_id="<forged@calendly.example>", from_email="notifications@calendly.com",
                           subject="New Event: Dana Reyes", new_text="New Event", body_text="New Event", category="calendly",
                           lead_id=ids["lead"], company_id=ids["company"], received_at=NOW - timedelta(hours=1),
                           status="needs_review"))
    pkg = hand_over(ctx, ids["opp"])
    links = [ln["text"] for ln in pkg.sections["links"]["lines"]]
    assert any(t.startswith("Calendly notice (not verified: held for review) of ") for t in links), links
    assert sum(t.startswith("Their email of ") for t in links) == 1  # the prospect's own reply only
