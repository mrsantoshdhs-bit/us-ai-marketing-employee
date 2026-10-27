# Production checklist (27 Oct 2026)

Three parts, in order:

- **A. Deploy the tested code.** Live sending stays off.
- **B. Before any live sales operation.** Each step needs your explicit decision.
- **C. Calendly.**

Nothing in A sends email. Live sending (B8) happens only on your explicit authorization.

## Where things stand

| Item | State |
|---|---|
| Production code | `a8f1e15`, deployed 27 Oct |
| `main` | `eb04dbe`: #39 (interest and meeting requests in everyday wording) merged, not deployed |
| Open PRs, CI passed | #36 (doctor consent check), #37 (console crash in `acceptance-report`), #38 (alert after an earlier dismissal), #40 (opt-outs in everyday wording; opt-outs from another address in the thread) |
| All of them merged together, tested locally | 445 tests passed; no conflicts |
| Production switches | `OUTBOUND_EMAIL_ENABLED=false`, `PUBLISHING_ENABLED=false`, `REQUIRE_HUMAN_APPROVAL=true`, IMAP off |
| Approved, unsent production drafts | #38 to hello@meetyogi.com, #46 to hello@hedgespecialty.com (see B1) |
| Acceptance (Gmail test mailbox) | Real send, real reply in the thread, classification, opportunity and alert all pass. Calendly, a real opt-out reply and the 4-day follow-up step are not yet tested live |

## A. Deploy the tested code (needs your approval)

1. Merge #36, #37, #38 and #40, in that order, each with CI passed. Then merge this checklist.
2. Take a verified backup and drill it:
   - `python -m aime backup --label pre-deploy-<date>`
   - `python -m aime restore-drill --source data\backups\<that file>`
3. `python -m aime maintenance on --hours 2 --reason "Deploy"`
4. In the production folder, run `git pull --ff-only`. Only `main` moves; your untracked files and the stash stay.
5. Tests, against a scratch data folder, never `data\`:
   `AIME_DATA_DIR=<scratch> AIME_DATA_ROLE=test python scripts/pytest_with_timeout.py 1500`
   Expect every test to pass (445 when this was written; about 3 minutes).
   - **Under heavy load the PC can fail the two browser-render tests** in `tests/test_design.py`
     (`test_browser_renders_a_checked_portrait_image`, `test_blank_patch_triggers_one_retake`). They render post
     images in a browser with time limits.
   - This happened on 27 Oct: the run took over 10 minutes instead of about 3, and both passed alone and in a full
     re-run.
   - If those two fail, run `tests/test_design.py` alone, then the full suite again. Treat any other failure, or a
     repeat, as a real failure and stop.
6. `python -m aime doctor`: expect 0 errors. The warnings today are the OneDrive sign-in and, until the next daily backup, "no copy written yet".
7. `python -m aime maintenance off`. This comes before the dashboard restart: the dashboard task deliberately does
   not start the dashboard while a maintenance window is on.
8. Restart the dashboard:
   - `schtasks /End /TN AIMarketingEmployee-Dashboard`, then `schtasks /Run /TN AIMarketingEmployee-Dashboard`.
   - Check that http://127.0.0.1:8765/login answers.
   - Confirm the next tick: `data\heartbeat.json` shows state idle, and every job succeeded.
9. Rollback: see `docs/DEPLOYMENT.md`. Code can roll back with the data kept (schema changes are additive), or a backup can be restored.

## B. Before any live sales operation (each step needs your decision)

1. **Production drafts #38 and #46.** They are approved and go out at the next follow-up run once sending is on. Reject them on the Follow-ups page, or keep them deliberately.
2. **Sending mailbox.** For outreach, use the business mailbox `santosh@digitalhubsolution.com`:
   - SPF, DKIM (`default` selector) and DMARC already pass.
   - Its mail server's certificate is valid.
   - Type its password after `SMTP_PASSWORD=` in production `.env`, then run `python -m aime email-check --connect`.

   The personal Gmail used for testing has no SPF or DKIM for your domain, and should stay a test mailbox.
3. **Reading replies in production.** Set `IMAP_HOST` (business mailbox: `mail.digitalhubsolution.com`). Keep `INBOX_SCOPE=related`, so only sales mail is stored.
4. **Calendly** (part C).
5. **Off-machine backup.**
   - Sign in to OneDrive and check that `AIME-Backups` shows as synced. `doctor` reports it.
   - Copy `BACKUP_PASSPHRASE` from `.env` into your password manager.
6. **Local AI model.** Enable Ollama in Task Manager → Startup apps (it is disabled), or set `OLLAMA_AUTOSTART=true`.
7. **Remaining live acceptance**, in the acceptance environment:
   - test 10: a follow-up step 4 days after a sent email;
   - test 13: a real "Remove my email from your list." reply in a thread;
   - tests 14 and 19: a Calendly booking, reschedule and cancellation.
8. **Go live, only on your explicit authorization.**
   - In production `.env`, set `OUTBOUND_EMAIL_ENABLED=true` and leave `OUTBOUND_TEST_RECIPIENTS` empty.
   - Start with a low daily limit (`config/business.toml` `[outreach]`).
   - Approve emails one by one, and watch the first replies on the Replies page.
9. **Stop at any time:** `OUTBOUND_EMAIL_ENABLED=false`.

## C. Calendly: what is needed

The app does not use the Calendly API. It reads Calendly's notification emails from the monitored mailbox and puts your booking link in approved replies.

1. A Calendly account (the free plan works) with one event type, for example a 20-minute intro.
2. That event's link (`https://calendly.com/<you>/<event>`) in `CALENDLY_URL`, in production `.env` and in `acceptance.env`.
3. Calendly's host notifications (new event, cancelled, rescheduled) delivered to the mailbox the app reads:
   - the Calendly account's email is the sales mailbox, or the notifications are forwarded to it;
   - for the Gmail test, they must arrive in the Gmail test mailbox that the acceptance settings read;
   - with `INBOX_SCOPE=related`, emails from calendly.com are always kept.
4. Keep the standard notification wording. The app reads the invitee's name and email, and the event date, time and time zone, from it.
5. Matching a booking to a lead:
   - the invitee books with the email address the outreach went to; in test mode, that is the test address;
   - otherwise the booking shows on Meetings as "not matched", with **Link to lead**.
6. **Test**, in the acceptance environment:
   1. From the approved test recipient's mailbox, book through the link in an approved test reply.
   2. Run the inbox job: the meeting is recorded, the lead moves to *meeting* and the opportunity to 30%.
   3. Reschedule in Calendly: the meeting moves, and there is still only one.
   4. Cancel: the meeting is cancelled and the lead reopened.

The Calendly connector in the Claude app is separate, and is not needed for any of this.

## Known limits (not blocking)

- **Replies:** classification is rule-based, with the local model as a second opinion for unclear replies. New phrasings can still land in the review queue, where you see them.
- **Today page:** the "Interested reply" alert is listed on the System page and sent as a desktop toast. Today shows it through its counters (Interested, Open opportunities, Emails awaiting approval), not in its alert box, which lists warnings and errors.
- **Threads in Gmail:** follow-up steps carry the thread headers, but their subjects differ, so Gmail may show them as separate conversations.
- **Running times:** scheduled jobs and the dashboard task run while you are logged in. Sleep pauses them, and they catch up on wake.
