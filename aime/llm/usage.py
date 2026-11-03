"""What the language model did for each agent run: calls, tokens (as the model reports them), time and failures.
Counted in memory while the agent works and saved with its run (agent_runs), so no extra database write happens in
the middle of an agent's transaction. Cost: the local model costs nothing per token; LLM_COST_PER_1K_TOKENS prices
a provider that charges (none is wired in).

Every call is recorded through the provider interface (aime.llm.base meters each provider's `generate`), so a call
counts whichever provider made it. A provider that reports exact token counts records them itself; for any other the
tokens are estimated and the call is counted as `estimated`. The last call's figures are also kept per thread
(`last_call`), inside an agent run or not, so the AI decision log can store each proposal's own tokens and time."""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass


@dataclass
class Usage:
    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ms: int = 0
    estimated: int = 0  # of the calls, the ones whose tokens were estimated, not reported by the model

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class Call:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ms: int = 0
    ok: bool = True
    estimated: bool = False

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_current: contextvars.ContextVar[Usage | None] = contextvars.ContextVar("llm_usage", default=None)
_local = threading.local()


def start() -> contextvars.Token:
    return _current.set(Usage())


def stop(token: contextvars.Token) -> Usage:
    usage = _current.get() or Usage()
    _current.reset(token)
    return usage


def calls_recorded() -> int:
    """How many calls this thread has recorded so far, in an agent run or not: the provider meter compares it before
    and after a call to see whether the provider counted the call itself."""
    return getattr(_local, "seq", 0)


def last_call() -> Call | None:
    return getattr(_local, "last", None)


def record(prompt_tokens: int, completion_tokens: int, ms: int, ok: bool, estimated: bool = False) -> None:
    call = Call(max(0, int(prompt_tokens or 0)), max(0, int(completion_tokens or 0)), max(0, int(ms)), bool(ok), bool(estimated))
    _local.seq = calls_recorded() + 1
    _local.last = call
    usage = _current.get()
    if usage is None:  # a call outside an agent run (the llm-check command): nothing to attribute it to
        return
    usage.calls += 1
    usage.failures += 0 if ok else 1
    usage.estimated += 1 if estimated else 0
    usage.prompt_tokens += call.prompt_tokens
    usage.completion_tokens += call.completion_tokens
    usage.ms += call.ms
