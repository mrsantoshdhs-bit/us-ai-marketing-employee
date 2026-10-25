"""Hacker News "Who is hiring?" threads via the official public HN Algolia API.

Companies post these themselves to announce open roles. We keep only company-level
facts (name, website, location, the needs the post describes) plus a link to the post.
Personal emails and phone numbers are removed and never stored.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

import httpx

from aime.dedupe import domain_from_url, is_job_board, normalize_name
from aime.knowledge.geo import parse_location
from aime.matching import classify_industry
from aime.sources.base import CompanyCandidate, ResearchFocus, Source, redact_personal

API = "https://hn.algolia.com/api/v1"
_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r'href="([^"]+)"')
_SPLIT_RE = re.compile(r"\s+[|–—]\s+|\s*\|\s*")


_DOMAIN_TEXT_RE = re.compile(r"\b([a-z0-9][a-z0-9-]{1,62}\.(?:com|io|ai|co|app|dev|org|net|xyz|so|tech|us|inc|health|bio))\b", re.I)
_NOT_NAMES = {"etc", "hiring", "remote", "we", "us", "onsite", "full-time", "full time", "various", "multiple"}


def clean_html(raw: str) -> str:
    text = raw.replace("<p>", "\n\n")
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def pick_website(raw: str, header: str, name: str) -> str | None:
    """Choose the company's own domain: one that matches the company name, else one linked in the
    header line. Job boards, short links and media sites are skipped. Unknown beats wrong."""
    compact = normalize_name(name).replace(" ", "")
    tokens = [t for t in normalize_name(name).split() if len(t) >= 4]
    header_raw = raw.split("<p>", 1)[0]
    header_domains = {domain_from_url(html.unescape(h)) for h in _HREF_RE.findall(header_raw)}
    header_domains |= {d.lower() for d in _DOMAIN_TEXT_RE.findall(header)}
    candidates = [domain_from_url(html.unescape(h)) for h in _HREF_RE.findall(raw)] + sorted(header_domains - {None})
    fallback = None
    for dom in candidates:
        if not dom or is_job_board(dom):
            continue
        label = dom.split(".")[0]
        if compact and len(compact) >= 3 and (compact in label or (len(label) >= 4 and label in compact)):
            return dom
        if any(t in label for t in tokens):
            return dom
        if fallback is None and dom in header_domains:
            fallback = dom
    return fallback


def parse_post(item: dict) -> CompanyCandidate | None:
    raw = item.get("text") or ""
    if not raw or item.get("author") in (None, "[deleted]"):
        return None
    header_raw = raw.split("<p>", 1)[0]
    header = clean_html(header_raw)
    if len(header) < 3:
        return None
    segments = [s.strip() for s in _SPLIT_RE.split(header) if s.strip()]
    if not segments:
        return None
    name = re.sub(r"\s*\(.*?\)\s*", " ", segments[0]).strip(" -:")
    name = re.sub(r"^https?://", "", name).split("/")[0] if name.startswith("http") else name
    if not name or len(name) > 80 or len(name.split()) > 8:
        return None
    if not re.search(r"[A-Za-z]{2}", name) or name.lower().strip(" .") in _NOT_NAMES:
        return None

    dom = pick_website(raw, header, name)
    website = f"https://{dom}" if dom else None

    full_text = clean_html(raw)
    loc = parse_location(header)
    item_id = str(item.get("id"))
    created = item.get("created_at")
    observed = None
    if created:
        try:
            observed = datetime.fromisoformat(created.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            observed = None
    return CompanyCandidate(
        name=name,
        source="hn_hiring",
        source_url=f"https://news.ycombinator.com/item?id={item_id}",
        source_record_id=f"hn:{item_id}",
        website=website,
        domain=domain_from_url(website) if website else None,
        location_text=header[:300],
        state=loc.state,
        city=loc.city,
        is_us=loc.is_us,
        industry=classify_industry(full_text, default="tech_startups"),
        description=redact_personal(full_text[:600]),
        signal_text=redact_personal(full_text[:1500]),
        signal_type="hiring",
        observed_at=observed,
        verified=False,  # self-published by the company, not an official registry
    )


class HNHiringSource(Source):
    name = "hn_hiring"
    title = "Hacker News 'Who is hiring?' (public API)"
    description = "Monthly public thread where US and global companies describe roles and the work they need done."

    def configured(self) -> tuple[bool, str]:
        if not self.settings.hn_source_enabled:
            return False, "Disabled (HN_SOURCE_ENABLED=false)"
        if not self.settings.network_enabled:
            return False, "Network disabled"
        return True, "Public API, no key required"

    def latest_threads(self, count: int = 1) -> list[dict]:
        try:
            with self.ctx.http() as client:
                resp = client.get(f"{API}/search_by_date", params={"tags": "story,author_whoishiring", "hitsPerPage": 12})
                resp.raise_for_status()
                hits = [h for h in resp.json().get("hits", [])
                        if (h.get("title") or "").lower().startswith("ask hn: who is hiring")]
            self.cache_put("threads", hits)
        except httpx.HTTPError:
            hits = self.cache_get("threads", max_age_hours=24 * 40)  # API down: use the last known list
            if hits is None:
                raise
        return hits[:count]

    def latest_thread(self) -> dict | None:
        threads = self.latest_threads(1)
        return threads[0] if threads else None

    def thread_items(self, story_id: str) -> list[dict]:
        cache_hours = float(self.settings.section("research").get("cache_hours", 12))
        cached = self.cache_get(f"items_{story_id}", cache_hours)
        if cached is not None:
            return cached
        try:
            with self.ctx.http(timeout=60) as client:
                resp = client.get(f"{API}/items/{story_id}")
                resp.raise_for_status()
                children = resp.json().get("children", [])
        except httpx.HTTPError:
            stale = self.cache_get(f"items_{story_id}", max_age_hours=24 * 40)
            if stale is not None:
                return stale
            raise
        slim = [
            {"id": c.get("id"), "author": c.get("author"), "created_at": c.get("created_at"), "text": c.get("text")}
            for c in children
            if c.get("type") == "comment"
        ]
        self.cache_put(f"items_{story_id}", slim)
        return slim

    def fetch(self, focus: ResearchFocus) -> list[CompanyCandidate]:
        out = []
        for thread in self.latest_threads(int(self.settings.section("research").get("hn_threads", 2))):
            for item in self.thread_items(str(thread["objectID"])):
                cand = parse_post(item)
                if cand:
                    cand.extra["thread_title"] = thread.get("title")
                    out.append(cand)
        return out
