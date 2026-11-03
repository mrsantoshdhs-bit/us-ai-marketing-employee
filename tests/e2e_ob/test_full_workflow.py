"""Option B, the whole workflow end to end through the real jobs, offline, on an isolated database:

market research (plan job) -> company discovery (research job: the owner's CSV list, Hacker News mocked) -> decision-maker
identification (enrich job: the company's own website with a team page) -> contact verification (MX table) ->
qualification (scoring) -> outreach draft -> a person approves it on the web -> the followups job sends it (fake SMTP) ->
the prospect replies (fake IMAP, inbox job) -> the AI conversation engine in drafts mode (no model, and a scripted model)
-> requirements, budget, timeline over several turns -> a meeting booked by a DKIM-verified Calendly notice -> CRM ->
the Sales Manager's handover package and its authorized internal email -> the manager accepts -> the daily manager report
counts what the records hold -> the funnel and the daily targets agree with it.

Then the ways it can go otherwise: not interested; an opt-out in the middle of the conversation; a process killed after
the send was claimed; a crash between storing a reply and handling it; a restart on the same database.
`sweep` (tests/e2e_ob/helpers.py) checks the safety invariants after every scenario."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from aime import funnel, targets
from aime import handover_package as hp
from aime import manager_report as mr
from aime.crm_ops import is_suppressed
from aime.models import (AIDecision, Company, Contact, Conversation, ConversationFact, FollowUp, HandoverPackage,
                         InboundEmail, Lead, Meeting, Opportunity, Report)
from tests.e2e_ob.helpers import (DAVID, MANAGER_ADDR, ORIGIN, SITE, FakeSMTP, World, at, drafted_and_approved, forward,
                                  sends_to, sweep)
from tests.test_inbound_sales import TUESDAY_11AM_ET

BRAINS = ("rules", "model")


def through_first_send(w: World) -> FollowUp:
    """Market research to the first email, every stage by its real job."""
    w.import_list()
    w.job("plan")
    w.job("research")
    with w.ctx.db.session() as s:
        company = s.execute(select(Company).where(Company.domain == SITE)).scalar_one()
        assert company.source == "csv_import" and company.website_status == "verified"
    w.job("enrich")  # the company's own website: the team page names David Chen and prints his address
    assert any(u.endswith("/team") for u in w.site_requests)
    with w.ctx.db.session() as s:
        leads = s.execute(select(Lead)).scalars().all()
        lead = next(x for x in leads if x.company.domain == SITE)
        assert lead.contact is not None, "no decision-maker identified"
        assert lead.contact.display_name == "David Chen" and lead.contact.email == DAVID
        assert lead.contact.email_status == "published"  # on the company's own site, next to him, and the domain takes mail
        assert lead.status == "qualified" and lead.grade in ("A", "B"), (lead.status, lead.grade, lead.score_breakdown)
    w.job("research")  # the next research run drafts the first touch (the research job never sends)
    (draft,) = w.followups(kind="outreach")
    assert draft.status == "awaiting_approval" and draft.approved_at is None and draft.to_email == DAVID
    w.send()
    assert FakeSMTP.sent == [] and w.followup(draft.id).status == "awaiting_approval"  # nothing before a person approves
    w.approve(draft.id)
    w.send()
    sent = w.followup(draft.id)
    assert sent.status == "sent" and [m["To"] for m in FakeSMTP.sent] == [DAVID]
    w.thread = sent.message_id
    return sent


def conversation(w: World):
    with w.ctx.db.session() as s:
        return s.execute(select(Conversation)).scalars().first()


def facts(w: World, row_id: int) -> set[str]:
    with w.ctx.db.session() as s:
        return {f.kind for f in s.execute(select(ConversationFact).where(ConversationFact.evidence_inbound_id == row_id)).scalars()}


def booked_notice():
    """The Calendly booking for David, DKIM-signed by calendly.com with the test key."""
    from tests.test_dkim import MESSAGE, RSA_KEY, dns_record, sign
    return sign(MESSAGE, "calendly.com"), {"s1._domainkey.calendly.com": dns_record(RSA_KEY)}, MESSAGE


@pytest.mark.parametrize("brain", BRAINS)
def test_success_path_from_market_research_to_the_managers_report(make_ctx, monkeypatch, network_attempts, brain):
    w = World(make_ctx, monkeypatch, brain=brain)
    w.conversation_mode("drafts")
    first = through_first_send(w)

    # turn 1: interested, with the requirement
    t1 = w.say("This sounds interesting. We are looking to automate how we enter supplier invoices.")
    assert t1.category == "interested" and t1.lead_id == w.lead().id and t1.followup_id == first.id
    assert "requirement" in facts(w, t1.id)
    assert conversation(w).state in ("discovering", "qualified", "scheduling")
    f1 = w.followup(t1.suggested_followup_id)
    assert f1.status == "awaiting_approval" and (f1.generator or "").startswith("AI"), f1.generator
    w.answer(t1)

    # turn 2: the budget
    t2 = w.say("Our budget is about $15k for the first phase. One thing: our data has to stay in the United States.")
    assert "budget" in facts(w, t2.id)
    if t2.suggested_followup_id:
        w.answer(t2)

    # turn 3: the timeline
    t3 = w.say("We would like it live by March. Happy to talk next week.")
    assert "timeline" in facts(w, t3.id)
    with w.ctx.db.session() as s:
        stated = {f.kind for f in s.execute(select(ConversationFact).where(ConversationFact.lead_id == t1.lead_id)).scalars()}
        sources = {f.source for f in s.execute(select(ConversationFact).where(ConversationFact.lead_id == t1.lead_id)).scalars()}
        quotes_ok = all(f.evidence_quote and " ".join(f.evidence_quote.lower().split()).rstrip("…") in
                        " ".join((s.get(InboundEmail, f.evidence_inbound_id).new_text or "").lower().split())
                        for f in s.execute(select(ConversationFact).where(ConversationFact.lead_id == t1.lead_id)).scalars())
    assert {"requirement", "budget", "timeline"} <= stated and quotes_ok  # every fact quotes the email it came from
    if brain == "model":
        assert w.ctx.conversation_llm.calls >= 3 and "ai" in sources, sources
    else:
        assert "ai" not in sources
    if t3.suggested_followup_id:
        w.answer(t3)

    # the meeting: a forged notice changes nothing, the signed one books it
    signed, records, unsigned = booked_notice()
    held = w.calendly(unsigned.replace(b"<n1@calendly.example>", b"<forged@calendly.example>"), monkeypatch, records)
    assert held.status == "needs_review"
    with w.ctx.db.session() as s:
        assert s.execute(select(Meeting)).first() is None
    notice = w.calendly(signed, monkeypatch, records)
    assert notice.status == "actioned" and "dkim=pass header.d=calendly.com" in notice.auth_results
    with w.ctx.db.session() as s:
        meeting = s.execute(select(Meeting)).scalar_one()
        lead = s.get(Lead, t1.lead_id)
        opp = s.execute(select(Opportunity).where(Opportunity.lead_id == lead.id)).scalar_one()
        assert meeting.lead_id == lead.id and lead.outreach_status == "meeting"  # CRM updated
        pkg = hp.latest(s, opp.id)
        assert pkg is not None and pkg.owner == "manager" and pkg.status != "accepted"
        assert "meeting_booked" in {c["key"] for c in pkg.conditions}
        assert all(hp.cited(ln["source"]) for key, sec in pkg.sections.items() if key != "unknown" for ln in sec["lines"])
        assert "budget is about $15k" in pkg.summary and "live by March" in pkg.summary
        pkg_id, opp_id = pkg.id, opp.id

    # the internal email to the Sales Manager: nothing until the Super Admin authorizes it, then once
    before = len(FakeSMTP.sent)
    w.send()
    assert len(FakeSMTP.sent) == before
    w.authorize_handover_email()
    w.send()
    w.send()
    internal = [m for m in FakeSMTP.sent if m["To"] == MANAGER_ADDR]
    assert len(internal) == 1 and DAVID not in internal[0]["To"]
    with w.ctx.db.session() as s:
        pkg = hp.latest(s, opp_id)  # the notice's own line came after the meeting: a newer version, one email for it
        assert pkg.id >= pkg_id and pkg.notify_status == "sent" and pkg.notify_to == MANAGER_ADDR
        pkg_id = pkg.id

    # the manager accepts: the AI stops drafting for this lead
    r = w.client.post(f"/opportunities/{opp_id}/handover/accept", data={"package_id": pkg_id}, headers=ORIGIN,
                      follow_redirects=False)
    assert r.status_code == 303
    with w.ctx.db.session() as s:
        assert s.get(HandoverPackage, pkg_id).status == "accepted" and hp.ai_paused(s, t1.lead_id)

    # the daily manager report, written by its job the next morning in India, counts what the records hold
    at(datetime(2026, 10, 27, 23, 30))  # 05:00 IST on the 28th: the report for Tue 27 Oct IST
    w.job("manager_report")
    with w.ctx.db.session() as s:
        report = s.execute(select(Report).where(Report.kind == "manager_daily")).scalars().first()
        assert report is not None
        period = mr.Period("daily", date(2026, 10, 27), date(2026, 10, 28), "Asia/Kolkata")
        data = mr.build(s, w.ctx.settings, period)
        v = {k: m["value"] for k, m in data["metrics"].items()}
        lo, hi = period.start_utc, period.end_utc

        def n(model, *conds):
            return s.execute(select(func.count(model.id)).where(*conds)).scalar_one()
        prospect_sent = n(FollowUp, FollowUp.status == "sent", FollowUp.to_email == DAVID, FollowUp.sent_at >= lo, FollowUp.sent_at < hi)
        outreach_sent = n(FollowUp, FollowUp.status == "sent", FollowUp.kind == "outreach", FollowUp.sent_at >= lo, FollowUp.sent_at < hi)
        replies = n(InboundEmail, InboundEmail.from_email == DAVID, InboundEmail.received_at >= lo, InboundEmail.received_at < hi)
        companies = n(Company, Company.created_at >= lo, Company.created_at < hi, Company.merged_into_id.is_(None),
                      Company.is_sample.is_(False))
        assert v["companies_discovered"] == companies >= 1  # the CSV company and those the mocked Hacker News thread names
        assert v["decision_makers"] >= 1 and v["verified_decision_makers"] == 1
        assert v["outreach_sent"] == outreach_sent == 1 and v["leads_contacted"] == 1
        assert v["emails_sent"] == prospect_sent, (v["emails_sent"], prospect_sent)
        assert v["replies"] == replies == 3 and v["interested_replies"] >= 1
        assert v["meetings_booked"] == n(Meeting) == 1 and v["opportunities_opened"] == n(Opportunity) == 1
        assert v["test_mode_deliveries"] == 0 and v["unsubscribes"] == 0
        stored = {k: m["value"] for k, m in report.data["metrics"].items()} if getattr(report, "data", None) else None
        if stored is not None:
            assert {k: stored[k] for k in ("outreach_sent", "replies", "meetings_booked")} == \
                   {k: v[k] for k in ("outreach_sent", "replies", "meetings_booked")}
        # the funnel and the daily targets agree with the report
        summary = funnel.summary(s)
        fs = {st["key"]: st for st in summary["stages"]}
        assert summary["companies"] == v["companies_discovered"]
        assert fs["qualified_lead"]["reached"] == v["qualified_leads"]
        assert fs["verified_contact"]["reached"] == v["verified_contacts"]
        assert fs[funnel.STAGE_KEYS[-1]]["in_funnel"] == 1  # only Brightline reaches the end: a verified decision-maker
        us = next(p for p in targets.progress(s, w.ctx.settings, now=TUESDAY_11AM_ET + timedelta(hours=2)) if p.country == "US")
        assert us.qualified == v["qualified_leads"] and us.disqualified == v["disqualified_leads"]
        assert us.companies_found == v["companies_discovered"]
        assert us.verified_leads == v["verified_decision_makers"] == 1
    sweep(w, network_attempts)


# --------------------------------------------------------------------------------------------------- otherwise

@pytest.mark.parametrize("brain", BRAINS)
def test_not_interested_closes_the_lead_and_nothing_more_is_sent(make_ctx, monkeypatch, network_attempts, brain):
    w = World(make_ctx, monkeypatch, brain=brain)
    w.conversation_mode("drafts")
    through_first_send(w)
    row = w.say("Not interested, thanks.")
    assert row.category == "not_interested"
    with w.ctx.db.session() as s:
        lead = s.get(Lead, row.lead_id)
        assert lead.outreach_status not in ("contacted", "drafted", "interested"), lead.outreach_status
        assert s.execute(select(Conversation)).scalar_one().state == "closed_lost"
    assert row.suggested_followup_id is None or not (w.followup(row.suggested_followup_id).generator or "").startswith("AI")
    # a week of check-ins and research runs later: no follow-up step, no new draft, nothing sent
    for days in (1, 4, 6, 8):
        at(TUESDAY_11AM_ET + timedelta(days=days, minutes=5))
        w.job("research", ("success", "partial"))
        w.send()
    assert sends_to(DAVID) == 1
    assert [f.step for f in w.followups(kind="outreach", to_email=DAVID)] == [1]
    assert w.followups(to_email=DAVID, status="awaiting_approval") == [] and w.followups(to_email=DAVID, status="approved") == []
    sweep(w, network_attempts)


@pytest.mark.parametrize("brain", BRAINS)
def test_opt_out_mid_conversation_stops_the_whole_company(make_ctx, monkeypatch, network_attempts, brain):
    w = World(make_ctx, monkeypatch, brain=brain)
    w.conversation_mode("drafts")
    through_first_send(w)
    t1 = w.say("This sounds interesting. We are looking to automate how we enter supplier invoices.")
    w.answer(t1)
    t2 = w.say("Actually, please remove me from your mailing list.")
    assert t2.category == "unsubscribe"
    with w.ctx.db.session() as s:
        assert is_suppressed(s, DAVID)
        assert s.execute(select(Conversation)).scalar_one().state == "opted_out"
        drafts = s.execute(select(AIDecision).where(AIDecision.task == "conversation_draft",
                                                    AIDecision.input_ref == f"inbound_email:{t2.id}")).scalars().all()
        assert drafts == []
        # a colleague at the same company, qualified after the opt-out
        company = s.execute(select(Company).where(Company.domain == SITE)).scalar_one()
        sarah = Contact(company_id=company.id, first_name="Sarah", last_name="Oneil", title="VP of Sales",
                        email=f"sarah.oneil@{SITE}", email_status="published", source="website")
        s.add(sarah)
        s.flush()
        s.add(Lead(company_id=company.id, contact_id=sarah.id, source="research", status="qualified", score=80, grade="A",
                   primary_service="bpa"))
    assert t2.suggested_followup_id is None, "a draft for someone who asked to be removed"
    # a person approves whatever is queued; research, enrichment and the send job run for a week
    for days in (0, 1, 4, 6):
        at(TUESDAY_11AM_ET + timedelta(days=days, minutes=30))
        w.job("research", ("success", "partial"))
        w.job("enrich", ("success", "partial"))
        for f in w.followups(status="awaiting_approval"):
            w.client.post(f"/followups/{f.id}/approve", headers=ORIGIN)
        w.send()
    assert sends_to(DAVID) == 2 and sends_to("sarah.oneil@") == 0
    with w.ctx.db.session() as s:
        late = s.execute(select(FollowUp).where(FollowUp.to_email.in_((DAVID, f"sarah.oneil@{SITE}")),
                                                FollowUp.created_at > t2.received_at)).scalars().all()
        assert all(f.status not in ("sent", "sending", "approved", "awaiting_approval") for f in late), \
            [(f.id, f.to_email, f.status) for f in late]
    sweep(w, network_attempts)


def test_a_process_killed_after_the_send_claim_is_uncertain_and_never_resent(make_ctx, monkeypatch, network_attempts):
    from aime.models import JobRun

    w = World(make_ctx, monkeypatch)
    draft = drafted_and_approved(w)
    w.job("inbox")
    FakeSMTP.die_before_accept = True
    with pytest.raises(KeyboardInterrupt):
        w.job("followups")
    FakeSMTP.die_before_accept = False
    assert w.followup(draft.id).status == "sending" and FakeSMTP.sent == []
    w.restart()  # the PC comes back: a new process on the same database
    forward(timedelta(minutes=3))
    w.send()  # the claim is still fresh: left alone
    assert w.followup(draft.id).status == "sending" and FakeSMTP.sent == []
    forward(timedelta(minutes=12))
    w.send()  # stale: a person checks the Sent folder; nothing is resent
    f = w.followup(draft.id)
    assert f.status == "uncertain" and "Sent folder" in (f.blocked_reason or "")
    for days in (1, 5):
        at(TUESDAY_11AM_ET + timedelta(days=days, minutes=10))
        w.job("research", ("success", "partial"))
        w.send()
    assert FakeSMTP.sent == [] and [x.step for x in w.followups(kind="outreach", to_email=DAVID)] == [1]
    with w.ctx.db.session() as s:
        stuck = s.execute(select(JobRun).where(JobRun.job == "followups", JobRun.status == "running")).scalars().all()
        assert stuck == [], [(r.id, r.started_at) for r in stuck]  # the killed run is closed out, not left running
    sweep(w, network_attempts)


def test_the_server_took_it_then_the_process_died_is_sent_once(make_ctx, monkeypatch, network_attempts):
    w = World(make_ctx, monkeypatch)
    draft = drafted_and_approved(w)
    w.job("inbox")
    FakeSMTP.die_after_accept = True
    with pytest.raises(KeyboardInterrupt):
        w.job("followups")
    FakeSMTP.die_after_accept = False
    w.restart()
    for minutes in (5, 15, 60):
        forward(timedelta(minutes=minutes))
        w.send()
    assert sends_to(DAVID) == 1 and w.followup(draft.id).status == "uncertain"
    sweep(w, network_attempts)


@pytest.mark.parametrize("brain", BRAINS)
def test_a_crash_between_storing_a_reply_and_handling_it_handles_it_once(make_ctx, monkeypatch, network_attempts, brain):
    from aime.agents import inbox_agent

    w = World(make_ctx, monkeypatch, brain=brain)
    w.conversation_mode("drafts")
    through_first_send(w)
    real = inbox_agent.process_pending
    calls = {"n": 0}

    def dies_after_storing(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:  # the first call handles what was pending before the read; the second, what was just stored
            raise KeyboardInterrupt
        return real(*a, **k)

    monkeypatch.setattr(inbox_agent, "process_pending", dies_after_storing)
    with pytest.raises(KeyboardInterrupt):
        w.say("This sounds interesting. We are looking to automate how we enter supplier invoices.")
    monkeypatch.setattr(inbox_agent, "process_pending", real)
    with w.ctx.db.session() as s:
        (row,) = s.execute(select(InboundEmail)).scalars().all()
        assert row.status == "new"  # stored, not yet handled: nothing lost
        rid = row.id
    w.restart()
    w.job("inbox")
    w.job("inbox")
    with w.ctx.db.session() as s:
        rows = s.execute(select(InboundEmail)).scalars().all()
        assert [r.id for r in rows] == [rid] and rows[0].status == "actioned" and rows[0].category == "interested"
        turns = s.execute(select(AIDecision).where(AIDecision.task == "conversation_turn",
                                                   AIDecision.input_ref == f"inbound_email:{rid}")).scalars().all()
        assert len(turns) == 1
        suggestions = s.execute(select(FollowUp).where(FollowUp.kind == "response", FollowUp.to_email == DAVID)).scalars().all()
        assert len(suggestions) == 1 and suggestions[0].status == "awaiting_approval"
    assert sends_to(DAVID) == 1
    sweep(w, network_attempts)


@pytest.mark.parametrize("brain", BRAINS)
def test_a_crash_between_the_rules_and_the_conversation_engine_still_gets_its_turn(make_ctx, monkeypatch, network_attempts, brain):
    """The rules' outcome is committed before the engine's turn. A process killed in between must not leave the email
    handled by the rules only, for good: the engine's turn is taken after the restart, once."""
    from aime import reply_intel

    w = World(make_ctx, monkeypatch, brain=brain)
    w.conversation_mode("drafts")
    through_first_send(w)
    real = reply_intel._conversation_turn

    def killed(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(reply_intel, "_conversation_turn", killed)
    with pytest.raises(KeyboardInterrupt):
        w.say("This sounds interesting. We are looking to automate how we enter supplier invoices.")
    monkeypatch.setattr(reply_intel, "_conversation_turn", real)
    w.restart()
    w.job("inbox")
    w.job("inbox")
    with w.ctx.db.session() as s:
        (row,) = s.execute(select(InboundEmail)).scalars().all()
        turns = s.execute(select(AIDecision).where(AIDecision.task == "conversation_turn",
                                                   AIDecision.input_ref == f"inbound_email:{row.id}")).scalars().all()
        assert len(turns) == 1, "the conversation engine never took its turn on this email"
        suggestions = s.execute(select(FollowUp).where(FollowUp.kind == "response", FollowUp.to_email == DAVID)).scalars().all()
        assert len(suggestions) == 1 and suggestions[0].status == "awaiting_approval"
    sweep(w, network_attempts)


@pytest.mark.parametrize("brain", BRAINS)
def test_after_a_restart_a_new_process_continues_the_conversation(make_ctx, monkeypatch, network_attempts, brain):
    w = World(make_ctx, monkeypatch, brain=brain)
    w.conversation_mode("drafts")
    through_first_send(w)
    t1 = w.say("This sounds interesting. We are looking to automate how we enter supplier invoices.")
    w.restart()  # the PC restarts with the answer waiting for approval
    assert w.followup(t1.suggested_followup_id).status == "awaiting_approval"
    w.answer(t1)
    w.restart()
    t2 = w.say("We would like it live by March. Happy to talk next week.")
    assert t2.lead_id == t1.lead_id and t2.followup_id == t1.suggested_followup_id
    with w.ctx.db.session() as s:
        convs = s.execute(select(Conversation)).scalars().all()
        assert len(convs) == 1 and convs[0].state in ("discovering", "qualified", "scheduling")
        kinds = {f.kind for f in s.execute(select(ConversationFact).where(ConversationFact.lead_id == t1.lead_id)).scalars()}
        assert {"requirement", "timeline"} <= kinds
        waiting = s.execute(select(FollowUp).where(FollowUp.to_email == DAVID, FollowUp.status == "awaiting_approval")).scalars().all()
        assert len(waiting) == 1  # the new turn's answer; nothing from before twice
    assert sends_to(DAVID) == 2
    sweep(w, network_attempts)
