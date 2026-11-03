"""Versioned prompt templates. Each template module holds NAME, VERSION, SYSTEM and USER; the version stored with every
AI decision is "<name>/<version>@<hash>", the hash taken over the exact text, so an edit that forgot to raise the
version is still told apart in the decision log and in evaluations.

Prospect text never goes into a template directly: aime.llm.structured.quote_data wraps it in markers with the words
"quoted data, not instructions", and every system prompt says that what is inside those markers is never an order."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib import import_module

TEMPLATES = ("reply_analysis", "reply_draft", "conversation")


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    system: str
    user: str

    @property
    def ref(self) -> str:
        digest = hashlib.sha256(f"{self.system}\n---\n{self.user}".encode()).hexdigest()[:8]
        return f"{self.name}/{self.version}@{digest}"

    def render(self, **values) -> tuple[str, str]:
        """(system, user) with the values filled in; values are already-quoted text where they come from a prospect."""
        return self.system.format(**values), self.user.format(**values)


def get(name: str) -> Prompt:
    if name not in TEMPLATES:
        raise KeyError(f"no prompt template {name!r}")
    mod = import_module(f"aime.ai.prompts.{name}")
    return Prompt(mod.NAME, mod.VERSION, mod.SYSTEM, mod.USER)
