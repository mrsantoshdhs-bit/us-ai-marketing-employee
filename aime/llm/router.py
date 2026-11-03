"""Which model answers which AI task, from the `ai` settings section (settings.section("ai"), with the defaults below
when the section is absent, so this works before the settings control plane stores one).

    [ai]
    provider = "auto"            # auto (follow LLM_PROVIDER) | ollama | template | none
    model = ""                   # empty: OLLAMA_MODEL
    timeout_seconds = 0          # 0: LLM_TIMEOUT_SECONDS
    monthly_spend_cap_usd = 0.0  # 0: every paid provider is refused
    allow_remote_model_server = false   # an Ollama URL that is not this PC sends prospect data off it: refused
    [ai.tasks.reply_analysis]    # per-task overrides: provider, model, temperature, max_tokens
    model = "llama3.2:latest"

"template" and "none" both mean no model: the caller keeps its rule-based behaviour (none also records nothing as
AI). A paid cloud provider is not implemented: `cloud` resolves to CloudProvider, a disabled stub that refuses every
call unless the owner has approved it in the settings AND a spend cap above 0 is set, and even then refuses, because
no paid API is wired in (owner approval and a data-processing decision come first; see the roadmap's section Q)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aime.llm.base import LLMProvider, ProviderRefused
from aime.llm.ollama import OllamaProvider, is_local_url

AI_DEFAULTS: dict[str, Any] = {
    "provider": "auto",
    "model": "",
    "timeout_seconds": 0,
    "temperature": 0.0,
    "max_tokens": 600,
    "monthly_spend_cap_usd": 0.0,
    "allow_remote_model_server": False,
    "cloud": {"owner_approved": False, "vendor": "", "model": ""},
    "tasks": {},
    "policy": {},  # aime.ai.policy thresholds: auto_min_confidence, external_min_confidence
    "escalation": {},  # aime.ai.escalation: min_confidence, keywords
    "claims": [],  # extra approved claims (aime.ai.claims), in the owner's words
    "prohibited_topics": [],
}
PROVIDERS = ("auto", "ollama", "template", "none", "cloud")
NO_MODEL = ("template", "none")


def ai_config(settings) -> dict[str, Any]:
    """The `ai` section over the code defaults. A malformed section never stops the app: unknown values fall back."""
    try:
        raw = settings.section("ai") or {}
    except Exception:
        raw = {}
    cfg = {**AI_DEFAULTS, **{k: v for k, v in dict(raw).items() if k in AI_DEFAULTS}}
    cloud = raw.get("cloud")
    cfg["cloud"] = {**AI_DEFAULTS["cloud"], **(cloud if isinstance(cloud, dict) else {})}
    cfg["tasks"] = raw.get("tasks") if isinstance(raw.get("tasks"), dict) else {}
    if str(cfg["provider"]).lower() not in PROVIDERS:
        cfg["provider"] = "auto"
    try:
        cfg["monthly_spend_cap_usd"] = max(0.0, float(cfg["monthly_spend_cap_usd"] or 0))
    except (TypeError, ValueError):
        cfg["monthly_spend_cap_usd"] = 0.0
    return cfg


@dataclass(frozen=True)
class Route:
    task: str
    provider: str  # ollama | template | none | cloud
    model: str
    temperature: float
    max_tokens: int
    timeout: float
    why: str

    @property
    def uses_model(self) -> bool:
        return self.provider not in NO_MODEL


class CloudProvider(LLMProvider):
    """A placeholder for a paid hosted model. It refuses every call: none is implemented, and using one needs the
    owner's approval, a spend cap and a decision about prospect data leaving this PC."""

    name = "cloud"
    paid = True

    def __init__(self, vendor: str = "", model: str = "", reason: str = "no paid AI provider is implemented"):
        self.vendor, self.model, self.reason = vendor, model, reason

    def available(self) -> bool:
        return False

    def generate(self, prompt: str, system: str | None = None, max_tokens: int = 500, temperature: float = 0.7) -> str:
        raise ProviderRefused(f"Cloud model refused: {self.reason}")


class ModelRouter:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    def config(self) -> dict[str, Any]:
        return ai_config(self.settings)

    def route(self, task: str) -> Route:
        cfg = self.config()
        over = cfg["tasks"].get(task) if isinstance(cfg["tasks"].get(task), dict) else {}
        provider = str(over.get("provider") or cfg["provider"]).lower()
        why = f"task override for {task}" if over.get("provider") else "the ai settings"
        if provider not in PROVIDERS:
            provider, why = "auto", f"unknown provider {provider!r}: following LLM_PROVIDER"
        if provider == "auto":
            base = (getattr(self.settings, "llm_provider", "auto") or "auto").lower()
            provider = "template" if base == "template" else "ollama"
            why = f"LLM_PROVIDER={base}"
        model = str(over.get("model") or cfg["model"] or (cfg["cloud"].get("model") if provider == "cloud" else "")
                    or getattr(self.settings, "ollama_model", "") or "")
        timeout = float(over.get("timeout_seconds") or cfg["timeout_seconds"] or getattr(self.settings, "llm_timeout", 180.0))
        return Route(task=task, provider=provider, model=model if provider not in NO_MODEL else "",
                     temperature=float(over.get("temperature", cfg["temperature"])),
                     max_tokens=int(over.get("max_tokens", cfg["max_tokens"])), timeout=timeout, why=why)

    def provider_for(self, task: str) -> LLMProvider | None:
        """The provider for this task, or None when the task runs without a model. Raises ProviderRefused for a
        provider that may not be used (a paid one, or a model server that is not on this PC)."""
        r = self.route(task)
        cfg = self.config()
        if r.provider in NO_MODEL:
            return None
        if r.provider == "cloud":
            cap = cfg["monthly_spend_cap_usd"]
            if not cfg["cloud"].get("owner_approved"):
                raise ProviderRefused("Cloud model refused: the owner has not approved a paid AI provider")
            if cap <= 0:
                raise ProviderRefused("Cloud model refused: the monthly AI spend cap is 0")
            return CloudProvider(cfg["cloud"].get("vendor", ""), r.model)
        url = getattr(self.settings, "ollama_url", "http://127.0.0.1:11434")
        if not is_local_url(url) and not cfg["allow_remote_model_server"]:
            raise ProviderRefused(f"Model server refused: {url} is not this PC, and prospect text would leave it")
        return OllamaProvider(url, r.model, r.timeout, transport=self.transport)
