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
# The site is gone, lapsed or suspended hosting: a real page may use these words in passing, so only its title or a page of little else counts.
GONE_MARKERS = (
    "this website has expired", "website expired", "this domain has expired", "domain has expired", "account suspended",
    "account has been suspended", "website has been suspended", "site has been suspended",
)
# A lander of paid links on the old address: its own disclaimer alone runs past the words of a coming-soon page.
LANDER_MARKERS = ("sponsored listings service", "sponsored listings displayed above")
LANDER_MAX_WORDS = 120
# A browser check answered in place of the page: nothing is learned of the site, and its title is the check's own.
CHALLENGE_TITLES = ("just a moment", "attention required! | cloudflare")
CHALLENGE_MARKERS = ("checking your browser before accessing", "enable javascript and cookies to continue")
# A real small company may say "coming soon" on a page that says much else, so only a page of little else counts.
PLACEHOLDER_MARKERS = ("coming soon", "launching soon", "under construction")
PLACEHOLDER_MAX_WORDS = 30
GENERIC_TOKENS = {"the", "inc", "llc", "corp", "co", "company", "labs", "lab", "hq", "group", "app"}
_SITE_NAME_RE = re.compile(r"""property=["']og:site_name["'][^>]+content=["']([^"']+)""", re.I)
MAX_META_TAGS, MAX_TAG_CHARS = 200, 2000
_META_CHARSET_RE = re.compile(rb"""<meta[^>]{0,200}?charset\s*=\s*["']?\s*([a-z0-9_.:-]{1,40})""", re.I)


OUTAGE_WITNESSES = 2  # known good sites asked whether the internet itself is what failed


@dataclass
class WebsiteResult:
    status: str  # verified | redirected | mismatch | parked | unreachable | blocked | robots_disallowed
    final_domain: str | None
    title: str | None
    note: str
    no_answer: bool = False  # nothing answered at all (no network, a timeout, a name not found), or a passing 5xx or 429: this says nothing about the site
    error_answer: bool = False  # it answered, with an error one check should not judge by: 5xx, 429, 401, 403, 404, a browser check
    plain_redirect: bool = False  # reached another domain through plain HTTP, where anyone on the way can answer: it moves nothing
    unproven: bool = False  # "verified" over plain HTTP on a page that does not itself name the company: a sign-in page could say the same

    @property
    def proven(self) -> bool:
        """Verified at its own domain, in a way a sign-in page in front of the internet could not have answered."""
        return self.status == "verified" and not self.unproven


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


def page_names_company(company: str, *texts: str | None) -> bool:
    """Every word of the name except GENERIC_TOKENS, short ones too, is a whole word of these texts. Never a substring:
    "Elm" is not in "Helmsman", and one shared word does not make "Acme Health" into "Bright Health"."""
    words = [w for w in normalize_name(company).split() if w not in GENERIC_TOKENS]
    found = set(re.findall(r"[a-z0-9]+", " ".join(t for t in texts if t).lower().replace("&", " and ")))
    return bool(words) and all(w in found for w in words)


def redirect_names_company(company: str, title: str | None, site: str | None, final_domain: str) -> bool:
    """Stricter than name_matches, for a redirect only: acting on one changes where the company's mail goes. The page names
    the company, or the new domain's label is exactly its name ("Acme Robotics" at acme-robotics.example)."""
    compact = normalize_name(company).replace(" ", "")
    label = final_domain.split(".")[0]
    # only ever narrower than name_matches: a name of one short word it would not trust does not pass here either
    return name_matches(company, title, site, label) and (page_names_company(company, title, site) or compact in (label, label.replace("-", "")))


def _read_limited(resp: httpx.Response, max_bytes: int, max_seconds: float) -> tuple[bytes, bool]:
    """The body up to a size and a total time, and whether it ended by itself."""
    chunks, size, deadline = [], 0, time.monotonic() + max_seconds
    for chunk in resp.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= max_bytes or time.monotonic() > deadline:
            return b"".join(chunks)[:max_bytes], False
    return b"".join(chunks), True


def _meta_charset(raw: bytes) -> str | None:
    """The charset a page declares in its first 2 KB. One readable there cannot really be UTF-16 or UTF-32, whatever it says."""
    found = _META_CHARSET_RE.search(raw[:2048])
    name = found.group(1).decode("ascii").lower() if found else None
    return None if name and name.startswith(("utf-16", "utf16", "utf-32", "utf32")) else name


def _read_capped(resp: httpx.Response) -> str:
    raw = _read_limited(resp, MAX_BYTES, MAX_SECONDS)[0]
    encoding = resp.encoding if resp.charset_encoding else _meta_charset(raw)
    try:
        return raw.decode(encoding or "utf-8", errors="replace")
    except (LookupError, ValueError):  # a charset Python does not know, or one that is no text encoding at all
        return raw.decode("utf-8", errors="replace")


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


def _visible_text(low: str) -> str:
    """What a person reads, the title included. Scripts, styles and comments are cut by searching: a hostile page costs one pass."""
    pieces = re.split(r"<(script\b|style\b|!--)", low)
    text = [pieces[0]]
    for tag, piece in zip(pieces[1::2], pieces[2::2], strict=True):
        end = piece.find("-->" if tag == "!--" else f"</{tag}")
        text.append(piece[end:] if end >= 0 else "")
    return " ".join(re.sub(r"<[^<>]*>|-->", " ", " ".join(text)).split())


def _word_count(text: str) -> int:
    return len(re.findall(r"[a-z0-9']+", text))


def _says_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _is_gone(title: str | None, text: str) -> bool:
    """A lapsed site or a paid lander. What a live company's page mentions, or its scripts carry, does not count."""
    words = _word_count(text)
    return (_says_any((title or "").lower(), GONE_MARKERS) or (words <= PLACEHOLDER_MAX_WORDS and _says_any(text, GONE_MARKERS))
            or (words <= LANDER_MAX_WORDS and _says_any(text, LANDER_MARKERS)))


def _is_placeholder(text: str) -> bool:
    """A coming-soon page with almost nothing else on it."""
    return _says_any(text, PLACEHOLDER_MARKERS) and _word_count(text) <= PLACEHOLDER_MAX_WORDS


def site_answers(ctx, domain: str) -> bool:
    """Does this site give any HTTP answer at all? One small request, to tell a dead site from a lost network."""
    with ctx.http(public_only=True, timeout=12) as client:
        return _robots(client, domain)[0] != "unreachable"


def known_sites_still_prove(ctx, session, not_these: list[int]) -> bool | None:
    """Do sites known to be good still prove good? site_answers cannot tell, as a sign-in page answers it: a witness must prove good.
    None: there is no other known site to ask."""
    from sqlalchemy import select

    from aime.models import Company

    witnesses = session.execute(select(Company.domain, Company.name).where(
        Company.merged_into_id.is_(None), Company.domain.is_not(None), Company.website_status == "verified",
        Company.website_retry_at.is_(None), Company.id.not_in(not_these))
        .order_by(Company.website_checked_at.desc()).limit(OUTAGE_WITNESSES)).all()
    session.commit()  # no write lock is held during network calls
    if not witnesses:
        return None
    return any(check_website(ctx, domain, name).proven for domain, name in witnesses)


def check_website(ctx, domain: str, company_name: str) -> WebsiteResult:
    with ctx.http(public_only=True, timeout=12, headers={"Accept": "text/html,application/xhtml+xml"}) as client:
        robots, note = _robots(client, domain)
        if robots == "disallow":
            return WebsiteResult("robots_disallowed", domain, None, "Live, but robots.txt disallows the homepage")
        if robots == "unavailable":  # a passing server error says nothing yet; too large or too slow to read is an answer
            passing = bool(re.fullmatch(r"HTTP \d+", note))
            return WebsiteResult("blocked", domain, None, f"robots.txt could not be read ({note}): not checked", no_answer=passing, error_answer=passing)
        if robots == "unreachable":
            return WebsiteResult("unreachable", None, None, "No answer (robots.txt request failed)", no_answer=True)
        resp = body = None
        last_error: Exception | None = None
        for scheme in ("https", "http"):
            try:
                with client.stream("GET", f"{scheme}://{domain}/") as r:
                    resp, body = r, _read_capped(r)
                plain_http = scheme == "http"
                break
            except httpx.HTTPError as exc:
                last_error = exc
        if resp is None:
            return WebsiteResult("unreachable", None, None, f"{type(last_error).__name__}", no_answer=True)

    final_domain = registrable_domain(resp.url.host or domain)
    plain_redirect = plain_http and final_domain != registrable_domain(domain)

    def answer(status: str, title: str | None, note: str, **more) -> WebsiteResult:
        return WebsiteResult(status, final_domain, title, note, plain_redirect=plain_redirect, **more)

    passing = resp.status_code == 429 or resp.status_code >= 500
    if resp.status_code in (401, 403, 429, 503):
        return answer("blocked", None, f"HTTP {resp.status_code}: live but blocks automated checks", no_answer=passing, error_answer=True)
    if resp.status_code >= 400:
        return answer("unreachable", None, f"HTTP {resp.status_code}", no_answer=passing, error_answer=passing or resp.status_code == 404)
    title = page_title(body or "")
    low = (body or "").lower()
    if (title or "").lower().startswith(CHALLENGE_TITLES) or any(marker in low for marker in CHALLENGE_MARKERS):
        return answer("blocked", None, "A browser check answered in place of the page: live, but not read", error_answer=True)
    text = _visible_text(low)
    if any(marker in low for marker in PARKED_MARKERS) or _is_gone(title, text):
        return answer("parked", title, "Domain appears parked or for sale")
    if _is_placeholder(text):
        return answer("mismatch", title, "Only a coming-soon page: it does not show the company")
    named = site_name(body or "")
    if final_domain != registrable_domain(domain):
        if is_job_board(final_domain):
            return answer("mismatch", title, "Redirects to a job board or third-party site")
        if redirect_names_company(company_name, title, named, final_domain):
            return answer("redirected", title, f"Redirects to {final_domain}")
        return answer("mismatch", title, f"Redirects to unrelated site {final_domain}")
    if is_job_board(final_domain):
        return answer("mismatch", title, "A page on a job board or third-party site, not the company's own site")
    if name_matches(company_name, title, named, final_domain.split(".")[0]):
        note = "Homepage names the company" if name_matches(company_name, title, named) else "The domain spells the name; the page does not confirm it"
        return answer("verified", title, note, unproven=plain_http and not page_names_company(company_name, title, named))
    return answer("mismatch", title, "Homepage does not name the company")
