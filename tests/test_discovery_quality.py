"""Discovery quality: hiring evidence counts for less as it ages (and the reason says how old it is), every company
keeps why we know it is a US company, and the 30-minute intake job checks a few websites so new companies do not
wait a day for their check."""

from datetime import timedelta

from sqlalchemy import select

from aime.agents.crm import CRMManagementAgent
from aime.crm_ops import upsert_company
from aime.dedupe import clean_company_name
from aime.models import Company
from aime.scoring import SignalFact, score_lead
from aime.sources.base import CompanyCandidate
from aime.timeutil import utcnow
from aime.website_check import WebsiteResult
from aime.workflows import job_inbound
from tests.test_scoring import facts


def test_hiring_evidence_counts_for_less_as_it_ages():
    def intent(days):
        r = score_lead(facts(signals=[SignalFact("hiring", ["ai_agents"], 1.0, utcnow() - timedelta(days=days), True)]))
        return r.intent, next(x for x in r.reasons if "intent" in x)

    assert intent(30) == (6, "+6 intent: public hiring post (30 days old) mentions a related need (signal, not buying intent)")
    assert intent(90)[0] == 3 and "90 days old, so it counts for less" in intent(90)[1]
    assert intent(150)[0] == 0 and "150 days old, too old to count" in intent(150)[1]


def test_every_company_keeps_why_it_is_a_us_company(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        filed, _ = upsert_company(s, CompanyCandidate(name="Filed Co", source="sec_edgar", source_url="https://sec.example/1",
                                                      domain="filed.example", state="CA", is_us=True))
        posted, _ = upsert_company(s, CompanyCandidate(name="Posted Co", source="hn_hiring", source_url="https://hn.example/2",
                                                       domain="posted.example", location_text="Remote (US)", is_us=True))
        assert filed.location_evidence == "US state CA (SEC filing business address)"
        assert posted.location_evidence == 'Hacker News hiring post: "Remote (US)"'
        older = Company(name="Older Co", normalized_name="older co", domain="older.example", source="hn_hiring",
                        state="TX", country="US")
        s.add(older)
    CRMManagementAgent(ctx).run({"operations": ["hygiene"]})
    with ctx.db.session() as s:
        assert s.execute(select(Company).where(Company.name == "Older Co")).scalar_one().location_evidence \
            == "US state TX (Hacker News hiring post)"


def test_the_intake_job_checks_a_small_batch_of_websites(make_ctx, monkeypatch):
    ctx = make_ctx()
    checked = []

    def fake_check(_ctx, domain, _name):
        checked.append(domain)
        return WebsiteResult("verified", domain, None, "test")

    monkeypatch.setattr("aime.agents.crm.check_website", fake_check)
    with ctx.db.session() as s:
        for i in range(7):
            s.add(Company(name=f"Co {i}", normalized_name=f"co {i}", domain=f"co{i}.example", source="test", country="US"))
    job_inbound(ctx)
    assert len(checked) == 5  # research.website_checks_per_inbound_run


def test_company_names_never_carry_a_pasted_web_address(make_ctx):
    assert clean_company_name("Snout https://snout.com/") == "Snout"
    assert clean_company_name("Acme (https://acme.example)") == "Acme"
    assert clean_company_name("https://only.example") == "https://only.example"  # nothing else to keep
    ctx = make_ctx()
    with ctx.db.session() as s:
        new, _ = upsert_company(s, CompanyCandidate(name="Fresh Labs https://fresh.example/", source="hn_hiring",
                                                    source_url="https://hn.example/3", domain="fresh.example"))
        assert (new.name, new.normalized_name) == ("Fresh Labs", "fresh labs")
        s.add(Company(name="Old Labs www.old.example", normalized_name="old labs www old example", domain="old.example",
                      source="hn_hiring"))
    CRMManagementAgent(ctx).run({"operations": ["hygiene"]})
    with ctx.db.session() as s:
        old = s.execute(select(Company).where(Company.domain == "old.example")).scalar_one()
        assert (old.name, old.normalized_name) == ("Old Labs", "old labs") and "web address removed" in old.validation["name"]
