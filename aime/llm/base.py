"""LLM provider interface. Agents treat the LLM as optional and always keep a template fallback."""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMError(Exception):
    pass


class LLMProvider(ABC):
    name: str = "llm"

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def generate(self, prompt: str, system: str | None = None, max_tokens: int = 500, temperature: float = 0.7) -> str: ...

    @property
    def label(self) -> str:
        return self.name
