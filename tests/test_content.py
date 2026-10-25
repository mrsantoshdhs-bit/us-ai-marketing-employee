from datetime import timezone
from zoneinfo import ZoneInfo

from PIL import Image
from sqlalchemy import select

from aime.agents import LinkedInContentAgent, MarketingManagerAgent
from aime.content import build_hashtags, compose_template, quality_check, sanitize, write_post
from aime.knowledge.content_bank import ANGLES
from aime.knowledge.industries import INDUSTRIES
from aime.knowledge.services import SERVICES
from aime.models import Campaign, ContentItem, ResearchTopic
from tests.conftest import FakeLLM

GOOD_LLM_POST = (
    "Most sales teams don't need more software. They need fewer manual steps between the tools they already have.\n\n"
    "When a deal moves, someone usually copies the update into the CRM, a spreadsheet, and a chat thread. "
    "That repetition is where details slip and follow-ups get missed, and it quietly eats into selling time.\n\n"
    "• Log activity to the CRM automatically\n• Trigger the next step when a deal changes stage\n"
    "• Keep a person in charge of anything that needs judgment\n\n"
    "Good automation should feel invisible to the team and obvious in the results they see each week.\n\n"
    "Where does your team lose the most time between tools?"
)


def test_every_template_combination_passes_quality_gate():
    for svc in SERVICES:
        for ind in INDUSTRIES:
            for angle in range(len(ANGLES[svc])):
                for pain in range(3):
                    d = compose_template(svc, ind, angle, pain, angle + pain)
                    q = quality_check(d.body)
                    assert q["passed"], (svc, ind, angle, pain, q["problems"])
                    assert "{" not in d.body


def test_quality_gate_rejects_fabricated_claims():
    body = GOOD_LLM_POST.replace("Most sales teams", "Our clients saw a 300% increase. Most sales teams")
    q = quality_check(body)
    assert not q["passed"] and any("percentage" in p for p in q["problems"])


def test_sanitize_strips_preamble_hashtags_emoji():
    raw = "Here's a LinkedIn post for you:\n\n🚀 Great hook\n\nBody text #AI\n\n#Automation #CRM"
    out = sanitize(raw)
    assert "Here's" not in out and "#" not in out and "🚀" not in out
    assert sanitize("Intro\n• • • Personalization\n- * Speed\n* Accuracy\n**Tip:** keep it") == \
        "Intro\n• Personalization\n• Speed\n• Accuracy\nTip: keep it"


def test_llm_used_when_draft_passes_and_template_when_not():
    good = write_post(FakeLLM([GOOD_LLM_POST]), "Co", "sales_automation", "b2b_sales", 0, 0, 0)
    assert good.generator == "fake:test" and good.quality["passed"]
    bad_llm = FakeLLM(["Guaranteed 10x growth for 500 companies! #win"])
    bad = write_post(bad_llm, "Co", "sales_automation", "b2b_sales", 0, 0, 0)
    assert bad.generator == "template" and bad.quality["llm_rejected"] and bad_llm.calls == 2


def test_hashtags_unique_and_capped():
    tags = build_hashtags("ai_agents", "real_estate", 5)
    assert len(tags) <= 5 and len(set(tags)) == len(tags) and all(t.startswith("#") for t in tags)


def test_content_agent_fills_calendar_with_images_for_approval(ctx):
    plan = MarketingManagerAgent(ctx).run({})
    assert plan.ok and len(plan.output.briefs) == 3 and len(plan.output.focus_states) == 5
    res = LinkedInContentAgent(ctx).run({})
    assert res.ok, res.error
    assert len(res.output.created) == 3 and res.output.topics_researched == 1
    et = ZoneInfo("America/New_York")
    with ctx.db.session() as s:
        items = s.execute(select(ContentItem)).scalars().all()
        assert {i.status for i in items} == {"awaiting_approval"}
        slots = [i.scheduled_for for i in items]
        assert len(set(slots)) == 3
        for i in items:
            local = i.scheduled_for.replace(tzinfo=timezone.utc).astimezone(et)
            assert local.strftime("%a %H:%M") in {"Tue 09:30", "Wed 09:30", "Thu 09:30"}
            img = Image.open(ctx.settings.assets_dir / i.image_path)
            assert img.size == (1200, 627)
            assert i.campaign_id is not None
        assert s.execute(select(Campaign)).scalars().one().status == "active"
        assert s.execute(select(ResearchTopic)).scalars().one().service == "ai_agents"


def test_content_agent_respects_pending_cap(make_ctx):
    ctx = make_ctx()
    ctx.settings.business["content"]["max_pending_items"] = 2
    first = LinkedInContentAgent(ctx).run({"research_topics": False}).output
    assert len(first.created) == 2
    second = LinkedInContentAgent(ctx).run({"research_topics": False}).output
    assert second.created == [] and "Queue full" in second.skipped_reason
