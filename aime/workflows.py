"""Jobs: orchestrated multi-agent workflows that the scheduler runs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from aime.agents import (AnalyticsReportingAgent, AuthorizedPublishingAgent, CRMManagementAgent,
                         LeadQualificationAgent, LinkedInContentAgent, MarketingManagerAgent, SalesWorkflowAgent,
                         USBusinessResearchAgent)
from aime.agents.base import AgentResult
from aime.backup import backup_sqlite_file, checkpoint, integrity_check, prune_backups
from aime.context import AppContext
from aime.doctor import run_checks
from aime.models import MarketingPlan
from aime.monitoring import alert_once, check_health
from aime.sources.census import CensusMarketSource


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


def job_content(ctx: AppContext, job: str = "content") -> JobOutcome:
    out = JobOutcome()
    out.add(LinkedInContentAgent(ctx).run({}, job=job))
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
    """Daily online backup (14 kept), integrity check, and WAL checkpoint."""
    out = JobOutcome()
    s = ctx.settings
    db_path = ctx.db.engine.url.database
    backup = backup_sqlite_file(Path(db_path), s.backups_dir, label="auto") if db_path else None
    pruned = prune_backups(s.backups_dir, label="auto", keep=14)
    check = integrity_check(ctx.db)
    checkpoint(ctx.db)
    step = {"agent": "db_maintenance", "status": "success" if check["ok"] else "failed",
            "output": {"backup": backup.name if backup else None, "pruned": pruned, **check}}
    if not check["ok"]:
        step["error"] = f"Integrity check failed: {check}"
        out.failed = True
    out.steps.append(step)
    return out


def job_monitor(ctx: AppContext, job: str = "monitor") -> JobOutcome:
    out = JobOutcome()
    with ctx.db.session() as s:
        summary = check_health(s, ctx.settings)
    failing = [c for c in run_checks(ctx) if not c["ok"]]
    with ctx.db.session() as s:
        for c in failing:
            created = alert_once(s, "error" if c["severity"] == "error" else "warning", "doctor",
                                 f"{c['name']}: {c['detail']}")
            summary["alerts_created"] += int(created)
    summary["doctor_failures"] = [f"{c['name']}: {c['detail']}" for c in failing]
    out.steps.append({"agent": "monitor", "status": "success", "output": summary})
    return out


JOBS: dict[str, Callable[..., JobOutcome]] = {
    "db_maintenance": job_db_maintenance,
    "crm_maintenance": job_crm_maintenance,
    "plan": job_plan,
    "research": job_research,
    "content": job_content,
    "inbound": job_inbound,
    "followups": job_followups,
    "publish": job_publish,
    "daily_report": job_daily_report,
    "weekly_report": job_weekly_report,
    "monitor": job_monitor,
}
