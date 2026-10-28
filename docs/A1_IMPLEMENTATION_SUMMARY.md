# A1 implementation summary, 28 October 2026

**Branch:** `sales/29-a1-unattended`, from production `main` at 9ed84b0.

**Goal:** scheduled work keeps running while the owner is signed out of Windows, provably and safely, without the dashboard open.

**Nothing in production was changed:**
- no scheduled task, power setting, credential or switch;
- no merge, no deploy.

The audit that led here is [A1_SCHEDULER_AUDIT.md](A1_SCHEDULER_AUDIT.md).

## What was already working (not rebuilt)

**Running while signed out is already supported in code.** `scheduler install --run-when-logged-off [--wake]` re-registers the Tick task with Password logon:
- Windows asks the owner for the password (`/RP *`), and the app never sees or stores it;
- a start-up trigger is added;
- the previous definition is saved, so it can be restored.

S4U is deliberately not offered. It cannot unlock the owner's DPAPI keys, which protect the LinkedIn token and the dashboard sessions.

**Reliability:**
- one tick at a time, and one run per job;
- locks left by dead processes are recovered;
- missed runs are made up;
- retries with a growing wait;
- crash recovery;
- retry when the database is locked;
- maintenance windows;
- Pause, Stop and Emergency stop are stored in the database and respected by every job;
- daily verified backups with restore drills.

**Agent monitoring:** each agent's status (never invented), current task, last run and result, last success, next run, 7-day counts, blocking reason, and a link to every run.

## What was changed

| Area | Change | Files |
|---|---|---|
| **Evidence of background runs** | Every check-in records the Windows session it ran in: *background* (session 0, the session a "run whether logged on or not" task uses) or *interactive*. It also carries forward `last_background_at`, the last time a run happened in the background session. This is how anyone can later see, from evidence and not from settings, that work ran while signed out | `aime/session_info.py` (new, standard library only), `aime/monitoring.py` `write_heartbeat`, `scripts/run_tick.py` (maintenance check-in) |
| **No desktop notifications in the background** | A toast is not attempted in session 0, where nobody can see it. The alert is still stored and shown on the dashboard | `aime/notify.py` `toast` |
| **Read-only preflight** | `python -m aime scheduler preflight` reads this PC's settings and says what would stop unattended runs. It changes nothing and prints no names, network names or secrets. It reads: <ul><li>account type and whether a password is set;</li><li>Administrators membership (the batch-logon right);</li><li>the blank-password policy;</li><li>whether the Wi-Fi profile is saved for all users;</li><li>wake timers on mains and battery;</li><li>sleep timeout;</li><li>Modern Standby and Fast Startup;</li><li>whether Windows task history is on.</li></ul> | `aime/scheduler/windows.py` `collect_preflight_facts`, `evaluate_preflight`, `preflight`; `aime/cli.py` |
| **Isolated test task** | `python -m aime scheduler test-unattended` registers a **separate, on-demand** task `AIMarketingEmployee-A1Test` (Password logon, no triggers). It runs once, then is **deleted**, even when a step fails. Windows asks the owner for the password. The production tasks are never touched | `aime/scheduler/windows.py` `test_unattended`, `build_task_xml(on_demand_only=True)` |
| **Probe** | The test task runs `scripts/unattended_probe.py`, which reports: <ul><li>the session (must be *background*);</li><li>a DPAPI round trip on a random value (no real secret is read);</li><li>a read-only `quick_check` of the database;</li><li>DNS resolution only (nothing is sent).</li></ul> | `scripts/unattended_probe.py` (new) |
| **Fuller task readout** | `scheduler verify` also lists task settings that would stop or duplicate runs: battery conditions, missed runs not made up, waiting for network or idle, parallel copies | `aime/scheduler/windows.py` `task_conditions`, `unattended_readiness` |
| **Manager Today status board** | One status per part of the automation, from records and settings, never assumed. The statuses are ACTIVE, PAUSED, BLOCKED, FAILED, WAITING FOR AUTHORIZATION and NOT CONFIGURED. The parts are: <ul><li>scheduled jobs;</li><li>running while signed out (A1);</li><li>AI agents;</li><li>email sending (A3);</li><li>mailbox reading (A2);</li><li>meeting booking (A4);</li><li>LinkedIn publishing;</li><li>backups.</li></ul> The board replaces the plain on/off rows and two rows it repeated (one place per metric) | `aime/web/app.py` `system_status`, `scheduler_times`; `manager.html` |
| **Manager Today counts and times** | New counts: companies discovered today, contacts with a usable email, qualified leads ready for outreach, follow-ups scheduled. New times: last successful scheduled run and next scheduled check-in | `aime/web/app.py` `manager_today`; `manager.html`; `style.css` |
| **Agent retries** | Each agent shows its pending retries: consecutive failures of its jobs, which the scheduler retries with a growing wait | `aime/web/app.py` `ai_employee`; `agent_detail.html`; `command_center.html` |
| **Tests** | 28 new tests | `tests/test_a1_unattended.py` |

**Database changes:** none. No migration.

**Production task definitions:** none changed. The existing `install --run-when-logged-off [--wake]` code is unchanged and is the proposed A1 step.

## Deliberately not changed

- **The Dashboard and Watchdog tasks stay interactive.** They exist for a person at the screen. Scheduled work never needs them.
- **No power setting** (wake timers on battery, lid action, Fast Startup). Each is the owner's choice. The preflight reports them.
- **Windows Task Scheduler history** stays off. Turning it on needs an administrator, and the app's job history already records every run.
- **A2, A3 and A4** are not enabled. Their readiness reports are separate.

## Still missing or blocked

1. **Logged-off execution has not run on this PC.** It needs the owner's Windows password at the prompt, which is authorization A1.
2. **Wake from sleep is not tested.** It needs A1 with `--wake` and a sleep test. On battery, wake timers are off on this PC.
3. **No 24/7 guarantee.** A laptop that is shut down, out of power or offline runs nothing. See [A1_PRODUCTION_READINESS.md](A1_PRODUCTION_READINESS.md) for the always-on options.
4. **Post graphics in the background session are not verified.** Image rendering uses headless Edge with a 60-second timeout. If it fails, it falls back to the built-in Pillow renderer, so a post still gets a simpler image.
