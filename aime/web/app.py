"""Local lead-management dashboard and JSON API (binds to 127.0.0.1 only)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aime.agents import AGENTS
from aime.context import AppContext, get_context
from aime.crm_ops import log_activity, suppress
from aime.db import kv_get, kv_set
from aime.inbound import InboundPayload, record_inquiry
from aime.integrations.email_smtp import EmailSender
from aime.integrations.linkedin import LinkedInAPIError, LinkedInClient, get_auth_status, get_token, new_state
from aime.integrations.status import integration_statuses
from aime.knowledge.geo import US_REGIONS
from aime.knowledge.industries import INDUSTRIES, industry_label
from aime.knowledge.services import SERVICES, service_name
from aime.models import (Activity, AgentRun, Alert, Campaign, Company, ContentItem, FollowUp, InboundInquiry,
                         JobRun, JobState, Lead, MarketingPlan, Opportunity, Report, ResearchTopic, Signal)
from aime.monitoring import agent_stats, read_heartbeat
from aime.scheduler.runner import due_jobs, run_job
from aime.timeutil import fmt_et, fmt_local, utcnow

WEB_DIR = Path(__file__).resolve().parent
STAGES = ["new", "discovery", "proposal", "negotiation", "won", "lost"]
LEAD_STATUSES = ["new", "qualified", "nurture", "disqualified", "converted", "merged"]


def _404():
    raise HTTPException(404)


def safe_url(url: str | None) -> str | None:
    if url and urlparse(url).scheme in ("http", "https"):
        return url
    return None


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Block cross-site form posts (CSRF) to this local dashboard."""

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path != "/api/inbound":
            origin = request.headers.get("origin") or request.headers.get("referer")
            host = request.headers.get("host", "")
            if not origin or urlparse(origin).netloc != host:
                return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
        return await call_next(request)


def create_app(ctx: AppContext | None = None) -> FastAPI:
    app = FastAPI(title="AI Marketing Employee", docs_url="/api/docs", redoc_url=None)
    app.state.ctx = ctx
    app.add_middleware(OriginCheckMiddleware)
    hosts = [h.strip() for h in (ctx or get_context()).settings.allowed_hosts.split(",") if h.strip()]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts + ["testserver"])
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    env = templates.env
    env.filters.update(local=fmt_local, et=fmt_et, service=service_name, industry=industry_label, safe_url=safe_url)
    env.globals.update(SERVICES=SERVICES, INDUSTRIES=INDUSTRIES, STATES=US_REGIONS, STAGES=STAGES,
                       LEAD_STATUSES=LEAD_STATUSES)

    def C() -> AppContext:
        return app.state.ctx or get_context()

    def render(request: Request, name: str, **data):
        data.setdefault("msg", request.query_params.get("msg"))
        data.setdefault("company_name", C().settings.company_name)
        return templates.TemplateResponse(request, name, data)

    def back(url: str, msg: str) -> RedirectResponse:
        sep = "&" if "?" in url else "?"
        return RedirectResponse(f"{url}{sep}msg={quote(msg)}", status_code=303)

    # ------------------------------------------------------------------ overview
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        ctx = C()
        with ctx.db.session() as s:
            cnt = lambda m, *c: s.execute(select(func.count(m.id)).where(*c)).scalar_one()  # noqa: E731
            kpis = [
                ("Companies", cnt(Company, Company.merged_into_id.is_(None)), "/companies"),
                ("Open leads", cnt(Lead, Lead.status.in_(("new", "qualified", "nurture"))), "/leads"),
                ("Qualified", cnt(Lead, Lead.status == "qualified"), "/leads?status=qualified"),
                ("Grade A", cnt(Lead, Lead.grade == "A", Lead.status.in_(("new", "qualified", "nurture"))), "/leads?grade=A"),
                ("Open opportunities", cnt(Opportunity, Opportunity.stage.not_in(("won", "lost"))), "/pipeline"),
                ("Replies to approve", cnt(FollowUp, FollowUp.status == "awaiting_approval"), "/followups"),
                ("Posts to approve", cnt(ContentItem, ContentItem.status == "awaiting_approval"), "/content"),
                ("Posts published", cnt(ContentItem, ContentItem.status == "published"), "/content?status=published"),
            ]
            statuses = integration_statuses(ctx, s)
            runs = s.execute(select(JobRun).order_by(JobRun.id.desc()).limit(10)).scalars().all()
            alerts = s.execute(select(Alert).where(Alert.acknowledged.is_(False)).order_by(Alert.id.desc()).limit(10)).scalars().all()
            stats = agent_stats(s, 7)
            plan = s.execute(select(MarketingPlan).order_by(MarketingPlan.id.desc())).scalars().first()
        return render(request, "overview.html", kpis=kpis, statuses=statuses, runs=runs, alerts=alerts,
                      stats=stats, plan=plan, heartbeat=read_heartbeat(ctx.settings))

    # ------------------------------------------------------------------ leads & companies
    @app.get("/leads", response_class=HTMLResponse)
    def leads(request: Request, grade: str = "", status: str = "", state: str = "", industry: str = "",
              source: str = "", q: str = "", page: int = 1):
        with C().db.session() as s:
            stmt = select(Lead).join(Company, Company.id == Lead.company_id)
            stmt = stmt.where(Lead.status == status) if status else stmt.where(Lead.status.not_in(("merged",)))
            if grade:
                stmt = stmt.where(Lead.grade == grade)
            if state:
                stmt = stmt.where(Company.state == state)
            if industry:
                stmt = stmt.where(Company.industry == industry)
            if source:
                stmt = stmt.where(Lead.source == source)
            if q:
                stmt = stmt.where(Company.name.ilike(f"%{q}%"))
            total = s.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
            rows = s.execute(stmt.order_by(Lead.score.desc(), Lead.id.desc()).limit(100).offset((page - 1) * 100)).scalars().all()
            for r in rows:
                _ = r.company, r.contact
        return render(request, "leads.html", rows=rows, total=total, page=page,
                      f=dict(grade=grade, status=status, state=state, industry=industry, source=source, q=q))

    @app.get("/leads/{lead_id}", response_class=HTMLResponse)
    def lead_detail(request: Request, lead_id: int):
        with C().db.session() as s:
            lead = s.get(Lead, lead_id)
            if not lead:
                raise HTTPException(404)
            _ = lead.company, lead.contact
            signals = s.execute(select(Signal).where(Signal.company_id == lead.company_id).order_by(Signal.observed_at.desc())).scalars().all()
            acts = s.execute(select(Activity).where(or_(Activity.lead_id == lead.id, Activity.company_id == lead.company_id))
                             .order_by(Activity.id.desc()).limit(50)).scalars().all()
            fups = s.execute(select(FollowUp).where(FollowUp.lead_id == lead.id).order_by(FollowUp.id)).scalars().all()
            opps = s.execute(select(Opportunity).where(Opportunity.lead_id == lead.id)).scalars().all()
        return render(request, "lead_detail.html", lead=lead, signals=signals, acts=acts, fups=fups, opps=opps)

    @app.post("/leads/{lead_id}/status")
    def lead_status(lead_id: int, status: str = Form(...), note: str = Form("")):
        if status not in LEAD_STATUSES:
            raise HTTPException(400)
        with C().db.session() as s:
            lead = s.get(Lead, lead_id) or _404()
            old = lead.status
            lead.status = status
            lead.last_activity_at = utcnow()
            log_activity(s, f"Status {old} → {status} (manual)", "human", type_="status_change", body=note or None,
                         company_id=lead.company_id, lead_id=lead.id)
        return back(f"/leads/{lead_id}", "Status updated")

    @app.post("/leads/{lead_id}/note")
    def lead_note(lead_id: int, note: str = Form(...)):
        with C().db.session() as s:
            lead = s.get(Lead, lead_id) or _404()
            log_activity(s, "Note", "human", type_="note", body=note, company_id=lead.company_id, lead_id=lead.id)
            lead.last_activity_at = utcnow()
        return back(f"/leads/{lead_id}", "Note added")

    @app.post("/leads/{lead_id}/replied")
    def lead_replied(lead_id: int):
        with C().db.session() as s:
            lead = s.get(Lead, lead_id) or _404()
            for f in s.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                      FollowUp.status.in_(("pending", "awaiting_approval", "approved")))).scalars():
                f.status, f.blocked_reason = "cancelled", "Contact replied; sequence stopped"
            for o in s.execute(select(Opportunity).where(Opportunity.lead_id == lead.id, Opportunity.stage == "new")).scalars():
                o.stage, o.probability = "discovery", 20
            lead.last_activity_at = utcnow()
            log_activity(s, "Contact replied (logged manually); automated follow-ups stopped", "human",
                         type_="reply", company_id=lead.company_id, lead_id=lead.id)
        return back(f"/leads/{lead_id}", "Reply logged and sequence stopped")

    @app.get("/companies", response_class=HTMLResponse)
    def companies(request: Request, q: str = "", state: str = "", industry: str = ""):
        with C().db.session() as s:
            stmt = select(Company).where(Company.merged_into_id.is_(None))
            if q:
                stmt = stmt.where(Company.name.ilike(f"%{q}%"))
            if state:
                stmt = stmt.where(Company.state == state)
            if industry:
                stmt = stmt.where(Company.industry == industry)
            rows = s.execute(stmt.order_by(Company.id.desc()).limit(300)).scalars().all()
            lead_by_co = {l.company_id: l for l in s.execute(select(Lead).where(
                Lead.company_id.in_([c.id for c in rows]), Lead.status != "merged")).scalars()}
        return render(request, "companies.html", rows=rows, lead_by_co=lead_by_co, f=dict(q=q, state=state, industry=industry))

    # ------------------------------------------------------------------ pipeline
    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline(request: Request):
        with C().db.session() as s:
            opps = s.execute(select(Opportunity).order_by(Opportunity.updated_at.desc())).scalars().all()
            for o in opps:
                _ = o.company
        board = {st: [o for o in opps if o.stage == st] for st in STAGES}
        return render(request, "pipeline.html", board=board)

    @app.post("/opportunities/{opp_id}")
    def update_opp(opp_id: int, stage: str = Form(...), next_step: str = Form(""), est_value: str = Form("")):
        if stage not in STAGES:
            raise HTTPException(400)
        with C().db.session() as s:
            o = s.get(Opportunity, opp_id) or _404()
            old = o.stage
            o.stage, o.next_step = stage, next_step or o.next_step
            o.probability = {"new": 10, "discovery": 20, "proposal": 50, "negotiation": 70, "won": 100, "lost": 0}[stage]
            if est_value.strip():
                try:
                    o.est_value = float(est_value.replace(",", "").replace("$", ""))
                except ValueError:
                    pass
            if stage in ("won", "lost"):
                o.closed_at = utcnow()
                if stage == "won" and o.lead_id:
                    lead = s.get(Lead, o.lead_id)
                    lead.status = "converted"
            log_activity(s, f"Opportunity stage {old} → {stage}", "human", type_="opportunity",
                         company_id=o.company_id, lead_id=o.lead_id, opportunity_id=o.id)
        return back("/pipeline", "Opportunity updated")

    # ------------------------------------------------------------------ follow-ups
    @app.get("/followups", response_class=HTMLResponse)
    def followups(request: Request, show: str = "open"):
        with C().db.session() as s:
            stmt = select(FollowUp)
            if show == "open":
                stmt = stmt.where(FollowUp.status.in_(("pending", "awaiting_approval", "approved")))
            rows = s.execute(stmt.order_by(FollowUp.due_at).limit(300)).scalars().all()
            for r in rows:
                _ = r.lead.company, r.contact
        ready, reason = EmailSender(C().settings).status()
        return render(request, "followups.html", rows=rows, show=show, email_ready=ready, email_reason=reason)

    @app.post("/followups/{fid}/{action}")
    def followup_action(fid: int, action: str, subject: str = Form(""), body: str = Form("")):
        with C().db.session() as s:
            f = s.get(FollowUp, fid) or _404()
            if action == "approve" and f.channel == "email" and f.status == "awaiting_approval":
                if subject.strip():
                    f.subject = subject.strip()
                if body.strip():
                    f.body = body
                f.status, f.approved_by, f.approved_at = "approved", "dashboard user", utcnow()
                msg = "Approved. It will be sent by the follow-ups job if outbound email is enabled."
            elif action == "reject" and f.status in ("awaiting_approval", "approved", "pending"):
                f.status = "rejected"
                msg = "Rejected"
            elif action == "done" and f.channel == "task":
                f.status = "done"
                msg = "Task completed"
            else:
                raise HTTPException(400, "Action not allowed in the current state")
            log_activity(s, f"Follow-up #{f.id} {action}", "human", type_="followup", company_id=f.company_id, lead_id=f.lead_id)
        return back("/followups", msg)

    # ------------------------------------------------------------------ content
    @app.get("/content", response_class=HTMLResponse)
    def content(request: Request, status: str = ""):
        with C().db.session() as s:
            stmt = select(ContentItem)
            if status:
                stmt = stmt.where(ContentItem.status == status)
            rows = s.execute(stmt.order_by(ContentItem.scheduled_for.desc().nullslast()).limit(200)).scalars().all()
            topics = s.execute(select(ResearchTopic).order_by(ResearchTopic.observed_at.desc()).limit(12)).scalars().all()
            auth = get_auth_status(C().settings, s)
        return render(request, "content.html", rows=rows, status=status, topics=topics, auth=auth)

    @app.get("/content/{cid}", response_class=HTMLResponse)
    def content_detail(request: Request, cid: int):
        with C().db.session() as s:
            item = s.get(ContentItem, cid) or _404()
            auth = get_auth_status(C().settings, s)
        return render(request, "content_detail.html", item=item, auth=auth)

    @app.post("/content/{cid}/{action}")
    def content_action(cid: int, action: str, title: str = Form(""), body: str = Form(""), hashtags: str = Form(""),
                       scheduled_et: str = Form("")):
        with C().db.session() as s:
            item = s.get(ContentItem, cid) or _404()
            if item.status == "published":
                raise HTTPException(400, "Published posts cannot be changed here")
            if action == "save":
                item.title = title.strip() or item.title
                item.body = body.strip() or item.body
                if hashtags.strip():
                    item.hashtags = ["#" + t.lstrip("#") for t in hashtags.replace(",", " ").split() if t.strip("#")]
                if scheduled_et.strip():
                    local = datetime.fromisoformat(scheduled_et).replace(tzinfo=ZoneInfo("America/New_York"))
                    item.scheduled_for = local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
                if item.status == "approved":
                    item.status = "awaiting_approval"  # edits need a fresh approval
                msg = "Saved"
            elif action == "approve" and item.status in ("awaiting_approval", "draft", "failed"):
                item.status, item.approved_by, item.approved_at = "approved", "dashboard user", utcnow()
                item.publish_attempts = 0
                msg = "Approved for publishing at its scheduled time (requires LinkedIn authorization)"
            elif action == "reject":
                item.status = "rejected"
                msg = "Rejected"
            else:
                raise HTTPException(400, "Action not allowed in the current state")
        return back(f"/content/{cid}", msg)

    @app.get("/assets/{name}")
    def asset(name: str):
        base = C().settings.assets_dir.resolve()
        path = (base / name).resolve()
        if path.parent != base or not path.exists() or path.suffix.lower() not in (".png", ".jpg"):
            raise HTTPException(404)
        return FileResponse(path)

    # ------------------------------------------------------------------ campaigns
    @app.get("/campaigns", response_class=HTMLResponse)
    def campaigns(request: Request):
        with C().db.session() as s:
            rows = s.execute(select(Campaign).order_by(Campaign.start_date.desc())).scalars().all()
            post_counts = dict(s.execute(select(ContentItem.campaign_id, func.count(ContentItem.id))
                                         .group_by(ContentItem.campaign_id)).all())
            plans = s.execute(select(MarketingPlan).order_by(MarketingPlan.id.desc()).limit(7)).scalars().all()
        return render(request, "campaigns.html", rows=rows, post_counts=post_counts, plans=plans)

    @app.post("/campaigns")
    def create_campaign(name: str = Form(...), objective: str = Form(...), service: str = Form(""),
                        industry: str = Form(""), start_date: str = Form(...), end_date: str = Form(...)):
        with C().db.session() as s:
            s.add(Campaign(name=name, objective=objective, service=service or None, industry=industry or None,
                           start_date=date.fromisoformat(start_date), end_date=date.fromisoformat(end_date),
                           status="active", created_by="human"))
        return back("/campaigns", "Campaign created")

    @app.post("/campaigns/{camp_id}/status")
    def campaign_status(camp_id: int, status: str = Form(...)):
        if status not in ("planned", "active", "paused", "completed"):
            raise HTTPException(400)
        with C().db.session() as s:
            c = s.get(Campaign, camp_id) or _404()
            c.status = status
        return back("/campaigns", "Campaign updated")

    # ------------------------------------------------------------------ inbound
    @app.get("/inbound", response_class=HTMLResponse)
    def inbound(request: Request):
        with C().db.session() as s:
            rows = s.execute(select(InboundInquiry).order_by(InboundInquiry.id.desc()).limit(100)).scalars().all()
        return render(request, "inbound.html", rows=rows)

    @app.post("/inbound")
    async def inbound_submit(request: Request):
        form = await request.form()
        data = {k: v for k, v in form.items() if isinstance(v, str)}
        data["services_interested"] = form.getlist("services_interested")
        data["consent_marketing"] = "consent_marketing" in form
        with C().db.session() as s:
            _, created = record_inquiry(s, data, "manual")
        return back("/inbound", "Inquiry recorded; the inbound job will process it" if created else "Duplicate inquiry ignored")

    @app.post("/inbound/process")
    def inbound_process(background: BackgroundTasks):
        background.add_task(run_job, C(), "inbound", "dashboard")
        return back("/inbound", "Inbound job started")

    @app.post("/api/inbound")
    async def api_inbound(request: Request):
        token = C().settings.inbound_api_token
        if token and request.headers.get("x-inbound-token") != token:
            raise HTTPException(401, "Invalid inbound token")
        payload = await request.json()
        InboundPayload.model_validate(payload)
        with C().db.session() as s:
            row, created = record_inquiry(s, payload, "api")
            rid = row.id if row else None
        return JSONResponse({"id": rid, "created": created}, status_code=201 if created else 200)

    @app.post("/suppress")
    def suppress_form(value: str = Form(...), reason: str = Form("Requested by contact")):
        with C().db.session() as s:
            suppress(s, value, reason)
        return back("/followups", f"{value} added to the do-not-contact list")

    # ------------------------------------------------------------------ agents, jobs, reports
    @app.get("/agents", response_class=HTMLResponse)
    def agents(request: Request, agent: str = ""):
        with C().db.session() as s:
            stats = {r["agent"]: r for r in agent_stats(s, 7)}
            stmt = select(AgentRun).order_by(AgentRun.id.desc()).limit(100)
            if agent:
                stmt = stmt.where(AgentRun.agent == agent)
            runs = s.execute(stmt).scalars().all()
        team = [cls.describe() for cls in AGENTS.values()]
        return render(request, "agents.html", team=team, stats=stats, runs=runs, agent=agent)

    @app.get("/agents/runs/{run_pk}", response_class=HTMLResponse)
    def agent_run(request: Request, run_pk: int):
        with C().db.session() as s:
            run = s.get(AgentRun, run_pk) or _404()
        return render(request, "agent_run.html", run=run)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request):
        ctx = C()
        with ctx.db.session() as s:
            states = {st.job: st for st in s.execute(select(JobState)).scalars()}
            runs = s.execute(select(JobRun).order_by(JobRun.id.desc()).limit(60)).scalars().all()
        from aime.scheduler import windows
        return render(request, "jobs.html", due=due_jobs(ctx), states=states, runs=runs,
                      heartbeat=read_heartbeat(ctx.settings), task=windows.query())

    @app.post("/jobs/{name}/run")
    def job_run(name: str, background: BackgroundTasks):
        from aime.workflows import JOBS
        if name not in JOBS:
            raise HTTPException(404)
        background.add_task(run_job, C(), name, "dashboard")
        return back("/jobs", f"Job '{name}' started in the background")

    @app.get("/jobs/runs/{run_id}", response_class=HTMLResponse)
    def job_run_detail(request: Request, run_id: int):
        with C().db.session() as s:
            run = s.get(JobRun, run_id) or _404()
        return render(request, "job_run.html", run=run)

    @app.post("/alerts/{alert_id}/ack")
    def ack_alert(alert_id: int):
        with C().db.session() as s:
            a = s.get(Alert, alert_id)
            if a:
                a.acknowledged = True
        return back("/", "Alert acknowledged")

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request):
        with C().db.session() as s:
            rows = s.execute(select(Report).order_by(Report.report_date.desc()).limit(60)).scalars().all()
        return render(request, "reports.html", rows=rows)

    @app.get("/reports/{rid}", response_class=HTMLResponse)
    def report_view(rid: int):
        with C().db.session() as s:
            r = s.get(Report, rid) or _404()
        path = Path(r.path_html)
        if not path.exists():
            raise HTTPException(404, "Report file missing")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ integrations
    @app.get("/integrations", response_class=HTMLResponse)
    def integrations(request: Request):
        ctx = C()
        with ctx.db.session() as s:
            statuses = integration_statuses(ctx, s)
            auth = get_auth_status(ctx.settings, s)
        return render(request, "integrations.html", statuses=statuses, auth=auth, settings=ctx.settings)

    @app.get("/integrations/linkedin/connect")
    def linkedin_connect():
        ctx = C()
        if not ctx.settings.linkedin_configured:
            return back("/integrations", "Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in .env first")
        state = new_state()
        with ctx.db.session() as s:
            kv_set(s, "linkedin.oauth_state", {"state": state, "at": utcnow().isoformat()})
        return RedirectResponse(LinkedInClient(ctx.settings).authorization_url(state), status_code=302)

    @app.get("/integrations/linkedin/callback")
    def linkedin_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
        ctx = C()
        if error:
            return back("/integrations", f"LinkedIn authorization declined: {error_description or error}")
        with ctx.db.session() as s:
            saved = kv_get(s, "linkedin.oauth_state") or {}
            kv_set(s, "linkedin.oauth_state", None)
            fresh = saved.get("at") and utcnow() - datetime.fromisoformat(saved["at"]) < timedelta(minutes=15)
            if not state or state != saved.get("state") or not fresh:
                return back("/integrations", "Invalid or expired OAuth state; please try again")
            client = LinkedInClient(ctx.settings, transport=ctx.http_transport)
            try:
                tok = client.exchange_code(code)
                info = client.userinfo(tok["access_token"])
            except LinkedInAPIError as exc:
                return back("/integrations", f"LinkedIn token exchange failed: {exc}")
            client.store_token(s, tok, info)
        return back("/integrations", "LinkedIn connected")

    @app.post("/integrations/linkedin/disconnect")
    def linkedin_disconnect():
        with C().db.session() as s:
            tok = get_token(s)
            if tok:
                s.delete(tok)
        return back("/integrations", "LinkedIn token removed from this computer")

    # ------------------------------------------------------------------ JSON API
    @app.get("/api/health")
    def api_health():
        ctx = C()
        with ctx.db.session() as s:
            statuses = integration_statuses(ctx, s, include_scheduler=True)
            failing = [r for r in s.execute(select(JobState).where(JobState.consecutive_failures >= 2)).scalars()]
        return {"ok": not failing, "heartbeat": read_heartbeat(ctx.settings),
                "failing_jobs": [f.job for f in failing], "integrations": statuses}

    @app.get("/api/stats")
    def api_stats():
        with C().db.session() as s:
            return {
                "companies": s.execute(select(func.count(Company.id)).where(Company.merged_into_id.is_(None))).scalar_one(),
                "leads_by_status": dict(s.execute(select(Lead.status, func.count(Lead.id)).group_by(Lead.status)).all()),
                "leads_by_grade": dict(s.execute(select(Lead.grade, func.count(Lead.id)).group_by(Lead.grade)).all()),
                "content_by_status": dict(s.execute(select(ContentItem.status, func.count(ContentItem.id))
                                                    .group_by(ContentItem.status)).all()),
                "opportunities_by_stage": dict(s.execute(select(Opportunity.stage, func.count(Opportunity.id))
                                                         .group_by(Opportunity.stage)).all()),
            }

    @app.get("/api/leads")
    def api_leads(status: str = "qualified", limit: int = 50):
        with C().db.session() as s:
            rows = s.execute(select(Lead).where(Lead.status == status).order_by(Lead.score.desc()).limit(min(limit, 500))).scalars().all()
            return [{
                "id": l.id, "company": l.company.name, "state": l.company.state, "industry": l.company.industry,
                "website": l.company.website, "score": l.score, "grade": l.grade, "source": l.source,
                "primary_service": l.primary_service, "status": l.status,
            } for l in rows]

    return app
