"""Every contact-source adapter, its settings (section "contact_sources") and what the admin page shows about it.

Two kinds of adapter:
- the sources AIME already had (aime.contact_sources.wrapped): described here, still run and switched as before;
- adapters these settings control (`controlled_by == "settings"`): the official registries (Companies House) and the paid
  stubs. Each is off by default, with a monthly budget of 0; it is used only for the countries its settings list that it
  covers and that are target countries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aime.contact_sources.base import AdapterRefused, ContactSource, RateLimited, SourceError
from aime.contact_sources.companies_house import CompaniesHouseOfficers
from aime.contact_sources.paid import PAID
from aime.contact_sources.wrapped import WRAPPED

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from aime.people import PersonFinding

ADAPTER_CLASSES: dict[str, type[ContactSource]] = {c.name: c for c in (*WRAPPED, CompaniesHouseOfficers, *PAID)}
OFFICER_REGISTRIES = (CompaniesHouseOfficers.name,)  # adapters that name a company's officers from an official register


def managed() -> list[str]:
    """The adapters the Contact sources settings switch (the others keep their own controls)."""
    return [n for n, c in ADAPTER_CLASSES.items() if c.controlled_by == "settings"]


def default_adapter_settings() -> list[dict]:
    return [{"name": name, "enabled": False, "priority": (i + 1) * 10,
             "countries": [c for c in ADAPTER_CLASSES[name].countries if c != "*"],
             "monthly_budget_usd": 0.0, "cost_per_call_usd": 0.0} for i, name in enumerate(managed())]


def adapter_settings(settings) -> list[dict]:
    """Every settings-controlled adapter with its settings, by priority; one missing from a saved version gets its defaults."""
    from aime import settings_store

    saved = {a["name"]: a for a in settings_store.get(settings, "contact_sources")["adapters"]}
    order = managed()
    rows = [saved.get(d["name"], d) for d in default_adapter_settings()]
    return sorted(rows, key=lambda r: (r["priority"], order.index(r["name"])))


def adapter_setting(settings, name: str) -> dict | None:
    return next((r for r in adapter_settings(settings) if r["name"] == name), None)


def get_adapter(ctx, name: str) -> ContactSource:
    return ADAPTER_CLASSES[name](ctx)


def adapter_for_source(source: str | None, consent_source: str | None = None) -> type[ContactSource] | None:
    """The adapter a contact with this Contact.source came from. An inquiry from a LinkedIn lead form is LinkedIn's."""
    if source == "inbound" and "via linkedin_form" in (consent_source or ""):
        return ADAPTER_CLASSES["linkedin_lead_sync"]
    for name, cls in ADAPTER_CLASSES.items():
        if name == "linkedin_lead_sync":
            continue
        if source in cls.contact_sources:
            return cls
    return None


def overview(session: "Session", ctx) -> list[dict]:
    """One row per adapter for the admin page: metadata, status, settings, this month's usage and the last run."""
    from aime.contact_sources.budget import approval, cooldown_until, last_usage, usage

    out = []
    for name, cls in ADAPTER_CLASSES.items():
        adapter = cls(ctx)
        st = adapter.status(session)
        setting = adapter_setting(ctx.settings, name)
        month = usage(session, name)
        last = last_usage(session, name)
        out.append({"name": name, "title": cls.title, "description": cls.description, "status": st.status, "reason": st.reason,
                    "countries": ", ".join(cls.countries) or "none claimed", "fields": ", ".join(cls.fields), "licence": cls.licence,
                    "lawful_basis": cls.lawful_basis, "terms_url": cls.terms_url, "cost": adapter.cost_label(setting),
                    "rate_limit": cls.rate_limit, "credentials": ", ".join(s.upper() for s in cls.credential_settings) or "none",
                    "controlled_by": cls.controlled_by, "wraps": cls.wraps, "setting": setting,
                    "approval": approval(session, name) if cls.requires_owner_approval else None,
                    "requires_owner_approval": cls.requires_owner_approval, "paid": cls.paid,
                    "month_calls": month.calls if month else 0, "month_cost": month.cost_usd if month else 0.0,
                    "month_refused": month.refused if month else 0, "last_call_at": last.last_call_at if last else None,
                    "last_result": last.last_result if last else None, "cooldown_until": cooldown_until(session, name)})
    return out


def registry_officers(ctx, session: "Session", company) -> tuple[list["PersonFinding"], str | None]:
    """Officers from the official registers that may be used for this company now, by priority: ([], None) unless one is
    switched on, ready, covers the company's country (a target country) and the company carries its register number.
    A refusal or a failure of the register never fails the company's research: it is recorded in the ledger and returned."""
    from aime.contact_sources.budget import refusal
    from aime.contact_sources.companies_house import company_number_of
    from aime.redact import redact

    found: list = []
    problem = None
    country = (getattr(company, "country", None) or "").upper()
    for row in adapter_settings(ctx.settings):
        if row["name"] not in OFFICER_REGISTRIES or not row["enabled"]:
            continue
        adapter = get_adapter(ctx, row["name"])
        if isinstance(adapter, CompaniesHouseOfficers) and not company_number_of(company):
            continue
        if refusal(session, adapter, country) is not None:  # quietly: nothing would be called
            continue
        try:
            found.extend(adapter.officers(session, company))
        except (AdapterRefused, RateLimited, SourceError) as exc:
            problem = redact(f"{adapter.title}: {exc}")[:300]
        except Exception as exc:  # noqa: BLE001 - the network or an unexpected answer: the register is tried on a coming run
            problem = redact(f"{adapter.title} could not be read: {type(exc).__name__}: {exc}")[:300]
    return found, problem


def registry_usable(ctx, session: "Session") -> bool:
    """Some officer register is switched on and ready for at least one target country (so companies that carry only a
    register number are worth researching)."""
    from aime import countries
    from aime.contact_sources.budget import refusal

    targets = countries.target_countries(ctx.settings)
    for row in adapter_settings(ctx.settings):
        if row["name"] in OFFICER_REGISTRIES and row["enabled"]:
            adapter = get_adapter(ctx, row["name"])
            if any(refusal(session, adapter, c) is None for c in targets):
                return True
    return False
