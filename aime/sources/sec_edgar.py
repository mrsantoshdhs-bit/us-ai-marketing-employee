"""SEC EDGAR company search (official, free). Requires a descriptive User-Agent per SEC fair-access policy.

Uses the company browse feed filtered by SIC code and state, then the submissions API for
official business address, phone, and website as filed with the SEC.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

from aime.dedupe import domain_from_url
from aime.knowledge.geo import STATES
from aime.knowledge.industries import INDUSTRIES
from aime.sources.base import CompanyCandidate, ResearchFocus, Source

BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
_CIK_RE = re.compile(r"CIK=(\d{1,10})", re.IGNORECASE)


def parse_company_feed(xml_text: str) -> list[dict]:
    """Return [{cik, name}] from an EDGAR browse Atom feed (lenient to minor format changes)."""
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
            elif tag in {"name", "conformed-name"} and el.text and not name:
                name = el.text.strip()
            elif tag == "title" and el.text and not name:
                name = el.text.strip()
            elif tag == "link" and not cik:
                m = _CIK_RE.search(el.attrib.get("href", ""))
                if m:
                    cik = m.group(1)
            elif tag == "id" and el.text and not cik:
                m = re.search(r"cik=(\d+)", el.text, re.IGNORECASE)
                if m:
                    cik = m.group(1)
        if cik and name:
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
                    for sic in ind.sic[:1]:
                        resp = client.get(BROWSE, params={
                            "action": "getcompany", "SIC": sic, "State": state, "type": "10-K",
                            "owner": "include", "count": self.per_query, "output": "atom",
                        })
                        resp.raise_for_status()
                        for row in parse_company_feed(resp.text)[: self.per_query]:
                            cand = self._detail(client, row, industry_id)
                            if cand:
                                out.append(cand)
                            time.sleep(0.15)  # stay well under SEC's 10 requests/second
                    if len(out) >= focus.limit:
                        return out
        return out

    def _detail(self, client, row: dict, industry_id: str) -> CompanyCandidate | None:
        resp = client.get(SUBMISSIONS.format(cik=row["cik"]))
        if resp.status_code != 200:
            return None
        data = resp.json()
        addr = (data.get("addresses") or {}).get("business") or {}
        state = (addr.get("stateOrCountry") or "").upper()
        website = (data.get("website") or "").strip() or None
        if website and not website.startswith("http"):
            website = "https://" + website
        return CompanyCandidate(
            name=data.get("name") or row["name"],
            source="sec_edgar",
            source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={row['cik']}",
            source_record_id=f"cik:{row['cik']}",
            website=website,
            domain=domain_from_url(website),
            state=state if state in STATES else None,
            city=(addr.get("city") or "").title() or None,
            is_us=state in STATES,
            industry=industry_id,
            description=data.get("sicDescription"),
            signal_text=None,
            signal_type="filing",
            phone=data.get("phone") or None,
            verified=True,
            extra={"sic": data.get("sic"), "category": data.get("category")},
        )
