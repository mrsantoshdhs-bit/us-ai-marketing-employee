"""Requirement extraction: map evidence text to our services and target industries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES

# Only firms that sell software or AI work to clients count as competitors (the owner's rule, 31 Oct 2026). A company selling
# its own product stays a prospect, even when that product uses AI agents.
_SAYS_IT_IS = r"(?:\bwe are|\bwe['\u2019]re|(?<!customer )(?<!client )(?<!partner )(?<!vendor )\bis) an? (?:[\w&]+[ ,-]+){0,3}?"
COMPETITOR_PATTERNS = [
    r"software (?:development|engineering) (?:agency|firm|company|services)",
    r"dev(?:elopment)? shop",
    r"we build (?:software|apps|products) for (?:our )?clients",
    r"(?:digital|software|ai) (?:consultancy|agency) (?:that|helping|building)",
    r"outsourc(?:ing|ed) (?:software|development)",
    r"staff augmentation",
    r"nearshore|offshore development",
    r"\b(?:applied[- ]ai|ai|ai transformation|digital transformation|automation) (?:advisory|consulting|consultancy|"
    r"implementation|transformation) (?:firm|company|agency|studio|partner)\b",
    r"\bai transformation firm\b",
    # These say who the company is, so they need it saying so: a post also names firms it sold to, replaced or wants experience of.
    _SAYS_IT_IS + r"(?:boutique )?(?:(?:it|tech|technology|software) consulting|outsourcing|outstaffing) (?:firm|company)\b",
    _SAYS_IT_IS + r"boutique (?:tech(?:nology)?|software|it|ai|digital|engineering|dev(?:elopment)?) "
    r"(?:agency|firm|company|consultancy|studio|shop)\b",
    r"\b(?:specializ\w+ in|we (?:do|offer|provide)) (?:ai/ml|ai|ml|machine learning) (?:and|&) software development for (?!our\b|your\b)",
]
_COMPETITOR_RE = re.compile("|".join(COMPETITOR_PATTERNS), re.IGNORECASE)
# What a company says it sells on its own website: software work, or developers for hire. A hiring post can describe the job
# instead, so these apply only to the company's own description.
OWN_SITE_COMPETITOR_PATTERNS = [
    r"\b(?:custom )?(?:crm|software|app) development (?:agency|company|firm|services)\b",
    r"\b(?:hire|access|connect with|join) (?:thousands of )?(?:elite |top(?:-tier)? |vetted |remote )*"
    r"(?:developers|engineers|software talent)\b",
    r"\bfractional (?:cto|cio|caio|chief)\b",
    r"\bcustom ai (?:development|solutions|systems|agents?)\b",
    r"\bdone[- ]for[- ]you,? by ai\b",
    r"\b(?:technology|tech|it) services (?:&|and) (?:it )?consulting\b",
    r"\boutstaffing (?:partner|company|firm|provider)\b",
    r"\bai automation agency\b|\bai consultancy\b|\bsoftware consultancy\b",
    r"\bai(?: agents?)? development (?:company|firm|agency|services)\b",
    r"\bcustom software development (?:for (?!our\b|your\b)|services\b|company\b)",
    r"\b(?:it|ai) consulting services\b",
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


# Words our keywords would catch that are no evidence of a need: a thing denied ("not LLMs"), what engineers use to write
# code ("agentic coding", "Claude Code"), and engineering terms that share a word with a service ("test automation").
_NOT_A_NEED_RE = re.compile(
    r"\b(?:not|no|non|without|beyond|rather than)[- ]+(?:an? )?(?:llms?|ai|gpt|genai|saas|crm|automation)\b"
    r"|\bbehind llms\b|\bwere not a saas\b|\bnot a saas\b"
    r"|\bnot an? (?:ml|ai|machine learning)\b[^.\n]{0,40}\b(?:position|role|job)\b"
    r"|\b(?:backed by|funded by|investors? (?:include|including|like)|investment from|raised (?:\S+ )?from)\b[^.\n;:]{0,100}?"
    r"\b(?:anthropic|openai|gpt|claude)\b"
    r"|\b(?:ex|former|formerly(?: at)?|alumni of|alums? of|previously at)[- ]+(?:anthropic|openai)\b"
    r"(?:[^.)\n;]{0,40}?\b(?:anthropic|openai)\b)?"
    r"|\bagentic (?:coding|engineering|development|tooling|practices|skills)\b"
    r"|\b(?:claude code|github copilot|coding (?:agents?|assistants?)|llm[- ]based tooling|ai[- ]assisted (?:coding|development)|"
    r"ai tools daily|ai coding tools?|ai ides?|claude or similar tools)\b"
    r"|\b(?:(?:claude|copilot|chatgpt|codex|windsurf|gemini)\s*(?:,|/|&|and|or)\s*)+(?:and |or )?cursor\b"
    r"|\bcursor\s*(?:,|/|&|and|or)\s*(?:and |or )?(?:claude|copilot|chatgpt|codex|windsurf|gemini)\b"
    r"|\bapply via mcp\b"
    r"|\b(?:container|kubernetes|k8s|workload|infrastructure|service|terraform) orchestration\b"
    r"|(?<!ai )(?<!agent )\b(?:workflow|data|pipeline|etl|batch|job)[- ]orchestration\b"
    r"|\b(?:pipelines?|data services?),? (?:and )?orchestration\b|\borchestration in resource[- ]constrained\b"
    r"|\b(?:apache )?airflow\b|\bdagster\b|\bprefect\b|\btemporal\b"
    r"|\b(?:test|testing|qa|cloud|infrastructure|infra|build|deployment|deploy|ci/?cd|security|network|browser|e2e|home|"
    r"factory|lab|industrial|warehouse|robotics) automation\b|\bautomation engineers?\b"
    r"|\bintegration test(?:s|ing)?\b|\bunit and integration\b",
    re.IGNORECASE,
)


def match_services(text: str | None) -> list[ServiceMatch]:
    """Return services evidenced by the text, strongest first."""
    if not text:
        return []
    lower = _NOT_A_NEED_RE.sub(" ", text.lower().replace("\u2019", "'"))
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


# "Agency" in a job post is usually a trait or a customer, not the company's industry.
_NOT_AN_INDUSTRY_RE = re.compile(
    r"\b(?:high|lots of|a lot of|more|personal|real|full|great|strong|much|sense of)[- ]agency\b"
    r"|\b(?:government|federal|state|public|regulatory|staffing|recruiting) agency\b")


def _about_the_industry(text: str) -> str:
    """What a text says of the company's industry: what it denies or merely mentions does not count ("we're not a SaaS company")."""
    return _NOT_AN_INDUSTRY_RE.sub(" ", _NOT_A_NEED_RE.sub(" ", text.lower().replace("’", "'")))


def industry_evidenced(industry: str | None, text: str | None) -> bool:
    """The text bears out this stored label, whichever other industry it may also mention."""
    ind = INDUSTRIES.get(industry or "")
    lower = _about_the_industry(text or "")
    return bool(ind) and any(_kw_re(kw).search(lower) for kw in ind.keywords)


def classify_industry(text: str | None, default: str | None = None) -> str | None:
    if not text:
        return default
    lower = _about_the_industry(text)
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
def competitor_in_post(post: str | None) -> str | None:
    """The phrase in a hiring post that shows the company sells what we sell, for a company whose own site is unread."""
    m = _COMPETITOR_RE.search(post or "")
    return m.group(0) if m else None


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
