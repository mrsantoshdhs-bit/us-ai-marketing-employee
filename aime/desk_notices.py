"""Desktop notices while someone is signed in, from the one process that can show them.

The tick runs in the background session even while the owner is signed in, and nothing shown there reaches a screen.
The dashboard runs on the signed-in desktop: once a minute it looks for new warning and error alerts and new interested
replies, and shows them through aime.notify. It remembers in the key-value store the last alert and reply it looked at,
so a restart repeats nothing and the first start shows nothing old. It sends no mail and changes no alert.
Started only by `aime serve`, never by the web app itself, so TestClient and the tests never run it.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import func, select

from aime.db import kv_get, kv_set
from aime.models import Alert, InboundEmail
from aime.notify import can_show, notify_alert, notify_event

log = logging.getLogger("aime.desk_notices")

SEEN = "desk_notices.seen"
POLL_SECONDS = 60
MOST_AT_ONCE = 3  # back after a night signed out: three toasts and a count, not a screen full


def _new_notices(session, seen: dict, top: dict) -> list[tuple[str | None, str]]:
    """(level, words) for each new notice, the most urgent first: a level for an alert, None for an interested reply."""
    replies = session.execute(select(InboundEmail).where(
        InboundEmail.id > seen["reply"], InboundEmail.id <= top["reply"], InboundEmail.category == "interested",
        InboundEmail.is_simulated.is_(False)).order_by(InboundEmail.id)).scalars()
    notices: list[tuple[str | None, str]] = [
        (None, f"Interested reply from {r.from_email}" + (f" ({r.lead.company.name})" if r.lead else "")) for r in replies]
    alerts = session.execute(select(Alert).where(  # one already dismissed or closed by itself needs nobody's attention
        Alert.id > seen["alert"], Alert.id <= top["alert"], Alert.level.in_(("warning", "error")),
        Alert.acknowledged.is_(False), Alert.resolved_at.is_(None)).order_by(Alert.id)).scalars().all()
    for level in ("error", "warning"):
        notices += [(a.level, a.message) for a in alerts if a.level == level]
    return notices


def poll_once(ctx) -> int:
    """Show what is new since the last look. Returns how many notices there were."""
    settings = ctx.settings
    with ctx.db.session() as s:
        top = {"alert": s.execute(select(func.max(Alert.id))).scalar_one() or 0,
               "reply": s.execute(select(func.max(InboundEmail.id))).scalar_one() or 0}
        seen = kv_get(s, SEEN)
        if seen is None:  # the first start: everything so far is old
            kv_set(s, SEEN, top)
            return 0
        if top["alert"] <= seen["alert"] and top["reply"] <= seen["reply"]:
            return 0
        notices = _new_notices(s, seen, top)
        kv_set(s, SEEN, top)
    # Remembered first: a toast that fails is still on the dashboard, and one shown again every minute would be worse
    for level, words in notices[:MOST_AT_ONCE]:
        if level:
            notify_alert(settings, level, words)
        else:
            notify_event(settings, words)
    if len(notices) > MOST_AT_ONCE:
        notify_event(settings, f"{len(notices) - MOST_AT_ONCE} more on the dashboard")
    return len(notices)


def _loop(ctx, stop: threading.Event, seconds: float = POLL_SECONDS) -> None:
    while True:
        try:
            poll_once(ctx)
        except Exception:  # a notice is an extra nudge: it must never stop the dashboard
            log.exception("Desktop notices: this look failed, trying again in a minute")
        if stop.wait(seconds):
            return


def start(ctx) -> threading.Thread | None:
    """Only for the real data, and only where a toast can be shown at all."""
    if not ctx.settings.is_production or not can_show(ctx.settings):
        return None
    thread = threading.Thread(target=_loop, args=(ctx, threading.Event()), name="desk-notices", daemon=True)
    thread.start()
    return thread
