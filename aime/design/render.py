"""Render branded HTML/CSS templates to PNG with a locally installed Chromium browser (Edge or Chrome), headless.

Each render runs twice in an isolated temporary profile: once to dump the DOM (a small script in the template
measures text overflow, overlaps, smallest font, contrast and empty space) and once to take the screenshot.
If the layout fails, the next style is tried; if no browser is available, callers fall back to the Pillow card.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from aime.design.brief import VisualBrief

TEMPLATES = Path(__file__).parent / "templates"
_ENV = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["j2", "html"]))

DEFAULT_BRAND = {
    "colors": {"navy": "#0b1630", "navy_2": "#12244a", "blue": "#3b82f6", "blue_light": "#8fbaff", "teal": "#14b8a6",
               "teal_light": "#5eead4", "ink": "#eef3ff", "muted": "#b4c3e0", "paper": "#f6f4ef",
               "paper_ink": "#0f172a", "paper_muted": "#3f4a5e", "accent_dark": "#1d4ed8"},
    "fonts": {"sans": '"Segoe UI", "DejaVu Sans", Arial, sans-serif', "serif": 'Georgia, "DejaVu Serif", serif'},
    "image": {"width": 1080, "height": 1350},
}
# Readable in the LinkedIn mobile feed: a 1080 px wide image shows at ~0.35x on a 375 px phone.
MIN_FONT_PX = 26
MIN_CONTRAST = 4.5
MAX_EMPTY_GAP = 0.30  # no vertical band without text taller than 30% of the image
BROWSER_CANDIDATES = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
]


@dataclass
class RenderResult:
    ok: bool
    path: Path | None = None
    style: str | None = None
    renderer: str = "browser"
    metrics: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    tried: list[dict] = field(default_factory=list)


def load_brand(base_dir: Path) -> dict:
    brand = {k: dict(v) for k, v in DEFAULT_BRAND.items()}
    path = base_dir / "config" / "brand.toml"
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for section in brand:
            brand[section].update(data.get(section, {}))
    return brand


def find_browser(configured: str | None = None) -> Path | None:
    if configured:
        p = Path(os.path.expandvars(configured))
        return p if p.exists() else None
    for cand in BROWSER_CANDIDATES:
        p = Path(os.path.expandvars(cand))
        if "%" not in str(p) and p.exists():
            return p
    for name in ("msedge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def render_html(brief: VisualBrief, style: str, brand: dict, monogram: str) -> str:
    img = brand["image"]
    return _ENV.get_template(f"{style}.html.j2").render(
        b=brief, c=brand["colors"], f=brand["fonts"], w=int(img["width"]), h=int(img["height"]), monogram=monogram)


def _browser_args(browser: Path, profile: Path, w: int, h: int) -> list[str]:
    args = [str(browser), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--disable-sync", f"--user-data-dir={profile}",
            "--force-device-scale-factor=1", f"--window-size={w},{h}", "--virtual-time-budget=3000"]
    if os.name != "nt":
        args.append("--no-sandbox")  # CI containers; on Windows the sandbox stays on
    return args


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=flags)


def check_layout(metrics: dict) -> list[str]:
    problems = []
    if not metrics:
        return ["layout could not be measured"]
    if metrics.get("overflow"):
        problems.append(f"text overflows: {', '.join(metrics['overflow'])}")
    if metrics.get("overlaps"):
        problems.append(f"elements overlap: {', '.join(metrics['overlaps'])}")
    if metrics.get("min_font", 0) < MIN_FONT_PX:
        problems.append(f"smallest text {metrics.get('min_font')} px (minimum {MIN_FONT_PX} px for phones)")
    low = [f"{name} {ratio}:1" for name, ratio in metrics.get("contrast", []) if ratio < MIN_CONTRAST]
    if low:
        problems.append(f"low contrast: {', '.join(low)}")
    if metrics.get("gap", 1) > MAX_EMPTY_GAP:
        problems.append(f"unbalanced layout: {int(metrics['gap'] * 100)}% of the height has no text")
    if not metrics.get("brand"):
        problems.append("brand name missing")
    return problems


def render_one(browser: Path, html: str, out_png: Path, w: int, h: int, timeout: float = 60) -> dict:
    """Measure, then screenshot. Returns the layout metrics (empty if the page could not be measured)."""
    with tempfile.TemporaryDirectory(prefix="aime-render-") as tmp:
        work = Path(tmp)
        page = work / "card.html"
        page.write_text(html, encoding="utf-8")
        url = page.resolve().as_uri()
        dump = _run(_browser_args(browser, work / "p1", w, h) + ["--dump-dom", url], timeout)
        m = re.search(r'<pre id="aime-qc">(.*?)</pre>', dump.stdout or "", re.S)
        try:
            metrics = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&")) if m else {}
        except ValueError:
            metrics = {}
        shot = work / "shot.png"
        _run(_browser_args(browser, work / "p2", w, h) + [f"--screenshot={shot}", url], timeout)
        if shot.exists() and shot.stat().st_size > 0:
            out_png.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(shot, out_png)
    return metrics


def render_designed_image(brief: VisualBrief, styles: list[str], out_png: Path, base_dir: Path, monogram: str,
                          browser_path: str | None = None, timeout: float = 60) -> RenderResult:
    """Try styles in order; keep the first whose layout passes every check."""
    browser = find_browser(browser_path)
    if browser is None:
        return RenderResult(ok=False, problems=["no Edge or Chrome browser found"])
    brand = load_brand(base_dir)
    w, h = int(brand["image"]["width"]), int(brand["image"]["height"])
    tried = []
    for style in styles:
        try:
            metrics = render_one(browser, render_html(brief, style, brand, monogram), out_png, w, h, timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            tried.append({"style": style, "problems": [f"render failed: {type(exc).__name__}"]})
            continue
        problems = check_layout(metrics) if out_png.exists() else ["no image produced"]
        tried.append({"style": style, "problems": problems})
        if not problems:
            return RenderResult(ok=True, path=out_png, style=style, metrics=metrics, tried=tried)
    if out_png.exists():
        out_png.unlink()  # never keep an image that failed its layout checks
    return RenderResult(ok=False, problems=[p for t in tried for p in t["problems"]], tried=tried)
