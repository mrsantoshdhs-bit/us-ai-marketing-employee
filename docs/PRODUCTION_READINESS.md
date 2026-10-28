# Production readiness, 28 October 2026

**Status:** production runs `main` at 2288f38, unchanged by this work. The switches are as follows:

| Switch | Value |
|---|---|
| `OUTBOUND_EMAIL_ENABLED` | false |
| `PUBLISHING_ENABLED` | false |
| `REQUIRE_HUMAN_APPROVAL` | true |
| Mailbox reading | not configured |
| Mode | Hybrid |
| Campaign authorizations | none |

Five emails are approved and unsent: #38 and #46 (27 Oct), and #51–53 (28 Oct). Nothing has been sent, published or booked.

**Branch:** `sales/28-autonomy-audit` is built and tested. It is not merged and not deployed. What it contains is in [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md). The test evidence is in [END_TO_END_TEST_REPORT.md](END_TO_END_TEST_REPORT.md).

## Readiness by step

| Step | Ready? | Needs the owner? |
|---|---|---|
| Discovery, research, qualification, CRM, drafting | **Running in production.** Better with this branch: ageing evidence, stored US evidence, faster website checks | Merge and deploy (**A5**) |
| Manager Today | Built and tested; checked on a copy of production | **A5** |
| Unattended operation (signed out, PC asleep) | Code ready; the live tasks are unchanged | **A1** |
| Reading replies | Built and tested with simulated mail; not configured | **A2** |
| Sending outreach and follow-ups | Built and tested with simulated mail; switched off | **A3** |
| Routine outreach without a person | Rule-based authorizations built and tested; none exist | **A3** |
| Meeting booking | Booking link only; no calendar or Calendly API | **A4** |
| Reachability | **Not ready.** 5 of 42 qualified leads have a usable email (all role mailboxes) | A sourcing decision (see the last section) |

## Authorizations needed

Each one is separate. None follows from another, and none has been given.

### A5: merge and deploy this branch

Uses the proven deploy flow from [DEPLOYMENT.md](DEPLOYMENT.md):
1. CI on the exact head.
2. Full suite on the merge commit.
3. Maintenance on.
4. Verified backup and restore drill.
5. `deploy_check` on the restored copy.
6. Stop the dashboard.
7. Fast-forward merge.
8. `doctor` (applies the two new nullable columns).
9. Maintenance off.
10. Start the Dashboard task.
11. Live checks, then confirm the next tick.

**What changes in production after the deploy:**
- The **Manager Today** page appears.
- **Pause** also holds LinkedIn posts. Today nothing publishes anyway, because publishing is off.
- **Approvals of emails and posts** go to the audit log.
- **Website checks:** each intake run checks up to 5 company websites, the same read-only homepage requests as the daily check. To keep the old pace, set `website_checks_per_inbound_run = 0` in `config/business.toml` before deploying.
- **CRM hygiene,** on its next daily run:
  - fills the US-location evidence for every company (280 on the copy);
  - removes a pasted web address from 4 company names, recording the old name in a validation note.
- **Hiring evidence older than 60 days scores less,** so some lead scores go down when they are next scored. This is intended: old evidence is weaker.

**Rollback:**
1. Fast-forward `main` back to 2288f38 and restart the Dashboard task.
2. The two new columns are nullable and ignored by the old code, so no database restore is needed.
3. The verified pre-deploy backup is the fallback.

**Not changed by A5:** the switches, credentials and scheduled tasks. No authorization is created.

### A1: run while signed out, and wake the PC

Today all three tasks run only while the owner is logged on, and never wake the PC (gaps of up to 147 minutes were seen).

**What the owner does, in an elevated terminal:**
```
python -m aime scheduler install --run-when-logged-off --wake
python -m aime scheduler verify
```

The Windows password is typed at the `schtasks` prompt by the owner. The app never sees or stores it.

**Trade-offs:**
- **S4U** ("do not store password") cannot be used. It would break the current-user protection (DPAPI) of the LinkedIn token and dashboard sessions.
- **Waking the PC** also needs Windows power settings that allow wake timers. Changing those is the owner's decision.

### A2: read the sales mailbox

**What the owner does:**
1. Creates a Gmail app password.
2. Types it into `.env` as `IMAP_PASSWORD` (with `IMAP_HOST` and `IMAP_USERNAME`).
3. Runs `python -m aime email-check --connect` (signs in read-only; sends nothing). `doctor` looks at the mailbox only when sending is on, so it is not the check for A2.

**What reading means:**
- Reading is read-only: the mailbox is opened with `readonly=True`.
- Replies are classified and suppressions applied. Interested replies open opportunities and get a drafted answer, which waits for a person.
- Calendly notices become meetings.

**Needs the local model:** the AI second opinion on replies is built, but it only runs while the local model (Ollama) runs; otherwise rule-based classification alone decides.

### A3: send email

This takes several steps. Each one is the owner's.

1. **SMTP password.** Type the Gmail app password into `.env` as `SMTP_PASSWORD`.
2. **Sending switch.** Set `OUTBOUND_EMAIL_ENABLED=true`. With `REQUIRE_HUMAN_APPROVAL=true`, every email still needs a person.
3. **"Confirm again" for the five old approvals.** #38, #46 and #51–53 were approved while sending was off, so each needs a fresh confirmation before it can go. Nothing old goes out by itself.
4. **Optional: routine outreach without a person.**
   - Set `REQUIRE_HUMAN_APPROVAL=false` and keep Hybrid mode.
   - Then an administrator creates **and activates** a rule-based authorization, for example: "grade B and better, published addresses, verified website, 5 a day, 20 in total, 7 days".
   - Pricing, legal, negotiation and unusual replies always go to a person. A rule never covers them.
5. **Stop switches.** Pause, Stop and Emergency stop on Automation Control hold sending at once. Emergency stop also stops every authorization.

### A4: meeting booking

**What exists:** drafted answers to interested replies carry the owner's Calendly link (first-touch outreach carries no links: the quality checks reject them), and booking notices are read from the mailbox (with A2).

**What is missing:** no API acts on a calendar. Building one needs the owner to choose the calendar and give an API token. Every booking would stay consent-based: the prospect picks the time.

## Not ready, and not fixable by automation alone

- **Reachability.** Free public sources rarely publish a decision-maker's own address. Paid enrichment is excluded by the owner's decision. The options are:
  - keep using company mailboxes, under a rule that allows them;
  - a source the owner approves, after checking its terms;
  - a different first-touch channel.

  This is a business decision, not code.
- **Grade A.** No lead reaches grade A with today's evidence.
- **Off-site backups.** OneDrive is not signed in, so backup copies stay on this PC. The owner should sign in.
- **Data clean-up.** Two company names are hiring-post text. A person should correct them on the lead pages.
