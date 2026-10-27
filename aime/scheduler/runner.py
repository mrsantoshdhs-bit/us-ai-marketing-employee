"""Scheduler core: decide which jobs are due, run them under locks, and record job history.

The OS scheduler (Windows Task Scheduler) only calls `tick` every few minutes; all timing
decisions live here, so missed runs (PC asleep/off) are caught up on the next tick.
A failed daily/weekly job is retried twice for the same slot, after 30 and then 60 minutes; a run that was
killed or crashed counts as a failure. Every job is idempotent or guarded by durable state, so retries never
duplicate external actions.
"""

from __future__ import annotations

import logging
import os
import time as _time
import traceback
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from aime import automation, maintenance
from aime.config import Settings
from aime.context import AppContext
from aime.models import AgentRun, JobRun, JobState
from aime.monitoring import alert_once, write_heartbeat
from aime.redact import redact
from aime.runtime import current_job_run_id
from aime.timeutil import as_aware_utc, local_tz, to_naive_utc, utcnow
from aime.workflows import JOBS

log = logging.getLogger("aime.scheduler")
RETRY_BACKOFF = timedelta(minutes=30)
MAX_DAILY_RETRIES = 3
ABANDON_AFTER = timedelta(hours=2)  # Task Scheduler kills a tick after 1 hour
ORPHAN_AFTER = timedelta(minutes=10)  # a 'running' row whose job lock has no live owner is abandoned after this
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class JobSpec:
    name: str
    every_minutes: int | None
    daily_at: time | None
    description: str
    enabled: bool = True
    weekdays: tuple[int, ...] | None = None  # set for weekly jobs (0 = Monday)

    @property
    def schedule_label(self) -> str:
        if self.every_minutes:
            return f"every {self.every_minutes} min"
        if self.weekdays:
            return f"weekly {'/'.join(WEEKDAYS[d] for d in self.weekdays)} {self.daily_at:%H:%M}"
        return f"daily {self.daily_at:%H:%M}"


def load_job_specs(settings: Settings) -> list[JobSpec]:
    cfg = settings.schedule.get("jobs", {})
    specs = []
    for name in JOBS:  # registry order = execution order within a tick
        jc = cfg.get(name)
        if jc is None:
            continue
        daily = time.fromisoformat(jc["daily_at"]) if jc.get("daily_at") else None
        every = int(jc["every_minutes"]) if jc.get("every_minutes") else None
        weekly = jc.get("weekly_on")
        weekdays = None
        if weekly:
            names = [weekly] if isinstance(weekly, str) else list(weekly)
            weekdays = tuple(sorted(WEEKDAYS.index(n[:3].title()) for n in names))
        if not daily and not every:
            continue
        specs.append(JobSpec(name, every, daily, jc.get("description", ""), bool(jc.get("enabled", True)), weekdays))
    return specs


def _last_slot(spec: JobSpec, local_now: datetime, tz) -> datetime:
    slot = datetime.combine(local_now.date(), spec.daily_at, tzinfo=tz)
    if local_now < slot:
        slot -= timedelta(days=1)
    if spec.weekdays:
        while slot.weekday() not in spec.weekdays:
            slot -= timedelta(days=1)
    return slot


def retry_backoff(consecutive_failures: int) -> timedelta:
    return RETRY_BACKOFF * (2 ** max(0, consecutive_failures - 1))


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
    last_slot = _last_slot(spec, as_aware_utc(now).astimezone(tz), tz)
    last_slot_utc = to_naive_utc(last_slot)
    if state and state.last_success_at and state.last_success_at >= last_slot_utc:
        return False, "already succeeded for the latest slot"
    if state and state.last_attempt_at and state.last_attempt_at >= last_slot_utc:
        if state.consecutive_failures >= MAX_DAILY_RETRIES:
            return False, "failed repeatedly; waiting for the next slot"
        wait = retry_backoff(state.consecutive_failures)
        if now - state.last_attempt_at < wait:
            return False, f"retry backoff ({int(wait.total_seconds() // 60)} min)"
        return True, f"retry {state.consecutive_failures + 1}/{MAX_DAILY_RETRIES} for slot {last_slot:%Y-%m-%d %H:%M}"
    return True, f"slot {last_slot:%Y-%m-%d %H:%M} pending"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX: signal 0 only checks existence
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class FileLock:
    """Cross-process lock file holding the owner's PID; recovered at once if the owner died."""

    def __init__(self, path: Path, stale_seconds: int = 7200):
        self.path = path
        self.stale_seconds = stale_seconds
        self.held = False

    def _owner_dead(self) -> bool:
        try:
            pid = int(self.path.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return False
        return pid != os.getpid() and not pid_alive(pid)

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
                if age > self.stale_seconds or self._owner_dead():
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


def _lock_owner_gone(ctx: AppContext, job: str) -> bool:
    path = ctx.settings.locks_dir / f"job_{job}.lock"
    try:
        pid = int(path.read_text(encoding="utf-8").split()[0])
    except FileNotFoundError:
        return True
    except (OSError, ValueError, IndexError):
        return False
    return pid != os.getpid() and not pid_alive(pid)


def recover(ctx: AppContext, now: datetime | None = None) -> dict:
    """Close out runs left 'running' by a crashed or killed process, and count each one as a failed attempt.

    A run is abandoned once its job lock has no live owner (after ORPHAN_AFTER), or after ABANDON_AFTER anyway.
    """
    now = now or utcnow()
    cutoff, orphaned = now - ABANDON_AFTER, now - ORPHAN_AFTER
    with ctx.db.session() as s:
        dead = [r for r in s.execute(select(JobRun).where(JobRun.status == "running")).scalars()
                if r.started_at < cutoff or (r.started_at < orphaned and _lock_owner_gone(ctx, r.job))]
        for r in dead:
            r.status, r.error = "abandoned", "Process ended before the job finished (crash, sleep, or kill)"
            st = s.get(JobState, r.job) or JobState(job=r.job, consecutive_failures=0, enabled=True)
            if st.last_attempt_at is None or st.last_attempt_at <= r.started_at:  # no later attempt recorded
                st.last_attempt_at, st.last_status = r.started_at, "abandoned"
                st.consecutive_failures = (st.consecutive_failures or 0) + 1
            s.add(st)
        ids = [r.id for r in dead]
        agents = s.execute(update(AgentRun).where(AgentRun.status == "running", (AgentRun.started_at < cutoff)
                                                  | AgentRun.job_run_id.in_(ids or [-1])).values(
            status="abandoned", error="Process ended before the agent finished")).rowcount
    return {"abandoned_jobs": len(dead), "abandoned_agent_runs": agents}


def _same_slot_failures(ctx: AppContext, name: str, state: JobState, started: datetime) -> int:
    """Failures so far for the current slot of a daily/weekly job (earlier days' failures don't count)."""
    spec = next((sp for sp in load_job_specs(ctx.settings) if sp.name == name), None)
    previous = state.consecutive_failures or 0
    if spec is None or spec.every_minutes or state.last_attempt_at is None:
        return previous
    tz = local_tz(ctx.settings.schedule.get("timezone"))
    slot = to_naive_utc(_last_slot(spec, as_aware_utc(started).astimezone(tz), tz))
    return previous if state.last_attempt_at >= slot else 0


def _record(ctx: AppContext, name: str, run_id: int | None, started: datetime, status: str, detail: dict,
            error: str | None, duration: int) -> None:
    finished = utcnow()
    with ctx.db.session() as s:
        if run_id is not None:
            row = s.get(JobRun, run_id)
            row.status, row.finished_at, row.duration_ms = status, finished, duration
            row.detail, row.error = detail, error
        st = s.get(JobState, name) or JobState(job=name, consecutive_failures=0, enabled=True)
        failures = _same_slot_failures(ctx, name, st, started)
        st.last_attempt_at = started
        st.last_status = status
        if status == "success":
            st.last_success_at = started
            st.consecutive_failures = 0
        else:
            st.consecutive_failures = failures + 1
        s.add(st)
        budget = float(ctx.settings.schedule.get("job_budget_minutes", 30))
        if duration > budget * 60_000:  # warn only; a running job is never killed
            alert_once(s, "warning", "monitor", f"Job '{name}' took {duration // 60_000} min "
                       f"(budget {budget:g} min); check the run log for a slow source or model", ctx.settings)


def run_job(ctx: AppContext, name: str, trigger: str = "manual", *, allow_during_maintenance: bool = False,
            recover_runs: bool = True) -> dict:
    if name not in JOBS:
        raise KeyError(f"Unknown job '{name}'. Known: {', '.join(JOBS)}")
    window = maintenance.active(ctx.settings.data_dir)
    if window and not allow_during_maintenance:
        return {"job": name, "status": "skipped", "reason": f"maintenance window until {window['until']}"}
    if name not in automation.ALWAYS_RUN_JOBS:
        with ctx.db.session() as s:
            state = automation.get_state(s)
        if state in ("stopped", "emergency_stop"):
            return {"job": name, "status": "skipped",
                    "reason": f"automation {state.replace('_', ' ')} in the Automation Control Center"}
    if recover_runs:  # manual runs close out crashed runs too, not only scheduled ticks
        recover(ctx)
    lock = FileLock(ctx.settings.locks_dir / f"job_{name}.lock")
    if not lock.acquire():
        return {"job": name, "status": "skipped", "reason": "already running"}
    ctx.reset_llm()  # pick up a local model that started (or stopped) since this process first looked
    started = utcnow()
    t0 = _time.perf_counter()
    status, detail, error, run_id = "failed", {}, None, None
    try:
        with ctx.db.session() as s:
            row = JobRun(job=name, trigger=trigger, status="running", started_at=started)
            s.add(row)
            s.flush()
            run_id = row.id
        token = current_job_run_id.set(run_id)
        try:
            outcome = JOBS[name](ctx, job=name)
            detail = outcome.detail()
            if not outcome.failed:
                status = "success"
            elif any(st.get("status") == "success" for st in outcome.steps):
                status = "partial"
            error = "; ".join(f"{st['agent']}: {st.get('error')}" for st in outcome.steps
                              if st.get("status") != "success") or None
        finally:
            current_job_run_id.reset(token)
    except Exception as exc:  # keep the scheduler alive no matter what
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
    finally:
        lock.release()
    duration = int((_time.perf_counter() - t0) * 1000)
    error = redact(error[:4000]) if error else None
    for attempt in range(3):  # "database is locked" while recording must not lose the result
        try:
            _record(ctx, name, run_id, started, status, detail, error, duration)
            break
        except OperationalError:
            if attempt == 2:
                raise
            _time.sleep(2)
    return {"job": name, "status": status, "duration_ms": duration, "error": error, "run_id": run_id}


def due_jobs(ctx: AppContext, now: datetime | None = None) -> list[tuple[JobSpec, bool, str]]:
    now = now or utcnow()
    tz = local_tz(ctx.settings.schedule.get("timezone"))
    tick = int(ctx.settings.schedule.get("tick_minutes", 15))
    with ctx.db.session() as s:
        states = {st.job: st for st in s.execute(select(JobState)).scalars()}
        return [(spec, *is_due(spec, states.get(spec.name), now, tz, tick)) for spec in load_job_specs(ctx.settings)]


def tick(ctx: AppContext, trigger: str = "scheduler", now: datetime | None = None) -> dict:
    window = maintenance.active(ctx.settings.data_dir)
    if window:
        write_heartbeat(ctx.settings, {"trigger": trigger, "pid": os.getpid(), "state": "maintenance",
                                       "maintenance": window})
        return {"status": "skipped", "reason": f"maintenance window until {window['until']}", "ran": []}
    lock = FileLock(ctx.settings.locks_dir / "tick.lock", stale_seconds=5400)
    if not lock.acquire():
        return {"status": "skipped", "reason": "another tick is running", "ran": []}
    ran: list[dict] = []
    try:
        write_heartbeat(ctx.settings, {"trigger": trigger, "pid": os.getpid(), "state": "running"})
        recovered = recover(ctx, now)
        for spec, due, reason in due_jobs(ctx, now):
            if due:
                write_heartbeat(ctx.settings, {"trigger": trigger, "pid": os.getpid(), "state": "running",
                                               "job": spec.name})
                try:
                    result = run_job(ctx, spec.name, trigger=trigger, recover_runs=False)
                except Exception as exc:  # one job's bookkeeping failure must not skip the rest of the tick
                    log.exception("Job %s could not be recorded", spec.name)
                    result = {"job": spec.name, "status": "failed", "error": redact(f"{type(exc).__name__}: {exc}")}
                result["reason"] = reason
                ran.append(result)
        write_heartbeat(ctx.settings, {"trigger": trigger, "pid": os.getpid(), "state": "idle", "recovered": recovered,
                                       "ran": [{"job": r["job"], "status": r["status"]} for r in ran]})
    finally:
        lock.release()
    return {"status": "ok", "ran": ran}
