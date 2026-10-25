"""Company website verification: is the domain live, is it the company's own site, is it parked?

One polite request per company: robots.txt is honoured, only the homepage is fetched, the response
is capped at 300 KB, and the User-Agent identifies this tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from urllib.robotparser import RobotFileParser

import httpx

from aime.context import USER_AGENT
from aime.dedupe import is_job_board, normalize_name, registrable_domain

MAX_BYTES = 300_000
PARKED_MARKERS = (
    "domain is for sale", "buy this domain", "this domain may be for sale", "domain parking",
    "parked domain", "parked free", "hugedomains", "sedo.com", "dan.com/buy", "afternic",
)
GENERIC_TOKENS = {"the", "inc", "llc", "corp", "co", "company", "labs", "lab", "hq", "group", "app"}
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SITE_NAME_RE = re.compile(r"""<meta[^>]+property=["']og:site_name["'][^>]+content=["']([^"']+)""", re.I)


@dataclass
class WebsiteResult:
    status: str  # verified | redirected | mismatch | parked | unreachable | blocked | robots_disallowed
    final_domain: str | None
    title: str | None
    note: str


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


def _read_capped(resp: httpx.Response) -> str:
    chunks, size = [], 0
    for chunk in resp.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_BYTES:
            break
    return b"".join(chunks)[:MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")


def _robots_allows(client: httpx.Client, domain: str) -> bool:
    try:
        r = client.get(f"https://{domain}/robots.txt")
    except httpx.HTTPError:
        return True
    if r.status_code != 200 or len(r.content) > 500_000:
        return True
    parser = RobotFileParser()
    parser.parse(r.text.splitlines())
    return parser.can_fetch(USER_AGENT.split("/")[0], f"https://{domain}/")


def check_website(ctx, domain: str, company_name: str) -> WebsiteResult:
    with ctx.http(timeout=12, headers={"Accept": "text/html,application/xhtml+xml"}) as client:
        if not _robots_allows(client, domain):
            return WebsiteResult("robots_disallowed", domain, None, "Live, but robots.txt disallows the homepage")
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
            return WebsiteResult("unreachable", None, None, f"{type(last_error).__name__}")

    final_domain = registrable_domain(resp.url.host or domain)
    if resp.status_code in (401, 403, 429, 503):
        return WebsiteResult("blocked", final_domain, None, f"HTTP {resp.status_code}: live but blocks automated checks")
    if resp.status_code >= 400:
        return WebsiteResult("unreachable", final_domain, None, f"HTTP {resp.status_code}")
    m = _TITLE_RE.search(body or "")
    title = unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:300] if m else None
    s = _SITE_NAME_RE.search(body or "")
    site_name = unescape(s.group(1)).strip() if s else None
    low = (body or "").lower()
    if any(marker in low for marker in PARKED_MARKERS):
        return WebsiteResult("parked", final_domain, title, "Domain appears parked or for sale")
    matched = name_matches(company_name, title, site_name, final_domain.split(".")[0])
    if final_domain != registrable_domain(domain):
        if is_job_board(final_domain):
            return WebsiteResult("mismatch", final_domain, title, "Redirects to a job board or third-party site")
        if matched:
            return WebsiteResult("redirected", final_domain, title, f"Redirects to {final_domain}")
        return WebsiteResult("mismatch", final_domain, title, f"Redirects to unrelated site {final_domain}")
    if matched:
        return WebsiteResult("verified", final_domain, title, "Homepage names the company")
    return WebsiteResult("mismatch", final_domain, title, "Homepage does not name the company")
