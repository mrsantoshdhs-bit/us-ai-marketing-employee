"""Deliverability safeguards: the reputation checks that sit beside the send limits, so that volume can one day be raised
safely. Nothing here raises a limit: every rule can only hold an email back, lower a cap or add a header.

**Bounce and complaint auto-pause (on by default).** Before every send run, and again before each email inside it, the
bounce rate and the complaint rate are read from what is recorded: emails that left (or may have), emails the mail server
refused because the address does not exist, bounces read back from the mailbox, and replies classified as a complaint
("this is spam", "reported you", a legal threat). They are judged for every sending mailbox and for all of them together.
When one is too high, outbound email is paused: every approved email stays approved and waits, with the reason, an
alert is raised and the pause is written to the audit log. Reading the mailbox, monitoring, research and drafting go on;
only sending stops. Only an administrator clears the pause, on the Deliverability page, and that is audited too. After a
clear the rates start again from that moment, so a pause is not raised again at once by the history that caused it.

The defaults are conservative and cannot trigger on a clean history (no bounce, no complaint):
- bounces: over the last 7 days, at least 20 emails attempted, at least 3 of them bounced, and 5% or more. One bounce in
  five emails is not a pattern; three bounces in twenty, or a 5% burst over a busy week, is;
- complaints: over the last 30 days, at least 2 complaints and 0.3% or more of the emails attempted (at least 20 are
  assumed, so two complaints among a handful of emails count as they should). 0.3% is the line the large mailbox
  providers publish for bulk senders; a complaint is a person's own words, so two of them are never noise.

**Authentication gate (warn by default).** The sending domain's SPF, DKIM and DMARC are checked with
aime.email_check.check_domain once a day by the monitor job (or when an administrator clicks Check now), never per
email, and the result is kept. Mode `off` ignores it; `warn` (the default) raises an alert when a check fails or could
not run, and sends as before; `block` holds every email while SPF, DKIM or DMARC does not pass, while the check could not
run (a DNS failure), or while the last check is more than 48 hours old. A doctor check reports it.

**Per-mailbox accounting.** Each send records which Mailboxes-panel mailbox sent it (`followups.mail_account_id`; empty for
the .env mailbox and for every email sent before this was recorded). The global `outreach.mailbox_daily_limit` (50 in 24
hours) still counts every email from every mailbox, exactly as before. On top of it each mailbox has its own rolling
24-hour count, where the emails of unknown mailbox count for every mailbox (they may have been its own), so with one
mailbox the two counts are the same number. A mailbox's own cap (`mailbox_caps`, by address) and its warm-up stage can
only lower what it may send: a cap above the global one is read as the global one.

**Per-hour cap and send spacing (both off by default).** `hourly_cap` limits what one mailbox sends in any 60 minutes.
`jitter_enabled` waits a random 20 to 90 seconds between two emails of one run; no wait is longer than 120 seconds (well
inside the send run's own five-minute check for a PC that slept), and a run waits at most 600 seconds in all: what is
left then goes out on the next run.

**Warm-up (off by default).** A new mailbox starts low and rises with its age in days: day 1 five emails, day 4 ten, day 8
twenty, day 15 thirty-five, day 22 fifty (the defaults). Its first day is the one set in `warmup_start` for its address,
or the day it first sent. A ramp can never go above the mailbox's other caps.

**One-click unsubscribe (RFC 8058).** The mailto List-Unsubscribe header stays. Only when an https address is set in
`one_click_unsubscribe_url` is it added to that header with `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. Empty
(the default): the headers are exactly as before. AIME does not serve that address: it is a hosting decision.

Settings come from `settings.section("deliverability")` over the defaults below, so this works with or without a settings
store. Every value is checked: one that cannot be read falls back to its default, and none can make a rule looser than
its safe bounds.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from aime.db import kv_get, kv_set
from aime.models import FollowUp, InboundEmail, MailAccount
from aime.redact import redact
from aime.timeutil import utcnow

DEFAULTS: dict = {
    "auto_pause": True,
    "bounce_window_days": 7,
    "bounce_rate_pause": 0.05,
    "min_sends": 20,
    "min_bounces": 3,
    "complaint_window_days": 30,
    "complaint_rate_pause": 0.003,
    "min_complaints": 2,
    "auth_gate": "warn",  # off | warn | block
    "auth_refresh_hours": 24,
    "auth_max_age_hours": 48,
    "mailbox_caps": {},  # {"sales@company.com": 30}: only ever lower than outreach.mailbox_daily_limit
    "hourly_cap": 0,  # 0: none
    "jitter_enabled": False,
    "jitter_min_seconds": 20,
    "jitter_max_seconds": 90,
    "jitter_run_budget_seconds": 600,
    "warmup_enabled": False,
    "warmup_schedule": [[1, 5], [4, 10], [8, 20], [15, 35], [22, 50]],
    "warmup_start": {},  # {"sales@company.com": "2026-11-03"}
    "one_click_unsubscribe_url": "",
}
# Bounds no setting can pass: a pause must need a real sample, and a wait inside a send run must stay far below the
# run's own five-minute check (aime.agents.sales_workflow.SEND_RUN_JUMP) and its send lock (30 minutes).
MIN_SAMPLE_FLOOR = 10
JITTER_MAX_CEILING = 120
JITTER_BUDGET_CEILING = 900
GATE_MODES = ("off", "warn", "block")
GATED_CHECKS = ("SPF", "DKIM", "DMARC", "Sending domain")
COUNTS_AS_SENT = ("sent", "sending", "uncertain")  # as the send limits count: an uncertain email may have gone out

PAUSE_KEY = "deliverability.pause"
CLEARED_KEY = "deliverability.cleared"
AUTH_KEY = "deliverability.auth"
PAUSE_ALERT = "deliverability:paused"
AUTH_ALERT = "deliverability:auth"
SOURCE = "deliverability"
CLEAR_PERMISSION = "admin_ops"
ENV_MAILBOX = "env"  # the .env mailbox, and every email whose mailbox was not recorded
HTTPS_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?(/[^\s<>,\"]*)?$")

# Injectable for tests: no test sleeps or looks up DNS for real.
SLEEP: Callable[[float], None] = time.sleep
RANDOM = random.Random()
RESOLVER: Callable[[str, str], list[str]] | None = None


# --------------------------------------------------------------------------- settings
def _number(raw, default, low, high, kind=float):
    try:
        value = kind(raw)
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return min(max(value, low), high)


def _flag(raw, default: bool) -> bool:
    """A switch from TOML (true/false) or from a form or store ("on", "off", "1", "0"); anything else keeps the default."""
    if isinstance(raw, bool):
        return raw
    words = {"true": True, "on": True, "yes": True, "1": True, "false": False, "off": False, "no": False, "0": False}
    return words.get(str(raw).strip().lower(), default) if raw is not None else default


def config(settings) -> dict:
    """The deliverability settings: the section over the defaults, each value checked and held inside its safe bounds."""
    section = getattr(settings, "section", None)
    raw = section("deliverability") if callable(section) else {}
    raw = raw if isinstance(raw, dict) else {}
    d = DEFAULTS
    cfg = {
        "auto_pause": _flag(raw.get("auto_pause"), d["auto_pause"]),
        "bounce_window_days": _number(raw.get("bounce_window_days"), d["bounce_window_days"], 1, 90, int),
        "bounce_rate_pause": _number(raw.get("bounce_rate_pause"), d["bounce_rate_pause"], 0.005, 0.5),
        "min_sends": _number(raw.get("min_sends"), d["min_sends"], MIN_SAMPLE_FLOOR, 1000, int),
        "min_bounces": _number(raw.get("min_bounces"), d["min_bounces"], 1, 100, int),
        "complaint_window_days": _number(raw.get("complaint_window_days"), d["complaint_window_days"], 1, 180, int),
        "complaint_rate_pause": _number(raw.get("complaint_rate_pause"), d["complaint_rate_pause"], 0.0005, 0.5),
        "min_complaints": _number(raw.get("min_complaints"), d["min_complaints"], 1, 100, int),
        "auth_gate": str(raw.get("auth_gate", d["auth_gate"])).strip().lower(),
        "auth_refresh_hours": _number(raw.get("auth_refresh_hours"), d["auth_refresh_hours"], 1, 168, int),
        "auth_max_age_hours": _number(raw.get("auth_max_age_hours"), d["auth_max_age_hours"], 1, 336, int),
        "hourly_cap": _number(raw.get("hourly_cap"), d["hourly_cap"], 0, 10_000, int),
        "jitter_enabled": _flag(raw.get("jitter_enabled"), d["jitter_enabled"]),
        "jitter_min_seconds": _number(raw.get("jitter_min_seconds"), d["jitter_min_seconds"], 0, JITTER_MAX_CEILING),
        "jitter_max_seconds": _number(raw.get("jitter_max_seconds"), d["jitter_max_seconds"], 0, JITTER_MAX_CEILING),
        "jitter_run_budget_seconds": _number(raw.get("jitter_run_budget_seconds"), d["jitter_run_budget_seconds"], 0,
                                             JITTER_BUDGET_CEILING),
        "warmup_enabled": _flag(raw.get("warmup_enabled"), d["warmup_enabled"]),
    }
    if cfg["auth_gate"] not in GATE_MODES:
        cfg["auth_gate"] = d["auth_gate"]
    cfg["jitter_min_seconds"] = min(cfg["jitter_min_seconds"], cfg["jitter_max_seconds"])
    caps = raw.get("mailbox_caps") if isinstance(raw.get("mailbox_caps"), dict) else {}
    cfg["mailbox_caps"] = {str(k).strip().lower(): v for k, v in
                           ((k, _number(v, None, 0, 10_000, int)) for k, v in caps.items()) if v is not None}
    schedule = []
    for step in raw.get("warmup_schedule", d["warmup_schedule"]) or []:
        try:
            day, cap = int(step[0]), int(step[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if day >= 1 and cap >= 0:
            schedule.append((day, cap))
    cfg["warmup_schedule"] = sorted(schedule) or [tuple(s) for s in d["warmup_schedule"]]
    starts = raw.get("warmup_start") if isinstance(raw.get("warmup_start"), dict) else {}
    cfg["warmup_start"] = {}
    for k, v in starts.items():
        try:
            cfg["warmup_start"][str(k).strip().lower()] = v if isinstance(v, date) else date.fromisoformat(str(v)[:10])
        except ValueError:
            continue
    url = str(raw.get("one_click_unsubscribe_url", "") or "").strip()
    cfg["one_click_unsubscribe_url_raw"] = url
    cfg["one_click_unsubscribe_url"] = url if HTTPS_URL.fullmatch(url) else ""
    return cfg


def one_click_url(settings) -> str:
    """The https unsubscribe address for RFC 8058 one-click, or "" (the default): then no header changes."""
    return config(settings)["one_click_unsubscribe_url"]


def global_cap(settings) -> int:
    return int(settings.section("outreach").get("mailbox_daily_limit", 50))


# --------------------------------------------------------------------------- the sending mailbox
def sending_mailbox(session: Session, settings) -> tuple[int | None, str]:
    """Which mailbox sends now: the primary Mailboxes-panel mailbox (its id), else .env (None), and its address."""
    from aime import mail_accounts

    a = mail_accounts.primary(session)
    if a is not None:
        return a.id, (a.email or "").lower()
    return None, (settings.smtp_from or "").lower()


def _sent_time():
    return func.coalesce(FollowUp.sent_at, FollowUp.send_started_at)


def _counted_for(account_id: int | None):
    """The emails that count for one mailbox's caps: its own, and those whose mailbox was not recorded (they may have
    been its own). With one mailbox this is every email, as the global count."""
    if account_id is None:
        return FollowUp.mail_account_id.is_(None)
    return or_(FollowUp.mail_account_id == account_id, FollowUp.mail_account_id.is_(None))


def mailbox_sent(session: Session, account_id: int | None, since: datetime) -> int:
    return session.execute(select(func.count(FollowUp.id)).where(
        FollowUp.channel == "email", FollowUp.status.in_(COUNTS_AS_SENT), _sent_time() >= since,
        _counted_for(account_id))).scalar_one()


def warmup_stage(session: Session, settings, account_id: int | None, email: str, now: datetime,
                 cfg: dict | None = None) -> dict | None:
    """Where this mailbox is on its warm-up ramp, or None when warm-up is off: {day, cap, start, complete}."""
    cfg = cfg or config(settings)
    if not cfg["warmup_enabled"]:
        return None
    start = cfg["warmup_start"].get((email or "").lower())
    if start is None:
        own = FollowUp.mail_account_id.is_(None) if account_id is None else FollowUp.mail_account_id == account_id
        first = session.execute(select(func.min(_sent_time())).where(
            FollowUp.channel == "email", FollowUp.status.in_(COUNTS_AS_SENT), own)).scalar()
        start = first.date() if first else now.date()
    day = max(1, (now.date() - start).days + 1)
    schedule = cfg["warmup_schedule"]
    cap = next((c for d, c in reversed(schedule) if d <= day), schedule[0][1])
    return {"day": day, "cap": cap, "start": start.isoformat(), "complete": day >= schedule[-1][0]}


def effective_cap(session: Session, settings, account_id: int | None, email: str, now: datetime,
                  cfg: dict | None = None) -> tuple[int, str]:
    """What this mailbox may send in 24 hours, and why: the lowest of the global cap, its own cap and its warm-up stage."""
    cfg = cfg or config(settings)
    cap, why = global_cap(settings), "the mailbox limit"
    own = cfg["mailbox_caps"].get((email or "").lower())
    if own is not None and own < cap:
        cap, why = own, "this mailbox's own cap"
    stage = warmup_stage(session, settings, account_id, email, now, cfg)
    if stage and stage["cap"] < cap:
        cap, why = stage["cap"], f"warm-up day {stage['day']}"
    return cap, why


# --------------------------------------------------------------------------- bounce and complaint rates
def _attempted():
    """An email that left, may have, or that the server refused because the address does not exist."""
    return and_(FollowUp.channel == "email", or_(FollowUp.status.in_(COUNTS_AS_SENT), FollowUp.delivery_status == "bounced"))


def _attempt_time():
    return func.coalesce(FollowUp.sent_at, FollowUp.send_started_at, FollowUp.updated_at)


def _is_complaint(reasons) -> bool:
    return any(str(r).lower().startswith("complaint") for r in reasons or [])


def _complaints(session: Session, since: datetime) -> list[int | None | str]:
    """The mailbox each complaint answered (its id, None for .env or unrecorded), or "?" when it matches no email of ours."""
    rows = session.execute(select(InboundEmail).where(
        InboundEmail.category == "unsubscribe", InboundEmail.is_simulated.is_(False),
        InboundEmail.received_at >= since)).scalars().all()
    out: list[int | None | str] = []
    for row in rows:
        if not _is_complaint(row.reasons):
            continue
        f = session.get(FollowUp, row.followup_id) if row.followup_id else None
        if f is None and row.in_reply_to:
            f = session.execute(select(FollowUp).where(FollowUp.message_id == row.in_reply_to)).scalars().first()
        out.append(f.mail_account_id if f is not None else "?")
    return out


def _since(now: datetime, days: int, floor: datetime | None) -> datetime:
    start = now - timedelta(days=days)
    return max(start, floor) if floor else start


def _cleared_at(session: Session) -> datetime | None:
    cleared = kv_get(session, CLEARED_KEY) or {}
    try:
        return datetime.fromisoformat(cleared["at"]) if cleared.get("at") else None
    except (TypeError, ValueError):
        return None


def rate_report(session: Session, settings, now: datetime | None = None, cfg: dict | None = None) -> dict:
    """Bounce and complaint figures, overall and per sending mailbox, since the later of the window start and the last clear.
    {"overall": scope, "mailboxes": {account_id or None: scope}}, each scope with its breach (words) or None."""
    cfg = cfg or config(settings)
    now = now or utcnow()
    floor = _cleared_at(session)
    b_since = _since(now, cfg["bounce_window_days"], floor)
    c_since = _since(now, cfg["complaint_window_days"], floor)

    def tally(since):
        return {acc: (n, int(b or 0)) for acc, n, b in session.execute(
            select(FollowUp.mail_account_id, func.count(FollowUp.id),
                   func.sum(case((FollowUp.delivery_status == "bounced", 1), else_=0)))
            .where(_attempted(), _attempt_time() >= since).group_by(FollowUp.mail_account_id)).all()}

    bounce_rows, complaint_rows = tally(b_since), tally(c_since)
    complaints = _complaints(session, c_since)
    accounts = set(bounce_rows) | set(complaint_rows) | {c for c in complaints if c != "?"}

    def scope(sends_b, bounces, sends_c, complained) -> dict:
        s = {"sends": sends_b, "bounces": bounces, "bounce_rate": bounces / sends_b if sends_b else 0.0,
             "complaint_sends": sends_c, "complaints": complained,
             "complaint_rate": complained / max(sends_c, cfg["min_sends"])}
        s["breach"] = _breach(s, cfg)
        return s

    overall = scope(sum(n for n, _ in bounce_rows.values()), sum(b for _, b in bounce_rows.values()),
                    sum(n for n, _ in complaint_rows.values()), len(complaints))
    boxes = {acc: scope(bounce_rows.get(acc, (0, 0))[0], bounce_rows.get(acc, (0, 0))[1],
                        complaint_rows.get(acc, (0, 0))[0], sum(1 for c in complaints if c == acc)) for acc in accounts}
    return {"overall": overall, "mailboxes": boxes, "bounce_since": b_since.isoformat(), "complaint_since": c_since.isoformat()}


def _breach(s: dict, cfg: dict) -> str | None:
    if (s["sends"] >= cfg["min_sends"] and s["bounces"] >= cfg["min_bounces"]
            and s["bounce_rate"] >= cfg["bounce_rate_pause"]):
        return (f"{s['bounces']} of {s['sends']} emails bounced in {cfg['bounce_window_days']} days "
                f"({s['bounce_rate']:.1%}; the pause line is {cfg['bounce_rate_pause']:.1%})")
    if s["complaints"] >= cfg["min_complaints"] and s["complaint_rate"] >= cfg["complaint_rate_pause"]:
        return (f"{s['complaints']} complaint(s) against {s['complaint_sends']} emails in {cfg['complaint_window_days']} days "
                f"({s['complaint_rate']:.2%}; the pause line is {cfg['complaint_rate_pause']:.2%})")
    return None


def mailbox_label(session: Session, account_id) -> str:
    if account_id is None:
        return "the .env mailbox (and emails sent before mailboxes were recorded)"
    a = session.get(MailAccount, account_id)
    return a.email if a is not None else f"mailbox #{account_id} (deleted)"


# --------------------------------------------------------------------------- the pause
def pause_state(session: Session) -> dict | None:
    p = kv_get(session, PAUSE_KEY)
    return p if isinstance(p, dict) and p.get("paused") else None


def hold_words(p: dict) -> str:
    return (f"Held: outbound email is paused for deliverability ({p.get('reason', 'see the Deliverability page')}). An "
            "administrator clears the pause on the Deliverability page")[:300]


def evaluate(session: Session, settings, now: datetime | None = None) -> dict | None:
    """The pause in force, raised now if a rate has just crossed its line. Reads only what is recorded: no mail, no network."""
    current = pause_state(session)
    if current:
        return current
    cfg = config(settings)
    if not cfg["auto_pause"]:
        return None
    now = now or utcnow()
    report = rate_report(session, settings, now, cfg)
    found = [("all mailboxes", report["overall"])] + [(mailbox_label(session, acc), s) for acc, s in report["mailboxes"].items()]
    scope, figures = next(((name, s) for name, s in found if s["breach"]), (None, None))
    if scope is None:
        return None
    return pause(session, settings, f"{scope}: {figures['breach']}", scope, figures, now)


def pause(session: Session, settings, reason: str, scope: str, figures: dict, now: datetime | None = None) -> dict:
    from aime import automation
    from aime.monitoring import alert_once

    state = {"paused": True, "since": (now or utcnow()).isoformat(), "reason": reason[:240], "scope": scope,
             "figures": {k: v for k, v in figures.items() if k != "breach"}}
    kv_set(session, PAUSE_KEY, state)
    automation.audit(session, SOURCE, "deliverability_paused", scope[:120], reason)
    alert_once(session, "error", SOURCE, f"Outbound email is paused: {reason}. Approved emails stay approved and wait; reading "
               "the mailbox and every other job go on. Check the bounces and complaints, then an administrator clears the "
               "pause on the Deliverability page.", settings, key=PAUSE_ALERT, until_resolved=True)
    session.flush()
    return state


def may_clear(perms) -> bool:
    """`perms` None: the dashboard runs without sign-in (every page is the owner's, as for the other admin actions)."""
    return perms is None or CLEAR_PERMISSION in set(perms)


def clear_pause(session: Session, actor: str, perms, note: str = "") -> dict:
    """An administrator lifts the pause. Refused (and audited) for anyone else. The rates start again from now."""
    from aime import automation
    from aime.monitoring import resolve_alerts

    if not may_clear(perms):
        automation.audit(session, actor, "deliverability_clear_refused", "outbound pause", "not an administrator")
        raise PermissionError("Only an administrator clears the deliverability pause")
    current = pause_state(session)
    if current is None:
        raise ValueError("Outbound email is not paused")
    at = utcnow()
    kv_set(session, PAUSE_KEY, {"paused": False, "cleared_at": at.isoformat(), "was": current})
    kv_set(session, CLEARED_KEY, {"at": at.isoformat(), "by": actor, "note": (note or "")[:300], "reason": current.get("reason")})
    automation.audit(session, actor, "deliverability_cleared", "outbound pause",
                     f"was: {current.get('reason')}" + (f"; note: {note[:300]}" if note else ""))
    resolve_alerts(session, (PAUSE_ALERT,), set())
    session.flush()
    return current


# --------------------------------------------------------------------------- the authentication gate
def auth_state(session: Session) -> dict | None:
    s = kv_get(session, AUTH_KEY)
    return s if isinstance(s, dict) else None


def _default_resolver(settings):
    if RESOLVER is not None:
        return RESOLVER
    if not getattr(settings, "network_enabled", True) or getattr(settings, "data_role", "") == "test":
        return None  # no look-up without the network, and never a real one from a test
    from aime.email_check import dns_resolver

    return dns_resolver()


def refresh_auth(session: Session, settings, resolve: Callable[[str, str], list[str]] | None = None,
                 now: datetime | None = None, actor: str = "monitor") -> dict | None:
    """Check the sending domain's SPF, DKIM and DMARC now and keep the result. None when no look-up may be made here."""
    from aime import mail_accounts
    from aime.email_check import check_domain
    from aime.monitoring import alert_once, resolve_alerts

    cfg = config(settings)
    now = now or utcnow()
    resolver = resolve or _default_resolver(settings)
    if resolver is None:
        return None
    sending = mail_accounts.sending_settings(session, settings)
    sender = (sending.smtp_from or "").lower()
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    state = {"domain": domain or None, "checked_at": now.isoformat(), "checks": [], "failing": [], "error": None, "by": actor}
    if not domain:
        state["error"] = "no sending address is configured"
    else:
        try:
            checks = check_domain(domain, resolver, sending.smtp_host or None)
        except Exception as exc:  # noqa: BLE001 - a DNS failure is an answer: kept, alerted, and fail-open only in warn mode
            state["error"] = redact(f"{type(exc).__name__}: {exc}")[:200]
        else:
            state["checks"] = [{"name": c.name, "status": c.status, "detail": c.detail[:200]} for c in checks]
            state["failing"] = [c.name for c in checks if c.name in GATED_CHECKS and c.status != "pass"]
    kv_set(session, AUTH_KEY, state)
    from aime import automation

    automation.audit(session, actor, "deliverability_auth_checked", domain or "no domain",
                     "passed" if not (state["error"] or state["failing"]) else
                     f"error: {state['error']}" if state["error"] else "failing: " + ", ".join(state["failing"]))
    problem = auth_problem(state, domain or None, now, cfg)
    if cfg["auth_gate"] != "off" and problem:
        block = cfg["auth_gate"] == "block"
        alert_once(session, "error" if block else "warning", SOURCE,
                   f"Sending domain authentication: the check {problem}. "
                   + ("Sending is held until it passes (gate in block mode)." if block else
                      "Emails still go out (gate in warn mode), but they are more likely to land in spam."), settings, key=AUTH_ALERT)
    else:
        resolve_alerts(session, (AUTH_ALERT,), set())
    session.flush()
    return state


def auth_problem(state: dict | None, domain: str | None, now: datetime, cfg: dict) -> str | None:
    """Why the kept check does not show a passing domain, in words, or None."""
    if state is None or not state.get("checked_at"):
        return f"has not run yet for {domain or 'the sending domain'}"
    if domain and state.get("domain") != domain:
        return f"has not run yet for {domain} (the last one was for {state.get('domain')})"
    try:
        age = now - datetime.fromisoformat(state["checked_at"])
    except (TypeError, ValueError):
        return "has no readable date"
    if age > timedelta(hours=cfg["auth_max_age_hours"]):
        return f"is out of date (last run {state['checked_at'][:16].replace('T', ' ')} UTC)"
    if state.get("error"):
        return f"could not run ({state['error']})"
    if state.get("failing"):
        details = {c["name"]: c for c in state.get("checks", [])}
        return "failed: " + "; ".join(f"{n} {details.get(n, {}).get('status', '?')}" for n in state["failing"])
    return None


def auth_hold(session: Session, settings, now: datetime | None = None) -> str | None:
    """Why sending must wait for the domain's authentication (block mode only). Reads the kept result: no look-up."""
    cfg = config(settings)
    if cfg["auth_gate"] != "block":
        return None
    now = now or utcnow()
    _, sender = sending_mailbox(session, settings)
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else None
    problem = auth_problem(auth_state(session), domain, now, cfg)
    return (f"Held: the sending domain's SPF/DKIM/DMARC check {problem}; the deliverability gate is in block mode")[:300] if problem else None


# --------------------------------------------------------------------------- the send run's hooks
def sending_hold(session: Session, settings, now: datetime | None = None) -> str | None:
    """Why nothing may go out in this send run: a deliverability pause (raised now if the rates call for one), or the
    authentication gate in block mode. None: the run goes on."""
    p = evaluate(session, settings, now)
    if p:
        return hold_words(p)
    return auth_hold(session, settings, now)


class SendGuard:
    """One send run's per-mailbox caps, per-hour cap, warm-up and spacing. Asked before each email; told after each send."""

    def __init__(self, session: Session, settings, now: datetime, account_id: int | None, email: str):
        self.settings, self.now, self.account_id, self.email = settings, now, account_id, (email or "").lower()
        self.cfg = config(settings)
        self.cap, self.cap_why = effective_cap(session, settings, account_id, self.email, now, self.cfg)
        self.box_sent = mailbox_sent(session, account_id, now - timedelta(hours=24))
        self.hour_cap = self.cfg["hourly_cap"]
        self.hour_sent = mailbox_sent(session, account_id, now - timedelta(hours=1)) if self.hour_cap else 0
        self.waited = 0.0
        self.started = False

    def wait(self, session: Session) -> str | None:
        """Why this email must wait, or None. A bounce earlier in this same run may have paused sending."""
        p = evaluate(session, self.settings, self.now)
        if p:
            return hold_words(p)
        if self.box_sent >= self.cap:
            return (f"The mailbox {self.email or ''} has sent {self.box_sent} emails in 24 hours ({self.cap_why}: {self.cap}); "
                    "it goes out when the count drops")[:300]
        if self.hour_cap and self.hour_sent >= self.hour_cap:
            return (f"Waiting: the mailbox sends at most {self.hour_cap} emails an hour; it goes out on a later run")[:300]
        return None

    def pace(self) -> str | None:
        """Wait a random moment before every email but the first of the run (spacing on). None: go on now."""
        if not self.started or not self.cfg["jitter_enabled"]:
            self.started = True
            return None
        delay = RANDOM.uniform(self.cfg["jitter_min_seconds"], self.cfg["jitter_max_seconds"])
        if self.waited + delay > self.cfg["jitter_run_budget_seconds"]:
            return "Waiting: this run's time for spacing out emails is used up; it goes out on the next run"
        SLEEP(delay)
        self.waited += delay
        return None

    def sent(self) -> None:
        self.box_sent += 1
        self.hour_sent += 1


# --------------------------------------------------------------------------- monitor, doctor, panel
def monitor(session: Session, settings, now: datetime | None = None,
            resolve: Callable[[str, str], list[str]] | None = None) -> dict:
    """The monitor job's part: refresh the authentication check once a day, and judge the rates (which may pause)."""
    cfg = config(settings)
    now = now or utcnow()
    issues: list[str] = []
    if cfg["auth_gate"] != "off":
        state = auth_state(session)
        _, sender = sending_mailbox(session, settings)
        domain = sender.rsplit("@", 1)[-1] if "@" in sender else None
        stale = (state is None or state.get("domain") != domain or not state.get("checked_at")
                 or now - datetime.fromisoformat(state["checked_at"]) >= timedelta(hours=cfg["auth_refresh_hours"]))
        if stale:
            state = refresh_auth(session, settings, resolve, now) or state
        problem = auth_problem(state, domain, now, cfg) if state else None
        if problem:
            issues.append(f"Sending domain authentication: the check {problem}")
    p = evaluate(session, settings, now)
    if p:
        issues.append(f"Outbound email paused for deliverability: {p.get('reason')}")
    return {"issues": issues, "paused": bool(p)}


def doctor_checks(ctx, add) -> None:
    """`aime doctor`: the pause, the authentication gate and the one-click address. Reads what is kept: no look-up."""
    cfg = config(ctx.settings)
    with ctx.db.session() as s:
        p = pause_state(s)
        add("Outbound not paused for deliverability", p is None,
            "no pause" if p is None else f"{hold_words(p)} (since {p.get('since', '?')[:16]} UTC)", own_alert=True)
        mode = cfg["auth_gate"]
        _, sender = sending_mailbox(s, ctx.settings)
        domain = sender.rsplit("@", 1)[-1] if "@" in sender else None
        state = auth_state(s)
        name = "Sending domain authentication (SPF, DKIM, DMARC)"
        if mode == "off":
            add(name, True, "the gate is off (deliverability.auth_gate = off)", "warning")
        elif state is None:
            add(name, mode != "block", f"not checked yet: the monitor job checks it once a day (gate in {mode} mode)",
                "error" if mode == "block" else "warning", own_alert=True)
        else:
            problem = auth_problem(state, domain, utcnow(), cfg)
            add(name, problem is None, f"the check {problem} (gate in {mode} mode)" if problem else
                f"SPF, DKIM and DMARC pass for {domain} (checked {state['checked_at'][:16].replace('T', ' ')} UTC; gate in {mode} mode)",
                "error" if mode == "block" else "warning", own_alert=True)
    raw = cfg["one_click_unsubscribe_url_raw"]
    if raw and not cfg["one_click_unsubscribe_url"]:
        add("One-click unsubscribe address", False, "deliverability.one_click_unsubscribe_url is not a plain https address, "
            "so it is not used (the mailto opt-out stays)", "warning")


def panel(session: Session, settings, now: datetime | None = None) -> dict:
    """What the Deliverability page shows. Reads only."""
    cfg = config(settings)
    now = now or utcnow()
    report = rate_report(session, settings, now, cfg)
    sending_id, sending_email = sending_mailbox(session, settings)
    rows = []
    boxes = [(a.id, a.email.lower(), a.label, a.is_primary and a.active, a.active) for a in
             session.execute(select(MailAccount).order_by(MailAccount.id)).scalars()]
    if sending_id is None or None in report["mailboxes"]:
        boxes.insert(0, (None, (settings.smtp_from or "").lower(), ".env mailbox", sending_id is None, sending_id is None))
    empty = {"sends": 0, "bounces": 0, "bounce_rate": 0.0, "complaint_sends": 0, "complaints": 0, "complaint_rate": 0.0,
             "breach": None}
    for acc, email, label, sends_now, active in boxes:
        cap, why = effective_cap(session, settings, acc, email, now, cfg)
        rows.append({"id": acc, "email": email, "label": label, "sending": sends_now, "active": active,
                     "sent_24h": mailbox_sent(session, acc, now - timedelta(hours=24)),
                     "sent_hour": mailbox_sent(session, acc, now - timedelta(hours=1)), "cap": cap, "cap_why": why,
                     "own_cap": cfg["mailbox_caps"].get(email), "warmup": warmup_stage(session, settings, acc, email, now, cfg),
                     "rates": report["mailboxes"].get(acc, empty)})
    domain = sending_email.rsplit("@", 1)[-1] if "@" in sending_email else None
    state = auth_state(session)
    return {"cfg": cfg, "pause": pause_state(session), "cleared": kv_get(session, CLEARED_KEY), "overall": report["overall"],
            "report": report, "rows": rows, "global_cap": global_cap(settings),
            "global_sent": session.execute(select(func.count(FollowUp.id)).where(
                FollowUp.channel == "email", FollowUp.status.in_(COUNTS_AS_SENT),
                _sent_time() >= now - timedelta(hours=24))).scalar_one(),
            "auth": state, "auth_domain": domain, "auth_problem": auth_problem(state, domain, now, cfg),
            "auth_hold": auth_hold(session, settings, now)}
