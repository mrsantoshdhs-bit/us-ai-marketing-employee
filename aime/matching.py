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


@dataclass
class ServiceMatch:
    service_id: str
    score: int
    keywords: list[str] = field(default_factory=list)

    @property
    def strength(self) -> float:
        return min(1.0, self.score / 4.0)


def match_services(text: str | None) -> list[ServiceMatch]:
    """Return services evidenced by the text, strongest first."""
    if not text:
        return []
    lower = text.lower()
    matches: list[ServiceMatch] = []
    for svc in SERVICES.values():
        score, hits = 0, []
        for kw in svc.strong:
            if _kw_re(kw).search(lower):
                score += 2
                hits.append(kw)
        for kw in svc.weak:
            if _kw_re(kw).search(lower):
                score += 1
                hits.append(kw)
        # Weak-only matches are too generic to count as a requirement.
        if score >= 2 and any(k in svc.strong for k in hits) or score >= 3:
            matches.append(ServiceMatch(svc.id, score, hits))
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


_EXTERNAL_HELP_RE = re.compile(
    r"\bcontract(?:or|ors)\b|\bcontract (?:role|position|engineer|developer)s?\b|\bcontract-to-hire\b"
    r"|\bfreelanc\w*|\b(?:agency|consulting|development|implementation) partners?\b|\bfractional\b",
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
