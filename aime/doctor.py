"""System self-check: database integrity, data isolation, compliance invariants, scheduler freshness."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select

from aime.backup import integrity_check, offsite_status
from aime.context import AppContext
from aime.db import kv_get
from aime.integrations.linkedin import configured_author_urn
from aime.models import (Company, Contact, ContentItem, FollowUp, InboundInquiry, Lead, OAuthToken,
                         PublishAttempt, User)
from aime.monitoring import read_heartbeat
from aime.secure_store import is_protected
from aime.timeutil import utcnow

BACKUP_MAX_AGE_HOURS = 30  # the backup job runs daily; allow for a late or retried run


def run_checks(ctx: AppContext) -> list[dict]:
    """Each check: {name, ok, detail, severity}. severity: error | warning."""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, severity: str = "error") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "severity": severity})

    ic = integrity_check(ctx.db)
    add("SQLite integrity", ic["ok"], f"integrity={ic['integrity']}, foreign-key violations={ic['foreign_key_violations']}")

    with ctx.db.session() as s:
        n = lambda m, *w: s.execute(select(func.count(m.id)).where(*w)).scalar_one()  # noqa: E731
        role = kv_get(s, "db_role")
        add("Database role matches settings", role == ctx.settings.data_role,
            f"database={role}, settings={ctx.settings.data_role}")
        if ctx.settings.is_production:
            samples = (n(Company, Company.is_sample) + n(Lead, Lead.is_sample) + n(Contact, Contact.is_sample)
                       + n(InboundInquiry, InboundInquiry.is_sample))
            add("No sample data in production", samples == 0, f"{samples} sample record(s)")
        bad_pub = n(ContentItem, ContentItem.status == "published",
                    or_(ContentItem.external_id.is_(None), ContentItem.external_id == ""))
        add("Every published post has a LinkedIn id", bad_pub == 0, f"{bad_pub} post(s) without an id")
        unapproved = n(ContentItem, ContentItem.status.in_(("published", "publishing", "uncertain")),
                       ContentItem.approved_at.is_(None))
        add("Nothing published without approval", unapproved == 0, f"{unapproved} post(s) without an approval record")
        unapproved_mail = n(FollowUp, FollowUp.channel == "email",
                            FollowUp.status.in_(("sending", "sent", "uncertain")), FollowUp.approved_at.is_(None))
        add("No email sent without approval", unapproved_mail == 0,
            f"{unapproved_mail} email(s) without an approval record")
        add("Human approval setting", ctx.settings.require_human_approval,
            "Every email and post needs an approval record" if ctx.settings.require_human_approval
            else "REQUIRE_HUMAN_APPROVAL=false is set but ignored; approval is still required", "warning")
        multi = s.execute(select(PublishAttempt.content_id).where(PublishAttempt.outcome.in_(("success", "reconciled")))
                          .group_by(PublishAttempt.content_id).having(func.count() > 1)).all()
        add("No post published twice", not multi, f"{len(multi)} post(s) with more than one successful attempt")
        uncertain = n(ContentItem, ContentItem.status == "uncertain") + n(FollowUp, FollowUp.status == "uncertain")
        add("No uncertain publications/sends awaiting review", uncertain == 0,
            f"{uncertain} item(s) need a person to confirm or requeue", "warning")
        stuck = n(ContentItem, ContentItem.status == "publishing",
                  ContentItem.publish_started_at < utcnow() - timedelta(minutes=15))
        add("No stuck publishing claims", stuck == 0, f"{stuck} claim(s) older than 15 minutes")
        no_consent = s.execute(select(func.count(FollowUp.id)).join(Contact, Contact.id == FollowUp.contact_id).where(
            FollowUp.status == "sent", Contact.consent_status.not_in(("inquiry", "opt_in", "opted_out")))).scalar_one()
        add("Emails only to consenting contacts", no_consent == 0, f"{no_consent} sent without consent on record")
        research_contacts = n(Contact, Contact.source.not_in(("inbound", "manual", "import")))
        add("No harvested contact data", research_contacts == 0, f"{research_contacts} contact(s) from public sources")
        dup_domains = s.execute(select(Company.domain).where(Company.merged_into_id.is_(None), Company.domain.is_not(None))
                                .group_by(Company.domain).having(func.count() > 1)).all()
        add("No duplicate company domains", not dup_domains, f"{len(dup_domains)} duplicated domain(s)", "warning")
        plain_tokens = [t.provider for t in s.execute(select(OAuthToken)).scalars() if not is_protected(t.access_token)]
        add("OAuth tokens encrypted at rest", not plain_tokens, f"unencrypted: {', '.join(plain_tokens) or 'none'}")
    _, author_ok = configured_author_urn(ctx.settings)
    add("LinkedIn author setting valid", author_ok,
        "LINKEDIN_AUTHOR_URN is empty or a valid URN" if author_ok
        else "LINKEDIN_AUTHOR_URN is not a valid LinkedIn URN (value hidden); fix or clear it in .env")

    with ctx.db.session() as s:
        last, off = kv_get(s, "backup.last_verified"), kv_get(s, "backup.last_offsite")
        users = s.execute(select(func.count(User.id)).where(User.active.is_(True))).scalar_one()
    if not ctx.settings.dashboard_auth:
        add("Dashboard sign-in", False, "DASHBOARD_AUTH=false: anyone using this computer can open the dashboard",
            "warning")
    else:
        add("Dashboard sign-in", users > 0, f"{users} active account(s)" if users
            else "No account yet: run `python -m aime user create --username <name>`", "warning")
    if last is None:
        add("Recent verified backup", False, "No verified backup recorded yet (the daily db_maintenance job records one)",
            "warning")
    else:
        hours = (utcnow() - datetime.fromisoformat(last["at"])).total_seconds() / 3600
        add("Recent verified backup", hours <= BACKUP_MAX_AGE_HOURS,
            f"{last['file']}: {last['rows']} rows, verified {hours:.0f} h ago")
    off_ready, off_reason = offsite_status(ctx.settings)
    if not off_ready:
        add("Off-machine encrypted backup", False, f"Off-machine backup {off_reason}", "warning")
    elif off is None:
        add("Off-machine encrypted backup", False, "Configured; no copy written yet", "warning")
    else:
        hours = (utcnow() - datetime.fromisoformat(off["at"])).total_seconds() / 3600
        add("Off-machine encrypted backup", hours <= BACKUP_MAX_AGE_HOURS, f"{off['file']}, {hours:.0f} h ago")

    hb = read_heartbeat(ctx.settings)
    if hb is None:
        add("Scheduler heartbeat", False, "No tick recorded yet", "warning")
    else:
        at = datetime.fromisoformat(hb["at"].rstrip("Z"))
        age = utcnow() - at
        limit = timedelta(minutes=int(ctx.settings.schedule.get("tick_minutes", 15)) * 2 + 5)
        state = hb.get("state")
        add("Scheduler heartbeat fresh", age <= limit or state == "maintenance",
            f"last tick {int(age.total_seconds() // 60)} min ago, state={state}", "warning")
    return checks
