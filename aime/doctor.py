"""System self-check: database integrity, data isolation, compliance invariants, scheduler freshness."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import DatabaseError

from aime import maintenance
from aime.backup import integrity_check, offsite_status, offsite_sync
from aime.context import AppContext
from aime.db import kv_get
from aime.integrations.linkedin import configured_author_urn
from aime.models import (Company, Contact, ContentItem, FollowUp, InboundInquiry, Lead, OAuthToken,
                         PublishAttempt, User)
from aime.monitoring import read_heartbeat
from aime.redact import redact
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
    try:
        _record_checks(ctx, add)
        _setup_checks(ctx, add)
    except DatabaseError as exc:  # a damaged file: say so, and still report what needs no database
        add("Database readable", False, f"Records cannot be read, so their checks did not run: {redact(str(exc.orig))[:200]}")

    _, author_ok = configured_author_urn(ctx.settings)
    add("LinkedIn author setting valid", author_ok,
        "LINKEDIN_AUTHOR_URN is empty or a valid URN" if author_ok
        else "LINKEDIN_AUTHOR_URN is not a valid LinkedIn URN (value hidden); fix or clear it in .env")
    mode = str(ctx.settings.section("publish_policy").get("mode", "shadow")).lower()
    add("Auto-publish policy is shadow-only", mode == "shadow",
        "records decisions only; never publishes" if mode == "shadow"
        else f"publish_policy.mode={mode!r} is not supported and is treated as shadow", "warning")

    hb = read_heartbeat(ctx.settings)
    if hb is None:
        add("Scheduler heartbeat", False, "No tick recorded yet", "warning")
    else:
        at = datetime.fromisoformat(hb["at"].rstrip("Z"))
        age = utcnow() - at
        limit = timedelta(minutes=int(ctx.settings.schedule.get("tick_minutes", 15)) * 2 + 5)
        state = hb.get("state")
        # A maintenance beat explains the silence only while its window lasts: after that, ticks have simply stopped.
        paused = state == "maintenance" and maintenance.active(ctx.settings.data_dir) is not None
        add("Scheduler heartbeat fresh", age <= limit or paused,
            f"last tick {int(age.total_seconds() // 60)} min ago, state={state}", "warning")
    return checks


def _record_checks(ctx: AppContext, add) -> None:
    """Data isolation and compliance invariants, read from the records."""
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
            else "REQUIRE_HUMAN_APPROVAL=false: active campaign authorizations may approve emails at send time "
            "(Hybrid/Automatic mode); every email still has an approval record", "warning")
        multi = s.execute(select(PublishAttempt.content_id).where(PublishAttempt.outcome.in_(("success", "reconciled")))
                          .group_by(PublishAttempt.content_id).having(func.count() > 1)).all()
        add("No post published twice", not multi, f"{len(multi)} post(s) with more than one successful attempt")
        uncertain = n(ContentItem, ContentItem.status == "uncertain") + n(FollowUp, FollowUp.status == "uncertain")
        add("No uncertain publications/sends awaiting review", uncertain == 0,
            f"{uncertain} item(s) need a person to confirm or requeue", "warning")
        stuck = n(ContentItem, ContentItem.status == "publishing",
                  ContentItem.publish_started_at < utcnow() - timedelta(minutes=15))
        add("No stuck publishing claims", stuck == 0, f"{stuck} claim(s) older than 15 minutes")
        sent_to = lambda *w: s.execute(select(func.count(FollowUp.id)).join(  # noqa: E731
            Contact, Contact.id == FollowUp.contact_id).where(FollowUp.status == "sent", *w)).scalar_one()
        # Replies to people who contacted us need their inquiry or opt-in. A response answers an email the
        # prospect sent us (kind "response"), so it must name that email. An opt-out forbids what is sent after it.
        no_consent = sent_to(FollowUp.kind.is_(None), Contact.consent_status.not_in(("inquiry", "opt_in", "opted_out")))
        unanswered = n(FollowUp, FollowUp.status == "sent", FollowUp.kind == "response", FollowUp.in_reply_to.is_(None))
        after_opt_out = sent_to(Contact.consent_status == "opted_out",
                                Contact.consent_at.is_(None) | (FollowUp.sent_at >= Contact.consent_at))
        bad_outreach = sent_to(FollowUp.kind == "outreach",
                               Contact.email_source_url.is_(None) | FollowUp.approved_at.is_(None))
        add("Emails only to consenting contacts or approved B2B outreach",
            no_consent == 0 and unanswered == 0 and after_opt_out == 0 and bad_outreach == 0,
            f"{no_consent} replies to inquiries without consent; {unanswered} responses that answer no received "
            f"email; {after_opt_out} emails sent after an opt-out; {bad_outreach} outreach emails without a published "
            "address or an approval record")
        other_sources = n(Contact, Contact.source.not_in(("inbound", "manual", "import", "website", "sec_filing",
                                                          "website_withdrawn")))
        add("Contacts only from approved sources", other_sources == 0,
            f"{other_sources} contact(s) from a source other than inquiries, manual entry, your imports, "
            "companies' own websites or SEC filings")
        researched = (Contact.source.in_(("website", "sec_filing")),)
        no_evidence = n(Contact, *researched, (Contact.evidence_url.is_(None)) | (Contact.evidence.is_(None)))
        unsourced_email = n(Contact, *researched, Contact.email.is_not(None), Contact.email_source_url.is_(None))
        add("Researched contacts have evidence", no_evidence == 0 and unsourced_email == 0,
            f"{no_evidence} without the page or filing they come from; {unsourced_email} with an email that was "
            "not published by the company")
        dup_domains = s.execute(select(Company.domain).where(Company.merged_into_id.is_(None), Company.domain.is_not(None))
                                .group_by(Company.domain).having(func.count() > 1)).all()
        add("No duplicate company domains", not dup_domains, f"{len(dup_domains)} duplicated domain(s)", "warning")
        plain_tokens = [t.provider for t in s.execute(select(OAuthToken)).scalars() if not is_protected(t.access_token)]
        add("OAuth tokens encrypted at rest", not plain_tokens, f"unencrypted: {', '.join(plain_tokens) or 'none'}")


def _setup_checks(ctx: AppContext, add) -> None:
    """Sign-in accounts, backups and the sales mailbox (their state is kept in the database)."""
    with ctx.db.session() as s:
        last, off = kv_get(s, "backup.last_verified"), kv_get(s, "backup.last_offsite")
        users = s.execute(select(func.count(User.id)).where(User.active.is_(True))).scalar_one()
        admins = s.execute(select(func.count(User.id)).where(User.active.is_(True), User.role.in_(("admin", "super_admin")))).scalar_one()
    if not ctx.settings.dashboard_auth:
        add("Dashboard sign-in", False, "DASHBOARD_AUTH=false: anyone using this computer can open the dashboard",
            "warning")
    else:
        add("Dashboard sign-in", users > 0, f"{users} active account(s)" if users
            else "No account yet: run `python -m aime user create --username <name>`", "warning")
        if users:
            add("Dashboard admin account", admins > 0, f"{admins} active admin(s)" if admins
                else "No admin: running jobs and connecting LinkedIn need one. Run "
                     "`python -m aime user set-role --username <you> --role admin`", "warning")
    if last is None:
        add("Recent verified backup", False, "No verified backup recorded yet (the daily db_maintenance job records one)",
            "warning")
    else:
        hours = (utcnow() - datetime.fromisoformat(last["at"])).total_seconds() / 3600
        present = (ctx.settings.backups_dir / last["file"]).exists()  # the record of a check is not the file itself
        add("Recent verified backup", hours <= BACKUP_MAX_AGE_HOURS and present,
            f"{last['file']}: {last['rows']} rows, verified {hours:.0f} h ago"
            + ("" if present else "; that file is no longer in the backups folder"))
    off_ready, off_reason = offsite_status(ctx.settings)
    if not off_ready:
        add("Off-machine encrypted backup", False, f"Off-machine backup {off_reason}", "warning")
    elif off is None:
        add("Off-machine encrypted backup", False, "Configured; no copy written yet", "warning")
    else:
        hours = (utcnow() - datetime.fromisoformat(off["at"])).total_seconds() / 3600
        add("Off-machine encrypted backup", hours <= BACKUP_MAX_AGE_HOURS, f"{off['file']}, {hours:.0f} h ago")
    if ctx.settings.backup_offsite_dir:
        leaves, why = offsite_sync(ctx.settings.backup_offsite_dir)
        add("Off-machine folder leaves this PC", leaves is True, why, "warning")

    st = ctx.settings
    if st.outbound_email_enabled and st.section("outreach").get("b2b_enabled"):
        from aime import mail_accounts

        with ctx.db.session() as s:  # the .env mailbox or a Mailboxes panel mailbox with Read replies on
            reading, where = mail_accounts.reading_status(s, st)
        add("Sales mailbox monitored", reading,
            f"replies, unsubscribes and bounces are read: {where}" if reading else
            "outreach can be sent but replies, unsubscribes and bounces are not read: on the Mailboxes page enable a "
            "mailbox and turn on Read replies (or set IMAP_HOST in .env)",
            "warning")
        every = int(st.schedule.get("jobs", {}).get("inbox", {}).get("every_minutes") or 0)
        add("Mailbox read before sending", every <= 15, f"the inbox job runs every {every} minutes" if every <= 15 else
            f"the inbox job runs only every {every} minutes: approved emails wait for a read just before they go, so they "
            "will often be held. Set it to 15 minutes or less", "warning")
