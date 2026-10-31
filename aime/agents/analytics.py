"""Analytics and Reporting Agent: KPIs, pipeline, content queue, blockers, agent health -> daily/weekly reports."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Literal

from jinja2 import Environment, select_autoescape
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent
from aime.contact_validation import STATUS_OK
from aime.crm_ops import CONTACT_NOT_SUPPRESSED, is_suppressed
from aime.integrations.status import integration_statuses
from aime.knowledge.industries import industry_label
from aime.knowledge.services import service_name
from aime.metrics_import import import_metrics_dir, latest_metric_ids
from aime.models import (Activity, AgentRun, Alert, Company, Contact, ContentItem, ContentMetric, FollowUp, InboundEmail,
                         InboundInquiry, Meeting,
                         Lead, Opportunity, Report, Signal)
from aime.monitoring import agent_stats
from aime.outreach import OPEN, PUBLISHED_SOURCES
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
    from_lead = (*real, InboundEmail.lead_id.is_not(None))  # mail nobody could match to a lead is no prospect's reply
    contacted = Lead.id.in_(select(FollowUp.lead_id).where(FollowUp.kind == "outreach", FollowUp.status == "sent"))
    replied = Lead.id.in_(select(InboundEmail.lead_id).where(*from_lead, InboundEmail.category.in_(REAL_REPLY)))
    interested = Lead.id.in_(select(InboundEmail.lead_id).where(*from_lead, InboundEmail.category == "interested"))
    booked = Lead.id.in_(select(Meeting.lead_id).where(Meeting.is_simulated.is_(False), Meeting.status != "cancelled"))
    read = Company.research_status.in_(("done", "no_people"))  # a site that could not be read is not research
    out = {
        "companies_researched_period": count(Company, read, Company.researched_at >= since),
        "companies_researched_total": count(Company, read, Company.researched_at.is_not(None)),
        "decision_makers_period": count(Contact, *researched, Contact.last_name.is_not(None), Contact.created_at >= since),
        "decision_makers_total": count(Contact, *researched, Contact.last_name.is_not(None)),
        "usable_emails_total": count(Contact, *researched, Contact.email_status.in_(("published", "role_address")),
                                     Contact.consent_status != "opted_out", CONTACT_NOT_SUPPRESSED),
        "outreach_drafted_period": count(FollowUp, FollowUp.kind == "outreach", FollowUp.created_at >= since),
        "outreach_drafted_total": count(FollowUp, FollowUp.kind == "outreach"),
        "outreach_sent_period": count(FollowUp, FollowUp.kind == "outreach", FollowUp.status == "sent", FollowUp.sent_at >= since),
        "outreach_sent_total": count(FollowUp, FollowUp.kind == "outreach", FollowUp.status == "sent"),
        "bounces_period": count(InboundEmail, *real, InboundEmail.category == "bounce", InboundEmail.received_at >= since),
        "bounces_total": count(InboundEmail, *real, InboundEmail.category == "bounce"),
        "unsubscribes_total": count(InboundEmail, *real, InboundEmail.category == "unsubscribe"),
        "replies_period": count(InboundEmail, *from_lead, InboundEmail.category.in_(REAL_REPLY), InboundEmail.received_at >= since),
        "replies_total": count(InboundEmail, *from_lead, InboundEmail.category.in_(REAL_REPLY)),
        "interested_period": count(InboundEmail, *from_lead, InboundEmail.category == "interested", InboundEmail.received_at >= since),
        "interested_total": count(InboundEmail, *from_lead, InboundEmail.category == "interested"),
        # The rates count prospects, each once, so none can pass 100%: replies and meetings above are events.
        "leads_contacted": count(Lead, contacted),
        "leads_contacted_replied": count(Lead, contacted, replied),
        "leads_replied": count(Lead, replied),
        "leads_interested": count(Lead, interested),
        "leads_interested_booked": count(Lead, interested, booked),
        "meetings_booked_period": count(Meeting, Meeting.is_simulated.is_(False), Meeting.status != "cancelled",
                                        Meeting.created_at >= since),
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
        ("Reply rate (leads who replied / leads contacted)", "—", _rate(k["leads_contacted_replied"], k["leads_contacted"])),
        ("Interest rate (interested leads / leads who replied)", "—", _rate(k["leads_interested"], k["leads_replied"])),
        ("Meeting rate (interested leads with a meeting / interested leads)", "—",
         _rate(k["leads_interested_booked"], k["leads_interested"])),
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
{% if window_note %}
{{ window_note }}
{% endif %}
## Summary

| Metric | {{ window_label }} | Total |
|---|---:|---:|
{% for label, day, total in summary %}| {{ label }} | {{ day }} | {{ total }} |
{% endfor %}
## Lead grades (open leads)

{% for g in "ABCD" %}- **{{ g }}**: {{ grades.get(g, 0) }}
{% endfor %}
## Can the qualified leads be reached?

{% for line in reach %}- {{ line }}
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
{% if window_note %}<p>{{ window_note }}</p>{% endif %}
<h2>Summary</h2><table><tr><th>Metric</th><th class="n">{{ window_label }}</th><th class="n">Total</th></tr>
{% for label, day, total in summary %}<tr><td>{{ label }}</td><td class="n">{{ day }}</td><td class="n">{{ total }}</td></tr>{% endfor %}</table>
<h2>Lead grades (open leads)</h2><p>{% for g in "ABCD" %}<span class="pill">{{ g }}: {{ grades.get(g, 0) }}</span> {% endfor %}</p>
<h2>Can the qualified leads be reached?</h2><ul>{% for line in reach %}<li>{{ line }}</li>{% endfor %}</ul>
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
        d = inp.report_date or self._report_date(session, datetime.now(tz), inp.kind)
        now = utcnow()
        period = timedelta(hours=24) if inp.kind == "daily" else timedelta(days=7)
        since = self._window_start(session, inp.kind, d, now, period)
        prev_since = since - period
        imported = 0
        if inp.import_metrics:
            imported, batch = import_metrics_dir(session, s.imports_dir / "linkedin_metrics")
            for problem in batch.rejected:
                session.add(Alert(level="warning", source=self.name, message=f"Metrics file not imported: {problem}"[:500]))
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
            ("Emails sent (approved replies)", kpis["emails_sent_period"] - kpis["outreach_sent_period"],
             kpis["emails_sent_total"] - kpis["outreach_sent_total"]),  # outreach has its own row below
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
        for item, metric in session.execute(
                select(ContentItem, ContentMetric).join(ContentMetric, ContentMetric.content_id == ContentItem.id)
                .where(ContentMetric.id.in_(latest_metric_ids())).order_by(ContentMetric.reactions.desc()).limit(5)).all():
            engagement.append({"title": item.title, "impressions": metric.impressions, "reactions": metric.reactions,
                               "comments": metric.comments})

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
        span = now - since
        window_note = None
        if abs(span - period) <= timedelta(hours=1 if inp.kind == "daily" else 12):
            window_label = "Last 24h" if inp.kind == "daily" else "Last 7 days"
        else:
            window_label = f"Since {fmt_local(since, s.schedule.get('timezone'))}"
            again = session.execute(select(Report.id).where(Report.report_date == d, Report.kind == inp.kind)).first()
            if span > period and not again:
                window_note = (f"The last report was {span.total_seconds() / 3600:.0f} hours ago (the PC may have been off or "
                               "asleep at report time), so this one covers all of that time.")
        reach = self._reach(session)
        trend = self._qualified_trend(session, d, kpis["leads_qualified"]) if inp.kind == "daily" else None
        if trend:
            reach.append(trend)
        data = dict(d=d.isoformat(), generated=fmt_local(now, s.schedule.get("timezone")), summary=summary,
                    reach=reach, window_note=window_note,
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
        report.summary = {"kpis": kpis, "blockers": blockers, "window": {"from": since.isoformat(), "to": now.isoformat()}}
        report.created_at = now
        session.flush()
        return ReportOutput(report_id=report.id, kind=inp.kind, report_date=d, path_md=str(md_path), path_html=str(html_path),
                            kpis=kpis, blockers=blockers, metrics_imported=imported)

    def _report_date(self, session: Session, local_now: datetime, kind: str) -> date:
        """The day a report is for. Written after midnight because the PC slept through report time, it is yesterday's:
        under today's date, today's own report would overwrite it and yesterday would have none."""
        today = local_now.date()
        slot = self.settings.schedule.get("jobs", {}).get("daily_report", {}).get("daily_at")
        if kind != "daily" or not slot or local_now.time() >= time.fromisoformat(slot):
            return today
        last = session.execute(select(func.max(Report.report_date)).where(Report.kind == "daily")).scalar_one()
        return today - timedelta(days=1) if last is not None and last < today - timedelta(days=1) else today

    @staticmethod
    def _window_start(session: Session, kind: str, d: date, now: datetime, period: timedelta) -> datetime:
        """Where this report's counts begin: where the last one ended, so that a late report leaves no gap and the next
        one does not count the same hours twice. A report written again for its day keeps its start."""
        row = session.execute(select(Report).where(Report.kind == kind, Report.report_date <= d).order_by(
            Report.report_date.desc(), Report.id.desc())).scalars().first()
        if row is None:
            return now - period
        window = (row.summary or {}).get("window") or {}
        if row.report_date == d:
            start = window.get("from")
            start = datetime.fromisoformat(start) if start else (row.created_at or now) - period
        else:
            end = window.get("to")
            start = datetime.fromisoformat(end) if end else (row.created_at or now - period)
        return start if start < now else now - period  # a clock that was wrong

    def _reach(self, session: Session) -> list[str]:
        qualified = session.execute(select(Lead).where(Lead.status == "qualified")).scalars().all()
        with_work = set(session.execute(select(FollowUp.lead_id).where(FollowUp.status.in_(OPEN))).scalars())
        closed = set(session.execute(select(FollowUp.lead_id).where(FollowUp.channel == "task", FollowUp.status == "done",
                                                                    FollowUp.blocked_reason.is_(None))).scalars())
        unreachable = sum(1 for lead in qualified if not self._usable_address(session, lead))
        by_hand = len(({lead.id for lead in qualified} & closed) - with_work)
        lines = [f"{unreachable} of {len(qualified)} qualified leads have no usable address (no published business email "
                 "that may be written to): nothing can be sent to them until a person finds another way."]
        if by_hand:
            lines.append(f"{by_hand} qualified lead(s) have nothing open: their review task was closed by hand.")
        since = utcnow() - timedelta(hours=24)
        moves = session.execute(select(Activity.subject).where(Activity.type == "status_change", Activity.actor == "qualification",
                                                               Activity.created_at >= since)).scalars().all()
        for status in ("qualified", "nurture", "disqualified"):
            if n := sum(f"→ {status} " in (m or "") for m in moves):
                lines.append(f"In the last 24 hours scoring moved {n} lead(s) to {status}.")
        return lines

    @staticmethod
    def _usable_address(session: Session, lead: Lead) -> bool:
        c = lead.contact
        if c is None or not c.email or c.consent_status == "opted_out" or is_suppressed(session, c.email, lead.company.domain):
            return False
        return c.consent_status in ("inquiry", "opt_in") or (
            c.source in PUBLISHED_SOURCES and bool(c.email_source_url) and c.email_status in STATUS_OK)

    @staticmethod
    def _qualified_trend(session: Session, d: date, today: int) -> str | None:
        rows = session.execute(select(Report).where(Report.kind == "daily", Report.report_date < d).order_by(
            Report.report_date.desc()).limit(6)).scalars().all()
        past = [r.summary["kpis"]["leads_qualified"] for r in reversed(rows)
                if "leads_qualified" in ((r.summary or {}).get("kpis") or {})]
        return f"Qualified leads, report by report: {', '.join(str(n) for n in [*past, today])}." if past else None
