"""The Sales Manager's dashboard (/sales-manager) and the Super Admin's settings for the emailed report
(/admin/reporting). Both read the figures from aime/manager_report.py, so the page, the stored report and the email
say the same thing with the same definitions.

Who may see what:
- /sales-manager: a Sales Manager and every role above (Sales Director, Administrator, Super Admin), and a custom role
  that holds at least what a Sales Manager holds. The page shows totals for the whole organisation, the same totals the
  emailed report carries; the record ids behind each figure are listed only for a role that sees every record
  (view_all), since a Sales Manager's own role sees their team's records only.
- /admin/reporting: Super Admin only (configure_system), viewing included. Every change and every authorization is in
  the audit log.
The pages run no JavaScript (the dashboard's CSP is script-src 'none'): charts are server-drawn SVG and every detail
opens with <details>."""

from __future__ import annotations

from datetime import timedelta

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from aime import manager_report as mr
from aime.db import take_write_lock
from aime.models import Company, Contact, FollowUp, InboundEmail, Report, ReportDelivery
from aime.permissions import BUILTIN_ROLES, effective
from aime.timeutil import as_aware_utc, utcnow

MANAGER_RANK = BUILTIN_ROLES["sales_manager"]["rank"]
MANAGER_PERMS = effective(BUILTIN_ROLES["sales_manager"]["permissions"] - {"receive_leads"})
PERIODS = {"today": "Today so far", "daily": "Yesterday", "weekly": "Last week", "monthly": "Last month"}
CHART_DAYS = 14


def may_view(settings, user: dict | None) -> bool:
    """A Sales Manager or above: a built-in role ranked at least Sales Manager, or a custom role holding everything a
    Sales Manager holds. Without sign-in (DASHBOARD_AUTH off) everyone is the owner."""
    if not settings.dashboard_auth:
        return True
    if not user:
        return False
    role = BUILTIN_ROLES.get(user.get("role"))
    if role is not None:
        return role["rank"] >= MANAGER_RANK
    return MANAGER_PERMS <= effective(user.get("perms", ()))


def super_admin(settings, user: dict | None) -> bool:
    return not settings.dashboard_auth or bool(user and "configure_system" in user.get("perms", ()))


def sees_all(settings, user: dict | None) -> bool:
    return not settings.dashboard_auth or bool(user and "view_all" in user.get("perms", ()))


def period_for(choice: str, cfg: dict, now) -> mr.Period:
    today = mr.local_today(cfg, now)
    if choice == "today":
        return mr.Period("daily", today, today + timedelta(days=1), cfg["timezone"], until=now)
    return mr.latest_period(choice, today, cfg)


def daily_series(s, data: dict, cfg: dict, now) -> dict[str, list[int]]:
    """Per day over the last two weeks, from the same records the figures count (their ids), on the report's clock."""
    from zoneinfo import ZoneInfo

    from aime.web import charts

    tz = ZoneInfo(cfg["timezone"])
    today = as_aware_utc(now).astimezone(tz).date()
    ids = data["full_ids"]
    when = {"companies_discovered": (Company.created_at, Company.id), "verified_decision_makers": (Contact.created_at, Contact.id),
            "outreach_sent": (FollowUp.sent_at, FollowUp.id), "replies": (InboundEmail.received_at, InboundEmail.id)}
    out = {}
    for key, (col, idcol) in when.items():
        dates = s.execute(select(col).where(idcol.in_(ids.get(key) or [-1]))).scalars().all()
        out[key] = charts.daily_counts(dates, CHART_DAYS, today, tz)
    return out


def dashboard(ctx, s, choice: str, user: dict | None) -> dict:
    from aime.web import charts

    now = utcnow()
    cfg = mr.config(s, ctx.settings)
    choice = choice if choice in PERIODS else "today"
    period = period_for(choice, cfg, now)
    data = mr.build(s, ctx.settings, period, now)
    m = data["metrics"]
    today = mr.local_today(cfg, now)
    window = mr.Period("daily", today - timedelta(days=CHART_DAYS - 1), today + timedelta(days=1), cfg["timezone"], until=now)
    series = daily_series(s, mr.build(s, ctx.settings, window, now, full_ids=True), cfg, now)
    labels = [(today - timedelta(days=CHART_DAYS - 1 - i)).strftime("%d %b") for i in range(CHART_DAYS)]
    funnel_keys = ("companies_discovered", "decision_makers", "contactable_prospects", "verified_decision_makers",
                   "qualified_leads", "outreach_sent", "replies", "meetings_booked")
    sections = [(heading, [m[k] for k in keys if k in m]) for heading, keys in mr.ORDER]
    stored = s.execute(select(Report).where(Report.kind.in_(tuple(mr.KINDS.values())))
                       .order_by(Report.report_date.desc(), Report.id.desc()).limit(30)).scalars().all()
    deliveries = {(d.kind, d.period_start): d for d in s.execute(select(ReportDelivery)).scalars()}
    reports = [{"r": r, "delivery": deliveries.get((r.kind, r.report_date)),
                "note": (r.summary or {}).get("delivery") or {}, "highlights": (r.summary or {}).get("highlights")}
               for r in stored]
    limit = int(ctx.settings.section("outreach").get("mailbox_daily_limit", 50))
    from sqlalchemy import func

    from aime.automation import COUNTS_AS_SENT

    since = now - timedelta(hours=24)
    mailbox_used = s.execute(select(func.count(FollowUp.id)).where(
        FollowUp.channel == "email", FollowUp.status.in_(COUNTS_AS_SENT),
        func.coalesce(FollowUp.sent_at, FollowUp.send_started_at) >= since)).scalar_one()
    report_used = mr.mailbox_sends_since(s, since)
    return dict(
        data=data, m=m, sections=sections, period=period, choice=choice, periods=PERIODS, cfg=cfg,
        show_ids=sees_all(ctx.settings, user), reports=reports, shown=mr.shown, labels=mr.LABELS,
        mailbox={"limit": limit, "outreach": mailbox_used, "reports": report_used},
        auth=mr.authorization(cfg), send_blockers=mr.blockers(s, ctx.settings, cfg, now),
        chart_trend=charts.combo_chart(labels, [("Companies discovered", series["companies_discovered"]),
                                                ("Verified decision-makers", series["verified_decision_makers"])],
                                       [("Outreach sent", series["outreach_sent"]), ("Replies", series["replies"])],
                                       f"The last {CHART_DAYS} days", f"Nothing recorded in the last {CHART_DAYS} days.",
                                       height=260, uid="mgr-trend"),
        chart_funnel=charts.bar_list([(m[k]["label"], m[k]["value"], None) for k in funnel_keys],
                                     "Funnel", "No data for this period: nothing was recorded in it."),
        chart_stages=charts.bar_list([(r["stage"].title(), r["value"], "/pipeline") for r in data["pipeline"]["by_stage_now"]],
                                     "Deals by stage now", "No deals yet."),
    )


def register(app, C, render, back, who) -> None:
    def user_of(request: Request) -> dict | None:
        return getattr(request.state, "user", None)

    def need_manager(request: Request) -> None:
        if not may_view(C().settings, user_of(request)):
            raise HTTPException(403, "The Sales Manager dashboard is for Sales Managers and the roles above them")

    def need_super_admin(request: Request) -> None:
        if not super_admin(C().settings, user_of(request)):
            raise HTTPException(403, "Super Admin only")

    @app.get("/sales-manager", response_class=HTMLResponse)
    def sales_manager_page(request: Request, period: str = "today"):
        need_manager(request)
        ctx = C()
        with ctx.db.session() as s:
            view = dashboard(ctx, s, period, user_of(request))
        return render(request, "sales_manager.html", **view)

    @app.get("/sales-manager/reports/{rid}", response_class=HTMLResponse)
    def sales_manager_report_view(request: Request, rid: int):
        """A stored Sales Manager report, for the same people as the dashboard (Sales Analytics, /reports, needs a role
        that sees every record). Only the manager's reports: any other stored report is not found here."""
        from pathlib import Path, PureWindowsPath

        from aime.web.app import REPORT_BACK, report_page

        need_manager(request)
        ctx = C()
        with ctx.db.session() as s:
            r = s.get(Report, rid)
            if r is None or r.kind not in mr.KINDS.values():
                raise HTTPException(404, "Not Found")
        path = ctx.settings.reports_dir / PureWindowsPath(r.path_html or "").name
        if not path.is_file():
            path = Path(r.path_html or "")
        if not path.is_file():
            raise HTTPException(404, "Report file missing")
        return HTMLResponse(report_page(path.read_text(encoding="utf-8")).replace(
            REPORT_BACK, '<a class="report-back" href="/sales-manager">&larr; Sales Manager report</a>', 1))

    def admin_view(request: Request, **extra):
        ctx = C()
        with ctx.db.session() as s:
            cfg = mr.config(s, ctx.settings)
            view = dict(cfg=cfg, auth=mr.authorization(cfg), weekdays=mr.WEEKDAYS,
                        sender=mr.sending_address(s, ctx.settings) or None,
                        problems=mr.recipient_problems(s, ctx.settings, cfg),
                        send_blockers=mr.blockers(s, ctx.settings, cfg, utcnow()), **extra)
        return render(request, "admin_reporting.html", **view)

    @app.get("/admin/reporting", response_class=HTMLResponse)
    def reporting_settings_page(request: Request):
        need_super_admin(request)
        return admin_view(request)

    @app.post("/admin/reporting")
    def reporting_settings_save(request: Request, recipients: str = Form(""), allowlist: str = Form(""),
                                daily_time: str = Form("04:00"), timezone: str = Form("Asia/Kolkata"),
                                weekly_day: str = Form("mon"), monthly_day: str = Form("1"), email_enabled: str = Form("")):
        need_super_admin(request)
        ctx = C()
        form = {"recipients": recipients, "allowlist": allowlist, "daily_time": daily_time, "timezone": timezone,
                "weekly_day": weekly_day, "monthly_day": monthly_day, "email_enabled": email_enabled == "yes"}
        try:
            with ctx.db.session() as s:
                take_write_lock(s)
                changes = mr.save(s, ctx.settings, who(request), form)
        except ValueError as exc:
            return back("/admin/reporting", f"Not saved: {exc}")
        return back("/admin/reporting", ("Saved: " + "; ".join(changes)) if changes else "Nothing changed")

    @app.post("/admin/reporting/authorize")
    def reporting_authorize(request: Request):
        need_super_admin(request)
        ctx = C()
        try:
            with ctx.db.session() as s:
                take_write_lock(s)
                note = mr.authorize(s, ctx.settings, who(request))
        except ValueError as exc:
            return back("/admin/reporting", f"Not authorized: {exc}")
        return back("/admin/reporting", note)

    @app.post("/admin/reporting/revoke")
    def reporting_revoke(request: Request):
        need_super_admin(request)
        ctx = C()
        with ctx.db.session() as s:
            take_write_lock(s)
            mr.revoke(s, who(request))
        return back("/admin/reporting", "Authorization withdrawn: the report is not emailed until it is authorized again")

    @app.post("/admin/reporting/write")
    def reporting_write_now(request: Request):
        """Write and store the due reports now, from the records as they are. Never emails."""
        need_super_admin(request)
        result = mr.run(C(), actor=who(request), send=False)
        return back("/sales-manager", f"Written: {', '.join(r['kind'] + ' ' + r['period'] for r in result['reports'])} (not emailed)")
