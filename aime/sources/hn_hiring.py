"""Hacker News "Who is hiring?" threads via the official public HN Algolia API.

Companies post these themselves to announce open roles. We keep only company-level
facts (name, website, location, the needs the post describes) plus a link to the post.
Personal emails and phone numbers are removed and never stored.
"""

from __future__ import annotations

import html
import re
import time
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
    """Choose the company's own domain: one that starts or ends with the company's full name, or is a shorter
    form of it. A link that only shares a word with the name may be a customer, investor or partner, so it is not
    used. Job boards, short links and media sites are skipped. Unknown beats wrong."""
    compact = normalize_name(name).replace(" ", "")
    header_raw = raw.split("<p>", 1)[0]
    header_domains = {domain_from_url(html.unescape(h)) for h in _HREF_RE.findall(header_raw)}
    header_domains |= {d.lower() for d in _DOMAIN_TEXT_RE.findall(header)}
    candidates = [domain_from_url(html.unescape(h)) for h in _HREF_RE.findall(raw)] + sorted(header_domains - {None})
    for dom in candidates:
        if not dom or is_job_board(dom):
            continue
        label = dom.split(".")[0].replace("-", "")
        if compact and len(compact) >= 3 and (label.startswith(compact) or label.endswith(compact)
                                              or (len(label) >= 4 and label in compact)):
            return dom
    return None


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
    first = segments[0].splitlines()[0]
    if re.match(r"(role|position|title|location|apply)\s*:", first, re.I):
        return None  # a job field, not the company
    name = re.sub(r"^(company|employer)\s*:\s*", "", first, flags=re.I)
    name = re.sub(r"\s*\(.*?\)\s*", " ", name).strip(" -:")
    if name.startswith("http"):
        name = re.sub(r"^https?://", "", name).split("/")[0]
    else:
        name = re.sub(r"\s+https?://\S+", "", name).strip()  # "WorkHero https://workhero.pro" -> "WorkHero"
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
        industry=classify_industry(full_text),  # none when the post gives no evidence of one: not every employer is a startup
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
    stale_hours: float | None = None  # this run used a saved answer because the API gave none: the age of the oldest one used

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
            if hits:  # an empty answer does not replace the last good list
                self.cache_put("threads", hits)
        except (httpx.HTTPError, ValueError, AttributeError):  # also a 200 whose body is not the API's JSON
            hits = self.cache_get("threads", max_age_hours=24 * 40)  # API down: use the last known list
            if hits is None:
                raise
            self._note_saved_answer("threads")
        return hits[:count]

    def _note_saved_answer(self, key: str) -> None:
        age = (time.time() - self._cache_path(key).stat().st_mtime) / 3600
        self.stale_hours = max(self.stale_hours or 0.0, age)

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
        except (httpx.HTTPError, ValueError, AttributeError):  # also a 200 whose body is not the API's JSON
            stale = self.cache_get(f"items_{story_id}", max_age_hours=24 * 40)
            if stale is not None:
                self._note_saved_answer(f"items_{story_id}")
                return stale
            raise
        slim = [
            {"id": c.get("id"), "author": c.get("author"), "created_at": c.get("created_at"), "text": c.get("text")}
            for c in children
            if c.get("type") == "comment"
        ]
        if slim:  # an empty answer does not replace the last good copy
            self.cache_put(f"items_{story_id}", slim)
        return slim

    def fetch(self, focus: ResearchFocus) -> list[CompanyCandidate]:
        """Raises when the API answers but nothing comes of it, so research tells a person. An empty newest thread is
        not that: it was only just posted, and last month's thread still has its posts."""
        self.stale_hours = None
        threads = self.latest_threads(int(self.settings.section("research").get("hn_threads", 2)))
        if not threads:
            raise ValueError("the API lists no 'Who is hiring?' thread")
        out, posts = [], 0
        for thread in threads:
            items = self.thread_items(str(thread["objectID"]))
            posts += len(items)
            for item in items:
                cand = parse_post(item)
                if cand:
                    cand.extra["thread_title"] = thread.get("title")
                    out.append(cand)
        if not posts:
            raise ValueError(f"none of the {len(threads)} 'Who is hiring?' thread(s) has a post")
        if not out:
            raise ValueError(f"none of the {posts} post(s) could be read: their format may have changed")
        return out
