"""Post composition: local-LLM drafting behind a strict quality gate, with a deterministic template fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aime.knowledge.content_bank import ANGLES, BRAND_LINES, BULLET_INTROS, CLICHES, CTAS
from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES
from aime.llm.base import LLMError, LLMProvider

SYSTEM_PROMPT = (
    "You are a senior B2B content writer for {company}, a software firm. "
    "Write one LinkedIn post in plain, professional US English for business readers in the US and abroad.\n"
    "Rules:\n"
    "- 120 to 200 words.\n"
    "- Start with a one-sentence hook on its own line: a specific observation, not a general statement.\n"
    "- Include one concrete example from the reader's day-to-day work.\n"
    "- Short paragraphs. Up to three bullet points, each starting with the character •\n"
    "- End with one open question that invites comments.\n"
    "- Do NOT include hashtags, emojis, statistics, percentages, dollar amounts, numbers of clients, "
    "client names, company names, testimonials, case studies, or promises of guaranteed results.\n"
    "- Do not invent facts. Speak from practical experience in general terms. No idioms or slang.\n"
    "- Never use these phrases: " + "; ".join(f'"{c}"' for c in CLICHES) + ".\n"
    "- Output only the post text, with no title and no preamble."
)

USER_PROMPT = (
    "Topic: {title}\n"
    "Audience: {audience} in the United States\n"
    "A pain point they recognize: {pain}\n"
    "Our relevant service: {service} — {pitch}\n"
    "Mention the service naturally once at most; do not make it a sales pitch."
)

_BANNED = [
    (re.compile(r"\d+(?:\.\d+)?\s?%"), "contains a percentage"),
    (re.compile(r"\$\s?\d"), "contains a dollar amount"),
    (re.compile(r"\b(guarantee[ds]?|guaranteed)\b", re.I), "promises guaranteed results"),
    (re.compile(r"\b(case study|our clients?|testimonial|customers? (?:like|such as))\b", re.I), "implies client claims"),
    (re.compile(r"\b(studies show|research shows|according to|survey)\b", re.I), "cites unverified research"),
    (re.compile(r"\b\d{2,}\s*(?:x|times|hours|companies|businesses|clients)\b", re.I), "contains a quantitative claim"),
    (re.compile(r"\bas an ai\b|\blanguage model\b", re.I), "mentions being an AI"),
    (re.compile(r"\[[^\]]+\]|\{[^}]+\}"), "contains placeholders"),
    (re.compile(r"\byour company\b|\bcompany name\b", re.I), "contains a placeholder brand"),
    (re.compile(r"\b(?:we|we've|we have|our team(?: has)?) (?:helped|delivered|built for|worked with|saved|increased)\b",
                re.I), "claims past results"),
    (re.compile(r"\b(?:industry[- ]leading|world[- ]class|best[- ]in[- ]class|number one|award[- ]winning|"
                r"leading provider|#1)\b", re.I), "uses unverifiable superlatives"),
]
# Words that say nothing about what a post is about ("The hidden cost of ...", "Give ... back").
_TITLE_FILLER = frozenset("a about an and are at back by do does for from give how in into is it its just need needs not of on one or "
                          "our real start stops than that the their them these this those to we what when where why with without work you your".split())
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_PREAMBLE_RE = re.compile(r"^(here(?:'s| is)|sure|certainly|below is)[^\n]*:\s*\n", re.I)


@dataclass
class Draft:
    title: str
    body: str
    generator: str
    quality: dict = field(default_factory=dict)


def build_hashtags(service_id: str, industry_id: str, max_tags: int = 5) -> list[str]:
    tags: list[str] = []
    for tag in list(SERVICES[service_id].hashtags) + list(INDUSTRIES[industry_id].hashtags) + ["#B2B"]:
        if tag not in tags:
            tags.append(tag)
    return tags[:max_tags]


def compose_template(service_id: str, industry_id: str, angle_idx: int, pain_idx: int = 0, cta_idx: int = 0) -> Draft:
    angles = ANGLES[service_id]
    angle = angles[angle_idx % len(angles)]
    ind = INDUSTRIES[industry_id]
    pain = ind.pains[pain_idx % len(ind.pains)]
    fill = {"audience": ind.audience, "pain": pain}
    hook = angle["hook"].format(**fill)
    insight = angle["insight"].format(**fill)
    bullets = "\n".join(f"• {b}" for b in angle["bullets"])
    intro = BULLET_INTROS[(angle_idx + pain_idx) % len(BULLET_INTROS)]
    cta = CTAS[cta_idx % len(CTAS)]
    body = f"{hook}\n\n{insight}\n\n{intro}\n{bullets}\n\n{angle['closing']}\n\n{cta}"
    return Draft(title=angle["title"], body=body, generator="template")


def add_brand_line(body: str, company: str, service_id: str) -> str:
    """Add the service's factual brand line just before the closing question (once)."""
    line = BRAND_LINES.get(service_id, "").format(company=company)
    if not line or company.lower() in body.lower():
        return body
    paragraphs = body.rstrip().split("\n\n")
    if paragraphs and paragraphs[-1].strip().endswith("?"):
        return "\n\n".join(paragraphs[:-1] + [line, paragraphs[-1]])
    return f"{body.rstrip()}\n\n{line}"


def sanitize(text: str) -> str:
    text = text.strip().strip('"').strip()
    text = _PREAMBLE_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and all(tok.startswith("#") for tok in stripped.split()):
            continue  # hashtag-only line: we add our own hashtags
        stripped = re.sub(r"\s#\w+", "", stripped)
        stripped = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped)  # LinkedIn shows markdown literally
        stripped = re.sub(r"^(?:[-*•·●▪]\s+)+", "• ", stripped)  # one bullet marker
        lines.append(stripped)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def quality_check(body: str, min_words: int = 70, max_words: int = 260) -> dict:
    words = len(re.findall(r"[A-Za-z']+", body))
    problems = []
    if words < min_words:
        problems.append(f"too short ({words} words)")
    if words > max_words:
        problems.append(f"too long ({words} words)")
    if len(body) > 2900:
        problems.append("over LinkedIn length budget")
    for pattern, reason in _BANNED:
        if pattern.search(body):
            problems.append(reason)
    if "?" not in body[-300:]:
        problems.append("no closing question")
    ascii_letters = sum(1 for c in body if c.isascii() and c.isalpha())
    letters = sum(1 for c in body if c.isalpha()) or 1
    if ascii_letters / letters < 0.97:
        problems.append("not plain English")
    lines = [ln.strip() for ln in body.splitlines()]
    bullets = sum(1 for ln in lines if ln.startswith("•"))
    if bullets > 4:
        problems.append(f"too many bullets ({bullets})")
    if len([b for b in body.split("\n\n") if b.strip()]) < 3:
        problems.append("needs short paragraphs")
    if body.count("!") > 1:
        problems.append("too many exclamation marks")
    sentences = [x for x in re.split(r"[.!?]+\s", re.sub(r"•[^\n]*", "", body)) if x.strip()]
    if sentences and words / len(sentences) > 30:
        problems.append("sentences too long")
    return {"passed": not problems, "problems": problems, "words": words}


def title_belongs(title: str, body: str) -> bool:
    """Most of what the title speaks of is in the text. A model handed one topic can write about another, and the title stays."""
    words = [w for w in re.findall(r"[a-z]+", title.lower()) if w not in _TITLE_FILLER]
    low = body.lower()
    found = sum(bool(re.search(rf"\b{w[:len(w) - 2]}" if len(w) > 5 else rf"\b{w}\b", low)) for w in words)
    return not words or found * 2 > len(words)


def write_post(llm: LLMProvider | None, company: str, service_id: str, industry_id: str,
               angle_idx: int, pain_idx: int, cta_idx: int, min_words: int = 70, max_words: int = 260) -> Draft:
    template = compose_template(service_id, industry_id, angle_idx, pain_idx, cta_idx)
    template.body = add_brand_line(template.body, company, service_id)
    template.quality = quality_check(template.body, min_words, max_words)
    if llm is None:
        template.quality["llm"] = "not available"
        return template

    svc = SERVICES[service_id]
    ind = INDUSTRIES[industry_id]
    prompt = USER_PROMPT.format(title=template.title, audience=ind.audience,
                                pain=ind.pains[pain_idx % len(ind.pains)], service=svc.name, pitch=svc.pitch)
    attempts = []
    for temperature in (0.7, 0.5):
        try:
            raw = llm.generate(prompt, system=SYSTEM_PROMPT.format(company=company), max_tokens=450,
                               temperature=temperature)
        except LLMError as exc:
            attempts.append(f"error: {exc}")
            break
        text = sanitize(raw)
        body = add_brand_line(text, company, service_id)
        q = quality_check(body, min_words, max_words)
        if not title_belongs(template.title, text):  # before the brand line, which speaks of the service whatever the post says
            q["passed"] = False
            q["problems"].append(f"the text is not about its title ({template.title})")
        if q["passed"]:
            q["llm_attempts"] = attempts
            return Draft(title=template.title, body=body, generator=llm.label, quality=q)
        attempts.append("; ".join(q["problems"]))
    template.quality["llm_rejected"] = attempts
    return template
