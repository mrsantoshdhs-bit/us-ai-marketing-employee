# Enterprise audit and implementation report (27 Oct 2026)

Branch `sales/26-enterprise-platform`, from `main` at `ffcdbf7`, which is also what production runs. Not merged, not
deployed.

## How results are labelled

| Label | Meaning |
|---|---|
| REAL | Real integration test: real Gmail, real Calendly, real company websites |
| AUTO | Automated test, offline, with mocks and simulated time |
| SIM | Simulated test on a copy of the acceptance data, every record labelled SIMULATED |
| MANUAL | Manual check in a browser |

## Starting state

| Item | State |
|---|---|
| Open PRs | #5 (LinkedIn image tagline, 25 Oct). Not touched. |
| Production | `ffcdbf7`. Sending, IMAP and publishing off; `REQUIRE_HUMAN_APPROVAL=true`. Drafts #38 and #46 approved and unsent. |
| Acceptance | Same code. Real Gmail reply, booking, reschedule and cancellation tested on 27 Oct. |
| Tests | 458 before this branch, 484 now. |

## Feature audit

| Feature | Status | Evidence |
|---|---|---|
| US company discovery (imports, hiring threads, SEC filings) | IMPLEMENTED AND VERIFIED | AUTO `test_end_to_end_sales`; production has 240 companies |
| Company research (own website, robots.txt first) | IMPLEMENTED AND VERIFIED | AUTO; REAL run on the two test domains today (website check, then research) |
| Provenance on records (source, URL, discovery and verification fields) | PARTIALLY IMPLEMENTED | Fields exist on companies and contacts. There is no single VERIFIED / PUBLICLY SOURCED / USER PROVIDED / AI INFERRED / UNVERIFIED label across records. |
| Decision-maker discovery | IMPLEMENTED AND VERIFIED | AUTO; production has 76 contacts with evidence URLs |
| Email verification | PARTIALLY IMPLEMENTED | Only addresses the company published are used; MX and role-address checks. Mailbox probing is deliberately not done. Guessed emails are never used. |
| Lead qualification (explainable score, grade) | IMPLEMENTED AND VERIFIED | AUTO |
| Lead tiers HOT / WARM / COLD / UNQUALIFIED | NEW, VERIFIED | AUTO `test_lead_tiers_explain_engagement_and_grade`. Shown on Leads, the lead page and the Command Center. |
| CRM: companies, contacts, leads, opportunities, activities, tasks, notes, meetings, dedupe | IMPLEMENTED AND VERIFIED | AUTO |
| Pipeline board | PARTIALLY IMPLEMENTED | Stage changes go through forms. There is no drag and drop, because the dashboard runs no JavaScript by policy. |
| Personalized outreach | PARTIALLY IMPLEMENTED | The composer quotes only published evidence. It is generic when research finds only a page title (see the test contacts below). |
| Approval / Hybrid / Automatic modes, campaign authorizations | NEW, VERIFIED | AUTO `tests/test_automation.py` (26 tests). Not live: sending is off. |
| Automation Control Center, audit log | NEW, VERIFIED | AUTO and MANUAL on demo data, at desktop, tablet and phone widths |
| Gmail sending (SMTP, test mode, threading) | IMPLEMENTED AND VERIFIED | REAL acceptance: two test-mode sends on 27 Oct |
| Inbox monitoring and thread matching | IMPLEMENTED AND VERIFIED | REAL acceptance: UID 6565 |
| Reply classification | IMPLEMENTED AND VERIFIED | REAL: interested and meeting request |
| Reply intents (pricing request, more information, meeting request) | NEW, VERIFIED | AUTO only |
| Unsubscribe handling (suppress, cancel, block approval) | IMPLEMENTED AND VERIFIED | AUTO `test_reply_optout`; not yet a real opt-out reply |
| Follow-up sequences, US business hours, stops on reply, meeting or opt-out | IMPLEMENTED AND VERIFIED | AUTO, with simulated time |
| Holidays, recipient time zones | MISSING | Weekdays plus one send window in US Eastern only |
| Calendly: link, booking, reschedule, cancel, duplicates, time zones | IMPLEMENTED AND VERIFIED | REAL on 27 Oct: booking, reschedule ("Updated:" email) and cancellation; SIM for double bookings |
| Sales Manager notifications | IMPLEMENTED AND VERIFIED | AUTO and REAL (alerts). You still need to confirm that the desktop notifications appeared. |
| LinkedIn content, QC, shadow auto-publish policy | IMPLEMENTED, BLOCKED live | LinkedIn not connected; `PUBLISHING_ENABLED=false` |
| Dashboard sign-in, admin roles, CSP, origin and host checks | IMPLEMENTED AND VERIFIED | 15/15 live probes on 27 Oct; new automation routes are admin-only |
| Windows tasks (tick, dashboard, watchdog) | IMPLEMENTED AND VERIFIED | Production ticks |
| Local AI (Ollama) | IMPLEMENTED, BLOCKED | Its startup entry is disabled; the template writer is used |
| Backups and restore drill | IMPLEMENTED AND VERIFIED | Drill passed 27 Oct |
| Off-machine backup copy | BLOCKED | OneDrive is not signed in |
| Executive Command Center | NEW, VERIFIED | Real counts only; empty charts say so. AUTO and MANUAL. |
| Separate Sales Manager page | PARTIALLY IMPLEMENTED | The "Needs you" action center sits on the Command Center |
| Email campaign screen (per-campaign metrics) | PARTIALLY IMPLEMENTED | Authorizations and their counts are in the Control Center. `/campaigns` is the LinkedIn marketing plan. |

## Operating modes (new)

| Mode | What can go out without a person |
|---|---|
| Approval | Nothing |
| Hybrid (default) | Outreach sequence emails to an exact address on an active campaign authorization |
| Automatic | As Hybrid, plus booking-link answers to clear (high-confidence) interested replies from authorized recipients |

**In every mode, all of the following must hold at the moment of sending:**
- `OUTBOUND_EMAIL_ENABLED=true`.
- `REQUIRE_HUMAN_APPROVAL=false`. True keeps every email with a person, so production behaves exactly as before.
- The automation state is running.
- An administrator-activated authorization names the exact address, within its dates and its daily and total limits.

**Always with a person, whatever the mode:**
- prices, discounts, contracts and other commitments;
- uncertain classifications;
- answers to inquiries;
- anything outside an authorization.

**Other protections:**
- A campaign approves an email only at send time, so switching sending on does not release a backlog. Its approval is checked again before every send.
- Emails a person approved while sending was off need **Confirm again** once sending is on. Production drafts #38 and #46 will need this.
- Pause, Stop and Emergency stop are open to anyone signed in. Start, Resume, mode changes and authorizations need an administrator. All of them are written to the audit log.

## Authorized test contacts (requirement 10)

Run in the acceptance environment only, with nothing sent.

| Contact | Result |
|---|---|
| santosh@digitalhubsolution.com | REAL research: the site is verified; research found only a company inbox. The company is flagged as selling similar services, so the lead is disqualified. Draft #49 was written; quality checks FAILED because the address is user-provided, not published. |
| abhishek@digitalhubsolution.net | REAL: the domain redirects to digitalhubsolution.com, so the website check merged it into the same company. Draft #50, quality checks FAILED. |

**Regenerated on 27 Oct** (still waiting for approval, nothing sent):
- The company is now "Digital Hub Solution", taken from its homepage title.
- The footer is truthful: the address "was given to us directly as a business contact; it was not taken from your website".
- The meeting offered is 30 minutes.

**Still not sendable:**
- Quality checks fail on the user-provided address.
- The lead is disqualified as a company selling similar services.
- The outreach pipeline sends only to published addresses.

A controlled test needs your decision on how to send to these two addresses.

A controlled test needs a decision first: either use the test addresses as `OUTBOUND_TEST_RECIPIENTS` on a published lead, or add a test-contact path with its own truthful footer.

## Known issues and blockers

- Outreach copy is template-based; it is thin when a website offers little to quote.
- Holidays and the recipient's own time zone are not considered.
- There is no single provenance label across records.
- Owner actions still needed:
  - OneDrive sign-in;
  - the Ollama startup entry;
  - the business mailbox password;
  - production IMAP, with Calendly emails forwarded to the business mailbox;
  - LinkedIn connection.
- Automation runs only while the PC is on and awake. It is not 24/7.

## Verdict

**Ready for review**, not for unattended sending.
- Deploying this branch changes nothing that goes out: `REQUIRE_HUMAN_APPROVAL=true` and sending off keep every email with a person.
- Automatic campaigns need, in order:
  1. your explicit decision on `REQUIRE_HUMAN_APPROVAL`;
  2. an activated campaign authorization with exact recipients;
  3. sending switched on.

## Deploying this branch (when approved)

Follow `docs/PRODUCTION_CHECKLIST.md` part A. The migrations are additive:
- new tables `campaign_authorizations` and `audit_events`;
- new columns `followups.authorization_id` and `inbound_emails.intent`.

After deploying, the Automation Control Center should answer "No" to "Can an email go out without a person?", with the reasons listed.
