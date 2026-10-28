# A2 + A3 + A4 integration test report, 28 October 2026

**Code under test:** three stacked branches from production `main` at efd3beb:
1. `sales/32-a2-mailbox` (A2);
2. `sales/33-a3-sending` (A3, on top of A2);
3. `sales/34-a4-meetings` (A4, on top of A3; contains all three).

**Environment:**
- **Automated tests only,** each run in a fresh scratch data folder (`AIME_DATA_ROLE=test`).
- **Fake external services:**
  - fake IMAP and SMTP servers that accept only the expected sign-in;
  - mock Google and Microsoft sign-in (token) services;
  - a fake Google Calendar and a fake Microsoft Graph;
  - a mock contact at a reserved `.example` domain.
- **Nothing left the test:** no real mailbox, email, calendar, meeting or account was used.
- **Production was not touched:** no file, switch, credential, task or record.

## Results

| Branch | Full suite (final head) | New tests on the branch |
|---|---|---|
| A2 `sales/32-a2-mailbox` | **581 passed, 0 failed** (head 590be37) | 17 (`test_a2_mailbox.py`) |
| A3 `sales/33-a3-sending` | **587 passed, 0 failed** (head 5fcb963) | 6 (`test_a3_sending.py`) |
| A4 `sales/34-a4-meetings` (all three) | **604 passed, 0 failed** (head 8deac77) | 16 (`test_a4_meetings.py`) + 1 end-to-end (`test_e2e_a2_a3_a4.py`) |

Lint (`ruff check aime tests scripts`, including the security rules) passes on every branch. The pre-commit secret scan was clean on every commit; it caught one placeholder secret in a test, which was replaced.

**Fixed during testing:**
1. **Booking emails would never have been sent.** Booking confirmations, move notes and cancel notes were drafted without answering a received email. The existing send rule (a response must answer an email we received) would have cancelled them at send time. Found by the end-to-end test; they now reply in the prospect's thread.
2. **A test depended on run order** (a token counter shared across test files). Fixed.
3. **An existing test checked the old mailbox-status wording.** It was updated to the new, more precise wording.

## End-to-end chain (`tests/test_e2e_a2_a3_a4.py`)

| # | Step | Result |
|---|---|---|
| 1 | The first outreach was already sent with password sign-in (as production today). The owner switches to OAuth and connects the mail and calendar accounts | PASS |
| 2 | **A2:** the prospect asks for a call. The email is read over IMAP with XOAUTH2, read-only. It is classified interested / meeting request / positive, matched to the lead, and the first email is marked `replied` | PASS |
| 3 | **A4:** free slots are read from the calendar, and one reply offering 3 slots waits for approval. No calendar event is created yet | PASS |
| 4 | Nothing is sent before a person approves | PASS |
| 5 | **A3:** a person approves. The email is sent over SMTP with XOAUTH2, inside the window and limits, and marked `sent` | PASS |
| 6 | **A2:** the prospect picks a time. The offer is marked `replied`, and the proposal is now `offered` | PASS |
| 7 | **A4:** a person books slot 2. The calendar is checked again, one event and one meeting are created, and a confirmation waits for approval | PASS |
| 8 | **A3:** the confirmation is approved and sent | PASS |
| 9 | The day before, the Sales Manager is reminded once. Manager Today shows Meeting booking ACTIVE, Mailbox reading ACTIVE, and the meeting | PASS |
| 10 | The audit log holds `account_connected`, `meeting_proposed`, `meeting_booked` and `email_approve` | PASS |
| 11 | An emergency stop from the command line: the followups job is skipped and booking is no longer ready | PASS |

## Security

**Sign-in and tokens:**
- OAuth uses PKCE (S256) and a one-time state that expires after 15 minutes.
- The redirect goes only to `127.0.0.1`.
- Access and refresh tokens are encrypted with Windows DPAPI, never logged or shown. Error messages never contain a token.

**Who can do what:**
- Connecting, disconnecting and the OAuth callback are **administrator-only** and audited.
- Booking, moving and cancelling are sales actions, open to anyone signed in. They are audited and protected by the existing same-origin check.

**Approval and switches:**
- Every outbound email, including offers, confirmations, move notes, cancel notes and reminders, waits for a person.
- A times offer is never sent automatically, even in Automatic mode with an authorization (tested).
- Calendar providers send no invitations by default.
- Sending without a token fails **before** connecting, so nothing is delivered and the email is safe to retry.
- Pause, Stop and Emergency stop hold sending and booking. The new command-line stop works without the dashboard.

## Backward compatibility

- **All new behaviour is off by default:**
  - `MAIL_AUTH=password`;
  - `CALENDAR_PROVIDER` empty;
  - `MEETING_BOOKING_ENABLED=false`;
  - `OUTBOUND_EMAIL_ENABLED=false` unchanged.
- **The existing suite (564 tests on efd3beb) still passes** on every branch, apart from one assertion updated for the new wording.
- **Database changes are additive only,** applied automatically at start-up:
  - a new table, `meeting_proposals`;
  - new columns `oauth_tokens.refresh_token`, `inbound_emails.sentiment`, `inbound_emails.recommended_status`, `followups.delivery_status`, `meetings.event_id`, `meetings.provider` and `meetings.reminded_at`.

  No existing column or row changes.
- **Two behaviour changes for the better:**
  - `b2b_enabled = false` now holds approved outreach instead of cancelling it;
  - Calendly's "Central European Time" is no longer read as US Central.

## Production readiness, by phase

| Phase | Code | Needs from the owner (each a separate authorization) |
|---|---|---|
| **A2** mailbox reading | Ready (PASS) | Either an app password (`IMAP_HOST`, `IMAP_USERNAME`, `IMAP_PASSWORD`), or an OAuth client (Google Cloud or Entra, free) plus `MAIL_AUTH=oauth` and **Connect**. For Microsoft 365, the tenant must allow IMAP |
| **A3** sending | Ready (PASS) | A2 first; `SMTP_PASSWORD` or OAuth; `OUTBOUND_EMAIL_ENABLED=true` with `REQUIRE_HUMAN_APPROVAL=true`; **Confirm again** for #38, #46 and #51–53. Optional later: a rule-based authorization |
| **A4** meeting booking | Ready (PASS) | A2 and A3; an OAuth client with calendar scopes; `CALENDAR_PROVIDER`; **Connect** the calendar; review `[meetings]`; `MEETING_BOOKING_ENABLED=true` |

**Not built, by design or decision:**
- one-click unsubscribe (needs a public endpoint);
- the commercial-message label (a legal decision);
- automatic parsing of which slot the prospect chose (a person clicks Book);
- the Calendly API;
- holidays;
- reading the Spam folder.

**Merging and deploying** each PR needs the owner's explicit approval. Deploying any of them changes no switch; everything stays off until enabled.
