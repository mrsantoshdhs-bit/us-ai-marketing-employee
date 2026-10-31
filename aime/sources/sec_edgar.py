"""SEC EDGAR company search (official, free). Requires a descriptive User-Agent per SEC fair-access policy.

Uses the company browse feed filtered by SIC code and state, then the submissions API for
official business address, phone, and website as filed with the SEC.

The browse feed lists every company that ever filed, most of them long defunct, so a company
is accepted only if it filed an annual report recently (ACTIVE_WINDOW_DAYS).
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from aime.dedupe import domain_from_url
from aime.knowledge.geo import STATES
from aime.knowledge.industries import INDUSTRIES
from aime.sources.base import CompanyCandidate, ResearchFocus, Source

log = logging.getLogger("aime.sources.sec_edgar")
BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
ANNUAL_FORMS = {"10-K", "10-K405", "10-KSB", "10-KT"}
ACTIVE_WINDOW_DAYS = 548  # an annual report within ~18 months means the company is still operating and filing
BROWSE_COUNT = 40  # companies listed per SIC/state query
MAX_LOOKUPS_PER_QUERY = 25  # submissions lookups per query (polite to SEC; most listed companies are inactive)


def latest_annual_report(data: dict) -> date | None:
    recent = (data.get("filings") or {}).get("recent") or {}
    # strict=False: tolerate malformed SEC data instead of crashing the research run
    for filed, form in zip(recent.get("filingDate") or [], recent.get("form") or [], strict=False):
        if form in ANNUAL_FORMS:
            try:
                return date.fromisoformat(filed)
            except ValueError:
                return None
    return None
_CIK_RE = re.compile(r"CIK=(\d{1,10})", re.IGNORECASE)
# SEC's company-list feed currently emits Perl placeholders such as "ARRAY(0x56198132cf18)" instead of names.
_PLACEHOLDER_NAME = re.compile(r"^ARRAY\(0x[0-9a-f]+\)$", re.IGNORECASE)


def _usable_name(text: str | None) -> str | None:
    value = (text or "").strip()
    return None if not value or _PLACEHOLDER_NAME.match(value) else value


def parse_company_feed(xml_text: str) -> list[dict]:
    """Return [{cik, name}] from an EDGAR browse Atom feed (lenient to minor format changes).

    The CIK is required; the name may be None because the feed's names are unreliable. The
    authoritative name always comes from the submissions API in SECEdgarSource._detail.
    """
    out: list[dict] = []
    try:
        if len(xml_text) > 5_000_000:
            return out  # far larger than any EDGAR company list: refuse to parse
        # Python 3.12's bundled expat limits entity expansion (billion-laughs); input is size-capped above.
        root = ET.fromstring(xml_text)  # noqa: S314
    except ET.ParseError:
        return out
    for entry in root.iter():
        if not entry.tag.endswith("entry"):
            continue
        cik, name = None, None
        for el in entry.iter():
            tag = el.tag.rsplit("}", 1)[-1].lower()
            if tag == "cik" and el.text:
                cik = el.text.strip()
            elif tag in {"name", "conformed-name", "title"} and not name:
                name = _usable_name(el.text)
            elif tag == "link" and not cik:
                m = _CIK_RE.search(el.attrib.get("href", ""))
                if m:
                    cik = m.group(1)
            elif tag == "id" and el.text and not cik:
                m = re.search(r"cik=(\d+)", el.text, re.IGNORECASE)
                if m:
                    cik = m.group(1)
        if cik:
            out.append({"cik": cik.zfill(10), "name": name})
    return out


class SECEdgarSource(Source):
    name = "sec_edgar"
    title = "SEC EDGAR (official filings)"
    description = "US-registered companies by SIC industry code and state, with addresses filed with the SEC."
    per_query = 10

    def configured(self) -> tuple[bool, str]:
        ua = self.settings.sec_user_agent.strip()
        if not ua:
            return False, "NOT CONFIGURED: set SEC_USER_AGENT (organization name + contact email)"
        if "@" not in ua:
            return False, "SEC_USER_AGENT must include a contact email"
        if not self.settings.network_enabled:
            return False, "Network disabled"
        return True, "Configured"

    def _client(self):
        return self.ctx.http(headers={"User-Agent": self.settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"})

    def fetch(self, focus: ResearchFocus) -> list[CompanyCandidate]:
        out: list[CompanyCandidate] = []
        with self._client() as client:
            for industry_id in focus.industries:
                ind = INDUSTRIES.get(industry_id)
                if not ind or not ind.sic:
                    continue
                for state in focus.states:
                    if state not in STATES:
                        continue
                    for sic in ind.sic:  # every SIC code of the industry (e.g. legal, accounting and consulting)
                        resp = client.get(BROWSE, params={
                            "action": "getcompany", "SIC": sic, "State": state, "type": "10-K",
                            "owner": "include", "count": BROWSE_COUNT, "output": "atom",
                        })
                        resp.raise_for_status()
                        accepted = inactive = 0
                        for row in parse_company_feed(resp.text)[:MAX_LOOKUPS_PER_QUERY]:
                            if accepted >= self.per_query:
                                break
                            cand = self._detail(client, row, industry_id)
                            if cand:
                                out.append(cand)
                                accepted += 1
                            else:
                                inactive += 1
                            time.sleep(0.15)  # stay well under SEC's 10 requests/second
                        log.info("SEC SIC %s %s: %s active companies accepted, %s inactive or unusable skipped",
                                 sic, state, accepted, inactive)
                    if len(out) >= focus.limit:
                        return out
        return out

    def _detail(self, client, row: dict, industry_id: str) -> CompanyCandidate | None:
        resp = client.get(SUBMISSIONS.format(cik=row["cik"]))
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:  # one unreadable answer costs one company, not the source's whole run
            return None
        name = _usable_name(data.get("name")) or row.get("name")
        if not name:
            return None  # never store a company without a real name
        last_annual = latest_annual_report(data)
        if last_annual is None or date.today() - last_annual > timedelta(days=ACTIVE_WINDOW_DAYS):
            return None  # defunct or no longer filing: not a real prospect
        addr = (data.get("addresses") or {}).get("business") or {}
        state = (addr.get("stateOrCountry") or "").upper()
        city = (addr.get("city") or "").title() or None
        website = (data.get("website") or "").strip() or None
        if website and not website.startswith("http"):
            website = "https://" + website
        sic_description = data.get("sicDescription")
        return CompanyCandidate(
            name=name,
            source="sec_edgar",
            source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={row['cik']}",
            source_record_id=f"cik:{row['cik']}",
            website=website,
            domain=domain_from_url(website),
            state=state if state in STATES else None,
            city=city,
            is_us=state in STATES,
            industry=industry_id,
            description=sic_description,
            # Registry facts only (existence, industry, address): evidence of fit, not of any need or intent.
            signal_text=f"SEC-registered company. Industry (SIC): {sic_description or 'n/a'}. "
                        f"Business address: {city or 'n/a'}, {state or 'n/a'}. Latest annual report: {last_annual}.",
            signal_type="filing",
            phone=data.get("phone") or None,
            verified=True,
            extra={"sic": data.get("sic"), "category": data.get("category")},
        )
