"""Visual brief: the exact text an image shows, taken from the approved post (never generated separately).

Because every word on the image comes from the post's own title, hook and bullets, the image can't say
something the caption doesn't, and its spelling is whatever passed the post's checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES

STYLES = ("architecture", "editorial", "infographic", "concept", "corporate")
# Styles that suit each service best come first; the rest follow as fallbacks.
PREFERRED = {
    "ai_agents": ("concept", "architecture"),
    "agent_orchestration": ("architecture", "concept"),
    "ai_consulting": ("concept", "editorial"),
    "custom_crm": ("corporate", "infographic"),
    "sales_automation": ("infographic", "corporate"),
    "leadgen_automation": ("infographic", "editorial"),
    "bpa": ("editorial", "infographic"),
    "saas_dev": ("corporate", "architecture"),
    "custom_software": ("corporate", "editorial"),
}
MAX_POINT_WORDS = 14


@dataclass
class VisualBrief:
    eyebrow: str
    tag: str
    lead: str
    highlight: str
    subheadline: str | None
    points: list[str] = field(default_factory=list)
    company: str = ""
    tagline: str = ""

    @property
    def headline(self) -> str:
        return f"{self.lead} {self.highlight}".strip()

    def words(self) -> list[str]:
        """Every word that will appear on the image (for spelling checks)."""
        text = " ".join([self.eyebrow, self.tag, self.headline, self.subheadline or "", *self.points, self.company,
                         self.tagline])
        return re.findall(r"[A-Za-z][A-Za-z'\-]*", text)


def _split_headline(title: str) -> tuple[str, str]:
    title = title.strip()
    m = re.match(r"^(.+?[.:?!])\s+(.+)$", title)
    if m:
        return m.group(1), m.group(2)
    words = title.split()
    if len(words) <= 3:
        return "", title
    cut = (len(words) + 1) // 2
    return " ".join(words[:cut]), " ".join(words[cut:])


def _short_point(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip().rstrip("."))
    words = text.split()
    if len(words) <= MAX_POINT_WORDS:
        return text
    clause = re.split(r"[,;:—]| - ", text)[0].strip()
    if 3 <= len(clause.split()) <= MAX_POINT_WORDS:
        return clause
    return ""  # too long to show well; the caption still carries it


def build_brief(title: str, body: str, service: str, industry: str, company: str, tagline: str) -> VisualBrief:
    lead, highlight = _split_headline(title)
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    hook = lines[0] if lines else ""
    first_sentence = re.split(r"(?<=[.!?])\s", hook)[0] if hook else ""
    sub = first_sentence if first_sentence and len(first_sentence.split()) <= 20 and first_sentence != title else None
    points = [p for p in (_short_point(ln.lstrip("•").strip()) for ln in lines if ln.startswith("•")) if p][:4]
    return VisualBrief(
        eyebrow=SERVICES[service].name if service in SERVICES else "",
        tag=INDUSTRIES[industry].label if industry in INDUSTRIES else "",
        lead=lead, highlight=highlight, subheadline=sub, points=points, company=company, tagline=tagline or "",
    )


def style_order(service: str, seed: int, brief: VisualBrief) -> list[str]:
    """Preferred styles first (rotated by seed), then the rest; styles that need points are skipped without them."""
    preferred = list(PREFERRED.get(service, ()))
    if preferred:
        k = seed % len(preferred)
        preferred = preferred[k:] + preferred[:k]
    order = preferred + [s for s in STYLES if s not in preferred]
    if len(brief.points) < 2:
        order = [s for s in order if s not in ("architecture", "infographic", "corporate")]
    return order
