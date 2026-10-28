"""Sales settings a Super Admin changes from the dashboard (/admin/settings) instead of config/business.toml:
target country, states, industries and services; qualification thresholds; sending limits; the follow-up sequence;
meeting hours; and the Sales Manager that interested prospects and booked meetings are handed over to.

The file stays the default. Changes are stored as overrides in the database (kv "admin.business_overrides") and laid
over the file wherever the settings are loaded: the dashboard at once, the scheduled jobs on their next run. "Reset"
removes every override. Each field is validated, and every change is in the audit log with the old and new value.
Switches that release email (OUTBOUND_EMAIL_ENABLED, REQUIRE_HUMAN_APPROVAL) are deliberately not here.
"""

from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.db import kv_get, kv_set

KEY = "admin.business_overrides"
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
COUNTRY_SOURCES = {"US": "Hacker News hiring posts, SEC EDGAR filings, US Census market data"}


def _int(lo: int, hi: int):
    def check(raw: str) -> int:
        try:
            value = int(str(raw).strip())
        except ValueError as exc:
            raise ValueError("must be a whole number") from exc
        if not lo <= value <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return value
    return check


def _hours(raw: str) -> str:
    start, sep, end = str(raw).strip().partition("-")
    if not sep or not HHMM.match(start) or not (HHMM.match(end) or end == "24:00") or start >= end:
        raise ValueError("must look like 09:00-17:00")
    return f"{start}-{end}"


def _clock(raw: str) -> str:
    if not HHMM.match(str(raw).strip()):
        raise ValueError("must look like 08:00")
    return str(raw).strip()


def _zone(raw: str) -> str:
    try:
        ZoneInfo(str(raw).strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("must be a time zone such as America/New_York or Asia/Kolkata") from exc
    return str(raw).strip()


def _country(raw: str) -> str:
    code = str(raw).strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        raise ValueError("must be a two-letter country code such as US")
    return code


def _states(raw: str) -> str | list[str]:
    from aime.knowledge.geo import STATES

    text = str(raw).strip().upper()
    if text in ("", "ALL"):
        return "ALL"
    codes = [c.strip() for c in text.replace(";", ",").split(",") if c.strip()]
    bad = [c for c in codes if c not in STATES]
    if bad:
        raise ValueError(f"unknown US state code(s): {', '.join(bad)}")
    return codes


def _days(raw: str) -> list[int]:
    try:
        days = [int(x) for x in str(raw).replace(";", ",").split(",") if x.strip()]
    except ValueError as exc:
        raise ValueError("must be day numbers such as 0, 4, 10") from exc
    if not days or days[0] != 0 or any(b <= a for a, b in zip(days, days[1:], strict=False)) or days[-1] > 60:
        raise ValueError("must start at 0 and increase, such as 0, 4, 10 (at most 60)")
    return days


def _bool(raw: str) -> bool:
    return str(raw).lower() in ("yes", "on", "true", "1")


# (section, key, label, check, kind) - kind decides the form control: text | number | multi | bool | user
FIELDS: list[tuple[str, str, str, Any, str]] = [
    ("targeting", "country", "Target country", _country, "text"),
    ("targeting", "states", "US states (ALL, or codes such as CA, TX, NY)", _states, "text"),
    ("targeting", "industries", "Target industries", None, "multi_industries"),
    ("targeting", "services", "Services promoted", None, "multi_services"),
    ("qualification", "grade_a", "Grade A from score", _int(1, 100), "number"),
    ("qualification", "grade_b", "Grade B from score", _int(1, 100), "number"),
    ("qualification", "grade_c", "Grade C from score", _int(1, 100), "number"),
    ("outreach", "min_grade", "Draft outreach for grade (A, B or C) and better", None, "grade"),
    ("outreach", "daily_send_limit", "Outreach emails a day", _int(1, 200), "number"),
    ("outreach", "per_domain_daily_limit", "Emails per company domain a day", _int(1, 10), "number"),
    ("outreach", "sends_per_run", "Outreach emails per run (every 15 min)", _int(1, 50), "number"),
    ("outreach", "mailbox_daily_limit", "All emails per mailbox in 24 hours", _int(1, 500), "number"),
    ("outreach", "max_touches", "Emails in a sequence", _int(1, 5), "number"),
    ("outreach", "intervals_days", "Days of each sequence email (0, 4, 10)", _days, "text"),
    ("followup", "send_window_start", "Send window starts (prospect time)", _clock, "text"),
    ("followup", "send_window_end", "Send window ends", _clock, "text"),
    ("followup", "send_window_timezone", "Send window time zone", _zone, "text"),
    ("meetings", "owner_timezone", "Your time zone", _zone, "text"),
    ("meetings", "owner_hours", "Your hours for calls", _hours, "text"),
    ("meetings", "prospect_hours", "Prospect business hours", _hours, "text"),
    ("meetings", "slot_minutes", "Meeting length (minutes)", _int(15, 120), "number"),
    ("meetings", "slots_offered", "Times offered", _int(1, 5), "number"),
    ("meetings", "min_notice_hours", "Minimum notice (hours)", _int(0, 168), "number"),
    ("meetings", "reminder_hours", "Reminder before a meeting (hours)", _int(1, 72), "number"),
    ("meetings", "remind_prospect", "Also draft a reminder for the prospect", _bool, "bool"),
    ("handover", "sales_manager", "Sales Manager for handovers", None, "user"),
]


def overrides(session: Session) -> dict:
    return kv_get(session, KEY) or {}


def apply(settings, values: dict) -> None:
    """Lay overrides over the settings' business configuration (in place: every reader sees them)."""
    for section, keys in (values or {}).items():
        settings.business.setdefault(section, {}).update(keys)


def load(session: Session, settings) -> None:
    apply(settings, overrides(session))


def current(settings) -> dict:
    return {f"{s}.{k}": settings.section(s).get(k) for s, k, *_ in FIELDS}


def _choice_check(session: Session, kind: str, raw):
    from aime.knowledge.industries import INDUSTRIES
    from aime.knowledge.services import SERVICES
    from aime.models import User

    if kind == "multi_industries":
        picked = [x for x in raw if x in INDUSTRIES]
        if not picked:
            raise ValueError("choose at least one industry")
        return picked
    if kind == "multi_services":
        picked = [x for x in raw if x in SERVICES]
        if not picked:
            raise ValueError("choose at least one service")
        return picked
    if kind == "grade":
        if raw not in ("A", "B", "C"):
            raise ValueError("must be A, B or C")
        return raw
    if kind == "user":
        if not raw:
            return ""
        names = set(session.execute(select(User.username).where(User.active.is_(True))).scalars())
        if raw not in names:
            raise ValueError("must be an active dashboard user")
        return raw
    raise ValueError("unknown field")


def save(session: Session, settings, actor: str, form) -> list[str]:
    """Validate every field, store the overrides, apply them now and audit the changes. Returns the changes."""
    from aime.automation import audit

    getlist = form.getlist if hasattr(form, "getlist") else (lambda k: form.get(k) or [])
    new: dict[str, dict] = {}
    errors = []
    for section, key, label, check, kind in FIELDS:
        name = f"{section}.{key}"
        try:
            if kind.startswith("multi"):
                value = _choice_check(session, kind, getlist(name))
            elif kind in ("grade", "user"):
                value = _choice_check(session, kind, (form.get(name) or "").strip())
            elif kind == "bool":
                value = _bool(form.get(name) or "")
            else:
                value = check(form.get(name, ""))
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        new.setdefault(section, {})[key] = value
    q = new.get("qualification", {})
    if not errors and not q.get("grade_a", 2) > q.get("grade_b", 1) > q.get("grade_c", 0):
        errors.append("Grades must go down: A above B above C")
    o = new.get("outreach", {})
    if not errors and len(o.get("intervals_days", [])) != o.get("max_touches"):
        errors.append("Give one day number for each email in the sequence")
    if errors:
        raise ValueError("; ".join(errors))
    before = current(settings)
    stored = overrides(session)
    for section, keys in new.items():
        stored.setdefault(section, {}).update(keys)
    kv_set(session, KEY, stored)
    apply(settings, new)
    after = current(settings)
    changes = [f"{k}: {before[k]!r} -> {after[k]!r}" for k in after if before[k] != after[k]]
    if changes:
        audit(session, actor, "settings_changed", "sales settings", "; ".join(changes)[:2000])
    return changes


def reset(session: Session, settings, actor: str) -> None:
    """Remove every override: back to config/business.toml."""
    from aime.automation import audit
    from aime.config import DEFAULT_BUSINESS, _deep_merge, _load_toml

    kv_set(session, KEY, {})
    fresh = _deep_merge(DEFAULT_BUSINESS, _load_toml(settings.base_dir / "config" / "business.toml"))
    settings.business.clear()
    settings.business.update(fresh)
    audit(session, actor, "settings_reset", "sales settings", "overrides removed: back to config/business.toml")
