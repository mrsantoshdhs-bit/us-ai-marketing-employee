"""Settings: environment variables (.env) plus TOML business and schedule config."""

from __future__ import annotations

import copy
import logging
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent.parent
log = logging.getLogger("aime.config")

DEFAULT_BUSINESS: dict[str, Any] = {
    "company": {"name": "Your Company", "website": "", "tagline": ""},
    "targeting": {"country": "US", "states": "ALL", "industries": [], "services": []},
    "content": {
        "posts_per_week": 3,
        "posting_days": ["Tue", "Wed", "Thu"],
        "posting_time": "09:30",
        "timezone": "America/New_York",
        "queue_horizon_days": 14,
        "max_pending_items": 9,
        "max_posts_per_generation_run": 3,
        "max_hashtags": 5,
        "min_words": 70,
        "max_words": 260,
    },
    "research": {
        "max_new_companies_per_run": 40,
        "states_per_run": 5,
        "industries_per_run": 2,
        "cache_hours": 12,
        "hn_threads": 2,
        "website_checks_per_run": 25,
    },
    "qualification": {"grade_a": 75, "grade_b": 55, "grade_c": 35},
    "publishing": {"max_posts_per_run": 1, "max_overdue_hours": 24},
    "followup": {
        "max_touches": 3,
        "intervals_days": [0, 3, 7],
        "send_window_start": "08:00",
        "send_window_end": "18:00",
        "send_window_timezone": "America/New_York",
        "stale_lead_days": 30,
    },
}

DEFAULT_SCHEDULE: dict[str, Any] = {"tick_minutes": 15, "timezone": "local", "jobs": {}}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _to_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Maps dataclass field -> (ENV_NAME, type)
_ENV_MAP: dict[str, tuple[str, type]] = {
    "llm_provider": ("LLM_PROVIDER", str),
    "ollama_url": ("OLLAMA_URL", str),
    "ollama_model": ("OLLAMA_MODEL", str),
    "llm_timeout": ("LLM_TIMEOUT_SECONDS", float),
    "ollama_autostart": ("OLLAMA_AUTOSTART", bool),
    "ollama_exe": ("OLLAMA_EXE", str),
    "alert_toast": ("ALERT_TOAST", bool),
    "linkedin_client_id": ("LINKEDIN_CLIENT_ID", str),
    "linkedin_client_secret": ("LINKEDIN_CLIENT_SECRET", str),
    "linkedin_redirect_uri": ("LINKEDIN_REDIRECT_URI", str),
    "linkedin_scopes": ("LINKEDIN_SCOPES", str),
    "linkedin_api_version": ("LINKEDIN_API_VERSION", str),
    "linkedin_author_urn": ("LINKEDIN_AUTHOR_URN", str),
    "publishing_enabled": ("PUBLISHING_ENABLED", bool),
    "require_human_approval": ("REQUIRE_HUMAN_APPROVAL", bool),
    "hn_source_enabled": ("HN_SOURCE_ENABLED", bool),
    "sec_user_agent": ("SEC_USER_AGENT", str),
    "census_api_key": ("CENSUS_API_KEY", str),
    "outbound_email_enabled": ("OUTBOUND_EMAIL_ENABLED", bool),
    "smtp_host": ("SMTP_HOST", str),
    "smtp_port": ("SMTP_PORT", int),
    "smtp_username": ("SMTP_USERNAME", str),
    "smtp_password": ("SMTP_PASSWORD", str),
    "smtp_from": ("SMTP_FROM", str),
    "smtp_use_tls": ("SMTP_USE_TLS", bool),
    "sender_name": ("SENDER_NAME", str),
    "company_postal_address": ("COMPANY_POSTAL_ADDRESS", str),
    "unsubscribe_email": ("UNSUBSCRIBE_EMAIL", str),
    "dashboard_host": ("DASHBOARD_HOST", str),
    "dashboard_port": ("DASHBOARD_PORT", int),
    "dashboard_auth": ("DASHBOARD_AUTH", bool),
    "inbound_api_token": ("INBOUND_API_TOKEN", str),
    "allowed_hosts": ("ALLOWED_HOSTS", str),
    "backup_offsite_dir": ("BACKUP_OFFSITE_DIR", str),
    "backup_passphrase": ("BACKUP_PASSPHRASE", str),
    "database_url": ("DATABASE_URL", str),
    "data_dir": ("AIME_DATA_DIR", Path),
    "data_role": ("AIME_DATA_ROLE", str),
}


@dataclass
class Settings:
    base_dir: Path = BASE_DIR
    data_dir: Path = BASE_DIR / "data"
    database_url: str = ""
    # production | demo | test. Stamped into each database; a mismatch refuses to start, so demo or
    # test data can never be written into the production CRM (and vice versa).
    data_role: str = "production"
    business: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_BUSINESS))
    schedule: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_SCHEDULE))

    llm_provider: str = "auto"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2:latest"
    llm_timeout: float = 180.0
    # Start a local `ollama serve` when the model is unreachable (local URL only). Off unless you enable it.
    ollama_autostart: bool = False
    ollama_exe: str = ""  # empty = %LOCALAPPDATA%\Programs\Ollama\ollama.exe
    # Windows desktop notification for each new warning/error alert (never in the test data role).
    alert_toast: bool = True

    linkedin_client_id: str = ""
    # Secrets use repr=False so printing or logging a Settings object can never show them.
    linkedin_client_secret: str = field(default="", repr=False)
    linkedin_redirect_uri: str = "http://127.0.0.1:8765/integrations/linkedin/callback"
    linkedin_scopes: str = "openid profile w_member_social"
    linkedin_api_version: str = "202609"
    linkedin_author_urn: str = ""
    publishing_enabled: bool = False
    # Read for compatibility only: emails and posts always need an approval record before they go out.
    require_human_approval: bool = True

    hn_source_enabled: bool = True
    sec_user_agent: str = ""
    census_api_key: str = field(default="", repr=False)

    outbound_email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_from: str = ""
    smtp_use_tls: bool = True
    sender_name: str = ""
    company_postal_address: str = ""
    unsubscribe_email: str = ""

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    # Sign-in required for every dashboard page and API (accounts: `aime user create`).
    dashboard_auth: bool = True
    inbound_api_token: str = field(default="", repr=False)
    # Host headers the dashboard answers to (blocks DNS-rebinding). Add your domain when behind a proxy.
    allowed_hosts: str = "127.0.0.1,localhost,::1,[::1]"

    # Encrypted off-machine backup copy (e.g. a OneDrive folder). Both empty = off.
    backup_offsite_dir: str = ""
    backup_passphrase: str = field(default="", repr=False)

    # Operational knobs (tests shrink these)
    retry_base_delay: float = 2.0
    polite_delay: float = 1.0  # pause between requests to third-party websites
    http_timeout: float = 20.0
    network_enabled: bool = True

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.data_dir / 'aime.db').as_posix()}"
        if not self.require_human_approval:
            log.warning("REQUIRE_HUMAN_APPROVAL=false is ignored: emails and posts always need an approval record")

    # ---- derived paths -------------------------------------------------
    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def imports_dir(self) -> Path:
        return self.data_dir / "imports"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def locks_dir(self) -> Path:
        return self.data_dir / "locks"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def is_production(self) -> bool:
        return self.data_role == "production"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.logs_dir,
            self.reports_dir,
            self.assets_dir,
            self.inbox_dir,
            self.inbox_dir / "processed",
            self.imports_dir,
            self.imports_dir / "processed",
            self.imports_dir / "linkedin_metrics",
            self.cache_dir,
            self.locks_dir,
            self.backups_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # ---- convenience accessors -----------------------------------------
    @property
    def company_name(self) -> str:
        return self.business["company"].get("name") or "Your Company"

    def section(self, name: str) -> dict:
        return self.business.get(name, {})

    @property
    def linkedin_configured(self) -> bool:
        return bool(self.linkedin_client_id and self.linkedin_client_secret)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.company_postal_address)

    @classmethod
    def load(cls, base_dir: Path | None = None, env_file: Path | None = None, **overrides: Any) -> "Settings":
        base = Path(base_dir) if base_dir else BASE_DIR
        env: dict[str, str | None] = {}
        env_path = env_file if env_file is not None else base / ".env"
        if env_path and Path(env_path).exists():
            env.update(dotenv_values(env_path))
        # Real environment variables win over .env
        env.update({k: v for k, v in os.environ.items() if k in {e for e, _ in _ENV_MAP.values()}})

        kwargs: dict[str, Any] = {"base_dir": base, "data_dir": base / "data"}
        for attr, (env_name, typ) in _ENV_MAP.items():
            raw = env.get(env_name)
            if raw is None or raw == "":
                continue
            if typ is bool:
                kwargs[attr] = _to_bool(raw)
            elif typ is Path:
                kwargs[attr] = Path(raw)
            else:
                kwargs[attr] = typ(raw)

        kwargs["business"] = _deep_merge(DEFAULT_BUSINESS, _load_toml(base / "config" / "business.toml"))
        kwargs["schedule"] = _deep_merge(DEFAULT_SCHEDULE, _load_toml(base / "config" / "schedule.toml"))

        valid = {f.name for f in fields(cls)}
        for key, value in overrides.items():
            if key not in valid:
                raise TypeError(f"Unknown setting: {key}")
            kwargs[key] = value
        return cls(**kwargs)
