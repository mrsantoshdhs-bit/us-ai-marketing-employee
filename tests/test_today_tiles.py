"""Today page tiles: what waits for approval is not counted as scheduled."""

from fastapi.testclient import TestClient

from aime.web.app import create_app
from tests.test_inbox import contacted


def test_today_separates_posts_waiting_for_approval_from_scheduled_ones(make_ctx):
    from aime.models import ContentItem
    ctx, _, _ = contacted(make_ctx)
    with ctx.db.session() as s:
        s.add(ContentItem(service="bpa", industry="logistics", title="Draft", body="Text", status="awaiting_approval",
                          generator="template", fingerprint="tile-test"))
    page = TestClient(create_app(ctx)).get("/").text
    assert "Posts awaiting approval" in page and "Posts scheduled" in page and "Emails awaiting approval" in page
