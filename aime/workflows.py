"""Jobs: orchestrated multi-agent workflows that the scheduler runs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

from aime.agents import (AnalyticsReportingAgent, AuthorizedPublishingAgent, CompanyResearchAgent, CRMManagementAgent,
                         InboxAgent,
                         LeadQualificationAgent, LinkedInContentAgent, MarketingManagerAgent, SalesWorkflowAgent,
                         USBusinessResearchAgent)
from aime.agents.base import AgentResult, agent_enabled
from aime.backup import (OFFSITE_KEY_CHANGED, OFFSITE_PROBLEM, PREDEPLOY_KEPT, backup_sqlite_file, checkpoint, integrity_check,
                         prune_backups, try_write_offsite, verify_backup, weekly_offsite_drill, weekly_restore_drill)
from aime.context import AppContext
from aime.db import kv_get, kv_set
from aime.doctor import run_checks
from aime.models import AgentRun, JobRun, JobState, MarketingPlan
from aime.monitoring import alert_once, check_health, resolve_alerts
from aime.policy import evaluate_shadow
from aime.redact import redact
from aime.sources.census import CensusMarketSource
from aime.timeutil import utcnow

MAILBOX_READ = "mailbox.last_read"  # when every mailbox was last read to the end; empty once a read failed or stopped short
MAILBOX_READ_SHORT = "mailbox.read_short"  # that the last read was fine but more mail is waiting
# The inbox job runs at every check-in and before followups, so a real run finds a read from seconds ago. This much
# slack is for a followups run started by hand between two check-ins.
MAILBOX_READ_FRESH = timedelta(minutes=20)
RUN_HISTORY_DAYS = 90
RETRY_KEEPS_COPY = timedelta(hours=12)  # a retry of a backup job that left a good copy this recently writes no second one


class JobOutcome:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.failed = False

    def add(self, result: AgentResult) -> AgentResult:
        self.steps.append({
            "agent": result.agent, "status": result.status, "attempts": result.attempts,
            "duration_ms": result.duration_ms, "error": result.error,
            "output": result.output.model_dump(mode="json") if result.output else None,
        })
        if not result.ok and result.status != "skipped":  # an agent switched off is not a failure
            self.failed = True
        return result

    def detail(self) -> dict:
        return {"steps": self.steps}


def _latest_plan(ctx: AppContext) -> dict | None:
    with ctx.db.session() as s:
        plan = s.execute(select(MarketingPlan).order_by(MarketingPlan.id.desc())).scalars().first()
        if plan is None:
            return None
        return {"industries": plan.focus_industries, "states": plan.focus_states}


def job_plan(ctx: AppContext, job: str = "plan") -> JobOutcome:
    out = JobOutcome()
    census = CensusMarketSource(ctx)
    if census.configured()[0]:
        try:
            with ctx.db.session() as s:
                n = census.refresh(s, ctx.settings.section("targeting").get("industries"))
            out.steps.append({"agent": "census_refresh", "status": "success", "output": {"segments": n}})
        except Exception as exc:  # market sizing is optional context
            out.steps.append({"agent": "census_refresh", "status": "failed", "error": str(exc)[:300]})
    out.add(MarketingManagerAgent(ctx).run({}, job=job))
    return out


def job_research(ctx: AppContext, job: str = "research") -> JobOutcome:
    out = JobOutcome()
    plan = _latest_plan(ctx)
    if plan is None:
        out.add(MarketingManagerAgent(ctx).run({}, job=job))
        plan = _latest_plan(ctx) or {"industries": [], "states": []}
    research = out.add(USBusinessResearchAgent(ctx).run(plan, job=job))
    if research.ok:
        if research.output.new_company_ids:
            out.add(CRMManagementAgent(ctx).run(
                {"operations": ["verify_websites", "dedupe"], "company_ids": research.output.new_company_ids}, job=job))
        out.add(LeadQualificationAgent(ctx).run({}, job=job))
        out.add(SalesWorkflowAgent(ctx).run({"send": False}, job=job))  # only the followups job sends
    return out


def _policy_shadow_step(ctx: AppContext, out: JobOutcome) -> None:
    """Record what the auto-publishing policy would do (shadow mode only: it never changes a post).
    Skipped when the job's own work failed, so the job still reports "failed" rather than "partial"."""
    if out.failed:
        return
    try:
        with ctx.db.session() as s:
            summary = evaluate_shadow(s, ctx.settings)
        out.steps.append({"agent": "publish_policy", "status": "success", "output": summary})
    except Exception as exc:  # the shadow policy must never affect content or publishing
        out.steps.append({"agent": "publish_policy", "status": "failed", "error": redact(str(exc))[:300]})
        out.failed = True


def job_content(ctx: AppContext, job: str = "content") -> JobOutcome:
    out = JobOutcome()
    out.add(LinkedInContentAgent(ctx).run({}, job=job))
    _policy_shadow_step(ctx, out)
    return out


def job_inbound(ctx: AppContext, job: str = "inbound") -> JobOutcome:
    out = JobOutcome()
    batch = int(ctx.settings.section("research").get("website_checks_per_inbound_run", 5))
    crm = out.add(CRMManagementAgent(ctx).run({"operations": ["ingest_inbound", "verify_websites"] if batch else ["ingest_inbound"],
                                               "website_check_limit": batch or None}, job=job))
    if crm.ok and crm.output.inbound_lead_ids:
        out.add(LeadQualificationAgent(ctx).run({"lead_ids": crm.output.inbound_lead_ids}, job=job))
        out.add(SalesWorkflowAgent(ctx).run({"lead_ids": crm.output.inbound_lead_ids, "send": False}, job=job))
    return out


def mailbox_hold(ctx: AppContext) -> str | None:
    """Why approved emails must wait, or None. An opt-out may be waiting in a mailbox nobody has read, so nothing goes
    out unless the mailbox was read just before. No mailbox set up at all: there is nothing to read first."""
    from aime import mail_accounts

    now = utcnow()
    with ctx.db.session() as s:
        if not ctx.settings.network_enabled or not (ctx.settings.imap_configured or mail_accounts.readable(s)):
            return None
        read = kv_get(s, MAILBOX_READ)
        short = kv_get(s, MAILBOX_READ_SHORT)
        job = s.get(JobState, "inbox")
        switched_off = job is not None and not job.enabled
    try:
        age = now - datetime.fromisoformat(read)
    except (TypeError, ValueError):
        age = None
    if age is not None and timedelta(minutes=-5) <= age <= MAILBOX_READ_FRESH:
        return None
    settings = ctx.settings.schedule.get("jobs", {}).get("inbox", {})
    every = int(settings.get("every_minutes") or 0)
    if switched_off or not settings.get("enabled", True) or not agent_enabled(ctx, "inbox_monitor"):
        return ("Held: reading the sales mailbox is switched off, so a reply or opt-out may be waiting in it; "
                "it goes out once reading is back on and has run")
    if every > 15:
        return (f"Held: the sales mailbox is read only every {every} minutes, so it was not read just before this run and a reply "
                "or opt-out may be waiting in it; it goes out after a read just before a run, and the inbox job should run at least "
                "every 15 minutes")
    if short:
        return ("Held: more mail is waiting in the sales mailbox than one read takes in, and a reply or opt-out may be among it; "
                "it goes out once the mailbox has been read to the end")
    return ("Held: the sales mailbox could not be read just before this run, and a reply or opt-out may be waiting in "
            "it; it goes out after the next good read")


def job_followups(ctx: AppContext, job: str = "followups") -> JobOutcome:
    """The one job that sends approved emails: it runs after the inbox job, and holds them if that read failed. The read is
    judged again before each email: the PC may sleep in the middle of a run."""
    out = JobOutcome()
    agent = SalesWorkflowAgent(ctx)
    agent.read_hold = lambda: mailbox_hold(ctx)
    out.add(agent.run({"hold_sends": mailbox_hold(ctx)}, job=job))
    return out


def job_publish(ctx: AppContext, job: str = "publish") -> JobOutcome:
    out = JobOutcome()
    out.add(AuthorizedPublishingAgent(ctx).run({}, job=job))
    _policy_shadow_step(ctx, out)
    return out


def job_enrich(ctx: AppContext, job: str = "enrich") -> JobOutcome:
    """Read companies' own websites and SEC filings for research, decision-makers and published emails, then
    re-score the researched leads (never contacts anyone)."""
    out = JobOutcome()
    research = out.add(CompanyResearchAgent(ctx).run({}, job=job))
    if research.ok and research.output.companies_researched:
        out.add(LeadQualificationAgent(ctx).run({}, job=job))
    return out


def job_inbox(ctx: AppContext, job: str = "inbox") -> JobOutcome:
    """Read the sales mailbox (read-only) and handle replies; approved emails are sent by the followups job."""
    out = JobOutcome()
    read = out.add(InboxAgent(ctx).run({}, job=job))
    configured = not read.ok or (read.output is not None and read.output.mailbox != "not configured")
    if configured:  # a read that failed or stopped short is no read: nothing goes out in this tick on an older one
        to_end = read.ok and read.output is not None and read.output.read_to_end
        with ctx.db.session() as s:
            kv_set(s, MAILBOX_READ, utcnow().isoformat() if to_end else None)
            kv_set(s, MAILBOX_READ_SHORT, bool(read.ok and not to_end))
    return out


def job_crm_maintenance(ctx: AppContext, job: str = "crm_maintenance") -> JobOutcome:
    out = JobOutcome()
    out.add(CRMManagementAgent(ctx).run({}, job=job))
    out.add(LeadQualificationAgent(ctx).run({}, job=job))
    return out


def job_daily_report(ctx: AppContext, job: str = "daily_report") -> JobOutcome:
    out = JobOutcome()
    out.add(AnalyticsReportingAgent(ctx).run({"kind": "daily"}, job=job))
    return out


def job_weekly_report(ctx: AppContext, job: str = "weekly_report") -> JobOutcome:
    out = JobOutcome()
    out.add(AnalyticsReportingAgent(ctx).run({"kind": "weekly"}, job=job))
    return out


def prune_run_history(ctx: AppContext, days: int = RUN_HISTORY_DAYS) -> int:
    """Delete job and agent run records older than `days`: nothing else. Unfinished runs stay, and so does the latest
    run of each job and agent in each outcome: "last success" and "last error" are read from those."""
    cutoff = utcnow() - timedelta(days=days)
    removed = 0
    with ctx.db.session() as s:
        for model, name in ((JobRun, JobRun.job), (AgentRun, AgentRun.agent)):
            latest = select(func.max(model.id)).group_by(name, model.status)
            removed += s.execute(delete(model).where(model.started_at < cutoff, model.status != "running",
                                                     model.id.not_in(latest))).rowcount
    return removed


def _copy_of_the_failed_try(ctx: AppContext, job: str) -> Path | None:
    """Today's local copy, when the last try of this job left it and failed for another reason, and it is still good. A retry
    then writes no second copy: 14 kept must reach back 14 days however long the off-machine folder stays broken."""
    with ctx.db.session() as session:
        state = session.get(JobState, job)
        last = kv_get(session, "backup.last_verified")
    if state is None or state.last_status in (None, "success") or not last:
        return None
    try:
        age = utcnow() - datetime.fromisoformat(last["at"])
    except (KeyError, TypeError, ValueError):
        return None
    file = ctx.settings.backups_dir / str(last.get("file"))
    fresh = timedelta(0) <= age <= RETRY_KEEPS_COPY and file.name.startswith(f"aime-auto-{datetime.now():%Y%m%d}-")
    return file if fresh and verify_backup(file)["ok"] else None


def job_db_maintenance(ctx: AppContext, job: str = "db_maintenance") -> JobOutcome:
    """Daily online backup (14 kept) verified after writing, encrypted off-machine copy when configured,
    integrity check, run history older than 90 days removed, and WAL checkpoint."""
    out = JobOutcome()
    s = ctx.settings
    db_path = ctx.db.engine.url.database
    kept = _copy_of_the_failed_try(ctx, job) if db_path else None
    backup = kept or (backup_sqlite_file(Path(db_path), s.backups_dir, label="auto") if db_path else None)
    verified = verify_backup(backup) if backup else None
    pruned = 0
    if verified and verified["ok"] and not kept:  # a copy that failed verification must not push out the last good one
        from aime.backup import prune_manual_backups

        pruned = prune_backups(s.backups_dir, label="auto", keep=14)
        pruned += len(prune_manual_backups(s.backups_dir, int(getattr(s, "backup_keep_manual", 0) or 0),
                                           keep_predeploy=PREDEPLOY_KEPT))
    check = integrity_check(ctx.db)
    # Only with a good backup of what is about to go, and a sound database.
    history_pruned = prune_run_history(ctx) if check["ok"] and (verified is None or verified["ok"]) else 0
    checkpoint(ctx.db)
    problems = []
    if not check["ok"]:
        problems.append(f"Integrity check failed: {check}")
    if verified is not None and not verified["ok"]:
        problems.append(f"Backup {backup.name} failed verification: {verified['integrity']}")
    elif verified is not None and not kept:
        with ctx.db.session() as session:
            kv_set(session, "backup.last_verified", {"file": backup.name, "at": utcnow().isoformat(),
                                                     "tables": verified["tables"], "rows": verified["rows"]})
    step = {"agent": "db_maintenance", "status": "failed" if problems else "success",
            "output": {"backup": backup.name if backup else None, "pruned": pruned, "history_pruned": history_pruned, **check,
                       "backup_verified": bool(verified and verified["ok"]), "copy_of_the_earlier_try": bool(kept),
                       "backup_rows": verified["rows"] if verified else None}}
    if problems:
        step["error"] = "; ".join(problems)
        out.failed = True
    out.steps.append(step)

    if db_path:  # encrypted off-machine copy (optional; a failure is reported but the local backup stands)
        offsite = try_write_offsite(s, Path(db_path))  # keep the job alive; the monitor raises an alert on repeated failures
        if offsite["status"] == "ok":
            with ctx.db.session() as session:
                kv_set(session, "backup.last_offsite", {"file": offsite["file"], "at": utcnow().isoformat(), "bytes": offsite["bytes"]})
                if offsite.get("key_changed_after"):
                    kv_set(session, OFFSITE_KEY_CHANGED, {"file": offsite["key_changed_after"], "at": utcnow().isoformat()})
        off_step = {"agent": "offsite_backup", "status": "failed" if offsite["status"] == "error" else "success",
                    "output": offsite}
        if offsite["status"] == "error":
            off_step["error"] = offsite["reason"]
            out.failed = True
        out.steps.append(off_step)
        problem = offsite["reason"] if offsite["status"] == "error" else None
    else:
        problem = None
    drill = weekly_restore_drill(ctx) if db_path else None
    if drill:
        out.failed = out.failed or drill["status"] != "success"
        out.steps.append(drill)
    drill = weekly_offsite_drill(ctx) if db_path else None
    if drill:
        out.failed = out.failed or drill["status"] != "success"
        out.steps.append(drill)
        problem = problem or drill.get("error")
    with ctx.db.session() as session:  # what doctor, the home page and the alert say, in words
        kv_set(session, OFFSITE_PROBLEM, {"why": problem, "at": utcnow().isoformat(), "local_ok": not problems} if problem else None)
    return out


def job_monitor(ctx: AppContext, job: str = "monitor") -> JobOutcome:
    out = JobOutcome()
    with ctx.db.session() as s:
        summary = check_health(s, ctx.settings)
    failing = [c for c in run_checks(ctx) if not c["ok"] and not c.get("own_alert")]  # one problem, one alert
    with ctx.db.session() as s:
        for c in failing:
            created = alert_once(s, "error" if c["severity"] == "error" else "warning", "doctor",
                                 f"{c['name']}: {c['detail']}", ctx.settings, key=f"doctor:{c['name']}")
            summary["alerts_created"] += int(created)
        summary["alerts_resolved"] += resolve_alerts(s, ("doctor:",), {f"doctor:{c['name']}" for c in failing})
    summary["doctor_failures"] = [f"{c['name']}: {c['detail']}" for c in failing]
    out.steps.append({"agent": "monitor", "status": "success", "output": summary})
    return out


JOBS: dict[str, Callable[..., JobOutcome]] = {
    "db_maintenance": job_db_maintenance,
    "crm_maintenance": job_crm_maintenance,
    "plan": job_plan,
    "research": job_research,
    "enrich": job_enrich,
    "inbox": job_inbox,
    "content": job_content,
    "inbound": job_inbound,
    "followups": job_followups,
    "publish": job_publish,
    "daily_report": job_daily_report,
    "weekly_report": job_weekly_report,
    "monitor": job_monitor,
}
