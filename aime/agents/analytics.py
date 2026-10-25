"""Analytics and Reporting Agent: KPIs, pipeline, content queue, blockers, agent health -> daily report."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta

from jinja2 import Environment, select_autoescape
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent
from aime.integrations.status import integration_statuses
from aime.knowledge.industries import industry_label
from aime.knowledge.services import service_name
from aime.metrics_import import import_metrics_dir
from aime.models import (Activity, AgentRun, Alert, Company, ContentItem, ContentMetric, FollowUp, InboundInquiry,
                         Lead, Opportunity, Report, Signal)
from aime.monitoring import agent_stats
from aime.timeutil import fmt_et, fmt_local, local_tz, utcnow

PROBLEM_STATES = {"NOT CONFIGURED", "NOT CONNECTED", "BLOCKED", "DISABLED", "UNAVAILABLE", "NOT RUNNING", "STALE"}


class ReportInput(BaseModel):
    report_date: date | None = None
    import_metrics: bool = True


class ReportOutput(BaseModel):
    report_id: int
    report_date: date
    path_md: str
    path_html: str
    kpis: dict[str, int] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    metrics_imported: int = 0


MD_TEMPLATE = """# Daily Marketing Report — {{ d }}

Generated {{ generated }} (local time). Window: last 24 hours. All figures are counts of records in the local CRM.

## Summary

| Metric | Last 24h | Total |
|---|---:|---:|
{% for label, day, total in summary %}| {{ label }} | {{ day }} | {{ total }} |
{% endfor %}
## Lead grades (open leads)

{% for g in "ABCD" %}- **{{ g }}**: {{ grades.get(g, 0) }}
{% endfor %}
## Pipeline (opportunities by stage)

{% if stages %}{% for stage, n in stages %}- {{ stage }}: {{ n }}
{% endfor %}{% else %}No opportunities yet. Opportunities open only for qualified leads who contacted us.
{% endif %}
## Top open leads

{% if top_leads %}| Company | State | Industry | Grade | Score | Best-fit service | Evidence |
|---|---|---|---|---:|---|---|
{% for l in top_leads %}| {{ l.company }} | {{ l.state }} | {{ l.industry }} | {{ l.grade }} | {{ l.score }} | {{ l.service }} | {{ l.evidence }} |
{% endfor %}{% else %}No scored leads yet.
{% endif %}
## Content queue (next items)

{% if queue %}| When | Title | Status | Note |
|---|---|---|---|
{% for c in queue %}| {{ c.when }} | {{ c.title }} | {{ c.status }} | {{ c.note }} |
{% endfor %}{% else %}Queue is empty.
{% endif %}
## Blockers and required actions

{% if blockers %}{% for b in blockers %}- {{ b }}
{% endfor %}{% else %}None.
{% endif %}
## Agent health (7 days)

| Agent | Runs | Success rate | Avg ms | p95 ms | Retries | Last error |
|---|---:|---:|---:|---:|---:|---|
{% for a in agents %}| {{ a.agent }} | {{ a.runs }} | {{ a.success_rate }}% | {{ a.avg_ms }} | {{ a.p95_ms }} | {{ a.retries }} | {{ (a.last_error or "")[:80] }} |
{% endfor %}
## Alerts (24h)

{% if alerts %}{% for a in alerts %}- [{{ a.level }}] {{ a.source }}: {{ a.message }}
{% endfor %}{% else %}None.
{% endif %}
{% if engagement %}## Content performance (imported analytics)

| Post | Impressions | Reactions | Comments |
|---|---:|---:|---:|
{% for e in engagement %}| {{ e.title }} | {{ e.impressions }} | {{ e.reactions }} | {{ e.comments }} |
{% endfor %}{% endif %}"""

HTML_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Daily report {{ d }}</title>
<style>
body{font-family:Segoe UI,system-ui,sans-serif;margin:24px;color:#1d2433;background:#fff;max-width:1100px}
h1{font-size:24px}h2{font-size:18px;margin-top:28px;border-bottom:1px solid #e3e7ef;padding-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid #eef1f6;padding:6px 8px;text-align:left}
td.n,th.n{text-align:right}.muted{color:#667085}.pill{display:inline-block;padding:1px 8px;border-radius:10px;background:#eef2ff}
</style></head><body>
<h1>Daily Marketing Report — {{ d }}</h1>
<p class="muted">Generated {{ generated }} (local time). Window: last 24 hours. Counts of records in the local CRM.</p>
<h2>Summary</h2><table><tr><th>Metric</th><th class="n">Last 24h</th><th class="n">Total</th></tr>
{% for label, day, total in summary %}<tr><td>{{ label }}</td><td class="n">{{ day }}</td><td class="n">{{ total }}</td></tr>{% endfor %}</table>
<h2>Lead grades (open leads)</h2><p>{% for g in "ABCD" %}<span class="pill">{{ g }}: {{ grades.get(g, 0) }}</span> {% endfor %}</p>
<h2>Pipeline</h2>{% if stages %}<p>{% for stage, n in stages %}<span class="pill">{{ stage }}: {{ n }}</span> {% endfor %}</p>{% else %}<p class="muted">No opportunities yet.</p>{% endif %}
<h2>Top open leads</h2>{% if top_leads %}<table><tr><th>Company</th><th>State</th><th>Industry</th><th>Grade</th><th class="n">Score</th><th>Service</th><th>Evidence</th></tr>
{% for l in top_leads %}<tr><td>{{ l.company }}</td><td>{{ l.state }}</td><td>{{ l.industry }}</td><td>{{ l.grade }}</td><td class="n">{{ l.score }}</td><td>{{ l.service }}</td><td>{% if l.evidence.startswith('http') %}<a href="{{ l.evidence }}">source</a>{% else %}{{ l.evidence }}{% endif %}</td></tr>{% endfor %}</table>{% else %}<p class="muted">No scored leads yet.</p>{% endif %}
<h2>Content queue</h2>{% if queue %}<table><tr><th>When</th><th>Title</th><th>Status</th><th>Note</th></tr>
{% for c in queue %}<tr><td>{{ c.when }}</td><td>{{ c.title }}</td><td>{{ c.status }}</td><td>{{ c.note }}</td></tr>{% endfor %}</table>{% else %}<p class="muted">Queue is empty.</p>{% endif %}
<h2>Blockers and required actions</h2>{% if blockers %}<ul>{% for b in blockers %}<li>{{ b }}</li>{% endfor %}</ul>{% else %}<p>None.</p>{% endif %}
<h2>Agent health (7 days)</h2><table><tr><th>Agent</th><th class="n">Runs</th><th class="n">Success</th><th class="n">Avg ms</th><th class="n">p95 ms</th><th class="n">Retries</th><th>Last error</th></tr>
{% for a in agents %}<tr><td>{{ a.agent }}</td><td class="n">{{ a.runs }}</td><td class="n">{{ a.success_rate }}%</td><td class="n">{{ a.avg_ms }}</td><td class="n">{{ a.p95_ms }}</td><td class="n">{{ a.retries }}</td><td>{{ (a.last_error or "")[:80] }}</td></tr>{% endfor %}</table>
<h2>Alerts (24h)</h2>{% if alerts %}<ul>{% for a in alerts %}<li>[{{ a.level }}] {{ a.source }}: {{ a.message }}</li>{% endfor %}</ul>{% else %}<p>None.</p>{% endif %}
</body></html>"""


class AnalyticsReportingAgent(BaseAgent[ReportInput, ReportOutput]):
    name = "analytics"
    title = "Analytics and Reporting Agent"
    responsibilities = [
        "Import user-exported LinkedIn analytics and attach them to published posts",
        "Compute daily KPIs from CRM records (no estimated or invented figures)",
        "List blockers that need a human (missing authorization, disabled channels)",
        "Write Markdown and HTML daily reports and keep a report index",
    ]
    input_model = ReportInput
    output_model = ReportOutput

    def execute(self, inp: ReportInput, session: Session) -> ReportOutput:
        s = self.settings
        tz = local_tz(s.schedule.get("timezone"))
        d = inp.report_date or datetime.now(tz).date()
        now = utcnow()
        since = now - timedelta(hours=24)
        imported = import_metrics_dir(session, s.imports_dir / "linkedin_metrics") if inp.import_metrics else 0

        def count(model, *conds):
            return session.execute(select(func.count(model.id)).where(*conds)).scalar_one()

        active_co = Company.merged_into_id.is_(None)
        open_lead = Lead.status.in_(("new", "qualified", "nurture"))
        kpis = {
            "companies_total": count(Company, active_co),
            "companies_new_24h": count(Company, active_co, Company.created_at >= since),
            "signals_new_24h": count(Signal, Signal.created_at >= since),
            "leads_open": count(Lead, open_lead),
            "leads_new_24h": count(Lead, Lead.created_at >= since),
            "leads_qualified": count(Lead, Lead.status == "qualified"),
            "qualified_24h": count(Activity, Activity.type == "status_change", Activity.created_at >= since,
                                   Activity.subject.like("%→ qualified%")),
            "inbound_24h": count(InboundInquiry, InboundInquiry.received_at >= since),
            "inbound_total": count(InboundInquiry),
            "opportunities_open": count(Opportunity, Opportunity.stage.not_in(("won", "lost"))),
            "opportunities_new_24h": count(Opportunity, Opportunity.created_at >= since),
            "followups_awaiting_approval": count(FollowUp, FollowUp.status == "awaiting_approval"),
            "tasks_pending": count(FollowUp, FollowUp.channel == "task", FollowUp.status == "pending"),
            "emails_sent_24h": count(FollowUp, FollowUp.status == "sent", FollowUp.sent_at >= since),
            "emails_sent_total": count(FollowUp, FollowUp.status == "sent"),
            "content_created_24h": count(ContentItem, ContentItem.created_at >= since),
            "content_awaiting_approval": count(ContentItem, ContentItem.status == "awaiting_approval"),
            "content_approved_queued": count(ContentItem, ContentItem.status == "approved"),
            "content_published_24h": count(ContentItem, ContentItem.status == "published", ContentItem.published_at >= since),
            "content_published_total": count(ContentItem, ContentItem.status == "published"),
            "agent_runs_24h": count(AgentRun, AgentRun.started_at >= since),
            "agent_failures_24h": count(AgentRun, AgentRun.started_at >= since, AgentRun.status == "failed"),
        }
        summary = [
            ("Companies discovered", kpis["companies_new_24h"], kpis["companies_total"]),
            ("Evidence signals", kpis["signals_new_24h"], count(Signal)),
            ("Leads", kpis["leads_new_24h"], kpis["leads_open"]),
            ("Qualified leads", kpis["qualified_24h"], kpis["leads_qualified"]),
            ("Inbound inquiries", kpis["inbound_24h"], kpis["inbound_total"]),
            ("Opportunities", kpis["opportunities_new_24h"], kpis["opportunities_open"]),
            ("Emails sent (approved replies)", kpis["emails_sent_24h"], kpis["emails_sent_total"]),
            ("Posts created", kpis["content_created_24h"], count(ContentItem)),
            ("Posts published (confirmed by LinkedIn)", kpis["content_published_24h"], kpis["content_published_total"]),
            ("Agent runs / failures", f"{kpis['agent_runs_24h']} / {kpis['agent_failures_24h']}", "—"),
        ]
        grades = Counter(g for g in session.execute(select(Lead.grade).where(open_lead)).scalars() if g)
        stages = session.execute(select(Opportunity.stage, func.count(Opportunity.id))
                                 .group_by(Opportunity.stage)).all()

        top_leads = []
        for lead in session.execute(select(Lead).where(open_lead, Lead.last_scored_at.is_not(None))
                                    .order_by(Lead.score.desc()).limit(10)).scalars():
            sig = session.execute(select(Signal).where(Signal.company_id == lead.company_id)
                                  .order_by(Signal.observed_at.desc())).scalars().first()
            evidence = sig.source_url if sig and sig.source_url else "—"
            top_leads.append({
                "company": lead.company.name.replace("|", "/"), "state": lead.company.state or "—",
                "industry": industry_label(lead.company.industry), "grade": lead.grade, "score": lead.score,
                "service": service_name(lead.primary_service), "evidence": evidence,
            })

        queue = []
        for c in session.execute(select(ContentItem).where(ContentItem.status.in_(("awaiting_approval", "approved", "failed")))
                                 .order_by(ContentItem.scheduled_for).limit(7)).scalars():
            queue.append({"when": fmt_et(c.scheduled_for), "title": c.title, "status": c.status,
                          "note": c.publish_blocked_reason or ("needs human approval" if c.status == "awaiting_approval" else "")})

        blockers = [f"{r['name']}: {r['state']} — {r['detail']}" for r in integration_statuses(self.ctx, session)
                    if r["state"] in PROBLEM_STATES]
        alerts = session.execute(select(Alert).where(Alert.created_at >= since).order_by(Alert.created_at.desc())
                                 .limit(20)).scalars().all()
        engagement = []
        for item, imp, rea, com in session.execute(
                select(ContentItem, func.sum(ContentMetric.impressions), func.sum(ContentMetric.reactions),
                       func.sum(ContentMetric.comments)).join(ContentMetric, ContentMetric.content_id == ContentItem.id)
                .group_by(ContentItem.id).order_by(func.sum(ContentMetric.reactions).desc()).limit(5)).all():
            engagement.append({"title": item.title, "impressions": imp or 0, "reactions": rea or 0, "comments": com or 0})

        data = dict(d=d.isoformat(), generated=fmt_local(now, s.schedule.get("timezone")), summary=summary,
                    grades=grades, stages=stages, top_leads=top_leads, queue=queue, blockers=blockers,
                    agents=agent_stats(session, 7), alerts=alerts, engagement=engagement)
        md = Environment(autoescape=False).from_string(MD_TEMPLATE).render(**data)
        html = Environment(autoescape=select_autoescape(default=True)).from_string(HTML_TEMPLATE).render(**data)
        s.reports_dir.mkdir(parents=True, exist_ok=True)
        md_path = s.reports_dir / f"daily_{d.isoformat()}.md"
        html_path = s.reports_dir / f"daily_{d.isoformat()}.html"
        md_path.write_text(md, encoding="utf-8")
        html_path.write_text(html, encoding="utf-8")

        report = session.execute(select(Report).where(Report.report_date == d, Report.kind == "daily")).scalars().first()
        if report is None:
            report = Report(report_date=d, kind="daily", path_md=str(md_path), path_html=str(html_path))
            session.add(report)
        report.summary = {"kpis": kpis, "blockers": blockers}
        report.created_at = now
        session.flush()
        return ReportOutput(report_id=report.id, report_date=d, path_md=str(md_path), path_html=str(html_path),
                            kpis=kpis, blockers=blockers, metrics_imported=imported)
