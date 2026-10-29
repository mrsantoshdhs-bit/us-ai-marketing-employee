"""Findings of the 29 Oct security review: a number written another way in the URL ("+1", "1.0", " 1") must not
slip past the approval rules, the do-not-contact form takes one address or domain and never a pattern, the inbound
API answers a malformed token with 401, and there are no interactive API docs on the signed-in origin."""

from sqlalchemy import select

from aime.crm_ops import suppress
from aime.models import Company, Contact, FollowUp, Suppression
from tests.test_dashboard_auth import ORIGIN
from tests.test_roles import signed_in, team  # noqa: F401  (pytest fixture)


def test_a_number_written_another_way_cannot_reach_an_approval(team):  # noqa: F811
    ctx, ids = team
    bde = signed_in(ctx, "bde")
    fid = ids["followup"]
    for path in (f"/followups/+{fid}/approve", f"/followups/{fid}.0/approve", f"/followups/%20{fid}/approve",
                 f"/followups/0_{fid}/approve", f"/content/+{ids['post']}/approve"):
        assert bde.post(path, headers=ORIGIN, follow_redirects=False).status_code == 404, path
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "awaiting_approval"
    assert bde.get(f"/leads/{ids['lead']}").status_code == 200  # plain numbers work as before


def test_the_do_not_contact_form_takes_an_address_or_a_domain_never_a_pattern(make_ctx):
    from fastapi.testclient import TestClient

    from aime.web.app import create_app

    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="Co", normalized_name="co", source="test")
        s.add(co)
        s.flush()
        s.add_all([Contact(company_id=co.id, email=e, consent_status="inquiry", source="test")
                   for e in ("ann@acme.example", "bob@other.example", "cy@ac_me.example")])
    c = TestClient(create_app(ctx))
    for value in ("%", "_", "%.example", "a%@acme.example", "acme"):
        r = c.post("/suppress", headers=ORIGIN, data={"value": value}, follow_redirects=False)
        assert "one email address or one domain" in r.headers["location"].replace("%20", " "), value
    with ctx.db.session() as s:
        assert s.execute(select(Suppression)).first() is None
        assert {x.consent_status for x in s.execute(select(Contact)).scalars()} == {"inquiry"}
        suppress(s, "ac_me.example", "test")  # called directly: "_" is a letter here, not "any character"
        status = {x.email: x.consent_status for x in s.execute(select(Contact)).scalars()}
        assert status == {"ann@acme.example": "inquiry", "bob@other.example": "inquiry", "cy@ac_me.example": "opted_out"}
    c.post("/suppress", headers=ORIGIN, data={"value": "Bob@Other.example"})
    with ctx.db.session() as s:
        assert s.execute(select(Contact).where(Contact.email == "bob@other.example")).scalar_one().consent_status == "opted_out"


def test_a_malformed_inbound_token_is_refused_not_an_error(make_ctx):
    from fastapi.testclient import TestClient

    from aime.web.app import create_app

    c = TestClient(create_app(make_ctx(inbound_api_token="tok")))
    r = c.post("/api/inbound", json={"email": "a@b.example", "message": "hello there"},
               headers={"X-Inbound-Token": b"t\xf6k"})  # a byte outside ASCII, as a raw client may send
    assert r.status_code == 401


def test_no_interactive_api_docs(make_ctx):
    from fastapi.testclient import TestClient

    from aime.web.app import create_app

    c = TestClient(create_app(make_ctx()))
    assert c.get("/api/docs").status_code == 404 and c.get("/openapi.json").status_code in (200, 404)
