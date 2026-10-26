import json

from pydantic import BaseModel
from sqlalchemy import select

from aime.agents import AGENTS
from aime.agents.base import BaseAgent, RetryPolicy, TransientAgentError
from aime.models import AgentRun


class In(BaseModel):
    n: int


class Out(BaseModel):
    doubled: int


class FlakyAgent(BaseAgent[In, Out]):
    name = "flaky"
    title = "Flaky"
    responsibilities = ["test"]
    input_model = In
    output_model = Out
    retry_policy = RetryPolicy(max_attempts=3, base_delay=0)

    def __init__(self, ctx, fail_times=0, exc=TransientAgentError, bad_output=False):
        super().__init__(ctx)
        self.fail_times, self.exc, self.bad_output, self.calls = fail_times, exc, bad_output, 0

    def execute(self, inp, session):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc("boom")
        if self.bad_output:
            return {"wrong": 1}
        return Out(doubled=inp.n * 2)


def test_retry_then_success_is_logged(ctx):
    res = FlakyAgent(ctx, fail_times=2).run({"n": 4}, job="t")
    assert res.ok and res.output.doubled == 8 and res.attempts == 3
    with ctx.db.session() as s:
        row = s.execute(select(AgentRun).where(AgentRun.run_id == res.run_id)).scalars().one()
        assert row.status == "success" and row.attempts == 3 and row.output_summary["doubled"] == 8
    lines = (ctx.settings.logs_dir / "agent_runs.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[-1])["status"] == "success"


def test_retries_exhausted(ctx):
    res = FlakyAgent(ctx, fail_times=5).run({"n": 1})
    assert not res.ok and res.attempts == 3 and "boom" in res.error


def test_non_retryable_error_stops_immediately(ctx):
    agent = FlakyAgent(ctx, fail_times=5, exc=ValueError)
    res = agent.run({"n": 1})
    assert not res.ok and res.attempts == 1 and agent.calls == 1


def test_input_contract_violation(ctx):
    res = FlakyAgent(ctx).run({"n": "not a number"})
    assert not res.ok and res.attempts == 0 and "Invalid input" in res.error
    with ctx.db.session() as s:
        assert s.execute(select(AgentRun).where(AgentRun.run_id == res.run_id)).scalars().one().status == "failed"


def test_output_contract_violation(ctx):
    res = FlakyAgent(ctx, bad_output=True).run({"n": 1})
    assert not res.ok and "ValidationError" in res.error


def test_team_has_nine_documented_agents():
    assert len(AGENTS) == 9
    for cls in AGENTS.values():
        d = cls.describe()
        assert d["responsibilities"] and d["input"] is not None and d["output"] and d["retry_policy"]
