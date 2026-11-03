"""The contact sources AIME already had, described as adapters. Nothing here changes how they work: each one is still run
by the module it names (`wraps`) and still switched by its own control (`controlled_by`); the adapter only declares its
countries, fields, licence, lawful basis, cost and status, so every source can be reviewed in one place and every contact
it yields records its provenance (aime.provenance)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aime.contact_sources.base import NEEDS_APPROVAL, NEEDS_CREDENTIALS, AdapterStatus, ContactSource

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LEGITIMATE_INTEREST_B2B = "legitimate_interest_b2b"


class CompanyWebsiteContacts(ContactSource):
    """People, titles, published emails and phones from the company's own website (team, about and contact pages)."""

    name = "company_website"
    title = "Company's own website"
    description = ("Names and titles the company publishes about its own people, and the emails and phone numbers it publishes "
                   "on its own domain. robots.txt is honoured and only a few pages are read.")
    countries = ("*",)
    fields = ("name", "title", "email", "phone")
    licence = "published_by_the_company"
    lawful_basis = LEGITIMATE_INTEREST_B2B
    terms_url = "each company's own website terms (to be confirmed by owner)"
    rate_limit = "a few pages per company, with the polite delay between pages (POLITE_DELAY)"
    contact_sources = ("website", "website_withdrawn")
    controlled_by = "the enrich job (company research) and NETWORK_ENABLED"
    wraps = "aime.agents.company_research, aime.people, aime.contact_validation"

    def status(self, session: "Session | None" = None) -> AdapterStatus:
        if not self.settings.network_enabled:
            return AdapterStatus(NEEDS_CREDENTIALS, "NETWORK_ENABLED=false: no website is read")
        return super().status(session)


class SECOfficerContacts(ContactSource):
    """Officers and directors named in a US-registered company's Form 3 and 4 insider filings (names and titles, no emails)."""

    name = "sec_officers"
    title = "SEC insider filings (officers)"
    description = "Officer and director names and their filed titles from Forms 3 and 4 on SEC EDGAR. No email addresses."
    countries = ("US",)
    fields = ("name", "title")
    licence = "us_public_record_sec_edgar"
    lawful_basis = LEGITIMATE_INTEREST_B2B
    terms_url = "https://www.sec.gov/os/accessing-edgar-data (to be confirmed by owner)"
    rate_limit = "SEC fair access: at most 10 requests a second; AIME waits 0.15 s between filings"
    credential_settings = ("sec_user_agent",)  # not a secret: a descriptive User-Agent with a contact email, as SEC asks
    contact_sources = ("sec_filing",)
    controlled_by = "SEC_USER_AGENT in .env (company research reads filings only when it is set)"
    wraps = "aime.sources.sec_officers"

    def missing_credentials(self) -> list[str]:
        ua = (self.settings.sec_user_agent or "").strip()
        return [] if ua and "@" in ua else ["SEC_USER_AGENT (with a contact email)"]


class CSVImportContacts(ContactSource):
    """The owner's own company lists (CSV files in the imports folder). They give companies, not people."""

    name = "csv_import"
    title = "Your own lists (CSV import)"
    description = "Companies from CSV files the owner puts in the imports folder; the owner warrants where the list came from."
    countries = ("*",)
    fields = ("company", "website", "location")
    licence = "owner_warranted"
    lawful_basis = "owner_warranted"
    terms_url = "the owner's own records (to be confirmed by owner)"
    rate_limit = "no calls: local files, at most 5,000 rows a file"
    contact_sources = ("import",)
    controlled_by = "the Lead sources settings (csv_import)"
    wraps = "aime.sources.csv_import"


class ManualContacts(ContactSource):
    """A contact a person entered or gave AIME (the owner's own contacts, test contacts)."""

    name = "manual_entry"
    title = "Entered by a person"
    description = "Contacts a dashboard user entered or gave AIME by hand. They are never described as published."
    countries = ("*",)
    fields = ("name", "title", "email", "phone")
    licence = "owner_provided"
    lawful_basis = "owner_warranted"
    terms_url = "the owner's own records (to be confirmed by owner)"
    rate_limit = "no calls"
    contact_sources = ("manual",)
    controlled_by = "the people who use the dashboard"
    wraps = "aime.web.app (manual entry)"


class InboundFormContacts(ContactSource):
    """People who wrote to us: the website form, the inbound API and dropped inquiry files."""

    name = "inbound_forms"
    title = "Inbound inquiries (forms, API, files)"
    description = "People who contacted the business themselves; marketing consent only where they ticked it."
    countries = ("*",)
    fields = ("name", "title", "email", "phone", "company", "consent", "message")
    licence = "submitted_by_the_person"
    lawful_basis = "inquiry_or_consent"
    terms_url = "the owner's own privacy notice on the form (to be confirmed by owner)"
    rate_limit = "no outbound calls"
    contact_sources = ("inbound",)
    controlled_by = "the inbound form and API (always on) and the inbox folder"
    wraps = "aime.inbound, aime.agents.crm"


class LinkedInLeadSyncContacts(ContactSource):
    """Opt-in leads from the owner's LinkedIn Lead Gen Forms and event registrations (Lead Sync API). No scraping."""

    name = "linkedin_lead_sync"
    title = "LinkedIn Lead Gen Forms (Lead Sync API)"
    description = ("Only people who filled in one of the owner's LinkedIn lead forms or registered for the owner's event. "
                   "No profile is ever read or scraped.")
    countries = ("*",)
    fields = ("name", "title", "email", "phone", "company", "consent")
    licence = "linkedin_marketing_api_terms"
    lawful_basis = "consent"
    terms_url = "LinkedIn Marketing API and Lead Sync terms (URL to be confirmed by owner)"
    rate_limit = "LinkedIn's API limits for the owner's app; one sync per CRM run"
    credential_settings = ("linkedin_leads_client_id", "linkedin_leads_client_secret")
    contact_sources = ("inbound",)  # a LinkedIn lead becomes an inbound inquiry on channel "linkedin_form"
    controlled_by = "LINKEDIN_LEADS_ENABLED in .env and the LinkedIn leads page"
    wraps = "aime.integrations.linkedin_leads"

    def status(self, session: "Session | None" = None) -> AdapterStatus:
        if self.missing_credentials():
            return AdapterStatus(NEEDS_APPROVAL, "LinkedIn has to approve the Lead Sync API for the owner's app first; then "
                                                 "LINKEDIN_LEADS_CLIENT_ID and LINKEDIN_LEADS_CLIENT_SECRET go in .env")
        st = super().status(session)
        if st.ready and not self.settings.linkedin_leads_enabled:
            return AdapterStatus(st.status, "ready, but LINKEDIN_LEADS_ENABLED=false keeps it switched off")
        return st


WRAPPED = (CompanyWebsiteContacts, SECOfficerContacts, CSVImportContacts, ManualContacts, InboundFormContacts,
           LinkedInLeadSyncContacts)
