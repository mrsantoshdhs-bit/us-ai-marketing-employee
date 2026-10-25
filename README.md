# Autonomous US AI Marketing Employee

A local, $0-per-month team of eight AI agents that researches US businesses from authorized public
sources, scores opportunities on evidence, drafts LinkedIn content with a local LLM, manages a
lightweight CRM, drafts compliant replies to inbound inquiries, and reports daily. It runs on a
Windows Task Scheduler task and has a local dashboard.

**Human in the loop by design.** Nothing is published or sent without your approval *and* an
authorized integration. Research leads are never contacted automatically.

## Quick start

```powershell
cd $HOME\Documents\us-ai-marketing-employee
.\.venv\Scripts\python.exe -m aime status              # integrations, jobs, counts
.\scripts\start_dashboard.ps1                           # http://127.0.0.1:8765
.\.venv\Scripts\python.exe -m pytest                    # test suite
```

Set your brand in `config/business.toml` (`[company] name = ...`). Integrations are enabled in `.env`
(created from `.env.example`).

## Architecture

```
Windows Task Scheduler (every 15 min, per-user)
  └─ scripts/run_tick.py → aime tick → runs due jobs (config/schedule.toml)
       ├─ crm_maintenance  06:30  CRM agent (dedupe, hygiene, stale, lifecycle) + re-score
       ├─ plan             06:45  Marketing Manager (50-state rotation, industry focus, campaign, briefs)
       ├─ research         07:00  Research agent → Qualification → Sales Workflow
       ├─ content          07:30  LinkedIn Content agent (topics, LLM drafts, images, calendar)
       ├─ inbound          30 min CRM intake → Qualification → Sales Workflow
       ├─ followups        60 min Sales Workflow (drafts, tasks, approved sends)
       ├─ publish          30 min Authorized Publishing (official LinkedIn API only)
       ├─ daily_report     20:00  Analytics & Reporting (Markdown + HTML)
       └─ monitor          30 min Failure / staleness / error-rate alerts
FastAPI dashboard (127.0.0.1) ── SQLite (data/aime.db, WAL) ── Ollama llama3.2 (local, optional)
```

| Path | Purpose |
|---|---|
| `aime/agents/` | The eight agents (typed input/output contracts, retries, run logs) |
| `aime/sources/` | HN "Who is hiring" API, SEC EDGAR, Census CBP, CSV import, topic trends |
| `aime/integrations/` | LinkedIn OAuth + Posts/Images API, SMTP, status checks |
| `aime/scheduler/` | Due-job logic, locks, Windows Task Scheduler installer |
| `aime/web/` | Dashboard and JSON API |
| `config/` | Business targeting, content cadence, scoring, follow-up policy, schedule |
| `data/` | Database, logs, reports, images, inbox (git-ignored) |
| `deploy/`, `docs/DEPLOYMENT.md` | systemd, Docker Compose, reverse-proxy guidance |

## The agent team

| Agent | Responsibilities | Retry policy |
|---|---|---|
| Marketing Manager | 50-state rotation, industry focus (explore + exploit), monthly campaign, content briefs | 3 attempts, exponential backoff |
| US Business Research | Authorized sources only, US-location evidence required, requirement extraction, dedupe, no personal data | 3 attempts on network/DB errors |
| LinkedIn Content | Topic research, local-LLM drafts behind a quality gate, template fallback, hashtags, 1200×627 images, calendar | 3 attempts |
| Authorized Publishing | Approved + due posts only, official API, published only on 201 + post id, no bursts | 1 attempt per run (non-idempotent), per-post 3 tries |
| Lead Qualification | 0–100 score: fit 35, need 35, intent 20 (evidence only), data 10, with reasons | 3 attempts |
| Sales Workflow | Opportunities for inbound, approval-gated reply sequences, tasks for research leads, send window | 1 attempt (non-idempotent) |
| CRM Management | Inbound intake + spam checks, suppression, merges without deletes, hygiene, lifecycle | 3 attempts |
| Analytics & Reporting | KPIs from records only, blockers, agent health, imported LinkedIn analytics | 3 attempts |

Every run is logged to `agent_runs` (dashboard → Agents) and `data/logs/agent_runs.jsonl`;
job runs to `job_runs`; health metrics (success rate, avg/p95 latency, retries) are on the Overview.

## Lead sources (authorized, $0)

- **Hacker News "Who is hiring?"** via the official public Algolia API: companies describe their
  own needs; only company-level facts and a link are kept. Emails/phones are stripped.
- **SEC EDGAR** (official filings): needs `SEC_USER_AGENT` with your org name and contact email.
- **US Census County Business Patterns**: market sizing by state; needs a free `CENSUS_API_KEY`.
- **Your CSV lists** in `data/imports/` (exports you are entitled to use).
- **Inbound inquiries**: dashboard form, `POST /api/inbound`, or files in `data/inbox/`.

## Safety and compliance

- No scraping, browser bots, cookie reuse, CAPTCHA bypass, connection requests, DMs, or fake engagement.
- LinkedIn: OAuth with your consent; `PUBLISHING_ENABLED` master switch; per-post approval.
- Email: only to people who contacted you or opted in; per-message approval; business-hours window;
  unsubscribe line, `List-Unsubscribe` header, postal address (CAN-SPAM); suppression list honored.
- No fabricated data: intent is scored only from evidence, opportunity values only from people,
  sample data uses reserved `.example` domains and is flagged.
- Dashboard binds to 127.0.0.1 and rejects cross-site form posts.

## Enabling integrations

| Capability | What you do | Cost |
|---|---|---|
| LinkedIn publishing | Create a LinkedIn developer app, add *Sign In with LinkedIn (OIDC)* + *Share on LinkedIn*, set `LINKEDIN_CLIENT_ID/SECRET`, click **Connect LinkedIn**, then `PUBLISHING_ENABLED=true` | Free |
| Company-page posting | Apply for Community Management API; set `LINKEDIN_AUTHOR_URN=urn:li:organization:<id>` | Free (approval required) |
| SEC EDGAR research | `SEC_USER_AGENT="Your Org you@yourdomain.com"` | Free |
| Census market sizing | Free key → `CENSUS_API_KEY` | Free |
| Reply emails | `OUTBOUND_EMAIL_ENABLED=true`, SMTP settings, `COMPANY_POSTAL_ADDRESS` | Your existing mailbox |
| LinkedIn analytics | Export post analytics CSV → `data/imports/linkedin_metrics/` | Free |

## Commands

```
python -m aime status | init | tick | serve
python -m aime run <plan|research|content|inbound|followups|publish|crm_maintenance|daily_report|monitor>
python -m aime scheduler install|uninstall|status|run
python -m aime llm-check --service ai_agents --industry real_estate
python -m aime inbound inquiries.json
python -m aime suppress someone@domain.com
python -m aime seed-demo --data-dir demo_data      # separate demo DB from fictional data
```

See `docs/DEPLOYMENT.md` for always-on hosting.
