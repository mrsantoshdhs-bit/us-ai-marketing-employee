# Production deployment

The system is built to run for $0 on this Windows PC. Use this guide when you want it to run
when the PC is off, or to receive website inquiries directly.

## Option A: this PC (current setup, $0)

- Windows Task Scheduler task `AIMarketingEmployee-Tick` runs `scripts/run_tick.py` every 15 minutes
  (and 2 minutes after logon) with `pythonw.exe`, so no console window appears.
- It runs only while you are logged in (no stored password, no admin rights). If the PC sleeps or is off,
  the next tick catches up missed daily jobs once (no bursts).
- Manage it: `python -m aime scheduler status | install | uninstall | run`.
- Dashboard: `scripts/start_dashboard.ps1` → http://127.0.0.1:8765 (localhost only).

Limits: nothing runs while the PC is off; inbound web forms cannot reach a localhost dashboard.
Use the inbox folder (`data/inbox/*.json|*.csv`) or the dashboard form to log inquiries.

## Option B: always-on Linux host (small VPS or a home server)

Cost depends on the host you choose (a free-tier VM can work; the local LLM needs about 4 GB RAM,
otherwise set `LLM_PROVIDER=template`).

```bash
sudo useradd --system --create-home aime
sudo git clone <your repo> /opt/us-ai-marketing-employee   # or copy the folder
cd /opt/us-ai-marketing-employee
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # fill in what you enable
.venv/bin/python -m aime init
sudo cp deploy/systemd/aime-*.service deploy/systemd/aime-tick.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aime-tick.timer aime-web.service
systemctl list-timers aime-tick.timer     # verify the timer is scheduled
journalctl -u aime-tick.service -n 50     # verify ticks ran
```

## Option C: Docker Compose

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml exec ollama ollama pull llama3.2
```

The compose file publishes the dashboard on `127.0.0.1:8765` only.

## Exposing the dashboard or inbound API (only if you need to)

The dashboard requires a Sales Manager sign-in (`python -m aime user create --username <name>`; you type
the password at the prompt). It has no TLS, multi-factor sign-in or account recovery by email, so still
never publish it directly. If you need remote access or a public `POST /api/inbound` endpoint for your
website form:

1. Put an authenticating reverse proxy in front (for example Caddy with `basic_auth`, or Cloudflare Access).
2. Expose only `/api/inbound` publicly if possible, and set `INBOUND_API_TOKEN` so callers must send
   `X-Inbound-Token`.
3. Update `LINKEDIN_REDIRECT_URI` to the HTTPS URL and add it to the LinkedIn app.

Example Caddyfile:

```
crm.example.com {
    @inbound path /api/inbound
    handle @inbound {
        reverse_proxy 127.0.0.1:8765
    }
    handle {
        basic_auth {
            admin <bcrypt-hash from `caddy hash-password`>
        }
        reverse_proxy 127.0.0.1:8765
    }
}
```

## Pre-flight checklist

- [ ] `python -m pytest` passes and `python -m ruff check aime tests scripts` is clean
- [ ] `python -m aime doctor` reports no FAIL lines
- [ ] `git config core.hooksPath .githooks` is set (secret scanning on every commit)
- [ ] `.env` contains only what you intend to enable; it is not committed (see `.gitignore`)
- [ ] `config/business.toml` has your real company name and website
- [ ] `COMPANY_POSTAL_ADDRESS` is set before enabling outbound email (CAN-SPAM)
- [ ] LinkedIn app has the correct redirect URL; `LINKEDIN_API_VERSION` is a currently supported month
- [ ] `PUBLISHING_ENABLED=true` only after reviewing the approval queue
- [ ] Back up `data/aime.db` (copy the file while no tick is running, or use `sqlite3 data/aime.db ".backup backup.db"`)

## Rollback

- Stop automation: `python -m aime scheduler uninstall` (or `systemctl disable --now aime-tick.timer`).
- Stop publishing immediately: set `PUBLISHING_ENABLED=false` (checked on every run).
- Stop email immediately: set `OUTBOUND_EMAIL_ENABLED=false`.
- Restore data: replace `data/aime.db` with a backup.
