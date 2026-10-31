"""Is the PC's clock believable? A wrong clock changes records for good: one tick a year ahead moved 37 of 40 qualified
leads to nurture, and correcting the clock did not bring them back. So a check-in stops before any work when:

- the time since the last check-in is longer than GAP_LIMIT. This PC sleeps: nights, and three-day weekends, are normal.
  Fourteen days lets a two-week absence pass without asking and stays well under the 30 days after which the stale-lead
  rule moves leads (the damage above); what a smaller wrong jump can do (a "stalled" task, a "no response" stage) is
  visible and a person can undo it. A real absence longer than that needs one command;
- or the clock is earlier than the newest record, by more than a small correction.

The stop never gets stuck: it ends by itself when the clock is corrected, and `aime confirm-clock` tells it the clock is
right (a long holiday, or a clock that used to be wrong and left records in the future). The confirmation is a file
outside the database, so restoring a backup does not undo it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from aime.models import JobRun
from aime.timeutil import utcnow

GAP_LIMIT = timedelta(days=14)
BACK_MARGIN = timedelta(minutes=5)  # a time-server correction is no reason to stop
ALERT_KEY = "scheduler:clock"
HOLD_STATE = "clock_hold"
FILENAME = "CLOCK_CONFIRMED.json"
ADVICE = ("If the clock is wrong, correct it (Windows Settings, Date and time) and the work resumes by itself. If it is "
          "right, run `python -m aime confirm-clock`.")


def _parse(stamp) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(stamp).rstrip("Z"))
    except ValueError:
        return None
    return value if value.tzinfo is None else None


def _confirmation(data_dir: Path) -> dict:
    try:
        info = json.loads((Path(data_dir) / FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def _newest_record(ctx) -> datetime | None:
    with ctx.db.session() as s:
        return s.execute(select(func.max(JobRun.started_at))).scalar()


def last_good(beat: dict | None) -> datetime | None:
    """The last check-in on a clock that looked right: a check-in that stopped for the clock does not count."""
    if not beat:
        return None
    return _parse(beat.get("last_good_at") or beat.get("at"))


def _known(ctx, beat: dict | None) -> list[datetime]:
    return [t for t in (last_good(beat), _newest_record(ctx)) if t is not None]


def problem(ctx, beat: dict | None, now: datetime) -> str | None:
    """What is wrong with the clock, in words for the owner, or None."""
    done = _confirmation(ctx.settings.data_dir)
    confirmed_at, accepted = _parse(done.get("at")), _parse(done.get("newest"))
    known = _known(ctx, beat)
    ahead = [t for t in known if t > now + BACK_MARGIN and (accepted is None or t > accepted)]
    if ahead:
        return (f"The PC's clock looks wrong: it says {now:%Y-%m-%d %H:%M} UTC, but records exist up to {max(ahead):%Y-%m-%d %H:%M} "
                f"UTC. No work was done in this check-in, so nothing is changed on a wrong date. {ADVICE}")
    last = max([t for t in known if t <= now + BACK_MARGIN] + ([confirmed_at] if confirmed_at else []), default=None)
    if last is not None and now - last > GAP_LIMIT:
        return (f"The PC's clock looks wrong: it says {now:%Y-%m-%d %H:%M} UTC, {(now - last).days} days after the last check-in "
                f"({last:%Y-%m-%d %H:%M} UTC), and sleep rarely explains more than {GAP_LIMIT.days} days. No work was done in "
                f"this check-in, so nothing is changed on a wrong date. {ADVICE}")
    return None


def anchor(ctx, beat: dict | None) -> str | None:
    """What a check-in that stopped keeps as its reference, so the next one is judged against the same record and not
    against the stopped one's wrong time."""
    done = _parse(_confirmation(ctx.settings.data_dir).get("at"))
    latest = max(_known(ctx, beat) + ([done] if done else []), default=None)
    return latest.isoformat() if latest else None


def confirm(ctx, beat: dict | None) -> datetime:
    """A person says the clock is right now. Records that lie ahead of it stay, and stop counting."""
    now = utcnow()
    path = Path(ctx.settings.data_dir) / FILENAME
    path.write_text(json.dumps({"at": now.isoformat(), "newest": max([*_known(ctx, beat), now]).isoformat()}), encoding="utf-8")
    return now
