"""LinkedIn Content Agent: topic research, post drafting, hashtags, visuals, and the content calendar."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent
from aime.agents.contracts import ContentBrief
from aime.calendar_slots import free_slots
from aime.content import build_hashtags, write_post
from aime.knowledge.content_bank import ANGLES
from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES
from aime.models import ContentItem, MarketingPlan, ResearchTopic
from aime.sources.trends import fetch_trending_topics
from aime.timeutil import utcnow
from aime.monitoring import alert_once
from aime.qc import summary as qc_summary
from aime.qc.checks import run_quality_checks
from aime.visuals import render_post_image

QC_ATTEMPTS = 3  # drafts tried per slot before it is left empty

PENDING_STATUSES = ("draft", "awaiting_approval", "approved")


class ContentInput(BaseModel):
    briefs: list[ContentBrief] = Field(default_factory=list)
    count: int | None = Field(default=None, ge=1, le=20)
    research_topics: bool = True


class ContentOutput(BaseModel):
    created: list[int] = Field(default_factory=list)
    scheduled_for: list[datetime] = Field(default_factory=list)
    generator_counts: dict[str, int] = Field(default_factory=dict)
    llm_rejections: int = 0
    topics_researched: int = 0
    topic_research_error: str | None = None
    pending_total: int = 0
    skipped_reason: str | None = None
    images_by_renderer: dict[str, int] = Field(default_factory=dict)
    qc_counts: dict[str, int] = Field(default_factory=dict)  # passed / held
    qc_rejected: list[str] = Field(default_factory=list)  # drafts replaced because they failed checks


class LinkedInContentAgent(BaseAgent[ContentInput, ContentOutput]):
    name = "linkedin_content"
    title = "LinkedIn Content Agent"
    responsibilities = [
        "Research timely topics from public discussion (titles and links only)",
        "Draft professional English posts with the local LLM behind a strict quality gate",
        "Fall back to curated templates when the LLM is unavailable or its draft fails checks",
        "Generate hashtags and a branded 1200x627 image for every post",
        "Fill the content calendar slots and queue posts for human approval",
    ]
    input_model = ContentInput
    output_model = ContentOutput

    def execute(self, inp: ContentInput, session: Session) -> ContentOutput:
        cfg = self.settings.section("content")
        out = ContentOutput()

        if inp.research_topics and self.settings.network_enabled:
            try:
                out.topics_researched = self._research_topics(session)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
                # Topics are optional context: neither an error nor a 200 that is not the API's JSON costs the posts.
                out.topic_research_error = f"{type(exc).__name__}: {exc}"[:200]
            session.commit()  # release the SQLite write lock before slow LLM calls

        pending = session.execute(select(func.count(ContentItem.id)).where(
            ContentItem.status.in_(PENDING_STATUSES))).scalar_one()
        capacity = int(cfg.get("max_pending_items", 9)) - pending
        slots = free_slots(session, self.settings)
        n = min(capacity, len(slots), inp.count or int(cfg.get("max_posts_per_generation_run", 3)))
        if n <= 0:
            out.pending_total = pending
            out.skipped_reason = ("Queue full: approve or reject pending posts" if capacity <= 0
                                  else "No free calendar slots in the horizon")
            return out

        briefs = inp.briefs or self._plan_briefs(session) or self._default_briefs()
        recent_fps = set(session.execute(select(ContentItem.fingerprint).where(
            ContentItem.created_at >= utcnow() - timedelta(days=60))).scalars())
        llm = self.ctx.llm
        company = self.settings.company_name
        gen_counts: Counter[str] = Counter()

        for i in range(n):
            brief = briefs[i % len(briefs)]
            hashtags = build_hashtags(brief.service, brief.industry, int(cfg.get("max_hashtags", 5)))
            accepted = None
            for attempt in range(QC_ATTEMPTS):  # a draft that fails quality checks is replaced by another one
                combo = self._unused_combo(brief, recent_fps)
                if combo is None:
                    break
                angle, pain = combo
                fp = f"{brief.service}:{brief.industry}:{angle}:{pain}"
                recent_fps.add(fp)  # never retry the same angle in this run
                draft = write_post(llm, company, brief.service, brief.industry, angle, pain, brief.cta + i + attempt,
                                   int(cfg.get("min_words", 70)), int(cfg.get("max_words", 260)))
                if draft.quality.get("llm_rejected"):
                    out.llm_rejections += 1
                image = render_post_image(self.settings, draft.title, draft.body, brief.service, brief.industry,
                                          seed=len(recent_fps) + i)
                info = {k: image[k] for k in ("renderer", "style", "metrics", "problems", "headline", "points")}
                qc = run_quality_checks(session, self.settings, title=draft.title, body=draft.body, hashtags=hashtags,
                                        image_path=image["path"], image_info=info)
                info["phash"] = qc.pop("image_phash", None)
                if qc["decision"] != "fail":
                    accepted = (fp, draft, image, info, qc)
                    break
                out.qc_rejected.append(f"{draft.title}: {qc_summary(qc)}"[:300])
                image["path"].unlink(missing_ok=True)  # discard the rejected draft's own image
            if accepted is None:
                if out.qc_rejected:
                    alert_once(session, "warning", self.name, f"A post slot stayed empty: drafts failed quality checks "
                               f"({out.qc_rejected[-1][:200]})", self.settings)
                continue
            fp, draft, image, info, qc = accepted
            out.images_by_renderer[image["renderer"]] = out.images_by_renderer.get(image["renderer"], 0) + 1
            out.qc_counts[qc["status"]] = out.qc_counts.get(qc["status"], 0) + 1
            quality = {**draft.quality, "image": info, "qc": qc}
            item = ContentItem(
                campaign_id=brief.campaign_id, platform="linkedin", service=brief.service, industry=brief.industry,
                title=draft.title, body=draft.body, hashtags=hashtags,
                image_path=image["path"].name,
                image_alt=(f"{company} graphic with the headline \"{draft.title}\". Topic: "
                           f"{SERVICES[brief.service].name} for {INDUSTRIES[brief.industry].label}.")[:300],
                scheduled_for=slots[i], generator=draft.generator, quality=quality, fingerprint=fp,
                status="awaiting_approval",  # a person (or, later, an approved policy) must authorize every post
            )
            session.add(item)
            session.commit()  # each finished post is saved before the next LLM call
            recent_fps.add(fp)
            out.created.append(item.id)
            out.scheduled_for.append(slots[i])
            gen_counts[draft.generator] += 1

        out.generator_counts = dict(gen_counts)
        out.pending_total = pending + len(out.created)
        return out

    # ------------------------------------------------------------------
    def _research_topics(self, session: Session) -> int:
        added = 0
        for t in fetch_trending_topics(self.ctx):
            exists = session.execute(select(ResearchTopic.id).where(ResearchTopic.source_url == t["source_url"])).first()
            if exists:
                continue
            session.add(ResearchTopic(**t))
            added += 1
        session.flush()
        return added

    def _plan_briefs(self, session: Session) -> list[ContentBrief]:
        plan = session.execute(select(MarketingPlan).order_by(MarketingPlan.id.desc())).scalars().first()
        return [ContentBrief.model_validate(b) for b in plan.briefs] if plan and plan.briefs else []

    def _default_briefs(self) -> list[ContentBrief]:
        services = self.settings.section("targeting").get("services") or list(SERVICES)
        industries = self.settings.section("targeting").get("industries") or list(INDUSTRIES)
        return [ContentBrief(service=services[i % len(services)], industry=industries[i % len(industries)])
                for i in range(3)]

    @staticmethod
    def _unused_combo(brief: ContentBrief, used: set[str]) -> tuple[int, int] | None:
        n_angles = len(ANGLES[brief.service])
        n_pains = len(INDUSTRIES[brief.industry].pains)
        for k in range(n_angles * n_pains):
            angle = (brief.angle + k) % n_angles
            pain = (brief.pain + k // n_angles) % n_pains
            if f"{brief.service}:{brief.industry}:{angle}:{pain}" not in used:
                return angle, pain
        return None
