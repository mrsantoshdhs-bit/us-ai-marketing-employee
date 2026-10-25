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


def render_card(path: Path, title: str, service_id: str, industry_id: str, company: str) -> Path:
    dark, accent = PALETTES.get(service_id, ((25, 25, 35), (90, 120, 200)))
    img = Image.new("RGB", (WIDTH, HEIGHT), dark)
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):  # vertical gradient
        t = y / HEIGHT
        color = tuple(int(dark[i] * (1 - t * 0.35) + accent[i] * t * 0.35) for i in range(3))
        draw.line([(0, y), (WIDTH, y)], fill=color)
    draw.rectangle([0, 0, 18, HEIGHT], fill=accent)
    draw.ellipse([WIDTH - 330, -140, WIDTH + 130, 320], outline=accent, width=6)
    draw.ellipse([WIDTH - 250, -60, WIDTH + 50, 240], outline=accent, width=2)

    tag_font = _font(_FONT_CANDIDATES_BOLD, 26)
    title_font = _font(_FONT_CANDIDATES_BOLD, 58)
    foot_font = _font(_FONT_CANDIDATES, 26)

    tag = f"{SERVICES[service_id].name.upper()}  ·  {INDUSTRIES[industry_id].label.upper()}"
    draw.text((80, 70), tag, font=tag_font, fill=accent)

    lines = _wrap(draw, title, title_font, WIDTH - 420)[:4]
    y = 140
    for line in lines:
        draw.text((80, y), line, font=title_font, fill=(255, 255, 255))
        y += 74

    draw.line([(80, HEIGHT - 110), (WIDTH - 80, HEIGHT - 110)], fill=accent, width=2)
    draw.text((80, HEIGHT - 85), company, font=foot_font, fill=(230, 230, 240))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return path
