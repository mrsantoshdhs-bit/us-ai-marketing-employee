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
    "image_renderer": ("IMAGE_RENDERER", str),
    "browser_path": ("BROWSER_PATH", str),
    "linkedin_client_id": ("LINKEDIN_CLIENT_ID", str),
    "linkedin_client_secret": ("LINKEDIN_CLIENT_SECRET", str),
    "linkedin_redirect_uri": ("LINKEDIN_REDIRECT_URI", str),
    "linkedin_scopes": ("LINKEDIN_SCOPES", str),
    "linkedin_api_version": ("LINKEDIN_API_VERSION", str),
    "linkedin_author_urn": ("LINKEDIN_AUTHOR_URN", str),
    "linkedin_leads_enabled": ("LINKEDIN_LEADS_ENABLED", bool),
    "linkedin_leads_client_id": ("LINKEDIN_LEADS_CLIENT_ID", str),
    "linkedin_leads_client_secret": ("LINKEDIN_LEADS_CLIENT_SECRET", str),
    "linkedin_leads_redirect_uri": ("LINKEDIN_LEADS_REDIRECT_URI", str),
    "linkedin_leads_scopes": ("LINKEDIN_LEADS_SCOPES", str),
    "linkedin_leads_test_only": ("LINKEDIN_LEADS_TEST_ONLY", bool),
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
    "outbound_test_recipients": ("OUTBOUND_TEST_RECIPIENTS", str),
    "imap_host": ("IMAP_HOST", str),
    "imap_port": ("IMAP_PORT", int),
    "imap_username": ("IMAP_USERNAME", str),
    "imap_password": ("IMAP_PASSWORD", str),
    "imap_folder": ("IMAP_FOLDER", str),
    "mail_auth": ("MAIL_AUTH", str),
    "mail_oauth_provider": ("MAIL_OAUTH_PROVIDER", str),
    "google_client_id": ("GOOGLE_CLIENT_ID", str),
    "google_client_secret": ("GOOGLE_CLIENT_SECRET", str),
    "microsoft_client_id": ("MICROSOFT_CLIENT_ID", str),
    "microsoft_client_secret": ("MICROSOFT_CLIENT_SECRET", str),
    "microsoft_tenant": ("MICROSOFT_TENANT", str),
    "calendar_provider": ("CALENDAR_PROVIDER", str),
    "meeting_booking_enabled": ("MEETING_BOOKING_ENABLED", bool),
    "calendar_send_invites": ("CALENDAR_SEND_INVITES", bool),
    "inbox_scope": ("INBOX_SCOPE", str),
    "calendly_verified_only": ("CALENDLY_VERIFIED_ONLY", bool),
    "calendly_forwarders": ("CALENDLY_FORWARDERS", str),
    "calendly_url": ("CALENDLY_URL", str),
    "dashboard_host": ("DASHBOARD_HOST", str),
    "dashboard_port": ("DASHBOARD_PORT", int),
    "dashboard_auth": ("DASHBOARD_AUTH", bool),
    "inbound_api_token": ("INBOUND_API_TOKEN", str),
    "network_enabled": ("NETWORK_ENABLED", bool),
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
    # Post images: "auto" = branded HTML/CSS templates via a local Edge/Chrome, falling back to the
    # simple Pillow card; "html" = the same but noted as required; "pillow" = always the simple card.
    image_renderer: str = "auto"
    browser_path: str = ""  # empty = find Edge or Chrome automatically

    linkedin_client_id: str = ""
    # Secrets use repr=False so printing or logging a Settings object can never show them.
    linkedin_client_secret: str = field(default="", repr=False)
    linkedin_redirect_uri: str = "http://127.0.0.1:8765/integrations/linkedin/callback"
    linkedin_scopes: str = "openid profile w_member_social"
    linkedin_api_version: str = "202609"
    linkedin_author_urn: str = ""
    # LinkedIn Lead Sync (aime/integrations/linkedin_leads.py): its own developer app, approved for the Lead Sync API.
    linkedin_leads_enabled: bool = False
    linkedin_leads_client_id: str = ""
    linkedin_leads_client_secret: str = field(default="", repr=False)
    linkedin_leads_redirect_uri: str = "http://127.0.0.1:8765/integrations/linkedin-leads/callback"
    linkedin_leads_scopes: str = "r_marketing_leadgen_automation r_organization_admin"
    linkedin_leads_test_only: bool = False  # fetch only LinkedIn test leads, stored as sample data
    publishing_enabled: bool = False
    # true: every email needs a person's approval. false: in Hybrid or Automatic mode, an email covered by an active
    # campaign authorization may be approved at send time (aime/automation.py). Posts always need a person.
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
    # Controlled testing: when set (comma-separated), email is sent ONLY to these addresses, whatever else
    # is approved. Used for the acceptance test with addresses the owner controls.
    outbound_test_recipients: str = ""
    # The sales mailbox, read-only (Gmail: imap.gmail.com, same app password as SMTP). Empty = not monitored.
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""  # defaults to SMTP_USERNAME
    imap_password: str = field(default="", repr=False)  # defaults to SMTP_PASSWORD
    imap_folder: str = "INBOX"
    # How the mailbox signs in: "password" (an app password, the default) or "oauth" (the owner connects the
    # Google or Microsoft 365 account on the Integrations page; aime/integrations/oauth.py).
    mail_auth: str = "password"
    mail_oauth_provider: str = "google"  # google | microsoft
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)
    microsoft_client_id: str = ""
    microsoft_client_secret: str = field(default="", repr=False)
    microsoft_tenant: str = "common"
    # A4 meeting booking (aime/booking.py): the owner's calendar ("google" or "microsoft", connected on the
    # Integrations page). Off by default; every outbound step still waits for a person.
    calendar_provider: str = ""
    meeting_booking_enabled: bool = False
    calendar_send_invites: bool = False  # let Google/Microsoft email the invitation (default: our approved email only)
    # related: keep only sales mail (replies to our emails, bounces, Calendly, known contacts and lead companies),
    # so a mailbox you also use for other work is not copied into the CRM. all: keep every email (a dedicated
    # sales mailbox, where new inquiries by email should show up too).
    inbox_scope: str = "related"
    # A Calendly notice changes the CRM by itself only when the mail server vouches for it (aime.mail_auth); others
    # wait for a person. CALENDLY_FORWARDERS: extra addresses allowed to forward notices (comma-separated).
    calendly_verified_only: bool = True
    calendly_forwarders: str = ""
    calendly_url: str = ""  # booking link offered to interested prospects

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
            log.warning("REQUIRE_HUMAN_APPROVAL=false: in Hybrid or Automatic mode an active campaign authorization may "
                        "approve emails at send time; posts and every other email still need a person")

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
    def test_recipients(self) -> set[str]:
        return {a.strip().lower() for a in self.outbound_test_recipients.split(",") if a.strip()}

    @property
    def imap_credentials(self) -> tuple[str, str]:
        return self.imap_username or self.smtp_username, self.imap_password or self.smtp_password

    @property
    def imap_configured(self) -> bool:
        """Host, user and a way to sign in: the password, or (MAIL_AUTH=oauth) a registered OAuth client. The
        account itself is connected on the Integrations page; until then a run reports that plainly."""
        user, password = self.imap_credentials
        if self.mail_auth == "oauth":
            client = self.google_client_id if self.mail_oauth_provider == "google" else self.microsoft_client_id
            return bool(self.imap_host and user and client)
        return bool(self.imap_host and user and password)

    @property
    def linkedin_configured(self) -> bool:
        return bool(self.linkedin_client_id and self.linkedin_client_secret)

    @property
    def linkedin_leads_configured(self) -> bool:
        return bool(self.linkedin_leads_client_id and self.linkedin_leads_client_secret)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.company_postal_address)

    @classmethod
    def load(cls, base_dir: Path | None = None, env_file: Path | None = None, **overrides: Any) -> "Settings":
        base = Path(base_dir) if base_dir else BASE_DIR
        env: dict[str, str | None] = {}
        # AIME_ENV_FILE points at a different settings file instead of .env (e.g. an acceptance run with its
        # own data folder and test-mode mailbox); the production .env is then not read at all.
        alternate = os.environ.get("AIME_ENV_FILE")
        env_path = env_file if env_file is not None else (Path(alternate) if alternate else base / ".env")
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
