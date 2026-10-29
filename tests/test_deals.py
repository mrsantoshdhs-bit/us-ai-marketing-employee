"""A deal's page: quotes entered by people (sent moves an early deal to proposal, accepted sets its value), documents
(allowed types only, size-limited, downloads only, and only for people who may see the deal), and pipeline stages
an administrator configures."""

import pytest
from sqlalchemy import select

from aime.models import Attachment, Company, Opportunity, Quote
from aime.stages import parse_stages, pipeline
from tests.test_dashboard_auth import ORIGIN
from tests.test_sales_team import client, org  # noqa: F401  (pytest fixture)


def deal(ctx, lead_id, owner="eva", stage="discovery"):
    with ctx.db.session() as s:
        co = s.execute(select(Company)).scalars().first()
        o = Opportunity(company_id=co.id, lead_id=lead_id, name="Acme CRM", stage=stage, owner=owner, probability=20)
        s.add(o)
        s.flush()
        return o.id


def test_quotes_are_entered_by_people_and_move_the_deal(org):  # noqa: F811
    ctx, leads = org
    oid = deal(ctx, leads["eva"])
    eva = client(ctx, "eva")
    r = eva.post(f"/opportunities/{oid}/quotes", headers=ORIGIN, data={"amount": "a lot"}, follow_redirects=False)
    assert "as a number" in r.headers["location"].replace("%20", " ")
    eva.post(f"/opportunities/{oid}/quotes", headers=ORIGIN,
             data={"amount": "$24,500", "currency": "usd", "valid_until": "2026-12-31", "terms": "50% upfront"})
    with ctx.db.session() as s:
        q = s.execute(select(Quote)).scalar_one()
        assert (q.amount, q.currency, q.status, q.created_by, q.terms) == (24500, "USD", "draft", "eva", "50% upfront")
        qid = q.id
    eva.post(f"/opportunities/{oid}/quotes/{qid}/send", headers=ORIGIN)
    with ctx.db.session() as s:
        o = s.get(Opportunity, oid)
        assert (s.get(Quote, qid).status, o.stage, o.probability) == ("sent", "proposal", 50)
    eva.post(f"/opportunities/{oid}/quotes/{qid}/accept", headers=ORIGIN)
    with ctx.db.session() as s:
        assert s.get(Opportunity, oid).est_value == 24500 and s.get(Quote, qid).decided_at is not None
    page = eva.get(f"/opportunities/{oid}").text
    assert "USD 24,500.00" in page and "discovery → <b>proposal</b>" in page and "quote #" in page


def test_documents_are_checked_stored_and_served_only_to_those_who_may_see_the_deal(org):  # noqa: F811
    ctx, leads = org
    oid = deal(ctx, leads["eva"])
    eva = client(ctx, "eva")
    pdf = b"%PDF-1.4 proposal for Acme"
    r = eva.post(f"/opportunities/{oid}/attachments", headers=ORIGIN,
                 files={"file": ("..\\..\\Acme proposal.pdf", pdf, "application/pdf")}, follow_redirects=False)
    assert "Uploaded Acme proposal.pdf" in r.headers["location"].replace("%20", " ")
    for name, body, why in (("run.exe", b"MZ", "allowed types"), ("page.html", b"<script>", "allowed types"),
                            ("big.pdf", b"x" * (10 * 1024 * 1024 + 1), "10 MB"), ("empty.pdf", b"", "empty")):
        r = eva.post(f"/opportunities/{oid}/attachments", headers=ORIGIN, files={"file": (name, body)}, follow_redirects=False)
        assert why in r.headers["location"].replace("%20", " "), name
    with ctx.db.session() as s:
        [a] = s.execute(select(Attachment)).scalars().all()
        assert (a.filename, a.size, a.content_type) == ("Acme proposal.pdf", len(pdf), "application/pdf")
        assert (ctx.settings.data_dir / "attachments" / a.stored_as).read_bytes() == pdf
        aid = a.id
    r = eva.get(f"/opportunities/{oid}/attachments/{aid}")
    assert r.content == pdf and r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client(ctx, "zoe").get(f"/opportunities/{oid}/attachments/{aid}").status_code == 404  # not her deal
    assert client(ctx, "zoe").get(f"/opportunities/{oid}").status_code == 404
    assert client(ctx, "ana").post(f"/opportunities/{oid}/attachments", headers=ORIGIN,
                                   files={"file": ("x.pdf", pdf)}).status_code == 403  # read-only


def test_an_administrator_configures_the_pipeline_stages(org):  # noqa: F811
    ctx, leads = org
    ctx.settings.business.setdefault("pipeline", {})["stages"] = "new:10, discovery:20, qualification:35, proposal:50"
    assert [n for n, _ in pipeline(ctx.settings)] == ["new", "discovery", "qualification", "proposal", "won", "lost"]
    oid = deal(ctx, leads["eva"], stage="negotiation")  # a stage no longer configured keeps its deals in view
    boss = client(ctx, "boss")
    board = boss.get("/pipeline").text
    assert "Qualification" in board and "Negotiation" in board
    boss.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "qualification"})
    with ctx.db.session() as s:
        assert (s.get(Opportunity, oid).stage, s.get(Opportunity, oid).probability) == ("qualification", 35)
    assert boss.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "imaginary"}).status_code == 400
    for bad, why in (("discovery:20", "needs new"), ("new:10, discovery:20, won:90", "won and lost"),
                     ("new:10, discovery:20, x1:5", "3-20 letters"), ("new:10, discovery:120", "0 to 99"),
                     ("new:10, discovery:20, new:5", "twice")):
        with pytest.raises(ValueError, match=why):
            parse_stages(bad)
