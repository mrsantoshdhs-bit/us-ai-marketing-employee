# Super Admin mailbox panel, 28 October 2026

**Branch:** `sales/35-mailbox-admin`, from production `main` at d92574b.

**What it does:** a Super Admin can add, edit, test, enable, disable, read and delete sales mailboxes from the dashboard (**Settings › Mailboxes**, `/admin/mailboxes`), and choose the one **primary** mailbox that sends, without editing `.env`.

**Safety:** everything is **off by default**. Nothing connects to a mail server unless a Super Admin clicks **Test connection**. Nothing is sent unless a mailbox is enabled and primary, `OUTBOUND_EMAIL_ENABLED` is on, and every existing approval is given.

## Mailbox types

| Type | Sign-in | Servers (prefilled, editable) |
|---|---|---|
| Gmail / Google Workspace | App password, typed into the panel and stored encrypted | `imap.gmail.com:993`, `smtp.gmail.com:587` (STARTTLS) |
| Microsoft 365 | OAuth (XOAUTH2) with the organisation's own Entra app registration: client ID, optional client secret (stored encrypted) and tenant. Then **Connect Microsoft 365** | `outlook.office365.com:993`, `smtp.office365.com:587` (STARTTLS) |

## States and what each allows

| State | Read for replies | Sends |
|---|---|---|
| New or **Disabled** (default) | no | no |
| **Enabled** | only with **Read replies** on: read-only (EXAMINE), with its own position | no |
| Enabled + **Primary** (one mailbox at most) | as above | yes, but only when `OUTBOUND_EMAIL_ENABLED=true` and each email is approved as today (`REQUIRE_HUMAN_APPROVAL`, modes, authorizations, limits, suppression and the stop switches all still apply) |

**Fallback:** with no panel mailboxes, the `.env` mail settings work exactly as before. The `.env` mailbox, if configured, is still read alongside panel mailboxes.

## UI and API

- **Panel** (`/admin/mailboxes`):
  - a list with each mailbox's state and last test result;
  - **Test connection**, **Connect Microsoft 365**, **Edit**, **Enable/Disable**, **Read replies**, **Make primary** and **Delete**;
  - an add form.

  The page has no scripts (the dashboard's content security policy). Secret fields are password inputs that are never pre-filled.
- **JSON API** (same session and same-origin rules as the dashboard):

  | Method | Path | Does |
  |---|---|---|
  | GET | `/api/mailboxes` | List mailboxes, with no secrets: `has_secret` and `has_oauth_secret` only |
  | POST | `/api/mailboxes` | Create one (JSON body with the form's fields) |
  | PATCH | `/api/mailboxes/{id}` | Edit one |
  | DELETE | `/api/mailboxes/{id}` | Delete one, with its stored secrets and tokens |
  | POST | `/api/mailboxes/{id}/test` | Test the connection |
  | POST | `/api/mailboxes/{id}/enable`, `/disable` | Enable or disable |
  | POST | `/api/mailboxes/{id}/read_on`, `/read_off` | Turn replies reading on or off |
  | POST | `/api/mailboxes/{id}/primary` | Make it the sending mailbox |

## Security review

| Concern | How it is handled |
|---|---|
| Who can see or change mailboxes | A new role `super_admin`, given **only from the command line** on this PC: `python -m aime user set-role <name> --role super_admin`. `/admin/mailboxes` and `/api/mailboxes` are Super Admin only, **viewing included**. Admins and Sales Managers get 403; signed-out requests get 401 (API) or the sign-in page. A Super Admin can also do everything an admin can |
| Secret storage | The app password and the OAuth client secret are encrypted with Windows DPAPI (current user), like the LinkedIn token. Microsoft 365 tokens are stored the same way, per mailbox (`oauth_tokens`, key `mailbox:<id>`) |
| Secret exposure | **Write-only:** secrets are never rendered, never returned by the API (only "stored: yes/no"), never written to the audit log, and redacted from test-connection errors. An edit keeps the stored secret unless a new one is typed. Deleting a mailbox deletes its secrets and tokens |
| Cross-site requests | Every POST, PATCH and DELETE (forms and API) needs a same-origin header (existing check). Tested: an API call without one is refused (403) |
| Test connection | IMAP sign-in plus read-only folder open, and SMTP STARTTLS plus sign-in, then sign-out. **No MAIL FROM, no recipient, no message.** It runs only on a Super Admin's click, and only when `NETWORK_ENABLED`. TLS certificates are verified (existing TLS context) |
| OAuth | PKCE (S256), a one-time state that expires after 15 minutes, a loopback redirect, and the mailbox's own client ID, secret and tenant. The sign-in is audited (`account_connected`) |
| Audit trail | The audit log records `mailbox_created`, `mailbox_updated` (secret changed or unchanged, never the value), `mailbox_enable`, `mailbox_disable`, `mailbox_read_on` and `mailbox_read_off`, `mailbox_primary`, `mailbox_tested` (passed or failed), `mailbox_deleted` and `account_connected`, each with who and when |
| Input validation | The type must be one of the two; the email address, host names and ports (1–65535) are checked; the app password is required for Gmail and the client ID for Microsoft 365; duplicate addresses are refused |

## CRM impact

- **None on existing data.**
- **Schema changes:** a new table, `mail_accounts`; the `users.role` column gains the value `super_admin` (no migration).
- **Reading:** replies from panel mailboxes go through the same classification, matching, suppression and CRM updates as today.
- **Your own mail:** email from any configured mailbox address counts as our own, so it is skipped, not treated as a reply.
- **Sending:** the primary mailbox uses the existing send path, so limits, delivery tracking and the audit log are unchanged.

## Tests

`tests/test_mailbox_admin.py`: 19 tests, using fake IMAP and SMTP servers and a mock Microsoft sign-in service. They cover:
- **Storage:** secrets are encrypted at rest, never in the view, API or audit log; edits keep the secret.
- **Validation:** 6 kinds of bad input are refused with a reason.
- **Lifecycle:** enable, read, single primary, disable and delete (with tokens removed).
- **Settings:** only the mail settings change; never a switch.
- **Sending:** the primary mailbox sends with its own server and password; nothing is sent without the switch or the primary choice.
- **Reading:** every enabled mailbox with read replies on is read with its own position, and our own mail is skipped.
- **Test connection:** signs in and sends nothing; a failed test is recorded without the password; no connection with the network off.
- **Microsoft 365:** connects with its own app registration (tenant, client ID and secret) and uses XOAUTH2 for IMAP and SMTP.
- **Panel and API:** never show a secret; a call without a same-origin header is refused.
- **Roles:** only a Super Admin sees or changes the panel (admin and Sales Manager get 403, signed-out gets 401); the role is given from the command line, and the doctor counts a Super Admin as an admin.

The full suite results are in the pull request.

## Activation (each step needs the owner's approval)

1. **Deploy this branch** (merge and deploy approval). Nothing changes in behaviour: there are no mailboxes and no switches change.
2. **Make your account Super Admin** from a terminal on this PC: `.\.venv\Scripts\python.exe -m aime user set-role <your user> --role super_admin`. Sign out and in again.
3. **Add a mailbox** in Settings › Mailboxes. It is saved disabled.
   - **Gmail / Workspace:** type the 16-character app password. Gmail needs 2-step verification and IMAP turned on.
   - **Microsoft 365:**
     1. Register an app in Entra, with redirect `http://127.0.0.1:8765/integrations/oauth/callback`.
     2. Give it the permissions IMAP.AccessAsUser.All, SMTP.Send and offline_access. The tenant must allow IMAP and authenticated SMTP for the mailbox.
     3. Enter the client ID, optional secret and tenant.
     4. Click **Connect Microsoft 365**.
4. **Test connection** (your approval to connect): it signs in and sends nothing.
5. **Reading replies** (A2 approval): **Enable**, then **Read replies**.
6. **Sending** (A3 approval):
   1. **Make primary**.
   2. Sending still needs `OUTBOUND_EMAIL_ENABLED=true`, which is the one remaining `.env` switch, kept there deliberately as the master off switch.
   3. Every email is approved as today, and the five held drafts need **Confirm again**.
