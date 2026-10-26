# US AI Sales Employee: baseline audit and gap analysis (26 Oct 2026)

Scope: the 7-day plan (26 Oct to 2 Nov 2026) to extend this project into a supervised, end-to-end US
sales employee. This file records the verified starting point, the owner's decisions, the gap for every
module, and what the owner must provide. It is updated as modules land.

## Verified baseline (production, 26 Oct 2026)

| Item | Value |
|---|---|
| Deployed code | `main` at `e6721c8` (PRs #1–#18) |
| Automated tests | 240 passed; ruff and secret scan clean; `pip-audit` clean |
| Doctor | 18 pass, 0 fail; warnings: no dashboard account yet, off-machine backup not configured |
| Scheduler | Windows task every 15 min, last ticks successful; runs only while the owner is logged on |
| Companies | 200 (175 from public hiring posts, 25 from SEC EDGAR); 105 websites verified, 76 not yet checked |
| Leads | 199 (36 qualified, grade B; 163 nurture) with explainable score breakdowns |
| Contacts / opportunities / suppressions | 0 / 0 / 0 |
| Follow-ups | 36 open "review this company" tasks; no emails |
| Outbound email | OFF, and no mailbox configured (SMTP/IMAP empty, postal address missing) |
| LinkedIn publishing | OFF (6 posts awaiting approval) |

## Owner decisions (26 Oct 2026)

1. **Outreach**: B2B first-touch emails to researched decision-makers are allowed. Every message is
   approved by the owner; CAN-SPAM footer (opt-out and postal address), suppression and rate limits
   apply. Live sending stays off until a separate, explicit approval.
2. **Data**: free public sources only: the company's own published website pages (robots.txt honoured)
   and SEC filings. Emails are never guessed or pattern-generated; missing data is shown as not found.
3. **Mailbox**: Google Workspace / Gmail: SMTP to send, IMAP to read replies, app password in `.env`.
4. **Meetings**: Calendly link in replies; bookings detected from Calendly's confirmation emails in the
   monitored inbox.

## Module-by-module gap

Status: **Complete** (works and tested), **Partial** (foundation exists, scope items missing),
**Missing** (not built).

| # | Module | Status at baseline | What exists | What is missing |
|---|---|---|---|---|
| 1 | US business discovery | Complete | Hiring-post feed, SEC EDGAR by SIC/state, CSV import (no 40-row cap), Census sizing; industry/state focus from the daily plan; dedupe and merge; source URL on every record | — (more sources only if the owner approves them) |
| 2 | Company research | Partial | Hiring/filing signals with source links; homepage check (live, names the company, parked) | Reading the company's own pages (about, services, team, contact); structured research summary with quoted evidence |
| 3 | Decision-maker identification | Missing | — | People and titles from the company's own team/leadership pages and SEC insider filings, with page URL and quote; role relevance; "not found" when absent |
| 4 | Contact enrichment | Missing | Contacts only from inbound inquiries | Business emails and phones only where the company publishes them; syntax, domain and MX checks; verification status and confidence; no guessing; dedupe |
| 5 | Lead qualification | Partial | Fit/need/intent/data score with reasons, grades, categories, re-scoring | Use decision-maker and contact quality in the score; priority list |
| 6 | CRM management | Partial | Companies, leads, opportunities, activities, tasks, dedupe/merge, audit trail | Outreach stages, lead owner, contact records for outbound |
| 7 | Personalized outreach | Partial | Reply drafts for people who contacted us; approval gate | First-touch emails grounded in research, subject lines, follow-up sequence, quality checks for emails |
| 8 | Email sending | Partial | SMTP sender, approval record required, atomic claim (no double send), uncertain handling, suppression, business-hours window, CAN-SPAM footer, List-Unsubscribe | Gmail setup, daily and per-domain limits, controlled test-recipient mode, B2B footer, bounce handling |
| 9 | Automated follow-ups | Partial | Step intervals, max touches, never more than one open step | Automatic stop on detected reply, unsubscribe or bounce |
| 10 | Inbound reply intelligence | Missing | Web-form/API inquiries only | IMAP inbox reading, matching replies to contacts, classification, suggested replies, review queue, CRM updates |
| 11 | Meeting booking | Missing | — | Calendly link in replies, booking detection from confirmation emails, meetings in CRM, Sales Manager notification |
| 12 | Pipeline automation | Partial | Manual opportunity stages, stale-lead checks | Automatic stage moves (contacted, replied, interested, meeting), stalled-deal tasks |
| 13 | Sales Manager dashboard | Partial | Today page, leads, companies, pipeline, approvals, alerts, health | Decision-makers with confidence, outreach status, inbox, meetings, manual overrides for them |
| 14 | Reporting | Partial | Daily/weekly reports from records | Outreach, reply, meeting and conversion metrics |
| 15 | Background automation | Complete for existing jobs | Scheduler, retries, crash recovery, run logs, watchdog | New jobs: enrichment, outreach drafting, inbox polling |
| 16 | Security and operations | Complete | Sign-in with lockout, named approvals, encrypted tokens, verified/encrypted backups, restore drill, doctor, audit log, maintenance mode, rollback | Role restrictions for admin actions; doctor rules updated for the new outreach policy |

## Plan

| Day | Delivery |
|---|---|
| 26–27 Oct | Company research from the company's own pages; decision-makers from team pages and SEC insider filings with evidence; enrichment job; decision-maker view |
| 28 Oct | Published business emails, validation (syntax, domain, MX), confidence, dedupe; scoring uses contact quality |
| 29 Oct | Outreach drafting grounded in research, email quality checks, approval UI, outbound sequences, B2B footer |
| 30 Oct | Gmail SMTP/IMAP, rate limits, test-recipient mode, bounce and unsubscribe handling, follow-up stop rules |
| 31 Oct | Reply classification, suggested replies, Calendly booking detection, meetings, pipeline automation, dashboard, reports |
| 1 Nov | Integrated QA, recovery and security tests, documentation |
| 2 Nov | Deploy, acceptance test with a controlled test recipient, handover |

## Owner actions needed (none block development; some block acceptance)

| Needed by | Action |
|---|---|
| 30 Oct | Google Workspace / Gmail mailbox for sales; app password; `SMTP_*` and `IMAP_*` values in `.env` (owner types them) |
| 30 Oct | `COMPANY_POSTAL_ADDRESS`, `SENDER_NAME`, `UNSUBSCRIBE_EMAIL` (CAN-SPAM) |
| 30 Oct | SPF and DKIM on the sending domain |
| 31 Oct | Calendly account and booking link (`CALENDLY_URL`); Calendly notifications going to the monitored mailbox |
| 2 Nov | A controlled test recipient address the owner controls, and explicit approval to send the acceptance test email |
| Any time | `python -m aime user create` for the dashboard account |

## What will not be claimed

No real prospect reply, meeting, revenue or verified contact is reported unless it actually happened.
Simulated replies used in tests are labelled simulated. Any blocked dependency or failed test is reported
as NOT COMPLETE.
