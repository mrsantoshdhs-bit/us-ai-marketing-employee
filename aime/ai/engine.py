"""The shadow analysis of a prospect's reply: the rule floor, the model's structured reading, proposals, the policy
verdict for each, and the decision log. Nothing here changes a lead, sends or books: the only rows written are
ai_decisions.

How the readings combine (`combine`): the rules always win for an opt-out, a complaint or a bounce; a confident rule
reading (high confidence) stands; otherwise the model's reading is used when it is confident enough, and a model that
reads an opt-out or a complaint the rules missed is followed, since that only makes the system more careful. The model
can add caution, never remove it.

With no model (provider template / none, the server down, or an answer that stays invalid after the repair attempt)
the analysis is the rules' alone and the decision says so: the behaviour degrades to what runs today."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aime.ai import decisions, prompts
from aime.ai.escalation import Escalation, assess
from aime.ai.guard import Floor, injection_signals, rule_floor
from aime.ai.policy import PolicyContext, PolicyDecision, evaluate
from aime.ai.schemas import ReplyAnalysis
from aime.ai.state import state_of_lead
from aime.llm.base import LLMError, LLMProvider, StructuredOutputError
from aime.llm.structured import quote_data, structured_call

TASK = "reply_analysis"
AGENT = "conversation_shadow"
STOP_INTENTS = ("unsubscribe", "complaint")


@dataclass
class AnalysisOutcome:
    floor: Floor
    analysis: ReplyAnalysis | None
    final_intent: str
    meeting_intent: bool
    injection: list[str]
    escalations: list[Escalation]
    prompt_ref: str
    provider: str = "none"
    model: str | None = None
    structured_ok: bool | None = None
    attempts: int = 0
    repaired: bool = False
    ms: int = 0
    tokens: int = 0
    error: str | None = None
    raw: str = ""


def combine(floor: Floor, analysis: ReplyAnalysis | None, min_confidence: float = 0.6) -> str:
    if floor.hit:
        return floor.as_intent
    if analysis is not None and analysis.intent in STOP_INTENTS:
        return analysis.intent  # more careful than the rules: followed, and a person checks
    if floor.confidence == "high" and floor.category not in ("other",):
        return floor.as_intent
    if analysis is not None and analysis.confidence >= min_confidence:
        return analysis.intent
    return floor.as_intent


def analyse(llm: LLMProvider | None, text: str, history: list[str] | tuple = (), subject: str = "", company: str = "our company",
            ai_turns_unanswered: int = 0, escalation_cfg: dict | None = None) -> AnalysisOutcome:
    floor = rule_floor(text, subject)
    injection = injection_signals(f"{subject}\n{text}")
    prompt = prompts.get(TASK)
    out = AnalysisOutcome(floor=floor, analysis=None, final_intent=floor.as_intent, meeting_intent=floor.intent == "meeting_request",
                          injection=injection, escalations=[], prompt_ref=prompt.ref)
    if llm is not None:
        out.provider, out.model = llm.name, llm.model_name
        thread = "\n".join(quote_data("EARLIER_MESSAGE", h, 1200) for h in list(history)[-6:]) or "(none)"
        system, user = prompt.render(company=company, history=thread,
                                     reply=quote_data("PROSPECT_EMAIL", f"Subject: {subject}\n\n{text}" if subject else text))
        try:
            res = structured_call(llm, user, ReplyAnalysis, system=system, max_tokens=400, temperature=0.0)
            out.analysis, out.structured_ok, out.attempts, out.repaired = res.value, True, res.attempts, res.repaired
            out.ms, out.tokens, out.raw = res.ms, res.tokens, res.raw
        except StructuredOutputError as exc:
            out.structured_ok, out.attempts, out.error, out.raw = False, exc.attempts, f"invalid answer: {'; '.join(exc.problems)[:200]}", exc.raw
        except LLMError as exc:
            out.structured_ok, out.error = False, f"model error: {exc}"[:300]
    min_conf = float((escalation_cfg or {}).get("min_confidence", 0.6))
    out.final_intent = combine(floor, out.analysis, min_conf)
    out.meeting_intent = (floor.intent == "meeting_request" or bool(out.analysis and out.analysis.meeting_intent)) and \
        out.final_intent not in STOP_INTENTS
    out.escalations = assess(text, floor, out.analysis, ai_turns_unanswered, injection,
                             out.error if llm is not None else None, escalation_cfg)
    return out


_STATE_FOR_INTENT = {"interested": "discovering", "question": "engaged", "objection": "engaged", "referral": "engaged",
                     "not_interested": "closed_lost"}


def proposals_from(outcome: AnalysisOutcome, lead_id: int) -> list[dict]:
    """The internal proposals an analysis gives: the facts it extracted, the state move, a handover when a rule of
    aime.ai.escalation fired. A reply draft is no part of the shadow analysis (Phase 3)."""
    a = outcome.analysis
    conf = a.confidence if a is not None else (0.9 if outcome.floor.confidence == "high" else 0.5)
    out: list[dict] = []
    if a is not None:
        if a.requirements:
            out.append({"kind": "field_update", "lead_id": lead_id, "field": "requirements", "value": "; ".join(a.requirements),
                        "confidence": conf})
        for name in ("budget", "timeline"):
            if getattr(a, name):
                out.append({"kind": "field_update", "lead_id": lead_id, "field": name, "value": getattr(a, name), "confidence": conf})
        if a.objection != "none":
            out.append({"kind": "field_update", "lead_id": lead_id, "field": "objection", "value": a.objection, "confidence": conf})
        if a.summary:
            out.append({"kind": "internal_note", "lead_id": lead_id, "text": a.summary, "confidence": conf})
    if outcome.meeting_intent:
        out.append({"kind": "field_update", "lead_id": lead_id, "field": "meeting_intent", "value": True, "confidence": conf})
    target = "scheduling" if outcome.meeting_intent else _STATE_FOR_INTENT.get(outcome.final_intent)
    if target:
        out.append({"kind": "field_update", "lead_id": lead_id, "field": "conversation_state", "value": target, "confidence": conf})
    if outcome.escalations:
        out.append({"kind": "handover", "lead_id": lead_id, "reason": "; ".join(e.why for e in outcome.escalations)[:400],
                    "summary": (a.summary if a else "") or f"rules read: {outcome.floor.category}", "confidence": 0.9})
    return out


@dataclass
class ShadowResult:
    outcome: AnalysisOutcome
    decisions: list[tuple[int, PolicyDecision]] = field(default_factory=list)


def _evidence(session: Session, settings, lead_id: int, text: str) -> tuple[list[str], list[str], Any]:
    from aime.ai.tools import ToolRegistry

    reg = ToolRegistry(session, settings, lead_id)
    history = reg.call("get_thread_history", {"limit": 10})
    facts = [str(reg.call("get_lead")), str(reg.call("get_company")), str(reg.call("get_research_snapshot"))]
    earlier = [h["text"] for h in history if h["text"] and h["text"] != text]
    return earlier, [text, *earlier, *facts], reg


def shadow_reply(ctx, session: Session, inbound_id: int, llm: LLMProvider | None = None, router=None) -> ShadowResult:
    """Analyse one stored reply in shadow: proposals are evaluated and logged, nothing else changes."""
    from aime.crm_ops import is_suppressed
    from aime.llm.base import ProviderRefused
    from aime.llm.router import ModelRouter, ai_config
    from aime.models import InboundEmail, Lead

    row = session.get(InboundEmail, inbound_id)
    if row is None or row.lead_id is None:
        raise LookupError(f"reply {inbound_id} has no lead to analyse")
    settings = ctx.settings
    cfg = ai_config(settings)
    if llm is None:
        try:
            llm = (router or ModelRouter(settings, transport=getattr(ctx, "http_transport", None))).provider_for(TASK)
        except ProviderRefused:
            llm = None
    text = row.new_text or row.body_text or ""
    earlier, evidence, _reg = _evidence(session, settings, row.lead_id, text)
    outcome = analyse(llm, text, earlier, row.subject or "", settings.company_name, escalation_cfg=cfg.get("escalation") or {})
    lead = session.get(Lead, row.lead_id)
    current = state_of_lead(lead)
    if current in ("new", "contacted", "nurture"):
        current = "engaged"  # their reply is itself that move (the inbox rules make it on the lead); the AI proposes the next
    pctx = PolicyContext(settings=settings, lead_id=row.lead_id, floor=outcome.floor, evidence=evidence,
                         suppressed=bool(row.from_email and is_suppressed(session, row.from_email)),
                         current_state=current, injection=outcome.injection, escalations=outcome.escalations)
    result = ShadowResult(outcome)
    props = proposals_from(outcome, row.lead_id)
    share_tokens = outcome.tokens // max(1, len(props))
    for raw in props or [{"kind": "internal_note", "lead_id": row.lead_id, "text": f"rules read: {outcome.floor.category}",
                          "confidence": 0.5}]:
        d = evaluate(raw, pctx)
        rec = decisions.record(session, agent=AGENT, task=TASK, verdict=d.verdict, proposal=raw, reasons=d.reasons,
                               escalations=d.escalations, prompt_version=outcome.prompt_ref, provider=outcome.provider,
                               model=outcome.model, lead_id=row.lead_id, input_ref=f"inbound_email:{row.id}", kind=d.kind,
                               structured_ok=outcome.structured_ok, error=outcome.error, tokens=share_tokens, ms=outcome.ms)
        result.decisions.append((rec.id, d))
    return result


class DraftOut(BaseModel):
    body: str
    confidence: float = Field(ge=0.0, le=1.0)


def draft_reply(llm: LLMProvider, text: str, history: list[str] | tuple = (), company: str = "our company",
                claims: list[str] | tuple = (), lead_id: int = 1) -> tuple[dict | None, Any]:
    """A model-written answer as a ReplyDraft proposal (dict) for the policy engine, and the StructuredResult or the
    error. Used by the evaluation and the red-team runs only in this phase: no draft reaches the approval queue."""
    prompt = prompts.get("reply_draft")
    thread = "\n".join(quote_data("EARLIER_MESSAGE", h, 1200) for h in list(history)[-6:]) or "(none)"
    system, user = prompt.render(company=company, claims="\n".join(f"- {c}" for c in claims) or "- (none)", history=thread,
                                 reply=quote_data("PROSPECT_EMAIL", text))
    try:
        res = structured_call(llm, user, DraftOut, system=system, max_tokens=300, temperature=0.2)
    except (StructuredOutputError, LLMError) as exc:
        return None, exc
    return {"kind": "reply", "lead_id": lead_id, "body": res.value.body or " ", "confidence": res.value.confidence}, res
