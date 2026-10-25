"""Application context shared by agents, jobs, CLI, and the web app."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from aime import __version__
from aime.config import Settings
from aime.db import Database
from aime.logging_setup import setup_logging

if TYPE_CHECKING:
    from aime.llm.base import LLMProvider

USER_AGENT = f"aime/{__version__} (local marketing assistant)"

log = logging.getLogger("aime.context")


@dataclass
class AppContext:
    settings: Settings
    db: Database
    http_transport: httpx.BaseTransport | None = None
    _llm: "LLMProvider | None" = field(default=None, repr=False)
    _llm_checked: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def http(self, **kwargs) -> httpx.Client:
        headers = {"User-Agent": USER_AGENT}
        headers.update(kwargs.pop("headers", {}) or {})
        return httpx.Client(
            timeout=kwargs.pop("timeout", self.settings.http_timeout),
            headers=headers,
            transport=self.http_transport,
            follow_redirects=kwargs.pop("follow_redirects", True),
            **kwargs,
        )

    @property
    def llm(self) -> "LLMProvider | None":
        with self._lock:
            if not self._llm_checked:
                from aime.llm.factory import build_llm

                self._llm = build_llm(self)
                self._llm_checked = True
            return self._llm

    def reset_llm(self) -> None:
        with self._lock:
            self._llm = None
            self._llm_checked = False


def create_context(settings: Settings | None = None, http_transport: httpx.BaseTransport | None = None) -> AppContext:
    settings = settings or Settings.load()
    settings.ensure_dirs()
    setup_logging(settings.logs_dir)
    db = Database(settings.database_url)
    db.create_all()
    return AppContext(settings=settings, db=db, http_transport=http_transport)


_CONTEXT: AppContext | None = None
_CONTEXT_LOCK = threading.Lock()


def get_context() -> AppContext:
    global _CONTEXT
    with _CONTEXT_LOCK:
        if _CONTEXT is None:
            _CONTEXT = create_context()
        return _CONTEXT


def set_context(ctx: AppContext | None) -> None:
    global _CONTEXT
    with _CONTEXT_LOCK:
        _CONTEXT = ctx
