"""The settings control plane (Option B, Phase 1): typed, versioned settings sections a Super Admin changes from the
dashboard, laid over the defaults from code and config/*.toml.

Precedence, highest first:
1. the safety floor in code (suppression, opt-out, the compliance footer, the pricing block);
2. the .env master switches REQUIRE_HUMAN_APPROVAL, OUTBOUND_EMAIL_ENABLED and PUBLISHING_ENABLED, which stay env-only and are
   in no section here (a section that names them is refused, as every unknown key is);
3. the active saved version of a section (table settings_versions);
4. the defaults: config/business.toml and config/schedule.toml with the sales-settings overrides (aime.admin_settings), and
   for the new sections the values the code used before they existed.

Each section is a pydantic model with safe bounds and no unknown keys. A save validates the whole section, writes it as a new
version, makes that the only active one, puts the change in the audit log with the old and new values, and applies it at
once: `settings.section(name)` (aime.config) returns the active version, so the agents read it with no change of their own.
Nothing is saved until something changes, `preview` shows what a save would change without writing, and `revert` restores
any earlier version by saving a copy of it, validated again by today's rules. With no saved version every section reads
exactly what the code read before, so a fresh install or a database from before Option B behaves as it always did.

The sales settings (aime.admin_settings) keep their own store (kv "admin.business_overrides"); each of their saves and
resets is also recorded here as a version of section "sales", so they can be compared and restored the same way.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

log = logging.getLogger("aime.settings_store")

SCHEMA_VERSION = 1
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
KEY = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
# The env-only master switches. No section may carry a key that looks like one, whatever its model says.
ENV_ONLY = re.compile(r"require_human_approval|outbound_email_enabled|publishing_enabled", re.I)


# --------------------------------------------------------------------------- field checks

def _zone(value: str) -> str:
    try:
        ZoneInfo(str(value).strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("must be a time zone such as America/New_York or Asia/Kolkata") from exc
    return str(value).strip()


def _clock(value: str) -> str:
    if not HHMM.match(str(value).strip()):
        raise ValueError("must look like 08:00")
    return str(value).strip()


def _code(value: str) -> str:
    code = str(value).strip().upper()
    if not COUNTRY_CODE.match(code):
        raise ValueError("must be a two-letter country code such as US")
    return code


class Strict(BaseModel):
    """Unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- countries

class Country(Strict):
    code: str
    enabled: bool = False
    name: str = Field(min_length=1, max_length=80)
    timezone: str
    send_window_start: str = "08:00"
    send_window_end: str = "18:00"
    holidays: list[str] = Field(default_factory=list, max_length=40)
    compliance_regime: str = Field(min_length=1, max_length=80)
    language: str = "en"
    legal_review_confirmed_by: str | None = Field(default=None, max_length=80)
    legal_review_confirmed_at: datetime | None = None
    legal_review_note: str | None = Field(default=None, max_length=300)

    @field_validator("code")
    @classmethod
    def check_code(cls, v: str) -> str:
        return _code(v)

    @field_validator("timezone")
    @classmethod
    def check_zone(cls, v: str) -> str:
        return _zone(v)

    @field_validator("send_window_start", "send_window_end")
    @classmethod
    def check_clock(cls, v: str) -> str:
        return _clock(v)

    @field_validator("language")
    @classmethod
    def check_language(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", v.strip()):
            raise ValueError("must be a language code such as en or en-GB")
        return v.strip()

    @field_validator("holidays")
    @classmethod
    def check_holidays(cls, v: list[str]) -> list[str]:
        from aime.calendar_slots import us_holidays

        names = set(us_holidays(2026))
        out = []
        for item in v:
            item = str(item).strip()
            if not item:
                continue
            if item not in names:
                try:
                    date.fromisoformat(item)
                except ValueError as exc:
                    raise ValueError(f"{item!r} is neither a known holiday name ({', '.join(sorted(names))}) nor a date such as "
                                     "2026-12-24") from exc
            out.append(item)
        return out

    @model_validator(mode="after")
    def check_window(self) -> "Country":
        if self.send_window_start >= self.send_window_end:
            raise ValueError(f"{self.code}: the send window must start before it ends")
        return self

    @property
    def legal_review_recorded(self) -> bool:
        return bool(self.legal_review_confirmed_by and self.legal_review_confirmed_at)


class CountriesSection(Strict):
    countries: list[Country] = Field(min_length=1, max_length=60)

    @model_validator(mode="after")
    def check_rules(self) -> "CountriesSection":
        codes = [c.code for c in self.countries]
        if len(set(codes)) != len(codes):
            raise ValueError("each country may be listed once")
        if not any(c.enabled for c in self.countries):
            raise ValueError("at least one country must be enabled")
        unreviewed = [c.code for c in self.countries if c.enabled and c.code != "US" and not c.legal_review_recorded]
        if unreviewed:  # the owner-gated rule: another country's email-marketing law is not CAN-SPAM
            raise ValueError(f"{', '.join(unreviewed)} cannot be enabled: no legal review of its email-marketing rules is recorded "
                             "(record it on the Countries page first)")
        return self


# --------------------------------------------------------------------------- targets

class Target(Strict):
    country: str
    daily_verified_leads: int = Field(default=0, ge=0, le=10000)
    daily_contactable_leads: int = Field(default=0, ge=0, le=10000)

    @field_validator("country")
    @classmethod
    def check_code(cls, v: str) -> str:
        return _code(v)


class TargetsSection(Strict):
    """Goals, never quotas: no cap, limit or quality rule is changed to reach them (aime.targets)."""

    targets: list[Target] = Field(default_factory=list, max_length=60)

    @model_validator(mode="after")
    def check_unique(self) -> "TargetsSection":
        codes = [t.country for t in self.targets]
        if len(set(codes)) != len(codes):
            raise ValueError("one target per country")
        return self


# --------------------------------------------------------------------------- catalogue

class CatalogueEntry(Strict):
    key: str
    label: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    builtin: bool = False
    description: str = Field(default="", max_length=300)

    @field_validator("key")
    @classmethod
    def check_key(cls, v: str) -> str:
        if not KEY.match(v.strip()):
            raise ValueError("must be lower case letters, digits and _ (2-40), starting with a letter")
        return v.strip()


class CatalogueSection(Strict):
    industries: list[CatalogueEntry] = Field(min_length=1, max_length=100)
    services: list[CatalogueEntry] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_rules(self) -> "CatalogueSection":
        from aime.knowledge.industries import INDUSTRIES
        from aime.knowledge.services import SERVICES

        for kind, entries, builtins in (("industry", self.industries, INDUSTRIES), ("service", self.services, SERVICES)):
            keys = [e.key for e in entries]
            if len(set(keys)) != len(keys):
                raise ValueError(f"each {kind} key may be listed once")
            missing = sorted(set(builtins) - set(keys))
            if missing:  # existing keys keep working: a built-in entry can be switched off, never removed
                raise ValueError(f"built-in {kind} entries cannot be removed, only disabled: {', '.join(missing)}")
            fake = [e.key for e in entries if e.builtin and e.key not in builtins]
            if fake:
                raise ValueError(f"only the code's own entries are built in: {', '.join(fake)}")
            for e in entries:  # the flag follows the key, whatever was sent
                e.builtin = e.key in builtins
            if not any(e.enabled for e in entries):
                raise ValueError(f"at least one {kind} must stay enabled")
        return self


# --------------------------------------------------------------------------- lead sources

class SourceSetting(Strict):
    name: str
    enabled: bool = True
    priority: int = Field(default=50, ge=1, le=100)  # 1 is read first; its companies come first within the per-run cap
    countries: list[str] = Field(default_factory=list, max_length=60)  # where it is used; within what the source covers

    @field_validator("countries")
    @classmethod
    def check_countries(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys("*" if str(c).strip() == "*" else _code(c) for c in v if str(c).strip()))


class SourcesSection(Strict):
    sources: list[SourceSetting] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def check_rules(self) -> "SourcesSection":
        from aime.sources.registry import SOURCE_CLASSES

        names = [s.name for s in self.sources]
        if len(set(names)) != len(names):
            raise ValueError("each source may be listed once")
        unknown = [n for n in names if n not in SOURCE_CLASSES]
        if unknown:  # only the existing, authorized sources: a new source is a code change
            raise ValueError(f"unknown source(s): {', '.join(unknown)}")
        for s in self.sources:
            covers = tuple(getattr(SOURCE_CLASSES[s.name], "countries", ("US",)))
            if not s.countries:
                raise ValueError(f"{s.name}: give the countries it is used for")
            if "*" not in covers:
                beyond = [c for c in s.countries if c not in covers]
                if beyond:
                    raise ValueError(f"{s.name} finds {', '.join(covers)} companies only; it cannot be used for {', '.join(beyond)}")
        return self


# --------------------------------------------------------------------------- schedules

class JobSchedule(Strict):
    enabled: bool = True
    every_minutes: int | None = Field(default=None, ge=15, le=1440)
    daily_at: str | None = None

    @field_validator("daily_at")
    @classmethod
    def check_daily(cls, v: str | None) -> str | None:
        return None if v in (None, "") else _clock(v)


class SchedulesSection(Strict):
    jobs: dict[str, JobSchedule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_rules(self) -> "SchedulesSection":
        from aime.automation import ALWAYS_RUN_JOBS
        from aime.workflows import JOBS

        unknown = [j for j in self.jobs if j not in JOBS]
        if unknown:
            raise ValueError(f"unknown job(s): {', '.join(unknown)}")
        off = [j for j in ALWAYS_RUN_JOBS if j in self.jobs and not self.jobs[j].enabled]
        if off:  # health and backups keep running whatever else is switched off
            raise ValueError(f"{', '.join(off)} cannot be disabled")
        return self


# --------------------------------------------------------------------------- agents (data and display; aime.agent_matrix)

class AgentPolicy(Strict):
    responsibilities: list[str] = Field(default_factory=list, max_length=30)
    allowed_tools: list[str] = Field(default_factory=list, max_length=40)
    prohibited_actions: list[str] = Field(default_factory=list, max_length=40)


class AgentsSection(Strict):
    agents: dict[str, AgentPolicy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_known(self) -> "AgentsSection":
        from aime.agents import AGENTS

        unknown = [a for a in self.agents if a not in AGENTS]
        if unknown:
            raise ValueError(f"unknown agent(s): {', '.join(unknown)}")
        return self


# --------------------------------------------------------------------------- read by other teams later (no behaviour now)

class DeliverabilitySection(Strict):
    """The keys aime.deliverability reads, with its own bounds: a pause always needs a real sample, waits stay far below
    the send run's own checks, and every cap here can only lower what the outreach limits already allow."""

    auto_pause: bool = True
    bounce_window_days: int = Field(default=7, ge=1, le=90)
    bounce_rate_pause: float = Field(default=0.05, ge=0.005, le=0.5)  # a share: 0.05 is 5%
    min_sends: int = Field(default=20, ge=10, le=1000)  # never below the module's sample floor
    min_bounces: int = Field(default=3, ge=1, le=100)
    complaint_window_days: int = Field(default=30, ge=1, le=180)
    complaint_rate_pause: float = Field(default=0.003, ge=0.0005, le=0.5)
    min_complaints: int = Field(default=2, ge=1, le=100)
    auth_gate: Literal["off", "warn", "block"] = "warn"
    auth_refresh_hours: int = Field(default=24, ge=1, le=168)
    auth_max_age_hours: int = Field(default=48, ge=1, le=336)
    mailbox_caps: dict[str, int] = Field(default_factory=dict)  # address -> emails a day, only ever lower than the global cap
    hourly_cap: int = Field(default=0, ge=0, le=1000)  # 0: none
    jitter_enabled: bool = False
    jitter_min_seconds: float = Field(default=20, ge=0, le=120)
    jitter_max_seconds: float = Field(default=90, ge=0, le=120)
    jitter_run_budget_seconds: float = Field(default=600, ge=0, le=900)
    warmup_enabled: bool = False
    warmup_schedule: list[list[int]] = Field(default_factory=lambda: [[1, 5], [4, 10], [8, 20], [15, 35], [22, 50]],
                                             max_length=60)  # [day, emails a day] steps
    warmup_start: dict[str, str] = Field(default_factory=dict)  # address -> first day of its warm-up (YYYY-MM-DD)
    one_click_unsubscribe_url: str = Field(default="", max_length=300)  # https only; empty keeps the mailto header alone

    @field_validator("mailbox_caps")
    @classmethod
    def check_caps(cls, v: dict[str, int]) -> dict[str, int]:
        if any(not 0 <= int(x) <= 10_000 for x in v.values()):
            raise ValueError("a mailbox cap must be 0 to 10000 emails a day")
        return {str(k).strip().lower(): int(x) for k, x in v.items()}

    @field_validator("warmup_schedule")
    @classmethod
    def check_schedule(cls, v: list[list[int]]) -> list[list[int]]:
        if any(len(step) != 2 or int(step[0]) < 1 or int(step[1]) < 0 for step in v):
            raise ValueError("each warm-up step is [day from 1, emails a day from 0]")
        return [[int(a), int(b)] for a, b in v]

    @field_validator("one_click_unsubscribe_url")
    @classmethod
    def check_url(cls, v: str) -> str:
        from aime.deliverability import HTTPS_URL

        if v and not HTTPS_URL.match(v):
            raise ValueError("the one-click unsubscribe address must be a plain https address")
        return v


class AISection(Strict):
    """Read by the AI core later. LLM_PROVIDER in .env still decides today."""

    provider: Literal["auto", "template", "ollama", "none"] = "auto"  # "cloud" is not offered here: a paid model is an owner decision
    model: str = Field(default="", max_length=80)
    timeout_seconds: float = Field(default=0, ge=0, le=600)  # 0: LLM_TIMEOUT from .env
    temperature: float = Field(default=0.0, ge=0, le=1.5)
    max_tokens: int = Field(default=600, ge=50, le=4000)
    monthly_spend_cap_usd: float = Field(default=0.0, ge=0, le=1000)  # 0: no paid model; anything above is an owner decision


class RetentionSection(Strict):
    """How long records are kept, in days; 0 keeps everything (today's behaviour). Nothing deletes by these yet."""

    job_runs_days: int = Field(default=0, ge=0, le=3650)
    agent_runs_days: int = Field(default=0, ge=0, le=3650)
    alerts_days: int = Field(default=0, ge=0, le=3650)
    inbound_emails_days: int = Field(default=0, ge=0, le=3650)
    audit_events_days: int = Field(default=0, ge=0, le=3650)

    @field_validator("audit_events_days")
    @classmethod
    def check_audit(cls, v: int) -> int:
        if 0 < v < 365:
            raise ValueError("the audit log is kept at least a year (365 days, or 0 for always)")
        return v


class ConversationSection(Strict):
    """The AI conversation engine (aime.ai.conversation), Option B Phase 3. Off by default, and every message to a
    prospect still waits for a person's approval in every mode:

    - mode: off (nothing runs), shadow (the engine records the conversation, its facts with their evidence, the state,
      the next step it would take and the draft it would write, in the AI decision log; no draft, alert or email
      changes), drafts (the same, and a policy-approved draft may replace the template's suggested answer, still
      waiting for approval);
    - pricing_disclosure: never (no price is stated: the draft offers a call with the Sales Manager) or owner_statement
      (the owner's own sentence below; it still passes the policy engine, which blocks any price, number or discount);
    - at most two questions an email, and the model only when one is configured, within a per-run budget."""

    mode: Literal["off", "shadow", "drafts"] = "off"
    pricing_disclosure: Literal["never", "owner_statement"] = "never"
    pricing_statement: str = Field(default="", max_length=300)
    max_questions_per_email: int = Field(default=2, ge=1, le=2)
    model_extraction: bool = True  # facts the model reads, on top of the rules', when a model is configured
    model_polish: bool = False  # the model may reword a draft; the policy engine checks the result again
    model_calls_per_run: int = Field(default=3, ge=0, le=20)
    approved_languages: list[str] = Field(default_factory=lambda: ["en"], max_length=10)  # drafts only in these
    escalation_alerts_in_shadow: bool = False


class SalesOverrides(BaseModel):
    """The sales settings' overrides as aime.admin_settings stores them; that module validates each field."""

    model_config = ConfigDict(extra="forbid")
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


# --------------------------------------------------------------------------- the sections

@dataclass(frozen=True)
class SectionSpec:
    name: str
    title: str
    model: type[BaseModel]
    default: Callable[[Any], dict]
    description: str
    editable: bool = True  # False: shown and versioned, changed elsewhere (sales) or later (agents)
    apply: Callable[[Session | None, Any, dict], None] | None = None
    preview_notes: Callable[[Any, dict], list[str]] | None = field(default=None)


def _default_countries(settings) -> dict:
    from aime.countries import default_registry

    return {"countries": default_registry(settings)}


def _default_targets(settings) -> dict:
    from aime.countries import target_countries

    return {"targets": [{"country": c, "daily_verified_leads": 0, "daily_contactable_leads": 0} for c in target_countries(settings)]}


def _default_catalogue(settings) -> dict:
    from aime.catalogue import default_catalogue

    return default_catalogue()


def _default_sources(settings) -> dict:
    from aime.sources.registry import default_source_settings

    return {"sources": default_source_settings()}


def _default_schedules(settings) -> dict:
    base = schedule_base(settings).get("jobs", {})
    from aime.workflows import JOBS

    jobs = {}
    for name in JOBS:
        jc = base.get(name)
        if not jc:
            continue
        jobs[name] = {"enabled": bool(jc.get("enabled", True)),
                      "every_minutes": int(jc["every_minutes"]) if jc.get("every_minutes") else None,
                      "daily_at": jc.get("daily_at") or None}
    return {"jobs": jobs}


def _default_agents(settings) -> dict:
    from aime.agent_matrix import default_matrix

    return {"agents": default_matrix()}


SECTIONS: dict[str, SectionSpec] = {}


def _register(spec: SectionSpec) -> None:
    SECTIONS[spec.name] = spec


def schedule_base(settings) -> dict:
    """config/schedule.toml as loaded, before any saved schedule was laid over it."""
    base = getattr(settings, "_schedule_base", None)
    if base is None:
        base = copy.deepcopy(settings.schedule)
        settings._schedule_base = base
    return base


def _apply_schedules(session: Session | None, settings, data: dict) -> None:
    """Lay the saved cadence and switches over config/schedule.toml (aime.scheduler.runner reads settings.schedule), and
    write job_state.enabled, which the runner has always honoured but nothing used to write."""
    from aime.automation import ALWAYS_RUN_JOBS

    jobs = copy.deepcopy(schedule_base(settings).get("jobs", {}))
    for name, js in (data.get("jobs") or {}).items():
        jc = jobs.setdefault(name, {})
        jc["enabled"] = bool(js.get("enabled", True)) or name in ALWAYS_RUN_JOBS
        if jc.get("every_minutes") and js.get("every_minutes"):
            jc["every_minutes"] = int(js["every_minutes"])
        if jc.get("daily_at") and js.get("daily_at"):
            jc["daily_at"] = js["daily_at"]
    settings.schedule["jobs"] = jobs
    if session is None:
        return
    from aime.models import JobState

    for name, jc in jobs.items():
        state = session.get(JobState, name)
        wanted = bool(jc.get("enabled", True)) or name in ALWAYS_RUN_JOBS
        if state is None:
            if not wanted:
                session.add(JobState(job=name, consecutive_failures=0, enabled=False))
        elif state.enabled != wanted:
            state.enabled = wanted


def _countries_notes(settings, data: dict) -> list[str]:
    from aime.countries import sources_for

    notes = []
    for c in data.get("countries", []):
        if c.get("enabled"):
            found = sources_for(settings, c["code"])
            notes.append(f"{c['code']}: discovery sources {', '.join(found) if found else 'none (only your own CSV lists, if any)'}")
    return notes


def _targets_notes(settings, data: dict) -> list[str]:
    cap = settings.section("research").get("max_new_companies_per_run", 40)
    return [f"Targets are goals: research still finds at most {cap} new companies per run, and no sending limit changes"]


def _schedules_notes(settings, data: dict) -> list[str]:
    out = []
    for name, js in (data.get("jobs") or {}).items():
        if not js.get("enabled", True):
            out.append(f"{name} will not run until it is enabled again")
    return out


for _spec in (
    SectionSpec("countries", "Countries", CountriesSection, _default_countries,
                "Where the AI employee looks for companies. A country other than the US can be enabled only after a recorded "
                "legal review of its email-marketing rules.", preview_notes=_countries_notes),
    SectionSpec("targets", "Daily targets", TargetsSection, _default_targets,
                "Daily goals per country. They are measured and explained, never forced: no cap or quality rule changes.",
                preview_notes=_targets_notes),
    SectionSpec("catalogue", "Industries and services", CatalogueSection, _default_catalogue,
                "The catalogue the sales settings choose from. Built-in entries can be disabled, not removed."),
    SectionSpec("sources", "Lead sources", SourcesSection, _default_sources,
                "Which authorized sources discovery reads, in which order, for which countries. The .env switches still turn "
                "a source off whatever is set here."),
    SectionSpec("schedules", "Automation schedules", SchedulesSection, _default_schedules,
                "Jobs on or off and how often they run, within safe bounds. The health monitor and the database backup "
                "always run.", apply=_apply_schedules, preview_notes=_schedules_notes),
    SectionSpec("agents", "Agent responsibilities", AgentsSection, _default_agents,
                "What each agent does, may use and must never do. Shown here; switching an agent on or off is on the AI Agents "
                "page.", editable=False),
    SectionSpec("deliverability", "Deliverability", DeliverabilitySection, lambda s: DeliverabilitySection().model_dump(mode="json"),
                "Placeholders for the deliverability safeguards: not used yet, and they cannot raise a sending limit."),
    SectionSpec("ai", "AI", AISection, lambda s: AISection().model_dump(mode="json"),
                "The AI provider and spending cap for the AI core (LLM_PROVIDER in .env still decides today)."),
    SectionSpec("retention", "Retention", RetentionSection, lambda s: RetentionSection().model_dump(mode="json"),
                "How long records are kept (0 keeps everything). Nothing is deleted by these yet."),
    SectionSpec("conversation", "AI conversations", ConversationSection, lambda s: ConversationSection().model_dump(mode="json"),
                "The AI conversation engine: off, shadow (records only) or drafts (a checked draft may replace the "
                "template's suggested answer). Every email still needs a person's approval. The objection playbook and "
                "approved claims are on /admin/settings/conversation."),
    SectionSpec("sales", "Sales settings", SalesOverrides, lambda s: {"overrides": {}},
                "The overrides saved on the Sales settings page, over config/business.toml.", editable=False),
):
    _register(_spec)


# --------------------------------------------------------------------------- reading

def _stored(settings) -> dict[str, int]:
    found = getattr(settings, "_settings_versions", None)
    if found is None:
        found = {}
        settings._settings_versions = found
    return found


def stored(settings, name: str) -> bool:
    """A saved version of this section is in effect (else its defaults)."""
    return name in _stored(settings)


def active_version_id(settings, name: str) -> int | None:
    return _stored(settings).get(name)


def defaults(settings, name: str) -> dict:
    return validate_shape(name, SECTIONS[name].default(settings))


def get(settings, name: str) -> dict:
    """The effective section: the active saved version, else the defaults, worked out now (so a default that follows
    another setting, such as the target country, follows it at once)."""
    if name not in SECTIONS:
        raise KeyError(f"Unknown settings section {name!r}")
    if stored(settings, name) and name in settings.business:
        return copy.deepcopy(settings.business[name])
    return defaults(settings, name)


def validate_shape(name: str, data: dict) -> dict:
    """The section as its model sees it (raises ValueError with readable reasons)."""
    if ENV_ONLY.search(_all_keys(data)):
        raise ValueError("the human-approval, sending and publishing switches are set in .env only")
    try:
        return SECTIONS[name].model.model_validate(data).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError(readable(exc)) from None


def _all_keys(data) -> str:
    if isinstance(data, dict):
        return " ".join(f"{k} {_all_keys(v)}" for k, v in data.items())
    if isinstance(data, list):
        return " ".join(_all_keys(v) for v in data)
    return ""


def readable(exc: ValidationError) -> str:
    out = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        out.append(f"{where}: {msg}" if where else msg)
    return "; ".join(out)


# --------------------------------------------------------------------------- loading at start-up

def load(session: Session, settings) -> None:
    """Lay every active saved version over the defaults (create_context does this once; a save does it for its section).
    A version that no longer validates, for instance after a rule became stricter, is set aside with a warning: the
    defaults apply, and nothing stops."""
    from aime.models import SettingsVersion

    schedule_base(settings)
    rows = {}
    try:
        for row in session.execute(select(SettingsVersion).where(SettingsVersion.active.is_(True))
                                   .order_by(SettingsVersion.id)).scalars():
            rows[row.section] = row
    except Exception as exc:  # noqa: BLE001 - a database from before this table: everything stays at its defaults
        log.warning("Saved settings could not be read; the defaults apply: %s", exc)
        rows = {}
    found = _stored(settings)
    found.clear()
    for name, spec in SECTIONS.items():
        if name == "sales":
            continue
        row = rows.get(name)
        data = None
        if row is not None:
            try:
                data = _check(None, settings, name, row.data or {}, trusted_review=True)
            except ValueError as exc:
                log.warning("Saved %s settings (version %s) no longer valid, using the defaults: %s", name, row.id, exc)
        if data is not None:
            found[name] = row.id
            settings.business[name] = data
        else:
            settings.business[name] = defaults(settings, name)
        if spec.apply is not None and data is not None:
            spec.apply(None, settings, data)
    if "sales" in rows:
        found["sales"] = rows["sales"].id


# --------------------------------------------------------------------------- checks across sections

def _check(session: Session | None, settings, name: str, data: dict, trusted_review: bool = False) -> dict:
    """The model's rules plus the ones that need the rest of the settings. `trusted_review`: legal-review fields may differ
    from the current ones (a recorded review, a revert to a version that held one, or loading what was saved)."""
    clean = validate_shape(name, data)
    if name == "countries" and not trusted_review:
        now = {c["code"]: c for c in get(settings, "countries")["countries"]}
        keys = ("legal_review_confirmed_by", "legal_review_confirmed_at", "legal_review_note")
        for c in clean["countries"]:
            before = now.get(c["code"], {})
            if any((c.get(k) or None) != (before.get(k) or None) for k in keys):
                raise ValueError(f"{c['code']}: a legal review is recorded with its own form, not by editing the country")
    if name == "targets":
        from aime.countries import registry

        known = {c["code"] for c in registry(settings)}
        unknown = [t["country"] for t in clean["targets"] if t["country"] not in known]
        if unknown:
            raise ValueError(f"no such country in the Countries list: {', '.join(unknown)}")
    if name == "schedules":
        base = schedule_base(settings).get("jobs", {})
        for job, js in clean["jobs"].items():
            jc = base.get(job)
            if jc is None:
                raise ValueError(f"{job} is not in config/schedule.toml")
            if jc.get("every_minutes"):
                if js.get("daily_at"):
                    raise ValueError(f"{job} runs at an interval; it cannot be given a daily time")
                if js.get("every_minutes") and int(js["every_minutes"]) < int(jc["every_minutes"]):
                    raise ValueError(f"{job} cannot run more often than every {jc['every_minutes']} min (config/schedule.toml)")
            elif js.get("every_minutes"):
                raise ValueError(f"{job} runs at a time of day; it cannot be given an interval")
    return clean


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """A section as path -> value, lists of records keyed by their code, name, key or country, for a readable diff."""
    out: dict[str, Any] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        for i, x in enumerate(data):
            ident = next((x[k] for k in ("code", "name", "key", "country") if k in x), i)
            out.update(flatten(x, f"{prefix}[{ident}]"))
    else:
        out[prefix] = data
    return out


def diff(old: dict, new: dict) -> list[str]:
    a, b = flatten(old), flatten(new)
    return [f"{k}: {a.get(k)!r} -> {b.get(k)!r}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


# --------------------------------------------------------------------------- preview, save, revert, history

@dataclass
class Preview:
    ok: bool
    errors: list[str]
    changes: list[str]
    notes: list[str]


def preview(session: Session, settings, name: str, data: dict) -> Preview:
    """What saving `data` would change, without writing anything."""
    spec = SECTIONS[name]
    try:
        clean = _check(session, settings, name, data)
    except ValueError as exc:
        return Preview(False, [str(exc)], [], [])
    notes = spec.preview_notes(settings, clean) if spec.preview_notes else []
    return Preview(True, [], diff(get(settings, name), clean), notes)


def _write(session: Session, settings, name: str, clean: dict, actor: str, note: str | None, action: str) -> tuple[Any, list[str]]:
    from aime.automation import audit
    from aime.db import take_write_lock
    from aime.models import SettingsVersion

    take_write_lock(session)
    changes = diff(get(settings, name), clean)
    if not changes:
        return None, []
    session.execute(update(SettingsVersion).where(SettingsVersion.section == name, SettingsVersion.active.is_(True))
                    .values(active=False))
    row = SettingsVersion(section=name, data=clean, schema_version=SCHEMA_VERSION, created_by=(actor or "system")[:80],
                          note=(note or "").strip()[:300] or None, active=True)
    session.add(row)
    session.flush()
    audit(session, actor, action, f"settings:{name}", f"version {row.id}: " + "; ".join(changes)[:1900])
    _stored(settings)[name] = row.id
    settings.business[name] = copy.deepcopy(clean)
    if SECTIONS[name].apply is not None:
        SECTIONS[name].apply(session, settings, clean)
    return row, changes


def save(session: Session, settings, name: str, data: dict, actor: str, note: str | None = None) -> tuple[Any, list[str]]:
    """Validate and save a new active version of a section, audit it and apply it now. Returns (version or None, changes)."""
    spec = SECTIONS.get(name)
    if spec is None or not spec.editable:
        raise ValueError(f"{name} settings cannot be changed here")
    clean = _check(session, settings, name, data)
    return _write(session, settings, name, clean, actor, note, "settings_saved")


def save_trusted(session: Session, settings, name: str, data: dict, actor: str, note: str | None, action: str) -> tuple[Any, list[str]]:
    """For aime.countries.record_legal_review only: the one path that may change a country's legal-review fields."""
    clean = _check(session, settings, name, data, trusted_review=True)
    return _write(session, settings, name, clean, actor, note, action)


def history(session: Session, name: str, limit: int = 50) -> list:
    from aime.models import SettingsVersion

    return session.execute(select(SettingsVersion).where(SettingsVersion.section == name)
                           .order_by(SettingsVersion.id.desc()).limit(limit)).scalars().all()


def revert(session: Session, settings, version_id: int, actor: str) -> tuple[Any, list[str]]:
    """Make an earlier version the active one again, as a new version (validated by today's rules; audited)."""
    from aime.models import SettingsVersion

    old = session.get(SettingsVersion, version_id)
    if old is None or old.section not in SECTIONS:
        raise LookupError("no such settings version")
    if old.section == "sales":
        from aime import admin_settings

        return admin_settings.restore(session, settings, actor, (old.data or {}).get("overrides") or {}, old.id)
    clean = _check(session, settings, old.section, old.data or {}, trusted_review=True)
    return _write(session, settings, old.section, clean, actor, f"restored version {old.id}", "settings_reverted")


def reset_to_defaults(session: Session, settings, name: str, actor: str) -> list[str]:
    """Back to the defaults: no saved version is active any more, so the section follows config/*.toml and the other
    settings again. Every earlier version stays in the history and can be restored. Audited; returns the changes."""
    from aime.automation import audit
    from aime.db import take_write_lock
    from aime.models import SettingsVersion

    spec = SECTIONS.get(name)
    if spec is None or not spec.editable:
        raise ValueError(f"{name} settings cannot be changed here")
    if not stored(settings, name):
        return []
    take_write_lock(session)
    fresh = defaults(settings, name)
    changes = diff(get(settings, name), fresh)
    session.execute(update(SettingsVersion).where(SettingsVersion.section == name, SettingsVersion.active.is_(True))
                    .values(active=False))
    _stored(settings).pop(name, None)
    settings.business[name] = fresh
    if spec.apply is not None:
        spec.apply(session, settings, fresh)
    audit(session, actor, "settings_reset_section", f"settings:{name}", ("back to the defaults: " + "; ".join(changes))[:2000])
    return changes


def record_sales_version(session: Session, settings, actor: str, overrides: dict, note: str) -> None:
    """A copy of the sales-settings overrides as a version of section "sales" (aime.admin_settings calls this)."""
    from aime.models import SettingsVersion

    data = {"overrides": copy.deepcopy(overrides or {})}
    current = session.execute(select(SettingsVersion).where(SettingsVersion.section == "sales", SettingsVersion.active.is_(True))
                              .order_by(SettingsVersion.id.desc())).scalars().first()
    if current is not None and current.data == data:
        return
    session.execute(update(SettingsVersion).where(SettingsVersion.section == "sales", SettingsVersion.active.is_(True))
                    .values(active=False))
    row = SettingsVersion(section="sales", data=data, schema_version=SCHEMA_VERSION, created_by=(actor or "system")[:80],
                          note=note[:300], active=True)
    session.add(row)
    session.flush()
    _stored(settings)["sales"] = row.id


def section_title(name: str) -> str:
    return SECTIONS[name].title


def editable_sections() -> list[str]:
    return [n for n, s in SECTIONS.items() if s.editable]


def utc_now() -> datetime:
    from aime.timeutil import utcnow

    return utcnow()
