# A2: Mailbox reading readiness

**Date:** 28 October 2026 · **Production commit:** 9ed84b0 · **Method:** code reading only. No app, test, job or
mail connection was run for this report.

## Verdict

**Ready to enable after the owner's steps.** The read-only mailbox reader, reply classification, opt-out and bounce
handling, drafted answers and Calendly-notice parsing are all built. In production it is **not enabled**:
`IMAP_HOST`, `IMAP_USERNAME` and `IMAP_PASSWORD` are empty, so the `inbox` job runs every 15 minutes and succeeds
without reading anything. It has **never been tested against a live mailbox**; it was tested only with fake
in-process IMAP servers (listed at the end). Reading a mailbox sends nothing: every drafted answer waits for a
person, and sending is a separate switch (A3). The small defects listed below do not block enabling.

## What exists

| Capability | Status | Where in the code | Notes |
|---|---|---|---|
| Secure connection | Exists | `aime/inbox.py` `Mailbox.fetch_new`; `aime/integrations/email_smtp.py` `tls_context`, `MAIL_TIMEOUT` | IMAP over SSL (port 993 by default). Certificate and host name are verified. 60-second timeout |
| Read-only access | Exists | `Mailbox.fetch_new`: `select(..., readonly=True)` (EXAMINE) and `FETCH (BODY.PEEK[])` | Nothing is marked read, moved or deleted. The app password itself is not read-only (see Risks) |
| Incremental fetch | Exists | `Mailbox.fetch_new`, `Mailbox._uidvalidity`; position saved as `inbox.state` in `aime/agents/inbox_agent.py` `InboxAgent.execute` | Tracked by UID and UIDVALIDITY. The first run looks back 14 days. At most 50 messages per run. A UIDVALIDITY change starts again |
| No duplicates | Exists | `InboxAgent.execute` (Message-ID check); `InboundEmail.message_id` unique (`aime/models.py`) | A message with no Message-ID could be stored twice after a crash (edge case) |
| Shared-mailbox filter | Exists | `inbox_agent.py` `sales_related`; `INBOX_SCOPE` in `aime/config.py` | Default `related` keeps replies in our threads, bounces, Calendly notices, and mail from known contacts or lead domains. Mail from our own addresses is skipped |
| Parsing, quote removal | Exists | `inbox.py` `parse_message`, `new_text`, `own_text` | Plain text first. Our own footer is never read as their words |
| Reply classification | Exists | `aime/reply_intel.py` `classify`, `_classify`, `PATTERNS`, `COMPLAINT`, `OOO` | Categories: bounce, calendly, unsubscribe, out_of_office, interested, not_interested, objection, referral, question, other. Confidence is high, medium or low, with reasons. English rules only |
| Intent | Exists | `reply_intel.py` `intent_of` | meeting_request, pricing_request or more_info. Pricing wins |
| Review queue | Exists | `reply_intel.py` `act`; Replies page (`aime/web/app.py` `inbox_page`, `inbox_action`) | Medium or low confidence, complaints, pricing questions and unmatched mail go to `needs_review`. A person can reclassify |
| AI second opinion | Exists, optional | `reply_intel.py` `ai_second_opinion`, `process_pending`; `aime/llm/factory.py` `build_llm` | Only when the local Ollama model answers, at most 5 per run. A suggestion only, never an action. Without the model the rules decide |
| Thread matching | Exists | `reply_intel.py` `match`; Message-ID stored before sending in `aime/agents/sales_workflow.py` `_send_locked` | In-Reply-To and References are matched to sent follow-ups, then the sender's address |
| Conversation history | Partial | `act` → `log_activity`; lead page (`app.py` `lead_detail`); Replies page | Each reply is logged on the lead with its text. There is no single threaded view of both directions |
| Every human reply stops the sequence | Exists | `act` → `aime/outreach.py` `stop_sequence` | Cancels that lead's pending, waiting and approved emails |
| Opt-out handling | Exists, one gap | `PATTERNS["unsubscribe"]`, `COMPLAINT`, `act`; `aime/crm_ops.py` `suppress` | Suppresses the sender and the address we wrote to, and cancels queued email. Complaints and data-removal requests are suppressed and sent to review. **Gap:** an email whose subject is "unsubscribe" but whose body is empty is classified `other` and waits for review. The sequence still stops, but the address is not suppressed until a person reclassifies it. This is what our own `List-Unsubscribe` mailto link can produce |
| Bounce handling | Exists | `inbox.py` `_dsn_info`, `BOUNCE_SENDERS`, `BOUNCE_SUBJECT`; `act` | Only permanent failures suppress and mark the contact bounced. Delay notices go to review |
| Out-of-office | Exists | `OOO`; `auto_submitted` in `parse_message` | The sequence continues |
| Drafted answers | Exists | `reply_intel.py` `suggest`; `act` creates a `FollowUp` with status `awaiting_approval` | An interested reply gets a booking-link answer. Questions and pricing get a placeholder that a person must replace (`QUESTION_PLACEHOLDER`) |
| Calendly notices | Exists | `aime/meetings.py` `handle_calendly_email` | Bookings, reschedules and cancellations become meeting records (see A4) |
| Notifications | Partial | `Alert` rows; `aime/notify.py` `notify_event` | Shown on the dashboard, plus a Windows desktop toast that only someone signed in at the PC sees. No email or phone alert |
| Errors and alerts | Partial | `aime/agents/base.py` `BaseAgent.run`; `aime/monitoring.py` `check_health` | A failed run is recorded. An error alert follows 2 failures in a row; a warning follows 45 minutes without a success. See defect 1 |
| Setup check | Exists | `aime/email_check.py` `check_settings`, `check_imap`; `python -m aime email-check --connect` | Signs in and opens the folder read-only, with Gmail hints |
| Gmail OAuth sign-in | Not built | — | App password only |
| More than one folder | Not built | `IMAP_FOLDER` (default `INBOX`) | Spam and other labels are not read |

**Defects and doc gaps found**
1. `InboxAgent.retry_policy` says "a dropped IMAP connection is retried once". However, `RetryPolicy.retry_on`
   (`aime/agents/base.py`) does not include `OSError` or `imaplib.IMAP4.error`, so an IMAP failure is not retried
   within the run. The next run, 15 minutes later, tries again. Low impact.
2. `Mailbox.fetch_new` moves `last_uid` past a message even when fetching it fails, so that message is never read.
   Low impact. No test covers it.
3. In production, Manager Today and the Command Center show "Mailbox reading: on" when only `IMAP_HOST` is set
   (`app.py` `manager_today` and `ai_employee`: `bool(st.imap_host)`). Reading actually needs the host, a user and a
   password (`Settings.imap_configured`); the Integrations page uses that correct check. The Manager Today status
   panel is being reworked on the current branch, so re-check this after the merge.
4. `docs/PRODUCTION_READINESS.md` (A2) says to verify with `doctor`. However, `aime/doctor.py` checks the mailbox only
   when sending and B2B outreach are both on. Use `email-check --connect` instead. The same document calls the AI
   second opinion "not built"; it is built and only needs the local model to be running.

## What the owner must do to enable it

1. **Recommended first:** the isolated acceptance run in `docs/ACCEPTANCE.md` (a separate data folder and settings
   file). Use a test address that is a *different* mailbox from the sales mailbox. Mail from the sales mailbox's
   own address is skipped (`InboxAgent.execute`, the `own` set), so a reply sent from it would never be read.
2. In the sales mailbox's Google account: turn on 2-Step Verification and create an app password. In Gmail
   settings → *Forwarding and POP/IMAP*, make sure IMAP is enabled (if the setting is shown).
3. In `.env`, the owner types the values:
   ```
   IMAP_HOST=imap.gmail.com
   IMAP_PASSWORD=            # the owner types the app password here
   ```
   `IMAP_USERNAME` can stay empty: it falls back to `SMTP_USERNAME`, which is already set (`Settings.imap_credentials`).
   The defaults `IMAP_PORT=993`, `IMAP_FOLDER=INBOX` and `INBOX_SCOPE=related` fit a mailbox that is also used for
   other work. Putting the password in `IMAP_PASSWORD`, rather than `SMTP_PASSWORD`, keeps A2 separate from A3:
   SMTP sign-in stays impossible.
4. Run `python -m aime email-check --connect`. Expect `IMAP read-only: pass ... nothing was changed`. The SMTP
   sign-in line shows `skip` while `SMTP_PASSWORD` is empty. Nothing is sent.
5. Restart the Dashboard task: it reads `.env` only when it starts (`aime/context.py` `get_context`). Scheduled jobs
   pick up the change at the next tick.
6. Within 15 minutes the `inbox` job reads the mailbox. The Agent runs page shows `fetched`, `stored` and
   `skipped_unrelated`; the Replies page shows the stored mail.
7. Make sure Calendly's host notifications reach this mailbox. A notice forwarded *from the sales mailbox's own
   address* is skipped as our own mail.

## What happens once enabled

- Every 15 minutes, new mail is read without changing the mailbox. The first run covers the last 14 days of
  `INBOX`, 50 messages per run, so a busy mailbox takes several runs to catch up. Mail that is not sales-related is
  downloaded, checked and discarded, never stored.
- Each stored email is classified and acted on (`act`):
  - unsubscribes and permanent bounces are suppressed;
  - any human reply stops that lead's sequence and cancels its queued emails, including approved, unsent ones;
  - interested replies open an opportunity, raise an alert and get a drafted booking-link answer;
  - not-interested replies move the lead to nurture.
- Calendly notices become meetings, and double bookings raise a warning (A4).
- The "no response" marking (`aime/pipeline.py` `pipeline_checks`) becomes meaningful, because replies are now seen.
- **Nothing is sent.** Sending is off (A3), and every drafted answer waits on Follow-ups for a person
  (`REQUIRE_HUMAN_APPROVAL=true`).

## What still needs a person

- Approving, editing or rejecting every drafted answer, and writing the answer to questions and pricing (the
  placeholder blocks approval, `followup_action`).
- Reviewing `needs_review` items: unsure classifications, complaints, unmatched senders, delay notices, and an
  "unsubscribe" with an empty body. A person reclassifies them on Replies.
- Carrying out data-deletion requests: the address is suppressed automatically, but deletion is manual.
- Linking an unmatched Calendly booking to its lead on Meetings.

## Risks and limits

- **Privacy:** with a personal Gmail used for other things, 14 days of mail are downloaded and filtered in memory.
  Only sales-related mail is stored in the local database (body up to 20,000 characters).
- **Credential scope:** an app password signs in to the whole mailbox. The code only reads, but the credential
  is not read-only. It sits in plain text in `.env` on this PC; `.env` is not encrypted the way the LinkedIn token
  is (`aime/secure_store.py`).
- **Rules can misread.** Example: a newsletter from a lead's own domain that contains "unsubscribe" may be read as
  an opt-out of that address. Reading the code suggests this fails safe (we stop writing to that address) but can
  wrongly stop a lead. Review the Replies page closely in the first week.
- **No sender authentication:** the app does not check SPF, DKIM or DMARC results, so a forged "calendly.com"
  notice could create a meeting record. It relies on Gmail's own filtering.
- Mail filtered to Spam is not read. Mail skipped as unrelated is never revisited, even if its sender later becomes
  a contact.
- If Google stops accepting the app password, sign-in fails every run, and the monitor raises an error alert.

## How to switch it off

- Clear `IMAP_HOST` in `.env`. The next tick stops reading. **Note:** clearing only `IMAP_PASSWORD` is not enough once
  `SMTP_PASSWORD` is set (A3), because the IMAP credentials fall back to the SMTP ones.
- To stop at once: Automation Control → **Stop** (the inbox job then waits; **Pause** does not stop reading). Or revoke
  the app password in the Google account.
- Emails already stored, and the suppressions made from them, stay in the database.

## What was and was not tested

- **Tested (fake IMAP servers, in-process):** the `FakeIMAP` class in `tests/test_inbox.py` (23 tests; asserts
  `readonly=True`). It is reused by `tests/test_calendly_booking.py`, `tests/test_meetings_pipeline.py`,
  `tests/test_reply_optout.py`, `tests/test_automation.py`, `tests/test_e2e_sales_autonomy.py` and
  `tests/test_end_to_end_sales.py`. `tests/test_email_check.py` has its own fake IMAP server for the sign-in check.
- **Not tested:** a live Gmail mailbox, an app-password sign-in, real Gmail bounces, real Calendly notices, Gmail
  threading of our Message-IDs, a large or shared mailbox, and the empty-body "unsubscribe" case. Not run for this
  report.
