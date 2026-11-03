"""Contact-source adapters (Option B, Phase 4): the declared contract every adapter meets, blocked adapters refusing, the
budget guard and its audited ledger, UK Companies House officers from mocked API answers (paging, 429, errors), provenance
on every new contact from each path with lazy legacy backfill and the additive migration. Nothing here
touches the network: every HTTP answer is an httpx.MockTransport."""

from __future__ import annotations

import base64

import httpx
import pytest
from sqlalchemy import create_engine, func, inspect, select, text

from aime import countries, settings_store as ss
from aime.agents.company_research import CompanyResearchAgent
from aime.company_research import save_contact
from aime.contact_sources import budget, registry
from aime.contact_sources.base import FIELDS, NEEDS_APPROVAL, NEEDS_CREDENTIALS, READY, REQUIRED_METADATA, STATUSES, AdapterRefused, RateLimited, SourceError
from aime.contact_sources.companies_house import CompaniesHouseOfficers, company_number, parse_officers
from aime.contact_sources.paid import PaidContactProvider, PaidEmailVerifier, PaidStub
from aime.contact_validation import assign_published_emails
from aime.crm_ops import upsert_contact
from aime.migrations import migrate
from aime.models import AuditEvent, Company, Contact, ContactSourceUsage, Lead
from aime.people import PersonFinding
from aime import provenance

CH_HOST = "api.company-information.service.gov.uk"
NUMBER = "01234567"
KEY = "ch-test-" + "key-not-real"  # synthetic
OFFICERS_P1 = {"total_results": 5, "items_per_page": 4, "start_index": 0, "items": [
    {"name": "SMITH, John Andrew", "officer_role": "director", "appointed_on": "2015-03-01",
     "date_of_birth": {"month": 4, "year": 1970}, "nationality": "British", "occupation": "Engineer",
     "address": {"premises": "1", "address_line_1": "High Street", "locality": "London"}},
    {"name": "DOE, Jane", "officer_role": "secretary", "appointed_on": "2018-01-10"},
    {"name": "BROWN, Old", "officer_role": "director", "appointed_on": "2001-01-01", "resigned_on": "2010-05-05"},
    {"name": "ACME NOMINEES LIMITED", "officer_role": "corporate-director", "appointed_on": "2019-01-01"},
]}
OFFICERS_P2 = {"total_results": 5, "items_per_page": 4, "start_index": 4, "items": [
    {"name": "O'NEIL, Sarah", "officer_role": "llp-designated-member", "appointed_on": "2020-02-02"}]}


def ch_handler(log: list, status: int = 200, headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if request.url.host != CH_HOST:
            return httpx.Response(404)
        if status != 200:
            return httpx.Response(status, headers=headers or {}, json={"error": "x"})
        if request.url.path == f"/company/{NUMBER}/officers":
            start = int(request.url.params.get("start_index", "0"))
            return httpx.Response(200, json=OFFICERS_P2 if start >= 4 else OFFICERS_P1)
        return httpx.Response(404, json={"errors": [{"error": "company-profile-not-found"}]})
    return handler


def audits(ctx, action=None):
    with ctx.db.session() as s:
        q = select(AuditEvent).order_by(AuditEvent.id)
        if action:
            q = q.where(AuditEvent.action == action)
        rows = s.execute(q).scalars().all()
        s.expunge_all()
        return rows


def enable_gb(ctx):
    with ctx.db.session() as s:
        countries.record_legal_review(s, ctx.settings, "GB", "Owner after counsel review", "owner", "UK GDPR / PECR reviewed")
    data = ss.get(ctx.settings, "countries")
    for c in data["countries"]:
        if c["code"] == "GB":
            c.update(enabled=True, timezone="Europe/London")
    with ctx.db.session() as s:
        ss.save(s, ctx.settings, "countries", data, "owner")


def set_adapter(ctx, name, **values):
    data = ss.get(ctx.settings, "contact_sources")
    for a in data["adapters"]:
        if a["name"] == name:
            a.update(values)
    with ctx.db.session() as s:
        ss.save(s, ctx.settings, "contact_sources", data, "owner")


def uk_company(s, number=NUMBER, **kw) -> Company:
    co = Company(name="Thames Freight Ltd", normalized_name="thames freight ltd", country="GB", source="test",
                 source_record_id=f"ch:{number}", **kw)
    s.add(co)
    s.flush()
    return co


@pytest.fixture
def ch_ctx(make_ctx):
    log: list = []
    ctx = make_ctx(handler=ch_handler(log), companies_house_api_key=KEY)
    ctx.requests = log
    enable_gb(ctx)
    set_adapter(ctx, "companies_house", enabled=True, countries=["GB"])
    return ctx


# --------------------------------------------------------------------------- the contract

def test_every_adapter_declares_the_contract(ctx):
    assert {"company_website", "sec_officers", "csv_import", "manual_entry", "inbound_forms", "linkedin_lead_sync",
            "companies_house", "paid_contact_provider", "paid_email_verifier"} <= set(registry.ADAPTER_CLASSES)
    for name, cls in registry.ADAPTER_CLASSES.items():
        meta = cls.metadata()
        assert set(REQUIRED_METADATA) <= set(meta), name
        assert meta["name"] == name and meta["title"] and meta["description"], name
        assert isinstance(cls.countries, tuple) and all(c == "*" or len(c) == 2 for c in cls.countries), name
        assert cls.fields and set(cls.fields) <= set(FIELDS), name
        assert cls.licence.strip() and cls.lawful_basis.strip() and cls.terms_url.strip() and cls.rate_limit.strip(), name
        assert isinstance(cls.cost_per_call_usd, float) and cls.cost_per_call_usd >= 0, name
        assert cls(ctx).status().status in STATUSES, name
        if cls.paid:  # a paid source never runs on its own: it needs the owner, credentials and a budget
            assert cls.requires_owner_approval and cls.credential_settings and cls.cost_per_call_usd == 0, name
            assert cls.countries == () and "to be confirmed by owner" in cls.terms_url, name  # no invented coverage
    assert "to be confirmed by owner" in CompaniesHouseOfficers.terms_url
    assert CompaniesHouseOfficers.countries == ("GB",) and CompaniesHouseOfficers.fields == ("name", "title")  # no emails


def test_statuses_by_default_and_once_configured(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        st = {r["name"]: r["status"] for r in registry.overview(s, ctx)}
    assert st["company_website"] == READY and st["inbound_forms"] == READY and st["csv_import"] == READY
    assert st["sec_officers"] == NEEDS_CREDENTIALS  # no SEC_USER_AGENT in the test settings
    assert st["companies_house"] == NEEDS_CREDENTIALS
    assert st["linkedin_lead_sync"] == NEEDS_APPROVAL  # LinkedIn has to approve the Lead Sync product first
    assert st["paid_contact_provider"] == NEEDS_APPROVAL and st["paid_email_verifier"] == NEEDS_APPROVAL
    keyed = make_ctx(companies_house_api_key=KEY, sec_user_agent="aime-test qa@example.com")
    assert CompaniesHouseOfficers(keyed).status().ready
    assert registry.get_adapter(keyed, "sec_officers").status().ready


def test_the_settings_keep_every_controlled_source_off_with_no_budget(ctx):
    data = ss.get(ctx.settings, "contact_sources")
    assert [a["name"] for a in data["adapters"]] == registry.managed()
    assert set(registry.managed()) == {"companies_house", "paid_contact_provider", "paid_email_verifier"}
    assert all(not a["enabled"] and a["monthly_budget_usd"] == 0 and a["cost_per_call_usd"] == 0 for a in data["adapters"])
    bad = [({"name": "nonsense"}, "unknown contact source"), ({"name": "company_website"}, "switched by its own control"),
           ({"name": "companies_house", "countries": ["US"]}, "covers GB"),
           ({"name": "companies_house", "countries": ["GB"], "monthly_budget_usd": 5}, "is free"),
           ({"name": "paid_contact_provider", "countries": ["US"]}, "no country yet")]
    for row, message in bad:
        with pytest.raises(ValueError, match=message):
            ss.validate_shape("contact_sources", {"adapters": [row]})
    with pytest.raises(ValueError, match="listed once"):
        ss.validate_shape("contact_sources", {"adapters": [{"name": "companies_house"}, {"name": "companies_house"}]})


# --------------------------------------------------------------------------- blocked adapters refuse

def test_a_credential_blocked_adapter_refuses_and_calls_nothing(make_ctx):
    log: list = []
    ctx = make_ctx(handler=ch_handler(log))  # no COMPANIES_HOUSE_API_KEY
    enable_gb(ctx)
    set_adapter(ctx, "companies_house", enabled=True, countries=["GB"])
    with ctx.db.session() as s:
        co = uk_company(s)
        with pytest.raises(AdapterRefused, match="blocked_needs_credentials: COMPANIES_HOUSE_API_KEY"):
            CompaniesHouseOfficers(ctx).officers(s, co)
        row = budget.usage(s, "companies_house")
        assert (row.calls, row.refused, row.cost_usd) == (0, 1, 0.0)
        assert registry.registry_officers(ctx, s, co) == ([], None)  # research asks quietly: nothing called, nothing refused
    assert log == []
    assert len(audits(ctx, "contact_source_refused")) == 1


def test_an_adapter_off_or_outside_its_countries_refuses(make_ctx):
    ctx = make_ctx(handler=ch_handler([]), companies_house_api_key=KEY)
    with ctx.db.session() as s:
        co = uk_company(s)
        with pytest.raises(AdapterRefused, match="switched off"):
            CompaniesHouseOfficers(ctx).officers(s, co)
    set_adapter(ctx, "companies_house", enabled=True, countries=["GB"])
    with ctx.db.session() as s:
        with pytest.raises(AdapterRefused, match="GB is not a target country"):  # the UK is not enabled by default
            CompaniesHouseOfficers(ctx).officers(s, s.get(Company, co.id))
        assert budget.refusal(s, CompaniesHouseOfficers(ctx), "US") == "it covers GB, not US"


@pytest.mark.parametrize("cls", [PaidContactProvider, PaidEmailVerifier])
def test_a_paid_stub_refuses_until_approval_credentials_budget_and_a_vendor_exist(make_ctx, cls):
    ctx = make_ctx()
    adapter = cls(ctx)

    def refused(match):
        with ctx.db.session() as s:
            with pytest.raises(AdapterRefused, match=match):
                adapter.call(s, {"kind": "lookup"})

    refused("switched off")
    set_adapter(ctx, cls.name, enabled=True)
    refused("blocked_needs_owner_approval: the owner has not recorded an approval")
    with ctx.db.session() as s:
        budget.record_approval(s, cls.name, "Owner", "boss", "trial quote reviewed")
    refused("blocked_needs_credentials")
    setattr(ctx.settings, cls.credential_settings[0], "vendor-" + "key-not-real")
    refused("monthly budget is 0")
    set_adapter(ctx, cls.name, monthly_budget_usd=10.0)
    refused("no price per call recorded")
    set_adapter(ctx, cls.name, cost_per_call_usd=0.01)
    refused("no vendor integration has been built")
    with ctx.db.session() as s:
        assert adapter.status(s).status == NEEDS_APPROVAL
        row = budget.usage(s, cls.name)
        assert row.calls == 0 and row.cost_usd == 0 and row.refused == 6
        assert budget.withdraw_approval(s, cls.name, "boss")
    refused("blocked_needs_owner_approval")
    assert [a.action for a in audits(ctx) if a.action.startswith("contact_source_approv")] == [
        "contact_source_approved", "contact_source_approval_withdrawn"]
    with ctx.db.session() as s, pytest.raises(ValueError, match="only a source that needs"):
        budget.record_approval(s, "companies_house", "Owner", "boss")


# --------------------------------------------------------------------------- the budget guard

class FakePaid(PaidStub):
    """A paid adapter with a vendor integration, for the guard's tests only."""

    name = "fake_paid"
    title = "Fake paid provider (tests)"
    description = "Answers from memory."
    countries = ("US",)
    fields = ("email",)
    credential_settings = ("contact_provider_api_key",)
    vendor_integration = True
    calls = 0

    def _call(self, request):
        FakePaid.calls += 1
        return {"status": "verified"}


@pytest.fixture
def paid_ctx(make_ctx, monkeypatch):
    monkeypatch.setitem(registry.ADAPTER_CLASSES, FakePaid.name, FakePaid)
    FakePaid.calls = 0
    ctx = make_ctx(contact_provider_api_key="vendor-" + "key-not-real")
    with ctx.db.session() as s:
        budget.record_approval(s, FakePaid.name, "Owner", "boss")
    return ctx


def test_a_budget_of_zero_refuses_every_paid_call(paid_ctx):
    set_adapter(paid_ctx, FakePaid.name, enabled=True, countries=["US"], cost_per_call_usd=0.25)
    with paid_ctx.db.session() as s:
        with pytest.raises(AdapterRefused, match="monthly budget is 0"):
            FakePaid(paid_ctx).call(s, {"kind": "verify"}, country="US")
    assert FakePaid.calls == 0


def test_the_cap_is_enforced_before_each_call_and_the_ledger_is_audited(paid_ctx):
    set_adapter(paid_ctx, FakePaid.name, enabled=True, countries=["US"], cost_per_call_usd=0.4, monthly_budget_usd=1.0)
    adapter = FakePaid(paid_ctx)
    with paid_ctx.db.session() as s:
        assert adapter.status(s).ready
        assert adapter.call(s, {"kind": "verify"}, country="US") == {"status": "verified"}
        adapter.call(s, {"kind": "verify"}, country="US")
        with pytest.raises(AdapterRefused, match=r"budget is reached \(\$0.80 of \$1.00 spent; this call costs \$0.40\)"):
            adapter.call(s, {"kind": "verify"}, country="US")
        with pytest.raises(AdapterRefused, match="covers US, not GB"):
            adapter.call(s, {"kind": "verify"}, country="GB")
        row = budget.usage(s, FakePaid.name)
        assert (row.calls, round(row.cost_usd, 2), row.refused, row.month) == (2, 0.8, 2, budget.month_key())
        assert row.last_call_at is not None and row.last_result.startswith("refused")
    assert FakePaid.calls == 2
    spends = audits(paid_ctx, "contact_source_spend")
    assert len(spends) == 2 and "$0.80 spent in" in spends[-1].detail
    assert len(audits(paid_ctx, "contact_source_refused")) == 2
    set_adapter(paid_ctx, FakePaid.name, monthly_budget_usd=2.0)  # the owner raises the cap: calls go on
    with paid_ctx.db.session() as s:
        adapter.call(s, {"kind": "verify"}, country="US")
        assert budget.usage(s, FakePaid.name).calls == 3


def test_a_failed_paid_call_still_counts_against_the_cap(paid_ctx, monkeypatch):
    set_adapter(paid_ctx, FakePaid.name, enabled=True, countries=["US"], cost_per_call_usd=0.5, monthly_budget_usd=0.5)

    def boom(self, request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(FakePaid, "_call", boom)
    with paid_ctx.db.session() as s:
        with pytest.raises(httpx.ConnectError):
            FakePaid(paid_ctx).call(s, {"kind": "verify"}, country="US")
        with pytest.raises(AdapterRefused, match="budget is reached"):
            FakePaid(paid_ctx).call(s, {"kind": "verify"}, country="US")
        row = budget.usage(s, FakePaid.name)
        assert (row.calls, row.cost_usd, row.errors) == (1, 0.5, 1)


# --------------------------------------------------------------------------- Companies House

def test_company_numbers_and_officer_parsing_keep_names_and_roles_only():
    assert company_number("1234567") == "01234567" and company_number("sc123456") == "SC123456"
    assert company_number("123") == "00000123" and company_number("not a number") is None and company_number("") is None
    people = parse_officers(OFFICERS_P1, NUMBER)
    assert [(p.full_name, p.title, p.role_category) for p in people] == [("John Andrew Smith", "Director", "board"),
                                                                         ("Jane Doe", "Company Secretary", people[1].role_category)]
    smith = people[0]
    assert smith.source == "companies_house" and smith.confidence == "high"
    assert smith.evidence_url == f"https://find-and-update.company-information.service.gov.uk/company/{NUMBER}/officers"
    assert '"SMITH, John Andrew", director, appointed 2015-03-01' in smith.evidence
    for leaked in ("1970", "British", "Engineer", "High Street", "London"):
        assert leaked not in smith.evidence and leaked not in smith.title
    assert parse_officers({"items": [{"name": "Madonna", "officer_role": "director"}, "junk", {"officer_role": "director"}]}, NUMBER) == []
    assert parse_officers({}, NUMBER) == []


def test_officers_come_from_the_api_with_the_key_and_every_page_through_the_guard(ch_ctx):
    with ch_ctx.db.session() as s:
        co = uk_company(s)
        people = CompaniesHouseOfficers(ch_ctx).officers(s, co)
        assert {p.full_name for p in people} == {"John Andrew Smith", "Jane Doe", "Sarah O'Neil"}
        assert next(p for p in people if p.full_name == "Sarah O'Neil").title == "Designated Member"
        row = budget.usage(s, "companies_house")
        assert (row.calls, row.cost_usd, row.refused, row.errors) == (2, 0.0, 0, 0)
    assert [r.url.params["start_index"] for r in ch_ctx.requests] == ["0", "4"]
    auth = ch_ctx.requests[0].headers["authorization"]
    assert auth == "Basic " + base64.b64encode(f"{KEY}:".encode()).decode()
    assert not audits(ch_ctx, "contact_source_spend")  # a free source is counted, never charged


def test_a_429_waits_for_retry_after_and_refuses_until_then(make_ctx):
    log: list = []
    ctx = make_ctx(handler=ch_handler(log, 429, {"Retry-After": "120"}), companies_house_api_key=KEY)
    enable_gb(ctx)
    set_adapter(ctx, "companies_house", enabled=True, countries=["GB"])
    with ctx.db.session() as s:
        co = uk_company(s)
        with pytest.raises(RateLimited) as err:
            CompaniesHouseOfficers(ctx).officers(s, co)
        assert err.value.retry_after == 120
        assert budget.cooldown_until(s, "companies_house") is not None
        with pytest.raises(AdapterRefused, match="asked us to wait"):
            CompaniesHouseOfficers(ctx).officers(s, co)
        found, problem = registry.registry_officers(ctx, s, co)
        assert found == [] and problem is None  # research does not call while the wait lasts
        row = budget.usage(s, "companies_house")
        assert (row.calls, row.errors, row.refused) == (1, 1, 1)
    assert len(log) == 1
    assert len(audits(ctx, "contact_source_rate_limited")) == 1


@pytest.mark.parametrize("status,error", [(401, "refused the API key"), (403, "refused the API key"), (500, "HTTP 500")])
def test_api_errors_are_reported_and_counted(make_ctx, status, error):
    ctx = make_ctx(handler=ch_handler([], status), companies_house_api_key=KEY)
    enable_gb(ctx)
    set_adapter(ctx, "companies_house", enabled=True, countries=["GB"])
    with ctx.db.session() as s:
        co = uk_company(s)
        with pytest.raises(SourceError, match=error):
            CompaniesHouseOfficers(ctx).officers(s, co)
        found, problem = registry.registry_officers(ctx, s, co)
        assert found == [] and error in problem
        assert budget.usage(s, "companies_house").errors == 2
        assert KEY not in (budget.usage(s, "companies_house").last_result or "")


def test_an_unknown_company_number_gives_nobody(ch_ctx):
    with ch_ctx.db.session() as s:
        assert CompaniesHouseOfficers(ch_ctx).officers(s, uk_company(s, number="09999999")) == []
        assert CompaniesHouseOfficers(ch_ctx).officers(s, Company(name="No number", normalized_name="x", country="GB", source="t")) == []


def test_company_research_records_register_officers_with_provenance(ch_ctx):
    with ch_ctx.db.session() as s:
        co = uk_company(s)
        s.add(Lead(company_id=co.id, source="research", status="qualified", score=60))
        cid = co.id
    out = CompanyResearchAgent(ch_ctx, mail_checker=lambda d: True).run({"company_ids": [cid]}).output
    assert out.registry_officers == 3 and out.contacts_created == 3 and out.companies_researched == 1
    with ch_ctx.db.session() as s:
        people = s.execute(select(Contact).where(Contact.company_id == cid)).scalars().all()
        assert {c.source for c in people} == {"companies_house"} and all(c.email is None for c in people)
        for c in people:
            assert c.source_licence == CompaniesHouseOfficers.licence and c.lawful_basis == CompaniesHouseOfficers.lawful_basis
            assert c.source_ref.endswith(f"/company/{NUMBER}/officers") and c.provenance["adapter"] == "companies_house"
        assert s.get(Company, cid).research_status == "done"
    from aime.doctor import run_checks

    checks = {c["name"]: c for c in run_checks(ch_ctx)}
    for name in ("Contacts only from approved sources", "Researched contacts have evidence"):
        assert checks[name]["ok"], checks[name]["detail"]


def test_company_research_never_asks_a_register_by_default(make_ctx):
    log: list = []
    ctx = make_ctx(handler=ch_handler(log), companies_house_api_key=KEY)  # a key, but the adapter is off and GB not a target
    with ctx.db.session() as s:
        cid = uk_company(s).id
    out = CompanyResearchAgent(ctx, mail_checker=lambda d: True).run({"company_ids": [cid]}).output
    assert out.registry_officers == 0 and out.companies_researched == 0  # no website, no CIK, no register: not a candidate
    assert log == []
    with ctx.db.session() as s:
        assert s.execute(select(func.count(ContactSourceUsage.id))).scalar_one() == 0


# --------------------------------------------------------------------------- provenance on every path

def finding(name, title, source="website", url="https://acme.example/team"):
    return PersonFinding(name, title, "executive", "high", f"{name}, {title}", url, source=source)


def test_every_new_contact_records_its_source_licence_and_lawful_basis(ctx):
    with ctx.db.session() as s:
        co = Company(name="Acme", normalized_name="acme", domain="acme.example", source="test")
        s.add(co)
        s.flush()
        web, _ = save_contact(s, co, finding("Maria Lopez", "Chief Executive Officer"), None)
        sec, _ = save_contact(s, co, finding("John Smith", "CFO", "sec_filing", "https://www.sec.gov/Archives/x-index.htm"), None)
        assign_published_emails(s, co, [{"email": "info@acme.example", "url": "https://acme.example/contact", "near": None,
                                         "on_domain": True}], lambda d: True)
        inbox = s.execute(select(Contact).where(Contact.email == "info@acme.example")).scalar_one()
        inbound = upsert_contact(s, co, "pat@buyer.example", "Pat", "Lee", None, "COO", "inbound", "inquiry",
                                 "Inbound inquiry #17 via web")
        linked = upsert_contact(s, co, "lin@buyer.example", "Lin", "Ho", None, "CTO", "inbound", "opt_in",
                                "Inbound inquiry #18 via linkedin_form with marketing opt-in")
        manual = Contact(company_id=co.id, first_name="Tess", email="tess@owner.example", source="manual", email_status="user_provided")
        imported = Contact(company_id=co.id, first_name="Ian", email="ian@list.example", source="import")
        odd = Contact(company_id=co.id, first_name="Odd", source="mystery")
        s.add_all([manual, imported, odd])
        s.flush()
        expect = {web: ("company_website", "https://acme.example/team"),
                  sec: ("sec_officers", "https://www.sec.gov/Archives/x-index.htm"),
                  inbox: ("company_website", "https://acme.example/contact"),
                  inbound: ("inbound_forms", "inbound_inquiry:17"), linked: ("linkedin_lead_sync", "inbound_inquiry:18"),
                  manual: ("manual_entry", "entered by a person"), imported: ("csv_import", None)}
        for c, (adapter, ref) in expect.items():
            cls = registry.ADAPTER_CLASSES[adapter]
            assert (c.source_licence, c.lawful_basis, c.source_ref) == (cls.licence, cls.lawful_basis, ref), c.source
            assert c.provenance["adapter"] == adapter and c.provenance["source"] == c.source and c.provenance["recorded_at"]
        assert linked.lawful_basis == "consent" and inbound.lawful_basis == "inquiry_or_consent"
        assert (odd.source_licence, odd.lawful_basis, odd.provenance["adapter"]) == ("unknown: mystery", "unknown", None)
        assert inbox.verified_by == "mx_check" and inbox.verified_at is not None


def test_older_contacts_are_backfilled_lazily_as_legacy(ctx):
    with ctx.db.session() as s:
        co = Company(name="Old", normalized_name="old", source="test")
        s.add(co)
        s.flush()
        s.add(Contact(company_id=co.id, first_name="Ann", last_name="Lee", source="website", evidence_url="https://old.example/team"))
        s.add(Contact(company_id=co.id, email="x@old.example", source="inbound", consent_source="Inbound inquiry #3 via api"))
    with ctx.db.session() as s:  # as a row from before the columns existed
        s.execute(text("UPDATE contacts SET source_licence = NULL, lawful_basis = NULL, source_ref = NULL, provenance = NULL"))
    with ctx.db.session() as s:
        assert provenance.pending_legacy(s) == 2
        first = s.execute(select(Contact).order_by(Contact.id)).scalars().first()
        assert provenance.view(first) == {"adapter": None, "licence": "legacy: website", "lawful_basis": "legacy: website",
                                          "ref": "https://old.example/team", "legacy": True}
        assert provenance.backfill_legacy(s, limit=1) == 1
        assert provenance.backfill_legacy(s) == 1 and provenance.backfill_legacy(s) == 0
        rows = s.execute(select(Contact).order_by(Contact.id)).scalars().all()
        assert [(c.source_licence, c.lawful_basis, c.source_ref) for c in rows] == [
            ("legacy: website", "legacy: website", "https://old.example/team"),
            ("legacy: inbound", "legacy: inbound", "inbound_inquiry:3")]
        assert all(provenance.is_legacy(c) and c.provenance["legacy"] for c in rows)


def test_company_research_backfills_legacy_contacts(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="Old", normalized_name="old", source="test")
        s.add(co)
        s.flush()
        s.add(Contact(company_id=co.id, first_name="Ann", last_name="Lee", source="sec_filing"))
    with ctx.db.session() as s:
        s.execute(text("UPDATE contacts SET source_licence = NULL, lawful_basis = NULL"))
    CompanyResearchAgent(ctx, mail_checker=lambda d: True).run({})
    with ctx.db.session() as s:
        assert s.execute(select(Contact.source_licence)).scalar_one() == "legacy: sec_filing"


def test_an_existing_database_gets_the_new_columns_and_table_additively(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE contacts (id INTEGER PRIMARY KEY, company_id INTEGER, first_name VARCHAR(80), "
                          "last_name VARCHAR(80), email VARCHAR(200), source VARCHAR(50) NOT NULL)"))
        conn.execute(text("INSERT INTO contacts (id, first_name, last_name, source) VALUES (1, 'Ann', 'Lee', 'website')"))
    applied = migrate(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("contacts")}
    assert {"source_licence", "lawful_basis", "source_ref", "provenance", "verified_by", "verified_at"} <= cols
    assert any("ADD COLUMN \"source_licence\"" in a for a in applied)
    assert "contact_source_usage" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT first_name, last_name, source, source_licence FROM contacts")).one() == ("Ann", "Lee", "website", None)
    assert migrate(engine) == []  # a second run changes nothing


def test_erasure_keeps_the_licence_and_basis_but_drops_the_page(ctx):
    from aime.crm_ops import erase_person

    with ctx.db.session() as s:
        co = Company(name="Acme", normalized_name="acme", domain="acme.example", source="test")
        s.add(co)
        s.flush()
        c = Contact(company_id=co.id, first_name="Jane", last_name="Doe", email="jane@acme.example", source="website",
                    evidence_url="https://acme.example/team/jane-doe")
        s.add(c)
        s.flush()
        erase_person(s, "jane@acme.example", "owner")
        s.flush()
        assert c.source_ref is None and c.provenance["ref"] is None and c.provenance["erased"]
        assert c.source_licence == "published_by_the_company" and c.provenance["adapter"] == "company_website"
