"""When a person must take the conversation over. Each rule has a code (stored with the decision and counted on the AI
quality page) and plain words:

- low_confidence: the model is not sure what the prospect means (below `ai.escalation.min_confidence`, default 0.6);
- safety_floor: the rules read an opt-out, a complaint or a bounce;
- sensitive_topic: prices, costs, budgets, discounts, contracts, guarantees or legal language, in the prospect's words
  (aime.automation.SENSITIVE and the reply classifier's PRICING words);
- upset: the prospect is angry or annoyed (the model's reading or the rules' UPSET words);
- unanswered_ai_turns: two AI-proposed turns already went without an answer from a person or the prospect;
- human_requested: they ask for a real person, or whether this is a bot;
- injection_suspected: the text tries to give the AI orders;
- model_failed: no valid answer from the model (down, or not in the asked form);
- manager_condition: a word the manager listed in `ai.escalation.keywords`.

Time-based escalation (a reply nobody answered in time) stays with aime.assignment."""

from __future__ import annotations

import re
from dataclasses import dataclass

from aime.automation import SENSITIVE

HUMAN_REQUEST = re.compile(r"\b((talk|speak|chat) (to|with) (a |an )?(real |actual )?(person|human|someone|manager|sales ?(rep|person))|"
                           r"(are|is) (you|this) (a |an )?(bot|robot|ai|machine|automated|real person|human)|real (person|human)|"
                           r"not (a |an )?(bot|robot|machine))\b", re.I)
MAX_UNANSWERED_AI_TURNS = 2


@dataclass(frozen=True)
class Escalation:
    code: str
    why: str


def assess(text: str, floor=None, analysis=None, ai_turns_unanswered: int = 0, injection: list[str] | None = None,
           model_error: str | None = None, cfg: dict | None = None) -> list[Escalation]:
    from aime.reply_intel import UPSET

    cfg = cfg or {}
    min_conf = float(cfg.get("min_confidence", 0.6))
    out: list[Escalation] = []
    if model_error:
        out.append(Escalation("model_failed", f"no valid answer from the model: {model_error[:120]}"))
    elif analysis is not None and analysis.confidence < min_conf:
        out.append(Escalation("low_confidence", f"the model is {analysis.confidence:.0%} sure, below {min_conf:.0%}"))
    if floor is not None and floor.hit:
        out.append(Escalation("safety_floor", f"the rules read this as {'a complaint' if floor.complaint else floor.category}"))
    from aime.reply_intel import PRICING

    sensitive = SENSITIVE.search(text or "") or PRICING.search(text or "")
    if sensitive or (floor is not None and floor.legal):
        word = sensitive.group(0) if sensitive else "legal language"
        out.append(Escalation("sensitive_topic", f'they mention "{word}": prices, contracts and legal matters need a person'))
    if (analysis is not None and analysis.upset) or UPSET.search(text or ""):
        out.append(Escalation("upset", "the prospect sounds upset"))
    if ai_turns_unanswered >= MAX_UNANSWERED_AI_TURNS:
        out.append(Escalation("unanswered_ai_turns", f"{ai_turns_unanswered} AI-proposed turns went unanswered"))
    if HUMAN_REQUEST.search(text or "") or (analysis is not None and analysis.asks_for_human):
        out.append(Escalation("human_requested", "they ask for a person"))
    if injection:
        out.append(Escalation("injection_suspected", f'the text tries to instruct the AI: "{injection[0][:80]}"'))
    for word in cfg.get("keywords") or []:
        if isinstance(word, str) and word.strip() and re.search(rf"\b{re.escape(word.strip())}\b", text or "", re.I):
            out.append(Escalation("manager_condition", f'the manager asked to see replies that mention "{word.strip()}"'))
            break
    return out
