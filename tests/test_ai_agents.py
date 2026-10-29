"""The AI employees: what the language model did in each run (calls, tokens as the model reported them, time and
cost), an administrator's switch per agent, and reply drafts whose personal opening the local model may write,
checked so that no price, number, link or promise gets through and labelled with who wrote what."""

from datetime import timedelta

import httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from aime.agents.base import BaseAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.ai_drafts import opening
from aime.llm.base import LLMError
from aime.llm.ollama import OllamaProvider
from aime.models import AgentRun, FollowUp
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.workflows import JobOutcome, job_inbound
from tests.test_dashboard_auth import ORIGIN
from tests.test_inbound_sales import TUESDAY_11AM_ET
from tests.test_linkedin_leads import SENDING, FakeLinkedIn, leads_ctx, ms
from tests.test_pilot_test_mode import MAILBOX, form_lead


class Empty(BaseModel):
    pass


class AskTheModel(BaseAgent[Empty, Empty]):
    name, title, responsibilities = "ask_the_model", "Test agent", ["asks the model once"]
    input_model = output_model = Empty

    def execute(self, inp, session):
        self.ctx.llm.generate("hello")
        return Empty()


def ollama(answer: str, prompt_tokens=12, completion_tokens=5) -> OllamaProvider:
    body = {"response": answer, "prompt_eval_count": prompt_tokens, "eval_count": completion_tokens}
    return OllamaProvider("http://127.0.0.1:11434", "llama3.2", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))


def test_each_run_records_what_the_model_did_and_what_it_cost(make_ctx):
    ctx = make_ctx(llm_cost_per_1k_tokens=2.0)
    ctx._llm, ctx._llm_checked = ollama("Hi there", 1200, 300), True
    assert AskTheModel(ctx).run({}).ok
    with ctx.db.session() as s:
        row = s.execute(select(AgentRun).where(AgentRun.agent == "ask_the_model")).scalar_one()
        assert (row.llm_calls, row.llm_tokens, row.llm_cost_usd) == (1, 1500, 3.0)
        assert row.llm_ms >= 0


def test_an_administrator_switches_one_agent_off_and_its_job_does_not_fail(make_ctx):
    ctx = make_ctx()
    c = TestClient(create_app(ctx))
    c.post("/agents/sales_workflow/switch", headers=ORIGIN, data={"on": ""})
    result = SalesWorkflowAgent(ctx).run({})
    assert result.status == "skipped" and not result.ok
    outcome = JobOutcome()
    outcome.add(result)
    assert not outcome.failed  # switched off is not a failure: no failure alerts
    with ctx.db.session() as s:
        assert s.execute(select(AgentRun).where(AgentRun.agent == "sales_workflow")).scalar_one().status == "skipped"
    page = c.get("/agents").text
    assert "switched off" in page and "Switch on" in page
    c.post("/agents/sales_workflow/switch", headers=ORIGIN, data={"on": "yes"})
    assert SalesWorkflowAgent(ctx).run({}).status == "success"


class Writer:
    label = "ollama:test"

    def __init__(self, answer):
        self.answer = answer

    def generate(self, prompt, system=None, max_tokens=500, temperature=0.7):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_the_model_writes_one_checked_sentence_or_nothing():
    good = "Thanks for telling us how your agents lose track of leads between spreadsheets."
    assert opening(Writer(good), "Ann", "Brightpath", "Custom CRM Development", "we lose leads") == good
    for bad in ("We can do it for $5,000 with a 20% discount.", "Our team guarantees results.",
                "Thanks for reaching out. We will call you tomorrow.", "Visit https://example.com for more.",
                "Hi Ann, thanks for your message.", "Short.", LLMError("down")):
        assert opening(Writer(bad), "Ann", "Brightpath", "Custom CRM Development", "x") is None, bad
    assert opening(None, "Ann", "Brightpath", "CRM", "x") is None


def lead_with_message(make_ctx, **overrides):
    fake = FakeLinkedIn([form_lead("t1", "Ann", "ann.lead@brightpath-realty.example", "Brightpath Realty Group", "TX")])
    ctx = leads_ctx(make_ctx, fake, **SENDING, **MAILBOX, **overrides)
    return ctx


def test_a_reply_draft_says_whether_the_model_wrote_its_opening(make_ctx):
    good = "Thanks for telling us how your agents lose track of follow-ups between spreadsheets."
    ctx = lead_with_message(make_ctx, ai_email_drafts=True)
    ctx._llm, ctx._llm_checked = Writer(good), True
    job_inbound(ctx)
    SalesWorkflowAgent(ctx).run({"now": TUESDAY_11AM_ET.isoformat()})
    with ctx.db.session() as s:
        f = s.execute(select(FollowUp).where(FollowUp.channel == "email")).scalar_one()
        assert f.generator == "AI (ollama:test) opening + template" and good in f.body and f.status == "awaiting_approval"
    ctx2 = lead_with_message(make_ctx, ai_email_drafts=True)
    ctx2._llm, ctx2._llm_checked = Writer("We can start next week at a 10% discount."), True
    job_inbound(ctx2)
    SalesWorkflowAgent(ctx2).run({"now": TUESDAY_11AM_ET.isoformat()})
    with ctx2.db.session() as s:
        f = s.execute(select(FollowUp).where(FollowUp.channel == "email")).scalar_one()
        assert f.generator == "template" and "discount" not in f.body  # rejected: the template's own words stay
    assert ms(utcnow() - timedelta(days=1))  # the fake LinkedIn helper is imported for its lead shape
