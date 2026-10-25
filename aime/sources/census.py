"""US Census County Business Patterns: establishment counts by state and industry (market sizing).

Free, but the API now requires a key: https://api.census.gov/data/key_signup.html
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.knowledge.geo import STATE_FIPS
from aime.knowledge.industries import INDUSTRIES
from aime.models import MarketSegment
from aime.timeutil import utcnow

CBP_YEAR = 2022
CBP_URL = "https://api.census.gov/data/{year}/cbp"


def parse_cbp(rows: list[list[str]], industry_id: str, naics: str, year: int) -> list[dict]:
    if not rows:
        return []
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    out = []
    for r in rows[1:]:
        state = STATE_FIPS.get(r[idx["state"]])
        if not state:
            continue
        try:
            estab = int(r[idx["ESTAB"]] or 0)
        except (ValueError, KeyError):
            continue
        emp = None
        if "EMP" in idx:
            try:
                emp = int(r[idx["EMP"]] or 0)
            except ValueError:
                emp = None
        out.append({
            "state": state, "naics": naics, "industry": industry_id,
            "label": r[idx["NAICS2017_LABEL"]] if "NAICS2017_LABEL" in idx else None,
            "establishments": estab, "employees": emp, "year": year,
        })
    return out


class CensusMarketSource:
    name = "census_cbp"
    title = "US Census County Business Patterns"

    def __init__(self, ctx):
        self.ctx = ctx
        self.settings = ctx.settings

    def configured(self) -> tuple[bool, str]:
        if not self.settings.census_api_key:
            return False, "NOT CONFIGURED: set CENSUS_API_KEY (free key)"
        if not self.settings.network_enabled:
            return False, "Network disabled"
        return True, "Configured"

    def refresh(self, session: Session, industries: list[str] | None = None) -> int:
        """Load establishment counts for each target industry (one request per NAICS code)."""
        count = 0
        with self.ctx.http() as client:
            for industry_id in industries or list(INDUSTRIES):
                ind = INDUSTRIES[industry_id]
                for naics in ind.naics:
                    resp = client.get(CBP_URL.format(year=CBP_YEAR), params={
                        "get": "ESTAB,EMP,NAICS2017_LABEL", "for": "state:*",
                        "NAICS2017": naics, "key": self.settings.census_api_key,
                    })
                    resp.raise_for_status()
                    for row in parse_cbp(resp.json(), industry_id, naics, CBP_YEAR):
                        existing = session.execute(select(MarketSegment).where(
                            MarketSegment.state == row["state"], MarketSegment.naics == naics,
                            MarketSegment.year == CBP_YEAR)).scalars().first()
                        if existing:
                            existing.establishments = row["establishments"]
                            existing.employees = row["employees"]
                            existing.fetched_at = utcnow()
                        else:
                            session.add(MarketSegment(**row))
                        count += 1
        return count
