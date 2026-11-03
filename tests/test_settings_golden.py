"""Golden equivalence for the settings control plane (Option B, Phase 1).

With default settings (US the only enabled country, no saved settings version), a day of discovery over the mocked
sources (the sample Hacker News thread, SEC EDGAR unconfigured, and a CSV list of the owner's own) followed by website
checks, company research, qualification and the sales workflow's drafts must give exactly the companies, leads, scores
and drafts that production commit 50b9172 gave. EXPECTED was recorded by running `snapshot` on that commit, before any
of the settings work; a difference here is a behaviour change at default settings, never something to re-record lightly.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from aime.agents import LeadQualificationAgent, SalesWorkflowAgent
from aime.models import Alert, Company, FollowUp, Lead
from aime.scheduler.runner import run_job

CSV = ("name,website,state,city,industry,notes,country\n"
       "Brightpath Realty Group,https://brightpath-realty.example,TX,Austin,real_estate,Leads land in three inboxes and need a CRM,US\n"
       "Pixel & Pine Agency,https://pixelpine.example,NY,Brooklyn,marketing_agencies,Manual reporting for clients,\n"
       "Maple Leaf Logistics,https://maple-leaf.example,,Toronto,smb,Dispatch runs on spreadsheets,Canada\n"
       "No Place Inc,https://noplace.example,,,saas,Subscription billing pain,\n"
       "Bad State Co,https://badstate.example,ZZ,,saas,Typo in the state,\n")


def run_a_day(ctx) -> None:
    (ctx.settings.imports_dir / "owner-list.csv").write_text(CSV, encoding="utf-8")
    for job in ("research", "enrich"):
        assert run_job(ctx, job)["status"] in ("success", "partial")
    assert LeadQualificationAgent(ctx).run({}).ok
    assert SalesWorkflowAgent(ctx).run({"send": False}).ok


def snapshot(ctx) -> dict:
    with ctx.db.session() as s:
        companies = sorted([c.name, c.country, c.state, c.location_evidence, c.industry, bool(c.is_competitor), c.website_status,
                            c.source] for c in s.execute(select(Company)).scalars())
        leads = sorted([x.company.name, x.source, x.status, x.score, x.grade, x.fit_score, x.need_score, x.intent_score, x.data_score,
                        x.disqualify_reason, x.primary_service, list(x.score_breakdown or [])] for x in s.execute(select(Lead)).scalars())
        drafts = sorted([f.lead.company.name, f.kind, f.status, f.to_email, f.subject] for f in s.execute(select(FollowUp)).scalars())
        alerts = sorted(a.message for a in s.execute(select(Alert)).scalars() if a.source == "research")
    return {"companies": companies, "leads": leads, "drafts": drafts, "alerts": alerts}


# Recorded on commit 50b9172 (production release #77) with this file's run_a_day and snapshot.
EXPECTED: dict | None = {'alerts': ['Import file partly imported: owner-list.csv: 3 of 5 row(s) left out: 3 row(s) without a US state (add a state '
            'column)'],
 'companies': [['Acme Dev Shop',
                'US',
                'TX',
                'US state TX (Hacker News hiring post)',
                'marketing_agencies',
                True,
                'verified',
                'hn_hiring'],
               ['Brightpath Realty Group',
                'US',
                'TX',
                'US state TX (your imported list)',
                'real_estate',
                False,
                'verified',
                'csv_import'],
               ['Lumen Home Loans',
                'US',
                None,
                'Hacker News hiring post: "Lumen Home Loans | Full-stack Engineer | Remote (US) | apply"',
                'real_estate',
                False,
                None,
                'hn_hiring'],
               ['Northwind Analytics',
                'US',
                'CO',
                'US state CO (Hacker News hiring post)',
                'saas',
                False,
                'verified',
                'hn_hiring'],
               ['Pixel & Pine Agency',
                'US',
                'NY',
                'US state NY (your imported list)',
                'marketing_agencies',
                False,
                'verified',
                'csv_import']],
 'drafts': [['Northwind Analytics', None, 'pending', None, 'Review Northwind Analytics']],
 'leads': [['Acme Dev Shop',
            'research',
            'disqualified',
            59,
            'B',
            30,
            18,
            6,
            5,
            'Company appears to sell software or AI work to clients (possible competitor)',
            'ai_agents',
            ['+15 fit: target industry (Marketing Agencies)',
             '+10 fit: US location verified (TX)',
             '+5 fit: company website verified',
             '+6 need: public evidence for AI Agent Development (strong)',
             '+7 need: it is hiring people to build software (work an outside team could share)',
             '+5 need: evidence observed in the last 45 days',
             '+6 intent: public hiring post (25 days old) mentions a related need (signal, not buying intent)',
             '+3 data: evidence links to its source',
             '+2 data: website confirmed live and naming the company',
             'disqualified: Company appears to sell software or AI work to clients (possible competitor)']],
           ['Brightpath Realty Group',
            'research',
            'nurture',
            45,
            'C',
            30,
            10,
            0,
            5,
            None,
            'custom_crm',
            ['+15 fit: target industry (Real Estate)',
             '+10 fit: US location verified (TX)',
             '+5 fit: company website verified',
             '+5 need: public evidence for Custom CRM Development (moderate)',
             '+5 need: evidence observed in the last 45 days',
             '+0 intent: no recorded buying signal',
             '+3 data: evidence links to its source',
             '+2 data: website confirmed live and naming the company']],
           ['Lumen Home Loans',
            'research',
            'nurture',
            55,
            'B',
            20,
            26,
            6,
            3,
            None,
            'bpa',
            ['+15 fit: target industry (Real Estate)',
             '+5 fit: US presence stated, state unknown',
             '+7 need: public evidence for Business Process Automation (strong)',
             '+7 need: public evidence for Custom CRM Development (strong)',
             '+7 need: public evidence for Custom Software Development (strong)',
             '+5 need: evidence observed in the last 45 days',
             '+6 intent: public hiring post (25 days old) mentions a related need (signal, not buying intent)',
             '+3 data: evidence links to its source',
             'Not moved to qualified: grade B today, but grade C within a month, as its newest hiring post counts for less']],
           ['Northwind Analytics',
            'research',
            'qualified',
            67,
            'B',
            30,
            26,
            6,
            5,
            None,
            'ai_agents',
            ['+15 fit: target industry (SaaS)',
             '+10 fit: US location verified (CO)',
             '+5 fit: company website verified',
             '+7 need: public evidence for AI Agent Development (strong)',
             '+7 need: public evidence for Business Process Automation (strong)',
             '+7 need: public evidence for Custom CRM Development (strong)',
             '+5 need: evidence observed in the last 45 days',
             '+6 intent: public hiring post (25 days old) mentions a related need (signal, not buying intent)',
             '+3 data: evidence links to its source',
             '+2 data: website confirmed live and naming the company']],
           ['Pixel & Pine Agency',
            'research',
            'nurture',
            35,
            'C',
            30,
            0,
            0,
            5,
            None,
            None,
            ['+15 fit: target industry (Marketing Agencies)',
             '+10 fit: US location verified (NY)',
             '+5 fit: company website verified',
             '+0 intent: no recorded buying signal',
             '+3 data: evidence links to its source',
             '+2 data: website confirmed live and naming the company']]]}


def test_default_settings_give_the_same_companies_leads_scores_and_drafts_as_before(make_ctx):
    ctx = make_ctx()
    run_a_day(ctx)
    got = json.loads(json.dumps(snapshot(ctx), default=str))
    if EXPECTED is None:  # recording mode, used once on the base commit
        print("GOLDEN=" + json.dumps(got, sort_keys=True))
        raise AssertionError("EXPECTED not recorded yet")
    assert got == EXPECTED


# Recorded on commit 50b9172: the job schedule exactly as config/schedule.toml gives it.
EXPECTED_SCHEDULE = [("db_maintenance", "daily 06:15", True), ("crm_maintenance", "daily 06:30", True), ("plan", "daily 06:45", True),
                     ("research", "daily 07:00", True), ("enrich", "daily 07:15", True), ("inbox", "every 15 min", True),
                     ("content", "daily 07:30", True), ("inbound", "every 30 min", True), ("followups", "every 60 min", True),
                     ("publish", "every 30 min", True), ("daily_report", "daily 20:00", True), ("weekly_report", "weekly Mon 08:00", True),
                     ("monitor", "every 30 min", True)]


def test_default_settings_give_the_same_schedule_as_before(ctx):
    from aime.scheduler.runner import is_due, load_job_specs
    from aime.timeutil import local_tz, utcnow

    specs = load_job_specs(ctx.settings)
    assert [(j.name, j.schedule_label, j.enabled) for j in specs] == EXPECTED_SCHEDULE
    tz = local_tz(ctx.settings.schedule.get("timezone"))
    assert all(is_due(j, None, utcnow(), tz)[0] for j in specs)  # nothing has run: every job is due, none disabled


def test_default_settings_give_the_same_research_counts_and_source_reports_as_before(ctx):
    from aime.agents import USBusinessResearchAgent

    (ctx.settings.imports_dir / "owner-list.csv").write_text(CSV, encoding="utf-8")
    out = USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring", "sec_edgar", "csv_import"]}).output
    counts = out.model_dump(include={"candidates_seen", "non_us_skipped", "unknown_location_skipped", "invalid_skipped", "companies_created",
                                     "companies_matched_existing", "leads_created", "limit_reached"})
    assert counts == EXPECTED_COUNTS
    assert [[r.name, r.configured, r.ok, r.records, r.message] for r in out.sources] == EXPECTED_SOURCES


# Recorded on commit 50b9172 with the same CSV list.
EXPECTED_COUNTS = {"candidates_seen": 11, "companies_created": 5, "companies_matched_existing": 1, "invalid_skipped": 0, "leads_created": 5,
                   "limit_reached": False, "non_us_skipped": 1, "unknown_location_skipped": 4}
EXPECTED_SOURCES = [["hn_hiring", True, True, 6, "Public API, no key required"],
                    ["sec_edgar", False, False, 0, "NOT CONFIGURED: set SEC_USER_AGENT (organization name + contact email)"],
                    ["csv_import", True, True, 5, "1 file(s) waiting in data/imports"]]
