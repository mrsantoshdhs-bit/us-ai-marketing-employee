"""Tick-overrun guard (Option B Phase 8): on a synthetic database at 25x today's production volume, every job of the
default schedule (the daily, weekly and interval jobs, in a tick's order, as when a tick catches up on a morning)
finishes within job_budget_minutes on this PC. The budget is read from config/schedule.toml and never relaxed here:
a job that cannot keep it fails this test, and the timings say which.

Seeding and the offline guard come from scripts/perf_bench.py (nothing leaves the PC). Set AIME_PERF_OUT to a folder
to keep the timings as tick_overrun_25x.json there.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import time
from pathlib import Path

from aime.inbox import Mailbox
from aime.scheduler.runner import load_job_specs, run_job

ROOT = Path(__file__).resolve().parents[1]
SCALE = 25
TICK_LIMIT_MINUTES = 60  # Windows Task Scheduler stops a tick after an hour (aime.scheduler.runner, ABANDON_AFTER)


def bench():
    spec = importlib.util.spec_from_file_location("perf_bench", ROOT / "scripts" / "perf_bench.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_default_job_finishes_within_the_job_budget_at_25x_volume(make_ctx, monkeypatch):
    pb = bench()
    pb.offline(monkeypatch.setattr)
    ctx = make_ctx(handler=pb.handler_factory(), **pb.BENCH_SETTINGS)
    seeded = pb.seed(ctx, SCALE, random.Random(20261103 + SCALE))
    pb.load_replies(ctx, seeded["sent_lead_ids"])  # the inbox job reads 20 replies from a fake mailbox
    monkeypatch.setattr("aime.agents.inbox_agent.Mailbox", lambda settings, *a, **k: Mailbox(settings, imap_factory=pb.FakeIMAP))
    budget = float(ctx.settings.schedule.get("job_budget_minutes", 30))
    assert budget == 30  # the configured budget, unchanged

    timings: dict[str, dict] = {}
    for spec in load_job_specs(ctx.settings):  # registry order: the order a tick runs them in
        t0 = time.perf_counter()
        result = run_job(ctx, spec.name)
        timings[spec.name] = {"seconds": round(time.perf_counter() - t0, 2), "status": result["status"], "error": result.get("error")}
    print(json.dumps(timings, indent=1))
    if os.environ.get("AIME_PERF_OUT"):
        out = Path(os.environ["AIME_PERF_OUT"]) / f"tick_overrun_{SCALE}x.json"
        out.write_text(json.dumps({"scale": SCALE, "budget_minutes": budget, "rows": {k: v for k, v in seeded.items() if k != "sent_lead_ids"},
                                   "jobs": timings}, indent=1), encoding="utf-8")
    assert len(timings) == 14  # every job of the default schedule ran
    assert {name: t["status"] for name, t in timings.items() if t["status"] != "success"} == {}
    over = {name: t["seconds"] for name, t in timings.items() if t["seconds"] > budget * 60}
    assert over == {}, f"over the {budget:g}-minute job budget at {SCALE}x: {over}"
    assert sum(t["seconds"] for t in timings.values()) < TICK_LIMIT_MINUTES * 60  # and the whole catching-up tick, too
