"""UK Companies House: the officers (directors, secretaries, LLP members) on a company's public register, through the
official Companies House Public Data API.

Terms: https://developer.company-information.service.gov.uk/ (developer hub, API terms and rate limits) - to be confirmed by
owner. The API needs a free key from that hub (COMPANIES_HOUSE_API_KEY); without one the adapter reports
blocked_needs_credentials. It covers companies registered in the UK (GB) only and is off by default: the UK is not a target
country until the owner records its legal review (aime.countries), and the adapter must also be switched on in the Contact
sources settings.

What is kept: the officer's name as registered, shown as "First Middle Last", their role (Director, Company Secretary ...)
and the register page as evidence. Never kept, although the register shows some of it: date of birth, nationality,
country of residence, correspondence address, occupation. Officers who have resigned, corporate officers (another company
acting as officer) and nominee officers are left out. The register has no email addresses, and none is guessed.

Rate limit: the hub documents 600 requests per five minutes per key. AIME keeps half a second between its calls and, on an
HTTP 429, waits for the Retry-After the API gives (five minutes when it gives none) before calling again.
"""

from __future__ import annotations

import re

import httpx

from aime.contact_sources.base import ContactSource, RateLimited, SourceError
from aime.people import PersonFinding, role_category
from aime.sources.sec_officers import display_name

API = "https://api.company-information.service.gov.uk"
OFFICERS = API + "/company/{number}/officers"
REGISTER_PAGE = "https://find-and-update.company-information.service.gov.uk/company/{number}/officers"
COMPANY_NUMBER = re.compile(r"^(?:[A-Z]{2}\d{6}|\d{8})$")
PAGE_SIZE = 100
MAX_PAGES = 3
ROLE_TITLES = {"director": "Director", "secretary": "Company Secretary", "llp-designated-member": "Designated Member",
               "llp-member": "LLP Member", "member": "Member", "managing-officer": "Managing Officer",
               "general-partner-in-a-limited-partnership": "General Partner"}
SKIPPED_ROLES = ("corporate-", "nominee-")  # another company acting as officer, or a nominee: not a person who decides


def company_number(value: str | None) -> str | None:
    """A UK company number as the register writes it: eight digits, or two letters and six digits (SC123456)."""
    raw = re.sub(r"\s+", "", str(value or "")).upper()
    if raw.isdigit() and len(raw) < 8:
        raw = raw.zfill(8)
    return raw if COMPANY_NUMBER.match(raw) else None


def company_number_of(company) -> str | None:
    """The company's Companies House number, when its record carries one (source_record_id "ch:<number>")."""
    rid = (getattr(company, "source_record_id", None) or "").strip()
    return company_number(rid[3:]) if rid.lower().startswith("ch:") else None


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(1.0, min(3600.0, float(resp.headers.get("retry-after", "300"))))
    except ValueError:
        return 300.0


def parse_officers(payload: dict, number: str) -> list[PersonFinding]:
    """The serving people on one page of the officers list. Only name and role are read from each item."""
    out = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or item.get("resigned_on"):
            continue
        role = str(item.get("officer_role") or "").strip().lower()
        filed = " ".join(str(item.get("name") or "").split())
        if not role or not filed or role.startswith(SKIPPED_ROLES):
            continue
        title = ROLE_TITLES.get(role) or role.replace("-", " ").title()
        name = display_name(filed)
        if len(name.split()) < 2:  # a single word is no name we can address anyone by
            continue
        appointed = str(item.get("appointed_on") or "").strip()
        evidence = (f'Companies House register of officers, company {number}: "{filed}", {role}'
                    + (f", appointed {appointed}" if appointed else ""))
        category = "board" if title == "Director" else role_category(title)
        out.append(PersonFinding(name, title, category, "high", evidence, REGISTER_PAGE.format(number=number),
                                 source="companies_house"))
    return out


class CompaniesHouseOfficers(ContactSource):
    name = "companies_house"
    title = "UK Companies House (officers)"
    description = ("Directors, secretaries and LLP members on a UK company's public register, from the official Companies House "
                   "API. Names and roles only: no emails, dates of birth or addresses.")
    countries = ("GB",)
    fields = ("name", "title")
    licence = "uk_companies_house_public_register (licence to be confirmed by owner)"
    lawful_basis = "legitimate_interest_b2b (UK GDPR; a legitimate-interests assessment and PECR review by the owner first)"
    terms_url = "https://developer.company-information.service.gov.uk/ (to be confirmed by owner)"
    cost_per_call_usd = 0.0
    rate_limit = "600 requests per 5 minutes per key (documented by Companies House); AIME keeps 0.5 s between calls"
    min_interval_seconds = 0.5
    credential_settings = ("companies_house_api_key",)
    contact_sources = ("companies_house",)

    def officers_page(self, number: str, start_index: int = 0) -> dict:
        """One page of the officers list. Raises RateLimited on 429 and SourceError on any other refusal or failure."""
        key = (self.settings.companies_house_api_key or "").strip()
        with self.ctx.http(auth=(key, ""), headers={"Accept": "application/json"}) as client:
            resp = client.get(OFFICERS.format(number=number), params={"items_per_page": PAGE_SIZE, "start_index": start_index})
        if resp.status_code == 429:
            raise RateLimited("Companies House said too many requests (HTTP 429)", _retry_after(resp))
        if resp.status_code == 404:
            return {"items": [], "total_results": 0}
        if resp.status_code in (401, 403):
            raise SourceError(f"Companies House refused the API key (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise SourceError(f"Companies House answered HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise SourceError("Companies House's answer could not be read") from None
        if not isinstance(data, dict):
            raise SourceError("Companies House's answer could not be read")
        return data

    def officers(self, session, company) -> list[PersonFinding]:
        """The company's serving officers, each page fetched through the budget guard."""
        from aime.contact_sources.budget import guarded_call

        number = company_number_of(company)
        if not number:
            return []
        found: dict[str, PersonFinding] = {}
        start = 0
        for _ in range(MAX_PAGES):
            page = guarded_call(session, self, lambda s=start: self.officers_page(number, s), country="GB",
                                note=f"officers of {number}")
            for f in parse_officers(page, number):
                found.setdefault(f.key, f)
            items = len(page.get("items") or [])
            start += items
            if not items or start >= int(page.get("total_results") or 0):
                break
        return list(found.values())

