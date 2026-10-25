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

    if args.during_maintenance:
        print("WARNING: running during the maintenance window (use only to verify a deployment)")
    result = run_job(ctx, args.job, trigger=args.trigger, allow_during_maintenance=args.during_maintenance)
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
        res = windows.install(ctx.settings, args.interval, run_when_logged_off=args.run_when_logged_off)
    elif args.action == "restore-previous":
        res = windows.restore_previous(ctx.settings)
    elif args.action == "install-watchdog":
        res = windows.install_watchdog(ctx.settings)
    elif args.action == "uninstall-watchdog":
        res = windows.uninstall(windows.WATCHDOG_TASK)
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
    from aime.backup import backup_sqlite_file, verify_backup, write_offsite

    db_path = Path(ctx.db.engine.url.database)
    path = backup_sqlite_file(db_path, ctx.settings.backups_dir, label=args.label)
    check = verify_backup(path)
    print(f"Backup written: {path}")
    print(f"Verified: {'ok' if check['ok'] else 'FAILED'} ({check['integrity']}, {check['tables']} tables, "
          f"{check['rows']} rows)")
    if args.offsite:
        result = write_offsite(ctx.settings, db_path)
        print("Off-machine copy:", result.get("file") or result.get("reason"))
        if result["status"] != "ok":
            return 1
    return 0 if check["ok"] else 1


def _ask_password(prompt_fn) -> str | None:
    first = prompt_fn("New password (at least 12 characters): ")
    second = prompt_fn("Repeat the password: ")
    if first != second:
        print("The two passwords do not match; nothing changed.")
        return None
    return first


def cmd_user(args, ctx, prompt_fn=None) -> int:
    """Dashboard accounts. Passwords are typed by you at the prompt and stored only as scrypt hashes."""
    import getpass

    from aime.models import User
    from aime.web.auth import hash_password

    prompt_fn = prompt_fn or getpass.getpass
    with ctx.db.session() as s:
        if args.action == "list":
            for u in s.execute(select(User).order_by(User.id)).scalars():
                print(f"{u.username:<20} {u.role:<15} {'active' if u.active else 'disabled':<9} "
                      f"last sign-in: {u.last_login_at or '-'}")
            return 0
        username = (args.username or "").strip().lower()
        if not username:
            print("--username is required")
            return 1
        user = s.execute(select(User).where(User.username == username)).scalars().first()
        if args.action == "create":
            if user:
                print(f"User {username} already exists")
                return 1
            password = _ask_password(prompt_fn)
            if password is None:
                return 1
            try:
                s.add(User(username=username, password_hash=hash_password(password), role=args.role))
            except ValueError as exc:
                print(exc)
                return 1
            print(f"Created {args.role} account {username}. Sign in at http://127.0.0.1:8765/login")
            return 0
        if user is None:
            print(f"No user {username}")
            return 1
        if args.action == "set-password":
            password = _ask_password(prompt_fn)
            if password is None:
                return 1
            try:
                user.password_hash = hash_password(password)
            except ValueError as exc:
                print(exc)
                return 1
            user.session_version += 1  # signs out every existing session
            user.failed_logins, user.locked_until = 0, None
            print(f"Password changed for {username}; existing sessions were signed out")
        elif args.action == "disable":
            user.active = False
            user.session_version += 1
            print(f"Disabled {username}")
        elif args.action == "enable":
            user.active = True
            print(f"Enabled {username}")
    return 0


def cmd_backup_verify(args, ctx) -> int:
    from aime.backup import verify_backup

    check = verify_backup(Path(args.file))
    print(f"{args.file}: {'ok' if check['ok'] else 'FAILED'} ({check['integrity']}, {check['tables']} tables, "
          f"{check['rows']} rows)")
    return 0 if check["ok"] else 1


def cmd_restore_drill(args, ctx) -> int:
    """Restore a backup into a temporary folder and check it; the live data folder is never written."""
    from aime.backup import BackupError, restore_drill

    try:
        result = restore_drill(ctx.settings, Path(args.source) if args.source else None,
                               Path(args.extract_to) if args.extract_to else None)
    except BackupError as exc:
        print(f"Restore drill FAILED: {exc}")
        return 1
    print(f"Restore drill {'PASSED' if result['ok'] else 'FAILED'}: {result.get('source')} — {result['detail']}"
          + (f" in {result['seconds']} s" if "seconds" in result else ""))
    return 0 if result["ok"] else 1


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
    """Re-render images for posts that are not published yet (e.g. after a brand change). Old files are kept."""
    from aime.models import ContentItem
    from aime.visuals import render_post_image

    if args.renderer:
        ctx.settings.image_renderer = args.renderer
    counts: dict[str, int] = {}
    with ctx.db.session() as s:
        for item in s.execute(select(ContentItem).where(
                ContentItem.status.in_(("draft", "awaiting_approval", "approved", "failed")))).scalars():
            image = render_post_image(ctx.settings, item.title, item.body, item.service, item.industry, seed=item.id)
            item.image_path, item.image_urn = image["path"].name, None
            item.quality = {**(item.quality or {}),
                            "image": {k: image[k] for k in ("renderer", "style", "metrics", "problems")}}
            counts[f"{image['renderer']}:{image['style']}"] = counts.get(f"{image['renderer']}:{image['style']}", 0) + 1
            s.commit()  # each image is saved as soon as it is rendered
    print(f"Re-rendered {sum(counts.values())} image(s) with brand '{ctx.settings.company_name}': {counts}")
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
        if task.get("Logon Mode", "").lower().startswith("interactive only"):
            print("NOTE: the task runs only while you are logged on. To keep it running when you sign out:"
                  " `python -m aime scheduler install --run-when-logged-off` (Windows asks for your password).")
    if not windows.query(windows.WATCHDOG_TASK).get("installed"):
        print("NOTE: no watchdog task. Install it with `python -m aime scheduler install-watchdog`.")
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
    r.add_argument("--during-maintenance", action="store_true",
                   help="Run even while a maintenance window is active (deployment verification only)")
    t = sub.add_parser("tick", help="Run all due jobs once (what the OS scheduler calls)")
    t.add_argument("--trigger", default="cli")
    t.add_argument("--quiet", action="store_true")
    sv = sub.add_parser("serve", help="Start the local dashboard")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.add_argument("--data-dir", help="Serve another data folder (e.g. the demo database)")
    sc = sub.add_parser("scheduler", help="Manage the Windows Task Scheduler task")
    sc.add_argument("action", choices=["install", "uninstall", "status", "run", "verify", "restore-previous",
                                       "install-watchdog", "uninstall-watchdog"])
    sc.add_argument("--interval", type=int)
    sc.add_argument("--run-when-logged-off", action="store_true",
                    help="Keep running while signed out and after reboot (Windows prompts for your password)")
    sub.add_parser("doctor", help="Integrity, isolation, compliance and scheduler self-checks")
    bk = sub.add_parser("backup", help="Write and verify an online backup of the database")
    bk.add_argument("--label", default="manual")
    bk.add_argument("--offsite", action="store_true", help="Also write an encrypted copy to BACKUP_OFFSITE_DIR")
    bv = sub.add_parser("backup-verify", help="Check a backup file's integrity and table counts (read-only)")
    bv.add_argument("file")
    rd = sub.add_parser("restore-drill", help="Restore a backup into a temp folder and check it (never touches data/)")
    rd.add_argument("--source", help="Backup .db, bundle .zip or encrypted .aimebak (default: latest local backup)")
    rd.add_argument("--extract-to", help="New folder (outside data/) to receive the checked files for a manual restore")
    mt = sub.add_parser("maintenance", help="Pause scheduled ticks for upgrades (always expires)")
    mt.add_argument("action", choices=["on", "off", "status"])
    mt.add_argument("--hours", type=float, default=2.0)
    mt.add_argument("--reason", default="Manual maintenance")
    rg = sub.add_parser("refresh-graphics", help="Re-render images of unpublished posts (e.g. after a brand change)")
    rg.add_argument("--renderer", choices=["auto", "html", "pillow"], help="Override IMAGE_RENDERER for this run")
    rg.add_argument("--data-dir", help="Use another data folder (e.g. the demo database)")
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
    us = sub.add_parser("user", help="Dashboard sign-in accounts (you type the password at the prompt)")
    us.add_argument("action", choices=["create", "list", "set-password", "disable", "enable"])
    us.add_argument("--username")
    us.add_argument("--role", default="sales_manager", choices=["sales_manager", "admin"])
    us.add_argument("--data-dir", help="Use another data folder (e.g. the demo database)")
    return p


COMMANDS = {
    "init": cmd_init, "status": cmd_status, "run": cmd_run, "tick": cmd_tick, "serve": cmd_serve,
    "scheduler": cmd_scheduler, "llm-check": cmd_llm_check, "inbound": cmd_inbound, "suppress": cmd_suppress,
    "seed-demo": cmd_seed_demo, "doctor": cmd_doctor, "backup": cmd_backup, "maintenance": cmd_maintenance,
    "refresh-graphics": cmd_refresh_graphics, "backup-verify": cmd_backup_verify, "restore-drill": cmd_restore_drill,
    "user": cmd_user,
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
