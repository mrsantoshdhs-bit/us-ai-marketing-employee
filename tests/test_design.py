"""Premium images: text comes only from the post, layouts are measured, and the Pillow card is the fallback."""

from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from aime.agents import LinkedInContentAgent
from aime.design.brief import build_brief, style_order
from aime.design.render import check_layout, find_browser, load_brand, render_designed_image, render_html
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
