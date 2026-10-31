"""Content calendar slot computation (posting days/time in US Eastern by default), and the US holidays the send rule keeps."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.config import Settings
from aime.models import ContentItem
from aime.timeutil import as_aware_utc, to_naive_utc, utcnow

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HELD = ("approved", "awaiting_approval", "draft", "rejected")  # every other status has gone out, or may have


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """As US offices keep it: a holiday on a Saturday on the Friday before, on a Sunday on the Monday after."""
    return day + timedelta(days={5: -1, 6: 1}.get(day.weekday(), 0))


@lru_cache(maxsize=16)
def us_holidays(year: int) -> dict[str, tuple[str, date]]:
    """The holidays of a year by the name used in `[followup] holidays`: (what a person reads, the day it is kept)."""
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    return {
        "new_years_day": ("New Year's Day", _observed(date(year, 1, 1))),
        "mlk_day": ("Martin Luther King Jr. Day", _nth_weekday(year, 1, 0, 3)),
        "presidents_day": ("Presidents' Day", _nth_weekday(year, 2, 0, 3)),
        "memorial_day": ("Memorial Day", _last_weekday(year, 5, 0)),
        "juneteenth": ("Juneteenth", _observed(date(year, 6, 19))),
        "independence_day": ("Independence Day", _observed(date(year, 7, 4))),
        "labor_day": ("Labor Day", _nth_weekday(year, 9, 0, 1)),
        "columbus_day": ("Columbus Day", _nth_weekday(year, 10, 0, 2)),
        "veterans_day": ("Veterans Day", _observed(date(year, 11, 11))),
        "thanksgiving": ("Thanksgiving", thanksgiving),
        "day_after_thanksgiving": ("The day after Thanksgiving", thanksgiving + timedelta(days=1)),
        "christmas": ("Christmas Day", _observed(date(year, 12, 25))),
    }


def holiday_on(day: date, entries: list[str] | None) -> str | None:
    """The holiday kept on this day, if the configured list has it. An entry is a name from `us_holidays`, or a date
    ("2026-12-24"). New Year's Day on a Saturday is kept in the year before, so the next year is looked at too."""
    for entry in entries or ():
        for year in (day.year, day.year + 1):
            found = us_holidays(year).get(entry)
            if found and found[1] == day:
                return found[0]
        if entry == day.isoformat():
            return "This day"
    return None


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


def requeue_late(session: Session, settings: Settings, late: list[ContentItem], now: datetime) -> tuple[list[ContentItem], list[ContentItem]]:
    """New slots for approved posts that missed theirs. Approvals and their order are kept: late posts come first, in
    their old order, then the approved posts after them, each keeping its time unless that would put it before an
    earlier one (never moved to an earlier time). A draft nobody approved gives way, to the next free slot. Returns
    (approved posts moved, drafts moved); with no posting day configured nothing can move."""
    items = session.execute(select(ContentItem).where(ContentItem.scheduled_for.is_not(None))).scalars().all()
    fixed = {c.scheduled_for for c in items if c.status not in HELD}
    drafts = sorted((c for c in items if c.status in ("awaiting_approval", "draft")), key=lambda c: c.scheduled_for)
    later = sorted((c for c in items if c.status == "approved" and c.scheduled_for > now), key=lambda c: c.scheduled_for)
    late = sorted(late, key=lambda c: c.scheduled_for)
    horizon = int(settings.section("content").get("queue_horizon_days", 14)) + 7 * (len(late) + len(later) + len(drafts))
    slots = [s for s in candidate_slots(settings, now, horizon) if s not in fixed]
    moved: list[ContentItem] = []
    used: set[datetime] = set()
    last = now
    for post in [*late, *later]:
        if post.scheduled_for > last and post not in late:
            last = post.scheduled_for
            used.add(last)
            continue
        slot = next((s for s in slots if s > last), None)
        if slot is None:
            return moved, []
        post.scheduled_for, last = slot, slot
        used.add(slot)
        moved.append(post)
    bumped: list[ContentItem] = []
    for draft in drafts:
        if draft.scheduled_for in used:
            taken = used | {d.scheduled_for for d in drafts}
            slot = next((s for s in slots if s > draft.scheduled_for and s not in taken), None)
            if slot is not None:
                draft.scheduled_for = slot
                bumped.append(draft)
    return moved, bumped
