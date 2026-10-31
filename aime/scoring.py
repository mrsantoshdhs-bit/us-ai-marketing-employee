"""Lead scoring: transparent, evidence-based, 0-100.

Fit (35) + Need (35) + Intent (20) + Data quality (10).
Intent is never inferred: it is scored only from recorded evidence
(an inbound inquiry, or a public hiring post that mentions a related need).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from aime.knowledge.geo import STATES, parse_location
from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import service_name
from aime.matching import classify_industry, match_services
from aime.people import decision_maker_title
from aime.timeutil import utcnow

FIRST_PARTY_SIGNALS = {"inbound", "manual"}


@dataclass
class SignalFact:
    signal_type: str
    matched_services: list[str]
    strength: float
    observed_at: datetime
    has_source_url: bool
    hires_builders: bool = False  # a hiring post for people who build software, whatever else it says


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
    running_business: bool = False  # its own words show customers, a product or funding
    us_office_only: bool = False  # places abroad come first in its own list: a US office, most likely not its home
    us_stated: bool = True  # its own words place it in the US; "US" may also be only the default on its record
    website_status: str | None = None  # see aime.website_check; None = not checked yet
    seeks_external_help: bool = False  # their own post asks for contractors / partners
    contact_source: str | None = None  # inbound | manual | import | website | sec_filing
    contact_role: str | None = None  # role category; "inbox" for a published company inbox
    contact_email_status: str | None = None  # see aime.contact_validation
    contact_title: str | None = None
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


SERVICE_GROUPS = {
    "custom_crm": "CRM & Sales", "sales_automation": "CRM & Sales", "leadgen_automation": "CRM & Sales",
    "ai_agents": "AI & Agents", "agent_orchestration": "AI & Agents", "ai_consulting": "AI & Agents",
    "bpa": "Automation", "saas_dev": "Product Engineering", "custom_software": "Product Engineering",
}


def categorize(source: str, grade: str, primary_service: str | None, disqualified: bool) -> str:
    """Opportunity category: temperature tier plus service group, e.g. 'hot · CRM & Sales'."""
    if disqualified:
        tier = "disqualified"
    elif grade in ("A", "B"):
        tier = "hot" if source == "inbound" else "warm"
    elif grade == "C":
        tier = "nurture"
    else:
        tier = "cold"
    return f"{tier} · {SERVICE_GROUPS.get(primary_service or '', 'General')}"


def grade_for(total: int, thresholds: dict) -> str:
    if total >= thresholds.get("grade_a", 75):
        return "A"
    if total >= thresholds.get("grade_b", 55):
        return "B"
    if total >= thresholds.get("grade_c", 35):
        return "C"
    return "D"


_BUILDERS_RE = re.compile(r"\b(?:engineers?|developers?|programmers?|swe)\b", re.I)
_RUNNING_BUSINESS_RE = re.compile(r"\b(?:customers?|clients?|users|patients?|partners|hq|headquartered|revenue|profitable|paying|funded|funding|raised|series [a-f]|"
                                  r"seed|backed by|investors?|venture|y ?combinator|yc|arr|mrr|live product|in production|piloting|"
                                  r"(?:builds?|creates?|makes?|sells?) software|our (?:product|platform))\b", re.I)


def signal_fact(signal) -> SignalFact:
    """A stored signal as scoring sees it. A hiring post is read again by today's rules, so what the matching rules no longer
    accept stops counting at the next score, with nothing rewritten; what a person stated stays as recorded."""
    services, strength = list(signal.matched_services or []), signal.strength
    if signal.signal_type == "hiring" and signal.text:
        matches = match_services(signal.text)
        services, strength = [m.service_id for m in matches], max((m.strength for m in matches), default=0.0)
    builds = signal.signal_type == "hiring" and bool(_BUILDERS_RE.search(signal.text or ""))
    return SignalFact(signal.signal_type, services, strength, signal.observed_at, bool(signal.source_url), builds)


# The label every hiring post once got when it named no industry.
OLD_DEFAULT_INDUSTRY = "tech_startups"


_WORLDWIDE_RE = re.compile(r"\b(?:global|globally|worldwide|anywhere)\b", re.I)


def read_company(company, signals) -> dict:
    """The company's facts as scoring sees them. A company found in a hiring post is checked against the post's own words:
    older records carry a default industry, labels and locations read by older rules, and only evidence earns points. A company
    whose industry a person or a registry gave is taken as it is, and no stored record is changed."""
    facts = {"industry": company.industry, "state": company.state, "country": company.country}
    if company.source != "hn_hiring":
        return facts
    posts = " ".join(s.text or "" for s in signals if s.signal_type == "hiring")
    given = company.verified or any(s.signal_type in FIRST_PARTY_SIGNALS for s in signals)
    if company.industry == OLD_DEFAULT_INDUSTRY or (posts.strip() and not given):
        facts["industry"] = classify_industry(posts)
    facts["running_business"] = bool(_RUNNING_BUSINESS_RE.search(posts))
    place = parse_location(company.location_text)
    if place.is_us is False:
        facts["country"] = "outside"
    facts["us_office_only"] = place.foreign_first
    # the record's country is "US" by default: a post that places it nowhere, or anywhere, says nothing about the US
    facts["us_stated"] = place.is_us is True and not _WORLDWIDE_RE.search(company.location_text or "")
    return facts


# Hiring evidence counts in full for 60 days, half until 120 days, then not at all.
FRESH_DAYS, STALE_DAYS = 60, 120
HOLD_DAYS = 30  # how long a lead must keep its grade on today's evidence to enter qualified
# Weights measured on the real leads (Phase 2): the default industry's +15 and the programming-language matches are gone, and
# these evidence-based points keep companies that really do run a business and build software within reach of grade B.
BUILDERS_POINTS = 7
BUILDERS_INTENT = 6
RUNNING_BUSINESS_POINTS = 12
US_STATED_POINTS = 5
PRESENCE_ONLY_POINTS = 2


def _how_strong(strength: float) -> str:
    return "strong" if strength >= 0.75 else "moderate" if strength >= 0.5 else "slight"


def score_lead(f: LeadFacts, thresholds: dict | None = None) -> ScoreResult:
    thresholds = thresholds or {}
    now = f.now or utcnow()
    reasons: list[str] = []

    # ---- Fit (0-35)
    fit = 0
    if f.industry and f.industry in f.target_industries:
        pts = 8 if f.industry == "smb" else 15
        fit += pts
        reasons.append(f"+{pts} fit: target industry ({INDUSTRIES[f.industry].label if f.industry in INDUSTRIES else f.industry})")
    elif f.running_business:
        fit += RUNNING_BUSINESS_POINTS
        reasons.append(f"+{RUNNING_BUSINESS_POINTS} fit: its own words show a running business (customers, a product or funding), "
                       "industry not stated")
    elif not f.industry:
        reasons.append("+0 fit: nothing on record shows its industry")
    if f.country == "US" and f.us_office_only:
        fit += PRESENCE_ONLY_POINTS
        reasons.append(f"+{PRESENCE_ONLY_POINTS} fit: a US office, listed after places abroad")
    elif f.country == "US" and f.state in STATES:
        fit += 10
        reasons.append(f"+10 fit: US location verified ({f.state})")
    elif f.country == "US" and f.state == "DC":
        fit += 10
        reasons.append("+10 fit: US location verified (DC)")
    elif f.country == "US" and f.us_stated:
        fit += US_STATED_POINTS
        reasons.append(f"+{US_STATED_POINTS} fit: US presence stated, state unknown")
    elif f.country == "US":
        reasons.append("+0 fit: nothing it says places it in the US")
    if f.has_website:
        if f.website_status in ("verified", "redirected"):
            fit += 5
            reasons.append("+5 fit: company website verified")
        elif f.website_status in (None, "blocked", "robots_disallowed"):
            fit += 3
            reasons.append("+3 fit: company website listed (not verified)")
        elif f.website_status == "mismatch":
            fit += 1
            reasons.append("+1 fit: website found but does not name the company")
        else:
            reasons.append(f"+0 fit: website {f.website_status}")
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
            reasons.append(f"+{pts} need: they asked about {service_name(svc)} ({_how_strong(strength)} evidence)")
        else:
            pts = round(3 + 4 * strength)
            reasons.append(f"+{pts} need: public evidence for {service_name(svc)} ({_how_strong(strength)})")
        need += pts
    # Hiring people to build software is work an outside team could share, but it names no need: it takes a place among the
    # needs and never decides what we would offer.
    builders = [s for s in f.signals if s.hires_builders and now - s.observed_at <= timedelta(days=STALE_DAYS)]
    if builders and len(ranked) < 3:
        need += BUILDERS_POINTS
        reasons.append(f"+{BUILDERS_POINTS} need: it is hiring people to build software (work an outside team could share)")
    recent = [s for s in f.signals if (s.matched_services or s.hires_builders) and now - s.observed_at <= timedelta(days=45)]
    if recent and (ranked or builders):
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
        age = (now - max(s.observed_at for s in f.signals if s.signal_type == "hiring" and s.matched_services)).days
        if age <= FRESH_DAYS:
            intent = 6
            reasons.append(f"+6 intent: public hiring post ({age} days old) mentions a related need (signal, not buying intent)")
        elif age <= STALE_DAYS:
            intent = 3
            reasons.append(f"+3 intent: the newest related hiring post is {age} days old, so it counts for less")
        else:
            reasons.append(f"+0 intent: the newest related hiring post is {age} days old, too old to count")
        if f.seeks_external_help and intent:
            intent += 3
            reasons.append("+3 intent: their post asks for contractors, freelancers, or partners")
    elif any(s.hires_builders and now - s.observed_at <= timedelta(days=FRESH_DAYS) for s in f.signals):
        intent = BUILDERS_INTENT
        reasons.append(f"+{BUILDERS_INTENT} intent: a recent hiring post for people who build software, naming no need "
                       "(signal, not buying intent)")
    else:
        reasons.append("+0 intent: no recorded buying signal")

    # ---- Data quality (0-10)
    data = 0
    if any(s.has_source_url for s in f.signals):
        data += 3
        reasons.append("+3 data: evidence links to its source")
    if f.website_status in ("verified", "redirected"):
        data += 2
        reasons.append("+2 data: website confirmed live and naming the company")
    if f.verified:
        data += 2
        reasons.append("+2 data: company record verified from an official registry")
    # A testimonial or a menu item read as a job title, or someone who has left, names no decision-maker.
    researched = f.contact_source in ("website", "sec_filing") and (
        f.contact_role == "inbox" or f.contact_title is None or decision_maker_title(f.contact_title))
    if f.contact_consent in {"inquiry", "opt_in"}:
        data += 3
        reasons.append("+3 data: contact provided by the person with consent")
    elif researched and f.contact_role != "inbox" and f.contact_email_status == "published":
        data += 3
        reasons.append("+3 data: decision-maker named by the company, with a business email it publishes "
                       "(mail server verified)")
    elif researched and f.contact_role == "inbox" and f.contact_email_status == "role_address":
        data += 1
        reasons.append("+1 data: company inbox published on its own site (no named decision-maker email)")
    elif researched and f.contact_role != "inbox":
        data += 1
        reasons.append("+1 data: decision-maker named by the company (no published email)")

    total = max(0, min(100, fit + need + intent + data))
    disqualify = None
    if f.country != "US":
        disqualify = "Outside US target market"
    elif f.is_competitor:
        disqualify = "Company appears to sell software or AI work to clients (possible competitor)"
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


def lasting_grade(f: LeadFacts, thresholds: dict | None = None) -> str:
    """The grade this evidence still earns a month from now. A lead enters qualified only if it holds its grade that long: for a
    post over 30 days old that means past the 60-day step down, so nothing goes in today to come out tomorrow with a draft made
    and cancelled on the way, and a fresh post still has a month for its emails."""
    return score_lead(replace(f, now=(f.now or utcnow()) + timedelta(days=HOLD_DAYS)), thresholds).grade


TIERS = ("HOT", "WARM", "COLD", "UNQUALIFIED")


def lead_tier(lead) -> str:
    """HOT | WARM | COLD | UNQUALIFIED, explained by tier_reason: engagement first (an interested reply or a meeting
    is the strongest buying signal), then the score's grade, with inbound A/B leads hot."""
    if lead is None or lead.status == "disqualified":
        return "UNQUALIFIED"
    if lead.outreach_status in ("interested", "meeting"):
        return "HOT"
    if lead.grade == "A" or (lead.grade == "B" and lead.source == "inbound"):
        return "HOT"
    return {"B": "WARM", "C": "COLD"}.get(lead.grade or "", "UNQUALIFIED")


def tier_reason(lead) -> str:
    tier = lead_tier(lead)
    if lead is None or lead.status == "disqualified":
        return "disqualified"
    if lead.outreach_status in ("interested", "meeting"):
        return f"{lead.outreach_status}: the prospect engaged"
    return f"grade {lead.grade or '-'} (score {lead.score or 0})" + (" from an inbound inquiry" if tier == "HOT" and lead.source == "inbound" else "")
