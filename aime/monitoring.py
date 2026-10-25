"""Performance monitoring: agent success rates and latency, job health, alerts, scheduler heartbeat."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.config import Settings
from aime.models import AgentRun, Alert, JobState
from aime.timeutil import utcnow


def agent_stats(session: Session, days: int = 7) -> list[dict]:
    since = utcnow() - timedelta(days=days)
    rows = session.execute(select(AgentRun).where(AgentRun.started_at >= since, AgentRun.status != "running")
                           .order_by(AgentRun.started_at)).scalars().all()
    by_agent: dict[str, list[AgentRun]] = defaultdict(list)
    for r in rows:
        by_agent[r.agent].append(r)
    out = []
    for agent, runs in sorted(by_agent.items()):
        durations = sorted(r.duration_ms or 0 for r in runs)
        ok = sum(1 for r in runs if r.status == "success")
        failed = [r for r in runs if r.status == "failed"]
        p95 = durations[min(len(durations) - 1, int(round(0.95 * (len(durations) - 1))))] if durations else 0
        out.append({
            "agent": agent,
            "runs": len(runs),
            "success": ok,
            "failed": len(failed),
            "success_rate": round(100 * ok / len(runs), 1) if runs else 0.0,
            "avg_ms": int(sum(durations) / len(durations)) if durations else 0,
            "p95_ms": p95,
            "retries": sum(max(0, (r.attempts or 1) - 1) for r in runs),
            "last_run": runs[-1].started_at,
            "last_error": failed[-1].error if failed else None,
        })
    return out


def alert_once(session: Session, level: str, source: str, message: str) -> bool:
    """Create an alert unless the same one was raised in the last 24 hours."""
    since = utcnow() - timedelta(hours=24)
    if session.execute(select(Alert.id).where(Alert.source == source, Alert.message == message[:500],
                                              Alert.created_at >= since)).first():
        return False
    session.add(Alert(level=level, source=source, message=message[:500]))
    return True


def check_health(session: Session, settings: Settings) -> dict:
    """Create alerts for failing/stale jobs and unhealthy agents. Returns a summary."""
    from aime.scheduler.runner import load_job_specs

    created = 0
    issues: list[str] = []
    now = utcnow()
    states = {s.job: s for s in session.execute(select(JobState)).scalars()}
    for spec in load_job_specs(settings):
        st = states.get(spec.name)
        if st is None:
            continue
        if st.consecutive_failures >= 2:
            msg = f"Job '{spec.name}' failed {st.consecutive_failures} times in a row"
            issues.append(msg)
            created += alert_once(session, "error", "monitor", msg)
        if spec.every_minutes:
            expected = timedelta(minutes=spec.every_minutes * 3)
        else:
            expected = timedelta(days=8) if spec.weekdays else timedelta(hours=26)
        if st.last_success_at and now - st.last_success_at > expected:
            msg = f"Job '{spec.name}' has not succeeded since {st.last_success_at:%Y-%m-%d %H:%M} UTC"
            issues.append(msg)
            created += alert_once(session, "warning", "monitor", msg)
    for row in agent_stats(session, days=1):
        if row["runs"] >= 3 and row["success_rate"] < 80:
            msg = f"Agent '{row['agent']}' success rate {row['success_rate']}% over the last 24h"
            issues.append(msg)
            created += alert_once(session, "warning", "monitor", msg)
    return {"issues": issues, "alerts_created": created}


def heartbeat_path(settings: Settings) -> Path:
    return settings.data_dir / "heartbeat.json"


def write_heartbeat(settings: Settings, info: dict) -> None:
    path = heartbeat_path(settings)
    payload = {"at": utcnow().isoformat() + "Z", **info}
    path.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")


def read_heartbeat(settings: Settings) -> dict | None:
    path = heartbeat_path(settings)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
