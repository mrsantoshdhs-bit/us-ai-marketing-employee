from datetime import timedelta

from aime.scoring import LeadFacts, SignalFact, grade_for, score_lead
from aime.timeutil import utcnow

TARGETS = ["saas", "real_estate", "smb"]


def facts(**kw):
    base = dict(source="research", industry="saas", target_industries=TARGETS, state="CA", country="US",
                has_website=True, employee_range=None, verified=False, is_competitor=False, suppressed=False,
                contact_consent=None, signals=[], now=utcnow())
    base.update(kw)
    return LeadFacts(**base)


def test_no_evidence_means_no_intent():
    r = score_lead(facts())
    assert r.intent == 0
    assert any("no recorded buying signal" in x for x in r.reasons)


def test_hiring_signal_is_weak_intent_only():
    sig = SignalFact("hiring", ["ai_agents"], 1.0, utcnow(), True)
    r = score_lead(facts(signals=[sig]))
    assert r.intent == 6
    assert r.need > 0 and r.matched_services == ["ai_agents"]


def test_inbound_with_timeline_has_full_intent():
    sig = SignalFact("inbound", ["custom_crm"], 1.0, utcnow(), True)
    r = score_lead(facts(source="inbound", signals=[sig], contact_consent="inquiry", stated_timeline=True))
    assert r.intent == 20
    assert r.grade in ("A", "B")


def test_disqualifiers():
    assert score_lead(facts(country="ZZ")).disqualify_reason
    assert score_lead(facts(is_competitor=True)).disqualify_reason
    assert score_lead(facts(contact_consent="opted_out")).disqualify_reason


def test_bounds_and_grades():
    sig = SignalFact("inbound", ["custom_crm", "bpa", "ai_agents"], 1.0, utcnow() - timedelta(days=1), True)
    r = score_lead(facts(source="inbound", signals=[sig], verified=True, employee_range="11-50",
                         contact_consent="opt_in", stated_budget=True, explicit_services=["saas_dev"]))
    assert 0 <= r.total <= 100
    assert r.total == r.fit + r.need + r.intent + r.data
    assert grade_for(80, {}) == "A" and grade_for(60, {}) == "B" and grade_for(40, {}) == "C" and grade_for(10, {}) == "D"


def test_recency_bonus_needs_a_recent_signal_with_a_need():
    old_need = SignalFact("hiring", ["crm"], 0.8, utcnow() - timedelta(days=200), True)
    fresh_filing = SignalFact("filing", [], 0.0, utcnow() - timedelta(days=2), True)
    with_old = score_lead(facts(signals=[old_need]))
    assert score_lead(facts(signals=[old_need, fresh_filing])).total == with_old.total
    fresh_need = SignalFact("hiring", ["crm"], 0.8, utcnow() - timedelta(days=2), True)
    assert any("last 45 days" in r for r in score_lead(facts(signals=[fresh_need])).reasons)


def test_a_lead_moved_to_nurture_is_not_requalified_without_new_evidence(make_ctx):
    from aime.agents.crm import CRMManagementAgent
    from aime.agents.qualification import LeadQualificationAgent
    from aime.crm_ops import add_signal, ensure_lead, log_activity
    from aime.models import Company, Lead
    ctx = make_ctx()
    post = ("We are hiring a senior engineer to rebuild our CRM integration, automate sales follow-up emails and "
            "lead scoring, and build AI agents for our pipeline. Contractors welcome.")
    with ctx.db.session() as s:
        co = Company(name="Acme", normalized_name="acme", domain="acme.example", website="https://acme.example",
                     state="TX", industry="saas", source="hn_hiring", website_status="verified",
                     website_checked_at=utcnow())
        s.add(co)
        s.flush()
        add_signal(s, co, "hiring", post, "hn_hiring", "https://news.ycombinator.com/item?id=1",
                   utcnow() - timedelta(days=3))
        lead, _ = ensure_lead(s, co, "research")
        s.flush()
        lid, cid = lead.id, co.id
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    with ctx.db.session() as s:
        lead = s.get(Lead, lid)
        assert lead.status == "qualified"
        lead.last_activity_at = lead.created_at = utcnow() - timedelta(days=40)
    CRMManagementAgent(ctx).run({"operations": ["stale"]})
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    with ctx.db.session() as s:
        assert s.get(Lead, lid).status == "nurture"  # the stale-lead rule's move holds
        lead = s.get(Lead, lid)
        lead.status = "nurture"
        log_activity(s, "Status qualified → nurture (manual)", "owner", type_="status_change", lead_id=lid)
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    with ctx.db.session() as s:
        assert s.get(Lead, lid).status == "nurture"  # a person's decision holds
        add_signal(s, s.get(Company, cid), "hiring", post + " Now also hiring a RevOps lead.", "hn_hiring",
                   "https://news.ycombinator.com/item?id=2", utcnow())
    LeadQualificationAgent(ctx).run({"rescore_all": True})
    with ctx.db.session() as s:
        assert s.get(Lead, lid).status == "qualified"  # new evidence after the move
