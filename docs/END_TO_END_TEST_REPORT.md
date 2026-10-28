# End-to-end test report, 28 October 2026

**Code under test:** branch `sales/28-autonomy-audit`, the head of this pull request.

**Environment.** Every run was isolated from production:
- **Automated tests** use a fresh scratch data folder (`AIME_DATA_ROLE=test`) with sending and publishing forced off by the environment, except where a test switches them on against a **fake** SMTP server.
- **Contacts** are mock contacts at reserved `.example` domains, which cannot receive mail.
- **Mail** is simulated: in-process fake SMTP and IMAP servers. No network connection is made.
- **The clock** is simulated: a Tuesday, 11:00 US Eastern.
- **Preview checks** used a read-only copy of the production database. The copy is labelled "acceptance", its LinkedIn token is removed, every password and key is blanked, and sending, mailbox reading and publishing are forced off.

**Not tested:** real email delivery, a real mailbox, Calendly, a changed scheduled task, or a run while signed out. Each of those needs the owner's authorization (see [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md)).

## 1. End-to-end chain (`tests/test_e2e_sales_autonomy.py`)

One test runs the whole chain with a mock contact at `brightline-logistics.example`:

| # | Step | Expected | Result |
|---|---|---|---|
| 1 | Discovery: a company found through an SEC-style record | It stores why it counts as a US company ("US state DE (SEC filing business address)") | PASS |
| 2 | Research and drafting: a researched lead gets a first outreach email | Drafted with evidence; **waits for a person**; nothing at the fake SMTP server | PASS |
| 3 | Manager Today, before any authorization | The email is listed under "Needs you", with the recipient and the reason | PASS |
| 4 | A rule-based authorization saved as a **draft** (grade B and better, published addresses, verified website) | Nothing goes out | PASS |
| 5 | An administrator activates it | The matching outreach goes out under the rule: `approved_by = campaign #N`, one message at the fake server; Manager Today no longer lists it | PASS |
| 6 | A simulated pricing reply arrives (fake IMAP, read-only) | Classified `pricing_request`, status `needs_review`; Manager Today lists it with "pricing"; its drafted answer **waits for a person** (a rule covers outreach only) | PASS |
| 7 | A simulated meeting request arrives | Classified interested / `meeting_request`; listed under "Meeting requests" and "Interested replies"; the page shows 1 meeting request not yet booked | PASS |
| 8 | Pause, with an approved answer waiting | Nothing is sent | PASS |
| 9 | Emergency stop | The follow-ups job is skipped, and the authorization is stopped | PASS |
| 10 | Audit trail | `authorization_created`, `authorization_activate` and `auto_approved` are all recorded | PASS |

## 2. Tests for each item

| Item | Tests | Result |
|---|---|---|
| 1. Defect fixes | `test_audit_fixes.py` (3): pause holds publishing; `NETWORK_ENABLED` is read; approvals reach the audit log | PASS |
| 2. Manager Today | `test_manager_today.py` (4): every section with honest empty states; only leads qualified today; each exception has its reason; covered emails are not exceptions | PASS |
| 3. Scheduler readiness | `test_scheduler_unattended.py` (3): the wake and signed-out settings are read back from the task definition; the readiness lines say plainly what stops when the owner signs out; install passes the wake option and lets Windows ask for the password. All use a fake `schtasks` | PASS |
| 4. Rule-based authorizations | `test_rule_authorizations.py` (9) checks four things: <ul><li>a matching email goes out under the rule;</li><li>below grade, unverified website, not qualified or a disallowed address each wait for a person;</li><li>`REQUIRE_HUMAN_APPROVAL`, a draft rule and Approval mode each still hold;</li><li>the form saves a draft and logs it.</li></ul> | PASS |
| 5. Discovery quality | `test_discovery_quality.py` (4): <ul><li>evidence ageing at 30, 90 and 150 days, with the age in the reason;</li><li>US evidence stored and backfilled;</li><li>the intake job checks 5 websites;</li><li>pasted web addresses are removed from names, and backfilled with a note.</li></ul> | PASS |
| 6. End to end | `test_e2e_sales_autonomy.py` (1), section 1 above | PASS |

## 3. Full suite, lint and secret scan

| Check | Result |
|---|---|
| Full test suite on the final code | **529 passed, 0 failed** (exit 0). The text-only change to two automation descriptions came after this run; the automation, rule, Manager Today and end-to-end tests (40) were re-run after it and passed |
| `ruff check aime tests` | All checks passed |
| Secret scan (pre-commit hook, every commit) | clean |
| GitHub Actions CI on the pull request head | Recorded in the pull request |

**A defect was found while testing.** The first full-suite run failed one existing test, `test_hardening.py::test_contractor_signal_and_categories`. The new evidence-age calculation read a clock field that can be empty. It was fixed in c109684 to use the clock that scoring already uses. That test, and the rest of the suite, pass on the final code.

## 4. Checks on a copy of production

The preview ran this branch's code against a fresh read-only copy of the production database (28 Oct). The copy and the screenshots are kept on the owner's PC, outside the repository, because they contain record-level data.

| Check | Result |
|---|---|
| The copy opens; the two new columns are added automatically; integrity check | PASS: `ok`, 280 companies, 279 leads, 0 tokens |
| **Manager Today** with real-shaped data | PASS. It shows: <ul><li>8 leads qualified today, each "no usable email yet: research continues";</li><li>0 exceptions;</li><li>"5 approved emails held: OUTBOUND_EMAIL_ENABLED=false";</li><li>6 LinkedIn posts waiting;</li><li>health: Hybrid, running, 0 authorizations, every switch off;</li><li>2 open warnings.</li></ul> Screenshots taken at 1440 and 1920 px wide |
| **Automation Control:** the "New rule-based authorization" form | PASS: rendered, with grade, address, website, limit and day fields. Screenshot at 1440 px |
| **CRM hygiene,** database only, on the copy | PASS: <ul><li>US evidence filled for 280 of 280 companies;</li><li>4 company names had a pasted web address removed;</li><li>2 of those 4 are hiring-post text and need a person (see [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md)).</li></ul> |

**Observation, not a defect of this branch:** on a copy, the AI-employee badge says "Off duty: no scheduled run recorded for this database". The heartbeat belongs to the production database, not the copy, so this is expected. In production the badge reads from the live heartbeat.

## 5. What was not changed while testing

The production database was opened read-only, and only to make the copy. The following were not touched:
- production `.env`;
- scheduled tasks;
- the mailbox;
- SMTP;
- LinkedIn;
- Calendly.

The approved drafts #38, #46 and #51–53 are unchanged and unsent.
