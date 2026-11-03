"""The AI conversation engine (aime.ai.conversation, Option B Phase 3): the settings switch, the rule-based facts and
their evidence, the next step, the draft and its policy check, the fallback to the template, the model's optional part,
the handover hook, the approval page, the playbook and claims page, and follow-up context. Synthetic data, no network,
fake models only."""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import settings_store as ss
from aime.agents.sales_workflow import SalesOutput, SalesWorkflowAgent
from aime.ai import conversation as cv
from aime.ai import decisions, playbook
from aime.ai.claims import load as load_claims
from aime.db import kv_set
from aime.llm.base import LLMProvider
from aime.models import AIDecision, AuditEvent, Conversation, ConversationFact, FollowUp, InboundEmail, Lead, User
from aime.web.app import create_app
from aime.web.auth import COOKIE, hash_password, make_cookie, session_secret
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID, FakeIMAP, contacted, mail, run_inbox

ORIGIN = {"origin": "http://testserver"}


class ScriptedLLM(LLMProvider):
    name = "fake"

    def __init__(self, facts=None, polish=None, raw=None):
        self.model = "scripted-1"
        self.facts, self.polish, self.raw = facts, polish, raw
        self.calls = 0

    @property
    def label(self) -> str:
        return "fake:scripted"

    def available(self) -> bool:
        return True

    def generate(self, prompt, system=None, max_tokens=500, temperature=0.7) -> str:
        self.calls += 1
        if self.raw is not None:
            return self.raw
        if "The draft:" in prompt:
            return json.dumps(self.polish or {"body": "x", "confidence": 0.1})
        return json.dumps({"facts": self.facts or [], "upset": False, "asks_for_human": False})


def set_conv(ctx, **values):
    with ctx.db.session() as s:
        ss.save(s, ctx.settings, "conversation", {**ss.get(ctx.settings, "conversation"), **values}, "owner")


def reply(ctx, sent, text, n=1, llm=None):
    ctx.conversation_llm = llm
    FakeIMAP.messages[max(FakeIMAP.messages, default=0) + 1] = mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", text,
                                                                     in_reply_to=sent.message_id, msg_id=f"<t{n}@mail.example>")
    run_inbox(ctx)
    with ctx.db.session() as s:
        row = s.execute(select(InboundEmail).order_by(InboundEmail.id.desc())).scalars().first()
        f = s.get(FollowUp, row.suggested_followup_id) if row.suggested_followup_id else None
        return row, f


def conversation_rows(ctx):
    with ctx.db.session() as s:
        return (s.execute(select(Conversation)).scalars().all(), s.execute(select(ConversationFact)).scalars().all(),
                s.execute(select(AIDecision).where(AIDecision.agent == "conversation")).scalars().all())


# --------------------------------------------------------------------------------------------------- the switch


def test_the_engine_is_off_by_default_and_then_nothing_changes(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    assert "conversation" in ss.SECTIONS and cv.mode(ctx.settings) == "off"
    cfg = ss.get(ctx.settings, "conversation")
    assert (cfg["mode"], cfg["pricing_disclosure"], cfg["max_questions_per_email"], cfg["model_polish"]) == ("off", "never", 2, False)
    row, f = reply(ctx, sent, "Sounds interesting. Could we talk Thursday afternoon?")
    assert row.category == "interested" and f.generator == "template" and "calendly.com/dhs/intro" in f.body
    assert conversation_rows(ctx) == ([], [], [])
    with ctx.db.session() as s:
        with pytest.raises(ValueError):
            ss.save(s, ctx.settings, "conversation", {**cfg, "mode": "autonomous"}, "owner")
        with pytest.raises(ValueError):
            ss.save(s, ctx.settings, "conversation", {**cfg, "max_questions_per_email": 5}, "owner")


def test_a_failing_engine_leaves_the_rules_outcome_alone(make_ctx, monkeypatch):
    ctx, _, sent = contacted(make_ctx)
    set_conv(ctx, mode="drafts")

    def boom(*a, **k):
        raise RuntimeError("engine down")
    monkeypatch.setattr(cv, "on_reply", boom)
    row, f = reply(ctx, sent, "Please remove me from your list.")
    assert row.category == "unsubscribe" and row.status == "actioned"
    with ctx.db.session() as s:
        from aime.crm_ops import is_suppressed
        assert is_suppressed(s, DAVID)
    assert conversation_rows(ctx)[0] == []


# --------------------------------------------------------------------------------------------------- facts by rules


@pytest.mark.parametrize("text,kind,value", [
    ("Our budget is around $25,000 for this.", "budget", "stated: $25,000"),
    ("We could spend up to 15k USD.", "budget", "stated: 15k USD"),
    ("Sadly the budget is frozen until spring.", "budget", "constrained: budget is frozen"),
    ("We need this live by end of Q2.", "timeline", "by end of Q2"),
    ("Ideally within 3 months.", "timeline", "within 3 months"),
    ("I'm the VP of Operations here.", "authority", "says they are the VP of Operations"),
    ("I will need to run it by our CFO first.", "stakeholder", "need to run it by our CFO first"),
    ("We use HubSpot today for the pipeline.", "requirement", "current tools: We use HubSpot today for the pipeline."),
    ("We already work with an agency on this.", "objection", "competitor"),
    ("Can we set up a call next week?", "meeting_intent", "asks to meet"),
    ("We are worried about data security.", "concern", "We are worried about data security."),
])
def test_rule_facts_carry_their_sentence(text, kind, value):
    facts = cv.rule_facts(text)
    hit = [f for f in facts if f.kind == kind]
    assert hit and hit[0].value == value and hit[0].quote == text and hit[0].source == "rule", facts


@pytest.mark.parametrize("text,not_kind", [
    ("We have 12 people in the team.", "budget"),  # a number with no budget word is no budget
    ("I don't want to schedule a call.", "meeting_intent"),
    ("Can we talk next week?", "timeline"),  # when to meet is not the project's timeline
    ("Our budget is tight this year.", "timeline"),
])
def test_rule_facts_do_not_guess(text, not_kind):
    assert not_kind not in {f.kind for f in cv.rule_facts(text)}


@pytest.mark.parametrize("text,lang", [("Hola, nos interesa mucho. ¿Podemos hablar? Gracias", "es"),
                                       ("Hallo, wir haben Interesse. Können wir bitte einen Termin machen? Danke", "de"),
                                       ("Bonjour, nous sommes intéressés, merci pour votre message", "fr"),
                                       ("नमस्ते, हमें इसमें रुचि है।", "hi"), ("Mujhe yeh chahiye, kya aap baat kar sakte hain", "hi"),
                                       ("Yes, let's talk on Thursday. Thanks!", "en"), ("Gracias!", "en")])
def test_language_guess(text, lang):
    assert cv.language_of(text) == lang


# --------------------------------------------------------------------------------------------------- drafts and their checks


def test_drafts_mode_replaces_the_suggestion_with_a_checked_draft_that_still_waits(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    set_conv(ctx, mode="drafts")
    before = len(FakeSMTP.sent)
    row, f = reply(ctx, sent, "Interested. We want to cut the time our team spends on manual data entry.")
    assert f.status == "awaiting_approval" and f.generator == cv.GENERATOR and f.blocked_reason is None
    assert "calendly.com/dhs/intro" in f.body and f.body.count("- ") == 2 and f.purpose == "booking_link"
    assert ctx.settings.require_human_approval is True
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(minutes=30)).isoformat()})
    assert len(FakeSMTP.sent) == before  # nothing goes until a person approves it
    with ctx.db.session() as s:
        from aime import automation as am
        auth, why = am.campaign_for(s, s.get(FollowUp, f.id), "automatic", TUESDAY_11AM_ET)
        assert auth is None and "AI" in why  # no campaign authorization approves an AI draft
        d = s.execute(select(AIDecision).where(AIDecision.input_ref == f"followup:{f.id}")).scalar_one()
        assert d.task == "conversation_draft" and d.verdict == "human_review" and d.proposal["facts_used"]


def test_an_ai_draft_still_waits_when_require_human_approval_is_off(make_ctx):
    ctx, _, sent = contacted(make_ctx, require_human_approval=False)
    set_conv(ctx, mode="drafts")
    _, f = reply(ctx, sent, "Can we set up a call next Tuesday?")
    assert f.status == "awaiting_approval" and f.generator.startswith("AI")


def test_a_blocked_draft_leaves_the_template_suggestion(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    set_conv(ctx, mode="drafts")
    with ctx.db.session() as s:  # written behind the page's back, past its refusal: the policy engine still stops it
        book = {k: {kk: v[kk] for kk in ("enabled", "points", "offers")} for k, v in playbook.load(s).items()}
        book["competitor"]["points"] = ["We will match their price with a 20% discount."]
        kv_set(s, playbook.KV_KEY, book)
    row, f = reply(ctx, sent, "We already work with an agency for this, thanks.")
    assert f.generator == "template" and "discount" not in f.body
    _, _, decs = conversation_rows(ctx)
    draft = [d for d in decs if d.task == "conversation_draft"]
    assert draft and draft[0].verdict == "block" and draft[0].input_ref == f"inbound_email:{row.id}"


def test_shadow_mode_records_everything_and_writes_to_nobody(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    set_conv(ctx, mode="shadow")
    _, f = reply(ctx, sent, "Our budget is around $25,000 for this. How much would it be?")
    convs, facts, decs = conversation_rows(ctx)
    assert convs[0].state == "scheduling" and {"budget", "question"} <= {x.kind for x in facts}
    assert f.generator == "template" and "[Write your answer here]" in f.body
    assert {d.task for d in decs} >= {"conversation_turn", "conversation_draft", "handover_request"}
    assert all(x.evidence_inbound_id and x.evidence_quote for x in facts)


@pytest.mark.parametrize("statement,expect_ai", [
    ("Every project is scoped to what you need, and the Sales Manager can walk you through it.", True),
    ("Projects start from $5,000.", False),
])
def test_the_owners_pricing_statement_still_passes_the_policy(make_ctx, statement, expect_ai):
    ctx, _, sent = contacted(make_ctx)
    set_conv(ctx, mode="drafts", pricing_disclosure="owner_statement", pricing_statement=statement)
    _, f = reply(ctx, sent, "What do you charge for this?")
    assert f.generator.startswith("AI") is expect_ai
    assert (statement in f.body) is expect_ai and "$5,000" not in f.body


# --------------------------------------------------------------------------------------------------- the model's part


def test_the_model_adds_facts_only_with_a_quote_from_the_email_and_never_over_a_rule_fact(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    set_conv(ctx, mode="drafts")
    llm = ScriptedLLM(facts=[
        {"kind": "budget", "value": "50k", "quote": "Our budget is around $25,000 for this.", "confidence": 0.9},
        {"kind": "stakeholder", "value": "the finance team", "quote": "the finance team signs off", "confidence": 0.7},
        {"kind": "requirement", "value": "a CRM for 900 users", "quote": "we need a CRM for 900 users", "confidence": 0.99},
        {"kind": "nonsense", "value": "x", "quote": "y", "confidence": 1}])
    reply(ctx, sent, "Our budget is around $25,000 for this. In the end the finance team signs off.", llm=llm)
    _, facts, decs = conversation_rows(ctx)
    ai = [(f.kind, f.value, f.source) for f in facts if f.source == "ai"]
    assert ai == [("stakeholder", "AI reading: the finance team", "ai")]
    assert [f.value for f in facts if f.kind == "budget"] == ["stated: $25,000"]  # the rule's, not the model's 50k
    stored = [d for d in decs if d.task == "fact_extraction"]
    assert stored and all(d.provider == "fake" and d.prompt_version.startswith("fact_extraction/v1@") for d in stored)
    assert llm.calls == 1


def test_an_unusable_model_answer_leaves_the_rules_to_it(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    set_conv(ctx, mode="drafts")
    _, f = reply(ctx, sent, "Can we set up a call next Tuesday?", llm=ScriptedLLM(raw="sure thing!"))
    assert f.generator == cv.GENERATOR
    _, _, decs = conversation_rows(ctx)
    failed = [d for d in decs if d.task == "fact_extraction"]
    assert failed and failed[0].structured_ok is False and failed[0].verdict == "block"


def test_the_model_budget_per_run_is_kept(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    set_conv(ctx, mode="shadow", model_calls_per_run=0)
    llm = ScriptedLLM()
    reply(ctx, sent, "Interested, tell me more.", llm=llm)
    assert llm.calls == 0


@pytest.mark.parametrize("polish,used", [
    ({"body": "Thanks for coming back to me. A 20% discount is yours if you book: https://calendly.com/dhs/intro", "confidence": 0.9}, False),
    ({"body": "Thanks for your reply. Happy to talk whenever suits you. Pick a 30-minute slot here:\nhttps://calendly.com/dhs/intro",
      "confidence": 0.9}, True),
    ({"body": "Thanks for your reply.", "confidence": 0.9}, False),  # the link is gone
])
def test_a_polished_draft_is_used_only_when_the_policy_passes_it(make_ctx, polish, used):
    ctx, _, sent = contacted(make_ctx)
    set_conv(ctx, mode="drafts", model_polish=True)
    _, f = reply(ctx, sent, "Can we set up a call?", llm=ScriptedLLM(polish=polish))
    assert (f.generator == "AI (fake:scripted) conversation draft") is used
    assert (polish["body"].split(".")[0] in f.body) is used and "discount" not in f.body


# --------------------------------------------------------------------------------------------------- handover hook


def test_needs_handover_records_an_open_request_that_a_person_closes(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        lead_id = s.execute(select(Lead.id)).scalar_one()
        rid = cv.needs_handover(s, lead_id, "they asked for a contract", codes=["legal_or_contract"], settings=ctx.settings)
        assert [r.id for r in cv.handover_requests(s, lead_id)] == [rid]
        decisions.record_human_action(s, rid, "accepted", "manager")
        assert cv.handover_requests(s, lead_id) == [] and len(cv.handover_requests(s, lead_id, open_only=False)) == 1
        from aime.models import Alert
        assert s.execute(select(Alert).where(Alert.source == "conversation")).scalars().first() is not None


# --------------------------------------------------------------------------------------------------- the approval page


def test_the_approval_page_shows_the_reasoning_and_records_what_a_person_did(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    set_conv(ctx, mode="drafts")
    _, f = reply(ctx, sent, "Interested. We want to cut the time our team spends on manual data entry.")
    client = TestClient(create_app(ctx))
    page = client.get("/followups").text
    assert "Why the AI drafted this" in page and "ask discovery" in page
    assert "We want to cut the time our team spends on manual data entry." in page and "<script" not in page
    r = client.post(f"/followups/{f.id}/approve", data={"body": f.body.replace("Thank you", "Thanks")}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303
    with ctx.db.session() as s:
        row = s.get(FollowUp, f.id)
        d = s.execute(select(AIDecision).where(AIDecision.input_ref == f"followup:{f.id}")).scalar_one()
        assert row.status == "approved" and row.generator.startswith("AI conversation draft, edited by")
        assert d.human_action == "edited" and d.edit_distance and d.edit_distance > 0


def test_a_rejected_ai_draft_is_recorded(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    set_conv(ctx, mode="drafts")
    _, f = reply(ctx, sent, "Can we set up a call next Tuesday?")
    TestClient(create_app(ctx)).post(f"/followups/{f.id}/reject", headers=ORIGIN)
    with ctx.db.session() as s:
        assert s.execute(select(AIDecision).where(AIDecision.input_ref == f"followup:{f.id}")).scalar_one().human_action == "rejected"


# --------------------------------------------------------------------------------------------------- playbook and claims page


@pytest.fixture
def admin_web(make_ctx):
    ctx = make_ctx(dashboard_auth=True, network_enabled=False)
    with ctx.db.session() as s:
        for name, role in (("boss", "super_admin"), ("ops", "admin"), ("mgr", "sales_manager")):
            s.add(User(username=name, password_hash=hash_password("pw-" * 4), role=role))
    app = create_app(ctx)

    def client(username):
        c = TestClient(app, follow_redirects=False, headers=ORIGIN)
        with ctx.db.session() as s:
            user = s.execute(select(User).where(User.username == username)).scalar_one()
            c.cookies.set(COOKIE, make_cookie(session_secret(ctx.settings.data_dir), user))
        return c
    return ctx, client


def _playbook_form(**override):
    form = {"playbook_form": "1"}
    for code, e in playbook.DEFAULT_PLAYBOOK.items():
        if e["enabled"]:
            form[f"{code}.enabled"] = "yes"
        form[f"{code}.points"] = "\n".join(e["points"])
        for o in e["offers"]:
            form[f"{code}.offer.{o}"] = "yes"
    form.update(override)
    return form


def test_only_a_super_admin_edits_the_playbook_and_claims_and_every_save_is_checked_and_audited(admin_web):
    ctx, client = admin_web
    boss = client("boss")
    page = boss.get("/admin/settings/conversation")
    assert page.status_code == 200 and "Objection playbook" in page.text and "<script" not in page.text
    assert "script-src 'none'" in page.headers.get("content-security-policy", "")
    for who in ("ops", "mgr"):
        c = client(who)
        assert c.get("/admin/settings/conversation").status_code in (403, 303)
        assert c.post("/admin/settings/conversation/playbook", data=_playbook_form()).status_code in (403, 303)
    r = boss.post("/admin/settings/conversation/playbook", data=_playbook_form(**{"price.points": "We can do 20% off this month."}))
    assert "Not saved" in unquote(r.headers["location"])
    r = boss.post("/admin/settings/conversation/playbook", data=_playbook_form(**{"timing.points": "No problem at all.\nJust say when."}))
    assert "Playbook saved" in unquote(r.headers["location"])
    r = boss.post("/admin/settings/conversation/claims", data={"claims_form": "1", "claims": "We guarantee a 50% saving."})
    assert "Not saved" in unquote(r.headers["location"])
    boss.post("/admin/settings/conversation/claims", data={"claims_form": "1", "claims": "Digital Hub Solution builds software in Austin."})
    with ctx.db.session() as s:
        assert playbook.load(s)["timing"]["points"] == ["No problem at all.", "Just say when."]
        assert playbook.load(s)["timing"]["approved_by"] == "boss"
        assert any(c.text == "Digital Hub Solution builds software in Austin." for c in load_claims(ctx.settings, s)[0])
        actions = {a.action for a in s.execute(select(AuditEvent)).scalars()}
    assert {"ai_playbook_saved", "ai_claims_saved"} <= actions


def test_the_settings_section_page_edits_the_mode(admin_web):
    ctx, client = admin_web
    boss = client("boss")
    page = boss.get("/admin/settings/section/conversation")
    assert page.status_code == 200 and 'name="mode"' in page.text
    form = {"settings_form": "1", "do": "save", "mode": "shadow", "pricing_disclosure": "never", "pricing_statement": "",
            "max_questions_per_email": "2", "model_extraction": "yes", "model_calls_per_run": "3", "approved_languages": "en"}
    boss.post("/admin/settings/section/conversation", data=form)
    assert cv.mode(ctx.settings) == "shadow"


# --------------------------------------------------------------------------------------------------- follow-ups


def _inquiry_lead_with_fact(ctx, quote):
    """A lead whose prospect told us something earlier (a stored fact with its evidence)."""
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalars().first()
        s.add(ConversationFact(lead_id=lead.id, kind="requirement", value=f"problem: {quote}", evidence_inbound_id=1,
                               evidence_quote=quote, confidence=0.75, source="rule", status="open"))
        return lead.id


@pytest.mark.parametrize("mode,quote,expect", [
    ("drafts", "We want to cut the time our team spends on manual data entry.", True),
    ("shadow", "We want to cut the time our team spends on manual data entry.", False),
    ("drafts", "We pay $5k a month for the current tool.", False),  # a price, even theirs, is not repeated by the AI
])
def test_a_later_step_refers_to_what_they_said_only_in_drafts_mode_and_under_the_policy(make_ctx, mode, quote, expect):
    ctx, sender, _ = contacted(make_ctx)
    set_conv(ctx, mode=mode)
    lead_id = _inquiry_lead_with_fact(ctx, quote)
    agent = SalesWorkflowAgent(ctx, sender=sender)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        agent._new_draft(s, lead, 2, TUESDAY_11AM_ET, SalesOutput())
        s.flush()
        f = s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.step == 2)).scalars().first()
        assert f.status == "awaiting_approval"
        assert (f"Last time you mentioned: \"{quote}\"" in f.body) is expect
        assert (f.generator == cv.CONTEXT_GENERATOR) is expect
        assert "$5k" not in f.body
