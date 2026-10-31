"""Company website verification: is the domain live, is it the company's own site, is it parked?

One polite request per company: robots.txt is honoured, only the homepage is fetched, the response
is capped at 300 KB, and the User-Agent identifies this tool. As RFC 9309 says, only a robots.txt that does
not exist (4xx) allows everything; one that cannot be read (5xx, 429, too large, too slow) means "do not crawl".
Every hop goes through a client that refuses private and local addresses (see AppContext.http).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from html import unescape
from urllib.robotparser import RobotFileParser

import httpx

from aime.context import USER_AGENT
from aime.dedupe import is_job_board, normalize_name, registrable_domain

MAX_BYTES = 300_000
MAX_SECONDS = 30  # a page that drips slowly is cut here: the HTTP timeout is per read, not per page
ROBOTS_MAX_BYTES = 500_000
ROBOTS_MAX_SECONDS = 15
PARKED_MARKERS = (
    "domain is for sale", "buy this domain", "this domain may be for sale", "domain parking",
    "parked domain", "parked free", "hugedomains", "sedo.com", "dan.com/buy", "afternic",
)
GENERIC_TOKENS = {"the", "inc", "llc", "corp", "co", "company", "labs", "lab", "hq", "group", "app"}
_SITE_NAME_RE = re.compile(r"""property=["']og:site_name["'][^>]+content=["']([^"']+)""", re.I)
MAX_META_TAGS, MAX_TAG_CHARS = 200, 2000


@dataclass
class WebsiteResult:
    status: str  # verified | redirected | mismatch | parked | unreachable | blocked | robots_disallowed
    final_domain: str | None
    title: str | None
    note: str
    no_answer: bool = False  # nothing answered at all (no network, a timeout, a name not found): this says nothing about the site


def name_matches(company: str, *haystacks: str | None) -> bool:
    norm = normalize_name(company)
    compact = norm.replace(" ", "")
    tokens = [t for t in norm.split() if len(t) >= 3 and t not in GENERIC_TOKENS]
    text = " ".join(h for h in haystacks if h).lower()
    text_compact = re.sub(r"[^a-z0-9]", "", text)
    if len(compact) >= 3 and compact in text_compact:
        return True
    if tokens and sum(1 for t in tokens if t in text) >= max(1, (len(tokens) + 1) // 2):
        return True
    return False


def _read_limited(resp: httpx.Response, max_bytes: int, max_seconds: float) -> tuple[bytes, bool]:
    """The body up to a size and a total time, and whether it ended by itself."""
    chunks, size, deadline = [], 0, time.monotonic() + max_seconds
    for chunk in resp.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= max_bytes or time.monotonic() > deadline:
            return b"".join(chunks)[:max_bytes], False
    return b"".join(chunks), True


def _read_capped(resp: httpx.Response) -> str:
    return _read_limited(resp, MAX_BYTES, MAX_SECONDS)[0].decode(resp.encoding or "utf-8", errors="replace")


def read_robots(client: httpx.Client, url: str) -> tuple[RobotFileParser | bool, str]:
    """robots.txt as rules, True (none exists: everything allowed) or False (unreadable: nothing), with a note. It is read
    within a size and a time limit, so one that is huge or drips slowly costs no more than that."""
    with client.stream("GET", url) as r:
        if r.status_code == 429 or r.status_code >= 500:
            return False, f"HTTP {r.status_code}"
        if r.status_code >= 400:
            return True, ""
        body, whole = _read_limited(r, ROBOTS_MAX_BYTES, ROBOTS_MAX_SECONDS)
        if not whole:
            return False, f"HTTP {r.status_code}, too large or too slow to read"
        text = body.decode(r.encoding or "utf-8", errors="replace")
    rules = RobotFileParser()
    rules.parse(text.splitlines())
    return rules, ""


def _robots(client: httpx.Client, domain: str) -> tuple[str, str]:
    """allow | disallow | unavailable (robots.txt unreadable) | unreachable (no answer at all), with a note."""
    for scheme in ("https", "http"):
        try:
            verdict, note = read_robots(client, f"{scheme}://{domain}/robots.txt")
        except httpx.HTTPError:
            continue
        if verdict is False:
            return "unavailable", note
        if verdict is True or verdict.can_fetch(USER_AGENT.split("/")[0], f"{scheme}://{domain}/"):
            return "allow", ""
        return "disallow", ""
    return "unreachable", ""


def page_title(body: str) -> str | None:
    """The first <title>. Found by searching rather than a regex: a page full of "<title>" with no end costs one pass."""
    low = body.lower()
    start = low.find("<title")
    close = low.find(">", start) if start >= 0 else -1
    end = low.find("</title", close) if close >= 0 else -1
    if end < 0:
        return None
    return unescape(" ".join(body[close + 1:end].split())).strip()[:300] or None


def site_name(body: str) -> str | None:
    """og:site_name from the page's first meta tags, each read only up to a sensible length."""
    low, at = body.lower(), 0
    for _ in range(MAX_META_TAGS):
        at = low.find("<meta", at)
        if at < 0:
            return None
        end = low.find(">", at, at + MAX_TAG_CHARS)
        if end > 0:
            found = _SITE_NAME_RE.search(body, at, end)
            if found:
                return unescape(found.group(1)).strip()
        at += 5
    return None


def site_answers(ctx, domain: str) -> bool:
    """Does this site give any HTTP answer at all? One small request, to tell a dead site from a lost network."""
    with ctx.http(public_only=True, timeout=12) as client:
        return _robots(client, domain)[0] != "unreachable"


def check_website(ctx, domain: str, company_name: str) -> WebsiteResult:
    with ctx.http(public_only=True, timeout=12, headers={"Accept": "text/html,application/xhtml+xml"}) as client:
        robots, note = _robots(client, domain)
        if robots == "disallow":
            return WebsiteResult("robots_disallowed", domain, None, "Live, but robots.txt disallows the homepage")
        if robots == "unavailable":
            return WebsiteResult("blocked", domain, None, f"robots.txt could not be read ({note}): not checked")
        if robots == "unreachable":
            return WebsiteResult("unreachable", None, None, "No answer (robots.txt request failed)", no_answer=True)
        resp = body = None
        last_error: Exception | None = None
        for scheme in ("https", "http"):
            try:
                with client.stream("GET", f"{scheme}://{domain}/") as r:
                    resp, body = r, _read_capped(r)
                break
            except httpx.HTTPError as exc:
                last_error = exc
        if resp is None:
            return WebsiteResult("unreachable", None, None, f"{type(last_error).__name__}", no_answer=True)

    final_domain = registrable_domain(resp.url.host or domain)
    if resp.status_code in (401, 403, 429, 503):
        return WebsiteResult("blocked", final_domain, None, f"HTTP {resp.status_code}: live but blocks automated checks")
    if resp.status_code >= 400:
        return WebsiteResult("unreachable", final_domain, None, f"HTTP {resp.status_code}")
    title = page_title(body or "")
    low = (body or "").lower()
    if any(marker in low for marker in PARKED_MARKERS):
        return WebsiteResult("parked", final_domain, title, "Domain appears parked or for sale")
    matched = name_matches(company_name, title, site_name(body or ""), final_domain.split(".")[0])
    if final_domain != registrable_domain(domain):
        if is_job_board(final_domain):
            return WebsiteResult("mismatch", final_domain, title, "Redirects to a job board or third-party site")
        if matched:
            return WebsiteResult("redirected", final_domain, title, f"Redirects to {final_domain}")
        return WebsiteResult("mismatch", final_domain, title, f"Redirects to unrelated site {final_domain}")
    if matched:
        return WebsiteResult("verified", final_domain, title, "Homepage names the company")
    return WebsiteResult("mismatch", final_domain, title, "Homepage does not name the company")
