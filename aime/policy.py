"""Auto-publishing policy, SHADOW MODE ONLY.

For every post waiting for a person (or approved by one) the policy records what it would do if it
were allowed to publish on its own: "would_publish" when every rule passes, otherwise "would_hold"
with the rules that held it. It never changes a post, never approves, schedules or publishes, and
never calls LinkedIn. The decisions are compared with what the person actually did so the policy
can be judged before anyone considers letting it act.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.config import Settings
from aime.crm_ops import log_activity
from aime.integrations.linkedin import get_auth_status
from aime.models import ContentItem, PolicyDecision
from aime.qc import content_sha256
from aime.timeutil import utcnow

log = logging.getLogger(__name__)

POLICY_VERSION = 1
ACTOR = "publish_policy"
EVALUATED_STATUSES = ("awaiting_approval", "approved")
HUMAN_YES = ("approved", "publishing", "uncertain", "published")


def policy_config(settings: Settings) -> dict:
    cfg = settings.section("publish_policy")
    return {
        "mode": str(cfg.get("mode", "shadow")).lower(),
        "allowed_services": list(cfg.get("allowed_services", [])),
        "require_designed_image": bool(cfg.get("require_designed_image", True)),
        "max_posts_per_week": int(cfg.get("max_posts_per_week", 3)),
        "min_hours_between_posts": float(cfg.get("min_hours_between_posts", 20)),
        "max_draft_age_days": int(cfg.get("max_draft_age_days", 14)),
        "hold_terms": [t.lower() for t in cfg.get("hold_terms", [])],
    }


def _rule(name: str, ok: bool, detail: str) -> dict:
    return {"rule": name, "ok": ok, "detail": detail}


def evaluate_post(item: ContentItem, cfg: dict, settings: Settings, now: datetime,
                  taken: list[datetime]) -> list[dict]:
    """Every rule with its result. `taken` holds publish times already used (published, or would-publish
    decisions earlier in this pass), for the weekly cap and spacing rules."""
    qc = item.qc
    rules = [_rule("quality_checks", item.qc_status == "passed", f"quality checks {item.qc_status}")]
    current = qc.get("content_sha256") == content_sha256(item.title, item.body, item.hashtags or [])
    rules.append(_rule("checked_text_is_current", current,
                       "checked text matches the post" if current else "post was edited after its checks"))

    image = (item.quality or {}).get("image") or {}
    path = settings.assets_dir / item.image_path if item.image_path else None
    if not path or not path.exists():
        rules.append(_rule("image", False, "no image file"))
    elif cfg["require_designed_image"] and image.get("renderer") != "browser":
        rules.append(_rule("image", False, "simple card image; designed images only"))
    else:
        rules.append(_rule("image", True, f"{image.get('style') or 'image'} ready"))

    allowed = cfg["allowed_services"]
    rules.append(_rule("service", not allowed or item.service in allowed,
                       f"service {item.service}" + ("" if not allowed or item.service in allowed else " not allowed")))

    text = f"{item.title}\n{item.body}".lower()
    hits = [t for t in cfg["hold_terms"] if re.search(rf"\b{re.escape(t)}\b", text)]
    rules.append(_rule("sensitive_terms", not hits, "mentions " + ", ".join(hits) if hits else "none found"))

    age = (now - item.created_at).days if item.created_at else 0
    fresh = age <= cfg["max_draft_age_days"]
    rules.append(_rule("draft_age", fresh, f"draft is {age} days old" if fresh
                       else f"draft older than {cfg['max_draft_age_days']} days"))

    slot = item.scheduled_for
    if slot is None:
        rules.append(_rule("slot", False, "no publish time"))
        return rules
    rules.append(_rule("slot", slot >= now, f"slot {slot:%Y-%m-%d %H:%M} UTC" + ("" if slot >= now else " has passed")))
    week = [t for t in taken if slot - timedelta(days=7) < t <= slot]
    rules.append(_rule("weekly_cap", len(week) < cfg["max_posts_per_week"],
                       f"{len(week)} other post(s) in the 7 days before this slot (cap {cfg['max_posts_per_week']})"))
    gap = cfg["min_hours_between_posts"]
    near = [t for t in taken if abs((t - slot).total_seconds()) < gap * 3600]
    rules.append(_rule("spacing", not near, f"another post within {gap:g} h" if near else f"no post within {gap:g} h"))
    return rules


def environment_blockers(session: Session, settings: Settings, cfg: dict) -> list[str]:
    """What would stop any publication regardless of the policy (shown, never changed here)."""
    blockers = ["shadow mode: the policy never publishes"]
    if cfg["mode"] != "shadow":
        blockers.append(f"publish_policy.mode = {cfg['mode']!r} is not supported; treated as shadow")
    if not settings.publishing_enabled:
        blockers.append("PUBLISHING_ENABLED=false")
    auth = get_auth_status(settings, session)
    if not auth.can_publish and "PUBLISHING_ENABLED" not in auth.reason:
        blockers.append(f"LinkedIn: {auth.reason}")
    if settings.require_human_approval:
        blockers.append("REQUIRE_HUMAN_APPROVAL=true: every post needs a person")
    return blockers


def latest_decisions(session: Session) -> dict[int, PolicyDecision]:
    latest: dict[int, PolicyDecision] = {}
    for d in session.execute(select(PolicyDecision).order_by(PolicyDecision.id)).scalars():
        latest[d.content_id] = d
    return latest


def evaluate_shadow(session: Session, settings: Settings, now: datetime | None = None) -> dict:
    """Evaluate every pending or approved post and record changed decisions. Reads posts, writes only
    policy_decisions rows and activity entries."""
    now = now or utcnow()
    cfg = policy_config(settings)
    if cfg["mode"] != "shadow":
        log.warning("publish_policy.mode=%r is not supported in this version; running in shadow mode", cfg["mode"])
    blockers = environment_blockers(session, settings, cfg)
    taken = [t for t in session.execute(select(ContentItem.published_at).where(
        ContentItem.status == "published", ContentItem.published_at.is_not(None))).scalars()]
    items = session.execute(select(ContentItem).where(ContentItem.status.in_(EVALUATED_STATUSES))
                            .order_by(ContentItem.scheduled_for, ContentItem.id)).scalars().all()
    latest = latest_decisions(session)
    out = {"mode": "shadow", "evaluated": len(items), "would_publish": [], "would_hold": [], "recorded": 0,
           "blockers": blockers}
    for item in items:
        rules = evaluate_post(item, cfg, settings, now, taken)
        reasons = [f"{r['rule']}: {r['detail']}" for r in rules if not r["ok"]]
        decision = "would_hold" if reasons else "would_publish"
        if decision == "would_publish":
            taken.append(item.scheduled_for)
        out[decision].append(item.id)
        sha = content_sha256(item.title, item.body, item.hashtags or [])
        prev = latest.get(item.id)
        if prev and prev.decision == decision and prev.content_sha256 == sha and prev.reasons == reasons \
                and prev.policy_version == POLICY_VERSION:
            continue
        session.add(PolicyDecision(content_id=item.id, decided_at=now, mode="shadow", policy_version=POLICY_VERSION,
                                   decision=decision, reasons=reasons, rules=rules, blockers=blockers,
                                   content_sha256=sha, post_status=item.status))
        verb = "would publish" if decision == "would_publish" else "would hold"
        log_activity(session, f"Policy (shadow) {verb} post #{item.id}: {item.title}"[:300], ACTOR,
                     type_="policy_shadow", body="; ".join(reasons) or "all rules passed")
        out["recorded"] += 1
    session.flush()
    return out


def agreement(session: Session) -> dict:
    """Compare each post's latest shadow decision with what the person did."""
    rows = []
    counts = {"agree": 0, "too_permissive": 0, "too_strict": 0, "pending": 0}
    for cid, d in latest_decisions(session).items():
        item = d.content
        if item.status == "rejected":
            human = "rejected"
        elif item.status in HUMAN_YES and item.approved_at is not None:
            human = "approved"
        else:
            human = "pending"
        if human == "pending":
            verdict = "pending"
        elif (d.decision == "would_publish") == (human == "approved"):
            verdict = "agree"
        else:
            verdict = "too_permissive" if d.decision == "would_publish" else "too_strict"
        counts[verdict] += 1
        rows.append({"content_id": cid, "title": item.title, "scheduled_for": item.scheduled_for,
                     "decision": d.decision, "reasons": d.reasons, "decided_at": d.decided_at,
                     "human": human, "verdict": verdict, "post_status": item.status})
    rows.sort(key=lambda r: (r["scheduled_for"] is None, r["scheduled_for"] or datetime.min))
    decided = counts["agree"] + counts["too_permissive"] + counts["too_strict"]
    counts["agreement_rate"] = round(100 * counts["agree"] / decided) if decided else None
    return {"rows": rows, "counts": counts}
