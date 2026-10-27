"""Read a company's own website politely, as the owner allowed: only its own pages, robots.txt honoured
for every page, a handful of pages per company, each capped in size, with a pause between requests.
A robots.txt that cannot be read (5xx, 429, too large, no answer) means "do not crawl" (RFC 9309); only a
missing one (4xx) allows everything. Redirects are followed one hop at a time, each checked against
robots.txt, and never to another domain: a site that now redirects elsewhere is not read.

The homepage is read first; then links on the same registrable domain whose path or link text points to
team / leadership, about, contact or services pages (a few well-known paths are tried when the homepage
links to none). Pages are parsed with the standard library into ordered text blocks (block elements),
finer segments (also split at inline elements, for card layouts such as <span>Name</span><span>Title</span>),
links, JSON-LD objects, and the emails and phone numbers the page itself publishes.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from aime.context import USER_AGENT
from aime.dedupe import registrable_domain
from aime.website_check import _read_capped, robots_verdict

# A link is classified by the first kind whose keywords appear in its path or text.
PAGE_KINDS: list[tuple[str, tuple[str, ...]]] = [
    ("team", ("team", "leadership", "founders", "executives", "who we are", "who-we-are", "board", "principals",
              "attorneys", "our people", "our-people", "our staff", "our-staff", "management team")),
    ("about", ("about", "our story", "our-story", "company", "mission", "history")),
    ("contact", ("contact", "get in touch", "get-in-touch", "locations", "offices")),
    ("services", ("services", "solutions", "what we do", "what-we-do", "capabilities", "products", "industries")),
]
# These name a team page only on their own ("/people", "/about/management", a "Management" link), never inside a
# product name ("/customer-management", "People analytics"): product pages quote customers, not staff.
TEAM_WORDS_ALONE = ("management", "people", "staff")
MAX_REDIRECTS = 5
KIND_ORDER = {"team": 0, "about": 1, "contact": 2, "services": 3}
FALLBACK_PATHS = (("/about", "about"), ("/about-us", "about"), ("/team", "team"), ("/our-team", "team"),
                  ("/leadership", "team"), ("/contact", "contact"), ("/contact-us", "contact"))
SKIP_PATH_RE = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|mp4|mp3)$|/(login|signin|sign-in|"
                          r"careers?|jobs?|blog|news|press|privacy|terms|cart|checkout|wp-admin|feed)(/|$)", re.I)
BLOCK_TAGS = {"p", "div", "li", "td", "th", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
              "header", "footer", "figcaption", "blockquote", "address", "label", "caption", "tr", "ul", "ol",
              "main", "aside", "nav", "figure", "table", "form", "button"}
INLINE_TAGS = {"span", "strong", "b", "em", "i", "small", "a", "cite"}
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "select", "option"}
# Menus, page headers and footers: their neighbouring items ("Career Coaching", "Executive Coaching") are not
# name/title cards. A header or footer inside an article or section belongs to that content and is kept.
CHROME_TAGS = {"nav", "header", "footer"}
CONTENT_TAGS = {"article", "section", "main", "aside"}
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}(?![\w-])")


@dataclass
class Page:
    url: str
    kind: str  # home | team | about | contact | services
    # ok | robots_disallowed | robots_unavailable | offsite_redirect | http_<code> | error | not_html
    status: str
    title: str = ""
    meta: dict = field(default_factory=dict)
    blocks: list[tuple[str, str]] = field(default_factory=list)  # (tag, text) per block element, reading order
    segments: list[str] = field(default_factory=list)  # text split at block and inline element boundaries
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, link text)
    jsonld: list = field(default_factory=list)
    content_blocks: list[tuple[str, str]] | None = None  # blocks outside menus, headers, footers (None: not split)
    content_segments: list[str] | None = None  # segments outside menus, headers, footers
    emails: list[str] = field(default_factory=list)  # as published: mailto: links and visible text
    phones: list[str] = field(default_factory=list)  # tel: links

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def text(self) -> str:
        return "\n".join(t for _, t in self.blocks)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title, self._in_title = "", False
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self._link: list | None = None
        self.blocks: list[tuple[str, str]] = []
        self.segments: list[str] = []
        self._block_buf: list[str] = []
        self._seg_buf: list[str] = []
        self._stack: list[str] = []
        self.jsonld_raw: list[str] = []
        self._jsonld: list[str] | None = None
        self._skip = 0
        self.mailtos: list[str] = []
        self.tels: list[str] = []
        self._chrome: list[str] = []  # open menu/header/footer elements
        self.content_blocks: list[tuple[str, str]] = []
        self.content_segments: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._jsonld = []
            return
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:description", "og:site_name", "og:title"):
                self.meta[key] = " ".join(unescape(a.get("content", "")).split())
        if tag in BLOCK_TAGS or tag == "br":
            self._flush_block()
            if tag in CHROME_TAGS and (tag == "nav" or not CONTENT_TAGS & set(self._stack)):
                self._chrome.append(tag)
            if tag != "br":
                self._stack.append(tag)
        elif tag in INLINE_TAGS:
            self._flush_segment()
        if tag == "a":
            href = a.get("href", "").strip()
            if href.lower().startswith("mailto:"):
                self.mailtos.append(href[7:].split("?")[0].strip())
            elif href.lower().startswith("tel:"):
                self.tels.append(href[4:].strip())
            self._link = [href, []]

    def handle_endtag(self, tag):
        if tag == "script" and self._jsonld is not None:
            self.jsonld_raw.append("".join(self._jsonld))
            self._jsonld = None
            return
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link is not None:
            self.links.append((self._link[0], " ".join("".join(self._link[1]).split())))
            self._link = None
        if tag in BLOCK_TAGS:
            self._flush_block()
            if tag in self._chrome:
                self._chrome.remove(tag)
            if tag in self._stack:
                while self._stack and self._stack.pop() != tag:
                    pass
        elif tag in INLINE_TAGS:
            self._flush_segment()

    def handle_data(self, data):
        if self._jsonld is not None:
            self._jsonld.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        if self._link is not None:
            self._link[1].append(data)
        self._block_buf.append(data)
        self._seg_buf.append(data)

    def _flush_segment(self) -> None:
        text = " ".join("".join(self._seg_buf).split()).strip(" ,|·•–—-")
        self._seg_buf = []
        if text:
            self.segments.append(text)
            if not self._chrome:
                self.content_segments.append(text)

    def _flush_block(self) -> None:
        self._flush_segment()
        text = " ".join("".join(self._block_buf).split())
        self._block_buf = []
        if text:
            self.blocks.append((self._stack[-1] if self._stack else "text", text))
            if not self._chrome:
                self.content_blocks.append(self.blocks[-1])


def parse_page(url: str, kind: str, html: str) -> Page:
    p = _PageParser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed markup: keep whatever was parsed so far
        pass
    p._flush_block()
    jsonld = []
    for raw in p.jsonld_raw:
        try:
            jsonld.append(json.loads(raw))
        except ValueError:
            continue
    links = [(urljoin(url, href).split("#")[0], text) for href, text in p.links
             if href and not href.lower().startswith(("mailto:", "tel:", "javascript:", "#"))]
    emails: list[str] = []
    for e in p.mailtos + EMAIL_RE.findall("\n".join(t for _, t in p.blocks)):
        e = e.strip().strip(".").lower()
        if EMAIL_RE.fullmatch(e) and e not in emails:
            emails.append(e)
    return Page(url=url, kind=kind, status="ok", title=" ".join(unescape(p.title).split()), meta=p.meta,
                blocks=p.blocks, segments=p.segments, content_blocks=p.content_blocks,
                content_segments=p.content_segments, links=links, jsonld=jsonld, emails=emails,
                phones=list(dict.fromkeys(t for t in p.tels if t)))


def classify_link(url: str, text: str) -> str | None:
    path = urlparse(url).path.lower()
    label = f" {text.lower()} "
    for kind, words in PAGE_KINDS:
        for w in words:
            if " " in w:
                if f" {w} " in label:
                    return kind
            elif re.search(rf"(^|[/_-]){re.escape(w)}([/_.-]|$)", path) or re.search(rf"\b{re.escape(w)}\b", label):
                return kind
        if kind == "team" and _team_word_alone(path, label):
            return kind
    return None


def _team_word_alone(path: str, label: str) -> bool:
    segments = {s for s in path.split("/") if s}
    text = " ".join(label.split())
    return any(w in segments or re.fullmatch(rf"(?:(?:meet )?(?:our|the) )?{w}", text) for w in TEAM_WORDS_ALONE)


class SiteReader:
    """Reads up to `max_pages` pages of one company site. `requested` records every URL fetched."""

    def __init__(self, ctx, domain: str, max_pages: int = 6, delay: float = 1.0):
        self.ctx = ctx
        self.domain = domain.lower().strip()
        self.max_pages = max(1, max_pages)
        self.delay = max(0.0, delay)
        self.requested: list[str] = []
        self._robots: dict[str, RobotFileParser | bool | None] = {}  # None: robots.txt request got no answer

    def _robots_status(self, client: httpx.Client, url: str) -> str:
        """ok | robots_disallowed | robots_unavailable | error (robots.txt got no answer)."""
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            try:
                self.requested.append(origin + "/robots.txt")
                self._robots[origin] = robots_verdict(client.get(origin + "/robots.txt"))
            except httpx.HTTPError:
                self._robots[origin] = None
        rules = self._robots[origin]
        if rules is None:
            return "error"
        if rules is False:
            return "robots_unavailable"
        return "ok" if rules is True or rules.can_fetch(USER_AGENT.split("/")[0], url) else "robots_disallowed"

    def _fetch(self, client: httpx.Client, url: str, kind: str) -> tuple[Page, str]:
        start, site = url, registrable_domain(self.domain) or self.domain
        for _ in range(MAX_REDIRECTS + 1):
            allowed = self._robots_status(client, url)
            if allowed != "ok":
                return Page(url=start, kind=kind, status=allowed,
                            title="robots.txt unreachable" if allowed == "error" else ""), url
            try:
                self.requested.append(url)
                with client.stream("GET", url, follow_redirects=False) as r:
                    if r.has_redirect_location:
                        target = urljoin(url, r.headers["location"])
                        if registrable_domain(urlparse(target).netloc) != site:
                            return Page(url=start, kind=kind, status="offsite_redirect",
                                        title=urlparse(target).netloc[:200]), target
                        url = target
                        continue
                    if r.status_code >= 400:
                        return Page(url=start, kind=kind, status=f"http_{r.status_code}"), url
                    if "html" not in r.headers.get("content-type", "").lower():
                        return Page(url=start, kind=kind, status="not_html"), url
                    body = _read_capped(r)
            except httpx.HTTPError as exc:
                return Page(url=start, kind=kind, status="error", title=type(exc).__name__), url
            return parse_page(url, kind, body), url
        return Page(url=start, kind=kind, status="error", title="too many redirects"), url

    def read(self) -> list[Page]:
        pages: list[Page] = []
        with self.ctx.http(timeout=12, headers={"Accept": "text/html,application/xhtml+xml"}) as client:
            home, final = self._fetch(client, f"https://{self.domain}/", "home")
            if home.status == "error":
                home, final = self._fetch(client, f"http://{self.domain}/", "home")
            pages.append(home)
            if not home.ok:
                return pages
            origin = f"{urlparse(final).scheme}://{urlparse(final).netloc}"
            site = registrable_domain(urlparse(final).netloc) or self.domain
            seen = {urlparse(final).path.rstrip("/") or "/"}
            candidates: list[tuple[float, str, str, bool]] = []  # (order, url, kind, guessed path)
            for url, text in home.links:
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or registrable_domain(parsed.netloc) != site:
                    continue
                path = parsed.path.rstrip("/") or "/"
                if path in seen or SKIP_PATH_RE.search(path) or path.count("/") > 3:
                    continue
                kind = classify_link(url, text)
                if kind:
                    seen.add(path)
                    candidates.append((KIND_ORDER[kind], url, kind, False))
            if not any(kind in ("team", "about") for _, _, kind, _ in candidates):
                for path, kind in FALLBACK_PATHS:
                    if path not in seen:
                        seen.add(path)
                        candidates.append((KIND_ORDER[kind] + 0.5, origin + path, kind, True))
            per_kind = {kind: 0 for kind in KIND_ORDER}
            fetched = 1
            for _, url, kind, guessed in sorted(candidates, key=lambda c: c[0]):
                if fetched >= self.max_pages:
                    break
                if per_kind[kind] >= 2:
                    continue
                if self.delay:
                    time.sleep(self.delay)
                page, _ = self._fetch(client, url, kind)
                if page.status != "robots_disallowed":
                    fetched += 1
                if guessed and page.status.startswith("http_"):
                    continue  # a well-known path that does not exist on this site is not worth recording
                pages.append(page)
                if page.ok:
                    per_kind[kind] += 1
        return pages
