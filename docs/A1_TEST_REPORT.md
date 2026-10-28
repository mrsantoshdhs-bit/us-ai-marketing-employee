# A1 test report, 28 October 2026

**Code under test:** branch `sales/29-a1-unattended` (from production 9ed84b0).

**Environment.** Everything was isolated from production:
- **Automated tests** use a fresh scratch data folder (`AIME_DATA_ROLE=test`). `schtasks` and PowerShell are replaced by recorders, so no real task is created or changed.
- **Mail** is simulated with in-process fake SMTP and IMAP servers.
- **Page checks** ran this branch's code on a read-only copy of the production database. The copy is labelled acceptance; every secret is blanked and every switch is off.
- **Production itself** was only read: task definitions, power settings and the database, opened read-only.

Only results that were actually run are reported. Anything not run says **NOT TESTED**, with the reason.

## Results by requirement

| # | Requirement | Result | Evidence |
|---|---|---|---|
| 1 | Windows logged-off execution | **NOT TESTED on this PC.** Simulated parts PASS | A real run needs the owner's Windows password typed at the Windows prompt (A1 step 3, `scheduler test-unattended`). PASS for the parts that can be simulated: <ul><li>session 0 is detected as background;</li><li>the heartbeat records the session and keeps the last background run;</li><li>no desktop notification is attempted in the background;</li><li>the test task is registered with Password logon and no triggers, reports through the probe, and is always deleted.</li></ul> |
| 2 | Scheduled task triggering | PASS (observed, read-only) | Production Tick ran on schedule at 13:36 and 13:51 (last result 0). 251 scheduled job runs in 24 h, all successful |
| 3 | Wake configuration behaviour | Definitions PASS. Live wake **NOT TESTED** | `<WakeToRun>` is written and read back (tests). The preflight read this PC: wake timers allowed on mains, off on battery, Modern Standby. A sleep test needs A1 with `--wake` |
| 4 | Automatic restart behaviour | Definitions PASS. Reboot **NOT TESTED** | Restart on failure (3 × 5 min) read from the live tasks. A Password-logon definition adds a start-up trigger (test). The Dashboard task restarted the dashboard during the 9ed84b0 deploy. No reboot was performed |
| 5 | Duplicate task prevention | PASS | A second tick is refused while one runs; a dead owner's lock is recovered (`test_a_second_tick_never_overlaps…`). Live tasks use `IgnoreNew`. One run per job uses job locks (existing tests) |
| 6 | Database integrity | PASS | Production, read-only: `integrity_check` ok, 0 foreign-key violations. Copy check: integrity ok before and after |
| 7 | Agent orchestration | PASS | Full suite, including the end-to-end sales chain with a mock contact (`test_e2e_sales_autonomy.py`) |
| 8 | Failed-job recovery | PASS | Retry with a doubling wait, at most 3 per slot (`test_failed_daily_jobs_retry…`). Abandoned runs are recovered (existing `test_reliability.py`, `test_hardening.py`) |
| 9 | Scheduler logging | PASS | Heartbeat session tests. Job history for every run (690 production runs, all successful) |
| 10 | Dashboard-independent execution | PASS | Ticks run without the dashboard (in-process tests; separate production tasks). The scheduler entry point `scripts/run_tick.py` ran as a subprocess with no dashboard (maintenance path) |
| 11 | Pause | PASS | `test_paused_or_stopped_automation…`, `test_pausing_automation_holds_linkedin_publishing` |
| 12 | Resume | PASS | Only an administrator can resume (`test_modes_and_states…`, `test_admin_only_routes…`) |
| 13 | Emergency stop | PASS | It stops jobs and every authorization (`test_e2e_sales_autonomy.py`). A deliberate stop survives a restart (`test_a_deliberate_stop_survives…`) |
| 14 | Backup integrity | PASS | `backup-verify` of the pre-deploy backup: ok, 30 tables, 3,219 rows. Restore drill passed at 13:28 |
| 15 | Restart recovery | Code PASS. PC restart **NOT TESTED** | Lock and run recovery after a dead process (tests). Not tested by restarting Windows |
| 16 | Existing authorization controls | PASS | `test_rule_authorizations.py`, `test_automation.py`, `test_approval_gates.py` |
| 17 | Email sending remains disabled | PASS | Production `OUTBOUND_EMAIL_ENABLED=false`, SMTP password empty, 0 emails ever sent, drafts #38, #46 and #51–53 approved and unsent (copy check). Manager Today shows WAITING FOR AUTHORIZATION (test) |
| 18 | Mailbox reading remains disabled | PASS | Production IMAP host, username and password are empty. The board shows NOT CONFIGURED (test) |
| 19 | Meeting booking remains disabled | PASS | No Calendly or calendar token in production. The board shows NOT CONFIGURED (test) |
| 20 | Existing dashboard functionality | PASS | This branch's code on a production copy: all 32 pages return 200, no secret value on any page, foreign Host refused, CSP blocks scripts, dashboard numbers equal database counts. 26/26 checks |

## Suites and checks

| Check | Result |
|---|---|
| Full regression suite, final code (`scripts/pytest_with_timeout.py`) | **557 passed, 0 failed** (529 before this branch, plus 28 new) |
| New A1 tests (`tests/test_a1_unattended.py`) | 28 passed |
| Scheduler, monitoring, notification, agent and Manager Today suites | passed (within the full suite) |
| `ruff check aime tests scripts` (includes the security rules) | all checks passed |
| Secret scan (pre-commit, every commit) | clean. It also caught a local Windows path in a draft doc, which was removed |
| Branch page and safety check on a production copy | 26/26 |
| Read-only preflight on this PC | ran; 1 WARN (wake timers off on battery), 0 FAIL |
| GitHub Actions CI | recorded on the pull request |

## Defect found and fixed while testing

`EmailSender.status()` answered "Ready" when an SMTP username was set and the password was empty, which is production's configuration today. With sending switched on, the dashboard would have claimed sending was ready while every send failed at sign-in.

It now answers "NOT CONFIGURED: SMTP_PASSWORD missing", and Manager Today shows **Email sending: BLOCKED** (`test_sending_switched_on_without_a_password_is_blocked_not_active`).

The code review for the A3 report found this. Other findings are in [A3_EMAIL_SENDING_READINESS.md](A3_EMAIL_SENDING_READINESS.md); the campaign-limit defect there is **not** fixed on this branch.

## Not tested, and why

- **A real signed-out run, a wake from sleep, and a Windows restart.** Each needs the owner: their password at the Windows prompt, or putting the PC to sleep or restarting it. The steps are in [A1_PRODUCTION_READINESS.md](A1_PRODUCTION_READINESS.md) (steps 3 and 5).
- **Post-image rendering in the background session.** If headless Edge fails there, the code falls back to the Pillow renderer.
- **Anything live** (email, mailbox, Calendly, LinkedIn). Out of A1's scope; all off.

## Production during testing

Unchanged:
- commit 9ed84b0;
- the three scheduled tasks and their definitions;
- power settings, credentials and `.env`;
- every switch.

Nothing was sent, read from a mailbox, published or booked. The database was opened read-only for the audit and to make copies.
