"""Data retention for the daily db_maintenance job (Option B Phase 8), driven by Settings > Retention.

Every value is in days and 0 means "as today", so the defaults change nothing:

- job_runs_days / agent_runs_days: 0 keeps the run history the job has always kept (90 days, aime.workflows
  RUN_HISTORY_DAYS); N > 0 keeps N days instead (longer or shorter). Unfinished runs and the latest run of each job and
  agent in each outcome always stay: "last success" and "last error" are read from those.
- alerts_days: 0 keeps every alert; N > 0 deletes alerts that were resolved more than N days ago. An open alert, or
  one only dismissed, is never deleted.
- inbound_emails_days and audit_events_days are not applied here: a reply is part of a lead's record, and the audit
  log is append-only (the settings already refuse less than a year for it).

Nothing here touches CRM records (companies, contacts, leads, deals, activities, emails), suppressions, the audit log
or the sent-email journal. It runs only after a verified backup and a sound integrity check (job_db_maintenance).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from aime.models import AgentRun, Alert, JobRun
from aime.timeutil import utcnow

RUN_HISTORY_DAYS = 90  # what db_maintenance has always kept (aime.workflows.RUN_HISTORY_DAYS)
TABLES_TOUCHED = ("job_runs", "agent_runs", "alerts")  # the only tables this module ever deletes from


def days(settings, name: str) -> int:
    try:
        value = int((settings.section("retention") or {}).get(name) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def plan(settings) -> dict[str, int | None]:
    """How many days each table keeps under the settings now (None: kept for good)."""
    return {"job_runs": days(settings, "job_runs_days") or RUN_HISTORY_DAYS,
            "agent_runs": days(settings, "agent_runs_days") or RUN_HISTORY_DAYS,
            "alerts": days(settings, "alerts_days") or None}


def _prune_runs(session, model, name_col, keep_days: int, now: datetime) -> int:
    cutoff = now - timedelta(days=keep_days)
    latest = select(func.max(model.id)).group_by(name_col, model.status)
    return session.execute(delete(model).where(model.started_at < cutoff, model.status != "running",
                                               model.id.not_in(latest))).rowcount


def apply(db, settings, now: datetime | None = None) -> dict:
    """Delete what the retention settings no longer keep. Returns the rows removed per table and the days applied."""
    now = now or utcnow()
    keep = plan(settings)
    removed = {"job_runs": 0, "agent_runs": 0, "alerts": 0}
    with db.session() as s:
        removed["job_runs"] = _prune_runs(s, JobRun, JobRun.job, keep["job_runs"], now)
        removed["agent_runs"] = _prune_runs(s, AgentRun, AgentRun.agent, keep["agent_runs"], now)
        if keep["alerts"]:
            removed["alerts"] = s.execute(delete(Alert).where(Alert.resolved_at.is_not(None),
                                                              Alert.resolved_at < now - timedelta(days=keep["alerts"]))).rowcount
    return {"removed": removed, "days": keep}
