"""Validation of discovered companies before they enter the CRM."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from aime.dedupe import FREE_EMAIL_DOMAINS, is_job_board
from aime.knowledge.geo import US_REGIONS
from aime.sources.base import CompanyCandidate

ROLE_WORDS = {
    "engineer", "engineers", "developer", "developers", "hiring", "remote", "onsite", "on-site", "hybrid",
    "full-time", "fulltime", "part-time", "senior", "staff", "lead", "principal", "intern", "interns",
    "contract", "various", "multiple", "positions", "roles", "role", "job", "jobs", "we", "are", "is",
    "the", "a", "an", "and", "etc", "us", "usa", "software", "frontend", "backend", "full-stack", "fullstack",
}
MIN_EVIDENCE_CHARS = 40


def validate_candidate(c: CompanyCandidate) -> tuple[list[str], list[str]]:
    """Return (problems that reject the candidate, fixes applied in place)."""
    problems: list[str] = []
    fixes: list[str] = []
    name = (c.name or "").strip()
    if not 2 <= len(name) <= 80:
        problems.append("company name length")
    if not re.search(r"[A-Za-z]{2}", name):
        problems.append("company name has no letters")
    words = set(re.findall(r"[a-z][a-z\-]*", name.lower()))
    if words and words <= ROLE_WORDS:
        problems.append("name looks like a job title, not a company")
    if c.state and c.state not in US_REGIONS:
        problems.append("invalid US state")
    scheme = urlparse(c.source_url or "").scheme
    if c.source in ("hn_hiring", "sec_edgar") and scheme != "https":
        problems.append("evidence link is not an https URL")
    if c.signal_type == "hiring" and len((c.signal_text or "").strip()) < MIN_EVIDENCE_CHARS:
        problems.append("too little evidence text")
    if c.domain and (is_job_board(c.domain) or c.domain in FREE_EMAIL_DOMAINS or "." not in c.domain):
        fixes.append(f"dropped non-company domain {c.domain}")
        c.domain, c.website = None, None
    return problems, fixes
