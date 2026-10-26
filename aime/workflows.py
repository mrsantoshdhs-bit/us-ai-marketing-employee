"""Jobs: orchestrated multi-agent workflows that the scheduler runs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from aime.agents import (AnalyticsReportingAgent, AuthorizedPublishingAgent, CompanyResearchAgent, CRMManagementAgent,
                         InboxAgent,
                         LeadQualificationAgent, LinkedInContentAgent, MarketingManagerAgent, SalesWorkflowAgent,
                         USBusinessResearchAgent)
from aime.agents.base import AgentResult
from aime.backup import (backup_sqlite_file, checkpoint, integrity_check, prune_backups, verify_backup,
                         write_offsite)
from aime.context import AppContext
from aime.db import kv_set
from aime.doctor import run_checks
from aime.models import MarketingPlan
from aime.monitoring import alert_once, check_health
from aime.policy import evaluate_shadow
from aime.redact import redact
from aime.sources.census import CensusMarketSource
from aime.timeutil import utcnow


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
        if not result.ok:
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
        out.add(SalesWorkflowAgent(ctx).run({}, job=job))
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
    crm = out.add(CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]}, job=job))
    if crm.ok and crm.output.inbound_lead_ids:
        out.add(LeadQualificationAgent(ctx).run({"lead_ids": crm.output.inbound_lead_ids}, job=job))
        out.add(SalesWorkflowAgent(ctx).run({"lead_ids": crm.output.inbound_lead_ids}, job=job))
    return out


def job_followups(ctx: AppContext, job: str = "followups") -> JobOutcome:
    out = JobOutcome()
    out.add(SalesWorkflowAgent(ctx).run({}, job=job))
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
    """Read the sales mailbox (read-only), handle replies, then send anything newly approved."""
    out = JobOutcome()
    out.add(InboxAgent(ctx).run({}, job=job))
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


def job_db_maintenance(ctx: AppContext, job: str = "db_maintenance") -> JobOutcome:
    """Daily online backup (14 kept) verified after writing, encrypted off-machine copy when configured,
    integrity check, and WAL checkpoint."""
    out = JobOutcome()
    s = ctx.settings
    db_path = ctx.db.engine.url.database
    backup = backup_sqlite_file(Path(db_path), s.backups_dir, label="auto") if db_path else None
    verified = verify_backup(backup) if backup else None
    pruned = prune_backups(s.backups_dir, label="auto", keep=14)
    check = integrity_check(ctx.db)
    checkpoint(ctx.db)
    problems = []
    if not check["ok"]:
        problems.append(f"Integrity check failed: {check}")
    if verified is not None and not verified["ok"]:
        problems.append(f"Backup {backup.name} failed verification: {verified['integrity']}")
    elif verified is not None:
        with ctx.db.session() as session:
            kv_set(session, "backup.last_verified", {"file": backup.name, "at": utcnow().isoformat(),
                                                     "tables": verified["tables"], "rows": verified["rows"]})
    step = {"agent": "db_maintenance", "status": "failed" if problems else "success",
            "output": {"backup": backup.name if backup else None, "pruned": pruned, **check,
                       "backup_verified": bool(verified and verified["ok"]),
                       "backup_rows": verified["rows"] if verified else None}}
    if problems:
        step["error"] = "; ".join(problems)
        out.failed = True
    out.steps.append(step)

    if db_path:  # encrypted off-machine copy (optional; a failure is reported but the local backup stands)
        try:
            offsite = write_offsite(s, Path(db_path))
        except Exception as exc:  # keep the job alive; the monitor raises an alert on repeated failures
            offsite = {"status": "error", "reason": redact(str(exc))}
        if offsite["status"] == "ok":
            with ctx.db.session() as session:
                kv_set(session, "backup.last_offsite", {"file": offsite["file"], "at": utcnow().isoformat()})
        off_step = {"agent": "offsite_backup", "status": "failed" if offsite["status"] == "error" else "success",
                    "output": offsite}
        if offsite["status"] == "error":
            off_step["error"] = offsite["reason"]
            out.failed = True
        out.steps.append(off_step)
    return out


def job_monitor(ctx: AppContext, job: str = "monitor") -> JobOutcome:
    out = JobOutcome()
    with ctx.db.session() as s:
        summary = check_health(s, ctx.settings)
    failing = [c for c in run_checks(ctx) if not c["ok"]]
    with ctx.db.session() as s:
        for c in failing:
            created = alert_once(s, "error" if c["severity"] == "error" else "warning", "doctor",
                                 f"{c['name']}: {c['detail']}", ctx.settings)
            summary["alerts_created"] += int(created)
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
