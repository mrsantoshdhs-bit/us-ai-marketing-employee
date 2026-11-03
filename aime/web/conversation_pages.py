"""The Super Admin page for the AI conversation engine (/admin/settings/conversation): the objection playbook, the
approved claims, the pricing rule and mode as they stand, and the open handover requests.

Everything lives under /admin/settings, so the sign-in middleware lets only `configure_system` (Super Admin) reach it,
viewing included, and the same-origin check covers every change. Plain forms: the content policy allows no script.
Each save is audited (aime.ai.playbook.save, aime.ai.claims.save). The mode and the pricing rule are a versioned
settings section ("AI conversations"), changed on its own page with preview and restore. Nothing here sends anything,
and no setting here can turn off the approval of what a prospect reads."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse

from aime.ai import playbook as pb_mod

URL = "/admin/settings/conversation"


def _lines(text) -> list[str]:
    return [x.strip() for x in str(text or "").replace("\r\n", "\n").split("\n") if x.strip()]


def claim_problems(text: str) -> list[str]:
    """A claim may name facts about us (the owner's own words), never a price, discount, contract, guarantee or legal
    matter: the policy engine would block every draft that used it."""
    return [p for p in pb_mod.problems(text) if not p.startswith("a number")]


def register(app, C, render, back, who) -> None:
    @app.get(URL, response_class=HTMLResponse)
    def conversation_admin(request: Request):
        from aime.ai import claims as claims_mod
        from aime.ai import conversation
        from aime.db import kv_get

        ctx = C()
        with ctx.db.session() as s:
            book = pb_mod.load(s)
            claims, topics = claims_mod.load(ctx.settings, s)
            stored = kv_get(s, claims_mod.KV_KEY) or {}
            handovers = [{"id": r.id, "lead_id": r.lead_id, "at": r.created_at, "reason": (r.proposal or {}).get("reason") or "",
                          "codes": r.escalations or []} for r in conversation.handover_requests(s)[:30]]
        return render(request, "conversation_admin.html", playbook=book, offers=pb_mod.OFFERS, claims=claims,
                      owner_claims="\n".join(stored.get("claims") or []), topics=topics, cfg=conversation.config(ctx.settings),
                      handovers=handovers, require_approval=ctx.settings.require_human_approval)

    @app.post(f"{URL}/playbook")
    async def conversation_playbook_save(request: Request):
        form = await request.form()
        if form.get("playbook_form") != "1":
            return back(URL, "Not saved: the form was incomplete")
        data = {}
        for code in pb_mod.DEFAULT_PLAYBOOK:
            data[code] = {"enabled": form.get(f"{code}.enabled") == "yes", "points": _lines(form.get(f"{code}.points")),
                          "offers": [o for o in pb_mod.OFFERS if form.get(f"{code}.offer.{o}") == "yes"]}
        ctx = C()
        with ctx.db.session() as s:
            try:
                changed = pb_mod.save(s, data, who(request))
            except ValueError as exc:
                s.rollback()
                return back(URL, f"Not saved: {exc}"[:600])
        return back(URL, f"Playbook saved: {', '.join(changed)} changed" if changed else "Playbook saved, unchanged")

    @app.post(f"{URL}/claims")
    async def conversation_claims_save(request: Request):
        from aime.ai import claims as claims_mod
        from aime.db import kv_get

        form = await request.form()
        if form.get("claims_form") != "1":
            return back(URL, "Not saved: the form was incomplete")
        lines = _lines(form.get("claims"))
        refused = [f'"{c[:80]}" mentions {", ".join(claim_problems(c)[:2])}' for c in lines if claim_problems(c)]
        if refused:
            return back(URL, ("Not saved: " + "; ".join(refused))[:600])
        ctx = C()
        with ctx.db.session() as s:
            stored = kv_get(s, claims_mod.KV_KEY) or {}
            claims_mod.save(s, lines, stored.get("prohibited_topics") or [], who(request))
        return back(URL, f"Approved claims saved: {len(lines)} of the owner's own")
