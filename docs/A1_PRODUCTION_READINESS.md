# A1 production readiness: running while signed out, 28 October 2026

**Verdict:** technically ready, pending the owner's authorization. Production (9ed84b0) is unchanged.

The change A1 needs in production is small:
- re-register **one** task (Tick) to "run whether the user is logged on or not", with the owner typing the Windows password at the Windows prompt;
- before that, deploy the A1 branch, so the change can be tested safely first and proven by evidence afterwards.

Supporting documents:
- [A1_SCHEDULER_AUDIT.md](A1_SCHEDULER_AUDIT.md): the current state;
- [A1_IMPLEMENTATION_SUMMARY.md](A1_IMPLEMENTATION_SUMMARY.md): the code changes;
- [A1_TEST_REPORT.md](A1_TEST_REPORT.md): the test results.

## Proposed production changes, in order

Each step needs the owner's separate go-ahead. None has been given. Commands run in the production folder, in the owner's own PowerShell terminal:

```
cd $env:USERPROFILE\Documents\us-ai-marketing-employee
```

### Step 1: merge and deploy the A1 branch (code only)

**How:** the proven deploy flow:
1. CI on the exact head.
2. Full suite.
3. Maintenance on.
4. Verified backup and restore drill.
5. Deploy check on the restored copy.
6. Stop the dashboard.
7. Fast-forward.
8. Doctor.
9. Maintenance off.
10. Start the dashboard.
11. Live checks.

**What changes:**
- Manager Today gets the status board and new counts.
- Every check-in records its Windows session.
- Two new commands: `scheduler preflight` and `scheduler test-unattended`.

There is no database migration and no task change.

**Rollback:** fast-forward back to 9ed84b0 and restart the dashboard.

### Step 2: preflight (read-only, safe at any time after step 1)

```
.\.venv\Scripts\python.exe -m aime scheduler preflight
```

**Expected on this PC today:**
- PASS: account with a password, administrator, all-users Wi-Fi, wake timers on mains, never sleeps on mains.
- WARN: wake timers off on battery.
- INFO: Modern Standby, Fast Startup, no 24/7 guarantee, Task Scheduler history off.

**Prerequisite:** the account's Windows password must not be blank. Windows refuses blank passwords for tasks that run while signed out.

### Step 3: isolated test (creates a temporary task, then deletes it)

```
.\.venv\Scripts\python.exe -m aime scheduler test-unattended
```

**What happens:**
1. Windows asks for the Windows password of `Lenovo`.
2. A separate task `AIMarketingEmployee-A1Test` is registered and run once.
3. It is then deleted, whatever the result.

**What it proves:** a task of this kind runs in the **background session** (the one used while signed out) on this PC. The checks are:
- the owner's DPAPI encryption keys work there (a round trip on a random value);
- the database opens (read-only);
- DNS works.

**Expected result:** `{"ok": true, "probe": {"session": "background", "dpapi": true, "database": true, "network": true}}`. The production tasks are not touched.

### Step 4: switch the Tick task to run while signed out (the A1 change)

```
.\.venv\Scripts\python.exe -m aime scheduler install --run-when-logged-off
```

Add `--wake` to also let it wake the PC from sleep (on mains power only on this PC; see the power notes below).

**What happens:**
1. Windows asks for the password once and stores it in Windows' own credential store. The app never sees, logs or stores it.
2. The current definition is saved first, so `restore-previous` can undo the change.

**The new definition is the same as today, except:**
- logon type **Password** instead of interactive;
- a **start-up trigger** (3 minutes after boot);
- wake-to-run, only if `--wake` is given.

**Unchanged:**
- the 15-minute schedule and the logon trigger;
- no battery restriction;
- missed runs are made up;
- a second copy is ignored;
- 1-hour run limit and 3 restarts.

**If Windows answers "Access is denied":** run the same command in a terminal opened with "Run as administrator".

### Step 5: verify, then prove it with a real sign-out

1. Run `.venv\Scripts\python.exe -m aime scheduler verify`. It should say: "AIMarketingEmployee-Tick: runs while you are signed out (Password logon); also starts after a reboot before you log in."
2. Within 15 minutes, Manager Today should show **Running while signed out: ACTIVE**: "the last check-in ran in the background session".
3. **Real test:** with the PC on mains power, **sign out** (do not shut down or sleep) for at least 35 minutes, then sign back in. The Jobs page and Manager Today should show check-ins during that window.
4. **Optional, with `--wake`:** put the PC to sleep on mains power for at least 35 minutes. Runs should appear during the sleep window. If they do not, Modern Standby on this PC did not honour the wake timer: report it, nothing breaks.

### Rollback of step 4

```
.\.venv\Scripts\python.exe -m aime scheduler restore-previous
```

This re-registers the saved interactive definition (no password needed), immediately. Data is unaffected.

## What changes for the owner after step 4

- **Signed out:** work continues — research, qualification, CRM upkeep, drafting, monitoring and backups. The PC must be on: locked or signed out, not shut down or asleep (unless `--wake`).
- **Signed in:** scheduled runs happen in the background session too.
  - Desktop notifications **from scheduled runs** no longer appear. The alerts are still stored and shown on Manager Today and the dashboard.
  - The hourly **Watchdog** still notifies, because it stays interactive.
- **If the Windows password changes,** the stored credential stops working and the task cannot start. Manager Today then shows *Scheduled jobs: FAILED* and the watchdog notifies. Fix it by re-running step 4.
- **Unchanged:** the Dashboard and Watchdog tasks (interactive, by design), every switch, and every authorization.
  - Sending, mailbox reading, publishing and booking stay off.
  - A2, A3 and A4 remain separate decisions.

## Power settings (none changed; each is the owner's call)

| Setting | Today | Effect | Change only if wanted |
|---|---|---|---|
| Wake timers on battery | off | With `--wake`, the PC is woken on mains power only | `powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1` then `powercfg /setactive SCHEME_CURRENT`. This drains the battery; not recommended |
| Sleep after idle | never | The PC never sleeps by itself | — |
| Lid close | not read | Closing the lid may sleep the PC | Settings › Power › Lid: "Do nothing" when plugged in |
| Fast Startup | on | After Shut down, the first run may wait for the 15-minute trigger instead of the start-up trigger | Leave as is |
| Task Scheduler history | off | Windows keeps no task history; the app's job history is complete | Optional, needs an administrator |

## Is 24/7 operation realistic?

**Not on this laptop.** After A1 it works whenever the PC is **on**, signed in or not, and catches up within 15 minutes after sleep or power-on. It cannot run while the PC is shut down, out of power, or offline, and wake timers cannot start a shut-down PC.

**For business-hours operation,** A1 on this laptop is enough if it stays plugged in, is never shut down, and is signed out or locked rather than put to sleep.

**If uninterrupted 24/7 operation is required,** there are two options. Neither has been started; both are the owner's decision.

1. **A dedicated always-on Windows PC** (a mini PC on mains power and a wired network). This is the smallest change: the same code, the same A1 setup, and the same Windows DPAPI protection of tokens. Cost: the hardware.
2. **A cloud or always-on server.** This needs engineering before any move:
   - **Secrets:** DPAPI is Windows-only. On Linux the app would store tokens unencrypted, so a different secret store is needed first.
   - **Scheduling:** Task Scheduler is replaced by a systemd timer or cron.
   - **Dashboard access:** the dashboard is local-only by design. It would be reached through an SSH tunnel or VPN, never exposed publicly.
   - **Backups:** copies go to the provider's storage.
   - **Cost:** it is a paid service, which the owner has so far excluded.

## Authorizations needed (none given)

| # | Action | Changes production? |
|---|---|---|
| A1-1 | Merge and deploy `sales/29-a1-unattended` | Code only |
| A1-2 | Run the isolated test task (step 3) | Creates and deletes a temporary task |
| A1-3 | Re-register the Tick task to run while signed out (step 4), with or without `--wake` | Yes: a scheduled task |
| optional | Any power-setting change listed above | Yes: Windows settings |

A2 (mailbox reading), A3 (sending) and A4 (meeting booking) are separate. See their readiness reports.
