"""Authorized Publishing Agent: publishes approved, due posts through the official LinkedIn API only.

Publishing is at-most-once:
1. claim:   an atomic UPDATE moves the post approved -> publishing and is committed before any API call
2. publish: LinkedIn must answer 201 with a post id; that id goes to an fsync'd journal, then the database
3. unknown: timeouts, dropped connections, 5xx and crashes leave the post "uncertain"; it is never retried
            automatically. Reconciliation resolves it from the journal, LinkedIn's duplicate-content answer,
            or (with read permission) the author's recent posts; otherwise a person confirms or requeues it.
Without authorization, posts stay queued with the missing-authorization reason recorded.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent, RetryPolicy
from aime.agents.contracts import BlockedItem
from aime.calendar_slots import free_slots
from aime.crm_ops import log_activity
from aime.monitoring import alert_once
from aime.integrations.linkedin import (LinkedInAPIError, LinkedInAuthStatus, LinkedInClient, access_token,
                                        format_commentary, get_auth_status, get_token, normalize_commentary,
                                        post_url)
from aime.models import ContentItem, PublishAttempt
from aime.publish_journal import confirmed, record_success
from aime.redact import redact
from aime.timeutil import utcnow

MAX_ATTEMPTS = 3
STALE_CLAIM = timedelta(minutes=10)
ABSENCE_CONFIRM_AFTER = timedelta(minutes=30)
UNCERTAIN_HINT = "Outcome unknown ({why}). Check LinkedIn, then Confirm published or Requeue in the dashboard."


class PublishInput(BaseModel):
    now: datetime | None = None
    max_items: int | None = Field(default=None, ge=1, le=10)


class PublishOutput(BaseModel):
    can_publish: bool
    authorization: str
    due: int = 0
    awaiting_approval: int = 0
    published: list[int] = Field(default_factory=list)
    reconciled: list[int] = Field(default_factory=list)
    requeued: list[int] = Field(default_factory=list)
    uncertain: list[BlockedItem] = Field(default_factory=list)
    blocked: list[BlockedItem] = Field(default_factory=list)
    failed: list[BlockedItem] = Field(default_factory=list)
    rescheduled: list[int] = Field(default_factory=list)


class AuthorizedPublishingAgent(BaseAgent[PublishInput, PublishOutput]):
    name = "publishing"
    title = "Authorized Publishing Agent"
    responsibilities = [
        "Publish only human-approved posts whose slot is due, via the official LinkedIn API",
        "Claim each post durably before calling the API; at most one publication per post",
        "Treat timeouts and crashes as uncertain and reconcile instead of re-posting",
        "Keep posts queued and report the reason when authorization is missing",
    ]
    input_model = PublishInput
    output_model = PublishOutput
    retry_policy = RetryPolicy(max_attempts=1)  # per-post state machine handles retries safely

    def __init__(self, ctx, client: LinkedInClient | None = None):
        super().__init__(ctx)
        self.client = client or LinkedInClient(ctx.settings, transport=ctx.http_transport)

    # ------------------------------------------------------------------
    def execute(self, inp: PublishInput, session: Session) -> PublishOutput:
        now = inp.now or utcnow()
        cfg = self.settings.section("publishing")
        max_items = inp.max_items or int(cfg.get("max_posts_per_run", 1))
        max_overdue = timedelta(hours=float(cfg.get("max_overdue_hours", 24)))
        auth = get_auth_status(self.settings, session)
        out = PublishOutput(can_publish=auth.can_publish, authorization=auth.reason)

        self._reconcile(session, now, auth, out)

        # "approved" without an approval record (e.g. set directly in the database) is not an approval.
        for item in session.execute(select(ContentItem).where(
                ContentItem.status == "approved", ContentItem.approved_at.is_(None))).scalars():
            item.status = "awaiting_approval"
            item.publish_blocked_reason = "Needs a person's approval before it can be published"
            out.blocked.append(BlockedItem(id=item.id, reason=item.publish_blocked_reason))
        session.commit()

        out.awaiting_approval = session.execute(select(func.count(ContentItem.id)).where(
            ContentItem.status == "awaiting_approval")).scalar_one()
        due = session.execute(select(ContentItem).where(
            ContentItem.status == "approved", ContentItem.approved_at.is_not(None),
            ContentItem.scheduled_for.is_not(None),
            ContentItem.scheduled_for <= now).order_by(ContentItem.scheduled_for)).scalars().all()
        out.due = len(due)

        if not auth.can_publish:
            for item in due:
                item.publish_blocked_reason = auth.reason
                out.blocked.append(BlockedItem(id=item.id, reason=auth.reason))
            if due:
                self._alert_once(session, f"{len(due)} approved post(s) are due but cannot be published: {auth.reason}")
            session.commit()
            return out

        token = access_token(get_token(session))
        published_now = 0
        for item in due:
            if now - item.scheduled_for > max_overdue:
                slots = free_slots(session, self.settings, now)
                if slots:
                    item.scheduled_for = slots[0]
                    item.publish_blocked_reason = "Slot was missed; rescheduled to the next free slot"
                    out.rescheduled.append(item.id)
                    self._alert_once(session, f"Post #{item.id} missed its slot and was moved to {slots[0]:%Y-%m-%d %H:%M} UTC",
                                     level="info")
                else:
                    # Never publish late: without a free slot the post goes back to a person.
                    late_hours = int((now - item.scheduled_for).total_seconds() // 3600)
                    item.status, item.approved_by, item.approved_at = "awaiting_approval", None, None
                    item.publish_blocked_reason = ("Missed its slot and no free slot is available; "
                                                   "choose a new time and approve it again")
                    out.blocked.append(BlockedItem(id=item.id, reason=item.publish_blocked_reason))
                    log_activity(session, f"Post #{item.id} held: {late_hours} h past its slot, approval withdrawn",
                                 self.name, type_="status_change")
                    self._alert_once(session, f"Post #{item.id} missed its slot and needs a new time and approval")
                session.commit()
                continue
            if published_now >= max_items:
                break
            attempt = self._claim(session, item, auth)
            if attempt is None:
                continue  # another process claimed it first
            if self._publish_one(session, item, attempt, token, auth, out):
                published_now += 1
            elif any(u.id == item.id for u in out.uncertain):
                break  # stop the batch: the API connection is unreliable right now
        return out

    # ------------------------------------------------------------------ claim / publish
    def _claim(self, session: Session, item: ContentItem, auth: LinkedInAuthStatus) -> PublishAttempt | None:
        attempt_id = uuid.uuid4().hex
        started = utcnow()
        res = session.execute(update(ContentItem).where(
            ContentItem.id == item.id, ContentItem.status == "approved", ContentItem.approved_at.is_not(None)).values(
            status="publishing", publish_attempt_id=attempt_id, publish_started_at=started,
            publish_attempts=ContentItem.publish_attempts + 1))
        if res.rowcount != 1:
            session.rollback()
            return None
        commentary = format_commentary(item.body, item.hashtags or [])
        attempt = PublishAttempt(attempt_id=attempt_id, content_id=item.id, author_urn=auth.author_urn,
                                 commentary_sha256=hashlib.sha256(commentary.encode()).hexdigest(),
                                 started_at=started, outcome="pending")
        session.add(attempt)
        session.commit()  # durable claim before any network call
        session.refresh(item)
        return attempt

    def _publish_one(self, session: Session, item: ContentItem, attempt: PublishAttempt, token: str,
                     auth: LinkedInAuthStatus, out: PublishOutput) -> bool:
        if item.image_path and not item.image_urn:
            path = self.settings.assets_dir / item.image_path
            if path.exists():
                try:
                    item.image_urn = self.client.upload_image(token, auth.author_urn, path)
                    session.commit()
                except (LinkedInAPIError, httpx.HTTPError) as exc:
                    # No post exists yet, so releasing the claim cannot cause a duplicate.
                    self._release(session, item, attempt, f"Image upload failed: {exc}", out,
                                  auth_problem=isinstance(exc, LinkedInAPIError) and exc.status in (401, 403))
                    return False
        commentary = format_commentary(item.body, item.hashtags or [])
        try:
            urn = self.client.create_post(token, auth.author_urn, commentary, image_urn=item.image_urn,
                                          image_title=item.title, alt_text=item.image_alt)
        except LinkedInAPIError as exc:
            attempt.http_status = exc.status
            if exc.duplicate_of:
                self._mark_published(session, item, attempt, exc.duplicate_of, "duplicate_response", out)
                return True
            if exc.status in (401, 403):
                self._release(session, item, attempt, str(exc), out, auth_problem=True)
            elif exc.status == 408 or exc.status >= 500 or exc.status == 201:
                self._mark_uncertain(session, item, attempt, f"LinkedIn answered {exc.status}", out)
            else:  # 4xx: request rejected, nothing was created
                self._release(session, item, attempt, str(exc), out)
            return False
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The connection was never established, so the request never reached LinkedIn.
            self._release(session, item, attempt, f"Could not connect: {exc}", out)
            return False
        except httpx.HTTPError as exc:  # read/write timeout, dropped connection: may have been created
            self._mark_uncertain(session, item, attempt, f"{type(exc).__name__}", out)
            return False

        record_success(self.settings.data_dir, attempt.attempt_id, item.id, urn)  # before the DB update
        self._mark_published(session, item, attempt, urn, "api_201", out)
        return True

    def _mark_published(self, session: Session, item: ContentItem, attempt: PublishAttempt | None, urn: str,
                        via: str, out: PublishOutput) -> None:
        now = utcnow()
        item.status = "published"
        item.external_id = urn
        item.external_url = post_url(urn)
        item.published_at = item.published_at or now
        item.reconciled_via = via
        item.publish_blocked_reason = None
        item.last_publish_error = None
        if attempt is not None:
            attempt.outcome = "success" if via == "api_201" else "reconciled"
            attempt.external_id = urn
            attempt.finished_at = now
        (out.published if via in ("api_201", "duplicate_response") else out.reconciled).append(item.id)
        session.commit()

    def _release(self, session: Session, item: ContentItem, attempt: PublishAttempt, error: str,
                 out: PublishOutput, auth_problem: bool = False) -> None:
        """Definite failure: nothing was created on LinkedIn, so the post may be retried later."""
        error = redact(error)[:500]
        attempt.outcome, attempt.error, attempt.finished_at = "failed", error, utcnow()
        item.last_publish_error = error
        item.publish_attempt_id = None
        item.publish_started_at = None
        if auth_problem:
            item.status = "approved"
            item.publish_blocked_reason = "LinkedIn rejected the authorization; reconnect LinkedIn"
        elif item.publish_attempts >= MAX_ATTEMPTS:
            item.status = "failed"
            item.publish_blocked_reason = f"Failed {item.publish_attempts} times; review and re-approve"
        else:
            item.status = "approved"
        out.failed.append(BlockedItem(id=item.id, reason=error[:200]))
        session.commit()

    def _mark_uncertain(self, session: Session, item: ContentItem, attempt: PublishAttempt | None, why: str,
                        out: PublishOutput) -> None:
        reason = UNCERTAIN_HINT.format(why=why)
        item.status = "uncertain"
        item.publish_blocked_reason = reason
        if attempt is not None:
            attempt.outcome, attempt.error, attempt.finished_at = "uncertain", why, utcnow()
        out.uncertain.append(BlockedItem(id=item.id, reason=reason))
        self._alert_once(session, f"Post #{item.id} '{item.title[:60]}': {reason}", level="error")
        session.commit()

    # ------------------------------------------------------------------ reconciliation
    def _reconcile(self, session: Session, now: datetime, auth: LinkedInAuthStatus, out: PublishOutput) -> None:
        open_items = session.execute(select(ContentItem).where(
            ContentItem.status.in_(("publishing", "uncertain")))).scalars().all()
        if not open_items:
            return
        journal = confirmed(self.settings.data_dir)
        posts = None
        for item in open_items:
            attempt = session.execute(select(PublishAttempt).where(
                PublishAttempt.attempt_id == item.publish_attempt_id)).scalars().first()
            rec = journal.get(item.publish_attempt_id or "")
            if rec:
                self._mark_published(session, item, attempt, rec["external_id"], "journal", out)
                continue
            if item.status == "publishing":
                if item.publish_started_at and now - item.publish_started_at < STALE_CLAIM:
                    continue  # still in flight in another process
                self._mark_uncertain(session, item, attempt, "the publishing process stopped mid-request", out)
            if not (auth.connected and auth.can_read_posts and auth.author_urn):
                continue
            if posts is None:
                try:
                    posts = self.client.recent_posts(access_token(get_token(session)), auth.author_urn)
                except (LinkedInAPIError, httpx.HTTPError) as exc:
                    self.log.warning("Could not read recent posts for reconciliation: %s", exc)
                    return
            wanted = normalize_commentary(format_commentary(item.body, item.hashtags or []))
            match = next((p for p in posts if normalize_commentary(p.get("commentary", "")) == wanted), None)
            if match and match.get("id"):
                self._mark_published(session, item, attempt, match["id"], "api_lookup", out)
            elif item.publish_started_at and now - item.publish_started_at > ABSENCE_CONFIRM_AFTER:
                # LinkedIn confirms the post does not exist: safe to put it back in the queue.
                item.status = "approved"
                item.publish_attempt_id = None
                item.publish_started_at = None
                item.publish_blocked_reason = "Previous attempt confirmed absent on LinkedIn; requeued"
                if attempt is not None:
                    attempt.outcome = "not_sent"
                out.requeued.append(item.id)
                session.commit()

    def _alert_once(self, session: Session, message: str, level: str = "warning") -> None:
        alert_once(session, level, self.name, message, self.settings)
