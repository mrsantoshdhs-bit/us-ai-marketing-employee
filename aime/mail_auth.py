"""Is an email really from who it says? A Calendly notice can create, move or cancel meetings and move deals, and
anyone can type "From: notifications@calendly.com". So such a notice changes the CRM by itself only when the
receiving mail server vouches for it: the topmost Authentication-Results header (the one our own server, or Gmail
for a forwarded notice, added; RFC 8601) shows a passing DKIM signature or DMARC result for calendly.com, or for
one of our trusted forwarders (our own mailbox, the owner's test address, CALENDLY_FORWARDERS). Anything else waits
for a person to confirm it on Replies. Lower Authentication-Results headers are ignored: a sender can write those."""

from __future__ import annotations

import re

_PASS = re.compile(r"\b(dkim|dmarc|spf)\s*=\s*pass\b([^;]*)", re.I)
_DOMAIN = re.compile(r"\b(?:header\.d|header\.i|header\.from|smtp\.mailfrom)\s*=\s*@?([a-z0-9.-]+\.[a-z]{2,})", re.I)


def passing_domains(auth_results: str | None) -> set[str]:
    """Domains with a passing DKIM or DMARC result (SPF only counts with DMARC's alignment, so it is not used)."""
    out = set()
    for method, rest in _PASS.findall(auth_results or ""):
        if method.lower() in ("dkim", "dmarc"):
            out.update(d.lower().rstrip(".") for d in _DOMAIN.findall(rest))
    return out


def _within(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith("." + parent)


def trusted_forwarders(session, settings) -> set[str]:
    from sqlalchemy import select

    from aime.models import MailAccount

    own = {a.lower() for a in (settings.smtp_from, settings.imap_credentials[0], settings.unsubscribe_email) if a}
    own |= {e.lower() for e in session.execute(select(MailAccount.email)).scalars() if e}
    own |= set(getattr(settings, "test_recipients", set()))
    own |= {a.strip().lower() for a in (getattr(settings, "calendly_forwarders", "") or "").split(",") if a.strip()}
    return own


def calendly_verified(session, settings, from_email: str | None, auth_results: str | None) -> tuple[bool, str]:
    """(trusted, why) for a Calendly notice."""
    sender = (from_email or "").lower()
    sender_domain = sender.rsplit("@", 1)[-1]
    domains = passing_domains(auth_results)
    if not auth_results:
        return False, "the mail server added no authentication result to this email"
    if _within(sender_domain, "calendly.com"):
        if any(_within(d, "calendly.com") for d in domains):
            return True, "signed by calendly.com (DKIM/DMARC pass)"
        return False, "it claims to come from Calendly but has no passing calendly.com signature"
    if sender in trusted_forwarders(session, settings):
        if any(_within(d, sender_domain) or _within(sender_domain, d) for d in domains):
            return True, f"forwarded by {sender}, signed by its domain"
        return False, f"forwarded from {sender} without a passing signature for {sender_domain}"
    return False, f"{sender or 'the sender'} is neither Calendly nor a trusted forwarder"
