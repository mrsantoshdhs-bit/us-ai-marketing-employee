"""LinkedIn Lead Sync: people who submit a LinkedIn lead form or register for an event the company Page organizes
become inbound inquiries, then leads that go through the usual qualification, reply drafts and approvals. Off until
enabled, connected and a Page is chosen; only chosen Pages are read; test leads are skipped; each lead is recorded
once; profile links and other data the business does not need are dropped. A fake LinkedIn server answers every
call: nothing reaches LinkedIn, nothing is sent."""

import csv
import json
import os
from datetime import timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import automation as am
from aime.cli import cmd_erase, cmd_user
from aime.crm_ops import ERASED
from aime.integrations import linkedin_leads as ll
from aime.integrations.email_smtp import EmailSender
from aime.integrations.status import integration_statuses
from aime.models import (Alert, AuditEvent, Company, Contact, FollowUp, InboundEmail, InboundInquiry, Lead, OAuthToken,
                         Opportunity, Suppression)
from aime.secure_store import is_protected, unprotect
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.workflows import job_inbound
from tests.conftest import hn_handler
from tests.test_dashboard_auth import GOOD, Args, _login
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP

ORIGIN = {"origin": "http://testserver"}


class EraseArgs:
    def __init__(self, email, confirm):
        self.email, self.confirm = email, confirm

OURS, OTHER = "urn:li:organization:111", "urn:li:organization:222"
LEADS_APP = dict(linkedin_leads_client_id="leads-app", linkedin_leads_client_secret="x" * 12)
PAT = "pat.lee@brightpath-realty.example"
SENDING = dict(outbound_email_enabled=True, smtp_host="smtp.example", smtp_from="hello@us.example",
               company_postal_address="1 Sample St, Austin, TX 78701")


def ms(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


FORM = {"id": 3162, "name": "AI agents for US service businesses", "content": {
    "questions": [
        {"questionId": 1, "predefinedField": "FIRST_NAME", "question": {"localized": {"en_US": "First name"}}},
        {"questionId": 2, "predefinedField": "LAST_NAME", "question": {"localized": {"en_US": "Last name"}}},
        {"questionId": 3, "predefinedField": "WORK_EMAIL", "question": {"localized": {"en_US": "Work email"}}},
        {"questionId": 4, "predefinedField": "COMPANY_NAME", "question": {"localized": {"en_US": "Company"}}},
        {"questionId": 5, "predefinedField": "JOB_TITLE", "question": {"localized": {"en_US": "Job title"}}},
        {"questionId": 6, "predefinedField": "COUNTRY", "question": {"localized": {"en_US": "Country"}}},
        {"questionId": 7, "predefinedField": "STATE", "question": {"localized": {"en_US": "State"}}},
        {"questionId": 8, "predefinedField": "LINKEDIN_PROFILE_LINK", "question": {"localized": {"en_US": "Profile"}}},
        {"questionId": 9, "predefinedField": "COMPANY_SIZE", "question": {"localized": {"en_US": "Company size"}}},
        {"questionId": 10, "question": {"localized": {"en_US": "What would you like help with?"}}},
    ],
    "legalInfo": {"consents": [{"id": 4, "consent": {"localized": {"en_US": "Our company may contact me by email"}}}]}}}


def lead(lead_id, email=PAT, test=False, owner=OURS, lead_type="EVENT", days_ago=1, consent=True):
    answers = [(1, "Pat"), (2, "Lee"), (3, email), (4, "Brightpath Realty Group"), (5, "Operations Director"),
               (6, "United States"), (7, "TX"), (8, "https://www.linkedin.com/in/pat-lee-example"), (9, "11-50"),
               (10, "A custom CRM for our 40 agents, to replace spreadsheets")]
    return {"id": lead_id, "owner": {"organization": owner}, "leadType": lead_type, "testLead": test,
            "submittedAt": ms(utcnow() - timedelta(days=days_ago)),
            "versionedLeadGenFormUrn": "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:3162,1)",
            "formResponse": {"answers": [{"questionId": q, "answerDetails": {"textQuestionAnswer": {"answer": a}}}
                                         for q, a in answers],
                             "consentResponses": [{"consentId": 4, "accepted": consent}]}}


class FakeLinkedIn:
    """The token endpoint, organizationAcls, organizations, adAccountUsers, leadForms and leadFormResponses."""

    def __init__(self, leads=None, refresh=False):
        self.leads = list(leads or [])
        self.refresh = refresh
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_responses = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host not in ("www.linkedin.com", "api.linkedin.com"):
            return hn_handler(request)  # the company websites the CRM verifies
        path, query = request.url.path, {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append((request.method, path, query))
        if path == "/oauth/v2/accessToken":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("code") == "bad" or form.get("refresh_token") == "dead":
                return httpx.Response(400, json={"error": "invalid_grant"})
            body = {"access_token": f"tok-{len(self.calls)}", "expires_in": 5184000,
                    "scope": "r_marketing_leadgen_automation,r_organization_admin"}
            if self.refresh:
                body |= {"refresh_token": "ref-1", "refresh_token_expires_in": 31536000}
            return httpx.Response(200, json=body)
        if path == "/rest/organizationAcls":
            return httpx.Response(200, json={"elements": [
                {"organization": OURS, "role": "ADMINISTRATOR", "state": "APPROVED"},
                {"organization": OTHER, "role": "CONTENT_ADMINISTRATOR", "state": "APPROVED"},
                {"organization": "urn:li:organization:333", "role": "DIRECT_SPONSORED_CONTENT_POSTER"}]})
        if path.startswith("/rest/organizations/"):
            return httpx.Response(200, json={"localizedName": {"111": "Our Company", "222": "Another Business"}.get(
                path.rsplit("/", 1)[1], "?")})
        if path == "/rest/adAccountUsers":
            return httpx.Response(403, json={"message": "Not enough permissions"})
        if path == "/rest/leadForms/3162":
            return httpx.Response(200, json=FORM)
        if path == "/rest/leadFormResponses":
            if self.fail_responses:
                return httpx.Response(self.fail_responses, json={"message": "no"})
            owner = query["owner"].split(":", 1)[1].rstrip(")")
            lead_type = query["leadType"].split(":", 1)[1].rstrip(")")
            start, end = (int(x.split(":")[1]) for x in query["submittedAtTimeRange"].strip("()").split(","))
            test_only = query["limitedToTestLeads"] == "true"
            found = [x for x in self.leads if x["owner"].get("organization", x["owner"].get("sponsoredAccount")) == owner
                     and x["leadType"] == lead_type and start <= x["submittedAt"] <= end
                     and (x["testLead"] or not test_only)]
            return httpx.Response(200, json={"elements": found, "paging": {"start": 0, "count": 100}})
        return httpx.Response(404, json={"message": "not mocked"})

    def asked(self, path):
        return [q for m, p, q in self.calls if p == path]


def leads_ctx(make_ctx, fake, enabled=True, connect=True, owners=(OURS,), **overrides):
    ctx = make_ctx(handler=fake, **LEADS_APP, linkedin_leads_enabled=enabled, **overrides)
    if connect:
        client = TestClient(create_app(ctx))
        go = client.get("/integrations/linkedin-leads/connect", follow_redirects=False)
        assert go.status_code == 302 and go.headers["location"].startswith("https://www.linkedin.com/oauth/v2/authorization")
        state = parse_qs(urlparse(go.headers["location"]).query)["state"][0]
        assert "r_marketing_leadgen_automation" in parse_qs(urlparse(go.headers["location"]).query)["scope"][0]
        done = client.get(f"/integrations/linkedin-leads/callback?code=good&state={state}", follow_redirects=False)
        assert done.status_code == 303 and "connected" in done.headers["location"]
        if owners:
            client.post("/integrations/linkedin-leads/owners", data={"owner": list(owners)}, headers=ORIGIN)
    return ctx


# --------------------------------------------------------------------------- switches and the connection

def test_nothing_is_read_from_linkedin_until_enabled_connected_and_a_page_is_chosen(make_ctx):
    fake = FakeLinkedIn([lead("a1")])
    ctx = leads_ctx(make_ctx, fake, enabled=False)
    calls = len(fake.calls)
    job_inbound(ctx)
    assert len(fake.calls) == calls  # switch off: not a single call
    ctx2 = leads_ctx(make_ctx, FakeLinkedIn(), connect=False)
    job_inbound(ctx2)
    with ctx2.db.session() as s:
        assert s.execute(select(InboundInquiry)).first() is None
        assert "not synced: Not connected" in s.execute(select(Alert).where(Alert.source == "crm")).scalars().first().message
    fake3 = FakeLinkedIn([lead("a1")])
    ctx3 = leads_ctx(make_ctx, fake3, owners=())
    job_inbound(ctx3)
    assert fake3.asked("/rest/leadFormResponses") == []  # connected, but no Page chosen yet


def test_connecting_stores_an_encrypted_token_and_lists_only_pages_that_may_read_leads(make_ctx):
    ctx = leads_ctx(make_ctx, FakeLinkedIn(), owners=())
    with ctx.db.session() as s:
        tok = s.execute(select(OAuthToken).where(OAuthToken.provider == ll.TOKEN_KEY)).scalar_one()
        if os.name == "nt":  # DPAPI is Windows-only; elsewhere secure_store keeps its marked "plain:" fallback
            assert is_protected(tok.access_token) and "tok-" not in tok.access_token
        assert unprotect(tok.access_token).startswith("tok-")
        found = ll.status(ctx.settings, s).found
        assert [(f["urn"], f["name"]) for f in found] == [(OURS, "Our Company"), (OTHER, "Another Business")]
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "account_connected")).scalar_one().target == ll.TOKEN_KEY
    page = TestClient(create_app(ctx)).get("/integrations").text
    assert "Another Business" in page and "x" * 12 not in page  # the client secret is never shown
    with ctx.db.session() as s:  # a Page LinkedIn did not list cannot be chosen
        assert [o["urn"] for o in ll.choose_owners(s, [OURS, "urn:li:organization:999"], "boss")] == [OURS]


def test_a_tampered_or_stale_sign_in_is_refused(make_ctx):
    ctx = leads_ctx(make_ctx, FakeLinkedIn(), connect=False)
    client = TestClient(create_app(ctx))
    client.get("/integrations/linkedin-leads/connect", follow_redirects=False)
    r = client.get("/integrations/linkedin-leads/callback?code=good&state=forged", follow_redirects=False)
    assert "expired or did not match" in unquote(r.headers["location"])
    with ctx.db.session() as s:
        assert ll.get_token(s) is None


# --------------------------------------------------------------------------- leads into the CRM

def test_event_registrations_become_consented_leads_once_with_only_the_data_we_need(make_ctx):
    fake = FakeLinkedIn([lead("a1"), lead("t1", email="tester@brightpath-realty.example", test=True),
                         lead("o1", email="someone@other.example", owner=OTHER)])
    ctx = leads_ctx(make_ctx, fake)
    out = job_inbound(ctx)
    asked = fake.asked("/rest/leadFormResponses")
    assert {q["owner"] for q in asked} == {f"(organization:{OURS})"}  # the other business's Page is never read
    assert {q["leadType"] for q in asked} == {"(leadType:EVENT)", "(leadType:COMPANY)", "(leadType:ORGANIZATION_PRODUCT)"}
    with ctx.db.session() as s:
        [inq] = s.execute(select(InboundInquiry)).scalars().all()  # the test lead was skipped
        assert inq.channel == "linkedin_form" and inq.status == "processed"
        assert "linkedin.com/in/" not in json.dumps(inq.payload)  # the profile link is dropped
        assert "A custom CRM for our 40 agents" in inq.payload["message"] and "Company size: 11-50" in inq.payload["message"]
        c = s.execute(select(Contact).where(Contact.email == PAT)).scalar_one()
        assert (c.first_name, c.title, c.consent_status) == ("Pat", "Operations Director", "opt_in")
        assert "LinkedIn event: AI agents for US service businesses" in c.consent_source
        lead_row = s.execute(select(Lead)).scalar_one()
        assert lead_row.source == "inbound" and lead_row.company.state == "TX"
        last = ll.status(ctx.settings, s).last_sync
        assert last["fetched"] == 2 and last["recorded"] == 1 and last["test_skipped"] == 1
    assert out.steps[0]["output"]["linkedin_leads_recorded"] == 1
    job_inbound(ctx)  # the next run overlaps the last one and records nothing twice
    with ctx.db.session() as s:
        assert len(s.execute(select(InboundInquiry)).scalars().all()) == 1
    second = fake.asked("/rest/leadFormResponses")[-1]["submittedAtTimeRange"]
    first_end = int(asked[0]["submittedAtTimeRange"].strip("()").split(",")[1].split(":")[1])
    assert int(second.strip("()").split(",")[0].split(":")[1]) == first_end - 3600 * 1000


def test_test_only_mode_fetches_test_leads_as_sample_data(make_ctx):
    fake = FakeLinkedIn([lead("a1"), lead("t1", email="tester@brightpath-realty.example", test=True)])
    ctx = leads_ctx(make_ctx, fake, linkedin_leads_test_only=True)
    job_inbound(ctx)
    assert {q["limitedToTestLeads"] for q in fake.asked("/rest/leadFormResponses")} == {"true"}
    with ctx.db.session() as s:
        [inq] = s.execute(select(InboundInquiry)).scalars().all()
        assert inq.payload["email"] == "tester@brightpath-realty.example" and inq.payload["is_sample"] is True


def test_the_first_email_thanks_the_registrant_and_waits_for_approval(make_ctx):
    ctx = leads_ctx(make_ctx, FakeLinkedIn([lead("a1")]))
    job_inbound(ctx)
    with ctx.db.session() as s:
        [draft] = s.execute(select(FollowUp).where(FollowUp.channel == "email")).scalars().all()
        assert draft.subject == "Thanks for registering: AI agents for US service businesses"
        assert "Thank you for registering for AI agents for US service businesses on LinkedIn." in draft.body
        assert draft.status == "awaiting_approval" and draft.kind is None and "unsubscribe" in draft.body.lower()


def test_an_asked_us_campaign_answers_linkedin_leads_and_no_other_rule_does(make_ctx):
    ctx = leads_ctx(make_ctx, FakeLinkedIn([lead("a1")]), **SENDING)
    job_inbound(ctx)
    ctx.settings.require_human_approval = False
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    FakeSMTP.sent = []
    with ctx.db.session() as s:
        assert s.execute(select(Lead)).scalar_one().status == "qualified"
        cold = am.create_rule_authorization(s, "boss", "Cold outreach", "B", "role_or_published", False, 5, 20, 7)
        cold.starts_at, cold.expires_at = TUESDAY_11AM_ET - timedelta(days=1), TUESDAY_11AM_ET + timedelta(days=6)
        am.change_authorization(s, cold, "activate", "boss")
    from aime.agents import SalesWorkflowAgent
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert FakeSMTP.sent == []  # an outreach rule never answers people who wrote to us
    with ctx.db.session() as s:
        warm = am.create_rule_authorization(s, "boss", "LinkedIn leads", "B", "asked_us", False, 5, 20, 7)
        assert warm.kinds == ["inquiry"] and "asked us to contact them" in am.describe_rule(warm.rule)
        warm.starts_at, warm.expires_at = TUESDAY_11AM_ET - timedelta(days=1), TUESDAY_11AM_ET + timedelta(days=6)
        am.change_authorization(s, warm, "activate", "boss")
        wid = warm.id
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert len(FakeSMTP.sent) == 1 and FakeSMTP.sent[0]["To"] == PAT
    with ctx.db.session() as s:
        f = s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalar_one()
        assert f.approved_by == f"campaign #{wid}" and f.subject.startswith("Thanks for registering")
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "auto_approved")).scalar_one().detail.startswith(
            "inquiry email to")


def test_an_asked_us_rule_never_covers_cold_outreach_or_researched_contacts(make_ctx):
    from tests.test_automation import drafted

    ctx, _sender, fid = drafted(make_ctx)
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        rule = {"min_grade": "B", "addresses": "asked_us", "verified_website": False}
        assert "not outreach" in am.rule_problem(s, rule, f, am.recipient(f))
        f.kind = None  # even as a reply, the researched (not inbound) contact did not ask us
        assert am.rule_problem(s, rule, f, am.recipient(f)) == "the person did not ask us to contact them"


# --------------------------------------------------------------------------- failures

def test_an_expiring_connection_is_renewed_and_a_refused_one_raises_an_alert(make_ctx):
    fake = FakeLinkedIn([lead("a1")], refresh=True)
    ctx = leads_ctx(make_ctx, fake)
    with ctx.db.session() as s:
        ll.get_token(s).expires_at = utcnow() + timedelta(hours=2)
    job_inbound(ctx)
    token_calls = [p for _m, p, _q in fake.calls if p == "/oauth/v2/accessToken"]
    assert len(token_calls) == 2 and len(fake.asked("/rest/leadFormResponses")) == 3  # sign-in, then the renewal
    with ctx.db.session() as s:
        assert ll.get_token(s).expires_at > utcnow() + timedelta(days=50)  # renewed
    fake.fail_responses = 401
    job_inbound(ctx)
    with ctx.db.session() as s:
        alert = s.execute(select(Alert).where(Alert.source == "crm", Alert.resolved_at.is_(None))).scalars().all()[-1]
        assert "connect it again" in alert.message


def test_an_expired_connection_without_renewal_is_reported_not_retried(make_ctx):
    fake = FakeLinkedIn([lead("a1")])
    ctx = leads_ctx(make_ctx, fake)
    with ctx.db.session() as s:
        ll.get_token(s).expires_at = utcnow() - timedelta(minutes=1)
        assert "expired" in ll.status(ctx.settings, s).reason
    before = len(fake.asked("/rest/leadFormResponses"))
    job_inbound(ctx)
    assert len(fake.asked("/rest/leadFormResponses")) == before
    with ctx.db.session() as s:
        statuses = {r["name"]: r for r in integration_statuses(ctx, s, include_scheduler=False)}
    assert statuses["LinkedIn leads (Lead Sync API)"]["state"] == "BLOCKED"


# --------------------------------------------------------------------------- downloads, handover, erasure, access

def test_a_linkedin_download_in_the_inbox_folder_is_recognized_and_recorded_once(make_ctx):
    ctx = make_ctx()
    ctx.settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    rows = [{"Lead ID": "77", "First Name": "Pat", "Last Name": "Lee", "Email": PAT, "Company Name": "Brightpath Realty Group",
             "Job Title": "Operations Director", "Country/Region": "United States"}]
    for name in ("event-registrants.csv", "same-leads-again.csv"):
        with (ctx.settings.inbox_dir / name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        job_inbound(ctx)
    with ctx.db.session() as s:
        [inq] = s.execute(select(InboundInquiry)).scalars().all()
        assert inq.status == "processed" and inq.payload["external_id"] == "linkedin:77"
        assert inq.payload["company"] == "Brightpath Realty Group" and inq.payload["title"] == "Operations Director"
        assert s.execute(select(Contact).where(Contact.email == PAT)).scalar_one().consent_status == "inquiry"


def test_two_sales_managers_share_new_opportunities(make_ctx):
    from aime.handover import handover, next_owner

    ctx = make_ctx()
    ctx.settings.business.setdefault("handover", {})["sales_manager"] = ["ann", "bob"]
    with ctx.db.session() as s:
        company = Company(name="Brightpath Realty Group", normalized_name="brightpath realty group", source="inbound")
        s.add(company)
        s.flush()
        owners = []
        for i in range(4):
            opp = Opportunity(company_id=company.id, name=f"Opportunity {i}", stage="new", probability=10)
            s.add(opp)
            s.flush()
            handover(s, ctx.settings, opp, "interested reply", "test")
            owners.append(opp.owner)
        assert owners == ["ann", "bob", "ann", "bob"]
        s.execute(select(Opportunity).where(Opportunity.owner == "ann")).scalars().first().stage = "won"
        s.flush()
        assert next_owner(s, ctx.settings) == "ann"  # a closed deal frees a place


def test_erasing_a_person_removes_their_data_and_keeps_them_off_every_list(make_ctx, capsys):
    ctx = leads_ctx(make_ctx, FakeLinkedIn([lead("a1")]))
    job_inbound(ctx)
    with ctx.db.session() as s:
        s.add(InboundEmail(from_email=PAT, subject="Re: thanks", body_text="Pat Lee here", new_text="Pat Lee here",
                           message_id="<x@mail.example>", received_at=utcnow(), category="interested"))
    assert cmd_erase(EraseArgs(PAT, confirm=False), ctx) == 2  # nothing happens without --confirm
    assert cmd_erase(EraseArgs(PAT, confirm=True), ctx) == 0
    with ctx.db.session() as s:
        assert s.execute(select(Contact).where(Contact.email == PAT)).first() is None
        c = s.execute(select(Contact).where(Contact.consent_source == ERASED)).scalar_one()
        assert (c.first_name, c.title, c.consent_status) == (None, None, "opted_out")
        assert s.execute(select(InboundInquiry)).scalar_one().payload == {"erased": True}
        assert s.execute(select(InboundEmail)).scalar_one().body_text == ERASED
        draft = s.execute(select(FollowUp).where(FollowUp.channel == "email")).scalar_one()
        assert draft.status == "cancelled" and draft.body == ERASED
        assert s.execute(select(Suppression).where(Suppression.value == PAT)).first()
        event = s.execute(select(AuditEvent).where(AuditEvent.action == "person_erased")).scalar_one()
        assert PAT not in event.target and PAT not in (event.detail or "")


def test_only_a_super_admin_connects_linkedin_leads(make_ctx):
    ctx = make_ctx(dashboard_auth=True, **LEADS_APP)
    for name, role in (("owner", "admin"), ("boss", "super_admin")):
        assert cmd_user(Args("create", name, role=role), ctx, prompt_fn=lambda _p: GOOD) == 0
    owner = TestClient(create_app(ctx))
    _login(owner, username="owner")
    assert owner.get("/integrations/linkedin-leads/connect", follow_redirects=False).status_code == 403
    page = owner.get("/integrations").text
    assert "LinkedIn leads (Lead Sync API)" in page and 'href="/integrations/linkedin-leads/connect"' not in page
    boss = TestClient(create_app(ctx))
    _login(boss, username="boss")
    assert 'href="/integrations/linkedin-leads/connect"' in boss.get("/integrations").text


def test_without_the_form_definition_the_address_is_still_found_and_the_profile_link_still_dropped():
    p = ll.payload_from(lead("z9"), None)
    assert p["external_id"] == "linkedin:z9" and p["message"].startswith("LinkedIn event: a lead form")
    assert p["email"] == PAT and "Answer 10: A custom CRM" in p["message"]
    assert "linkedin.com/in/" not in json.dumps(p)


# --------------------------------------------------------------------------- everyone who asked gets an answer

UNLISTED = "pat.lee@unlisted-co.example"  # a company website the checker cannot reach, so the score stays below B


def test_a_registrant_below_grade_b_still_gets_an_answer_that_waits_for_a_person(make_ctx):
    from aime.agents import SalesWorkflowAgent
    from aime.crm_ops import log_activity

    ctx = leads_ctx(make_ctx, FakeLinkedIn([lead("c1", email=UNLISTED)]), **SENDING)
    job_inbound(ctx)
    with ctx.db.session() as s:
        parked = s.execute(select(Lead)).scalar_one()
        assert (parked.status, parked.grade) == ("nurture", "C")
        [draft] = s.execute(select(FollowUp).where(FollowUp.channel == "email")).scalars().all()
        assert draft.status == "awaiting_approval" and draft.subject.startswith("Thanks for registering")
        lead_id = parked.id
    ctx.settings.require_human_approval = False
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    FakeSMTP.sent = []
    with ctx.db.session() as s:  # an "asked us" campaign covers grade B and up: this answer waits for a person
        warm = am.create_rule_authorization(s, "boss", "LinkedIn leads", "B", "asked_us", False, 5, 20, 7)
        warm.starts_at, warm.expires_at = TUESDAY_11AM_ET - timedelta(days=1), TUESDAY_11AM_ET + timedelta(days=6)
        am.change_authorization(s, warm, "activate", "boss")
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert FakeSMTP.sent == []
    with ctx.db.session() as s:
        f = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id)).scalar_one()
        assert f.status == "awaiting_approval"
        f.status, f.approved_by, f.approved_at = "approved", "boss", utcnow()
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert [m["To"] for m in FakeSMTP.sent] == [UNLISTED]
    with ctx.db.session() as s:  # no answer: the day-3 follow-up names the event, and also waits for a person
        first = s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalar_one()
        first.sent_at = TUESDAY_11AM_ET - timedelta(days=4)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    with ctx.db.session() as s:
        step2 = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.step == 2)).scalar_one()
        assert step2.subject == "Following up: AI agents for US service businesses"
        assert "follow up on your registration for AI agents for US service businesses" in step2.body
        assert step2.status == "awaiting_approval" and len(FakeSMTP.sent) == 1
        step2.status = "cancelled"
        # a person parks the lead: nothing more is drafted, and an approval given earlier is not sent
        log_activity(s, "Lead nurture → nurture (moved by a person)", "boss", type_="status_change", lead_id=lead_id)
        s.add(FollowUp(lead_id=lead_id, contact_id=first.contact_id, company_id=first.company_id, channel="email",
                       step=3, due_at=TUESDAY_11AM_ET, status="approved", approved_by="boss", approved_at=utcnow(),
                       subject="Closing the loop on your inquiry", body=first.body))
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    with ctx.db.session() as s:
        last = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.step == 3)).scalar_one()
        assert last.status == "cancelled" and last.blocked_reason == "Lead is nurture"
        assert s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id)).scalars().all()[-1].id == last.id
    assert len(FakeSMTP.sent) == 1


def test_only_a_lead_parked_by_its_score_alone_is_still_answered(make_ctx):
    from aime.agents import SalesWorkflowAgent
    from aime.crm_ops import log_activity

    ctx = leads_ctx(make_ctx, FakeLinkedIn([lead("c2", email=UNLISTED)]))
    job_inbound(ctx)
    with ctx.db.session() as s:
        s.execute(select(FollowUp)).scalar_one().status = "cancelled"
        parked = s.execute(select(Lead)).scalar_one()
        agent = SalesWorkflowAgent(ctx)
        assert agent._may_answer(s, parked)
        parked.source = "research"
        assert not agent._may_answer(s, parked)
        parked.source, parked.status = "inbound", "disqualified"
        assert not agent._may_answer(s, parked)
        # qualified later, then a "not interested" reply parks it (that move leaves no status record of its own)
        log_activity(s, "Lead nurture → qualified (score 60, grade B)", "qualification", type_="status_change",
                     lead_id=parked.id)
        parked.status = "nurture"
        assert not agent._may_answer(s, parked)


def test_service_names_keep_their_acronyms_in_replies(tmp_path):
    from aime.config import Settings
    from aime.email_templates import draft_reply

    settings = Settings.load(env_file=tmp_path / "no.env", data_dir=tmp_path / "data", data_role="test",
                             company_postal_address="1 Sample St, Austin, TX 78701")
    settings.business["company"]["name"] = "Example Co"
    assert draft_reply(settings, 2, "Sam", "ai_agents", None)[0] == "Following up on your AI agent development inquiry"
    assert "about custom CRM development." in draft_reply(settings, 2, "Sam", "custom_crm", None)[1]
    assert draft_reply(settings, 1, "Sam", "saas_dev", None)[0] == "Re: your inquiry about SaaS development"
    assert draft_reply(settings, 2, "Sam", None, None)[0] == "Following up on your inquiry"
    form_reply = draft_reply(settings, 1, "Sam", None, None, "LinkedIn lead form: Contact us")
    assert form_reply[0] == "Thanks for your interest in Example Co"
