"""What the language model did for each agent run: calls, tokens (as the model reports them), time and failures.
Counted in memory while the agent works and saved with its run (agent_runs), so no extra database write happens in
the middle of an agent's transaction. Cost: the local model costs nothing per token; LLM_COST_PER_1K_TOKENS prices
a provider that charges (none is wired in)."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass
class Usage:
    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ms: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_current: contextvars.ContextVar[Usage | None] = contextvars.ContextVar("llm_usage", default=None)


def start() -> contextvars.Token:
    return _current.set(Usage())


def stop(token: contextvars.Token) -> Usage:
    usage = _current.get() or Usage()
    _current.reset(token)
    return usage


def record(prompt_tokens: int, completion_tokens: int, ms: int, ok: bool) -> None:
    usage = _current.get()
    if usage is None:  # a call outside an agent run (the llm-check command): nothing to attribute it to
        return
    usage.calls += 1
    usage.failures += 0 if ok else 1
    usage.prompt_tokens += max(0, int(prompt_tokens or 0))
    usage.completion_tokens += max(0, int(completion_tokens or 0))
    usage.ms += max(0, int(ms))
