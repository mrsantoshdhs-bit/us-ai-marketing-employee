"""Addresses given to us (a manual entry or the owner's test contacts) are never described as published, and a
company still named after its domain takes its own name from its homepage title."""

from types import SimpleNamespace

import httpx

from aime.agents.crm import CRMManagementAgent
from aime.dedupe import name_from_title, normalize_name
from aime.models import Company
from aime.outreach import GIVEN_TO_US, Evidence, draft_outreach


def test_name_from_title_only_when_a_part_spells_the_domain():
    assert name_from_title("Digital Hub Solution | The best digital marketing agency", "digitalhubsolution.com") == "Digital Hub Solution"
    assert name_from_title("Digital Hub Solution | The best", "www.digitalhubsolution.net") == "Digital Hub Solution"
    assert name_from_title("Home - Acme Widgets", "acmewidgets.com") == "Acme Widgets"
    assert name_from_title("Acme: Home", "acme.com") == "Acme"
    assert name_from_title("Welcome to our site", "acme.com") is None  # nothing guessed
    assert name_from_title("Acme Widgets", "widgets-r-us.com") is None and name_from_title(None, "acme.com") is None


def test_the_footer_tells_the_truth_about_where_the_address_came_from(make_ctx):
    settings = make_ctx(company_postal_address="1 Sample St, Austin, TX 78701", smtp_from="hello@us.example").settings
    lead = SimpleNamespace(company=SimpleNamespace(name="Acme Widgets"), primary_service=None)
    evidence = [Evidence(fact="We build widgets for hospitals", url="https://acmewidgets.example/", kind="website",
                         source_text="We build widgets for hospitals.")]
    for source, published in (("website", True), ("sec_filing", True), ("manual", False), ("import", False)):
        contact = SimpleNamespace(first_name="Ana", role_category=None, source=source)
        _, body = draft_outreach(settings, 1, lead, contact, evidence)
        assert ("publishes this address" in body) is published and (GIVEN_TO_US in body) is not published
        assert "unsubscribe" in body and "1 Sample St" in body


def test_a_company_named_after_its_domain_takes_the_name_its_website_gives(make_ctx):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        titles = {"acmewidgets.example": "Acme Widgets | Hospital supplies", "other.example": "Other Corp | Home"}
        return httpx.Response(200, text=f"<title>{titles[request.url.host]}</title>", headers={"content-type": "text/html"})

    ctx = make_ctx(handler=handler)
    with ctx.db.session() as s:
        ids = []
        for name, domain in (("acmewidgets.example", "acmewidgets.example"), ("Other Corporation", "other.example")):
            c = Company(name=name, normalized_name=normalize_name(name), domain=domain, website=f"https://{domain}", source="manual")
            s.add(c)
            s.flush()
            ids.append(c.id)
    assert CRMManagementAgent(ctx).run({"operations": ["verify_websites"], "company_ids": ids}).ok
    with ctx.db.session() as s:
        renamed, kept = s.get(Company, ids[0]), s.get(Company, ids[1])
        assert renamed.name == "Acme Widgets" and "homepage title" in renamed.validation["name"]
        assert kept.name == "Other Corporation"  # a real name is never replaced
