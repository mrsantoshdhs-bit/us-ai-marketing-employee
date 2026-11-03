"""The AI decision log (table ai_decisions): every proposal, whatever its verdict, with the model, the prompt version,
tokens, time, the policy's reasons and, once a person has acted on it, what they did.

Text is redacted with the same rules as the logs (aime.redact: secrets and email addresses) before it is stored, at
every depth of the proposal. `record_human_action` stores a person's accepted / edited / rejected, and for an edit the
edit distance between the proposed and the final text (characters, Levenshtein), so agreement with people can be
measured on the AI quality page."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aime.models import AIDecision
from aime.redact import redact
from aime.timeutil import utcnow

HUMAN_ACTIONS = ("accepted", "edited", "rejected")


def scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(value, str):
        return redact(value)[:4000]
    if isinstance(value, dict):
        return {str(k)[:60]: scrub(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple)):
        return [scrub(v, depth + 1) for v in list(value)[:60]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))[:400]


def record(session: Session, *, agent: str, task: str, verdict: str, proposal: dict | None = None, reasons: list[str] | None = None,
           escalations: list[str] | None = None, prompt_version: str | None = None, provider: str = "none", model: str | None = None,
           lead_id: int | None = None, input_ref: str | None = None, kind: str | None = None, structured_ok: bool | None = None,
           error: str | None = None, tokens: int = 0, ms: int = 0) -> AIDecision:
    row = AIDecision(created_at=utcnow(), agent=agent[:60], task=task[:60], prompt_version=(prompt_version or "")[:80] or None,
                     provider=(provider or "none")[:30], model=(model or "")[:80] or None, lead_id=lead_id,
                     input_ref=(input_ref or "")[:120] or None, proposal=scrub(proposal or {}), kind=(kind or "")[:30] or None,
                     verdict=str(getattr(verdict, "value", verdict))[:20], reasons=scrub(list(reasons or []))[:20],
                     escalations=list(escalations or [])[:20], structured_ok=structured_ok,
                     error=(redact(error) or "")[:300] or None, tokens=max(0, int(tokens or 0)), ms=max(0, int(ms or 0)))
    session.add(row)
    session.flush()
    return row


def levenshtein(a: str, b: str, limit: int = 2000) -> int:
    a, b = (a or "")[:limit], (b or "")[:limit]
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def record_human_action(session: Session, decision_id: int, action: str, actor: str, final_text: str | None = None) -> AIDecision:
    if action not in HUMAN_ACTIONS:
        raise ValueError(f"human action must be one of {', '.join(HUMAN_ACTIONS)}")
    row = session.get(AIDecision, decision_id)
    if row is None:
        raise LookupError(f"no AI decision {decision_id}")
    row.human_action, row.human_actor, row.human_at = action, (actor or "")[:80], utcnow()
    proposed = (row.proposal or {}).get("body") or (row.proposal or {}).get("message") or (row.proposal or {}).get("text") or ""
    if action == "accepted":
        row.edit_distance = 0
    elif action == "edited" and final_text is not None:
        row.edit_distance = levenshtein(proposed, redact(final_text) or "")
    return row
