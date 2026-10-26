"""Analytics and Reporting Agent: KPIs, pipeline, content queue, blockers, agent health -> daily/weekly reports."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Literal

from jinja2 import Environment, select_autoescape
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent
from aime.integrations.status import integration_statuses
from aime.knowledge.industries import industry_label
from aime.knowledge.services import service_name
from aime.metrics_import import import_metrics_dir
from aime.models import (Activity, AgentRun, Alert, Company, Contact, ContentItem, ContentMetric, FollowUp, InboundEmail,
                         InboundInquiry, Meeting,
                         Lead, Opportunity, Report, Signal)
from aime.monitoring import agent_stats
from aime.timeutil import fmt_et, fmt_local, local_tz, utcnow

PROBLEM_STATES = {"NOT CONFIGURED", "NOT CONNECTED", "BLOCKED", "DISABLED", "UNAVAILABLE", "NOT RUNNING", "STALE"}


def md_safe(value):
    """Neutralise HTML and table syntax in values that come from public sources (company names, titles)."""
    if isinstance(value, str):
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "/")
    return value


REAL_REPLY = ("interested", "not_interested", "objection", "referral", "question", "other")


def _rate(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}% ({part}/{whole})" if whole else "—"


def sales_kpis(count, since) -> dict:
    """Sales pipeline counts from records only. Simulated replies and meetings are kept apart."""
    researched = (Contact.source.in_(("website", "sec_filing")),)
    real = (InboundEmail.is_simulated.is_(False),)
    out = {
        "companies_researched_period": count(Company, Company.researched_at >= since),
        "companies_researched_total": count(Company, Company.researched_at.is_not(None)),
        "decision_makers_period": count(Contact, *researched, Contact.last_name.is_not(None), Contact.created_at >= since),
        "decision_makers_total": count(Contact, *researched, Contact.last_name.is_not(None)),
        "usable_emails_total": count(Contact, Contact.email_status.in_(("published", "role_address"))),
        "outreach_drafted_period": count(FollowUp, FollowUp.kind == "outreach", FollowUp.created_at >= since),
        "outreach_drafted_total": count(FollowUp, FollowUp.kind == "outreach"),
        "outreach_sent_period": count(FollowUp, FollowUp.kind == "outreach", FollowUp.status == "sent", FollowUp.sent_at >= since),
        "outreach_sent_total": count(FollowUp, FollowUp.kind == "outreach", FollowUp.status == "sent"),
        "bounces_period": count(InboundEmail, *real, InboundEmail.category == "bounce", InboundEmail.received_at >= since),
        "bounces_total": count(InboundEmail, *real, InboundEmail.category == "bounce"),
        "unsubscribes_total": count(InboundEmail, *real, InboundEmail.category == "unsubscribe"),
        "replies_period": count(InboundEmail, *real, InboundEmail.category.in_(REAL_REPLY), InboundEmail.received_at >= since),
        "replies_total": count(InboundEmail, *real, InboundEmail.category.in_(REAL_REPLY)),
        "interested_period": count(InboundEmail, *real, InboundEmail.category == "interested", InboundEmail.received_at >= since),
        "interested_total": count(InboundEmail, *real, InboundEmail.category == "interested"),
        "meetings_booked_period": count(Meeting, Meeting.is_simulated.is_(False), Meeting.created_at >= since),
        "meetings_booked_total": count(Meeting, Meeting.is_simulated.is_(False), Meeting.status != "cancelled"),
        "meetings_held_total": count(Meeting, Meeting.is_simulated.is_(False), Meeting.status == "completed"),
        "simulated_replies_total": count(InboundEmail, InboundEmail.is_simulated.is_(True)),
        "simulated_meetings_total": count(Meeting, Meeting.is_simulated.is_(True)),
        "replies_to_review": count(InboundEmail, InboundEmail.status == "needs_review"),
    }
    return out


def sales_rows(k: dict) -> list[tuple]:
    rows = [
        ("Companies researched (own website / SEC)", k["companies_researched_period"], k["companies_researched_total"]),
        ("Decision-makers identified (with evidence)", k["decision_makers_period"], k["decision_makers_total"]),
        ("Usable business emails (published)", "—", k["usable_emails_total"]),
        ("Outreach drafted (awaiting or past approval)", k["outreach_drafted_period"], k["outreach_drafted_total"]),
        ("Outreach sent (approved)", k["outreach_sent_period"], k["outreach_sent_total"]),
        ("Bounces / unsubscribes", k["bounces_period"], f"{k['bounces_total']} / {k['unsubscribes_total']}"),
        ("Replies from prospects", k["replies_period"], k["replies_total"]),
        ("Interested replies", k["interested_period"], k["interested_total"]),
        ("Meetings booked / held", k["meetings_booked_period"], f"{k['meetings_booked_total']} / {k['meetings_held_total']}"),
        ("Reply rate (replies / outreach sent)", "—", _rate(k["replies_total"], k["outreach_sent_total"])),
        ("Interest rate (interested / replies)", "—", _rate(k["interested_total"], k["replies_total"])),
        ("Meeting rate (meetings / interested)", "—", _rate(k["meetings_booked_total"], k["interested_total"])),
    ]
    if k["simulated_replies_total"] or k["simulated_meetings_total"]:
        rows.append(("SIMULATED test activity (not counted above): replies / meetings", "—",
                     f"{k['simulated_replies_total']} / {k['simulated_meetings_total']}"))
    return rows


class ReportInput(BaseModel):
    report_date: date | None = None
    kind: Literal["daily", "weekly"] = "daily"
    import_metrics: bool = True


class ReportOutput(BaseModel):
    report_id: int
    kind: str = "daily"
    report_date: date
    path_md: str
    path_html: str
    kpis: dict[str, int] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    metrics_imported: int = 0


MD_TEMPLATE = """# {{ title }} — {{ d }}

Generated {{ generated }} (local time). Window: {{ window_label|lower }}. All figures are counts of records in the local CRM.

## Summary

| Metric | {{ window_label }} | Total |
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
## Alerts ({{ window_label|lower }})

{% if alerts %}{% for a in alerts %}- [{{ a.level }}] {{ a.source }}: {{ a.message }}
{% endfor %}{% else %}None.
{% endif %}
{% if breakdowns %}## Segments (open leads)

{% for name, rows in breakdowns.items() %}**{{ name }}:** {% for k, v in rows %}{{ k }} {{ v }}{{ ", " if not loop.last }}{% endfor %}

{% endfor %}{% endif %}{% if previous %}## Compared with the previous period

| Metric | This period | Previous period |
|---|---:|---:|
{% for label, now_v, prev_v in previous %}| {{ label }} | {{ now_v }} | {{ prev_v }} |
{% endfor %}
{% endif %}{% if engagement %}## Content performance (imported analytics)

| Post | Impressions | Reactions | Comments |
|---|---:|---:|---:|
{% for e in engagement %}| {{ e.title }} | {{ e.impressions }} | {{ e.reactions }} | {{ e.comments }} |
{% endfor %}{% endif %}"""

HTML_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{{ title }} {{ d }}</title>
<style>
body{font-family:Segoe UI,system-ui,sans-serif;margin:24px;color:#1d2433;background:#fff;max-width:1100px}
h1{font-size:24px}h2{font-size:18px;margin-top:28px;border-bottom:1px solid #e3e7ef;padding-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid #eef1f6;padding:6px 8px;text-align:left}
td.n,th.n{text-align:right}.muted{color:#667085}.pill{display:inline-block;padding:1px 8px;border-radius:10px;background:#eef2ff}
</style></head><body>
<h1>{{ title }} — {{ d }}</h1>
<p class="muted">Generated {{ generated }} (local time). Window: {{ window_label|lower }}. Counts of records in the local CRM.</p>
<h2>Summary</h2><table><tr><th>Metric</th><th class="n">{{ window_label }}</th><th class="n">Total</th></tr>
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
{% if breakdowns %}<h2>Segments (open leads)</h2>{% for name, rows in breakdowns.items() %}<p><strong>{{ name }}:</strong> {% for k, v in rows %}<span class="pill">{{ k }}: {{ v }}</span> {% endfor %}</p>{% endfor %}{% endif %}
{% if previous %}<h2>Compared with the previous period</h2><table><tr><th>Metric</th><th class="n">This period</th><th class="n">Previous</th></tr>{% for label, a, b in previous %}<tr><td>{{ label }}</td><td class="n">{{ a }}</td><td class="n">{{ b }}</td></tr>{% endfor %}</table>{% endif %}
<h2>Alerts ({{ window_label|lower }})</h2>{% if alerts %}<ul>{% for a in alerts %}<li>[{{ a.level }}] {{ a.source }}: {{ a.message }}</li>{% endfor %}</ul>{% else %}<p>None.</p>{% endif %}
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
        period = timedelta(hours=24) if inp.kind == "daily" else timedelta(days=7)
        since = now - period
        prev_since = since - period
        imported = 0
        if inp.import_metrics:
            imported, batch = import_metrics_dir(session, s.imports_dir / "linkedin_metrics")
            session.commit()  # metrics are saved before their files leave the import folder
            batch.archive()

        def count(model, *conds):
            return session.execute(select(func.count(model.id)).where(*conds)).scalar_one()

        active_co = Company.merged_into_id.is_(None)
        open_lead = Lead.status.in_(("new", "qualified", "nurture"))
        kpis = {
            "companies_total": count(Company, active_co),
            "companies_new_period": count(Company, active_co, Company.created_at >= since),
            "signals_new_period": count(Signal, Signal.created_at >= since),
            "leads_open": count(Lead, open_lead),
            "leads_new_period": count(Lead, Lead.created_at >= since),
            "leads_qualified": count(Lead, Lead.status == "qualified"),
            "qualified_period": count(Activity, Activity.type == "status_change", Activity.created_at >= since,
                                   Activity.subject.like("%→ qualified%")),
            "inbound_period": count(InboundInquiry, InboundInquiry.received_at >= since),
            "inbound_total": count(InboundInquiry),
            "opportunities_open": count(Opportunity, Opportunity.stage.not_in(("won", "lost"))),
            "opportunities_new_period": count(Opportunity, Opportunity.created_at >= since),
            "followups_awaiting_approval": count(FollowUp, FollowUp.status == "awaiting_approval"),
            "tasks_pending": count(FollowUp, FollowUp.channel == "task", FollowUp.status == "pending"),
            "emails_sent_period": count(FollowUp, FollowUp.status == "sent", FollowUp.sent_at >= since),
            "emails_sent_total": count(FollowUp, FollowUp.status == "sent"),
            "content_created_period": count(ContentItem, ContentItem.created_at >= since),
            "content_awaiting_approval": count(ContentItem, ContentItem.status == "awaiting_approval"),
            "content_approved_queued": count(ContentItem, ContentItem.status == "approved"),
            "content_published_period": count(ContentItem, ContentItem.status == "published", ContentItem.published_at >= since),
            "content_published_total": count(ContentItem, ContentItem.status == "published"),
            "agent_runs_period": count(AgentRun, AgentRun.started_at >= since),
            "agent_failures_period": count(AgentRun, AgentRun.started_at >= since, AgentRun.status == "failed"),
        }
        kpis.update(sales_kpis(count, since))
        summary = [
            ("Companies discovered", kpis["companies_new_period"], kpis["companies_total"]),
            ("Evidence signals", kpis["signals_new_period"], count(Signal)),
            ("Leads", kpis["leads_new_period"], kpis["leads_open"]),
            ("Qualified leads", kpis["qualified_period"], kpis["leads_qualified"]),
            ("Inbound inquiries", kpis["inbound_period"], kpis["inbound_total"]),
            ("Opportunities", kpis["opportunities_new_period"], kpis["opportunities_open"]),
            ("Emails sent (approved replies)", kpis["emails_sent_period"], kpis["emails_sent_total"]),
            ("Posts created", kpis["content_created_period"], count(ContentItem)),
            ("Posts published (confirmed by LinkedIn)", kpis["content_published_period"], kpis["content_published_total"]),
            ("Agent runs / failures", f"{kpis['agent_runs_period']} / {kpis['agent_failures_period']}", "—"),
        ]
        summary += sales_rows(kpis)
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

        previous, breakdowns = [], {}
        if inp.kind == "weekly":
            def between(model, col, *conds):
                return count(model, col >= prev_since, col < since, *conds)
            previous = [
                ("Companies discovered", kpis["companies_new_period"], between(Company, Company.created_at, active_co)),
                ("Leads", kpis["leads_new_period"], between(Lead, Lead.created_at)),
                ("Qualified leads", kpis["qualified_period"], between(Activity, Activity.created_at,
                                                                      Activity.type == "status_change",
                                                                      Activity.subject.like("%→ qualified%"))),
                ("Inbound inquiries", kpis["inbound_period"], between(InboundInquiry, InboundInquiry.received_at)),
                ("Posts created", kpis["content_created_period"], between(ContentItem, ContentItem.created_at)),
            ]
            open_rows = session.execute(select(Lead, Company).join(Company, Company.id == Lead.company_id)
                                        .where(open_lead)).all()
            breakdowns = {
                "Category": Counter(lead.category or "unscored" for lead, _ in open_rows).most_common(),
                "Industry": Counter(industry_label(co.industry) for _, co in open_rows).most_common(8),
                "State": Counter(co.state or "unknown" for _, co in open_rows).most_common(10),
                "Website check": Counter(co.website_status or "unchecked" for _, co in open_rows).most_common(),
            }
        title = "Daily Marketing Report" if inp.kind == "daily" else "Weekly Marketing Report"
        window_label = "Last 24h" if inp.kind == "daily" else "Last 7 days"
        data = dict(d=d.isoformat(), generated=fmt_local(now, s.schedule.get("timezone")), summary=summary,
                    grades=grades, stages=stages, top_leads=top_leads, queue=queue, blockers=blockers,
                    agents=agent_stats(session, 7), alerts=alerts, engagement=engagement, title=title,
                    window_label=window_label, previous=previous, breakdowns=breakdowns)
        # Markdown output: values are escaped by md_safe (HTML characters and table pipes) instead of HTML autoescape.
        md = Environment(autoescape=False, finalize=md_safe).from_string(MD_TEMPLATE).render(**data)  # noqa: S701
        html = Environment(autoescape=select_autoescape(default=True)).from_string(HTML_TEMPLATE).render(**data)
        s.reports_dir.mkdir(parents=True, exist_ok=True)
        stem = f"daily_{d.isoformat()}" if inp.kind == "daily" else f"weekly_{d.isocalendar().year}-W{d.isocalendar().week:02d}"
        md_path = s.reports_dir / f"{stem}.md"
        html_path = s.reports_dir / f"{stem}.html"
        md_path.write_text(md, encoding="utf-8")
        html_path.write_text(html, encoding="utf-8")

        report = session.execute(select(Report).where(Report.report_date == d, Report.kind == inp.kind)).scalars().first()
        if report is None:
            report = Report(report_date=d, kind=inp.kind, path_md=str(md_path), path_html=str(html_path))
            session.add(report)
        report.summary = {"kpis": kpis, "blockers": blockers}
        report.created_at = now
        session.flush()
        return ReportOutput(report_id=report.id, kind=inp.kind, report_date=d, path_md=str(md_path), path_html=str(html_path),
                            kpis=kpis, blockers=blockers, metrics_imported=imported)
