"""Email verification statuses and the lead funnel (Option B, Phase 4): the free mail-server check as the default verifier,
the stored statuses mapped into one vocabulary, a role address never becoming a verified decision-maker, and every
company classified into its funnel stage on a seeded database, with the Sales Manager's report counting from the same
definitions."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from aime import funnel, manager_report as mr
from aime.agents.analytics import sales_kpis
from aime.models import Company, Contact, Lead
from aime.timeutil import utcnow
from aime.verification import (INVALID, MX_OK, PUBLISHED, RISKY, ROLE_ADDRESS, STATUSES, UNKNOWN, VERIFIED, MXVerifier,
                               VerificationResult, Verifier, status_of, verify_contact)


class Checker:
    def __init__(self, answers):
        self.answers = answers

    def __call__(self, domain):
        return self.answers.get(domain, True)


class SaysVerified(Verifier):
    name = "fake_verifier"

    def __init__(self, status=VERIFIED):
        self.status = status

    def check(self, email):
        return VerificationResult(self.status, "mailbox answered", self.name)


# --------------------------------------------------------------------------- verification

def test_the_default_verifier_checks_syntax_and_the_mail_server():
    v = MXVerifier(Checker({"nomail.example": False, "slow.example": None}))
    assert v.check("jane.doe@acme.example") == VerificationResult(MX_OK, "the domain takes mail", "mx_check")
    assert v.check("sales@acme.example").status == ROLE_ADDRESS
    assert v.check("jane@nomail.example").status == INVALID
    assert v.check("sales@nomail.example").status == INVALID  # an inbox that cannot receive mail is no inbox
    assert v.check("jane@slow.example").status == UNKNOWN
    assert v.check("not an address").status == INVALID and v.check("a..b@acme.example").status == INVALID
    assert set(STATUSES) == {"published", "mx_ok", "verified", "risky", "invalid", "role_address", "unknown"}


@pytest.mark.parametrize("email,stored,role,expected", [
    (None, None, None, UNKNOWN), ("jane@acme.example", "published", None, PUBLISHED), ("jane@acme.example", "verified", None, VERIFIED),
    ("jane@acme.example", "valid", None, VERIFIED), ("jane@acme.example", "unchecked", None, UNKNOWN),
    ("jane@acme.example", "user_provided", None, UNKNOWN), ("jane@acme.example", "mx_ok", None, MX_OK),
    ("jane@acme.example", "risky", None, RISKY), ("jane@acme.example", "bounced", None, INVALID),
    ("jane@acme.example", "no_mail_server", None, INVALID), ("info@acme.example", "role_address", "inbox", ROLE_ADDRESS),
    ("sales@acme.example", "published", None, ROLE_ADDRESS),  # printed under a name, still a company inbox
    ("hello@acme.example", "verified", None, ROLE_ADDRESS), ("info@acme.example", "bounced", "inbox", INVALID)])
def test_stored_statuses_map_to_one_vocabulary(email, stored, role, expected):
    assert status_of(Contact(email=email, email_status=stored, role_category=role, source="website")) == expected


def test_verify_contact_records_who_checked_and_never_verifies_a_role_address(ctx):
    with ctx.db.session() as s:
        co = Company(name="Acme", normalized_name="acme", domain="acme.example", source="test")
        s.add(co)
        s.flush()
        person = Contact(company_id=co.id, first_name="Jane", last_name="Doe", title="CEO", email="jane@acme.example",
                         source="website", email_status="unchecked")
        inbox = Contact(company_id=co.id, last_name="Hill", first_name="Sam", title="CEO", email="sales@acme.example",
                        source="website", email_status="published")
        bounced = Contact(company_id=co.id, first_name="Bo", last_name="Unce", email="bo@acme.example", source="website",
                          email_status="bounced")
        given = Contact(company_id=co.id, first_name="Tess", email="tess@owner.example", source="manual", email_status="user_provided")
        s.add_all([person, inbox, bounced, given])
        s.flush()
        assert not funnel.is_verified_decision_maker(person)
        res = verify_contact(s, person, SaysVerified())
        assert res.status == VERIFIED and person.email_status == "verified" and person.verified_by == "fake_verifier"
        assert person.verified_at is not None and person.provenance["verification"]["status"] == VERIFIED
        assert funnel.is_verified_decision_maker(person)
        res = verify_contact(s, inbox, SaysVerified())
        assert res.status == ROLE_ADDRESS and inbox.email_status == "published" and status_of(inbox) == ROLE_ADDRESS
        assert not funnel.is_verified_decision_maker(inbox)
        verify_contact(s, bounced, SaysVerified())
        assert bounced.email_status == "bounced" and not funnel.is_verified_decision_maker(bounced)
        verify_contact(s, given, SaysVerified())
        assert given.email_status == "user_provided"
        mx = MXVerifier(Checker({"acme.example": False}))
        verify_contact(s, person, mx)
        assert person.email_status == "no_mail_server" and person.verified_by == "mx_check"
        verify_contact(s, person, SaysVerified(RISKY))
        assert person.email_status == "risky" and not funnel.is_verified_decision_maker(person)


def test_a_published_named_address_is_a_verified_decision_maker_and_titles_follow_the_report():
    def c(**kw):
        return Contact(**{"first_name": "Jane", "last_name": "Doe", "email": "jane@acme.example", "source": "website",
                          "email_status": "published", "consent_status": "none", "is_sample": False, **kw})
    assert funnel.is_verified_decision_maker(c(title="Chief Executive Officer"))
    assert funnel.is_verified_decision_maker(c(title=None, source="sec_filing"))
    assert not funnel.is_verified_decision_maker(c(title="Executive Assistant"))
    assert funnel.is_verified_decision_maker(c(title="Assistant Vice President, Sales"))
    assert not funnel.is_verified_decision_maker(c(title="Former CEO"))
    assert not funnel.is_verified_decision_maker(c(title="CEO", consent_status="opted_out"))
    assert not funnel.is_verified_decision_maker(c(title="CEO", source="inbound"))  # an inquiry is not a researched decision-maker
    assert not funnel.is_verified_decision_maker(c(title="CEO", last_name=None))
    assert not funnel.is_verified_decision_maker(c(title="CEO", email="info@acme.example"))


# --------------------------------------------------------------------------- the funnel

def seed(s) -> dict:
    def co(name, **kw):
        row = Company(name=name, normalized_name=name.lower(), domain=f"{name.lower()}.example", source="test", **kw)
        s.add(row)
        s.flush()
        return row.id

    def person(cid, email=None, status=None, last="Doe", title="CEO", source="website", **kw):
        s.add(Contact(company_id=cid, first_name="Jo", last_name=last, title=title, email=email, email_status=status, source=source, **kw))

    ids = {}
    ids["bare"] = co("Bare")  # discovered only
    ids["named"] = co("Named")
    person(ids["named"])  # a decision-maker with no address
    ids["inbox"] = co("Inbox")
    person(ids["inbox"], "info@inbox.example", "role_address", last=None, title="Company inbox")  # contactable, nobody named
    ids["verified"] = co("Verified")
    person(ids["verified"], "jo.doe@verified.example", "published")  # verified decision-maker, lead not qualified
    s.add(Lead(company_id=ids["verified"], source="research", status="new"))
    ids["ready"] = co("Ready")
    person(ids["ready"], "jo.doe@ready.example", "published")
    s.add(Lead(company_id=ids["ready"], source="research", status="qualified"))
    ids["inbox_only_qualified"] = co("Qualifiedinbox")
    person(ids["inbox_only_qualified"])
    person(ids["inbox_only_qualified"], "sales@qualifiedinbox.example", "published", last="Hill")  # an inbox under a name
    s.add(Lead(company_id=ids["inbox_only_qualified"], source="research", status="qualified"))
    ids["competitor"] = co("Rival", is_competitor=True)
    person(ids["competitor"], "jo.doe@rival.example", "published")
    s.add(Lead(company_id=ids["competitor"], source="research", status="qualified"))
    ids["bounced"] = co("Bounced")
    person(ids["bounced"], "jo.doe@bounced.example", "bounced")
    s.add(Lead(company_id=ids["bounced"], source="research", status="qualified"))
    ids["unverified_qualified"] = co("Unverified")  # a qualified company record with no verified person
    s.add(Lead(company_id=ids["unverified_qualified"], source="research", status="qualified"))
    sample = co("Sample", is_sample=True)
    person(sample, "jo.doe@sample.example", "published", is_sample=True)
    s.flush()
    return ids


def test_each_company_is_classified_into_its_funnel_stage(ctx):
    with ctx.db.session() as s:
        ids = seed(s)
        rows = funnel.classify(s)
        stage = {name: rows[cid].stage for name, cid in ids.items()}
        assert stage == {"bare": "discovered_company", "named": "identified_decision_maker", "inbox": "discovered_company",
                         "verified": "verified_contact", "ready": "outreach_ready_lead",
                         "inbox_only_qualified": "contactable_prospect", "competitor": "qualified_lead",
                         "bounced": "identified_decision_maker", "unverified_qualified": "discovered_company"}
        assert rows[ids["inbox"]].flags["contactable_prospect"] and not rows[ids["inbox"]].flags["verified_contact"]
        assert rows[ids["unverified_qualified"]].flags["qualified_lead"]
        assert not rows[ids["unverified_qualified"]].flags["outreach_ready_lead"]  # never a verified decision-maker lead
        assert len(rows) == len(ids)  # sample data is left out
        summary = funnel.summary(s)
    assert summary["companies"] == 9
    got = {st["key"]: (st["reached"], st["in_funnel"]) for st in summary["stages"]}
    assert got == {"discovered_company": (9, 9), "identified_decision_maker": (6, 6), "contactable_prospect": (5, 4),
                   "verified_contact": (3, 3), "qualified_lead": (5, 2), "outreach_ready_lead": (1, 1)}
    assert all(st["definition"] for st in summary["stages"]) and [s.key for s in funnel.STAGES] == list(got)


def test_the_managers_report_counts_with_the_funnels_definitions(ctx):
    assert mr.VERIFIED_EMAIL_STATUSES is funnel.VERIFIED_EMAIL_STATUSES
    assert mr.NOT_CONTACTABLE_STATUSES is funnel.NOT_CONTACTABLE_STATUSES
    with ctx.db.session() as s:
        seed(s)
        now = utcnow() + timedelta(hours=1)
        period = mr.Period("daily", (now - timedelta(days=3)).date(), (now + timedelta(days=1)).date(), "UTC", until=now)
        data = mr.build(s, ctx.settings, period, now)
        v = {k: m["value"] for k, m in data["metrics"].items() if "value" in m}
        real = (Contact.is_sample.is_(False),)
        count = lambda *conds: s.execute(select(func.count(Contact.id)).where(*real, *conds)).scalar_one()  # noqa: E731
        assert v["decision_makers"] == count(*funnel.decision_maker())
        assert v["contactable_prospects"] == count(*funnel.contactable())
        assert v["verified_decision_makers"] == count(*funnel.verified_decision_maker())
        assert v["verified_contacts"] == count(*funnel.contactable(), *funnel.verified_email())
        assert v["verified_decision_makers"] == 3  # Verified, Ready and Rival: never the inbox printed under a name


def test_a_register_officer_counts_as_a_decision_maker_in_both_reports(ctx):
    with ctx.db.session() as s:
        co = Company(name="Thames", normalized_name="thames", country="GB", source="test")
        s.add(co)
        s.flush()
        s.add(Contact(company_id=co.id, first_name="John", last_name="Smith", title="Director", source="companies_house"))
        s.flush()
        now = utcnow() + timedelta(hours=1)
        period = mr.Period("daily", (now - timedelta(days=3)).date(), (now + timedelta(days=1)).date(), "UTC", until=now)
        v = mr.build(s, ctx.settings, period, now)["metrics"]

        def count(model, *conds):
            return s.execute(select(func.count(model.id)).where(*conds)).scalar_one()
        kpis = sales_kpis(count, datetime(2000, 1, 1))
    assert v["decision_makers"]["value"] == 1 == kpis["decision_makers_total"]
    assert v["verified_decision_makers"]["value"] == 0  # a name from a register has no verified address


def test_every_status_the_verifier_reads_as_invalid_is_not_contactable():
    """A verifier writes "invalid" (aime.verification): such an address must never be counted as one we may write to."""
    from aime import funnel, verification

    invalid = {k for k, v in verification._STORED.items() if v == verification.INVALID}
    assert invalid <= set(funnel.NOT_CONTACTABLE_STATUSES)
