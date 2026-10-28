# A1 scheduler audit, 28 October 2026

**Scope:** a read-only audit of production at about 13:40 IST, before any A1 change. Nothing was changed while auditing:
- no scheduled task, power setting, credential, switch, or record;
- the database was opened read-only.

Record counts are aggregates; the repository is public.

**Verdict:**
- Production runs **9ed84b0** (PR #46) and is healthy.
- All three scheduled tasks run **only while the owner is signed in to Windows**, and none of them wakes the PC. That is the whole A1 gap. Everything else the scheduler needs (no overlapping runs, catching up missed runs, retries, crash recovery, stop switches) already exists and works.

## 1. Production state

| Check | Finding |
|---|---|
| Git commit | `9ed84b0` on `main`, the same as `origin/main` |
| Dashboard | `http://127.0.0.1:8765/login` answers 200. Other pages redirect to sign-in. A foreign Host header is refused (400). Scripts are blocked by CSP (`script-src 'none'`) |
| Database integrity | `PRAGMA integrity_check` = ok. 0 foreign-key violations. WAL journal. Role `production` |
| Job history | 690 job runs since 25 Oct, **all successful**. 580 agent runs in 7 days, all successful. No `scheduler_crash.log` |
| Error logs | `aime.log`: 41 WARNING lines, 0 ERROR or CRITICAL. `dashboard.log`: no tracebacks |
| Watchdog log | 4 entries since 27 Oct: 3 × "scheduled ticks stopped" (45–59 min gaps) and 1 × "dashboard not running" (27 Oct, before the Dashboard task existed) |
| Open alerts | 2 warnings: a LinkedIn post slot left empty by quality checks; OneDrive not signed in |
| Backups | Daily verified backup (06:21 today). Pre-deploy backup verified (30 tables, 3,219 rows) and restore-drilled at 13:28. Encrypted off-machine copies are written to the OneDrive folder, but OneDrive is not signed in, so they stay on this PC |
| Automation | Mode **Hybrid**, state **running** (defaults). 0 campaign authorizations, by address or by rule |
| Email sending | `OUTBOUND_EMAIL_ENABLED=false`, `REQUIRE_HUMAN_APPROVAL=true`. SMTP host and username set; **SMTP password empty**. 0 emails ever sent. Drafts #38, #46 and #51–53 approved and unsent |
| Mailbox reading | IMAP host, username and password all empty: **not configured** |
| Meeting booking | Calendly booking link set (`calendly.com/mrsantoshdhs/30min`). No Calendly or calendar API token: **not configured** |
| LinkedIn publishing | `PUBLISHING_ENABLED=false` |

## 2. Scheduled tasks

Discovered by name and by their actions: every task whose action runs this project. There are exactly three. No other app-related task exists, and the A1 test task is not present.

| Setting | Tick | Dashboard | Watchdog |
|---|---|---|---|
| Runs | `pythonw.exe scripts/run_tick.py` | `pythonw.exe scripts/dashboard_task.py` | `pythonw.exe scripts/watchdog.py` |
| Working folder | production folder | production folder | production folder |
| Account | `Lenovo` (this PC) | `Lenovo` | `Lenovo` |
| **Logon type** | **Interactive only** | **Interactive only** | **Interactive only** |
| Run level | Limited (least privilege) | Limited | Limited |
| Triggers | every 15 min; at logon (+2 min) | every 15 min; at logon (+2 min) | hourly; at logon (+2 min) |
| Start-up trigger | none | none | none |
| **Wake the PC** | **no** | **no** | **no** |
| Battery conditions | none (runs on battery) | none | none |
| Missed runs made up (StartWhenAvailable) | yes | yes | yes |
| Second copy while one runs | ignored (IgnoreNew) | ignored | ignored |
| Time limit per run | 1 h | none (long-running server) | 1 h |
| Restart on failure | 3 × every 5 min | 3 × every 5 min | 3 × every 5 min |
| Needs network or idle | no | no | no |
| Last run (result) | 13:36 (0) | 13:33 (0x800710E0: request ignored because the dashboard was already running) | 12:48 (0) |
| Next run | 13:51 | 13:48 | 13:48 |

**What this means:**
- **Signing out stops everything.** When the owner signs out of Windows, every task stops: no research, drafting, monitoring or backups.
- **Locking the screen does not stop anything.** Interactive tasks keep running while the session is locked.
- **Sleep stops everything.** While the PC sleeps nothing runs. Missed runs are made up when it wakes.
- **Restarting without signing in.** Nothing runs until someone signs in.

**Task history:** the Windows Task Scheduler history log (`Microsoft-Windows-TaskScheduler/Operational`) is **disabled** on this PC. The app's own job history is the record of every run.

**Gaps in the app's history (last 48 h):** the largest gaps between scheduled runs were 74, 60, 60, 52 and 46 minutes. The last was on 28 Oct, 09:36–10:21 IST. They are consistent with the PC being asleep or the owner signed out. The task settings cannot tell which.

## 3. How the scheduler protects itself today (already built)

| Protection | Where | Status |
|---|---|---|
| One tick at a time | `tick.lock` with the owner's process id, `aime/scheduler/runner.py` `FileLock` | Exists. A lock left by a dead process is recovered at once |
| One run per job at a time | `job_<name>.lock` | Exists |
| Task Scheduler duplicate guard | `MultipleInstancesPolicy=IgnoreNew` | Exists |
| Missed runs made up | `StartWhenAvailable`; jobs decide due slots from their own history | Exists |
| Retries | daily jobs retry with a doubling wait, at most 3 times per slot; interval jobs run again next interval | Exists |
| Crash recovery | runs left "running" by a killed process are marked abandoned (`recover`) | Exists |
| Database lock contention | WAL; recording a result retries on "database is locked" | Exists |
| Maintenance window | ticks and the dashboard launcher stand down; the window always expires | Exists |
| Pause / Stop / Emergency stop | `aime/automation.py`; every job checks the state; a stop is stored in the database, so a restart does not undo it | Exists |
| Heartbeat | `data/heartbeat.json` after every tick | Exists. It did **not** record the Windows session the tick ran in (added on the A1 branch) |
| Watchdog | hourly. If ticks are more than 45 min old or the dashboard is down, it writes `watchdog.log` and shows a desktop notification. It changes nothing | Exists. Interactive only, so it cannot notify anyone while the owner is signed out |

## 4. AI agents (no duplicates needed)

| Responsibility asked for | Existing agent or job |
|---|---|
| Company discovery | `research` (Company discovery): Hacker News hiring posts and SEC EDGAR |
| Company research | `company_research`: company websites (robots honoured) and SEC filings |
| Contact discovery and verification | `company_research` (decision-makers with evidence URLs; published addresses only); `crm` (website verification) |
| Lead qualification | `qualification`: scores, grades and tiers with reasons |
| CRM management | `crm`: dedupe, merge, hygiene, lifecycle |
| Email drafting and follow-ups | `sales_workflow`: drafts for approval, sequences, sends only approved email when sending is on |
| Reply intelligence | `inbox_monitor`: needs mailbox reading (A2) |
| Meeting coordination | `inbox_monitor` reads Calendly notices into meetings (needs A2). No booking agent (A4) |
| Reporting | `analytics` (daily and weekly reports) |
| System monitoring | the `monitor` job and `aime/monitoring.py` (alerts, health), plus the watchdog |
| LinkedIn | `marketing_manager`, `linkedin_content`, `publishing` (publishing is off) |

**What each agent already shows** (Sales Command Center and the agent pages):
- status, never invented: Running only while its job is open;
- current task;
- last run and its result;
- last successful run;
- next scheduled run;
- completed and failed runs in 7 days;
- why it is blocked;
- a link to every run (the evidence).

**Missing:** the number of pending retries (added on the A1 branch).

## 5. Power and sign-in facts that decide A1

Read with `powercfg`, `netsh`, `Get-LocalUser` and the registry, read-only. They are repeatable with `python -m aime scheduler preflight` on the A1 branch.

| Fact | Value | Effect |
|---|---|---|
| Edition | Windows 11 Home Single Language | No Group Policy editor; the defaults below apply |
| Account | Local account, password set (June 2026), member of Administrators (UAC-filtered) | **Password logon is possible.** Administrators have "Log on as a batch job" by default. A blank password would be refused (`LimitBlankPasswordUse=1`) |
| Sleep states | **Modern Standby** (S0 low-power idle, network connected) and hibernate. No S3 | In standby, a task runs only if a wake timer is allowed |
| Wake timers | allowed on mains power; **disabled on battery** | A1 "wake" works on mains power only, unless the owner changes the battery setting |
| Sleep after idle | never (mains and battery) | The PC does not fall asleep by itself. Closing the lid or choosing Sleep still sends it to sleep (the lid action was not read) |
| Hibernate after idle | effectively never | — |
| Fast Startup | on | After Shut down, Windows may not count the next power-on as a start-up. The 15-minute trigger still makes up the missed run within minutes |
| Wi-Fi | the connected network is an **all-users** profile | Stays connected while signed out |
| Battery | present, on mains at the time | Tasks are allowed on battery |

## 6. Limits that no setting removes

- **Shut down, out of power or no network:** nothing runs. A laptop cannot guarantee 24/7 operation. Wake timers do not start a PC that is shut down.
- **OneDrive:** it runs only while the owner is signed in. Off-machine backup copies wait on this PC until then (and today they wait anyway, because OneDrive is not signed in).
- **Local AI second opinion (Ollama):** if it runs only as a signed-in app, it is unavailable while signed out. The app then falls back to its template writer and rule-based classification.
- **Notifications:** desktop notifications cannot reach anyone while signed out. Alerts are stored and shown on the dashboard, and in Manager Today once signed in.

## 7. Conclusion

A1 needs exactly one production change: the **Tick** task re-registered with Password logon ("run whether the user is logged on or not"), with a start-up trigger and, optionally, wake-to-run. The code for that already exists (`scheduler install --run-when-logged-off --wake`).

The Dashboard and Watchdog tasks should **stay interactive**. They exist for a person at the screen, and scheduled work does not need them.

What the A1 branch adds, and the exact proposed change, are in [A1_IMPLEMENTATION_SUMMARY.md](A1_IMPLEMENTATION_SUMMARY.md) and [A1_PRODUCTION_READINESS.md](A1_PRODUCTION_READINESS.md).
