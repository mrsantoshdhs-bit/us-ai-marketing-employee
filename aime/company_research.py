"""Turn a company's own pages into a research snapshot and evidence-backed contact records.

Everything stored is quoted from, or published on, the company's pages or SEC filings, with the URL:
its description, the services it lists, topics related to our services, the people it names, and the
emails and phone numbers it publishes. Nothing is inferred beyond matching published words.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.dedupe import normalize_name, registrable_domain
from aime.matching import match_services
from aime.models import Company, Contact, ResearchSnapshot
from aime.people import PersonFinding, looks_like_name, relevance, same_name, split_name
from aime.timeutil import utcnow

MIN_DESCRIPTION = 60


def describe(pages) -> tuple[str | None, str | None]:
    """The company's own description: meta description first, else the first substantial paragraph."""
    for page in sorted((p for p in pages if p.ok), key=lambda p: {"home": 0, "about": 1}.get(p.kind, 2)):
        for key in ("description", "og:description"):
            text = page.meta.get(key)
            if text and len(text) >= MIN_DESCRIPTION:
                return text[:500], page.url
    for page in (p for p in pages if p.ok and p.kind in ("home", "about")):
        for tag, text in page.blocks:
            if tag == "p" and len(text) >= 80:
                return text[:500], page.url
    return None, None


def services_offered(pages) -> list[str]:
    out: list[str] = []
    for page in (p for p in pages if p.ok and p.kind in ("services", "home")):
        for tag, text in page.blocks:
            words = text.split()
            if tag in ("h2", "h3", "h4") and 1 <= len(words) <= 7 and not any(
                    w.lower().strip(".,:") in ("our", "contact", "about", "team", "testimonials", "news", "blog")
                    for w in words) and text not in out:
                out.append(text)
            if len(out) >= 8:
                return out
    return out


def needs(pages) -> list[dict]:
    """Topics on the company's own pages that relate to our services, each with the sentence it came from."""
    best: dict[str, dict] = {}
    for page in (p for p in pages if p.ok):
        for tag, text in page.blocks:
            if tag not in ("p", "li", "div", "text") or len(text) < 40:
                continue
            for m in match_services(text):
                cur = best.get(m.service_id)
                if cur is None or m.score > cur["score"]:
                    best[m.service_id] = {"service": m.service_id, "keywords": m.keywords, "score": m.score,
                                          "quote": text[:300], "url": page.url}
    return sorted(best.values(), key=lambda d: -d["score"])[:5]


def published_contacts(pages, company_domain: str | None) -> tuple[list[dict], list[dict]]:
    """Emails and phones exactly as the pages publish them, with the nearest named person, if any."""
    site = registrable_domain(company_domain or "")
    emails: dict[str, dict] = {}
    phones: dict[str, dict] = {}
    for page in (p for p in pages if p.ok):
        segs = page.segments
        for email in page.emails:
            near = None
            for i, seg in enumerate(segs):
                if email in seg.lower():
                    for prev in reversed(segs[max(0, i - 4):i]):
                        if looks_like_name(prev):
                            near = prev.strip(" ,|·•–—-:")
                            break
                    break
            domain = email.split("@")[-1]
            emails.setdefault(email, {"email": email, "url": page.url, "near": near,
                                      "on_domain": bool(site) and registrable_domain(domain) == site})
        for phone in page.phones:
            digits = re.sub(r"\D", "", phone)
            if 10 <= len(digits) <= 11:
                phones.setdefault(digits, {"phone": phone, "url": page.url})
    return list(emails.values()), list(phones.values())


def build_snapshot(company: Company, pages, people: list[PersonFinding], status: str,
                   error: str | None = None) -> ResearchSnapshot:
    description, description_url = describe(pages)
    offered = services_offered(pages)
    topics = needs(pages)
    emails, phones = published_contacts(pages, company.domain)
    parts = []
    if description:
        parts.append(f'In its own words: "{description}"')
    if offered:
        parts.append("Services it lists: " + ", ".join(offered[:6]) + ".")
    if topics:
        parts.append("Topics on its site related to our services: "
                     + "; ".join(f"{t['service']} ({', '.join(t['keywords'][:3])})" for t in topics[:3]) + ".")
    if people:
        parts.append("People it names: " + "; ".join(f"{p.full_name}, {p.title}" for p in people[:5]) + ".")
    elif status in ("done", "no_people"):
        parts.append("No decision-maker is named on the pages read.")
    return ResearchSnapshot(
        company_id=company.id, fetched_at=utcnow(), status=status,
        pages=[{"url": p.url, "kind": p.kind, "status": p.status, "title": p.title[:200]} for p in pages],
        description=description, description_url=description_url, services_offered=offered, needs=topics,
        emails=emails, phones=phones,
        people=[{"name": p.full_name, "title": p.title, "confidence": p.confidence, "url": p.evidence_url,
                 "source": p.source} for p in people],
        summary=" ".join(parts) or None, error=error)


WITHDRAWN = "website_withdrawn"  # recorded from the website earlier, not found (or not valid) on a re-check


def _same_person(contact: Contact, finding: PersonFinding) -> bool:
    return same_name(normalize_name(f"{contact.first_name or ''} {contact.last_name or ''}"), finding.key)


def save_contact(session: Session, company: Company, finding: PersonFinding, service: str | None) -> tuple[Contact, bool]:
    """Create or refresh the contact for a researched person. Never overwrites a person's own details
    (inbound or manual contacts) and never adds an email: emails are assigned from published pages only."""
    rank = {"high": 2, "medium": 1, None: 0}
    existing = next((c for c in session.execute(select(Contact).where(Contact.company_id == company.id)).scalars()
                     if _same_person(c, finding)), None)
    score = relevance(finding.role_category, finding.title, service)
    if existing:
        if existing.source == WITHDRAWN:  # found again after a re-check withdrew it
            existing.source, existing.confidence = finding.source, None
        if existing.source in ("website", "sec_filing") and rank[finding.confidence] >= rank[existing.confidence]:
            existing.title, existing.role_category, existing.relevance = finding.title, finding.role_category, score
            existing.confidence, existing.evidence, existing.evidence_url = (finding.confidence, finding.evidence[:2000],
                                                                             finding.evidence_url[:500])
        return existing, False
    first, last = split_name(finding.full_name)
    contact = Contact(company_id=company.id, first_name=first[:80], last_name=last[:80], title=finding.title[:120],
                      source=finding.source, consent_status="none", role_category=finding.role_category,
                      relevance=score, confidence=finding.confidence, evidence=finding.evidence[:2000],
                      evidence_url=finding.evidence_url[:500], discovered_at=utcnow())
    session.add(contact)
    session.flush()
    return contact, True


def _page_key(url: str | None) -> str:
    parsed = urlparse(url or "")
    return f"{parsed.netloc.lower().removeprefix('www.')}{parsed.path.rstrip('/')}"


def withdraw_missing(session: Session, company: Company, people: list[PersonFinding], pages=None) -> list[Contact]:
    """After a successful re-read of the company's site: people recorded from it earlier who are no longer found
    (or no longer pass the rules) are withdrawn, never deleted. Anyone with an email queued or sent is kept for a
    person to review. With `pages`, only people whose page (or a page of the same kind) was read this time can
    be withdrawn: a team page that failed to load says nothing about its people."""
    from aime.models import FollowUp
    from aime.site_reader import classify_link

    def kind_of(url: str | None) -> str | None:
        return "home" if urlparse(url or "").path.rstrip("/") == "" else classify_link(url or "", "")

    ok_pages = {_page_key(p.url) for p in pages or [] if p.ok}
    ok_kinds = {p.kind for p in pages or [] if p.ok}
    withdrawn = []
    for c in session.execute(select(Contact).where(Contact.company_id == company.id, Contact.source == "website",
                                                   Contact.last_name.is_not(None))).scalars().all():
        if any(_same_person(c, f) for f in people):
            continue
        if pages is not None and _page_key(c.evidence_url) not in ok_pages and kind_of(c.evidence_url) not in ok_kinds:
            continue
        if session.execute(select(FollowUp.id).where(FollowUp.contact_id == c.id)).first():
            continue
        c.source = WITHDRAWN
        c.evidence = f"Not found when {company.domain} was re-read on {utcnow():%Y-%m-%d}. Earlier: {c.evidence}"[:2000]
        withdrawn.append(c)
    return withdrawn


def is_plausible_website(company: Company) -> bool:
    return bool(company.domain) and company.website_status in ("verified", "redirected")


def cik_of(company: Company) -> str | None:
    rid = company.source_record_id or ""
    if rid.startswith("cik:"):
        return rid[4:]
    m = re.search(r"CIK=(\d+)", company.source_url or "", re.I)
    return m.group(1) if m else None
