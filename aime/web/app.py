"""Local lead-management dashboard and JSON API (binds to 127.0.0.1 only)."""

from __future__ import annotations

import hmac
import json
import re
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import httpx
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, or_, select, update
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aime import maintenance
from aime.agents import AGENTS
from aime.context import AppContext, get_context
from aime.crm_ops import is_suppressed, log_activity, suppress
from aime.db import kv_get, kv_set
from aime.inbound import InboundPayload, record_inquiry
from aime.integrations.email_smtp import POSTAL_PLACEHOLDER, EmailSender, compliance_problem
from aime import mail_accounts
from aime.integrations import linkedin_leads, oauth
from aime.integrations.linkedin import (POST_REF_RE, LinkedInAPIError, LinkedInClient, get_auth_status, get_token,
                                        new_state, post_url)
from aime.integrations.status import integration_statuses
from aime.knowledge.geo import US_REGIONS
from aime.knowledge.industries import INDUSTRIES, industry_label
from aime.knowledge.services import SERVICES, service_name
from aime import automation as am
from aime.models import (MailAccount, MeetingProposal, Activity, AgentRun, Alert, AuditEvent, Campaign, CampaignAuthorization, Company, Contact,
                         ContentItem, FollowUp, InboundEmail, InboundInquiry, Meeting,
                         JobRun, JobState, Lead, MarketingPlan, Opportunity, PolicyDecision, PublishAttempt, Report,
                         ResearchSnapshot, ResearchTopic, Signal, User)
from aime.monitoring import agent_stats, read_heartbeat
from aime.policy import agreement, environment_blockers, evaluate_shadow, policy_config
from aime.qc import summary as qc_summary
from aime.outreach import record_delivery
from aime.outreach import set_status as set_outreach_status
from aime.outreach import stop_sequence
from aime.qc.checks import run_quality_checks
from aime.qc.email_checks import run_email_checks
from aime.reply_intel import INTENT_LABELS, QUESTION_PLACEHOLDER
from aime.reply_intel import act as act_on_reply
from aime.meetings import check_double_bookings, record_meeting, reopen_after_cancel
from aime.scheduler.runner import ABANDON_AFTER, due_jobs, load_job_specs, next_run, run_job
from aime.scoring import lead_tier, tier_reason
from aime.timeutil import as_aware_utc, fmt_et, fmt_local, local_tz, to_naive_utc, utcnow
from aime.permissions import role_label
from aime.stages import PROBABILITY
from aime.stages import move as move_stage
from aime.web.team_pages import assignment_history
from aime.web.team_pages import register as register_team_pages
from aime.visibility import EVERYTHING, lead_condition, opportunity_condition, record_condition
from aime.web.auth import (COOKIE, LOGIN_LOCK, SESSION_SECONDS, AuthMiddleware, authenticate,
                           current_username, make_cookie, safe_next, session_secret, user_count)

WEB_DIR = Path(__file__).resolve().parent
STAGES = ["new", "discovery", "proposal", "negotiation", "won", "lost"]
LEAD_STATUSES = ["new", "qualified", "nurture", "disqualified", "converted", "merged"]


# one email address or one domain: never a pattern (% or _ would match other contacts)
SUPPRESS_VALUE = re.compile(r"[a-z0-9.!#$&'*+/=?^`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+|[a-z0-9-]+(?:\.[a-z0-9-]+)+")


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
FOLLOWUPS_PAGE = 300
# A contact the Sales Manager can actually email: researched, a checked published address, not opted out.
USABLE_EMAIL = (Contact.source.in_(("website", "sec_filing")), Contact.email_status.in_(("published", "role_address")),
                Contact.consent_status != "opted_out")


def create_app(ctx: AppContext | None = None) -> FastAPI:
    # no interactive API docs: they need a script from a CDN and a relaxed content policy on the signed-in origin
    app = FastAPI(title="AI Marketing Employee", docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    limiter = InboundLimiter()
    app.add_middleware(AuthMiddleware, get_ctx=lambda: app.state.ctx or get_context())
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(OriginCheckMiddleware)
    settings = (ctx or get_context()).settings
    hosts = [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()]
    if settings.data_role == "test":
        hosts.append("testserver")  # the test client's host name; never accepted for real data
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    env = templates.env
    env.filters.update(local=fmt_local, et=fmt_et, service=service_name, industry=industry_label, safe_url=safe_url,
                       tier=lead_tier, tier_reason=tier_reason, run_summary=run_summary, intent_label=lambda i: INTENT_LABELS.get(i or "", ""))
    env.globals.update(describe_rule=am.describe_rule, ADDRESS_RULES=am.ADDRESS_RULES, SERVICES=SERVICES, INDUSTRIES=INDUSTRIES, STATES=US_REGIONS, STAGES=STAGES,
                       LEAD_STATUSES=LEAD_STATUSES)

    def C() -> AppContext:
        return app.state.ctx or get_context()

    def render(request: Request, name: str, **data):
        data.setdefault("msg", request.query_params.get("msg"))
        data.setdefault("company_name", C().settings.company_name)
        data.setdefault("data_role", C().settings.data_role)
        data.setdefault("maintenance", maintenance.active(C().settings.data_dir))
        data.setdefault("current_user", getattr(request.state, "user", None))
        data.setdefault("sending_on", C().settings.outbound_email_enabled)
        user = data["current_user"]
        data.setdefault("user_name", (user["username"].title() if user else C().settings.sender_name or ""))
        data.setdefault("is_super_admin", not C().settings.dashboard_auth or bool(user and "configure_system" in user.get("perms", ())))
        data.setdefault("can_manage_users", not C().settings.dashboard_auth or bool(user and "manage_users" in user.get("perms", ())))
        if "user_role" not in data:
            with C().db.session() as s:
                data["user_role"] = role_label(s, user["role"]) if user else "Preview · no sign-in"
        if "nav_status" not in data and request.url.path != "/login":  # the top bar and sidebar on every page
            with C().db.session() as s:
                state = am.get_state(s)
                data["nav_status"] = {"mode": am.get_mode(s), "state": state, "transitions": am.TRANSITIONS,
                                      "duty": employee_duty(C().settings, state), "modes": am.MODES,
                                      "admin": is_admin(request), "here": str(request.url.path)}
                cnt = lambda m, *c: s.execute(select(func.count(m.id)).where(*c)).scalar_one()  # noqa: E731
                data["nav_counts"] = {
                    "approvals": cnt(FollowUp, FollowUp.channel == "email", FollowUp.status == "awaiting_approval")
                    + cnt(ContentItem, ContentItem.status == "awaiting_approval"),
                    "replies": cnt(InboundEmail, InboundEmail.status == "needs_review"),
                    "alerts": cnt(Alert, Alert.acknowledged.is_(False), Alert.level.in_(("error", "warning")))}
        return templates.TemplateResponse(request, name, data)

    who = current_username  # the signed-in Sales Manager, recorded on approvals and activities

    def scope(request: Request):
        """The records this user may see (aime.visibility), set by the sign-in middleware."""
        return getattr(request.state, "scope", EVERYTHING)

    # ------------------------------------------------------------------ sign in
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/", error: str = ""):
        with C().db.session() as s:
            has_users = user_count(s) > 0
        return render(request, "login.html", next=safe_next(next), error=error, has_users=has_users,
                      auth_on=C().settings.dashboard_auth)

    @app.post("/login")
    def login_submit(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
        with LOGIN_LOCK, C().db.session() as s:
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
        path, mark, anchor = url.partition("#")  # the message goes in the query, before any #anchor
        sep = "&" if "?" in path else "?"
        return RedirectResponse(f"{path}{sep}msg={quote(msg)}{mark}{anchor}", status_code=303)

    # ------------------------------------------------------------------ dashboard (Sales Manager home)
    @app.get("/", response_class=HTMLResponse)
    def today(request: Request):
        ctx = C()
        days = 30 if request.query_params.get("range") == "30" else 7
        with ctx.db.session() as s:
            view = {**ai_employee(ctx, s), **dashboard_view(ctx, s, days)}
        return render(request, "today.html", **view)

    # ------------------------------------------------------------------ Sales Manager: today and exceptions
    @app.get("/manager", response_class=HTMLResponse)
    def manager_page(request: Request):
        ctx = C()
        with ctx.db.session() as s:
            view = manager_today(ctx, s)
        return render(request, "manager.html", **view)

    # ------------------------------------------------------------------ Sales Command Center (how the work runs)
    @app.get("/command-center", response_class=HTMLResponse)
    def command_center_page(request: Request):
        ctx = C()
        with ctx.db.session() as s:
            cnt = lambda m, *c: s.execute(select(func.count(m.id)).where(*c)).scalar_one()  # noqa: E731
            posts = s.execute(select(ContentItem).where(ContentItem.status == "awaiting_approval")
                              .order_by(ContentItem.scheduled_for).limit(10)).scalars().all()
            mine = record_condition(scope(request), FollowUp.lead_id)
            emails = s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "awaiting_approval", mine)
                               .order_by(FollowUp.due_at).limit(10)).scalars().all()
            tasks = s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending", mine)
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
            task_total = cnt(FollowUp, FollowUp.channel == "task", FollowUp.status == "pending")
            health = health_strip(ctx, s)
            replies = s.execute(select(InboundEmail).where(InboundEmail.status == "needs_review",
                                                           record_condition(scope(request), InboundEmail.lead_id))
                                .order_by(InboundEmail.received_at.desc()).limit(10)).scalars().all()
            policy = agreement(s)
            pending = [r for r in policy["rows"] if r["post_status"] in ("awaiting_approval", "approved")]
            policy_now = {"would_publish": sum(r["decision"] == "would_publish" for r in pending),
                          "would_hold": sum(r["decision"] == "would_hold" for r in pending), **policy["counts"]}
            post_decisions = {r["content_id"]: r["decision"] for r in policy["rows"]}
            view = {**operations_view(ctx, s), **ai_employee(ctx, s)}
        return render(request, "command_center.html", posts=posts, emails=emails, tasks=tasks, task_total=task_total,
                      exceptions=exceptions, alerts=alerts, health=health, policy=policy_now, post_decisions=post_decisions,
                      replies=replies, modes=am.MODES, is_admin=is_admin(request), now=utcnow(), **view)

    # ------------------------------------------------------------------ replies (sales mailbox)
    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page(request: Request, show: str = "review"):
        settings = C().settings
        with C().db.session() as s:
            stmt = select(InboundEmail).where(record_condition(scope(request), InboundEmail.lead_id))
            if show == "review":
                stmt = stmt.where(InboundEmail.status == "needs_review")
            rows = s.execute(stmt.order_by(InboundEmail.received_at.desc()).limit(200)).scalars().all()
            for r in rows:
                _ = r.lead.company if r.lead else None
            counts = dict(s.execute(select(InboundEmail.category, func.count(InboundEmail.id))
                                    .where(record_condition(scope(request), InboundEmail.lead_id))
                                    .group_by(InboundEmail.category)).all())
            # the .env mailbox's position, else the first Mailboxes panel mailbox that is read
            state = kv_get(s, "inbox.state") or next(
                (kv_get(s, f"inbox.state.mailbox{a.id}") for a in mail_accounts.readable(s)), None)
            reading, where = mail_accounts.reading_status(s, settings)
        return render(request, "mailbox.html", rows=rows, show=show, counts=counts, state=state,
                      mailbox=where.removesuffix(", read-only") if reading else None)

    @app.post("/inbox/{eid}/{action}")
    def inbox_action(request: Request, eid: int, action: str, category: str = Form("")):
        with C().db.session() as s:
            row = s.get(InboundEmail, eid) or _404()
            if action == "reviewed":
                row.status = "reviewed"
                msg = "Marked reviewed"
            elif action == "reclassify" and category in ("interested", "not_interested", "objection", "referral",
                                                          "question", "unsubscribe", "out_of_office", "bounce", "other"):
                if row.suggested_followup_id:
                    old = s.get(FollowUp, row.suggested_followup_id)
                    if old and old.status == "awaiting_approval":
                        old.status, old.blocked_reason = "rejected", "Replaced after the reply was reclassified"
                previous = row.category
                row.category, row.confidence = category, "high"
                row.reasons = list(row.reasons or []) + [f"set to {category} by {who(request)}"]
                act_on_reply(s, C().settings, row, who(request))
                row.status = "reviewed" if row.status == "needs_review" else row.status
                log_activity(s, f"Reply #{row.id} reclassified from {previous} to {category}", who(request),
                             type_="override", company_id=row.company_id, lead_id=row.lead_id)
                msg = f"Reclassified as {category.replace('_', ' ')}; {row.action_note or ''}"
            else:
                raise HTTPException(400, "Action not allowed")
        return back("/inbox", msg)

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
            stmt = select(Lead).join(Company, Company.id == Lead.company_id).where(lead_condition(scope(request)))
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
            withdrawn = sum(1 for p in people if p.source == "website_withdrawn")
            people = [p for p in people if p.source != "website_withdrawn"]
            research = s.execute(select(ResearchSnapshot).where(ResearchSnapshot.company_id == lead.company_id)
                                 .order_by(ResearchSnapshot.id.desc())).scalars().first()
            meetings = s.execute(select(Meeting).where(Meeting.lead_id == lead.id).order_by(Meeting.starts_at.desc())).scalars().all()
            replies = s.execute(select(InboundEmail).where(InboundEmail.lead_id == lead.id)).scalars().all()
            assignments = assignment_history(s, lead.id)
            sc = scope(request)
            # who this user may hand the lead to: anyone active (view_all), else their team
            assignees = [u for u in s.execute(select(User.username).where(User.active.is_(True)).order_by(User.username)).scalars()
                         if sc.all or u in sc.usernames]
            # both directions in time order: what we sent (or may have sent) and what they wrote back
            thread = sorted([{"at": f.sent_at, "dir": "out", "subject": f.subject, "who": f.to_email, "text": f.body,
                              "status": f.status} for f in fups if f.channel == "email" and f.sent_at]
                            + [{"at": r.received_at, "dir": "in", "subject": r.subject, "who": r.from_email,
                                "text": r.new_text or r.body_text, "status": (r.category or "new").replace("_", " "),
                                "sentiment": r.sentiment} for r in replies], key=lambda m: m["at"])
        user = getattr(request.state, "user", None)
        can_assign = not C().settings.dashboard_auth or bool(user and "assign_leads" in user.get("perms", ()))
        return render(request, "lead_detail.html", lead=lead, signals=signals, acts=acts, fups=fups, opps=opps,
                      people=people, research=research, meetings=meetings, withdrawn=withdrawn, thread=thread,
                      assignments=assignments, assignees=assignees, can_assign=can_assign)

    @app.get("/contacts", response_class=HTMLResponse)
    def contacts_page(request: Request, confidence: str = "", role: str = "", reachable: str = ""):
        with C().db.session() as s:
            stmt = select(Contact).where(Contact.source.in_(("website", "sec_filing")))
            if reachable:
                stmt = stmt.where(*USABLE_EMAIL)
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
            if lead.outreach_status:
                stop_sequence(s, lead, "replied", "Contact replied (logged manually); sequence stopped", who(request))
            for f in s.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                      FollowUp.status.in_(("pending", "awaiting_approval", "approved")))).scalars():
                f.status, f.blocked_reason = "cancelled", "Contact replied; sequence stopped"
            for o in s.execute(select(Opportunity).where(Opportunity.lead_id == lead.id, Opportunity.stage == "new")).scalars():
                move_stage(s, o, "discovery", who(request), "contact replied (logged manually)")
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
            opps = s.execute(select(Opportunity).where(opportunity_condition(scope(request)))
                             .order_by(Opportunity.updated_at.desc())).scalars().all()
            for o in opps:
                _ = o.company
            outreach = dict(s.execute(select(Lead.outreach_status, func.count(Lead.id)).where(
                Lead.outreach_status.is_not(None), lead_condition(scope(request))).group_by(Lead.outreach_status)).all())
        board = {st: [o for o in opps if o.stage == st] for st in STAGES}
        order = ["drafted", "approved", "contacted", "replied", "interested", "meeting", "not_interested", "no_response",
                 "unsubscribed", "bounced", "stopped"]
        return render(request, "pipeline.html", board=board, outreach=[(k, outreach.get(k, 0)) for k in order])

    # ------------------------------------------------------------------ meetings
    @app.get("/meetings", response_class=HTMLResponse)
    def meetings_page(request: Request):
        from aime import booking
        now = utcnow()
        with C().db.session() as s:
            rows = s.execute(select(Meeting).where(record_condition(scope(request), Meeting.lead_id))
                             .order_by(Meeting.starts_at.desc()).limit(200)).scalars().all()
            for m in rows:
                _ = m.lead.company if m.lead else None
            proposals = s.execute(select(MeetingProposal).where(record_condition(scope(request), MeetingProposal.lead_id))
                                  .order_by(MeetingProposal.id.desc()).limit(50)).scalars().all()
            for p in proposals:
                _ = p.lead.company
            asked = {p.inbound_email_id for p in proposals}
            requests = [r for r in s.execute(select(InboundEmail).where(
                InboundEmail.intent == "meeting_request", InboundEmail.lead_id.is_not(None),
                record_condition(scope(request), InboundEmail.lead_id),
                InboundEmail.received_at >= now - timedelta(days=7))).scalars() if r.id not in asked]
            ready, why = booking.ready(s, C().settings)
        return render(request, "meetings.html", upcoming=[m for m in rows if m.status == "scheduled" and m.ends_at >= now][::-1],
                      past=[m for m in rows if not (m.status == "scheduled" and m.ends_at >= now)],
                      proposals=proposals, requests=requests, booking_ready=ready, booking_why=why,
                      slot_label=booking.label, slots_of=lambda p: [datetime.fromisoformat(x) for x in p.slots or []])

    @app.post("/meetings/{mid}/link")
    def link_meeting_to_lead(request: Request, mid: int, lead_id: int = Form(...)):
        from aime.meetings import link_meeting
        with C().db.session() as s:
            m = s.get(Meeting, mid) or _404()
            lead = s.get(Lead, lead_id)
            if lead is None:
                return back("/meetings", f"No lead #{lead_id}")
            if m.lead_id is not None:
                raise HTTPException(400, "This meeting is already linked to a lead")
            link_meeting(s, m, lead, who(request))
            for r in s.execute(select(InboundEmail).where(InboundEmail.id == m.inbound_email_id,
                                                          InboundEmail.status == "needs_review")).scalars():
                r.lead_id, r.company_id, r.status = lead.id, lead.company_id, "actioned"
        return back("/meetings", f"Meeting linked to lead #{lead_id}")

    def _with_calendar(fn):
        """Run `fn(session, calendar)` with the connected calendar; calendar and sign-in errors come back as words."""
        from aime.integrations.calendar import CalendarError, calendar_for
        ctx = C()
        with ctx.db.session() as s, ctx.http() as http:
            provider = (ctx.settings.calendar_provider or "").lower()
            cal = calendar_for(ctx.settings, http,
                               lambda: oauth.access_token(s, ctx.settings, http, provider, "calendar"))
            try:
                return fn(s, cal), None
            except (CalendarError, oauth.OAuthError, ValueError) as exc:
                s.rollback()
                return None, str(exc)

    @app.post("/meetings/proposals/{pid}/book")
    def proposal_book(request: Request, pid: int, slot: int = Form(...)):
        from aime import booking
        res, err = _with_calendar(lambda s, cal: booking.book(s, C().settings, cal, s.get(MeetingProposal, pid) or _404(),
                                                              slot, who(request)).id)
        return back("/meetings#proposals", err or f"Booked: meeting #{res} is in the calendar; the confirmation "
                    "email waits for your approval on Follow-ups")

    @app.post("/meetings/proposals/{pid}/decline")
    def proposal_decline(request: Request, pid: int):
        with C().db.session() as s:
            p = s.get(MeetingProposal, pid) or _404()
            if p.status in ("draft", "offered"):
                p.status = "declined"
                am.audit(s, who(request), "meeting_proposal_declined", f"proposal #{p.id}", p.prospect_email)
        return back("/meetings#proposals", "Proposal closed")

    @app.post("/meetings/requests/{rid}/propose")
    def proposal_create(request: Request, rid: int):
        from aime import booking

        def run(s, cal):
            ok, why = booking.ready(s, C().settings)
            if not ok:
                raise ValueError(f"Not proposed: {why}")
            return booking.propose(s, C().settings, cal, s.get(InboundEmail, rid) or _404(), who(request))
        res, err = _with_calendar(run)
        return back("/meetings#proposals", err or f"Meeting times: {res}")

    @app.post("/meetings/{mid}/move")
    def meeting_move(request: Request, mid: int, start_local: str = Form(...)):
        from aime import booking

        def run(s, cal):
            m = s.get(Meeting, mid) or _404()
            try:
                local = datetime.fromisoformat(start_local).replace(tzinfo=ZoneInfo(m.timezone or "UTC"))
            except ValueError as exc:
                raise ValueError("Not moved: give the new date and time") from exc
            booking.move(s, C().settings, cal, m, to_naive_utc(local), who(request))
        _res, err = _with_calendar(run)
        return back("/meetings", err or "Meeting moved in the calendar; the note to the prospect waits for your approval")

    @app.post("/meetings/{mid}/{action}")
    def meeting_action(request: Request, mid: int, action: str, notes: str = Form("")):
        with C().db.session() as s:
            m = s.get(Meeting, mid) or _404()
            if action not in ("held", "no_show", "cancel") or m.status != "scheduled":
                raise HTTPException(400, "Action not allowed in the current state")
            m.status = {"held": "completed", "no_show": "no_show", "cancel": "cancelled"}[action]
            m.notes = (notes or m.notes or "")[:2000] or None
            opp = s.get(Opportunity, m.opportunity_id) if m.opportunity_id else None
            if opp and opp.stage not in ("won", "lost"):
                if action == "held":
                    opp.probability = max(opp.probability or 0, 40)
                    opp.next_step = "Meeting held: send a proposal or decide the next step"
                elif action == "no_show":
                    opp.next_step = "No-show: follow up to rebook"
            if action == "cancel":  # as for a cancellation Calendly reports: the lead reopens unless another is booked
                reopen_after_cancel(s, m, who(request))
                if m.event_id:  # booked through the calendar: cancel the event too (a person clicked Cancel)
                    from aime import booking
                    from aime.integrations.calendar import CalendarError, calendar_for
                    with C().http() as http:
                        provider = m.provider or C().settings.calendar_provider
                        cal = calendar_for(C().settings, http, lambda: oauth.access_token(s, C().settings, http,
                                                                                          provider, "calendar"))
                        try:
                            booking.cancel_event(s, C().settings, cal, m, who(request))
                        except (CalendarError, oauth.OAuthError) as exc:
                            s.add(Alert(level="warning", source="meetings",
                                        message=f"Meeting #{m.id} cancelled here, but not in the calendar: {exc}"))
            if m.lead_id and action == "no_show":
                s.add(FollowUp(lead_id=m.lead_id, company_id=m.company_id, channel="task", step=1, status="pending",
                               subject=f"Rebook the meeting with {m.invitee_name or m.invitee_email}",
                               body=f"They did not join the meeting on {m.starts_at:%Y-%m-%d %H:%M} UTC."))
            for t in s.execute(select(FollowUp).where(FollowUp.lead_id == m.lead_id, FollowUp.channel == "task",
                                                      FollowUp.status == "pending",
                                                      FollowUp.subject.like("Record the outcome of the meeting%"))).scalars():
                t.status = "done"
            log_activity(s, f"Meeting {m.status.replace('_', ' ')}: {m.invitee_name or m.invitee_email}", who(request),
                         type_="meeting", body=notes or None, company_id=m.company_id, lead_id=m.lead_id,
                         opportunity_id=m.opportunity_id)
            check_double_bookings(s, C().settings)
        return back("/meetings", f"Meeting marked {m.status.replace('_', ' ')}")

    @app.post("/leads/{lead_id}/meeting")
    def log_meeting(request: Request, lead_id: int, date: str = Form(...), time_et: str = Form(...),
                    minutes: int = Form(30), title: str = Form("Meeting"), invitee_email: str = Form("")):
        from zoneinfo import ZoneInfo
        with C().db.session() as s:
            lead = s.get(Lead, lead_id) or _404()
            email = (invitee_email or (lead.contact.email if lead.contact else "") or "").strip().lower()
            if "@" not in email:
                return back(f"/leads/{lead_id}", "Enter the invitee's email")
            try:
                start = datetime.fromisoformat(f"{date}T{time_et}").replace(tzinfo=ZoneInfo("America/New_York"))
            except ValueError:
                return back(f"/leads/{lead_id}", "Enter the date as YYYY-MM-DD and the time as HH:MM (US Eastern)")
            start_utc = start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            info = {"kind": "booked", "invitee_email": email, "invitee_name": lead.contact.display_name if lead.contact else None,
                    "starts_at": start_utc, "ends_at": start_utc + timedelta(minutes=max(5, min(minutes, 480))),
                    "event": title[:200] or "Meeting", "tz": "America/New_York"}
            meeting, note = record_meeting(s, C().settings, info, who(request), source="manual")
            check_double_bookings(s, C().settings)
        return back(f"/leads/{lead_id}", f"Meeting logged: {note}")

    @app.post("/companies/{company_id}/not_competitor")
    def not_competitor(request: Request, company_id: int):
        """A person overrides the automatic competitor flag; research does not set it again."""
        with C().db.session() as s:
            company = s.get(Company, company_id) or _404()
            from aime.crm_ops import primary_lead

            company.is_competitor, company.competitor_cleared_by = False, who(request)
            lead = s.execute(select(Lead).where(Lead.company_id == company.id, Lead.status == "disqualified",
                                                Lead.disqualify_reason.contains("competitor"))
                             .order_by(Lead.id.desc())).scalars().first() or primary_lead(s, company.id)
            lead_id = lead.id if lead else None
            if lead and lead.status == "disqualified" and "competitor" in (lead.disqualify_reason or ""):
                lead.status, lead.disqualify_reason = "new", None
            if lead:
                lead.last_scored_at = None  # re-scored by the next qualification run
            log_activity(s, f"Competitor flag cleared by {who(request)}", who(request), type_="override",
                         company_id=company.id, lead_id=lead_id)
        return back(f"/leads/{lead_id}" if lead_id else "/companies",
                    "Marked as not a competitor; the lead is re-scored on the next run")

    @app.post("/leads/{lead_id}/stop_outreach")
    def stop_outreach(request: Request, lead_id: int):
        with C().db.session() as s:
            lead = s.get(Lead, lead_id) or _404()
            n = stop_sequence(s, lead, "stopped", f"Stopped by {who(request)}", who(request))
        return back(f"/leads/{lead_id}", f"Outreach stopped; {n} queued email(s) cancelled")

    @app.post("/opportunities/{opp_id}")
    def update_opp(request: Request, opp_id: int, stage: str = Form(...), next_step: str = Form(""),
                   est_value: str = Form(""), lost_reason: str = Form(""), expected_close: str = Form("")):
        if stage not in STAGES:
            raise HTTPException(400)
        value = None
        if est_value.strip():
            try:
                value = float(est_value.replace(",", "").replace("$", ""))
            except ValueError:
                return back("/pipeline", f"Estimated value '{est_value[:40]}' is not a number; nothing was changed")
        close_by = None
        if expected_close.strip():
            try:
                close_by = date.fromisoformat(expected_close.strip())
            except ValueError:
                return back("/pipeline", "Expected close date is not a date; nothing was changed")
        with C().db.session() as s:
            o = s.get(Opportunity, opp_id) or _404()
            old = o.stage
            o.next_step = next_step or o.next_step
            reason = (" ".join(lost_reason.split())[:200] or o.lost_reason or "No reason given") if stage == "lost" else None
            move_stage(s, o, stage, who(request), reason)  # history, close date, credit for a win; reopening clears them
            o.probability = PROBABILITY[stage]
            if value is not None:
                o.est_value = value
            if close_by is not None:
                o.expected_close = close_by
            if stage == "won" and o.lead_id:
                lead = s.get(Lead, o.lead_id)
                lead.status = "converted"
            if stage == "lost":
                o.lost_reason = reason
            change = f"Opportunity stage {old} → {stage}" + (f" ({o.lost_reason})" if stage == "lost" else "")
            log_activity(s, change, who(request), type_="opportunity",
                         company_id=o.company_id, lead_id=o.lead_id, opportunity_id=o.id)
        return back("/pipeline", "Opportunity updated")

    # ------------------------------------------------------------------ follow-ups
    @app.get("/followups", response_class=HTMLResponse)
    def followups(request: Request, show: str = "open"):
        with C().db.session() as s:
            needs_you = ("awaiting_approval", "uncertain")  # always listed, whatever else is open
            mine = record_condition(scope(request), FollowUp.lead_id)
            first = s.execute(select(FollowUp).where(FollowUp.status.in_(needs_you), mine)
                              .order_by(FollowUp.due_at)).scalars().all()
            stmt = select(FollowUp).where(FollowUp.status.not_in(needs_you), mine)
            if show == "open":
                stmt = stmt.where(FollowUp.status.in_(("pending", "approved", "sending")))
            total = s.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
            rest = s.execute(stmt.order_by(FollowUp.due_at).limit(FOLLOWUPS_PAGE)).scalars().all()
            rows = first + rest
            for r in rows:
                _ = r.lead.company, r.contact
            reconfirm = {r.id for r in rows if am.needs_reconfirmation(s, r)}
            ready, reason = mail_accounts.sending_status(s, C().settings)  # the primary panel mailbox, else .env
        return render(request, "followups.html", rows=rows, show=show, email_ready=ready, email_reason=reason,
                      hidden=total - len(rest), page_size=FOLLOWUPS_PAGE, reconfirm=reconfirm)

    @app.post("/followups/{fid}/{action}")
    def followup_action(request: Request, fid: int, action: str, subject: str = Form(""), body: str = Form(""),
                        qc_override: str = Form("")):
        with C().db.session() as s:
            f = s.get(FollowUp, fid) or _404()
            if action in ("approve", "save") and f.channel == "email" and f.status == "awaiting_approval":
                edited = False
                if subject.strip() and subject.strip() != f.subject:
                    f.subject, edited = subject.strip(), True
                if body.strip() and body.replace("\r\n", "\n") != f.body:
                    f.body, edited = body.replace("\r\n", "\n"), True
                if edited:
                    f.generator = f"template, edited by {who(request)}"[:80]
                address = C().settings.company_postal_address
                if address and POSTAL_PLACEHOLDER in (f.body or ""):  # any email kind drafted before it was set
                    f.body, edited = f.body.replace(POSTAL_PLACEHOLDER, address), True
                recipient = f.to_email or (f.contact.email if f.contact else None)
                suppressed = (is_suppressed(s, recipient, f.lead.company.domain if f.kind == "outreach" else None)
                              or bool(f.contact and f.contact.consent_status == "opted_out"))  # opted out any way
                qc_status = None
                if f.kind == "outreach":
                    if edited or action == "save":  # an edited email is checked again before anyone approves it
                        f.quality = {**(f.quality or {}), "qc": run_email_checks(
                            f.subject, f.body or "", list(f.evidence or []), f.contact, suppressed, step=f.step)}
                    qc_status = ((f.quality or {}).get("qc") or {}).get("status")
                if action == "save":
                    msg = "Saved" + (f"; quality checks: {qc_status}" if qc_status else "")
                elif QUESTION_PLACEHOLDER in (f.body or ""):
                    return back(f"/followups#f{f.id}", "Write your answer in place of the placeholder before approving")
                elif suppressed:  # checked on every approval, edited or not
                    return back(f"/followups#f{f.id}", "Cannot approve: this address is on the do-not-contact list")
                elif problem := compliance_problem(C().settings, f.body or ""):
                    return back(f"/followups#f{f.id}", f"Cannot approve: {problem}. Put the footer back first")
                elif qc_status in ("held", "failed") and qc_override != "yes":
                    return back(f"/followups#f{f.id}", f"Quality checks {qc_status}: review the findings and tick "
                                                       "'I reviewed the quality findings' to approve")
                else:
                    now = utcnow()
                    # The switch as this dashboard sees it, recorded before the approval: one given while sending is
                    # on is not asked to "Confirm again" because the follow-ups job had not run since the switch.
                    am.observe_sending(s, C().settings.outbound_email_enabled, now)
                    f.status, f.approved_by, f.approved_at = "approved", who(request), now
                    if f.kind == "outreach":
                        lead = f.lead
                        user = getattr(request.state, "user", None)
                        # the approver becomes the owner only if they work leads (an administrator approving does not)
                        if not lead.owner and (user is None or "receive_leads" in user.get("perms", ())):
                            lead.owner = who(request)
                        if f.step == 1:
                            set_outreach_status(s, lead, "approved", who(request))
                        if qc_status in ("held", "failed"):
                            log_activity(s, f"Outreach email #{f.id} approved despite {qc_status} quality checks",
                                         who(request), type_="override", company_id=f.company_id, lead_id=f.lead_id)
                    msg = "Approved. It will be sent by the follow-ups job if outbound email is enabled."
            elif action == "reconfirm" and f.channel == "email" and f.status == "approved" and f.approved_at:
                am.reconfirm(s, f, who(request))  # approved before sending was switched on: a person confirms it again
                msg = "Confirmed again: it goes out on the next follow-ups run"
            elif action == "reject" and f.status in ("awaiting_approval", "approved", "pending"):
                f.status = "rejected"
                if f.kind == "outreach" and f.step == 1 and f.lead.outreach_status in ("drafted", "approved"):
                    set_outreach_status(s, f.lead, "stopped", who(request), "first email rejected")
                msg = "Rejected"
            elif action == "done" and f.channel == "task":
                f.status = "done"
                msg = "Task completed"
            elif action == "mark_sent" and f.status == "uncertain":
                to = f.to_email or (f.contact.email if f.contact else None)
                record_delivery(s, f, f.send_started_at or utcnow(),
                                EmailSender(C().settings).recipient_for(to)[0] if to else None, who(request))
                msg = "Marked as sent (confirmed in your Sent folder)"
            elif action == "requeue" and f.status == "uncertain":
                f.status, f.send_started_at = "approved", None
                f.blocked_reason = "Requeued after a person confirmed it was not delivered"
                msg = "Requeued"
            else:
                raise HTTPException(400, "Action not allowed in the current state")
            log_activity(s, f"Follow-up #{f.id} {action}", who(request), type_="followup", company_id=f.company_id,
                         lead_id=f.lead_id)
            if f.channel == "email" and action in ("approve", "reject", "mark_sent", "requeue"):
                am.audit(s, who(request), f"email_{action}", f"follow-up #{f.id}",
                         f"{f.kind or 'email'} to {am.recipient(f) or 'no address'}: {(f.subject or '')[:120]}")
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
        if action == "save" and scheduled_et.strip():
            try:
                scheduled = datetime.fromisoformat(scheduled_et.strip()).replace(tzinfo=ZoneInfo("America/New_York"))
            except ValueError:
                return back(f"/content/{cid}", "The scheduled time is not a valid date and time; nothing was changed")
        with C().db.session() as s:
            # Take the database write lock before reading the post: the publisher's claim then waits until this
            # change is committed, so it can never publish an old text that this edit or rejection replaces.
            s.execute(update(ContentItem).where(ContentItem.id == cid).values(status=ContentItem.status)
                      .execution_options(synchronize_session=False))
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
                    item.publish_blocked_reason = None
                    log_activity(s, f"Post #{item.id} requeued after {who(request)} confirmed it is not on LinkedIn",
                                 who(request), type_="content")
                    msg = "Requeued for publishing"
                else:
                    raise HTTPException(400, "Uncertain posts need: confirm with the LinkedIn post URL/URN, or "
                                             "requeue after ticking that you checked LinkedIn")
                return back(f"/content/{cid}", msg)
            if action == "save":
                item.title = title.strip() or item.title
                # A browser sends the textarea's line breaks as CR LF: store plain ones, as email drafts do.
                item.body = body.replace("\r\n", "\n").replace("\r", "\n").strip() or item.body
                if hashtags.strip():
                    item.hashtags = ["#" + t.lstrip("#") for t in hashtags.replace(",", " ").split() if t.strip("#")]
                if scheduled_et.strip():
                    item.scheduled_for = scheduled.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
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
                item.publish_attempts, item.publish_blocked_reason = 0, None  # old reasons no longer apply
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
            if action in ("approve", "reject"):
                am.audit(s, who(request), f"post_{action}", f"post #{item.id}", (item.title or "")[:120])
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
        try:
            start, end = date.fromisoformat(start_date.strip()), date.fromisoformat(end_date.strip())
        except ValueError:
            return back("/campaigns", "Start and end dates must be valid dates (YYYY-MM-DD); nothing was created")
        with C().db.session() as s:
            s.add(Campaign(name=name, objective=objective, service=service or None, industry=industry or None,
                           start_date=start, end_date=end, status="active", created_by="human"))
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
        if not token:
            raise HTTPException(403, "The inbound API is off: set INBOUND_API_TOKEN in .env and send it as the "
                                     "X-Inbound-Token header")
        if not hmac.compare_digest(request.headers.get("x-inbound-token", "").encode("utf-8", "replace"), token.encode("utf-8")):
            raise HTTPException(401, "Invalid inbound token")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "Send the inquiry as JSON (Content-Type: application/json)")
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
        value = value.strip().lower()
        if not SUPPRESS_VALUE.fullmatch(value):
            return back("/followups", "Give one email address or one domain (example.com) to add to the do-not-contact list")
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

    @app.get("/command-center/agents/{agent}", response_class=HTMLResponse)
    def agent_detail(request: Request, agent: str):
        if agent not in TEAM_TITLES:
            raise HTTPException(404, "No such agent")
        ctx = C()
        with ctx.db.session() as s:
            view = ai_employee(ctx, s)
            runs = s.execute(select(AgentRun).where(AgentRun.agent == agent).order_by(AgentRun.id.desc()).limit(25)).scalars().all()
            totals = dict(s.execute(select(AgentRun.status, func.count(AgentRun.id)).where(AgentRun.agent == agent)
                                    .group_by(AgentRun.status)).all())
        member = next(a for a in view["team"] if a["agent"] == agent)
        jobs = [row for row in view["schedule"] if row["job"] in member["jobs"]]
        return render(request, "agent_detail.html", a=member, runs=runs, totals=totals, jobs=jobs, now=utcnow())

    @app.get("/agents/runs/{run_pk}", response_class=HTMLResponse)
    def agent_run(request: Request, run_pk: int):
        with C().db.session() as s:
            run = s.get(AgentRun, run_pk) or _404()
        return render(request, "agent_run.html", run=run)

    # ------------------------------------------------------------------ search, approval queue, settings
    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = ""):
        q = " ".join(q.split())[:100]
        groups: list[tuple[str, str, list[tuple[str, str, str]]]] = []
        if len(q) >= 2:
            like = f"%{q.lower()}%"
            with C().db.session() as s:
                companies = s.execute(select(Company).where(Company.merged_into_id.is_(None), or_(
                    func.lower(Company.name).like(like), func.lower(Company.domain).like(like))).limit(12)).scalars().all()
                leads = {lead.company_id: lead.id for lead in s.execute(select(Lead).where(
                    Lead.company_id.in_([c.id for c in companies] or [0]), lead_condition(scope(request)))).scalars()}
                groups.append(("Companies", "building", [
                    (c.name, f"/leads/{leads[c.id]}" if c.id in leads else "/companies", " · ".join(
                        x for x in (c.domain, c.state, c.website_status) if x)) for c in companies]))
                contacts = s.execute(select(Contact).where(or_(
                    func.lower(Contact.first_name).like(like), func.lower(Contact.last_name).like(like),
                    func.lower(Contact.email).like(like), func.lower(Contact.title).like(like))).limit(12)).scalars().all()
                groups.append(("Contacts", "users", [
                    (c.display_name, "/contacts", " · ".join(x for x in (c.title, c.email, c.company.name if c.company else None) if x))
                    for c in contacts]))
                emails = s.execute(select(FollowUp).where(FollowUp.channel == "email",
                                                          record_condition(scope(request), FollowUp.lead_id), or_(
                    func.lower(FollowUp.subject).like(like), func.lower(FollowUp.to_email).like(like)))
                    .order_by(FollowUp.id.desc()).limit(12)).scalars().all()
                groups.append(("Emails", "mail", [(f.subject, f"/followups#f{f.id}", f"{f.status.replace('_', ' ')} · to {f.to_email or '—'}")
                                                  for f in emails]))
                replies = s.execute(select(InboundEmail).where(record_condition(scope(request), InboundEmail.lead_id), or_(
                    func.lower(InboundEmail.subject).like(like), func.lower(InboundEmail.from_email).like(like)))
                    .order_by(InboundEmail.id.desc()).limit(12)).scalars().all()
                groups.append(("Conversations", "chat", [(r.subject or "(no subject)", "/inbox?show=all",
                                                          f"{r.from_email} · {(r.category or 'new').replace('_', ' ')}") for r in replies]))
                meetings = s.execute(select(Meeting).where(record_condition(scope(request), Meeting.lead_id), or_(
                    func.lower(Meeting.invitee_name).like(like), func.lower(Meeting.invitee_email).like(like),
                    func.lower(Meeting.title).like(like))).order_by(Meeting.starts_at.desc()).limit(12)).scalars().all()
                groups.append(("Meetings", "calendar", [(f"{m.invitee_name or m.invitee_email} · {m.title}",
                                                         f"/leads/{m.lead_id}" if m.lead_id else "/meetings",
                                                         f"{fmt_et(m.starts_at)} · {m.status}") for m in meetings]))
        return render(request, "search.html", q=q, groups=groups, found=sum(len(rows) for _, _, rows in groups))

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request):
        with C().db.session() as s:
            mine = record_condition(scope(request), FollowUp.lead_id)
            emails = s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status.in_(
                ("awaiting_approval", "uncertain")), mine).order_by(FollowUp.due_at)).scalars().all()
            for f in emails:
                _ = f.lead.company, f.contact
            posts = s.execute(select(ContentItem).where(ContentItem.status == "awaiting_approval")
                              .order_by(ContentItem.scheduled_for)).scalars().all()
            reconfirm = s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "approved",
                                                         mine)).scalars().all()
            reconfirm = [f for f in reconfirm if am.needs_reconfirmation(s, f)]
            for f in reconfirm:
                _ = f.lead.company
        return render(request, "approvals.html", emails=emails, posts=posts, reconfirm=reconfirm)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        ctx = C()
        st = ctx.settings
        with ctx.db.session() as s:
            mode, state = am.get_mode(s), am.get_state(s)
            ready, reason = mail_accounts.sending_status(s, st)  # the primary panel mailbox, else .env
            box = mail_accounts.primary(s)
            server, sender = (box.smtp_host, box.email) if box else (st.smtp_host or "not set", st.smtp_from or "not set")
            reading, read_where = mail_accounts.reading_status(s, st)
        follow = st.section("followup")
        outreach = st.section("outreach")
        sections = [
            ("Company", [("Company name", st.company_name), ("Sender name", st.sender_name or "—"),
                         ("Postal address (CAN-SPAM footer)", st.company_postal_address or "not set"),
                         ("Unsubscribe address", st.unsubscribe_email or st.smtp_from or "—")]),
            ("Automation", [("Operating mode", mode.title()), ("Automation state", state.replace("_", " ").title()),
                            ("REQUIRE_HUMAN_APPROVAL", str(st.require_human_approval).lower()),
                            ("PUBLISHING_ENABLED", str(st.publishing_enabled).lower())]),
            ("Email", [("OUTBOUND_EMAIL_ENABLED", str(st.outbound_email_enabled).lower()),
                       ("Mail server", server), ("Sender address", sender),
                       ("Mail server status", "ready" if ready else reason),
                       ("Reading replies (IMAP)", read_where if reading else "off"),
                       ("Test mode recipients", ", ".join(sorted(st.test_recipients)) or "off")]),
            ("Outreach rules", [("Send window", f"{follow.get('send_window_start', '08:00')}–{follow.get('send_window_end', '18:00')} "
                                                f"{follow.get('send_window_timezone', 'America/New_York')}, weekdays"),
                                ("Daily outreach limit", str(outreach.get("daily_send_limit", 20))),
                                ("Per domain per day", str(outreach.get("per_domain_daily_limit", 1))),
                                ("Sequence (days after the first email)", ", ".join(str(d) for d in outreach.get("intervals_days", [0, 4, 10])))]),
            ("Meetings", [("Calendly link", st.calendly_url or "not set")]),
            ("This installation", [("Data", st.data_role), ("Data folder", str(st.data_dir)),
                                   ("Dashboard sign-in", "on" if st.dashboard_auth else "off (local preview)")]),
        ]
        return render(request, "settings.html", sections=sections)

    # ------------------------------------------------------------------ automation control center
    def is_admin(request: Request) -> bool:
        user = getattr(request.state, "user", None)
        return not C().settings.dashboard_auth or bool(user and "admin_ops" in user.get("perms", ()))

    @app.get("/automation", response_class=HTMLResponse)
    def automation_page(request: Request):
        ctx = C()
        now = utcnow()
        with ctx.db.session() as s:
            ready, reason = mail_accounts.sending_status(s, ctx.settings)  # the primary panel mailbox, else .env
            am.expire_authorizations(s)
            auths = s.execute(select(CampaignAuthorization).order_by(CampaignAuthorization.id.desc()).limit(50)).scalars().all()
            queued = s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "approved")).scalars().all()
            pol = ctx.settings.section("outreach")
            data = dict(
                mode=am.get_mode(s), state=am.get_state(s), auths=auths, counts={a.id: am.sent_counts(s, a, now) for a in auths},
                blockers=am.blockers(s, ctx.settings, ready, reason), sender_ready=ready, sender_reason=reason,
                waiting=s.execute(select(func.count(FollowUp.id)).where(FollowUp.channel == "email",
                                                                    FollowUp.status == "awaiting_approval")).scalar_one(),
                posts_waiting=s.execute(select(func.count(ContentItem.id)).where(
                    ContentItem.status == "awaiting_approval")).scalar_one(),
                queued=len(queued), reconfirm=sum(am.needs_reconfirmation(s, f) for f in queued),
                sent_24h=s.execute(select(func.count(FollowUp.id)).where(
                    FollowUp.channel == "email", FollowUp.status.in_(am.COUNTS_AS_SENT),
                    func.coalesce(FollowUp.sent_at, FollowUp.send_started_at) >= now - timedelta(hours=24))).scalar_one(),
                limits={"daily": pol.get("daily_send_limit", 20), "per_domain": pol.get("per_domain_daily_limit", 1),
                        "per_run": pol.get("sends_per_run", 5)},
                failing=s.execute(select(JobState).where(JobState.consecutive_failures > 0)).scalars().all(),
                failed_runs=s.execute(select(JobRun).where(JobRun.status.in_(("failed", "partial")),
                                                           JobRun.started_at >= now - timedelta(hours=24))
                                      .order_by(JobRun.id.desc()).limit(8)).scalars().all(),
                agents=agent_stats(s, days=1),
                alerts=s.execute(select(Alert).where(Alert.acknowledged.is_(False), Alert.level.in_(("error", "warning")))
                                 .order_by(Alert.id.desc()).limit(8)).scalars().all(),
                events=s.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(20)).scalars().all(),
            )
        return render(request, "automation.html", modes=am.MODES, states=am.STATES, kinds=am.KINDS,
                      transitions=am.TRANSITIONS, admin_actions=am.ADMIN_ACTIONS, is_admin=is_admin(request),
                      test_recipients=sorted(ctx.settings.test_recipients), heartbeat=read_heartbeat(ctx.settings),
                      settings_flags={"OUTBOUND_EMAIL_ENABLED": ctx.settings.outbound_email_enabled,
                                      "REQUIRE_HUMAN_APPROVAL": ctx.settings.require_human_approval,
                                      "PUBLISHING_ENABLED": ctx.settings.publishing_enabled},
                      limits_max={"days": am.MAX_DAYS, "recipients": am.MAX_RECIPIENTS, "daily": am.MAX_DAILY,
                                  "total": am.MAX_TOTAL}, **data)

    @app.post("/automation/mode")
    def automation_mode(request: Request, mode: str = Form(...), next: str = Form("/automation")):
        if not is_admin(request):
            raise HTTPException(403, "Only an administrator can change the operating mode")
        with C().db.session() as s:
            try:
                am.set_mode(s, mode, who(request))
            except ValueError as exc:
                return back(safe_next(next), str(exc))
        return back(safe_next(next), f"Operating mode set to {mode.title()}")

    @app.post("/automation/state/{action}")
    def automation_state(request: Request, action: str, reason: str = Form(""), next: str = Form("/automation")):
        if action in am.ADMIN_ACTIONS and not is_admin(request):
            raise HTTPException(403, "Only an administrator can start or resume automation")
        with C().db.session() as s:
            try:
                new = am.change_state(s, action, who(request), reason.strip()[:300])
            except ValueError as exc:
                return back(safe_next(next), str(exc))
        return back(safe_next(next), f"Automation: {am.STATES[new]}")

    @app.post("/automation/authorizations")
    def automation_authorize(request: Request, name: str = Form(...), recipients: str = Form(...),
                             kinds: Annotated[list[str] | None, Form()] = None, daily_limit: int = Form(5), total_limit: int = Form(20),
                             days: int = Form(7), notes: str = Form("")):
        if not is_admin(request):
            raise HTTPException(403, "Only an administrator can authorize a campaign")
        with C().db.session() as s:
            try:
                a = am.create_authorization(s, who(request), name, recipients, kinds or [], daily_limit, total_limit, days, notes)
            except ValueError as exc:
                return back("/automation#authorize", str(exc))
            aid = a.id
        return back("/automation#authorizations", f"Campaign authorization #{aid} saved as a draft: activate it to use it")

    @app.post("/automation/authorizations/rule")
    def automation_authorize_rule(request: Request, name: str = Form(...), min_grade: str = Form("B"),
                                  addresses: str = Form("role_or_published"), verified_website: str = Form(""),
                                  daily_limit: int = Form(5), total_limit: int = Form(20), days: int = Form(7),
                                  notes: str = Form(""), answer_replies: str = Form("")):
        if not is_admin(request):
            raise HTTPException(403, "Only an administrator can authorize a campaign")
        with C().db.session() as s:
            try:
                a = am.create_rule_authorization(s, who(request), name, min_grade, addresses, verified_website == "yes",
                                                 daily_limit, total_limit, days, notes, answer_replies=answer_replies == "yes")
            except ValueError as exc:
                return back("/automation#authorize-rule", str(exc))
            aid = a.id
        return back("/automation#authorizations", f"Rule-based authorization #{aid} saved as a draft: activate it to use it")

    @app.post("/automation/authorizations/{aid}/{action}")
    def automation_authorization_action(request: Request, aid: int, action: str):
        if not is_admin(request):
            raise HTTPException(403, "Only an administrator can change a campaign authorization")
        with C().db.session() as s:
            a = s.get(CampaignAuthorization, aid) or _404()
            try:
                am.change_authorization(s, a, action, who(request))
            except ValueError as exc:
                return back("/automation#authorizations", str(exc))
            status = a.status
        return back("/automation#authorizations", f"Campaign authorization #{aid} is now {status}")

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, action: str = ""):
        with C().db.session() as s:
            q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(300)
            if action:
                q = q.where(AuditEvent.action.like(f"{action}%"))
            events = s.execute(q).scalars().all()
            actions = [r[0] for r in s.execute(select(AuditEvent.action).distinct().order_by(AuditEvent.action))]
        return render(request, "audit.html", events=events, actions=actions, action=action)

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
            view = analytics_view(C(), s)
        return render(request, "reports.html", rows=rows, **view)

    @app.get("/reports/{rid}", response_class=HTMLResponse)
    def report_view(rid: int):
        with C().db.session() as s:
            r = s.get(Report, rid) or _404()
        path = Path(r.path_html)
        if not path.exists():
            raise HTTPException(404, "Report file missing")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ integrations
    # ------------------------------------------------------------------ Super Admin: mailboxes
    # The auth middleware lets only a Super Admin reach /admin/mailboxes and /api/mailboxes (viewing included); the
    # same-origin check covers every change. Secrets are write-only: never rendered or returned.
    # ------------------------------------------------------------------ Super Admin: sales settings
    @app.get("/admin/settings", response_class=HTMLResponse)
    def sales_settings_page(request: Request):
        from aime import admin_settings
        from aime.knowledge.industries import INDUSTRIES
        from aime.knowledge.services import SERVICES
        ctx = C()
        with ctx.db.session() as s:
            users = [(u.username, u.role) for u in s.execute(select(User).where(User.active.is_(True))
                                                             .order_by(User.username)).scalars()]
            overridden = {f"{sec}.{k}" for sec, keys in admin_settings.overrides(s).items() for k in keys}
        values = admin_settings.current(ctx.settings)
        country = str(values["targeting.country"] or "US").upper()
        return render(request, "admin_settings.html", fields=admin_settings.FIELDS, values=values, users=users,
                      overridden=overridden, industries=INDUSTRIES, services=SERVICES,
                      coverage=admin_settings.COUNTRY_SOURCES.get(country))

    @app.post("/admin/settings")
    async def sales_settings_save(request: Request):
        from aime import admin_settings
        ctx = C()
        form = await request.form()
        with ctx.db.session() as s:
            try:
                changes = admin_settings.save(s, ctx.settings, who(request), form)
            except ValueError as exc:
                s.rollback()
                return back("/admin/settings", f"Not saved: {exc}")
        return back("/admin/settings", f"Saved: {len(changes)} change(s), in effect now and on the next scheduled run"
                    if changes else "Nothing changed")

    @app.post("/admin/settings/reset")
    def sales_settings_reset(request: Request):
        from aime import admin_settings
        ctx = C()
        with ctx.db.session() as s:
            admin_settings.reset(s, ctx.settings, who(request))
        return back("/admin/settings", "Back to the defaults in config/business.toml")

    @app.get("/admin/mailboxes", response_class=HTMLResponse)
    def mailboxes_page(request: Request, edit: int | None = None):
        ctx = C()
        with ctx.db.session() as s:
            rows = [mail_accounts.public(a) for a in s.execute(select(MailAccount).order_by(MailAccount.id)).scalars()]
            for r in rows:
                r["connected"] = r["auth"] == "oauth" and oauth.connected(s, "microsoft", "mail", account=r["id"]) is not None
            ready, why = EmailSender(mail_accounts.sending_settings(s, ctx.settings)).status()
        editing = next((r for r in rows if r["id"] == edit), None)
        return render(request, "mailboxes.html", rows=rows, presets=mail_accounts.PRESETS, editing=editing,
                      sending_on=ctx.settings.outbound_email_enabled, sender_status=why if not ready else "ready",
                      redirect_uri=oauth.redirect_uri(ctx.settings))

    def _mailbox_form(form) -> dict:
        keys = ("provider", "label", "email", "username", "secret", "imap_host", "imap_port", "imap_folder",
                "smtp_host", "smtp_port", "oauth_client_id", "oauth_client_secret", "oauth_tenant")
        return {k: form.get(k) or "" for k in keys}

    @app.post("/admin/mailboxes")
    async def mailbox_create(request: Request):
        form = _mailbox_form(await request.form())
        with C().db.session() as s:
            try:
                a = mail_accounts.save(s, who(request), form)
            except ValueError as exc:
                s.rollback()
                return back("/admin/mailboxes#add", str(exc))
            aid = a.id
        return back("/admin/mailboxes", f"Mailbox #{aid} saved. It is disabled: nothing is read or sent until you "
                    "enable it (and sending also needs OUTBOUND_EMAIL_ENABLED)")

    @app.post("/admin/mailboxes/{aid}/edit")
    async def mailbox_edit(request: Request, aid: int):
        form = _mailbox_form(await request.form())
        with C().db.session() as s:
            try:
                mail_accounts.save(s, who(request), form, account_id=aid)
            except ValueError as exc:
                s.rollback()
                return back(f"/admin/mailboxes?edit={aid}", str(exc))
        return back("/admin/mailboxes", f"Mailbox #{aid} updated")

    @app.post("/admin/mailboxes/{aid}/test")
    def mailbox_test(request: Request, aid: int):
        ctx = C()
        with ctx.db.session() as s, ctx.http() as http:
            a = s.get(MailAccount, aid) or _404()
            ok, detail = mail_accounts.test_connection(s, ctx.settings, a, http, who(request))
        return back("/admin/mailboxes", f"Test {'passed' if ok else 'failed'}: {detail}")

    @app.get("/admin/mailboxes/{aid}/connect")
    def mailbox_connect(aid: int):
        ctx = C()
        with ctx.db.session() as s:
            a = s.get(MailAccount, aid) or _404()
            if a.auth != "oauth":
                return back("/admin/mailboxes", "This mailbox signs in with an app password: nothing to connect")
            try:
                url = oauth.start(s, ctx.settings, "microsoft", "mail", account=aid)
            except oauth.OAuthError as exc:
                return back("/admin/mailboxes", str(exc))
        return RedirectResponse(url, status_code=302)

    @app.post("/admin/mailboxes/{aid}/{action}")
    def mailbox_action(request: Request, aid: int, action: str):
        if action not in ("enable", "disable", "read_on", "read_off", "primary", "delete"):
            raise HTTPException(400, "Unknown action")
        with C().db.session() as s:
            try:
                a = mail_accounts.change(s, who(request), aid, action)
            except ValueError as exc:
                s.rollback()
                return back("/admin/mailboxes", str(exc))
            email = a.email
        words = {"enable": "enabled (it still reads nothing until 'read replies' is on, and sends only as the primary "
                           "mailbox with OUTBOUND_EMAIL_ENABLED)", "disable": "disabled: it is neither read nor used to send",
                 "read_on": "will be read for replies (read-only)", "read_off": "is no longer read",
                 "primary": "is now the sending mailbox (sending still needs OUTBOUND_EMAIL_ENABLED and every approval)",
                 "delete": "deleted, with its stored secrets and tokens"}
        return back("/admin/mailboxes", f"{email} {words[action]}")

    @app.get("/api/mailboxes")
    def api_mailboxes():
        with C().db.session() as s:
            return {"mailboxes": [mail_accounts.public(a) for a in s.execute(select(MailAccount).order_by(MailAccount.id)).scalars()]}

    @app.post("/api/mailboxes")
    async def api_mailbox_create(request: Request):
        body = await request.json()
        with C().db.session() as s:
            try:
                a = mail_accounts.save(s, who(request), body)
            except ValueError as exc:
                s.rollback()
                return JSONResponse({"detail": str(exc)}, status_code=400)
            return JSONResponse(mail_accounts.public(a), status_code=201)

    @app.patch("/api/mailboxes/{aid}")
    async def api_mailbox_edit(request: Request, aid: int):
        body = await request.json()
        with C().db.session() as s:
            try:
                return mail_accounts.public(mail_accounts.save(s, who(request), body, account_id=aid))
            except ValueError as exc:
                s.rollback()
                return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/api/mailboxes/{aid}/{action}")
    def api_mailbox_action(request: Request, aid: int, action: str):
        ctx = C()
        with ctx.db.session() as s:
            if action == "test":
                a = s.get(MailAccount, aid)
                if a is None:
                    return JSONResponse({"detail": "No such mailbox"}, status_code=404)
                with ctx.http() as http:
                    ok, detail = mail_accounts.test_connection(s, ctx.settings, a, http, who(request))
                return {"ok": ok, "detail": detail}
            if action not in ("enable", "disable", "read_on", "read_off", "primary"):
                return JSONResponse({"detail": "Unknown action"}, status_code=400)
            try:
                return mail_accounts.public(mail_accounts.change(s, who(request), aid, action))
            except ValueError as exc:
                s.rollback()
                return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.delete("/api/mailboxes/{aid}")
    def api_mailbox_delete(request: Request, aid: int):
        with C().db.session() as s:
            try:
                mail_accounts.change(s, who(request), aid, "delete")
            except ValueError as exc:
                s.rollback()
                return JSONResponse({"detail": str(exc)}, status_code=404)
        return {"deleted": aid}

    @app.get("/integrations", response_class=HTMLResponse)
    def integrations(request: Request):
        ctx = C()
        with ctx.db.session() as s:
            statuses = integration_statuses(ctx, s)
            auth = get_auth_status(ctx.settings, s)
            accounts = [{"provider": p, "label": oauth.PROVIDERS[p]["label"], "purpose": purpose,
                         "configured": oauth.configured(ctx.settings, p), "token": oauth.connected(s, p, purpose)}
                        for p in oauth.PROVIDERS for purpose in oauth.PURPOSES]
            leads = linkedin_leads.status(ctx.settings, s)
        return render(request, "integrations.html", statuses=statuses, auth=auth, settings=ctx.settings,
                      accounts=accounts, redirect_uri=oauth.redirect_uri(ctx.settings), leads=leads)

    # ------------------------------------------------------------------ LinkedIn leads (Super Admin)
    @app.get("/integrations/linkedin-leads/connect")
    def linkedin_leads_connect():
        ctx = C()
        with ctx.db.session() as s:
            try:
                url = linkedin_leads.start(s, ctx.settings)
            except linkedin_leads.LeadSyncError as exc:
                return back("/integrations#linkedin-leads", str(exc))
        return RedirectResponse(url, status_code=302)

    @app.get("/integrations/linkedin-leads/callback")
    def linkedin_leads_callback(request: Request, code: str = "", state: str = "", error: str = "",
                                error_description: str = ""):
        ctx = C()
        if error:
            return back("/integrations#linkedin-leads", f"LinkedIn sign-in declined: {error_description or error}")
        with ctx.db.session() as s, ctx.http() as http:
            try:
                linkedin_leads.finish(s, ctx.settings, http, state, code, who(request))
            except linkedin_leads.LeadSyncError as exc:
                s.commit()  # the one-time sign-in state is used up either way
                return back("/integrations#linkedin-leads", str(exc))
            except httpx.HTTPError as exc:
                s.commit()
                return back("/integrations#linkedin-leads", f"Could not reach LinkedIn ({type(exc).__name__}); try again")
        return back("/integrations#linkedin-leads", "LinkedIn leads connected: choose which Pages and ad accounts to "
                    "sync. Nothing is fetched until LINKEDIN_LEADS_ENABLED=true.")

    @app.post("/integrations/linkedin-leads/refresh")
    def linkedin_leads_refresh():
        ctx = C()
        with ctx.db.session() as s, ctx.http() as http:
            try:
                found = linkedin_leads.refresh_found(s, ctx.settings, http)
            except linkedin_leads.LeadSyncError as exc:
                return back("/integrations#linkedin-leads", str(exc))
            except httpx.HTTPError as exc:
                return back("/integrations#linkedin-leads", f"Could not reach LinkedIn ({type(exc).__name__}); try again")
        return back("/integrations#linkedin-leads", f"LinkedIn lists {len(found)} Page(s) and ad account(s) for you")

    @app.post("/integrations/linkedin-leads/owners")
    async def linkedin_leads_owners(request: Request):
        form = await request.form()
        with C().db.session() as s:
            chosen = linkedin_leads.choose_owners(s, list(form.getlist("owner")), who(request))
        return back("/integrations#linkedin-leads", f"Syncing leads from {len(chosen)} Page(s) and ad account(s)"
                    if chosen else "No Page or ad account chosen: nothing is synced")

    @app.post("/integrations/linkedin-leads/disconnect")
    def linkedin_leads_disconnect(request: Request):
        with C().db.session() as s:
            removed = linkedin_leads.disconnect(s, who(request))
        return back("/integrations#linkedin-leads", "LinkedIn leads disconnected: the tokens were deleted from this "
                    "computer" if removed else "Nothing was connected")

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
            except httpx.HTTPError as exc:
                return back("/integrations", f"Could not reach LinkedIn ({type(exc).__name__}); please try again")
            client.store_token(s, tok, info)
        return back("/integrations", "LinkedIn connected")

    @app.get("/integrations/oauth/{provider}/{purpose}/connect")
    def oauth_connect(provider: str, purpose: str):
        ctx = C()
        with ctx.db.session() as s:
            try:
                url = oauth.start(s, ctx.settings, provider, purpose)
            except oauth.OAuthError as exc:
                return back("/integrations#accounts", str(exc))
        return RedirectResponse(url, status_code=302)

    @app.get("/integrations/oauth/callback")
    def oauth_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
        ctx = C()
        if error:
            return back("/integrations#accounts", f"Sign-in declined: {error_description or error}")
        with ctx.db.session() as s, ctx.http() as http:
            try:
                row = oauth.finish(s, ctx.settings, http, state, code, actor=who(request))
            except oauth.OAuthError as exc:
                return back("/integrations#accounts", str(exc))
            key = row.provider
        return back("/integrations#accounts", f"{key.replace(':', ' ')} connected. Nothing is read, sent or booked "
                    "until its own switch is turned on.")

    @app.post("/integrations/oauth/{provider}/{purpose}/disconnect")
    def oauth_disconnect(request: Request, provider: str, purpose: str):
        with C().db.session() as s:
            removed = oauth.disconnect(s, provider, purpose)
            if removed:
                am.audit(s, who(request), "account_disconnected", oauth.token_key(provider, purpose), "tokens deleted")
        return back("/integrations#accounts", "Disconnected: the tokens were deleted from this computer" if removed
                    else "Nothing was connected")

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

    register_team_pages(app, C, render, back, who)

    @app.get("/api/leads")
    def api_leads(request: Request, status: str = "qualified", limit: int = 50):
        with C().db.session() as s:
            rows = s.execute(select(Lead).where(Lead.status == status, lead_condition(scope(request)))
                             .order_by(Lead.score.desc()).limit(min(limit, 500))).scalars().all()
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
    if li.expires_at and li.expires_at <= utcnow():
        rows.append(("LinkedIn connection", "bad", "expired: reconnect LinkedIn on Integrations"))
    elif li.expires_at:
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


HUMAN_REPLIES = ("interested", "not_interested", "objection", "referral", "question", "other")


def money(value: float | None) -> str:
    return f"${value:,.0f}" if value else "—"


def funnel_detail(s) -> list[tuple[str, int, str]]:
    """Every step from a company found to a deal won, each counted from the records and linked to them."""
    cnt = lambda m, *c: s.execute(select(func.count(m.id)).where(*c)).scalar_one()  # noqa: E731
    distinct = lambda col, *c: s.execute(select(func.count(func.distinct(col))).where(*c)).scalar_one()  # noqa: E731
    open_co = (Company.merged_into_id.is_(None),)
    return [
        ("Companies found", cnt(Company, *open_co), "/companies"),
        ("Websites verified", cnt(Company, Company.website_status == "verified", *open_co), "/companies"),
        ("Companies with a decision-maker", distinct(Contact.company_id, Contact.source.in_(("website", "sec_filing")),
                                                     Contact.last_name.is_not(None)), "/contacts"),
        ("Companies with a usable email", distinct(Contact.company_id, *USABLE_EMAIL), "/contacts?reachable=1"),
        ("Qualified companies", distinct(Lead.company_id, Lead.status == "qualified"), "/leads?status=qualified"),
        ("Emails awaiting approval", cnt(FollowUp, FollowUp.channel == "email", FollowUp.status == "awaiting_approval"),
         "/followups"),
        ("Posts awaiting approval", cnt(ContentItem, ContentItem.status == "awaiting_approval"), "/content"),
        ("Posts scheduled", cnt(ContentItem, ContentItem.status == "approved"), "/content"),
        ("Posts published", cnt(ContentItem, ContentItem.status == "published"), "/content?status=published"),
        ("Open opportunities", cnt(Opportunity, Opportunity.stage.not_in(("won", "lost"))), "/pipeline"),
        ("Outreach emails sent", cnt(FollowUp, FollowUp.kind == "outreach", FollowUp.status == "sent"), "/followups?show=all"),
        ("Replies (real)", cnt(InboundEmail, InboundEmail.is_simulated.is_(False), InboundEmail.category.in_(HUMAN_REPLIES)),
         "/inbox?show=all"),
        ("Interested", cnt(Lead, Lead.outreach_status.in_(("interested", "meeting"))), "/pipeline"),
        ("Meetings booked", cnt(Meeting, Meeting.is_simulated.is_(False), Meeting.status != "cancelled"), "/meetings"),
        ("Deals won", cnt(Opportunity, Opportunity.stage == "won"), "/pipeline"),
    ]


def analytics_view(ctx: AppContext, s) -> dict:
    """Sales Analytics: eight weeks of leads, emails and meetings, the funnel, opportunities by stage, and every funnel
    step as a tile. All counted from the records; a chart with nothing to draw says so."""
    from aime.web import charts

    now = utcnow()
    since = now - timedelta(weeks=8)
    cnt = lambda m, *c: s.execute(select(func.count(m.id)).where(*c)).scalar_one()  # noqa: E731

    def weeks(column, *where):
        return charts.weekly_buckets(s.execute(select(column).where(column >= since, *where)).scalars().all())

    leads_w = weeks(Lead.created_at)
    sent_w = weeks(FollowUp.sent_at, FollowUp.status == "sent")
    replies_w = weeks(InboundEmail.received_at, InboundEmail.is_simulated.is_(False),
                      InboundEmail.category.in_(HUMAN_REPLIES))
    meetings_w = weeks(Meeting.created_at, Meeting.is_simulated.is_(False))
    labels = [w for w, _ in leads_w]
    contacted = s.execute(select(func.count(func.distinct(FollowUp.lead_id))).where(
        FollowUp.kind == "outreach", FollowUp.status == "sent")).scalar_one()
    replied = s.execute(select(func.count(func.distinct(InboundEmail.lead_id))).where(
        InboundEmail.is_simulated.is_(False), InboundEmail.category.in_(HUMAN_REPLIES),
        InboundEmail.lead_id.is_not(None))).scalar_one()
    by_stage = dict(s.execute(select(Opportunity.stage, func.count(Opportunity.id)).group_by(Opportunity.stage)).all())
    funnel = [("Companies discovered", cnt(Company, Company.merged_into_id.is_(None)), "/companies"),
              ("Qualified", cnt(Lead, Lead.status == "qualified"), "/leads?status=qualified"),
              ("Contacted", contacted, "/followups?show=all"), ("Replied", replied, "/inbox?show=all"),
              ("Interested", cnt(Lead, Lead.outreach_status.in_(("interested", "meeting"))), "/pipeline"),
              ("Meeting booked", cnt(Meeting, Meeting.is_simulated.is_(False), Meeting.status != "cancelled"), "/meetings"),
              ("Won", by_stage.get("won", 0), "/pipeline")]
    return dict(
        chart_leads=charts.column_chart(labels, [("Leads", [n for _, n in leads_w])], "Leads created per week",
                                        "No leads created in the last 8 weeks"),
        chart_email=charts.column_chart(labels, [("Emails sent", [n for _, n in sent_w]),
                                                 ("Replies", [n for _, n in replies_w])],
                                        "Emails sent and replies per week", "No emails sent or replies in the last 8 weeks"),
        chart_meetings=charts.column_chart(labels, [("Meetings booked", [n for _, n in meetings_w])],
                                           "Meetings booked per week", "No meetings booked in the last 8 weeks"),
        chart_funnel=charts.bar_list(funnel, "Sales funnel", "The funnel fills as research runs"),
        chart_pipeline=charts.bar_list([(stage.title(), by_stage.get(stage, 0), "/pipeline") for stage in STAGES],
                                       "Opportunities by stage", "No opportunities yet: they open on an interested reply"),
        funnel_tiles=funnel_detail(s),
    )


def operations_view(ctx: AppContext, s) -> dict:
    """How the work runs, for the Sales Command Center: the execution timeline (agent runs, CRM changes and control
    actions, each with its status) and the automation, email and Calendly status. No business metric shown on the
    dashboard is repeated here."""
    st = ctx.settings
    now = utcnow()
    timeline = []
    open_runs = {r.id for r in running_agent_runs(s, now)}
    for r in s.execute(select(AgentRun).order_by(AgentRun.id.desc()).limit(14)).scalars():
        if r.status == "running" and r.id not in open_runs:
            continue  # a leftover row of a finished or abandoned job: the monitor reports it; it is not running
        status = {"success": "completed", "running": "running"}.get(r.status, "failed")
        took = f" · {r.duration_ms / 1000:.1f} s" if r.duration_ms else ""
        timeline.append({"at": r.finished_at or r.started_at, "kind": "Agent run", "icon": "cpu", "status": status,
                         "text": TEAM_TITLES.get(r.agent, r.agent.replace("_", " ").capitalize()),
                         "sub": f"{(r.job or 'manual').replace('_', ' ')}{took}", "href": f"/agents/runs/{r.id}"})
    for a in s.execute(select(Activity).order_by(Activity.id.desc()).limit(10)).scalars():
        timeline.append({"at": a.created_at, "kind": "CRM", "icon": ACTIVITY_LOOK.get(a.type, ("sparkle",))[0],
                         "status": "logged", "text": a.subject, "sub": f"by {a.actor}",
                         "href": f"/leads/{a.lead_id}" if a.lead_id else None})
    for e in s.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(6)).scalars():
        timeline.append({"at": e.at, "kind": "Control", "icon": "sliders", "status": "control",
                         "text": e.action.replace("_", " ").capitalize() + (f": {e.target}" if e.target else ""),
                         "sub": f"by {e.actor}", "href": "/audit"})
    timeline.sort(key=lambda row: row["at"] or datetime.min, reverse=True)
    for row in timeline:
        row["ago"] = ago(row["at"], now)
    ready, reason = EmailSender(mail_accounts.sending_settings(s, st)).status()
    box = mail_accounts.primary(s)
    return dict(
        timeline=timeline[:14],
        automation={"mode": am.get_mode(s), "state": am.get_state(s), "active": len(am.active_authorizations(s, now)),
                    "blockers": am.blockers(s, st, ready, reason)},
        gmail={"sending": st.outbound_email_enabled, "ready": ready, "reason": reason,
               "sender": box.email if box else st.smtp_from,
               "reading": mail_accounts.reading_status(s, st)[0], "test_mode": len(st.test_recipients)},
        calendly={"url": st.calendly_url,
                  "last_notice": s.execute(select(func.max(InboundEmail.received_at)).where(
                      InboundEmail.category == "calendly")).scalar_one()},
    )


# The AI employee's team in workflow order: the agent, its title, what it does, its stage of the workflow, its icon.
TEAM = [
    ("research", "Company discovery", "Finds US companies from approved sources", "Find", "search"),
    ("company_research", "Company research", "Reads company websites and SEC filings; finds decision-makers", "Find", "building"),
    ("qualification", "Lead qualification", "Scores and tiers every lead, with its reasons", "Qualify", "target"),
    ("crm", "CRM upkeep", "Verifies websites, merges duplicates, keeps records clean", "Qualify", "users"),
    ("sales_workflow", "Outreach & follow-ups", "Drafts emails for approval; sends approved ones", "Engage", "send"),
    ("inbox_monitor", "Reply intelligence", "Reads replies, classifies intent, updates the CRM", "Engage", "chat"),
    ("marketing_manager", "Marketing planner", "Plans LinkedIn themes and campaigns", "Market", "megaphone"),
    ("linkedin_content", "Content writer", "Drafts LinkedIn posts with quality checks", "Market", "doc"),
    ("publishing", "Publishing", "Publishes approved posts", "Market", "shield"),
    ("analytics", "Analytics", "Daily and weekly sales reports", "Report", "chart"),
]
TEAM_TITLES = {agent: title for agent, title, *_ in TEAM}
WORKFLOW = [("Find", "Discover and research companies"), ("Qualify", "Score leads, keep the CRM clean"),
            ("Engage", "Outreach, follow-ups and replies"), ("Market", "LinkedIn plan, posts, publishing"),
            ("Report", "Daily and weekly reports")]
# How an agent's status reads, in the order the summary lists them.
AGENT_STATUS = {"RUNNING": "Running", "COMPLETED": "Completed", "IDLE": "Idle", "BLOCKED": "Blocked", "FAILED": "Failed",
                "PAUSED": "Paused"}


def employee_duty(settings, state: str) -> tuple[str, str, str, str]:
    """(status, tone, detail, headline) of the AI employee, from the scheduler's heartbeat and the automation state:
    working, on duty, off duty (no recent check-in) or stopped. Never claims work that is not happening."""
    now = utcnow()
    hb = read_heartbeat(settings)
    if state in ("stopped", "emergency_stop"):
        duty = ("Stopped", "bad", "automation is stopped in the Control Center", "AI is stopped")
    elif hb is None:
        duty = ("Off duty", "warn", "no scheduled run recorded for this database", "AI is off duty")
    else:
        beat = datetime.fromisoformat(hb["at"].rstrip("Z"))
        if hb.get("state") == "running":
            duty = ("Working now", "ok", "running its scheduled jobs", "AI is working for you!")
        elif now - beat <= timedelta(minutes=35):
            duty = ("On duty", "ok", f"last check-in {ago(beat, now)}; it checks in every 15 minutes", "AI is working for you!")
        else:
            duty = ("Off duty", "warn", f"last check-in {ago(beat, now)}: the PC may be asleep or the scheduler stopped",
                    "AI is off duty")
    if state == "paused":
        duty = (duty[0], "warn", duty[2] + "; sending paused", "Sending is paused")
    return duty


def ago(at: datetime | None, now: datetime) -> str:
    if at is None:
        return "never"
    minutes = int((now - at).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} days ago"


def run_summary(output: dict | None, limit: int = 6) -> str:
    """A run's result in its own words and numbers ("fetched 0 · mailbox not configured"); lists and nested data
    are left to the run's detail page."""
    parts = []
    for key, value in (output or {}).items():
        if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
            continue
        if isinstance(value, str) and len(value) > 40:
            continue
        parts.append(f"{key.replace('_', ' ')} {value}")
    return " · ".join(parts[:limit]) or "no figures recorded"


def manager_today(ctx: AppContext, s) -> dict:
    """The Sales Manager's day, read from the records: what happened today (leads newly qualified, interested
    replies, meeting requests, confirmed meetings), the hot opportunities, the exceptions that need a person with
    the reason each one does, the failures, and the automation's health. Routine work the AI employee handles on
    its own is counted, not listed. "Today" starts at local midnight."""
    st = ctx.settings
    now = utcnow()
    tz = local_tz(st.schedule.get("timezone"))
    day_start = to_naive_utc(as_aware_utc(now).astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0))
    week = now - timedelta(days=7)
    real = InboundEmail.is_simulated.is_(False)
    reachable = lambda company_id: bool(s.execute(select(Contact.id).where(  # noqa: E731
        Contact.company_id == company_id, *USABLE_EMAIL)).first())

    qualified, seen = [], set()
    for a in s.execute(select(Activity).where(Activity.type == "status_change", Activity.subject.like("%→ qualified%"),
                                              Activity.created_at >= day_start).order_by(Activity.id.desc())).scalars():
        lead = s.get(Lead, a.lead_id) if a.lead_id else None
        if lead is None or lead.id in seen or lead.status != "qualified":
            continue
        seen.add(lead.id)
        qualified.append({"lead": lead, "company": lead.company.name, "at": a.created_at, "reachable": reachable(lead.company_id)})

    hot = [{"lead": lead, "company": lead.company.name, "why": tier_reason(lead), "reachable": reachable(lead.company_id)}
           for lead in s.execute(select(Lead).where(Lead.status != "disqualified")).scalars() if lead_tier(lead) == "HOT"]
    opportunities = s.execute(select(Opportunity).where(Opportunity.stage.not_in(("won", "lost")))
                              .order_by(Opportunity.updated_at.desc()).limit(10)).scalars().all()
    interested = s.execute(select(InboundEmail).where(real, InboundEmail.category == "interested",
                                                      InboundEmail.received_at >= week)
                           .order_by(InboundEmail.received_at.desc()).limit(10)).scalars().all()
    booked = {m.lead_id for m in s.execute(select(Meeting).where(Meeting.status == "scheduled", Meeting.ends_at >= now)).scalars()}
    requests = [r for r in s.execute(select(InboundEmail).where(real, InboundEmail.intent == "meeting_request",
                                                                InboundEmail.received_at >= week)
                                     .order_by(InboundEmail.received_at.desc()).limit(10)).scalars()
                if r.lead_id not in booked]
    meetings = s.execute(select(Meeting).where(Meeting.status == "scheduled", Meeting.ends_at >= now,
                                               Meeting.is_simulated.is_(False)).order_by(Meeting.starts_at).limit(10)).scalars().all()

    # exceptions: only what a person has to decide, each with the reason
    mode = am.get_mode(s)
    needs = []
    for r in s.execute(select(InboundEmail).where(InboundEmail.status == "needs_review")
                       .order_by(InboundEmail.received_at.desc()).limit(10)).scalars():
        why = ("a pricing question: prices always need a person" if r.intent == "pricing_request"
               else "the AI is not sure what it means" if r.confidence != "high" else "the reply needs a person's answer")
        needs.append({"kind": "Reply", "text": r.subject or "(no subject)", "who": r.from_email, "why": why, "href": "/inbox"})
    for f in s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "awaiting_approval")
                       .order_by(FollowUp.due_at).limit(15)).scalars():
        if st.require_human_approval:
            why = "every email needs a person while REQUIRE_HUMAN_APPROVAL=true"
        elif mode == "approval":
            why = "Approval mode: every email needs a person"
        else:
            covered, why = am.campaign_for(s, f, mode, now)
            if covered is not None:
                continue  # it goes out at send time under its authorization: routine work, not an exception
        needs.append({"kind": "Email", "text": f.subject, "who": am.recipient(f), "why": why, "href": f"/followups#f{f.id}"})
    for f in s.execute(select(FollowUp).where(FollowUp.status == "uncertain")).scalars():
        needs.append({"kind": "Email outcome", "text": f.subject, "who": am.recipient(f),
                      "why": "the mail server did not confirm delivery: check the Sent folder", "href": "/followups?show=all"})
    for p in s.execute(select(ContentItem).where(ContentItem.status == "uncertain")).scalars():
        needs.append({"kind": "Post outcome", "text": p.title, "who": "LinkedIn", "why": "LinkedIn did not confirm the post",
                      "href": f"/content/{p.id}"})
    posts_waiting = s.execute(select(func.count(ContentItem.id)).where(ContentItem.status == "awaiting_approval")).scalar_one()
    held = s.execute(select(func.count(FollowUp.id)).where(FollowUp.channel == "email", FollowUp.status == "approved",
                                                           FollowUp.sent_at.is_(None))).scalar_one()

    employee = ai_employee(ctx, s)
    ready, reason = EmailSender(mail_accounts.sending_settings(s, st)).status()
    counts = {
        "discovered": s.execute(select(func.count(Company.id)).where(Company.created_at >= day_start, Company.is_sample.is_(False),
                                                                     Company.merged_into_id.is_(None))).scalar_one(),
        "verified_contacts": s.execute(select(func.count(Contact.id)).where(*USABLE_EMAIL)).scalar_one(),
        "outreach_ready": s.execute(select(func.count(func.distinct(Lead.id))).join(Contact, Contact.company_id == Lead.company_id)
                                    .where(Lead.status == "qualified", *USABLE_EMAIL)).scalar_one(),
        "followups": s.execute(select(func.count(FollowUp.id)).where(
            FollowUp.status.in_(("pending", "awaiting_approval", "approved")), FollowUp.subject.not_like("Review %"),
            ~((FollowUp.channel == "email") & (FollowUp.kind == "outreach")))).scalar_one(),
    }
    failures = employee["employee"]["live"]["problems"] + [
        {"what": f"{a.level} alert", "why": a.message[:160], "at": a.created_at, "href": "/system#alerts"}
        for a in s.execute(select(Alert).where(Alert.acknowledged.is_(False), Alert.level.in_(("error", "warning")))
                           .order_by(Alert.id.desc()).limit(6)).scalars()]
    return dict(
        day_label=f"{as_aware_utc(now).astimezone(tz):%A %d %B %Y}", qualified=qualified, hot=hot, opportunities=opportunities,
        interested=interested, requests=requests, meetings=meetings, needs=needs, posts_waiting=posts_waiting,
        held=held, failures=failures, employee=employee["employee"], team_counts=employee["team_counts"], counts=counts,
        board=system_status(ctx, s, employee["team"], (ready, reason), now), scheduler=scheduler_times(ctx, s, now),
        health={"mode": mode, "state": am.get_state(s), "sending": st.outbound_email_enabled,
                "reading": mail_accounts.reading_status(s, st)[0],
                "publishing": st.publishing_enabled, "blockers": am.blockers(s, st, ready, reason),
                "authorizations": len(am.active_authorizations(s, now))},
    )


# How each status on the Manager Today board reads (pill colour), in the words the Sales Manager sees.
BOARD_TONE = {"ACTIVE": "ok", "PAUSED": "warn", "BLOCKED": "warn", "FAILED": "bad", "WAITING FOR AUTHORIZATION": "",
              "NOT CONFIGURED": "off"}


def scheduler_times(ctx: AppContext, s, now: datetime) -> dict:
    """The last check-in, the last successful scheduled job and the next expected check-in, from the records."""
    beat = read_heartbeat(ctx.settings)
    at = datetime.fromisoformat(beat["at"].rstrip("Z")) if beat else None
    tick = int(ctx.settings.schedule.get("tick_minutes", 15))
    last_ok = s.execute(select(JobRun).where(JobRun.trigger == "scheduler", JobRun.status == "success")
                        .order_by(JobRun.id.desc()).limit(1)).scalars().first()
    expected = at + timedelta(minutes=tick) if at else None
    return {"last_checkin": at, "session": (beat or {}).get("session"),
            "last_success": (last_ok.finished_at or last_ok.started_at) if last_ok else None,
            "last_success_job": last_ok.job.replace("_", " ") if last_ok else None,
            "next_checkin": expected if expected and expected > now else None,
            "overdue": bool(expected and now - expected > timedelta(minutes=tick + 5))}


def system_status(ctx: AppContext, s, team: list[dict], sender: tuple[bool, str], now: datetime) -> list[dict]:
    """One status per part of the automation, from the records and settings: ACTIVE, PAUSED, BLOCKED, FAILED,
    WAITING FOR AUTHORIZATION (built, switched off until the owner approves) or NOT CONFIGURED. Never assumed."""
    st = ctx.settings
    state = am.get_state(s)
    stopped = state in ("stopped", "emergency_stop")
    beat = read_heartbeat(st)
    tick = int(st.schedule.get("tick_minutes", 15))
    rows: list[dict] = []

    def add(name: str, status: str, detail: str, href: str) -> None:
        rows.append({"name": name, "status": status, "tone": BOARD_TONE[status], "detail": detail, "href": href})

    at = datetime.fromisoformat(beat["at"].rstrip("Z")) if beat else None
    if beat is None:
        add("Scheduled jobs", "NOT CONFIGURED", "no scheduled run recorded for this database", "/system")
    elif beat.get("state") == "maintenance":
        add("Scheduled jobs", "PAUSED", "maintenance window: scheduled runs wait until it ends", "/system")
    elif stopped:
        add("Scheduled jobs", "PAUSED", "stopped in Automation Control: jobs wait until an administrator starts them",
            "/automation")
    elif now - at <= timedelta(minutes=2 * tick + 5):
        add("Scheduled jobs", "ACTIVE", f"last check-in {ago(at, now)}; it checks in every {tick} minutes", "/jobs")
    else:
        add("Scheduled jobs", "FAILED", f"last check-in {ago(at, now)}: the PC may be asleep or off, or the task stopped",
            "/system")

    background = (beat or {}).get("last_background_at")
    if beat and beat.get("session") == "background":
        add("Running while signed out", "ACTIVE", "the last check-in ran in the background session, so work continues "
            "while you are signed out of Windows", "/system")
    elif background:
        add("Running while signed out", "BLOCKED", f"the last background run was {ago(datetime.fromisoformat(background.rstrip('Z')), now)}; "
            "recent runs happened only while you were signed in", "/system")
    else:
        add("Running while signed out", "WAITING FOR AUTHORIZATION", "jobs run only while you are signed in to Windows; "
            "running while signed out needs your approval (A1)", "/system")

    failed = [a["title"] for a in team if a["status"] == "FAILED"]
    blocked = sum(1 for a in team if a["status"] == "BLOCKED")
    if failed:
        add("AI agents", "FAILED", f"{len(failed)} failed: {', '.join(failed)}", "/command-center")
    elif stopped:
        add("AI agents", "PAUSED", "automation is stopped in Automation Control", "/automation")
    else:
        done = sum(1 for a in team if a["status"] in ("COMPLETED", "RUNNING"))
        add("AI agents", "ACTIVE", f"{done} of {len(team)} ran in the last 24 hours"
            + (f"; {blocked} waiting on a switch" if blocked else ""), "/command-center")

    ready, reason = sender
    if not st.outbound_email_enabled:
        add("Email sending", "WAITING FOR AUTHORIZATION", "OUTBOUND_EMAIL_ENABLED=false: emails are drafted, never sent; "
            "sending needs your approval (A3)", "/automation")
    elif not ready:
        add("Email sending", "BLOCKED", reason, "/integrations")
    elif stopped or state == "paused":
        add("Email sending", "PAUSED", "automation is paused or stopped: nothing is sent", "/automation")
    else:
        add("Email sending", "ACTIVE", "approved emails go out within the limits"
            + ("; every email needs a person" if st.require_human_approval else ""), "/followups")

    inbox = s.execute(select(JobState).where(JobState.job == "inbox")).scalars().first()
    if not (st.imap_configured or mail_accounts.readable(s)):
        add("Mailbox reading", "NOT CONFIGURED", "no mailbox is set up, so replies are not read; reading needs your "
            "approval (A2)", "/integrations")
    elif inbox and inbox.consecutive_failures:
        add("Mailbox reading", "FAILED", f"the last {inbox.consecutive_failures} mailbox check(s) failed", "/jobs")
    else:
        add("Mailbox reading", "ACTIVE", "replies are read (read-only) and classified", "/inbox")

    from aime import booking
    ok, why = booking.ready(s, st)
    link = "emails offer your booking link; " if st.calendly_url else ""
    provider = (st.calendar_provider or "").lower()
    linked = provider in oauth.PROVIDERS and oauth.connected(s, provider, "calendar") is not None
    if ok:
        add("Meeting booking", "ACTIVE", "free times are proposed from your calendar; a person approves each email "
            "and books each slot", "/meetings")
    elif not provider or not linked:
        add("Meeting booking", "NOT CONFIGURED", f"{link}"
            + ("no calendar is set (CALENDAR_PROVIDER)" if not provider else f"the {provider} calendar is not connected")
            + ", so nothing is booked automatically (A4)", "/meetings")
    elif not st.meeting_booking_enabled:
        add("Meeting booking", "WAITING FOR AUTHORIZATION", f"{link}calendar connected; MEETING_BOOKING_ENABLED=false "
            "(A4)", "/meetings")
    else:
        add("Meeting booking", "PAUSED", why, "/automation")

    if not st.publishing_enabled:
        add("LinkedIn publishing", "WAITING FOR AUTHORIZATION", "PUBLISHING_ENABLED=false: posts are drafted for "
            "approval and never published", "/content")
    elif stopped or state == "paused":
        add("LinkedIn publishing", "PAUSED", "automation is paused or stopped: nothing is published", "/automation")
    else:
        add("LinkedIn publishing", "ACTIVE", "approved posts are published at their time", "/content")

    last = (kv_get(s, "backup.last_verified") or {}).get("at")
    if not last:
        add("Backups", "NOT CONFIGURED", "no verified backup recorded yet", "/system")
    else:
        when = datetime.fromisoformat(str(last).rstrip("Z"))
        add("Backups", "ACTIVE" if now - when <= timedelta(hours=26) else "FAILED",
            f"last verified backup {ago(when, now)}", "/system")
    return rows


def job_schedule(ctx: AppContext, s, now: datetime) -> list[dict]:
    """Every scheduled job with its schedule, last result and next run, read from the job state with the scheduler's
    own rules (aime.scheduler.runner.next_run); a next run at or before now means "at the next check-in"."""
    tz = local_tz(ctx.settings.schedule.get("timezone"))
    states = {row.job: row for row in s.execute(select(JobState)).scalars()}
    rows = []
    for spec in load_job_specs(ctx.settings):
        state = states.get(spec.name)
        rows.append({"job": spec.name, "label": spec.schedule_label, "description": spec.description.split(":")[0],
                     "next": next_run(spec, state, now, tz), "last_status": state.last_status if state else None,
                     "last_success": state.last_success_at if state else None})
    return rows


def running_agent_runs(s, now: datetime) -> list[AgentRun]:
    """Agent runs that are really running: still open, started inside the scheduler's abandon window and, when a job
    started them, belonging to a job run that is itself still open. A finished job's leftover row never counts."""
    rows = s.execute(select(AgentRun).where(AgentRun.status == "running", AgentRun.started_at >= now - ABANDON_AFTER)
                     .order_by(AgentRun.id)).scalars().all()
    open_jobs = set(s.execute(select(JobRun.id).where(JobRun.status == "running")).scalars())
    return [r for r in rows if (r.job_run_id in open_jobs if r.job_run_id else r.started_at >= now - timedelta(minutes=30))]


def ai_employee(ctx: AppContext, s) -> dict:
    """The AI Sales Employee as one colleague, all read from the records and run logs: on duty or not (scheduler
    heartbeat and automation state), its live workspace (the task running now, the latest completed task, the last
    successful run and action, the next scheduled task and check-in, approvals waiting, failed and blocked work),
    what it did in the last 24 hours, and each agent of its team in workflow order with one status: running (only
    while its job is still open), completed (a successful run in the last 24 hours), idle, blocked by a switch,
    failed or paused. A past completion is never shown as running, and nothing is invented."""
    st = ctx.settings
    now = utcnow()
    day, week = now - timedelta(hours=24), now - timedelta(days=7)
    cnt = lambda m, *c: s.execute(select(func.count(m.id)).where(*c)).scalar_one()  # noqa: E731
    state = am.get_state(s)
    duty = employee_duty(st, state)
    did = [
        ("companies researched", cnt(Company, Company.researched_at >= day), "/companies"),
        ("leads scored", cnt(Lead, Lead.last_scored_at >= day), "/leads"),
        ("emails drafted", cnt(FollowUp, FollowUp.channel == "email", FollowUp.created_at >= day), "/followups"),
        ("emails sent", cnt(FollowUp, FollowUp.channel == "email", FollowUp.sent_at >= day), "/followups?show=all"),
        ("replies read", cnt(InboundEmail, InboundEmail.processed_at >= day, InboundEmail.is_simulated.is_(False)), "/inbox?show=all"),
        ("bookings received", cnt(Meeting, Meeting.created_at >= day, Meeting.is_simulated.is_(False)), "/meetings"),
    ]
    waiting = [
        ("emails to approve", cnt(FollowUp, FollowUp.channel == "email", FollowUp.status == "awaiting_approval"), "/approvals"),
        ("posts to approve", cnt(ContentItem, ContentItem.status == "awaiting_approval"), "/approvals"),
        ("replies to review", cnt(InboundEmail, InboundEmail.status == "needs_review"), "/inbox"),
        ("meetings in 24 h", cnt(Meeting, Meeting.status == "scheduled", Meeting.starts_at >= now,
                                 Meeting.starts_at <= now + timedelta(hours=24)), "/meetings"),
    ]
    schedule = job_schedule(ctx, s, now)
    by_job = {row["job"]: row for row in schedule}
    agent_jobs: dict[str, set[str]] = defaultdict(set)
    for agent, job in s.execute(select(AgentRun.agent, AgentRun.job).where(
            AgentRun.started_at >= now - timedelta(days=14), AgentRun.job.is_not(None)).distinct()).all():
        if job in by_job:
            agent_jobs[agent].add(job)
    running = {r.agent: r for r in running_agent_runs(s, now)}
    week_counts: dict[str, Counter] = defaultdict(Counter)
    for agent, status, n in s.execute(select(AgentRun.agent, AgentRun.status, func.count(AgentRun.id)).where(
            AgentRun.started_at >= week, AgentRun.status != "running").group_by(AgentRun.agent, AgentRun.status)).all():
        week_counts[agent][status] = n
    failures_by_job = {row.job: row.consecutive_failures or 0 for row in s.execute(select(JobState)).scalars()}
    blocked_by = {"publishing": None if st.publishing_enabled else "publishing is off (PUBLISHING_ENABLED is false)",
                  "inbox_monitor": None if st.imap_configured or mail_accounts.readable(s)
                  else "mailbox reading is off (mailbox not set up)"}
    team = []
    for agent, title, job, stage, ico in TEAM:
        last = s.execute(select(AgentRun).where(AgentRun.agent == agent, AgentRun.status != "running")
                         .order_by(AgentRun.id.desc()).limit(1)).scalars().first()
        ended = (last.finished_at or last.started_at) if last else None
        last_ok = s.execute(select(func.max(AgentRun.finished_at)).where(AgentRun.agent == agent,
                                                                         AgentRun.status == "success")).scalar_one()
        now_run = running.get(agent)
        nexts = [by_job[j]["next"] for j in agent_jobs[agent] if by_job[j]["next"]]
        blocked = blocked_by.get(agent)
        if blocked:
            status, note = "BLOCKED", blocked
        elif state in ("stopped", "emergency_stop"):
            status, note = "PAUSED", "automation is stopped"
        elif now_run:
            status, note = "RUNNING", f"running the {now_run.job or 'manual'} job since {ago(now_run.started_at, now)}"
        elif last is None:
            status, note = "IDLE", "has not run on this database yet"
        elif last.status in ("failed", "abandoned"):
            status, note = "FAILED", (last.error or f"last run {last.status}")[:120]
        elif last.status == "success" and ended >= day:
            status, note = "COMPLETED", f"last run completed {ago(ended, now)}"
        else:
            status, note = "IDLE", f"last ran {ago(ended, now)}"
        if agent == "sales_workflow" and status in ("COMPLETED", "IDLE") and not st.outbound_email_enabled:
            note += "; drafting only, sending off"
        team.append({"agent": agent, "title": title, "job": job, "stage": stage, "icon": ico, "status": status,
                     "label": AGENT_STATUS[status], "note": note, "blocked": blocked,
                     "current": {"job": now_run.job or "manual", "since": now_run.started_at} if now_run else None,
                     "last_run": ended, "last_result": last.status if last else None, "last_success": last_ok,
                     "ok_7d": week_counts[agent]["success"],
                     "failed_7d": week_counts[agent]["failed"] + week_counts[agent]["abandoned"],
                     "next_at": min(nexts) if nexts else None, "jobs": sorted(agent_jobs[agent]),
                     "retries": max((failures_by_job.get(j, 0) for j in agent_jobs[agent]), default=0)})
    counts = Counter(a["status"] for a in team)

    # the live workspace
    title_of = lambda agent: TEAM_TITLES.get(agent, agent.replace("_", " ").capitalize())  # noqa: E731
    last_job = s.execute(select(JobRun).where(JobRun.status != "running").order_by(JobRun.id.desc()).limit(1)).scalars().first()
    last_ok = s.execute(select(AgentRun).where(AgentRun.status == "success").order_by(AgentRun.id.desc()).limit(1)).scalars().first()
    last_action = s.execute(select(Activity).where(Activity.actor.in_(list(TEAM_TITLES)))
                            .order_by(Activity.id.desc()).limit(1)).scalars().first()
    upcoming = sorted((row for row in schedule if row["next"]), key=lambda row: row["next"])
    beat = read_heartbeat(st)
    next_checkin = None
    if beat and duty[0] in ("On duty", "Working now"):
        next_checkin = datetime.fromisoformat(beat["at"].rstrip("Z")) + timedelta(minutes=int(st.schedule.get("tick_minutes", 15)))
        next_checkin = next_checkin if next_checkin > now else None
    problems = [{"what": f"{r.job.replace('_', ' ')} job {r.status}", "why": (r.error or "no error recorded")[:160],
                 "at": r.finished_at or r.started_at, "href": f"/jobs/runs/{r.id}"}
                for r in s.execute(select(JobRun).where(JobRun.status.in_(("failed", "partial", "abandoned")),
                                                        JobRun.started_at >= day).order_by(JobRun.id.desc()).limit(5)).scalars()]
    problems += [{"what": f"{title_of(r.agent)} run {r.status}", "why": (r.error or "no error recorded")[:160],
                  "at": r.finished_at or r.started_at, "href": f"/agents/runs/{r.id}"}
                 for r in s.execute(select(AgentRun).where(AgentRun.status.in_(("failed", "abandoned")),
                                                           AgentRun.started_at >= day).order_by(AgentRun.id.desc()).limit(5)).scalars()]
    blocked_list = [{"what": f"{a['title']} blocked", "why": a["blocked"]} for a in team if a["blocked"]]
    live = {
        "running": [{"agent": title_of(r.agent), "job": (r.job or "manual").replace("_", " "), "since": r.started_at,
                     "ago": ago(r.started_at, now)} for r in running.values()],
        "last_job": {"job": last_job.job.replace("_", " "), "status": last_job.status, "ago": ago(last_job.finished_at or last_job.started_at, now),
                     "at": last_job.finished_at or last_job.started_at,
                     "took": f"{last_job.duration_ms / 1000:.1f} s" if last_job.duration_ms else ""} if last_job else None,
        "last_ok": {"agent": title_of(last_ok.agent), "job": (last_ok.job or "manual").replace("_", " "),
                    "ago": ago(last_ok.finished_at or last_ok.started_at, now), "at": last_ok.finished_at or last_ok.started_at,
                    "took": f"{last_ok.duration_ms / 1000:.1f} s" if last_ok.duration_ms else ""} if last_ok else None,
        "last_action": {"text": last_action.subject, "agent": title_of(last_action.actor), "ago": ago(last_action.created_at, now),
                        "href": f"/leads/{last_action.lead_id}" if last_action.lead_id else None} if last_action else None,
        "next_task": ({"job": upcoming[0]["job"].replace("_", " "), "description": upcoming[0]["description"],
                       "at": upcoming[0]["next"], "due": upcoming[0]["next"] <= now} if upcoming else None),
        "next_checkin": next_checkin,
        "approvals": sum(n for label, n, _ in waiting if "approve" in label or "review" in label),
        "problems": problems, "blocked": blocked_list,
        "jobs_24h": cnt(JobRun, JobRun.trigger == "scheduler", JobRun.started_at >= day),
        "failed_24h": cnt(JobRun, JobRun.status.in_(("failed", "partial", "abandoned")), JobRun.started_at >= day),
    }
    return {"employee": {"duty": duty, "did": did, "waiting": waiting, "mode": am.get_mode(s), "state": state, "live": live},
            "team": team, "team_counts": [(AGENT_STATUS[k], k.lower(), counts[k]) for k in AGENT_STATUS if counts[k]],
            "workflow": [(stage, note, [a for a in team if a["stage"] == stage]) for stage, note in WORKFLOW],
            "schedule": schedule}


# How each kind of activity shows on the dashboard timeline: icon, tone, status word.
ACTIVITY_LOOK = {
    "reply": ("chat", "purple", "Analyzed"), "meeting": ("calendar-check", "blue", "Confirmed"),
    "email_draft": ("mail", "red", "Drafted"), "outreach": ("send", "blue", "Updated"), "status": ("kanban", "green", "Updated"),
    "research": ("building", "blue", "Researched"), "note": ("doc", "slate", "Noted"), "opportunity": ("star", "orange", "Opened"),
    "override": ("alert", "orange", "Override"),
}


def change(current: int, previous: int) -> tuple[str, str]:
    """('+12%', 'up') for the change against the previous period. With fewer than 5 in the previous period a
    percentage would mislead (1 to 3 is "+200%"), so the plain difference is shown instead; a dash when there is
    nothing in either period."""
    if previous == 0:
        return ("new" if current else "—"), ("up" if current else "flat")
    trend = "up" if current > previous else "down" if current < previous else "flat"
    if previous < 5:
        diff = current - previous
        return (f"{'+' if diff > 0 else ''}{diff}" if diff else "no change"), trend
    pct = round(100 * (current - previous) / previous)
    return (f"{'+' if pct > 0 else ''}{pct}%", trend)


def dashboard_view(ctx: AppContext, s, days: int) -> dict:
    """The executive dashboard in the reference layout, all from records: KPI cards with their change against the
    previous period and a daily trend line, the leads badge, the live activity feed, the sales-performance chart,
    the pipeline by stage, upcoming meetings and one insight. Nothing is estimated or invented."""
    from aime.web import charts

    st = ctx.settings
    now = utcnow()
    start, prev = now - timedelta(days=days), now - timedelta(days=2 * days)
    cnt = lambda m, *c: s.execute(select(func.count(m.id)).where(*c)).scalar_one()  # noqa: E731

    def window(column, *where) -> tuple[int, int, list[int]]:
        dates = s.execute(select(column).where(column >= prev, *where)).scalars().all()
        current = sum(1 for d in dates if d >= start)
        return current, len(dates) - current, charts.daily_counts(dates, days)

    open_co = (Company.merged_into_id.is_(None),)
    cards = []
    for label, total, (cur, before, daily), icon, tone, href in (
            ("Companies discovered", cnt(Company, *open_co), window(Company.created_at, *open_co), "building", "blue", "/companies"),
            ("Verified contacts", cnt(Contact, *USABLE_EMAIL), window(Contact.discovered_at, *USABLE_EMAIL), "users", "purple",
             "/contacts?reachable=1"),
            ("Emails sent", cnt(FollowUp, FollowUp.channel == "email", FollowUp.status == "sent"),
             window(FollowUp.sent_at, FollowUp.status == "sent"), "mail", "green", "/followups?show=all"),
            ("Meetings booked", cnt(Meeting, Meeting.is_simulated.is_(False), Meeting.status != "cancelled"),
             window(Meeting.created_at, Meeting.is_simulated.is_(False)), "calendar", "orange", "/meetings")):
        text, trend = change(cur, before)
        cards.append({"label": label, "value": total, "change": text, "trend": trend, "icon": icon, "tone": tone,
                      "href": href, "period": cur, "spark": charts.sparkline(daily, tone, f"{label} per day")})
    leads_now, leads_before, _ = window(Lead.created_at)
    lead_change, lead_trend = change(leads_now, leads_before)

    names = dict(s.execute(select(Company.id, Company.name)).all())
    feed = []
    for a in s.execute(select(Activity).order_by(Activity.id.desc()).limit(8)).scalars():
        icon, tone, word = ACTIVITY_LOOK.get(a.type, ("sparkle", "slate", "Logged"))
        if a.type == "meeting" and "cancel" in a.subject.lower():
            word = "Cancelled"
        feed.append({"at": a.created_at, "icon": icon, "tone": tone, "status": word, "title": a.subject,
                     "sub": " · ".join(x for x in (names.get(a.company_id), f"by {a.actor}") if x),
                     "href": f"/leads/{a.lead_id}" if a.lead_id else None})
    for r in s.execute(select(AgentRun).where(AgentRun.status != "running").order_by(AgentRun.id.desc()).limit(4)).scalars():
        feed.append({"at": r.finished_at or r.started_at, "icon": "cpu", "tone": "green" if r.status == "success" else "red",
                     "status": "Completed" if r.status == "success" else "Failed",
                     "title": f"{TEAM_TITLES.get(r.agent, r.agent.replace('_', ' ').capitalize())} run",
                     "sub": f"{r.duration_ms / 1000:.1f} s" if r.duration_ms else (r.job or ""), "href": f"/agents/runs/{r.id}"})
    feed.sort(key=lambda row: row["at"] or datetime.min, reverse=True)
    for row in feed:
        row["ago"] = ago(row["at"], now)

    # Sales performance: ten real series in three views, per day over the chosen period
    day_labels = [(now.date() - timedelta(days=days - 1 - i)).strftime("%a" if days <= 7 else "%b %d") for i in range(days)]

    def per_day(column, *where):
        return charts.daily_counts(s.execute(select(column).where(column >= now - timedelta(days=days), *where))
                                   .scalars().all(), days)

    real_replies = (InboundEmail.is_simulated.is_(False),)
    # follow-up work a person or the pipeline completed (rebook, record the outcome, chase a stale lead); a
    # "Review <company>" research task closes on its own when the first email is drafted, so it is not counted
    tasks_done = per_day(FollowUp.updated_at, FollowUp.channel == "task", FollowUp.status == "done",
                         FollowUp.subject.not_like("Review %"))
    followups_sent = per_day(FollowUp.sent_at, FollowUp.channel == "email", FollowUp.status == "sent", FollowUp.step > 1)
    span = f"the last {days} days"
    views = [
        ("prospecting", "Prospecting",
         [("Companies discovered", per_day(Company.created_at, Company.merged_into_id.is_(None))),
          ("Leads generated", per_day(Lead.created_at))],
         [("Verified contacts", per_day(Contact.discovered_at, *USABLE_EMAIL))],
         f"No companies, leads or verified contacts recorded in {span}."),
        ("outreach", "Outreach",
         [("Outreach drafts created", per_day(FollowUp.created_at, FollowUp.kind == "outreach")),
          ("Emails sent", per_day(FollowUp.sent_at, FollowUp.channel == "email", FollowUp.status == "sent"))],
         [("Follow-ups completed", [a + b for a, b in zip(tasks_done, followups_sent, strict=True)])],
         f"No outreach activity recorded in {span}."),
        ("responses", "Responses",
         [("Replies received", per_day(InboundEmail.received_at, *real_replies, InboundEmail.category.in_(HUMAN_REPLIES))),
          ("Positive replies", per_day(InboundEmail.received_at, *real_replies, InboundEmail.category == "interested"))],
         [("Meetings booked", per_day(Meeting.created_at, Meeting.is_simulated.is_(False), Meeting.status != "cancelled")),
          ("Qualified opportunities", per_day(Opportunity.created_at))],
         f"No replies, meetings or opportunities recorded in {span}."),
    ]
    performance = [{"key": key, "label": label,
                    "series": [(name, sum(v), f"s{i}") for i, (name, v) in enumerate(bars)]
                    + [(name, sum(v), f"l{i}") for i, (name, v) in enumerate(lines)],
                    "chart": charts.combo_chart(day_labels, bars, lines, f"{label}, {span}", empty, height=300,
                                                uid=f"perf-{key}")}
                   for key, label, bars, lines, empty in views]

    contacted = s.execute(select(func.count(func.distinct(FollowUp.lead_id))).where(
        FollowUp.kind == "outreach", FollowUp.status == "sent")).scalar_one()
    stages_raw = [("New leads", cnt(Lead), "users", "blue", "/leads"),
                  ("Contacted", contacted, "mail", "purple", "/followups?show=all"),
                  ("Interested", cnt(Lead, Lead.outreach_status.in_(("interested", "meeting"))), "star", "green", "/pipeline"),
                  ("Meeting booked", cnt(Meeting, Meeting.is_simulated.is_(False), Meeting.status != "cancelled"),
                   "calendar", "orange", "/meetings"),
                  ("Closed won", cnt(Opportunity, Opportunity.stage == "won"), "trophy", "green", "/pipeline")]
    stages = []
    for i, (label, n, icon, tone, href) in enumerate(stages_raw):
        before = stages_raw[i - 1][1] if i else None
        stages.append({"label": label, "value": n, "icon": icon, "tone": tone, "href": href,
                       "rate": (f"{100 * n / before:.0f}% conversion" if before else ("" if i else "all leads"))})

    meetings = []
    for m in s.execute(select(Meeting).where(Meeting.status == "scheduled", Meeting.ends_at >= now,
                                             Meeting.is_simulated.is_(False)).order_by(Meeting.starts_at).limit(4)).scalars():
        local = m.starts_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
        today_et = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York")).date()
        day = "Today" if local.date() == today_et else "Tomorrow" if local.date() == today_et + timedelta(days=1) else f"{local:%b %d}"
        meetings.append({"day": day, "time": f"{local.hour % 12 or 12}:{local:%M %p} ET",
                         "who": (m.lead.company.name if m.lead else None) or m.invitee_name or m.invitee_email,
                         "title": m.title, "href": f"/leads/{m.lead_id}" if m.lead_id else "/meetings"})

    week = now - timedelta(days=7)
    approvals = cnt(FollowUp, FollowUp.channel == "email", FollowUp.status == "awaiting_approval")
    interested_week = cnt(InboundEmail, InboundEmail.category == "interested", InboundEmail.is_simulated.is_(False),
                          InboundEmail.received_at >= week)
    booked_week = cnt(Meeting, Meeting.is_simulated.is_(False), Meeting.status != "cancelled", Meeting.created_at >= week)
    booked_before = cnt(Meeting, Meeting.is_simulated.is_(False), Meeting.status != "cancelled",
                        Meeting.created_at >= week - timedelta(days=7), Meeting.created_at < week)
    if booked_week and booked_week > booked_before:
        insight = ("You're on a roll!", f"Your AI employee recorded {booked_week} meeting booking{'s' if booked_week != 1 else ''} "
                   f"this week, up from {booked_before} the week before.")
    elif interested_week:
        insight = ("Interest is coming in", f"{interested_week} interested repl{'ies' if interested_week != 1 else 'y'} this week. "
                   "Answer them quickly to turn them into meetings.")
    elif approvals:
        insight = ("Drafts are waiting", f"{approvals} email{'s' if approvals != 1 else ''} wait for your approval before "
                   "anything goes out.")
    else:
        insight = ("All quiet", "No replies or bookings this week yet. Research and drafting carry on in the background.")

    local_hour = datetime.now().hour
    return dict(
        greeting="Good morning" if local_hour < 12 else "Good afternoon" if local_hour < 17 else "Good evening",
        period_days=days, period_label=f"{start:%b %d} – {now:%b %d, %Y}", cards=cards,
        leads_badge={"change": lead_change, "trend": lead_trend, "count": leads_now},
        feed=feed[:6], performance=performance, pipeline_stages=stages, upcoming_list=meetings, insight=insight,
        calendly_url=st.calendly_url,
    )
