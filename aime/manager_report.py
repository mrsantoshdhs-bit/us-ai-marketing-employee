"""The Sales Manager's performance report: daily, weekly and monthly, from the CRM's own records, stored as a report on
the dashboard and emailed to the Sales Manager only once that has been authorized.

**What is counted.** Every number is a count of records, and each one keeps what it was counted from: a plain-English
definition, the SQL that counted it and the ids of the records (the first 500), so anyone can open the records behind a
figure. The funnel stages are kept apart and never stand in for each other:

  discovered companies -> identified decision-makers (named people) -> contactable prospects (any usable email)
  -> verified contacts -> qualified leads -> outreach drafted / approved / sent -> replies -> follow-ups
  -> meetings booked -> opportunities by stage

A company inbox (info@, sales@, a "role_address") is contactable but never a verified decision-maker: it names nobody.
A test-mode delivery (an email redirected to our own test address, aime.outreach.REACHED_PROSPECT), a simulated reply
or meeting (`aime simulate-reply`) and a deal opened only by a simulated reply (aime.performance.NOT_SIMULATED_DEAL)
are never counted as business; the report says how many there were beside the real figures. Sample records
(is_sample) are not counted either. A period in which nothing was recorded says "no data": no trend, estimate or
target attainment is drawn from nothing.

**Periods**, on the report's own clock (`timezone`, Asia/Kolkata by default, which has no daylight saving time):
the daily report covers the whole day before the day it runs; the weekly one the seven days ending at the start of
`weekly_day`; the monthly one the calendar month before the month in which `monthly_day` falls. The scheduled job
(`manager_report`, config/schedule.toml, 04:00 Asia/Kolkata) writes the daily report every day, the weekly one on
`weekly_day` and the monthly one on `monthly_day` (or on the next run, when the PC was off that day).

**Sending is off by default** and stays off until every one of these holds at the moment of sending:
  1. `email_enabled` is on (Administration > Manager report);
  2. at least one recipient is set;
  3. a Super Admin has authorized exactly these recipients (recorded and audited; changing the recipients or the
     allow-list withdraws the authorization);
  4. OUTBOUND_EMAIL_ENABLED=true;
  5. a sending mailbox is configured and ready (the primary panel mailbox, else .env), as sending judges it;
  6. the automation is not stopped or in emergency stop;
  7. every recipient is internal (the sending mailbox's own company domain, or on the explicit allow-list; a free
     mail domain such as gmail.com is never "the company's domain", so such an address must be listed itself), is
     not a contact or a prospect company in the CRM, and is not on the do-not-contact list;
  8. the mailbox's daily limit (outreach.mailbox_daily_limit, all emails in 24 hours) leaves room for every recipient.
When one fails, the report is still written and stored, and the reason it was not sent is recorded with it.

**At most one email per period.** Before anything is handed to the mail server a delivery row is committed for the
period (table report_deliveries, unique per kind and period), as the follow-up sends do with their claim. A second run,
or a run after a crash, finds the row and sends nothing; a send cut off midway is marked "uncertain" for a person to
check, never sent again by itself. Only a send that certainly left nothing (the mail server could not be reached,
the sign-in was refused) may be tried again, by the job's normal retry.

These emails are internal notifications: they are not outreach, they use none of the prospect limits, and no prospect
is ever a recipient. Each one counts toward the mailbox's own daily limit, which protects its reputation.
"""

from __future__ import annotations

import hashlib
import logging
import re
import smtplib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment, select_autoescape
from sqlalchemy import and_, func, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aime import automation as am
from aime import funnel
from aime.agents.analytics import REAL_REPLY
from aime.automation import COUNTS_AS_SENT, audit
from aime.crm_ops import is_suppressed
from aime.db import kv_get, kv_set
from aime.dedupe import FREE_EMAIL_DOMAINS
from aime.knowledge.industries import industry_label
from aime.models import (Activity, AgentRun, Alert, Company, Contact, FollowUp, InboundEmail, JobRun, Lead, Meeting,
                         Opportunity, Report, ReportDelivery, StageChange, Suppression)
from aime.outreach import REACHED_PROSPECT
from aime.performance import NOT_SIMULATED_DEAL
from aime.timeutil import as_aware_utc, fmt_local, to_naive_utc, utcnow

log = logging.getLogger("aime.manager_report")

KINDS = {"daily": "manager_daily", "weekly": "manager_weekly", "monthly": "manager_monthly"}
KIND_TITLES = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
KV_KEY = "reporting.settings"
JOB = "manager_report"
# The code's defaults; [reporting] in config/business.toml may change them, and the Super Admin's page (stored in the
# database) changes them again. The authorization itself is never read from a file: only a person records it.
DEFAULTS: dict = {"email_enabled": False, "recipients": [], "daily_time": "04:00", "timezone": "Asia/Kolkata",
                  "weekly_day": "mon", "monthly_day": 1, "allowlist": []}
AUTH_KEYS = ("authorized_by", "authorized_at", "authorized_for")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMAIL = re.compile(r"^[a-z0-9.!#$&'+/=?^_`{|}~-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")
DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
# A mailbox at one of these is a person's own account, not a company's: its domain says nothing about who is internal.
PUBLIC_MAIL_DOMAINS = frozenset(FREE_EMAIL_DOMAINS | {"zoho.com", "yandex.com", "mail.com", "rediffmail.com", "outlook.in",
                                                      "yahoo.co.in", "hotmail.co.uk", "aol.co.uk", "pm.me", "fastmail.com"})
# What "verified" means for an email address, what may be written to and who is a decision-maker: defined once in
# aime.funnel, the single source of truth for the funnel stages, and counted here with the same conditions.
VERIFIED_EMAIL_STATUSES = funnel.VERIFIED_EMAIL_STATUSES
NOT_CONTACTABLE_STATUSES = funnel.NOT_CONTACTABLE_STATUSES
ID_CAP = 500  # ids kept per figure; the figure itself is always the full count
STALE_CLAIM = timedelta(minutes=10)
HELD_STATUSES = ("pending", "awaiting_approval", "approved", "uncertain")


# =============================================================================================== settings

def _listify(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[\s,;]+", value)
    return [str(v).strip().lower() for v in value if str(v).strip()]


def fingerprint(recipients, allowlist) -> str:
    """What an authorization covers: exactly these recipients and this allow-list."""
    raw = "\n".join(sorted(_listify(recipients))) + "|" + "\n".join(sorted(_listify(allowlist)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def config(session: Session, settings) -> dict:
    """The reporting settings in force now: the code's defaults, laid over by [reporting] in business.toml (never its
    authorization), laid over by what the Super Admin saved. Read fresh at every use, so a change counts at once."""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (settings.section("reporting") or {}).items() if k in DEFAULTS})
    stored = kv_get(session, KV_KEY) or {}
    out.update({k: v for k, v in stored.items() if k in DEFAULTS or k in AUTH_KEYS})
    out["recipients"], out["allowlist"] = _listify(out["recipients"]), _listify(out["allowlist"])
    out["email_enabled"] = bool(out["email_enabled"])
    try:
        out["monthly_day"] = int(out["monthly_day"])
    except (TypeError, ValueError):
        out["monthly_day"] = DEFAULTS["monthly_day"]
    out["weekly_day"] = str(out["weekly_day"]).strip().lower()[:3]
    if out["weekly_day"] not in WEEKDAYS:
        out["weekly_day"] = DEFAULTS["weekly_day"]
    try:
        ZoneInfo(str(out["timezone"]))
    except (ZoneInfoNotFoundError, ValueError):
        out["timezone"] = DEFAULTS["timezone"]
    return out


def authorization(cfg: dict) -> tuple[bool, str]:
    """Whether a person's authorization covers the recipients as they are now, and in words why not."""
    if not cfg.get("authorized_by") or not cfg.get("authorized_at"):
        return False, "no Super Admin has authorized emailing this report"
    if cfg.get("authorized_for") != fingerprint(cfg["recipients"], cfg["allowlist"]):
        return False, "the recipients changed after the authorization, so it no longer applies: authorize them again"
    return True, f"authorized by {cfg['authorized_by']} at {cfg['authorized_at'][:16].replace('T', ' ')} UTC"


def apply_schedule(settings, cfg: dict) -> None:
    """Lay the report's time and zone over the `manager_report` job's schedule (in place, as admin_settings does for the
    sales settings), so the scheduler runs it when the Super Admin said. config/schedule.toml stays the default."""
    jobs = settings.schedule.setdefault("jobs", {})
    if JOB in jobs:
        jobs[JOB] = {**jobs[JOB], "daily_at": cfg["daily_time"], "timezone": cfg["timezone"]}


def load_schedule(session: Session, settings) -> None:
    """At start-up: only what a person saved changes the schedule; the file's own entry stands otherwise."""
    stored = kv_get(session, KV_KEY) or {}
    if "daily_time" in stored or "timezone" in stored:
        apply_schedule(settings, config(session, settings))


def save(session: Session, settings, actor: str, form: dict) -> list[str]:
    """Validate and store the Super Admin's reporting settings; returns what changed, in words. Raises ValueError with
    the reason when a value is not valid or a recipient may not receive the report. A change of the recipients or of
    the allow-list withdraws the authorization. Every change is in the audit log with the old and new value."""
    cfg = config(session, settings)
    new = dict(cfg)
    if "recipients" in form:
        new["recipients"] = _listify(form["recipients"])
        bad = [a for a in new["recipients"] if not EMAIL.match(a)]
        if bad:
            raise ValueError(f"not an email address: {', '.join(bad)}")
        if len(new["recipients"]) > 10:
            raise ValueError("at most 10 recipients")
    if "allowlist" in form:
        new["allowlist"] = _listify(form["allowlist"])
        bad = [a for a in new["allowlist"] if not (EMAIL.match(a) or DOMAIN.match(a))]
        if bad:
            raise ValueError(f"the allow-list takes email addresses or domains: {', '.join(bad)}")
        public = [a for a in new["allowlist"] if "@" not in a and a in PUBLIC_MAIL_DOMAINS]
        if public:
            raise ValueError(f"{', '.join(public)} is a public mail service: list the exact addresses instead")
    if "daily_time" in form:
        if not HHMM.match(str(form["daily_time"]).strip()):
            raise ValueError("the time must look like 04:00")
        new["daily_time"] = str(form["daily_time"]).strip()
    if "timezone" in form:
        try:
            ZoneInfo(str(form["timezone"]).strip())
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("the time zone must be one such as Asia/Kolkata or America/New_York") from exc
        new["timezone"] = str(form["timezone"]).strip()
    if "weekly_day" in form:
        day = str(form["weekly_day"]).strip().lower()[:3]
        if day not in WEEKDAYS:
            raise ValueError("the weekly day must be one of Mon, Tue, Wed, Thu, Fri, Sat, Sun")
        new["weekly_day"] = day
    if "monthly_day" in form:
        try:
            md = int(str(form["monthly_day"]).strip())
        except ValueError as exc:
            raise ValueError("the monthly day must be a number from 1 to 28") from exc
        if not 1 <= md <= 28:
            raise ValueError("the monthly day must be a number from 1 to 28")
        new["monthly_day"] = md
    if "email_enabled" in form:
        new["email_enabled"] = bool(form["email_enabled"])
    problems = recipient_problems(session, settings, new)
    if problems:
        raise ValueError("; ".join(f"{a}: {why}" for a, why in problems))
    stored = dict(kv_get(session, KV_KEY) or {})
    changes = []
    for key in DEFAULTS:
        if new[key] != cfg[key]:
            changes.append(f"{key}: {cfg[key]!r} -> {new[key]!r}")
            stored[key] = new[key]
    if not changes:
        return []
    was_authorized = authorization(cfg)[0]
    if fingerprint(new["recipients"], new["allowlist"]) != cfg.get("authorized_for") and cfg.get("authorized_by"):
        for key in AUTH_KEYS:
            stored.pop(key, None)
        if was_authorized:
            changes.append("authorization withdrawn: the recipients changed")
            audit(session, actor, "reporting_authorization_withdrawn", "manager report",
                  f"recipients changed after {cfg['authorized_by']} authorized them")
    kv_set(session, KV_KEY, stored)
    audit(session, actor, "reporting_settings_changed", "manager report", "; ".join(changes)[:2000])
    apply_schedule(settings, config(session, settings))
    return changes


def authorize(session: Session, settings, actor: str) -> str:
    """A Super Admin authorizes emailing the report to the recipients as they are now. Recorded and audited."""
    cfg = config(session, settings)
    if not cfg["recipients"]:
        raise ValueError("set at least one recipient before authorizing")
    problems = recipient_problems(session, settings, cfg)
    if problems:
        raise ValueError("; ".join(f"{a}: {why}" for a, why in problems))
    stored = dict(kv_get(session, KV_KEY) or {})
    stored.update(authorized_by=actor, authorized_at=utcnow().isoformat(timespec="seconds"),
                  authorized_for=fingerprint(cfg["recipients"], cfg["allowlist"]))
    kv_set(session, KV_KEY, stored)
    audit(session, actor, "reporting_authorized", "manager report", f"recipients: {', '.join(cfg['recipients'])}")
    return f"Authorized emailing the report to {', '.join(cfg['recipients'])}"


def revoke(session: Session, actor: str) -> None:
    stored = dict(kv_get(session, KV_KEY) or {})
    if any(stored.pop(k, None) for k in AUTH_KEYS):
        kv_set(session, KV_KEY, stored)
        audit(session, actor, "reporting_authorization_withdrawn", "manager report", "withdrawn by a Super Admin")


def sending_address(session: Session, settings) -> str:
    from aime import mail_accounts

    return (mail_accounts.sending_settings(session, settings).smtp_from or "").strip().lower()


def recipient_problems(session: Session, settings, cfg: dict) -> list[tuple[str, str]]:
    """(address, why) for every recipient that may not receive the report: one outside the company, a prospect, or one
    on the do-not-contact list. Internal: the sending mailbox's own domain (unless that is a public mail service), or on
    the allow-list, as the exact address or its domain."""
    sender = sending_address(session, settings)
    own = sender.rpartition("@")[2] if "@" in sender else ""
    own = "" if own in PUBLIC_MAIL_DOMAINS else own
    allow = set(cfg.get("allowlist") or [])
    out = []
    for addr in cfg.get("recipients") or []:
        domain = addr.rpartition("@")[2]
        if not (addr in allow or (domain in allow and domain not in PUBLIC_MAIL_DOMAINS) or (own and domain == own)):
            where = (f"the sending mailbox's domain {own}" if own else
                     "the sending mailbox's company domain (it has none: " + (f"{sender} is a public mail service" if sender else
                                                                              "no sending mailbox is configured") + ")")
            out.append((addr, f"not an internal address: not at {where} and not on the allow-list"))
            continue
        if session.execute(select(Contact.id).where(func.lower(Contact.email) == addr)).first():
            out.append((addr, "is a contact in the CRM: the report never goes to a prospect"))
        elif session.execute(select(Company.id).where(func.lower(Company.domain) == domain,
                                                      Company.merged_into_id.is_(None))).first():
            out.append((addr, f"{domain} is a prospect company's domain in the CRM: the report never goes to a prospect"))
        elif is_suppressed(session, addr):
            out.append((addr, "is on the do-not-contact list"))
    return out


# =============================================================================================== periods

@dataclass(frozen=True)
class Period:
    kind: str  # daily | weekly | monthly | custom
    start: date  # first day covered, on the report's clock
    end: date  # the day after the last one covered
    tz: str
    until: datetime | None = None  # naive UTC: "so far" figures end here instead of at the end's midnight

    @property
    def start_utc(self) -> datetime:
        return to_naive_utc(datetime.combine(self.start, time(0), tzinfo=ZoneInfo(self.tz)))

    @property
    def end_utc(self) -> datetime:
        return self.until or to_naive_utc(datetime.combine(self.end, time(0), tzinfo=ZoneInfo(self.tz)))

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def label(self) -> str:
        last = self.end - timedelta(days=1)
        if self.until:
            return f"{self.start:%a %d %b %Y}, so far"
        if self.kind == "monthly":
            return f"{self.start:%B %Y}"
        if self.days == 1:
            return f"{self.start:%a %d %b %Y}"
        return f"{self.start:%a %d %b} – {last:%a %d %b %Y}"


def latest_period(kind: str, today: date, cfg: dict) -> Period:
    """The latest whole period of this kind that has ended by `today` (a date on the report's clock)."""
    tz = cfg["timezone"]
    if kind == "daily":
        return Period("daily", today - timedelta(days=1), today, tz)
    if kind == "weekly":
        end = today - timedelta(days=(today.weekday() - WEEKDAYS.index(cfg["weekly_day"])) % 7)
        return Period("weekly", end - timedelta(days=7), end, tz)
    send_day = today.replace(day=cfg["monthly_day"]) if today.day >= cfg["monthly_day"] else \
        (today.replace(day=1) - timedelta(days=1)).replace(day=cfg["monthly_day"])
    end = send_day.replace(day=1)
    return Period("monthly", (end - timedelta(days=1)).replace(day=1), end, tz)


def send_day(period: Period, cfg: dict) -> date:
    """The day a period's report is due: the day after a daily one, the weekly day, the monthly day."""
    if period.kind == "monthly":
        return period.end.replace(day=cfg["monthly_day"])
    return period.end


def local_today(cfg: dict, now: datetime | None = None) -> date:
    return as_aware_utc(now or utcnow()).astimezone(ZoneInfo(cfg["timezone"])).date()


# =============================================================================================== counting

def _sql(session: Session, stmt) -> str:
    try:
        sql = str(stmt.compile(dialect=session.get_bind().dialect, compile_kwargs={"literal_binds": True}))
    except Exception:  # noqa: BLE001 - a type without a literal form: the statement with its parameters
        compiled = stmt.compile(dialect=session.get_bind().dialect)
        sql = f"{compiled} -- params {compiled.params}"
    return " ".join(sql.split())


class Tally:
    """Counts records for one period, keeping for each figure its definition, its SQL and the ids it counted."""

    def __init__(self, session: Session, period: Period):
        self.s = session
        self.p = period
        self.metrics: dict[str, dict] = {}
        self.full_ids: dict[str, list[int]] = {}

    @property
    def within(self):
        start, end = self.p.start_utc, self.p.end_utc
        return lambda col: and_(col >= start, col < end)

    def count(self, key: str, label: str, definition: str, column, *conds, snapshot: bool = False) -> int:
        stmt = select(column).where(*conds).distinct().order_by(column)
        ids = [i for i in self.s.execute(stmt).scalars() if i is not None]
        self.full_ids[key] = ids
        self.metrics[key] = {"key": key, "label": label, "value": len(ids), "definition": definition,
                             "sql": _sql(self.s, stmt), "ids": ids[:ID_CAP], "ids_total": len(ids),
                             "table": column.table.name, "snapshot": snapshot}
        return len(ids)

    def value(self, key: str) -> int:
        return self.metrics[key]["value"]

    def rate(self, key: str, label: str, part: str, whole: str, definition: str) -> None:
        n, d = self.value(part), self.value(whole)
        self.metrics[key] = {"key": key, "label": label, "value": round(100 * n / d) if d else None, "part": n, "whole": d,
                             "definition": definition, "rate": True,
                             "text": f"{round(100 * n / d)}% ({n} of {d})" if d else "no data (nothing to divide by)"}


def _company_of(session: Session, table: str, ids: list[int]) -> dict[int, int | None]:
    """Record id -> company id, for the per-country and per-industry figures."""
    if not ids:
        return {}
    if table == "companies":
        return {i: i for i in ids}
    if table == "contacts":
        rows = session.execute(select(Contact.id, Contact.company_id).where(Contact.id.in_(ids))).all()
    elif table == "leads":
        rows = session.execute(select(Lead.id, Lead.company_id).where(Lead.id.in_(ids))).all()
    elif table == "followups":
        rows = session.execute(select(FollowUp.id, Lead.company_id).join(Lead, Lead.id == FollowUp.lead_id)
                               .where(FollowUp.id.in_(ids))).all()
    elif table == "inbound_emails":
        rows = session.execute(select(InboundEmail.id, Lead.company_id).join(Lead, Lead.id == InboundEmail.lead_id)
                               .where(InboundEmail.id.in_(ids))).all()
    elif table == "meetings":
        rows = session.execute(select(Meeting.id, func.coalesce(Meeting.company_id, Lead.company_id))
                               .outerjoin(Lead, Lead.id == Meeting.lead_id).where(Meeting.id.in_(ids))).all()
    else:
        return {}
    return dict(rows)


def _targets(settings) -> dict[str, dict]:
    """settings.section("targets"), when present: daily goals per country, as {"US": {"verified_decision_makers": 20}} or
    as a list of {"country": "US", "daily_verified_leads": 20, ...}. Goals are per day; a week's is seven days' worth."""
    raw = settings.section("targets") or {}
    rows = raw if isinstance(raw, list) else raw.get("countries", raw) if isinstance(raw, dict) else {}
    out: dict[str, dict] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("country"):
                out[str(row["country"]).upper()] = {k: v for k, v in row.items() if k != "country"}
    elif isinstance(rows, dict):
        out = {str(k).upper(): v for k, v in rows.items() if isinstance(v, dict)}
    return out


TARGET_ALIASES = {
    "companies_discovered": ("companies_discovered", "discovered_companies", "companies"),
    "verified_decision_makers": ("verified_decision_makers", "verified_leads", "verified_lead", "verified"),
    "contactable_prospects": ("contactable_prospects", "contactable", "contactable_leads"),
    "qualified_leads": ("qualified_leads", "qualified"),
    "outreach_sent": ("outreach_sent", "emails_sent", "outreach"),
    "meetings_booked": ("meetings_booked", "meetings"),
}


def _target_for(goals: dict, metric: str) -> float | None:
    for name in TARGET_ALIASES.get(metric, (metric,)):
        for key in (name, f"daily_{name}", f"{name}_daily", f"{name}_per_day"):
            value = goals.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return float(value)
    return None


def _ai_decisions(session: Session, period: Period) -> dict | None:
    """The AI decisions log (aime.models.AIDecision, written by aime.ai): how many proposals in the period, by policy
    verdict, how many were blocked, sent to a person, or escalated. None when this database predates the table. Read-only."""
    from aime.models import AIDecision

    if not inspect(session.get_bind()).has_table("ai_decisions"):
        return None
    rows = session.execute(select(AIDecision.verdict, AIDecision.escalations)
                           .where(AIDecision.created_at >= period.start_utc, AIDecision.created_at < period.end_utc)).all()
    counted: dict[str, int] = {}
    for verdict, _ in rows:
        counted[str(verdict)] = counted.get(str(verdict), 0) + 1
    escalated = sum(1 for _, esc in rows if esc)
    return {"total": len(rows), "by": counted, "blocked": counted.get("block", 0), "human_review": counted.get("human_review", 0),
            "escalated": escalated, "escalated_or_blocked": len([1 for v, esc in rows if v == "block" or esc]),
            "definition": "Rows of ai_decisions created in the period, by policy verdict (auto_internal, human_review, block); "
                          "escalated: a proposal that named at least one escalation rule."}


def build(session: Session, settings, period: Period, now: datetime | None = None, full_ids: bool = False) -> dict:
    """Every figure of the report for this period, from the records. Returns plain data (JSON-safe), stored with the
    report: the figures, their definitions, SQL and ids, the breakdowns, and whether the period holds any data at all."""
    now = now or utcnow()
    t = Tally(session, period)
    w = t.within
    real_co = funnel.real_company()
    real_contact = funnel.real_contact()
    named = funnel.decision_maker()
    has_email = funnel.contactable()
    verified = funnel.verified_email()

    # ---- discovery and contacts
    t.count("companies_discovered", "Companies discovered",
            "Companies added to the CRM in the period (merged duplicates and sample data left out).",
            Company.id, w(Company.created_at), *real_co)
    t.count("decision_makers", "Decision-makers identified (named people)",
            "Contacts added in the period with a last name, found on the company's own website, in its SEC filings or on an "
            "official company register, whose title decides (not a former employee, an assistant, an office manager or a social media role).",
            Contact.id, w(Contact.created_at), *real_contact, *named)
    t.count("contactable_prospects", "Contactable prospects (any email)",
            "Contacts added in the period with an email address that may be written to: not opted out, not on the "
            "do-not-contact list, not bounced and not on a domain without a mail server. Company inboxes included.",
            Contact.id, w(Contact.created_at), *real_contact, *has_email)
    t.count("verified_contacts", "Verified contacts",
            f"Contactable prospects whose email status is {', '.join(VERIFIED_EMAIL_STATUSES)} (published: on the "
            "company's own website next to the person, on its own domain, which takes mail). A company inbox "
            "(role address such as info@ or sales@) is never verified.",
            Contact.id, w(Contact.created_at), *real_contact, *has_email, *verified)
    t.count("verified_decision_makers", "Verified decision-makers",
            "Decision-makers identified in the period whose own email address is verified (as above). A company "
            "inbox does not count, even when printed under a person's name.",
            Contact.id, w(Contact.created_at), *real_contact, *named, *has_email, *verified)
    t.count("role_addresses", "Company inboxes found (not counted as verified)",
            "Contactable prospects added in the period whose address is a company inbox (email status role_address).",
            Contact.id, w(Contact.created_at), *real_contact, *has_email, Contact.email_status == "role_address")
    t.rate("verification_rate", "Verification rate", "verified_contacts", "contactable_prospects",
           "Verified contacts divided by contactable prospects, both added in the period.")
    t.rate("dm_verification_rate", "Decision-makers with a verified email", "verified_decision_makers", "decision_makers",
           "Verified decision-makers divided by decision-makers identified, both added in the period.")

    # ---- qualification
    moved = lambda status: (Activity.type == "status_change", w(Activity.created_at), Activity.lead_id.is_not(None),  # noqa: E731
                            Activity.subject.like(f"%→ {status}%"))
    t.count("qualified_leads", "Qualified leads",
            "Leads moved to qualified in the period (by scoring or by a person), each lead once.",
            Activity.lead_id, *moved("qualified"))
    t.count("disqualified_leads", "Disqualified leads",
            "Leads moved to disqualified in the period, each lead once.", Activity.lead_id, *moved("disqualified"))
    t.metrics["decided_leads"] = {"key": "decided_leads", "label": "Leads decided (qualified + disqualified)",
                                  "value": t.value("qualified_leads") + t.value("disqualified_leads"),
                                  "definition": "Qualified leads plus disqualified leads in the period."}
    t.rate("qualification_rate", "Qualified share of leads decided", "qualified_leads", "decided_leads",
           "Qualified leads divided by leads qualified or disqualified in the period.")

    # ---- outreach
    outreach = (FollowUp.kind == "outreach",)
    t.count("outreach_drafted", "Outreach drafted",
            "Outreach emails drafted in the period, except drafts cancelled before anyone approved them.",
            FollowUp.id, *outreach, w(FollowUp.created_at), or_(FollowUp.status != "cancelled", FollowUp.approved_at.is_not(None)))
    t.count("outreach_approved", "Outreach approved", "Outreach emails approved (by a person or a campaign) in the period.",
            FollowUp.id, *outreach, w(FollowUp.approved_at))
    t.count("outreach_sent", "Outreach sent to prospects",
            "Outreach emails sent in the period that reached the prospect: a test-mode delivery to our own test address "
            "is not counted.", FollowUp.id, *outreach, FollowUp.status == "sent", w(FollowUp.sent_at), REACHED_PROSPECT)
    t.count("leads_contacted", "Leads emailed", "Leads that were sent outreach in the period (as above), each lead once.",
            FollowUp.lead_id, *outreach, FollowUp.status == "sent", w(FollowUp.sent_at), REACHED_PROSPECT)
    t.count("emails_sent", "All emails sent to prospects",
            "Every email (outreach, follow-ups and answers) sent in the period that reached the prospect; test-mode "
            "deliveries left out.", FollowUp.id, FollowUp.channel == "email", FollowUp.status == "sent", w(FollowUp.sent_at),
            REACHED_PROSPECT)
    t.count("test_mode_deliveries", "Test-mode deliveries (not counted)",
            "Emails sent in the period that went to the test address instead of the prospect (OUTBOUND_TEST_RECIPIENTS).",
            FollowUp.id, FollowUp.channel == "email", FollowUp.status == "sent", w(FollowUp.sent_at), ~REACHED_PROSPECT)

    # ---- replies
    human = (InboundEmail.is_simulated.is_(False), InboundEmail.lead_id.is_not(None))
    t.count("replies", "Replies from prospects",
            "Emails received in the period from a person at a lead (interested, not interested, objection, referral, "
            "question, other): no bounces, auto-replies, unsubscribes or simulated replies.",
            InboundEmail.id, *human, InboundEmail.category.in_(REAL_REPLY), w(InboundEmail.received_at))
    t.count("interested_replies", "Interested replies", "Replies in the period classified as interested.",
            InboundEmail.id, *human, InboundEmail.category == "interested", w(InboundEmail.received_at))
    t.count("leads_replied", "Leads that replied", "Leads with a reply (as above) in the period, each lead once.",
            InboundEmail.lead_id, *human, InboundEmail.category.in_(REAL_REPLY), w(InboundEmail.received_at))
    contacted = t.full_ids["leads_contacted"]
    t.count("contacted_and_replied", "Leads emailed that replied",
            "Of the leads emailed in the period, those that also replied in the period.",
            InboundEmail.lead_id, *human, InboundEmail.category.in_(REAL_REPLY), w(InboundEmail.received_at),
            InboundEmail.lead_id.in_(contacted or [-1]))
    t.rate("engagement_rate", "Reply rate", "contacted_and_replied", "leads_contacted",
           "Leads emailed in the period that replied in the period, divided by leads emailed in the period.")
    t.count("unsubscribes", "Unsubscribes", "Unsubscribe requests received in the period (simulated ones left out).",
            InboundEmail.id, InboundEmail.is_simulated.is_(False), InboundEmail.category == "unsubscribe", w(InboundEmail.received_at))
    t.count("bounces", "Bounces", "Bounce notices received in the period (simulated ones left out).",
            InboundEmail.id, InboundEmail.is_simulated.is_(False), InboundEmail.category == "bounce", w(InboundEmail.received_at))
    t.rate("bounce_rate", "Bounce rate", "bounces", "emails_sent",
           "Bounce notices received in the period divided by all emails sent to prospects in the period.")
    t.count("simulated_replies", "Simulated replies (not counted)", "Replies made by `aime simulate-reply` in the period.",
            InboundEmail.id, InboundEmail.is_simulated.is_(True), w(InboundEmail.received_at))

    # ---- follow-ups
    t.count("followup_emails_sent", "Follow-up emails sent",
            "Later steps of an email sequence (step 2 onwards) sent to the prospect in the period; test-mode deliveries left out.",
            FollowUp.id, FollowUp.channel == "email", FollowUp.status == "sent", FollowUp.step > 1, w(FollowUp.sent_at), REACHED_PROSPECT)
    t.count("answers_sent", "Answers to prospects sent",
            "Emails answering a prospect or an inquiry (not outreach) sent to them in the period; test-mode deliveries left out.",
            FollowUp.id, FollowUp.channel == "email", FollowUp.status == "sent", or_(FollowUp.kind.is_(None), FollowUp.kind != "outreach"),
            w(FollowUp.sent_at), REACHED_PROSPECT)
    t.count("tasks_completed", "Follow-up tasks completed",
            "Follow-up tasks marked done in the period (a research review task, which closes by itself, left out).",
            FollowUp.id, FollowUp.channel == "task", FollowUp.status == "done", FollowUp.subject.not_like("Review %"),
            w(FollowUp.updated_at))
    t.count("tasks_overdue", "Follow-up tasks overdue at the end of the period",
            "Follow-up tasks still pending whose due time was before the end of the period (as the records stand now).",
            FollowUp.id, FollowUp.channel == "task", FollowUp.status == "pending", FollowUp.due_at < period.end_utc, snapshot=True)

    # ---- meetings
    real_meeting = (Meeting.is_simulated.is_(False),)
    t.count("meetings_booked", "Meetings booked", "Meetings recorded in the period, not cancelled and not simulated.",
            Meeting.id, *real_meeting, Meeting.status != "cancelled", w(Meeting.created_at))
    t.count("meetings_held", "Meetings held", "Meetings that took place in the period and were marked held.",
            Meeting.id, *real_meeting, Meeting.status == "completed", w(Meeting.starts_at))
    t.count("simulated_meetings", "Simulated meetings (not counted)", "Meetings from simulated replies recorded in the period.",
            Meeting.id, Meeting.is_simulated.is_(True), w(Meeting.created_at))

    # ---- pipeline
    real_deal = Opportunity.id.in_(select(Opportunity.id).where(NOT_SIMULATED_DEAL))
    t.count("opportunities_opened", "Opportunities opened", "Deals opened in the period (a deal from a simulated reply left out).",
            Opportunity.id, w(Opportunity.created_at), NOT_SIMULATED_DEAL)
    t.count("deals_won", "Deals won", "Deals moved to won in the period.", Opportunity.id, Opportunity.stage == "won",
            w(Opportunity.closed_at), NOT_SIMULATED_DEAL)
    t.count("deals_lost", "Deals lost", "Deals moved to lost in the period.", Opportunity.id, Opportunity.stage == "lost",
            w(Opportunity.closed_at), NOT_SIMULATED_DEAL)
    won_ids = t.full_ids["deals_won"]
    won_value = float(session.execute(select(func.coalesce(func.sum(Opportunity.est_value), 0.0))
                                      .where(Opportunity.id.in_(won_ids or [-1]))).scalar_one())
    moves = []
    for stage_name, ids in _grouped(session, StageChange.to_stage, StageChange.id,
                                    w(StageChange.at), StageChange.from_stage.is_not(None),
                                    StageChange.opportunity_id.in_(select(Opportunity.id).where(NOT_SIMULATED_DEAL))):
        moves.append({"stage": stage_name, "value": len(ids), "ids": ids[:ID_CAP]})
    open_by_stage = []
    for stage_name, ids in _grouped(session, Opportunity.stage, Opportunity.id, real_deal):
        open_by_stage.append({"stage": stage_name, "value": len(ids), "ids": ids[:ID_CAP]})

    # ---- blocked activities (as they stand now, except where a time is named)
    held_rows = session.execute(select(FollowUp.id, FollowUp.status, FollowUp.blocked_reason).where(
        FollowUp.channel == "email", FollowUp.status.in_(HELD_STATUSES), FollowUp.blocked_reason.is_not(None))
        .order_by(FollowUp.id)).all()
    held = defaultdict(list)
    for fid, _status, reason in held_rows:
        held[_reason_group(reason)].append(fid)
    t.count("uncertain_sends", "Sends of unknown outcome", "Emails whose sending was cut off: a person checks the Sent folder.",
            FollowUp.id, FollowUp.channel == "email", FollowUp.status == "uncertain", snapshot=True)
    t.count("suppressions_added", "Added to the do-not-contact list",
            "Addresses and domains put on the do-not-contact list in the period, and not lifted since.",
            Suppression.id, w(Suppression.created_at), Suppression.lifted_at.is_(None))
    t.count("suppressions_active", "On the do-not-contact list now", "Entries on the do-not-contact list that are not lifted.",
            Suppression.id, Suppression.lifted_at.is_(None), snapshot=True)
    t.count("failed_jobs", "Failed job runs", "Scheduled or manual job runs started in the period that failed, failed in part "
            "or were cut off.", JobRun.id, w(JobRun.started_at), JobRun.status.in_(("failed", "partial", "abandoned")))
    t.count("error_alerts", "Error alerts raised", "Error-level alerts raised in the period.", Alert.id,
            Alert.level == "error", w(Alert.created_at))
    failed_by_job = Counter(session.execute(select(JobRun.job).where(
        JobRun.id.in_(t.full_ids["failed_jobs"] or [-1]))).scalars())
    # ---- agents
    agents = []
    runs = session.execute(select(AgentRun.agent, AgentRun.status, AgentRun.duration_ms, AgentRun.llm_calls,
                                  AgentRun.llm_failures, AgentRun.id).where(w(AgentRun.started_at)).order_by(AgentRun.id)).all()
    per = defaultdict(list)
    for row in runs:
        per[row.agent].append(row)
    for name in sorted(per):
        rows = per[name]
        durations = [r.duration_ms for r in rows if r.duration_ms is not None]
        ok = sum(r.status == "success" for r in rows)
        failed = [r.id for r in rows if r.status in ("failed", "abandoned")]
        agents.append({"agent": name, "runs": len(rows), "success": ok, "failed": len(failed),
                       "success_rate": round(100 * ok / len(rows)), "avg_s": round(sum(durations) / len(durations) / 1000, 1) if durations else None,
                       "max_s": round(max(durations) / 1000, 1) if durations else None,
                       "llm_calls": sum(r.llm_calls or 0 for r in rows), "llm_failures": sum(r.llm_failures or 0 for r in rows),
                       "failed_ids": failed[:ID_CAP]})
    t.count("agent_runs", "Agent runs", "Agent runs started in the period.", AgentRun.id, w(AgentRun.started_at))
    t.count("agent_failures", "Agent runs failed", "Agent runs started in the period that failed or were cut off.",
            AgentRun.id, w(AgentRun.started_at), AgentRun.status.in_(("failed", "abandoned")))

    # ---- countries and industries
    goals = _targets(settings)
    breakdown_keys = ("companies_discovered", "verified_decision_makers", "contactable_prospects", "qualified_leads",
                      "outreach_sent", "replies", "meetings_booked")
    country_of, industry_of = {}, {}
    company_ids: set[int] = set()
    record_company: dict[str, dict[int, int | None]] = {}
    for key in breakdown_keys:
        m = t.metrics[key]
        table = "leads" if key == "qualified_leads" else m["table"]
        record_company[key] = _company_of(session, table, t.full_ids[key])
        company_ids |= {c for c in record_company[key].values() if c is not None}
    if company_ids:
        for cid, country, industry in session.execute(select(Company.id, Company.country, Company.industry)
                                                      .where(Company.id.in_(company_ids))).all():
            country_of[cid] = (country or "unknown").upper()
            industry_of[cid] = industry or "unknown"
    by_country: dict[str, Counter] = defaultdict(Counter)
    by_industry: dict[str, Counter] = defaultdict(Counter)
    for key in breakdown_keys:
        for _rid, cid in record_company[key].items():
            by_country[country_of.get(cid, "unknown")][key] += 1
            by_industry[industry_of.get(cid, "unknown")][key] += 1
    countries = []
    for code in sorted(set(by_country) | set(goals)):
        row = {"country": code, "figures": {}}
        for key in breakdown_keys:
            goal = _target_for(goals.get(code, {}), key)
            got = by_country[code][key]
            target = goal * period.days if goal is not None else None
            row["figures"][key] = {"value": got, "target": target,
                                   "attainment": round(100 * got / target) if target else None}
        countries.append(row)
    industries = [{"industry": k, "label": industry_label(k) if k != "unknown" else "Unknown",
                   "figures": {key: by_industry[k][key] for key in breakdown_keys}}
                  for k in sorted(by_industry, key=lambda x: -sum(by_industry[x].values()))]

    period_keys = [k for k, m in t.metrics.items() if not m.get("snapshot") and not m.get("rate")
                   and k not in ("decided_leads",) and "value" in m]
    has_data = any(t.metrics[k]["value"] for k in period_keys) or bool(moves)
    return {
        "kind": period.kind, "title": f"{KIND_TITLES.get(period.kind, 'Sales')} Sales Manager Report",
        "period": {"kind": period.kind, "start": period.start.isoformat(), "end": period.end.isoformat(), "tz": period.tz,
                   "label": period.label, "from_utc": period.start_utc.isoformat(), "to_utc": period.end_utc.isoformat()},
        "generated_at": now.isoformat(timespec="seconds"), "has_data": has_data,
        "metrics": t.metrics,
        "pipeline": {"stage_moves": moves, "by_stage_now": open_by_stage, "won_value": won_value,
                     "stage_moves_definition": "Stage changes of deals in the period, by the stage moved to (a deal "
                                               "opening is not a move; deals from simulated replies left out).",
                     "by_stage_definition": "All deals by their stage now (deals from simulated replies left out)."},
        "blocked": {"held_sends": [{"reason": r, "value": len(ids), "ids": ids[:ID_CAP]}
                                   for r, ids in sorted(held.items(), key=lambda kv: -len(kv[1]))],
                    "held_definition": "Emails waiting with a reason they cannot go out yet (pending, awaiting approval, "
                                       "approved or of unknown outcome, with a blocked reason), as they stand now.",
                    "automation_state": am.get_state(session), "automation_mode": am.get_mode(session),
                    "failed_jobs_by_job": dict(failed_by_job), "ai_decisions": _ai_decisions(session, period)},
        "agents": agents,
        "countries": countries, "industries": industries,
        "targets_set": bool(goals),
        "breakdown_keys": list(breakdown_keys),
        **({"full_ids": t.full_ids} if full_ids else {}),  # for the dashboard's charts; never stored
    }


def _grouped(session: Session, key_col, id_col, *conds) -> list[tuple[str, list[int]]]:
    out = defaultdict(list)
    for k, i in session.execute(select(key_col, id_col).where(*conds).order_by(id_col)).all():
        out[str(k)].append(i)
    return sorted(out.items(), key=lambda kv: (-len(kv[1]), kv[0]))


_NUMBERS = re.compile(r"\d+")
_DOMAINISH = re.compile(r"\b[\w.-]+@[\w.-]+\b|\b[\w-]+(\.[\w-]+)+\b")


def _reason_group(reason: str) -> str:
    """A held send's reason with its addresses, domains and numbers taken out, so the same reason groups together."""
    first = reason.split(";")[0].split(" — ")[0]
    return " ".join(_NUMBERS.sub("N", _DOMAINISH.sub("…", first)).split())[:120]


# =============================================================================================== rendering

ORDER = [
    ("Discovery and contacts", ["companies_discovered", "decision_makers", "contactable_prospects", "verified_contacts",
                                "verified_decision_makers", "role_addresses", "verification_rate", "dm_verification_rate"]),
    ("Qualification", ["qualified_leads", "disqualified_leads", "qualification_rate"]),
    ("Outreach", ["outreach_drafted", "outreach_approved", "outreach_sent", "leads_contacted", "emails_sent"]),
    ("Replies", ["replies", "interested_replies", "leads_replied", "engagement_rate", "simulated_replies"]),
    ("Follow-ups", ["followup_emails_sent", "answers_sent", "tasks_completed", "tasks_overdue"]),
    ("Meetings", ["meetings_booked", "meetings_held", "simulated_meetings"]),
    ("Pipeline", ["opportunities_opened", "deals_won", "deals_lost"]),
    ("Deliverability", ["bounces", "bounce_rate", "unsubscribes", "test_mode_deliveries", "suppressions_added",
                        "suppressions_active"]),
    ("Blocked activities", ["uncertain_sends", "failed_jobs", "error_alerts"]),
    ("Agent performance", ["agent_runs", "agent_failures"]),
]
LABELS = {"companies_discovered": "Companies", "verified_decision_makers": "Verified DMs", "contactable_prospects": "Contactable",
          "qualified_leads": "Qualified", "outreach_sent": "Outreach sent", "replies": "Replies", "meetings_booked": "Meetings"}


def shown(m: dict) -> str:
    if m.get("rate"):
        return m["text"]
    return f"{m['value']:,}"


TEXT_TEMPLATE = """{{ title }} - {{ company }}
{{ period.label }} ({{ period.tz }})
Generated {{ generated }}. Every figure is a count of records in the CRM; definitions and record ids are stored with the report.
{% if not has_data %}
NO DATA: nothing was recorded in this period. No figures, trends or targets are shown for it.
{% else %}{% for heading, rows in sections %}
{{ heading|upper }}
{% for m in rows %}  {{ m.label }}: {{ shown(m) }}
{% endfor %}{% endfor %}
PIPELINE
  Stage moves: {% if pipeline.stage_moves %}{% for r in pipeline.stage_moves %}{{ r.stage }} {{ r.value }}{{ ', ' if not loop.last }}{% endfor %}{% else %}none{% endif %}
  Won value (entered on the deals): {{ '${:,.0f}'.format(pipeline.won_value) if pipeline.won_value else 'none' }}
{% if agents %}
AGENTS (runs / succeeded / failed / average seconds)
{% for a in agents %}  {{ a.agent }}: {{ a.runs }} / {{ a.success }} / {{ a.failed }} / {{ a.avg_s if a.avg_s is not none else '-' }}
{% endfor %}{% endif %}{% if countries %}
BY COUNTRY (achieved / target)
{% for c in countries %}  {{ c.country }}: {% for key in breakdown_keys %}{% set f = c.figures[key] %}{{ labels[key] }} {{ f.value }}/{{ f.target|int if f.target is not none else 'no target set' }}{{ ', ' if not loop.last }}{% endfor %}
{% endfor %}{% endif %}{% if industries %}
BY INDUSTRY
{% for i in industries %}  {{ i.label }}: {% for key in breakdown_keys %}{{ labels[key] }} {{ i.figures[key] }}{{ ', ' if not loop.last }}{% endfor %}
{% endfor %}{% endif %}{% endif %}
AS THINGS STAND NOW
  Deals by stage: {% if pipeline.by_stage_now %}{% for r in pipeline.by_stage_now %}{{ r.stage }} {{ r.value }}{{ ', ' if not loop.last }}{% endfor %}{% else %}none{% endif %}
  Automation: {{ blocked.automation_state }} (mode {{ blocked.automation_mode }})
  Emails held: {% if blocked.held_sends %}{% for h in blocked.held_sends[:8] %}
    {{ h.value }} x {{ h.reason }}{% endfor %}{% else %}none{% endif %}
{% if blocked.failed_jobs_by_job %}  Failed jobs in the period: {% for j, n in blocked.failed_jobs_by_job.items() %}{{ j }} {{ n }}{{ ', ' if not loop.last }}{% endfor %}
{% endif %}{% if blocked.ai_decisions %}  AI decisions in the period: {{ blocked.ai_decisions.total }}{% if blocked.ai_decisions.escalated_or_blocked %} ({{ blocked.ai_decisions.escalated_or_blocked }} escalated or blocked){% endif %}
{% endif %}
This is an internal report for {{ company }}'s sales team. It is not a marketing email.
"""

CELL = "padding:6px 10px;border-bottom:1px solid #e6e9f0;font-size:14px"
HTML_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} {{ period.label }}</title><style>body{margin:0}</style></head><body>
<div style="font-family:Segoe UI,Arial,sans-serif;color:#1d2433;background:#ffffff;max-width:760px;margin:0 auto;padding:20px">
<h1 style="font-size:22px;margin:0 0 4px">{{ title }}</h1>
<p style="margin:0 0 4px;font-size:15px"><b>{{ period.label }}</b> ({{ period.tz }}) &middot; {{ company }}</p>
<p style="margin:0 0 16px;color:#667085;font-size:13px">Generated {{ generated }}. Every figure is a count of records in the CRM; the definition and the record ids behind each one are stored with the report on the dashboard.</p>
{% if not has_data %}
<p style="padding:12px 14px;background:#f4f6fa;border-radius:8px;font-size:15px"><b>No data</b>: nothing was recorded in this period. No figures, trends or targets are shown for it.</p>
{% else %}{% for heading, rows in sections %}
<h2 style="font-size:16px;margin:22px 0 6px;border-bottom:2px solid #e6e9f0;padding-bottom:4px">{{ heading }}</h2>
<table role="presentation" style="border-collapse:collapse;width:100%">
{% for m in rows %}<tr><td style="{{ cell }}">{{ m.label }}<br><span style="color:#667085;font-size:12px">{{ m.definition }}</span></td><td style="{{ cell }};text-align:right;white-space:nowrap;font-weight:600">{{ shown(m) }}</td></tr>
{% endfor %}</table>{% endfor %}
<h2 style="font-size:16px;margin:22px 0 6px;border-bottom:2px solid #e6e9f0;padding-bottom:4px">Pipeline progress</h2>
<table role="presentation" style="border-collapse:collapse;width:100%">
<tr><td style="{{ cell }}">Stage moves in the period</td><td style="{{ cell }};text-align:right">{% if pipeline.stage_moves %}{% for r in pipeline.stage_moves %}{{ r.stage }} {{ r.value }}{{ ', ' if not loop.last }}{% endfor %}{% else %}none{% endif %}</td></tr>
<tr><td style="{{ cell }}">Won value (entered on the deals)</td><td style="{{ cell }};text-align:right">{{ '${:,.0f}'.format(pipeline.won_value) if pipeline.won_value else 'none' }}</td></tr></table>
{% if agents %}<h2 style="font-size:16px;margin:22px 0 6px;border-bottom:2px solid #e6e9f0;padding-bottom:4px">Agent performance</h2>
<table style="border-collapse:collapse;width:100%"><tr><th style="{{ cell }};text-align:left">Agent</th><th style="{{ cell }};text-align:right">Runs</th><th style="{{ cell }};text-align:right">Succeeded</th><th style="{{ cell }};text-align:right">Failed</th><th style="{{ cell }};text-align:right">Avg s</th></tr>
{% for a in agents %}<tr><td style="{{ cell }}">{{ a.agent }}</td><td style="{{ cell }};text-align:right">{{ a.runs }}</td><td style="{{ cell }};text-align:right">{{ a.success }}</td><td style="{{ cell }};text-align:right">{{ a.failed }}</td><td style="{{ cell }};text-align:right">{{ a.avg_s if a.avg_s is not none else '—' }}</td></tr>
{% endfor %}</table>{% endif %}
{% if countries %}<h2 style="font-size:16px;margin:22px 0 6px;border-bottom:2px solid #e6e9f0;padding-bottom:4px">By country (achieved / target)</h2>
<table style="border-collapse:collapse;width:100%"><tr><th style="{{ cell }};text-align:left">Country</th>{% for key in breakdown_keys %}<th style="{{ cell }};text-align:right">{{ labels[key] }}</th>{% endfor %}</tr>
{% for c in countries %}<tr><td style="{{ cell }}">{{ c.country }}</td>{% for key in breakdown_keys %}{% set f = c.figures[key] %}<td style="{{ cell }};text-align:right">{{ f.value }} / {{ f.target|int if f.target is not none else 'no target set' }}{% if f.attainment is not none %}<br><span style="color:#667085;font-size:12px">{{ f.attainment }}%</span>{% endif %}</td>{% endfor %}</tr>
{% endfor %}</table>{% endif %}
{% if industries %}<h2 style="font-size:16px;margin:22px 0 6px;border-bottom:2px solid #e6e9f0;padding-bottom:4px">By industry</h2>
<table style="border-collapse:collapse;width:100%"><tr><th style="{{ cell }};text-align:left">Industry</th>{% for key in breakdown_keys %}<th style="{{ cell }};text-align:right">{{ labels[key] }}</th>{% endfor %}</tr>
{% for i in industries %}<tr><td style="{{ cell }}">{{ i.label }}</td>{% for key in breakdown_keys %}<td style="{{ cell }};text-align:right">{{ i.figures[key] }}</td>{% endfor %}</tr>
{% endfor %}</table>{% endif %}
{% endif %}
<h2 style="font-size:16px;margin:22px 0 6px;border-bottom:2px solid #e6e9f0;padding-bottom:4px">As things stand now</h2>
<table role="presentation" style="border-collapse:collapse;width:100%">
<tr><td style="{{ cell }}">Deals by stage</td><td style="{{ cell }};text-align:right">{% if pipeline.by_stage_now %}{% for r in pipeline.by_stage_now %}{{ r.stage }} {{ r.value }}{{ ', ' if not loop.last }}{% endfor %}{% else %}none{% endif %}</td></tr>
<tr><td style="{{ cell }}">Automation</td><td style="{{ cell }};text-align:right">{{ blocked.automation_state }} (mode {{ blocked.automation_mode }})</td></tr>
<tr><td style="{{ cell }}">Emails held, by reason</td><td style="{{ cell }};text-align:right">{% if blocked.held_sends %}{% for h in blocked.held_sends[:8] %}{{ h.value }} &times; {{ h.reason }}<br>{% endfor %}{% else %}none{% endif %}</td></tr>
{% if blocked.failed_jobs_by_job %}<tr><td style="{{ cell }}">Failed jobs in the period</td><td style="{{ cell }};text-align:right">{% for j, n in blocked.failed_jobs_by_job.items() %}{{ j }} {{ n }}{{ ', ' if not loop.last }}{% endfor %}</td></tr>{% endif %}
{% if blocked.ai_decisions %}<tr><td style="{{ cell }}">AI decisions in the period</td><td style="{{ cell }};text-align:right">{{ blocked.ai_decisions.total }}{% if blocked.ai_decisions.escalated_or_blocked %} ({{ blocked.ai_decisions.escalated_or_blocked }} escalated or blocked){% endif %}</td></tr>{% endif %}
</table>
<p style="margin:22px 0 0;color:#667085;font-size:12px">This is an internal report for {{ company }}'s sales team. It is not a marketing email.</p>
</div></body></html>"""


def render(data: dict, settings) -> tuple[str, str, str]:
    """(subject, plain text, HTML) for the email and the stored copy. HTML with inline styles only: no remote images,
    no scripts, no tracking of any kind."""
    sections = [(heading, [data["metrics"][k] for k in keys if k in data["metrics"]]) for heading, keys in ORDER]
    company = settings.company_name
    ctx = dict(data, sections=sections, company=company, shown=shown, labels=LABELS, cell=CELL,
               generated=fmt_local(datetime.fromisoformat(data["generated_at"]), data["period"]["tz"]) + f" {data['period']['tz']}")
    plain = Environment(autoescape=False).from_string(TEXT_TEMPLATE).render(**ctx)  # noqa: S701 - plain text, never HTML
    html = Environment(autoescape=select_autoescape(default=True)).from_string(HTML_TEMPLATE).render(**ctx)
    subject = f"{data['title']}: {data['period']['label']} - {company}"
    return " ".join(subject.split())[:200], plain, html


# =============================================================================================== storing

def stored(session: Session, period: Period) -> Report | None:
    return session.execute(select(Report).where(Report.kind == KINDS[period.kind], Report.report_date == period.start)
                           .order_by(Report.id.desc())).scalars().first()


def delivery_for(session: Session, period: Period) -> ReportDelivery | None:
    return session.execute(select(ReportDelivery).where(ReportDelivery.kind == KINDS[period.kind],
                                                        ReportDelivery.period_start == period.start)).scalars().first()


def store(session: Session, settings, period: Period, now: datetime | None = None) -> Report:
    """Write the report's files and its row (one per kind and period). A report already emailed is kept as it was
    sent; otherwise it is written again from the records as they are now."""
    report = stored(session, period)
    if report is not None and delivery_for(session, period) is not None:
        return report
    data = build(session, settings, period, now)
    subject, plain, html = render(data, settings)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{KINDS[period.kind]}_{period.start.isoformat()}"
    txt_path, html_path = settings.reports_dir / f"{stem}.txt", settings.reports_dir / f"{stem}.html"
    txt_path.write_text(plain, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    if report is None:
        report = Report(report_date=period.start, kind=KINDS[period.kind], path_md=str(txt_path), path_html=str(html_path))
        session.add(report)
    previous = (report.summary or {}).get("delivery")
    kpis = {"companies_new_period": data["metrics"]["companies_discovered"]["value"],
            "qualified_period": data["metrics"]["qualified_leads"]["value"]}
    report.summary = {"manager_report": data, "subject": subject, "kpis": kpis, "has_data": data["has_data"],
                      "highlights": highlights(data), "blockers": [h["reason"] for h in data["blocked"]["held_sends"]],
                      "window": {"from": data["period"]["from_utc"], "to": data["period"]["to_utc"]},
                      **({"delivery": previous} if previous else {})}
    report.created_at = now or utcnow()
    session.flush()
    return report


def highlights(data: dict) -> str:
    if not data["has_data"]:
        return "no data"
    m = data["metrics"]
    return " · ".join(f"{m[k]['value']} {words}" for k, words in (
        ("companies_discovered", "companies"), ("verified_decision_makers", "verified decision-makers"),
        ("qualified_leads", "qualified"), ("outreach_sent", "outreach sent"), ("replies", "replies"),
        ("meetings_booked", "meetings")))


def _note_delivery(session: Session, report_id: int, delivery: dict) -> None:
    report = session.get(Report, report_id)
    if report is not None:
        report.summary = {**(report.summary or {}), "delivery": delivery}


# =============================================================================================== sending

def mailbox_sends_since(session: Session, since: datetime) -> int:
    """Report emails that count toward the mailbox's daily limit: every recipient of a delivery claimed since then,
    except one that certainly left nothing, and the handover emails (aime.handover_notify) claimed since then."""
    n = 0
    for results, recipients in session.execute(select(ReportDelivery.results, ReportDelivery.recipients).where(
            ReportDelivery.claimed_at >= since)).all():
        results = results or {}
        n += sum(1 for a in (recipients or []) if (results.get(a) or {}).get("status") != "not_sent")
    from aime.handover_notify import sends_since

    return n + sends_since(session, since)  # the handover emails to the Sales Manager share the mailbox's limit


def blockers(session: Session, settings, cfg: dict, now: datetime) -> list[str]:
    """Every reason the report may not be emailed now; empty when every gate is open."""
    from aime import mail_accounts

    reasons = []
    if not cfg["email_enabled"]:
        reasons.append("emailing the report is switched off (Administration > Manager report)")
    if not cfg["recipients"]:
        reasons.append("no recipient is set")
    ok, why = authorization(cfg)
    if not ok:
        reasons.append(why)
    if not settings.outbound_email_enabled:
        reasons.append("OUTBOUND_EMAIL_ENABLED is off: no email leaves AIME")
    else:
        ready, status = mail_accounts.sending_status(session, settings)
        if not ready:
            reasons.append(f"no sending mailbox is ready ({status})")
    state = am.get_state(session)
    if state in ("stopped", "emergency_stop"):
        reasons.append(f"the automation is {am.STATES[state].split(':')[0].lower()}")
    for addr, why in recipient_problems(session, settings, cfg):
        reasons.append(f"{addr} {why}")
    if cfg["recipients"]:
        limit = int(settings.section("outreach").get("mailbox_daily_limit", 50))
        since = now - timedelta(hours=24)
        used = session.execute(select(func.count(FollowUp.id)).where(
            FollowUp.channel == "email", FollowUp.status.in_(COUNTS_AS_SENT),
            func.coalesce(FollowUp.sent_at, FollowUp.send_started_at) >= since)).scalar_one() + mailbox_sends_since(session, since)
        if used + len(cfg["recipients"]) > limit:
            reasons.append(f"the mailbox's daily limit ({limit} emails in 24 hours) has {max(0, limit - used)} left, "
                           f"fewer than the {len(cfg['recipients'])} recipient(s)")
    return reasons


def _sender(ctx, session: Session, smtp_factory):
    """The sender the follow-up sends use: the primary panel mailbox when there is one, else .env; OAuth tokens fresh."""
    from aime import mail_accounts
    from aime.integrations.email_smtp import EmailSender

    a = mail_accounts.primary(session)
    sender = EmailSender(mail_accounts.sending_settings(session, ctx.settings), smtp_factory=smtp_factory)
    provider = "microsoft" if a is not None and a.auth == "oauth" else (
        ctx.settings.mail_oauth_provider if a is None and ctx.settings.mail_auth == "oauth" else None)
    if provider:
        from aime.integrations import oauth

        def token() -> str:
            with ctx.http() as http:
                return oauth.access_token(session, ctx.settings, http, provider, "mail", **({"account": a.id} if a else {}))
        sender.token = token
    return sender


def _claim(session: Session, period: Period, report_id: int, recipients: list[str], actor: str, now: datetime) -> ReportDelivery | None:
    """The one delivery for this period, committed before any email: None when it exists already (sent, being sent,
    or of unknown outcome). A delivery that certainly left nothing ("failed") is claimed again in one statement, so two
    runs can never both take it."""
    row = ReportDelivery(kind=KINDS[period.kind], period_start=period.start, report_id=report_id, status="sending",
                         recipients=recipients, results={}, claimed_at=now, claimed_by=actor)
    try:  # the session holds nothing else: a refused insert rolls back only itself
        session.add(row)
        session.commit()
        return row
    except IntegrityError:
        session.rollback()
    taken = session.execute(update(ReportDelivery).where(
        ReportDelivery.kind == KINDS[period.kind], ReportDelivery.period_start == period.start,
        ReportDelivery.status == "failed").values(status="sending", claimed_at=now, claimed_by=actor, report_id=report_id,
                                                  recipients=recipients, results={}, error=None, finished_at=None))
    session.commit()
    return delivery_for(session, period) if taken.rowcount == 1 else None


def deliver(ctx, period: Period, report_id: int, now: datetime | None = None, smtp_factory=smtplib.SMTP,
            actor: str = JOB) -> dict:
    """Email this period's stored report when every gate is open, at most once. Returns {"status", "reason", ...}:
    status is not_sent (a gate is closed; the reasons are recorded on the report), already (this period's email was
    handled before), sent, partial, failed (nothing left; the job's retry may try again) or uncertain."""
    from aime.integrations.email_smtp import EmailNotReady, new_message_id

    now = now or utcnow()
    settings = ctx.settings
    with ctx.db.session() as s:
        existing = delivery_for(s, period)
        if existing is not None and existing.status == "sending" and existing.claimed_at < now - STALE_CLAIM:
            existing.status, existing.finished_at = "uncertain", now
            existing.error = "the send was cut off (the PC slept or the job stopped): check the Sent folder; it is not sent again"
            s.add(Alert(level="warning", source=JOB, message=f"Manager report for {period.label}: {existing.error}"[:500]))
        if existing is not None and existing.status != "failed":
            return {"status": "already", "reason": f"this period's report email was handled already ({existing.status})",
                    "delivery_id": existing.id}
        cfg = config(s, settings)
        reasons = blockers(s, settings, cfg, now)
        if reasons:
            _note_delivery(s, report_id, {"sent": False, "checked_at": now.isoformat(timespec="seconds"), "reasons": reasons})
            return {"status": "not_sent", "reason": "; ".join(reasons), "reasons": reasons}
        report = s.get(Report, report_id)
        subject = (report.summary or {}).get("subject") or "Sales Manager Report"
        plain = Path(report.path_md).read_text(encoding="utf-8")
        html = Path(report.path_html).read_text(encoding="utf-8")
        recipients = list(cfg["recipients"])
    with ctx.db.session() as s:
        row = _claim(s, period, report_id, recipients, actor, now)
        if row is None:
            return {"status": "already", "reason": "another run claimed this period's report email"}
        delivery_id = row.id
    results: dict[str, dict] = {}
    with ctx.db.session() as s:
        try:
            sender = _sender(ctx, s, smtp_factory)
        except Exception as exc:  # nothing was handed over: the job's retry may claim it again
            row = s.get(ReportDelivery, delivery_id)
            row.status, row.finished_at, row.error = "failed", utcnow(), f"{type(exc).__name__}: {exc}"[:500]
            return {"status": "failed", "delivery_id": delivery_id, "reason": row.error}
        for addr in recipients:
            mid = new_message_id(sender.settings)
            results[addr] = {"status": "sending", "message_id": mid}
            row = s.get(ReportDelivery, delivery_id)
            row.results = dict(results)
            s.commit()  # the Message-ID is on record before the message leaves
            try:
                sender.send(addr, subject, plain, message_id=mid, html=html)
                results[addr] = {"status": "sent", "message_id": mid, "delivered_to": sender.recipient_for(addr)[0]}
            except (EmailNotReady, smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
                results[addr] = {"status": "not_sent", "message_id": mid, "error": f"{type(exc).__name__}: {exc}"[:300]}
            except Exception as exc:  # noqa: BLE001 - it may have left: never sent again by itself
                results[addr] = {"status": "uncertain", "message_id": mid, "error": f"{type(exc).__name__}: {exc}"[:300]}
        states = {r["status"] for r in results.values()}
        status = ("sent" if states == {"sent"} else "failed" if states == {"not_sent"} else
                  "uncertain" if "uncertain" in states else "partial")
        row = s.get(ReportDelivery, delivery_id)
        row.results, row.status, row.finished_at = dict(results), status, utcnow()
        row.error = "; ".join(f"{a}: {r['error']}" for a, r in results.items() if r.get("error"))[:500] or None
        sent_to = [a for a, r in results.items() if r["status"] == "sent"]
        _note_delivery(s, report_id, {"sent": bool(sent_to), "status": status, "delivery_id": delivery_id,
                                      "sent_to": sent_to, "at": now.isoformat(timespec="seconds"), "error": row.error})
        audit(s, actor, "manager_report_emailed" if sent_to else "manager_report_not_emailed",
              f"{KINDS[period.kind]} {period.start.isoformat()}", f"{status}: " + ", ".join(
                  f"{a} {r['status']}" for a, r in results.items()))
        if status in ("partial", "uncertain"):
            s.add(Alert(level="warning", source=JOB, message=f"Manager report for {period.label}: {status} ({row.error})"[:500]))
    return {"status": status, "delivery_id": delivery_id, "results": results,
            "reason": None if status == "sent" else row.error}


# =============================================================================================== the job

def due_periods(session: Session, cfg: dict, today: date) -> list[Period]:
    """The daily period always; the weekly and monthly ones on their day, or later when their report was never written
    (the PC was off on the day)."""
    out = [latest_period("daily", today, cfg)]
    for kind in ("weekly", "monthly"):
        p = latest_period(kind, today, cfg)
        if today == send_day(p, cfg) or stored(session, p) is None:
            out.append(p)
    return out


def run(ctx, now: datetime | None = None, smtp_factory=smtplib.SMTP, actor: str = JOB, send: bool = True) -> dict:
    """The scheduled job: write and store the due reports, and email each when every gate is open. `send=False` writes
    them only (the Super Admin's "Write now")."""
    now = now or utcnow()
    with ctx.db.session() as s:
        cfg = config(s, ctx.settings)
        periods = due_periods(s, cfg, local_today(cfg, now))
    out = []
    for period in periods:
        with ctx.db.session() as s:
            report = store(s, ctx.settings, period, now)
            report_id = report.id
            has_data = bool((report.summary or {}).get("has_data"))
        delivery = deliver(ctx, period, report_id, now, smtp_factory, actor) if send else {"status": "not_sent", "reason": "written only"}
        out.append({"kind": period.kind, "period": period.label, "report_id": report_id, "has_data": has_data, **{
            k: v for k, v in delivery.items() if k in ("status", "reason", "delivery_id")}})
        log.info("Manager report %s %s: %s", period.kind, period.label, delivery.get("status"))
    return {"reports": out, "failed": any(r["status"] in ("failed", "uncertain", "partial") for r in out)}
