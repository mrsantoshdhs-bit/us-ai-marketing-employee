"""The AI quality page (/agents/ai-quality): what the AI reasoning core proposed in shadow and what became of it.

It sits under /agents, an organisation page (aime.visibility.ORG_PREFIXES), so the sign-in middleware lets only roles
that see the whole organisation reach it: Super Admin, Administrator, Sales Director and Read-Only Analyst. It only
reads: decisions per agent, the verdict mix, escalations by rule, the latest blocks with their reasons, latency, tokens,
agreement with people (once a person's action is recorded) and model health (valid structured answers and failures),
plus the route each AI task would take. No model is called to draw it. Server-rendered, no script."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from aime.models import AIDecision
from aime.timeutil import utcnow

TASKS = ("reply_analysis", "conversation")
WINDOWS = {"1": 1, "7": 7, "30": 30, "90": 90}


def _pct(part: int, whole: int) -> int | None:
    return round(100 * part / whole) if whole else None


def _quantile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * (len(values) - 1) + 0.5))]


def quality_summary(session, days: int = 30, limit: int = 20000) -> dict:
    since = utcnow() - timedelta(days=days)
    rows = session.execute(select(AIDecision).where(AIDecision.created_at >= since).order_by(AIDecision.id.desc())
                           .limit(limit)).scalars().all()
    verdicts = Counter(r.verdict for r in rows)
    per_agent: dict[str, dict] = defaultdict(lambda: {"total": 0, "auto_internal": 0, "human_review": 0, "block": 0, "tokens": 0, "ms": []})
    escal: Counter[str] = Counter()
    models: dict[str, dict] = defaultdict(lambda: {"asked": 0, "valid": 0, "failed": 0, "ms": [], "last": None})
    prompts: Counter[str] = Counter()
    for r in rows:
        a = per_agent[r.agent]
        a["total"] += 1
        a[r.verdict] = a.get(r.verdict, 0) + 1
        a["tokens"] += r.tokens or 0
        if r.ms:
            a["ms"].append(r.ms)
        escal.update(set(r.escalations or []))
        if r.prompt_version:
            prompts[r.prompt_version] += 1
        if r.structured_ok is not None:
            m = models[f"{r.provider}:{r.model or '?'}"]
            m["asked"] += 1
            m["valid" if r.structured_ok else "failed"] += 1
            if r.ms:
                m["ms"].append(r.ms)
            m["last"] = m["last"] or r.created_at
    agents = []
    for name, a in sorted(per_agent.items()):
        agents.append({"agent": name, "total": a["total"], "auto": a.get("auto_internal", 0), "review": a.get("human_review", 0),
                       "block": a.get("block", 0), "tokens": a["tokens"], "p50": _quantile(a["ms"], 0.5), "p95": _quantile(a["ms"], 0.95)})
    health = [{"model": k, "asked": v["asked"], "valid_pct": _pct(v["valid"], v["asked"]), "failed": v["failed"],
               "p50": _quantile(v["ms"], 0.5), "p95": _quantile(v["ms"], 0.95), "last": v["last"]} for k, v in sorted(models.items())]
    judged = [r for r in rows if r.human_action]
    edited = [r.edit_distance for r in judged if r.human_action == "edited" and r.edit_distance is not None]
    human = {"judged": len(judged), "accepted": sum(r.human_action == "accepted" for r in judged),
             "edited": sum(r.human_action == "edited" for r in judged), "rejected": sum(r.human_action == "rejected" for r in judged),
             "avg_edit_distance": round(sum(edited) / len(edited)) if edited else None}
    human["agreement_pct"] = _pct(human["accepted"] + human["edited"], human["judged"])
    blocks = [r for r in rows if r.verdict == "block"][:20]
    all_ms = [r.ms for r in rows if r.ms]
    return {"days": days, "total": len(rows), "verdicts": {k: verdicts.get(k, 0) for k in ("auto_internal", "human_review", "block")},
            "verdict_pct": {k: _pct(verdicts.get(k, 0), len(rows)) for k in ("auto_internal", "human_review", "block")},
            "agents": agents, "escalations": escal.most_common(), "health": health, "human": human, "blocks": blocks,
            "prompts": prompts.most_common(8), "p50": _quantile(all_ms, 0.5), "p95": _quantile(all_ms, 0.95),
            "tokens": sum(r.tokens or 0 for r in rows), "escalated": sum(1 for r in rows if r.escalations)}


def register(app, C, render, back, who) -> None:
    @app.get("/agents/ai-quality", response_class=HTMLResponse)
    def ai_quality(request: Request, days: str = "30"):
        from aime.llm.router import ModelRouter, ai_config

        window = WINDOWS.get(days, 30)
        ctx = C()
        router = ModelRouter(ctx.settings)
        routes = [router.route(t) for t in TASKS]
        cfg = ai_config(ctx.settings)
        with ctx.db.session() as s:
            summary = quality_summary(s, window)
            return render(request, "ai_quality.html", q=summary, routes=routes, windows=list(WINDOWS), window=str(window),
                          spend_cap=cfg["monthly_spend_cap_usd"], cloud_approved=bool(cfg["cloud"].get("owner_approved")),
                          require_approval=ctx.settings.require_human_approval)
