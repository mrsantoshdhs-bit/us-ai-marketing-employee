# A4 implementation: meeting booking, 28 October 2026

**Branch:** `sales/34-a4-meetings`, stacked on A3 and A2.

**Status:** built and tested against fake Google Calendar and Microsoft Graph servers only. It is **off** in production: no calendar is connected and nothing books a real meeting.

The starting point was [A4_MEETING_BOOKING_READINESS.md](A4_MEETING_BOOKING_READINESS.md). Several pieces already existed and are kept unchanged:
- meeting-request detection (A2);
- the booking link in drafted answers;
- Calendly notice parsing, including reschedules and cancellations;
- meetings in the CRM, with opportunity and stage;
- the Meetings page.

This branch adds the calendar itself.

## How it works

1. **A meeting request arrives** (A2). The AI employee reads when the owner is busy in the connected calendar. It then picks up to 3 free slots, at most one per weekday:
   - inside the prospect's business hours (`[meetings] prospect_hours`, 09:00–17:00), in their US time zone, taken from the company's state;
   - inside the owner's working hours (`owner_hours`, 08:00–23:30 Asia/Kolkata, because US mornings are Indian evenings);
   - at least 24 hours ahead.
2. **It drafts one reply offering the slots,** replacing the booking-link draft rather than adding a second email. That reply **waits for a person's approval** and is never sent on its own, even in Automatic mode with an authorization.
3. **The prospect answers with the time that suits them,** and a person clicks **Book this time** on the Meetings page. The slot is checked against the calendar **again**, then:
   - **exactly one event** is created (idempotent: the Google event id or Microsoft `transactionId` comes from the proposal, so a retry never books twice);
   - the meeting is recorded in the CRM (stage, opportunity, activity);
   - the action is written to the audit log;
   - a confirmation is **drafted for approval** in the prospect's thread.
4. **Move and Cancel** on the Meetings page change the calendar event and draft a note for approval.
5. **Reminder:** 24 hours before each meeting (`reminder_hours`), the Sales Manager gets an alert. A reminder for the prospect is drafted only if `remind_prospect = true`, and it also waits for approval.
6. **No provider invitation emails by default** (`sendUpdates=none`). The prospect hears only from the owner's approved email. `CALENDAR_SEND_INVITES=true` changes that.

## What changed

| Area | Where |
|---|---|
| Calendar providers: busy times, plus idempotent create, move and cancel | `aime/integrations/calendar.py` (new): `GoogleCalendar` (API v3) and `MicrosoftCalendar` (Graph) |
| Slots, proposals, booking, moving, cancelling, reminders | `aime/booking.py` (new) |
| Proposal model; calendar fields on meetings | `MeetingProposal` (new table `meeting_proposals`); `Meeting.event_id`, `provider`, `reminded_at` |
| Inbox job hook (every 15 minutes) | `inbox_agent.py` `_meetings`: reminders, marking proposals offered, proposing for new requests when booking is ready |
| Dashboard | **Meetings page:** offered times, with Book, Close, Propose and Move. **Manager Today:** Meeting booking shows NOT CONFIGURED, WAITING FOR AUTHORIZATION, ACTIVE or PAUSED, and the request panel text is now accurate |
| Settings | `.env`: `CALENDAR_PROVIDER`, `MEETING_BOOKING_ENABLED`, `CALENDAR_SEND_INVITES`. `business.toml`: the `[meetings]` section |
| Time-zone fix | Calendly labels are matched as whole names: "Central European Time" is no longer read as US Central (`meetings.py` `_zone`) |

## Off by default

Booking works only when **all** of these are true:
- `CALENDAR_PROVIDER` is set;
- the calendar account is connected (A2's OAuth, purpose "calendar");
- `MEETING_BOOKING_ENABLED=true`;
- the automation is running.

Otherwise everything behaves as before: the booking link plus Calendly notices.

## What the owner provides to enable it (authorization A4)

1. **Register an OAuth client** with the calendar scopes:
   - **Google:** `calendar.events` and `calendar.freebusy` (requested automatically).
   - **Microsoft:** `Calendars.ReadWrite`.
2. **Set** `CALENDAR_PROVIDER=google` (or `microsoft`), then **Connect** the calendar on Integrations.
3. **Review `[meetings]`:** owner time zone, working hours, notice, slots offered.
4. **Switch on:** `MEETING_BOOKING_ENABLED=true`.
5. **Prerequisites:** A2 (to read the requests and answers) and A3 (to send the approved offers and confirmations).

## Not built, or limits

- **Choosing the slot:** a person reads which time the prospect chose and clicks Book. Free-text time parsing is not automated.
- **Calendar coverage:** only the owner's primary calendar is checked, and US public holidays are not considered.
- **Calendly:** not integrated through its API. The link and notice emails still work.
- **Rescheduling by the prospect:** done through their own reply, which a person reads.

## Tests

`tests/test_a4_meetings.py`: 16 tests against a fake Google Calendar, a fake Microsoft Graph and a mock token server. They cover:
- **Slot rules:** both time zones, busy times, notice, no weekends, and the owner's hours.
- **Time-zone labels.**
- **The offer:** it waits for a person, even in Automatic mode.
- **Nothing touches the calendar** when booking is disabled, not connected or paused.
- **Booking:** exactly one event; a retry after a 409 books nothing new; a slot that has become busy is refused.
- **Move and cancel** go through the calendar, with a note drafted for approval.
- **Reminders** fire once.
- **Microsoft 365:** free time is ignored, and the transaction id makes the create idempotent.
- **The Manager Today status.**

The end-to-end chain is in [A2_A3_A4_INTEGRATION_TEST_REPORT.md](A2_A3_A4_INTEGRATION_TEST_REPORT.md).
