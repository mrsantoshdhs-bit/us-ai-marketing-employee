"""Moving a deal through the pipeline: one place that sets the stage, its probability and close date, credits the
person who won it, and keeps the stage history (table opportunity_stage_changes)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from aime.models import Opportunity, StageChange
from aime.timeutil import utcnow

PROBABILITY = {"new": 10, "discovery": 20, "proposal": 50, "negotiation": 70, "won": 100, "lost": 0}
CLOSED = ("won", "lost")


def record(session: Session, opp: Opportunity, from_stage: str | None, to_stage: str, actor: str,
           reason: str | None = None) -> None:
    session.flush()  # the deal's id
    session.add(StageChange(opportunity_id=opp.id, from_stage=from_stage, to_stage=to_stage, actor=actor,
                            reason=(reason or None) and reason[:300]))


def opened(session: Session, opp: Opportunity, actor: str, reason: str) -> None:
    record(session, opp, None, opp.stage, actor, reason)


def move(session: Session, opp: Opportunity, stage: str, actor: str, reason: str | None = None,
         probability: int | None = None) -> bool:
    """Move the deal to `stage` (probability: the stage's, unless given). Closing sets the close date; won credits
    the actor; reopening clears the close date, the lost reason and the credit. Returns whether the stage changed
    (a given probability is applied either way)."""
    if probability is not None:
        opp.probability = probability
    old = opp.stage
    if old == stage:
        return False
    opp.stage = stage
    if probability is None:
        opp.probability = PROBABILITY[stage]
    if stage in CLOSED:
        opp.closed_at = utcnow()
        opp.won_by = actor if stage == "won" else None
    else:
        opp.closed_at, opp.lost_reason, opp.won_by = None, None, None
    record(session, opp, old, stage, actor, reason)
    return True
