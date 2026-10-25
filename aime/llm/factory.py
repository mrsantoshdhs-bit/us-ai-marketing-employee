"""Choose an LLM provider. Paid APIs are intentionally not wired in."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aime.llm.base import LLMProvider
from aime.llm.ollama import OllamaProvider

if TYPE_CHECKING:
    from aime.context import AppContext

log = logging.getLogger("aime.llm")


def build_llm(ctx: "AppContext") -> LLMProvider | None:
    s = ctx.settings
    provider = (s.llm_provider or "auto").lower()
    if provider == "template":
        return None
    if provider in {"auto", "ollama"}:
        ollama = OllamaProvider(s.ollama_url, s.ollama_model, s.llm_timeout, transport=ctx.http_transport)
        if ollama.available():
            log.info("Using local LLM %s", ollama.label)
            return ollama
        level = logging.WARNING if provider == "ollama" else logging.INFO
        log.log(level, "Ollama not reachable at %s (model %s); using template writer", s.ollama_url, s.ollama_model)
        return None
    log.warning("Unknown LLM_PROVIDER=%s; using template writer", provider)
    return None
