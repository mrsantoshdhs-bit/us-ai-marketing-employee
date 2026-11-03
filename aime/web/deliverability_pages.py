"""The Deliverability page: per-mailbox sends in 24 hours and caps, bounce and complaint rates, the SPF/DKIM/DMARC gate,
the warm-up stage and the outbound pause with its reason (aime.deliverability).

Who sees it: roles that see the whole organisation (view_all), managers who approve what goes out (approve_outbound) and the
Super Admin. Who changes it: an administrator (admin_ops) clears a pause or runs the authentication check now; both are
audited, and so is a refused attempt to clear. Plain HTML forms: no JavaScript (the pages run with script-src 'none').
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse

VIEW_PERMISSIONS = ("view_all", "approve_outbound", "configure_system")


def _perms(C, request: Request):
    """The signed-in user's permissions, or None when the dashboard runs without sign-in (the owner's own PC)."""
    if not C().settings.dashboard_auth:
        return None
    user = getattr(request.state, "user", None)
    return set(user.get("perms", ())) if user else set()


def register(app, C, render, back, who) -> None:
    from aime import deliverability

    @app.get("/deliverability", response_class=HTMLResponse)
    def deliverability_page(request: Request):
        perms = _perms(C, request)
        if perms is not None and not perms & set(VIEW_PERMISSIONS):
            raise HTTPException(403, "The deliverability figures are for managers and administrators")
        ctx = C()
        with ctx.db.session() as s:
            view = deliverability.panel(s, ctx.settings)
        return render(request, "deliverability.html", may_clear=deliverability.may_clear(perms), **view)

    @app.post("/deliverability/clear")
    def deliverability_clear(request: Request, note: Annotated[str, Form()] = ""):
        ctx = C()
        with ctx.db.session() as s:
            try:
                was = deliverability.clear_pause(s, who(request), _perms(C, request), note)
            except PermissionError as exc:
                s.commit()  # the refused attempt stays in the audit log
                return back("/deliverability", str(exc))
            except ValueError as exc:
                return back("/deliverability", str(exc))
            s.commit()
        return back("/deliverability", f"Pause cleared (it was: {was.get('reason', '')[:160]}). The rates count again from now")

    @app.post("/deliverability/auth-check")
    def deliverability_auth_check(request: Request):
        perms = _perms(C, request)
        if not deliverability.may_clear(perms):
            return back("/deliverability", "Only an administrator runs the authentication check")
        ctx = C()
        with ctx.db.session() as s:
            state = deliverability.refresh_auth(s, ctx.settings, actor=who(request))
            s.commit()
        if state is None:
            return back("/deliverability", "Not checked: no DNS look-up can be made here (network off, or test data)")
        problem = deliverability.auth_problem(state, state.get("domain"), deliverability.utcnow(), deliverability.config(ctx.settings))
        return back("/deliverability", f"Checked {state.get('domain') or 'no domain'}: " + (f"the check {problem}" if problem else "SPF, DKIM and DMARC pass"))
