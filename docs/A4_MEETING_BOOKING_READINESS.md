# A4: Meeting booking readiness

**Date:** 28 October 2026 · **Production commit:** 9ed84b0 · **Method:** code reading only. No app, test, job,
mailbox or Calendly connection was run for this report.

## Verdict

**Not built.** There is no calendar or Calendly API. Nothing looks up availability, and nothing creates, moves or
cancels an event. What *is* built is link-based booking, and it is ready once A2 (mailbox reading) is on:

- the prospect picks a time through the owner's public Calendly link;
- the app reads Calendly's notification emails and records the meeting, including reschedules and cancellations;
- it moves the pipeline and tells the Sales Manager.

`CALENDLY_URL` is set in production to the owner's public 30-minute event. No Calendly or calendar token is
configured. Today no booking can be recorded automatically, because the mailbox is not read.

## What exists

| Capability | Status | Where in the code | Notes |
|---|---|---|---|
| Booking link in emails | Partial | `aime/reply_intel.py` `suggest`; `aime/meetings.py` `booking_minutes` | Only in drafted answers to interested or pricing replies, which exist only with A2 and wait for approval. First-touch outreach has no link (`aime/outreach.py` `draft_outreach` asks for a call; the quality checks reject links). The length (30) is read from the link |
| Meeting-request detection | Exists | `reply_intel.py` `MEETING`, `NEGATED`, `intent_of`, `_classify` | Everyday wording and typos. A negated request ("don't schedule a call") goes to review |
| Calendly notice parsing | Exists, needs A2 | `meetings.py` `parse_calendly`, `_when`, `_date`, `_zone`, `handle_calendly_email`; `reply_intel.py` `CALENDLY`, `CALENDLY_NOTICE` | Subjects "New Event:", "Canceled:", "Rescheduled:" and "Updated:", also when forwarded. Times written as a range or as a start time only; dates month first or day first. Unreadable notices go to review |
| Matching to a lead | Exists | `meetings.py` `match_lead`; `link_meeting`; Meetings page (`aime/web/app.py` `link_meeting_to_lead`) | Matched by the invitee's email, then the company domain, then the address we emailed, then earlier replies. Otherwise an "unmatched" alert, and a person links it |
| Meeting record and pipeline | Exists | `record_meeting`, `_advance_opportunity`, `close_open_emails` | Lead → `meeting`. Opportunity to at least 30% with a next step. The lead's sequence emails and unsent booking-link emails are cancelled |
| Reschedule | Exists | `record_meeting` (kind `rescheduled`), `_the_meeting` | Moves the recorded meeting. Calendly's cancellation of the former time changes nothing |
| Cancellation | Exists | `record_meeting`, `reopen_after_cancel` | Lead back to `interested` with a "follow up to rebook" step, unless another meeting is booked |
| Duplicate booking prevention | Exists | `record_meeting` (same invitee and start); `check_double_bookings`; overlap alert in `record_meeting` | The same notice twice is recorded once. Two upcoming meetings for one lead raise a warning; overlapping meetings with different people raise an alert. Calendly itself prevents double booking on its own calendar |
| Lead that already booked | Exists | `suggest(..., meeting=...)`, `next_meeting` | The drafted answer confirms the booked time instead of offering a second link |
| Time zones | Exists, limits | `meetings.py` `TZ_NAMES`, `_zone`, `local_time`; `aime/timeutil.py` `fmt_et`; `app.py` `log_meeting` | Stored in UTC with the zone. The zone is read from Calendly's label (US zones, India, UTC/GMT, or an IANA name); otherwise it defaults to `America/New_York`. Uses `zoneinfo`, so daylight saving is handled. See gaps 1–2 |
| Manager Today panels | Exists | `app.py` `manager_today` (`requests`, `meetings`); `aime/web/templates/manager.html` | **Meeting requests:** the last 7 days, not booked yet, real replies only. **Confirmed meetings:** upcoming, not simulated, shown in ET |
| Sales Manager notification | Partial | `Alert` rows; `aime/notify.py` `notify_event` | Booked, rescheduled, cancelled and unmatched bookings appear on the dashboard, with a Windows toast for someone signed in. No email or phone alert |
| After the meeting | Exists | `aime/pipeline.py` `pipeline_checks` ("Record the outcome" task); `app.py` `meeting_action` (held, no-show, cancel) | A no-show creates a rebooking task |
| Manual logging | Exists | `app.py` `log_meeting` | A person enters the date and time in US Eastern |
| Confirmation workflow | Partial | — | Calendly sends the invitee's confirmation and reminders. The app sends nothing on a booking; it only drafts a confirmation if the prospect writes again |
| Calendly or calendar API | Not built | No API, webhook or OAuth code for Calendly or Google Calendar in `aime/` | — |
| Availability lookup | Not built | — | `aime/calendar_slots.py` is the LinkedIn post calendar, not meeting availability |
| Create, move or cancel events | Not built | `meetings.py` docstring: nothing is created, moved or cancelled in Calendly | **Cancel** on the Meetings page changes only the CRM record |

**Gaps found**
1. **Zone labels match by substring** (`_zone`). For example, "Central European Time" or "Eastern European Time" would
   be read as the US zones, and "Australian Eastern…" as New York, which gives the wrong time. Only Eastern, Pacific
   and India labels are tested.
2. **One meeting, three time zones.** The Meetings page and Manager Today show US Eastern (`et` filter). Alerts use
   the meeting's own zone (`local_time`). The opportunity's next step is written in UTC (`_advance_opportunity`). An
   owner outside the US could misread a time.
3. **Cancel does not reach Calendly.** **Cancel** on the Meetings page does not cancel in Calendly, so the invitee
   still has the event. The owner must cancel it in Calendly as well.
4. **Hard-coded panel text.** The "Meeting requests" panel always says "the reply offers the booking link", even
   before that reply has been approved or sent.
5. **No sender check** (SPF/DKIM), so a forged "calendly.com" notice could create a meeting record (see A2). A
   notice forwarded from the sales mailbox's own address is skipped.
6. **Doc gap.** `docs/PRODUCTION_READINESS.md` (A4) says "the emails carry the owner's Calendly link". Only drafted
   answers to replies do.

## What the owner must do to enable it

To use what exists (no new token is needed):
1. Enable A2. Without it, no booking is ever recorded.
2. In Calendly, keep email notifications on for new, cancelled and rescheduled events, sent to the monitored
   mailbox.
3. Keep `CALENDLY_URL` as it is. `python -m aime email-check` checks that it is a `https://calendly.com/<you>/<event>`
   link.
4. Booking-link answers go out only with A3. Until then, the owner can send the link by hand.

To build a real integration later, the owner first chooses one of the options below and provides its token.

## What happens once enabled

With A2 on:
- A booking appears on Meetings and Manager Today within about 15 minutes of Calendly's email, with an alert.
- The lead moves to `meeting` and its unsent emails are cancelled. A reschedule or cancellation updates the record.
- Two to three hours after the meeting ends, a "Record the outcome" task appears (`pipeline_checks`, on the hourly
  `followups` run).

**Options for building it later:**

- **A. Calendly API v2, read-only (preferred).**
  - The owner creates a personal access token in Calendly. The app polls the user's scheduled events and their
    invitees on each inbox run, replacing email parsing with exact times and zones.
  - Webhooks (`invitee.created`, `invitee.canceled`) would need a public HTTPS endpoint. This app has none: the
    dashboard listens on 127.0.0.1 only. To my knowledge, webhooks also need a paid Calendly plan. Verify this
    against Calendly's current documentation before building. Polling avoids both problems.
- **B. Google Calendar API.**
  - Needs an OAuth client in a Google Cloud project, and the owner signs in once.
  - Free/busy lookup (read-only scope) could suggest times in replies. Creating events needs a write scope and would
    mean booking on someone's behalf.
  - Unverified OAuth apps in "testing" status may have short-lived refresh tokens. Verify this.

Either token should be stored encrypted the way the LinkedIn token is (`aime/secure_store.py`, Windows DPAPI), not in
`.env`. The owner types or authorizes it; the app never asks for a password.

**Consent principle (applies to every option):**
- The prospect picks the time.
- The app never books, moves or cancels a meeting on someone's behalf.
- Any future write action happens only on the invitee's explicit request, with a person's approval, and is
  written to the audit log.
- The most the app does on its own is send a booking link in Automatic mode, under an authorization that covers
  replies (`aime/automation.py` `campaign_for`).

## What still needs a person

- Approving every booking-link answer and confirmation.
- Linking unmatched bookings.
- Reviewing notices that could not be read.
- Cancelling or rescheduling in Calendly itself.
- Marking meetings held or no-show, and deciding the next step.

## Risks and limits

- Email parsing depends on Calendly's email layout. A changed layout goes to review rather than failing silently,
  but no booking is recorded until a person acts.
- The time-zone gaps above.
- An API token would give access to the owner's Calendly or calendar data. As far as I know, a Calendly personal
  access token is not limited to reading, so the read-only rule would be enforced by our code, not by the token.
- Invitee names and emails are stored in the local database.

## How to switch it off

Clear `IMAP_HOST`: no notices are read. Clear `CALENDLY_URL`: drafted answers then ask for times instead of
offering the link (`suggest`). Meetings already recorded stay. Nothing in Calendly is affected, because the app never
writes to it.

## What was and was not tested

- **Tested (fake IMAP and SMTP servers, sample notices):**
  - `tests/test_calendly_booking.py` (13 tests: host layout, day-first dates, reschedule, cancellation, double
    booking, already-booked confirmation, forwarded notices, newsletters ignored);
  - `tests/test_meetings_pipeline.py` (12 tests: Eastern and Pacific conversion, pipeline moves, unmatched
    bookings, manual logging);
  - meeting-request wording in `tests/test_inbox.py` and `tests/test_automation.py` `test_reply_intents`;
  - `tests/test_e2e_sales_autonomy.py`.

  The notices are hand-written copies of Calendly's layouts, not emails from the owner's account.
- **Not tested:** a live Calendly account or its real notification emails, zone labels other than US and India,
  and any calendar API (none exists). Not run for this report.
