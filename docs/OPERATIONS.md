# Operations runbook

All commands run from the project folder with the virtual environment's Python:
`.\.venv\Scripts\python.exe -m aime <command>`.

## Daily routine (5 minutes)

1. Open the dashboard (`scripts\start_dashboard.ps1` → http://127.0.0.1:8765) and sign in. **Dashboard** is the home
   page: open alerts first (acknowledge them there or on **System health**), then the last 7 or 30 days: the big number
   of each card is that period, the small one the total.
2. **Manager Today**: **Needs you** lists what only a person can decide, the most urgent first (an interested prospect
   waiting for an answer comes before anything else), each with its reason. **Automation health** says what is really
   running, and **Failures and alerts** adds what failed since you last signed in.
3. **Approval Queue**: read, then approve or reject the emails and LinkedIn posts waiting. Nothing goes out without a
   person: B2B outreach, follow-ups and answers to replies all wait here (and on **Follow-ups**, which also shows how long
   each draft has waited). Posts also need `PUBLISHING_ENABLED=true` and a LinkedIn connection.
4. **Conversations**: read the replies marked for review.
5. **Follow-ups**: work the "Review …" tasks: qualified leads no email can go to yet.

A salesperson starts on **My work**: their leads and what happens next with each, deals, tasks, meetings, one "Do this
next" line, and anything handed over to them. **Mine** is what they own, **Team** what their team shares.

**First sign-in of a new person.** On **Users & teams** create the account (it gives a one-time link to choose a
password), or on this PC: `python -m aime user create --username <name>` for a new account and
`python -m aime user set-password --username <name>` for an existing one (for example a built-in `sales1`). They then
sign in at the dashboard's address.

## PC facts

- **The send window needs this PC awake in the evening, India time.** Emails go out 08:00 to 18:00 New York time on
  weekdays: 18:30 to 04:30 IST in US winter (17:30 to 03:30 in US summer). If the PC sleeps through it, approved emails
  simply wait; Manager Today says so. Jobs that were due run at the next check-in.
- **Mail in the Junk folder is not read.** Only the inbox is. Check Junk now and then, and move a prospect's reply out of
  it so that the next mailbox check reads it.
- **Keep the backup passphrase somewhere other than this PC.** The off-machine backup cannot be opened without it, and
  this PC is what the backup protects you from losing.

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
  and table counts) and keeps 14. Once a week it also restores the newest copy into a temporary folder and checks it.
- Manual, pre-deploy and pre-migration copies are kept for at least 30 days. After that the newest 20
  (`BACKUP_KEEP_MANUAL`; 0 keeps every one) and the newest 5 pre-deploy copies stay, and the last copy that still
  verifies is never removed.
- **Off-machine copy (optional):** set `BACKUP_OFFSITE_DIR` (e.g. `C:\Users\<you>\OneDrive\AIME-Backups`)
  and `BACKUP_PASSPHRASE` (12+ characters) in `.env`. Each day an encrypted bundle (database, publish and sent
  journals, post images, config, SHA-256 manifest; never `.env`) is written there and test-decrypted;
  the last 30 copies are kept. **Store the passphrase in your password manager: without it the copies
  cannot be read.**
- Manual: `backup --label before-change` (add `--offsite` for an encrypted copy too);
  `backup-verify <file>` checks any backup.
- An encrypted copy only counts as off-machine when the folder really leaves the PC. `doctor` checks that a
  signed-in OneDrive account syncs `BACKUP_OFFSITE_DIR` ("Off-machine folder leaves this PC"); on 27 Oct
  OneDrive was installed but not signed in, so the folder was local only.
- `doctor` warns while the off-machine copy is not configured (folder or passphrase missing), and when under 2 GB
  of disk is free. `doctor` also opens the newest verified backup each time, and names the next good copy if it has gone bad.
- **Restore drill:** `restore-drill` (latest local backup) or `restore-drill --source <file.aimebak>`
  restores into a temporary folder and checks it. It never writes to `data/`.

### Restoring a backup

**Choose the backup.** `doctor` names the newest copy that passes its check today. Take the newest one from before the
trouble began; `backup-verify <file>` checks any other. Every restore loses what happened after that copy was made.

**Run:**

1. Stop the dashboard: end the `AIMarketingEmployee-Dashboard` task in Task Scheduler, or close its window. With the
   dashboard running, its `-wal` file cannot be deleted on Windows and would damage the restored database.
2. `python -m aime restore --from <file> --confirm`. It refuses while the dashboard or a run is using the file. It
   starts a maintenance window, keeps a copy of the present database in the backups folder (`aime-pre-restore-*`, or
   `damaged-*` if it is damaged), removes the `-wal` and `-shm` files, puts the backup in place, checks it, applies newer
   schema, and holds sending. It takes a `.db`, a `.zip` bundle or an encrypted `.aimebak`.
3. Read what it prints, then your **Sent folder**, then Follow-ups.
4. `python -m aime confirm-sending`, once you have looked.
5. `python -m aime maintenance off`. The tasks start the work and the dashboard again.

**What comes back to life, and how each is handled:**

- **Approved emails.** An email sent after the backup is "approved" again in the restored database, and would go out
  twice. Each email is recorded in `data/sent_journal.jsonl` before it goes to the mail server; that file is outside
  `aime.db` and a restore leaves it alone. An email it names is not sent again: it goes to Follow-ups, where a person
  checks the Sent folder, then marks it sent or requeues it. Sending is held for everything until `confirm-sending`, also
  when a database was copied back by hand: the record knowing more than the database is how it is found.
- **Do-not-contact entries and edits** made after the backup are gone. Replies, opt-outs and bounces are read again from the
  mailbox, because the reading position went back too; anything entered by hand must be entered again.
- **Mailbox passwords.** They are encrypted for this Windows account on this PC (DPAPI). After a Windows password reset or on
  another PC they cannot be read: pages say so, one alert says so, nothing signs in, and the rest of the work goes on.
  Re-enter the password on Administration > Mailboxes. LinkedIn and other connections are connected again the same way.

**A new PC** needs: the code (`git clone`) and its `.venv`, your `.env` from the password manager, an encrypted copy
and `BACKUP_PASSPHRASE`, and the tasks (`scheduler install`, `install-watchdog`, `install-dashboard`). An encrypted copy
holds the sent record of its own day only, so what went out after it is unknown: check the Sent folder before confirming.
A backup more than 14 days old, with no check-in on this PC yet, also stops the first check-in for the clock:
`python -m aime confirm-clock`, once you have seen the clock is right.

**Damaged or missing database.** `maintenance`, `doctor`, `status`, `restore-drill`, `backup-verify` and `restore` work
without the database, and `doctor` gives the next step. With a missing `aime.db` on production while backups exist,
nothing starts and no empty database is created (to start empty on purpose, move the backups folder away). A database that
fails its integrity check holds sending the same way as a restore; after `confirm-sending` only a different result holds it again.

**A wrong clock.** A check-in stops before any work, with one alert naming the clock, when the clock is more than 14 days past the
last check-in or earlier than the newest record: on a wrong date, work changes records for good. Correct the clock and it
resumes by itself. If the clock is right (a holiday longer than two weeks), run `python -m aime confirm-clock`.

## Scheduler recovery

- Locks left by a crashed process are taken over immediately (the owner PID is checked).
- A run left "running" by a crashed or killed process is marked `abandoned` at the next tick once its job lock
  has no live owner (after 10 minutes; after two hours in any case), and counts as a failed attempt.
- A failed daily/weekly job is retried twice for the same slot (after 30, then 60 minutes), then waits for the
  next slot; failures from earlier days don't use up today's retries.
- One job whose result cannot be recorded (for example "database is locked") no longer stops the other jobs in
  the same tick.
- A job running longer than `job_budget_minutes` (config/schedule.toml, default 30) raises a warning.
- Reinstall the task at any time: `scheduler install` (per-user, no admin rights, no stored password;
  runs only while you are logged on).
- **Keep running while signed out / after a reboot:** `scheduler install --run-when-logged-off`.
  Windows asks for your Windows password in the console and stores it in Task Scheduler; the app never
  sees it. Undo with `scheduler restore-previous` (the previous definition is saved in `data/`).
- **Watchdog:** `scheduler install-watchdog` adds an hourly task that shows a desktop notification if
  ticks have stopped for 45 minutes or the dashboard is not running (log: `data/logs/watchdog.log`).
  Remove: `scheduler uninstall-watchdog`.
- **A clock wrong by less than the stop notices.** Web servers answer each request research and the website checks make with
  their time. When at least four sites are heard in a run and most of them differ from this PC by over an hour, by the same
  amount, one warning names the clock. A page that looks kept by a cache is left out. It stops nothing. It closes when two or
  more sites, and most of those heard, agree with the clock again, or after `python -m aime confirm-clock`.
- **Dashboard restart:** the dashboard is a separate process, and nothing restarts it by default (on 26–27 Oct
  it was down for about 5 hours after the window running it closed). `scheduler install-dashboard` adds a
  task that starts it at logon and every 15 minutes if it is not answering; it never starts it during a
  maintenance window, so deploys keep control. Remove: `scheduler uninstall-dashboard`.

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

Alerts about a condition (a `doctor` check, the local model, a failing or stale job) are kept as one alert
per condition with the latest details, and close themselves once the condition clears. Dismissing one keeps
it quiet for 24 hours. One-off notices (a slow job, a rejected import file) stay until you dismiss them.

## Company research and decision-makers

The `enrich` job (daily, 07:15) reads up to 15 companies' own websites, qualified leads first: the homepage
and up to five team, about, contact or services pages it links to. robots.txt is honoured for every page,
pages are capped at 300 KB, and requests are spaced out. SEC-registered companies are also checked for
officers in their insider filings (Forms 3 and 4). Each company is re-read after 30 days
(`[enrichment]` in `config/business.toml`).

- **Research snapshot** (lead page, "Website research"): the company's own description quoted with its
  URL, the services it lists, topics related to our services with the sentence they come from, the pages
  read, and the emails and phone numbers it publishes.
- **Decision-makers** (`/contacts` and the lead page): a person is recorded only when the company's team or
  about page pairs a name with a title (or its structured data does, or a sentence such as "founded by
  Jane Doe"), or an SEC filing names them. Testimonials on homepages and titles naming another
  organisation ("Founder, OtherCo") are ignored. Each person shows the page or filing and the exact text.
  Companies whose pages name nobody show "not found"; nothing is guessed.
- **Business emails**: only addresses the company publishes on its own domain. An address printed next to
  a named person is theirs (high confidence); one whose local part matches exactly one named person's
  name is theirs (medium); general inboxes (info@, hello@, sales@, contact@ ...) become a "Company inbox"
  contact. Support, jobs, privacy, no-reply, placeholder, free-mail and other-domain addresses are never
  used. Each domain is checked for a mail server (MX, implicit MX, null MX): `published` /
  `role_address` are usable; `no_mail_server` and `unchecked` (DNS could not tell) are not.
- **Fit score** (0-100, "Fit" column): seniority first (founder/C-level, VP/head, director, lead), then
  how well the role usually buys the lead's best-fit service. The lead's primary contact is a named
  person with a usable email, else the company inbox, else the best-fit named person.
- **Lead score**: the 10-point data part credits +3 for a decision-maker with a published, verified email,
  +1 for a named decision-maker without one, +1 for a published company inbox (or +3 for a person who
  contacted you). The breakdown on the lead page says which. Nobody is contacted by this job.

## B2B outreach (drafts and approvals)

Owner decision (26 Oct 2026): researched decision-makers may receive B2B emails, each approved by you.
Nothing is sent while `OUTBOUND_EMAIL_ENABLED=false`.

- **Who gets a draft** (`[outreach]` in `config/business.toml`): qualified leads of grade B or better whose
  primary contact has a business email the company publishes on its own domain (status published, or a
  company inbox), not suppressed, not a competitor, not already in a sequence. Up to 10 drafts per run.
  Leads without quotable evidence get a review task instead: no generic emails.
- **What it says**: one sentence quoting what the company published (its job post, its website on a
  topic we serve, or its own description), one sentence on what we do for that need, and a low-pressure
  question, signed by `SENDER_NAME`. The footer says why they got it, how to opt out, and the postal
  address. Steps 2 and 3 (after 4 and 10 days, `intervals_days`) are short follow-ups; each needs approval.
- **Checks** (shown on the Follow-ups page): length, subject (no "Re:"/"Fwd:" or shouting), spam words,
  unsupported claims, numbers outside quotes, quotes found verbatim in the evidence, no links, opt-out and
  postal address, a usable recipient, spelling, grammar, clichés. Held or failed drafts need "I reviewed the
  quality findings" to approve; editing re-runs the checks ("Save changes").
- **Approval** records your name; the first approval makes you the lead's owner. Rejecting the first email
  stops outreach to that lead. Rejecting or cancelling an email takes a role that approves, or a custom role with
  "Reject or cancel an email" (`reject_outbound`). Stop outreach on a person's own lead stays open to them, for when a
  prospect says stop. At send time each email is re-checked: if the contact, address, grade,
  suppression or policy changed, it is cancelled with the reason.
- **Lead outreach stage** (lead page): drafted, approved, contacted, then replied / stopped. "They replied"
  stops the sequence. The page says why the stage is what it is, where that is on record.
- **Reopening a stopped lead.** On the page of a lead whose stage is *stopped*, someone who may approve outbound email
  (a Sales Executive never sees this) types the reason and clicks **Reopen outreach**. The stage goes back to *contacted* if
  an outreach email went out, otherwise to none, with an Activity line and an audit event (`outreach_reopen`) saying who
  and why. It approves, drafts and sends nothing, and cancelled emails stay cancelled. Its message says what really follows:
  a next email only if the usual rules allow one (a rejected email stays rejected, so after a rejected first email nothing
  more is drafted), and it waits for a person's approval. Nothing automatic ever reopens a lead. It refuses, says why and
  changes nothing when: anyone at the company has opted out; an address or a domain of the company is on the do-not-contact
  list; the stage is *unsubscribed* or *bounced*; the contact's address bounced; anyone at the company asked not to be
  contacted or said they are not interested; the lead is disqualified, merged, converted or a possible competitor; or any
  email about the company (any of its leads, a merged duplicate, its domain) has not yet been read by a person on
  Conversations, including one that holds sending.
- **Stop outreach** changes the stage only from *drafted*, *approved* or *contacted*. From a page left open meanwhile, on
  any other stage, it still cancels what is queued, leaves the stage as it is, and says so; with nothing queued it says
  nothing changed.
- **Correcting a company's name** (lead page, anyone who may edit records): it is logged in Activity, and research and
  clean-ups leave a corrected name alone. Emails already waiting keep their words: edit those on Follow-ups.

## Conversations: replies, unsubscribes and bounces (sales mailbox)

Setup: `docs/SALES_SETUP.md`. The `inbox` job (every 15 minutes) reads the mailbox read-only and matches
each email to a lead by thread, then by address. **Conversations** page: category, confidence, the new text,
and what was done.

- Any human reply stops the automated sequence. Interested: the lead becomes *interested*, an
  opportunity opens (discovery), you get a notification, and a reply with your Calendly link waits in
  Follow-ups. Not interested: the lead moves to nurture. Objections, referrals, questions and anything
  unclear go to review with a suggested reply (a question's reply needs your answer before approval).
- Unsubscribe requests and complaints are suppressed at once; bounces mark the address bounced and
  suppress it. Out-of-office replies leave the sequence running.
- Interest is recognised in everyday wording and with common typos: "Sound interesting", "can we talk
  Thursday?", "can we schedule (a) meeting", "let's arrange a call", "I'd like a demo", "send me a proposal".
  A negation in front ("please don't schedule a call", "I can't meet this week") goes to review.
- When the rules can only say "question" or "other", the local AI model (Ollama, if running) gives a second
  opinion, shown in the reply's reasons ("AI … suggests: interested, asks for a meeting (please check)").
  It never acts by itself: such a reply stays in review, with a "Possible interest" alert, until you
  reclassify it. At most five replies per run get a second opinion (a few seconds each).
- Reclassify on the Conversations page applies the right actions and is logged.
- Limits on approved outreach: 20 a day, one per company domain a day, five per run (`[outreach]`).
- Test mode (`OUTBOUND_TEST_RECIPIENTS`): every email goes only to your test addresses, marked
  `[TEST for <intended recipient>]`. `aime simulate-reply --lead <id> --text "..."` processes a reply
  that is labelled SIMULATED everywhere.

## How fast replies leave

`replies_per_run` under `[followup]` in `config/business.toml` (10): approved replies to people who wrote to us leave at
most this many per follow-ups run, so that a full approval page does not all go in one minute. The rest stay approved, say
"Nothing is lost: it goes out on the next run", and go then. Notes about a booked meeting go first and are not counted. No
approval or check is skipped.

## Meetings, pipeline automation and sales reporting

- **Meetings** (`/meetings`): Calendly's "New Event" and "Canceled" emails in the sales mailbox become meeting
  records (times converted from the event's time zone; shown in US Eastern). A booking matched to a lead moves it
  to *meeting*, the opportunity to 30% with the meeting as next step, and notifies you; a cancellation moves it
  back to *interested*. Unreadable or unmatched notifications go to review. You can also log a meeting on a
  lead's page. After a meeting mark it held (opportunity 40%, "send a proposal") or no-show (a rebooking task).
- **Pipeline automation** (followups job, `[pipeline]` in `config/business.toml`): open opportunities with
  nothing logged for 10 days get a "Stalled" task; leads whose three outreach emails went unanswered for 7 days
  become *no response* and move to nurture; meetings that ended get a "Record the outcome" task. Each task is
  created once. "Stop outreach" on a lead page cancels its queued emails.
- **Re-engagement** (`[outreach] reengage_after_days`, 90; 0 turns it off): a lead parked in nurture because its
  whole sequence went unanswered gets ONE short check-in that many days after the last email, in the same thread,
  promising not to write again. It waits for your approval (campaign authorizations never approve it) and counts
  toward the sending limits. Nobody who said no, unsubscribed, bounced or is on the do-not-contact list gets one.
  Rejecting it parks the lead again; it is never drafted a second time.
- **Customer handover**: moving a deal to won converts the lead and gives the deal's owner one "Customer handover"
  task with the service, the accepted quote (or the deal's value), the customer contact, the last meeting held and
  the onboarding checklist (scope and start date, contract or invoice, delivery lead and kickoff call, access).
  Nothing is sent to the customer. Reopening and winning the deal again adds no second task.
- **Reports** (daily and weekly): companies researched, decision-makers, usable emails, outreach drafted and
  sent, bounces and unsubscribes, replies, interested replies, meetings booked and held, and reply / interest /
  meeting rates, all from records. Simulated test replies and meetings are listed separately and never counted
  in these figures.

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

## Logs

`aime.log` keeps 5 older copies of 2 MB, `agent_runs.jsonl` 5 of 10 MB, and the dashboard's own `dashboard.log` (when the
task runs it) 5 of 5 MB. A file someone has open just waits for its turn, and no older copy is lost meanwhile. On Windows `aime.log` turns over only at a moment no other process has it open, so it can run past
its size for a while.

`python -m aime scrub-logs` takes query strings out of old access lines in `dashboard.log` and its older copies (they could
hold sign-in codes and one-time links; new lines never have them). `--file <path>` names another log instead. It prints
counts, never a line, keeps every other byte, and running it twice changes nothing more. It writes a cleaned copy and swaps
it in. A dashboard of this version holds the file only for the moment of each write, so it can run meanwhile, and what it
writes during the work is kept. A dashboard started by an older version holds the file open: Windows then refuses the swap
and the command says "in use" with the log unchanged, so run it again after that dashboard has restarted.

## Keeping secrets out of GitHub

`git config core.hooksPath .githooks` enables a pre-commit hook that runs `scripts/secret_scan.py`.
It blocks `.env`, databases, logs, the `data/` folder, tokens, private keys, personal email addresses,
and local user paths. Run `python scripts/secret_scan.py --all` for a full scan.
