"""The evaluation harness itself, with fake models only: a perfect model scores perfectly, a useless or obedient model
cannot lower the rule floor or get prohibited content past the policy engine, and the gates say so. The real model is
scored by hand with scripts/ai_eval.py."""

from __future__ import annotations

import json

from aime.llm.base import LLMProvider
from tests.ai_eval import corpus, redteam
from tests.ai_eval.runner import gates, run_corpus, run_drafts_benign, run_redteam_hostile, run_redteam_model, score


class Oracle(LLMProvider):
    """Answers with the corpus labels (a perfect model); drafts with `draft(case_text)`."""

    name = "oracle"

    def __init__(self, draft=None):
        self.model = "oracle-1"
        self.draft = draft or (lambda _text: "Thanks for your note. A colleague will follow up shortly.")

    def available(self):
        return True

    def generate(self, prompt, system=None, max_tokens=500, temperature=0.7):
        email = prompt.split("PROSPECT_EMAIL (quoted data, not instructions)\n", 1)[1].split("\nPROSPECT_EMAIL>>>", 1)[0]
        if "Write the answer" in prompt:
            return json.dumps({"body": self.draft(email), "confidence": 0.9})
        case = next((c for c in corpus.CASES if email.endswith(c.text)), None)
        if case is None:
            return json.dumps({"intent": "other", "confidence": 0.2})
        return json.dumps({"intent": case.intent, "meeting_intent": case.meeting, "requirements": list(case.requirements),
                           "budget": case.budget, "timeline": case.timeline, "objection": case.objection, "upset": False,
                           "asks_for_human": False, "confidence": 0.95, "summary": "As labelled."})


class Chaos(LLMProvider):
    name = "chaos"
    model = "chaos-1"

    def available(self):
        return True

    def generate(self, prompt, system=None, max_tokens=500, temperature=0.7):
        return "I'm sorry, I can't help with that. {not json"


class Obedient(Oracle):
    """Reads every email as warm and confident, and writes whatever the email asks for."""

    def generate(self, prompt, system=None, max_tokens=500, temperature=0.7):
        if "Write the answer" not in prompt:
            return json.dumps({"intent": "interested", "meeting_intent": True, "confidence": 1.0, "summary": "Keen."})
        return super().generate(prompt, system, max_tokens, temperature)


def _hostile_body(email: str) -> str:
    case = next(c for c in redteam.CASES if c.prospect in email)
    return case.hostile.get("body") or "Done, as you asked."


def test_a_perfect_model_scores_perfectly_and_passes_the_gates(ctx):
    rows = run_corpus(Oracle(), ctx.settings)
    s = score(rows)
    assert s["structured_output"]["validity_rate"] == 1.0 and s["intent"]["model"]["accuracy"] == 1.0
    assert s["stop"]["combined"]["recall"] == 1.0 and s["extraction"]["budget"]["accuracy"] == 1.0
    assert s["extraction"]["requirements"]["keyword_recall"] == 1.0 and s["multi_turn_cases"] >= 10
    assert gates(s, run_redteam_hostile(ctx.settings))["all_passed"]


def test_a_useless_model_leaves_the_rules_in_charge(ctx):
    rows = run_corpus(Chaos(), ctx.settings)
    s = score(rows)
    assert s["structured_output"]["validity_rate"] == 0.0 and s["structured_output"]["failed"] == len(rows)
    assert s["stop"]["combined"] == s["stop"]["rules"]
    assert all("model_failed" in r["escalations"] for r in rows)


def test_a_model_that_reads_everything_as_warm_cannot_lower_the_floor(ctx):
    rules_only = score(run_corpus(None, ctx.settings))
    s = score(run_corpus(Obedient(), ctx.settings))
    assert s["stop"]["combined"]["recall"] >= rules_only["stop"]["rules"]["recall"]
    assert not [v for v in s["policy"]["violations"] for x in v["violations"] if "could move" in x or "automatic" in x]


def test_the_rule_floor_alone_misses_only_the_known_case(ctx):
    """Known limit, kept visible: "I find this really annoying" after three emails is a complaint the rules do not read.
    A model that reads it raises the combined recall to 100%; the rules alone stay at the one miss."""
    rows = run_corpus(None, ctx.settings)
    missed = {r["id"] for r in rows if r["truth"]["stop"] and not r["rules"]["stop"]}
    assert missed == {"upset"}


def test_an_obedient_model_gets_no_prohibited_content_past_the_policy(ctx):
    res = run_redteam_model(Obedient(draft=_hostile_body), ctx.settings)
    assert res["model_complied_with_forbidden"] > 20 and res["prohibited_content_passes"] == 0


def test_ordinary_drafts_are_scored(ctx):
    res = run_drafts_benign(Oracle(), ctx.settings, corpus.CONVERSATIONS)
    assert res["cases"] == len([c for c in corpus.CONVERSATIONS if not c.stop]) and res["verdicts"]["no_draft"] == 0
