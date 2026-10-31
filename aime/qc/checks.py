"""The individual quality checks. Deterministic, offline, and free (no paid APIs)."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.content import quality_check
from aime.knowledge.content_bank import CLICHES
from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES
from aime.models import ContentItem
from aime.qc import CheckResult, build_report
from aime.timeutil import utcnow

DUPLICATE_WINDOW = timedelta(days=120)
MAX_SHINGLE_SIMILARITY = 0.45
MAX_IMAGE_HASH_DISTANCE = 6
LINKEDIN_MAX_CHARS = 3000

# Business vocabulary the general dictionary doesn't know (lower case).
VOCABULARY = {
    "analytics", "automations", "chatbot", "chatbots", "crm", "crms", "dashboards", "devops", "ecommerce",
    "fintech", "handoff", "handoffs", "onboarding", "orchestration", "proptech", "revops", "saas", "upsell",
    "workflow", "workflows", "multi", "rekeyed", "keyed", "realtor", "realtors", "startups", "kpi", "kpis", "llm",
    "llms", "api", "apis", "b2b", "linkedin", "followup", "followups", "hashtag", "hashtags", "inbox", "inboxes",
    "dataset", "datasets", "toolset", "toolsets", "reskill", "upskill", "timeline", "timelines", "prioritize",
    "digitize", "agentic", "ups", "admin", "rekey", "rekeying", "offboarding", "tech",
}
THIRD_PARTIES = ["Google", "Microsoft", "OpenAI", "Anthropic", "Salesforce", "HubSpot", "Amazon", "AWS", "Meta",
                 "Apple", "IBM", "Oracle", "SAP", "Gartner", "McKinsey", "Forrester", "Deloitte", "Accenture",
                 "Zapier", "Slack", "Zoom", "Nvidia", "NVIDIA", "Tesla", "Zillow", "Redfin", "Pipedrive", "Zoho"]
EXTRA_HASHTAGS = {"#B2B", "#RealEstateMarketing", "#DigitalTransformation", "#AI", "#SmallBusiness", "#SalesOps"}
A_EXCEPTIONS = {"one", "once", "unique", "unit", "united", "universal", "university", "use", "used", "useful",
                "user", "users", "usual", "usually", "utility", "european", "eu", "us", "u", "ubiquitous", "uniform"}
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_HASHTAG = re.compile(r"#\w+")
# Figures written in words are figures too. "one", "two" and "three" alone are ordinary English ("one process", "two
# systems"), so those count only inside a statistic ("three in ten", "two out of three", "three times faster").
_LOW = r"one|two|three"
_HIGH = (r"four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
         r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion")
_NUMBER_WORD = re.compile(rf"\b(?:{_HIGH})\b"
                          rf"|\b(?:{_LOW})\s+(?:percent|per\s+cent)\b"
                          rf"|\b(?:{_LOW})\s+(?:out\s+of|in|of\s+every)\s+(?:{_LOW})\b"
                          rf"|\b(?:{_LOW})\s+times\b|\b(?:twice|double|triple)\s+(?:as|the)\b"
                          r"|\bhalf\s+of\b|\b(?:a|one|two)\s+thirds?\s+of\b|\b(?:a|one|three)\s+quarters?\s+of\b"
                          r"|\b(?:dozens|hundreds|thousands|millions|billions)\s+of\b", re.I)
# What Pillow raises on a damaged file: broken PNG chunks surface as SyntaxError.
_BAD_IMAGE = (OSError, ValueError, SyntaxError, Image.DecompressionBombError)


def _prose(title: str, body: str) -> str:
    return _URL.sub(" ", _HASHTAG.sub(" ", f"{title}\n{body}"))


# First-hand experience, for which there is nothing on record: the check cannot tell a true one from an invented one.
_FIRST_HAND = re.compile(r"\b(?:first[- ]?hand|in our experience|(?:we|i)(?:'ve|\u2019ve| have) (?:seen|watched|witnessed|helped|worked with))\b", re.I)


def check_gate(body: str, settings) -> CheckResult:
    cfg = settings.section("content")
    q = quality_check(body, int(cfg.get("min_words", 70)), int(cfg.get("max_words", 260)))
    return CheckResult("content rules", "pass" if q["passed"] else "fail", "; ".join(q["problems"]))


def check_claims(title: str, body: str) -> CheckResult:
    text = _prose(title, body)
    found = []
    words = _NUMBER_WORD.search(text)
    if re.search(r"\d", text):
        found.append("contains a number")
    elif words:
        found.append(f"contains a number in words ('{words.group(0)}')")
    named = sorted({n for n in THIRD_PARTIES if re.search(rf"\b{re.escape(n)}\b", text)})
    if named:
        found.append(f"names {', '.join(named)}")
    first_hand = _FIRST_HAND.search(text)
    if first_hand:
        found.append(f"claims first-hand experience ('{first_hand.group(0)}')")
    if not found:
        return CheckResult("factual claims", "pass", "no figures or named third parties")
    return CheckResult("factual claims", "uncertain", "; ".join(found) + " (needs a person to verify the source)")


_SPELLER: list = []


def _speller():
    """Loading the English dictionary takes about a second, so it is loaded once per process."""
    if not _SPELLER:
        from spellchecker import SpellChecker
        _SPELLER.append(SpellChecker(language="en", distance=1))  # one edit catches common typos, and is fast
    return _SPELLER[0]


def check_spelling(title: str, body: str) -> CheckResult:
    try:
        checker = _speller()
    except ImportError:
        return CheckResult("spelling", "uncertain", "spell checker not installed")
    words = []
    for token in re.findall(r"[A-Za-z][A-Za-z'’\-]*", _prose(title, body)):
        if "'" in token or "’" in token:
            continue  # contractions and possessives ("doesn't", "team's") are not dictionary words
        for part in token.split("-"):
            if len(part) < 3 or part.lower() in VOCABULARY:
                continue
            if part.isupper() or re.search(r"[a-z][A-Z]", part):  # acronyms and CamelCase names
                continue
            words.append(part.lower())
    suspicious = []
    for w in sorted(checker.unknown(words)):
        fix = checker.correction(w)
        if fix and fix != w:
            suspicious.append(f"{w} → {fix}")
    if suspicious:
        return CheckResult("spelling", "fail", ", ".join(suspicious[:6]))
    return CheckResult("spelling", "pass", f"{len(words)} words checked")


def check_cliches(title: str, body: str) -> CheckResult:
    low = f"{title}\n{body}".lower()
    hits = [c for c in CLICHES if c in low]
    return CheckResult("clichés", "fail" if hits else "pass", ", ".join(f"'{h}'" for h in hits))


def check_grammar(title: str, body: str) -> CheckResult:
    text = _prose(title, body)
    problems = []
    for m in re.finditer(r"\b(\w+)\s+\1\b", text, re.I):
        if m.group(1).lower() not in ("that", "had"):
            problems.append(f"repeated word '{m.group(1)}'")
    if re.search(r"[^\S\n]{2,}", text):
        problems.append("double space")
    if re.search(r"\s[,.;:!?]", text):
        problems.append("space before punctuation")
    for m in re.finditer(r"[.!?]\s+([a-z])", text):
        problems.append(f"sentence starts with lower case '{m.group(1)}'")
        break
    for m in re.finditer(r"\ba\s+([aeiouAEIOU]\w*)", text):
        if m.group(1).lower() not in A_EXCEPTIONS:
            problems.append(f"'a {m.group(1)}' should be 'an'")
    if text.count("(") != text.count(")") or text.count('"') % 2:
        problems.append("unbalanced brackets or quotes")
    return CheckResult("grammar", "fail" if problems else "pass", "; ".join(dict.fromkeys(problems)))


def check_brand(title: str, body: str, hashtags: list[str], settings) -> CheckResult:
    brand = settings.company_name
    text = f"{title}\n{body}"
    problems = []
    for m in re.finditer(r"digital[\s\-]*hub[\s\-]*solutions?", text, re.I):
        if m.group(0) != brand:
            problems.append(f"brand written as '{m.group(0)}'")
    allowed = EXTRA_HASHTAGS | {t for s in SERVICES.values() for t in s.hashtags} | {
        t for i in INDUSTRIES.values() for t in i.hashtags}
    off_list = [t for t in hashtags if t not in allowed]
    if off_list:
        problems.append(f"hashtags not on the approved list: {' '.join(off_list)}")
    max_tags = int(settings.section("content").get("max_hashtags", 5))
    if len(hashtags) > max_tags:
        problems.append(f"{len(hashtags)} hashtags (max {max_tags})")
    return CheckResult("brand", "fail" if problems else "pass", "; ".join(problems))


def check_linkedin_rules(title: str, body: str, hashtags: list[str]) -> CheckResult:
    full = f"{body}\n\n{' '.join(hashtags)}"
    problems, uncertain = [], []
    if len(full) > LINKEDIN_MAX_CHARS:
        problems.append(f"{len(full)} characters (LinkedIn allows {LINKEDIN_MAX_CHARS})")
    if re.search(r"\b(comment\s+(yes|below|['\"]?\w+['\"]?\s+if)|like\s+if|tag\s+(someone|a\s+friend|\d)|share\s+if)\b",
                 full, re.I):
        problems.append("engagement bait")
    if re.search(r"(^|\s)@\w", full):
        problems.append("@-mentions a person or page")
    if re.search(r"\b(bit\.ly|tinyurl\.com|t\.co|goo\.gl|ow\.ly)/", full, re.I):
        problems.append("shortened link")
    elif _URL.search(full):
        uncertain.append("contains a link (check where it goes)")
    if problems:
        return CheckResult("LinkedIn rules", "fail", "; ".join(problems))
    return CheckResult("LinkedIn rules", "uncertain" if uncertain else "pass", "; ".join(uncertain))


def image_hash(path: Path) -> str:
    """64-bit difference hash: near-identical images give near-identical hashes."""
    with Image.open(path) as img:
        px = list(img.convert("L").resize((9, 8)).tobytes())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}"


def _shingles(text: str) -> set[str]:
    words = re.findall(r"[a-z']+", text.lower())
    return {" ".join(words[i:i + 3]) for i in range(max(0, len(words) - 2))}


def check_duplicates(session: Session, title: str, body: str, phash: str | None, exclude_id: int | None) -> CheckResult:
    since = utcnow() - DUPLICATE_WINDOW
    others = session.execute(select(ContentItem).where(ContentItem.created_at >= since,
                                                       ContentItem.status != "rejected")).scalars().all()
    mine = _shingles(body)
    problems = []
    for o in others:
        if exclude_id is not None and o.id == exclude_id:
            continue
        if o.title.strip().lower() == title.strip().lower():
            problems.append(f"same title as post #{o.id}")
            continue
        theirs = _shingles(o.body)
        if mine and theirs:
            sim = len(mine & theirs) / len(mine | theirs)
            if sim >= MAX_SHINGLE_SIMILARITY:
                problems.append(f"{int(sim * 100)}% similar to post #{o.id}")
        other_hash = ((o.quality or {}).get("image") or {}).get("phash")
        if phash and other_hash and bin(int(phash, 16) ^ int(other_hash, 16)).count("1") <= MAX_IMAGE_HASH_DISTANCE:
            problems.append(f"image nearly identical to post #{o.id}")
    return CheckResult("duplicates", "fail" if problems else "pass", "; ".join(problems))


def check_image(image_path: Path | None, image_info: dict) -> CheckResult:
    if not image_path or not image_path.exists():
        return CheckResult("image", "uncertain", "no image file")
    if image_path.stat().st_size > 5 * 1024 * 1024:
        return CheckResult("image", "fail", "image larger than 5 MB")
    try:
        with Image.open(image_path) as img:
            size = img.size
    except _BAD_IMAGE as exc:
        return CheckResult("image", "fail", f"damaged image file ({type(exc).__name__})")
    if size not in ((1080, 1350), (1080, 1080), (1200, 627)):
        return CheckResult("image", "fail", f"unexpected size {size[0]}x{size[1]}")
    if image_info.get("renderer") == "browser":
        return CheckResult("image", "pass", f"{image_info.get('style')} design, layout checks passed")
    note = "simple card"
    if image_info.get("problems"):
        note += f" (designed images unavailable: {image_info['problems'][0]})"
    return CheckResult("image", "pass", note)


def check_consistency(title: str, body: str, image_info: dict) -> CheckResult:
    if image_info.get("renderer") != "browser":
        return CheckResult("image matches caption", "pass", "card shows the post title")
    bullets = [ln.lstrip("•").strip().rstrip(".") for ln in body.splitlines() if ln.strip().startswith("•")]
    if image_info.get("headline", title).strip() != title.strip():
        return CheckResult("image matches caption", "fail", "image headline differs from the post title")
    missing = [p for p in image_info.get("points", []) if not any(b.startswith(p) for b in bullets)]
    if missing:
        return CheckResult("image matches caption", "fail", f"image point not in the post: {missing[0]}")
    return CheckResult("image matches caption", "pass", "headline and points come from the post")


def run_quality_checks(session: Session, settings, *, title: str, body: str, hashtags: list[str],
                       image_path: Path | None, image_info: dict, exclude_id: int | None = None) -> dict:
    image_info = dict(image_info or {})
    if image_path and image_path.exists() and not image_info.get("phash"):
        try:
            image_info["phash"] = image_hash(image_path)
        except _BAD_IMAGE:  # reported by check_image; the post can still be edited and saved
            image_info["phash"] = None
    results = [
        check_gate(body, settings),
        check_claims(title, body),
        check_spelling(title, body),
        check_grammar(title, body),
        check_cliches(title, body),
        check_brand(title, body, hashtags, settings),
        check_linkedin_rules(title, body, hashtags),
        check_duplicates(session, title, body, image_info.get("phash"), exclude_id),
        check_image(image_path, image_info),
        check_consistency(title, body, image_info),
    ]
    report = build_report(results, title, body, hashtags)
    report["image_phash"] = image_info.get("phash")
    return report
