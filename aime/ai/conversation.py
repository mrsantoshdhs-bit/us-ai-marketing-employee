"""The conversation engine (Option B, Phase 3): what a prospect has told us, where the conversation stands, the next
step, and a draft answer that a person approves.

It runs on every new reply the inbox rules (aime.reply_intel) have already acted on, through the hand-off hook in
`reply_intel.process_pending`, and only reads what they decided. The rules stay the floor: an opt-out, a complaint, a
legal threat or a bounce is theirs, settled before this engine looks, and nothing here can downgrade it; an
out-of-office, an automatic reply or a calendar notice is not a person and is skipped.

For a reply a person wrote, in order:

1. the Conversation row (table conversations) is found or made, and the state moved along aime.ai.state's arrows;
2. facts are read into conversation_facts, each with the reply's id and the prospect's own words as evidence: rules
   first (the classifier's PRICING, NO_BUDGET and MEETING words, and careful patterns for a stated budget, a timeline,
   who decides, requirements, objections, questions and concerns); then, when a model is configured and the run's
   budget allows, the model's reading as an addition: each of its facts needs a quote found word for word in the
   email, goes through the policy engine (aime.ai.policy), is stored with source "ai" and never replaces a rule fact;
3. the rolling summary is rebuilt from cited facts only (every line names the reply it came from);
4. the next-best-action is chosen by rules: escalate, hand over (prices go to the Sales Manager), offer a meeting,
   handle an objection from the owner's playbook (aime.ai.playbook), answer a question from an approved claim, ask the
   next one or two missing discovery questions (never an interrogation), nurture, or nothing;
5. a draft is composed from fixed wording, playbook points and approved claims, with no model needed; a model may
   reword it when the settings allow, and either text goes through the policy engine; the draft and its verdict are
   recorded in ai_decisions with the facts it used and the reasons;
6. escalations raise the Sales Manager's attention through `needs_handover`, the hook the handover package reads.

The mode (settings section "conversation", aime.settings_store) decides how far it goes: off (default: nothing runs),
shadow (1-6 are recorded, without alerts; no draft changes), drafts (as shadow, plus: a draft the policy engine sends to
HUMAN_REVIEW may replace the template's suggested answer, which still waits for a person's approval; a BLOCK leaves the
template's suggestion as it was). Nothing here sends, books or approves: REQUIRE_HUMAN_APPROVAL is never read as off,
and the send gate (aime.agents.sales_workflow) is unchanged.

`followup_context` gives the sequence's later steps a line that refers to what the prospect said, under the same
policy check (mode drafts only); the stop rules of the sequence are untouched."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.ai import claims as claims_mod
from aime.ai import decisions, playbook, prompts
from aime.ai import state as state_mod
from aime.ai.escalation import Escalation, assess
from aime.ai.guard import Floor, injection_signals
from aime.ai.policy import PolicyContext, PolicyDecision, Verdict, evaluate
from aime.models import AIDecision, Conversation, ConversationFact, FollowUp, InboundEmail, Lead
from aime.timeutil import utcnow

log = logging.getLogger("aime.ai.conversation")

AGENT = "conversation"
TASK_TURN = "conversation_turn"
TASK_STATE = "conversation_state"
TASK_FACTS = "fact_extraction"
TASK_DRAFT = "conversation_draft"
TASK_HANDOVER = "handover_request"
TASK_FOLLOWUP = "followup_context"
MODES = ("off", "shadow", "drafts")
GENERATOR = "AI conversation draft (template + playbook)"
CONTEXT_GENERATOR = "AI conversation context + template"  # starts with "AI": no campaign authorization sends it (aime.automation)
NOT_A_PERSON = ("bounce", "calendly", "out_of_office")
FACT_KINDS = ("requirement", "budget", "timeline", "stakeholder", "authority", "objection", "question", "meeting_intent",
              "concern")
ACTIONS = ("answer_question", "ask_discovery", "handle_objection", "offer_meeting", "nurture", "hand_over", "escalate", "none")
SLOW_SECONDS = 30  # a slower model answer ends the model's part for the rest of the run
PLACEHOLDER_BLOCK = "Write your answer before approving"

# --------------------------------------------------------------------------------------------------- settings


def config(settings) -> dict:
    """The effective "conversation" section (the defaults when none is saved, or when it cannot be read)."""
    from aime import settings_store as ss

    try:
        return ss.get(settings, "conversation")
    except Exception:  # noqa: BLE001 -- a broken section never turns the engine on
        return ss.ConversationSection().model_dump(mode="json")


def mode(settings) -> str:
    m = str(config(settings).get("mode") or "off")
    return m if m in MODES else "off"


# --------------------------------------------------------------------------------------------------- language

_LANG_WORDS = {
    "es": re.compile(r"\b(el|los|las|que|por|para|con|una|gracias|hola|nosotros|necesitamos|reuni[oó]n|precio|"
                     r"podemos|tenemos|queremos|usted|estamos|saludos|cu[aá]nto|empresa)\b", re.I),
    "de": re.compile(r"\b(der|die|das|und|nicht|wir|ich|ist|mit|f[uü]r|bitte|danke|hallo|k[oö]nnen|termin|haben|"
                     r"brauchen|unser|ihr|gr[uü](ß|ss)e|wie|viel)\b", re.I),
    "fr": re.compile(r"\b(le|les|et|nous|vous|est|pour|avec|merci|bonjour|une|des|pas|notre|avons|besoin|"
                     r"rendez-vous|cordialement|combien|pouvons)\b", re.I),
    "hi": re.compile(r"\b(mujhe|hamein|hume|chahiye|kya|aap|hai|hain|nahi|nahin|kripya|dhanyavad|ke liye|baat|"
                     r"karna|karenge|milte)\b", re.I),
}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def language_of(text: str) -> str:
    """A rough guess: "en", "es", "de", "fr" or "hi". Devanagari script, or clearly more of one language's everyday
    words than English ones, decides; anything unclear is English."""
    from aime.reply_intel import _ENGLISH_WORDS

    text = text or ""
    if len(_DEVANAGARI.findall(text)) >= 3:
        return "hi"
    english = len(_ENGLISH_WORDS.findall(text))
    best, count = "en", 0
    for code, words in _LANG_WORDS.items():
        n = len(words.findall(text))
        if n > count:
            best, count = code, n
    return best if count >= 3 and count > english else "en"


# --------------------------------------------------------------------------------------------------- facts by rules

_AMOUNT = (r"(?:[$€£₹]\s?\d[\d,.]*\s?(?:k|m|mm|thousand|million|lakhs?|crores?)?\b"
           r"|\b(?:rs\.?|inr|usd|eur|gbp)\s?\d[\d,.]*\s?(?:k|m|thousand|million|lakhs?|crores?)?\b"
           r"|\b\d[\d,.]*\s?(?:k|thousand|million|lakhs?|crores?)?\s?(?:usd|dollars|eur|euros?|gbp|pounds|inr|rupees)\b"
           r"|\b\d[\d,.]*\s?(?:k|thousand|lakhs?)\b)")
AMOUNT = re.compile(_AMOUNT, re.I)
BUDGET_WORDS = re.compile(r"\b(budget\w*|spend|spending|allocated|set aside|afford|up to|no more than|ceiling|max(imum)?|"
                          r"range|invest|could do|can do|around|about|roughly)\b", re.I)


def _timeline_re():
    from aime.reply_intel import _MONTH_WORD

    when = (rf"(?:\d{{1,2}}\s+(?:days?|weeks?|months?)|q[1-4](?:\s+20\d\d)?|(?:the\s+)?(?:next|this|coming)\s+(?:month|quarter|year)|"
            rf"(?:the\s+)?(?:end|start|beginning|middle)\s+of\s+(?:the\s+)?(?:month|quarter|year|q[1-4]|{_MONTH_WORD})|"
            rf"{_MONTH_WORD}\b(?:\s+20\d\d)?|(?:early|mid|late)[ -]{_MONTH_WORD}\b|20\d\d|spring|summer|autumn|fall|winter)")
    return re.compile(rf"\b(?:(?:by|before|until|within|in|from|starting|around|after|for)\s+(?:the\s+)?{when}"
                      r"|(?:as soon as possible|asap|urgent(?:ly)?|right away|immediately|no rush|this (?:quarter|year)|"
                      r"next (?:quarter|year)|go(?:ing)?[- ]live\b[^.!?\n]{0,30}))", re.I)


TIMELINE = _timeline_re()
_ROLE = (r"(ceo|cto|cfo|coo|cio|cmo|cro|vp( of [a-z]+)?|vice president( of [a-z]+)?|head of [a-z]+|(it |sales |marketing |operations |"
         r"engineering |finance |procurement |purchasing )?(director|manager|lead)|founder|co-?founder|owner|partner|president|"
         r"procurement( team| manager| department)?|purchasing( team| manager| department)?|legal( team| department)?|board|"
         r"finance( team| department)?|it (team|department)|managing director)")
SELF_ROLE = re.compile(rf"\b(i'?m|i am|as)\s+(the\s+|a\s+|an\s+|our\s+)?{_ROLE}\b", re.I)
HEADS = re.compile(r"\bi (head|lead|run|manage|look after) (up )?(the |our )?(procurement|purchasing|it|engineering|operations|"
                   r"finance|marketing|sales|technology|product)\b", re.I)
DECIDER = re.compile(r"\b(i (make|own|sign off on|take) (the )?(final )?(decisions?|call)|i'?m the (final )?decision[- ]?maker|"
                     r"i am the (final )?decision[- ]?maker|(it'?s|it is) my (call|decision)|i (can|will) decide|i decide)\b", re.I)
NEEDS_OTHERS = re.compile(rf"\b(need(s)? (to )?(run (it|this) (by|past)|approval from|sign[- ]off from|to check with|to involve|to loop in|"
                          rf"to get (the )?(ok|okay|approval|sign[- ]off) from)|(the )?(decision|sign[- ]off|approval) (is|will be|would be|rests?|sits?) "
                          rf"(made )?(with|by)|(loop|looping|bring|bringing) in (my|our|the)|(check|talk|speak) (with|to) (my|our) {_ROLE}|"
                          rf"(my|our) {_ROLE} (would|will|needs? to|has to|must) (decide|approve|sign|be involved|join|see)|"
                          rf"(my|our|the) {_ROLE}( team)? (needs?|wants?|requires?|insists?|has to|must)\b)[^.!?\n]{{0,60}}", re.I)
PROBLEM = re.compile(r"\b(we (struggle|are struggling|have trouble|have a problem|keep losing|waste|spend too much time)|"
                     r"(our|the) (main |biggest |real )?(problem|challenge|pain( point)?|issue|bottleneck) (is|are)|"
                     r"(we|i) need (help|to [a-z]+|a |an |some|someone|better|more)|we('re| are) looking (for|to)|we want to|"
                     r"(we|i)'?d like to|"
                     r"looking for (a|an|someone|help))\b", re.I)
OUTCOME = re.compile(r"\b((the |our )?(goal|aim|objective|target) (is|would be)|so (that|we can)|in order to|"
                     r"we (want|hope|aim) to (reduce|increase|improve|cut|save|automate|grow|speed up|eliminate))\b", re.I)
SCOPE = re.compile(r"\b(\d+\s+(users?|people|staff|employees|seats|agents|reps|locations|stores|offices|sites|branches|"
                   r"clients|customers|teams?)|team of \d+|across (all |our |\d+ )\w+|company[- ]wide)\b", re.I)
TOOLS = re.compile(r"\b(we (currently |still )?(use|run on|rely on|are using|work in|track (it|everything|this) in)|"
                   r"we'?re (on|using)|our current (system|tool|crm|platform|setup|stack|vendor)|"
                   r"(in|on|with) (spreadsheets?|excel|google sheets|paper))\b", re.I)
OBJECTIONS: dict[str, re.Pattern] = {
    "price": re.compile(r"\b(too expensive|too pricey|can'?t afford|cannot afford|out of (our )?(price )?range|"
                        r"cheaper|over (our )?budget|budget is tight)\b", re.I),
    "timing": re.compile(r"\b(not (the )?right time|bad time|not now|maybe later|too busy|swamped|"
                         r"not a priority (right now|at the moment|this (quarter|year)))\b", re.I),
    "competitor": re.compile(r"\b(already (use|have|work with|using|working with|partnered with|signed with)|"
                             r"(we have|we've got) (a|an) (vendor|agency|partner|provider|supplier)|in-?house (team|developers|devs))\b", re.I),
    "no_need": re.compile(r"\b(don'?t (really )?need|do not need|no need for|not (something )?we need|we'?re (all )?set|"
                          r"not a (fit|priority)|not relevant)\b", re.I),
    "authority": re.compile(r"\b(not my (call|decision|department|area)|not the right person|(i|we) don'?t (handle|decide|make (the )?decisions?))\b", re.I),
    "trust": re.compile(r"\b(never heard of you|how did you get my|who are you|is this (legit|a scam|real)|scam|"
                        r"references?|case stud(y|ies)|testimonials?|proof|track record|portfolio)\b", re.I),
}
CONCERN = re.compile(r"\b(concern(ed|s)?|worr(y|ied|ies)|security|privacy|data protection|compliance|gdpr|"
                     r"integrat(e|es|ion|ions)|downtime|risk(s|y)?)\b", re.I)
CAPABILITY = re.compile(r"\b((do|can|could|does|would) (you|your team|y'?all)|do you (offer|have|do|build|support|work)|"
                        r"what (services|kind of|sort of)|is (it|this) possible)\b", re.I)
CONTRACT_OR_LEGAL = re.compile(r"\b(contract\w*|agreement\w*|terms and conditions|(payment|contract|legal) terms|nda|msa|sow|"
                               r"statement of work|legal\w*|lawyer\w*|attorney\w*|invoice (terms|dispute)|refund\w*|guarantee\w*|"
                               r"warrant(y|ies)|payment (terms|schedule|plan)|liabilit\w*|sla|w-?9|security questionnaire|"
                               r"vendor (form|onboarding))\b", re.I)  # "automate our invoices" is a requirement, not a contract
PRICE_ASK = re.compile(r"\b(charge|charges|charging|rate card|day rate|hourly rate|what does it run|pay for (it|this))\b", re.I)
BUSY = re.compile(r"\b(busy|swamped|short on time|no time|keep it short|in a nutshell|tl;?dr|bottom line|"
                  r"quick version|one line|in brief|briefly)\b", re.I)
REQUIREMENT_SLOTS = {"problem": PROBLEM, "outcome": OUTCOME, "scope": SCOPE, "current tools": TOOLS}
SLOT_LABEL = {"problem": "problem", "outcome": "outcome", "scope": "scope", "current tools": "current_tools"}

# The discovery schema: what a good first call needs, asked one or two at a time. Budget is never asked by email: it
# is the Sales Manager's conversation.
DISCOVERY: list[tuple[str, str]] = [
    ("problem", "What prompted you to look at this now, and what would you most like to fix?"),
    ("current_tools", "What do you use for this today?"),
    ("timeline", "Is there a date you are working towards?"),
    ("decision", "Who else would be involved in deciding?"),
    ("outcome", "What would a good result look like for you?"),
    ("scope", "Roughly how many people would use it?"),
]
QUALIFIED_NEEDS = ("problem", "timeline", "decision")


@dataclass
class Fact:
    kind: str
    value: str
    quote: str
    confidence: float
    source: str = "rule"
    decision: PolicyDecision | None = None
    raw: dict = field(default_factory=dict)

    @property
    def slot(self) -> str | None:
        return slot_of(self.kind, self.value)


def slot_of(kind: str, value: str) -> str | None:
    if kind == "requirement":
        head = (value or "").split(":", 1)[0].strip().lower().removeprefix("ai reading ").strip()
        return SLOT_LABEL.get(head, "problem" if head else None)
    if kind == "timeline":
        return "timeline"
    if kind in ("authority", "stakeholder"):
        return "decision"
    if kind == "budget":
        return "budget"
    return None


def _sentences(text: str) -> list[str]:
    return [s for s in claims_mod.sentences(re.sub(r"[ \t]+", " ", text or "")) if len(s) >= 3]


def _negated(sentence: str, at: int) -> bool:
    from aime.reply_intel import NEGATED

    return bool(NEGATED.search(sentence[:at]))


def _clip(text: str, n: int = 300) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def rule_facts(text: str, row: InboundEmail | None = None) -> list[Fact]:
    """What the prospect states, by the rules, each with the sentence it is in. Careful on purpose: an amount counts as
    a budget only next to a budget word; a meeting phrase that is negated is no meeting wish; a timeline inside a
    meeting request ("can we talk next week") is when to meet, not the project's timeline."""
    from aime.reply_intel import MEETING, NO_BUDGET, PRICING, check_back_when

    out: list[Fact] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, quote: str, conf: float) -> None:
        key = (kind, re.sub(r"\W+", " ", value.lower()).strip())
        if key not in seen:
            seen.add(key)
            out.append(Fact(kind, _clip(value, 200), _clip(quote), conf))

    for s in _sentences(text):
        meeting = MEETING.search(s)
        meeting_wish = bool(meeting and not _negated(s, meeting.start()))
        if meeting_wish:
            add("meeting_intent", "asks to meet", s, 0.9)
        if s.rstrip().endswith("?"):
            add("question", s, s, 0.9)
        for m in AMOUNT.finditer(s):
            if BUDGET_WORDS.search(s) or PRICING.search(s):
                add("budget", f"stated: {m.group(0).strip()}", s, 0.9)
        nb = NO_BUDGET.search(s)
        if nb:
            add("budget", f"constrained: {nb.group(0).strip()}", s, 0.85)
        if not meeting_wish and not nb:  # "our budget is tight this year" names no project date
            for m in TIMELINE.finditer(s):
                add("timeline", m.group(0).strip(), s, 0.75)
        role = SELF_ROLE.search(s)
        heads = HEADS.search(s)
        if heads:
            add("authority", f"says they {heads.group(0).split(None, 1)[-1].strip()}", s, 0.85)
        if role:
            add("authority", f"says they are {role.group(0).split(None, 1)[-1].strip()}", s, 0.85)
        if DECIDER.search(s):
            add("authority", "says they decide", s, 0.85)
        others = NEEDS_OTHERS.search(s)
        if others:
            add("stakeholder", others.group(0).strip(), s, 0.8)
        for label, pattern in REQUIREMENT_SLOTS.items():
            m = pattern.search(s)
            if m and not _negated(s, m.start()) and not s.rstrip().endswith("?"):
                add("requirement", f"{label}: {s}", s, 0.75)
        for code, pattern in OBJECTIONS.items():
            m = pattern.search(s)
            if m:
                add("objection", code, s, 0.8)
        if nb and not any(f.kind == "objection" and f.value == "price" for f in out):
            add("objection", "price", s, 0.8)
        c = CONCERN.search(s)
        if c:
            add("concern", s, s, 0.7)
    if row is not None:
        when = check_back_when(row)
        if when:
            add("timeline", f"check back {when['words']}", text, 0.85)
    return out


# --------------------------------------------------------------------------------------------------- facts by a model


class ExtractedFact(BaseModel):
    kind: str
    value: str = Field(max_length=300)
    quote: str = Field(max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v):
        return v.strip().lower().replace(" ", "_").replace("-", "_") if isinstance(v, str) else v

    @field_validator("value", "quote", mode="before")
    @classmethod
    def _short(cls, v):
        return str(v or "")[:300] if not isinstance(v, str) else v[:600]


class FactsOut(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)
    upset: bool = False
    asks_for_human: bool = False

    @field_validator("facts", mode="before")
    @classmethod
    def _facts(cls, v):
        if not isinstance(v, list):
            return []
        return [x for x in v if isinstance(x, dict) and str(x.get("kind", "")).strip().lower().replace(" ", "_") in FACT_KINDS][:8]


class PolishOut(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)


_AI_FIELD = {"requirement": "requirements", "budget": "budget", "timeline": "timeline", "stakeholder": "stakeholders",
             "authority": "stakeholders", "objection": "objection", "meeting_intent": "meeting_intent"}


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").lower().replace("’", "'").split())


def ai_fact_candidates(out: FactsOut, text: str, lead_id: int, pctx: PolicyContext) -> list[Fact]:
    """The model's facts that survive: the quote is in the email word for word, the value's words are the quote's, and
    the policy engine does not block it. Its confidence is kept (and shown); its words are marked as its reading."""
    kept: list[Fact] = []
    said = _norm(text)
    for f in out.facts:
        quote = _clip(f.quote, 300)
        if len(_norm(quote)) < 8 or _norm(quote).rstrip("…") not in said:
            continue
        words = claims_mod.content_words(f.value)
        if not words or len(words & claims_mod.content_words(quote)) / len(words) < 0.6:
            continue
        if f.kind in _AI_FIELD:
            raw = {"kind": "field_update", "lead_id": lead_id, "field": _AI_FIELD[f.kind],
                   "value": f.value if f.kind != "meeting_intent" else True, "evidence": quote, "confidence": f.confidence}
        else:
            raw = {"kind": "internal_note", "lead_id": lead_id, "text": f"{f.kind}: {f.value}", "confidence": f.confidence}
        d = evaluate(raw, pctx)
        if f.kind == "objection":
            from aime.ai.schemas import OBJECTIONS as CODES

            code = f.value.strip().lower().replace(" ", "_")
            value = code if code in CODES and code != "none" else "other"
        else:
            value = f"AI reading: {f.value}"
        kept.append(Fact(f.kind, _clip(value, 200), quote, round(min(f.confidence, 0.95), 2), "ai", d, raw))
    return kept


# --------------------------------------------------------------------------------------------------- next best action


@dataclass
class Plan:
    action: str
    why: list[str] = field(default_factory=list)
    questions: list[tuple[str, str]] = field(default_factory=list)
    objection: str | None = None
    claim: claims_mod.Claim | None = None
    escalations: list[Escalation] = field(default_factory=list)
    pricing: bool = False
    meeting: bool = False
    offers: list[str] = field(default_factory=list)
    target_state: str | None = None

    @property
    def drafts(self) -> bool:
        return self.action in ("answer_question", "ask_discovery", "handle_objection", "offer_meeting", "hand_over")


ESCALATE_CODES = frozenset({"upset", "human_requested", "injection_suspected", "manager_condition", "unanswered_ai_turns",
                            "safety_floor", "legal_or_contract", "language", "changed_mind", "outside_playbook",
                            "references_asked", "model_upset", "model_asks_for_human"})


def _service_claim(question: str, claims: list[claims_mod.Claim]) -> claims_mod.Claim | None:
    """The approved claim about the service a capability question names ("do you build AI agents?")."""
    from aime.knowledge.services import SERVICES

    low = question.lower()
    for s in SERVICES.values():
        names = {s.name.lower(), *(k.lower() for k in s.strong)}  # the specific words, not the generic ones
        if any(re.search(rf"\b{re.escape(n)}\b", low) for n in names if len(n) > 2):
            for c in claims:
                if c.id == f"service:{s.id}":
                    return c
    return None


def known_slots(facts: list[Any]) -> set[str]:
    out = set()
    for f in facts:
        if getattr(f, "status", "open") in ("withdrawn",):
            continue
        slot = slot_of(f.kind, f.value)
        if slot:
            out.add(slot)
    return out


def plan_next(*, floor: Floor, text: str, facts: list[Fact], all_facts: list[Any], current: str, language: str,
              escalations: list[Escalation], pb: dict, claims: list[claims_mod.Claim], cfg: dict, asked_before: dict[str, int],
              discovery_turns: int, booked: bool, suppressed: bool, check_back: bool) -> Plan:
    """The next step, by rules. The model never chooses it: its facts only fill the discovery schema (so nothing is asked
    twice), and its caution (upset, asks for a person) is followed."""
    cat = floor.category
    p = Plan("none", escalations=list(escalations))
    kinds = {f.kind for f in facts}
    objections = [f.value for f in facts if f.kind == "objection" and f.source == "rule"]
    questions = [f.value for f in facts if f.kind == "question"]
    meeting = floor.intent == "meeting_request" or "meeting_intent" in kinds
    from aime.reply_intel import PRICING

    pricing = bool(floor.intent == "pricing_request" or PRICING.search(text or "") or PRICE_ASK.search(text or "")
                   or "budget" in kinds)
    contract = CONTRACT_OR_LEGAL.search(text or "")
    if contract or floor.legal:
        p.escalations.append(Escalation("legal_or_contract", f'they mention "{contract.group(0) if contract else "legal language"}": '
                                        "contracts, terms and legal matters are the Sales Manager's"))
    if "trust" in objections and not pb.get("trust", {}).get("enabled"):
        p.escalations.append(Escalation("references_asked", "they ask for references, case studies or proof: only the owner's "
                                        "approved material can be shared, so a person answers"))
    approved_languages = {str(x).lower() for x in cfg.get("approved_languages") or ["en"]}
    if language not in approved_languages:
        p.escalations.append(Escalation("language", f"the reply is in {language}, which is not approved for drafts: a person answers"))
    if current == "closed_lost" and cat in ("interested", "question", "referral") or (current == "closed_lost" and meeting):
        p.escalations.append(Escalation("changed_mind", "they said no earlier and now write with interest: a person reopens it"))
    if suppressed or current == "opted_out":
        p.action, p.why = "none", ["the contact is suppressed or opted out: the rules decide, a person reads it"]
        return p
    if current == "handed_over":
        p.action, p.why = "escalate", ["the conversation is already with the Sales Manager: they answer"]
        return p
    hard = [e for e in p.escalations if e.code in ESCALATE_CODES]
    if hard:
        p.action, p.why = "escalate", [e.why for e in hard]
        return p
    if booked:
        p.action, p.why = "none", ["a meeting is booked: the template's confirmation stays, and a person reads the reply"]
        return p
    if cat == "not_interested":
        p.action = "nurture" if check_back else "none"
        p.target_state = "nurture" if check_back else "closed_lost"
        p.why = ["they are not interested" + (": a check-back at the time they named" if check_back else ": nothing more is written")]
        return p
    if pricing:
        p.action, p.pricing, p.meeting = "hand_over", True, True
        p.why = ["prices, budgets and discounts are the Sales Manager's: the draft states no price and offers a call with them"]
        if objections:
            p.objection = objections[0]
        p.target_state = "scheduling"
        return p
    if cat == "objection" and check_back:
        p.action, p.target_state = "nurture", "nurture"
        p.why = ["they asked to hear back later: the template's check-back answer stays"]
        return p
    if meeting:
        p.action, p.meeting, p.target_state = "offer_meeting", True, "scheduling"
        p.why = ["they ask to meet: offer the booking link (or ask for times)"]
        return p
    if cat == "interested" and objections == ["timing"]:
        objections = []  # "interested, but swamped this week": busy, not a no; a link, not questions (below)
    if objections:
        code = objections[0]
        entry = pb.get(code) or pb.get("other")
        if not entry or not entry.get("enabled") or not entry.get("points"):
            p.escalations.append(Escalation("outside_playbook", f"an objection ({code}) the playbook does not cover: a person answers"))
            p.action, p.why = "escalate", [p.escalations[-1].why]
            return p
        p.action, p.objection, p.offers = "handle_objection", code, list(entry.get("offers") or [])
        p.meeting = "meeting" in p.offers
        p.why = [f"objection ({code}): answer with the playbook's approved points"]
        if code == "timing":
            p.target_state = "nurture"
        return p
    known = known_slots(all_facts)
    busy = bool(BUSY.search(text or "")) or len((text or "").split()) <= 6 and cat == "interested"
    limit = int(cfg.get("max_questions_per_email") or 2)
    missing = [(slot, q) for slot, q in DISCOVERY if slot not in known and asked_before.get(slot, 0) < 2]
    may_ask = not busy and discovery_turns < 2
    if floor.intent == "more_info" and not any(CAPABILITY.search(q) for q in questions):
        p.action, p.target_state = "answer_question", "discovering"
        p.why = ["they ask for details: no approved material covers them, so a person writes that answer"]
        return p  # no draft: the template's suggestion keeps its placeholder
    if questions:
        claim = next((c for c in (_service_claim(q, claims) for q in questions if CAPABILITY.search(q)) if c), None)
        if claim is None:
            p.action, p.why = "answer_question", ["they ask something no approved claim answers: a person writes that answer"]
            p.target_state = "discovering"
            return p  # no draft: the template's suggestion keeps its placeholder for the person's answer
        if any(not CAPABILITY.search(q) for q in questions):
            p.action, p.why = "answer_question", ["they ask more than an approved claim answers: a person writes that answer"]
            p.target_state = "discovering"
            return p
        p.action, p.claim, p.meeting = "answer_question", claim, True
        p.questions = missing[:1] if may_ask else []
        p.why = [f"a capability question an approved claim answers ({claim.id})"]
        p.target_state = "discovering"
        return p
    if all(n in known for n in QUALIFIED_NEEDS):
        p.action, p.meeting, p.target_state = "offer_meeting", True, "qualified"
        p.why = ["the problem, the timeline and who decides are known: the next step is a call"]
        return p
    if cat in ("interested", "question", "other"):
        if may_ask and missing:
            p.action, p.questions, p.target_state = "ask_discovery", missing[:limit], "discovering"
            p.meeting = True
            p.why = [f"discovery: {', '.join(s for s, _ in missing[:limit])} not known yet (at most {limit} questions)"]
        else:
            p.action, p.meeting, p.target_state = "offer_meeting", True, "scheduling" if cat == "interested" else "discovering"
            p.why = ["busy, or asked enough already: offer a call instead of more questions" if missing else
                     "enough is known: offer a call"]
        return p
    p.why = [f"the rules read {cat}: the template's answer stays"]
    p.target_state = "engaged"
    return p


# --------------------------------------------------------------------------------------------------- the draft


def compose(plan: Plan, settings, pb: dict, *, day: str | None = None) -> str | None:
    """The draft's own words (between the greeting and the signature), from fixed wording, the playbook and approved
    claims. None when a person writes the answer."""
    from aime.meetings import booking_minutes

    if not plan.drafts or (plan.action == "answer_question" and plan.claim is None):
        return None
    cfg = config(settings)
    minutes = booking_minutes(settings)
    url = getattr(settings, "calendly_url", "") or ""
    lines = ["Thank you for getting back to me."]
    if plan.action == "handle_objection" or (plan.pricing and plan.objection):
        entry = pb.get(plan.objection or "") or {}
        points = list(entry.get("points") or [])
        if points and points[0].lower().startswith("thank"):
            lines = []  # the playbook's own thanks opens it: no second one
        lines += points
        offers = list(entry.get("offers") or [])
        if "referral" in offers and not any("right person" in x for x in lines):
            lines.append("Who would be the right person to speak with?")
        if "check_back" in offers and not any("check" in x.lower() for x in lines):
            lines.append("Would it help if I checked back at a better time?")
    if plan.pricing and plan.objection != "price":
        statement = (cfg.get("pricing_statement") or "").strip() if cfg.get("pricing_disclosure") == "owner_statement" else ""
        lines.append(statement or "The best way to answer that properly is a short call with the Sales Manager, who can go "
                                  "through it with you.")
    if plan.claim is not None:
        lines.append(f"Yes: {plan.claim.text}")
    if plan.questions:
        if len(plan.questions) == 1:
            lines.append(plan.questions[0][1])
        else:
            lines.append("So that a call is worth your time, two quick questions:\n" +
                         "\n".join(f"- {q}" for _, q in plan.questions))
    if plan.meeting:
        if url:
            if day and not plan.questions:
                when = f"You mentioned {day}: you can pick any open {minutes}-minute slot then, or another time that suits you, here:"
            elif plan.questions:
                when = f"Or, if it is easier, you can pick a {minutes}-minute slot that suits you here:"
            else:
                when = f"You can pick a {minutes}-minute slot that suits you here:"
            lines.append(f"{when}\n{url}")
        else:
            ask = f"could you share two or three times that suit you for a {minutes}-minute call?"
            lines.append(f"Or, if it is easier, {ask}" if plan.questions else ask[0].upper() + ask[1:])
    return "\n\n".join(x for x in lines if x)


def wrap(settings, row: InboundEmail, contact, lead, core: str) -> str:
    """The whole email around the draft's words: the template's own greeting, signature and footer."""
    from aime.reply_intel import _signature, first_name, reply_footer

    name = first_name(row.from_name or (contact.first_name if contact else None), lead) or "there"
    return f"Hi {name},\n\n{core}\n\n{_signature(settings)}" + reply_footer(settings)


# --------------------------------------------------------------------------------------------------- handover hook


def needs_handover(session: Session, lead_id: int, reason: str, *, codes: list[str] | tuple = (), inbound_id: int | None = None,
                   settings=None, alert: bool = True, summary: str = "") -> int:
    """The Sales Manager should take this conversation over. Recorded as an open handover request (an ai_decisions row:
    task handover_request, kind handover, verdict human_review), which the handover package (aime.handover) reads with
    `handover_requests` and closes with aime.ai.decisions.record_human_action; with `alert`, an alert (one open per
    lead) says so on the dashboard. Nothing is sent, booked or moved."""
    rec = decisions.record(session, agent=AGENT, task=TASK_HANDOVER, verdict=Verdict.HUMAN_REVIEW,
                           proposal={"kind": "handover", "lead_id": lead_id, "reason": reason[:400], "summary": summary[:1500],
                                     "confidence": 0.9},
                           reasons=[reason[:300]], escalations=list(codes), lead_id=lead_id,
                           input_ref=f"inbound_email:{inbound_id}" if inbound_id else f"lead:{lead_id}", kind="handover")
    if alert:
        from aime.monitoring import alert_once

        lead = session.get(Lead, lead_id)
        who = f"{lead.company.name}" if lead is not None and lead.company is not None else f"lead #{lead_id}"
        alert_once(session, "warning", "conversation", f"Sales Manager: please take over the conversation with {who}: {reason}"[:500],
                   settings, key=f"conversation_handover:{lead_id}")
    return rec.id


def handover_requests(session: Session, lead_id: int | None = None, open_only: bool = True) -> list[AIDecision]:
    """Handover requests, newest first: open ones (no person has acted on them yet) unless `open_only` is False."""
    q = select(AIDecision).where(AIDecision.agent == AGENT, AIDecision.task == TASK_HANDOVER)
    if lead_id is not None:
        q = q.where(AIDecision.lead_id == lead_id)
    if open_only:
        q = q.where(AIDecision.human_action.is_(None))
    return list(session.execute(q.order_by(AIDecision.id.desc())).scalars())


# --------------------------------------------------------------------------------------------------- state and summary


def _path(current: str, target: str, actor: str) -> list[str]:
    """The shortest allowed walk along aime.ai.state's arrows (at most three moves), each move checked."""
    if current == target:
        return []
    todo, seen = deque([(current, [])]), {current}
    while todo:
        at, walk = todo.popleft()
        if len(walk) >= 3:
            continue
        for nxt in sorted(state_mod.TRANSITIONS.get(at, ())):
            if nxt in seen or not state_mod.check_transition(at, nxt, actor)[0]:
                continue
            if nxt == target:
                return [*walk, nxt]
            seen.add(nxt)
            todo.append((nxt, [*walk, nxt]))
    return []


def summary_of(facts: list[ConversationFact]) -> str:
    """The rolling summary: one line per kind, every item naming the reply (#id) its words came from. Nothing in it
    that is not a stored fact with evidence."""
    order = [("requirement", "Requirements"), ("budget", "Budget (as stated)"), ("timeline", "Timeline"),
             ("authority", "Who decides"), ("stakeholder", "Others involved"), ("objection", "Objections"),
             ("concern", "Concerns"), ("question", "Their questions"), ("meeting_intent", "Meeting")]
    lines = []
    for kind, label in order:
        items = [f"{f.value} [#{f.evidence_inbound_id}{', AI reading' if f.source == 'ai' else ''}]"
                 for f in facts if f.kind == kind and f.status != "withdrawn" and f.evidence_inbound_id]
        if items:
            lines.append(f"{label}: " + "; ".join(dict.fromkeys(items[-4:])))
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------- the turn


@dataclass
class TurnResult:
    lead_id: int
    inbound_id: int
    mode: str
    state_before: str
    state_after: str
    language: str = "en"
    floor: str = ""
    plan: Plan | None = None
    facts: list[ConversationFact] = field(default_factory=list)
    draft: str | None = None
    draft_verdict: str | None = None
    draft_reasons: list[str] = field(default_factory=list)
    followup_id: int | None = None  # the suggestion this draft replaced (mode drafts)
    handover_id: int | None = None
    decision_ids: list[int] = field(default_factory=list)
    model_used: bool = False
    model_error: str | None = None
    skipped: str | None = None


def _resolve_llm(settings, llm, run: dict, cfg: dict):
    if llm is not None:
        return llm
    if "router_llm" not in run:
        from aime.llm.base import ProviderRefused
        from aime.llm.router import ModelRouter

        try:
            run["router_llm"] = ModelRouter(settings).provider_for(TASK_FACTS)
        except ProviderRefused:
            run["router_llm"] = None
    return run["router_llm"]


def _model_allowed(run: dict, cfg: dict) -> bool:
    left = run.setdefault("model_calls", int(cfg.get("model_calls_per_run", 3) or 0))
    return left > 0 and not run.get("model_slow")


def _spend(run: dict, started: float) -> None:
    run["model_calls"] = run.get("model_calls", 0) - 1
    if time.perf_counter() - started > SLOW_SECONDS:
        run["model_slow"] = True


def _floor_of(row: InboundEmail) -> Floor:
    from aime.reply_intel import COMPLAINT, LEGAL_MENTION

    said = f"{row.subject or ''}\n{row.new_text or row.body_text or ''}"
    complaint = bool(COMPLAINT.search(said)) and row.category in ("unsubscribe", "not_interested", "other", "objection")
    return Floor(row.category or "other", row.confidence or "low", list(row.reasons or []), row.intent, complaint,
                 bool(LEGAL_MENTION.search(said)))


def _asked_before(session: Session, lead_id: int) -> tuple[dict[str, int], int]:
    rows = session.execute(select(AIDecision).where(AIDecision.agent == AGENT, AIDecision.task == TASK_TURN,
                                                    AIDecision.lead_id == lead_id)).scalars().all()
    asked: dict[str, int] = {}
    turns = 0
    for r in rows:
        slots = (r.proposal or {}).get("asked") or []
        if slots:
            turns += 1  # in shadow too: the shadow conversation is read as if its drafts had gone
        for s in slots:
            asked[str(s)] = asked.get(str(s), 0) + 1
    return asked, turns


def on_reply(session: Session, settings, row: InboundEmail, *, llm=None, run: dict | None = None,
             actor: str = "conversation") -> TurnResult | None:
    """One turn of the conversation for a reply the inbox rules have acted on. Reads (and asks the model) first, then
    writes; the caller commits. Returns None when the engine is off or the email is not a person's reply."""
    from aime.crm_ops import is_suppressed
    from aime.meetings import next_meeting
    from aime.reply_intel import check_back_when, offered_day

    run = run if run is not None else {}
    cfg = config(settings)
    m = mode(settings)
    if m == "off" or row.lead_id is None or row.category in NOT_A_PERSON or getattr(row, "mail_kind", None) == "calendar":
        return None
    lead = session.get(Lead, row.lead_id)
    if lead is None:
        return None
    conv = session.execute(select(Conversation).where(Conversation.lead_id == lead.id)).scalar_one_or_none()
    if conv is not None and conv.last_inbound_id and conv.last_inbound_id >= row.id:
        return None  # already taken into account
    sent_before = session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                           FollowUp.status == "sent")).first()
    current = conv.state if conv is not None else ("contacted" if sent_before else "new")
    text = row.new_text or row.body_text or ""
    floor = _floor_of(row)
    res = TurnResult(lead.id, row.id, m, current, current, floor=floor.category)

    # ---- the floor settled it: the rules' outcome is recorded, nothing else is read or proposed
    if floor.hit:
        target = "opted_out" if (floor.category == "unsubscribe" or floor.complaint) else current
        moves = _path(current, target, "rules") if target != current else []
        conv = _ensure(session, conv, lead.id, current)
        if moves:
            conv.state = moves[-1]
        conv.last_inbound_id, conv.unanswered_ai_turns = row.id, 0
        res.state_after = conv.state
        rec = decisions.record(session, agent=AGENT, task=TASK_TURN, verdict=Verdict.AUTO_INTERNAL,
                               proposal={"kind": "internal_note", "lead_id": lead.id, "mode": m, "action": "none",
                                         "text": f"the rules settled this reply ({'a complaint' if floor.complaint else floor.category}): "
                                                 "no AI reading, no draft", "state": [current, conv.state], "confidence": 1.0},
                               reasons=["safety floor: the rules decide"], escalations=["safety_floor"], lead_id=lead.id,
                               input_ref=f"inbound_email:{row.id}", kind="internal_note")
        res.decision_ids.append(rec.id)
        if floor.complaint or floor.legal:
            res.handover_id = needs_handover(session, lead.id, "a complaint or legal language: the Sales Manager reads it",
                                             codes=["safety_floor"], inbound_id=row.id, settings=settings,
                                             alert=m == "drafts" or bool(cfg.get("escalation_alerts_in_shadow")))
        res.plan = Plan("none", why=["safety floor"])
        res.skipped = "floor"
        return res

    # ---- reads
    from aime.ai.engine import _evidence
    from aime.llm.router import ai_config

    language = language_of(text)
    res.language = language
    injection = injection_signals(f"{row.subject or ''}\n{text}")
    earlier, evidence, _reg = _evidence(session, settings, lead.id, text)
    claims, topics = claims_mod.load(settings, session)
    suppressed = bool(row.from_email and is_suppressed(session, row.from_email))
    minutes_text = []
    from aime.meetings import booking_minutes

    minutes_text.append(f"{booking_minutes(settings)}-minute")
    if getattr(settings, "calendly_url", ""):
        minutes_text.append(settings.calendly_url)
    pctx = PolicyContext(settings=settings, lead_id=lead.id, floor=floor, evidence=[*evidence, row.from_name or ""],
                         suppressed=suppressed, current_state=current if current not in ("new", "contacted", "nurture") else "engaged",
                         injection=injection, escalations=[], claims=claims, topics=topics, offered_slots=minutes_text)
    facts = rule_facts(text, row)
    out_model: FactsOut | None = None
    the_llm = _resolve_llm(settings, llm, run, cfg) if cfg.get("model_extraction", True) else None
    prompt = prompts.get(TASK_FACTS)
    model_meta = {"provider": "none", "model": None, "ms": 0, "tokens": 0, "structured_ok": None, "error": None}
    if the_llm is not None and _model_allowed(run, cfg) and not injection:
        from aime.llm.base import LLMError, StructuredOutputError
        from aime.llm.structured import quote_data, structured_call

        system, user = prompt.render(company=settings.company_name, reply=quote_data("PROSPECT_EMAIL", text, 3000))
        started = time.perf_counter()
        model_meta.update(provider=getattr(the_llm, "name", "llm"), model=getattr(the_llm, "model_name", None))
        try:
            r = structured_call(the_llm, user, FactsOut, system=system, max_tokens=500, temperature=0.0)
            out_model = r.value
            model_meta.update(ms=r.ms, tokens=r.tokens, structured_ok=True)
        except StructuredOutputError as exc:
            model_meta.update(structured_ok=False, error=f"invalid answer: {'; '.join(exc.problems)[:200]}")
        except LLMError as exc:
            model_meta.update(structured_ok=False, error=f"model error: {exc}"[:300])
        finally:
            _spend(run, started)
        res.model_used, res.model_error = True, model_meta["error"]
    ai_facts: list[Fact] = []
    if out_model is not None:
        rule_kinds = {f.kind for f in facts}
        for f in ai_fact_candidates(out_model, text, lead.id, pctx):
            if f.decision is not None and f.decision.blocked:
                ai_facts.append(f)  # recorded as blocked, never stored as a fact
                continue
            if f.kind in ("budget", "timeline", "objection", "meeting_intent") and f.kind in rule_kinds:
                continue  # the rules already read it: their fact stands, the model's is not added
            ai_facts.append(f)
    stored = list(session.execute(select(ConversationFact).where(ConversationFact.lead_id == lead.id)).scalars())
    esc_cfg = (ai_config(settings).get("escalation") or {})
    escalations = assess(text, floor, None, 0, injection, None, esc_cfg)  # they answered: no AI turn is unanswered now
    if out_model is not None and out_model.upset and not any(e.code == "upset" for e in escalations):
        escalations.append(Escalation("model_upset", "the model reads the prospect as upset (the rules did not): a person answers"))
    if out_model is not None and out_model.asks_for_human and not any(e.code == "human_requested" for e in escalations):
        escalations.append(Escalation("model_asks_for_human", "the model reads a request for a person: a person answers"))
    escalations = [e for e in escalations if e.code != "sensitive_topic"]  # prices and contracts are judged in plan_next
    pb = playbook.load(session)
    asked, discovery_turns = _asked_before(session, lead.id)
    booked = next_meeting(session, lead.id) is not None
    usable_ai = [f for f in ai_facts if f.decision is not None and not f.decision.blocked]
    plan = plan_next(floor=floor, text=text, facts=facts, all_facts=[*stored, *facts, *usable_ai], current=current,
                     language=language, escalations=escalations, pb=pb, claims=claims, cfg=cfg, asked_before=asked,
                     discovery_turns=discovery_turns, booked=booked, suppressed=suppressed,
                     check_back=check_back_when(row) is not None)
    res.plan = plan
    pctx.escalations = plan.escalations
    core = compose(plan, settings, pb, day=offered_day(text))
    draft_dec: PolicyDecision | None = None
    polished = None
    if core:
        raw = {"kind": "reply", "lead_id": lead.id, "body": core, "confidence": 0.7,
               "rationale": "; ".join(plan.why)[:500], "claims_used": [plan.claim.id] if plan.claim else []}
        draft_dec = evaluate(raw, pctx)
        if (draft_dec.verdict is Verdict.HUMAN_REVIEW and cfg.get("model_polish") and the_llm is not None
                and _model_allowed(run, cfg)):
            polished = _polish(the_llm, settings, core, lead.id, pctx, plan, run)

    # ---- writes
    conv = _ensure(session, conv, lead.id, current)
    conv.language = language
    target = plan.target_state or ("engaged" if current in ("new", "contacted", "nurture") else current)  # their reply engages
    actor_kind = "rules" if floor.category == "not_interested" else "ai"
    moves = _path(current, target, actor_kind) if target != current else []
    if not moves and target != current and current in ("new", "contacted", "nurture"):
        moves = _path(current, "engaged", "ai")
    state_reason = (f"{current} -> {' -> '.join(moves)}" if moves else
                    f"stays {current}" + (f" (no allowed move to {target})" if target != current else ""))
    if moves:
        st = decisions.record(session, agent=AGENT, task=TASK_STATE, verdict=Verdict.AUTO_INTERNAL,
                              proposal={"kind": "field_update", "lead_id": lead.id, "field": "conversation_state",
                                        "value": moves[-1], "confidence": 0.9, "path": [current, *moves]},
                              reasons=[state_reason], lead_id=lead.id, input_ref=f"inbound_email:{row.id}", kind="field_update")
        res.decision_ids.append(st.id)
        conv.state = moves[-1]
    res.state_after = conv.state
    new_rows: list[ConversationFact] = []
    have = {(f.kind, _norm(f.value)) for f in stored}
    for f in facts:
        if (f.kind, _norm(f.value)) in have and f.kind not in ("question", "meeting_intent"):
            continue
        new_rows.append(ConversationFact(lead_id=lead.id, kind=f.kind, value=f.value[:500], evidence_inbound_id=row.id,
                                         evidence_quote=f.quote[:500], confidence=f.confidence, source="rule", status="open"))
    for f in ai_facts:
        if f.decision is None:
            continue
        rec = decisions.record(session, agent=AGENT, task=TASK_FACTS, verdict=f.decision.verdict, proposal=f.raw,
                               reasons=f.decision.reasons, escalations=f.decision.escalations, prompt_version=prompt.ref,
                               provider=model_meta["provider"], model=model_meta["model"], lead_id=lead.id,
                               input_ref=f"inbound_email:{row.id}", kind=f.decision.kind, structured_ok=True,
                               tokens=model_meta["tokens"] // max(1, len(ai_facts)), ms=model_meta["ms"])
        res.decision_ids.append(rec.id)
        if f.decision.blocked or (f.kind, _norm(f.value)) in have:
            continue
        new_rows.append(ConversationFact(lead_id=lead.id, kind=f.kind, value=f.value[:500], evidence_inbound_id=row.id,
                                         evidence_quote=f.quote[:500], confidence=f.confidence, source="ai", status="open",
                                         ai_decision_id=rec.id))
    if model_meta["structured_ok"] is False:
        rec = decisions.record(session, agent=AGENT, task=TASK_FACTS, verdict=Verdict.BLOCK,
                               proposal={"kind": "internal_note", "lead_id": lead.id, "text": "no usable answer from the model",
                                         "confidence": 0.0},
                               reasons=["the rules' facts stand alone"], prompt_version=prompt.ref,
                               provider=model_meta["provider"], model=model_meta["model"], lead_id=lead.id,
                               input_ref=f"inbound_email:{row.id}", kind="internal_note", structured_ok=False,
                               error=model_meta["error"], ms=model_meta["ms"])
        res.decision_ids.append(rec.id)
    for r in new_rows:
        session.add(r)
    session.flush()
    res.facts = new_rows
    everything = [*stored, *new_rows]
    if new_rows or not conv.summary:
        conv.summary = summary_of(everything) or None
        conv.summary_version = (conv.summary_version or 0) + 1
    conv.last_inbound_id, conv.unanswered_ai_turns = row.id, 0
    used = [{"kind": f.kind, "value": f.value, "quote": f.evidence_quote, "inbound_id": f.evidence_inbound_id,
             "source": f.source} for f in everything if f.evidence_inbound_id][-12:]
    turn = decisions.record(session, agent=AGENT, task=TASK_TURN,
                            verdict=Verdict.HUMAN_REVIEW if plan.action == "escalate" or plan.escalations else Verdict.AUTO_INTERNAL,
                            proposal={"kind": "internal_note", "lead_id": lead.id, "mode": m, "action": plan.action,
                                      "text": f"next step: {plan.action.replace('_', ' ')}: {'; '.join(plan.why)}"[:1900],
                                      "asked": [s for s, _ in plan.questions], "objection": plan.objection,
                                      "state": [current, conv.state], "language": language, "facts": used,
                                      "placed": False, "confidence": 0.9},
                            reasons=[*plan.why, state_reason], escalations=[e.code for e in plan.escalations], lead_id=lead.id,
                            input_ref=f"inbound_email:{row.id}", kind="internal_note",
                            provider=model_meta["provider"], model=model_meta["model"], structured_ok=model_meta["structured_ok"])
    res.decision_ids.append(turn.id)
    final_text = None
    if core and draft_dec is not None:
        final_text, final_dec, provider = core, draft_dec, "template"
        if polished is not None:
            final_text, final_dec, provider = polished
        res.draft, res.draft_verdict, res.draft_reasons = final_text, final_dec.verdict.value, list(final_dec.reasons)
        placed = None
        if m == "drafts" and final_dec.verdict is Verdict.HUMAN_REVIEW:
            placed = _place(session, settings, row, lead, final_text, plan, provider)
        rec = decisions.record(session, agent=AGENT, task=TASK_DRAFT, verdict=final_dec.verdict,
                               proposal={"kind": "reply", "lead_id": lead.id, "body": final_text, "action": plan.action,
                                         "rationale": "; ".join(plan.why)[:500], "facts_used": used,
                                         "questions": [q for _, q in plan.questions],
                                         "claims_used": [plan.claim.id] if plan.claim else [], "objection": plan.objection,
                                         "mode": m, "written_by": provider, "confidence": 0.7,
                                         "checks": final_dec.checks},
                               reasons=final_dec.reasons, escalations=[e.code for e in plan.escalations],
                               prompt_version=prompts.get("draft_polish").ref if polished is not None else "template/conversation-v1",
                               provider="template" if polished is None else model_meta["provider"],
                               model=None if polished is None else model_meta["model"], lead_id=lead.id,
                               input_ref=f"followup:{placed}" if placed else f"inbound_email:{row.id}", kind="reply")
        res.decision_ids.append(rec.id)
        if placed:
            res.followup_id = placed
            turn.proposal = {**(turn.proposal or {}), "placed": True}
            conv.unanswered_ai_turns = 1
            conv.last_ai_turn_at = utcnow()
    if plan.action == "escalate" or plan.pricing:
        reason = "; ".join(e.why for e in plan.escalations) if plan.action == "escalate" and plan.escalations else "; ".join(plan.why)
        res.handover_id = needs_handover(session, lead.id, reason or "a person should answer", codes=[e.code for e in plan.escalations]
                                         or (["pricing"] if plan.pricing else []), inbound_id=row.id, settings=settings,
                                         alert=m == "drafts" or bool(cfg.get("escalation_alerts_in_shadow")),
                                         summary=conv.summary or "")
    return res


def _ensure(session: Session, conv: Conversation | None, lead_id: int, state: str) -> Conversation:
    if conv is None:
        conv = Conversation(lead_id=lead_id, state=state, summary_version=0, unanswered_ai_turns=0)
        session.add(conv)
        session.flush()
    return conv


def _polish(llm, settings, core: str, lead_id: int, pctx: PolicyContext, plan: Plan, run: dict):
    """The model's rewording of the composed draft, kept only when the policy engine sends it to a person too and it
    keeps every link and question. Returns (text, decision, provider) or None."""
    from aime.llm.base import LLMError, StructuredOutputError
    from aime.llm.structured import quote_data, structured_call

    prompt = prompts.get("draft_polish")
    system, user = prompt.render(company=settings.company_name, draft=quote_data("DRAFT", core, 2000))
    started = time.perf_counter()
    try:
        r = structured_call(llm, user, PolishOut, system=system, max_tokens=300, temperature=0.2)
    except (StructuredOutputError, LLMError):
        return None
    finally:
        _spend(run, started)
    text = r.value.body.strip()
    links = re.findall(r"https?://\S+", core)
    if any(link not in text for link in links) or text.count("?") < core.count("?"):
        return None
    d = evaluate({"kind": "reply", "lead_id": lead_id, "body": text, "confidence": r.value.confidence,
                  "rationale": "; ".join(plan.why)[:500]}, pctx)
    if d.verdict is not Verdict.HUMAN_REVIEW:
        return None
    return text, d, getattr(llm, "label", "model")


NEW_DRAFT_NOTE = "an AI draft of the answer waits for your approval"


def may_write_new(session: Session, settings, row: InboundEmail, lead: Lead) -> str | None:
    """Why no answer may be drafted where the rules suggested none (None: it may). The same tests the inbox rules and the
    approval page apply, read again here, and stricter: a simulation, a junk-folder email, a no-reply or suppressed
    address, an opted-out contact or company, a closed or merged lead, a booked meeting, or an answer already waiting."""
    from aime.crm_ops import is_suppressed
    from aime.inbox import is_address
    from aime.meetings import next_meeting
    from aime.outreach import company_opted_out
    from aime.reply_intel import NO_REPLY

    contact = row.contact if row.contact_id else None
    if row.category not in ("interested", "question", "other", "objection", "referral"):
        return f"the rules read {row.category}"
    if row.is_simulated or getattr(row, "in_junk", None):
        return "a simulated reply or one found in junk"
    if not is_address(row.from_email) or NO_REPLY.match(row.from_email or ""):
        return "no address a person reads"
    if is_suppressed(session, row.from_email) or (contact is not None and contact.consent_status == "opted_out"):
        return "the address is on the do-not-contact list"
    if lead.status in ("disqualified", "merged") or lead.outreach_status in ("unsubscribed", "bounced"):
        return f"the lead is {lead.status} / {lead.outreach_status}"
    if lead.company is not None and company_opted_out(session, lead.company):
        return "someone at this company said not to contact them"
    if next_meeting(session, lead.id) is not None:
        return "a meeting is booked"
    if session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email", FollowUp.kind == "response",
                                                 FollowUp.status.in_(("awaiting_approval", "approved", "sending")))).first():
        return "an answer to them already waits"
    return None


def _place(session: Session, settings, row: InboundEmail, lead: Lead, core: str, plan: Plan, provider: str) -> int | None:
    """Mode drafts: the checked draft replaces the template's suggested answer to this reply, which still waits for a
    person's approval. Only a suggestion the rules made, still awaiting approval and untouched by a person, is
    replaced. Where the rules suggested none (a reply they read as "other", such as an answer to our questions), a new
    answer awaiting approval is drafted only when `may_write_new` finds nothing against it."""
    from aime.meetings import has_booking_link

    if row.suggested_followup_id:
        f = session.get(FollowUp, row.suggested_followup_id)
        if f is None or f.status != "awaiting_approval" or f.kind != "response" or (f.generator or "template") != "template":
            return None
    else:
        if may_write_new(session, settings, row, lead) is not None:
            return None
        subject = row.subject or ""
        f = FollowUp(lead_id=lead.id, contact_id=row.contact_id, company_id=lead.company_id, channel="email", kind="response",
                     step=1, due_at=utcnow(), status="awaiting_approval",
                     subject=(subject if subject.lower().startswith("re:") else f"Re: {subject}")[:300], body="",
                     to_email=row.from_email, in_reply_to=row.message_id)
        session.add(f)
        session.flush()
        row.suggested_followup_id = f.id
        row.action_note = "; ".join(x for x in (row.action_note, NEW_DRAFT_NOTE) if x)[:2000]
    f.body = wrap(settings, row, f.contact, lead, core)
    f.generator = (GENERATOR if provider == "template" else f"AI ({provider}) conversation draft")[:80]
    if f.blocked_reason == PLACEHOLDER_BLOCK:
        f.blocked_reason = None
    if has_booking_link(settings, f.body):
        f.purpose = "booking_link"
    elif plan.meeting and not getattr(settings, "calendly_url", ""):
        f.purpose = "ask_times"
    elif f.purpose in ("booking_link", "ask_times"):
        f.purpose = None
    return f.id


# --------------------------------------------------------------------------------------------------- the approval page


def approval_cards(session: Session, followup_ids: list[int]) -> dict[int, dict]:
    """For each AI draft waiting for approval: the next step and why, the facts it used with the prospect's words, the
    risk flags and the policy's reasons. Read from the decision log; nothing is computed again."""
    if not followup_ids:
        return {}
    refs = {f"followup:{i}": i for i in followup_ids}
    rows = session.execute(select(AIDecision).where(AIDecision.agent == AGENT, AIDecision.task == TASK_DRAFT,
                                                    AIDecision.input_ref.in_(list(refs))).order_by(AIDecision.id)).scalars()
    out: dict[int, dict] = {}
    for r in rows:
        p = r.proposal or {}
        flags = [*(r.escalations or []), *[k for k, v in (p.get("checks") or {}).items() if v == "fail"]]
        out[refs[r.input_ref]] = {"decision_id": r.id, "action": str(p.get("action") or "").replace("_", " "),
                                  "why": p.get("rationale") or "", "facts": list(p.get("facts_used") or [])[-8:],
                                  "flags": list(dict.fromkeys(flags)), "reasons": list(r.reasons or []), "verdict": r.verdict,
                                  "written_by": p.get("written_by") or "template", "questions": p.get("questions") or []}
    return out


def record_review(session: Session, f: FollowUp, action: str, actor: str) -> None:
    """A person approved, edited or rejected an AI draft: stored on its decision for the agreement figures."""
    if not (f.generator or "").startswith("AI") or "conversation" not in (f.generator or ""):
        return
    row = session.execute(select(AIDecision).where(AIDecision.agent == AGENT, AIDecision.task == TASK_DRAFT,
                                                   AIDecision.input_ref == f"followup:{f.id}")
                          .order_by(AIDecision.id.desc())).scalars().first()
    if row is None or row.human_action:
        return
    decisions.record_human_action(session, row.id, action, actor, final_text=f.body if action == "edited" else None)


# --------------------------------------------------------------------------------------------------- follow-ups


def followup_context(session: Session, settings, lead: Lead, step: int) -> str | None:
    """Mode drafts: one line for a later step of the sequence (2 or 3) that refers to what the prospect said, in their
    own words, from the newest open requirement or question fact; checked by the policy engine like any draft and
    recorded. None when there is nothing to refer to, in any other mode, or when the policy does not pass it; the
    template stays as it is then. The sequence's stop rules decide elsewhere whether a step is written at all."""
    if step < 2 or mode(settings) != "drafts":
        return None
    conv = session.execute(select(Conversation).where(Conversation.lead_id == lead.id)).scalar_one_or_none()
    if conv is not None and (conv.state in state_mod.TERMINAL_FOR_AI or conv.unanswered_ai_turns >= 2):
        return None
    facts = session.execute(select(ConversationFact).where(ConversationFact.lead_id == lead.id,
                                                           ConversationFact.kind.in_(("requirement", "question")),
                                                           ConversationFact.status == "open",
                                                           ConversationFact.evidence_quote.is_not(None))
                            .order_by(ConversationFact.id.desc())).scalars().first()
    if facts is None or not facts.evidence_quote:
        return None
    quote = _clip(facts.evidence_quote, 160)
    line = f'Last time you mentioned: "{quote}"'
    claims, topics = claims_mod.load(settings, session)
    pctx = PolicyContext(settings=settings, lead_id=lead.id, evidence=[facts.evidence_quote], claims=claims, topics=topics,
                         current_state=conv.state if conv is not None else "engaged")
    # The quote is the prospect's own sentence, word for word from the stored evidence: its "we" is theirs, not ours, so
    # the claims check reads the line with the quote set aside, and the quote itself must name no price, discount,
    # contract, guarantee, legal matter or number (aime.ai.playbook.problems): the AI repeats none of those.
    d = evaluate({"kind": "reply", "lead_id": lead.id, "body": 'Last time you mentioned: "(their words)"', "confidence": 0.7,
                  "rationale": f"step {step} refers to fact #{facts.id}"}, pctx)
    quoted = playbook.problems(quote)
    verdict = Verdict.BLOCK if quoted else d.verdict
    reasons = [*d.reasons, *(f"the quote mentions {q}: not repeated" for q in quoted)]
    decisions.record(session, agent=AGENT, task=TASK_FOLLOWUP, verdict=verdict,
                     proposal={"kind": "reply", "lead_id": lead.id, "body": line, "fact_id": facts.id, "step": step,
                               "confidence": 0.7}, reasons=reasons, lead_id=lead.id, input_ref=f"lead:{lead.id}", kind="reply")
    if verdict is not Verdict.HUMAN_REVIEW:
        return None
    if conv is not None:
        conv.unanswered_ai_turns = (conv.unanswered_ai_turns or 0) + 1
        conv.last_ai_turn_at = utcnow()
    return line


def with_context(body: str, line: str | None) -> str:
    """The line placed after the greeting of a template body."""
    if not line:
        return body
    parts = body.split("\n\n", 1)
    return f"{parts[0]}\n\n{line}\n\n{parts[1]}" if len(parts) == 2 else f"{line}\n\n{body}"


def conversation_of(session: Session, lead_id: int) -> tuple[Conversation | None, list[ConversationFact]]:
    conv = session.execute(select(Conversation).where(Conversation.lead_id == lead_id)).scalar_one_or_none()
    facts = list(session.execute(select(ConversationFact).where(ConversationFact.lead_id == lead_id)
                                 .order_by(ConversationFact.id)).scalars())
    return conv, facts


__all__ = ["on_reply", "needs_handover", "handover_requests", "followup_context", "approval_cards", "record_review",
           "rule_facts", "plan_next", "compose", "language_of", "config", "mode"]
