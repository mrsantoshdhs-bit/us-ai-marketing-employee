"""Render branded HTML/CSS templates to PNG with a locally installed Chromium browser (Edge or Chrome), headless.

Each render runs twice in an isolated temporary profile: once to dump the DOM (a small script in the template
measures text overflow, overlaps, smallest font, contrast and empty space) and once to take the screenshot.
If the layout fails, the next style is tried; if no browser is available, callers fall back to the Pillow card.

On Windows a Chromium browser starting with a new profile checks whether the account's password is blank by
signing in with an empty one, which Windows counts as a failed sign-in. One per browser start locked the Windows
account within minutes (10 failures in 10 minutes) and stopped the scheduled tasks, so every new profile is given
the answer the browser saved last time (OS_PASSWORD_STATE) and the check runs once per password change.
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
# The browser's own answer to "is the Windows password blank?" ("Local State" › password_manager › os_password_*),
# shared by every render of this Windows user (the scheduled tasks, the dashboard and test runs alike).
OS_PASSWORD_STATE = Path(tempfile.gettempdir()) / "aime-browser-os-password.json"


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
            "--force-device-scale-factor=1", f"--window-size={w},{h}", "--virtual-time-budget=3000",
            # no crash reporter: it outlives the browser and keeps files in the throwaway profile open
            "--disable-breakpad", "--disable-crash-reporter",
            # Edge 156+ relaunches itself and returns at once, losing --dump-dom and racing --screenshot; Chrome ignores it
            "--edge-skip-compat-layer-relaunch"]
    if os.name != "nt":
        args.append("--no-sandbox")  # CI containers; on Windows the sandbox stays on
    return args


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=flags)


def seed_profile(profile: Path) -> None:
    """Give a new profile the browser's saved answer about the Windows password, so it does not sign in to find out."""
    try:
        answer = json.loads(OS_PASSWORD_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(answer, dict) and answer:
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "Local State").write_text(json.dumps({"password_manager": answer}), encoding="utf-8")


def keep_answer(profile: Path) -> None:
    """Save the answer the browser wrote into this profile, for the next new profile."""
    try:
        saved = json.loads((profile / "Local State").read_text(encoding="utf-8")).get("password_manager") or {}
        answer = {k: v for k, v in saved.items() if k.startswith("os_password_")}
        if answer:
            OS_PASSWORD_STATE.write_text(json.dumps(answer), encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        pass


def _run_browser(browser: Path, profile: Path, w: int, h: int, extra: list[str], timeout: float):
    seed_profile(profile)
    try:
        return _run(_browser_args(browser, profile, w, h) + extra, timeout)
    finally:
        keep_answer(profile)


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
    if metrics.get("crossings"):
        problems.append(f"decorative line crosses text: {', '.join(metrics['crossings'])}")
    if metrics.get("blank_patches"):
        problems.append(f"blank or unpainted patch in the image: {'; '.join(metrics['blank_patches'][:3])}")
    return problems


def find_blank_patches(png: Path, white_boxes: list | None = None, black_boxes: list | None = None) -> list[str]:
    """Solid pure-white or pure-black blocks that no element on the page paints: unpainted areas or render glitches.

    Each colour is masked exactly and the areas the page itself paints in it are cleared. The mask is reduced to
    3x3-pixel cells (a cell stays set only if all 9 pixels match), then eroded by 3x3 cells, so any solid block from
    about 12 px across survives and is reported with its approximate position and size."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    with Image.open(png) as img:
        channels = img.convert("RGB").split()
    found = []
    for name, value, allowed in (("white", 255, white_boxes or []), ("black", 0, black_boxes or [])):
        r, g, b = (c.point(lambda v, value=value: 255 if v == value else 0) for c in channels)
        mask = ImageChops.multiply(ImageChops.multiply(r, g), b)
        draw = ImageDraw.Draw(mask)
        for left, top, right, bottom in allowed:
            draw.rectangle([left - 2, top - 2, right + 2, bottom + 2], fill=0)
        cells = mask.reduce(3).point(lambda v: 255 if v == 255 else 0)
        box = cells.filter(ImageFilter.MinFilter(3)).getbbox()
        if box:
            x0, y0 = max(0, (box[0] - 1) * 3), max(0, (box[1] - 1) * 3)
            x1, y1 = min(mask.width, (box[2] + 1) * 3), min(mask.height, (box[3] + 1) * 3)
            found.append(f"{name} at {x0},{y0} about {x1 - x0}x{y1 - y0} px")
    return found


def render_one(browser: Path, html: str, out_png: Path, w: int, h: int, timeout: float = 60) -> dict:
    """Measure, then screenshot. Returns the layout metrics (empty if the page could not be measured).
    The throwaway browser profile is removed afterwards; a file a browser helper still holds open is left for the
    system's temp cleanup instead of failing the render."""
    with tempfile.TemporaryDirectory(prefix="aime-render-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        page = work / "card.html"
        page.write_text(html, encoding="utf-8")
        url = page.resolve().as_uri()
        dump = _run_browser(browser, work / "p1", w, h, ["--dump-dom", url], timeout)
        m = re.search(r'<pre id="aime-qc">(.*?)</pre>', dump.stdout or "", re.S)
        try:
            metrics = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&")) if m else {}
        except ValueError:
            metrics = {}
        shot = work / "shot.png"
        for attempt in (2, 3):  # a blank patch is a rendering glitch, not a layout fault: take the shot once more
            shot.unlink(missing_ok=True)
            _run_browser(browser, work / f"p{attempt}", w, h, [f"--screenshot={shot}", url], timeout)
            if not (shot.exists() and shot.stat().st_size > 0):
                break
            patches = find_blank_patches(shot, metrics.get("white_boxes"), metrics.get("black_boxes")) if metrics else []
            if metrics:
                metrics["blank_patches"] = patches
            if not patches:
                break
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
