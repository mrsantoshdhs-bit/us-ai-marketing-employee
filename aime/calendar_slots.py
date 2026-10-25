"""Content calendar slot computation (posting days/time in US Eastern by default)."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.config import Settings
from aime.models import ContentItem
from aime.timeutil import as_aware_utc, to_naive_utc, utcnow

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def candidate_slots(settings: Settings, start_utc: datetime, horizon_days: int) -> list[datetime]:
    cfg = settings.section("content")
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    days = {d[:3].title() for d in cfg.get("posting_days", ["Tue", "Wed", "Thu"])}
    hh, mm = (int(x) for x in str(cfg.get("posting_time", "09:30")).split(":"))
    start_local = as_aware_utc(start_utc).astimezone(tz)
    out = []
    for offset in range(0, horizon_days + 1):
        d = (start_local + timedelta(days=offset)).date()
        if DAY_NAMES[d.weekday()] not in days:
            continue
        slot = datetime.combine(d, time(hh, mm), tzinfo=tz)
        if slot.astimezone(timezone.utc) <= as_aware_utc(start_utc):
            continue
        out.append(to_naive_utc(slot))
    return out


def free_slots(session: Session, settings: Settings, now: datetime | None = None, horizon_days: int | None = None) -> list[datetime]:
    now = now or utcnow()
    horizon = horizon_days or int(settings.section("content").get("queue_horizon_days", 14))
    taken = {
        row for row in session.execute(
            select(ContentItem.scheduled_for).where(ContentItem.status != "rejected",
                                                    ContentItem.scheduled_for.is_not(None))).scalars()
    }
    return [s for s in candidate_slots(settings, now, horizon) if s not in taken]
