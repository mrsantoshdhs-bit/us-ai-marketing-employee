import xml.etree.ElementTree as ET
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from aime.models import Alert, JobRun, JobState
from aime.scheduler import runner
from aime.scheduler.runner import FileLock, JobSpec, is_due, load_job_specs, run_job, tick
from aime.scheduler.windows import build_task_xml
from aime.workflows import JOBS, JobOutcome

TZ = ZoneInfo("America/New_York")


def at(h, m=0, day=27):
    return datetime(2026, 10, day, h, m, tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)


def test_specs_loaded_from_config(ctx):
    names = [s.name for s in load_job_specs(ctx.settings)]
    assert names == list(JOBS)


def test_interval_due_logic():
    spec = JobSpec("inbound", 30, None, "")
    assert is_due(spec, None, at(10), TZ)[0]
    st = JobState(job="inbound", last_attempt_at=at(10), consecutive_failures=0, enabled=True)
    assert not is_due(spec, st, at(10, 15), TZ)[0]
    assert is_due(spec, st, at(10, 29), TZ)[0]  # within tick slack
    st.enabled = False
    assert not is_due(spec, st, at(11), TZ)[0]


def test_daily_due_catch_up_and_backoff():
    spec = JobSpec("research", None, time(7, 0), "")
    assert is_due(spec, None, at(6), TZ)[0]  # yesterday's slot never ran: catch up
    done = JobState(job="research", last_success_at=at(7, 1), last_attempt_at=at(7, 1), consecutive_failures=0, enabled=True)
    assert not is_due(spec, done, at(12), TZ)[0]
    assert is_due(spec, done, at(7, 5, day=28), TZ)[0]  # next day
    failed = JobState(job="research", last_success_at=at(7, day=26), last_attempt_at=at(7, 1),
                      consecutive_failures=1, enabled=True)
    assert not is_due(spec, failed, at(7, 10), TZ)[0]  # backoff
    assert is_due(spec, failed, at(7, 40), TZ)[0]
    failed.consecutive_failures = 3
    assert not is_due(spec, failed, at(9), TZ)[0]  # give up until the next slot


def test_file_lock(tmp_path):
    a, b = FileLock(tmp_path / "x.lock"), FileLock(tmp_path / "x.lock")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_tick_runs_due_jobs_and_records_state(ctx):
    result = tick(ctx, trigger="test")
    ran = {r["job"]: r["status"] for r in result["ran"]}
    assert set(ran) == set(JOBS)
    assert all(v == "success" for v in ran.values()), result
    with ctx.db.session() as s:
        assert s.execute(select(JobRun)).scalars().all()
        assert all(st.last_success_at for st in s.execute(select(JobState)).scalars())
    assert (ctx.settings.data_dir / "heartbeat.json").exists()
    second = tick(ctx, trigger="test")
    assert second["ran"] == []  # nothing due right after


def test_tick_lock_prevents_overlap(ctx):
    lock = FileLock(ctx.settings.locks_dir / "tick.lock")
    assert lock.acquire()
    try:
        assert tick(ctx)["status"] == "skipped"
    finally:
        lock.release()


def test_failures_counted_and_monitor_alerts(ctx, monkeypatch):
    def broken(ctx, job="publish"):
        raise RuntimeError("simulated outage")

    monkeypatch.setitem(runner.JOBS, "publish", broken)
    for _ in range(2):
        assert run_job(ctx, "publish")["status"] == "failed"
    with ctx.db.session() as s:
        assert s.get(JobState, "publish").consecutive_failures == 2
    assert run_job(ctx, "monitor")["status"] == "success"
    with ctx.db.session() as s:
        msgs = [a.message for a in s.execute(select(Alert)).scalars()]
    assert any("publish" in m and "2 times" in m for m in msgs)


def test_partial_status(ctx, monkeypatch):
    def half(ctx, job="x"):
        out = JobOutcome()
        out.steps = [{"agent": "a", "status": "success"}, {"agent": "b", "status": "failed", "error": "x"}]
        out.failed = True
        return out

    monkeypatch.setitem(runner.JOBS, "content", half)
    assert run_job(ctx, "content")["status"] == "partial"


def test_task_xml_is_valid():
    xml = build_task_xml(Path(r"C:\p\pythonw.exe"), Path(r"C:\p\scripts\run_tick.py"), Path(r"C:\p"), 15, "PC\\me",
                         start=datetime(2026, 10, 25, 8, 0))
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:Interval", ns).text == "PT15M"
    assert root.find(".//t:Command", ns).text.endswith("pythonw.exe")
    assert root.find(".//t:LogonType", ns).text == "InteractiveToken"
    assert root.find(".//t:StartWhenAvailable", ns).text == "true"
