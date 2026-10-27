# Operational readiness report: US AI Sales Employee (27 Oct 2026)

This supersedes `docs/COMPLETION_REPORT.md` (26 Oct).

## 1. Executive summary

**Verdict: READY FOR CONTROLLED TESTING.**

What works today:
- The system runs unattended in production. It finds US companies, researches their own websites and SEC
  filings, names decision-makers with quoted evidence, scores leads, keeps the CRM up to date and drafts
  outreach.
- Outbound email and LinkedIn publishing are off. Every recorded job run (355) and agent run (296) has
  succeeded.

Why it is not ready for live operation:
- **The live Gmail and Calendly acceptance has not run.** Tests 9–14 and 19 need your mailbox, postal
  address, Calendly link and a test address.
- **The off-machine backup does not work yet.** OneDrive is installed but not signed in, so
  `OneDrive\AIME-Backups` never leaves this PC, and the passphrase is empty.
- **The deployed version (`6844bfe`) has defects that are fixed but not deployed.** A code audit on 27 Oct
  found about 45. The most serious:
  - Gmail connections did not verify the server's certificate.
  - A company inbox printed under a name could become that person's email.
  - An unreadable robots.txt was treated as "allowed".
  - A delay notice counted as a hard bounce.
  - Our own quoted "reply unsubscribe" line could unsubscribe a prospect.
  - The dashboard could be down for hours with nothing to restart it.

  All are fixed in PRs #29–#32, which are not merged. Together they pass 382 tests.

## 2. Verified production baseline (27 Oct, 05:18–05:25 local)

Checked against GitHub, `.env` (secrets shown only as set/empty), a read-only database snapshot, Task
Scheduler, the logs, Ollama and the dashboard. Nothing in production was changed.

| Item | Reported | Verified |
|---|---|---|
| Production commit | `6844bfe` | `6844bfe`, equal to `origin/main`. Only your personal files are untracked |
| Tests | 324 passed | 324 passed on `main` |
| Dashboard | operational | Running, started 05:18 from Windows Terminal. **Down from about 00:21 to 05:18**: it had been hosted by the Claude app's preview, and nothing restarts it |
| Scheduler | operational | `AIMarketingEmployee-Tick` enabled, every 15 min, last result 0. 355 job runs, all successful; 0 abandoned |
| Ollama | `llama3.2:latest` | Up. A real local generation took 50 s and passed the quality checks |
| Database | additive migrations | `integrity_check` ok, 0 foreign-key violations, 28 tables, 1,988 rows |
| Decision-makers | 42 | 42 named people at 10 companies, plus 2 company inboxes; 3 withdrawn |
| Qualified leads | 31 | 31: all grade B, scores 55–68 |
| Competitors | 5 disqualified | 5 leads disqualified. 6 companies flagged (Stellar Science has no lead) |
| Draft awaiting approval | `hello@meetyogi.com` | Follow-up #38 (lead #3) |
| Switches | off | `OUTBOUND_EMAIL_ENABLED=false`, `PUBLISHING_ENABLED=false`, `REQUIRE_HUMAN_APPROVAL=true` |
| Sent / published | 0 | 0 emails ever, 0 posts ever, 0 publish attempts |
| Local backups | verified | Daily backup verified 14 h earlier. doctor: 21 pass, 0 errors |
| Off-machine backup | pending | Passphrase empty, and **OneDrive is not signed in** |
| Dependencies | — | `pip-audit`: no known vulnerabilities in the 61 installed packages |
| Alerts on Today | — | 4 shown, of which 3 were stale ("No account yet", "Ollama unreachable", "monitor has not succeeded"). Fixed in #29 |

## 3. Module status

**Live** means verified on real data or in production. **Fakes only** means tested in CI with fake mail
servers, not yet against your real Gmail or Calendly.

| Module | Status | Evidence |
|---|---|---|
| Company discovery (HN hiring posts, SEC EDGAR, your CSV lists) | Live | 200 companies (175 HN, 25 SEC) |
| Website verification | Live | 105 verified, 5 mismatch, 9 blocked, 4 unreachable, 76 not yet checked |
| Company research (own website, robots.txt) | Live; fixes in #30 | 15 researched: 10 with people, 4 without, 1 blocked |
| Decision-makers | Live; fixes in #30 | 42 named, all with the quoted page as evidence |
| Contact validation | Live; fixes in #30 | 2 usable addresses, both company inboxes |
| Scoring and qualification | Live; fixes in #30 | 31 qualified, 163 nurture, 5 disqualified; each score lists its reasons |
| CRM (companies, contacts, leads, activities, tasks) | Live | 478 activity entries, 30 review tasks open, 0 duplicate open leads |
| Opportunities and pipeline | Fakes only | 0 opportunities: none until a real reply |
| Outreach drafting and quality checks | Live | Draft #38 quotes its evidence |
| Approval gate | Live | doctor: 0 emails without an approval record |
| Sending (Gmail SMTP) | Fakes only; fixes in #32 | Off; never sent |
| Follow-up sequences | Fakes only; fixes in #32 | — |
| Inbox and reply classification | Fakes only; fixes in #32 | The job runs every 15 min and is idle until IMAP is set |
| Calendly meetings | Fakes only; fixes in #32 | — |
| Suppression and unsubscribe | Fakes only | 0 suppressions |
| LinkedIn content | Live | 6 drafts await your approval |
| LinkedIn publishing | Off | Token valid until 24 Dec 2026 |
| Dashboard | Live; fixes in #31 | Every page renders on today's data, and Today's figures match the database |
| Reports | Live | 4 reports (daily, weekly) |
| Scheduler and recovery | Live; fixes in #29 | 355 runs, 0 failures |
| Backups | Local live; off-machine not working | Restore drills pass; OneDrive not signed in |

## 4. Agents

| Agent | Job (local time) | What it does | Human boundary | Runs / failures |
|---|---|---|---|---|
| marketing_manager | plan 06:45 | Picks the day's industries and states | — | 4 / 0 |
| research (discovery) | research 07:00 | Reads HN hiring posts and SEC EDGAR; dedupes; saves companies, signals and leads | Public sources only | 5 / 0 |
| company_research | enrich 07:15 | Checks websites; reads up to 6 own pages (robots.txt honoured) and SEC officers; checks emails' mail servers | Never contacts anyone | 2 / 0 |
| qualification | enrich, crm_maintenance 06:30, inbound | Scores fit, need, intent and data with reasons; qualifies, nurtures or disqualifies | Sales Manager overrides are kept | 14 / 0 |
| crm | inbound (30 min), crm_maintenance | Imports inquiries; merges duplicates; ages stale leads | — | 96 / 0 |
| sales_workflow | followups (hourly), inbound | Drafts outreach and responses; sends **approved** emails within limits and business hours; pipeline steps | **Every email needs your approval** | 49 / 0 |
| inbox_monitor | inbox (15 min) | Reads the mailbox read-only; classifies replies, bounces, unsubscribes and Calendly emails | Uncertain replies go to review | 28 / 0 (idle: no mailbox) |
| linkedin_content | content 07:30 | Writes post drafts with Ollama (template fallback) and checks their quality | **Every post needs your approval** | 4 / 0 |
| publishing | publish (30 min) | Publishes approved posts through the official API | Master switch off | 84 / 0 |
| analytics | daily 20:00, weekly Mon 08:00 | Daily and weekly reports | — | 10 / 0 |
| monitor and doctor | monitor (30 min), db_maintenance 06:15 | Health checks, alerts, verified backups | — | 83 / 0 |

- **Retries:** each agent gets up to 3 attempts with backoff. The sales workflow gets 1 attempt, so a send is
  never repeated.
- **Sending safety:** sends use an atomic claim, and an unclear outcome becomes "uncertain" for a person to
  resolve.
- **What still needs you:** without anyone stepping in, the system finds, researches, scores and drafts. You
  approve emails and posts, decide on replies sent to review, and take the meetings.

## 5. End-to-end workflow coverage

| Stage | Covered by | Live evidence |
|---|---|---|
| Discovery → website check → research → decision-maker → contact check → score → CRM | Production | Yes (tests 1–6) |
| Personalized draft → human approval | Production | Yes: draft #38 (tests 7, 8) |
| Send through real SMTP → reply through real IMAP → classification → opportunity | CI with fake servers | **Blocked:** needs your mailbox (tests 9–12) |
| Unsubscribe and suppression | CI | **Blocked** (test 13) |
| Calendly booking → meeting → pipeline update | CI | **Blocked:** needs your Calendly link (test 14) |
| Reports | Production | Yes (tests 15, 16) |

## 6. Database integrity and backups

- **Integrity:** ok, with 0 foreign-key violations.
- **Migrations:** additive only, with a backup before each. #29 adds two nullable columns (`alerts.key`,
  `alerts.resolved_at`). The other PRs have no schema change.
- **Local backups:** a verified backup every day, 14 kept; deploy and migration backups are never pruned.
  Latest: `aime-pre-migration-20261026-233356.db`.
- **Encrypted off-machine drill:** run on an isolated copy of today's data, with a throwaway passphrase and a
  scratch folder.
  - The file was AES-256-GCM with a scrypt key, and contained no readable table data.
  - It restored with 28 tables and 1,988 rows.
  - A wrong passphrase was refused, and so was a damaged file.
  - The bundle holds the database, config and a checksum manifest. `.env` is never included.
- **Not working:**
  - `BACKUP_PASSPHRASE` is empty.
  - No OneDrive account is signed in (neither the Personal nor the Business account has a user folder), so
    `OneDrive\AIME-Backups` in your user folder is just a local folder.
  - With #29, doctor reports this as "Off-machine folder leaves this PC".
- **Disaster recovery:**
  - Restore a local backup: `docs/DEPLOYMENT.md`.
  - Restore an encrypted copy: `restore-drill --source <file> --extract-to <new folder>`, then put that
    `aime.db` in `data/`.
  - Keep the `.env` values in your password manager.

## 7. Gmail / Google Workspace

| Check | Result |
|---|---|
| SMTP send, read-only IMAP, threading, bounces, unsubscribes, reply matching | Built; tested with fake servers |
| Mailbox settings (`SMTP_*`, `IMAP_HOST`, `SENDER_NAME`, `COMPANY_POSTAL_ADDRESS`, `UNSUBSCRIBE_EMAIL`) | **All empty** |
| `email-check`: settings, SPF/DKIM/DMARC/MX, sign-in, a test email to your address only | Ready; needs the mailbox |
| TLS certificate and host-name verification | **Missing in `6844bfe`; fixed in #32** |
| Live sign-in, test email and real reply | **BLOCKED**: needs your mailbox |

## 8. Calendly

- **Built and tested with fake emails:**
  - Calendly's booked, cancelled and rescheduled emails are parsed.
  - Duplicate bookings are ignored.
  - An unmatched booking can be linked to a lead.
  - Re-booking a cancelled time is fixed in #32.
- **Not configured:** `CALENDLY_URL` is empty.
- **Live test booking: BLOCKED.** It needs your Calendly link, and Calendly's notifications going to the
  monitored mailbox.

## 9. LinkedIn

- **Connection:** official OAuth with scopes `openid,profile,w_member_social`. The token is DPAPI-encrypted
  and expires on **24 Dec 2026**, so reconnect before then.
- **Publishing:** blocked by `PUBLISHING_ENABLED=false`. Six drafts wait for your approval; nothing has been
  published.
- **Safeguards:** the master switch, an approval record, an atomic claim, a journal written before the
  database, duplicate reconciliation and uncertain-on-timeout.
- **Fixed in #31:** an expired token now shows as "expired: reconnect"; editing a post can no longer race the
  publisher; and old "blocked" notes no longer show as blocks. There is no scraping.

## 10. Ollama

- **Status:** `llama3.2:latest` at 127.0.0.1:11434.
- **Use:** LinkedIn drafts only. Research, contacts, scoring, outreach, replies and meetings are deterministic,
  and quote their evidence.
- **When it is down:** the template writer takes over and one alert is raised. With #29 that alert closes
  itself once Ollama is back, and each job run re-checks the model.
- On 26 Oct Ollama did not start after a restart. Add it to Startup apps or set `OLLAMA_AUTOSTART=true`.

## 11. CRM and dashboard verification

All 36 page requests rendered on a copy of today's data, with no server errors: Today, Replies, System,
Leads (all, qualified, disqualified), Decision-makers, Companies, Pipeline, Meetings, Follow-ups, Content,
Policy, Campaigns, Inbound, Agents, Jobs, Reports, Integrations, the APIs, and the detail pages. Unknown IDs
give 404.

The 11 Today figures matched direct database counts:
- 200 companies found;
- 105 websites verified;
- 10 companies with a decision-maker;
- 2 companies with a usable email;
- 31 qualified companies;
- 1 email awaiting approval;
- 6 posts awaiting approval;
- 0 posts scheduled;
- 0 posts published;
- 0 outreach emails sent;
- 0 meetings booked.

Both runs passed: main, and the four PRs merged together. The definitions behind several figures are fixed
in #31 (withdrawn and opted-out contacts, distinct companies, cancelled meetings), and the Follow-ups page
now always lists every email awaiting approval.

## 12. Lead and contact quality

| Measure | Value |
|---|---|
| Companies discovered | 200 (no samples, no duplicate domains) |
| Companies researched (own website) | 15 |
| Companies with a named decision-maker | 10 |
| Named decision-makers | 42: 19 executive, 4 technology, 4 sales, 3 operations, 2 marketing, 2 finance, 1 people, 7 other |
| Usable business emails | 2, both company inboxes: `hello@meetyogi.com`, and `info@balerion.ai` (Balerion is a competitor) |
| Named people with a published email | 0 |
| Qualified leads | 31, all grade B |
| Competitors excluded | 5 leads |

**Research re-check with all four PRs**, on a copy of today's data with real site reads (robots.txt honoured):
- 10 companies re-read, 0 failures.
- 0 contacts withdrawn or changed.
- One lead's score fell from 68 to 63 under the stricter recency rule; it is still qualified.

So deploying the PRs and running `research-recheck` does not disturb the current records.

**Practical limits:**
- **Few emails are published.** Small US companies rarely publish named people's email addresses, and the
  system never guesses them. Most researched companies end with a named decision-maker but no address.
  Outreach then relies on a published company inbox, your own CSV lists (with their source), or inbound
  inquiries.
- **Discovery leans to tech.** The hiring-post source favours tech startups, and 5 of the 15 researched
  companies turned out to be competitors.
- **Grade A is out of reach for outbound leads.** They top out at about 73 points because intent counts only
  evidence, which is grade B. Grade A (75+) in practice means a company that contacted you. This follows the
  scoring rules; changing it is your decision.

## 13. Automated tests

| Code | Tests | Lint / secret scan | CI |
|---|---|---|---|
| `main` (`6844bfe`) | 324 passed | clean | passed |
| #29 operations | 339 passed | clean | passed |
| #30 research | 337 passed | clean | passed |
| #31 dashboard and security | 335 passed | clean | passed |
| #32 mail | 343 passed | clean | passed |
| #29–#32 merged together | **382 passed** | clean | merged locally, no conflicts |

Every fix has a regression test that failed on the old code.

## 14. Live acceptance evidence (19 points)

From `acceptance-report` on a copy of today's production data:

| Result | Tests |
|---|---|
| PASS | 1 discovery (company #1 Quobyte, from an HN hiring post), 2 research (snapshot #30, curaihealth.com, 4 pages quoted), 3 decision-maker (contact #43, CTO, from toolhouse.ai/en/about), 4 contact validated (contact #1, role address published at meetyogi.com/get-a-demo, mail server checked), 5 explainable score (lead #3: 68/100 B, 11 reasons), 6 CRM (lead #2, company #2, contact #11), 7 outreach draft (#38, evidence quoted), 8 approval gate, 16 background jobs (all 13 succeeded in the last 26 h), 17 sign-in, audit log (478 entries), verified backup |
| PASS, checked separately | 15 dashboard (section 11), 18 regression (section 13) |
| **BLOCKED** (owner input) | 9 send through real SMTP, 10 follow-up step, 11 reply through real IMAP, 12 reply updates CRM, 13 unsubscribe (covered in CI), 14 Calendly booking, 19 full workflow |

No simulated result is counted as live.

## 15. Security and compliance

**Verified:**
- Every page requires sign-in; running jobs is admin-only.
- Cross-site form posts are refused.
- Passwords are hashed with scrypt; session cookies are HttpOnly and SameSite.
- Templates autoescape, CSP is set, and there is no string-built SQL.
- The LinkedIn token is DPAPI-encrypted.
- Secrets are redacted in logs.
- No secrets are in the repository.
- Email compliance:
  - Every email needs a named approval and the CAN-SPAM footer.
  - Suppression is checked at drafting, approval and send.
  - Limits are 20 a day and 1 per domain.
  - Test mode reaches only your address.
- Dependencies: no known vulnerabilities.

**Fixed in the PRs (not deployed):**
- **High:** no TLS certificate check for Gmail SMTP/IMAP, so the app password could be intercepted (#32).
- **High:** wrong email owners, and robots.txt failures read as "allowed" (#30).
- **Medium:**
  - The inbound API accepted posts from any web page, including a marketing opt-in (#31).
  - Send limits could break when runs overlapped (#32).
  - Inquiry sequences did not stop after a reply (#32).
  - Replies to inquiries skipped the footer check (#32).
  - Delay notices were treated as bounces (#32).
  - Different companies could be merged (#30).
  - A person's "nurture" decision could be undone automatically (#30).
- **Low:**
  - A parallel-login lockout bypass (#31).
  - Gaps in the secret scan, and `*.env` files not ignored (#31).
  - The test host name was accepted in production (#31).

## 16. Fixes and pull requests

| PR | Content | Findings |
|---|---|---|
| [#29](https://github.com/mrsantoshdhs-bit/us-ai-marketing-employee/pull/29) | Dashboard restart task, OneDrive sync check, self-closing alerts, crash recovery, per-slot retries | 10 |
| [#30](https://github.com/mrsantoshdhs-bit/us-ai-marketing-employee/pull/30) | Email owners, robots.txt and redirects, menus, merges, nurture, competitor clearing, SEC officers, email re-checks | 12 |
| [#31](https://github.com/mrsantoshdhs-bit/us-ai-marketing-employee/pull/31) | Inbound API token, lockout, true figures, approvals always listed, posts and forms, secret scan | 12 |
| [#32](https://github.com/mrsantoshdhs-bit/us-ai-marketing-employee/pull/32) | Verified TLS, send lock, sequence stops, footer on every email, reply and bounce accuracy, Calendly re-booking, threading | 13 |
| #33 | This report | — |

**Not fixed, reported only:**
- The scoring ceiling (section 12).
- An SEC CIK can still be lost when two records with different ids merge (there is only one id field).

## 17. Outstanding blockers

1. The live Gmail acceptance needs your Google Workspace mailbox and settings.
2. The live Calendly acceptance needs your Calendly link.
3. The off-machine backup needs OneDrive signed in and a passphrase.
4. The fixes need your approval to merge and deploy.

## 18. What you need to do

1. **Approve merging and deploying #29–#32**, then #33, in order.
2. **Sign in to OneDrive** (cloud icon in the taskbar) and check that `AIME-Backups` shows as synced.
3. **Set the backup passphrase:** type a passphrase of 12+ characters after `BACKUP_PASSPHRASE=` in `.env`
   and keep it in your password manager. Then run `python -m aime backup --label first-offsite --offsite`
   and `python -m aime restore-drill --source <the .aimebak file>`.
4. **Set up the sales mailbox:** a Google Workspace mailbox on your domain, with 2-Step Verification, an app
   password and IMAP enabled.
5. **Fill in the mail settings yourself** in `.env` (passwords are never typed into chat): `SMTP_HOST`,
   `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SENDER_NAME`, `COMPANY_POSTAL_ADDRESS`,
   `UNSUBSCRIBE_EMAIL`, `IMAP_HOST`.
6. **Add DNS records:** SPF, DKIM and DMARC on the sending domain. `python -m aime email-check --domain <your domain>` lists what is missing.
7. **Calendly:** an event link in `CALENDLY_URL`, with notifications going to the sales mailbox.
8. **Live acceptance:** a test address you control, and your approval to run `docs/ACCEPTANCE.md`.
9. **Optional, each needs your approval:**
   - `python -m aime scheduler install-dashboard` (restarts the dashboard), and `install-watchdog`.
   - Ollama in Startup apps, or `OLLAMA_AUTOSTART=true`.
   - Your company website in `config/business.toml`.
10. **Decide on draft #38** (`hello@meetyogi.com`).
11. **Reconnect LinkedIn** before 24 Dec 2026.

## 19. Remaining risks and limitations

- Nothing runs while the PC is off or asleep. Ticks catch up when it wakes.
- Until the dashboard task is installed, a closed window stops the dashboard.
- Until OneDrive syncs, a disk failure loses the database.
- Contact coverage is limited by what companies publish, and pages rendered by JavaScript are not read.
- Reply classification uses rules. Uncertain, negated or quoted-only replies go to your review queue rather
  than being acted on.
- Gmail may show follow-up steps as separate conversations, because their subjects differ.

## 20. Deployment and rollback

**Deploy** (same flow as 26 Oct):
1. Verify a fresh backup.
2. `maintenance on`.
3. Merge #29, #30, #31, #32 and #33 in that order.
4. Fast-forward the production folder.
5. Run the tests and `doctor` (#29's migration takes a backup first).
6. Restart the dashboard.
7. `maintenance off`.
8. Confirm the next tick.

Optionally, run `research-recheck` afterwards; on a copy it changed one score and nothing else.

**What changes after deploying:**
- `POST /api/inbound` refuses requests until `INBOUND_API_TOKEN` is set.
- `SMTP_USE_TLS=false` is refused.
- More replies go to review.
- Stale alerts close themselves.

**Rollback:** follow `docs/DEPLOYMENT.md`, either a code rollback that keeps the data (the schema changes
are additive) or a restore from the pre-deploy backup.

## 21. Verdict

**READY FOR CONTROLLED TESTING.**

Evidence for that:
- Production has run unattended with 0 failures in 355 job runs.
- The research and CRM stages are verified on real data.
- Every outbound path is gated by your approval, with the switches off.
- The fixes for every defect found in the audit pass 382 tests together.

Not yet READY FOR LIVE OPERATION:
- The real Gmail and Calendly path has never run.
- The off-machine backup does not leave the PC.
- The fixes are not deployed.

Once items 1–8 in section 18 are done and the live acceptance passes, live outreach still needs your
separate, explicit authorization.
