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
    page = TestClient(create_app(ctx)).get("/reports").text
    assert "Posts awaiting approval" in page and "Posts scheduled" in page and "Emails awaiting approval" in page


def _tile(page, label):
    import re
    m = re.search(r"(\d+)\s*(?:</[^>]+>\s*<[^>]+>\s*)+" + re.escape(label) + "<", page)
    return int(m.group(1)) if m else None


def test_usable_emails_and_qualified_companies_count_what_can_really_be_used(make_ctx):
    from datetime import timedelta

    from sqlalchemy import func, select

    from aime.agents.analytics import sales_kpis
    from aime.models import Company, Contact, Lead, Meeting
    from aime.timeutil import utcnow
    ctx = make_ctx()
    with ctx.db.session() as s:
        cos = [Company(name=f"Co{i}", normalized_name=f"co{i}", domain=f"co{i}.example", source="test") for i in range(3)]
        s.add_all(cos)
        s.flush()
        s.add_all([
            Contact(company_id=cos[0].id, first_name="Ann", last_name="Able", email="ann@co0.example", source="website",
                    email_status="published"),
            Contact(company_id=cos[1].id, first_name="Ben", last_name="Bold", email="ben@co1.example",
                    source="website_withdrawn", email_status="published"),
            Contact(company_id=cos[2].id, first_name="Cyd", last_name="Cole", email="cyd@co2.example", source="website",
                    email_status="published", consent_status="opted_out"),
            Lead(company_id=cos[0].id, source="research", status="qualified"),
            Lead(company_id=cos[0].id, source="inbound", status="qualified"),
            Meeting(invitee_email="x@co0.example", starts_at=utcnow(), ends_at=utcnow() + timedelta(minutes=30),
                    source="manual", status="cancelled"),
        ])
    client = TestClient(create_app(ctx))
    page = client.get("/reports").text
    assert _tile(page, "Companies with a usable email") == 1
    assert _tile(page, "Qualified companies") == 1
    reachable = client.get("/contacts?reachable=1").text
    assert "ann@co0.example" in reachable and "cyd@co2.example" not in reachable
    with ctx.db.session() as s:
        count = lambda m, *c: s.execute(select(func.count(m.id)).where(*c)).scalar_one()  # noqa: E731
        k = sales_kpis(count, utcnow() - timedelta(days=1))
    assert k["usable_emails_total"] == 1
    assert k["meetings_booked_period"] == 0 == k["meetings_booked_total"]
