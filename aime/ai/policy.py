"""The policy engine: every AI proposal is checked here and routed to one of three verdicts.

- AUTO_INTERNAL: an internal CRM note or field (aime.ai.schemas.INTERNAL_KINDS), reversible, logged; only when the
  model is confident enough, the value is found in the evidence, and nothing asked for a person;
- HUMAN_REVIEW: a person decides. While REQUIRE_HUMAN_APPROVAL is true every external action (anything a prospect would
  see) is HUMAN_REVIEW at best; with it off it still is, because autonomous sending is no part of this phase;
- BLOCK: the proposal is not shown as a draft at all.

The checks, in order, each adding a reason:
1. prohibited actions: a proposal kind outside the closed list (sending, booking, publishing, changing settings, lifting
   a suppression ...) is blocked before anything else is read;
2. schema: the proposal must validate as one of aime.ai.schemas' proposal models;
3. the safety floor: when the rules (aime.ai.guard.rule_floor) read an opt-out, a complaint or a bounce, or the contact
   is suppressed, no external proposal passes, and no state move other than to opted_out / closed_lost;
4. prices, discounts, contracts, guarantees and legal language in text a prospect would read: aime.automation.SENSITIVE,
   the same words that stop a draft at send time, plus the prohibited topics of aime.ai.claims;
5. invented facts: a number, amount, percentage, date, weekday, time or proper name in the proposal that appears nowhere
   in the evidence (the prospect's emails, the lead's records, the research snapshot, the approved claims, the offered
   slots) blocks an external proposal and sends an internal one to a person;
6. approved claims: a sentence that states something about us must match an approved claim;
7. prompt injection: a proposal that repeats injected words is blocked, and so is every external proposal answering a
   text with injection signals (a person writes that answer); an internal one goes to a person;
8. the conversation state machine (aime.ai.state) for a proposed state move;
9. confidence thresholds (`ai.policy.auto_min_confidence`, default 0.8; `ai.policy.external_min_confidence`, 0.3).

The engine only reads: it changes nothing and never calls a model."""

from __future__ import annotations

import enum
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from aime.ai import claims as claims_mod
from aime.ai import state as state_mod
from aime.ai.escalation import Escalation
from aime.ai.guard import INJECTION, Floor, hidden_characters, plain
from aime.ai.schemas import EXTERNAL_KINDS, INTERNAL_KINDS, KINDS, PROPOSAL, FieldUpdate, MeetingOffer, proposal_text
from aime.automation import SENSITIVE


class Verdict(str, enum.Enum):
    AUTO_INTERNAL = "auto_internal"
    HUMAN_REVIEW = "human_review"
    BLOCK = "block"


PROHIBITED_ACTIONS = frozenset({
    "send", "send_email", "send_reply", "email", "reply_now", "book", "book_meeting", "schedule", "create_event", "publish",
    "post", "publish_post", "change_setting", "update_setting", "update_settings", "settings", "configure", "delete",
    "delete_lead", "approve", "approve_followup", "unsuppress", "lift_suppression", "remove_suppression", "opt_in",
    "call_api", "http_request", "execute", "run_command", "transfer", "pay", "purchase", "export", "share_data"})
AI_WRITABLE_FIELDS = frozenset({"requirements", "budget", "timeline", "objection", "meeting_intent", "summary", "next_step",
                                "conversation_state", "sentiment", "stakeholders"})
GROUNDED_FIELDS = frozenset({"budget", "timeline", "requirements", "stakeholders"})  # their value must be in the evidence
POLICY_DEFAULTS = {"auto_min_confidence": 0.8, "external_min_confidence": 0.3}

_MONTHS = r"(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?)"
_WEEKDAYS = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
NUMBER = re.compile(r"(?<![\w.])\$?\d[\d,]*(\.\d+)?\s?(%|k\b|m\b|percent\b)?")
DATE_WORD = re.compile(rf"\b({_WEEKDAYS}|{_MONTHS}\s+\d{{1,2}}(st|nd|rd|th)?|\d{{1,2}}(st|nd|rd|th)?\s+(of\s+)?{_MONTHS}|"
                       r"tomorrow|next week|this week|end of (the )?(month|quarter|year)|q[1-4])\b", re.I)
_WORD = re.compile(r"[A-Za-z][\w'&.-]*")
NAME_STOP = {"I", "We", "Our", "Thanks", "Thank", "Hi", "Hello", "Dear", "Best", "Regards", "Kind", "Please", "Sure", "Yes", "No",
             "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "The", "This", "That", "AI", "CRM",
             "Looking", "Happy", "Glad", "Would", "Could", "Let", "If", "As", "For", "It", "Is", "Are", "Can", "Do", "Does",
             "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
             "December", "ET", "PT", "CT", "MT", "US", "USA", "Calendly", "Zoom", "Teams", "Google", "Meet", "Sales", "Manager"}


@dataclass
class PolicyContext:
    settings: Any
    lead_id: int | None = None
    floor: Floor | None = None
    evidence: list[str] = field(default_factory=list)  # every text the model was given or may rely on
    suppressed: bool = False
    current_state: str = "engaged"
    injection: list[str] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)
    claims: list[claims_mod.Claim] | None = None
    topics: dict[str, tuple[str, str]] | None = None
    offered_slots: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.claims is None or self.topics is None:
            c, t = claims_mod.load(self.settings)
            self.claims = c if self.claims is None else self.claims
            self.topics = t if self.topics is None else self.topics

    @property
    def company(self) -> str:
        return getattr(self.settings, "company_name", "") or ""

    @property
    def require_human_approval(self) -> bool:
        return bool(getattr(self.settings, "require_human_approval", True))

    def thresholds(self) -> dict:
        try:
            raw = (self.settings.section("ai") or {}).get("policy") or {}
        except Exception:
            raw = {}
        out = dict(POLICY_DEFAULTS)
        for k in out:
            try:
                out[k] = float(raw.get(k, out[k]))
            except (TypeError, ValueError):
                pass
        return out


@dataclass
class PolicyDecision:
    verdict: Verdict
    reasons: list[str]
    kind: str | None
    proposal: BaseModel | None
    raw: dict
    checks: dict[str, str] = field(default_factory=dict)  # check -> pass | fail | skip
    escalations: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict is Verdict.BLOCK


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace(",", ""))


def _numbers(text: str) -> list[str]:
    out = []
    for m in NUMBER.finditer(text or ""):
        tok = m.group(0).strip()
        if re.fullmatch(r"[01]", tok):  # "1 call", "0" carry no fact
            continue
        out.append(tok)
    return out


def proper_names(text: str) -> list[str]:
    """Runs of capitalised words that do not start a sentence: the names of people, clients and products."""
    out = []
    for sentence in claims_mod.sentences(text or ""):
        matches = list(_WORD.finditer(sentence))
        run: list[str] = []
        for i, m in enumerate([*matches, None]):
            clean = m.group(0).strip(".,;:") if m else ""
            if i > 0 and clean[:1].isupper() and clean not in NAME_STOP:
                run.append(clean)
                if sentence[m.end():m.end() + 1] not in (",", ";", ":"):
                    continue  # a list ("Custom CRM Development, AI Agent Development") is several names, not one
                out.append(" ".join(run))
                run = []
                continue
            if run:
                out.append(" ".join(run))
            run = []
    return out


def ungrounded(text: str, evidence: str, company: str = "") -> list[str]:
    """Numbers, dates and names in `text` found nowhere in `evidence`."""
    ev = _norm(evidence)
    ev_digits = {d.rstrip(".") for d in re.findall(r"\d[\d.]*", ev)}  # "$30,000." ends a sentence
    out = []
    for tok in _numbers(text):
        digits = re.sub(r"[^\d.]", "", tok).rstrip(".")
        if digits and digits not in ev_digits:
            out.append(f"the number {tok}")
    for m in DATE_WORD.finditer(text or ""):
        if _norm(m.group(0)) not in ev:
            out.append(f"the date {m.group(0)}")
    own = set(_norm(company).split())
    for name in proper_names(text):
        words = [w for w in name.split() if w not in ("&", "of", "and")]
        if all(w in NAME_STOP for w in words) or set(_norm(name).split()) <= own:
            continue
        if _norm(name) not in ev:
            out.append(f"the name {name}")
    return list(dict.fromkeys(out))


def _prohibited_topics(text: str, topics: dict[str, tuple[str, str]]) -> list[str]:
    out = []
    for label, words in topics.values():
        m = re.search(words, text or "", re.I)
        if m:
            out.append(f'{label} ("{m.group(0)[:40]}")')
    return out


def evaluate(raw: dict | BaseModel, pctx: PolicyContext) -> PolicyDecision:
    raw_dict = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else dict(raw or {})
    checks: dict[str, str] = {}
    reasons: list[str] = []

    def block(why: str, check: str) -> PolicyDecision:
        checks[check] = "fail"
        return PolicyDecision(Verdict.BLOCK, [*reasons, why], raw_dict.get("kind"), None, raw_dict, checks,
                              [e.code for e in pctx.escalations])

    # 1. prohibited actions, before anything else is read
    kind = str(raw_dict.get("kind") or raw_dict.get("action") or raw_dict.get("tool") or "").strip().lower()
    asked = {str(raw_dict.get(k) or "").strip().lower() for k in ("kind", "action", "tool", "type")} - {""}
    if asked & PROHIBITED_ACTIONS or kind.startswith(("send", "book", "publish", "delete")):
        return block(f"prohibited action: {', '.join(sorted(asked & PROHIBITED_ACTIONS) or [kind])} (the AI only proposes; "
                     "sending, booking, publishing and settings are never its to do)", "prohibited_action")
    if kind not in KINDS:
        return block(f"{kind or 'no kind'!s} is not a proposal the AI may make", "prohibited_action")
    checks["prohibited_action"] = "pass"

    # 2. schema
    try:
        p = PROPOSAL.validate_python(raw_dict)
    except ValidationError as exc:
        return block("not a valid proposal: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:4]),
                     "schema")
    checks["schema"] = "pass"
    if pctx.lead_id is not None and p.lead_id != pctx.lead_id:
        return block(f"the proposal is about lead {p.lead_id}, this conversation is about lead {pctx.lead_id}", "scope")
    external = p.kind in EXTERNAL_KINDS
    th = pctx.thresholds()
    text = unicodedata.normalize("NFKC", proposal_text(p))  # full-width or look-alike letters read as plain ones

    # 3. the safety floor
    floor_hit = bool(pctx.floor is not None and pctx.floor.hit) or pctx.suppressed
    if floor_hit:
        what = ("the contact is suppressed" if pctx.suppressed else
                f"the rules read {'a complaint' if pctx.floor.complaint else pctx.floor.category}")
        if external:
            return block(f"safety floor: {what}, so no AI message goes to them", "safety_floor")
        if isinstance(p, FieldUpdate) and p.field == "conversation_state" and p.value not in ("opted_out", "closed_lost"):
            return block(f"safety floor: {what}; the lead cannot move to {p.value}", "safety_floor")
        reasons.append(f"safety floor: {what}; the rules decide, a person checks")
        checks["safety_floor"] = "fail"
    else:
        checks["safety_floor"] = "pass"

    if isinstance(p, FieldUpdate) and p.field not in AI_WRITABLE_FIELDS:
        return block(f"the field {p.field!r} is not one the AI may propose (status, owner, consent and contact details are "
                     "a person's or the rules')", "field")

    # 4. prices, contracts, guarantees, legal; prohibited topics. Read as a person reads it: a draft with characters the
    # approver cannot see is blocked outright, and look-alike letters hide no word from the checks.
    seen = plain(text)
    if external:
        hidden = hidden_characters(text)
        if hidden:
            return block(f"contains characters a reader cannot see ({', '.join(hidden[:3])}): a person writes this answer",
                         "hidden_characters")
        checks["hidden_characters"] = "pass"
        sensitive = SENSITIVE.search(seen)
        topics = _prohibited_topics(seen, pctx.topics or {})
        if sensitive or topics:
            words = ([f'"{sensitive.group(0)}"'] if sensitive else []) + topics
            return block("mentions " + ", ".join(words[:3]) + ": prices, discounts, contracts, guarantees and legal matters "
                         "are written by a person", "sensitive")
        checks["sensitive"] = "pass"

    # 7. prompt injection: a proposal that repeats injected words; an external proposal answering a text that tries to
    # instruct the AI (whatever the draft says, a person writes that answer)
    echoed = INJECTION.search(seen)
    if echoed:
        return block(f'the proposal repeats an instruction-like phrase ("{echoed.group(0)[:60]}")', "injection")
    if pctx.injection and external:
        return block(f'the prospect\'s text tries to instruct the AI ("{pctx.injection[0][:60]}"): no AI draft, a person '
                     "writes this answer", "injection")
    checks["injection"] = "pass"

    # 5. invented facts
    evidence = "\n".join([*pctx.evidence, *(c.text for c in pctx.claims or []), *pctx.offered_slots])
    if isinstance(p, MeetingOffer):
        unknown = [s for s in p.slots if _norm(s) not in _norm("\n".join(pctx.offered_slots))]
        if unknown:
            return block(f"offers times that are not free calendar slots: {', '.join(unknown[:3])}", "invented_facts")
        if not p.slots:
            reasons.append("no times offered: a person picks them")
    check_text = text if not isinstance(p, FieldUpdate) or p.field in GROUNDED_FIELDS else ""
    invented = ungrounded(check_text, evidence, pctx.company)
    if isinstance(p, FieldUpdate) and p.field in GROUNDED_FIELDS and isinstance(p.value, str) and p.value.strip():
        words = claims_mod.content_words(p.value)
        if words and len(words & claims_mod.content_words(evidence)) / len(words) < 0.6:
            invented.append(f"the value {p.value[:60]!r}")
    if invented:
        if external:
            return block("states what the evidence does not show: " + ", ".join(invented[:4]), "invented_facts")
        reasons.append("not found in the evidence: " + ", ".join(invented[:4]))
        checks["invented_facts"] = "fail"
    else:
        checks["invented_facts"] = "pass"

    # 6. approved claims
    if external:
        unapproved = claims_mod.unapproved_statements(text, pctx.claims or [], pctx.company)
        if unapproved:
            return block(f'says something about us that is no approved claim: "{unapproved[0][:120]}"', "claims")
        checks["claims"] = "pass"

    # 8. the state machine
    if isinstance(p, FieldUpdate) and p.field == "conversation_state":
        ok, why = state_mod.check_transition(pctx.current_state, str(p.value), actor="ai")
        if not ok:
            return block(f"state move refused: {why}", "state")
        checks["state"] = "pass"

    # escalations, and injection signals for what is internal
    if pctx.injection:
        reasons.append(f'the prospect\'s text tries to instruct the AI ("{pctx.injection[0][:60]}"): a person decides')
    for e in pctx.escalations:
        reasons.append(f"escalation: {e.why}")

    # 9. confidence, then the route
    if external:
        if p.confidence < th["external_min_confidence"]:
            return block(f"the model is only {p.confidence:.0%} sure: no draft", "confidence")
        reasons.append("REQUIRE_HUMAN_APPROVAL is on: a person approves anything a prospect sees" if pctx.require_human_approval
                       else "autonomous external actions are not part of this phase: a person approves")
        return PolicyDecision(Verdict.HUMAN_REVIEW, reasons, p.kind, p, raw_dict, checks, [e.code for e in pctx.escalations])
    if p.kind == "handover":
        reasons.append("a person accepts a handover")
        return PolicyDecision(Verdict.HUMAN_REVIEW, reasons, p.kind, p, raw_dict, checks, [e.code for e in pctx.escalations])
    assert p.kind in INTERNAL_KINDS
    if p.confidence < th["auto_min_confidence"]:
        reasons.append(f"the model is {p.confidence:.0%} sure, below {th['auto_min_confidence']:.0%}: a person checks")
    if reasons:
        return PolicyDecision(Verdict.HUMAN_REVIEW, reasons, p.kind, p, raw_dict, checks, [e.code for e in pctx.escalations])
    return PolicyDecision(Verdict.AUTO_INTERNAL, ["an internal, reversible CRM note or field: recorded and logged"], p.kind, p,
                          raw_dict, checks, [])
