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
         probability: int | None = None, settings=None) -> bool:
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
        opp.probability = probability_of(settings, stage) if settings is not None else PROBABILITY.get(stage, 0)
    if stage in CLOSED:
        opp.closed_at = utcnow()
        opp.won_by = actor if stage == "won" else None
    else:
        opp.closed_at, opp.lost_reason, opp.won_by = None, None, None
    record(session, opp, old, stage, actor, reason)
    return True


# ------------------------------------------------------------------ the pipeline's stages (Settings › Sales settings)
# Open stages in order with their win probability; "new" and "discovery" are where the agents open deals, so they
# stay; "won" and "lost" always close the pipeline. Example: "new:10, discovery:20, qualification:30, proposal:50,
# negotiation:70".
DEFAULT_STAGES = "new:10, discovery:20, proposal:50, negotiation:70"
REQUIRED = ("new", "discovery")


def parse_stages(raw: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for part in (raw or DEFAULT_STAGES).split(","):
        name, _, pct = part.strip().partition(":")
        name = name.strip().lower().replace(" ", "_")
        if not name:
            continue
        if not (3 <= len(name) <= 20 and name.replace("_", "").isalpha()) or name in CLOSED:
            raise ValueError(f"{name!r}: stage names are 3-20 letters (won and lost are added by themselves)")
        try:
            value = int(pct.strip() or "0")
        except ValueError:
            raise ValueError(f"{name}: the probability must be a whole number") from None
        if not 0 <= value <= 99:
            raise ValueError(f"{name}: the probability must be 0 to 99")
        if name in dict(out):
            raise ValueError(f"{name} appears twice")
        out.append((name, value))
    missing = [r for r in REQUIRED if r not in dict(out)]
    if missing:
        raise ValueError(f"the pipeline needs {' and '.join(missing)}: the agents open deals there")
    return out


def pipeline(settings) -> list[tuple[str, int]]:
    try:
        open_stages = parse_stages(str(settings.section("pipeline").get("stages") or DEFAULT_STAGES))
    except ValueError:
        open_stages = parse_stages(DEFAULT_STAGES)
    return [*open_stages, ("won", 100), ("lost", 0)]


def stage_names(settings) -> list[str]:
    return [name for name, _ in pipeline(settings)]


def probability_of(settings, stage: str) -> int:
    return dict(pipeline(settings)).get(stage, PROBABILITY.get(stage, 0))
