"""Post image cards: LinkedIn size, and footer text that stays legible in the mobile feed."""

from PIL import Image, ImageDraw

from aime import visuals
from aime.visuals import render_card

TAGLINE = "Custom CRM, AI agents and automation for US businesses"


def _draw() -> ImageDraw.ImageDraw:
    return ImageDraw.Draw(Image.new("RGB", (10, 10)))


def test_card_is_linkedin_size(tmp_path):
    path = render_card(tmp_path / "card.png", "When an off-the-shelf CRM stops fitting your process", "custom_crm",
                       "saas", "Digital Hub Solution", TAGLINE)
    with Image.open(path) as img:
        assert img.size == (visuals.WIDTH, visuals.HEIGHT)


def test_footer_text_stays_legible_on_a_phone():
    # A 375px-wide phone shows the card at 375/1200 of its size; footer text must stay at 8.5px or more there.
    phone = 375 / visuals.WIDTH
    assert min(visuals.FOOTER_TAGLINE_SIZES) * phone >= 8.5
    assert min(visuals.FOOTER_NAME_SIZES) * phone >= 8.5


def test_fit_uses_the_largest_size_that_fits():
    font, text = visuals._fit(_draw(), "Digital Hub Solution", visuals._FONT_CANDIDATES_BOLD, (38, 30), 900)
    assert (font.size, text) == (38, "Digital Hub Solution")


def test_fit_shrinks_then_shortens_text_that_is_too_long():
    draw = _draw()
    font, text = visuals._fit(draw, TAGLINE * 3, visuals._FONT_CANDIDATES, (34, 28), 600)
    assert font.size == 28
    assert text.endswith("…")
    assert draw.textlength(text, font=font) <= 600


def test_long_brand_text_renders_without_error(tmp_path):
    path = render_card(tmp_path / "long.png", "Title", "ai_consulting", "marketing_agencies",
                       "A Very Long Company Name Private Limited Holdings", TAGLINE * 3)
    assert path.exists()
