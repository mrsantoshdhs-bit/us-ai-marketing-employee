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
    "applytojob.com", "curriculo.me", "jazz.co", "jazzhr.com", "workday.com", "myworkdayjobs.com", "icims.com",
    "paylocity.com", "gem.com", "ycombinator.co",
}
_MULTI_PART_TLDS = {"co.uk", "com.au", "co.in", "com.br", "co.jp"}
# Hosting platforms give each customer a subdomain: there the full host is the company's site, not the platform.
HOSTED_PLATFORMS = {"myshopify.com", "wixsite.com", "github.io", "squarespace.com", "webflow.io", "wordpress.com",
                    "blogspot.com", "netlify.app", "vercel.app", "herokuapp.com", "carrd.co", "weebly.com",
                    "godaddysites.com", "square.site", "business.site", "framer.website", "pages.dev", "web.app",
                    "firebaseapp.com", "glitch.me", "replit.app", "bubbleapps.io", "softr.app", "notion.site"}


def clean_company_name(name: str) -> str:
    """The name without a web address pasted into it ("Snout https://snout.com/" -> "Snout"); unchanged when nothing
    else is left."""
    cleaned = re.sub(r"\s*[(\[]?\s*(?:https?://|www\.)\S*", " ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -|,;:([")
    return cleaned or name.strip()


# A web address used as a name: "meetzentro.com", "Zentro.com", "www.zentro.io".
_DOMAIN_NAME_RE = re.compile(r"^(?:www\.)?([a-z0-9][a-z0-9-]*)(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|ai|co|app|dev|xyz|so|tech|us|inc|health|bio|me)$",
                             re.I)
# A post's first words that describe rather than name: "Looking for a co-founder", "SaaS Startup", "AI x Healthcare Startup".
_DESCRIPTION_RE = re.compile(r"^(?:looking|seeking|hiring|join|building|we|our)\b|\b(?:start-?up|stealth)$|\sx\s", re.I)


def describes_rather_than_names(name: str) -> bool:
    return bool(_DESCRIPTION_RE.search(name.strip(" .,-")))


def domain_part_of_name(name: str) -> str | None:
    """The name inside a web address used as a name ("Zentro.com" -> "Zentro"); None when it is not a web address."""
    m = _DOMAIN_NAME_RE.match(name.strip())
    return m.group(1) if m else None


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


def name_from_title(title: str | None, domain: str | None) -> str | None:
    """The company's own name from its homepage title ("Digital Hub Solution | The best ..."), only when one part of
    the title spells the domain's name ("digitalhubsolution.com"). None when nothing matches: no name is guessed."""
    if not title or not domain:
        return None
    label = re.sub(r"[^a-z0-9]", "", registrable_domain(domain).split(".")[0].lower())
    for part in re.split(r"\s+[|\-\u2013\u2014\u00b7\u2022]\s+|\s*:\s+", title):
        part = " ".join(part.split())
        if label and 2 <= len(part) <= 80 and re.sub(r"[^a-z0-9]", "", part.lower()) == label:
            return part
    return None


def registrable_domain(host: str) -> str:
    host = host.lower().rsplit("@", 1)[-1]
    if host.count(":") == 1:  # "www.example.com:443"; several colons are an IPv6 address
        host = host.split(":")[0]
    host = host.strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_PART_TLDS | HOSTED_PLATFORMS:
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


DESCRIPTOR_WORDS = {
    "labs", "lab", "technologies", "technology", "tech", "software", "systems", "solutions", "group",
    "holdings", "hq", "app", "ai", "io", "studio", "studios", "platform", "global", "usa", "us",
}


def core_name(normalized: str) -> str:
    """Name without generic descriptors, used only together with a same-state check."""
    words = [w for w in normalized.split() if w not in DESCRIPTOR_WORDS]
    return " ".join(words) or normalized


def find_duplicate(session: Session, name: str, domain: str | None, state: str | None,
                   source_record_id: str | None = None, source: str | None = None) -> Company | None:
    """Find an existing (non-merged) company by source record, domain, normalized name, core name,
    or a close name in the same state.

    A name alone matches only with the same state. When either side has no state, only the same source
    listing the same name again counts (a company re-posting in a later hiring thread); "Atlas" (remote) from
    a hiring post is not "ATLAS CORP" (NJ) from an SEC filing."""
    active = Company.merged_into_id.is_(None)
    if source_record_id:
        hit = session.execute(select(Company).where(Company.source_record_id == source_record_id)).scalars().first()
        if hit:
            seen: set[int] = set()
            while hit.merged_into_id and hit.id not in seen:  # follow merges to the surviving record
                seen.add(hit.id)
                hit = session.get(Company, hit.merged_into_id)
            return hit
    if domain:
        hit = session.execute(select(Company).where(active, Company.domain == domain)).scalars().first()
        if hit:
            return hit
    norm = normalize_name(name)
    if not norm:
        return None
    candidates = session.execute(select(Company).where(active, Company.normalized_name == norm)).scalars().all()
    for c in candidates:
        if domain and c.domain and c.domain != domain:
            continue  # same name, different website: different companies
        if state and c.state:
            if c.state == state:
                return c
            continue
        if source and c.source == source:
            return c
    core = core_name(norm)
    if len(norm) >= 4:
        prefix = norm[:3]
        stmt = select(Company).where(active, func.substr(Company.normalized_name, 1, 3) == prefix)
        if state:
            stmt = stmt.where(Company.state == state)
        for c in session.execute(stmt).scalars():
            if domain and c.domain and c.domain != domain:
                continue
            if not (state and c.state) and not (source and c.source == source):
                continue
            if state and len(core) >= 4 and core_name(c.normalized_name) == core:
                return c
            if len(norm) >= 6 and SequenceMatcher(None, norm, c.normalized_name).ratio() >= 0.93:
                return c
    return None
