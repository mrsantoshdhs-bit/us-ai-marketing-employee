# Operations runbook

All commands run from the project folder with the virtual environment's Python:
`.\.venv\Scripts\python.exe -m aime <command>`.

## Daily routine (5 minutes)

1. Open the dashboard (`scripts\start_dashboard.ps1` → http://127.0.0.1:8765).
2. **Content** → approve or edit posts waiting for approval. Nothing is published without approval
   *and* an authorized LinkedIn connection with `PUBLISHING_ENABLED=true`.
3. **Follow-ups** → approve reply drafts to people who contacted you; work the "Review …" tasks for
   qualified research leads (never contacted automatically).
4. **Overview** → acknowledge alerts; anything red needs attention.

## Health checks

| Command | What it checks |
|---|---|
| `status` | Integrations, maintenance window, every job's schedule and next run |
| `doctor` | SQLite integrity, production/sample isolation, nothing published twice or without approval, consent on sent email, tokens encrypted, scheduler heartbeat |
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

## Backups and restore

- `db_maintenance` (daily 06:15) writes an online backup to `data/backups/` and keeps 14.
- Manual: `backup --label before-change`.
- Restore: `maintenance on`, copy the chosen backup over `data/aime.db` (delete `aime.db-wal` and
  `aime.db-shm` first), run `doctor`, then `maintenance off`.

## Scheduler recovery

- Locks left by a crashed process are taken over immediately (the owner PID is checked).
- Runs left "running" for over two hours are marked `abandoned` at the next tick.
- Failed daily/weekly jobs retry after 30, 60, then 120 minutes, then wait for the next slot.
- Reinstall the task at any time: `scheduler install` (per-user, no admin rights, no stored password).

## Keeping secrets out of GitHub

`git config core.hooksPath .githooks` enables a pre-commit hook that runs `scripts/secret_scan.py`.
It blocks `.env`, databases, logs, the `data/` folder, tokens, private keys, personal email addresses,
and local user paths. Run `python scripts/secret_scan.py --all` for a full scan.
