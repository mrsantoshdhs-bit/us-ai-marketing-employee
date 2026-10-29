# Remote access for the sales team (procedure, not enabled)

The dashboard listens only on this PC (`127.0.0.1:8765`) and refuses any other address (`aime serve`). A second
salesperson working elsewhere therefore cannot open it. This page is the plan for letting them in **without putting
the dashboard on the public internet**. Nothing here is switched on: each step changes infrastructure or an
external account and needs the owner's approval.

## Recommended: a private network between the PCs

1. **Private network (no open ports).** Install a WireGuard-based private network client (for example Tailscale's
   free personal plan, or plain WireGuard) on this PC and on the salesperson's computer, signed in to an account
   the owner controls, with device approval on. The two computers can then reach each other; nobody else can.
2. **A local HTTPS proxy on this PC** that listens only on the private-network address and forwards to
   `127.0.0.1:8765`. Use a certificate for the private name (the private network can issue one). The dashboard
   keeps listening on `127.0.0.1` only.
3. **Settings**:
   - add the private host name to `ALLOWED_HOSTS` in `.env` (the dashboard refuses unknown Host headers);
   - keep `DASHBOARD_AUTH=true`.
   - Session cookies are marked Secure automatically over HTTPS.
4. **Accounts**: create the salesperson's account on Administration › Users with a one-time setup link. Give it the
   least role that fits (Sales Executive or Sales Manager); records outside their team stay invisible to them.
5. **Check**:
   - from the salesperson's PC, the dashboard opens over HTTPS on the private name only;
   - from any other network it does not answer;
   - signing out ends the session;
   - the Audit log shows their sign-in.

## Not recommended

- Binding the dashboard to `0.0.0.0` or forwarding a router port. The dashboard is built as a local tool, and
  exposing it would put the sign-in page on the internet.
- Sharing one account between people. Every approval records who approved it.

## Always on (24/7)

The scheduler already runs without anyone signed in. It starts at boot, wakes the PC, catches up missed runs, runs
on battery, and sleep is off. The dashboard runs only while someone is signed in to Windows.

For round-the-clock dashboard access, either:

- keep this PC on and signed in, ideally on a UPS; or
- move AIME to a small always-on Windows machine or VPS. That is a purchase decision. A Linux server would also need
  replacements for DPAPI secret storage and Task Scheduler.

Backups are verified daily. Off-machine copies need OneDrive signed in on this PC: Doctor warns until it is.
