# Implementation summary, 28 October 2026

**Branch:** `sales/28-autonomy-audit`, based on `main` at 2288f38 (production).

**Scope:** the six safe items from [SALES_AUTOMATION_GAP_ANALYSIS.md](SALES_AUTOMATION_GAP_ANALYSIS.md). Nothing here changes production:
- no switch, credential or scheduled task was changed;
- nothing was sent, read from a mailbox, published or booked.

Everything new that could let work out without a person is **off by default**. It also stays behind every gate that already existed.

This document has three parts: what existed, what changed, and what is still missing. What needs the owner's authorization is in [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md). The test evidence is in [END_TO_END_TEST_REPORT.md](END_TO_END_TEST_REPORT.md).

## Commits

| Commit | Item |
|---|---|
| 366d712 | Read-only audit: [CURRENT_SYSTEM_AUDIT.md](CURRENT_SYSTEM_AUDIT.md), [LEAD_DATA_AUDIT.md](LEAD_DATA_AUDIT.md), [SALES_AUTOMATION_GAP_ANALYSIS.md](SALES_AUTOMATION_GAP_ANALYSIS.md) |
| 27099a0 | 1. Defect fixes from the audit |
| 74aefaf | 2. Manager Today |
| 5674caf | 3. Scheduler readiness, code only |
| 1cdf363 | 4. Rule-based campaign authorizations, off by default |
| 4709833, c109684 | 5. Discovery quality, plus one defect found while testing |
| this commit | 6. End-to-end test and these reports |

## 1. Defect fixes

| What existed | What changed |
|---|---|
| **Paused** automation held email only; LinkedIn posts that were already approved could still publish | Paused also holds LinkedIn publishing, with the reason "Automation is paused" |
| `NETWORK_ENABLED` was documented in `.env.example` but was never read from the environment | The setting is read |
| A person's approval, rejection, "mark sent" or re-queue of an email, and approval or rejection of a post, were logged as CRM activities only | They also go to the audit log, with the recipient and subject |
| Two code comments described `REQUIRE_HUMAN_APPROVAL` and the inbox job wrongly | The comments are corrected |

## 2. Manager Today (`/manager`, second item in the sidebar)

**What existed:** the Dashboard, Command Center and Approval Queue covered most items, but across several pages. The Approval Queue lists every email, as if a person approves each one.

**What changed:** one page for the Sales Manager. It shows:
- **What happened today** (from local midnight): leads qualified today, hot leads and open opportunities, interested replies (7 days), meeting requests not yet booked, and confirmed meetings ahead.
- **Needs you:** only exceptions, each with the reason a person is needed:
  - pricing questions and unclear replies;
  - emails that wait for a person, with the reason: `REQUIRE_HUMAN_APPROVAL`, Approval mode, or no active authorization or rule covers it;
  - sends and posts whose outcome is uncertain.

  Emails that an active authorization covers are routine work, so they are not listed. Approved emails held by a switch are counted, with the switch named.
- **Failures and alerts:** failed and blocked jobs, and open warnings.
- **Automation health:** mode, state, the sending, reading and publishing switches, what stops an email going out without a person, and the number of active authorizations.

Every number comes from the records. Each empty state says why it is empty, for example "Mailbox reading is off, so replies are not read".

## 3. Scheduler readiness (code only)

**What existed:**
- `scheduler install --run-when-logged-off` could already register the Tick task with a stored-password logon (the owner types the Windows password at the prompt).
- No task could wake the PC.
- `scheduler verify` did not say whether jobs run while the owner is signed out.

**What changed:**
- `scheduler install --wake` sets the task's "wake the computer to run" option.
- `scheduler verify` adds a read-only section: "While you are signed out or the PC sleeps", one line per task. It reads the installed task definitions with `schtasks /Query /XML`.

**Not done:** no task was installed or changed. A read-only check of the live Tick task found:
- it runs only while the owner is logged on;
- it does not wake the PC;
- it has no start-up trigger.

Changing that is authorization **A1** in [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md).

## 4. Rule-based campaign authorizations (off by default)

**What existed:** in Hybrid or Automatic mode, an email could go out without a person only if an active authorization named its **exact** recipient address.

**What changed:**
- **Creating a rule:** an administrator can create an authorization that names a **rule** instead of addresses. The form is "New rule-based authorization" on Automation Control. A rule names:
  - the lowest grade (A, or B and better);
  - which addresses it covers: a decision-maker's own published address, or also the company's published mailbox;
  - whether a verified company website is required;
  - limits per day and in total, and how many days it lasts.
- **What a rule covers:** outreach emails only. The lead must be qualified. The company must be real: not a sample, and not an uncleared competitor. The address must be the lead's researched contact, published by the company (website or SEC filing).
- **What a rule never covers:** replies to people who wrote to us, prices or commitments, test addresses, held quality checks, and opted-out or suppressed addresses.
- **New rules start as drafts.** A draft lets nothing out. An administrator activates it, and the activation goes to the audit log.
- **Every existing send-time gate still applies:** the sending switch, `REQUIRE_HUMAN_APPROVAL`, Approval mode, paused or stopped automation, business hours, limits, suppression and "Confirm again".
- **The rule is checked twice:** when the email is approved under it, and again just before sending. If the lead or contact has changed since, the email goes back to a person.

**Production:** there are no rule-based authorizations in production. One can be created only with authorization **A3**.

## 5. Discovery quality

| What existed | What changed |
|---|---|
| A hiring post scored +6 intent however old it was | Full weight for 60 days, half (+3) until 120 days, then nothing. The score reason says how many days old the evidence is |
| The US-location evidence was used when a company was found, but not stored | Each company stores **why it counts as a US company**, for example "US state CA (SEC filing business address)". It is shown on the lead page. CRM hygiene fills it for older companies: all 280 on a copy of production |
| Websites were checked about 20 a day, in the daily run; 113 were waiting | Each 30-minute intake run also checks up to 5 websites (`research.website_checks_per_inbound_run`; 0 turns it off). At most 240 a day, so the 113 waiting take about 12 hours of the PC running. These are the same read-only homepage requests as before |
| Some company names contained a web address pasted from a hiring post | New names have the address removed. CRM hygiene cleans existing names and records the old name in a validation note: 4 names on the copy of production |
| *(defect found by the full suite)* The new age calculation used the scoring clock field, which can be empty | Fixed in c109684 to use the clock that scoring already uses. An existing test caught it |

## 6. Tests and reports

New tests:
- `tests/test_audit_fixes.py`
- `tests/test_manager_today.py`
- `tests/test_scheduler_unattended.py`
- `tests/test_rule_authorizations.py`
- `tests/test_discovery_quality.py`
- `tests/test_e2e_sales_autonomy.py`

The end-to-end test runs the whole chain with a mock contact at a reserved `.example` domain and fake SMTP and IMAP servers, so nothing leaves the test. Results are in [END_TO_END_TEST_REPORT.md](END_TO_END_TEST_REPORT.md).

## Database changes

Two new **nullable** columns, added automatically at start-up as before:
- `companies.location_evidence`
- `campaign_authorizations.rule`

No existing column or row is changed by the migration. On the copy of production, CRM hygiene later fills `location_evidence` and cleans the 4 names.

## Still missing

These are not built, and not all can be built without the owner:

1. **Unattended operation.** The tasks still run only while the owner is logged on (**A1**).
2. **Sending and mailbox reading.** Both are off, and production has no app password (**A2**, **A3**).
3. **Reachability.**
   - 5 of 42 qualified leads have a usable email, all of them role mailboxes.
   - 0 decision-makers have a published personal address.
   - Free public sources rarely publish one. Paid enrichment is excluded by the owner's decision.
   - More automation does not fix this.
4. **Meeting booking.** There is no calendar or Calendly API (**A4**). The booking link in emails is still the only path.
5. **Grade A.** No lead reaches grade A with today's evidence, so rules can only usefully cover "B and better".
6. **AI second opinion on replies.** The local model (Ollama) is not running, so rule-based classification alone decides.
7. **Two company names are hiring-post text** (labelled fields such as "Role: ..."), not company names. The cleanup removed only the web address. A person should correct them.
