# A2 implementation: mailbox reading, 28 October 2026

**Branch:** `sales/32-a2-mailbox`, from production `main` at efd3beb.

**Status:** built and tested with mocks only. It is **off** in production, and nothing here reads a real mailbox.

The starting point was [A2_MAILBOX_READINESS.md](A2_MAILBOX_READINESS.md). The mailbox reader, reply classification, opt-out and bounce handling, CRM matching and drafted answers already existed and were reused. This branch adds what was missing.

## What changed

| Area | Change | Where |
|---|---|---|
| **OAuth sign-in (Gmail and Microsoft 365)** | **Owner flow:** the owner connects the account on the Integrations page. Sign-in uses PKCE (S256) and a one-time state that expires after 15 minutes. **Token storage:** tokens are stored DPAPI-encrypted, like the LinkedIn token, and refreshed 2 minutes before they expire. **Who can do it:** connecting is for administrators only and is written to the audit log. **What it enables:** it gives tokens to the mailbox (and, for A3/A4, to sending and the calendar). On its own it reads nothing | `aime/integrations/oauth.py` (new); `app.py` routes `/integrations/oauth/...`; `integrations.html` |
| **IMAP with XOAUTH2** | With `MAIL_AUTH=oauth`, the mailbox signs in with XOAUTH2 instead of the app password. It stays read-only (EXAMINE) | `aime/inbox.py` `Mailbox.fetch_new`; `inbox_agent.py` `_oauth_token` |
| **Reliable fetching** | A message that fails to fetch is tried again on the next 2 runs, then skipped and recorded (`skipped_uids`). Before, it was skipped at once and never read | `aime/inbox.py` (`FETCH_TRIES`) |
| **Retries** | Socket, TLS and IMAP errors are retried once within the run. The comment said so before, but the policy did not | `inbox_agent.py` `retry_policy` |
| **Unsubscribe by subject** | An email whose subject is only "unsubscribe" (what our List-Unsubscribe link produces) is an opt-out with high confidence, and suppresses the address | `reply_intel.py` `UNSUBSCRIBE_SUBJECT` |
| **Sentiment and recommended status** | Every reply gets a sentiment (positive, neutral or negative) and a recommended lead status in words, shown on Replies. Examples: "Opportunity: book the meeting they asked for"; "A person answers the pricing question" | `reply_intel.py` `sentiment_and_recommendation`; `InboundEmail.sentiment` and `recommended_status` |
| **Conversation history** | The lead page shows the conversation in both directions, in time order | `lead_detail.html`, `app.py` `lead_detail` |
| **Honest status** | Mailbox reading counts as configured only with host, user and a way to sign in. Before, the host alone was enough on Manager Today and the Command Center | `config.py` `imap_configured`; `app.py` |

**Database:** additive only, applied automatically:
- `oauth_tokens.refresh_token`;
- `inbound_emails.sentiment`;
- `inbound_emails.recommended_status`.

## Off by default

Nothing is read unless the owner does all of the following:
1. sets `IMAP_HOST`;
2. sets an app password, **or** sets `MAIL_AUTH=oauth` and connects the account;
3. has `NETWORK_ENABLED` true (the default).

Production today has no IMAP host. Reading never changes the mailbox, and every drafted answer waits for a person.

## What the owner provides to enable it (authorization A2)

| Option | Credentials and settings |
|---|---|
| App password (simplest) | `IMAP_HOST=imap.gmail.com`, `IMAP_USERNAME`, and `IMAP_PASSWORD` (a Gmail app password; Gmail must have IMAP on). The owner types the password into `.env` |
| Google OAuth | A Google Cloud OAuth client (free), with the redirect `http://127.0.0.1:8765/integrations/oauth/callback`. Then `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `MAIL_AUTH=oauth`, `MAIL_OAUTH_PROVIDER=google`, `IMAP_HOST=imap.gmail.com` and `IMAP_USERNAME`; then **Connect** on Integrations |
| Microsoft 365 OAuth | An Entra app registration (free), with the same redirect, and IMAP allowed for the mailbox by the tenant admin. Then `MICROSOFT_CLIENT_ID` (and the secret, if confidential), `MICROSOFT_TENANT`, `MAIL_AUTH=oauth`, `MAIL_OAUTH_PROVIDER=microsoft`, `IMAP_HOST=outlook.office365.com` and `IMAP_USERNAME`; then **Connect** |

**Check before relying on it:** `python -m aime email-check --connect` signs in read-only and sends nothing.

## Not built, or limits

- **Folders:** only one folder is read (`IMAP_FOLDER`). Replies filtered to Spam are not read.
- **Notice authenticity:** the sender of a Calendly notice is not authenticated (SPF/DKIM).
- **AI second opinion:** it works only while the local model (Ollama) runs, which needs the owner signed in. Otherwise the rule-based classification alone decides.
- **Notifications:** they appear on the dashboard and as a desktop toast. There is no email or phone notification.

## Tests

`tests/test_a2_mailbox.py`: 17 tests, with mock token servers and fake IMAP servers. They cover:
- PKCE;
- a one-time state that expires;
- tokens encrypted at rest;
- refresh, keeping the old refresh token;
- Microsoft tenant and scopes;
- XOAUTH2 sign-in, still read-only;
- "not connected" reported plainly;
- fetch retry and skip;
- a dropped connection retried in the run;
- unsubscribe by subject;
- sentiment and recommendation;
- the conversation view;
- connect and disconnect being admin-only and audited.

The full-suite results are in [A2_A3_A4_INTEGRATION_TEST_REPORT.md](A2_A3_A4_INTEGRATION_TEST_REPORT.md) on the A4 branch.
