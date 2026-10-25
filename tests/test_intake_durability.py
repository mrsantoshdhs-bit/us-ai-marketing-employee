"""Inbox, CSV-import and metrics files are never lost: archived only after their rows are committed."""

import json

from sqlalchemy import func, select

from aime.agents import AnalyticsReportingAgent, CRMManagementAgent, USBusinessResearchAgent
from aime.models import Alert, Company, ContentMetric, InboundInquiry
from aime.sample_data import SAMPLE_INBOUND


def _count(ctx, model):
    with ctx.db.session() as s:
        return s.execute(select(func.count(model.id))).scalar_one()


def _inbox_file(ctx, name="leads.json", content=None):
    ctx.settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.settings.inbox_dir / name
    path.write_text(content if content is not None else json.dumps(SAMPLE_INBOUND), encoding="utf-8")
    return path


def _csv_file(ctx, rows, name="companies.csv"):
    ctx.settings.imports_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.settings.imports_dir / name
    lines = ["name,website,state,industry,notes"]
    lines += [f"Sample Company {i:03d},https://sample{i:03d}.example,TX,saas,Imported from a trade show list"
              for i in range(rows)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _processed(folder):
    return list((folder / "processed").glob("*")) if (folder / "processed").exists() else []


def test_inbox_file_stays_when_processing_fails(ctx, monkeypatch):
    path = _inbox_file(ctx)

    def boom(self, session, out):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(CRMManagementAgent, "_process_inbound", boom)
    res = CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]})
    assert not res.ok
    assert path.exists() and _processed(ctx.settings.inbox_dir) == []
    assert _count(ctx, InboundInquiry) == 0


def test_inbox_file_archived_after_commit(ctx):
    path = _inbox_file(ctx)
    res = CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]})
    assert res.ok and res.output.inbound_files_loaded == len(SAMPLE_INBOUND)
    assert not path.exists() and len(_processed(ctx.settings.inbox_dir)) == 1
    assert _count(ctx, InboundInquiry) == len(SAMPLE_INBOUND)


def test_crash_between_commit_and_archive_is_safe(ctx, monkeypatch):
    path = _inbox_file(ctx)

    def fail_move(*a, **k):
        raise OSError("file locked")
    monkeypatch.setattr("aime.intake.shutil.move", fail_move)
    assert CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]}).ok
    assert path.exists() and _count(ctx, InboundInquiry) == len(SAMPLE_INBOUND)
    monkeypatch.undo()
    res = CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]})
    assert res.ok and res.output.inbound_files_loaded == 0
    assert not path.exists() and _count(ctx, InboundInquiry) == len(SAMPLE_INBOUND)


def test_unreadable_inbox_file_is_quarantined_not_deleted(ctx):
    _inbox_file(ctx, "broken.json", "{not json")
    res = CRMManagementAgent(ctx).run({"operations": ["ingest_inbound"]})
    assert res.ok and res.output.inbound_files_rejected
    kept = list((ctx.settings.inbox_dir / "rejected").glob("*broken.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "{not json"
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.message.like("Inbox file moved to rejected/%"))).first()


def test_csv_file_stays_when_research_fails(ctx, monkeypatch):
    path = _csv_file(ctx, 3)

    def boom(*a, **k):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr("aime.agents.research.ensure_lead", boom)
    res = USBusinessResearchAgent(ctx).run({"sources": ["csv_import"]})
    assert not res.ok and path.exists() and _count(ctx, Company) == 0


def test_csv_rows_beyond_research_limit_are_all_imported(ctx):
    path = _csv_file(ctx, 60)
    res = USBusinessResearchAgent(ctx).run({"sources": ["csv_import"], "limit": 40})
    assert res.ok and res.output.companies_created == 60 and not res.output.limit_reached
    assert not path.exists() and res.output.import_files_archived == 1
    assert _count(ctx, Company) == 60


def test_reimporting_the_same_csv_is_skipped(ctx):
    _csv_file(ctx, 5)
    USBusinessResearchAgent(ctx).run({"sources": ["csv_import"]})
    _csv_file(ctx, 5)  # same bytes dropped again
    res = USBusinessResearchAgent(ctx).run({"sources": ["csv_import"]})
    assert res.ok and res.output.companies_created == 0 and res.output.import_files_archived == 1
    assert _count(ctx, Company) == 5


def test_oversized_csv_is_left_in_place_with_alert(ctx, monkeypatch):
    monkeypatch.setattr("aime.sources.csv_import.MAX_ROWS_PER_FILE", 10)
    path = _csv_file(ctx, 11)
    res = USBusinessResearchAgent(ctx).run({"sources": ["csv_import"]})
    assert res.ok and path.exists() and _count(ctx, Company) == 0
    assert res.output.import_files_rejected and "split it" in res.output.import_files_rejected[0]


def test_metrics_import_is_idempotent(ctx):
    folder = ctx.settings.imports_dir / "linkedin_metrics"
    folder.mkdir(parents=True, exist_ok=True)
    csv_text = "Post URL,Impressions,Reactions,Comments\nhttps://www.linkedin.com/feed/update/urn:li:share:1,120,7,2\n"
    (folder / "export.csv").write_text(csv_text, encoding="utf-8")
    assert AnalyticsReportingAgent(ctx).run({"kind": "daily"}).output.metrics_imported == 1
    (folder / "export.csv").write_text(csv_text, encoding="utf-8")
    assert AnalyticsReportingAgent(ctx).run({"kind": "daily"}).output.metrics_imported == 0
    assert _count(ctx, ContentMetric) == 1 and not (folder / "export.csv").exists()
