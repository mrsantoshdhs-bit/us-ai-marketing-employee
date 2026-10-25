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
