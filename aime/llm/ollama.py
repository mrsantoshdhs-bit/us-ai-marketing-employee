"""Local inference through Ollama (free, runs on this machine)."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from aime.llm import usage
from aime.llm.base import LLMError, LLMProvider

log = logging.getLogger("aime.llm")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_local_url(url: str) -> bool:
    return (urlparse(url).hostname or "") in LOCAL_HOSTS


def default_ollama_exe() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"


def start_local_server(provider: "OllamaProvider", exe: Path, wait: float = 30.0, popen=subprocess.Popen,
                       sleep=time.sleep) -> bool:
    """Start `ollama serve` in the background if the local server is down. Never stops or restarts one."""
    if not is_local_url(provider.url):
        return False
    if provider.available():
        return True
    if not exe.exists():
        log.warning("Ollama auto-start skipped: %s not found", exe)
        return False
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
    popen([str(exe), "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
          creationflags=flags, close_fds=True)
    log.warning("Ollama was not running; started %s serve", exe)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        sleep(1.0)
        if provider.available():
            return True
    return False


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, url: str, model: str, timeout: float = 180.0, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.transport = transport

    @property
    def label(self) -> str:
        return f"ollama:{self.model}"

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(timeout=timeout, transport=self.transport)

    def available(self) -> bool:
        try:
            with self._client(5.0) as client:
                resp = client.get(f"{self.url}/api/tags")
                resp.raise_for_status()
                names = {m.get("name") for m in resp.json().get("models", [])}
                base = self.model.split(":")[0]
                return self.model in names or any((n or "").split(":")[0] == base for n in names)
        except (httpx.HTTPError, ValueError, AttributeError):  # down, or answering with something else than JSON
            return False

    def generate(self, prompt: str, system: str | None = None, max_tokens: int = 500, temperature: float = 0.7) -> str:
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if system:
            body["system"] = system
        t0 = time.perf_counter()
        try:
            with self._client(self.timeout) as client:
                resp = client.post(f"{self.url}/api/generate", json=body)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError("the answer is not a JSON object")
                text = data.get("response") or ""
                if not isinstance(text, str):
                    raise ValueError("the answer has no text")
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:  # ValueError: a 200 that is not the expected JSON
            usage.record(0, 0, int((time.perf_counter() - t0) * 1000), ok=False)
            raise LLMError(f"Ollama request failed: {exc}"[:300]) from exc
        usage.record(data.get("prompt_eval_count") or 0, data.get("eval_count") or 0,
                     int((time.perf_counter() - t0) * 1000), ok=bool(text.strip()))
        if not text.strip():
            raise LLMError("Ollama returned an empty response")
        return text.strip()
