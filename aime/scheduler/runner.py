"""Scheduler core: decide which jobs are due, run them under locks, and record job history.

The OS scheduler (Windows Task Scheduler) only calls `tick` every few minutes; all timing
decisions live here, so missed runs (PC asleep/off) are caught up on the next tick.
"""

from __future__ import annotations

import os
import time as _time
import traceback
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select

from aime.config import Settings
from aime.context import AppContext
from aime.models import JobRun, JobState
from aime.monitoring import write_heartbeat
from aime.timeutil import as_aware_utc, local_tz, to_naive_utc, utcnow
from aime.workflows import JOBS

RETRY_BACKOFF = timedelta(minutes=30)
MAX_DAILY_RETRIES = 3


@dataclass
class JobSpec:
    name: str
    every_minutes: int | None
    daily_at: time | None
    description: str
    enabled: bool = True


def load_job_specs(settings: Settings) -> list[JobSpec]:
    cfg = settings.schedule.get("jobs", {})
    specs = []
    for name in JOBS:  # registry order = execution order within a tick
        jc = cfg.get(name)
        if jc is None:
            continue
        daily = time.fromisoformat(jc["daily_at"]) if jc.get("daily_at") else None
        every = int(jc["every_minutes"]) if jc.get("every_minutes") else None
        if not daily and not every:
            continue
        specs.append(JobSpec(name, every, daily, jc.get("description", ""), bool(jc.get("enabled", True))))
    return specs


def is_due(spec: JobSpec, state: JobState | None, now: datetime, tz, tick_minutes: int = 15) -> tuple[bool, str]:
    if not spec.enabled or (state is not None and not state.enabled):
        return False, "disabled"
    if spec.every_minutes:
        if state is None or state.last_attempt_at is None:
            return True, "never run"
        elapsed = now - state.last_attempt_at
        slack = timedelta(minutes=min(2, tick_minutes / 4))
        if elapsed >= timedelta(minutes=spec.every_minutes) - slack:
            return True, f"interval elapsed ({int(elapsed.total_seconds() // 60)} min)"
        remaining = timedelta(minutes=spec.every_minutes) - elapsed
        return False, f"next in {int(remaining.total_seconds() // 60)} min"
    local_now = as_aware_utc(now).astimezone(tz)
    today_slot = datetime.combine(local_now.date(), spec.daily_at, tzinfo=tz)
    last_slot = today_slot if local_now >= today_slot else today_slot - timedelta(days=1)
    last_slot_utc = to_naive_utc(last_slot)
    if state and state.last_success_at and state.last_success_at >= last_slot_utc:
        return False, "already succeeded for the latest slot"
    if state and state.last_attempt_at and state.last_attempt_at >= last_slot_utc:
        if state.consecutive_failures >= MAX_DAILY_RETRIES:
            return False, "failed repeatedly; waiting for the next daily slot"
        if now - state.last_attempt_at < RETRY_BACKOFF:
            return False, "retry backoff"
    return True, f"daily slot {last_slot:%Y-%m-%d %H:%M} pending"


class FileLock:
    def __init__(self, path: Path, stale_seconds: int = 7200):
        self.path = path
        self.stale_seconds = stale_seconds
        self.held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {datetime.now().isoformat()}".encode())
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                try:
                    age = _time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > self.stale_seconds:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                return False
        return False

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.held = False


def run_job(ctx: AppContext, name: str, trigger: str = "manual") -> dict:
    if name not in JOBS:
        raise KeyError(f"Unknown job '{name}'. Known: {', '.join(JOBS)}")
    lock = FileLock(ctx.settings.locks_dir / f"job_{name}.lock")
    if not lock.acquire():
        return {"job": name, "status": "skipped", "reason": "already running"}
    started = utcnow()
    t0 = _time.perf_counter()
    with ctx.db.session() as s:
        row = JobRun(job=name, trigger=trigger, status="running", started_at=started)
        s.add(row)
        s.flush()
        run_id = row.id
    status, detail, error = "failed", {}, None
    try:
        outcome = JOBS[name](ctx, job=name)
        detail = outcome.detail()
        if not outcome.failed:
            status = "success"
        elif any(st.get("status") == "success" for st in outcome.steps):
            status = "partial"
        error = "; ".join(f"{st['agent']}: {st.get('error')}" for st in outcome.steps if st.get("status") != "success") or None
    except Exception as exc:  # keep the scheduler alive no matter what
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
    finally:
        lock.release()
    finished = utcnow()
    duration = int((_time.perf_counter() - t0) * 1000)
    with ctx.db.session() as s:
        row = s.get(JobRun, run_id)
        row.status, row.finished_at, row.duration_ms = status, finished, duration
        row.detail, row.error = detail, (error[:4000] if error else None)
        st = s.get(JobState, name) or JobState(job=name, consecutive_failures=0, enabled=True)
        st.last_attempt_at = started
        st.last_status = status
        if status == "success":
            st.last_success_at = started
            st.consecutive_failures = 0
        else:
            st.consecutive_failures = (st.consecutive_failures or 0) + 1
        s.add(st)
    return {"job": name, "status": status, "duration_ms": duration, "error": error, "run_id": run_id}


def due_jobs(ctx: AppContext, now: datetime | None = None) -> list[tuple[JobSpec, bool, str]]:
    now = now or utcnow()
    tz = local_tz(ctx.settings.schedule.get("timezone"))
    tick = int(ctx.settings.schedule.get("tick_minutes", 15))
    with ctx.db.session() as s:
        states = {st.job: st for st in s.execute(select(JobState)).scalars()}
        return [(spec, *is_due(spec, states.get(spec.name), now, tz, tick)) for spec in load_job_specs(ctx.settings)]


def tick(ctx: AppContext, trigger: str = "scheduler", now: datetime | None = None) -> dict:
    lock = FileLock(ctx.settings.locks_dir / "tick.lock", stale_seconds=5400)
    if not lock.acquire():
        return {"status": "skipped", "reason": "another tick is running", "ran": []}
    ran: list[dict] = []
    try:
        write_heartbeat(ctx.settings, {"trigger": trigger, "pid": os.getpid(), "state": "running"})
        for spec, due, reason in due_jobs(ctx, now):
            if due:
                result = run_job(ctx, spec.name, trigger=trigger)
                result["reason"] = reason
                ran.append(result)
        write_heartbeat(ctx.settings, {"trigger": trigger, "pid": os.getpid(), "state": "idle",
                                       "ran": [{"job": r["job"], "status": r["status"]} for r in ran]})
    finally:
        lock.release()
    return {"status": "ok", "ran": ran}
