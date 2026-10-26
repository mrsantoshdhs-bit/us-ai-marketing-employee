# Operations runbook

All commands run from the project folder with the virtual environment's Python:
`.\.venv\Scripts\python.exe -m aime <command>`.

## Daily routine (5 minutes)

1. Open the dashboard (`scripts\start_dashboard.ps1` → http://127.0.0.1:8765) and sign in.
   **Today** is the home page: a health strip (scheduler, jobs, local AI model, images, LinkedIn,
   publishing, email, backups), everything waiting for you (posts with their quality-check result,
   reply drafts, outcomes that need checking), open alerts, the pipeline funnel and companies to review.
2. **Content** → approve or edit posts waiting for approval. Nothing is published without approval
   *and* an authorized LinkedIn connection with `PUBLISHING_ENABLED=true`.
3. **Follow-ups** → approve reply drafts to people who contacted you; work the "Review …" tasks for
   qualified research leads (never contacted automatically).
4. **Today** → acknowledge alerts; anything red needs attention. Job runs and agent performance
   are on **System**.

## Health checks

| Command | What it checks |
|---|---|
| `status` | Integrations, maintenance window, every job's schedule and next run |
| `doctor` | SQLite integrity, production/sample isolation, nothing published twice or without approval, no email sent without approval, consent on sent email, tokens encrypted, scheduler heartbeat |
| `scheduler verify` | Windows task installed and enabled, last result 0, fresh heartbeat, recent scheduler-triggered runs |

The `monitor` job runs `doctor` every 30 minutes and raises alerts for failures.

## Uncertain publications or emails

If LinkedIn or the mail server times out mid-request, the item becomes **uncertain**. It is never
retried automatically, because it may already have gone out.

- **Post:** open it under Content. Check your LinkedIn activity. Then either paste the post URL and
  click *Confirm published*, or tick *I checked LinkedIn and this post is not there* and *Requeue*.
  With the `r_member_social` permission the system reconciles this itself.
- **Email:** check your Sent folder, then *Mark sent* or *Requeue* under Follow-ups.

Every API call is recorded in `publish_attempts`, and confirmed posts are journaled in
`data/publish_journal.jsonl` so a crash between LinkedIn's confirmation and the database update
is repaired automatically on the next run.

## Upgrades and maintenance

```powershell
.\.venv\Scripts\python.exe -m aime maintenance on --hours 2 --reason "upgrade"
# ...change code or config, run tests...
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m aime maintenance off
```

A maintenance window always expires on its own, so automation can never stay paused by accident.
Schema changes are applied automatically on the next start (additive only, with a backup first).

During a window, scheduled ticks, dashboard "Run now" buttons and `aime run <job>` are all refused.
To verify a deployment before ending the window, run a job explicitly with
`aime run <job> --during-maintenance`.

A post that missed its slot by more than `max_overdue_hours` is moved to the next free slot (with an
alert). If no slot is free it is never published late: it returns to "awaiting approval" for a new time.

## Backups and restore

- `db_maintenance` (daily 06:15) writes an online backup to `data/backups/`, **verifies it** (integrity
  and table counts) and keeps 14. Pre-deploy and manual backups are never pruned.
- **Off-machine copy (optional):** set `BACKUP_OFFSITE_DIR` (e.g. `C:\Users\<you>\OneDrive\AIME-Backups`)
  and `BACKUP_PASSPHRASE` (12+ characters) in `.env`. Each day an encrypted bundle (database, publish
  journal, post images, config, SHA-256 manifest; never `.env`) is written there and test-decrypted;
  the last 30 copies are kept. **Store the passphrase in your password manager: without it the copies
  cannot be read.**
- Manual: `backup --label before-change` (add `--offsite` for an encrypted copy too);
  `backup-verify <file>` checks any backup.
- **Restore drill:** `restore-drill` (latest local backup) or `restore-drill --source <file.aimebak>`
  restores into a temporary folder and checks it. It never writes to `data/`.
- Restore: `maintenance on`, copy the chosen backup over `data/aime.db` (delete `aime.db-wal` and
  `aime.db-shm` first), run `doctor`, then `maintenance off`. For an encrypted copy, first run
  `restore-drill --source <file.aimebak> --extract-to C:\restore-check` (a new folder outside `data/`),
  then restore the checked `aime.db`, `publish_journal.jsonl` and `assets/` from that folder.

## Scheduler recovery

- Locks left by a crashed process are taken over immediately (the owner PID is checked).
- Runs left "running" for over two hours are marked `abandoned` at the next tick (and by manual runs).
- Failed daily/weekly jobs retry after 30, 60, then 120 minutes, then wait for the next slot.
- A job running longer than `job_budget_minutes` (config/schedule.toml, default 30) raises a warning.
- Reinstall the task at any time: `scheduler install` (per-user, no admin rights, no stored password;
  runs only while you are logged on).
- **Keep running while signed out / after a reboot:** `scheduler install --run-when-logged-off`.
  Windows asks for your Windows password in the console and stores it in Task Scheduler; the app never
  sees it. Undo with `scheduler restore-previous` (the previous definition is saved in `data/`).
- **Watchdog:** `scheduler install-watchdog` adds an hourly task that shows a desktop notification if
  ticks have stopped for 45 minutes (log: `data/logs/watchdog.log`). Remove: `scheduler uninstall-watchdog`.

## Local AI model (Ollama)

- The `monitor` job checks the model every 30 minutes; two failed checks in a row raise a warning
  ("Local AI model unreachable since …"). Posts then use the template writer.
- Windows: make sure **Ollama is enabled in Task Manager → Startup apps** so it starts when you log in.
- Optional `OLLAMA_AUTOSTART=true` lets the app start `ollama serve` itself when the local server is
  down (local URL only; it never stops or restarts a running server).
- The Integrations page re-checks the model each minute instead of showing the state from start-up.

## Desktop notifications

New warning and error alerts also appear as a Windows notification (`ALERT_TOAST=true`, the default).
Alerts are always stored and shown on the dashboard; notifications are an extra nudge.

## Post images

- Images are 1080×1350 (4:5, the largest feed format), designed from HTML/CSS templates in
  `aime/design/templates/` (architecture, editorial, infographic, concept, corporate) with the colours
  and fonts in `config/brand.toml`, and rendered by the Edge or Chrome already on this computer.
- Every word on an image comes from the post (title, first sentence, bullets), so image and caption
  always agree. Each design is measured before it is kept: no text overflow or overlaps, text at least
  26 px (readable on phones), contrast at least 4.5:1, no large empty bands, brand name present, no
  decorative line crossing text, and no blank or unpainted patch in the screenshot (a solid pure white
  or black block the design does not paint; the screenshot is retaken once before the style fails). If a
  design fails, the next style is tried; if none passes or no browser is found, the simple card is used.
- Re-render unpublished posts: `refresh-graphics` (`--renderer pillow` to go back to the simple card).

## Automated quality checks

Every new post (and every edit you save) is checked before it can be scheduled:

| Check | Fails (draft replaced) | Held for you (kept, flagged) |
|---|---|---|
| Content rules | statistics, $ amounts, guarantees, client claims, superlatives, missing question, length | — |
| Factual claims | — | any number, or a named company (needs a source you verify) |
| Spelling / grammar | likely misspellings (offline dictionary + business vocabulary), repeated words, "a" before a vowel, unbalanced brackets | spell checker unavailable |
| Clichés | filler such as "in today's fast-paced", "leverage", "game-changer" | — |
| Brand | brand name written differently, hashtags not on the approved list, too many hashtags | — |
| LinkedIn rules | over 3,000 characters, engagement bait, @-mentions, shortened links | any other link |
| Duplicates | same title, ≥ 45% text overlap or a near-identical image in the last 120 days | — |
| Image | wrong size, over 5 MB | missing image |
| Image matches caption | image headline/points not taken from the post | — |

The content agent tries up to three drafts per slot; if none passes, the slot stays empty and you get
an alert. Held posts show "held for review" and can only be approved after ticking "I reviewed the
quality findings" (logged). Every post still needs your approval before it is published.

## Auto-publish policy (shadow mode only)

After every content and publish job the policy records what it **would** do with each post waiting for
you or approved by you: *would publish* when every rule passes, otherwise *would hold* with the reasons.
It never approves, schedules or publishes, and never calls LinkedIn. Rules (`[publish_policy]` in
`config/business.toml`): all quality checks passed on the current text, designed image, allowed
service, no sensitive terms (pricing, guarantees, client stories…), draft not older than 14 days, slot
not passed, at most 3 posts in any 7 days and 20 h apart.

**Content → Auto-publish policy** (`/policy`) compares its decisions with yours: *agreed*, *too
permissive* (it would have published a post you rejected) and *too strict*. CLI: `policy` to show,
`policy evaluate` to check now. `doctor` warns if the mode is set to anything other than `shadow`
(other modes are not supported and are treated as shadow).

## Keeping secrets out of GitHub

`git config core.hooksPath .githooks` enables a pre-commit hook that runs `scripts/secret_scan.py`.
It blocks `.env`, databases, logs, the `data/` folder, tokens, private keys, personal email addresses,
and local user paths. Run `python scripts/secret_scan.py --all` for a full scan.
