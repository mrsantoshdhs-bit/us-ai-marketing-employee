"""Campaign audiences: which leads a campaign is for, as a saved filter (industries, US states, grades, services,
lead sources, a minimum score). The Campaigns page shows how many leads match and can attach the matching leads that
belong to no campaign yet, so the campaign's results are counted from them (aime.performance.campaign_stats).
Attaching sends nothing and changes no lead's status: it only records which campaign a lead is credited to."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES
from aime.models import Company, Lead

FIELDS = ("industries", "states", "grades", "services", "sources")


def parse(form: dict) -> dict:
    """Clean an audience from the form's comma-separated fields; unknown values are dropped."""

    def items(name: str) -> list[str]:
        return [x.strip() for x in re.split(r"[,\s]+", str(form.get(name, "") or "")) if x.strip()]

    out = {"industries": [x for x in items("industries") if x in INDUSTRIES],
           "states": [x.upper() for x in items("states") if re.fullmatch(r"[A-Za-z]{2}", x)],
           "grades": [x.upper() for x in items("grades") if x.upper() in ("A", "B", "C", "D")],
           "services": [x for x in items("services") if x in SERVICES],
           "sources": [x.lower() for x in items("sources") if re.fullmatch(r"[a-z_]{2,30}", x.lower())]}
    try:
        score = int(str(form.get("min_score", "") or "0").strip() or 0)
    except ValueError:
        score = 0
    out["min_score"] = max(0, min(100, score))
    return out


def condition(audience: dict):
    parts = [Lead.status.not_in(("merged", "disqualified", "converted"))]  # a customer already won is no open lead
    a = audience or {}
    if a.get("industries"):
        parts.append(Company.industry.in_(a["industries"]))
    if a.get("states"):
        parts.append(Company.state.in_(a["states"]))
    if a.get("grades"):
        parts.append(Lead.grade.in_(a["grades"]))
    if a.get("services"):
        parts.append(Lead.primary_service.in_(a["services"]))
    if a.get("sources"):
        parts.append(Lead.source.in_(a["sources"]))
    if a.get("min_score"):
        parts.append(Lead.score >= int(a["min_score"]))
    return parts


def matching(session: Session, audience: dict) -> list[Lead]:
    return list(session.execute(select(Lead).join(Company, Company.id == Lead.company_id)
                                .where(*condition(audience)).order_by(Lead.score.desc(), Lead.id)).scalars())


def describe(audience: dict) -> str:
    a = audience or {}
    bits = [f"{k}: {', '.join(a[k])}" for k in FIELDS if a.get(k)]
    if a.get("min_score"):
        bits.append(f"score ≥ {a['min_score']}")
    return "; ".join(bits) or "every open lead"
