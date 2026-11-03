"""What the AI may say about us (approved claims) and what it may never bring up (prohibited topics).

Stored in the database (kv_store "ai.claims", written by an administrator through `save`, with an audit event), over
the `ai` settings section's `claims` / `prohibited_topics`, over the code defaults. The defaults contain no invented
fact: one claim per service in our own service catalogue (aime.knowledge.services), naming the service and nothing
more. No client names, results, numbers, prices or dates: the owner adds those, if ever, in their own words.

A sentence in a proposal that states something about us ("we", "our", "us", the company name) must match an approved
claim; aime.ai.policy blocks one that does not. Matching is on content words: most of the sentence's content words
must appear in one approved claim. That is strict on purpose: a sentence a person would have to check is not let
through as approved."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from aime.knowledge.services import SERVICES

KV_KEY = "ai.claims"

# code: (label, words). Applied to text a model proposes; aime.automation.SENSITIVE is applied as well.
DEFAULT_PROHIBITED_TOPICS: dict[str, tuple[str, str]] = {
    "pricing": ("prices, rates and quotes", r"\b(pric\w*|cost\w*|rates?|fees?|quot(e|es|ed|ation)|\$\s?\d|usd|dollars?|"
                                            r"per (hour|month|year|seat|user)|hourly|cheap\w*|afford\w*|invoice\w*)\b"),
    "discount": ("discounts and free offers", r"\b(discount\w*|\d+\s?% off|percent off|free\b[^.!?\n]{0,25}\b(trial|pilot|months?|"
                                              r"weeks?|audit|consult\w*|setup|implementation|access|version|period|of charge)|"
                                              r"for free|complimentary|no charge|at no cost|waive\w*|special offer|promo\w*|coupon)\b"),
    "commitment": ("commitments made for us", r"\b((is|are|has been|have been|it'?s) (approved|confirmed|granted|agreed)|"
                                              r"you('ll| will) (get|receive|have)|we('ll| will) (give|grant|waive|include|throw in)|"
                                              r"(it'?s|that'?s) a deal|deal done)\b"),
    "contract": ("contracts, terms and commitments", r"\b(contract\w*|agreement\w*|(payment|contract|legal) terms|"
                                                     r"terms (and|&) conditions|sla|service level|liabilit\w*|"
                                                     r"indemn\w*|exclusiv\w*|lock(ed)?[- ]in|sign (here|today|now)|"
                                                     r"nda|msa|statement of work|sow)\b"),
    "guarantee": ("guarantees and promised results", r"\b(guarantee\w*|warrant\w*|promise\w*|money[- ]back|risk[- ]free|"
                                                      r"100\s?%|assur(e|ed|ance)|certain(ly)? (save|increase|double))\b"),
    "legal": ("legal matters", r"\b(legal\w*|lawyer\w*|attorney\w*|lawsuit\w*|sue|court|gdpr|ccpa|can-?spam|compliance "
                               r"advice|liable)\b"),
    "competitor": ("other vendors' shortcomings", r"\b(better than|unlike|cheaper than|worse than|beats?) [A-Z]\w+|"
                                                 r"\b(competitors?|rivals?) (are|is|can'?t|cannot|fail)"),
    "secrets": ("internal or system details", r"\b(system prompt|my instructions|api key|password|internal (notes?|"
                                              r"pricing|docs?))\b"),
}

_STOP = set("a an the and or of to for in on at by with from as is are be been our we us you your it its this that "
            "these those can will help helps helping offer offers provide provides build builds do does".split())
ABOUT_US = re.compile(r"\b(we|we'?re|we'?ve|our|ours|us)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    source: str  # default | settings | owner


def default_claims(settings) -> list[Claim]:
    company = settings.company_name
    return [Claim(f"service:{s.id}", f"{company} offers {s.name}.", "default") for s in SERVICES.values()]


def _topics(raw) -> dict[str, tuple[str, str]]:
    out = {}
    for item in raw or []:
        if isinstance(item, dict) and item.get("code") and item.get("words"):
            try:
                re.compile(item["words"])
            except re.error:
                continue  # a broken pattern from the settings is skipped, never the defaults
            out[str(item["code"])] = (str(item.get("label") or item["code"]), str(item["words"]))
    return out


def load(settings, session: Session | None = None) -> tuple[list[Claim], dict[str, tuple[str, str]]]:
    """(approved claims, prohibited topics {code: (label, regex)}). The defaults' topics always stay: settings and the
    owner can add topics, never remove the built-in ones."""
    try:
        section = settings.section("ai") or {}
    except Exception:
        section = {}
    claims = default_claims(settings)
    topics = dict(DEFAULT_PROHIBITED_TOPICS)
    extra = section.get("claims") if isinstance(section.get("claims"), list) else []
    claims += [Claim(f"settings:{i}", str(t), "settings") for i, t in enumerate(extra) if isinstance(t, str) and t.strip()]
    topics.update({k: v for k, v in _topics(section.get("prohibited_topics")).items() if k not in DEFAULT_PROHIBITED_TOPICS})
    if session is not None:
        from aime.db import kv_get

        try:
            stored = kv_get(session, KV_KEY) or {}
        except Exception:
            stored = {}
        claims += [Claim(f"owner:{i}", str(t), "owner") for i, t in enumerate(stored.get("claims") or [])
                   if isinstance(t, str) and t.strip()]
        topics.update({k: v for k, v in _topics(stored.get("prohibited_topics")).items() if k not in DEFAULT_PROHIBITED_TOPICS})
    return claims, topics


def save(session: Session, claims: list[str], prohibited_topics: list[dict] | None, actor: str) -> None:
    """The owner's approved claims and extra prohibited topics, replacing the stored ones. Audited."""
    from aime.automation import audit
    from aime.db import kv_set

    clean = [c.strip()[:300] for c in claims if isinstance(c, str) and c.strip()][:100]
    kv_set(session, KV_KEY, {"claims": clean, "prohibited_topics": list(prohibited_topics or [])[:50]})
    audit(session, actor, "ai_claims_saved", "ai.claims", f"{len(clean)} approved claims")


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1}


def matching_claim(sentence: str, claims: list[Claim], company: str = "") -> Claim | None:
    words = content_words(sentence) - content_words(company)
    if not words:
        return claims[0] if claims else None  # nothing stated beyond "we"
    best, score = None, 0.0
    for c in claims:
        cw = content_words(c.text) - content_words(company)
        if not cw:
            continue
        s = len(words & cw) / len(words)
        if s > score:
            best, score = c, s
    return best if score >= 0.75 else None


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text or "") if s.strip()]


def unapproved_statements(text: str, claims: list[Claim], company: str = "") -> list[str]:
    """Sentences that speak about us and match no approved claim. A question, a thanks or an offer to talk is no
    statement of fact; the checks for numbers, names and dates (aime.ai.policy) cover the rest."""
    out = []
    pleasant = re.compile(r"^(thanks?|thank you|happy to|glad to|would you|could we|shall we|let us know|let me know|"
                          r"we'?d (love|be happy|be glad) to|we can (set up|arrange|find|schedule) a (time|call|meeting)|"
                          r"we look forward|looking forward|best|regards|kind regards|hi|hello|dear)\b", re.I)
    company_re = re.compile(rf"\b{re.escape(company)}\b", re.I) if company else None
    for s in sentences(text):
        about_us = ABOUT_US.search(s) or (company_re and company_re.search(s))
        if not about_us or s.rstrip().endswith("?") or pleasant.search(s):
            continue
        if matching_claim(s, claims, company) is None:
            out.append(s[:200])
    return out
