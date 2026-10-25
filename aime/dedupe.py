"""Company normalization and duplicate detection."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.models import Company

LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "l l c", "ltd", "limited", "corp", "corporation", "co", "company",
    "plc", "lp", "llp", "pc", "pllc", "gmbh",
}
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "aol.com",
    "icloud.com", "me.com", "proton.me", "protonmail.com", "gmx.com", "msn.com", "ymail.com",
}
JOB_BOARD_DOMAINS = {
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "ycombinator.com", "linkedin.com",
    "wellfound.com", "angel.co", "notion.site", "notion.so", "google.com", "forms.gle", "bit.ly",
    "breezy.hr", "jobvite.com", "smartrecruiters.com", "bamboohr.com", "recruitee.com", "rippling.com",
    "workatastartup.com", "news.ycombinator.com", "github.com", "gitlab.com", "docs.google.com",
    "typeform.com", "calendly.com", "twitter.com", "x.com", "medium.com", "substack.com",
    # short links, hiring platforms, media and reference sites that are not the company's own website
    "grnh.se", "deel.com", "youtu.be", "youtube.com", "vimeo.com", "loom.com", "techcrunch.com",
    "mercurynews.com", "bloomberg.com", "forbes.com", "nytimes.com", "wsj.com", "reuters.com",
    "businessinsider.com", "crunchbase.com", "glassdoor.com", "indeed.com", "builtin.com", "levels.fyi",
    "hnhiring.com", "wikipedia.org", "arxiv.org", "apple.com", "facebook.com", "instagram.com", "tiktok.com",
    "dover.com", "teamtailor.com", "pinpointhq.com", "jobs.lever.co", "welcometothejungle.com", "otta.com",
}
_MULTI_PART_TLDS = {"co.uk", "com.au", "co.in", "com.br", "co.jp"}


def normalize_name(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"\(.*?\)", " ", n)
    n = n.replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    words = [w for w in n.split() if w]
    if words and words[0] == "the":
        words = words[1:]
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def registrable_domain(host: str) -> str:
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_PART_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlparse(raw).hostname
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    return registrable_domain(host)


def domain_from_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    dom = registrable_domain(email.rsplit("@", 1)[1])
    return None if dom in FREE_EMAIL_DOMAINS else dom


def is_job_board(domain: str | None) -> bool:
    return bool(domain) and (domain in JOB_BOARD_DOMAINS)


def find_duplicate(session: Session, name: str, domain: str | None, state: str | None) -> Company | None:
    """Find an existing (non-merged) company by domain, exact normalized name, or close name in the same state."""
    active = Company.merged_into_id.is_(None)
    if domain:
        hit = session.execute(select(Company).where(active, Company.domain == domain)).scalars().first()
        if hit:
            return hit
    norm = normalize_name(name)
    if not norm:
        return None
    candidates = session.execute(select(Company).where(active, Company.normalized_name == norm)).scalars().all()
    for c in candidates:
        if not state or not c.state or c.state == state:
            if domain and c.domain and c.domain != domain:
                continue  # same name, different website: different companies
            return c
    if len(norm) >= 6:
        prefix = norm[:3]
        stmt = select(Company).where(active, func.substr(Company.normalized_name, 1, 3) == prefix)
        if state:
            stmt = stmt.where(Company.state == state)
        for c in session.execute(stmt).scalars():
            if domain and c.domain and c.domain != domain:
                continue
            if SequenceMatcher(None, norm, c.normalized_name).ratio() >= 0.93:
                return c
    return None
