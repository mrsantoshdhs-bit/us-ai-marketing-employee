# Release readiness: PRs #19–#24 (US AI Sales Employee), 26 Oct 2026

Review before any merge or deployment. Nothing was merged or deployed; production data, Windows and security
settings were not changed. All database work below ran on scratch copies.

## Verdict

| Question | Result |
|---|---|
| Merge #19–#24 and deploy with outbound email and LinkedIn publishing **off** | **PASS** (once #24's CI is green) |
| Go live: send real outreach to prospects | **FAIL / not ready**: live acceptance has not run (needs your Gmail, postal address, Calendly link and test address) |
| Project "fully complete" (all 19 acceptance tests passed on real records) | **Not yet**: 3 of 19 pass on production records today; the rest need the live acceptance run |

## 1. CI and merge order

| PR | Branch | Head | CI |
|---|---|---|---|
| #19 | sales/01-research-decision-makers | 258224f | passed |
| #20 | sales/02-contacts-scoring | f41cc62 | passed |
| #21 | sales/03-outreach | 6365ebf | passed |
| #22 | sales/04-email-inbox | 105bfe3 | passed |
| #23 | sales/05-meetings-pipeline | 3449a4b | passed |
| #24 | sales/06-acceptance-security | review fixes pushed | see the PR |

Each branch contains the one before it plus its own commits; the whole stack merges into `main` (e6721c8) with no
conflicts. Merge in order #19 → #24.

## 2. Modules (automated tests; live checks where possible)

All 16 modules are built and covered by tests, including one test that takes a company through every stage in
test mode (`tests/test_end_to_end_sales.py`). Research, decision-makers and contact checks were also run on real
companies using a copy of production data. Email, inbox and Calendly were tested with a fake mail server and
mailbox only; **they have not run against your real Gmail or Calendly**. Status per module:
`docs/SALES_EMPLOYEE_GAP_ANALYSIS.md`.

Acceptance report on production records today: tests 1 (discovery), 5 (explainable score) and 17 (auth, audit,
backups) PASS; the rest are NOT YET because no research, outreach, reply or meeting has happened on real records.

## 3. Database migration, backups, rollback

Run on a read-only snapshot of the live database, in a scratch folder:

- Additive only: 3 new tables (`research_snapshots`, `inbound_emails`, `meetings`) and 22 new nullable columns
  (companies 2, contacts 11, leads 3, follow-ups 6). No column dropped, renamed or retyped.
- 1,705 rows before; no existing row changed by the migration. Integrity `ok`, 0 foreign-key violations.
- A pre-migration backup is taken automatically before the first change.
- Doctor on the migrated copy: 19 pass, 0 errors (warnings: off-machine backup not set; no tick in a copy).
- **Rollback by code**: the current production code (e6721c8) opened the migrated database, read every row of
  all 25 tables, passed doctor (18/18) and wrote new rows; the new code then read those rows back. Going back to
  e6721c8 needs no database restore.
- **Rollback by restore**: restore drill of the newest backup passed (integrity ok, 25 tables, 1,626 rows).

## 4. Outbound email and LinkedIn publishing

- Production `.env`: `OUTBOUND_EMAIL_ENABLED=false`, `PUBLISHING_ENABLED=false`, `REQUIRE_HUMAN_APPROVAL=true`.
  SMTP, IMAP, postal address and Calendly are empty, so nothing can be sent even if the switch were turned on.
- Defaults in code are off for both switches. Deploying the PRs does not change `.env`.

## 5. Controls

| Control | Evidence |
|---|---|
| Sign-in | Every page needs sign-in except the login page, static files and the token-protected inbound API; lockout after 5 failures |
| Roles | Running jobs and connecting LinkedIn are admin-only (403 otherwise); Sales Managers approve and edit. Roles change only from the command line (`user set-role`) |
| Approval | Nothing is sent without a named approval; "approved" without an approval record is reset |
| Quality checks | Held or failed drafts need an explicit, logged override |
| Compliance footer | Opt-out line and postal address cannot be removed or overridden (added in this review) |
| Suppression | Checked at drafting, approval and send time; unsubscribe replies and bounces suppress automatically and cancel queued emails |
| Limits | 20 outreach emails per day, 1 per company domain per day, 5 per run, weekdays 08:00–18:00 ET |
| Test mode | `OUTBOUND_TEST_RECIPIENTS` delivers every email only to your test address, marked `[TEST for …]` |
| Acceptance isolation | `AIME_ENV_FILE` uses a separate settings file and data folder; refused if it points at production data |
| Simulated replies | `simulate-reply` is refused on live production data (added in this review) |

## 6. Found and fixed in this review (in #24)

1. CI failure: the acceptance-isolation test was affected by CI's `AIME_DATA_DIR`. Test fixed.
2. `simulate-reply` could add a simulated reply to live production records. Now refused there.
3. Editing an email could remove its CAN-SPAM footer, and the quality override would let it be approved. Now blocked
   at approval and again at send time; drafts written before the postal address was set get it filled in on approval.
4. After a refused approval, the reason never showed (the message was placed after the `#` anchor). Fixed.
5. Production's only dashboard account is a Sales Manager, so after #24 nobody could run jobs or reconnect LinkedIn
   from the dashboard. Added `python -m aime user set-role --username <you> --role admin` (command line only,
   audit-logged) and a doctor warning when there is no admin.

Tests after the fixes: 310 passed with CI's environment settings; ruff and secret scan clean.

## 7. Remaining blockers and limits

| Item | Blocks | Owner |
|---|---|---|
| Gmail sales mailbox, app password, `SMTP_*`/`IMAP_*` in `.env` | Live acceptance and go-live | You |
| `COMPANY_POSTAL_ADDRESS`, `SENDER_NAME`, `UNSUBSCRIBE_EMAIL` | Any sending (CAN-SPAM) | You |
| SPF and DKIM on the sending domain | Deliverability | You |
| Calendly link (`CALENDLY_URL`), notifications to the sales mailbox | Meeting detection | You |
| A test address you control, and approval to run `docs/ACCEPTANCE.md` | Acceptance tests 2–19 on real records | You |
| Make your existing account admin after deploying (`user set-role`, step 4 below) | Running jobs from the dashboard | You |
| `pip install -r requirements.txt` at deploy (adds `dnspython`) | Deploy step | Deploy |
| 12 stuck Python processes (security software interference) | Cleared by your Windows restart at 21:03; CPU normal | — |

Known limits: few companies publish named email addresses, so outreach volume will be low (no guessing, by
design). Replies to older inbound web-form enquiries keep their footer from the template but do not have the new
hard footer check. Off-machine backups are not configured.

## 8. Deployment steps (only after your approval)

1. `python -m aime backup --label pre-sales-deploy` and verify it.
2. Merge #19, #20, #21, #22, #23, #24 in that order.
3. Production folder: `git pull`, `pip install -r requirements.txt`, tests, `python -m aime doctor`.
4. `python -m aime user set-role --username <your account> --role admin`.
5. Restart the dashboard; confirm the next tick succeeds; outbound and publishing stay off.
6. Rollback: redeploy e6721c8 (database stays compatible), or restore the pre-deploy backup.
