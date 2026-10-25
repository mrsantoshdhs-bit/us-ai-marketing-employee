"""Command line interface: python -m aime <command>."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from sqlalchemy import func, select

from aime.config import BASE_DIR, Settings
from aime.context import create_context, set_context


def _print(obj, quiet: bool = False) -> None:
    if quiet:
        return
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(obj)


def cmd_init(args, ctx) -> int:
    env = BASE_DIR / ".env"
    if not env.exists():
        shutil.copy(BASE_DIR / ".env.example", env)
        print(f"Created {env} from .env.example")
    print(f"Database: {ctx.settings.database_url}")
    print(f"Data folder: {ctx.settings.data_dir}")
    return 0


def cmd_status(args, ctx) -> int:
    from aime.integrations.status import integration_statuses
    from aime.models import Company, ContentItem, FollowUp, Lead
    from aime.scheduler import windows
    from aime.scheduler.runner import due_jobs

    with ctx.db.session() as s:
        rows = integration_statuses(ctx, s)
        counts = {
            "companies": s.execute(select(func.count(Company.id)).where(Company.merged_into_id.is_(None))).scalar_one(),
            "leads": s.execute(select(func.count(Lead.id))).scalar_one(),
            "qualified_leads": s.execute(select(func.count(Lead.id)).where(Lead.status == "qualified")).scalar_one(),
            "content_items": s.execute(select(func.count(ContentItem.id))).scalar_one(),
            "followups_awaiting_approval": s.execute(select(func.count(FollowUp.id)).where(
                FollowUp.status == "awaiting_approval")).scalar_one(),
        }
    print("Integrations:")
    for r in rows:
        print(f"  [{r['state']}] {r['name']}: {r['detail']}")
    print("Counts:", json.dumps(counts))
    from aime import maintenance

    window = maintenance.active(ctx.settings.data_dir)
    print("Maintenance:", f"ACTIVE until {window['until']} ({window['reason']})" if window else "off")
    print("Jobs:")
    for spec, due, reason in due_jobs(ctx):
        print(f"  {spec.name:<16} {spec.schedule_label:<22} {'DUE' if due else 'waiting'}: {reason}")
    task = windows.query()
    print("Windows task:", "installed" if task.get("installed") else "not installed",
          {k: task.get(k) for k in ("Status", "Last Run Time", "Last Result", "Next Run Time") if k in task})
    return 0


def cmd_run(args, ctx) -> int:
    from aime.scheduler.runner import run_job

    result = run_job(ctx, args.job, trigger=args.trigger)
    _print(result)
    return 0 if result["status"] in ("success", "skipped") else 1


def cmd_tick(args, ctx) -> int:
    from aime.scheduler.runner import tick

    result = tick(ctx, trigger=args.trigger)
    _print(result, args.quiet)
    # Job failures are retried with backoff and raised as alerts; exit non-zero only if the tick crashed.
    return 0


def cmd_serve(args, ctx) -> int:
    import uvicorn

    from aime.web.app import create_app

    host = args.host or ctx.settings.dashboard_host
    port = args.port or ctx.settings.dashboard_port
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Refusing to bind to a non-local address. The dashboard is a local tool; see docs/DEPLOYMENT.md.")
        return 2
    uvicorn.run(create_app(ctx), host=host, port=port, log_level="info")
    return 0


def cmd_scheduler(args, ctx) -> int:
    from aime.scheduler import windows

    if args.action == "verify":
        return cmd_scheduler_verify(ctx)
    if args.action == "install":
        res = windows.install(ctx.settings, args.interval)
    elif args.action == "uninstall":
        res = windows.uninstall()
    elif args.action == "run":
        res = windows.run_now()
    else:
        res = windows.query()
    _print(res)
    return 0 if res.get("ok", res.get("installed", False)) else 1


def cmd_llm_check(args, ctx) -> int:
    from aime.content import write_post

    llm = ctx.llm
    print("LLM:", llm.label if llm else "not available (template writer)")
    draft = write_post(llm, ctx.settings.company_name, args.service, args.industry, 0, 0, 0)
    print(f"Generator: {draft.generator}\nQuality: {json.dumps(draft.quality)}\n\n{draft.title}\n\n{draft.body}")
    return 0


def cmd_inbound(args, ctx) -> int:
    from aime.inbound import record_inquiry

    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else [data]
    created = 0
    with ctx.db.session() as s:
        for rec in records:
            _, ok = record_inquiry(s, rec, "manual")
            created += int(ok)
    print(f"Recorded {created} new inquiry(ies). They are processed by the 'inbound' job.")
    return 0


def cmd_suppress(args, ctx) -> int:
    from aime.crm_ops import suppress

    with ctx.db.session() as s:
        suppress(s, args.value, args.reason)
    print(f"Suppressed {args.value}")
    return 0


def cmd_seed_demo(args, ctx) -> int:
    """Build a separate demo database from fictional sample data (never mixed with real data)."""
    import httpx

    from aime.inbound import record_inquiry
    from aime.sample_data import SAMPLE_HN_ITEMS, SAMPLE_HN_THREAD, SAMPLE_INBOUND
    from aime.scheduler.runner import run_job

    def handler(request: httpx.Request) -> httpx.Response:
        if "search_by_date" in request.url.path:
            return httpx.Response(200, json={"hits": [SAMPLE_HN_THREAD]})
        if "/items/" in request.url.path:
            return httpx.Response(200, json={"children": [{**i, "type": "comment"} for i in SAMPLE_HN_ITEMS]})
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"hits": []})
        return httpx.Response(404)

    demo_dir = Path(args.data_dir).resolve()
    if demo_dir == PRODUCTION_DATA_DIR.resolve():
        print("Refusing to write sample data into the production data folder.")
        return 2
    settings = Settings.load(data_dir=demo_dir, database_url="", llm_provider=args.llm, retry_base_delay=0,
                             polite_delay=0, data_role="demo")
    demo = create_context(settings, http_transport=httpx.MockTransport(handler))
    with demo.db.session() as s:
        for rec in SAMPLE_INBOUND:
            record_inquiry(s, rec, "web_form")
    for job in ("plan", "research", "inbound", "content", "publish", "crm_maintenance", "daily_report"):
        r = run_job(demo, job, trigger="cli")
        print(f"{job:<16} {r['status']}")
    print(f"Demo data written to {demo_dir}. View it with: python -m aime serve --data-dir \"{demo_dir}\" --port 8766")
    return 0


def cmd_doctor(args, ctx) -> int:
    from aime.doctor import run_checks

    checks = run_checks(ctx)
    for c in checks:
        mark = "PASS" if c["ok"] else ("FAIL" if c["severity"] == "error" else "WARN")
        print(f"[{mark}] {c['name']}: {c['detail']}")
    return 1 if any(not c["ok"] and c["severity"] == "error" for c in checks) else 0


def cmd_backup(args, ctx) -> int:
    from aime.backup import backup_sqlite_file

    path = backup_sqlite_file(Path(ctx.db.engine.url.database), ctx.settings.backups_dir, label=args.label)
    print(f"Backup written: {path}")
    return 0


def cmd_maintenance(args, ctx) -> int:
    from aime import maintenance

    if args.action == "on":
        print(maintenance.start(ctx.settings.data_dir, args.hours, args.reason))
    elif args.action == "off":
        print("Maintenance window ended" if maintenance.stop(ctx.settings.data_dir) else "No maintenance window was active")
    else:
        print(maintenance.active(ctx.settings.data_dir) or "Not in maintenance")
    return 0


def cmd_refresh_graphics(args, ctx) -> int:
    """Re-render images for posts that are not published yet (e.g. after a brand change)."""
    import uuid

    from aime.models import ContentItem
    from aime.visuals import render_card

    count = 0
    with ctx.db.session() as s:
        for item in s.execute(select(ContentItem).where(
                ContentItem.status.in_(("draft", "awaiting_approval", "approved", "failed")))).scalars():
            path = render_card(ctx.settings.assets_dir / f"post_{uuid.uuid4().hex[:10]}.png", item.title,
                               item.service, item.industry, ctx.settings.company_name,
                               ctx.settings.business["company"].get("tagline") or None)
            item.image_path, item.image_urn = path.name, None
            count += 1
    print(f"Re-rendered {count} image(s) with brand '{ctx.settings.company_name}'")
    return 0


def cmd_scheduler_verify(ctx) -> int:
    """Verify the OS task exists, its last result is 0, and recent ticks reached the database."""
    from datetime import datetime, timedelta

    from aime.models import JobRun
    from aime.monitoring import read_heartbeat
    from aime.scheduler import windows
    from aime.timeutil import utcnow

    problems = []
    task = windows.query()
    if not task.get("installed"):
        problems.append("Windows task is not installed")
    else:
        if task.get("Last Result") not in ("0", "267009"):  # 267009 = currently running
            problems.append(f"Last Result is {task.get('Last Result')}")
        if task.get("Scheduled Task State", "Enabled") != "Enabled":
            problems.append("Task is disabled")
    hb = read_heartbeat(ctx.settings)
    tick = int(ctx.settings.schedule.get("tick_minutes", 15))
    if not hb:
        problems.append("No heartbeat")
    else:
        age = utcnow() - datetime.fromisoformat(hb["at"].rstrip("Z"))
        if age > timedelta(minutes=tick * 2 + 5):
            problems.append(f"Heartbeat is {int(age.total_seconds() // 60)} min old")
    with ctx.db.session() as s:
        recent = s.execute(select(JobRun).where(JobRun.trigger == "scheduler", JobRun.started_at >= utcnow() - timedelta(days=1))
                           .order_by(JobRun.id.desc())).scalars().all()
    failed = [r for r in recent if r.status not in ("success", "skipped")]
    print(f"Task: {'installed' if task.get('installed') else 'missing'} | status={task.get('Status')} | "
          f"last run={task.get('Last Run Time')} | last result={task.get('Last Result')} | next={task.get('Next Run Time')}")
    print(f"Heartbeat: {hb}")
    print(f"Scheduler-triggered job runs in 24h: {len(recent)} ({len(failed)} not successful)")
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aime", description="Autonomous US AI Marketing Employee")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="Create data folders, database, and .env")
    sub.add_parser("status", help="Show integrations, job schedule, and counts")
    r = sub.add_parser("run", help="Run one job now")
    r.add_argument("job")
    r.add_argument("--trigger", default="cli")
    t = sub.add_parser("tick", help="Run all due jobs once (what the OS scheduler calls)")
    t.add_argument("--trigger", default="cli")
    t.add_argument("--quiet", action="store_true")
    sv = sub.add_parser("serve", help="Start the local dashboard")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.add_argument("--data-dir", help="Serve another data folder (e.g. the demo database)")
    sc = sub.add_parser("scheduler", help="Manage the Windows Task Scheduler task")
    sc.add_argument("action", choices=["install", "uninstall", "status", "run", "verify"])
    sc.add_argument("--interval", type=int)
    sub.add_parser("doctor", help="Integrity, isolation, compliance and scheduler self-checks")
    bk = sub.add_parser("backup", help="Write an online backup of the database")
    bk.add_argument("--label", default="manual")
    mt = sub.add_parser("maintenance", help="Pause scheduled ticks for upgrades (always expires)")
    mt.add_argument("action", choices=["on", "off", "status"])
    mt.add_argument("--hours", type=float, default=2.0)
    mt.add_argument("--reason", default="Manual maintenance")
    sub.add_parser("refresh-graphics", help="Re-render images of unpublished posts (e.g. after a brand change)")
    lc = sub.add_parser("llm-check", help="Generate one sample post with the configured LLM")
    lc.add_argument("--service", default="ai_agents")
    lc.add_argument("--industry", default="real_estate")
    ib = sub.add_parser("inbound", help="Record inbound inquiries from a JSON file")
    ib.add_argument("file")
    su = sub.add_parser("suppress", help="Add an email or domain to the do-not-contact list")
    su.add_argument("value")
    su.add_argument("--reason", default="Requested by contact")
    sd = sub.add_parser("seed-demo", help="Build a separate demo database from fictional sample data")
    sd.add_argument("--data-dir", default=str(BASE_DIR / "demo_data"))
    sd.add_argument("--llm", default="template", choices=["template", "auto", "ollama"])
    return p


COMMANDS = {
    "init": cmd_init, "status": cmd_status, "run": cmd_run, "tick": cmd_tick, "serve": cmd_serve,
    "scheduler": cmd_scheduler, "llm-check": cmd_llm_check, "inbound": cmd_inbound, "suppress": cmd_suppress,
    "seed-demo": cmd_seed_demo, "doctor": cmd_doctor, "backup": cmd_backup, "maintenance": cmd_maintenance,
    "refresh-graphics": cmd_refresh_graphics,
}
PRODUCTION_DATA_DIR = BASE_DIR / "data"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "seed-demo":
        return cmd_seed_demo(args, None)
    data_dir = getattr(args, "data_dir", None)
    settings = None
    if data_dir:
        resolved = Path(data_dir).resolve()
        role = "production" if resolved == PRODUCTION_DATA_DIR.resolve() else "demo"
        settings = Settings.load(data_dir=resolved, database_url="", data_role=role)
    ctx = create_context(settings)
    set_context(ctx)
    return COMMANDS[args.cmd](args, ctx)


if __name__ == "__main__":
    sys.exit(main())
