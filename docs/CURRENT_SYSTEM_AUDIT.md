# Current system audit, 28 October 2026

This is a read-only audit of the US AI Sales Employee as it runs in production. Nothing was changed: no settings, scheduled tasks, secrets or data.

**What was inspected:**
- the repository at `main` = `2288f38`;
- the production database, read-only (`mode=ro`);
- the Windows scheduled tasks, through read-only queries;
- logs, backups, tests, CI and docs.

**How claims are backed:**
- Code claims carry `file:line` references.
- Data claims are counts from the production database at about 11:30 IST.

The record-level lead classification is in [LEAD_DATA_AUDIT.md](LEAD_DATA_AUDIT.md). The repository is **public**, so these files contain no names or addresses.

## 1. Snapshot
| Item | Value |
|---|---|
| Production version | `2288f38` (PR #45), deployed 28 Oct 11:17 IST; in production since 25 Oct |
| Code size | `aime/` 136 files, about 19,100 lines; `tests/` 40 files, about 7,500 lines (505 tests); 7 scripts; 12 docs |
| Stack | Python 3.12, FastAPI dashboard on 127.0.0.1:8765 (sign-in required, server-rendered, CSP `script-src 'none'`), SQLite in WAL mode with additive migrations, Windows Task Scheduler |
| Safety switches (production `.env`) | `OUTBOUND_EMAIL_ENABLED=false`, `PUBLISHING_ENABLED=false`, `REQUIRE_HUMAN_APPROVAL=true`; IMAP not configured; SMTP password **not set** |
| Operating mode and state | Hybrid / running (defaults); 0 campaign authorizations; empty audit log |
| Health | 669 job runs since 25 Oct with **0 failures**; 0 ERROR lines in the logs; doctor 22 pass, 1 warning (OneDrive) |

## 2. Agents
All ten agents share one retry framework: up to 3 attempts, starting at 2 s and doubling to a 30 s cap, on network and database errors (`aime/agents/base.py:37-48`, `122-146`). Every run is recorded in `agent_runs` and in `logs/agent_runs.jsonl`.

| Agent | What it does | External contact | Runs since 25 Oct (failed) |
|---|---|---|---|
| research | Finds US companies from HN "Who is hiring" posts, SEC EDGAR and your own CSV lists; skips non-US and unknown locations (`research.py:97-102`); at most 40 new companies per run | hn.algolia.com, sec.gov | 7 (0) |
| company_research | Reads each company's own website (robots.txt honoured on every page, own domain only) and SEC Form 3/4 officer filings; checks email domains with DNS MX; 15 companies per run | the company's site, sec.gov, DNS | 4 (0) |
| qualification | Scores and grades every lead with its reasons; deterministic | none | 20 (0) |
| crm | Inbound intake, website verification, dedupe and merge (never deletes), hygiene, stale leads | company homepages and robots.txt | 155 (0) |
| sales_workflow | Drafts evidence-based outreach and follow-ups; sends approved emails behind every gate (section 5) | SMTP (only when sending is on) | 80 (0) |
| inbox_monitor | Reads the sales mailbox read-only (EXAMINE / BODY.PEEK); classifies replies; Calendly notices become meetings | IMAP (not configured in production) | 135 (0) |
| marketing_manager | Daily focus: states, industries, campaign | none (Census only when a key is set; it is not) | 6 (0) |
| linkedin_content | Drafts LinkedIn posts with quality checks; renders images locally | hn.algolia.com, local Ollama | 6 (0) |
| publishing | Publishes approved posts through the official API | api.linkedin.com (switch off) | 139 (0) |
| analytics | Daily and weekly reports | none | 11 (0) |

## 3. Jobs and scheduler
- **Jobs:** 13, in `config/schedule.toml` and `aime/workflows.py:226-240`:
  - every 15 min: inbox;
  - every 30 min: inbound, publish, monitor;
  - hourly: follow-ups;
  - daily: db_maintenance 06:15, crm_maintenance 06:30, plan 06:45, research 07:00, enrich 07:15, content 07:30, daily_report 20:00;
  - weekly: weekly_report, Monday 08:00.
- **Last 30 days:** 669 runs, all successful.
- **Runner** (`aime/scheduler/runner.py`):
  - a tick lock and one lock per job;
  - maintenance windows;
  - crashed runs are marked abandoned after 10 min without a lock owner, or after 2 h;
  - a failed daily or weekly job gets up to 3 attempts per slot;
  - a heartbeat after every tick.
- **Windows tasks** (read with `Get-ScheduledTask`):

| Task | Logon type | Triggers | Other |
|---|---|---|---|
| AIMarketingEmployee-Tick | **Interactive** | every 15 min, plus at logon | start-when-available on; **wake-to-run off**; 1 h limit |
| AIMarketingEmployee-Dashboard | **Interactive** | every 15 min, plus at logon | keeps the dashboard up |
| AIMarketingEmployee-Watchdog | **Interactive** | hourly, plus at logon | |

- **Consequence:** nothing runs while you are logged off or the PC is asleep. The 7-day history shows gaps between scheduled runs of 147, 74, 60, 60, 52 and 46 minutes.
- **What the code already offers:**
  - `python -m aime scheduler install --run-when-logged-off` switches the Tick task to a **stored-password** logon with a boot trigger (`aime/scheduler/windows.py:32`, `61-65`, `148-167`). Windows asks for the password at the console; the app never sees it.
  - The Dashboard and Watchdog tasks are always interactive (`windows.py:185-207`).
  - **S4U** ("do not store password") is not supported. It would also break the current-user DPAPI that protects the LinkedIn token and the dashboard session key (`aime/secure_store.py:33-45`).
  - The wake-to-run setting is never set.

## 4. Automation flows
- **Research → CRM → qualification → drafting**, runs in the `research` and `inbound` jobs:
  - companies and signals are stored with their source URLs;
  - leads are scored;
  - drafts are written only for qualified leads with a usable email published on the company's own domain.
- **Enrichment:** `enrich`, then `company_research`, then qualification.
- **Replies:** `inbox`, then `inbox_monitor`, then `reply_intel`:
  - each reply is classified with an intent;
  - unsubscribe and bounce replies suppress the address;
  - interested replies open an opportunity and get a reply draft with the Calendly link;
  - Calendly notices create, reschedule or cancel meetings (`aime/meetings.py`).
- **Follow-ups:** `followups`, then `sales_workflow`: sequence steps and tasks, with sending gated as in section 5.
- **Marketing:** `plan`, `content` and `publish` (posts need approval; publishing is switched off).
- **Reports:** daily and weekly. Live counts appear on the dashboard, the Command Center and Sales Analytics.

## 5. Safety controls (all present and tested)
- **Approval:**
  - drafts are created `awaiting_approval`;
  - sending needs `approved` plus `approved_at`;
  - approval is refused for suppressed addresses, a missing footer, unfilled placeholders and held quality checks (`app.py:622-660`).
- **Operating modes** (`aime/automation.py`):
  - **Approval mode:** every email needs a person.
  - **Hybrid and Automatic:** an email goes out without a person only when **all** of these hold:
    - `REQUIRE_HUMAN_APPROVAL=false`;
    - sending is on;
    - automation is running;
    - an active campaign authorization names the **exact recipient** (at most 50 recipients, 50 a day, 500 in total, 30 days).
  - **Always a person:** prices, commitments, inquiry replies, test contacts and held quality checks.
- **States:**
  - **Pause** blocks sending.
  - **Stop** blocks every job except monitoring and maintenance.
  - **Emergency stop** also stops all authorizations.
- **Suppression, footer and limits:**
  - email and domain suppression, checked at drafting, approval and send;
  - CAN-SPAM footer, opt-out wording and a List-Unsubscribe header;
  - a postal address is required.
- **Limits:**
  - 20 outreach emails a day, 1 per recipient domain a day, 5 per run;
  - business hours Monday–Friday 08:00–18:00 America/New_York;
  - test-mode recipient redirection.
- **Accountability:**
  - "Confirm again" for approvals given while sending was off;
  - an append-only audit log of control changes;
  - doctor checks for any email sent without approval, and for sample data in production.

## 6. Integrations (production configuration; values not shown)
| Integration | Configured | Status |
|---|---|---|
| Gmail SMTP | host, from and postal address set; **password not set** | Sending switched off; would also fail at sign-in until a password is entered |
| Gmail IMAP (replies, Calendly notices) | **not set** | Reply and meeting automation inactive in production (verified in acceptance on 27 Oct) |
| Calendly | booking link set | Link only. **There is no Calendly or calendar API client**: meetings are read from notification emails and never booked by the app (`aime/meetings.py:17`) |
| LinkedIn | app credentials and token set (token valid until 26 Dec) | Publishing switched off; 6 posts waiting for approval |
| SEC EDGAR | user-agent set | Working (45 companies, officer filings) |
| HN Algolia | on | Working (235 companies) |
| Census | **key not set** | Not used |
| Ollama (local AI model) | `auto` | **Not running** (nothing on port 11434), so posts use templates and replies get no AI second opinion |
| Inbound API | **token not set** | `/api/inbound` disabled; file intake only |
| Off-machine backup | folder and passphrase set | Encrypted copies written, but **OneDrive is not signed in**, so they stay on this PC |

## 7. Database and backups
- **Database:** 30 tables, 3,172 rows.
- **Integrity:** integrity and quick checks ok; 0 foreign-key violations, orphans, duplicates or stale runs.
- **Backups:**
  - 30 local backups, 46 MB in total (a daily automatic backup, plus pre-deploy and pre-migration copies);
  - restore drills passed on 28 Oct (`ffae950` and `2288f38` deployments).

## 8. Dashboard
- **Size:** 64 routes, 36 of them pages.
- **Access:**
  - sign-in is required (1 active administrator account);
  - administrator-only actions: mode changes, start/resume, campaign authorizations, job runs, LinkedIn connection;
  - any signed-in user can approve emails and posts (the Sales Manager role).
- **Pages:** dashboard, Sales Command Center (live status of the AI team), Approval Queue, CRM pages, Conversations, Follow-ups, Meetings, Automation Control, Audit log, Agents, Jobs, Sales Analytics, Integrations, System, Settings.

## 9. Logs, alerts, tests, CI
- **Logs:** `aime.log` has 0 ERROR lines; its warnings are schema-migration notices only.
- **Alerts:** 9 in total, 7 acknowledged, 2 open:
  - OneDrive not signed in;
  - a post slot left empty because its drafts failed quality checks.
- **Tests:** 505 automated tests, all passing on `2288f38` (offline mocks, isolated data folders).
- **CI on every PR:**
  - guards against secrets and outbound network access;
  - a secret, personal-data and lead-data scan;
  - ruff, including the bandit security rules;
  - a compile check;
  - the test suite;
  - `pip-audit`.

## 10. Environments
| Environment | Location | Notes |
|---|---|---|
| Production | `Documents\us-ai-marketing-employee` | Scheduler and dashboard; `data\` and `.env` are not in git |
| Development | `Documents\us-ai-marketing-employee-sprint` (worktree) | Branches and PRs |
| Acceptance | `Documents\aime-acceptance` (data + `acceptance.env`) | A production copy with the owner's real Gmail IMAP; sending off. **Labelled `production`** because no `AIME_DATA_ROLE` is set |
| Previews and rehearsals | `Documents\aime-acceptance\PREVIEW-*`, `DEPLOYCHECK-*` and similar | Throw-away copies, secrets blanked |
| Demo | `demo_data\` | Fictional data, role `demo` |

## 11. Findings, most important first
1. **Not 24/7.** All three scheduled tasks are interactive-only and never wake the PC. Work stops while you are logged off or the PC is asleep. A stored-password option exists for the Tick task but is not installed.
2. **Reply, follow-up and meeting automation is inactive in production.** Mailbox reading is not configured and sending is off with no SMTP password. These paths have test and acceptance evidence only.
3. **Thin reachability.** 5 of 42 qualified leads have a usable email, all of them company role mailboxes (see the lead audit).
4. **Autonomy is per recipient.** Campaign authorizations must name each exact address (at most 50). There is no rule-based authorization such as "qualified leads with a verified mailbox, within limits", so routine outreach still needs a person or a hand-made list.
5. **No meeting-booking integration.** Meetings come only from parsed Calendly emails.
6. **Ollama is not running.** Content uses templates, and replies get no AI second opinion.
7. **Pausing does not hold LinkedIn publishing.** Only Stop does (`aime/agents/publishing.py` never reads the automation state). Publishing is off today, so there is no current exposure.
8. **`NETWORK_ENABLED` is mentioned but not wired.** `aime/agents/company_research.py:78` tells the user to set it, but `aime/config.py` never reads it from the environment.
9. **Stale wording:**
   - `aime/config.py:167` and `.env.example` say `REQUIRE_HUMAN_APPROVAL` is "read for compatibility only", but the auto-approval path depends on it;
   - the `job_inbox` description says it sends approved emails, but it does not (`aime/workflows.py:139`).
10. **OneDrive is not signed in,** so off-machine backups stay on this PC.
11. **Business hours** use one time zone for every recipient, with no holiday calendar.
12. **The acceptance database is labelled `production`.**
13. **Minor:**
    - every `plan` run adds a marketing plan, and every research run adds a snapshot, even when nothing changed;
    - desktop notifications fail silently without a signed-in desktop (alerts are still stored).
