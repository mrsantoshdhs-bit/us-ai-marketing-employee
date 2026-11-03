"""LLM provider interface. Agents treat the LLM as optional and always keep a template fallback.

Every provider is metered here, at the interface, not inside each provider: a subclass's `generate` (and
`generate_json`) is wrapped when the class is defined, so a call made by any agent, through any provider, is counted
in the agent run's usage (aime.llm.usage). A provider that knows its exact token counts (Ollama reports them) records
them itself and the wrapper sees that and adds nothing; any other provider is counted with an estimate of about four
characters a token, marked as estimated. Before this only OllamaProvider counted its own calls, so a model that is not
Ollama, or a wrapper around one, left agent_runs.llm_calls at 0 however much it wrote.

Beyond plain text, a provider can answer in a typed form (`generate_structured`: a pydantic model, validated, with one
repair attempt and then a typed StructuredOutputError) and run a short tool loop (`run_with_tools`) over a registry of
Python callables; both live in aime.llm.structured and work with any provider. The model only ever proposes: no tool it
can reach sends, books, publishes or changes a setting (aime.ai.tools)."""

from __future__ import annotations

import functools
import json
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from aime.llm import usage

if TYPE_CHECKING:
    from pydantic import BaseModel


class LLMError(Exception):
    pass


class StructuredOutputError(LLMError):
    """The model did not answer in the asked form, even after one repair attempt. `raw` is its last answer, `problems`
    what was wrong with it, `attempts` how many calls were made."""

    def __init__(self, message: str, raw: str = "", problems: list[str] | None = None, attempts: int = 0):
        super().__init__(message)
        self.raw = raw
        self.problems = list(problems or [])
        self.attempts = attempts


class ProviderRefused(LLMError):
    """A provider that may not be used: a paid one without the owner's approval and a spend allowance, a model server
    that is not on this PC, or one switched off in the `ai` settings."""


def estimate_tokens(*texts: str | None) -> int:
    return sum(len(t) for t in texts if isinstance(t, str)) // 4


def _metered(fn):
    @functools.wraps(fn)
    def wrapper(self, prompt, *args, **kwargs):
        before = usage.calls_recorded()
        t0 = time.perf_counter()
        system = kwargs.get("system")
        try:
            text = fn(self, prompt, *args, **kwargs)
        except Exception:
            if usage.calls_recorded() == before:
                usage.record(estimate_tokens(prompt, system), 0, int((time.perf_counter() - t0) * 1000), ok=False,
                             estimated=True)
            raise
        if usage.calls_recorded() == before:  # the provider did not count this call itself
            said = text if isinstance(text, str) else ""
            usage.record(estimate_tokens(prompt, system), estimate_tokens(said), int((time.perf_counter() - t0) * 1000),
                         ok=bool(said.strip()), estimated=True)
        return text

    wrapper.__aime_metered__ = True
    return wrapper


class LLMProvider(ABC):
    name: str = "llm"
    paid: bool = False  # charges per token: usable only through the router, with owner approval and a spend cap

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for attr in ("generate", "generate_json"):
            fn = cls.__dict__.get(attr)
            if callable(fn) and not getattr(fn, "__aime_metered__", False) and not getattr(fn, "__isabstractmethod__", False):
                setattr(cls, attr, _metered(fn))

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def generate(self, prompt: str, system: str | None = None, max_tokens: int = 500, temperature: float = 0.7) -> str: ...

    @property
    def label(self) -> str:
        return self.name

    @property
    def model_name(self) -> str:
        return str(getattr(self, "model", None) or self.name)

    def generate_json(self, prompt: str, schema: dict[str, Any], system: str | None = None, max_tokens: int = 600,
                      temperature: float = 0.0) -> str:
        """An answer meant to be one JSON object following `schema`. A provider with constrained decoding (Ollama's
        `format`) overrides this; the default asks for it in words and relies on the validation and repair in
        aime.llm.structured."""
        ask = (f"{prompt}\n\nAnswer with one JSON object only, no other text, following this JSON schema:\n"
               f"{json.dumps(schema, separators=(',', ':'))}")
        return self.generate(ask, system=system, max_tokens=max_tokens, temperature=temperature)

    def generate_structured(self, prompt: str, schema: "type[BaseModel]", system: str | None = None, max_tokens: int = 600,
                            temperature: float = 0.0, repair: bool = True) -> "BaseModel":
        """A validated instance of `schema`, or StructuredOutputError after one repair attempt."""
        from aime.llm.structured import structured_call

        return structured_call(self, prompt, schema, system=system, max_tokens=max_tokens, temperature=temperature,
                               repair=repair).value

    def run_with_tools(self, prompt: str, registry, system: str | None = None, final_schema: "type[BaseModel] | None" = None,
                       max_steps: int = 4):
        """A short loop in which the model may call tools from `registry` (aime.ai.tools.ToolRegistry) before its final
        answer. Tool results go back to the model as quoted data, never as instructions."""
        from aime.llm.structured import run_with_tools

        return run_with_tools(self, prompt, registry, system=system, final_schema=final_schema, max_steps=max_steps)
