"""One place that reports what is connected, configured, disabled, or blocked."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from aime.context import AppContext
from aime.integrations.email_smtp import EmailSender
from aime.integrations.linkedin import get_auth_status
from aime.monitoring import read_heartbeat
from aime.sources.census import CensusMarketSource
from aime.sources.registry import get_sources
from aime.timeutil import fmt_local, utcnow


def integration_statuses(ctx: AppContext, session: Session, include_scheduler: bool = True) -> list[dict]:
    s = ctx.settings
    rows: list[dict] = []

    li = get_auth_status(s, session)
    if not li.configured:
        state = "NOT CONFIGURED"
    elif not li.connected:
        state = "NOT CONNECTED"
    elif li.can_publish:
        state = "CONNECTED"
    else:
        state = "BLOCKED"
    rows.append({"name": "LinkedIn publishing (official API)", "state": state, "detail": li.reason,
                 "action": "Add LinkedIn app credentials to .env, then Connect LinkedIn" if not li.configured else
                 ("Connect LinkedIn on the Integrations page" if not li.connected else "")})
    rows.append({"name": "Publishing master switch", "state": "ENABLED" if s.publishing_enabled else "DISABLED",
                 "detail": "PUBLISHING_ENABLED in .env (posts also need per-post approval)", "action": ""})

    ready, reason = EmailSender(s).status()
    rows.append({"name": "Outbound email (replies to inquiries)", "state": "READY" if ready else
                 ("DISABLED" if "DISABLED" in reason else "NOT CONFIGURED"), "detail": reason, "action": ""})

    llm = ctx.llm
    rows.append({"name": "Local LLM (Ollama)", "state": "AVAILABLE" if llm else "UNAVAILABLE",
                 "detail": llm.label if llm else f"Not reachable at {s.ollama_url}; template writer in use",
                 "action": "" if llm else "Start Ollama or set LLM_PROVIDER=template"})

    for src in get_sources(ctx):
        ok, msg = src.configured()
        rows.append({"name": f"Source: {src.title}", "state": "CONFIGURED" if ok else
                     ("DISABLED" if msg.startswith("Disabled") else "NOT CONFIGURED"), "detail": msg, "action": ""})
    ok, msg = CensusMarketSource(ctx).configured()
    rows.append({"name": "Source: US Census market sizing", "state": "CONFIGURED" if ok else "NOT CONFIGURED",
                 "detail": msg, "action": ""})

    if include_scheduler:
        hb = read_heartbeat(s)
        if hb is None:
            rows.append({"name": "Scheduler heartbeat", "state": "NOT RUNNING", "detail": "No tick recorded yet",
                         "action": "Run: python -m aime scheduler install"})
        else:
            at = datetime.fromisoformat(hb["at"].rstrip("Z"))
            tick = int(s.schedule.get("tick_minutes", 15))
            fresh = utcnow() - at <= timedelta(minutes=tick * 2 + 5)
            rows.append({"name": "Scheduler heartbeat", "state": "RUNNING" if fresh else "STALE",
                         "detail": f"Last tick {fmt_local(at)} (trigger: {hb.get('trigger')})", "action": ""})
    return rows
