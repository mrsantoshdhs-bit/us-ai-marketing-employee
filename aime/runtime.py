"""Per-execution context shared across modules without passing it through every call."""

from __future__ import annotations

from contextvars import ContextVar

# Set by the scheduler while a job runs; agent runs record it so logs correlate job -> agents.
current_job_run_id: ContextVar[int | None] = ContextVar("current_job_run_id", default=None)
