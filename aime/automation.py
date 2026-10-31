"""Operating modes, campaign authorizations and the stop switches behind the Automation Control Center.

Modes (who may let an external email go out):
- approval: every external email needs a person's approval.
- hybrid (the default): research, CRM and drafting run on their own; routine outreach emails (the sequence steps)
  to a recipient named in an active campaign authorization go out without a per-email approval. Replies, pricing,
  discounts, contracts and other commitments, uncertain classifications and anything outside an authorization
  still wait for a person.
- automatic: as hybrid, and booking-link answers to clearly interested replies (high confidence, no pricing
  question) to a recipient named in an authorization that covers replies go out too.

In every mode nothing goes out unless, at the moment of sending: OUTBOUND_EMAIL_ENABLED=true;
REQUIRE_HUMAN_APPROVAL=false (true keeps every email with a person, whatever the mode); the automation state is
running; and an administrator has activated a campaign authorization that names the exact address, within its
dates and limits. A campaign approves an email only at send time, inside the send lock, so switching sending on
never releases a backlog of drafts a campaign approved earlier; the authorization is checked again before every
send, so pausing or stopping it takes effect at once. The send loop's own checks (suppression, consent, sequence
stops, compliance footer, quality checks, daily and per-domain limits, the business-hours window) still apply.

States: running; paused (nothing is sent; research and drafting go on); stopped (scheduled and manual jobs wait
too, except health monitoring and database housekeeping); emergency_stop (as stopped, and every active campaign
authorization is stopped and must be activated again). Starting and resuming need an administrator; anyone signed
in may pause or stop.

Approvals a person gave while sending was off (for example drafts approved weeks earlier and never sent) do not
go out the moment sending is switched on: each waits for "Confirm again" on Follow-ups.

Every change, and every email a campaign approved, is written to the audit log (AuditEvent).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aime.crm_ops import is_suppressed
from aime.db import kv_get, kv_set
from aime.models import AuditEvent, CampaignAuthorization, FollowUp, InboundEmail
from aime.timeutil import utcnow

MODES = {
    "approval": "Every external email needs a person's approval.",
    "hybrid": "Routine outreach that an active campaign authorization covers (by address or by rule) goes out on its own; "
              "replies, pricing, commitments and anything uncertain need a person.",
    "automatic": "As Hybrid, plus booking-link answers to clearly interested replies from authorized recipients.",
}
DEFAULT_MODE = "hybrid"
STATES = {"running": "Running", "paused": "Paused: nothing is sent", "stopped": "Stopped: jobs wait",
          "emergency_stop": "Emergency stop: jobs wait, campaign authorizations stopped"}
# action: (states it may start from, resulting state)
TRANSITIONS = {"start": (("stopped", "emergency_stop"), "running"), "resume": (("paused",), "running"),
               "pause": (("running",), "paused"), "stop": (("running", "paused"), "stopped"),
               "emergency_stop": (("running", "paused", "stopped"), "emergency_stop")}
ADMIN_ACTIONS = ("start", "resume")
ALWAYS_RUN_JOBS = ("monitor", "db_maintenance")  # health and housekeeping keep running while stopped
KINDS = {"outreach": "Outreach sequence emails", "response": "Booking-link replies to interested prospects (Automatic mode)",
         "inquiry": "Replies and follow-ups to people who asked us to contact them (inquiry and LinkedIn forms)"}
MAX_DAYS, MAX_RECIPIENTS, MAX_DAILY, MAX_TOTAL = 30, 50, 50, 500
EMAIL = re.compile(r"^[a-z0-9.!#$&'+/=?^_`{|}~-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")  # no * or %: no wildcards
# Wording that always goes to a person, whatever the mode: prices, discounts and commitments.
SENSITIVE = re.compile(r"\b(pric(e|es|ed|ing)|discount\w*|quot(e|es|ation)|contract\w*|agreement|terms and conditions|"
                       r"invoice\w*|refund\w*|guarantee\w*|warrant(y|ies)|legal|lawyer|nda|payment\w*|free trial|"
                       r"\d+\s?% off)\b", re.I)
COUNTS_AS_SENT = ("sent", "sending", "uncertain")
EPOCH = "1970-01-01T00:00:00"


def audit(session: Session, actor: str | None, action: str, target: str | None = None, detail: str | None = None) -> None:
    session.add(AuditEvent(at=utcnow(), actor=(actor or "system")[:80], action=action[:60],
                           target=target[:120] if target else None, detail=detail))


def get_mode(session: Session) -> str:
    mode = kv_get(session, "automation.mode")
    return mode if mode in MODES else DEFAULT_MODE


def get_state(session: Session) -> str:
    state = kv_get(session, "automation.state")
    return state if state in STATES else "running"


def set_mode(session: Session, mode: str, actor: str) -> str:
    if mode not in MODES:
        raise ValueError(f"Unknown mode {mode!r}")
    old = get_mode(session)
    if old != mode:
        kv_set(session, "automation.mode", mode)
        audit(session, actor, "mode_changed", mode, f"{old} -> {mode}")
    return mode


def change_state(session: Session, action: str, actor: str, reason: str = "") -> str:
    if action not in TRANSITIONS:
        raise ValueError(f"Unknown action {action!r}")
    allowed, new = TRANSITIONS[action]
    old = get_state(session)
    if old not in allowed:
        raise ValueError(f"Cannot {action.replace('_', ' ')} while {STATES[old].split(':')[0].lower()}")
    kv_set(session, "automation.state", new)
    audit(session, actor, f"state_{action}", new, f"{old} -> {new}" + (f": {reason}" if reason else ""))
    if action == "emergency_stop":
        for a in session.execute(select(CampaignAuthorization).where(CampaignAuthorization.status == "active")).scalars():
            a.status = "stopped"
            audit(session, actor, "authorization_stopped", f"#{a.id} {a.name}", "emergency stop")
    return new


# --------------------------------------------------------------------------- campaign authorizations

def parse_recipients(text: str) -> tuple[list[str], list[str]]:
    """Exact addresses, separated by new lines, commas or spaces: (valid, rejected). Wildcards and domains are
    rejected: an authorization names people, not whole companies."""
    valid: list[str] = []
    rejected: list[str] = []
    for item in re.split(r"[\s,;]+", text or ""):
        item = item.strip().lower()
        if not item or item in valid:
            continue
        (valid if EMAIL.match(item) else rejected).append(item)
    return valid, rejected


GRADE_RANK = {"A": 4, "B": 3, "C": 2, "D": 1}
# Answers about a meeting that is already booked: they may go out under a campaign in Automatic mode.
MEETING_NOTES = ("meeting_confirmation", "meeting_move", "meeting_cancel", "meeting_reminder")


def autonomous_authorization(session: Session, settings, lead, contact, to: str,
                             now: datetime | None = None) -> CampaignAuthorization | None:
    """The active campaign that lets the AI employee act for this prospect on its own (answer and book a meeting):
    Automatic mode, running, REQUIRE_HUMAN_APPROVAL=false, and an authorization covering answers ("response") to
    this exact address or by a rule this lead meets. None: a person decides."""
    now = now or utcnow()
    if settings.require_human_approval or get_mode(session) != "automatic" or get_state(session) != "running":
        return None
    to = (to or "").strip().lower()
    for a in active_authorizations(session, now):
        if "response" not in (a.kinds or []):
            continue
        if a.rule:
            if lead_rule_problem(session, a.rule, lead, contact, to) is None:
                return a
        elif to in (a.recipients or []):
            return a
    return None
ADDRESS_RULES = {"published": "a decision-maker's own address that the company publishes",
                 "role_or_published": "the company's published mailbox (info@, hello@ ...) or a decision-maker's own",
                 "asked_us": "the address of a person who asked us to contact them (inquiry forms, LinkedIn lead "
                             "forms and event registrations)"}


def describe_rule(rule: dict) -> str:
    return (f"qualified leads graded {rule['min_grade']} or better, emailed at {ADDRESS_RULES[rule['addresses']]}"
            + (", on a verified company website" if rule.get("verified_website") else ""))


def email_kind(f: FollowUp) -> str:
    """outreach | response | inquiry (a reply or follow-up to a person who asked us to contact them: no kind set)."""
    return f.kind or "inquiry"


def rule_problem(session: Session, rule: dict, f: FollowUp, to: str) -> str | None:
    """Why the email falls outside a rule-based authorization (None: the rule covers it). Outreach and its sequence
    to a qualified, researched prospect, and (when the campaign covers answers) answers to that same prospect. Only an
    "asked_us" rule covers replies to people who asked us to contact them; it covers no cold outreach."""
    kind = email_kind(f)
    if rule.get("addresses") == "asked_us":
        if kind not in ("inquiry", "response"):
            return "this rule covers replies to people who asked us to contact them, not outreach"
    elif kind not in ("outreach", "response"):
        return "a rule covers outreach and answers to its own prospects only"
    return lead_rule_problem(session, rule, f.lead, f.contact, to)


def lead_rule_problem(session: Session, rule: dict, lead, contact, to: str) -> str | None:
    """The lead, company and address conditions of a rule (see rule_problem)."""
    if lead is None or lead.status != "qualified":
        return "the lead is not qualified"
    if GRADE_RANK.get(lead.grade or "", 0) < GRADE_RANK[rule["min_grade"]]:
        return f"the lead is graded {lead.grade or '-'}, below {rule['min_grade']}"
    company = lead.company
    if company is None or company.is_sample or (company.is_competitor and not company.competitor_cleared_by):
        return "the company is a sample or a flagged competitor"
    if rule.get("verified_website") and company.website_status != "verified":
        return "the company's website is not verified"
    if contact is None or (contact.email or "").strip().lower() != to:
        return "the address is not the lead's researched contact"
    if rule["addresses"] == "asked_us":
        if contact.source != "inbound" or contact.consent_status not in ("inquiry", "opt_in"):
            return "the person did not ask us to contact them"
        return None
    if contact.source not in ("website", "sec_filing"):
        return "the address was not published by the company"
    allowed = ("published",) if rule["addresses"] == "published" else ("published", "role_address")
    if contact.email_status not in allowed:
        return "the address is not one the rule allows"
    return None


def create_rule_authorization(session: Session, actor: str, name: str, min_grade: str, addresses: str,
                              verified_website: bool, daily_limit: int, total_limit: int, days: int,
                              notes: str = "", answer_replies: bool = False) -> CampaignAuthorization:
    """A draft rule-based authorization for outreach: qualified leads that meet the rule, within the limits. It lets
    nothing out until an administrator activates it, and every send-time gate still applies."""
    if not name.strip():
        raise ValueError("Give the campaign a name")
    if min_grade not in ("A", "B"):
        raise ValueError("A rule covers grade A, or grade B and better")
    if addresses not in ADDRESS_RULES:
        raise ValueError("Choose which addresses the rule covers")
    if not (1 <= daily_limit <= MAX_DAILY and 1 <= total_limit <= MAX_TOTAL and 1 <= days <= MAX_DAYS):
        raise ValueError(f"Limits: 1-{MAX_DAILY} a day, 1-{MAX_TOTAL} in total, valid for 1-{MAX_DAYS} days")
    now = utcnow()
    rule = {"min_grade": min_grade, "addresses": addresses, "verified_website": bool(verified_website)}
    first = "inquiry" if addresses == "asked_us" else "outreach"
    kinds = [first, "response"] if answer_replies else [first]
    a = CampaignAuthorization(name=name.strip()[:200], status="draft", recipients=[], rule=rule, kinds=kinds,
                              daily_limit=daily_limit, total_limit=total_limit, starts_at=now,
                              expires_at=now + timedelta(days=days), notes=(notes or "").strip()[:2000] or None,
                              created_by=actor)
    session.add(a)
    session.flush()
    audit(session, actor, "authorization_created", f"#{a.id} {a.name}",
          f"rule: {describe_rule(rule)}; covers {' and '.join(kinds)}"
          + ("" if not answer_replies else " (in Automatic mode: meeting offers, booking and meeting notes)")
          + f"; {daily_limit} a day, {total_limit} in total; "
          f"until {a.expires_at:%Y-%m-%d %H:%M} UTC")
    return a


def create_authorization(session: Session, actor: str, name: str, recipients: str, kinds: list[str],
                         daily_limit: int, total_limit: int, days: int, notes: str = "") -> CampaignAuthorization:
    """A draft authorization; it lets nothing out until an administrator activates it."""
    valid, rejected = parse_recipients(recipients)
    if not name.strip():
        raise ValueError("Give the campaign a name")
    if rejected:
        raise ValueError(f"Not an exact email address: {', '.join(rejected[:5])}")
    if not valid:
        raise ValueError("Name at least one exact recipient address")
    if len(valid) > MAX_RECIPIENTS:
        raise ValueError(f"At most {MAX_RECIPIENTS} recipients per authorization")
    kinds = [k for k in KINDS if k in (kinds or [])]
    if not kinds:
        raise ValueError("Choose which emails it covers")
    if not (1 <= daily_limit <= MAX_DAILY and 1 <= total_limit <= MAX_TOTAL and 1 <= days <= MAX_DAYS):
        raise ValueError(f"Limits: 1-{MAX_DAILY} a day, 1-{MAX_TOTAL} in total, valid for 1-{MAX_DAYS} days")
    now = utcnow()
    a = CampaignAuthorization(name=name.strip()[:200], status="draft", recipients=valid, kinds=kinds,
                              daily_limit=daily_limit, total_limit=total_limit, starts_at=now,
                              expires_at=now + timedelta(days=days), notes=(notes or "").strip()[:2000] or None,
                              created_by=actor)
    session.add(a)
    session.flush()
    audit(session, actor, "authorization_created", f"#{a.id} {a.name}",
          f"{len(valid)} recipient(s): {', '.join(valid[:10])}{' ...' if len(valid) > 10 else ''}; covers "
          f"{', '.join(kinds)}; {daily_limit} a day, {total_limit} in total; until {a.expires_at:%Y-%m-%d %H:%M} UTC")
    return a


def change_authorization(session: Session, a: CampaignAuthorization, action: str, actor: str) -> None:
    expire_authorizations(session)
    if action == "activate":
        if a.status not in ("draft", "paused"):
            raise ValueError(f"Only a draft or paused authorization can be activated (this one is {a.status})")
        a.status, a.authorized_by, a.authorized_at = "active", actor, utcnow()
    elif action == "pause":
        if a.status != "active":
            raise ValueError("Only an active authorization can be paused")
        a.status = "paused"
    elif action == "stop":
        if a.status not in ("draft", "active", "paused"):
            raise ValueError(f"It is already {a.status}")
        a.status = "stopped"
    else:
        raise ValueError(f"Unknown action {action!r}")
    audit(session, actor, f"authorization_{action}", f"#{a.id} {a.name}", f"now {a.status}")


def expire_authorizations(session: Session) -> None:
    now = utcnow()
    for a in session.execute(select(CampaignAuthorization).where(
            CampaignAuthorization.status.in_(("draft", "active", "paused")), CampaignAuthorization.expires_at <= now)).scalars():
        a.status = "expired"
        audit(session, "system", "authorization_expired", f"#{a.id} {a.name}", f"ended {a.expires_at:%Y-%m-%d %H:%M} UTC")


def active_authorizations(session: Session, now: datetime | None = None) -> list[CampaignAuthorization]:
    now = now or utcnow()
    return session.execute(select(CampaignAuthorization).where(
        CampaignAuthorization.status == "active", CampaignAuthorization.starts_at <= now,
        CampaignAuthorization.expires_at > now).order_by(CampaignAuthorization.id)).scalars().all()


def sent_counts(session: Session, a: CampaignAuthorization, now: datetime | None = None) -> tuple[int, int]:
    """(in the last 24 hours, in total) emails this authorization approved that went out or may have."""
    now = now or utcnow()
    base = select(func.count(FollowUp.id)).where(FollowUp.authorization_id == a.id, FollowUp.status.in_(COUNTS_AS_SENT))
    day = session.execute(base.where(func.coalesce(FollowUp.sent_at, FollowUp.send_started_at) >= now - timedelta(hours=24))).scalar_one()
    return day, session.execute(base).scalar_one()


def committed_counts(session: Session, a: CampaignAuthorization, now: datetime | None = None) -> tuple[int, int]:
    """(in the last 24 hours, in total) emails this authorization has committed to: sent or possibly sent, plus the
    ones it approved that are still waiting to go out. Limits are checked against these, so approvals that wait
    (outside the send window, or behind the per-run and per-domain limits) can never add up past a limit."""
    day, total = sent_counts(session, a, now)
    waiting = session.execute(select(func.count(FollowUp.id)).where(
        FollowUp.authorization_id == a.id, FollowUp.status == "approved")).scalar_one()
    return day + waiting, total + waiting


# --------------------------------------------------------------------------- send-time decisions

def blockers(session: Session, settings, sender_ready: bool, sender_reason: str = "") -> list[str]:
    """Why no email can go out without a person right now (empty: campaign approvals are possible)."""
    out = []
    if not settings.outbound_email_enabled:
        out.append("OUTBOUND_EMAIL_ENABLED=false: nothing is sent at all")
    elif not sender_ready:
        out.append(f"Mail server not ready: {sender_reason}")
    if settings.require_human_approval:
        out.append("REQUIRE_HUMAN_APPROVAL=true: every email needs a person, whatever the mode")
    if get_mode(session) == "approval":
        out.append("Approval mode: every email needs a person")
    state = get_state(session)
    if state != "running":
        out.append(f"Automation {STATES[state].split(':')[0].lower()}")
    if not active_authorizations(session):
        out.append("No active campaign authorization")
    return out


def recipient(f: FollowUp) -> str:
    return (f.to_email or (f.contact.email if f.contact else "") or "").strip().lower()


def campaign_for(session: Session, f: FollowUp, mode: str, now: datetime,
                 pending: dict[int, int] | None = None) -> tuple[CampaignAuthorization | None, str]:
    """The authorization that lets `f` go out without a person, or (None, why a person must approve it)."""
    from aime.reply_intel import QUESTION_PLACEHOLDER

    if f.purpose == "reengage":
        return None, "a re-engagement email to a lead who never answered always needs a person's approval"
    if (f.generator or "").startswith("AI"):
        return None, "its opening was written by the local AI model: a person reads it first"
    to = recipient(f)
    active = active_authorizations(session, now)
    camps = [a for a in active if to in (a.recipients or [])]
    rule_misses = []
    for a in active:
        if a.rule:
            miss = rule_problem(session, a.rule, f, to)
            if miss is None:
                camps.append(a)
            else:
                rule_misses.append(f"campaign #{a.id}: {miss}")
    if not camps:
        if rule_misses:
            return None, "no active campaign rule covers it (" + "; ".join(rule_misses[:2]) + ")"
        return None, "the recipient is not on an active campaign authorization"
    kind = email_kind(f)
    camps = [a for a in camps if kind in (a.kinds or [])]
    if not camps:
        return None, f"no active authorization covers {kind} emails to {to}"
    if f.kind == "response":
        if mode != "automatic":
            return None, "replies need a person in Hybrid mode"
        if f.purpose in MEETING_NOTES:
            pass  # about a meeting already booked (by a person or the campaign): no question answered, no commitment
        else:
            row = session.execute(select(InboundEmail).where(InboundEmail.suggested_followup_id == f.id)).scalars().first()
            if row is None or row.category != "interested" or row.confidence != "high" or row.intent == "pricing_request":
                return None, "the reply it answers is not a clear interested reply"
            if f.purpose != "meeting_offer" and "calendly.com/" not in (f.body or "").lower():
                return None, "only booking-link replies, meeting-time offers and meeting notes go out on their own"
    if is_given_test_contact(f.contact):
        return None, "an address given to us for testing always needs a person's approval"
    text = f"{f.subject or ''}\n{f.body or ''}"
    if QUESTION_PLACEHOLDER in text:
        return None, "it needs an answer written by a person"
    sensitive = SENSITIVE.search(text)
    if sensitive:
        return None, f'it mentions "{sensitive.group(0)}": prices, discounts and commitments need a person'
    qc = (((f.quality or {}).get("qc") or {}).get("status"))
    if f.kind == "outreach" and qc in ("held", "failed"):
        return None, f"its quality checks {qc}"
    if (f.contact and f.contact.consent_status == "opted_out") or is_suppressed(session, to):
        return None, "the recipient opted out or is suppressed"
    for a in camps:
        day, total = committed_counts(session, a, now)
        if f.status == "approved" and f.authorization_id == a.id:  # its own approval is already counted
            day, total = day - 1, total - 1
        extra = (pending or {}).get(a.id, 0)
        if day + extra < a.daily_limit and total + extra < a.total_limit:
            return a, ""
    return None, "the campaign's daily or total limit is reached"


def apply_campaign_approvals(session: Session, settings, now: datetime, actor: str) -> list[int]:
    """Approve, at send time, the waiting emails an active campaign authorization covers. Call it inside the send
    lock, and only when an email can actually go out now."""
    mode = get_mode(session)
    if mode == "approval" or settings.require_human_approval or not settings.outbound_email_enabled \
            or get_state(session) != "running":
        return []
    expire_authorizations(session)
    if not active_authorizations(session, now):
        return []
    waiting = session.execute(select(FollowUp).where(
        FollowUp.channel == "email", FollowUp.status == "awaiting_approval",
        or_(FollowUp.kind.in_(("outreach", "response")), FollowUp.kind.is_(None)),
        FollowUp.due_at <= now).order_by(FollowUp.due_at, FollowUp.id)).scalars().all()
    pending: dict[int, int] = {}
    approved = []
    for f in waiting:
        a, _why = campaign_for(session, f, mode, now, pending)
        if a is None:
            continue
        f.status, f.approved_at, f.approved_by, f.authorization_id = "approved", now, f"campaign #{a.id}", a.id
        f.blocked_reason = None
        pending[a.id] = pending.get(a.id, 0) + 1
        audit(session, actor, "auto_approved", f"follow-up #{f.id}",
              f"{email_kind(f)} email to {recipient(f)} approved by campaign #{a.id} {a.name} "
              f"(authorized by {a.authorized_by}; {mode} mode)")
        approved.append(f.id)
    return approved


def campaign_approval_problem(session: Session, f: FollowUp, now: datetime, settings=None) -> str | None:
    """For an email a campaign approved: why that approval no longer holds (None: it does). Checked just before it
    is sent: the authorization, its rule or recipients, its limits, the automation state and mode, and
    REQUIRE_HUMAN_APPROVAL (switched back on, it takes back every approval a campaign gave)."""
    if not f.authorization_id:
        return None
    a = session.get(CampaignAuthorization, f.authorization_id)
    if a is None or a.status != "active" or not (a.starts_at <= now < a.expires_at):
        return "its campaign authorization is no longer active"
    if a.rule:
        miss = rule_problem(session, a.rule, f, recipient(f))
        if miss:
            return f"its campaign rule no longer covers it: {miss}"
    elif recipient(f) not in (a.recipients or []):
        return "the recipient is no longer on its campaign authorization"
    if (f.approved_by or "").startswith("campaign") and (get_state(session) != "running" or get_mode(session) == "approval"):
        return "automation is not running in Hybrid or Automatic mode"
    if (f.approved_by or "").startswith("campaign"):
        if settings is not None and settings.require_human_approval:
            return "REQUIRE_HUMAN_APPROVAL=true: every email needs a person"
        if (f.generator or "").startswith("AI"):
            return "its opening was written by the local AI model: a person reads it first"
        day, total = sent_counts(session, a, now)
        if day >= a.daily_limit or total >= a.total_limit:
            return "its campaign's daily or total limit is reached"
    return None


def return_to_person(f: FollowUp, why: str) -> None:
    f.status, f.approved_at, f.approved_by, f.authorization_id = "awaiting_approval", None, None, None
    f.blocked_reason = f"Needs a person's approval: {why}"[:300]


# --------------------------------------------------------------------------- test contacts given by the owner

GIVEN = "user_provided"  # Contact.email_status of an address the owner gave for testing (never "published")


def is_given_test_contact(contact) -> bool:
    """An address the owner gave us for controlled testing rather than one the company published."""
    return bool(contact is not None and contact.email_status == GIVEN)


def test_contact_authorization(session: Session, f: FollowUp, now: datetime) -> tuple[CampaignAuthorization | None, str]:
    """The only way an outreach email to a given test address goes out: a person approved it (seeing its quality
    findings, which stay on the email) and an active campaign authorization covering outreach names the exact
    address, under its limits. Returns (authorization, "") or (None, why not). Opt-outs, suppression and stopped
    sequences still stop it; the send loop's other checks (limits, window, footer) still apply."""
    from aime.outreach import STOP_STATUSES

    to = recipient(f)
    if not f.approved_at or not f.approved_by or f.approved_by.startswith("campaign"):
        return None, "a person must approve it"
    if (f.contact and f.contact.consent_status == "opted_out") or is_suppressed(session, to):
        return None, "the address opted out or is suppressed"
    if f.lead is not None and f.lead.outreach_status in STOP_STATUSES:
        stage = f.lead.outreach_status.replace("_", " ")
        return None, "the sequence was stopped" if stage == "stopped" else f"the sequence stopped ({stage})"
    camps = [a for a in active_authorizations(session, now) if to in (a.recipients or []) and "outreach" in (a.kinds or [])]
    if not camps:
        return None, "no active campaign authorization names this exact address"
    for a in camps:
        day, total = sent_counts(session, a, now)
        if day < a.daily_limit and total < a.total_limit:
            return a, ""
    return None, "the campaign's daily or total limit is reached"


# --------------------------------------------------------------------------- approvals given while sending was off

def observe_sending(session: Session, enabled: bool, now: datetime) -> None:
    """Remember when sending was last switched on (the first observation counts as "always on")."""
    state = kv_get(session, "outbound.state")
    if not isinstance(state, dict):
        kv_set(session, "outbound.state", {"enabled": enabled, "since": EPOCH if enabled else now.isoformat()})
    elif bool(state.get("enabled")) != enabled:
        kv_set(session, "outbound.state", {"enabled": enabled, "since": now.isoformat()})


def sending_enabled_since(session: Session) -> datetime | None:
    state = kv_get(session, "outbound.state")
    if not isinstance(state, dict) or not state.get("enabled"):
        return None
    return datetime.fromisoformat(state["since"])


def needs_reconfirmation(session: Session, f: FollowUp) -> bool:
    """A person approved it before sending was switched on: it waits for "Confirm again" instead of going out."""
    since = sending_enabled_since(session)
    return bool(since and f.status == "approved" and not f.authorization_id and f.approved_at and f.approved_at < since)


def reconfirm(session: Session, f: FollowUp, actor: str) -> None:
    f.approved_at, f.approved_by, f.blocked_reason = utcnow(), actor, None
    audit(session, actor, "approval_confirmed_again", f"follow-up #{f.id}", f"{f.kind or 'email'} to {recipient(f)}")
