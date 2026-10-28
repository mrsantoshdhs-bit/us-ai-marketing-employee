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


# --------------------------------------------------------------------------- the controlled send path for test contacts

TESTER = "tester@given.example"
ORIGIN = {"origin": "http://testserver"}


def given_draft(make_ctx, **overrides):
    """A company flagged as a competitor (lead disqualified), a test contact the owner gave (not published), and an
    outreach draft to it whose quality checks fail on that address."""
    from datetime import timedelta

    from aime.integrations.email_smtp import EmailSender
    from aime.models import Contact, FollowUp, Lead
    from aime.qc.email_checks import run_email_checks
    from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
    from tests.test_outreach import SENDING

    ctx = make_ctx(**{**SENDING, **overrides})
    FakeSMTP.sent = []
    with ctx.db.session() as s:
        company = Company(name="Given Co", normalized_name="given co", domain="given.example", website="https://given.example",
                          source="manual", is_competitor=True)
        s.add(company)
        s.flush()
        contact = Contact(company_id=company.id, first_name="Tess", email=TESTER, source="manual", email_status="user_provided")
        s.add(contact)
        s.flush()
        lead = Lead(company_id=company.id, contact_id=contact.id, source="manual", status="disqualified",
                    disqualify_reason="Company appears to sell similar services (competitor)")
        s.add(lead)
        s.flush()
        evidence = [Evidence(fact="We help clinics", url="https://given.example/", kind="website", source_text="We help clinics.")]
        subject, body = draft_outreach(ctx.settings, 1, lead, contact, evidence)
        qc = run_email_checks(subject, body, [e.as_dict() for e in evidence], contact, False, step=1)
        assert qc["status"] == "failed" and GIVEN_TO_US in body
        f = FollowUp(lead_id=lead.id, contact_id=contact.id, company_id=company.id, channel="email", kind="outreach", step=1,
                     due_at=TUESDAY_11AM_ET - timedelta(hours=1), status="awaiting_approval", subject=subject, body=body,
                     to_email=TESTER, evidence=[e.as_dict() for e in evidence], quality={"qc": qc})
        s.add(f)
        s.flush()
        fid = f.id
    return ctx, EmailSender(ctx.settings, smtp_factory=FakeSMTP), fid


def authorize_given(ctx, recipients=TESTER, activate=True):
    from datetime import timedelta

    from aime import automation as am
    from tests.test_inbound_sales import TUESDAY_11AM_ET
    with ctx.db.session() as s:
        a = am.create_authorization(s, "admin", "Controlled test, given contacts", recipients, ["outreach"], 2, 2, 3)
        a.starts_at, a.expires_at = TUESDAY_11AM_ET - timedelta(days=1), TUESDAY_11AM_ET + timedelta(days=2)
        if activate:
            am.change_authorization(s, a, "activate", "admin")
        return a.id


def send_run(ctx, sender):
    from aime.agents.sales_workflow import SalesWorkflowAgent
    from tests.test_inbound_sales import TUESDAY_11AM_ET
    return SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})


def draft_state(ctx, fid):
    from aime.models import FollowUp
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        return f.status, f.blocked_reason, f.authorization_id, ((f.quality or {}).get("qc") or {}).get("status")


def test_a_given_test_contact_goes_out_only_on_an_authorization_naming_it(make_ctx):
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from aime import automation as am
    from aime.models import AuditEvent, CampaignAuthorization
    from aime.web.app import create_app
    from tests.test_inbound_sales import FakeSMTP

    ctx, sender, fid = given_draft(make_ctx)
    client = TestClient(create_app(ctx))
    assert "test contact: goes out only with your approval" in client.get("/followups").text
    r = client.post(f"/followups/{fid}/approve", headers=ORIGIN, follow_redirects=False)
    assert "Quality%20checks%20failed" in r.headers["location"]  # the failure stays visible: a person ticks it
    client.post(f"/followups/{fid}/approve", data={"qc_override": "yes"}, headers=ORIGIN)
    send_run(ctx, sender)
    status, why, _, qc = draft_state(ctx, fid)
    assert status == "approved" and "no active campaign authorization names this exact address" in why and FakeSMTP.sent == []
    authorize_given(ctx, recipients="someone.else@given.example")  # another address: not this one
    aid = authorize_given(ctx, activate=False)  # a draft authorization lets nothing out
    send_run(ctx, sender)
    assert draft_state(ctx, fid)[0] == "approved" and FakeSMTP.sent == []
    with ctx.db.session() as s:
        am.change_authorization(s, s.get(CampaignAuthorization, aid), "activate", "admin")
    send_run(ctx, sender)
    status, _, used, qc = draft_state(ctx, fid)
    assert status == "sent" and used == aid and qc == "failed" and [m["To"] for m in FakeSMTP.sent] == [TESTER]
    with ctx.db.session() as s:
        [e] = s.execute(select(AuditEvent).where(AuditEvent.action == "test_contact_send")).scalars().all()
        assert TESTER in e.detail and "quality checks failed" in e.detail and f"campaign #{aid}" in e.detail


def test_campaigns_never_approve_a_given_test_contact_on_their_own(make_ctx):
    ctx, sender, fid = given_draft(make_ctx, require_human_approval=False)
    authorize_given(ctx)
    send_run(ctx, sender)
    assert draft_state(ctx, fid)[0] == "awaiting_approval"


def test_opt_outs_and_paused_authorizations_still_stop_a_test_send(make_ctx):
    from fastapi.testclient import TestClient

    from aime import automation as am
    from aime.crm_ops import suppress
    from aime.models import CampaignAuthorization
    from aime.web.app import create_app
    from tests.test_inbound_sales import FakeSMTP

    ctx, sender, fid = given_draft(make_ctx)
    TestClient(create_app(ctx)).post(f"/followups/{fid}/approve", data={"qc_override": "yes"}, headers=ORIGIN)
    aid = authorize_given(ctx)
    with ctx.db.session() as s:
        am.change_authorization(s, s.get(CampaignAuthorization, aid), "pause", "admin")
    send_run(ctx, sender)
    assert draft_state(ctx, fid)[0] == "approved" and FakeSMTP.sent == []
    with ctx.db.session() as s:
        am.change_authorization(s, s.get(CampaignAuthorization, aid), "activate", "admin")
        suppress(s, TESTER, "Asked not to be contacted")
    assert draft_state(ctx, fid)[0] == "cancelled"  # an opt-out cancels what was waiting for that address
    send_run(ctx, sender)
    assert draft_state(ctx, fid)[0] == "cancelled" and FakeSMTP.sent == []
