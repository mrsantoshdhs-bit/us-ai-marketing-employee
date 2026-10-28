"""Authorized public data sources for company discovery."""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aime.context import AppContext

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")


def redact_personal(text: str) -> str:
    """Strip emails and phone numbers from public posts; we never harvest personal contact data."""
    return _PHONE_RE.sub("[phone removed]", _EMAIL_RE.sub("[email removed]", text))


@dataclass
class CompanyCandidate:
    name: str
    source: str
    source_url: str
    source_record_id: str | None = None
    website: str | None = None
    domain: str | None = None
    location_text: str | None = None
    state: str | None = None
    city: str | None = None
    is_us: bool | None = None
    industry: str | None = None
    description: str | None = None
    signal_text: str | None = None
    signal_type: str = "hiring"
    observed_at: datetime | None = None
    employee_range: str | None = None
    phone: str | None = None  # only from official business filings
    verified: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceStatus:
    name: str
    configured: bool
    message: str
    records: int = 0
    ok: bool = True


@dataclass
class ResearchFocus:
    industries: list[str]
    states: list[str]
    limit: int = 40


class Source(ABC):
    countries: tuple[str, ...] = ("US",)  # where this source finds companies ("*": any, e.g. your own list)
    name: str = "source"
    title: str = "Source"
    description: str = ""

    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self.settings = ctx.settings

    @abstractmethod
    def configured(self) -> tuple[bool, str]:
        """(is_configured, human-readable reason)."""

    @abstractmethod
    def fetch(self, focus: ResearchFocus) -> list[CompanyCandidate]: ...

    # Small JSON file cache so scheduled runs stay polite to public APIs.
    def _cache_path(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:120]
        return self.settings.cache_dir / f"{self.name}_{safe}.json"

    def cache_get(self, key: str, max_age_hours: float) -> Any | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > max_age_hours * 3600:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def cache_put(self, key: str, value: Any) -> None:
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
