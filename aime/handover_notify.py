"""The internal email that tells a deal's owner a handover package waits for them (Option B, Phase 6).

The dashboard alert and the desktop toast (aime.handover) stay as they were; this email is the way to reach the Sales
Manager away from the PC. It is **off by default** and uses the same kind of authorization as the Sales Manager's report
(aime.manager_report): a Super Admin sets the internal address of each person who may receive it, and authorizes exactly
those addresses and the allow-list (recorded, audited; changing them withdraws the authorization). An email goes only when,
at the moment of sending, every one of these holds:
  1. `email_enabled` is on;
  2. the deal has an owner, and an internal address is set for that owner;
  3. a Super Admin's authorization covers the addresses as they are now;
  4. OUTBOUND_EMAIL_ENABLED=true, and a sending mailbox is ready;
  5. the automation is not stopped or in emergency stop;
  6. the address is internal (aime.manager_report.recipient_problems: the sending mailbox's company domain or the allow-list),
     is no contact or prospect company in the CRM and is not on the do-not-contact list;
  7. the mailbox's daily limit leaves room (outreach, the report and these emails share it).
**At most one email per package version**: the version row is claimed (notify_status "sending") and committed before
anything reaches the mail server; a run that finds it claimed sends nothing, and a send cut off midway becomes "uncertain"
for a person to check, never sent again by itself. Only a send that certainly left nothing ("failed") may be tried again.
The email holds the package's own text and a link to the deal: no tracking, no remote content, and never a prospect."""

from __future__ import annotations

import logging
import re
import smtplib
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from aime import automation as am
from aime import manager_report as mr
from aime.automation import COUNTS_AS_SENT, audit
from aime.db import kv_get, kv_set
from aime.models import Alert, FollowUp, HandoverPackage, Opportunity, User
from aime.timeutil import utcnow

log = logging.getLogger("aime.handover_notify")

KV_KEY = "handover.notify"
JOB = "handover_notify"
DEFAULTS: dict = {"email_enabled": False, "addresses": {}, "allowlist": []}
AUTH_KEYS = mr.AUTH_KEYS
STALE_CLAIM = mr.STALE_CLAIM


# =============================================================================================== settings

def _addresses(value) -> dict[str, str]:
    """{username: address}, from a dict or from lines of "username = address"."""
    if isinstance(value, dict):
        items = value.items()
    else:
        items = [tuple(part.strip() for part in line.split("=", 1)) for line in re.split(r"[\r\n;,]+", str(value or ""))
                 if "=" in line]
    return {str(u).strip().lower(): str(a).strip().lower() for u, a in items if str(u).strip() and str(a).strip()}


def config(session: Session) -> dict:
    """What a Super Admin saved over the defaults. Never read from a file: only a person sets or authorizes it."""
    out = dict(DEFAULTS)
    stored = kv_get(session, KV_KEY) or {}
    out.update({k: v for k, v in stored.items() if k in DEFAULTS or k in AUTH_KEYS})
    out["email_enabled"] = bool(out["email_enabled"])
    out["addresses"] = _addresses(out["addresses"])
    out["allowlist"] = mr._listify(out["allowlist"])
    return out


def fingerprint(cfg: dict) -> str:
    return mr.fingerprint([f"{u}={a}" for u, a in cfg["addresses"].items()], cfg["allowlist"])


def authorization(cfg: dict) -> tuple[bool, str]:
    if not cfg.get("authorized_by") or not cfg.get("authorized_at"):
        return False, "no Super Admin has authorized handover notification emails"
    if cfg.get("authorized_for") != fingerprint(cfg):
        return False, "the addresses changed after the authorization, so it no longer applies: authorize them again"
    return True, f"authorized by {cfg['authorized_by']} at {cfg['authorized_at'][:16].replace('T', ' ')} UTC"


def problems(session: Session, settings, cfg: dict, addresses=None) -> list[tuple[str, str]]:
    """(address, why) for each address that may not receive it: the manager report's internal-recipient rules."""
    addrs = list(addresses if addresses is not None else cfg["addresses"].values())
    return mr.recipient_problems(session, settings, {"recipients": addrs, "allowlist": cfg["allowlist"]})


def save(session: Session, settings, actor: str, form: dict) -> list[str]:
    """Store the Super Admin's settings; a change of the addresses or the allow-list withdraws the authorization. Raises
    ValueError with the reason when an address is not valid or may not receive these emails. Audited."""
    cfg = config(session)
    new = dict(cfg)
    if "addresses" in form:
        new["addresses"] = _addresses(form["addresses"])
        users = set(session.execute(select(func.lower(User.username))).scalars())
        bad = [a for a in new["addresses"].values() if not mr.EMAIL.match(a)]
        if bad:
            raise ValueError(f"not an email address: {', '.join(bad)}")
        if users and (unknown := [u for u in new["addresses"] if u not in users]):
            raise ValueError(f"no such user: {', '.join(unknown)}")
        if len(new["addresses"]) > 50:
            raise ValueError("at most 50 addresses")
    if "allowlist" in form:
        new["allowlist"] = mr._listify(form["allowlist"])
        bad = [a for a in new["allowlist"] if not (mr.EMAIL.match(a) or mr.DOMAIN.match(a))]
        if bad:
            raise ValueError(f"the allow-list takes email addresses or domains: {', '.join(bad)}")
        public = [a for a in new["allowlist"] if "@" not in a and a in mr.PUBLIC_MAIL_DOMAINS]
        if public:
            raise ValueError(f"{', '.join(public)} is a public mail service: list the exact addresses instead")
    if "email_enabled" in form:
        new["email_enabled"] = str(form["email_enabled"]).strip().lower() in ("1", "true", "on", "yes")
    if found := problems(session, settings, new):
        raise ValueError("; ".join(f"{a}: {why}" for a, why in found))
    changes = [f"{k}: {cfg[k]!r} -> {new[k]!r}" for k in DEFAULTS if cfg[k] != new[k]]
    if not changes:
        return []
    stored = {k: new[k] for k in DEFAULTS}
    withdrawn = new["addresses"] != cfg["addresses"] or new["allowlist"] != cfg["allowlist"]
    if not withdrawn:
        stored.update({k: cfg[k] for k in AUTH_KEYS if cfg.get(k)})
    kv_set(session, KV_KEY, stored)
    audit(session, actor, "handover_notify_settings", "handover notification email", "; ".join(changes)[:300])
    if withdrawn and cfg.get("authorized_by"):
        audit(session, actor, "handover_notify_authorization_withdrawn", "handover notification email", "the addresses changed")
    return changes


def authorize(session: Session, settings, actor: str) -> str:
    cfg = config(session)
    if not cfg["addresses"]:
        raise ValueError("set at least one address before authorizing")
    if found := problems(session, settings, cfg):
        raise ValueError("; ".join(f"{a}: {why}" for a, why in found))
    stored = dict(kv_get(session, KV_KEY) or {})
    stored.update(authorized_by=actor, authorized_at=utcnow().isoformat(timespec="seconds"), authorized_for=fingerprint(cfg))
    kv_set(session, KV_KEY, stored)
    audit(session, actor, "handover_notify_authorized", "handover notification email",
          ", ".join(f"{u}: {a}" for u, a in cfg["addresses"].items())[:300])
    return f"Authorized handover emails to {', '.join(cfg['addresses'].values())}"


def revoke(session: Session, actor: str) -> None:
    stored = dict(kv_get(session, KV_KEY) or {})
    if any(stored.pop(k, None) for k in AUTH_KEYS):
        kv_set(session, KV_KEY, stored)
        audit(session, actor, "handover_notify_authorization_withdrawn", "handover notification email", "withdrawn by a Super Admin")


# =============================================================================================== the gates

def sends_since(session: Session, since: datetime) -> int:
    """Handover emails that count toward the mailbox's daily limit: every claim since then that may have left."""
    return session.execute(select(func.count(HandoverPackage.id)).where(
        HandoverPackage.notify_claimed_at >= since, HandoverPackage.notify_status.in_(("sending", "sent", "uncertain")))).scalar_one()


def blockers(session: Session, settings, pkg: HandoverPackage, now: datetime, cfg: dict | None = None) -> list[str]:
    """Every reason this package's email may not go now; empty when every gate is open."""
    from aime import mail_accounts

    cfg = cfg or config(session)
    reasons = []
    if not cfg["email_enabled"]:
        reasons.append("handover emails are switched off (Handovers page, Super Admin)")
    opp = session.get(Opportunity, pkg.opportunity_id)
    owner = (opp.owner or "").lower() if opp is not None else ""
    addr = cfg["addresses"].get(owner) if owner else None
    if not owner:
        reasons.append("the deal has no owner")
    elif not addr:
        reasons.append(f"no internal address is set for {owner}")
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
    if addr:
        reasons += [f"{a} {why}" for a, why in problems(session, settings, cfg, [addr])]
        limit = int(settings.section("outreach").get("mailbox_daily_limit", 50))
        since = now - timedelta(hours=24)
        used = session.execute(select(func.count(FollowUp.id)).where(
            FollowUp.channel == "email", FollowUp.status.in_(COUNTS_AS_SENT),
            func.coalesce(FollowUp.sent_at, FollowUp.send_started_at) >= since)).scalar_one() + mr.mailbox_sends_since(session, since)
        if used + 1 > limit:
            reasons.append(f"the mailbox's daily limit ({limit} emails in 24 hours) is used up")
    return reasons


def message(pkg: HandoverPackage, opp: Opportunity) -> tuple[str, str]:
    subject = f"Handover for you: {opp.name} (version {pkg.version})"
    body = (f"{opp.name} is handed over to you. Open the deal on the dashboard to accept it or return it to the AI: "
            f"/opportunities/{opp.id}\n\n{pkg.summary}\n"
            "This is an internal notification from the AI Sales Employee. Nothing has been sent to the prospect.\n")
    return subject, body


# =============================================================================================== sending

def _claim(session: Session, pkg_id: int, addr: str, now: datetime) -> bool:
    taken = session.execute(update(HandoverPackage).where(
        HandoverPackage.id == pkg_id, or_(HandoverPackage.notify_status.is_(None), HandoverPackage.notify_status == "failed"))
        .values(notify_status="sending", notify_claimed_at=now, notify_to=addr, notify_error=None))
    session.commit()  # on record before anything reaches the mail server
    return taken.rowcount == 1


def deliver(ctx, pkg_id: int, now: datetime | None = None, smtp_factory=smtplib.SMTP, actor: str = JOB) -> dict:
    """Email this package version to the deal's owner when every gate is open, at most once. Returns {"status", "reason"}:
    not_sent (a gate is closed), already, sent, failed (certainly nothing left; tried again later) or uncertain."""
    from aime.integrations.email_smtp import EmailNotReady, new_message_id

    now = now or utcnow()
    with ctx.db.session() as s:
        pkg = s.get(HandoverPackage, pkg_id)
        if pkg is None:
            return {"status": "not_sent", "reason": "no such package"}
        if pkg.notify_status == "sending" and pkg.notify_claimed_at and pkg.notify_claimed_at < now - STALE_CLAIM:
            pkg.notify_status = "uncertain"
            pkg.notify_error = "the send was cut off (the PC slept or the job stopped): check the Sent folder; it is not sent again"
            s.add(Alert(level="warning", source="handover", message=f"Handover email for package #{pkg.id}: {pkg.notify_error}"[:500]))
        if pkg.notify_status not in (None, "failed"):
            return {"status": "already", "reason": f"this version's email was handled already ({pkg.notify_status})"}
        if pkg.status != "open":
            return {"status": "not_sent", "reason": f"the package is {pkg.status}"}
        cfg = config(s)
        reasons = blockers(s, ctx.settings, pkg, now, cfg)
        if reasons:
            return {"status": "not_sent", "reason": "; ".join(reasons), "reasons": reasons}
        opp = s.get(Opportunity, pkg.opportunity_id)
        addr = cfg["addresses"][opp.owner.lower()]
        subject, body = message(pkg, opp)
    with ctx.db.session() as s:
        if not _claim(s, pkg_id, addr, now):
            return {"status": "already", "reason": "another run claimed this version's email"}
    with ctx.db.session() as s:
        pkg = s.get(HandoverPackage, pkg_id)
        try:
            sender = mr._sender(ctx, s, smtp_factory)
            mid = new_message_id(sender.settings)
        except Exception as exc:  # nothing was handed over
            pkg.notify_status, pkg.notify_error = "failed", f"{type(exc).__name__}: {exc}"[:500]
            return {"status": "failed", "reason": pkg.notify_error}
        pkg.notify_message_id = mid
        s.commit()  # the Message-ID is on record before the message leaves
        try:
            sender.send(addr, subject, body, message_id=mid)
            pkg.notify_status = "sent"
        except (EmailNotReady, smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            pkg.notify_status, pkg.notify_error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        except Exception as exc:  # noqa: BLE001 - it may have left: never sent again by itself
            pkg.notify_status, pkg.notify_error = "uncertain", f"{type(exc).__name__}: {exc}"[:500]
            s.add(Alert(level="warning", source="handover", message=f"Handover email for package #{pkg.id}: uncertain ({pkg.notify_error})"[:500]))
        audit(s, actor, "handover_emailed" if pkg.notify_status == "sent" else "handover_not_emailed",
              f"opportunity #{pkg.opportunity_id}", f"version {pkg.version} to {addr}: {pkg.notify_status}"
              + (f" ({pkg.notify_error})" if pkg.notify_error else ""))
        return {"status": pkg.notify_status, "reason": pkg.notify_error}


def run(ctx, now: datetime | None = None, smtp_factory=smtplib.SMTP, actor: str = JOB) -> dict:
    """Email every open package version not emailed yet. Switched off (the default): nothing is looked at."""
    now = now or utcnow()
    with ctx.db.session() as s:
        if not config(s)["email_enabled"]:
            return {"status": "off", "results": []}
        ids = s.execute(select(HandoverPackage.id).where(
            HandoverPackage.status == "open",
            or_(HandoverPackage.notify_status.is_(None), HandoverPackage.notify_status.in_(("failed", "sending"))))
            .order_by(HandoverPackage.id)).scalars().all()
    results = [{"package_id": i, **deliver(ctx, i, now, smtp_factory, actor)} for i in ids]
    return {"status": "done", "results": results,
            "failed": any(r["status"] in ("failed", "uncertain") for r in results)}
