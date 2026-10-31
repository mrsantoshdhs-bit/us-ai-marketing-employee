"""Is an email really from who it says? A Calendly notice can create, move or cancel meetings and move deals, and
anyone can type "From: notifications@calendly.com". So such a notice changes the CRM by itself only when its
signature is verified: AIME checks the DKIM signatures itself when it reads the email (aime.dkim_verify; our mail
server stamps no Authentication-Results), and records "aime.dkim; dkim=pass header.d=<domain>" for each that holds.
A server's own Authentication-Results line counts only if that server is named in TRUSTED_AUTHSERV_IDS; a header
the sender wrote is never believed. The notice must be signed by calendly.com, or forwarded by one of our trusted
forwarders (our own mailbox, the owner's test address, CALENDLY_FORWARDERS) and signed by that forwarder's domain. Calendly
signs what it sends to anyone, so a notice it sent directly must also be addressed to one of those addresses.
Anything else waits for a person to confirm it on Replies."""

from __future__ import annotations

import re

_PASS = re.compile(r"\b(dkim|dmarc|spf)\s*=\s*pass\b([^;]*)", re.I)
_COMMENT = re.compile(r"\([^()]*\)")
_PROPERTIES = ("header.d", "header.i", "header.from", "smtp.mailfrom")
_HOSTNAME = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}")


OWN_AUTHSERV = "aime.dkim"  # the line AIME writes itself after verifying DKIM signatures (aime.dkim_verify)


def authserv_id(auth_results: str | None) -> str:
    return (auth_results or "").split(";", 1)[0].strip().lower()


def trusted_authserv_ids(settings) -> set[str]:
    """Servers whose Authentication-Results lines are believed (TRUSTED_AUTHSERV_IDS, comma-separated): only a server
    that removes such lines from incoming mail may be named. Empty by default: our mail server stamps none."""
    return {x.strip().lower() for x in (getattr(settings, "trusted_authserv_ids", "") or "").split(",") if x.strip()}


def _passes(auth_results: str | None) -> list[tuple[str, bool]]:
    """(domain, whether the signature covers the To line) for each passing DKIM or DMARC result. SPF only counts with DMARC's
    alignment, so it is not used. Only AIME's own line says when To is left out (Microsoft 365 commonly does)."""
    out = []
    for method, rest in _PASS.findall(auth_results or ""):
        if method.lower() not in ("dkim", "dmarc"):
            continue
        # Whole properties only, each value a whole hostname: "header.d=a,header.d=calendly.com,.evil.example" names nobody.
        tokens = re.sub(r"\s*=\s*", "=", _COMMENT.sub(" ", rest)).split()
        to_signed = "header.to=unsigned" not in tokens
        for token in tokens:
            key, _, value = token.partition("=")
            value = value.strip('"').rsplit("@", 1)[-1].lower().rstrip(".")
            if key.lower() in _PROPERTIES and len(value) <= 253 and _HOSTNAME.fullmatch(value):
                out.append((value, to_signed))
    return out


def passing_domains(auth_results: str | None) -> set[str]:
    """Domains with a passing DKIM or DMARC result."""
    return {domain for domain, _ in _passes(auth_results)}


def _within(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith("." + parent)


def sender_signed(from_email: str | None, auth_results: str | None) -> bool:
    """Did the sender's own domain sign this email? `auth_results` is what we stored: AIME's line or a trusted server's."""
    domain = (from_email or "").rsplit("@", 1)[-1].lower()
    return "@" in (from_email or "") and any(_within(d, domain) or _within(domain, d) for d in passing_domains(auth_results))


def trusted_forwarders(session, settings) -> set[str]:
    from sqlalchemy import select

    from aime.models import MailAccount

    own = {a.lower() for a in (settings.smtp_from, settings.imap_credentials[0], settings.unsubscribe_email) if a}
    own |= {e.lower() for e in session.execute(select(MailAccount.email)).scalars() if e}
    own |= set(getattr(settings, "test_recipients", set()))
    own |= {a.strip().lower() for a in (getattr(settings, "calendly_forwarders", "") or "").split(",") if a.strip()}
    return own


def calendly_verified(session, settings, from_email: str | None, auth_results: str | None,
                      to_email: str | None = None) -> tuple[bool, str]:
    """(trusted, why) for a Calendly notice. `to_email`: who it was addressed to, for a notice Calendly sent directly."""
    sender = (from_email or "").lower()
    sender_domain = sender.rsplit("@", 1)[-1]
    if not auth_results:
        return False, "no verified signature was recorded for this email"
    if authserv_id(auth_results) not in {OWN_AUTHSERV} | trusted_authserv_ids(settings):
        return False, "its authentication line was not written by AIME or a trusted mail server"
    domains = passing_domains(auth_results)
    if _within(sender_domain, "calendly.com"):
        if any(_within(d, "calendly.com") for d in domains):
            if to_email is not None and not to_email:
                return False, "Calendly signed it, but it has no To line, so it cannot be shown to be meant for us"
            if to_email is not None and to_email.lower() not in trusted_forwarders(session, settings):
                return False, (f"Calendly signed it, but it is addressed to {to_email}, not to one of our addresses "
                               "(if that address is ours, add it to the Calendly forwarders)")
            if to_email is not None and not any(_within(d, "calendly.com") and to_signed for d, to_signed in _passes(auth_results)):
                return False, "Calendly signed it, but its signature does not cover who it is addressed to"
            return True, "signed by calendly.com (DKIM/DMARC pass)"
        return False, "it claims to come from Calendly but has no passing calendly.com signature"
    if sender in trusted_forwarders(session, settings):
        if any(_within(d, sender_domain) or _within(sender_domain, d) for d in domains):
            return True, f"forwarded by {sender}, signed by its domain"
        return False, f"forwarded from {sender} without a passing signature for {sender_domain}"
    return False, f"{sender or 'the sender'} is neither Calendly nor a trusted forwarder"
