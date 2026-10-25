"""Lead scoring: transparent, evidence-based, 0-100.

Fit (35) + Need (35) + Intent (20) + Data quality (10).
Intent is never inferred: it is scored only from recorded evidence
(an inbound inquiry, or a public hiring post that mentions a related need).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aime.knowledge.geo import STATES
from aime.timeutil import utcnow

FIRST_PARTY_SIGNALS = {"inbound", "manual"}


@dataclass
class SignalFact:
    signal_type: str
    matched_services: list[str]
    strength: float
    observed_at: datetime
    has_source_url: bool


@dataclass
class LeadFacts:
    source: str
    industry: str | None
    target_industries: list[str]
    state: str | None
    country: str
    has_website: bool
    employee_range: str | None
    verified: bool
    is_competitor: bool
    suppressed: bool
    contact_consent: str | None  # inquiry | opt_in | none | opted_out | None
    signals: list[SignalFact] = field(default_factory=list)
    explicit_services: list[str] = field(default_factory=list)
    stated_timeline: bool = False
    stated_budget: bool = False
    now: datetime | None = None


@dataclass
class ScoreResult:
    total: int
    grade: str
    fit: int
    need: int
    intent: int
    data: int
    matched_services: list[str]
    reasons: list[str]
    disqualify_reason: str | None = None


def grade_for(total: int, thresholds: dict) -> str:
    if total >= thresholds.get("grade_a", 75):
        return "A"
    if total >= thresholds.get("grade_b", 55):
        return "B"
    if total >= thresholds.get("grade_c", 35):
        return "C"
    return "D"


def score_lead(f: LeadFacts, thresholds: dict | None = None) -> ScoreResult:
    thresholds = thresholds or {}
    now = f.now or utcnow()
    reasons: list[str] = []

    # ---- Fit (0-35)
    fit = 0
    if f.industry and f.industry in f.target_industries:
        pts = 8 if f.industry == "smb" else 15
        fit += pts
        reasons.append(f"+{pts} fit: target industry ({f.industry})")
    if f.country == "US" and f.state in STATES:
        fit += 10
        reasons.append(f"+10 fit: US location verified ({f.state})")
    elif f.country == "US" and f.state == "DC":
        fit += 10
        reasons.append("+10 fit: US location verified (DC)")
    elif f.country == "US":
        fit += 4
        reasons.append("+4 fit: US presence stated, state unknown")
    if f.has_website:
        fit += 5
        reasons.append("+5 fit: company website known")
    if f.employee_range:
        fit += 5
        reasons.append(f"+5 fit: company size known ({f.employee_range})")

    # ---- Need (0-35). Needs a person stated (inbound/manual) weigh more than needs
    # inferred from public posts: a company hiring engineers may build in-house.
    service_strength: dict[str, float] = {}
    first_party: set[str] = set()
    for s in f.signals:
        for svc in s.matched_services:
            service_strength[svc] = max(service_strength.get(svc, 0.0), s.strength)
            if s.signal_type in FIRST_PARTY_SIGNALS:
                first_party.add(svc)
    for svc in f.explicit_services:
        service_strength[svc] = 1.0
        first_party.add(svc)
    ranked = sorted(service_strength.items(), key=lambda kv: (-(kv[0] in first_party), -kv[1]))
    need = 0
    for svc, strength in ranked[:3]:
        if svc in first_party:
            pts = round(5 + 5 * strength)
            reasons.append(f"+{pts} need: they asked about {svc} (strength {strength:.2f})")
        else:
            pts = round(3 + 4 * strength)
            reasons.append(f"+{pts} need: public evidence for {svc} (strength {strength:.2f})")
        need += pts
    recent = [s for s in f.signals if now - s.observed_at <= timedelta(days=45)]
    if recent and ranked:
        need += 5
        reasons.append("+5 need: evidence observed in the last 45 days")
    need = min(35, need)

    # ---- Intent (0-20): evidence only
    intent = 0
    if any(s.signal_type == "inbound" for s in f.signals) or f.source == "inbound":
        intent = 15
        reasons.append("+15 intent: they contacted us (inbound inquiry)")
        if f.stated_timeline or f.stated_budget:
            intent = 20
            reasons.append("+5 intent: stated timeline or budget")
    elif any(s.signal_type == "hiring" and s.matched_services for s in f.signals):
        intent = 6
        reasons.append("+6 intent: public hiring post mentions a related need (signal, not buying intent)")
    else:
        reasons.append("+0 intent: no recorded buying signal")

    # ---- Data quality (0-10)
    data = 0
    if any(s.has_source_url for s in f.signals):
        data += 4
        reasons.append("+4 data: evidence links to its source")
    if f.verified:
        data += 3
        reasons.append("+3 data: company record verified from an authorized source")
    if f.contact_consent in {"inquiry", "opt_in"}:
        data += 3
        reasons.append("+3 data: contact provided by the person with consent")

    total = max(0, min(100, fit + need + intent + data))
    disqualify = None
    if f.country != "US":
        disqualify = "Outside US target market"
    elif f.is_competitor:
        disqualify = "Company appears to sell similar services (competitor)"
    elif f.suppressed or f.contact_consent == "opted_out":
        disqualify = "Contact or domain is on the suppression list"
    if disqualify:
        reasons.append(f"disqualified: {disqualify}")
    return ScoreResult(
        total=total,
        grade=grade_for(total, thresholds),
        fit=fit,
        need=need,
        intent=intent,
        data=data,
        matched_services=[svc for svc, _ in ranked],
        reasons=reasons,
        disqualify_reason=disqualify,
    )
