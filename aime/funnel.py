"""The lead funnel, defined once (Option B, Phase 4). Each stage is a plain-English definition and the SQL that tests it;
the Sales Manager's report (aime.manager_report) counts its contact figures with these same conditions, so the two cannot
drift apart.

A company moves through these stages, each one a stricter claim than the last:

1. discovered company - a company in the CRM (merged duplicates and sample data left out). Nothing about it is verified;
2. identified decision-maker - it has a named person (a last name) found on its own website, in its SEC filings or on an
   official company register, whose title decides (not a former employee, an assistant, an office manager or a social
   media role). A name only: no usable email is implied;
3. contactable prospect - it has a contact with an email address that may be written to: not opted out, not on the
   do-not-contact list, not bounced, not on a domain without a mail server. A company inbox (info@) counts here;
4. verified contact - it has a contactable contact whose own address is verified: published by the company next to the
   person on its own domain, which takes mail, or verified by an approved verification service. A company inbox
   (a role address such as info@ or sales@) is never verified;
5. qualified lead - its lead is qualified now (by scoring or by a person);
6. outreach-ready lead - a qualified lead, not a possible competitor, whose company has a verified decision-maker: a
   decision-maker (2) whose own address is verified (4). An unverified company record, a name with no verified address and
   a company inbox are never an outreach-ready, verified decision-maker lead.

`reached` counts every company that meets a stage's own test; `in_funnel` counts those that meet it and every stage before
it, so a company with an inbox but no named person is contactable yet stops at "discovered company" in the funnel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aime.models import Company, Contact, Lead

DECISION_MAKER_SOURCES = ("website", "sec_filing", "companies_house")
# "published" as above; "verified" (and the older "valid") from an approved verification service (aime.verification).
# Never "role_address", "unchecked", "mx_ok", "risky", "no_mail_server" or "bounced".
VERIFIED_EMAIL_STATUSES = ("published", "verified", "valid")
NOT_CONTACTABLE_STATUSES = ("bounced", "no_mail_server", "invalid")  # every status aime.verification reads as INVALID


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    definition: str


STAGES = (
    Stage("discovered_company", "Discovered company",
          "A company in the CRM (merged duplicates and sample data left out). Nothing about it is verified yet."),
    Stage("identified_decision_maker", "Identified decision-maker",
          "The company has a named person (a last name) found on its own website, in its SEC filings or on an official company "
          "register, whose title decides. A name only: no usable email is implied."),
    Stage("contactable_prospect", "Contactable prospect",
          "The company has a contact with an email address that may be written to: not opted out, not on the do-not-contact "
          "list, not bounced and not on a domain without a mail server. A company inbox counts here."),
    Stage("verified_contact", "Verified contact",
          "The company has a contactable contact whose own address is verified: published by the company next to the person "
          "on its own domain, which takes mail, or verified by an approved verification service. A company inbox is never "
          "verified."),
    Stage("qualified_lead", "Qualified lead", "The company's lead is qualified now (by scoring or by a person)."),
    Stage("outreach_ready_lead", "Outreach-ready lead",
          "A qualified lead, not a possible competitor, whose company has a verified decision-maker: a named decision-maker "
          "whose own address is verified. A company inbox or an unverified record never counts."),
)
STAGE_KEYS = tuple(s.key for s in STAGES)


# --------------------------------------------------------------------------- the conditions (SQL)

def real_company() -> tuple:
    return (Company.merged_into_id.is_(None), Company.is_sample.is_(False))


def real_contact() -> tuple:
    return (Contact.is_sample.is_(False),)


def decision_maker() -> tuple:
    """A named person from a researched source whose title decides."""
    from aime.agents.analytics import deciding_title

    return (Contact.source.in_(DECISION_MAKER_SOURCES), Contact.last_name.is_not(None), deciding_title())


def contactable() -> tuple:
    from aime.crm_ops import CONTACT_NOT_SUPPRESSED

    return (Contact.email.is_not(None), Contact.email != "", Contact.consent_status != "opted_out", CONTACT_NOT_SUPPRESSED,
            or_(Contact.email_status.is_(None), Contact.email_status.not_in(NOT_CONTACTABLE_STATUSES)))


def verified_email() -> tuple:
    from aime.contact_validation import ROLE_LOCALS

    local_part = func.lower(func.substr(Contact.email, 1, func.instr(Contact.email, "@") - 1))
    return (Contact.email_status.in_(VERIFIED_EMAIL_STATUSES), local_part.not_in(sorted(ROLE_LOCALS)))


def verified_decision_maker() -> tuple:
    return (*decision_maker(), *contactable(), *verified_email())


# --------------------------------------------------------------------------- the same tests in Python, for one contact

def _like(pattern: str, text: str) -> bool:
    rx = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.fullmatch(rx, text, re.S) is not None


def deciding(title: str | None) -> bool:
    """aime.agents.analytics.deciding_title for one title."""
    from aime.agents.analytics import DECIDING_ANYWAY, NOT_DECIDING

    if title is None:
        return True
    t = title.lower()
    return (not any(_like(p, t) for p in NOT_DECIDING)) or (any(_like(p, t) for p in DECIDING_ANYWAY) and not _like("former %", t))


def is_decision_maker(contact: Contact) -> bool:
    return contact.source in DECISION_MAKER_SOURCES and bool(contact.last_name) and deciding(contact.title)


def is_verified_decision_maker(contact: Contact) -> bool:
    """A decision-maker whose own address is verified and may be written to. A role address never is (aime.verification)."""
    from aime.verification import VERIFIED_STATUSES, status_of

    return (is_decision_maker(contact) and contact.consent_status != "opted_out" and not contact.is_sample
            and status_of(contact) in VERIFIED_STATUSES)


# --------------------------------------------------------------------------- classifying companies

@dataclass
class CompanyFunnel:
    company_id: int
    flags: dict[str, bool] = field(default_factory=dict)

    @property
    def stage(self) -> str:
        """The furthest stage reached with every stage before it."""
        last = STAGE_KEYS[0]
        for key in STAGE_KEYS:
            if not self.flags.get(key):
                break
            last = key
        return last


def _companies_with(session: Session, *conds, ids=None) -> set[int]:
    """`ids`: a list of company ids, or a SELECT of them (every real company: no list of tens of thousands of parameters,
    which SQLite refuses past 32,766)."""
    stmt = select(Contact.company_id).where(Contact.company_id.is_not(None), *real_contact(), *conds)
    if ids is not None:
        stmt = stmt.where(Contact.company_id.in_(ids if not isinstance(ids, list) else (ids or [-1])))
    return set(session.execute(stmt.distinct()).scalars())


def classify(session: Session, company_ids: list[int] | None = None) -> dict[int, CompanyFunnel]:
    """Each discovered company (or those given) with the stages it reaches."""
    stmt = select(Company.id, Company.is_competitor).where(*real_company())
    if company_ids is not None:
        stmt = stmt.where(Company.id.in_(company_ids or [-1]))
    companies = dict(session.execute(stmt).all())
    # every company: the same set as a subquery, not as one parameter per company (Phase 8: perf_bench at 100x)
    ids = list(companies) if company_ids is not None else select(Company.id).where(*real_company()).scalar_subquery()
    dm = _companies_with(session, *decision_maker(), ids=ids)
    reach = _companies_with(session, *contactable(), ids=ids)
    verified = _companies_with(session, *contactable(), *verified_email(), ids=ids)
    verified_dm = _companies_with(session, *verified_decision_maker(), ids=ids)
    qualified = set(session.execute(select(Lead.company_id).where(Lead.status == "qualified", Lead.is_sample.is_(False),
                                                                  Lead.company_id.in_(ids if not isinstance(ids, list) else (ids or [-1])))
                                    .distinct()).scalars())
    out = {}
    for cid, competitor in companies.items():
        out[cid] = CompanyFunnel(cid, {
            "discovered_company": True,
            "identified_decision_maker": cid in dm,
            "contactable_prospect": cid in reach,
            "verified_contact": cid in verified,
            "qualified_lead": cid in qualified,
            "outreach_ready_lead": cid in qualified and cid in verified_dm and not competitor,
        })
    return out


def summary(session: Session, company_ids: list[int] | None = None) -> dict:
    """Counts per stage, with the definitions: `reached` (the stage's own test) and `in_funnel` (it and all before it)."""
    rows = classify(session, company_ids)
    stages = []
    for i, st in enumerate(STAGES):
        reached = sum(1 for r in rows.values() if r.flags[st.key])
        in_funnel = sum(1 for r in rows.values() if all(r.flags[k] for k in STAGE_KEYS[:i + 1]))
        stages.append({"key": st.key, "label": st.label, "definition": st.definition, "reached": reached, "in_funnel": in_funnel})
    return {"companies": len(rows), "stages": stages}
