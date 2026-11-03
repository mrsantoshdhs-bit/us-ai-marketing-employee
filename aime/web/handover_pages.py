"""Handovers to the Sales Manager (aime.handover_package): the package on the deal's page with Accept and Return, and the
/handovers list.

Who may do what:
- the package on a deal's page: whoever may open the deal (aime.visibility, checked by the sign-in middleware);
- Accept and Return: the deal's owner, or an administrator (admin_ops or configure_system); `edit_records` as for any change;
- /handovers: a Sales Manager and every role above (as /sales-manager), listing only the deals they may see;
- the notification email's settings and authorization (on /handovers): Super Admin only (configure_system), audited.
No JavaScript (the dashboard's CSP is script-src 'none'): plain forms and <details>."""

from __future__ import annotations

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from aime import handover_notify as hn
from aime import handover_package as hp
from aime.db import take_write_lock
from aime.models import HandoverPackage, Opportunity
from aime.timeutil import utcnow
from aime.visibility import opportunity_condition
from aime.web.manager_pages import may_view, super_admin

STATUSES = ("open", "accepted", "returned", "all")


def deal_view(s, settings, opp: Opportunity, user: dict | None) -> dict:
    """What the deal's page shows about its handover."""
    pkg = hp.latest(s, opp.id)
    if pkg is None:
        return {"handover": None}
    older = s.execute(select(HandoverPackage).where(HandoverPackage.opportunity_id == opp.id, HandoverPackage.id != pkg.id)
                      .order_by(HandoverPackage.version.desc()).limit(10)).scalars().all()
    blockers = hn.blockers(s, settings, pkg, utcnow()) if pkg.status == "open" and pkg.notify_status is None else []
    return {"handover": pkg, "handover_older": older, "handover_may_act": hp.may_act(opp, user, settings),
            "handover_paused": bool(opp.lead_id and hp.ai_paused(s, opp.lead_id)), "handover_blockers": blockers,
            "cite_label": hp.cite_label}


def register(app, C, render, back, who) -> None:
    def user_of(request: Request):
        return getattr(request.state, "user", None)

    def need_manager(request: Request) -> None:
        if not may_view(C().settings, user_of(request)):
            raise HTTPException(403, "A Sales Manager or above opens the handovers")

    def need_super_admin(request: Request) -> None:
        if not super_admin(C().settings, user_of(request)):
            raise HTTPException(403, "Super Admin only")

    def _package(s, opp_id: int, pkg_id: int) -> tuple[Opportunity, HandoverPackage]:
        opp = s.get(Opportunity, opp_id)
        pkg = s.get(HandoverPackage, pkg_id)
        if opp is None or pkg is None or pkg.opportunity_id != opp.id:
            raise HTTPException(404)
        return opp, pkg

    @app.post("/opportunities/{opp_id}/handover/accept")
    def handover_accept(request: Request, opp_id: int, package_id: int = Form(...)):
        ctx = C()
        url = f"/opportunities/{opp_id}#handover"
        with ctx.db.session() as s:
            take_write_lock(s)
            opp, pkg = _package(s, opp_id, package_id)
            if not hp.may_act(opp, user_of(request), ctx.settings):
                raise HTTPException(403, "Only the deal's owner or an administrator accepts its handover")
            try:
                note = hp.accept(s, ctx.settings, pkg, who(request))
            except ValueError as exc:
                return back(url, f"Not accepted: {exc}")
        return back(url, note)

    @app.post("/opportunities/{opp_id}/handover/return")
    def handover_return(request: Request, opp_id: int, package_id: int = Form(...), reason: str = Form("")):
        ctx = C()
        url = f"/opportunities/{opp_id}#handover"
        with ctx.db.session() as s:
            take_write_lock(s)
            opp, pkg = _package(s, opp_id, package_id)
            if not hp.may_act(opp, user_of(request), ctx.settings):
                raise HTTPException(403, "Only the deal's owner or an administrator returns its handover")
            try:
                note = hp.return_to_ai(s, ctx.settings, pkg, who(request), reason)
            except ValueError as exc:
                return back(url, f"Not returned: {exc}")
        return back(url, note)

    @app.get("/handovers", response_class=HTMLResponse)
    def handovers_page(request: Request, status: str = "open"):
        need_manager(request)
        status = status if status in STATUSES else "open"
        ctx = C()
        sc = getattr(request.state, "scope", None)
        with ctx.db.session() as s:
            newest = select(HandoverPackage.opportunity_id, func.max(HandoverPackage.version).label("v")) \
                .group_by(HandoverPackage.opportunity_id).subquery()
            q = (select(HandoverPackage, Opportunity).join(newest, (newest.c.opportunity_id == HandoverPackage.opportunity_id)
                                                           & (newest.c.v == HandoverPackage.version))
                 .join(Opportunity, Opportunity.id == HandoverPackage.opportunity_id))
            if sc is not None:
                q = q.where(opportunity_condition(sc))
            if status != "all":
                q = q.where(HandoverPackage.status == status)
            rows = s.execute(q.order_by(HandoverPackage.waiting_since.desc()).limit(200)).all()
            view = {"rows": [(p, o) for p, o in rows], "status": status, "statuses": STATUSES,
                    "rules": hp.rules(ctx.settings)}
            if super_admin(ctx.settings, user_of(request)):
                cfg = hn.config(s)
                view["notify"] = cfg
                view["notify_auth"] = hn.authorization(cfg)
                view["notify_lines"] = "\n".join(f"{u} = {a}" for u, a in cfg["addresses"].items())
        return render(request, "handovers.html", **view)

    @app.post("/handovers/notify")
    def handovers_notify_save(request: Request, addresses: str = Form(""), allowlist: str = Form(""),
                              email_enabled: str = Form("")):
        need_super_admin(request)
        ctx = C()
        try:
            with ctx.db.session() as s:
                take_write_lock(s)
                changes = hn.save(s, ctx.settings, who(request), {"addresses": addresses, "allowlist": allowlist,
                                                                   "email_enabled": email_enabled == "yes"})
        except ValueError as exc:
            return back("/handovers", f"Not saved: {exc}")
        return back("/handovers", ("Saved: " + "; ".join(changes)) if changes else "Nothing changed")

    @app.post("/handovers/notify/authorize")
    def handovers_notify_authorize(request: Request):
        need_super_admin(request)
        ctx = C()
        try:
            with ctx.db.session() as s:
                take_write_lock(s)
                note = hn.authorize(s, ctx.settings, who(request))
        except ValueError as exc:
            return back("/handovers", f"Not authorized: {exc}")
        return back("/handovers", note)

    @app.post("/handovers/notify/revoke")
    def handovers_notify_revoke(request: Request):
        need_super_admin(request)
        ctx = C()
        with ctx.db.session() as s:
            take_write_lock(s)
            hn.revoke(s, who(request))
        return back("/handovers", "Authorization withdrawn: no handover email goes until it is authorized again")
