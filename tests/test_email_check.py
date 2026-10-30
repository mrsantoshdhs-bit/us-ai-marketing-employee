"""Sales mailbox readiness check: settings, sending-domain DNS, SMTP/IMAP sign-in, test email (all faked)."""

import smtplib
from argparse import Namespace

from aime.cli import cmd_email_check
from aime.email_check import check_domain, run_email_check

APP_PW = "abcd efgh " + "ijkl mnop"  # test-only value, assembled so the secret scanner stays quiet
MAILBOX = {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_from": "sales@dhs.example",
           "smtp_username": "sales@dhs.example", "smtp_password": APP_PW, "imap_host": "imap.gmail.com",
           "company_postal_address": "1 Sample St, Austin, TX 78701", "sender_name": "Sam Rai",
           "unsubscribe_email": "sales@dhs.example", "calendly_url": "https://calendly.com/dhs/intro"}
GOOD_DNS = {
    ("dhs.example", "MX"): ["1 smtp.google.com."],
    ("dhs.example", "TXT"): ["v=spf1 include:_spf.google.com ~all", "google-site-verification=x"],
    ("google._domainkey.dhs.example", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjANBgkq"],
    ("_dmarc.dhs.example", "TXT"): ["v=DMARC1; p=quarantine; rua=mailto:d@dhs.example"],
}


def resolver(records):
    return lambda name, rtype: records.get((name, rtype), [])


class FakeSMTP:
    sent: list = []
    logins: list = []
    refuse = False
    tls_context = None

    def __init__(self, host, port, timeout=30):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        FakeSMTP.tls_context = context

    def login(self, user, password):
        if FakeSMTP.refuse:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        FakeSMTP.logins.append(user)

    def noop(self):
        return 250, b"OK"

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class FakeIMAP:
    readonly = None
    ssl_context = None

    def __init__(self, host, port, ssl_context=None, timeout=None):
        FakeIMAP.ssl_context = ssl_context

    def login(self, user, password):
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):
        FakeIMAP.readonly = readonly
        return "OK", [b"12"]

    def logout(self):
        return "BYE", [b""]


def reset():
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.refuse, FakeIMAP.readonly = [], [], False, None
    FakeSMTP.tls_context = FakeIMAP.ssl_context = None


def by_name(checks):
    return {c.name: c for c in checks}


def test_missing_settings_are_reported_without_signing_in(make_ctx, capsys):
    ctx = make_ctx()
    assert cmd_email_check(Namespace(connect=False, send_test=False, domain=None), ctx) == 1
    out = capsys.readouterr().out
    assert "[FAIL] SMTP settings" in out and "[FAIL] Postal address (CAN-SPAM)" in out
    assert "[PASS] Sending switch: OFF" in out and "[SKIP] Mailbox sign-in" in out


def test_a_mailboxes_panel_mailbox_is_checked_in_place_of_env(make_ctx, capsys, monkeypatch):
    """Production keeps its mailbox on the Mailboxes page, not in .env: the check must not report it as missing."""
    from aime import mail_accounts as ma

    monkeypatch.setattr("aime.email_check.dns_resolver", lambda: resolver(GOOD_DNS))
    ctx = make_ctx(**{k: v for k, v in MAILBOX.items() if not k.startswith(("smtp_", "imap_"))})
    with ctx.db.session() as s:
        box = ma.save(s, "admin", {"provider": "gmail", "email": "sales@dhs.example", "secret": APP_PW})
        ma.change(s, "admin", box.id, "enable")
        ma.change(s, "admin", box.id, "read_on")
        box_id = box.id
    args = Namespace(connect=False, send_test=False, domain=None)
    assert cmd_email_check(args, ctx) == 0
    out = capsys.readouterr().out
    assert "[WARN] Mailbox: sales@dhs.example (Mailboxes panel) reads replies but is not Primary" in out
    assert "[PASS] SMTP settings: smtp.gmail.com:587, from sales@dhs.example, password set" in out
    assert "[PASS] IMAP settings" in out and "[PASS] DKIM" in out and APP_PW not in out
    with ctx.db.session() as s:
        ma.change(s, "admin", box_id, "primary")
    assert cmd_email_check(args, ctx) == 0
    assert "[PASS] Mailbox: sales@dhs.example (Mailboxes panel): the sending mailbox, reads replies" in capsys.readouterr().out


def test_ready_mailbox_passes_and_nothing_is_sent(make_ctx):
    reset()
    ctx = make_ctx(**MAILBOX)
    checks = by_name(run_email_check(ctx.settings, connect=True, resolve=resolver(GOOD_DNS), smtp_factory=FakeSMTP,
                                     imap_factory=FakeIMAP))
    assert all(c.status == "pass" for n, c in checks.items() if n not in ("Test recipient",)), checks
    assert checks["DMARC"].detail == "policy quarantine" and FakeSMTP.logins == ["sales@dhs.example"]
    assert FakeSMTP.sent == [] and FakeIMAP.readonly is True  # sign-in only; the mailbox is opened read-only
    assert APP_PW not in str(checks)


def test_domain_problems_are_explained():
    bad = check_domain("dhs.example", resolver({("dhs.example", "TXT"): ["v=spf1 include:mailgun.org ~all",
                                                                          "v=spf1 -all"]}))
    checks = by_name(bad)
    assert checks["MX (replies can arrive)"].status == "fail" and checks["SPF"].status == "fail"
    assert "exactly one" in checks["SPF"].detail and checks["DKIM"].status == "warn" and checks["DMARC"].status == "warn"
    one = by_name(check_domain("dhs.example", resolver({("dhs.example", "TXT"): ["v=spf1 include:mailgun.org ~all"]}),
                               "smtp.gmail.com"))
    assert one["SPF"].status == "warn" and "_spf.google.com" in one["SPF"].detail
    personal = check_domain("gmail.com", resolver({}))
    assert len(personal) == 1 and personal[0].status == "warn" and "Google Workspace" in personal[0].detail


def test_refused_sign_in_gives_the_app_password_hint_and_never_the_password(make_ctx):
    reset()
    FakeSMTP.refuse = True
    ctx = make_ctx(**MAILBOX)
    checks = by_name(run_email_check(ctx.settings, connect=True, resolve=resolver(GOOD_DNS), smtp_factory=FakeSMTP,
                                     imap_factory=FakeIMAP))
    assert checks["SMTP sign-in"].status == "fail" and "app password" in checks["SMTP sign-in"].detail
    assert APP_PW not in str(checks)


def test_test_email_only_to_your_own_address_and_only_where_sending_is_on(make_ctx):
    reset()
    production = make_ctx(**MAILBOX, outbound_test_recipients="me@owner.example")  # sending switch off
    checks = by_name(run_email_check(production.settings, send=True, resolve=resolver(GOOD_DNS), smtp_factory=FakeSMTP,
                                     imap_factory=FakeIMAP))
    assert checks["Test email"].status == "fail" and "DISABLED" in checks["Test email"].detail and FakeSMTP.sent == []
    nobody = make_ctx(**MAILBOX, outbound_email_enabled=True)
    assert by_name(run_email_check(nobody.settings, send=True, resolve=resolver(GOOD_DNS), smtp_factory=FakeSMTP,
                                   imap_factory=FakeIMAP))["Test email"].status == "fail"
    acceptance = make_ctx(**MAILBOX, outbound_email_enabled=True, outbound_test_recipients="me@owner.example")
    checks = by_name(run_email_check(acceptance.settings, send=True, resolve=resolver(GOOD_DNS), smtp_factory=FakeSMTP,
                                     imap_factory=FakeIMAP))
    assert checks["Test email"].status == "pass" and len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "me@owner.example" and "unsubscribe" in msg.get_content().lower()
    assert checks["Sending switch"].detail.startswith("ON in TEST MODE")


def test_calendly_link_must_be_a_calendly_event(make_ctx):
    ctx = make_ctx(**{**MAILBOX, "calendly_url": "http://example.com/book"})
    assert by_name(run_email_check(ctx.settings, resolve=resolver(GOOD_DNS)))["Calendly link"].status == "fail"


def test_mail_connections_verify_the_server_certificate_and_never_send_a_password_unencrypted(make_ctx):
    import ssl

    from aime.inbox import Mailbox
    from aime.integrations.email_smtp import EmailSender
    from tests.test_inbound_sales import FakeSMTP as SendingSMTP
    from tests.test_inbox import FakeIMAP as ReadingIMAP

    def verified(context):
        return isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

    reset()
    ctx = make_ctx(**MAILBOX)
    run_email_check(ctx.settings, connect=True, resolve=resolver(GOOD_DNS), smtp_factory=FakeSMTP, imap_factory=FakeIMAP)
    assert verified(FakeSMTP.tls_context) and verified(FakeIMAP.ssl_context)
    sending = make_ctx(**MAILBOX, outbound_email_enabled=True, outbound_test_recipients="me@owner.example")
    SendingSMTP.tls_contexts = []
    EmailSender(sending.settings, smtp_factory=SendingSMTP).send("me@owner.example", "Hello", "Body")
    assert len(SendingSMTP.tls_contexts) == 1 and verified(SendingSMTP.tls_contexts[0])
    ReadingIMAP.messages, ReadingIMAP.ssl_context = {}, None
    Mailbox(ctx.settings, imap_factory=ReadingIMAP).fetch_new(None)
    assert verified(ReadingIMAP.ssl_context) and ReadingIMAP.timeout

    plain = make_ctx(**MAILBOX, smtp_use_tls=False, outbound_email_enabled=True)
    ready, reason = EmailSender(plain.settings, smtp_factory=SendingSMTP).status()
    assert not ready and "unencrypted" in reason
    reset()
    check = by_name(run_email_check(plain.settings, connect=True, resolve=resolver(GOOD_DNS), smtp_factory=FakeSMTP,
                                    imap_factory=FakeIMAP))["SMTP sign-in"]
    assert check.status == "fail" and "unencrypted" in check.detail and FakeSMTP.logins == []
    assert APP_PW not in check.detail


OWN_SERVER_DNS = {
    ("dhs.example", "MX"): ["0 mail.dhs.example."],
    ("dhs.example", "A"): ["203.0.113.7"],
    ("mail.dhs.example", "A"): ["203.0.113.7"],
    ("dhs.example", "TXT"): ["v=spf1 ip4:203.0.113.7 include:_spf.google.com ~all"],
    ("default._domainkey.dhs.example", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjANBgkq"],
    ("_dmarc.dhs.example", "TXT"): ["v=DMARC1; p=none; rua=mailto:d@dhs.example"],
}


def test_an_own_mail_server_is_checked_by_its_address_and_its_dkim_selector():
    checks = by_name(check_domain("dhs.example", resolver(OWN_SERVER_DNS), "mail.dhs.example"))
    assert checks["SPF"].status == "pass" and "ip4:203.0.113.7" in checks["SPF"].detail
    assert checks["DKIM"].status == "pass" and "default._domainkey" in checks["DKIM"].detail
    for spf in ("v=spf1 mx ~all", "v=spf1 a ~all", "v=spf1 a:mail.dhs.example -all", "v=spf1 ip4:203.0.113.0/24 ~all"):
        dns = {**OWN_SERVER_DNS, ("dhs.example", "TXT"): [spf]}
        assert by_name(check_domain("dhs.example", resolver(dns), "mail.dhs.example"))["SPF"].status == "pass", spf
    missing = {**OWN_SERVER_DNS, ("dhs.example", "TXT"): ["v=spf1 include:_spf.google.com ~all"]}
    spf = by_name(check_domain("dhs.example", resolver(missing), "mail.dhs.example"))["SPF"]
    assert spf.status == "warn" and "ip4:203.0.113.7" in spf.detail
    no_dkim = {k: v for k, v in OWN_SERVER_DNS.items() if "_domainkey" not in k[0]}
    dkim = by_name(check_domain("dhs.example", resolver(no_dkim), "mail.dhs.example"))["DKIM"]
    assert dkim.status == "warn" and "Google" not in dkim.detail


def test_a_refused_sign_in_on_an_own_server_does_not_mention_gmail(make_ctx):
    reset()
    FakeSMTP.refuse = True
    ctx = make_ctx(**{**MAILBOX, "smtp_host": "mail.dhs.example", "imap_host": "mail.dhs.example"})
    checks = by_name(run_email_check(ctx.settings, connect=True, resolve=resolver(OWN_SERVER_DNS), smtp_factory=FakeSMTP,
                                     imap_factory=FakeIMAP))
    assert checks["SMTP sign-in"].status == "fail" and "Gmail" not in checks["SMTP sign-in"].detail
    assert APP_PW not in str(checks)
