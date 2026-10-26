"""Local lead-management dashboard and JSON API (binds to 127.0.0.1 only)."""

from __future__ import annotations

import hmac
import json
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aime import maintenance
from aime.agents import AGENTS
from aime.context import AppContext, get_context
from aime.crm_ops import log_activity, suppress
from aime.db import kv_get, kv_set
from aime.inbound import InboundPayload, record_inquiry
from aime.integrations.email_smtp import EmailSender
from aime.integrations.linkedin import (POST_REF_RE, LinkedInAPIError, LinkedInClient, get_auth_status, get_token,
                                        new_state, post_url)
from aime.integrations.status import integration_statuses
from aime.knowledge.geo import US_REGIONS
from aime.knowledge.industries import INDUSTRIES, industry_label
from aime.knowledge.services import SERVICES, service_name
from aime.models import (Activity, AgentRun, Alert, Campaign, Company, Contact, ContentItem, FollowUp, InboundInquiry,
                         JobRun, JobState, Lead, MarketingPlan, Opportunity, PolicyDecision, PublishAttempt, Report,
                         ResearchSnapshot, ResearchTopic, Signal)
from aime.monitoring import agent_stats, read_heartbeat
from aime.policy import agreement, environment_blockers, evaluate_shadow, policy_config
from aime.qc import summary as qc_summary
from aime.qc.checks import run_quality_checks
from aime.scheduler.runner import due_jobs, run_job
from aime.timeutil import fmt_et, fmt_local, utcnow
from aime.web.auth import (COOKIE, SESSION_SECONDS, AuthMiddleware, authenticate, current_username, make_cookie,
                           safe_next, session_secret, user_count)

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


SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                "script-src 'none'; "
                                "frame-ancestors 'none'; form-action 'self' https://www.linkedin.com; base-uri 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # Not "no-referrer": browsers then send "Origin: null" on the dashboard's own form posts, which the origin check blocks.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            if k == "Content-Security-Policy" and request.url.path.startswith("/api/docs"):
                continue  # Swagger UI needs its own scripts
            response.headers.setdefault(k, v)
        return response


class InboundLimiter:
    """Small in-memory rate limit for the public-facing inbound endpoint."""

    def __init__(self, per_minute: int = 20):
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


MAX_INBOUND_BYTES = 20_000


def create_app(ctx: AppContext | None = None) -> FastAPI:
    app = FastAPI(title="AI Marketing Employee", docs_url="/api/docs", redoc_url=None)
    app.state.ctx = ctx
    limiter = InboundLimiter()
    app.add_middleware(AuthMiddleware, get_ctx=lambda: app.state.ctx or get_context())
    app.add_middleware(SecurityHeadersMiddleware)
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
        data.setdefault("data_role", C().settings.data_role)
        data.setdefault("maintenance", maintenance.active(C().settings.data_dir))
        data.setdefault("current_user", getattr(request.state, "user", None))
        return templates.TemplateResponse(request, name, data)

    who = current_username  # the signed-in Sales Manager, recorded on approvals and activities

    # ------------------------------------------------------------------ sign in
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/", error: str = ""):
        with C().db.session() as s:
            has_users = user_count(s) > 0
        return render(request, "login.html", next=safe_next(next), error=error, has_users=has_users,
                      auth_on=C().settings.dashboard_auth)

    @app.post("/login")
    def login_submit(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
        with C().db.session() as s:
            user, message = authenticate(s, username, password)
            cookie = make_cookie(session_secret(C().settings.data_dir), user) if user else None
        if not cookie:
            return RedirectResponse(f"/login?error={quote(message)}&next={quote(safe_next(next))}", status_code=303)
        response = RedirectResponse(safe_next(next), status_code=303)
        response.set_cookie(COOKIE, cookie, max_age=SESSION_SECONDS, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https", path="/")
        return response

    @app.post("/logout")
    def logout():
        response = RedirectResponse("/login?error=" + quote("Signed out"), status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    def back(url: str, msg: str) -> RedirectResponse:
        sep = "&" if "?" in url else "?"
        return RedirectResponse(f"{url}{sep}msg={quote(msg)}", status_code=303)

    # ------------------------------------------------------------------ today (Sales Manager home)
    @app.get("/", response_class=HTMLResponse)
    def today(request: Request):
        ctx = C()
        with ctx.db.session() as s:
            cnt = lambda m, *c: s.execute(select(func.count(m.id)).where(*c)).scalar_one()  # noqa: E731
            posts = s.execute(select(ContentItem).where(ContentItem.status == "awaiting_approval")
                              .order_by(ContentItem.scheduled_for).limit(10)).scalars().all()
            emails = s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "awaiting_approval")
                               .order_by(FollowUp.due_at).limit(10)).scalars().all()
            tasks = s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending")
                              .order_by(FollowUp.id).limit(6)).scalars().all()
            for f in emails + tasks:
                _ = f.lead.company
            exceptions = (
                [("Post outcome unknown", f"/content/{p.id}", p.title) for p in s.execute(select(ContentItem).where(
                    ContentItem.status == "uncertain")).scalars()]
                + [("Email outcome unknown", "/followups?show=all", f.subject or "") for f in s.execute(select(FollowUp).where(
                    FollowUp.status == "uncertain")).scalars()]
                + [("Approved post blocked", f"/content/{p.id}", f"{p.title}: {p.publish_blocked_reason}")
                   for p in s.execute(select(ContentItem).where(ContentItem.status == "approved",
                                                                ContentItem.publish_blocked_reason.is_not(None))).scalars()]
            )
            alerts = s.execute(select(Alert).where(Alert.acknowledged.is_(False), Alert.level.in_(("error", "warning")))
                               .order_by(Alert.id.desc()).limit(8)).scalars().all()
            stages = dict(s.execute(select(Opportunity.stage, func.count(Opportunity.id)).group_by(Opportunity.stage)).all())
            open_leads = (Company.merged_into_id.is_(None),)
            funnel = [
                ("Companies found", cnt(Company, *open_leads), "/companies", ""),
                ("Websites verified", cnt(Company, Company.website_status == "verified", *open_leads), "/companies", ""),
                ("Companies with a decision-maker", s.execute(select(func.count(func.distinct(Contact.company_id))).where(
                    Contact.source.in_(("website", "sec_filing")), Contact.last_name.is_not(None))).scalar_one(),
                 "/contacts", ""),
                ("Companies with a usable email", s.execute(select(func.count(func.distinct(Contact.company_id))).where(
                    Contact.email_status.in_(("published", "role_address")))).scalar_one(), "/contacts?reachable=1", ""),
                ("Qualified companies", cnt(Lead, Lead.status == "qualified"), "/leads?status=qualified", ""),
                ("Posts scheduled", cnt(ContentItem, ContentItem.status.in_(("awaiting_approval", "approved"))), "/content", ""),
                ("Posts published", cnt(ContentItem, ContentItem.status == "published"), "/content?status=published", ""),
                ("Open opportunities", cnt(Opportunity, Opportunity.stage.not_in(("won", "lost"))), "/pipeline", ""),
                ("Deals won", stages.get("won", 0), "/pipeline", ""),
            ]
            task_total = cnt(FollowUp, FollowUp.channel == "task", FollowUp.status == "pending")
            health = health_strip(ctx, s)
            policy = agreement(s)
            pending = [r for r in policy["rows"] if r["post_status"] in ("awaiting_approval", "approved")]
            policy_now = {"would_publish": sum(r["decision"] == "would_publish" for r in pending),
                          "would_hold": sum(r["decision"] == "would_hold" for r in pending), **policy["counts"]}
            post_decisions = {r["content_id"]: r["decision"] for r in policy["rows"]}
        return render(request, "today.html", posts=posts, emails=emails, tasks=tasks, task_total=task_total,
                      exceptions=exceptions, alerts=alerts, funnel=funnel, stages=stages, health=health,
                      policy=policy_now, post_decisions=post_decisions)

    # ------------------------------------------------------------------ system overview
    @app.get("/system", response_class=HTMLResponse)
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
            people = s.execute(select(Contact).where(Contact.company_id == lead.company_id)
                               .order_by(Contact.relevance.desc().nullslast(), Contact.id)).scalars().all()
            research = s.execute(select(ResearchSnapshot).where(ResearchSnapshot.company_id == lead.company_id)
                                 .order_by(ResearchSnapshot.id.desc())).scalars().first()
        return render(request, "lead_detail.html", lead=lead, signals=signals, acts=acts, fups=fups, opps=opps,
                      people=people, research=research)

    @app.get("/contacts", response_class=HTMLResponse)
    def contacts_page(request: Request, confidence: str = "", role: str = "", reachable: str = ""):
        with C().db.session() as s:
            stmt = select(Contact).where(Contact.source.in_(("website", "sec_filing")))
            if reachable:
                stmt = stmt.where(Contact.email_status.in_(("published", "role_address")))
            if confidence:
                stmt = stmt.where(Contact.confidence == confidence)
            if role:
                stmt = stmt.where(Contact.role_category == role)
            rows = s.execute(stmt.order_by(Contact.relevance.desc().nullslast(), Contact.id).limit(500)).scalars().all()
            leads = {lead.company_id: lead for lead in s.execute(select(Lead)).scalars()}
            for c in rows:
                _ = c.company
            open_companies = (Company.merged_into_id.is_(None),)
            researched = s.execute(select(func.count(Company.id)).where(Company.researched_at.is_not(None),
                                                                         *open_companies)).scalar_one()
            with_people = s.execute(select(func.count(func.distinct(Contact.company_id))).where(
                Contact.source.in_(("website", "sec_filing")), Contact.last_name.is_not(None))).scalar_one()
            by_status = dict(s.execute(select(Company.research_status, func.count(Company.id)).where(
                Company.research_status.is_not(None), *open_companies).group_by(Company.research_status)).all())
        return render(request, "contacts.html", rows=rows, leads=leads, confidence=confidence, role=role, reachable=reachable,
                      researched=researched, with_people=with_people, by_status=by_status)

    @app.post("/leads/{lead_id}/status")
    def lead_status(request: Request, lead_id: int, status: str = Form(...), note: str = Form("")):
        if status not in LEAD_STATUSES:
            raise HTTPException(400)
        with C().db.session() as s:
            lead = s.get(Lead, lead_id) or _404()
            old = lead.status
            lead.status = status
            lead.last_activity_at = utcnow()
            log_activity(s, f"Status {old} → {status} (manual)", who(request), type_="status_change", body=note or None,
                         company_id=lead.company_id, lead_id=lead.id)
        return back(f"/leads/{lead_id}", "Status updated")

    @app.post("/leads/{lead_id}/note")
    def lead_note(request: Request, lead_id: int, note: str = Form(...)):
        with C().db.session() as s:
            lead = s.get(Lead, lead_id) or _404()
            log_activity(s, "Note", who(request), type_="note", body=note, company_id=lead.company_id, lead_id=lead.id)
            lead.last_activity_at = utcnow()
        return back(f"/leads/{lead_id}", "Note added")

    @app.post("/leads/{lead_id}/replied")
    def lead_replied(request: Request, lead_id: int):
        with C().db.session() as s:
            lead = s.get(Lead, lead_id) or _404()
            for f in s.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                      FollowUp.status.in_(("pending", "awaiting_approval", "approved")))).scalars():
                f.status, f.blocked_reason = "cancelled", "Contact replied; sequence stopped"
            for o in s.execute(select(Opportunity).where(Opportunity.lead_id == lead.id, Opportunity.stage == "new")).scalars():
                o.stage, o.probability = "discovery", 20
            lead.last_activity_at = utcnow()
            log_activity(s, "Contact replied (logged manually); automated follow-ups stopped", who(request),
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
    def update_opp(request: Request, opp_id: int, stage: str = Form(...), next_step: str = Form(""),
                   est_value: str = Form("")):
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
            log_activity(s, f"Opportunity stage {old} → {stage}", who(request), type_="opportunity",
                         company_id=o.company_id, lead_id=o.lead_id, opportunity_id=o.id)
        return back("/pipeline", "Opportunity updated")

    # ------------------------------------------------------------------ follow-ups
    @app.get("/followups", response_class=HTMLResponse)
    def followups(request: Request, show: str = "open"):
        with C().db.session() as s:
            stmt = select(FollowUp)
            if show == "open":
                stmt = stmt.where(FollowUp.status.in_(("pending", "awaiting_approval", "approved", "sending", "uncertain")))
            rows = s.execute(stmt.order_by(FollowUp.due_at).limit(300)).scalars().all()
            for r in rows:
                _ = r.lead.company, r.contact
        ready, reason = EmailSender(C().settings).status()
        return render(request, "followups.html", rows=rows, show=show, email_ready=ready, email_reason=reason)

    @app.post("/followups/{fid}/{action}")
    def followup_action(request: Request, fid: int, action: str, subject: str = Form(""), body: str = Form("")):
        with C().db.session() as s:
            f = s.get(FollowUp, fid) or _404()
            if action == "approve" and f.channel == "email" and f.status == "awaiting_approval":
                if subject.strip():
                    f.subject = subject.strip()
                if body.strip():
                    f.body = body
                f.status, f.approved_by, f.approved_at = "approved", who(request), utcnow()
                msg = "Approved. It will be sent by the follow-ups job if outbound email is enabled."
            elif action == "reject" and f.status in ("awaiting_approval", "approved", "pending"):
                f.status = "rejected"
                msg = "Rejected"
            elif action == "done" and f.channel == "task":
                f.status = "done"
                msg = "Task completed"
            elif action == "mark_sent" and f.status == "uncertain":
                f.status, f.sent_at, f.blocked_reason = "sent", f.send_started_at or utcnow(), None
                msg = "Marked as sent (confirmed in your Sent folder)"
            elif action == "requeue" and f.status == "uncertain":
                f.status, f.send_started_at = "approved", None
                f.blocked_reason = "Requeued after a person confirmed it was not delivered"
                msg = "Requeued"
            else:
                raise HTTPException(400, "Action not allowed in the current state")
            log_activity(s, f"Follow-up #{f.id} {action}", who(request), type_="followup", company_id=f.company_id,
                         lead_id=f.lead_id)
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
            decision = s.execute(select(PolicyDecision).where(PolicyDecision.content_id == cid)
                                 .order_by(PolicyDecision.id.desc())).scalars().first()
        return render(request, "content_detail.html", item=item, auth=auth, decision=decision)

    # ------------------------------------------------------------------ auto-publishing policy (shadow mode)
    @app.get("/policy", response_class=HTMLResponse)
    def policy_page(request: Request):
        settings = C().settings
        with C().db.session() as s:
            report = agreement(s)
            cfg = policy_config(settings)
            blockers = environment_blockers(s, settings, cfg)
        return render(request, "policy.html", report=report, cfg=cfg, blockers=blockers)

    @app.post("/policy/evaluate")
    def policy_evaluate(request: Request):
        if window := maintenance.active(C().settings.data_dir):
            return back("/policy", f"Maintenance in progress until {window['until']}; try again afterwards")
        with C().db.session() as s:
            out = evaluate_shadow(s, C().settings)
        return back("/policy", f"Shadow policy checked {out['evaluated']} post(s): {len(out['would_publish'])} would "
                               f"publish, {len(out['would_hold'])} would hold. Nothing was approved or published.")

    @app.post("/content/{cid}/{action}")
    def content_action(request: Request, cid: int, action: str, title: str = Form(""), body: str = Form(""),
                       hashtags: str = Form(""), scheduled_et: str = Form(""), post_ref: str = Form(""),
                       checked: str = Form(""), qc_override: str = Form("")):
        with C().db.session() as s:
            item = s.get(ContentItem, cid) or _404()
            if item.status == "published":
                raise HTTPException(400, "Published posts cannot be changed here")
            if item.status == "publishing":
                raise HTTPException(409, "This post is being published right now; try again in a few minutes")
            if item.status == "uncertain":
                ref = post_ref.strip()
                if action == "confirm_published" and POST_REF_RE.match(ref):
                    urn = ref if ref.startswith("urn:li:") else None
                    item.status, item.reconciled_via = "published", "manual"
                    item.external_id = urn or ref
                    item.external_url = post_url(urn) if urn else ref
                    item.published_at = item.published_at or utcnow()
                    item.publish_blocked_reason = None
                    s.query(PublishAttempt).filter(PublishAttempt.attempt_id == item.publish_attempt_id).update(
                        {"outcome": "reconciled", "external_id": item.external_id, "finished_at": utcnow()})
                    msg = "Marked as published"
                elif action == "requeue" and checked == "yes":
                    s.query(PublishAttempt).filter(PublishAttempt.attempt_id == item.publish_attempt_id).update(
                        {"outcome": "not_sent", "finished_at": utcnow()})
                    item.status, item.publish_attempt_id, item.publish_started_at = "approved", None, None
                    item.publish_blocked_reason = "Requeued after a person confirmed it is not on LinkedIn"
                    msg = "Requeued for publishing"
                else:
                    raise HTTPException(400, "Uncertain posts need: confirm with the LinkedIn post URL/URN, or "
                                             "requeue after ticking that you checked LinkedIn")
                return back(f"/content/{cid}", msg)
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
                    item.approved_by, item.approved_at = None, None
                image_info = dict((item.quality or {}).get("image") or {"renderer": "pillow"})
                qc = run_quality_checks(s, C().settings, title=item.title, body=item.body, hashtags=item.hashtags or [],
                                        image_path=C().settings.assets_dir / item.image_path if item.image_path else None,
                                        image_info=image_info, exclude_id=item.id)
                image_info["phash"] = qc.pop("image_phash", None)
                item.quality = {**(item.quality or {}), "image": image_info, "qc": qc}
                msg = f"Saved; quality checks: {qc['status']} ({qc_summary(qc)[:160]})"
            elif action == "approve" and item.status in ("awaiting_approval", "draft", "failed"):
                if item.qc_status in ("held", "failed") and qc_override != "yes":
                    return back(f"/content/{cid}", "Tick 'I reviewed the quality findings' to approve this post")
                item.status, item.approved_by, item.approved_at = "approved", who(request), utcnow()
                item.publish_attempts = 0
                msg = "Approved for publishing at its scheduled time (requires LinkedIn authorization)"
                if item.qc_status in ("held", "failed"):
                    log_activity(s, f"Post #{item.id} approved despite quality findings: {qc_summary(item.qc)[:200]}",
                                 who(request), type_="content")
            elif action == "reject":
                item.status = "rejected"
                msg = "Rejected"
            else:
                raise HTTPException(400, "Action not allowed in the current state")
            log_activity(s, f"Post #{item.id} {action}", who(request), type_="content")
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
        if window := maintenance.active(C().settings.data_dir):
            return back("/inbound", f"Maintenance in progress until {window['until']}; try again afterwards")
        background.add_task(run_job, C(), "inbound", "dashboard")
        return back("/inbound", "Inbound job started")

    @app.post("/api/inbound")
    async def api_inbound(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        if not limiter.allow(client_ip):
            raise HTTPException(429, "Too many requests; try again in a minute")
        token = C().settings.inbound_api_token
        if token and not hmac.compare_digest(request.headers.get("x-inbound-token", ""), token):
            raise HTTPException(401, "Invalid inbound token")
        raw = await request.body()
        if len(raw) > MAX_INBOUND_BYTES:
            raise HTTPException(413, "Inquiry too large")
        try:
            payload = json.loads(raw)
            InboundPayload.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, f"Invalid inquiry: {str(exc)[:200]}") from exc
        if C().settings.is_production and payload.get("is_sample"):
            raise HTTPException(422, "Sample records are not accepted by the production database")
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
        if window := maintenance.active(C().settings.data_dir):
            return back("/jobs", f"Maintenance in progress until {window['until']}; jobs can't be started now")
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


def health_strip(ctx: AppContext, s) -> list[tuple[str, str, str]]:
    """(label, ok|warn|bad, detail) for the things a Sales Manager needs to trust before reading anything else."""
    from aime.backup import offsite_status
    from aime.design.render import find_browser
    from aime.integrations.status import llm_reachable

    st = ctx.settings
    rows: list[tuple[str, str, str]] = []
    hb = read_heartbeat(st)
    if hb is None:
        rows.append(("Scheduler", "bad", "no tick recorded yet"))
    else:
        age = int((utcnow() - datetime.fromisoformat(hb["at"].rstrip("Z"))).total_seconds() // 60)
        rows.append(("Scheduler", "ok" if age <= 35 or hb.get("state") == "maintenance" else "bad",
                     f"last tick {age} min ago" + (" (maintenance)" if hb.get("state") == "maintenance" else "")))
    failed = s.execute(select(func.count(JobRun.id)).where(JobRun.status.in_(("failed", "partial")),
                                                           JobRun.started_at >= utcnow() - timedelta(hours=24))).scalar_one()
    rows.append(("Jobs (24 h)", "ok" if failed == 0 else "warn", f"{failed} failed or partial"))
    if (st.llm_provider or "auto").lower() in ("auto", "ollama"):
        ok = llm_reachable(ctx)
        rows.append(("Local AI model", "ok" if ok else "warn", "available" if ok else "unreachable: template writer in use"))
    browser = find_browser(st.browser_path or None) if (st.image_renderer or "auto") != "pillow" else None
    rows.append(("Post images", "ok" if browser else "warn",
                 f"designed ({browser.stem})" if browser else "simple cards (no designer)"))
    li = get_auth_status(st, s)
    if li.expires_at:
        days = (li.expires_at - utcnow()).days
        rows.append(("LinkedIn connection", "ok" if days > 14 else "warn", f"connected, {days} days left"))
    else:
        rows.append(("LinkedIn connection", "warn", "not connected"))
    rows.append(("Publishing", "ok", "ON: approved posts go out at their slot") if st.publishing_enabled
                else ("Publishing", "ok", "off: nothing is posted"))
    rows.append(("Outbound email", "ok", "ON") if st.outbound_email_enabled else ("Outbound email", "ok", "off"))
    last = kv_get(s, "backup.last_verified")
    if last:
        hours = int((utcnow() - datetime.fromisoformat(last["at"])).total_seconds() // 3600)
        rows.append(("Backup", "ok" if hours <= 30 else "bad", f"verified {hours} h ago"))
    else:
        rows.append(("Backup", "warn", "no verified backup recorded yet"))
    ready, reason = offsite_status(st)
    rows.append(("Off-machine backup", "ok" if ready else "warn", "configured" if ready else reason))
    return rows
