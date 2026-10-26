# Live acceptance test (controlled, isolated)

Goal: one real, researched US company through every stage, with real Gmail and Calendly, **without
emailing the company and without changing production records**. Nobody but you receives anything.

## 0. What makes it safe

- A **separate data folder** (a copy of a verified backup): production records are never touched.
- A **separate settings file** (`AIME_ENV_FILE`): the production `.env` is not read, and the command line
  refuses to run if that file points at the production data folder.
- **Test mode** (`OUTBOUND_TEST_RECIPIENTS`): every email, whoever it is addressed to, is delivered only to your
  test address, with `[TEST for <real recipient>]` in the subject and a banner.
- Every email still needs your approval in the dashboard.

## 1. Prepare (about 10 minutes)

1. Take and verify a backup: `python -m aime backup --label pre-acceptance`.
2. Create `C:\aime-acceptance\data\` and copy the new backup file into it as `aime.db`.
3. Create `C:\aime-acceptance\acceptance.env` yourself (type the secrets; see `docs/SALES_SETUP.md`):

   ```
   AIME_DATA_DIR=C:\aime-acceptance\data
   OUTBOUND_EMAIL_ENABLED=true
   OUTBOUND_TEST_RECIPIENTS=<an address you control>
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USERNAME=<sales mailbox>
   SMTP_PASSWORD=
   SMTP_FROM=<sales mailbox>
   SENDER_NAME=<your name>
   COMPANY_POSTAL_ADDRESS=<street, city, state, ZIP>
   UNSUBSCRIBE_EMAIL=<sales mailbox>
   IMAP_HOST=imap.gmail.com
   CALENDLY_URL=<your Calendly event link>
   SEC_USER_AGENT=<as in your production .env>
   DASHBOARD_PORT=8770
   ```
   (Fill in `SMTP_PASSWORD=` with the app password yourself.)
4. In a new PowerShell window: `$env:AIME_ENV_FILE = "C:\aime-acceptance\acceptance.env"`. Every command in
   that window now uses the acceptance folder; close the window to end the session.
5. `python -m aime user create --username <you> --role admin` (in that window: the account lives in the acceptance
   copy only), then `python -m aime doctor` and `python -m aime status` (outbound: TEST MODE; mailbox configured).
6. `python -m aime email-check --connect --send-test`: settings, your domain's SPF/DKIM/DMARC, SMTP and IMAP
   sign-in, and one email to your test address only. Fix every FAIL before going on.

## 2. Run (each step maps to the acceptance tests)

| Step | Command / action | Tests |
|---|---|---|
| Discovery is already in the copy (the companies found from real sources) | `python -m aime acceptance-report` shows test 1 | 1 |
| Research companies' own sites and SEC filings | `python -m aime run enrich` (or `research-recheck` to re-read the ones already researched) | 2, 3, 4 |
| Re-score with contact quality | `python -m aime run crm_maintenance` | 5, 6 |
| Draft outreach | `python -m aime run followups` | 7, 8 |
| Open the acceptance dashboard | `python -m aime serve` → http://127.0.0.1:8770 (sign in) | 15 |
| Approve **one** outreach email in Follow-ups | dashboard | 8 |
| Send it (test mode: it reaches only you) | `python -m aime run followups` (weekday, 08:00–18:00 ET) | 9 |
| Reply to it from your test mailbox, e.g. "Sounds interesting, can we talk Thursday?" | your mailbox | — |
| Read the reply | `python -m aime run inbox` → Replies page: *interested* | 11, 12 |
| Approve the suggested reply (your Calendly link) and send it | dashboard, then `run followups` | 9 |
| Book a slot through the link from your test mailbox | Calendly | — |
| Record the booking | `python -m aime run inbox` → Meetings page | 14 |
| Reschedule it in Calendly (optional), then `run inbox` | Meetings page shows the new time, not two meetings | 14 |
| Reply "unsubscribe" from the test mailbox to a second test email (optional) | `run inbox` → suppression | 13 |
| Report | `python -m aime run daily_report` and `python -m aime acceptance-report` | 15, 16, 17, 19 |
| Regression | `python scripts/pytest_with_timeout.py 900` (development folder) and CI | 18 |

`acceptance-report` prints PASS / NOT YET / FAIL with evidence for every test and writes a Markdown copy to
`C:\aime-acceptance\data\reports\`. A pass that relied on test mode or simulated data says so.

## 3. Afterwards

Close the window (the `AIME_ENV_FILE` setting goes with it). The acceptance folder can be kept as evidence.
Going live is a separate decision: see `docs/SALES_SETUP.md` section 5.
