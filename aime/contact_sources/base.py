"""The contact-source adapter contract (Option B, Phase 4).

A contact source is anywhere a person's business contact details may come from: the company's own website, an official
register, an opt-in form, the owner's own list, or (only once the owner approves and pays for one) a data provider. Each
adapter declares, in code, what a reviewer needs before trusting it:

- `countries`: where it has records ("*": any, such as the owner's own lists). It is used only for target countries.
- `fields`: what it can give (name, title, email, phone, company ...). Nothing beyond these is ever stored from it.
- `licence`: the terms its data is used under, as a tag; `terms_url` where those terms are published.
- `lawful_basis`: the basis on which AIME processes the people it yields (consent, legitimate interest for B2B ...).
- `cost_per_call_usd`: 0 for a free source. A `paid` adapter's price is the owner's quote, set in the settings.
- `rate_limit`: what the source allows, in words, and `min_interval_seconds` that AIME keeps between its own calls.
- `credential_settings`: the .env settings it needs (empty when it needs none).
- `requires_owner_approval`: it may not run until the owner has recorded an approval for it.

Its status is worked out each time: `ready`, `blocked_needs_credentials` (a key or setting is missing) or
`blocked_needs_owner_approval` (an approval, a budget or a vendor integration is missing). No adapter guesses an email
address: guessing a pattern such as firstname@company stays prohibited until the owner decides otherwise.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

READY = "ready"
NEEDS_CREDENTIALS = "blocked_needs_credentials"
NEEDS_APPROVAL = "blocked_needs_owner_approval"
STATUSES = (READY, NEEDS_CREDENTIALS, NEEDS_APPROVAL)

# The kinds of data an adapter may declare. Each one is a business detail; nothing personal beyond these is kept
# (no home address, no date of birth, no personal social profile).
FIELDS = ("company", "website", "location", "name", "title", "email", "phone", "consent", "message")
REQUIRED_METADATA = ("name", "title", "countries", "fields", "licence", "lawful_basis", "cost_per_call_usd", "rate_limit",
                     "terms_url", "contact_sources")


class AdapterRefused(RuntimeError):
    """The adapter may not be called now: not enabled, blocked, outside its countries, no approval or over its budget.
    Nothing was called and nothing was spent."""


class RateLimited(RuntimeError):
    """The source said "too many requests" (HTTP 429). The adapter waits `retry_after` seconds before its next call."""

    def __init__(self, message: str, retry_after: float = 300.0):
        super().__init__(message)
        self.retry_after = retry_after


class SourceError(RuntimeError):
    """The source answered with an error that is not ours to retry at once (a refused key, a server error)."""


@dataclass(frozen=True)
class AdapterStatus:
    status: str  # READY | NEEDS_CREDENTIALS | NEEDS_APPROVAL
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.status == READY


class ContactSource(ABC):  # noqa: B024 - an adapter overrides only what it needs; the contract is its metadata
    """One contact-source adapter: its declared metadata and its status. Calling it goes through the budget guard
    (aime.contact_sources.budget.guarded_call), never straight to the source."""

    name: ClassVar[str] = "contact_source"
    title: ClassVar[str] = "Contact source"
    description: ClassVar[str] = ""
    countries: ClassVar[tuple[str, ...]] = ("US",)
    fields: ClassVar[tuple[str, ...]] = ()
    licence: ClassVar[str] = ""
    lawful_basis: ClassVar[str] = ""
    terms_url: ClassVar[str] = ""  # "to be confirmed by owner" where the terms have not been reviewed
    cost_per_call_usd: ClassVar[float] = 0.0
    paid: ClassVar[bool] = False  # the price is the owner's quote (settings), and the call needs a budget
    rate_limit: ClassVar[str] = ""
    min_interval_seconds: ClassVar[float] = 0.0
    credential_settings: ClassVar[tuple[str, ...]] = ()
    requires_owner_approval: ClassVar[bool] = False
    # The Contact.source values this adapter's contacts carry (for provenance); empty: it yields companies, not people.
    contact_sources: ClassVar[tuple[str, ...]] = ()
    # Where it is switched on or off. "settings": the Contact sources settings section (aime.settings_store). Anything
    # else names the control an existing source already has, which these settings do not override.
    controlled_by: ClassVar[str] = "settings"
    wraps: ClassVar[str] = ""  # the existing module it describes, for a wrapped source

    def __init__(self, ctx):
        self.ctx = ctx
        self.settings = ctx.settings

    # ------------------------------------------------------------------ status
    def missing_credentials(self) -> list[str]:
        return [name.upper() for name in self.credential_settings if not str(getattr(self.settings, name, "") or "").strip()]

    def status(self, session: "Session | None" = None) -> AdapterStatus:
        """ready, or why not. Approval comes first: credentials alone never make an approval-bound adapter ready."""
        if self.requires_owner_approval:
            from aime.contact_sources.budget import approval

            if session is None or approval(session, self.name) is None:
                return AdapterStatus(NEEDS_APPROVAL, "the owner has not recorded an approval for this source")
        missing = self.missing_credentials()
        if missing:
            return AdapterStatus(NEEDS_CREDENTIALS, f"{', '.join(missing)} not set in .env")
        return AdapterStatus(READY)

    def cost_label(self, setting: dict | None = None) -> str:
        if not self.paid:
            return "free" if not self.cost_per_call_usd else f"${self.cost_per_call_usd:.4f} a call"
        price = float((setting or {}).get("cost_per_call_usd") or 0)
        return f"${price:.4f} a call (owner's quote)" if price else "paid: price not recorded (a quote is needed)"

    @classmethod
    def metadata(cls) -> dict:
        return {k: getattr(cls, k) for k in (*REQUIRED_METADATA, "description", "paid", "credential_settings",
                                              "requires_owner_approval", "controlled_by", "wraps", "min_interval_seconds")}
