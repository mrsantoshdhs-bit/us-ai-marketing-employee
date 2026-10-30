"""UAT: the 10 agents and automation ownership, on synthetic data only (temp database, mocked HTTP, fake mail servers).

test_ok_*      : a behaviour that was checked and holds.
test_defect_*  : asserts the correct behaviour and FAILS on the current code; each one is a reported defect.
"""

from __future__ import annotations

import csv
import imaplib
import smtplib
import socket
import threading
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

import aime.agents.crm as crm_module
from aime.agents import (AGENTS, AnalyticsReportingAgent, AuthorizedPublishingAgent, CompanyResearchAgent,
                         CRMManagementAgent, InboxAgent, LeadQualificationAgent, LinkedInContentAgent,
                         MarketingManagerAgent, SalesWorkflowAgent, USBusinessResearchAgent)
from aime.ai_drafts import opening
from aime.crm_ops import is_suppressed
from aime.db import kv_set
from aime.inbound import record_inquiry
from aime.inbox import Mailbox
from aime.integrations.email_smtp import EmailSender
from aime.llm.ollama import OllamaProvider
from aime.models import (AgentRun, Alert, Campaign, Company, Contact, ContentItem, FollowUp, InboundEmail, JobRun,
                         JobState, Lead, MarketingPlan, Opportunity, Report, Signal)
from aime.monitoring import check_health
from aime.sample_data import SAMPLE_INBOUND
from aime.scheduler.runner import FileLock, is_due, load_job_specs, run_job, tick
from aime.timeutil import local_tz, utcnow
from aime.workflows import JOBS, job_inbound
from aime import mail_accounts as ma
from tests.conftest import hn_handler
from tests.test_company_research import SITE, add_company, make_handler
from tests.test_contact_validation import FakeChecker
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import MAIL, FakeIMAP, mail
from tests.test_outreach import SENDING, approve_all, drafts, lead_of, researched
from tests.test_publishing import AUTH, add_post, add_token, linkedin_api


# --------------------------------------------------------------------------- safety and helpers

@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Nothing in this file may reach a real server, DNS included."""
    def refuse(*a, **k):
        raise AssertionError(f"real network attempted: {a}")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", refuse)
    monkeypatch.setattr("aime.agents.company_research.MailDomainChecker", lambda: FakeChecker({}))
    FakeSMTP.sent, FakeIMAP.messages = [], {}


def ollama(handler_or_answer, prompt_tokens=12, completion_tokens=5) -> OllamaProvider:
    def default(request):
        return httpx.Response(200, json={"response": handler_or_answer, "prompt_eval_count": prompt_tokens,
                                         "eval_count": completion_tokens})
    handler = handler_or_answer if callable(handler_or_answer) else default
    return OllamaProvider("http://127.0.0.1:11434", "llama3.2", transport=httpx.MockTransport(handler))


def use_llm(ctx, provider) -> None:
    ctx._llm, ctx._llm_checked = provider, True


def run_row(ctx, res) -> AgentRun:
    with ctx.db.session() as s:
        return s.execute(select(AgentRun).where(AgentRun.run_id == res.run_id)).scalar_one()


def count(ctx, model, *conds) -> int:
    with ctx.db.session() as s:
        return s.execute(select(func.count()).select_from(model).where(*conds)).scalar_one()


def chain_handler(requests=None, down_hosts=()):
    site = make_handler(requests if requests is not None else [])

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host in (SITE, "data.sec.gov", "www.sec.gov"):
            return site(request)
        return hn_handler(request)
    return handle


def hn_answers(status=200, text="x", exc=None):
    """HN's API answers with `status`/`text` (or raises `exc`); everything else stays the normal mock."""
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hn.algolia.com":
            if exc:
                raise exc("boom", request=request)
            return httpx.Response(status, text=text)
        return hn_handler(request)
    return handle


def write_csv(ctx, name="Brightline Logistics", website=f"https://{SITE}", state="OH") -> None:
    ctx.settings.imports_dir.mkdir(parents=True, exist_ok=True)
    with (ctx.settings.imports_dir / "owner-list.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "website", "state", "industry", "notes"])
        w.writerow([name, website, state, "saas", "Dispatchers re-key orders from email into spreadsheets; they want "
                    "to automate manual data entry and replace spreadsheets with a CRM"])


def ingest_samples(ctx) -> None:
    with ctx.db.session() as s:
        for rec in SAMPLE_INBOUND:
            record_inquiry(s, rec, "web_form")


def locked_db():
    return OperationalError("UPDATE x", {}, Exception("database is locked"))


# --------------------------------------------------------------------------- schedule and ownership

def test_ok_schedule_and_job_registry_match(ctx):
    assert set(ctx.settings.schedule["jobs"]) == set(JOBS)
    assert {sp.name for sp in load_job_specs(ctx.settings)} == set(JOBS)
    assert all(sp.enabled for sp in load_job_specs(ctx.settings))


def test_ok_one_tick_runs_every_agent_and_a_second_tick_runs_nothing(make_ctx, monkeypatch):
    monkeypatch.setattr("aime.workflows.run_checks", lambda c: [])  # doctor reads the OS scheduler: not needed here
    ctx = make_ctx(handler=chain_handler())
    write_csv(ctx)
    ingest_samples(ctx)
    first = tick(ctx, trigger="uat")
    assert {r["job"] for r in first["ran"]} == set(JOBS)
    assert [r for r in first["ran"] if r["status"] != "success"] == []
    with ctx.db.session() as s:
        rows = s.execute(select(AgentRun)).scalars().all()
        assert {r.agent for r in rows} == set(AGENTS)  # no agent is left out of the schedule
        assert all(r.job_run_id is not None and r.job in JOBS and r.status != "running" for r in rows)
        by_job = {j.job: {st["agent"] for st in (j.detail or {}).get("steps", [])}
                  for j in s.execute(select(JobRun)).scalars()}
    no_agent = {job for job, steps in by_job.items() if not steps & set(AGENTS)}
    assert no_agent == {"db_maintenance", "monitor"}  # housekeeping, intended
    assert tick(ctx, trigger="uat")["ran"] == []  # nothing runs twice for the same slot or interval


def test_ok_job_lock_stops_a_second_copy_of_the_same_job(ctx):
    lock = FileLock(ctx.settings.locks_dir / "job_plan.lock")
    assert lock.acquire()
    try:
        res = run_job(ctx, "plan")
    finally:
        lock.release()
    assert res["status"] == "skipped" and res["reason"] == "already running"
    assert count(ctx, MarketingPlan) == 0


def test_ok_stopped_automation_holds_jobs_but_not_health(make_ctx, monkeypatch):
    monkeypatch.setattr("aime.workflows.run_checks", lambda c: [])
    ctx = make_ctx()
    with ctx.db.session() as s:
        kv_set(s, "automation.state", "stopped")
    assert run_job(ctx, "research")["status"] == "skipped" and count(ctx, AgentRun) == 0
    assert run_job(ctx, "monitor")["status"] == "success"


def test_ok_failed_daily_job_is_retried_alerted_and_recovers(make_ctx, monkeypatch):
    ctx = make_ctx()

    def broken(self, inp, session):
        raise ValueError("report template broke")
    original = AnalyticsReportingAgent.execute
    monkeypatch.setattr(AnalyticsReportingAgent, "execute", broken)
    assert run_job(ctx, "daily_report")["status"] == "failed"
    spec = next(sp for sp in load_job_specs(ctx.settings) if sp.name == "daily_report")
    tz = local_tz(ctx.settings.schedule.get("timezone"))
    with ctx.db.session() as s:
        st = s.get(JobState, "daily_report")
        assert st.consecutive_failures == 1
        assert is_due(spec, st, utcnow(), tz)[0] is False  # retry backoff
        assert is_due(spec, st, utcnow() + timedelta(minutes=31), tz)[0] is True
    assert run_job(ctx, "daily_report")["status"] == "failed"
    with ctx.db.session() as s:
        check_health(s, ctx.settings)
    assert count(ctx, Alert, Alert.key == "job_failing:daily_report") == 1
    monkeypatch.setattr(AnalyticsReportingAgent, "execute", original)
    assert run_job(ctx, "daily_report")["status"] == "success"
    with ctx.db.session() as s:
        assert s.get(JobState, "daily_report").consecutive_failures == 0
        check_health(s, ctx.settings)
    with ctx.db.session() as s:
        alert = s.execute(select(Alert).where(Alert.key == "job_failing:daily_report")).scalar_one()
        assert alert.resolved_at is not None


# --------------------------------------------------------------------------- the switch, runs, retries (base class)

@pytest.mark.parametrize("name", sorted(AGENTS))
def test_ok_switch_off_each_agent_records_a_skip_and_does_nothing(make_ctx, name):
    ctx = make_ctx()
    with ctx.db.session() as s:
        kv_set(s, "agents.disabled", {"names": [name]})
    res = AGENTS[name](ctx).run({})
    assert res.status == "skipped" and not res.ok and res.attempts == 0
    assert run_row(ctx, res).status == "skipped"
    for model in (MarketingPlan, Company, ContentItem, Report):
        assert count(ctx, model) == 0


def test_ok_switched_off_agent_does_not_fail_the_job_and_can_be_switched_back_on(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        kv_set(s, "agents.disabled", {"names": ["linkedin_content"]})
    assert run_job(ctx, "content")["status"] == "success"
    assert count(ctx, ContentItem) == 0
    with ctx.db.session() as s:
        kv_set(s, "agents.disabled", {"names": []})
    assert LinkedInContentAgent(ctx).run({}).output.created


def test_ok_invalid_input_is_recorded_as_failed(ctx):
    res = USBusinessResearchAgent(ctx).run({"limit": -5})
    assert not res.ok and res.attempts == 0 and "Invalid input" in res.error
    assert run_row(ctx, res).status == "failed"


def test_ok_real_agent_retries_a_locked_database_without_partial_state(make_ctx, monkeypatch):
    ctx = make_ctx()
    original, calls = MarketingManagerAgent.execute, []

    def flaky(self, inp, session):
        calls.append(1)
        out = original(self, inp, session)
        if len(calls) == 1:
            raise locked_db()  # after its writes: they must roll back
        return out
    monkeypatch.setattr(MarketingManagerAgent, "execute", flaky)
    res = MarketingManagerAgent(ctx).run({})
    assert res.ok and res.attempts == 2 and run_row(ctx, res).attempts == 2
    assert count(ctx, MarketingPlan) == 1 and count(ctx, Campaign) == 1


# --------------------------------------------------------------------------- handoffs

def test_ok_real_handoff_plan_research_enrich_qualify_sales(make_ctx):
    ctx = make_ctx(handler=chain_handler(), company_postal_address="1 Sample St, Austin, TX 78701")
    write_csv(ctx)
    for job in ("plan", "research", "enrich", "followups"):
        assert run_job(ctx, job, trigger="uat")["status"] == "success", job
    with ctx.db.session() as s:
        company = s.execute(select(Company).where(Company.domain == SITE)).scalar_one()
        assert company.website_status == "verified" and company.research_status == "done"
        lead = s.execute(select(Lead).where(Lead.company_id == company.id)).scalar_one()
        assert lead.status == "qualified" and lead.grade in ("A", "B"), lead.score_breakdown
        assert lead.contact.email == f"david.chen@{SITE}"  # found by company_research, used by sales_workflow
        draft = s.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.kind == "outreach")).scalar_one()
        assert draft.status == "awaiting_approval" and draft.approved_at is None and draft.evidence
        steps = {(r.job, r.agent) for r in s.execute(select(AgentRun)).scalars()}
    assert {("research", "research"), ("research", "crm"), ("research", "qualification"), ("research", "sales_workflow"),
            ("enrich", "company_research"), ("enrich", "qualification"), ("followups", "sales_workflow")} <= steps


def test_ok_inbound_handoff_crm_qualification_sales(ctx):
    ingest_samples(ctx)
    out = job_inbound(ctx)
    assert not out.failed and [st["agent"] for st in out.steps] == ["crm", "qualification", "sales_workflow"]
    assert count(ctx, Lead, Lead.status == "qualified", Lead.source == "inbound") == 2
    assert count(ctx, Opportunity) == 2
    assert count(ctx, FollowUp, FollowUp.channel == "email", FollowUp.status == "awaiting_approval") >= 2
    before = (count(ctx, FollowUp), count(ctx, Opportunity))
    job_inbound(ctx)
    assert (count(ctx, FollowUp), count(ctx, Opportunity)) == before


def test_defect_inbound_handoff_is_lost_when_the_crm_step_is_retried(make_ctx, monkeypatch):
    """The CRM agent commits the new leads, then a locked database during website checks makes it retry. The retry
    finds no pending inquiry, so its output has no lead ids and job_inbound runs neither Qualification nor Sales."""
    ctx = make_ctx()
    ingest_samples(ctx)
    real, calls = crm_module.check_website, []

    def locked_once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise locked_db()
        return real(*a, **k)
    monkeypatch.setattr(crm_module, "check_website", locked_once)
    out = job_inbound(ctx)
    assert count(ctx, Lead, Lead.source == "inbound") == 3, out.steps  # the CRM step did its work
    assert count(ctx, Lead, Lead.source == "inbound", Lead.last_scored_at.is_(None)) == 0, \
        f"inquirers left unscored and unanswered; steps: {[st['agent'] for st in out.steps]}"


def test_ok_inbound_leads_missed_by_their_job_are_picked_up_by_the_daily_jobs(make_ctx, monkeypatch):
    """How far the lost handoff above reaches: the next crm_maintenance (daily) scores them, the next followups drafts."""
    ctx = make_ctx()
    ingest_samples(ctx)
    real, calls = crm_module.check_website, []

    def locked_once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise locked_db()
        return real(*a, **k)
    monkeypatch.setattr(crm_module, "check_website", locked_once)
    job_inbound(ctx)
    assert count(ctx, FollowUp) == 0
    job_inbound(ctx)  # the next half-hourly run does not help
    assert count(ctx, FollowUp) == 0
    assert run_job(ctx, "crm_maintenance")["status"] == "success"
    assert run_job(ctx, "followups")["status"] == "success"
    assert count(ctx, Lead, Lead.source == "inbound", Lead.last_scored_at.is_(None)) == 0
    assert count(ctx, FollowUp, FollowUp.channel == "email", FollowUp.status == "awaiting_approval") >= 2


# --------------------------------------------------------------------------- research agent

def test_ok_research_run_twice_adds_nothing_new(ctx):
    first = USBusinessResearchAgent(ctx).run({})
    assert first.ok and first.output.companies_created > 0 and first.output.leads_created > 0
    counts = (count(ctx, Company), count(ctx, Lead), count(ctx, Signal))
    second = USBusinessResearchAgent(ctx).run({})
    assert second.ok and second.output.companies_created == 0
    assert (count(ctx, Company), count(ctx, Lead), count(ctx, Signal)) == counts
    assert run_row(ctx, second).status == "success"


def test_ok_research_network_failure_of_the_only_source_is_retried(make_ctx):
    ctx = make_ctx(handler=hn_answers(exc=httpx.ConnectError))
    res = USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring"]})
    assert not res.ok and res.attempts == 3 and run_row(ctx, res).attempts == 3


def test_defect_scheduled_research_job_reports_success_when_the_network_is_down(make_ctx):
    """In production no `sources` are named, so the CSV source (always "configured") is in the count and
    `transport_failures == configured_count` can never be true: no retry, no failure, 0 companies, "success"."""
    ctx = make_ctx(handler=hn_answers(exc=httpx.ConnectError))
    res = USBusinessResearchAgent(ctx).run({})
    assert count(ctx, Company) == 0
    assert not res.ok and res.attempts == 3, f"status={res.status} attempts={res.attempts}"


def test_defect_scheduled_research_job_reports_success_when_every_source_answers_500(make_ctx):
    """Every source that could find companies failed; the job records success and no alert is raised."""
    ctx = make_ctx(handler=hn_answers(status=500))
    result = run_job(ctx, "research")
    assert count(ctx, Company) == 0
    with ctx.db.session() as s:
        state = s.get(JobState, "research")
        alerts = s.execute(select(Alert)).scalars().all()
    assert result["status"] != "success" or alerts, f"job status={result['status']}, last_success_at={state.last_success_at}"


def test_defect_malformed_json_from_one_source_stops_the_other_sources(make_ctx):
    """HTTP errors are isolated per source; a 200 with a non-JSON body is not, so the owner's own CSV list waits too."""
    ctx = make_ctx(handler=hn_answers(status=200, text="<html>bad gateway page</html>"))
    write_csv(ctx, name="Northwind Analytics", website="https://northwind-analytics.example", state="TX")
    res = USBusinessResearchAgent(ctx).run({})
    assert count(ctx, Company, Company.source == "csv_import") == 1, f"status={res.status} error={res.error}"


# --------------------------------------------------------------------------- company research agent

def test_ok_company_research_names_people_only_with_evidence(make_ctx):
    ctx = make_ctx(handler=make_handler([]))
    cid = add_company(ctx)
    res = CompanyResearchAgent(ctx).run({"company_ids": [cid]})
    assert res.ok and res.output.companies_researched == 1 and run_row(ctx, res).status == "success"
    with ctx.db.session() as s:
        people = s.execute(select(Contact).where(Contact.company_id == cid, Contact.last_name.is_not(None))).scalars().all()
        assert people and all(p.source in ("website", "sec_filing") for p in people)
        assert "Secret Person" not in {f"{p.first_name} {p.last_name}" for p in people}  # robots.txt honoured


def test_ok_company_research_does_not_invent_people(make_ctx):
    empty = "<html><head><title>Brightline Logistics</title></head><body><p>We move freight.</p></body></html>"
    gone = ("text/html", 404)
    ctx = make_ctx(handler=make_handler([], {"/": ("text/html", empty), "/team": gone, "/about-us": gone, "/contact": gone}))
    cid = add_company(ctx)
    res = CompanyResearchAgent(ctx).run({"company_ids": [cid]})
    assert res.ok and res.output.no_people == [cid] and res.output.contacts_created == 0
    assert count(ctx, Contact) == 0


def test_ok_company_research_one_failure_does_not_stop_the_batch(make_ctx, monkeypatch):
    ctx = make_ctx(handler=make_handler([]))
    bad = add_company(ctx, name="Broken Co", domain="broken-co.example")
    good = add_company(ctx)
    import aime.agents.company_research as cr
    real = cr.extract_people

    def crash_for_broken(pages, name):
        if name == "Broken Co":
            raise RuntimeError("parser crashed")
        return real(pages, name)
    monkeypatch.setattr(cr, "extract_people", crash_for_broken)
    res = CompanyResearchAgent(ctx).run({"company_ids": [bad, good]})
    assert res.ok and [f.id for f in res.output.failed] == [bad]
    with ctx.db.session() as s:
        assert s.get(Company, bad).research_status == "failed" and s.get(Company, good).research_status != "failed"


def _site_down_ctx(make_ctx):
    return make_ctx(handler=make_handler([], {"/": ("text/html", 500)}))


def test_defect_company_research_counts_an_unread_site_as_researched_and_not_failed(make_ctx):
    """The homepage answered 500, nothing was read and research_status is "failed", yet the run says 1 researched,
    0 failed; job_enrich and the run log show a success that did nothing."""
    ctx = _site_down_ctx(make_ctx)
    cid = add_company(ctx)
    out = CompanyResearchAgent(ctx).run({"company_ids": [cid]}).output
    with ctx.db.session() as s:
        assert s.get(Company, cid).research_status == "failed" and out.pages_read == 0
    assert out.companies_researched == 0 and [f.id for f in out.failed] == [cid], \
        f"researched={out.companies_researched} failed={out.failed}"


def test_defect_company_research_does_not_retry_after_a_passing_failure(make_ctx):
    """A one-off 500 stamps researched_at, so the scheduled job does not look at the company for 30 days (recheck_days)."""
    down = {"on": True}
    up = make_handler([])

    def handler(request):
        if request.url.host == SITE and request.url.path == "/" and down["on"]:
            return httpx.Response(500)
        return up(request)
    ctx = make_ctx(handler=handler)
    cid = add_company(ctx)
    assert CompanyResearchAgent(ctx).run({}).ok
    down["on"] = False
    res = CompanyResearchAgent(ctx).run({})  # what the next day's enrich job does
    with ctx.db.session() as s:
        status = s.get(Company, cid).research_status
    assert res.output.companies_researched == 1 and status == "done", f"status stays {status!r}"


def test_defect_report_counts_unread_sites_as_companies_researched(make_ctx):
    ctx = _site_down_ctx(make_ctx)
    add_company(ctx)
    assert CompanyResearchAgent(ctx).run({}).ok
    kpis = AnalyticsReportingAgent(ctx).run({}).output.kpis
    assert kpis["companies_researched_total"] == 0, "the report says a company was researched; none was read"


# --------------------------------------------------------------------------- qualification agent

def test_ok_qualification_scores_evidence_only_and_is_idempotent(ctx):
    with ctx.db.session() as s:
        c = Company(name="Plain Co", normalized_name="plain co", domain="plain.example", source="hn_hiring", state="TX",
                    country="US")
        s.add(c)
        s.flush()
        s.add(Lead(company_id=c.id, source="research", status="new"))
    res = LeadQualificationAgent(ctx).run({})
    assert res.ok and res.output.scored == 1
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalar_one()
        assert lead.intent_score == 0 and lead.status == "nurture" and lead.score_breakdown
    assert LeadQualificationAgent(ctx).run({}).output.scored == 0  # nothing changed: nothing re-scored


def test_ok_qualification_disqualifies_competitor_and_clears_its_queue(ctx):
    cid = add_company(ctx, is_competitor=True)
    with ctx.db.session() as s:
        lead = s.execute(select(Lead).where(Lead.company_id == cid)).scalar_one()
        s.add(FollowUp(lead_id=lead.id, company_id=cid, channel="email", step=1, due_at=utcnow(),
                       status="awaiting_approval", subject="x", body="y"))
    assert LeadQualificationAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        assert s.execute(select(Lead)).scalar_one().status == "disqualified"
        assert s.execute(select(FollowUp)).scalar_one().status == "cancelled"


# --------------------------------------------------------------------------- CRM agent

def test_ok_crm_ingest_twice_makes_one_lead_per_inquiry(ctx):
    ingest_samples(ctx)
    first = CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]})
    assert first.ok and first.output.inbound_processed == 3 and first.output.inbound_rejected == 1
    leads = count(ctx, Lead)
    ingest_samples(ctx)
    second = CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]})
    assert second.ok and second.output.inbound_processed == 0 and count(ctx, Lead) == leads


def test_ok_crm_bad_inbox_file_is_rejected_with_an_alert(ctx):
    ctx.settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    (ctx.settings.inbox_dir / "broken.json").write_text("{not json", encoding="utf-8")
    res = CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]})
    assert res.ok and res.output.inbound_files_rejected
    assert count(ctx, Alert, Alert.source == "crm") == 1


def test_defect_crm_reports_zero_merged_after_a_redirect_merge(make_ctx):
    """_verify_websites counts a merge in out.merged; the dedupe step then overwrites it with its own count."""
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "old-acme.example":
            return httpx.Response(301, headers={"location": "https://acme-widgets.example/"})
        return httpx.Response(200, text="<html><head><title>Acme Widgets</title></head></html>",
                              headers={"content-type": "text/html"})
    ctx = make_ctx(handler=handler)
    with ctx.db.session() as s:
        keeper = Company(name="Acme Widgets", normalized_name="acme widgets", domain="acme-widgets.example",
                         website_checked_at=utcnow(), source="hn_hiring", state="CA")
        old = Company(name="Acme Widgets", normalized_name="acme widgets", domain="old-acme.example", source="hn_hiring",
                      state="TX")
        s.add_all([keeper, old])
        s.flush()
        old_id, keeper_id = old.id, keeper.id
    res = CRMManagementAgent(ctx).run({"operations": ["verify_websites", "dedupe"]})
    with ctx.db.session() as s:
        assert s.get(Company, old_id).merged_into_id == keeper_id  # the merge really happened
    assert res.output.merged == 1, f"merged reported as {res.output.merged}"


# --------------------------------------------------------------------------- sales workflow agent

def test_ok_sales_run_twice_makes_one_draft_and_sends_nothing_unapproved(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    for expected in (1, 0):
        res = SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
        assert res.ok and res.output.outreach_drafts == expected
    assert FakeSMTP.sent == [] and len(drafts(ctx)) == 1 and drafts(ctx)[0].status == "awaiting_approval"
    assert run_row(ctx, res).agent == "sales_workflow"


def test_ok_sales_sends_each_approved_email_once(make_ctx):
    ctx = researched(make_ctx, **SENDING)
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    for _ in range(2):
        assert SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()}).ok
    assert len(FakeSMTP.sent) == 1 and drafts(ctx)[0].status == "sent"


def test_ok_sales_outbound_off_sends_nothing_even_when_approved(make_ctx):
    ctx = researched(make_ctx, **{**SENDING, "outbound_email_enabled": False})
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    res = SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert res.ok and res.output.emails_sent == 0 and FakeSMTP.sent == [] and res.output.blocked


def test_ok_sales_mid_send_timeout_is_uncertain_alerted_and_never_resent(make_ctx):
    class TimesOut(FakeSMTP):
        attempts = 0

        def send_message(self, msg):
            TimesOut.attempts += 1
            raise TimeoutError("timed out")
    ctx = researched(make_ctx, **SENDING)
    sender = EmailSender(ctx.settings, smtp_factory=TimesOut)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    for _ in range(2):
        SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert TimesOut.attempts == 1 and drafts(ctx)[0].status == "uncertain"
    assert count(ctx, Alert, Alert.source == "sales_workflow") == 1


def test_defect_sales_mail_login_failure_reports_success_with_no_alert(make_ctx):
    """A rejected mailbox password leaves every approved email unsent; the agent and job say success, no alert is
    raised (a mid-send timeout does raise one) and the same failure repeats every hour."""
    class BadLogin(FakeSMTP):
        def login(self, u, p):
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
    ctx = researched(make_ctx, **SENDING, smtp_username="sales@us.example", smtp_password="x" * 4)
    sender = EmailSender(ctx.settings, smtp_factory=BadLogin)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    res = SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert drafts(ctx)[0].status == "approved" and "Send failed" in drafts(ctx)[0].blocked_reason
    assert not res.ok or count(ctx, Alert) > 0, f"status={res.status}, emails_sent={res.output.emails_sent}, no alert"


def test_defect_two_overlapping_sales_runs_draft_the_same_lead_twice(make_ctx, monkeypatch):
    """research, inbound and followups each run this agent under their own job lock, so a dashboard "Run now" can overlap
    the scheduler. Both runs pass the "no email yet" check before either writes (forced here with a barrier on the real
    code path), there is no unique constraint and no guard, and the owner gets two first-touch drafts for one person."""
    ctx = researched(make_ctx, **SENDING)
    barrier = threading.Barrier(2, timeout=15)
    real = SalesWorkflowAgent._new_outreach

    def both_past_the_check(self, *a, **k):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:  # a fixed version serializes the runs: the second never gets here in time
            pass
        return real(self, *a, **k)
    monkeypatch.setattr(SalesWorkflowAgent, "_new_outreach", both_past_the_check)
    results = []

    def run():
        results.append(SalesWorkflowAgent(ctx).run({"now": TUESDAY_11AM_ET.isoformat()}))
    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert len(results) == 2
    assert count(ctx, FollowUp, FollowUp.kind == "outreach") == 1, \
        f"{count(ctx, FollowUp, FollowUp.kind == 'outreach')} first-touch drafts for one lead; {[r.status for r in results]}"


# --------------------------------------------------------------------------- inbox agent

def inbox_agent(ctx, imap=FakeIMAP):
    return InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=imap))


def test_ok_inbox_run_twice_stores_each_email_once(make_ctx):
    ctx = make_ctx(inbox_scope="all", **MAIL)
    FakeIMAP.messages = {1: mail("Pat <pat@prospect.example>", "Hello", "Please unsubscribe me"),
                         2: mail("Pat <pat@prospect.example>", "Hello", "Please unsubscribe me")}  # same Message-ID
    first = inbox_agent(ctx).run({})
    second = inbox_agent(ctx).run({})
    assert first.ok and second.ok and first.output.stored == 1 and second.output.stored == 0
    with ctx.db.session() as s:
        row = s.execute(select(InboundEmail)).scalar_one()
        assert row.category == "unsubscribe" and is_suppressed(s, "pat@prospect.example")
    assert run_row(ctx, first).agent == "inbox_monitor"


def test_ok_inbox_mail_server_failure_is_retried_reported_and_recovers(make_ctx):
    class Down(FakeIMAP):
        logins = 0

        def login(self, user, password):
            Down.logins += 1
            raise OSError("connection reset")
    ctx = make_ctx(inbox_scope="all", **MAIL)
    FakeIMAP.messages = {1: mail("Pat <pat@prospect.example>", "Hello", "Please unsubscribe me")}
    res = inbox_agent(ctx, Down).run({})
    assert not res.ok and res.attempts == 2 and Down.logins == 2 and run_row(ctx, res).status == "failed"
    assert inbox_agent(ctx).run({}).output.stored == 1


def test_ok_inbox_without_a_mailbox_says_so(ctx):
    res = InboxAgent(ctx).run({})
    assert res.ok and res.output.mailbox == "not configured" and res.output.fetched == 0  # documented in its docstring


def test_ok_inbox_model_junk_is_ignored_and_its_reading_never_acts(make_ctx):
    ctx = make_ctx(inbox_scope="all", **MAIL)
    use_llm(ctx, ollama("I think this person might be very keen!"))
    FakeIMAP.messages = {1: mail("Pat <pat@prospect.example>", "Hello", "Who are you and what do you do?")}
    res = inbox_agent(ctx).run({})
    with ctx.db.session() as s:
        row = s.execute(select(InboundEmail)).scalar_one()
        assert row.category == "question" and not any("AI" in r for r in row.reasons)
    run = run_row(ctx, res)
    assert (run.llm_calls, run.llm_tokens) == (1, 17)

    ctx2 = make_ctx(inbox_scope="all", **MAIL)
    use_llm(ctx2, ollama("interested|yes"))
    assert inbox_agent(ctx2).run({}).ok
    with ctx2.db.session() as s:
        row = s.execute(select(InboundEmail)).scalar_one()
        assert row.category == "question" and row.status == "needs_review"  # a suggestion, not an action
        assert s.execute(select(func.count(Opportunity.id))).scalar_one() == 0
        assert s.execute(select(Alert).where(Alert.message.like("%Possible interest%"))).scalars().first()


def test_defect_one_broken_mailbox_stops_replies_from_the_working_one_being_handled(make_ctx):
    """Reading is all-or-nothing across panel mailboxes: while one cannot log in, process_pending is never reached, so
    an unsubscribe already fetched from a working mailbox stays unhandled ("new", address not suppressed)."""
    class OneBad(FakeIMAP):
        def login(self, user, password):
            if user == "bad@us.example":
                raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
            return "OK", [b"ok"]
    ctx = make_ctx(inbox_scope="all")
    with ctx.db.session() as s:
        for address in ("good@us.example", "bad@us.example"):
            account = ma.save(s, "owner", {"provider": "gmail", "label": address, "email": address, "secret": "a" * 16})
            ma.change(s, "owner", account.id, "enable")
            ma.change(s, "owner", account.id, "read_on")
    FakeIMAP.messages = {1: mail("Pat <pat@prospect.example>", "Stop", "Please unsubscribe me")}
    res = InboxAgent(ctx, imap_factory=OneBad).run({})
    assert not res.ok  # the broken mailbox is reported, as it should be
    with ctx.db.session() as s:
        row = s.execute(select(InboundEmail)).scalar_one()  # the good mailbox was read and committed
        assert row.status != "new" and is_suppressed(s, "pat@prospect.example"), \
            f"unsubscribe left status={row.status!r}, address not suppressed"


# --------------------------------------------------------------------------- content agent and the model

BAD_POST = ("Studies show our clients increased revenue by 73% after we helped them.\n\n"
            "We have helped over 500 companies. Our team guarantees results for every customer, and we are the "
            "number one provider. " * 3 + "\n\nWhat do you think?")


def test_ok_content_rejects_hallucinated_model_post_and_records_usage(make_ctx):
    ctx = make_ctx(llm_cost_per_1k_tokens=2.0)
    use_llm(ctx, ollama(BAD_POST, 1200, 300))
    res = LinkedInContentAgent(ctx).run({"research_topics": False})
    assert res.ok and res.output.created and res.output.llm_rejections >= 1
    with ctx.db.session() as s:
        for item in s.execute(select(ContentItem)).scalars():
            assert item.generator == "template" and "73%" not in item.body and item.status == "awaiting_approval"
    run = run_row(ctx, res)
    assert run.llm_calls >= 2 and run.llm_tokens == run.llm_calls * 1500
    assert run.llm_cost_usd == pytest.approx(run.llm_tokens / 1000 * 2.0) and run.llm_ms >= 0


def test_ok_content_model_down_falls_back_and_counts_the_failed_calls(make_ctx):
    ctx = make_ctx()
    use_llm(ctx, ollama(lambda request: httpx.Response(500, text="model crashed")))
    res = LinkedInContentAgent(ctx).run({"research_topics": False})
    assert res.ok and res.output.created and set(res.output.generator_counts) == {"template"}
    assert run_row(ctx, res).llm_calls == len(res.output.created)


def test_ok_usage_is_recorded_through_a_scheduled_job(make_ctx):
    def handler(request):
        if request.url.host == "127.0.0.1" and request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3.2:latest"}]})
        if request.url.host == "127.0.0.1" and request.url.path == "/api/generate":
            return httpx.Response(200, json={"response": BAD_POST, "prompt_eval_count": 10, "eval_count": 10})
        return hn_handler(request)
    ctx = make_ctx(handler=handler, llm_provider="ollama")
    assert run_job(ctx, "content")["status"] == "success"
    with ctx.db.session() as s:
        run = s.execute(select(AgentRun).where(AgentRun.agent == "linkedin_content")).scalar_one()
        assert run.llm_calls > 0 and run.llm_tokens == run.llm_calls * 20 and run.job == "content"


def test_ok_content_queue_is_bounded_when_run_repeatedly(ctx):
    created = []
    for _ in range(5):
        res = LinkedInContentAgent(ctx).run({"research_topics": False})
        assert res.ok
        created += res.output.created
    assert len(created) == len(set(created)) and count(ctx, ContentItem) <= 9
    with ctx.db.session() as s:
        items = s.execute(select(ContentItem)).scalars().all()
    assert len({i.fingerprint for i in items}) == len(items)
    assert len({i.scheduled_for for i in items}) == len(items) and {i.status for i in items} == {"awaiting_approval"}


def test_ok_content_survives_a_topic_research_http_error(make_ctx):
    ctx = make_ctx(handler=hn_answers(status=500))
    res = LinkedInContentAgent(ctx).run({})
    assert res.ok and res.output.created and res.output.topic_research_error


def test_defect_malformed_json_from_topic_research_loses_all_posts(make_ctx):
    """Topic research is optional and an HTTP error is tolerated, but a 200 with a non-JSON body fails the whole run."""
    ctx = make_ctx(handler=hn_answers(status=200, text="<html>bad gateway page</html>"))
    res = LinkedInContentAgent(ctx).run({})
    assert res.ok and res.output.created, f"status={res.status} error={res.error}"


def test_defect_malformed_json_from_the_model_fails_the_content_agent(make_ctx):
    """OllamaProvider.generate turns only httpx errors into LLMError; a non-JSON answer escapes as ValueError, past the
    template fallback the agent promises ("Fall back to curated templates when the LLM is unavailable")."""
    ctx = make_ctx()
    use_llm(ctx, ollama(lambda request: httpx.Response(200, text="<html>proxy error</html>")))
    res = LinkedInContentAgent(ctx).run({"research_topics": False})
    assert res.ok and res.output.created, f"status={res.status} error={res.error}"


def test_defect_malformed_json_from_the_model_fails_the_sales_agent(make_ctx):
    """With AI_EMAIL_DRAFTS on, the same error aborts the sales run: no draft for anyone, and no approved email is sent."""
    ctx = make_ctx(ai_email_drafts=True)
    ingest_samples(ctx)
    ids = CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]}).output.inbound_lead_ids
    assert LeadQualificationAgent(ctx).run({"lead_ids": ids}).output.newly_qualified
    use_llm(ctx, ollama(lambda request: httpx.Response(200, text="<html>proxy error</html>")))
    res = SalesWorkflowAgent(ctx).run({"lead_ids": ids})
    assert res.ok and res.output.drafts_created, f"status={res.status} error={res.error}"


def test_ok_sales_model_opening_is_labelled_and_its_usage_recorded(make_ctx):
    good = "Thanks for telling us how your agents lose track of follow-ups between spreadsheets."
    ctx = make_ctx(ai_email_drafts=True)
    ingest_samples(ctx)
    ids = CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]}).output.inbound_lead_ids
    assert LeadQualificationAgent(ctx).run({"lead_ids": ids}).output.newly_qualified
    use_llm(ctx, ollama(good, 10, 10))
    res = SalesWorkflowAgent(ctx).run({"lead_ids": ids})
    assert res.ok and res.output.drafts_created
    with ctx.db.session() as s:
        made = s.execute(select(FollowUp).where(FollowUp.channel == "email")).scalars().all()
        assert all(f.generator.startswith("AI (ollama:") and good in f.body and f.status == "awaiting_approval" for f in made)
    run = run_row(ctx, res)
    assert run.llm_calls == len(made) and run.llm_tokens == 20 * len(made)


def test_defect_failed_model_calls_are_not_saved_with_the_run(make_ctx):
    """aime/llm/usage.py counts failures and says they are "saved with its run"; agent_runs has no such figure, so a run
    whose every model call failed looks the same as one that worked."""
    assert hasattr(AgentRun, "llm_failures")


def test_ok_model_opening_refuses_prices_links_and_two_sentences():
    for bad in ("We can do it for $5,000 with a 20% discount.", "Visit https://example.com for more.",
                "Thanks for reaching out. We will call you tomorrow.", "Hi Ann, thanks for your message."):
        assert opening(ollama(bad), "Ann", "Brightpath", "CRM", "x") is None, bad


@pytest.mark.parametrize("made_up", [
    "We will call you tomorrow to go over your project.",  # a date and a promise
    "We can have your new system running by next Friday.",  # a date and a promise
    "Thanks for asking, we have already built this for Acme and they loved the result.",  # a made-up client claim
])
def test_defect_model_opening_lets_made_up_dates_promises_and_claims_through(made_up):
    """ai_drafts.py: "It never writes prices, numbers, dates, links, promises or discounts" and uses "facts we hold";
    its check only looks for digits and a fixed word list, so these reach the draft (and a campaign can approve it)."""
    assert opening(ollama(made_up), "Ann", "Brightpath", "CRM", "we lose leads") is None


# --------------------------------------------------------------------------- marketing manager

def test_ok_marketing_manager_makes_a_plan_for_the_research_and_content_agents(ctx):
    res = MarketingManagerAgent(ctx).run({})
    assert res.ok and res.output.focus_states and res.output.focus_industries and res.output.briefs
    assert run_row(ctx, res).status == "success" and count(ctx, Campaign) == 1
    assert LinkedInContentAgent(ctx).run({"research_topics": False}).output.created
    with ctx.db.session() as s:
        assert all(i.campaign_id == res.output.campaign_id for i in s.execute(select(ContentItem)).scalars())


def test_defect_second_plan_run_on_one_day_duplicates_the_plan_and_skips_states(ctx):
    """A manual "Run now" makes a second plan for the same date and moves the state rotation on, so the first plan's
    next states are skipped. plan_date has no unique rule and the agent does not look for today's plan."""
    day = "2026-10-27"
    first = MarketingManagerAgent(ctx).run({"plan_date": day})
    second = MarketingManagerAgent(ctx).run({"plan_date": day})
    assert first.ok and second.ok
    assert count(ctx, MarketingPlan) == 1 and second.output.focus_states == first.output.focus_states, \
        f"{count(ctx, MarketingPlan)} plans; {first.output.focus_states} then {second.output.focus_states}"


# --------------------------------------------------------------------------- publishing

def test_ok_publishing_without_authorization_keeps_the_post_queued_and_says_why(make_ctx):
    ctx = make_ctx()
    cid = add_post(ctx)
    res = AuthorizedPublishingAgent(ctx).run({})
    assert res.ok and not res.output.can_publish and res.output.published == []
    with ctx.db.session() as s:
        item = s.get(ContentItem, cid)
        assert item.status == "approved" and item.publish_blocked_reason
    assert count(ctx, Alert, Alert.source == "publishing") == 1


def test_ok_publishing_needs_an_approval_record_and_publishes_once(make_ctx):
    calls = []
    ctx = make_ctx(handler=linkedin_api(calls), **AUTH)
    add_token(ctx)
    cid = add_post(ctx)
    unapproved = add_post(ctx, status="awaiting_approval")
    assert AuthorizedPublishingAgent(ctx).run({}).output.published == [cid]
    assert AuthorizedPublishingAgent(ctx).run({}).output.published == []
    assert len([c for c in calls if c.url.path == "/rest/posts"]) == 1
    with ctx.db.session() as s:
        assert s.get(ContentItem, unapproved).status == "awaiting_approval"


def test_ok_publishing_two_concurrent_runs_publish_once(make_ctx):
    calls = []
    ctx = make_ctx(handler=linkedin_api(calls), **AUTH)
    add_token(ctx)
    add_post(ctx)
    barrier, results = threading.Barrier(2, timeout=15), []

    def run():
        barrier.wait()
        results.append(AuthorizedPublishingAgent(ctx).run({}))
    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert len([c for c in calls if c.url.path == "/rest/posts"]) == 1
    assert sum(len(r.output.published) for r in results if r.output) == 1


def test_ok_publishing_500_is_uncertain_alerted_and_never_reposted(make_ctx):
    calls = []
    ctx = make_ctx(handler=linkedin_api(calls, post_status=500), **AUTH)
    add_token(ctx)
    cid = add_post(ctx)
    res = AuthorizedPublishingAgent(ctx).run({})
    assert res.ok and [u.id for u in res.output.uncertain] == [cid]
    AuthorizedPublishingAgent(ctx).run({})
    assert len([c for c in calls if c.url.path == "/rest/posts"]) == 1
    with ctx.db.session() as s:
        assert s.get(ContentItem, cid).status == "uncertain"
    assert count(ctx, Alert, Alert.level == "error") >= 1


def test_defect_publishing_malformed_image_answer_strands_the_post(make_ctx):
    """Before any post exists LinkedIn's image step answers 200 with a non-JSON body. _publish_one releases the claim only
    for LinkedInAPIError and httpx errors ("No post exists yet, so releasing the claim cannot cause a duplicate"), so
    this one escapes: the run fails and the post stays "publishing", later "uncertain", for a person to check."""
    def handler(request):
        if request.url.path == "/rest/images":
            return httpx.Response(200, text="<html>gateway</html>")
        return httpx.Response(404)
    ctx = make_ctx(handler=handler, **AUTH)
    add_token(ctx)
    cid = add_post(ctx, image=True)
    ctx.settings.assets_dir.mkdir(parents=True, exist_ok=True)
    (ctx.settings.assets_dir / "img.png").write_bytes(b"png")
    res = AuthorizedPublishingAgent(ctx).run({})
    with ctx.db.session() as s:
        status = s.get(ContentItem, cid).status
    assert status in ("approved", "failed") and res.ok, f"post left {status!r}; run {res.status}: {res.error}"


def test_ok_publishing_paused_automation_holds_due_posts(make_ctx):
    calls = []
    ctx = make_ctx(handler=linkedin_api(calls), **AUTH)
    add_token(ctx)
    add_post(ctx)
    with ctx.db.session() as s:
        kv_set(s, "automation.state", "paused")
    res = AuthorizedPublishingAgent(ctx).run({})
    assert res.ok and res.output.published == [] and calls == [] and res.output.blocked


# --------------------------------------------------------------------------- analytics

def test_ok_analytics_reports_from_records_and_is_idempotent(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        s.add(AgentRun(run_id="x1", agent="research", status="failed", started_at=utcnow(), error="boom"))
    first = AnalyticsReportingAgent(ctx).run({"report_date": "2026-10-27"})
    assert first.ok and first.output.kpis["agent_failures_period"] == 1 and first.output.kpis["companies_total"] == 0
    second = AnalyticsReportingAgent(ctx).run({"report_date": "2026-10-27"})
    assert second.output.report_id == first.output.report_id and count(ctx, Report) == 1
    assert first.output.path_md.endswith("daily_2026-10-27.md")
    with open(first.output.path_md, encoding="utf-8") as fh:
        text = fh.read()
    assert "Agent health" in text and "research" in text
    weekly = AnalyticsReportingAgent(ctx).run({"kind": "weekly", "report_date": "2026-10-27"})
    assert weekly.ok and "weekly" in weekly.output.path_html and count(ctx, Report) == 2


def test_ok_analytics_failure_is_recorded(make_ctx, monkeypatch):
    ctx = make_ctx()

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("aime.agents.analytics.import_metrics_dir", broken)
    res = AnalyticsReportingAgent(ctx).run({})
    assert not res.ok and run_row(ctx, res).status == "failed" and "disk full" in res.error
    assert count(ctx, Report) == 0
