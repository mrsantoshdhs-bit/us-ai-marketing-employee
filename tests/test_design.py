"""Premium images: text comes only from the post, layouts are measured, and the Pillow card is the fallback."""

from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from aime.agents import LinkedInContentAgent
from aime.design.brief import build_brief, style_order
import aime.design.render as render_mod
from aime.design.render import (check_layout, find_blank_patches, find_browser, load_brand, render_designed_image,
                                 render_html, render_one)
from aime.models import ContentItem
from aime.visuals import monogram, render_post_image

ROOT = Path(__file__).resolve().parent.parent
TITLE = "The hidden cost of copy-paste work"
BODY = """Every manual handoff between two systems is a place where work waits and mistakes creep in.

For real estate teams, it often looks like transaction paperwork re-keyed into several systems.

Where to start:
• List the tasks people repeat every day or week
• Find where data is re-typed from one tool into another
• Automate the handoff, and keep a person on the exceptions

Where does work get stuck in your team's week?"""
BROWSER = find_browser()
needs_browser = pytest.mark.skipif(BROWSER is None, reason="no Edge/Chrome on this machine")


def _brief(title=TITLE, body=BODY, service="bpa"):
    return build_brief(title, body, service, "real_estate", "Digital Hub Solution", "Custom CRM, AI agents and automation")


def test_brief_uses_only_the_posts_own_words():
    b = _brief()
    assert b.headline == TITLE and b.eyebrow == "Business Process Automation" and b.tag == "Real Estate"
    assert b.points == ["List the tasks people repeat every day or week",
                        "Find where data is re-typed from one tool into another",
                        "Automate the handoff, and keep a person on the exceptions"]
    post_words = set((TITLE + " " + BODY).lower().replace(",", " ").split())
    assert all(p.lower().split()[0] in post_words for p in b.points)


def test_long_bullets_are_shortened_to_a_clause_or_left_out():
    body = "Hook line.\n\n• " + " ".join(["word"] * 20) + "\n• Short point here, then a long tail " + "x " * 15
    b = _brief(body=body)
    assert b.points == ["Short point here"]


def test_style_order_prefers_service_styles_and_needs_points():
    assert style_order("agent_orchestration", 0, _brief())[0] == "architecture"
    no_points = _brief(body="Just a hook sentence.\n\nAnd a paragraph?")
    order = style_order("agent_orchestration", 0, no_points)
    assert "architecture" not in order and "infographic" not in order and "corporate" not in order


def test_html_escapes_post_text_but_not_brand_fonts():
    html = render_html(_brief(title="<script>alert(1)</script> & co"), "concept", load_brand(ROOT), "DHS")
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html
    assert "&#34;" not in html.split("</style>")[0]  # quoted font names stay valid CSS


def test_layout_checks():
    good = {"overflow": [], "overlaps": [], "min_font": 26, "contrast": [["headline", 12.0]], "gap": 0.2, "brand": True}
    assert check_layout(good) == []
    bad = {"overflow": ["headline"], "overlaps": ["node1/hub"], "min_font": 18, "contrast": [["tag", 2.1]],
           "gap": 0.45, "brand": False}
    problems = " | ".join(check_layout(bad))
    for word in ("overflows", "overlap", "smallest text", "low contrast", "unbalanced", "brand name missing"):
        assert word in problems
    assert check_layout({}) == ["layout could not be measured"]
    glitch = check_layout({**good, "crossings": ["headline"], "blank_patches": ["white at 1017,1272 about 63x78 px"]})
    assert any("decorative line crosses text: headline" in p for p in glitch)
    assert any("blank or unpainted patch" in p and "1017,1272" in p for p in glitch)


def test_blank_patch_detector(tmp_path):
    from PIL import ImageDraw

    path = tmp_path / "shot.png"
    img = Image.new("RGB", (1080, 1350), (16, 40, 79))
    draw = ImageDraw.Draw(img)
    draw.rectangle([1017, 1271, 1079, 1349], fill=(255, 255, 255))  # the corner glitch seen in production
    draw.rectangle([100, 100, 107, 107], fill=(255, 255, 255))  # small dot: ignored
    draw.rectangle([100, 300, 900, 301], fill=(255, 255, 255))  # thin rule: ignored
    img.save(path)
    found = find_blank_patches(path)
    assert found == ["white at 1017,1272 about 63x78 px"]
    assert find_blank_patches(path, white_boxes=[[1010, 1260, 1080, 1350]]) == []  # the page paints white there
    draw.rectangle([400, 600, 439, 639], fill=(0, 0, 0))
    img.save(path)
    assert any(f.startswith("black at") for f in find_blank_patches(path, white_boxes=[[1010, 1260, 1080, 1350]]))


@needs_browser
def test_browser_renders_a_checked_portrait_image(tmp_path):
    res = render_designed_image(_brief(), ["editorial"], tmp_path / "card.png", ROOT, monogram("Digital Hub Solution"))
    assert res.ok, res.tried
    with Image.open(res.path) as img:
        assert img.size == (1080, 1350)
    assert res.metrics["brand"] and res.metrics["min_font"] >= 26


@needs_browser
def test_browser_layout_check_rejects_overflowing_text(tmp_path):
    huge = _brief(title=" ".join(["Unbelievably"] * 30))
    res = render_designed_image(huge, ["concept"], tmp_path / "card.png", ROOT, monogram("Digital Hub Solution"))
    assert not res.ok and any("overflow" in p for p in res.problems)
    assert not (tmp_path / "card.png").exists()  # a failed design is never kept


@needs_browser
def test_concept_rings_stay_clear_of_text_and_crossings_are_rejected(tmp_path):
    brand = load_brand(ROOT)
    html = render_html(_brief(), "concept", brand, "DHS")
    metrics = render_one(BROWSER, html, tmp_path / "ok.png", 1080, 1350)
    assert metrics["crossings"] == [] and metrics["blank_patches"] == [] and check_layout(metrics) == []
    moved = html.replace(".r1 { width: 560px; height: 560px; right: -230px; top: -250px;",
                         ".r1 { width: 700px; height: 700px; right: 190px; top: 300px;")
    assert moved != html
    metrics = render_one(BROWSER, moved, tmp_path / "bad.png", 1080, 1350)
    assert metrics["crossings"] and any("decorative line crosses text" in p for p in check_layout(metrics))


@needs_browser
def test_blank_patch_triggers_one_retake(tmp_path, monkeypatch):
    calls = []

    def flaky(png, white_boxes=None, black_boxes=None):
        calls.append(png)
        return ["white at 1017,1272 about 63x78 px"] if len(calls) == 1 else []
    monkeypatch.setattr(render_mod, "find_blank_patches", flaky)
    html = render_html(_brief(), "editorial", load_brand(ROOT), "DHS")
    metrics = render_one(BROWSER, html, tmp_path / "card.png", 1080, 1350)
    assert len(calls) == 2 and metrics["blank_patches"] == [] and (tmp_path / "card.png").exists()

    calls.clear()
    monkeypatch.setattr(render_mod, "find_blank_patches", lambda *a, **k: calls.append(1) or ["white at 0,0 about 30x30 px"])
    metrics = render_one(BROWSER, html, tmp_path / "card2.png", 1080, 1350)
    assert len(calls) == 2 and any("blank or unpainted patch" in p for p in check_layout(metrics))


def test_falls_back_to_pillow_card_without_a_browser(make_ctx, monkeypatch):
    ctx = make_ctx(image_renderer="auto")
    monkeypatch.setattr("aime.design.render.find_browser", lambda configured=None: None)
    image = render_post_image(ctx.settings, TITLE, BODY, "bpa", "real_estate")
    assert image["renderer"] == "pillow" and image["path"].exists()
    assert any("no Edge or Chrome" in p for p in image["problems"])


def test_content_agent_records_how_each_image_was_made(ctx):
    res = LinkedInContentAgent(ctx).run({"research_topics": False})
    assert res.ok and res.output.images_by_renderer == {"pillow": len(res.output.created)}
    with ctx.db.session() as s:
        item = s.execute(select(ContentItem)).scalars().first()
        assert item.quality["image"]["renderer"] == "pillow"
        assert item.image_alt.startswith(ctx.settings.company_name)
