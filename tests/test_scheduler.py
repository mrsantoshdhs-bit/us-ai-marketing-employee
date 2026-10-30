import xml.etree.ElementTree as ET
from datetime import datetime, time, timedelta, timezone
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
    assert is_due(spec, st, at(10, 23), TZ)[0]  # a tick that started late must not cost the next slot
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


def test_dashboard_task_has_no_time_limit_and_ignores_overlapping_starts(tmp_path):
    from aime.scheduler.windows import DASHBOARD_TASK, build_task_xml
    xml = build_task_xml(Path("C:/py/pythonw.exe"), Path("C:/app/scripts/dashboard_task.py"), Path("C:/app"), 15,
                         "PC\\me", task_name=DASHBOARD_TASK, time_limit="PT0S")
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml and "<MultipleInstancesPolicy>IgnoreNew" in xml
    assert "<LogonTrigger>" in xml and "Password" not in xml.split("<LogonType>")[1].split("<")[0]
    assert "<ExecutionTimeLimit>PT1H</ExecutionTimeLimit>" in build_task_xml(
        Path("C:/py/pythonw.exe"), Path("C:/app/scripts/run_tick.py"), Path("C:/app"), 15, "PC\\me")


def test_dashboard_task_starts_nothing_when_running_or_in_maintenance(tmp_path, monkeypatch):
    import importlib.util
    import socket

    from aime import maintenance
    spec = importlib.util.spec_from_file_location("dashboard_task", Path(__file__).parent.parent / "scripts" / "dashboard_task.py")
    task = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(task)
    task.PAUSE = 0
    started = []
    monkeypatch.setattr("aime.cli.main", lambda argv: started.append(argv) or 0)
    monkeypatch.setenv("AIME_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIME_ENV_FILE", str(tmp_path / "none.env"))
    with socket.socket() as srv:  # something already answers on the dashboard port
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        monkeypatch.setenv("DASHBOARD_PORT", str(srv.getsockname()[1]))
        assert task.main() == 0 and started == []
    maintenance.start(tmp_path, hours=1, reason="deploy")
    assert task.main() == 0 and started == []
    maintenance.stop(tmp_path)
    # a restart: the old dashboard still answers once while it stops, then it is gone; this run must take over
    answers = iter([True, False])
    monkeypatch.setattr(task, "listening", lambda host, port: next(answers, False))
    assert task.main() == 0 and started == [["serve"]]
    started.clear()
    assert task.main() == 0 and started == [["serve"]]


def test_watchdog_reports_a_stopped_dashboard():
    import importlib.util
    import socket
    spec = importlib.util.spec_from_file_location("watchdog", Path(__file__).parent.parent / "scripts" / "watchdog.py")
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        assert "not running" in wd.dashboard_problem(port=port)  # bound but not listening: refused
        srv.listen()
        assert wd.dashboard_problem(port=port) is None


def test_daily_failures_count_per_slot_not_across_days(ctx, monkeypatch):
    from aime.timeutil import utcnow
    with ctx.db.session() as s:  # yesterday: three failed attempts, then gave up
        s.add(JobState(job="research", last_attempt_at=utcnow() - timedelta(days=1, hours=1), last_status="failed",
                       consecutive_failures=3, enabled=True))
    monkeypatch.setitem(runner.JOBS, "research", lambda ctx, job="research": (_ for _ in ()).throw(RuntimeError("down")))
    assert run_job(ctx, "research")["status"] == "failed"
    with ctx.db.session() as s:
        assert s.get(JobState, "research").consecutive_failures == 1  # today's slot keeps its two retries


def test_a_job_whose_result_cannot_be_recorded_does_not_stop_the_tick(ctx, monkeypatch):
    from sqlalchemy.exc import OperationalError

    real = runner._record

    def locked(ctx, name, *a, **k):
        if name == "db_maintenance":
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        return real(ctx, name, *a, **k)

    monkeypatch.setattr(runner, "_record", locked)
    monkeypatch.setattr(runner._time, "sleep", lambda _s: None)
    result = tick(ctx, trigger="test")
    ran = {r["job"]: r["status"] for r in result["ran"]}
    assert ran["db_maintenance"] == "failed" and ran["monitor"] == "success"


def test_research_recheck_waits_for_a_running_enrich_job(ctx, capsys):
    from aime.cli import cmd_research_recheck
    from aime.models import Company
    from aime.timeutil import utcnow
    with ctx.db.session() as s:
        s.add(Company(name="Acme", normalized_name="acme", source="test", researched_at=utcnow()))
    lock = FileLock(ctx.settings.locks_dir / "job_enrich.lock")
    assert lock.acquire()
    try:
        class Args:
            company = None
        assert cmd_research_recheck(Args(), ctx) == 1
    finally:
        lock.release()
    assert "enrich job is running" in capsys.readouterr().out


def test_dashboard_task_gives_a_console_less_process_somewhere_to_write(tmp_path, monkeypatch):
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("dashboard_task", Path(__file__).parent.parent / "scripts" / "dashboard_task.py")
    task = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(task)
    monkeypatch.setattr(sys, "stdout", None)  # what pythonw.exe gives the process
    monkeypatch.setattr(sys, "stderr", None)
    task.ensure_output(tmp_path / "logs" / "dashboard.log")
    print("Uvicorn running")
    sys.stderr.write("INFO: started\n")
    sys.stdout.close()
    assert "Uvicorn running" in (tmp_path / "logs" / "dashboard.log").read_text(encoding="utf-8")


def _watchdog():
    import importlib.util
    spec = importlib.util.spec_from_file_location("watchdog", Path(__file__).parent.parent / "scripts" / "watchdog.py")
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    return wd


class FakeClock:
    """time.monotonic and time.sleep without waiting; `on_sleep` runs at each pause."""

    def __init__(self, on_sleep=None):
        self.now, self.sleeps, self.on_sleep = 0.0, 0, on_sleep

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)


def test_watchdog_gives_a_dashboard_that_is_still_starting_time_to_answer():
    """At sign-in the Watchdog and Dashboard tasks start together; a dashboard that comes up within the grace
    period is not reported as stopped (it used to be, two seconds after it started)."""
    import socket
    wd = _watchdog()
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        fake = FakeClock(on_sleep=lambda n: n == 2 and srv.listen())  # starts answering after the second pause
        assert wd.dashboard_problem(port=port, wait_seconds=wd.DASHBOARD_GRACE_SECONDS, sleep=fake.sleep,
                                    clock=fake.clock) is None
        assert fake.sleeps == 2


def test_watchdog_still_reports_a_dashboard_that_never_answers():
    import socket
    wd = _watchdog()
    assert wd.DASHBOARD_GRACE_SECONDS >= 120  # longer than a slow start right after sign-in
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        fake = FakeClock()
        problem = wd.dashboard_problem(port=port, wait_seconds=60, sleep=fake.sleep, clock=fake.clock)
        assert "not running" in problem and "after waiting 60 s" in problem
        assert fake.sleeps == 60 // wd.DASHBOARD_RECHECK_SECONDS
