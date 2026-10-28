# Sales workflow audit, 28 October 2026 (PR #45)

The 13 stages from company discovery to sales analytics, each with its implementation, its database integration and
what the records show.

**Evidence sources:**
- **Production database,** read-only (`mode=ro`), on `ffae950` with sending, publishing and mailbox reading off.
- **Acceptance database,** where the real Gmail and Calendly runs of 26–27 October were made.
- **The automated tests.**

Nothing was sent, published or authorized during the audit.

| # | Stage | Implementation | Database integration | Records (production unless noted) | Result |
|---|---|---|---|---|---|
| 1 | Company discovery | `agents/research.py` (US Business Research Agent); public sources: HN "who is hiring" and SEC EDGAR | `companies`, `signals` | 280 companies (235 HN, 45 SEC), none merged; `research` job daily 07:00; 7 runs in 7 days, 0 failed | PASS |
| 2 | Company research | `agents/company_research.py`: the company's own website and SEC filings | `research_snapshots`, `companies.website_status`, `contacts` | 45 companies researched, 141 websites verified, 60 research snapshots; `enrich` job daily 07:15; 4 runs, 0 failed | PASS |
| 3 | Lead qualification | `agents/qualification.py`, `scoring.py` (grades A–D, tiers) | `leads.score/grade/status`, `activities` (status changes) | 279 of 279 leads scored: 42 qualified, 228 nurture, 9 disqualified; 20 runs, 0 failed | PASS |
| 4 | Contact verification | `contact_validation.py`: published-on-site check, mail server (MX) check, role vs. personal address | `contacts.email_status`, `contacts.source` | 129 contacts; 7 have a usable address (all company role mailboxes published on the company's own site); 122 are named decision-makers without a published email | PASS (the code works; the data is thin, see gaps) |
| 5 | Personalized outreach draft | `agents/sales_workflow.py`, `outreach.py`: researched facts cited in each email | `followups` (kind `outreach`, `evidence`) | 6 outreach drafts, all 6 with evidence | PASS |
| 6 | Quality and compliance checks | `qc/email_checks.py`; compliance footer and suppression (`email_smtp.py`, `crm_ops.py`) | `followups.quality.qc`, `suppressions` | 5 passed, 1 held; 0 suppressions; doctor compliance checks pass | PASS |
| 7 | Human approval | Approval Queue, Follow-ups pages, `automation.py` (approval at send time; "Confirm again") | `followups.approved_by/approved_at`, `audit_events` | #38 and #46 approved by `admin` on 27 Oct and unsent; 3 waiting | PASS |
| 8 | Authorized sending | `integrations/email_smtp.py` behind `OUTBOUND_EMAIL_ENABLED`, `REQUIRE_HUMAN_APPROVAL`, authorizations, limits | `followups.sent_at/message_id`, `campaign_authorizations` | **Production:** 0 sent, by design (sending off). **Acceptance:** 2 real sends on 27 Oct in test mode, delivered to the owner's own Gmail instead of the prospects. **Tests:** send-loop, limits, authorization and approval gates. | BLOCKED in production (owner's switch); PASS in tests and acceptance |
| 9 | Reply classification | `agents/inbox_agent.py`, `reply_intel.py` (category and intent) | `inbound_emails` | **Production:** mailbox reading off; the inbox job reports "mailbox not configured" every 15 min (130 runs, 0 failed). **Acceptance:** real replies classified (1 interested, 3 Calendly notices). | BLOCKED in production (IMAP off by design); PASS in acceptance |
| 10 | Follow-up management | `agents/sales_workflow.py` sequences; Follow-ups page | `followups` (tasks, steps) | 51 tasks: 6 done, 37 pending, 8 cancelled. No step-2 emails yet, because no first email has been sent. | PASS for tasks; email follow-ups wait for sending |
| 11 | Meeting scheduling | `meetings.py` (Calendly notices from the mailbox), `CALENDLY_URL` in first emails | `meetings` | **Production:** link configured; 0 meetings, because notices arrive by mailbox, which is off. **Acceptance:** a real booking, reschedule and cancellation were all processed. | BLOCKED in production (IMAP off); PASS in acceptance |
| 12 | CRM pipeline update | `reply_intel.act`, `outreach.set_status`, Pipeline page | `opportunities`, `leads.outreach_status`, `activities` | **Production:** 0 opportunities (no replies yet); outreach status drafted 4, approved 2. **Acceptance:** 1 opportunity opened from the real interested reply. | PASS (acceptance); no production data yet |
| 13 | Sales analytics | `agents/analytics.py` (daily 20:00, weekly Monday 08:00); dashboard and Sales Analytics pages | `reports`, live counts | 3 daily and 2 weekly reports; 11 runs, 0 failed; every dashboard figure counted from the tables above | PASS |

## Integrity of the production database
`PRAGMA integrity_check` and `quick_check` both return ok, with 0 foreign-key violations and 0 orphan leads, contacts or follow-ups. There are no duplicate open domains, no leads on merged companies, and no stale "running" agent or job rows. All 248 job runs in the last 24 hours succeeded.

## Defects found and fixed in PR #45
1. **"Running" status:** an agent could be shown as running because of a leftover row from a job that had already finished. Now an agent is running only while its own job run is still open. Tested in `test_only_open_runs_count_as_running`.
2. **Misleading percentages:** KPI changes showed large percentages from tiny bases (1 → 3 showed "+200%"). With fewer than 5 in the previous period, the plain difference is shown instead. Tested in `test_small_previous_periods_show_plain_differences_not_percentages`.
3. **"Waiting for you" count:** upcoming meetings were counted as approvals. They are now excluded. Tested in `test_live_workspace_reports_failures_blocks_and_approvals`.
4. **Chart period:** Sales Performance always showed 7 days, even with the 30-day range selected. It now follows the range. Tested in `test_sales_performance_has_ten_real_series_and_honest_empty_views`.
5. **Missing expiry test:** nothing proved that an expired campaign authorization approves nothing. Added `test_an_expired_authorization_approves_nothing`.

## Gaps and external blockers
- **Local AI model:** Ollama is not running on this PC, so the content writer falls back to its templates. The monitor reports it.
- **Thin contact data:** only 7 of 129 contacts have a usable email address. Personal business emails would need paid enrichment, which the owner excluded.
- **Production switches:** sending, mailbox reading and publishing are off by the owner's decision. Stages 8, 9, 11 and 12 therefore have acceptance and test evidence, but no production records yet.
- **Off-PC backups:** encrypted off-machine copies are written to a OneDrive folder, but OneDrive is not signed in, so they never leave this PC.
- **Scheduler:** it runs only while the owner is logged on to Windows.
- **Next check-in:** this is derived from the scheduler's heartbeat and the 15-minute tick, not read from Windows Task Scheduler.
