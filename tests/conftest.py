from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aime.config import Settings
from aime.context import create_context, set_context
from aime.llm.base import LLMProvider
from aime.sample_data import SAMPLE_HN_ITEMS, SAMPLE_HN_THREAD

# Homepages for the fictional sample companies (reserved .example domains).
SAMPLE_SITES = {
    "northwind-analytics.example": "<html><head><title>Northwind Analytics | Revenue platform</title></head></html>",
    "acme-devshop.example": "<html><head><title>Acme Dev Shop</title></head></html>",
    "brightpath-realty.example": "<html><head><title>Brightpath Realty Group - Austin homes</title></head></html>",
    "pixelpine.example": "<html><head><title>Pixel &amp; Pine Agency</title></head></html>",
}


def hn_handler(request: httpx.Request) -> httpx.Response:
    """Mock for public APIs and sample websites used in tests (no network access during tests)."""
    host = request.url.host
    if host == "hn.algolia.com":
        path = request.url.path
        if path.endswith("/search_by_date"):
            return httpx.Response(200, json={"hits": [SAMPLE_HN_THREAD]})
        if "/items/" in path:
            return httpx.Response(200, json={"children": [{**i, "type": "comment"} for i in SAMPLE_HN_ITEMS]})
        if path.endswith("/search"):
            return httpx.Response(200, json={"hits": [
                {"objectID": "777001", "title": "Building reliable AI agents for CRM workflows", "points": 120}]})
    site = host.removeprefix("www.")
    if site in SAMPLE_SITES:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=SAMPLE_SITES[site], headers={"content-type": "text/html; charset=utf-8"})
    return httpx.Response(404, json={"error": "not mocked"})


class FakeLLM(LLMProvider):
    name = "fake"

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    @property
    def label(self) -> str:
        return "fake:test"

    def available(self) -> bool:
        return True

    def generate(self, prompt, system=None, max_tokens=500, temperature=0.7) -> str:
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


@pytest.fixture
def make_ctx(tmp_path):
    created = []

    def _make(handler=hn_handler, **overrides):
        base = dict(data_dir=tmp_path / f"data{len(created)}", database_url="", llm_provider="template",
                    retry_base_delay=0.0, polite_delay=0.0, data_role="test")
        base.update(overrides)
        settings = Settings.load(env_file=Path(tmp_path / "no.env"), **base)
        ctx = create_context(settings, http_transport=httpx.MockTransport(handler))
        set_context(ctx)
        created.append(ctx)
        return ctx

    yield _make
    set_context(None)
    for c in created:
        c.db.dispose()


@pytest.fixture
def ctx(make_ctx):
    return make_ctx()
