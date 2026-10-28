# Sales automation gap analysis, 28 October 2026

**Objective:** routine US B2B sales work runs on its own, end to end. The Sales Manager monitors and handles only exceptions: pricing, legal or compliance questions, major negotiations, unusual requests.

**Sources:** this compares that objective with what exists today, from [CURRENT_SYSTEM_AUDIT.md](CURRENT_SYSTEM_AUDIT.md) and [LEAD_DATA_AUDIT.md](LEAD_DATA_AUDIT.md). It supersedes the 26 October [SALES_EMPLOYEE_GAP_ANALYSIS.md](SALES_EMPLOYEE_GAP_ANALYSIS.md) for this objective.

**Legend:**
- **Exists:** built and running in production.
- **Built, inactive:** built and tested, but switched off in production.
- **Partial**
- **Missing**
- **Needs approval:** cannot happen without the owner's explicit authorization.

| Phase | Requirement | Today | Gap | Needs approval? |
|---|---|---|---|---|
| 2 | Real US company discovery from authorized sources | **Exists.** HN "Who is hiring" and SEC EDGAR, with explicit US-location evidence; 280 genuine companies, 0 unknown or test | Only two public sources. Hiring evidence ages (up to 8 weeks) with no freshness mark. Census unused (no key). 113 websites waiting to be checked at about 20 a day | No, for using existing sources better. A new data source needs its terms checked first |
| 3 | Verified decision-maker research | **Exists.** Company websites (robots honoured) and SEC officer filings; 119 named decision-makers, each with an evidence URL | **Reachability:** 0 personal published emails; 7 role mailboxes. Free sources rarely publish a personal address, and paid enrichment is excluded by the owner | Changing the "no paid sources" decision is the owner's call |
| 4 | Evidence-backed qualification | **Exists.** Scored with reasons; grades B/C/D | No grade A. No recency weighting for old evidence | No |
| 5 | CRM updates | **Exists.** Pipeline, activities, opportunities, merge/dedupe, lifecycle | — | No |
| 6 | Personalized outreach drafts | **Exists.** Facts from research cited in each email; quality checks; CAN-SPAM footer | — | No |
| 7 | Authorized sending without routine approvals | **Built, inactive.** Hybrid/Automatic modes send without a person only within an authorization naming each **exact** recipient; limits, business hours, suppression, "Confirm again" | 1. Production cannot send (switch off, no SMTP password). 2. There is no **rule-based** authorization (for example "qualified leads with a verified mailbox, at most N a day"), so routine outreach still needs a person or a hand-made address list | **Yes:** turning sending on, entering the SMTP password, setting `REQUIRE_HUMAN_APPROVAL=false`, and any rule-based authorization |
| 8 | Reply intelligence | **Built, inactive.** Intent classification, unsubscribe/bounce suppression, interested → opportunity + reply draft with the booking link (verified in acceptance) | Mailbox reading not configured in production; no AI second opinion (Ollama not running) | **Yes:** mailbox reading needs the owner's IMAP app password |
| 9 | Follow-ups | **Built, inactive.** Sequence steps and pipeline tasks | Waits for sending | Follows from 7 |
| 10 | Meeting coordination with consent | **Partial.** The booking link goes in emails, and Calendly notices are parsed into meetings (reschedule and cancel included) | No calendar or Calendly API; the app never books or moves a meeting. Notices need mailbox reading | **Yes:** any booking action, and any calendar or Calendly API token |
| — | Suppression, audit trail, retries | **Exists** | A person's approvals are logged as activities, not in the audit log | No |
| — | 24/7 operation without the dashboard open | **Partial.** Jobs do not need the dashboard | All scheduled tasks run only while the owner is logged on, and never wake the PC (gaps up to 147 min) | **Yes:** changing a scheduled task, and the Windows password typed at the prompt |
| — | Truthful statuses | **Exists** (PR #45: running only while a job is open; failures and blocks with reasons) | — | No |
| 11 | Sales Manager dashboard: today's qualified leads, hot opportunities, interested replies, meeting requests, confirmed meetings, failures, automation health | **Partial.** Dashboard, Command Center and Approval Queue cover most items | No single "today and exceptions" view; the approval queue assumes every email is approved by a person | No |
| 12–14 | Orchestration, scheduler, backup and recovery | **Exists** (locks, retries, abandoned-run recovery, daily verified backups, restore drills) | Logged-off limitation (above); OneDrive not signed in | Task change: **yes**. OneDrive sign-in is the owner's to do |

## What blocks full autonomy, in order
1. **Scheduler** runs only while the owner is logged on.
   - The code already supports a stored-password logon for the Tick task. The owner types the Windows password at the prompt; the app never sees it.
   - S4U ("do not store password") would break the current-user DPAPI protection of the LinkedIn token and dashboard sessions.
   - Waking the PC needs the task's wake-to-run setting.
2. **Sending and mailbox reading** are off, and the SMTP/IMAP app password is not in production.
3. **Authorizations** are per address. Routine outreach without a person needs a rule-based authorization, which does not exist yet.
4. **Reachability:** 5 of 42 qualified leads can be emailed. More automation cannot fix that without other authorized sources or a different outreach channel.
5. **Meetings:** no booking integration; the booking link is the only consent-based path today.

## Plan

### Safe to build now
These happen on a branch, tested in isolation, and change nothing live:
1. **Verified defects** from the audit:
   - Pause also holds LinkedIn publishing.
   - `NETWORK_ENABLED` is wired to the environment, or the message mentioning it is removed.
   - Stale comments about `REQUIRE_HUMAN_APPROVAL` and the `job_inbox` description are corrected.
   - A person's approvals are also written to the audit log.
2. **Sales Manager "Today and exceptions" view (phase 11):**
   - today's newly qualified leads, hot opportunities, interested replies, meeting requests and confirmed meetings;
   - failures and blocks;
   - automation health;
   - only the items that need a person.
3. **Scheduler readiness (phase 12), code only:**
   - an option to set wake-to-run on the Tick task, plus a stored-password install for the Watchdog;
   - a `scheduler verify` report saying exactly why work stops when the owner logs off;
   - all tested on generated task XML, never installed.
4. **Rule-based campaign authorization (phase 7):** code only, off by default and behind every existing gate.
   - Example rule: qualified grade-B-or-better leads whose company role mailbox is verified, at most N a day, for D days.
   - Every send stays subject to suppression, the footer, limits, business hours, quality checks and "always a person" topics.
   - It activates only when an administrator creates and activates it **and** the production switches allow sending.
5. **Discovery quality (phases 2–4):**
   - a freshness mark and recency weighting for ageing evidence;
   - a configurable website-check rate, to clear the backlog faster within politeness limits;
   - a stored US-evidence field.
6. **Tests and reports (phases 15–16):**
   - tests for each change;
   - an isolated acceptance run with mock contacts and simulated mail;
   - `IMPLEMENTATION_SUMMARY.md`, `END_TO_END_TEST_REPORT.md` and `PRODUCTION_READINESS.md`.

### Needs the owner's explicit authorization, one item at a time
- **A1. Scheduled tasks:** reinstall the Tick task (and optionally the Watchdog) to run when logged off and to wake the PC. The owner types the Windows password.
- **A2. Mailbox reading:** turn on production mailbox reading (IMAP app password).
- **A3. Sending:**
  - enter the SMTP app password;
  - set `OUTBOUND_EMAIL_ENABLED=true` and `REQUIRE_HUMAN_APPROVAL=false`;
  - create and activate an authorization;
  - the five drafts approved earlier still need "Confirm again".
- **A4. Meeting booking:** any booking integration (Calendly or calendar API token) and any booking action.
- **A5. Release:** merge and deploy of the work above.
