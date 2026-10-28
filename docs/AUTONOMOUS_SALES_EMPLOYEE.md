# Autonomous AI Sales Employee: status, fixes, evidence and activation

Branch `sales/36-autonomous-sales`, based on production `6ed328a` (28 Oct 2026).

**Nothing here sends email, reads a mailbox or books a meeting.** Everything new is off until a Super Admin turns it on. Production is unchanged, and nothing is merged or deployed without the owner's approval.

## 1. Implementation status

Each component was audited against the code and the tests, then against production data. The production data was read from a read-only copy of the live database.

| # | Component | Status | Evidence |
|---|---|---|---|
| 1 | **Lead discovery and sources** | PARTIAL | `aime/sources/` (`hn_hiring`, `sec_edgar`, `csv_import`, `census`) and `aime/agents/research.py`. **Production:** 280 US companies discovered from authorized sources (235 HN hiring, 45 SEC EDGAR). Discovery now follows the configured country; only US has sources. There is no source of local businesses, so the SEO and website services can't be targeted. **Tests:** `test_research`, `test_discovery_quality`. |
| 2 | **Verification and dedup** | WORKING | `validation.py`, `website_check.py`, `dedupe.py`, `contact_validation.py`, `email_check.py`. **Production:** 0 duplicate domains; 1 duplicate name, both records with unreachable websites. Company-name parser fixed here. **Tests:** `test_contact_validation`, `test_email_check`, `test_given_contacts`. |
| 3 | **Scoring and qualification** | WORKING | `scoring.py`, `agents/qualification.py`; thresholds now editable by the Super Admin. **Production:** 42 qualified (B), 228 nurture (C/D), 9 disqualified. **Tests:** `test_scoring`. |
| 4 | **CRM lead creation** | WORKING | `crm_ops.py` (company, lead, signal, activity, opportunity). **Production:** 280 companies and 279 leads with evidence signals. |
| 5 | **AI email generation** | WORKING | `outreach.py`, `email_templates.py`, `qc/`: personalized from the company's own public evidence, with quality checks. **Production:** 5 outreach drafts approved by the owner. **Tests:** `test_outreach`, `test_quality_checks`. |
| 6 | **Sending and delivery tracking** | BLOCKED | Built and tested (A3): `integrations/email_smtp.py`, campaign, domain and mailbox limits, send window, bounce tracking. Blocked in production: `OUTBOUND_EMAIL_ENABLED=false` and no mailbox added. **Tests:** `test_a3_sending`, `test_campaign_limits`. |
| 7 | **Follow-ups** | BLOCKED | Built: 3-email sequence on days 0, 4 and 10, stopping on a reply, an opt-out or a bounce (`agents/sales_workflow.py`). Blocked in production by sending. **Tests:** `test_outreach`, `test_automation`. |
| 8 | **Inbound classification** | BLOCKED | Built (A2): `reply_intel.py` (interested, meeting request, pricing, question, not interested, opt-out, out of office, bounce). Blocked in production: no mailbox reading. **Tests:** `test_inbox`, `test_reply_optout`, `test_inbound_sales`. |
| 9 | **Autonomous conversations** | WORKING (fixed here) | Before: every reply waited for a person. **Now:** in Automatic mode with a campaign that covers answers, the AI employee offers meeting times, books the time the prospect chose and confirms it on its own. Prices, questions, unclear or sensitive replies still go to a person. **Tests:** `test_autonomous_sales`. |
| 10 | **Calendar and booking** | BLOCKED | Built (A4): free slots, offer, book, move, cancel, reminders; Calendly link as the fallback. Now also autonomous booking. Blocked in production: no calendar connected and booking switched off. **Tests:** `test_a4_meetings`, `test_calendly_booking`. |
| 11 | **Sales Manager handover** | WORKING (was missing) | `aime/handover.py`: an interested reply or a booked meeting assigns the opportunity to the configured Sales Manager, with an alert, an activity, an audit entry and a notification; shown on the pipeline card. **Tests:** `test_autonomous_sales`. |
| 12 | **Super Admin configuration** | WORKING (was partial) | Before: only the Mailboxes panel. **Now:** a new Sales settings page at `/admin/settings`. Campaigns and emergency stop are on Automation Control; calendar on Integrations. Production has no Super Admin account yet. **Tests:** `test_autonomous_sales`, `test_mailbox_admin`. |
| 13 | **Scheduler reliability** | WORKING | A1: the unattended tick ran after a real sign-out (PASS); watchdog; Task Scheduler history on since 28 Oct. Ticks missed while asleep on battery are expected (wake timers are off on battery). **Docs:** `A1_*.md`. |
| 14 | **Security, compliance, audit** | WORKING | **Opt-out:** opt-out line and List-Unsubscribe header in every email, suppression list. **CAN-SPAM:** postal address required before anything sends. **Controls:** limits, emergency stop, audit log of every decision. **Access and secrets:** dashboard roles, DPAPI secrets, CSP, secret scan on the public repo. **Tests:** `test_policy`, `test_approval_gates`, `test_hardening`. |

The Super Admin page in row 12 sets:
- country, states, industries and services;
- grade thresholds and the minimum grade for outreach;
- daily, per-domain, per-run and per-mailbox limits;
- the sequence;
- the send window;
- meeting hours, length, notice and reminders;
- the Sales Manager.

## 2. Confirmed missing functionality

### Fixed in this branch

1. **Autonomous answering was missing.**
   - Before: every reply needed a person, even inside an approved campaign.
   - Now: a rule campaign can also cover answers ("Also answer these prospects on its own" on Automation Control).
2. **Autonomous booking was missing.** The prospect's answer to a times offer was never read. Now `booking.act_on_answers` reads it:
   - when the prospect clearly chose one time (by option, ordinal, weekday or clock time, with every clue pointing to the same time) and wrote nothing else that needs an answer, the AI employee books it and the confirmation goes out under the campaign;
   - "Nothing else" means: written above the quote, with no question, no price or information request, and no objection, opt-out or referral;
   - an unclear answer, a declined time or a request for another time goes to a person, with the reason.
3. **Sales Manager handover was missing.** `aime/handover.py`, `Opportunity.owner`, and the Sales Manager is chosen in Sales settings.
4. **Super Admin configuration was missing.**
   - Before: only the TOML file.
   - Now: `aime/admin_settings.py` and `/admin/settings`, open to the Super Admin role only.
   - Every field is validated: grades must go A > B > C, there is one day number per sequence email, and time zones and hours are checked.
   - Settings are stored as overrides in the database and applied at once in the dashboard and on the next scheduled run.
   - Every change is audited with its old and new value; "Reset" returns to `config/business.toml`.
5. **Discovery ignored the target country.** Sources now declare the countries they cover. A country without a source reports "no coverage" instead of storing US companies under it, while your CSV imports work for any country.
6. **Company names could carry a URL or a label** ("Name https://...", "Company: name", or a "Role:" line instead of a name). The HN parser is fixed for new records; existing records are left as they are.

### Not fixed, with a plan

7. **Digital Marketing, SEO, Local SEO and Website Development are not in the service catalog**, and no authorized source finds the local businesses that buy them. Two parts are needed:
   - three catalog entries, with content-bank copy, outreach phrasing and contact-role maps;
   - a free, lawful source of local businesses with a website: OpenStreetMap (ODbL) or your own CSV lists.

   Estimate: 2 to 3 days of development, then a review.
8. **Free-form answers to questions and price requests are not autonomous.** This is deliberate: they go to a person with a suggested draft. Autonomy could later be widened to an approved FAQ answer bank.

## 3. Fixes and files

| Area | Files |
|---|---|
| Autonomy rules | `aime/automation.py`: `autonomous_authorization`, `lead_rule_problem`, rule campaigns with `answer_replies`, response purposes |
| Booking from the answer | `aime/booking.py`: `chosen_slot`, `act_on_answers`, purposes on drafts, handover on booking; `aime/agents/inbox_agent.py` |
| Handover | `aime/handover.py` (new); `aime/reply_intel.py`; `aime/meetings.py`; `aime/web/templates/pipeline.html` |
| Sales settings | `aime/admin_settings.py` (new); `aime/context.py`; `aime/web/app.py`; `aime/web/auth.py`; `aime/web/templates/admin_settings.html` (new), `base.html`, `automation.html`; `aime/web/static/style.css` |
| Country rule | `aime/sources/base.py`, `aime/sources/csv_import.py`, `aime/agents/research.py` |
| Name parsing | `aime/sources/hn_hiring.py` |
| Schema | `aime/models.py` adds three columns: `followups.purpose`, `meeting_proposals.answer_email_id`, `opportunities.owner`. They are added automatically on start, and existing rows are untouched. |
| Config | `config/business.toml` gets a new `[handover]` section |

## 4. Tests

| Run | Result |
|---|---|
| New: `tests/test_autonomous_sales.py` | 19 passed |
| New name-parsing test in `tests/test_research.py` | passed |
| Changed: the A4 offer test, which now also proves the campaign path | passed |
| Full regression (`tests/`, 50+ files) | FULL_RESULT |
| `ruff check aime tests scripts` | clean |

`test_autonomous_sales.py` covers:

1. **Reading the chosen time:**
   - 5 clear answers are read;
   - 5 unclear or declined answers go to a person.
2. **Who may act alone:** a person is needed when approval is required, outside Automatic mode, when the campaign does not cover answers, for another address, and when paused.
3. **Rule campaigns with answers:** created from the form, audited, and shown on the page.
4. **The whole chain with fakes:**
   1. a meeting request arrives;
   2. the opportunity is handed over to the Sales Manager, who appears on the pipeline card;
   3. a times offer is drafted from the free calendar;
   4. the offer is sent under the campaign with no person;
   5. "The second one works" arrives;
   6. the time is booked once, with one calendar event, and the actor is "AI employee (campaign #N)";
   7. the confirmation is sent under the campaign;
   8. the booking is handed over too;
   9. no draft is left for a person to send by mistake.
5. **Escalation:** each of these goes to a person, nothing is booked, and the drafted answer to their question is kept:
   - an unclear answer in Automatic mode;
   - a chosen time that comes with a price question;
   - a clear answer in Hybrid mode.
6. **Country rule:** with Canada as the target, HN and SEC report "no CA coverage".
7. **Sales settings:**
   - saved, applied, audited, and loaded by a fresh context (as the scheduled jobs do);
   - five invalid inputs are refused with nothing half-saved;
   - reset works;
   - an Admin gets 403, a Super Admin sees and saves.

## 5. The 20 USA prospects

All 20 were discovered by the production AI employee from authorized public sources (HN hiring posts via the official API; SEC EDGAR was searched too). They are **already in the CRM**: none were inserted by hand and there are no duplicates (20 different CRM ids and websites).

| Check | Result |
|---|---|
| Location evidence from the source | 20 of 20 US |
| Website names the company; re-checked live on 28 Oct (one homepage request each) | 20 of 20, all HTTP 200 |
| Grade B, qualified, with score, need evidence and source URL | 20 of 20 |
| Published email address | 5 |
| Published phone only | 2 |
| Named decision-maker without a published email address | 13 (the system does not guess addresses) |

Matched services: Software Development, AI Agents, CRM Development, Business Process Automation.

The record-level list is kept **outside this public repository**, in `%USERPROFILE%\Documents\aime-acceptance\PILOT-USA-20261028\`. That folder holds `prospects.csv` (company, website, industry, location, public business contact with its source page, potential need, score, source URL and verification) and a README.

## 6. End-to-end evidence

| Stage | Where it is proven | Live or simulated |
|---|---|---|
| Discovery → verification → qualification → CRM | Production CRM: 280 companies, 42 qualified, the 20-prospect list | **Live (production, read-only)** |
| Personalized email → approval or campaign → send | `test_e2e_sales_autonomy`, `test_end_to_end_sales`, `test_rule_authorizations` | Simulated (fake SMTP) |
| Follow-up sequence and stop rules | `test_outreach`, `test_automation`, `test_reply_optout` | Simulated |
| Reply understanding | `test_inbox`, `test_inbound_sales`, `test_e2e_a2_a3_a4` | Simulated (fake IMAP) |
| AI conversation → meeting time → booking → confirmation → handover | `test_autonomous_sales::test_offer_book_and_confirm_on_their_own_and_hand_over_to_the_sales_manager` | Simulated (fake IMAP, SMTP and Google Calendar) |
| Escalation of sensitive or unclear cases | `test_autonomous_sales::test_unclear_answers_and_hybrid_mode_go_to_a_person`, `test_inbound_sales` | Simulated |

A live end-to-end run is **not** done yet. It needs the activation steps below and the owner's explicit go-ahead. Until then, the AI Sales Employee is proven in simulation only and must not be called complete in production.

## 7. Remaining blockers

1. This branch must be reviewed, merged and deployed: needs owner approval.
2. Production has **no Super Admin account**: the owner creates one and types the password.
3. Production has **no mailbox**. Sending (A3) and reading replies (A2) can't run until the owner adds one in the Mailboxes panel and types its password or connects OAuth.
4. Production switches: `OUTBOUND_EMAIL_ENABLED=false` and `REQUIRE_HUMAN_APPROVAL=true`. The owner changes them, and only after the controlled test.
5. **Calendar:** no Google or Microsoft OAuth client is configured, so autonomous booking can't run. Until then, interested prospects get the Calendly link, and the owner books by hand.
6. **13 of the 20 pilot prospects have no published email address.** Outreach reaches only the 5 with an address (the 2 with a phone can be called by a person).
7. **Gap 7** (the SEO and website services and local businesses) limits the pilot to the tech services.
8. **Deliverability:** a new sending mailbox should ramp up slowly (about 5 a day, rising over 2 to 3 weeks). Check SPF, DKIM and DMARC for the sending domain.

## 8. Realistic timeline

All the work below starts after the owner approves each step.

| When | What | Who |
|---|---|---|
| Day 0 (1 to 2 h) | Review and approve this PR; merge and deploy with the usual backup and restore drill; create a Super Admin; open Sales settings and choose the Sales Manager | Owner approves; deploy as before |
| Day 1 (1 h) | Add the sending mailbox in the Mailboxes panel and test the connection; turn on reading replies; send one test from the owner's second address to themself | Owner |
| Day 1 to 2 | Controlled live test: turn sending on, with a campaign for the owner's own test address only. Run outreach → reply → classification → offer → booking (Calendly link, or the calendar once connected) → handover. Then check the audit log | Owner and me |
| Days 3 to 7 | Pilot in **Hybrid** mode: a rule campaign for grade B prospects with published role addresses, about 5 a day; a person approves every answer | Owner monitors daily |
| Week 2 | **Automatic** mode with "answer these prospects on its own", if week 1 has no wrong classifications and no complaints | Owner decides |
| Weeks 2 to 3 | Gap 7 (the SEO and website services and a source of local businesses), as a separate PR | Development |

## 9. Production activation checklist

Do these in order; each item needs the owner's explicit approval. Stop and switch back to Approval mode on any surprise; the emergency stop is on Automation Control.

1. [ ] PR approved; CI green on the exact head; full suite green.
2. [ ] Maintenance on; `python -m aime backup --label autonomous-sales`; restore drill; deploy check on the restored copy.
3. [ ] Merge with `--match-head-commit`; fast-forward production; doctor; maintenance off; dashboard restarted; post-deploy check on a copy of the live database; next tick confirmed.
4. [ ] `python -m aime user create --username <name> --role super_admin`, or promote the existing account with `python -m aime user set-role --username <name> --role super_admin`. The owner types any password.
5. [ ] In Sales settings, check the target country, industries, services, grades and limits, choose the Sales Manager, and save. The change appears in the audit log.
6. [ ] In the Mailboxes panel, add the mailbox, test it, enable it and make it primary. Leave sending off.
7. [ ] Turn on reading replies; confirm that one test reply is classified correctly.
8. [ ] Check that `COMPANY_POSTAL_ADDRESS` and the unsubscribe address are correct, then preview one outreach email.
9. [ ] Set `OUTBOUND_EMAIL_ENABLED=true`. Create a campaign for the owner's own test address only; run the controlled test end to end; review the audit log.
10. [ ] Calendar (optional): set up the OAuth client, connect on Integrations, then `MEETING_BOOKING_ENABLED=true`.
11. [ ] Pilot campaign in Hybrid mode: rule on grade B and published or role addresses, a daily limit of 5 at most, 7 days.
12. [ ] After a clean week, set `REQUIRE_HUMAN_APPROVAL=false` and Automatic mode. Tick "answer these prospects on its own" only on a campaign you trust.
13. [ ] Check daily: Manager Today, the alerts, bounces under 3%, every opt-out honoured. Use the emergency stop if anything looks wrong.
