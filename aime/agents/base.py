"""Agent framework: typed input/output contracts, retries, execution logs, and metrics."""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from aime.context import AppContext
from aime.logging_setup import append_jsonl
from aime.models import AgentRun
from aime.redact import redact
from aime.runtime import current_job_run_id
from aime.timeutil import utcnow


class AgentError(Exception):
    """Non-retryable failure."""

    retryable = False


class TransientAgentError(AgentError):
    """Retryable failure (network hiccup, locked database, rate limit)."""

    retryable = True


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float | None = None  # None -> settings.retry_base_delay
    backoff: float = 2.0
    max_delay: float = 30.0
    retry_on: tuple[type[BaseException], ...] = (
        TransientAgentError,
        httpx.TransportError,
        httpx.TimeoutException,
        OperationalError,
    )

    def describe(self) -> str:
        names = ", ".join(e.__name__ for e in self.retry_on)
        return f"up to {self.max_attempts} attempts, exponential backoff x{self.backoff}, retry on: {names}"


InT = TypeVar("InT", bound=BaseModel)
OutT = TypeVar("OutT", bound=BaseModel)


class AgentResult(BaseModel, Generic[OutT]):
    agent: str
    run_id: str
    status: str  # success | failed
    attempts: int
    duration_ms: int
    output: OutT | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"


def summarize(value: Any, max_list: int = 15, max_str: int = 400, depth: int = 0) -> Any:
    """Shrink payloads before they go into execution logs."""
    if depth > 4:
        return "…"
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: summarize(v, max_list, max_str, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        items = [summarize(v, max_list, max_str, depth + 1) for v in list(value)[:max_list]]
        if len(value) > max_list:
            items.append(f"… {len(value) - max_list} more")
        return items
    if isinstance(value, str) and len(value) > max_str:
        return value[:max_str] + "…"
    return value


class BaseAgent(ABC, Generic[InT, OutT]):
    name: ClassVar[str]
    title: ClassVar[str]
    responsibilities: ClassVar[list[str]]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]
    retry_policy: ClassVar[RetryPolicy] = RetryPolicy()

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.settings = ctx.settings
        self.log = logging.getLogger(f"aime.agent.{self.name}")

    @abstractmethod
    def execute(self, inp: InT, session: Session) -> OutT:
        """Do the work inside one DB transaction. Raise TransientAgentError to request a retry."""

    # ------------------------------------------------------------------
    def run(self, payload: dict | BaseModel | None = None, *, job: str | None = None) -> AgentResult:
        run_id = uuid.uuid4().hex[:12]
        started = utcnow()
        t0 = time.perf_counter()

        try:
            if isinstance(payload, BaseModel):
                payload = payload.model_dump()
            inp = self.input_model.model_validate(payload or {})
        except ValidationError as exc:
            return self._finish(run_id, job, started, t0, 0, None, f"Invalid input: {exc}", payload or {}, record_new=True)

        record_id = self._record_start(run_id, job, started, inp)
        policy = self.retry_policy
        delay = policy.base_delay if policy.base_delay is not None else self.settings.retry_base_delay
        attempts, output, error = 0, None, None

        while attempts < policy.max_attempts:
            attempts += 1
            try:
                with self.ctx.db.session() as session:
                    raw = self.execute(inp, session)
                    output = self.output_model.model_validate(
                        raw.model_dump() if isinstance(raw, BaseModel) else raw
                    )
                error = None
                break
            except policy.retry_on as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.log.warning("%s attempt %s/%s failed (retryable): %s", self.name, attempts, policy.max_attempts, error)
                if attempts < policy.max_attempts and delay > 0:
                    time.sleep(min(policy.max_delay, delay * policy.backoff ** (attempts - 1)))
            except Exception as exc:  # non-retryable
                error = f"{type(exc).__name__}: {exc}"
                self.log.exception("%s failed (not retryable)", self.name)
                break

        return self._finish(run_id, job, started, t0, attempts, output, error, inp, record_id=record_id)

    # ------------------------------------------------------------------
    def _record_start(self, run_id: str, job: str | None, started, inp: BaseModel) -> int | None:
        try:
            with self.ctx.db.session() as s:
                row = AgentRun(
                    run_id=run_id, agent=self.name, job=job, job_run_id=current_job_run_id.get(),
                    status="running", started_at=started, input_summary=summarize(inp),
                )
                s.add(row)
                s.flush()
                return row.id
        except Exception:  # logging must never break the agent
            self.log.exception("Could not record agent start")
            return None

    def _finish(self, run_id, job, started, t0, attempts, output, error, inp, record_id=None, record_new=False) -> AgentResult:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        status = "success" if output is not None and error is None else "failed"
        error = redact(error)
        finished = utcnow()
        out_summary = summarize(output) if output is not None else {}
        try:
            with self.ctx.db.session() as s:
                row = s.get(AgentRun, record_id) if record_id else None
                if row is None:
                    row = AgentRun(run_id=run_id, agent=self.name, job=job, job_run_id=current_job_run_id.get(),
                                   started_at=started, input_summary=summarize(inp))
                    s.add(row)
                row.status = status
                row.attempts = attempts
                row.finished_at = finished
                row.duration_ms = duration_ms
                row.output_summary = out_summary if isinstance(out_summary, dict) else {"value": out_summary}
                row.error = error
        except Exception:
            self.log.exception("Could not record agent finish")
        append_jsonl(
            self.settings.logs_dir / "agent_runs.jsonl",
            {
                "ts": finished.isoformat() + "Z", "agent": self.name, "run_id": run_id, "job": job,
                "status": status, "attempts": attempts, "duration_ms": duration_ms, "error": error,
                "job_run_id": current_job_run_id.get(),
            },
        )
        level = logging.INFO if status == "success" else logging.ERROR
        self.log.log(level, "%s %s in %sms (attempts=%s)%s", self.name, status, duration_ms, attempts,
                     f" error={error}" if error else "")
        return AgentResult(agent=self.name, run_id=run_id, status=status, attempts=attempts,
                           duration_ms=duration_ms, output=output, error=error)

    # ------------------------------------------------------------------
    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "title": cls.title,
            "responsibilities": cls.responsibilities,
            "input": {k: str(v.annotation) for k, v in cls.input_model.model_fields.items()},
            "output": {k: str(v.annotation) for k, v in cls.output_model.model_fields.items()},
            "retry_policy": cls.retry_policy.describe(),
        }
