"""Authorized Publishing Agent: publishes approved, due posts through the official LinkedIn API only.

A post is marked "published" only after LinkedIn returns 201 with a post id. Without
authorization, posts stay in the queue with the missing-authorization reason recorded.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent, RetryPolicy
from aime.agents.contracts import BlockedItem
from aime.calendar_slots import free_slots
from aime.integrations.linkedin import (LinkedInAPIError, LinkedInClient, format_commentary, get_auth_status,
                                        get_token, post_url)
from aime.models import Alert, ContentItem
from aime.timeutil import utcnow

MAX_ATTEMPTS = 3


class PublishInput(BaseModel):
    now: datetime | None = None
    max_items: int | None = Field(default=None, ge=1, le=10)


class PublishOutput(BaseModel):
    can_publish: bool
    authorization: str
    due: int = 0
    awaiting_approval: int = 0
    published: list[int] = Field(default_factory=list)
    blocked: list[BlockedItem] = Field(default_factory=list)
    failed: list[BlockedItem] = Field(default_factory=list)
    rescheduled: list[int] = Field(default_factory=list)


class AuthorizedPublishingAgent(BaseAgent[PublishInput, PublishOutput]):
    name = "publishing"
    title = "Authorized Publishing Agent"
    responsibilities = [
        "Publish only human-approved posts whose slot is due",
        "Use only the official LinkedIn Posts/Images API with a user-granted OAuth token",
        "Keep posts queued and report the reason when authorization is missing",
        "Never mark a post published without a LinkedIn post id; reschedule stale slots instead of bursting",
    ]
    input_model = PublishInput
    output_model = PublishOutput
    # API calls are not idempotent: never re-run a whole batch automatically.
    retry_policy = RetryPolicy(max_attempts=1)

    def __init__(self, ctx, client: LinkedInClient | None = None):
        super().__init__(ctx)
        self.client = client or LinkedInClient(ctx.settings, transport=ctx.http_transport)

    def execute(self, inp: PublishInput, session: Session) -> PublishOutput:
        now = inp.now or utcnow()
        cfg = self.settings.section("publishing")
        max_items = inp.max_items or int(cfg.get("max_posts_per_run", 1))
        max_overdue = timedelta(hours=float(cfg.get("max_overdue_hours", 24)))
        auth = get_auth_status(self.settings, session)
        out = PublishOutput(can_publish=auth.can_publish, authorization=auth.reason)
        out.awaiting_approval = session.execute(select(func.count(ContentItem.id)).where(
            ContentItem.status == "awaiting_approval")).scalar_one()

        due = session.execute(select(ContentItem).where(
            ContentItem.status == "approved", ContentItem.scheduled_for.is_not(None),
            ContentItem.scheduled_for <= now).order_by(ContentItem.scheduled_for)).scalars().all()
        out.due = len(due)

        if not auth.can_publish:
            for item in due:
                item.publish_blocked_reason = auth.reason
                out.blocked.append(BlockedItem(id=item.id, reason=auth.reason))
            if due:
                self._alert_once(session, f"{len(due)} approved post(s) are due but cannot be published: {auth.reason}")
            return out

        token = get_token(session).access_token  # type: ignore[union-attr]
        published_now = 0
        for item in due:
            if now - item.scheduled_for > max_overdue:
                slots = free_slots(session, self.settings, now)
                if slots:
                    item.scheduled_for = slots[0]
                    item.publish_blocked_reason = "Slot was missed; rescheduled to next free slot"
                    out.rescheduled.append(item.id)
                    session.commit()
                    continue
            if published_now >= max_items:
                break
            try:
                image_urn = None
                if item.image_path:
                    path = self.settings.assets_dir / item.image_path
                    if path.exists():
                        image_urn = self.client.upload_image(token, auth.author_urn, path)
                urn = self.client.create_post(token, auth.author_urn, format_commentary(item.body, item.hashtags or []),
                                              image_urn=image_urn, image_title=item.title, alt_text=item.image_alt)
            except LinkedInAPIError as exc:
                item.publish_attempts += 1
                item.last_publish_error = str(exc)[:500]
                if exc.status in (401, 403):
                    item.publish_blocked_reason = f"LinkedIn rejected authorization ({exc.status}); reconnect"
                elif item.publish_attempts >= MAX_ATTEMPTS:
                    item.status = "failed"
                out.failed.append(BlockedItem(id=item.id, reason=str(exc)[:200]))
                session.commit()
                if exc.status in (401, 403):
                    break
                continue
            item.status = "published"
            item.external_id = urn
            item.external_url = post_url(urn)
            item.published_at = utcnow()
            item.publish_attempts += 1
            item.publish_blocked_reason = None
            item.last_publish_error = None
            out.published.append(item.id)
            published_now += 1
            session.commit()  # persist immediately: the post exists on LinkedIn now
        return out

    def _alert_once(self, session: Session, message: str) -> None:
        since = utcnow() - timedelta(hours=24)
        exists = session.execute(select(Alert.id).where(
            Alert.source == self.name, Alert.created_at >= since, Alert.message == message)).first()
        if not exists:
            session.add(Alert(level="warning", source=self.name, message=message[:500]))
