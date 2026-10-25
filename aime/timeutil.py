"""Time helpers. The database stores naive UTC datetimes."""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_aware_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def local_tz(name: str | None = None) -> tzinfo:
    if not name or name == "local":
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]
    return ZoneInfo(name)


def fmt_local(dt: datetime | None, tz_name: str | None = None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:
        return "—"
    return as_aware_utc(dt).astimezone(local_tz(tz_name)).strftime(fmt)


def fmt_et(dt: datetime | None, fmt: str = "%a %b %d, %I:%M %p ET") -> str:
    if dt is None:
        return "—"
    return as_aware_utc(dt).astimezone(ET).strftime(fmt)
