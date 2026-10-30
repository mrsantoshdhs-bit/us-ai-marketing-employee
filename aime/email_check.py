"""Sales mailbox readiness, for `python -m aime email-check`.

Checks the settings, the sending domain's SPF, DKIM, DMARC and MX records, SMTP sign-in (no email is sent) and
read-only IMAP (EXAMINE: nothing in the mailbox changes). With `--send-test`, one email goes to your own
test address only, and only where sending is enabled (the acceptance settings). Passwords are never printed.
"""

from __future__ import annotations

import imaplib
import ipaddress
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from aime.integrations.email_smtp import MAIL_TIMEOUT, EmailNotReady, EmailSender, compliance_footer, tls_context
from aime.redact import redact

GOOGLE_SPF = "include:_spf.google.com"
PERSONAL_GMAIL = {"gmail.com", "googlemail.com"}
# The SPF include a hosted mail provider needs, by SMTP host. Any other server (your own, cPanel, a VPS) must be
# authorized by its address: ip4:, a, a:<host> or mx.
PROVIDER_SPF = {"gmail": GOOGLE_SPF, "google": GOOGLE_SPF, "office365": "include:spf.protection.outlook.com",
                "outlook": "include:spf.protection.outlook.com"}
# Where providers publish DKIM keys: Google, Microsoft 365, cPanel/Exim ("default"), and other common selectors.
DKIM_SELECTORS = ("google", "selector1", "selector2", "default", "dkim", "mail", "k1", "s1", "s2", "zmail")
OAUTH_SIGN_IN = "signs in with OAuth: use Test on the Mailboxes page (or the Integrations page) to sign in"


def is_gmail(host: str | None) -> bool:
    return any(k in (host or "").lower() for k in ("gmail", "google"))


@dataclass
class Check:
    name: str
    status: str  # pass | warn | fail | skip
    detail: str


def panel_mailbox_check(a) -> Check:
    """The Mailboxes-page mailbox the other checks use in place of the .env mail settings."""
    if a.is_primary:
        return Check("Mailbox", "pass", f"{a.email} (Mailboxes panel): the sending mailbox"
                                        + (", reads replies" if a.read_replies else ""))
    return Check("Mailbox", "warn", f"{a.email} (Mailboxes panel) reads replies but is not Primary: nothing can be sent "
                                    "until a mailbox is made Primary on the Mailboxes page")


def check_settings(s) -> list[Check]:
    out: list[Check] = []

    def add(name: str, ok: bool, good: str, bad: str, severity: str = "fail") -> None:
        out.append(Check(name, "pass" if ok else severity, good if ok else bad))

    if s.outbound_email_enabled:
        test = sorted(s.test_recipients)
        add("Sending switch", bool(test), f"ON in TEST MODE: everything goes only to {', '.join(test)}",
            "ON with no test recipients: approved emails go to real recipients", "warn")
    else:
        out.append(Check("Sending switch", "pass", "OFF (OUTBOUND_EMAIL_ENABLED=false): nothing can be sent"))
    oauth = s.mail_auth == "oauth"  # Microsoft 365: signs in with a token, no password is stored
    add("SMTP settings", bool(s.smtp_host and s.smtp_from and s.smtp_username and (s.smtp_password or oauth)),
        f"{s.smtp_host}:{s.smtp_port}, from {s.smtp_from}, " + ("signs in with OAuth" if oauth else "password set"),
        "SMTP_HOST, SMTP_FROM, SMTP_USERNAME and SMTP_PASSWORD are needed (see docs/SALES_SETUP.md)")
    if s.smtp_host and "gmail" in s.smtp_host.lower():
        add("Gmail SMTP", s.smtp_port == 587 and s.smtp_use_tls, "smtp.gmail.com:587 with STARTTLS",
            "Gmail needs SMTP_PORT=587 and SMTP_USE_TLS=true", "warn")
    if s.smtp_from and s.smtp_username and s.smtp_from.lower() != s.smtp_username.lower():
        out.append(Check("Sender identity", "warn", f"SMTP_FROM ({s.smtp_from}) differs from SMTP_USERNAME: most "
                                                     "servers send as the signed-in mailbox unless it is an allowed "
                                                     "alias ('Send mail as' in Gmail)"))
    user, password = s.imap_credentials
    add("IMAP settings", bool(s.imap_host and user and (password or oauth)),
        f"{s.imap_host}:{s.imap_port}, folder {s.imap_folder}",
        "IMAP_HOST (your mail server's IMAP host, e.g. imap.gmail.com or mail.<your domain>) is needed to read "
        "replies; it reuses the SMTP username and password")
    if s.smtp_from and user and s.smtp_from.lower() != user.lower():
        out.append(Check("Replies reach the monitored inbox", "warn",
                         f"emails are sent from {s.smtp_from} but the inbox job reads {user}: replies may be missed"))
    add("Postal address (CAN-SPAM)", bool(s.company_postal_address), "set",
        "COMPANY_POSTAL_ADDRESS is required in every sales email; nothing is sent without it")
    add("Sender name", bool(s.sender_name), s.sender_name or "", "SENDER_NAME is empty: the company name is used", "warn")
    add("Unsubscribe address", bool(s.unsubscribe_email), s.unsubscribe_email or "",
        "UNSUBSCRIBE_EMAIL is empty: SMTP_FROM is used for opt-outs", "warn")
    add("Test recipient", bool(s.test_recipients), ", ".join(sorted(s.test_recipients)),
        "OUTBOUND_TEST_RECIPIENTS is empty: set an address you control for the acceptance test", "warn")
    url = urlparse(s.calendly_url or "")
    if not s.calendly_url:
        out.append(Check("Calendly link", "warn", "CALENDLY_URL is empty: replies to interested prospects have no booking link"))
    else:
        add("Calendly link", url.scheme == "https" and (url.hostname or "").endswith("calendly.com") and len(url.path) > 1,
            s.calendly_url, f"{s.calendly_url} is not a https://calendly.com/<you>/<event> link")
    return out


def dns_resolver(timeout: float = 5.0) -> Callable[[str, str], list[str]]:
    import dns.exception
    import dns.resolver

    r = dns.resolver.Resolver()
    r.lifetime = timeout

    def resolve(name: str, rtype: str) -> list[str]:
        try:
            answers = r.resolve(name, rtype)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            return []
        except dns.exception.Timeout as exc:
            raise TimeoutError(f"DNS lookup of {name} timed out") from exc
        if rtype == "TXT":
            return ["".join(part.decode("utf-8", "replace") for part in a.strings) for a in answers]
        return [a.to_text() for a in answers]
    return resolve


def _host_ips(name: str, resolve) -> set[str]:
    return {ip.strip() for ip in resolve(name.rstrip("."), "A")}


def _spf_authorizes(spf: str, domain: str, smtp_host: str, resolve) -> str | None:
    """The SPF mechanism that authorizes `smtp_host` to send for `domain`, or None."""
    terms = [t.lstrip("+") for t in spf.lower().split()[1:]]
    want = next((inc for key, inc in PROVIDER_SPF.items() if key in smtp_host.lower()), None)
    if want and want in terms:
        return want
    ips = _host_ips(smtp_host, resolve)
    if not ips:
        return None
    for t in terms:
        if t.startswith("ip4:"):
            try:
                net = ipaddress.ip_network(t[4:], strict=False)
            except ValueError:
                continue
            if any(ipaddress.ip_address(ip) in net for ip in ips if ":" not in ip):
                return t
        elif t in ("a", f"a:{domain}") and ips & _host_ips(domain, resolve):
            return t
        elif t.startswith("a:") and ips & _host_ips(t[2:], resolve):
            return t
        elif t == "mx":
            for mx in resolve(domain, "MX"):
                if ips & _host_ips(mx.split()[-1], resolve):
                    return t
    return None


def check_domain(domain: str, resolve: Callable[[str, str], list[str]], smtp_host: str | None = None) -> list[Check]:
    """MX, SPF, DKIM and DMARC of the sending domain. With `smtp_host`, SPF must authorize that server: the
    provider's include (Google, Microsoft 365) or, for any other server, its address."""
    domain = domain.lower().strip()
    if domain in PERSONAL_GMAIL:
        return [Check("Sending domain", "warn", f"a personal @{domain} address: SPF and DKIM cannot be set, and business "
                                                "filters treat it as consumer mail. Use Google Workspace on your "
                                                "company domain for outreach")]
    out: list[Check] = []
    mx = resolve(domain, "MX")
    out.append(Check("MX (replies can arrive)", "pass" if mx else "fail",
                     ", ".join(sorted(mx))[:200] if mx else f"{domain} has no MX record: replies cannot be delivered"))
    spf = [t for t in resolve(domain, "TXT") if t.lower().startswith("v=spf1")]
    gmail = is_gmail(smtp_host)
    if not spf:
        mechanism = GOOGLE_SPF if gmail or not smtp_host else f"a:{smtp_host}"
        out.append(Check("SPF", "fail", f"no SPF record on {domain}: add TXT \"v=spf1 {mechanism} ~all\""))
    elif len(spf) > 1:
        out.append(Check("SPF", "fail", f"{len(spf)} SPF records on {domain}: there must be exactly one"))
    elif not smtp_host:
        out.append(Check("SPF", "warn", f"{spf[0][:160]}: set SMTP_HOST to check that it authorizes your mail server"))
    else:
        how = _spf_authorizes(spf[0], domain, smtp_host, resolve)
        ips = ", ".join(sorted(_host_ips(smtp_host, resolve))) or "its address"
        need = GOOGLE_SPF if gmail else next((inc for k, inc in PROVIDER_SPF.items() if k in smtp_host.lower()),
                                             f"ip4:{ips}")
        out.append(Check("SPF", "pass" if how else "warn",
                         f"{spf[0][:160]} (authorizes {smtp_host} via {how})" if how else
                         f"{spf[0][:140]} does not authorize {smtp_host} ({ips}): add {need} before ~all"))
    selectors = ("google",) + tuple(x for x in DKIM_SELECTORS if x != "google") if gmail else DKIM_SELECTORS
    found = next((sel for sel in selectors if any("p=" in t for t in resolve(f"{sel}._domainkey.{domain}", "TXT"))),
                 None)
    hint = ("turn on DKIM signing in the Google Admin console" if gmail else
            "turn on DKIM signing at your mail provider (cPanel: Email Deliverability)")
    out.append(Check("DKIM", "pass" if found else "warn",
                     f"key published at {found}._domainkey; a test email's headers show whether the server signs "
                     "with it (dkim=pass)" if found else
                     f"no DKIM key at the usual selectors ({', '.join(selectors)}): {hint}"))
    dmarc = [t for t in resolve(f"_dmarc.{domain}", "TXT") if t.lower().startswith("v=dmarc1")]
    if dmarc:
        policy = next((p.split("=", 1)[1].strip() for p in dmarc[0].split(";") if p.strip().lower().startswith("p=")), "?")
        out.append(Check("DMARC", "pass", f"policy {policy}" + (" (monitoring only)" if policy == "none" else "")))
    else:
        out.append(Check("DMARC", "warn", f"no DMARC record: add TXT at _dmarc.{domain}, e.g. \"v=DMARC1; p=none; "
                                          f"rua=mailto:<you>@{domain}\""))
    return out


def check_smtp(s, smtp_factory=smtplib.SMTP) -> Check:
    if s.mail_auth == "oauth":
        return Check("SMTP sign-in", "skip", OAUTH_SIGN_IN)
    if not (s.smtp_host and s.smtp_username and s.smtp_password):
        return Check("SMTP sign-in", "skip", "SMTP host, username or password not set")
    if not s.smtp_use_tls:
        return Check("SMTP sign-in", "fail", "not tried: SMTP_USE_TLS=false would send the password unencrypted; "
                                              "set SMTP_USE_TLS=true (Gmail: port 587 with STARTTLS)")
    try:
        with smtp_factory(s.smtp_host, s.smtp_port, timeout=MAIL_TIMEOUT) as smtp:
            smtp.starttls(context=tls_context())  # certificate and host name are verified
            smtp.login(s.smtp_username, s.smtp_password)
            smtp.noop()
        return Check("SMTP sign-in", "pass", f"signed in to {s.smtp_host} as {s.smtp_username}; no email was sent")
    except smtplib.SMTPAuthenticationError:
        hint = ("Gmail needs an app password (Google account > Security > 2-Step Verification > App passwords), not "
                "your normal password" if is_gmail(s.smtp_host) else
                "check SMTP_USERNAME (usually the full email address) and the mailbox password")
        return Check("SMTP sign-in", "fail", f"sign-in refused. {hint}")
    except (OSError, smtplib.SMTPException) as exc:
        return Check("SMTP sign-in", "fail", redact(f"{type(exc).__name__}: {exc}")[:300])


def check_imap(s, imap_factory=imaplib.IMAP4_SSL) -> Check:
    if s.mail_auth == "oauth":
        return Check("IMAP read-only", "skip", OAUTH_SIGN_IN)
    user, password = s.imap_credentials
    if not (s.imap_host and user and password):
        return Check("IMAP read-only", "skip", "IMAP host, username or password not set")
    try:
        conn = imap_factory(s.imap_host, s.imap_port, ssl_context=tls_context(), timeout=MAIL_TIMEOUT)
    except OSError as exc:
        return Check("IMAP read-only", "fail", redact(f"{type(exc).__name__}: {exc}")[:300])
    try:
        conn.login(user, password)
        typ, data = conn.select(f'"{s.imap_folder}"', readonly=True)  # EXAMINE: never changes the mailbox
        if typ != "OK":
            return Check("IMAP read-only", "fail", f"cannot open folder {s.imap_folder}")
        count = (data[0] or b"?").decode() if data else "?"
        return Check("IMAP read-only", "pass", f"opened {s.imap_folder} read-only ({count} messages); nothing was changed")
    except imaplib.IMAP4.error:
        hint = ("In Gmail: Settings > Forwarding and POP/IMAP > Enable IMAP, and use the app password"
                if is_gmail(s.imap_host) else "check IMAP_USERNAME/IMAP_PASSWORD (default: the SMTP ones)")
        return Check("IMAP read-only", "fail", f"sign-in refused. {hint}")
    except OSError as exc:
        return Check("IMAP read-only", "fail", redact(f"{type(exc).__name__}: {exc}")[:300])
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - closing a connection that already failed
            pass


def send_test(s, sender: EmailSender) -> Check:
    tests = sorted(s.test_recipients)
    if not tests:
        return Check("Test email", "fail", "OUTBOUND_TEST_RECIPIENTS is empty: the test email goes only to your own address")
    ready, reason = sender.status()
    if not ready:
        return Check("Test email", "fail", f"not sent: {reason}. Run it with the acceptance settings (docs/ACCEPTANCE.md)")
    body = ("This is a mailbox test from your AI Sales Employee. Reply to it with any text to check that replies "
            "are read (python -m aime run inbox)." + compliance_footer(s))
    try:
        msg_id = sender.send(tests[0], "AI Sales Employee mailbox test", body)
    except (EmailNotReady, OSError, smtplib.SMTPException) as exc:
        return Check("Test email", "fail", redact(f"not sent: {exc}")[:300])
    return Check("Test email", "pass", f"sent to {tests[0]} only (Message-ID {msg_id})")


def run_email_check(s, *, connect: bool = False, send: bool = False, domain: str | None = None,
                    resolve: Callable[[str, str], list[str]] | None = None, smtp_factory=smtplib.SMTP,
                    imap_factory=imaplib.IMAP4_SSL) -> list[Check]:
    checks = check_settings(s)
    domain = domain or (s.smtp_from.rsplit("@", 1)[-1] if "@" in (s.smtp_from or "") else None)
    if domain:
        try:
            checks += check_domain(domain, resolve or dns_resolver(), s.smtp_host or None)
        except TimeoutError as exc:
            checks.append(Check("Sending domain", "warn", str(exc)))
    else:
        checks.append(Check("Sending domain", "skip", "SMTP_FROM is not set (or pass --domain yourcompany.com)"))
    if connect or send:
        checks += [check_smtp(s, smtp_factory), check_imap(s, imap_factory)]
    else:
        checks.append(Check("Mailbox sign-in", "skip", "add --connect to sign in to SMTP and IMAP (nothing is sent)"))
    if send:
        checks.append(send_test(s, EmailSender(s, smtp_factory=smtp_factory)))
    return checks
