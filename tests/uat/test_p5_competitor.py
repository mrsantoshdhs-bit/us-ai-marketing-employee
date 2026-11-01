"""Phase 5, competitor rules: a flag does not end a lead in conversation, a flagged company gets a lead, and the wording rules say who
the company is. Synthetic data only, fake web only."""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import aime.agents.research as rs
from aime.agents.company_research import CompanyResearchAgent
from aime.agents.crm import CRMManagementAgent
from aime.agents.qualification import LeadQualificationAgent
from aime.agents.research import USBusinessResearchAgent
from aime.competitor_guard import live_lead_on_company
from aime.matching import competitor_evidence, competitor_in_post
from aime.models import Activity, Alert, Company, Contact, FollowUp, Lead, ResearchSnapshot, Signal
from aime.sources.base import CompanyCandidate
from aime.timeutil import utcnow
from aime.web.app import create_app

WORDS = "Fractional CTO and custom AI development for growing regional manufacturers across the United States."
HOME = f'<html><head><title>Home</title><meta name="description" content="{WORDS}"></head><body><h1>Home</h1><p>{WORDS}</p></body></html>'


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    return httpx.Response(200, text=HOME, headers={"content-type": "text/html; charset=utf-8"})


def lead_on_file(ctx, name, outreach=None, status="qualified", leads=1, flag=False, desc=None, draft=None, draft_status="awaiting_approval"):
    dom = f"{name}.example"
    with ctx.db.session() as s:
        co = Company(name=name.title(), normalized_name=name, source="hn_hiring", domain=dom, website=f"https://{dom}", website_status="verified",
                     state="TX", country="US", is_competitor=flag, location_text="Austin, TX")
        s.add(co)
        s.flush()
        ct = Contact(company_id=co.id, email=f"p@{dom}", first_name="Pat", consent_status="unknown", source="website", email_status="valid")
        s.add(ct)
        s.flush()
        s.add(Signal(company_id=co.id, signal_type="hiring", text="We are hiring engineers.", source="hn_hiring", source_url=f"https://n.example/{name}",
                     fingerprint=name, observed_at=utcnow() - timedelta(days=3)))
        if desc:
            s.add(ResearchSnapshot(company_id=co.id, status="done", description=desc, description_url=f"https://{dom}/"))
        ids = []
        for i in range(leads):
            lead = Lead(company_id=co.id, contact_id=ct.id, source="research" if i == 0 else "inbound", status=status, score=70, grade="B",
                        outreach_status=outreach if i == 0 else None, primary_service="ai_agents",
                        disqualify_reason="Company appears to sell software or AI work to clients (possible competitor)" if status == "disqualified" else None)
            s.add(lead)
            s.flush()
            ids.append(lead.id)
        if outreach:
            s.add(FollowUp(lead_id=ids[0], company_id=co.id, contact_id=ct.id, channel="email", step=1, kind="outreach", status="sent", to_email=f"p@{dom}",
                           subject="Hi", body="Body", sent_at=utcnow() - timedelta(days=2)))
        if draft:
            approved = draft_status != "awaiting_approval"
            s.add(FollowUp(lead_id=ids[0], company_id=co.id, contact_id=ct.id, channel="email", step=2, kind=draft, status=draft_status, to_email=f"p@{dom}",
                           subject="Re: Hi", body="Body", approved_by="owner" if approved else None, approved_at=utcnow() if approved else None))
        return co.id, ids


def row(ctx, lead_id):
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        return lead.status, lead.outreach_status, [f.status for f in s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id).order_by(FollowUp.id)).scalars()]


def flagged(ctx, cid):
    with ctx.db.session() as s:
        return s.get(Company, cid).is_competitor


def warnings(ctx, cid):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert).where(Alert.key == f"competitor_live:{cid}")).scalars()]


def read_site_and_score(ctx, cid):
    assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).ok
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok


def crm_and_score(ctx):
    assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).ok
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok


# ------------------------------------------------------------------ a lead in conversation

@pytest.mark.parametrize("stage, kind", [("contacted", "outreach"), ("interested", "response"), ("meeting", "response"), ("replied", "response")])
def test_research_rereading_a_site_does_not_end_a_lead_already_emailed(make_ctx, stage, kind):
    ctx = make_ctx(handler=handler)
    cid, [lid] = lead_on_file(ctx, "emailed", outreach=stage, draft=kind)
    read_site_and_score(ctx, cid)
    assert row(ctx, lid) == ("qualified", stage, ["sent", "awaiting_approval"])
    assert not flagged(ctx, cid)
    told = warnings(ctx, cid)
    assert len(told) == 1 and f"#{lid}" in told[0] and "Fractional CTO" in told[0] and "Stop outreach" in told[0] and "disqualify" in told[0]


def test_a_second_lead_on_the_company_does_not_end_the_emailed_one(make_ctx):
    ctx = make_ctx(handler=handler)
    cid, [emailed, new] = lead_on_file(ctx, "twoleads", outreach="contacted", leads=2, desc=WORDS, draft="outreach")
    crm_and_score(ctx)
    assert row(ctx, emailed) == ("qualified", "contacted", ["sent", "awaiting_approval"])
    assert not flagged(ctx, cid)
    assert len(warnings(ctx, cid)) == 1
    with ctx.db.session() as s:
        assert s.get(Lead, new).status != "disqualified"


def test_the_warning_does_not_read_as_a_flag_aime_set_and_comes_once_from_either_path(make_ctx):
    ctx = make_ctx(handler=handler)
    cid, [emailed, _] = lead_on_file(ctx, "repeats", outreach="contacted", leads=2, desc=WORDS)
    for _ in range(2):
        crm_and_score(ctx)
    read_site_and_score(ctx, cid)
    assert len(warnings(ctx, cid)) == 1
    with ctx.db.session() as s:
        subjects = s.execute(select(Activity.subject).where(Activity.company_id == cid, Activity.subject.like("%competitor%"))).scalars().all()
    assert len(subjects) == 1 and not subjects[0].startswith("Marked as a possible competitor")
    with ctx.db.session() as s:  # a person dismisses it: the next morning does not raise it again
        for a in s.execute(select(Alert).where(Alert.key == f"competitor_live:{cid}")).scalars():
            a.acknowledged, a.acknowledged_at = True, utcnow() - timedelta(days=2)
    crm_and_score(ctx)
    assert len(warnings(ctx, cid)) == 1
    assert row(ctx, emailed)[:2] == ("qualified", "contacted")


@pytest.mark.parametrize("via", ["research", "crm"])
def test_an_approved_step_goes_back_for_approval_and_a_later_approval_stands(make_ctx, via):
    ctx = make_ctx(handler=handler)
    cid, [lid, *_] = lead_on_file(ctx, f"approved{via}", outreach="contacted", leads=1 if via == "research" else 2, desc=WORDS,
                                  draft="outreach", draft_status="approved")
    run = (lambda: read_site_and_score(ctx, cid)) if via == "research" else (lambda: crm_and_score(ctx))
    run()
    with ctx.db.session() as s:
        step = s.execute(select(FollowUp).where(FollowUp.lead_id == lid, FollowUp.step == 2)).scalar_one()
        assert step.status == "awaiting_approval" and step.approved_at is None and step.approved_by is None
        assert "Fractional CTO" in step.blocked_reason
        step.status, step.approved_at, step.approved_by = "approved", utcnow(), "owner"  # a person read the warning and approved it again
    run()
    assert row(ctx, lid)[2] == ["sent", "approved"]


def test_an_approved_first_email_of_a_second_lead_goes_back_too(make_ctx):
    """The flag used to cancel it. With no flag it would go on the next send run, to a company that reads as a seller."""
    ctx = make_ctx(handler=handler)
    cid, [_, second] = lead_on_file(ctx, "sibling", outreach="contacted", leads=2, desc=WORDS)
    with ctx.db.session() as s:
        s.add(FollowUp(lead_id=second, company_id=cid, channel="email", step=1, kind="outreach", status="approved", to_email="q@sibling.example",
                       subject="Hi", body="Body", approved_by="owner", approved_at=utcnow()))
    crm_and_score(ctx)
    assert row(ctx, second)[2] == ["awaiting_approval"]


@pytest.mark.parametrize("draft_status", ["sending", "uncertain"])
def test_mail_that_may_have_gone_is_left_alone(make_ctx, draft_status):
    ctx = make_ctx(handler=handler)
    cid, [lid] = lead_on_file(ctx, "inflight", outreach="contacted", draft="outreach", draft_status=draft_status)
    read_site_and_score(ctx, cid)
    assert row(ctx, lid)[2] == ["sent", draft_status]


@pytest.mark.parametrize("stage, status", [("no_response", "nurture"), ("stopped", "qualified"), ("not_interested", "nurture"), ("bounced", "nurture"),
                                           ("unsubscribed", "qualified"), ("reengage", "nurture"), (None, "qualified"), ("drafted", "qualified"), ("approved", "qualified")])
def test_a_lead_not_in_conversation_does_not_stop_the_flag(make_ctx, stage, status):
    """outreach.eligible refuses a flagged company's re-engagement email and its reopening: these stages must not take the flag away."""
    ctx = make_ctx(handler=handler)
    cid, [lid] = lead_on_file(ctx, "quiet", outreach=stage, status=status)
    with ctx.db.session() as s:
        assert live_lead_on_company(s, cid) is None
    assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).output.competitors == [cid]
    assert flagged(ctx, cid) and warnings(ctx, cid) == []


@pytest.mark.parametrize("stage", ["contacted", "interested", "meeting"])
def test_a_company_with_only_a_closed_lead_is_still_flagged(make_ctx, stage):
    ctx = make_ctx(handler=handler)
    cid, _ = lead_on_file(ctx, "closed", outreach=stage, status="disqualified")
    assert CompanyResearchAgent(ctx).run({"company_ids": [cid]}).output.competitors == [cid]
    assert flagged(ctx, cid)


def test_a_new_lead_beside_a_stopped_one_is_still_flagged_once(make_ctx):
    ctx = make_ctx(handler=handler)
    cid, [_, new] = lead_on_file(ctx, "stoppedone", outreach="stopped", leads=2, desc=WORDS)
    with ctx.db.session() as s:
        third = Lead(company_id=cid, source="manual", status="new")  # the flag and its log line come once, not per lead
        s.add(third)
        s.flush()
        eligible = (new, third.id)
    assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).output.competitors_flagged == 1
    assert flagged(ctx, cid)
    with ctx.db.session() as s:
        marks = s.execute(select(Activity).where(Activity.company_id == cid, Activity.subject.like("Marked as a possible competitor%"))).scalars().all()
    assert len(marks) == 1 and marks[0].lead_id in eligible


def test_a_persons_clear_stands_and_no_warning_comes(make_ctx):
    ctx = make_ctx(handler=handler)
    cid, _ = lead_on_file(ctx, "cleared", outreach="contacted", leads=2, desc=WORDS)
    with ctx.db.session() as s:
        s.get(Company, cid).competitor_cleared_by = "owner"
    crm_and_score(ctx)
    read_site_and_score(ctx, cid)
    assert not flagged(ctx, cid) and warnings(ctx, cid) == []


# ------------------------------------------------------------------ a flagged company has a lead

class FakeSource:
    name, countries = "fake", ("US",)

    def __init__(self, cands):
        self.cands = cands

    def configured(self):
        return True, "ok"

    def fetch(self, focus):
        return self.cands


def discovered(name, post, record="p1"):
    return CompanyCandidate(name=name, source="hn_hiring", source_url=f"https://n.example/{record}", source_record_id=record, domain=f"{name.lower()}.example",
                            website=f"https://{name.lower()}.example", location_text="Austin, TX", state="TX", is_us=True, description=post, signal_text=post,
                            signal_type="hiring", observed_at=utcnow())


def test_a_true_competitor_post_at_discovery_gets_one_lead_a_reason_and_a_way_to_clear_it(make_ctx, monkeypatch):
    ctx = make_ctx(handler=handler)
    post = "Synthseller | Backend Engineer | Austin, TX. We are a software development company building apps for clients."
    for record in ("p1", "p2"):
        monkeypatch.setattr(rs, "get_sources", lambda c, s=None, r=record: [FakeSource([discovered("Synthseller", post + " " + r, r)])])
        assert USBusinessResearchAgent(ctx).run({"limit": 5}).ok
    with ctx.db.session() as s:
        co = s.execute(select(Company).where(Company.normalized_name == "synthseller")).scalar_one()
        assert co.is_competitor
        cid, [lid] = co.id, s.execute(select(Lead.id).where(Lead.company_id == co.id)).scalars().all()
        assert s.execute(select(Activity).where(Activity.company_id == cid, Activity.subject.like("Marked as a possible competitor%"))).scalars().first()
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok
    assert row(ctx, lid)[0] == "disqualified" and row(ctx, lid)[2] == []
    assert "Not a competitor" in TestClient(create_app(ctx)).get(f"/leads/{lid}").text


def flagged_with_no_lead(ctx, name, source="hn_hiring", post="We are a software development company for hire."):
    with ctx.db.session() as s:
        co = Company(name=name.title(), normalized_name=name, source=source, domain=f"{name}.example", state="TX", country="US", is_competitor=True)
        s.add(co)
        s.flush()
        s.add(Signal(company_id=co.id, signal_type="hiring", text=post, source=source, source_url=f"https://n.example/{name}", fingerprint=name))
        return co.id


def leads_of(ctx, cid):
    with ctx.db.session() as s:
        return [(x.status, x.source) for x in s.execute(select(Lead).where(Lead.company_id == cid)).scalars()]


def test_a_flagged_company_with_no_lead_gets_one_and_only_one(make_ctx):
    ctx = make_ctx(handler=handler)
    cid = flagged_with_no_lead(ctx, "nolead")
    assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).output.competitors_flagged == 0
    assert leads_of(ctx, cid) == [("new", "research")]
    assert LeadQualificationAgent(ctx).run({"rescore_all": True}).ok
    for _ in range(2):
        assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).ok
    assert leads_of(ctx, cid) == [("disqualified", "research")]  # a closed lead counts: no new one each morning
    assert flagged(ctx, cid)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead).where(Lead.company_id == cid)).scalar_one()
        marks = s.execute(select(Activity).where(Activity.company_id == cid, Activity.subject.like("Marked as a possible competitor%"))).scalars().all()
        assert "competitor" in lead.disqualify_reason and len(marks) == 1 and marks[0].lead_id == lead.id


def test_the_lead_of_a_flagged_company_gets_no_email(make_ctx):
    ctx = make_ctx(handler=handler)
    cid = flagged_with_no_lead(ctx, "nomail")
    crm_and_score(ctx)
    with ctx.db.session() as s:
        assert not s.execute(select(FollowUp).where(FollowUp.company_id == cid)).scalars().first()


def test_a_merged_lead_does_not_count_a_merged_company_gets_none_and_a_filing_makes_no_lead(make_ctx):
    ctx = make_ctx(handler=handler)
    only_merged = flagged_with_no_lead(ctx, "onlymerged")
    gone = flagged_with_no_lead(ctx, "gone")
    filer = flagged_with_no_lead(ctx, "filer", source="sec_edgar")
    with ctx.db.session() as s:
        s.add(Lead(company_id=only_merged, source="research", status="merged"))
        s.get(Company, gone).merged_into_id = only_merged
    assert CRMManagementAgent(ctx).run({"operations": ["competitors"]}).ok
    assert leads_of(ctx, only_merged) == [("merged", "research"), ("new", "research")]
    assert leads_of(ctx, gone) == [] and leads_of(ctx, filer) == []


# ------------------------------------------------------------------ the wording rules

@pytest.mark.parametrize("sentence", [
    "We're not a dev shop.", "No dev shops, please.", "Web dev Shopify plugins are our main product.", "Product development shop floor tools for machinists.",
    "Nearshore wind farms need better turbine monitoring.", "We finance offshore development of wind projects.",
    "You will coordinate with our nearshore team in Mexico.", "Experience at a software development company is required.",
])
def test_a_post_with_these_words_says_nothing_about_who_the_company_is(sentence):
    assert competitor_in_post(sentence) is None


@pytest.mark.parametrize("sentence", ["Fractional chief marketing officers for consumer brands.", "Fractional chief of staff for founders.",
                                      "Connect with engineers in your company using our chat app.", "We build AI agents for retailers."])
def test_a_site_with_these_words_is_not_selling_software_work(sentence):
    assert competitor_evidence(sentence) is None


@pytest.mark.parametrize("sentence", [
    "Access vetted remote developers on demand.", "Connect with elite engineers for your project.", "Access thousands of vetted developers.",
    "Hire engineers in 48 hours.", "Hire vetted remote developers in 48 hours.", "Join thousands of engineers who ship faster with our CI.",
    "Fractional CTO for startups.", "Fractional chief technology officer services.", "Fractional CIO and CAIO.",
    "We are an AI agency.", "We are a global IT services company.", "We are a systems integrator for ERP platforms.", "A leading IT services provider.",
])
def test_real_selling_wording_on_a_site_still_flags(sentence):
    assert competitor_evidence(sentence)


@pytest.mark.parametrize("sentence", [
    "We're a dev shop of twelve engineers.", "We are a development shop that builds for startups.", "Dev shops like ours ship fast.",
    "Nearshore software engineers for US companies.", "Offshore development center in Vietnam.", "We are a nearshore company.",
    "We are a leading nearshore partner.", "We are an offshore development partner.", "Nearshore staffing for product teams.",
    "We provide offshore development.", "We offer nearshore teams.", "Your trusted offshore development partner.", "A leading nearshore partner.",
    "We are a software development company.", "A software engineering firm in Ohio.", "Staff augmentation for engineering teams.",
])
def test_a_firm_saying_so_still_flags_in_a_post_and_on_its_site(sentence):
    assert competitor_in_post(sentence) and competitor_evidence(sentence)


@pytest.mark.parametrize("sentence", ["You will work with systems integrators.", "We are an AI agency.", "Partner with an IT services firm."])
def test_the_new_self_descriptions_are_for_a_companys_own_words_not_a_post(sentence):
    assert competitor_in_post(sentence) is None


def site_saying(ctx_maker, words):
    page = HOME.replace(WORDS, words)
    return ctx_maker(handler=lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"}))


@pytest.mark.parametrize("sentence", [
    "Our inventory platform for retailers is deployed by leading systems integrators across the United States.",
    "We partner with leading systems integrators.", "Our platform is sold through system integrators.", "We work with an AI agency.",
    "Why it services company fleets.", "Deployed by a leading systems integrator.", "Trusted by a leading IT services provider, and by banks.",
    "Our customer is a systems integrator.",
])
def test_a_company_that_only_mentions_these_firms_is_not_one(sentence):
    assert competitor_evidence(sentence) is None


def test_a_product_company_that_mentions_integrators_stays_a_prospect(make_ctx):
    ctx = site_saying(make_ctx, "Our inventory platform for retailers is deployed by leading systems integrators across the United States.")
    cid, [lid] = lead_on_file(ctx, "mentions")
    read_site_and_score(ctx, cid)
    assert not flagged(ctx, cid) and row(ctx, lid)[0] != "disqualified"


@pytest.mark.parametrize("sentence", [
    "Your nearshore partner in Latin America for growing United States companies.", "Top nearshore company in Mexico.", "Premier nearshore firm.",
    "Nearshore agency for US startups.", "Nearshore IT services.", "Nearshore services for product teams.", "Offshore development partner for startups.",
])
def test_plain_selling_wording_flags_in_a_post_and_on_a_site(sentence):
    assert competitor_in_post(sentence) and competitor_evidence(sentence)


@pytest.mark.parametrize("sentence", [
    "Offshore development team in India for growing companies.", "Nearshore teams.", "Your partner for software engineering services across the United States.",
    "We help startups with software engineering services.",
])
def test_plain_selling_wording_flags_on_a_site_only(sentence):
    assert competitor_evidence(sentence) and competitor_in_post(sentence) is None


def test_a_seller_in_plain_words_does_not_reach_a_draft(make_ctx):
    ctx = site_saying(make_ctx, "Your nearshore partner in Latin America for growing United States companies and their engineering needs.")
    cid, [lid] = lead_on_file(ctx, "plainseller")
    read_site_and_score(ctx, cid)
    assert flagged(ctx, cid) and row(ctx, lid)[0] == "disqualified"


@pytest.mark.parametrize("sentence", [
    "We work with a nearshore partner in Mexico.", "You will coordinate with our nearshore partner.", "We are not a nearshore company.",
    "Experience at an offshore development partner helps.", "Tools for managing nearshore teams.", "A chat app for offshore development teams.",
    "Time tracking for every software engineering company.", "Our offshore development team is in Vietnam.",
])
def test_a_company_that_uses_or_serves_such_a_firm_is_not_one(sentence):
    assert competitor_evidence(sentence) is None
