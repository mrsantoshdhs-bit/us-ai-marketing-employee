"""The budget guard for contact-source adapters: every call to an adapter goes through `guarded_call`, which refuses before
anything is called or spent when

- the adapter is switched off in the Contact sources settings;
- its status is not ready (a key missing, no owner approval, no budget, no vendor integration);
- the country asked for is outside what it covers, outside what its settings allow, or not a target country;
- the source told us to wait (HTTP 429) and the wait is not over;
- it is paid and its monthly budget is 0, its price per call is not recorded, or this call would take the month over the cap.

The usage ledger (table contact_source_usage) counts each adapter's calls, cost, refusals and errors per calendar month
(UTC). A paid call's cost is booked before the call is made, so a call that fails still counts against the cap: the cap can
be under-used, never overrun. Every refusal and every paid charge is written to the audit log. The ledger is written in the
caller's database session, so it is kept with the work it paid for.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.automation import audit
from aime.contact_sources.base import AdapterRefused, ContactSource, RateLimited
from aime.db import kv_get, kv_set
from aime.models import ContactSourceUsage
from aime.redact import redact
from aime.timeutil import utcnow

APPROVALS_KEY = "contact_sources.approvals"
COOLDOWN_KEY = "contact_sources.cooldown"
ACTOR = "contact_sources"
_last_call: dict[str, float] = {}  # adapter -> time.monotonic() of its last call in this process


def month_key(now: datetime | None = None) -> str:
    return (now or utcnow()).strftime("%Y-%m")


def usage(session: Session, name: str, month: str | None = None) -> ContactSourceUsage | None:
    return session.execute(select(ContactSourceUsage).where(ContactSourceUsage.adapter == name,
                                                            ContactSourceUsage.month == (month or month_key()))).scalars().first()


def last_usage(session: Session, name: str) -> ContactSourceUsage | None:
    return session.execute(select(ContactSourceUsage).where(ContactSourceUsage.adapter == name)
                           .order_by(ContactSourceUsage.month.desc())).scalars().first()


def _row(session: Session, name: str) -> ContactSourceUsage:
    row = usage(session, name)
    if row is None:
        row = ContactSourceUsage(adapter=name, month=month_key(), calls=0, cost_usd=0.0, refused=0, errors=0)
        session.add(row)
        session.flush()
    return row


# --------------------------------------------------------------------------- the owner's approval

def approval(session: Session, name: str) -> dict | None:
    return (kv_get(session, APPROVALS_KEY, {}) or {}).get(name)


def record_approval(session: Session, name: str, approved_by: str, actor: str, note: str = "") -> dict:
    """The owner approved this source (a paid provider or verifier). It does not switch anything on: the budget, the
    price, the credentials and a vendor integration are still needed, and each is checked before every call."""
    from aime.contact_sources.registry import ADAPTER_CLASSES

    cls = ADAPTER_CLASSES.get(name)
    if cls is None or not cls.requires_owner_approval:
        raise ValueError("only a source that needs the owner's approval takes one")
    who = " ".join((approved_by or "").split())[:80]
    if not who:
        raise ValueError("give the name of the person who approved it")
    entry = {"approved_by": who, "approved_at": utcnow().isoformat(timespec="seconds"), "note": " ".join((note or "").split())[:300]}
    approvals = dict(kv_get(session, APPROVALS_KEY, {}) or {})
    approvals[name] = entry
    kv_set(session, APPROVALS_KEY, approvals)
    audit(session, actor, "contact_source_approved", name, f"approved by {who}" + (f": {entry['note']}" if entry["note"] else ""))
    return entry


def withdraw_approval(session: Session, name: str, actor: str) -> bool:
    approvals = dict(kv_get(session, APPROVALS_KEY, {}) or {})
    if approvals.pop(name, None) is None:
        return False
    kv_set(session, APPROVALS_KEY, approvals)
    audit(session, actor, "contact_source_approval_withdrawn", name, "no further calls until approved again")
    return True


# --------------------------------------------------------------------------- the guard

def cooldown_until(session: Session, name: str) -> datetime | None:
    raw = (kv_get(session, COOLDOWN_KEY, {}) or {}).get(name)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _set_cooldown(session: Session, name: str, seconds: float) -> None:
    waits = dict(kv_get(session, COOLDOWN_KEY, {}) or {})
    waits[name] = (utcnow() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    kv_set(session, COOLDOWN_KEY, waits)


def price_per_call(adapter: ContactSource, setting: dict | None) -> float:
    return float((setting or {}).get("cost_per_call_usd") or 0) if adapter.paid else float(adapter.cost_per_call_usd or 0)


def refusal(session: Session, adapter: ContactSource, country: str | None = None, calls: int = 1) -> str | None:
    """Why this call may not be made now (None: it may). Reads only; nothing is recorded."""
    from aime import countries
    from aime.contact_sources.registry import adapter_setting

    setting = adapter_setting(adapter.settings, adapter.name)
    if adapter.controlled_by == "settings" and not (setting or {}).get("enabled"):
        return "switched off in the Contact sources settings"
    st = adapter.status(session)
    if not st.ready:
        return f"{st.status}: {st.reason}"
    if country:
        code = country.upper()
        if "*" not in adapter.countries and code not in adapter.countries:
            return f"it covers {', '.join(adapter.countries) or 'no country'}, not {code}"
        allowed = (setting or {}).get("countries")
        if setting is not None and "*" not in allowed and code not in allowed:
            return f"its settings do not use it for {code}"
        if code not in countries.target_countries(adapter.settings):
            return f"{code} is not a target country"
    until = cooldown_until(session, adapter.name)
    if until is not None and utcnow() < until:
        return f"the source asked us to wait (too many requests) until {until:%Y-%m-%d %H:%M} UTC"
    price = price_per_call(adapter, setting)
    if adapter.paid or price > 0:
        cap = float((setting or {}).get("monthly_budget_usd") or 0)
        if cap <= 0:
            return "its monthly budget is 0, so no paid call is made"
        if price <= 0:
            return "no price per call is recorded"
        spent = (usage(session, adapter.name) or ContactSourceUsage(cost_usd=0.0)).cost_usd or 0.0
        if spent + price * calls > cap + 1e-9:
            return f"this month's budget is reached (${spent:.2f} of ${cap:.2f} spent; this call costs ${price * calls:.2f})"
    return None


def _throttle(adapter: ContactSource) -> None:
    gap = adapter.min_interval_seconds if adapter.settings.polite_delay > 0 else 0.0
    last = _last_call.get(adapter.name)
    if gap and last is not None:
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_call[adapter.name] = time.monotonic()


def guarded_call(session: Session, adapter: ContactSource, fn: Callable[[], Any], country: str | None = None, calls: int = 1,
                 note: str = "") -> Any:
    """Run `fn` (one call to the source) only if the guard allows it, booking it in the ledger first."""
    reason = refusal(session, adapter, country, calls)
    row = _row(session, adapter.name)
    now = utcnow()
    if reason:
        row.refused += 1
        row.last_result = f"refused: {reason}"[:300]
        audit(session, ACTOR, "contact_source_refused", adapter.name, reason[:500])
        session.flush()
        raise AdapterRefused(reason)
    from aime.contact_sources.registry import adapter_setting

    cost = price_per_call(adapter, adapter_setting(adapter.settings, adapter.name)) * calls
    row.calls += calls
    row.cost_usd = round((row.cost_usd or 0.0) + cost, 6)
    row.last_call_at = now
    if cost:
        audit(session, ACTOR, "contact_source_spend", adapter.name,
              f"${cost:.4f} for {calls} call(s){f' ({note})' if note else ''}; ${row.cost_usd:.2f} spent in {row.month}")
    session.flush()
    _throttle(adapter)
    try:
        result = fn()
    except RateLimited as exc:
        row.errors += 1
        row.last_result = f"rate limited: waiting {int(exc.retry_after)} s"[:300]
        _set_cooldown(session, adapter.name, exc.retry_after)
        audit(session, ACTOR, "contact_source_rate_limited", adapter.name, f"{exc} - next call after {int(exc.retry_after)} s")
        session.flush()
        raise
    except Exception as exc:
        row.errors += 1
        row.last_result = redact(f"error: {type(exc).__name__}: {exc}")[:300]
        session.flush()
        raise
    row.last_result = f"ok{f' ({note})' if note else ''}"[:300]
    session.flush()
    return result
