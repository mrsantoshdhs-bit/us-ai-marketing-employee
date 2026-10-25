"""Post composition: local-LLM drafting behind a strict quality gate, with a deterministic template fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aime.knowledge.content_bank import ANGLES, BULLET_INTROS, CTAS
from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES
from aime.llm.base import LLMError, LLMProvider

SYSTEM_PROMPT = (
    "You are a senior B2B content writer for {company}, a software firm. "
    "Write one LinkedIn post in professional US English.\n"
    "Rules:\n"
    "- 120 to 200 words.\n"
    "- Start with a one-sentence hook on its own line.\n"
    "- Short paragraphs. Up to three bullet points, each starting with the character •\n"
    "- End with one open question that invites comments.\n"
    "- Do NOT include hashtags, emojis, statistics, percentages, dollar amounts, numbers of clients, "
    "client names, testimonials, case studies, or promises of guaranteed results.\n"
    "- Do not invent facts. Speak from practical experience in general terms.\n"
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
]
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
    return {"passed": not problems, "problems": problems, "words": words}


def write_post(llm: LLMProvider | None, company: str, service_id: str, industry_id: str,
               angle_idx: int, pain_idx: int, cta_idx: int, min_words: int = 70, max_words: int = 260) -> Draft:
    template = compose_template(service_id, industry_id, angle_idx, pain_idx, cta_idx)
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
        body = sanitize(raw)
        q = quality_check(body, min_words, max_words)
        if q["passed"]:
            q["llm_attempts"] = attempts
            return Draft(title=template.title, body=body, generator=llm.label, quality=q)
        attempts.append("; ".join(q["problems"]))
    template.quality["llm_rejected"] = attempts
    return template
