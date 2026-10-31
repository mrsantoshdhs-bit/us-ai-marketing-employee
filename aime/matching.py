"""Requirement extraction: map evidence text to our services and target industries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES

COMPETITOR_PATTERNS = [
    r"software (?:development|engineering) (?:agency|firm|company|services)",
    r"dev(?:elopment)? shop",
    r"we build (?:software|apps|products) for (?:our )?clients",
    r"(?:digital|software|ai) (?:consultancy|agency) (?:that|helping|building)",
    r"outsourc(?:ing|ed) (?:software|development)",
    r"staff augmentation",
    r"nearshore|offshore development",
]
_COMPETITOR_RE = re.compile("|".join(COMPETITOR_PATTERNS), re.IGNORECASE)
# What a company says it sells on its own website: AI agents or workers, an agent platform, or developers for
# hire. A hiring post can describe the job instead, so these apply only to the company's own description.
OWN_SITE_COMPETITOR_PATTERNS = [
    r"\bai (?:agents?|workers?|employees?|teammates?) (?:that|which|for|to)\b",
    r"\b(?:build|create|source|deploy|launch|design) (?:your own |a |an |custom |autonomous )*ai (?:agents?|workers?|"
    r"employees?)\b",
    r"\bagentic ai\b",
    r"\bai agent (?:platform|builder|infrastructure|framework|marketplace)\b",
    r"\b(?:custom )?(?:crm|software|app) development (?:agency|company|firm|services)\b",
    r"\b(?:hire|access|connect with|join) (?:thousands of )?(?:elite |top(?:-tier)? |vetted |remote )*"
    r"(?:developers|engineers|software talent)\b",
    *COMPETITOR_PATTERNS,
]
_OWN_SITE_COMPETITOR_RE = re.compile("|".join(OWN_SITE_COMPETITOR_PATTERNS), re.IGNORECASE)


@lru_cache(maxsize=512)
def _kw_re(keyword: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])")


# Words that say "automate" without saying what. They are evidence of business process automation only when the
# text names nothing more specific: "lead generation automation" asks for lead generation and "automate our sales
# follow-ups" for sales automation, not for back-office process work.
GENERIC_KEYWORDS = frozenset({"automate", "automation", "automating"})


@dataclass
class ServiceMatch:
    service_id: str
    score: int
    keywords: list[str] = field(default_factory=list)

    @property
    def strength(self) -> float:
        return min(1.0, self.score / 4.0)

    @property
    def confidence(self) -> str:
        """high: two specific phrases, or one with supporting words; medium: one specific phrase; low: generic."""
        specific = [k for k in self.keywords if k in SERVICES[self.service_id].strong and k not in GENERIC_KEYWORDS]
        if len(specific) >= 2 or specific and self.score >= 4:
            return "high"
        return "medium" if specific else "low"

    def explain(self) -> str:
        return f"{SERVICES[self.service_id].name} ({self.confidence}): mentions {', '.join(self.keywords[:4])}"


def match_services(text: str | None) -> list[ServiceMatch]:
    """Return services evidenced by the text, strongest first."""
    if not text:
        return []
    lower = text.lower()
    found: dict[str, list[tuple[str, int, list[tuple[int, int]]]]] = {}
    for svc in SERVICES.values():
        for kw, weight in [(k, 2) for k in svc.strong] + [(k, 1) for k in svc.weak]:
            spans = [m.span() for m in _kw_re(kw).finditer(lower)]
            if spans:
                found.setdefault(svc.id, []).append((kw, weight, spans))

    def inside_another_service(sid: str, spans: list[tuple[int, int]]) -> bool:
        # every mention is part of a longer phrase of another service: "automation" in "sales automation"
        longer = [(a, b) for other, hits in found.items() if other != sid for _, _, os in hits for a, b in os]
        return all(any(a <= s and e <= b and b - a > e - s for a, b in longer) for s, e in spans)

    counted = {sid: [(kw, w) for kw, w, spans in hits if not inside_another_service(sid, spans)]
               for sid, hits in found.items()}
    specific = {sid for sid, hits in counted.items() if any(w == 2 and kw not in GENERIC_KEYWORDS for kw, w in hits)}
    matches: list[ServiceMatch] = []
    for sid, hits in counted.items():
        score = sum(w for _, w in hits)
        # Weak-only matches are too generic to count as a requirement, and a generic "automate" yields to a service
        # the text names specifically.
        if not (score >= 2 and any(w == 2 for _, w in hits) or score >= 3):
            continue
        generic_only = sid not in specific and any(kw in GENERIC_KEYWORDS for kw, _ in hits)
        if generic_only and specific - {sid}:
            continue
        matches.append(ServiceMatch(sid, score, [kw for kw, _ in hits]))
    matches.sort(key=lambda m: (-m.score, m.service_id))
    return matches


def classify_industry(text: str | None, default: str | None = None) -> str | None:
    if not text:
        return default
    lower = text.lower()
    best, best_hits = default, 0
    for ind in INDUSTRIES.values():
        hits = sum(1 for kw in ind.keywords if _kw_re(kw).search(lower))
        if hits > best_hits:
            best, best_hits = ind.id, hits
    return best


def detect_competitor(text: str | None) -> bool:
    return bool(text and _COMPETITOR_RE.search(text))


def competitor_evidence(own_words: str | None) -> str | None:
    """The phrase in a company's own description that shows it sells what we sell, or None."""
    m = _OWN_SITE_COMPETITOR_RE.search(own_words or "")
    return m.group(0) if m else None


# The company asks for help. "Contractors" alone is usually someone's customers or trade: "software for HVAC contractors", "a
# defense contractor", "(employees + contractors)".
_EXTERNAL_HELP_RE = re.compile(
    r"\b(?:hiring|hire|seeking|looking for|need|open to|accepting|considering|work with|welcome)\s+(?:\w+\s+){0,2}"
    r"(?:contractors?|freelancers?)\b"
    r"|\b(?:contractors?|freelancers?)\s+(?:are\s+)?(?:welcome|ok|okay|considered|accepted|wanted|needed)\b"
    r"|\bcontract (?:role|position|engineer|developer|work|basis)s?\b|\bcontract-to-hire\b|\b1099\b"
    r"|\b(?:part[- ]time|full[- ]time)\s*(?:/|or|,)?\s*contract\b|\bcontract\s*(?:/|or)\s*full[- ]time\b"
    r"|\bfreelance (?:role|position|engineer|developer|work|basis)s?\b"
    r"|\b(?:agency|consulting|development|implementation) partners?\b"
    r"|\b(?:hiring|hire|seeking|looking for|need)\s+(?:an?\s+)?fractional\b",
    re.IGNORECASE,
)


def seeks_external_help(text: str | None) -> bool:
    """The company itself says it hires contractors, freelancers, or partners (evidence, not a guess)."""
    return bool(text and _EXTERNAL_HELP_RE.search(text))


def describe_requirements(matches: list[ServiceMatch], limit: int = 3) -> list[str]:
    out = []
    for m in matches[:limit]:
        svc = SERVICES[m.service_id]
        out.append(f"{svc.name}: mentions {', '.join(m.keywords[:4])}")
    return out
