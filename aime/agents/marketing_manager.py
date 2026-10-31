"""Marketing Manager Agent: sets daily focus, keeps a monthly campaign active, and briefs the content agent."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent
from aime.agents.contracts import ContentBrief
from aime.db import kv_get, kv_set
from aime.knowledge.content_bank import ANGLES, CTAS
from aime.knowledge.geo import STATE_PRIORITY, normalize_state
from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES
from aime.metrics_import import latest_metric_ids
from aime.models import (Campaign, Company, ContentItem, ContentMetric, InboundEmail, Lead, MarketingPlan, MarketSegment,
                         ResearchTopic)
from aime.timeutil import local_tz, utcnow


class PlanInput(BaseModel):
    plan_date: date | None = None
    states_per_run: int | None = Field(default=None, ge=1, le=50)
    industries_per_run: int | None = Field(default=None, ge=1, le=8)


class PlanOutput(BaseModel):
    plan_id: int
    plan_date: date
    focus_states: list[str]
    focus_industries: list[str]
    briefs: list[ContentBrief]
    campaign_id: int | None
    rationale: list[str]


class MarketingManagerAgent(BaseAgent[PlanInput, PlanOutput]):
    name = "marketing_manager"
    title = "Marketing Manager Agent"
    responsibilities = [
        "Rotate research focus across all 50 states and the 8 target industries",
        "Weight focus toward industries producing higher-scoring leads (exploit) while still exploring",
        "Keep one monthly campaign active and close expired ones",
        "Produce content briefs (service x industry x angle) for the LinkedIn Content Agent",
    ]
    input_model = PlanInput
    output_model = PlanOutput

    def execute(self, inp: PlanInput, session: Session) -> PlanOutput:
        s = self.settings
        targeting = s.section("targeting")
        research_cfg = s.section("research")
        tz = local_tz(s.schedule.get("timezone"))
        today = inp.plan_date or datetime.now(tz).date()
        rationale: list[str] = []

        # One plan a day: another run (a manual "Run now", a retry) must not move the rotation on and skip states.
        existing = session.execute(select(MarketingPlan).where(MarketingPlan.plan_date == today)
                                   .order_by(MarketingPlan.id.desc())).scalars().first()
        if existing:
            return PlanOutput(plan_id=existing.id, plan_date=today, focus_states=existing.focus_states,
                              focus_industries=existing.focus_industries,
                              briefs=[ContentBrief.model_validate(b) for b in existing.briefs or []],
                              campaign_id=existing.campaign_id,
                              rationale=[*(existing.rationale or []), "Today's plan already exists; kept as it is."])

        industries = [i for i in targeting.get("industries", []) if i in INDUSTRIES] or list(INDUSTRIES)
        services = [x for x in targeting.get("services", []) if x in SERVICES] or list(SERVICES)
        states_cfg = targeting.get("states", "ALL")
        states = list(STATE_PRIORITY) if states_cfg == "ALL" else [st for st in (normalize_state(x) for x in states_cfg) if st]

        # ---- State rotation, ordered by Census establishment counts when available
        market = dict(session.execute(
            select(MarketSegment.state, func.sum(MarketSegment.establishments))
            .where(MarketSegment.industry.in_(industries)).group_by(MarketSegment.state)).all())
        if market:
            states.sort(key=lambda st: -(market.get(st) or 0))
            order_note = "ordered by Census establishment counts for target industries"
        else:
            order_note = "ordered by business population (no Census data loaded)"
        n_states = min(inp.states_per_run or int(research_cfg.get("states_per_run", 5)), len(states))
        cursor = int(kv_get(session, "plan.state_cursor", 0))
        focus_states = [states[(cursor + i) % len(states)] for i in range(n_states)]
        kv_set(session, "plan.state_cursor", (cursor + n_states) % len(states))
        rationale.append(f"States {', '.join(focus_states)}: next in the {len(states)}-state rotation, {order_note}.")

        # ---- Industry focus: one exploration pick + best performer
        since = utcnow() - timedelta(days=30)
        perf_rows = session.execute(
            select(Company.industry, func.avg(Lead.score), func.count(Lead.id))
            .join(Lead, Lead.company_id == Company.id)
            .where(Lead.created_at >= since, Lead.last_scored_at.is_not(None))
            .group_by(Company.industry)).all()
        perf = {ind: (float(avg or 0), int(n)) for ind, avg, n in perf_rows if ind in industries}
        n_ind = min(inp.industries_per_run or int(research_cfg.get("industries_per_run", 2)), len(industries))
        ind_cursor = int(kv_get(session, "plan.industry_cursor", 0))
        focus_industries = [industries[ind_cursor % len(industries)]]
        kv_set(session, "plan.industry_cursor", (ind_cursor + 1) % len(industries))
        rationale.append(f"Exploring {focus_industries[0]} (industry rotation).")
        # What real prospects did comes before what the score thinks of them.
        answered = dict(session.execute(
            select(Company.industry, func.count(InboundEmail.lead_id.distinct()))
            .join(Lead, Lead.company_id == Company.id).join(InboundEmail, InboundEmail.lead_id == Lead.id)
            .where(InboundEmail.is_simulated.is_(False), InboundEmail.category.in_(("interested", "question")),
                   InboundEmail.received_at >= utcnow() - timedelta(days=90))
            .group_by(Company.industry)).all())
        ranked_perf = sorted(((answered.get(k, 0), v[0], k) for k, v in perf.items() if v[1] >= 3 or answered.get(k)), reverse=True)
        for replies, avg, ind in ranked_perf:
            if len(focus_industries) >= n_ind:
                break
            if ind not in focus_industries:
                focus_industries.append(ind)
                rationale.append(f"Exploiting {ind}: {replies} interested prospect(s) in 90 days." if replies
                                 else f"Exploiting {ind}: highest average lead score in 30 days ({avg:.0f}).")
        k = 1
        while len(focus_industries) < n_ind:
            nxt = industries[(ind_cursor + k) % len(industries)]
            if nxt not in focus_industries:
                focus_industries.append(nxt)
            k += 1

        # ---- Service ranking for content: least recently used, boosted by trends and engagement
        last_used = dict(session.execute(
            select(ContentItem.service, func.max(ContentItem.created_at)).group_by(ContentItem.service)).all())
        trend = dict(session.execute(
            select(ResearchTopic.service, func.count(ResearchTopic.id))
            .where(ResearchTopic.observed_at >= utcnow() - timedelta(days=14), ResearchTopic.service.is_not(None))
            .group_by(ResearchTopic.service)).all())
        engagement = dict(session.execute(
            select(ContentItem.service, func.avg(ContentMetric.reactions + 2 * ContentMetric.comments))
            .join(ContentMetric, ContentMetric.content_id == ContentItem.id)
            .where(ContentMetric.id.in_(latest_metric_ids())).group_by(ContentItem.service)).all())
        epoch = datetime(2000, 1, 1)

        def svc_key(svc: str):
            return (last_used.get(svc) or epoch, -(trend.get(svc) or 0), -(engagement.get(svc) or 0))

        ranked_services = sorted(services, key=svc_key)
        posts = int(s.section("content").get("posts_per_week", 3))
        # Trend only breaks a tie between services equally due, so the plan says so only where it changed which service a
        # brief is about: this run's order against the order without it.
        untrended = sorted(services, key=lambda svc: (last_used.get(svc) or epoch, -(engagement.get(svc) or 0)))
        for used_trend, without in zip(ranked_services[:posts], untrended[:posts], strict=False):
            if used_trend != without:
                rationale.append(f"Trending public discussion favours {used_trend} ({trend.get(used_trend, 0)} recent stories).")
                break

        # ---- Campaign
        campaign = self._ensure_campaign(session, today, ranked_services[0], focus_industries[0], rationale)

        # ---- Briefs
        briefs: list[ContentBrief] = []
        for i in range(posts):
            svc = ranked_services[i % len(ranked_services)]
            ind = focus_industries[i % len(focus_industries)]
            used = session.execute(select(func.count(ContentItem.id)).where(
                ContentItem.service == svc, ContentItem.industry == ind)).scalar_one()
            briefs.append(ContentBrief(
                service=svc, industry=ind, angle=used % len(ANGLES[svc]), pain=(cursor + i) % 3,
                cta=(ind_cursor + i) % len(CTAS), campaign_id=campaign.id if campaign else None,
                reason="least recently featured service" if svc not in last_used else "service rotation",
            ))

        plan = MarketingPlan(plan_date=today, focus_industries=focus_industries, focus_states=focus_states,
                             briefs=[b.model_dump() for b in briefs], rationale=rationale,
                             campaign_id=campaign.id if campaign else None)
        session.add(plan)
        session.flush()
        return PlanOutput(plan_id=plan.id, plan_date=today, focus_states=focus_states,
                          focus_industries=focus_industries, briefs=briefs,
                          campaign_id=campaign.id if campaign else None, rationale=rationale)

    def _ensure_campaign(self, session: Session, today: date, service: str, industry: str,
                         rationale: list[str]) -> Campaign | None:
        for old in session.execute(select(Campaign).where(Campaign.status == "active", Campaign.end_date < today)).scalars():
            old.status = "completed"
            rationale.append(f"Closed expired campaign '{old.name}'.")
        active = session.execute(select(Campaign).where(
            Campaign.status == "active", Campaign.start_date <= today, Campaign.end_date >= today)).scalars().first()
        if active:
            return active
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        posts = int(self.settings.section("content").get("posts_per_week", 3))
        camp = Campaign(
            name=f"{start:%b %Y} · {SERVICES[service].name} for {INDUSTRIES[industry].label}",
            objective=f"Build awareness and inbound inquiries for {SERVICES[service].name} among US {INDUSTRIES[industry].audience}",
            service=service, industry=industry, states=[], channel="linkedin", start_date=start, end_date=end,
            status="active", kpis={"target_posts": posts * 4},
        )
        session.add(camp)
        session.flush()
        rationale.append(f"Started campaign '{camp.name}'.")
        return camp
