"""End-to-end: the whole AI employee team on fictional sample data, offline."""

from pathlib import Path

from sqlalchemy import func, select

from aime.inbound import record_inquiry
from aime.models import Company, ContentItem, FollowUp, JobRun, Lead, Opportunity, Report
from aime.sample_data import SAMPLE_INBOUND
from aime.scheduler.runner import run_job


def test_full_pipeline_on_sample_data(ctx):
    with ctx.db.session() as s:
        for rec in SAMPLE_INBOUND:
            record_inquiry(s, rec, "web_form")

    order = ["crm_maintenance", "plan", "research", "content", "inbound", "followups", "publish",
             "daily_report", "monitor"]
    results = {job: run_job(ctx, job, trigger="test") for job in order}
    assert all(r["status"] == "success" for r in results.values()), results

    with ctx.db.session() as s:
        companies = s.execute(select(Company).where(Company.merged_into_id.is_(None))).scalars().all()
        assert len(companies) == 6
        assert all(c.is_sample or c.domain.endswith(".example") for c in companies if c.domain)

        qualified = s.execute(select(Lead).where(Lead.status == "qualified")).scalars().all()
        assert {l.source for l in qualified} == {"inbound", "research"}
        assert s.execute(select(func.count(Opportunity.id))).scalar_one() == 2

        # Safety invariants: nothing left the building.
        assert s.execute(select(func.count(FollowUp.id)).where(FollowUp.status == "sent")).scalar_one() == 0
        assert s.execute(select(func.count(ContentItem.id)).where(ContentItem.status == "published")).scalar_one() == 0
        assert all(i.status == "awaiting_approval" for i in s.execute(select(ContentItem)).scalars())

        report = s.execute(select(Report)).scalars().one()
        assert s.execute(select(func.count(JobRun.id))).scalar_one() == len(order)

    md = Path(report.path_md).read_text(encoding="utf-8")
    assert "LinkedIn publishing (official API): NOT CONFIGURED" in md
    assert "Brightpath Realty Group" in md
    assert Path(report.path_html).exists()
