"""Performance monitoring: agent success rates and latency, job health, alerts, scheduler heartbeat."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.config import Settings
from aime.db import kv_get, kv_set
from aime.llm.ollama import OllamaProvider
from aime.models import AgentRun, Alert, JobState
from aime.notify import notify_alert
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


# Alerts raised before keys existed (until 27 Oct 2026): their key is read from the message.
LEGACY_KEYS = [
    (re.compile(r"^Job '([^']+)' failed \d+ times in a row"), "job_failing:{}"),
    (re.compile(r"^Job '([^']+)' has not succeeded since"), "job_stale:{}"),
    (re.compile(r"^Agent '([^']+)' success rate"), "agent_rate:{}"),
    (re.compile(r"^Local AI model \(Ollama\) unreachable"), "llm:down"),
]


def alert_key(alert: Alert) -> str | None:
    if alert.key:
        return alert.key
    if alert.source == "doctor" and ":" in alert.message:
        return "doctor:" + alert.message.split(":", 1)[0]
    if alert.source == "monitor":
        for pattern, key in LEGACY_KEYS:
            m = pattern.match(alert.message)
            if m:
                return key.format(*m.groups())
    return None


def alert_once(session: Session, level: str, source: str, message: str, settings: Settings | None = None,
               key: str | None = None) -> bool:
    """Create an alert unless it is already known; new ones can raise a desktop toast.

    Without `key`: the same message at most once per 24 hours. With `key` (what the alert is about): an open alert
    with that key gets the latest message instead of a repeat, one a person dismissed stays quiet for 24 hours,
    and `resolve_alerts` closes it once the problem is gone.
    """
    since = utcnow() - timedelta(hours=24)
    if key is None:
        if session.execute(select(Alert.id).where(Alert.source == source, Alert.message == message[:500],
                                                  Alert.created_at >= since)).first():
            return False
    else:
        same = [a for a in session.execute(select(Alert).where(Alert.source == source, (Alert.acknowledged.is_(False))
                                                               | (Alert.created_at >= since))).scalars()
                if alert_key(a) == key]
        for a in same:
            if not a.acknowledged:
                a.key, a.level, a.message = key, level, message[:500]
                return False
        if any(a.resolved_at is None for a in same):  # dismissed by a person within the last 24 hours
            return False
    session.add(Alert(level=level, source=source, message=message[:500], key=key))
    notify_alert(settings, level, message)
    return True


def resolve_alerts(session: Session, prefixes: tuple[str, ...], active: set[str]) -> int:
    """Close open alerts whose key starts with one of `prefixes` but is no longer in `active` (problem gone)."""
    closed = 0
    for a in session.execute(select(Alert).where(Alert.acknowledged.is_(False))).scalars():
        key = alert_key(a)
        if key and key.startswith(prefixes) and key not in active:
            a.key, a.acknowledged, a.resolved_at = key, True, utcnow()
            closed += 1
    return closed


def check_llm(session: Session, settings: Settings, probe: Callable[[], bool] | None = None) -> dict:
    """Probe the local model when one is expected. Two failed probes in a row (about 30-60 min) raise an alert."""
    provider = (settings.llm_provider or "auto").lower()
    if provider not in ("auto", "ollama"):
        return {"expected": False}
    ok = (probe or OllamaProvider(settings.ollama_url, settings.ollama_model).available)()
    now = utcnow()
    prev = kv_get(session, "llm.health") or {}
    if ok:
        state = {"ok": True, "last_ok_at": now.isoformat(), "failures": 0}
    else:
        state = {"ok": False, "last_ok_at": prev.get("last_ok_at"), "failures": int(prev.get("failures", 0)) + 1,
                 "down_since": prev.get("down_since") or now.isoformat()}
    kv_set(session, "llm.health", state)
    if not ok and state["failures"] >= 2:
        since = datetime.fromisoformat(state["down_since"])
        alert_once(session, "warning", "monitor", f"Local AI model (Ollama) unreachable since {since:%Y-%m-%d %H:%M} "
                   "UTC; new posts use the template writer. Start Ollama or enable OLLAMA_AUTOSTART.", settings,
                   key="llm:down")
    elif ok:
        resolve_alerts(session, ("llm:",), set())
    return {"expected": True, **state}


def check_health(session: Session, settings: Settings, llm_probe: Callable[[], bool] | None = None) -> dict:
    """Create alerts for failing/stale jobs, unhealthy agents and an unreachable local model. Returns a summary."""
    from aime.scheduler.runner import load_job_specs

    created = 0
    issues: list[str] = []
    active: set[str] = set()
    now = utcnow()
    llm = check_llm(session, settings, llm_probe)
    if llm["expected"] and not llm["ok"]:
        issues.append(f"Local AI model unreachable ({llm['failures']} check(s) in a row)")
    states = {s.job: s for s in session.execute(select(JobState)).scalars()}
    for spec in load_job_specs(settings):
        st = states.get(spec.name)
        if st is None or not spec.enabled or not st.enabled:
            continue
        if st.consecutive_failures >= 2:
            msg = f"Job '{spec.name}' failed {st.consecutive_failures} times in a row"
            issues.append(msg)
            active.add(f"job_failing:{spec.name}")
            created += alert_once(session, "error", "monitor", msg, settings, key=f"job_failing:{spec.name}")
        if spec.name == "monitor":  # its own state is written only after this run ends: never stale here
            continue
        if spec.every_minutes:
            expected = timedelta(minutes=spec.every_minutes * 3)
        else:
            expected = timedelta(days=8) if spec.weekdays else timedelta(hours=26)
        if st.last_success_at and now - st.last_success_at > expected:
            msg = f"Job '{spec.name}' has not succeeded since {st.last_success_at:%Y-%m-%d %H:%M} UTC"
            issues.append(msg)
            active.add(f"job_stale:{spec.name}")
            created += alert_once(session, "warning", "monitor", msg, settings, key=f"job_stale:{spec.name}")
    for row in agent_stats(session, days=1):
        if row["runs"] >= 3 and row["success_rate"] < 80:
            msg = f"Agent '{row['agent']}' success rate {row['success_rate']}% over the last 24h"
            issues.append(msg)
            active.add(f"agent_rate:{row['agent']}")
            created += alert_once(session, "warning", "monitor", msg, settings, key=f"agent_rate:{row['agent']}")
    resolved = resolve_alerts(session, ("job_failing:", "job_stale:", "agent_rate:"), active)
    return {"issues": issues, "alerts_created": created, "alerts_resolved": resolved}


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
