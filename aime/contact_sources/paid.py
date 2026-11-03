"""Paid contact data and paid email verification: generic stubs, no vendor.

No provider has been chosen, no price is known and no vendor's API is called from here: the roadmap leaves each one to the
owner (a quote, the licence for email outreach, data-source transparency, per-country coverage, GDPR/CCPA posture). A stub
declares what such a source would be and stays `blocked_needs_owner_approval`. It refuses to run unless all of these exist:
an owner approval recorded for it, its credentials in .env, a monthly budget above 0 and a price per call (the owner's quote)
in the Contact sources settings - and even then it refuses, because no vendor integration has been built. A real provider
is a new subclass with its own reviewed `_call`, added as a code change after approval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aime.contact_sources.base import NEEDS_APPROVAL, AdapterRefused, AdapterStatus, ContactSource

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

TO_CONFIRM = "to be confirmed by owner"


class PaidStub(ContactSource):
    paid = True
    requires_owner_approval = True
    countries: tuple[str, ...] = ()  # no coverage is claimed until a provider is chosen and its coverage confirmed
    licence = f"vendor licence ({TO_CONFIRM} before purchase)"
    lawful_basis = f"{TO_CONFIRM}: depends on the vendor's data sources and each country's rules"
    terms_url = TO_CONFIRM
    rate_limit = "per vendor (not known)"
    vendor_integration = False  # True only in a reviewed subclass that implements _call

    def status(self, session: "Session | None" = None) -> AdapterStatus:
        st = super().status(session)  # approval first, then credentials
        if not st.ready:
            return st
        from aime.contact_sources.registry import adapter_setting

        setting = adapter_setting(self.settings, self.name) or {}
        if float(setting.get("monthly_budget_usd") or 0) <= 0:
            return AdapterStatus(NEEDS_APPROVAL, "the monthly budget is 0: set one in the Contact sources settings")
        if float(setting.get("cost_per_call_usd") or 0) <= 0:
            return AdapterStatus(NEEDS_APPROVAL, "no price per call recorded (the owner's quote)")
        if not self.vendor_integration:
            return AdapterStatus(NEEDS_APPROVAL, "no vendor integration has been built: choosing a provider is the owner's decision")
        return st

    def _call(self, request: dict) -> dict:
        raise AdapterRefused("no vendor integration has been built for this paid source")

    def call(self, session: "Session", request: dict, country: str | None = None) -> dict:
        """One request to the provider, through the budget guard (which refuses first when anything above is missing)."""
        from aime.contact_sources.budget import guarded_call

        return guarded_call(session, self, lambda: self._call(request), country=country, note=str(request.get("kind") or ""))


class PaidContactProvider(PaidStub):
    name = "paid_contact_provider"
    title = "Paid B2B contact data provider (none chosen)"
    description = ("A placeholder for a licensed B2B contact database. Choosing one, and its budget, is the owner's decision; "
                   "nothing is bought or called until then.")
    fields = ("name", "title", "email", "phone")
    credential_settings = ("contact_provider_api_key",)
    contact_sources = ("paid_provider",)


class PaidEmailVerifier(PaidStub):
    """Also an aime.verification verifier: it would answer verified / risky / invalid for one address."""

    name = "paid_email_verifier"
    title = "Paid email verification service (none chosen)"
    description = ("A placeholder for a mailbox-level verification service. Until one is approved, the free mail-server "
                   "(MX/A) check is the only verifier.")
    fields = ("email",)
    credential_settings = ("email_verifier_api_key",)
    contact_sources = ()

    def verify(self, session: "Session", email: str):
        from aime.verification import VerificationResult

        answer = self.call(session, {"kind": "verify", "email": email})
        return VerificationResult(str(answer.get("status") or "unknown"), str(answer.get("detail") or ""), self.name)


PAID = (PaidContactProvider, PaidEmailVerifier)
