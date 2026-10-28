# A3 implementation: email sending, 28 October 2026

**Branch:** `sales/33-a3-sending`, stacked on `sales/32-a2-mailbox` (A2).

**Status:** built and tested with fake SMTP servers only. Sending stays **off** in production, and nothing here sends a real email. Every email still needs a person's approval.

The starting point was [A3_EMAIL_SENDING_READINESS.md](A3_EMAIL_SENDING_READINESS.md). Almost everything already existed and was reused:
- personalized drafts with quality checks;
- sequences and follow-ups;
- human approval and "Confirm again";
- campaign authorizations by address and by rule;
- limits per day, per run and per domain;
- the business-hours window;
- suppression;
- the CAN-SPAM footer and List-Unsubscribe;
- duplicate-safe sending;
- the audit log;
- Pause, Stop and Emergency stop.

The campaign-limit defect was fixed in PR #48 (deployed).

## What changed

| Area | Change | Where |
|---|---|---|
| **OAuth SMTP (Gmail and Microsoft 365)** | With `MAIL_AUTH=oauth`, sending signs in with XOAUTH2 using the account connected in A2. The token is fetched **before** connecting; without one, nothing is sent and the email waits ("not delivered", safe to retry) | `email_smtp.py` `EmailSender.send` and `status`; `sales_workflow.py` `_connect_oauth` |
| **Per-mailbox daily limit** | `[outreach] mailbox_daily_limit = 50`: every email from the sales mailbox in a rolling 24 hours counts, whether outreach, follow-up or answer. Over the limit, emails wait; they are not cancelled. This is on top of the existing 20 outreach a day, 5 per run and 1 per company domain | `sales_workflow.py` `_outreach_limits` and `_send_locked`; `business.toml` |
| **Delivery tracking** | Each email moves from `sent` to `bounced` (a bounce for its address) or `replied` (a person answered it). Shown on Follow-ups | `FollowUp.delivery_status`; `outreach.py` `record_delivery`; `reply_intel.py` `act` |
| **B2B switch** | `[outreach] b2b_enabled = false` now **holds** approved outreach, as the docs say. Before, it cancelled approved emails at send time, which would have cancelled drafts #38, #46 and #51–53 | `sales_workflow.py` `_send_locked` |
| **Command-line stop** | `python -m aime automation pause|stop|emergency-stop|status`: stops that work when the dashboard is down. They are audited as "command line". Starting and resuming stay admin-only on the dashboard | `aime/cli.py` `cmd_automation` |

**Database:** additive only: `followups.delivery_status`.

## Off by default

Nothing is sent while `OUTBOUND_EMAIL_ENABLED=false`, which is production today. With it on and `REQUIRE_HUMAN_APPROVAL=true`, every email still needs a person.

Drafts #38, #46 and #51–53 were approved while sending was off. They stay held until a person presses **Confirm again** on each. Nothing on this branch releases them.

## What the owner provides to enable it (authorization A3)

Order matters:
1. **A2 first**, so opt-outs and bounces are read.
2. **Mail sign-in:**
   - **App password:** `SMTP_PASSWORD`, typed by the owner.
   - **Or OAuth:** `MAIL_AUTH=oauth` with the account connected in A2. Gmail needs the `https://mail.google.com/` scope (already requested). Microsoft needs `SMTP.Send`, and the tenant must allow authenticated SMTP for the mailbox.
3. **Check:** `python -m aime email-check --connect`.
4. **Pause the automation,** set `OUTBOUND_EMAIL_ENABLED=true` (keep `REQUIRE_HUMAN_APPROVAL=true`), wait for one followups run, and check that the five drafts read "confirm it again". Then resume.
5. **Confirm or reject** each old draft by hand.
6. **Later and optional:** routine outreach without a person. That means `REQUIRE_HUMAN_APPROVAL=false` plus an administrator-activated rule-based authorization; defect 2 is fixed by PR #48.

## Not built, or limits

- **One-click unsubscribe** (RFC 8058, `List-Unsubscribe-Post`) needs a public HTTPS endpoint. The dashboard is local-only, so only the mailto link exists.
- **"This is a commercial message" label:** a legal question for the owner. It is not added automatically.
- **Deliverability:**
  - **Gmail address:** a personal `@gmail.com` sender cannot have SPF or DKIM for a business domain.
  - **Company-domain mailbox:** recommended, but a paid service and the owner's decision.
- **Reach:** only 5 of 42 qualified leads have a usable address, all company mailboxes.

## Tests

`tests/test_a3_sending.py`: 6 tests, with fake SMTP and IMAP servers and a mock token server. They cover:
- XOAUTH2 sign-in, with no password;
- nothing sent, and the email waiting, without a connected account;
- the per-mailbox limit across every kind of email;
- `bounced` and `replied` tracking;
- the B2B switch holding and then releasing;
- the command-line emergency stop, audited.

The full-suite results are in [A2_A3_A4_INTEGRATION_TEST_REPORT.md](A2_A3_A4_INTEGRATION_TEST_REPORT.md) on the A4 branch.
