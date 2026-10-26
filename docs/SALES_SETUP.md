# Sales email setup (Gmail / Google Workspace + Calendly)

This guide connects the sales mailbox and the booking link. You enter every credential yourself in
`.env`; nothing here is ever shown in the dashboard, logs or reports. Sending stays off until you set
`OUTBOUND_EMAIL_ENABLED=true`, and every email still needs your approval in the dashboard.

## 1. Mailbox and app password

1. Use a Google Workspace (or Gmail) mailbox for sales, for example `sales@yourdomain.com`.
2. Turn on 2-Step Verification for that account (Google Account → Security).
3. Create an **app password** (Google Account → Security → App passwords). Copy the 16 characters.
4. In Gmail settings → *Forwarding and POP/IMAP*, make sure **IMAP is enabled**.

## 2. `.env` lines (you type the values)

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=sales@yourdomain.com
SMTP_PASSWORD=                                     # paste the app password here yourself
SMTP_FROM=sales@yourdomain.com
SENDER_NAME=<your name>
COMPANY_POSTAL_ADDRESS=<street, city, state, ZIP>   # required by CAN-SPAM
UNSUBSCRIBE_EMAIL=sales@yourdomain.com
IMAP_HOST=imap.gmail.com                           # reads replies; same app password
CALENDLY_URL=https://calendly.com/<you>/<event>
OUTBOUND_TEST_RECIPIENTS=<an address you control>  # test mode (see 4); empty for live sending
OUTBOUND_EMAIL_ENABLED=false                       # keep false until you approve sending
```

Then check: `python -m aime doctor` and the dashboard's **Integrations** page (outbound email, sales
mailbox and booking link show their state, never the secrets).

## 3. Deliverability

- In Google Admin, turn on **DKIM** for your domain and publish the TXT record it gives you.
- Your domain's **SPF** record must include Google: `v=spf1 include:_spf.google.com ~all`.
- Add a **DMARC** record, starting with monitoring: `v=DMARC1; p=none; rua=mailto:<you>@yourdomain.com`.
- Start slowly: the defaults send at most 20 outreach emails a day, one per company domain per day, five
  per run (`[outreach]` in `config/business.toml`).

## 4. Controlled test (before any live sending)

1. Set `OUTBOUND_TEST_RECIPIENTS` to an address you control and `OUTBOUND_EMAIL_ENABLED=true`.
2. Approve one outreach email in **Follow-ups**. It is delivered to your test address with
   `[TEST for <prospect>]` in the subject and a banner naming the real prospect; the prospect receives nothing.
3. Reply to it from the test mailbox (for example "Sounds interesting, can we talk Thursday?"). Within 15
   minutes the **inbox** job reads it, classifies it, stops the sequence and drafts a reply with your
   Calendly link, which you approve (it also goes only to your test address).
4. Book a slot through the link; the meetings step records it.

For an acceptance run without touching production records, use a separate data folder (see
`docs/OPERATIONS.md`). Without a mailbox, `python -m aime simulate-reply --lead <id> --text "..."`
processes a reply that is labelled SIMULATED everywhere.

## 5. Going live

Only after you approve it: clear `OUTBOUND_TEST_RECIPIENTS`, keep `OUTBOUND_EMAIL_ENABLED=true`, and keep
approving each email. To stop everything at once, set `OUTBOUND_EMAIL_ENABLED=false` (approved emails stay
queued, nothing is sent) or `[outreach] b2b_enabled = false` (no new outreach drafts).

## 6. Calendly

Create a free Calendly event (for example a 20-minute call), copy its link into `CALENDLY_URL`, and make
sure Calendly's booking notifications go to the monitored mailbox, so booked meetings are recorded.
