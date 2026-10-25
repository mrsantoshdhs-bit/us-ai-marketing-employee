"""Branded 1200x627 PNG cards for LinkedIn posts (rendered locally with Pillow)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES

WIDTH, HEIGHT = 1200, 627
PALETTES = {
    "custom_crm": ((17, 45, 78), (63, 136, 197)),
    "ai_agents": ((32, 18, 77), (124, 92, 255)),
    "agent_orchestration": ((20, 33, 61), (0, 168, 150)),
    "bpa": ((24, 52, 45), (64, 168, 120)),
    "sales_automation": ((61, 20, 38), (226, 87, 76)),
    "leadgen_automation": ((52, 34, 10), (240, 160, 40)),
    "saas_dev": ((15, 40, 60), (40, 180, 220)),
    "custom_software": ((30, 30, 40), (120, 130, 160)),
    "ai_consulting": ((40, 16, 60), (200, 90, 200)),
}
_FONT_CANDIDATES_BOLD = ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
                         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_FONT_CANDIDATES = ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def _font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_card(path: Path, title: str, service_id: str, industry_id: str, company: str,
                tagline: str | None = None) -> Path:
    dark, accent = PALETTES.get(service_id, ((25, 25, 35), (90, 120, 200)))
    img = Image.new("RGB", (WIDTH, HEIGHT), dark)
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):  # vertical gradient
        t = y / HEIGHT
        color = tuple(int(dark[i] * (1 - t * 0.35) + accent[i] * t * 0.35) for i in range(3))
        draw.line([(0, y), (WIDTH, y)], fill=color)
    draw.rectangle([0, 0, 18, HEIGHT], fill=accent)
    dot = tuple(int(c * 0.55 + 255 * 0.12) for c in accent)
    for gx in range(WIDTH - 360, WIDTH - 60, 28):  # subtle dot grid
        for gy in range(330, HEIGHT - 150, 28):
            draw.ellipse([gx, gy, gx + 3, gy + 3], fill=dot)
    draw.ellipse([WIDTH - 330, -140, WIDTH + 130, 320], outline=accent, width=6)
    draw.ellipse([WIDTH - 250, -60, WIDTH + 50, 240], outline=accent, width=2)

    tag_font = _font(_FONT_CANDIDATES_BOLD, 24)
    foot_font = _font(_FONT_CANDIDATES_BOLD, 24)
    small_font = _font(_FONT_CANDIDATES, 20)
    badge_font = _font(_FONT_CANDIDATES_BOLD, 26)

    tag = f"{SERVICES[service_id].name.upper()}  ·  {INDUSTRIES[industry_id].label.upper()}"
    draw.text((80, 64), tag, font=tag_font, fill=accent)
    draw.line([(80, 104), (150, 104)], fill=accent, width=4)

    for size in (58, 52, 46):  # shrink long titles so they always fit
        title_font = _font(_FONT_CANDIDATES_BOLD, size)
        lines = _wrap(draw, title, title_font, WIDTH - 420)
        if len(lines) <= 4:
            break
    y = 136
    for line in lines[:4]:
        draw.text((80, y), line, font=title_font, fill=(255, 255, 255))
        y += int(size * 1.27)

    draw.line([(80, HEIGHT - 118), (WIDTH - 80, HEIGHT - 118)], fill=accent, width=2)
    initials = monogram(company)
    draw.rounded_rectangle([80, HEIGHT - 96, 80 + 64, HEIGHT - 32], radius=12, fill=accent)
    tw = draw.textlength(initials, font=badge_font)
    draw.text((80 + (64 - tw) / 2, HEIGHT - 82), initials, font=badge_font, fill=dark)
    draw.text((162, HEIGHT - 90), company, font=foot_font, fill=(240, 240, 248))
    if tagline:
        draw.text((162, HEIGHT - 58), tagline[:90], font=small_font, fill=(190, 196, 214))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return path


_NON_INITIAL_WORDS = {"private", "limited", "pvt", "ltd", "llc", "inc", "co", "company", "corp", "the", "of", "and"}


def monogram(company: str) -> str:
    words = [w for w in company.replace(".", " ").split() if w.lower() not in _NON_INITIAL_WORDS]
    letters = "".join(w[0] for w in words if w[0].isalnum()).upper()
    return (letters or company[:2].upper())[:3]
