# A3: Email sending readiness

**Date:** 28 October 2026 · **Production commit:** 9ed84b0 · **Method:** code reading only. No app, test, job or
mail connection was run for this report.

## Verdict

**Ready to enable after the owner's steps, for emails a person approves.** SMTP sending, approvals, limits,
suppression, the CAN-SPAM footer, duplicate protection and the stop switches are built and tested with fake mail
servers. In production nothing can be sent today, for two reasons: `OUTBOUND_EMAIL_ENABLED=false`, and
`SMTP_PASSWORD` is empty. 0 emails have ever been sent. The five approved drafts (#38, #46, #51, #52, #53) cannot go
out by themselves: once sending is switched on, each waits until a person presses **Confirm again**. Two conditions
come before any live send. First, mailbox reading (A2) should be on, so that opt-outs and bounces are acted on.
Second, reachability is the real limit: only 5 of 42 qualified leads have a usable address, and all 5 are company
role mailboxes. Sending **without a person** (rule-based authorization) should wait until defect 2 below is fixed (fix on branch `sales/30-campaign-limits`, pending merge).

## What exists

| Capability | Status | Where in the code | Notes |
|---|---|---|---|
| SMTP with verified TLS | Exists | `aime/integrations/email_smtp.py` `EmailSender.send`, `tls_context` | STARTTLS on port 587 with the certificate checked. `status()` refuses `SMTP_USE_TLS=false` when a username is set |
| Timeouts | Exists | `MAIL_TIMEOUT` = 60 s | Applies to the connection and every SMTP command |
| Readiness check | Partial | `EmailSender.status`; `Settings.smtp_configured` (`aime/config.py`) | Checks the switch, `SMTP_HOST`, `SMTP_FROM`, `COMPANY_POSTAL_ADDRESS` and TLS. On 9ed84b0 it **did not check `SMTP_PASSWORD`** (defect 1, fixed on branch `sales/29-a1-unattended`) |
| Message-ID and threading | Exists | `aime/agents/sales_workflow.py` `_send_locked` (Message-ID stored before sending), `_references`; `send(in_reply_to, references)` | Follow-up steps and answers stay in the prospect's thread |
| Uncertain sends | Exists | `_send_locked` exception handling; `_recover_stale_sends`; Follow-ups **Mark sent** / **Requeue** (`aime/web/app.py` `followup_action`) | A timeout mid-send becomes `uncertain` with an error alert. It is never resent automatically |
| Person approval | Exists | `followup_action` (approve); `_send_approved` | "approved" without an approval record is put back to waiting |
| "Confirm again" for approvals given while sending was off | Exists | `aime/automation.py` `observe_sending`, `needs_reconfirmation`, `reconfirm`; checked in `_send_locked`; shown on Follow-ups and Approvals | Switching sending on never releases a backlog. Depends on a stored "sending was off" record (see Risks) |
| Authorization by address | Exists | `automation.py` `create_authorization`, `parse_recipients`, `change_authorization` | Exact addresses only, no wildcards. Limits: 50 recipients, 50 a day, 500 in total, 30 days. Saved as a draft until an administrator activates it |
| Authorization by rule | Exists | `create_rule_authorization`, `rule_problem`, `describe_rule` | Outreach only. Grade A, or B and better. Choice of a decision-maker's published address only, or also company role mailboxes. Optional verified-website condition |
| Send-time decision | Exists (defect 2 fixed on `sales/30-campaign-limits`) | `campaign_for`, `apply_campaign_approvals`, `campaign_approval_problem` | Needs `REQUIRE_HUMAN_APPROVAL=false`, Hybrid or Automatic mode, state running and an active authorization. Pricing, legal and commitment words always go to a person (`SENSITIVE`), as do held or failed checks. See defect 2 |
| Personalized drafting | Exists | `aime/outreach.py` `gather_evidence`, `draft_outreach`, `personal_line` | Quotes the company's own words verbatim and keeps the source link on the draft. With no evidence, the lead gets a review task instead of an email |
| Quality checks | Exists | `aime/qc/email_checks.py` `run_email_checks` | Length 40–160 words. Subject: 70 characters or fewer, no fake "Re:", no shouting. Also checks spam words, unsupported claims, numbers, that quotes match the evidence, no links in the email, the footer, the recipient, spelling, grammar and clichés. A held or failed result needs an explicit "I reviewed" tick. Checks run again after edits |
| Re-check at send time | Exists | `outreach.py` `eligible(for_send=True)` in `_send_locked` | Cancels the email if the lead, address or consent changed |
| Sending limits | Exists | `_outreach_limits`, `_over_limit`; `config/business.toml` `[outreach]` | 20 a day, 1 per company domain a day, 5 per run (outreach only). Counted per US-Eastern day. Uncertain emails count. Answers to people who wrote to us are not limited |
| Business hours | Exists | `_in_send_window`; `[followup]` | Weekdays 08:00–18:00 America/New_York. US holidays are not built |
| Duplicate prevention | Exists | Claim `UPDATE … WHERE status='approved'` in `_send_locked`; `FileLock` `send.lock`; `RetryPolicy(max_attempts=1)`; `_progress_sequences` (one open step); `_ensure_outreach` | At most one send per follow-up and one open email per lead |
| Suppression and opt-outs | Exists | `aime/crm_ops.py` `is_suppressed`, `suppress`; checked when drafting, approving and sending; `python -m aime suppress` | By address or whole domain. Suppressing cancels queued emails |
| CAN-SPAM footer and postal address | Exists | `compliance_footer`, `compliance_problem`; `outreach.py` `outreach_footer` | The footer gives the true reason, the opt-out and the postal address. A missing footer or address blocks the send. The placeholder is replaced when the email is approved |
| `List-Unsubscribe` header | Exists (mailto only) | `EmailSender.send` | No one-click (`List-Unsubscribe-Post`) web link |
| Truthful From and subject | Exists | `send` (From is `SENDER_NAME <SMTP_FROM>`); subject checks in `run_email_checks`; `GIVEN_TO_US` footer | — |
| "This is a commercial message" label | Not built | — | The footer says why the person received the email, but not that it is a solicitation. A legal question for the owner |
| Audit log | Exists | `automation.audit` (`AuditEvent`) for approve, reject, mark sent, requeue, confirmed again, auto-approved, and mode, state and authorization changes; Activity "Email sent" with the body; `/audit` page | — |
| Pause, Stop, Emergency stop | Exists | `automation.change_state`; state check in `_send_approved`; `aime/scheduler/runner.py` `run_job` | Pause sends nothing. Stop also makes the jobs wait. Emergency stop also stops every authorization. Anyone signed in can stop; only an administrator can start or resume |
| Deliverability check | Exists | `aime/email_check.py` `check_settings`, `check_domain`, `check_smtp`, `send_test` | See Risks |
| Test mode | Exists | `EmailSender.recipient_for`; `OUTBOUND_TEST_RECIPIENTS` | For the isolated acceptance copy (`docs/ACCEPTANCE.md`) only. On production data it would mark real prospects' emails as sent |

**Defects and doc gaps found**
1. `EmailSender.status()` reports "Ready" when `SMTP_USERNAME` is set but `SMTP_PASSWORD` is empty, which is
   production today. With the switch on, the dashboard would show "Mail server status: ready". Every send would
   then fail at sign-in ("Send failed (not delivered)") and be retried each run. Nothing would be delivered.
   `email-check` does check the password. **Fixed on branch `sales/29-a1-unattended`:** with a username and no
   password, `status()` now answers "NOT CONFIGURED: SMTP_PASSWORD missing", and Manager Today shows Email sending
   as BLOCKED (test: `test_sending_switched_on_without_a_password_is_blocked_not_active`).
2. **Campaign limits and `REQUIRE_HUMAN_APPROVAL`.** `_send_approved` calls `apply_campaign_approvals` on every run
   while a campaign is active, including outside the send window and when limits are holding emails. Emails it
   approves but cannot send yet stay `approved`, and `sent_counts` counts only sent, sending and uncertain emails.
   So the next run can approve more, and a campaign's daily or total limit can be exceeded. The global 20/day,
   5/run and 1/domain limits still hold. Also, `campaign_approval_problem` does not re-check
   `REQUIRE_HUMAN_APPROVAL`: setting it back to `true` does not hold emails a campaign has already approved. Pausing
   or stopping the authorization, Pause, or Approval mode does. It is not active today (0 authorizations).
   **Fixed on branch `sales/30-campaign-limits`:** limits count approvals still waiting to go out
   (`committed_counts`); campaigns approve only inside the send window; just before sending, a campaign approval
   is re-checked against the campaign's limits and `REQUIRE_HUMAN_APPROVAL`, and goes back to a person if either
   fails (`tests/test_campaign_limits.py`, 5 tests that fail on the old code).
3. `docs/SALES_SETUP.md` §5 and `docs/HANDOVER.md` describe `[outreach] b2b_enabled = false` as only stopping new
   drafts. In the code, `eligible()` then fails at send time, so approved outreach emails, the five drafts
   included, are **cancelled** at the next run. `_ensure_outreach` also never drafts again for a lead that already
   has an email record. Do not use it as a pause.
4. `docs/PRODUCTION_READINESS.md` (A4) said "the emails carry the owner's Calendly link" (corrected on the A1 branch). First-touch outreach
   carries no link: the quality checks reject links. Only the drafted answers to interested replies carry it, and
   those exist only with A2.

## What the owner must do to enable it (in order)

1. **Turn on A2 first** and let it run. Without it, opt-outs by reply and bounces are not acted on. `doctor` warns
   about this (`aime/doctor.py`, "Sales mailbox monitored").
2. **Check the settings:** `COMPANY_POSTAL_ADDRESS`, `SMTP_FROM`, `SENDER_NAME`, and `UNSUBSCRIBE_EMAIL`, which
   should be the monitored mailbox. This review did not confirm their production values. Run
   `python -m aime email-check`: it makes DNS lookups and sends nothing.
3. **Recommended:** the isolated acceptance run in `docs/ACCEPTANCE.md` (test mode, separate data).
4. **Type the app password into `.env` as `SMTP_PASSWORD`** (the owner does this), then run
   `python -m aime email-check --connect`. Expect `SMTP sign-in: pass ... no email was sent`.
5. **Pause** automation in Automation Control. This is a precaution: the next run records that sending was
   switched on but sends nothing.
6. **Set `OUTBOUND_EMAIL_ENABLED=true` and keep `REQUIRE_HUMAN_APPROVAL=true`.** Restart the Dashboard task.
7. **Wait for one `followups` run** (hourly), or run `python -m aime run followups`. On Follow-ups, the five drafts
   should read "Approved before sending was switched on: confirm it again". **If they do not, set the switch back to
   `false` and call an engineer.**
8. **Resume** automation (an administrator does this).
9. **For each draft:** re-read it, then **Confirm again** or **Reject**. A confirmed email goes out on the next run
   inside the window and the limits.
10. **Later, and optional:** routine outreach without a person. Set `REQUIRE_HUMAN_APPROVAL=false`, keep Hybrid mode,
    and have an administrator create **and activate** a rule. Do this only after the defect 2 fix is merged and deployed.

## What happens once enabled

- The drafts #38, #46, #51, #52 and #53 stay held ("confirm it again") until a person confirms each one. Each run
  re-checks the following, and any of them can still cancel or hold an email:
  - eligibility, suppression and opt-outs (and a reply from that lead, with A2);
  - the footer;
  - the per-domain limit (1 per company per day);
  - the weekday 08:00–18:00 ET window.
- Each later sequence step is drafted only after the previous step was sent, and it waits for a person. Step 2 comes
  4 days after step 1 was sent, and step 3 comes 6 days after step 2 (`intervals_days = [0, 4, 10]`,
  `_progress_sequences`).

**Recommended first campaign (conservative):** no authorization at all. Keep `REQUIRE_HUMAN_APPROVAL=true` and confirm
the existing drafts one at a time over several days. Optionally lower `daily_send_limit` and `sends_per_run` in
`config/business.toml` for the first two weeks. Watch Replies, bounces and the Gmail Sent folder for 7–10 days
before approving step 2. If a rule comes later, `addresses = "published"` covers **no** lead today, because 0
decision-makers publish a personal address. Only "company mailbox or published" applies, for example grade B, a
verified website, 3 a day, 10 in total, 7 days.

## What still needs a person

Every email while `REQUIRE_HUMAN_APPROVAL=true`: approving, editing, confirming again, and resolving uncertain sends
(Mark sent / Requeue after checking the Sent folder). Even under an authorization, these always need a person
(`campaign_for`):
- answers to people who wrote to us. The one exception: in Automatic mode, a booking-link answer to a clearly
  interested reply, when the authorization covers replies;
- anything mentioning pricing, contracts or discounts;
- emails whose quality checks are held;
- owner-given test contacts.

## Risks and limits

- **Reachability:** 5 usable role mailboxes out of 42 qualified leads. Role mailboxes are often unread or filtered.
- **Deliverability:** `email-check` checks the settings. It checks MX, exactly one SPF record that authorizes the
  SMTP host, a DKIM key at common selectors (it cannot prove that messages are signed), and DMARC. With `--connect`
  it also signs in to SMTP and IMAP. For a personal `@gmail.com` sender it only warns that SPF and DKIM cannot be set
  and that business filters treat such mail as consumer mail. A Workspace mailbox on the company domain is
  recommended (`docs/SALES_SETUP.md`), but it is a paid service and needs the owner's decision.
- **"Confirm again" relies on stored state.** If the "sending was off" record (`outbound.state`) were missing, the
  first run with sending on would treat sending as always on (`observe_sending`), and old approvals would not need
  confirming. Hourly runs with sending off should have stored it; step 5 and step 7 guard against this.
- **Legal and other limits:**
  - the opt-out works only while replies to the unsubscribe address are read (A2) or read by hand;
  - there is no advertisement label (see the table);
  - sends happen only while the PC runs the scheduled task (A1);
  - mail passwords sit in plain text in `.env`;
  - a sent email cannot be recalled.

## How to switch it off

- `OUTBOUND_EMAIL_ENABLED=false`: from the next tick nothing is sent, and approved emails stay queued. Switching it
  on again requires "Confirm again" for older approvals.
- Automation Control: **Pause** (immediate; nothing is sent) or **Emergency stop** (also stops every authorization).
- Remove `SMTP_PASSWORD`, or revoke the app password in Google. If `IMAP_PASSWORD` is empty, this also stops mailbox
  reading (A2), because reading falls back to the SMTP password.
- Avoid `b2b_enabled = false` as a stop: it cancels approved outreach (defect 3).

## What was and was not tested

- **Tested (fake SMTP servers, in-process):** `FakeSMTP` in `tests/test_inbound_sales.py`, used by `test_outreach`,
  `test_automation` (incl. `test_approvals_given_while_sending_was_off_need_confirming_again`),
  `test_rule_authorizations`, `test_given_contacts`, `test_approval_gates`, `test_inbox`, `test_reply_optout`,
  `test_calendly_booking`, `test_e2e_sales_autonomy`, `test_end_to_end_sales` and `test_live_status` (all in
  `tests/`). `tests/test_email_check.py` uses its own fake SMTP and IMAP servers and supplied DNS answers.
- **Not tested:** a real SMTP sign-in, delivery to a real inbox, Gmail's handling of our Message-IDs, the real
  sending domain's DNS, spam placement, and campaign approvals outside the send window. Not run for this report.
