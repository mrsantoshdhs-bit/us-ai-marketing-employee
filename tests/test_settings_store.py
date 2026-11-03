"""Settings control plane (Option B, Phase 1): typed, versioned sections with bounds, preview, audit and one-click restore; the
country framework and its legal-review guard; country-wise daily targets; the editable catalogue; lead-source switches and
priorities; automation schedules the runner honours; and the agent matrix. Default settings changing nothing is proven by
tests/test_settings_golden.py."""

from __future__ import annotations

import copy
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aime import admin_settings, countries, settings_store as ss, targets
from aime.agent_matrix import COMMON_PROHIBITED, agent_policy
from aime.agents import LeadQualificationAgent, USBusinessResearchAgent
from aime.models import AuditEvent, Company, Contact, JobState, Lead, SettingsVersion, User
from aime.scheduler.runner import is_due, load_job_specs, next_run
from aime.timeutil import local_tz, utcnow
from aime.web.app import create_app
from aime.web.auth import COOKIE, hash_password, make_cookie, session_secret

ORIGIN = {"origin": "http://testserver"}
PW = "settings-test " + "passphrase only"  # synthetic, assembled so the secret scanner stays quiet


def versions(ctx, name=None) -> int:
    with ctx.db.session() as s:
        q = select(func.count(SettingsVersion.id))
        if name:
            q = q.where(SettingsVersion.section == name)
        return s.execute(q).scalar_one()


def audits(ctx, action=None) -> list[AuditEvent]:
    with ctx.db.session() as s:
        q = select(AuditEvent).order_by(AuditEvent.id)
        if action:
            q = q.where(AuditEvent.action == action)
        rows = s.execute(q).scalars().all()
        s.expunge_all()
        return rows


def save(ctx, name, data, actor="owner", note=None):
    with ctx.db.session() as s:
        row, changes = ss.save(s, ctx.settings, name, data, actor, note)
        return (row.id if row else None), changes


def review(ctx, code="CA", who="Owner after counsel review"):
    with ctx.db.session() as s:
        countries.record_legal_review(s, ctx.settings, code, who, "owner", "CASL B2B rules reviewed")


def enable(ctx, code="CA", **extra):
    data = ss.get(ctx.settings, "countries")
    for c in data["countries"]:
        if c["code"] == code:
            c.update(enabled=True, **extra)
    return save(ctx, "countries", data)


# --------------------------------------------------------------------------- validation and bounds

@pytest.mark.parametrize("name,data,words", [
    ("ai", {"provider": "auto", "unknown_key": 1}, "Extra inputs are not permitted"),
    ("ai", {"provider": "auto", "outbound_email_enabled": True}, ".env only"),
    ("reporting", {"manager_report": {"require_human_approval": False}}, ".env only"),
    ("ai", {"provider": "cloud-paid"}, "provider"),
    ("ai", {"monthly_spend_cap_usd": 5000}, "less than or equal to 1000"),
    ("reporting", {"manager_report": {"recipients": ["not-an-address"]}}, "not an email address"),
    ("reporting", {"manager_report": {"time": "25:00"}}, "08:00"),
    ("reporting", {"manager_report": {"timezone": "Mars/Base"}}, "time zone"),
    ("retention", {"audit_events_days": 30}, "at least a year"),
    ("deliverability", {"per_hour_send_limit": 500}, "less than or equal to 50"),
    ("deliverability", {"warmup_daily_plan": [5, 500]}, "1 to 50"),
    ("targets", {"targets": [{"country": "US", "daily_verified_leads": -1}]}, "greater than or equal to 0"),
    ("targets", {"targets": [{"country": "US"}, {"country": "US"}]}, "one target per country"),
    ("targets", {"targets": [{"country": "ZZ"}]}, "no such country"),
    ("schedules", {"jobs": {"monitor": {"enabled": False, "every_minutes": 30}}}, "cannot be disabled"),
    ("schedules", {"jobs": {"db_maintenance": {"enabled": False, "daily_at": "06:15"}}}, "cannot be disabled"),
    ("schedules", {"jobs": {"inbox": {"every_minutes": 5}}}, "greater than or equal to 15"),
    ("schedules", {"jobs": {"followups": {"every_minutes": 30}}}, "more often than every 60 min"),
    ("schedules", {"jobs": {"research": {"every_minutes": 60}}}, "time of day"),
    ("schedules", {"jobs": {"nonsense": {}}}, "unknown job"),
    ("sources", {"sources": [{"name": "hn_hiring", "countries": ["US", "CA"]}]}, "cannot be used for CA"),
    ("sources", {"sources": [{"name": "scraper", "countries": ["US"]}]}, "unknown source"),
    ("sources", {"sources": [{"name": "hn_hiring", "priority": 0, "countries": ["US"]}]}, "greater than or equal to 1"),
    ("countries", {"countries": [{"code": "USA", "name": "x", "timezone": "UTC", "compliance_regime": "x"}]}, "two-letter"),
])
def test_a_section_refuses_unknown_keys_and_values_out_of_bounds(ctx, name, data, words):
    with ctx.db.session() as s:
        result = ss.preview(s, ctx.settings, name, data)
    assert not result.ok and words in result.errors[0], result.errors
    with pytest.raises(ValueError):
        save(ctx, name, data)
    assert versions(ctx) == 0 and not audits(ctx)


def test_the_catalogue_keeps_its_built_in_keys(ctx):
    data = ss.get(ctx.settings, "catalogue")
    data["industries"] = [e for e in data["industries"] if e["key"] != "saas"]
    with pytest.raises(ValueError, match="cannot be removed, only disabled: saas"):
        save(ctx, "catalogue", data)
    data = ss.get(ctx.settings, "catalogue")
    data["industries"].append({"key": "fake_builtin", "label": "Fake", "builtin": True})
    with pytest.raises(ValueError, match="only the code's own entries"):
        save(ctx, "catalogue", data)
    data = ss.get(ctx.settings, "catalogue")
    for e in data["services"]:
        e["enabled"] = False
    with pytest.raises(ValueError, match="at least one service"):
        save(ctx, "catalogue", data)


def test_every_section_has_defaults_that_pass_its_own_rules(ctx):
    for name in ss.SECTIONS:
        assert ss.validate_shape(name, ss.get(ctx.settings, name)) == ss.get(ctx.settings, name)
    assert ss.get(ctx.settings, "reporting") == {"manager_report": {"enabled": False, "recipients": [], "time": "04:00",
                                                                    "timezone": "Asia/Kolkata"}}
    assert ss.get(ctx.settings, "ai")["monthly_spend_cap_usd"] == 0
    assert ctx.settings.section("reporting")["manager_report"]["time"] == "04:00"  # other modules read it as before
    assert ctx.settings.section("retention")["audit_events_days"] == 0


# --------------------------------------------------------------------------- versions, audit, preview, revert

def test_a_save_is_a_new_active_version_audited_with_old_and_new_values(ctx):
    v1, changes = save(ctx, "reporting", {"manager_report": {"recipients": ["Boss@Example.com"]}}, note="first")
    assert v1 and changes == ["manager_report.recipients: [] -> ['boss@example.com']"]
    assert ctx.settings.section("reporting")["manager_report"]["recipients"] == ["boss@example.com"]
    v2, _ = save(ctx, "reporting", {"manager_report": {"recipients": [], "time": "05:30"}})
    with ctx.db.session() as s:
        rows = {r.id: r.active for r in s.execute(select(SettingsVersion).where(SettingsVersion.section == "reporting")).scalars()}
    assert rows == {v1: False, v2: True}
    assert save(ctx, "reporting", {"manager_report": {"time": "05:30"}}) == (None, [])  # nothing changed: nothing saved
    saved = audits(ctx, "settings_saved")
    assert [a.target for a in saved] == ["settings:reporting", "settings:reporting"]
    assert f"version {v2}" in saved[-1].detail and "'04:00' -> '05:30'" in saved[-1].detail and "owner" == saved[-1].actor


def test_preview_shows_the_change_and_writes_nothing(ctx):
    before = (versions(ctx), len(audits(ctx)), copy.deepcopy(ctx.settings.business))
    with ctx.db.session() as s:
        result = ss.preview(s, ctx.settings, "targets", {"targets": [{"country": "US", "daily_verified_leads": 1000,
                                                                      "daily_contactable_leads": 100}]})
    assert result.ok and "targets[US].daily_verified_leads: 0 -> 1000" in result.changes
    assert any("at most 40 new companies per run" in n for n in result.notes)
    assert (versions(ctx), len(audits(ctx)), ctx.settings.business) == before


def test_any_earlier_version_is_restored_with_one_click_as_a_new_version(ctx):
    v1, _ = save(ctx, "ai", {"model": "llama3.2:latest"})
    v2, _ = save(ctx, "ai", {"model": "qwen2.5:7b"})
    with ctx.db.session() as s:
        row, changes = ss.revert(s, ctx.settings, v1, "owner")
        v3 = row.id
    assert v3 > v2 and changes == ["model: 'qwen2.5:7b' -> 'llama3.2:latest'"]
    assert ctx.settings.section("ai")["model"] == "llama3.2:latest"
    restored = audits(ctx, "settings_reverted")
    assert len(restored) == 1 and f"version {v3}" in restored[0].detail
    with ctx.db.session() as s:
        assert s.get(SettingsVersion, v3).note == f"restored version {v1}"
        ss.reset_to_defaults(s, ctx.settings, "ai", "owner")
    assert not ss.stored(ctx.settings, "ai") and ctx.settings.section("ai")["model"] == ""
    assert audits(ctx, "settings_reset_section") and versions(ctx, "ai") == 3  # the history keeps every version


def test_a_saved_version_is_in_effect_after_a_restart(make_ctx, tmp_path):
    from aime.config import Settings
    from aime.context import create_context

    ctx = make_ctx()
    save(ctx, "ai", {"model": "llama3.2:latest"})
    again = create_context(Settings.load(env_file=tmp_path / "no.env", data_dir=ctx.settings.data_dir, database_url="",
                                         llm_provider="template", data_role="test"))
    try:
        assert again.settings.section("ai")["model"] == "llama3.2:latest" and ss.stored(again.settings, "ai")
    finally:
        again.db.dispose()


def test_a_saved_version_that_breaks_todays_rules_is_set_aside_and_the_defaults_apply(ctx):
    """A hand-edited database cannot enable a country without its legal review: start-up ignores that version."""
    data = ss.get(ctx.settings, "countries")
    data["countries"].append({**data["countries"][0], "code": "CA", "name": "Canada", "enabled": True, "compliance_regime": "CASL"})
    with ctx.db.session() as s:
        s.add(SettingsVersion(section="countries", data=data, created_by="hand edit", active=True))
    with ctx.db.session() as s:
        ss.load(s, ctx.settings)
    assert not ss.stored(ctx.settings, "countries") and countries.target_countries(ctx.settings) == ["US"]


def test_the_sales_settings_keep_versions_that_can_be_restored(ctx):
    def form(**changes):
        values = admin_settings.current(ctx.settings)
        data = {k: (", ".join(map(str, v)) if isinstance(v, list) and not k.endswith(("industries", "services", "sales_manager")) else v)
                for k, v in values.items()}
        data.update(changes)

        class Form(dict):
            def getlist(self, k):
                v = self.get(k)
                return v if isinstance(v, list) else ([v] if v else [])
        return Form(data)

    with ctx.db.session() as s:
        admin_settings.save(s, ctx.settings, "owner", form(**{"outreach.daily_send_limit": "12"}))
    first = ss.active_version_id(ctx.settings, "sales")
    with ctx.db.session() as s:
        admin_settings.save(s, ctx.settings, "owner", form(**{"outreach.daily_send_limit": "15"}))
    assert ctx.settings.section("outreach")["daily_send_limit"] == 15 and versions(ctx, "sales") == 2
    with ctx.db.session() as s:
        _, changes = ss.revert(s, ctx.settings, first, "owner")
    assert ctx.settings.section("outreach")["daily_send_limit"] == 12 and "outreach.daily_send_limit: 15 -> 12" in changes
    with ctx.db.session() as s:
        assert admin_settings.overrides(s)["outreach"]["daily_send_limit"] == 12
    assert audits(ctx, "settings_reverted")


# --------------------------------------------------------------------------- countries and the legal-review guard

def test_only_the_us_is_enabled_by_default_with_todays_values(ctx):
    reg = countries.registry(ctx.settings)
    assert [(c["code"], c["enabled"]) for c in reg] == [("US", True)]
    f = ctx.settings.section("followup")
    us = reg[0]
    assert (us["timezone"], us["send_window_start"], us["send_window_end"], us["holidays"]) == (
        f["send_window_timezone"], f["send_window_start"], f["send_window_end"], f["holidays"])
    assert countries.target_countries(ctx.settings) == countries.discovery_countries(ctx.settings) == ["US"]
    assert countries.outside_reason(["US"]) == "Outside US target market"


def test_a_country_cannot_be_enabled_without_a_recorded_legal_review(ctx):
    data = ss.get(ctx.settings, "countries")
    data["countries"].append({"code": "CA", "enabled": True, "name": "Canada", "timezone": "America/Toronto",
                              "compliance_regime": "CASL"})
    with pytest.raises(ValueError, match="CA cannot be enabled: no legal review"):
        save(ctx, "countries", data)
    data["countries"][-1].update(legal_review_confirmed_by="me", legal_review_confirmed_at="2026-10-01T00:00:00")
    with pytest.raises(ValueError, match="recorded with its own form"):  # a review cannot be typed into the country itself
        save(ctx, "countries", data)
    assert countries.target_countries(ctx.settings) == ["US"] and versions(ctx) == 0
    with ctx.db.session() as s:
        with pytest.raises(ValueError, match="name of the person"):
            countries.record_legal_review(s, ctx.settings, "CA", "", "owner")
        with pytest.raises(ValueError, match="needs no recorded review"):
            countries.record_legal_review(s, ctx.settings, "US", "Owner", "owner")
    review(ctx)
    assert countries.target_countries(ctx.settings) == ["US"]  # recorded, still not enabled
    entry = countries.country(ctx.settings, "CA")
    assert entry["legal_review_confirmed_by"] == "Owner after counsel review" and entry["legal_review_confirmed_at"]
    enable(ctx, "CA", timezone="America/Toronto", compliance_regime="CASL")
    assert countries.target_countries(ctx.settings) == ["US", "CA"]
    assert [a.action for a in audits(ctx)] == ["country_legal_review_recorded", "country_legal_review", "settings_saved"]
    assert "confirmed by Owner after counsel review" in audits(ctx, "country_legal_review")[0].detail


def test_a_review_survives_edits_of_the_country(ctx):
    review(ctx)
    enable(ctx, "CA", name="Canada (B2B)", compliance_regime="CASL")
    data = ss.get(ctx.settings, "countries")
    data["countries"][1]["language"] = "fr"
    save(ctx, "countries", data)
    assert countries.cleared(countries.country(ctx.settings, "CA"))


def test_target_country_set_in_the_sales_settings_alone_is_not_enough(ctx):
    """Before Option B a Target country of CA made discovery ask for CA; that stays, and nothing from CA is accepted."""
    ctx.settings.business["targeting"]["country"] = "CA"
    assert countries.discovery_countries(ctx.settings) == ["CA"]
    assert countries.target_countries(ctx.settings) == ["US"]
    assert [(c["code"], c["enabled"]) for c in countries.registry(ctx.settings)] == [("US", True), ("CA", False)]


CSV_CA = ("name,website,state,city,industry,notes,country\n"
          "Maple Leaf Logistics,https://maple-leaf.example,,Toronto,smb,Dispatch runs on spreadsheets and needs a CRM,Canada\n"
          "Brightpath Realty Group,https://brightpath-realty.example,TX,Austin,real_estate,Leads land in three inboxes,US\n"
          "Rhein Werke,https://rhein.example,,Cologne,smb,Spreadsheets everywhere,Germany\n")


def test_a_second_enabled_country_with_a_legal_review_lets_its_rows_through_research_and_scoring(ctx):
    (ctx.settings.imports_dir / "list.csv").write_text(CSV_CA, encoding="utf-8")
    review(ctx)
    enable(ctx, "CA", timezone="America/Toronto", compliance_regime="CASL")
    out = USBusinessResearchAgent(ctx).run({"sources": ["csv_import"]}).output
    assert out.companies_created == 2 and out.unknown_location_skipped == 1  # Germany is not a target: its column is not read
    assert LeadQualificationAgent(ctx).run({}).ok
    with ctx.db.session() as s:
        maple = s.execute(select(Company).where(Company.name == "Maple Leaf Logistics")).scalar_one()
        assert (maple.country, maple.state) == ("CA", None)
        assert maple.location_evidence.startswith("country CA (your imported list)")
        lead = s.execute(select(Lead).where(Lead.company_id == maple.id)).scalar_one()
        assert lead.status != "disqualified" and lead.disqualify_reason is None
        assert any("target country CA" in r for r in lead.score_breakdown)
        brightpath = s.execute(select(Company).where(Company.name == "Brightpath Realty Group")).scalar_one()
        assert brightpath.country == "US"


def test_with_the_us_alone_the_same_rows_stay_out_and_the_old_words_are_kept(ctx):
    (ctx.settings.imports_dir / "list.csv").write_text(CSV_CA, encoding="utf-8")
    out = USBusinessResearchAgent(ctx).run({"sources": ["csv_import"]}).output
    assert out.companies_created == 1 and out.unknown_location_skipped == 2
    with ctx.db.session() as s:
        assert s.execute(select(Company.country)).scalars().all() == ["US"]


def test_scoring_disqualifies_outside_the_enabled_target_countries(ctx):
    from aime.scoring import LeadFacts, score_lead

    base = dict(source="research", industry="saas", target_industries=["saas"], has_website=True, employee_range=None,
                verified=False, is_competitor=False, suppressed=False, contact_consent=None)
    assert score_lead(LeadFacts(country="CA", state=None, **base)).disqualify_reason == "Outside US target market"
    assert score_lead(LeadFacts(country="CA", state=None, target_countries=("US", "CA"), **base)).disqualify_reason is None
    assert (score_lead(LeadFacts(country="DE", state=None, target_countries=("US", "CA"), **base)).disqualify_reason
            == "Outside the enabled target countries (US, CA)")
    us = score_lead(LeadFacts(country="US", state="TX", **base))
    assert us.disqualify_reason is None and score_lead(LeadFacts(country="US", state="TX", target_countries=("US", "CA"), **base)) == us


# --------------------------------------------------------------------------- daily targets

def test_daily_progress_is_counted_from_the_records_with_reasons_for_a_shortfall(ctx):
    save(ctx, "targets", {"targets": [{"country": "US", "daily_verified_leads": 4, "daily_contactable_leads": 2}]})
    with ctx.db.session() as s:
        def company(name, **kw):
            c = Company(name=name, normalized_name=name.lower(), country=kw.pop("country", "US"), source="csv_import", **kw)
            s.add(c)
            s.flush()
            return c
        live = company("Live Co", website_status="verified")
        registry = company("Registry Co", verified=True)
        unchecked = company("Unchecked Co")
        bad = company("Competitor Co", website_status="verified", is_competitor=True)
        company("Sample Co", website_status="verified", is_sample=True)
        abroad = company("Abroad Co", country="CA", website_status="verified")
        for c, status in ((live, "qualified"), (registry, "nurture"), (unchecked, "new"), (bad, "disqualified"), (abroad, "new")):
            s.add(Lead(company_id=c.id, source="research", status=status))
        s.add(Contact(company_id=live.id, email="sales@livecorp.example", email_status="published", source="website"))
        s.add(Contact(company_id=registry.id, email="x@registry.example", email_status="unverified", source="website"))
    with ctx.db.session() as s:
        [p] = targets.progress(s, ctx.settings)
    assert (p.companies_found, p.leads_created, p.verified_leads, p.contactable_leads, p.qualified, p.disqualified) == (4, 4, 2, 1, 1, 1)
    assert (p.shortfall_verified, p.shortfall_contactable, p.pct_verified, p.pct_contactable) == (2, 1, 50, 50)
    words = " | ".join(p.reasons)
    assert "Research has not run today yet" in words and "1 of 4 new lead(s) were disqualified" in words
    assert "1 new lead(s) wait for their website check" in words and "1 verified lead(s) have no usable, verified email" in words


def test_targets_are_goals_and_never_raise_a_cap(ctx):
    save(ctx, "targets", {"targets": [{"country": "US", "daily_verified_leads": 10000, "daily_contactable_leads": 10000}]})
    assert ctx.settings.section("research")["max_new_companies_per_run"] == 40
    out = USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring"], "limit": 1}).output
    assert out.companies_created == 1 and out.limit_reached
    with ctx.db.session() as s:
        [p] = targets.progress(s, ctx.settings)
    assert any("per-run cap of 40" in r and "never raise it" in r for r in p.reasons)
    assert {k: ctx.settings.section("outreach")[k] for k in ("daily_send_limit", "sends_per_run", "mailbox_daily_limit")} == {
        "daily_send_limit": 20, "sends_per_run": 5, "mailbox_daily_limit": 50}


def test_no_goal_says_so(ctx):
    with ctx.db.session() as s:
        [p] = targets.progress(s, ctx.settings)
    assert p.country == "US" and p.reasons == ["No goal set for this country (Settings › Daily targets)"]


# --------------------------------------------------------------------------- lead sources

def test_a_source_switched_off_in_settings_is_not_read(ctx):
    data = ss.get(ctx.settings, "sources")
    data["sources"][0]["enabled"] = False  # hn_hiring
    save(ctx, "sources", data)
    out = USBusinessResearchAgent(ctx).run({}).output
    hn = next(r for r in out.sources if r.name == "hn_hiring")
    assert not hn.configured and "switched off in Settings" in hn.message and out.companies_created == 0


def test_the_env_switch_still_wins_over_a_source_switched_on(make_ctx):
    ctx = make_ctx(hn_source_enabled=False)
    out = USBusinessResearchAgent(ctx).run({"sources": ["hn_hiring"]}).output
    assert not out.sources[0].configured and out.companies_created == 0


def test_sources_are_read_by_priority(ctx):
    from aime.sources.registry import ordered_sources

    assert [s.name for s, _ in ordered_sources(ctx)] == ["hn_hiring", "sec_edgar", "csv_import"]
    data = ss.get(ctx.settings, "sources")
    {s["name"]: s for s in data["sources"]}["csv_import"]["priority"] = 1
    save(ctx, "sources", data)
    assert [s.name for s, _ in ordered_sources(ctx)] == ["csv_import", "hn_hiring", "sec_edgar"]
    (ctx.settings.imports_dir / "list.csv").write_text("name,website,state\nBrightpath Realty Group,https://brightpath-realty.example,TX\n",
                                                       encoding="utf-8")
    out = USBusinessResearchAgent(ctx).run({}).output
    assert [r.name for r in out.sources] == ["csv_import", "hn_hiring", "sec_edgar"]


# --------------------------------------------------------------------------- schedules

def test_a_job_switched_off_is_not_run_by_the_scheduler_and_the_switch_is_written_to_job_state(ctx):
    data = ss.get(ctx.settings, "schedules")
    data["jobs"]["research"]["enabled"] = False
    data["jobs"]["inbox"]["every_minutes"] = 30
    save(ctx, "schedules", data)
    specs = {j.name: j for j in load_job_specs(ctx.settings)}
    tz = local_tz(ctx.settings.schedule.get("timezone"))
    with ctx.db.session() as s:
        state = s.get(JobState, "research")
        assert state is not None and state.enabled is False
        assert is_due(specs["research"], state, utcnow(), tz) == (False, "disabled") and next_run(specs["research"], state, utcnow(), tz) is None
    assert specs["inbox"].schedule_label == "every 30 min" and is_due(specs["plan"], None, utcnow(), tz)[0]
    with ctx.db.session() as s:
        ss.reset_to_defaults(s, ctx.settings, "schedules", "owner")
    with ctx.db.session() as s:
        assert s.get(JobState, "research").enabled is True
    specs = {j.name: j for j in load_job_specs(ctx.settings)}
    assert specs["research"].enabled and specs["inbox"].schedule_label == "every 15 min"


def test_the_monitor_and_the_backup_always_run(ctx):
    with ctx.db.session() as s:  # even a hand-written job_state row cannot switch them off
        s.add(JobState(job="monitor", consecutive_failures=0, enabled=False))
    ctx.settings.schedule["jobs"]["db_maintenance"]["enabled"] = False
    specs = {j.name: j for j in load_job_specs(ctx.settings)}
    tz = local_tz(ctx.settings.schedule.get("timezone"))
    with ctx.db.session() as s:
        assert is_due(specs["monitor"], s.get(JobState, "monitor"), utcnow(), tz)[0]
    assert specs["db_maintenance"].enabled


# --------------------------------------------------------------------------- catalogue

def test_the_catalogue_adds_entries_and_disabled_ones_stop_being_targets(ctx):
    data = ss.get(ctx.settings, "catalogue")
    data["industries"].append({"key": "logistics", "label": "Logistics", "description": "freight and dispatch"})
    {e["key"]: e for e in data["industries"]}["smb"]["enabled"] = False
    save(ctx, "catalogue", data)
    from aime.catalogue import enabled, keep_enabled

    offered = enabled(ctx.settings, "industries")
    assert "logistics" in offered and "smb" not in offered and "saas" in offered
    assert keep_enabled(ctx.settings, "industries", ["saas", "smb", "unknown_old_key"]) == ["saas", "unknown_old_key"]
    with ctx.db.session() as s:
        assert admin_settings._choice_check(s, "multi_industries", ["logistics", "smb"], ctx.settings) == ["logistics"]


# --------------------------------------------------------------------------- the agent matrix

def test_agent_policy_lists_responsibilities_tools_and_the_safety_floor(ctx):
    p = agent_policy("research", ctx)
    assert p["enabled"] and p["responsibilities"] and p["allowed_tools"]
    assert set(COMMON_PROHIBITED) <= set(p["prohibited_actions"])
    with pytest.raises(KeyError):
        agent_policy("nobody", ctx)
    with ctx.db.session() as s:
        with pytest.raises(ValueError, match="cannot be changed here"):
            ss.save(s, ctx.settings, "agents", {"agents": {}}, "owner")


# --------------------------------------------------------------------------- the pages: Super Admin only

@pytest.fixture
def web(make_ctx):
    ctx = make_ctx(dashboard_auth=True, network_enabled=False)
    with ctx.db.session() as s:
        for name, role in (("boss", "super_admin"), ("ops", "admin"), ("mgr", "sales_manager"), ("ana", "analyst")):
            s.add(User(username=name, password_hash=hash_password(PW), role=role))
    app = create_app(ctx)

    def client(username):
        c = TestClient(app, raise_server_exceptions=True, follow_redirects=False, headers=ORIGIN)
        with ctx.db.session() as s:
            user = s.execute(select(User).where(User.username == username)).scalar_one()
            c.cookies.set(COOKIE, make_cookie(session_secret(ctx.settings.data_dir), user))
        return c
    return ctx, client


PAGES = ["/admin/settings/system"] + [f"/admin/settings/section/{n}" for n in ss.SECTIONS]


def test_only_a_super_admin_reaches_the_settings_pages(web):
    ctx, client = web
    boss = client("boss")
    for page in PAGES:
        r = boss.get(page)
        assert r.status_code == 200, (page, r.status_code)
        assert "<script" not in r.text
    for who in ("ops", "mgr", "ana"):
        c = client(who)
        for page in PAGES[:3]:
            assert c.get(page).status_code in (403, 303), (who, page)
        assert c.post("/admin/settings/section/ai", data={"settings_form": "1", "do": "save", "model": "x"}).status_code in (403, 303)
        assert c.post("/admin/settings/countries/review", data={"settings_form": "1", "code": "CA", "confirmed_by": "Mallory",
                                                                 "confirm": "yes"}).status_code in (403, 303)
    assert versions(ctx) == 0 and countries.country(ctx.settings, "CA") is None


def test_the_pages_preview_save_restore_and_record_a_review(web):
    ctx, client = web
    boss = client("boss")
    form = {"settings_form": "1", "manager_report.enabled": "", "manager_report.recipients": "sm@example.com",
            "manager_report.time": "04:30", "manager_report.timezone": "Asia/Kolkata"}
    r = boss.post("/admin/settings/section/reporting", data={**form, "do": "preview"})
    assert r.status_code == 200 and "nothing was saved" in r.text and "&#39;04:00&#39; -&gt; &#39;04:30&#39;" in r.text
    assert versions(ctx) == 0
    r = boss.post("/admin/settings/section/reporting", data={**form, "do": "save", "note": "manager report"})
    assert r.status_code == 303 and "Saved as version" in unquote(r.headers["location"])
    assert ctx.settings.section("reporting")["manager_report"]["recipients"] == ["sm@example.com"]
    assert boss.post("/admin/settings/section/reporting", data={}).status_code == 303 and versions(ctx) == 1  # an empty post: nothing
    assert boss.post("/admin/settings/section/agents", data={"settings_form": "1", "do": "save"}).status_code == 303
    r = boss.post("/admin/settings/countries/review", data={"settings_form": "1", "code": "CA", "confirmed_by": "Owner", "note": "CASL"})
    assert "tick the box" in unquote(r.headers["location"]) and countries.country(ctx.settings, "CA") is None
    boss.post("/admin/settings/countries/review", data={"settings_form": "1", "code": "CA", "confirmed_by": "Owner", "confirm": "yes"})
    assert countries.cleared(countries.country(ctx.settings, "CA")) and countries.target_countries(ctx.settings) == ["US"]
    page = boss.get("/admin/settings/section/countries").text
    assert "by Owner" in page
    first = ss.active_version_id(ctx.settings, "reporting")
    boss.post("/admin/settings/section/reporting", data={**form, "manager_report.recipients": "", "do": "save"})
    r = boss.post(f"/admin/settings/versions/{first}/revert")
    assert r.status_code == 303 and "restored" in unquote(r.headers["location"])
    assert ctx.settings.section("reporting")["manager_report"]["recipients"] == ["sm@example.com"]
    assert boss.post("/admin/settings/versions/999999/revert").status_code == 404
    assert boss.get("/admin/settings/section/nonsense").status_code == 404


def test_the_env_switches_cannot_be_set_from_any_settings_page(web):
    ctx, client = web
    boss = client("boss")
    for name in ss.SECTIONS:
        boss.post(f"/admin/settings/section/{name}", data={"settings_form": "1", "do": "save", "OUTBOUND_EMAIL_ENABLED": "true",
                                                           "outbound_email_enabled": "true", "publishing_enabled": "true",
                                                           "require_human_approval": "false"})
    s = ctx.settings
    assert s.outbound_email_enabled is False and s.publishing_enabled is False and s.require_human_approval is True
    assert not [n for n in ss.SECTIONS if ss.ENV_ONLY.search(ss._all_keys(ss.get(s, n)))]


# --------------------------------------------------------------------------- migration

def test_a_database_from_before_option_b_gets_the_table_additively_and_keeps_its_data(ctx):
    from sqlalchemy import inspect, text

    from aime.migrations import migrate

    with ctx.db.session() as s:
        s.add(Company(name="Kept Co", normalized_name="kept co", country="US", source="manual"))
    with ctx.db.engine.begin() as conn:
        conn.execute(text("DROP TABLE settings_versions"))
    assert "settings_versions" not in inspect(ctx.db.engine).get_table_names()
    with ctx.db.session() as s:  # code reading it before the migration ran: the defaults, no failure
        ss.load(s, ctx.settings)
    assert countries.target_countries(ctx.settings) == ["US"]
    migrate(ctx.db.engine)
    assert "settings_versions" in inspect(ctx.db.engine).get_table_names()
    with ctx.db.session() as s:
        assert s.execute(select(Company.name)).scalars().all() == ["Kept Co"]
    save(ctx, "ai", {"model": "llama3.2:latest"})
    assert versions(ctx, "ai") == 1
