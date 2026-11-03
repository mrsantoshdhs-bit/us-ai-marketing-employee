"""Country-wise daily targets (settings section "targets") and today's progress towards them, counted from the records
themselves: no separate tally is kept, so the numbers cannot drift from the CRM.

Targets are goals, not quotas. Nothing here raises research.max_new_companies_per_run, a sending limit or any other cap,
and no quality or compliance rule is relaxed to reach a number: a shortfall is reported with its reason instead (the
per-run cap reached, a source not read or giving nothing new, records rejected by validation, leads disqualified, no
verified email address). For each target country, today (in that country's time zone) counts:

- companies found: companies created today with that country (sample data and merged duplicates left out);
- leads created: leads created today for those companies;
- verified leads: of those, the ones not disqualified whose company is confirmed (its website checked live, or a record
  verified from an official registry);
- contactable leads: of the verified ones, those whose company has a contact with a usable email address (published by the
  company, or its own published inbox) that is not on the suppression list;
- qualified: leads created today that are qualified now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

VERIFIED_WEBSITE = ("verified", "redirected")


@dataclass
class Progress:
    country: str
    day: str
    timezone: str
    goal_verified: int
    goal_contactable: int
    companies_found: int = 0
    leads_created: int = 0
    verified_leads: int = 0
    contactable_leads: int = 0
    qualified: int = 0
    disqualified: int = 0
    shortfall_verified: int = 0
    shortfall_contactable: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def pct_verified(self) -> int:
        return min(100, round(100 * self.verified_leads / self.goal_verified)) if self.goal_verified else 0

    @property
    def pct_contactable(self) -> int:
        return min(100, round(100 * self.contactable_leads / self.goal_contactable)) if self.goal_contactable else 0


def day_window(tz_name: str, now_utc: datetime) -> tuple[datetime, datetime, str]:
    """Today in this time zone, as naive UTC bounds (how the database stores times) and its date."""
    tz = ZoneInfo(tz_name)
    local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    start = datetime.combine(local.date(), time(0), tzinfo=tz)
    to_utc = lambda d: d.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)  # noqa: E731
    return to_utc(start), to_utc(start + timedelta(days=1)), local.date().isoformat()


def _research_runs(session: Session, start: datetime, end: datetime) -> list:
    from aime.models import AgentRun

    return session.execute(select(AgentRun).where(AgentRun.agent == "research", AgentRun.started_at >= start,
                                                  AgentRun.started_at < end).order_by(AgentRun.id)).scalars().all()


def progress(session: Session, settings, now: datetime | None = None) -> list[Progress]:
    """Today's progress for every target in the settings, each with the reasons it falls short."""
    from aime import countries, settings_store
    from aime.contact_validation import STATUS_OK
    from aime.crm_ops import CONTACT_NOT_SUPPRESSED
    from aime.models import Company, Contact, Lead
    from aime.timeutil import utcnow

    now = now or utcnow()
    cap = int(settings.section("research").get("max_new_companies_per_run", 40))
    out = []
    for t in settings_store.get(settings, "targets")["targets"]:
        entry = countries.country(settings, t["country"]) or {}
        tz = entry.get("timezone") or "America/New_York"
        start, end, day = day_window(tz, now)
        p = Progress(t["country"], day, tz, t["daily_verified_leads"], t["daily_contactable_leads"])
        companies = session.execute(select(Company).where(Company.country == t["country"], Company.created_at >= start,
                                                          Company.created_at < end, Company.merged_into_id.is_(None),
                                                          Company.is_sample.is_(False))).scalars().all()
        p.companies_found = len(companies)
        ids = [c.id for c in companies]
        leads = session.execute(select(Lead).where(Lead.company_id.in_(ids or [-1]), Lead.created_at >= start,
                                                   Lead.created_at < end, Lead.status != "merged")).scalars().all()
        p.leads_created = len(leads)
        by_id = {c.id: c for c in companies}
        reachable = set(session.execute(select(Contact.company_id).where(
            Contact.company_id.in_(ids or [-1]), Contact.email.is_not(None), Contact.email_status.in_(STATUS_OK),
            CONTACT_NOT_SUPPRESSED)).scalars())
        for lead in leads:
            company = by_id[lead.company_id]
            if lead.status == "disqualified":
                p.disqualified += 1
                continue
            if lead.status == "qualified":
                p.qualified += 1
            if company.website_status in VERIFIED_WEBSITE or company.verified:
                p.verified_leads += 1
                if company.id in reachable:
                    p.contactable_leads += 1
        p.shortfall_verified = max(0, p.goal_verified - p.verified_leads)
        p.shortfall_contactable = max(0, p.goal_contactable - p.contactable_leads)
        p.reasons = _reasons(session, settings, p, start, end, cap)
        out.append(p)
    return out


def _reasons(session: Session, settings, p: Progress, start: datetime, end: datetime, cap: int) -> list[str]:
    from aime import countries

    if not (p.goal_verified or p.goal_contactable):
        return ["No goal set for this country (Settings › Daily targets)"]
    if not (p.shortfall_verified or p.shortfall_contactable):
        return []
    why = []
    if p.country not in countries.target_countries(settings):
        why.append(f"{p.country} is not an enabled target country, so none of its companies are kept")
    covering = countries.sources_for(settings, p.country)
    if not [s for s in covering if s != "csv_import"]:
        why.append(f"No public discovery source covers {p.country}; only your own CSV lists can add companies")
    runs = _research_runs(session, start, end)
    if not runs:
        why.append("Research has not run today yet")
    for run in runs:
        o = run.output_summary or {}
        if o.get("limit_reached"):
            why.append(f"Discovery reached its per-run cap of {cap} new companies (research.max_new_companies_per_run). Targets "
                       "never raise it")
        for src in o.get("sources") or []:
            if isinstance(src, dict) and src.get("configured") and not src.get("ok"):
                why.append(f"Source {src.get('name')} was not read: {src.get('message')}")
            elif isinstance(src, dict) and src.get("ok") and not src.get("records"):
                why.append(f"Source {src.get('name')} gave nothing new (exhausted for now)")
        seen, bad = int(o.get("candidates_seen") or 0), int(o.get("invalid_skipped") or 0)
        if seen and bad:
            why.append(f"Validation rejected {round(100 * bad / seen)}% of the records found ({bad} of {seen})")
        if run.status not in ("success", "running"):
            why.append(f"A research run ended {run.status}")
    if p.leads_created and p.disqualified:
        why.append(f"{p.disqualified} of {p.leads_created} new lead(s) were disqualified (competitor, suppression or location)")
    if p.leads_created > p.verified_leads + p.disqualified:
        why.append(f"{p.leads_created - p.verified_leads - p.disqualified} new lead(s) wait for their website check")
    if p.shortfall_contactable and p.verified_leads > p.contactable_leads:
        why.append(f"{p.verified_leads - p.contactable_leads} verified lead(s) have no usable, verified email address yet")
    return list(dict.fromkeys(why)) or ["Fewer new companies than the goal were found today"]
