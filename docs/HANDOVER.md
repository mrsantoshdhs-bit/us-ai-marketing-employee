# US AI Sales Employee: handover

## What it does

A supervised sales workflow for US B2B prospects, run by scheduled jobs on this PC, with one Sales Manager
approving everything that leaves the building:

discovery (public hiring posts, SEC EDGAR, your CSV lists) → website check → **company research** (their own
pages, quoted) → **decision-makers** (named by the company or SEC filings, with evidence) → **business emails**
(only those the company publishes; mail server checked) → **explainable scoring** → CRM (companies, contacts,
leads, opportunities, activities, tasks) → **evidence-quoted outreach drafts** with quality checks →
**your approval** → sending through Gmail with limits and test mode → **follow-up steps** (each approved) →
**inbox reading** (read-only) → reply classification → sequence stops, suppression, opportunities,
suggested replies → **Calendly bookings** → meetings → pipeline automation → daily/weekly reports.

## Where things are

| Area | Code | Dashboard |
|---|---|---|
| Discovery | `aime/agents/research.py`, `aime/sources/` | Companies |
| Company research, decision-makers | `aime/site_reader.py`, `aime/people.py`, `aime/company_research.py`, `aime/sources/sec_officers.py`, `aime/agents/company_research.py` | Decision-makers, lead page |
| Contact validation | `aime/contact_validation.py` | Decision-makers |
| Scoring | `aime/scoring.py`, `aime/agents/qualification.py` | Leads |
| Outreach, approvals, sending | `aime/outreach.py`, `aime/qc/email_checks.py`, `aime/agents/sales_workflow.py`, `aime/integrations/email_smtp.py` | Follow-ups |
| Inbox, replies | `aime/inbox.py`, `aime/reply_intel.py`, `aime/agents/inbox_agent.py` | Replies |
| Meetings, pipeline | `aime/meetings.py`, `aime/pipeline.py` | Meetings, Pipeline |
| Reports, acceptance evidence | `aime/agents/analytics.py`, `aime/acceptance.py` | Reports |
| Sign-in, roles | `aime/web/auth.py` (admin: job runs, integrations; Sales Manager: everything sales) | — |

## Scheduled jobs (`config/schedule.toml`)

db_maintenance 06:15 (verified backup, off-machine copy) · crm_maintenance 06:30 · plan 06:45 · research 07:00 ·
**enrich 07:15** · content 07:30 · inbound every 30 min · **inbox every 15 min** · **followups hourly** (drafts,
approved sends within 08:00–18:00 ET weekdays, pipeline automation) · publish every 30 min · reports · monitor.

## Settings that matter

- `.env`: `OUTBOUND_EMAIL_ENABLED` (master switch, **false**), Gmail `SMTP_*` / `IMAP_*`, `COMPANY_POSTAL_ADDRESS`,
  `CALENDLY_URL`, `OUTBOUND_TEST_RECIPIENTS` (test mode). Setup: `docs/SALES_SETUP.md`.
- `config/business.toml`: `[outreach]` (on/off, drafts per run, limits, intervals), `[enrichment]`, `[pipeline]`.

## Deployment status

- **Deployed 26 Oct 2026:** #19–#24 (production `main` at `715f881`), with outbound email and publishing off.
- **Waiting for your approval** (each with CI; merge in this order):
  - #25 research accuracy: no product pages as team pages, no places or customers as staff, competitors from
    their own site, withdrawal of people no longer found, and disqualified leads' drafts cancelled.
  - #26 `email-check`.
  - #27 Calendly reschedules, late cancellations and linking unmatched bookings.
  - #28 Today tiles and these docs.
- **Same deploy flow as before:** verified backup, maintenance on, merge, fast-forward the production folder,
  tests and doctor, restart the dashboard, maintenance off, confirm the next tick.
- **After #25, run `python -m aime research-recheck` once.** On a copy of production it withdrew the three wrong
  contacts, found Curai's CEO and flagged five competitors.

**Nothing is sent after deploying**: outbound stays off, every email needs approval, and drafts only appear for
researched contacts with a published email.

## Rollback

Revert the PRs in reverse order (new tables and columns stay, unused), or restore the pre-deploy backup
(`python -m aime restore-drill --source <backup>` first, then the documented manual restore). Without
reverting: `OUTBOUND_EMAIL_ENABLED=false` stops all email; `[outreach] b2b_enabled = false` stops new outreach
drafts; empty `IMAP_HOST` stops mailbox reading.

## Owner actions

1. Gmail app password, `.env` values, SPF/DKIM/DMARC (`docs/SALES_SETUP.md`).
2. Calendly link, with notifications to the sales mailbox.
3. Your account is admin (set on 26 Oct with `user set-role`). Add Sales Manager accounts with
   `python -m aime user create --username <name>` if others help.
4. The off-machine backup passphrase (`docs/SALES_SETUP.md` section 7).
5. The live acceptance run (`docs/ACCEPTANCE.md`) with a test address you control.
6. Going live: your explicit decision, after the acceptance report.

## Known limitations

- **Contact coverage:** decision-makers and emails come only from what companies publish, so many companies
  show "not found", and team pages rendered by JavaScript are not read. In the first production run, 15
  companies were researched: 10 named decision-makers, 2 published a usable address (both company inboxes),
  and none published a named person's address.
- **Discovery sources:** most discovered companies come from public hiring posts, which favour tech startups.
  Some of these sell AI agents themselves, and #25 flags them. Better-fitting small businesses need other
  authorized sources (your CSV lists, or a source you approve).
- Reply classification is rule-based; anything unclear goes to review.
- Calendly is read from its notification emails (works with the free plan); a changed format goes to review.
- Wording of outreach is template-based around quoted evidence (no AI rewriting, to avoid unsupported claims).
