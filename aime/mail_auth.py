"""Is an email really from who it says? A Calendly notice can create, move or cancel meetings and move deals, and
anyone can type "From: notifications@calendly.com". So such a notice changes the CRM by itself only when its
signature is verified: AIME checks the DKIM signatures itself when it reads the email (aime.dkim_verify; our mail
server stamps no Authentication-Results), and records "aime.dkim; dkim=pass header.d=<domain>" for each that holds.
A server's own Authentication-Results line counts only if that server is named in TRUSTED_AUTHSERV_IDS; a header
the sender wrote is never believed. The notice must be signed by calendly.com, or forwarded by one of our trusted
forwarders (our own mailbox, the owner's test address, CALENDLY_FORWARDERS) and signed by that forwarder's domain.
Anything else waits for a person to confirm it on Replies."""

from __future__ import annotations

import re

_PASS = re.compile(r"\b(dkim|dmarc|spf)\s*=\s*pass\b([^;]*)", re.I)
_DOMAIN = re.compile(r"\b(?:header\.d|header\.i|header\.from|smtp\.mailfrom)\s*=\s*@?([a-z0-9.-]+\.[a-z]{2,})", re.I)


OWN_AUTHSERV = "aime.dkim"  # the line AIME writes itself after verifying DKIM signatures (aime.dkim_verify)


def authserv_id(auth_results: str | None) -> str:
    return (auth_results or "").split(";", 1)[0].strip().lower()


def trusted_authserv_ids(settings) -> set[str]:
    """Servers whose Authentication-Results lines are believed (TRUSTED_AUTHSERV_IDS, comma-separated): only a server
    that removes such lines from incoming mail may be named. Empty by default: our mail server stamps none."""
    return {x.strip().lower() for x in (getattr(settings, "trusted_authserv_ids", "") or "").split(",") if x.strip()}


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
    if not auth_results:
        return False, "no verified signature was recorded for this email"
    if authserv_id(auth_results) not in {OWN_AUTHSERV} | trusted_authserv_ids(settings):
        return False, "its authentication line was not written by AIME or a trusted mail server"
    domains = passing_domains(auth_results)
    if _within(sender_domain, "calendly.com"):
        if any(_within(d, "calendly.com") for d in domains):
            return True, "signed by calendly.com (DKIM/DMARC pass)"
        return False, "it claims to come from Calendly but has no passing calendly.com signature"
    if sender in trusted_forwarders(session, settings):
        if any(_within(d, sender_domain) or _within(sender_domain, d) for d in domains):
            return True, f"forwarded by {sender}, signed by its domain"
        return False, f"forwarded from {sender} without a passing signature for {sender_domain}"
    return False, f"{sender or 'the sender'} is neither Calendly nor a trusted forwarder"
