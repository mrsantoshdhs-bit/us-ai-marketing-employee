"""Option B Phase 8: the work queue, company research through it, data retention, the added indexes and the funnel
at a volume past SQLite's parameter limit. Default behaviour stays as it was: the queue ranks exactly as a scan does,
and retention at its defaults deletes nothing it did not delete before."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from aime import funnel, retention, work_queue
from aime.agents.company_research import QUEUE_KIND, CompanyResearchAgent
from aime.migrations import migrate, pending_indexes
from aime.models import (Activity, AgentRun, Alert, AuditEvent, Company, Contact, JobRun, Lead, ResearchSnapshot, Suppression,
                         WorkItem)
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.workflows import job_db_maintenance

W1, W2 = "pc-a:101", "pc-b:202"  # two workers on other PCs: never judged dead from here


# ------------------------------------------------------------------ the queue itself

def test_enqueue_is_idempotent_and_claims_go_in_order_with_a_lease(ctx):
    now = utcnow()
    with ctx.db.session() as s:
        assert work_queue.enqueue(s, "k", "a", {"n": 1}, priority=2, now=now)
        assert not work_queue.enqueue(s, "k", "a", {"n": 9}, priority=0, now=now)  # already waiting: kept as it was
        assert work_queue.enqueue(s, "k", "b", priority=1, now=now) and work_queue.enqueue(s, "k", "c", priority=1, now=now)
        assert work_queue.enqueue(s, "other", "a", now=now)  # the key is unique per kind
    with ctx.db.session() as s:
        assert s.execute(select(func.count(WorkItem.id))).scalar_one() == 4
        assert s.execute(select(WorkItem.payload).where(WorkItem.kind == "k", WorkItem.key == "a")).scalar_one() == {"n": 1}
        got = work_queue.claim(s, "k", 2, W1, now=now)
        assert [i.key for i in got] == ["b", "c"]  # priority, then age
        assert all(i.status == "claimed" and i.locked_by == W1 and i.attempts == 1 and i.lease_until == now + work_queue.LEASE for i in got)
        assert work_queue.complete(s, got[0].id, W1, {"ok": True})
        assert not work_queue.complete(s, got[1].id, W2)  # not the holder: nothing changes
    with ctx.db.session() as s:
        assert work_queue.enqueue(s, "k", "b", priority=5, now=now)  # done: due again, pending once more
        b = s.execute(select(WorkItem).where(WorkItem.kind == "k", WorkItem.key == "b")).scalar_one()
        assert (b.status, b.attempts, b.priority, b.result) == ("pending", 0, 5, None)


def test_one_worker_per_kind_and_a_lease_that_ended_is_claimed_again(ctx):
    now = utcnow()
    with ctx.db.session() as s:
        for key in "abc":
            work_queue.enqueue(s, "k", key, now=now)
        assert [i.key for i in work_queue.claim(s, "k", 1, W1, now=now)] == ["a"]
    with ctx.db.session() as s:
        assert work_queue.busy(s, "k", W2, now)
        assert work_queue.claim(s, "k", 5, W2, now=now) == []  # no second worker reads the same sources at once
        assert len(work_queue.claim(s, "k", 5, W2, now=now, one_worker=False)) == 2  # b and c, never a
    later = now + work_queue.LEASE + timedelta(minutes=1)
    with ctx.db.session() as s:
        work_queue.release(s, [i.id for i in s.execute(select(WorkItem).where(WorkItem.locked_by == W2)).scalars()], W2)
        again = work_queue.claim(s, "k", 5, W2, now=later)  # W1's lease ended: its item is claimed again
        assert [i.key for i in again] == ["a", "b", "c"]
        assert [i.attempts for i in again] == [2, 1, 1]  # a release does not count an attempt


def test_a_failure_waits_its_backoff_and_dead_letters_after_the_last_attempt(ctx):
    now = utcnow()
    with ctx.db.session() as s:
        work_queue.enqueue(s, "k", "x", now=now)
    t = now
    for attempt in range(1, 4):
        with ctx.db.session() as s:
            (item,) = work_queue.claim(s, "k", 1, W1, now=t)
            status = work_queue.fail(s, item.id, W1, "boom", max_attempts=3, now=t)
            assert status == ("dead" if attempt == 3 else "pending")
            if status == "pending":
                assert item.available_at == t + work_queue.retry_delay(attempt)
                assert work_queue.claim(s, "k", 1, W1, now=t) == []  # not before its backoff
            t = item.available_at if status == "pending" else t
    with ctx.db.session() as s:
        item = s.execute(select(WorkItem)).scalar_one()
        assert (item.status, item.attempts, item.last_error) == ("dead", 3, "boom")
        assert not work_queue.enqueue(s, "k", "x", now=t)  # a dead letter stays for a person
        assert work_queue.claim(s, "k", 1, W1, now=t + timedelta(days=9)) == []
        assert work_queue.revive(s, item.id) and [i.key for i in work_queue.claim(s, "k", 1, W1, now=utcnow())] == ["x"]


def test_an_item_claimed_too_often_without_finishing_goes_to_the_dead_letters(ctx):
    now = utcnow()
    with ctx.db.session() as s:
        work_queue.enqueue(s, "k", "poison", now=now)
    for n in range(work_queue.MAX_ATTEMPTS):  # each worker died holding it
        with ctx.db.session() as s:
            assert work_queue.claim(s, "k", 1, f"pc-{n}:1", now=now + n * (work_queue.LEASE + timedelta(minutes=1)))
    with ctx.db.session() as s:
        assert work_queue.claim(s, "k", 1, W1, now=now + timedelta(days=1)) == []
        assert s.execute(select(WorkItem.status)).scalar_one() == "dead"
        assert work_queue.status(s)[0] | {"next_at": None, "last_done_at": None} == {
            "kind": "k", "pending": 0, "claimed": 0, "done": 0, "dead": 1, "blocked": 0, "cancelled": 0, "retrying": 0, "stuck": 0,
            "next_at": None, "last_done_at": None, "leased_by": []}


# ------------------------------------------------------------------ company research through the queue

def sites(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if not host.endswith(".example"):
        return httpx.Response(404)
    name = host.split(".")[0].replace("-", " ").title()
    if path == "/robots.txt":
        return httpx.Response(404)
    if path == "/":
        return httpx.Response(200, text=f"<html><head><title>{name}</title></head><body><a href='/team'>Our Team</a>"
                                        "<p>We still copy orders into spreadsheets.</p></body></html>",
                              headers={"content-type": "text/html; charset=utf-8"})
    if path == "/team":
        return httpx.Response(200, text="<html><body><div class='card'><h3>Alex Morgan</h3><p>Chief Executive Officer</p></div></body></html>",
                              headers={"content-type": "text/html; charset=utf-8"})
    return httpx.Response(404)


def accepts_mail(_domain):
    return True


def research_ctx(make_ctx, queue: bool, companies: int = 12, per_run: int = 5):
    ctx = make_ctx(handler=sites)
    ctx.settings.business.setdefault("enrichment", {}).update(work_queue=queue, companies_per_run=per_run)
    with ctx.db.session() as s:
        for i in range(1, companies + 1):
            c = Company(name=f"Queue Co {i}", normalized_name=f"queue co {i}", domain=f"queue-co-{i}.example",
                        website=f"https://queue-co-{i}.example", website_status="verified", source="csv_import")
            s.add(c)
            s.flush()
            s.add(Lead(company_id=c.id, source="research", status="qualified" if i % 3 == 0 else "new", score=(i * 37) % 90))
    return ctx


def research_run(ctx, now) -> list[int]:
    with ctx.db.session() as s:
        before = s.execute(select(func.max(ResearchSnapshot.id))).scalar() or 0
    assert CompanyResearchAgent(ctx, mail_checker=accepts_mail).run({"now": now.isoformat()}).ok
    with ctx.db.session() as s:
        return list(s.execute(select(ResearchSnapshot.company_id).where(ResearchSnapshot.id > before).order_by(ResearchSnapshot.id)).scalars())


def rescore(ctx, run: int) -> None:
    """Between runs the ranking changes: a lead is qualified, another's score moves."""
    with ctx.db.session() as s:
        leads = s.execute(select(Lead).order_by(Lead.id)).scalars().all()
        leads[-run].status, leads[-run].score = "qualified", 95
        leads[run].score = 1


def test_daily_runs_through_the_queue_research_what_a_scan_researches(make_ctx):
    """Same companies, same order, same number per run, even when the ranking changes between the daily runs."""
    got = {}
    for queue in (False, True):
        ctx = research_ctx(make_ctx, queue)
        day = utcnow()
        got[queue] = []
        for run in range(1, 4):
            got[queue].append(research_run(ctx, day + timedelta(days=run)))
            rescore(ctx, run)
    assert got[True] == got[False]
    assert [len(r) for r in got[True]] == [5, 5, 2]  # companies_per_run, then the rest: no cap is raised
    assert len({c for r in got[True] for c in r}) == 12


def test_runs_within_a_day_take_the_next_chunk_from_the_queue_without_scanning(make_ctx, monkeypatch):
    scan = research_ctx(make_ctx, False)
    start = utcnow()
    expected = [research_run(scan, start + timedelta(hours=h)) for h in range(3)]  # off: each run scans
    ctx = research_ctx(make_ctx, True)
    scans = []
    real = CompanyResearchAgent._candidates
    monkeypatch.setattr(CompanyResearchAgent, "_candidates", lambda self, *a: scans.append(1) or real(self, *a))
    assert [research_run(ctx, start + timedelta(hours=h)) for h in range(3)] == expected
    assert len(scans) == 2  # the first run ranked them; the second took the next five; the third found two and topped up
    with ctx.db.session() as s:
        rows = {r["kind"]: r for r in work_queue.status(s)}
        assert rows[QUEUE_KIND]["pending"] == 0 and rows[QUEUE_KIND]["claimed"] == 0
        assert rows[QUEUE_KIND]["done"] == 12  # every company once: researched, never twice


def test_a_company_no_longer_due_is_skipped_and_the_run_still_takes_its_share(make_ctx):
    ctx = research_ctx(make_ctx, True)
    start = utcnow()
    first = research_run(ctx, start)
    with ctx.db.session() as s:  # merged and marked a competitor since the ranking: neither is read
        waiting = [int(k) for k in s.execute(select(WorkItem.key).where(WorkItem.status == "pending").order_by(WorkItem.priority)).scalars()]
        s.get(Company, waiting[0]).merged_into_id = first[0]
        s.get(Company, waiting[1]).is_competitor = True
    second = research_run(ctx, start + timedelta(hours=1))
    assert waiting[0] not in second and waiting[1] not in second and len(second) == 5
    assert second == waiting[2:7]


def test_the_jobs_page_shows_the_work_queue(make_ctx):
    ctx = research_ctx(make_ctx, True)
    research_run(ctx, utcnow())
    page = TestClient(create_app(ctx)).get("/jobs").text
    assert "Work queue" in page and QUEUE_KIND in page
    row = page[page.index(f"<strong>{QUEUE_KIND}</strong>"):][:400]
    assert '<td class="n">7</td>' in row and '<td class="n">5</td>' in row  # 7 waiting, 5 done


# ------------------------------------------------------------------ retention

def crm_and_history(ctx, old_days: int = 200):
    now = utcnow()
    old = now - timedelta(days=old_days)
    with ctx.db.session() as s:
        c = Company(name="Kept Co", normalized_name="kept co", source="manual", created_at=old)
        s.add(c)
        s.flush()
        s.add_all([Lead(company_id=c.id, source="manual", created_at=old), Contact(company_id=c.id, source="manual", email="a@kept.example", created_at=old),
                   Activity(type="note", subject="Old note", actor="owner", created_at=old),
                   Suppression(value="no@kept.example", kind="email", reason="unsubscribe", created_at=old),
                   AuditEvent(actor="owner", action="settings.save", at=old),
                   Alert(level="warning", source="monitor", message="old resolved", created_at=old, resolved_at=old, acknowledged=True),
                   Alert(level="warning", source="monitor", message="old open", created_at=old),
                   Alert(level="warning", source="monitor", message="recent resolved", created_at=now, resolved_at=now - timedelta(days=2)),
                   JobRun(job="inbox", trigger="scheduler", status="success", started_at=old),
                   JobRun(job="inbox", trigger="scheduler", status="success", started_at=now - timedelta(days=20)),
                   JobRun(job="inbox", trigger="scheduler", status="success", started_at=now),
                   JobRun(job="inbox", trigger="scheduler", status="running", started_at=old),
                   AgentRun(run_id="a1", agent="crm", status="success", started_at=old),
                   AgentRun(run_id="a2", agent="crm", status="success", started_at=now)])


def counts(ctx) -> dict:
    with ctx.db.session() as s:
        return {m.__tablename__: s.execute(select(func.count()).select_from(m)).scalar_one()
                for m in (Company, Lead, Contact, Activity, Suppression, AuditEvent, Alert, JobRun, AgentRun)}


def test_retention_defaults_delete_nothing_but_the_run_history_the_job_always_pruned(ctx):
    crm_and_history(ctx)
    before = counts(ctx)
    assert retention.plan(ctx.settings) == {"job_runs": 90, "agent_runs": 90, "alerts": None}
    result = retention.apply(ctx.db, ctx.settings)
    assert result["removed"] == {"job_runs": 1, "agent_runs": 1, "alerts": 0}  # the 200-day-old finished runs, as before Phase 8
    after = counts(ctx)
    assert after == {**before, "job_runs": before["job_runs"] - 1, "agent_runs": before["agent_runs"] - 1}


def test_the_daily_maintenance_job_at_default_retention_deletes_no_alert_or_record(ctx):
    crm_and_history(ctx, old_days=60)  # younger than the 90 days kept: nothing at all goes
    before = counts(ctx)
    out = job_db_maintenance(ctx)
    step = out.steps[0]
    assert step["status"] == "success" and step["output"]["retention"]["removed"] == {"job_runs": 0, "agent_runs": 0, "alerts": 0}
    assert {k: v for k, v in counts(ctx).items() if k != "job_runs"} == {k: v for k, v in before.items() if k != "job_runs"}


def test_retention_set_by_the_owner_applies_only_to_run_history_and_resolved_alerts(ctx):
    crm_and_history(ctx)
    ctx.settings.business["retention"] = {"job_runs_days": 10, "agent_runs_days": 400, "alerts_days": 30, "inbound_emails_days": 1,
                                          "audit_events_days": 365}
    before = counts(ctx)
    result = retention.apply(ctx.db, ctx.settings)
    assert result["removed"] == {"job_runs": 2, "agent_runs": 0, "alerts": 1}
    with ctx.db.session() as s:
        assert sorted(a.message for a in s.execute(select(Alert)).scalars()) == ["old open", "recent resolved"]
        # 10 days kept: the 200- and 20-day-old runs go; the latest success stays, and so does the unfinished one
        assert s.execute(select(JobRun.status).order_by(JobRun.id)).scalars().all() == ["success", "running"]
        assert s.execute(select(func.count(AgentRun.id))).scalar_one() == 2  # 400 days kept: the 200-day-old run stays
    after = counts(ctx)
    for table in ("companies", "leads", "contacts", "activities", "suppressions", "audit_events", "agent_runs"):
        assert after[table] == before[table], table


def test_retention_never_names_a_crm_table():
    assert set(retention.TABLES_TOUCHED) == {"job_runs", "agent_runs", "alerts"}


# ------------------------------------------------------------------ indexes and volume

PHASE8_INDEXES = {"ix_companies_merged_into_id", "ix_inbound_inquiries_lead_id", "ix_inbound_emails_company_id", "ix_followups_company_id",
                  "ix_companies_created_at", "ix_contacts_created_at", "ix_leads_created_at", "ix_followups_sent_at", "ix_work_items_claim",
                  "ix_suppressions_kind_lifted"}


def test_an_existing_database_gets_the_new_indexes_additively(ctx):
    engine = ctx.db.engine
    with engine.begin() as conn:
        for name in PHASE8_INDEXES:
            conn.execute(text(f"DROP INDEX {name}"))
    with ctx.db.session() as s:
        s.add(Company(name="Old Co", normalized_name="old co", source="manual"))
    assert {i.name for i in pending_indexes(engine)} == PHASE8_INDEXES
    applied = migrate(engine)
    assert sorted(applied) == sorted(f"CREATE INDEX {n}" for n in PHASE8_INDEXES)
    with engine.connect() as conn:
        have = set(conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'")).scalars())
    assert PHASE8_INDEXES <= have
    with ctx.db.session() as s:
        assert s.execute(select(Company.name)).scalars().all() == ["Old Co"]  # no data changed


def test_the_funnel_counts_past_sqlites_parameter_limit(ctx):
    """34,000 companies (100x today) as one IN list was refused by SQLite ("too many SQL variables"); all companies are a subquery now."""
    with ctx.db.engine.begin() as conn:
        conn.execute(Company.__table__.insert(), [dict(name=f"Co {i}", normalized_name=f"co {i}", source="csv_import", country="US", verified=True,
                                                       is_competitor=False, is_sample=False, lifecycle_stage="prospect", validation={})
                                                  for i in range(1, 34_001)])
        conn.execute(Lead.__table__.insert(), [dict(company_id=i, source="research", status="qualified", score=60, score_breakdown=[],
                                                    matched_services=[], is_sample=False) for i in range(1, 34_001, 2)])
        conn.execute(Contact.__table__.insert(), [dict(company_id=i, source="website", first_name="Alex", last_name="Lee", title="Chief Executive Officer",
                                                       email=f"alex.lee@co{i}.example", email_status="published", consent_status="none",
                                                       is_sample=False) for i in range(1, 34_001, 4)])
    with ctx.db.session() as s:
        whole = funnel.summary(s)
        some = funnel.summary(s, list(range(1, 2001)))
        listed = funnel.summary(s, [c for c in s.execute(select(Company.id).where(Company.id <= 30_000)).scalars()])
    reached = {st["key"]: st["reached"] for st in whole["stages"]}
    assert whole["companies"] == 34_000 and reached["qualified_lead"] == 17_000 and reached["identified_decision_maker"] == 8_500
    assert {st["key"]: st["reached"] for st in some["stages"]}["qualified_lead"] == 1000
    assert listed["companies"] == 30_000  # a given list still works as before


@pytest.mark.parametrize("email,domain,listed", [("no@kept.example", None, True), ("No+tag@Kept.example", None, True),
                                                  ("yes@kept.example", None, False), (None, "kept.example", False),
                                                  ("x@sub.blocked.example", None, True), (None, None, False)])
def test_the_prepared_suppression_check_answers_as_before(ctx, email, domain, listed):
    from aime.crm_ops import _entries_covering, is_suppressed

    with ctx.db.session() as s:
        s.add_all([Suppression(value="no@kept.example", kind="email", reason="unsubscribe"),
                   Suppression(value="blocked.example", kind="domain", reason="asked"),
                   Suppression(value="lifted@kept.example", kind="email", reason="unsubscribe", lifted_at=utcnow())])
    with ctx.db.session() as s:
        entries = _entries_covering(email, domain)
        old_way = entries is not None and s.execute(entries.where(Suppression.lifted_at.is_(None))).first() is not None
        assert is_suppressed(s, email, domain) is old_way is listed
