"""Command line interface: python -m aime <command>."""

from __future__ import annotations

import argparse
import json
import os
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


class PathOnlyAccessLog:
    """Logging filter for uvicorn's access log: the path without its query string. Queries can carry sign-in codes
    (OAuth callbacks), one-time setup links and messages with email addresses, which do not belong in a log file."""

    def filter(self, record) -> bool:
        args = getattr(record, "args", None)
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def cmd_serve(args, ctx) -> int:
    import logging

    import uvicorn

    from aime.web.app import create_app

    host = args.host or ctx.settings.dashboard_host
    port = args.port or ctx.settings.dashboard_port
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Refusing to bind to a non-local address. The dashboard is a local tool; see docs/DEPLOYMENT.md.")
        return 2
    logging.getLogger("uvicorn.access").addFilter(PathOnlyAccessLog())
    # proxy_headers off: nothing sits in front of this local dashboard, so X-Forwarded-For is never trusted (it
    # would let any local program pick the address the inbound API's rate limit counts)
    uvicorn.run(create_app(ctx), host=host, port=port, log_level="info", proxy_headers=False)
    return 0


def cmd_scheduler(args, ctx) -> int:
    from aime.scheduler import windows

    if args.action == "verify":
        return cmd_scheduler_verify(ctx)
    if args.action == "preflight":
        findings = windows.preflight(ctx.settings)
        for level, text in findings:
            print(f"[{level}] {text}")
        return 1 if any(level == "FAIL" for level, _ in findings) else 0
    if args.action == "test-unattended":
        res = windows.test_unattended(ctx.settings)
        _print(res)
        return 0 if res.get("ok") else 1
    if args.action == "install":
        res = windows.install(ctx.settings, args.interval, run_when_logged_off=args.run_when_logged_off, wake=args.wake)
    elif args.action == "restore-previous":
        res = windows.restore_previous(ctx.settings)
    elif args.action == "install-watchdog":
        res = windows.install_watchdog(ctx.settings)
    elif args.action == "uninstall-watchdog":
        res = windows.uninstall(windows.WATCHDOG_TASK)
    elif args.action == "install-dashboard":
        res = windows.install_dashboard(ctx.settings)
    elif args.action == "uninstall-dashboard":
        res = windows.uninstall(windows.DASHBOARD_TASK)
    elif args.action == "uninstall":
        res = windows.uninstall()
    elif args.action == "run":
        res = windows.run_now()
    else:
        res = windows.query()
    _print(res)
    return 0 if res.get("ok", res.get("installed", False)) else 1


def cmd_automation(args, ctx) -> int:
    """Stop switches from the command line, for when the dashboard is not running. Starting and resuming stay on
    the dashboard, for an administrator."""
    from aime import automation as am

    with ctx.db.session() as s:
        if args.action != "status":
            action = {"pause": "pause", "stop": "stop", "emergency-stop": "emergency_stop"}[args.action]
            try:
                am.change_state(s, action, "command line", args.reason)
            except ValueError as exc:
                print(exc)
                return 1
        print(f"Automation: {am.STATES[am.get_state(s)]} · mode {am.get_mode(s)} · "
              f"{len(am.active_authorizations(s))} active campaign authorization(s)")
    return 0


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


def cmd_erase(args, ctx) -> int:
    """A person asked us to delete their data: erase it and keep the address only on the suppression list."""
    from aime.crm_ops import erase_person

    if not args.confirm:
        print("This permanently erases the person's details, inquiries and emails. Run again with --confirm.")
        return 2
    with ctx.db.session() as s:
        try:
            counts = erase_person(s, args.email, "owner (command line)")
        except ValueError as exc:
            print(exc)
            return 2
    print("Erased: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items())
          + ". The address stays on the suppression list so nothing is sent to it again.")
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
        if result.get("file"):
            from aime.backup import offsite_sync
            from aime.db import kv_set
            from aime.timeutil import utcnow

            with ctx.db.session() as s:  # doctor and the monitor job read this, as for the daily copy
                kv_set(s, "backup.last_offsite", {"file": result["file"], "at": utcnow().isoformat()})
            print("Off-machine folder:", offsite_sync(ctx.settings.backup_offsite_dir)[1])
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


def cmd_email_check(args, ctx) -> int:
    """Sales mailbox readiness: settings, SPF/DKIM/DMARC, SMTP and read-only IMAP sign-in, optional test email. A
    mailbox on the Mailboxes page is checked in place of the .env mail settings, as sending and reading use it."""
    from aime import mail_accounts
    from aime.email_check import panel_mailbox_check, run_email_check

    settings, first = ctx.settings, []
    with ctx.db.session() as s:
        box = mail_accounts.primary(s) or next(iter(mail_accounts.readable(s)), None)
        if box is not None:
            settings, first = mail_accounts.settings_for(ctx.settings, box), [panel_mailbox_check(box)]
    checks = first + run_email_check(settings, connect=args.connect, send=args.send_test, domain=args.domain)
    for c in checks:
        print(f"[{c.status.upper():<4}] {c.name}: {c.detail}")
    failed = [c for c in checks if c.status == "fail"]
    print(f"{len(failed)} problem(s) to fix" if failed else "No blocking problems")
    return 1 if failed else 0


def cmd_user(args, ctx, prompt_fn=None) -> int:
    """Dashboard accounts. Passwords are typed by you at the prompt and stored only as scrypt hashes."""
    import getpass

    from aime.crm_ops import log_activity
    from aime.models import User
    from aime.permissions import all_roles
    from aime.web.auth import hash_password

    prompt_fn = prompt_fn or getpass.getpass
    with ctx.db.session() as s:
        if args.action == "list":
            for u in s.execute(select(User).order_by(User.id)).scalars():
                print(f"{u.username:<20} {u.role:<15} {'active' if u.active else 'disabled':<9} "
                      f"last sign-in: {u.last_login_at or '-'}")
            return 0
        username = (args.username or getattr(args, "name", None) or "").strip().lower()
        if not username:
            print("--username is required")
            return 1
        user = s.execute(select(User).where(User.username == username)).scalars().first()
        role = getattr(args, "role", None) or "sales_manager"
        roles = all_roles(s)  # the built-in roles and the custom ones defined on Administration › Roles
        if args.action in ("create", "set-role") and getattr(args, "role", None) and role not in roles:
            print(f"Unknown role {role}: choose one of {', '.join(roles)}")
            return 1
        if args.action == "create":
            if user:
                print(f"User {username} already exists")
                return 1
            password = _ask_password(prompt_fn)
            if password is None:
                return 1
            try:
                s.add(User(username=username, password_hash=hash_password(password), role=role))
            except ValueError as exc:
                print(exc)
                return 1
            print(f"Created {role} account {username}. Sign in at http://127.0.0.1:8765/login")
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
        elif args.action == "set-role":
            # Only from the command line on this computer: the dashboard cannot change roles.
            if not getattr(args, "role", None):
                print(f"--role is required ({', '.join(roles)})")
                return 1
            old, user.role = user.role, args.role
            log_activity(s, f"Dashboard account {username}: role {old} -> {args.role}", "command line", type_="security")
            print(f"{username} is now {args.role} (was {old})")
    return 0


def cmd_research_recheck(args, ctx) -> int:
    """Re-read companies' own websites now (e.g. after a research rule fix), then re-score them. Contacts no one."""
    from aime.agents.company_research import CompanyResearchAgent
    from aime.agents.qualification import LeadQualificationAgent
    from aime.models import Company

    with ctx.db.session() as s:
        ids = list(args.company or s.execute(select(Company.id).where(Company.researched_at.is_not(None))
                                              .order_by(Company.id)).scalars())
    if not ids:
        print("No researched companies to re-check")
        return 0
    from aime.scheduler.runner import FileLock

    lock = FileLock(ctx.settings.locks_dir / "job_enrich.lock")  # the scheduled research job uses the same lock
    if not lock.acquire():
        print("The enrich job is running now; run research-recheck again when it has finished.")
        return 1
    try:
        res = CompanyResearchAgent(ctx).run({"company_ids": ids, "limit": len(ids)}, job="research_recheck")
        if not res.ok:
            print(f"Research re-check failed: {res.error}")
            return 1
        o = res.output
        if o.skipped:
            print(f"Skipped: {o.skipped}")
            return 1
        print(f"Re-checked {o.companies_researched} company(ies): {o.people_found} decision-maker(s) named, "
              f"{o.contacts_withdrawn} withdrawn, {len(o.competitors)} marked as possible competitors, "
              f"{len(o.failed)} failed")
        LeadQualificationAgent(ctx).run({}, job="research_recheck")
    finally:
        lock.release()
    return 0


def cmd_backup_verify(args, ctx) -> int:
    from aime.backup import verify_backup

    check = verify_backup(Path(args.file))
    print(f"{args.file}: {'ok' if check['ok'] else 'FAILED'} ({check['integrity']}, {check['tables']} tables, "
          f"{check['rows']} rows)")
    return 0 if check["ok"] else 1


def cmd_simulate_reply(args, ctx) -> int:
    """A clearly labelled SIMULATED reply from a lead's contact, processed like a real one (for testing)."""
    import uuid

    from aime.models import FollowUp, InboundEmail, Lead
    from aime.reply_intel import process_pending

    if ctx.settings.is_production and not os.environ.get("AIME_ENV_FILE"):
        print("simulate-reply refused on live production data: a simulated reply would change real leads. Use a demo "
              "folder or an acceptance copy (AIME_ENV_FILE, see docs/ACCEPTANCE.md).")
        return 2
    with ctx.db.session() as s:
        lead = s.get(Lead, args.lead)
        if lead is None:
            print(f"No lead #{args.lead}")
            return 1
        last = s.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email")
                         .order_by(FollowUp.id.desc())).scalars().first()
        sender = args.from_email or (last.to_email if last and last.to_email else None) or (lead.contact.email if lead.contact else None)
        if not sender:
            print("This lead has no email to reply from; pass --from-email")
            return 1
        subject = f"Re: {last.subject}" if last else "Re: your email"
        s.add(InboundEmail(message_id=f"<simulated-{uuid.uuid4().hex}@aime.local>", in_reply_to=last.message_id if last else None,
                           from_email=sender.lower(), from_name=lead.contact.display_name if lead.contact else None,
                           subject=subject, body_text=args.text, new_text=args.text, is_simulated=True, status="new"))
        s.commit()
        process_pending(s, ctx.settings, "simulate-reply")
        row = s.execute(select(InboundEmail).order_by(InboundEmail.id.desc())).scalars().first()
        print(f"SIMULATED reply from {row.from_email}: {row.category} ({row.confidence}) -> {row.status}; {row.action_note}")
    return 0


def cmd_acceptance_report(args, ctx) -> int:
    """Read-only: what the records show for each of the 19 acceptance tests; also written as Markdown."""
    from aime.acceptance import acceptance_evidence, render_markdown
    from aime.timeutil import utcnow

    with ctx.db.session() as s:
        rows = acceptance_evidence(s, ctx.settings)
    label = f"{ctx.settings.data_role} data, {utcnow():%Y-%m-%d %H:%M} UTC"
    out = ctx.settings.reports_dir / f"acceptance_{utcnow():%Y%m%d_%H%M}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(rows, label), encoding="utf-8")  # the evidence file first, then the screen
    for r in rows:
        print(f"{r['test']:>2}. {r['status']:<18} {r['name']}\n      {r['evidence']}")
    print(f"Written to {out}")
    return 0


def cmd_policy(args, ctx) -> int:
    """Shadow-mode auto-publishing policy: record and show what it would do. It never publishes."""
    from aime.policy import agreement, evaluate_shadow

    with ctx.db.session() as s:
        if args.action == "evaluate":
            out = evaluate_shadow(s, ctx.settings)
            print(f"Checked {out['evaluated']} post(s): {len(out['would_publish'])} would publish, "
                  f"{len(out['would_hold'])} would hold, {out['recorded']} new decision(s) recorded.")
            print("Nothing was approved or published. " + "; ".join(out["blockers"]))
        report = agreement(s)
    for r in report["rows"]:
        print(f"#{r['content_id']:<4} {r['decision']:<14} you: {r['human']:<9} {r['title'][:60]}")
        for reason in r["reasons"]:
            print(f"        - {reason}")
    c = report["counts"]
    rate = f"{c['agreement_rate']}%" if c["agreement_rate"] is not None else "n/a"
    print(f"Agreement {rate}: {c['agree']} agree, {c['too_permissive']} too permissive, "
          f"{c['too_strict']} too strict, {c['pending']} waiting for you")
    return 0


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
    print("While you are signed out or the PC sleeps:")
    for line in windows.unattended_readiness({name: windows.installed_xml(name) for name in
                                              (windows.TASK_NAME, windows.WATCHDOG_TASK, windows.DASHBOARD_TASK)}):
        print("  " + line)
    print("  The dashboard and the watchdog need a signed-in desktop; scheduled jobs do not need the dashboard.")
    if not windows.query(windows.WATCHDOG_TASK).get("installed"):
        print("NOTE: no watchdog task. Install it with `python -m aime scheduler install-watchdog`.")
    if not windows.query(windows.DASHBOARD_TASK).get("installed"):
        print("NOTE: nothing restarts the dashboard if it stops. Install `python -m aime scheduler install-dashboard`.")
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
    sc.add_argument("action", choices=["install", "uninstall", "status", "run", "verify", "preflight", "test-unattended",
                                       "restore-previous",
                                       "install-watchdog", "uninstall-watchdog", "install-dashboard",
                                       "uninstall-dashboard"])
    sc.add_argument("--interval", type=int)
    sc.add_argument("--run-when-logged-off", action="store_true",
                    help="Keep running while signed out and after reboot (Windows prompts for your password)")
    sc.add_argument("--wake", action="store_true", help="Let the tick task wake the PC from sleep to run")
    sub.add_parser("doctor", help="Integrity, isolation, compliance and scheduler self-checks")
    bk = sub.add_parser("backup", help="Write and verify an online backup of the database")
    bk.add_argument("--label", default="manual")
    bk.add_argument("--offsite", action="store_true", help="Also write an encrypted copy to BACKUP_OFFSITE_DIR")
    bv = sub.add_parser("backup-verify", help="Check a backup file's integrity and table counts (read-only)")
    bv.add_argument("file")
    rd = sub.add_parser("restore-drill", help="Restore a backup into a temp folder and check it (never touches data/)")
    rd.add_argument("--source", help="Backup .db, bundle .zip or encrypted .aimebak (default: latest local backup)")
    rd.add_argument("--extract-to", help="New folder (outside data/) to receive the checked files for a manual restore")
    sub.add_parser("acceptance-report", help="Read-only: evidence for each of the 19 acceptance tests")
    sr = sub.add_parser("simulate-reply", help="Process a clearly labelled SIMULATED reply from a lead (testing)")
    sr.add_argument("--lead", type=int, required=True)
    sr.add_argument("--text", required=True)
    sr.add_argument("--from-email", help="Default: the address of the lead's latest email")
    pl = sub.add_parser("policy", help="Shadow-mode auto-publishing policy (records decisions; never publishes)")
    pl.add_argument("action", choices=["show", "evaluate"], nargs="?", default="show")
    au = sub.add_parser("automation", help="Pause, stop or emergency-stop the automation (works without the dashboard)")
    au.add_argument("action", choices=["status", "pause", "stop", "emergency-stop"])
    au.add_argument("--reason", default="from the command line")
    mt = sub.add_parser("maintenance", help="Pause scheduled ticks for upgrades (always expires)")
    mt.add_argument("action", choices=["on", "off", "status"])
    mt.add_argument("--hours", type=float, default=2.0)
    mt.add_argument("--reason", default="Manual maintenance")
    ec = sub.add_parser("email-check", help="Check the sales mailbox: settings, SPF/DKIM/DMARC, SMTP/IMAP sign-in")
    ec.add_argument("--domain", help="sending domain to check (default: the SMTP_FROM domain)")
    ec.add_argument("--connect", action="store_true", help="sign in to SMTP and IMAP (read-only); sends nothing")
    ec.add_argument("--send-test", action="store_true",
                    help="also send one email to your OUTBOUND_TEST_RECIPIENTS address (acceptance settings only)")
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
    er = sub.add_parser("erase", help="A person asked us to delete their data: erase it (kept only as do-not-contact)")
    er.add_argument("email")
    er.add_argument("--confirm", action="store_true", help="required: the erasure cannot be undone")
    sd = sub.add_parser("seed-demo", help="Build a separate demo database from fictional sample data")
    sd.add_argument("--data-dir", default=str(BASE_DIR / "demo_data"))
    sd.add_argument("--llm", default="template", choices=["template", "auto", "ollama"])
    rr = sub.add_parser("research-recheck", help="Re-read researched companies' own websites now and re-score them")
    rr.add_argument("--company", type=int, action="append", help="company id (repeat); default: every researched one")
    us = sub.add_parser("user", help="Dashboard sign-in accounts (you type the password at the prompt)")
    us.add_argument("action", choices=["create", "list", "set-password", "set-role", "disable", "enable"])
    us.add_argument("name", nargs="?", help="the account's username (the same as --username)")
    us.add_argument("--username")
    us.add_argument("--role", help="default for create: sales_manager. Built in: super_admin, admin, sales_director, "
                                   "sales_manager, team_leader, sales_executive, bde, lead_gen_executive, analyst; "
                                   "or a custom role from Administration > Roles")
    us.add_argument("--data-dir", help="Use another data folder (e.g. the demo database)")
    return p


COMMANDS = {
    "init": cmd_init, "status": cmd_status, "run": cmd_run, "tick": cmd_tick, "serve": cmd_serve,
    "email-check": cmd_email_check,
    "scheduler": cmd_scheduler, "automation": cmd_automation, "llm-check": cmd_llm_check, "inbound": cmd_inbound, "suppress": cmd_suppress,
    "erase": cmd_erase,
    "seed-demo": cmd_seed_demo, "doctor": cmd_doctor, "backup": cmd_backup, "maintenance": cmd_maintenance,
    "refresh-graphics": cmd_refresh_graphics, "backup-verify": cmd_backup_verify, "restore-drill": cmd_restore_drill,
    "user": cmd_user, "policy": cmd_policy, "simulate-reply": cmd_simulate_reply, "research-recheck": cmd_research_recheck,
    "acceptance-report": cmd_acceptance_report,
}
PRODUCTION_DATA_DIR = BASE_DIR / "data"


def _console_safe() -> None:
    """A Windows console (cp1252, cp437) cannot show every character our output uses ("→", "—"); print a
    replacement character instead of failing the command (acceptance-report crashed exactly when test 19 passed)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):  # a stream that cannot be reconfigured keeps its settings
                pass


def main(argv: list[str] | None = None) -> int:
    _console_safe()
    args = build_parser().parse_args(argv)
    if args.cmd == "seed-demo":
        return cmd_seed_demo(args, None)
    data_dir = getattr(args, "data_dir", None)
    settings = None
    if data_dir:
        resolved = Path(data_dir).resolve()
        role = "production" if resolved == PRODUCTION_DATA_DIR.resolve() else "demo"
        settings = Settings.load(data_dir=resolved, database_url="", data_role=role)
    if os.environ.get("AIME_ENV_FILE"):
        settings = settings or Settings.load()
        if settings.data_dir.resolve() == PRODUCTION_DATA_DIR.resolve():
            print("AIME_ENV_FILE is set but its AIME_DATA_DIR is the production data folder. An alternate settings file "
                  "must use its own data folder (set AIME_DATA_DIR in it). Nothing was run.")
            return 2
    ctx = create_context(settings)
    set_context(ctx)
    return COMMANDS[args.cmd](args, ctx)


if __name__ == "__main__":
    sys.exit(main())
