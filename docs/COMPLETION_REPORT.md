# Completion report: US AI Sales Employee (26 Oct 2026)

**Verdict: TEST READY.** Everything that can run without your accounts is built, tested and running in production
with outbound email and publishing off. It is not PRODUCTION READY for outreach: the live Gmail and Calendly
acceptance tests (9–14, 19) have not run, because they need your mailbox, postal address, Calendly link and a
test address.

## Production

| Item | State |
|---|---|
| Version | `main` `715f881` (#19–#24, deployed 26 Oct) |
| Health | doctor: 21 pass, 0 errors. Warning: off-machine backup needs its passphrase |
| Scheduler | every 15 min; 297 job runs and 243 agent runs, **0 failures** |
| Switches | `OUTBOUND_EMAIL_ENABLED=false`, `PUBLISHING_ENABLED=false`, `REQUIRE_HUMAN_APPROVAL=true` |
| Sent / published | 0 emails ever, 0 posts ever |
| Waiting for approval | 6 LinkedIn posts; 2 outreach drafts (to company inboxes `hello@meetyogi.com` and `info@balerion.ai`, held by quality checks: no postal address yet). Balerion AI is a competitor (see #25): reject that draft |
| Ollama | `llama3.2:latest` up. It was unreachable after the 21:03 restart until started by hand; `OLLAMA_AUTOSTART=true` would avoid that |

## Open PRs (not merged; each needs your approval)

| PR | What |
|---|---|
| #25 | Research accuracy. On a copy of production: 3 wrong contacts withdrawn (a city and two customers), 1 missed CEO found, 5 competitors flagged and disqualified; drafts of disqualified leads cancelled |
| #26 | `email-check`: settings, SPF/DKIM/DMARC/MX, SMTP and read-only IMAP sign-in, test email to your address only |
| #27 | Calendly reschedules, late cancellations, linking unmatched bookings |
| #28 | Today tiles (posts awaiting approval vs scheduled, emails awaiting approval) and the updated docs |

All four merge together in order with no conflicts, and the combined result passes 324 tests.

## Modules

All 16 are built and tested. Built and verified on real data or in production: discovery, research,
decision-makers, contact validation, scoring, CRM, outreach drafting, approvals, background jobs, dashboard,
reporting, security and operations. Built and tested with fake mail servers only: sending, follow-up steps,
reply reading and classification, and meetings from Calendly emails. Those need the live acceptance.

## Agents (production run history)

| Agent | Job (schedule) | Runs | Failures |
|---|---|---|---|
| research (discovery) | research (07:00) | 5 | 0 |
| company_research | enrich (07:15) | 1 | 0 |
| qualification | enrich, crm_maintenance | 13 | 0 |
| crm | inbound (30 min), crm_maintenance | 84 | 0 |
| sales_workflow | followups (hourly) | 43 | 0 |
| inbox_monitor | inbox (15 min); idle until IMAP is set | 4 | 0 |
| marketing_manager | plan (06:45) | 4 | 0 |
| linkedin_content | content (07:30); Ollama with template fallback | 4 | 0 |
| publishing | publish (30 min); switch off | 72 | 0 |
| analytics | daily (20:00) and weekly reports | 10 | 0 |

The AI model writes LinkedIn post drafts only. Research, contacts, scoring, outreach, reply handling and meetings
are deterministic: they quote published evidence and never invent names, titles or addresses.

## Lead generation (first production run)

15 companies researched (58 pages, 0 failures). 10 companies have named decision-makers: 44 found, 42 after #25's
corrections. 2 published a usable address, both company inboxes; none published a named person's address. Emails
are never guessed. Limitations: hiring-post discovery favours tech startups (5 of the 15 turned out to be
competitors), and team pages rendered by JavaScript are not read.

## Acceptance (19 points; isolated copy of production data, deployed code)

| Result | Tests |
|---|---|
| PASS | 1 discovery, 2 research, 3 decision-maker, 4 contact validation, 5 explainable score, 6 CRM, 7 outreach draft, 8 approval gate, 16 background jobs, 17 auth/audit/backups |
| PASS (checked separately) | 15 dashboard: all 23 pages render on real data and their counts match the database; 18 regression: full suite passes on every PR |
| BLOCKED (owner input) | 9 sending, 10 follow-up step, 11 reply detection, 12 reply updates CRM, 13 unsubscribe (CI covers it), 14 meeting, 19 full workflow |

Simulated results are never counted as live.

## Security and compliance (verified)

- **Sign-in:** every page requires sign-in; running jobs is admin-only; cross-site form posts are refused.
- **Approval:** every email needs a named approval, and the CAN-SPAM footer can't be removed or overridden.
- **Suppression:** checked at drafting, approval and send.
- **Limits:** 20 a day, 1 per domain a day.
- **Test mode:** delivers only to your address.
- **Secrets:** none in the repo (secret scan on every commit).

## Backups

- **Local:** verified daily; latest `aime-pre-workstreams-20261026-221643.db`, restore drill passed.
- **Rollback:** going back from `715f881` to `e6721c8` runs on the migrated database.
- **Off-machine:** folder set (`OneDrive\AIME-Backups`); the passphrase is yours to type.

## What you need to provide (one list)

1. `BACKUP_PASSPHRASE` (12+ characters, in `.env`, kept in your password manager).
2. A Google Workspace sales mailbox on your domain, with 2-Step Verification, an app password and IMAP enabled.
3. In `.env`: `SMTP_*`, `IMAP_HOST`, `SENDER_NAME`, `COMPANY_POSTAL_ADDRESS`, `UNSUBSCRIBE_EMAIL`.
4. SPF, DKIM and DMARC records on the sending domain (`email-check --domain` lists them).
5. A Calendly event link (`CALENDLY_URL`), with notifications going to the sales mailbox.
6. A test address you control, and your approval to run `docs/ACCEPTANCE.md`.
7. Approval to merge and deploy #25–#28, and then to run `research-recheck`.
8. Optional: `OLLAMA_AUTOSTART=true`, and the company website in `config/business.toml`.

## Remaining work and estimate

Once items 1–6 exist: about 1 hour for the live acceptance, plus fixes for anything it finds. There is no other
open development work.

## Authorizing live outbound (after a passing live acceptance)

1. Production `.env`: set the mailbox values, set `OUTBOUND_EMAIL_ENABLED=true`, and leave
   `OUTBOUND_TEST_RECIPIENTS` empty.
2. Run `python -m aime email-check --connect` and expect no FAIL.
3. Approve emails one by one in Follow-ups; the limits apply.
4. To stop at any time: `OUTBOUND_EMAIL_ENABLED=false`.
