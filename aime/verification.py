"""Email verification: one vocabulary for what is known about an address, a verifier interface, and the free mail-server
check as the default verifier.

Statuses, in plain English:
- published: printed on the company's own website, on its own domain, next to the person it belongs to, and the domain takes
  mail (aime.contact_validation). The strongest evidence AIME can gather for free;
- mx_ok: the domain takes mail (an MX record, or an A record as the implicit mail host). Says nothing about the mailbox;
- verified: a mailbox-level check by an approved verification service said the mailbox exists. No such service is approved
  yet (aime.contact_sources.paid.PaidEmailVerifier is a blocked stub);
- risky: such a service could not confirm the mailbox (a catch-all domain, a full mailbox);
- invalid: the address cannot receive mail (bad syntax, a domain without a mail server, a bounce);
- role_address: a company inbox (info@, sales@ ...). It may be written to, but it names nobody, so it is never a verified
  decision-maker, whatever a verifier says about the mailbox;
- unknown: not checked yet, or the check could not tell (a DNS timeout).

Only `published` and `verified` make a verified contact (VERIFIED_STATUSES). Nothing here guesses an address.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from aime.contact_validation import EMAIL_SYNTAX, ROLE_LOCALS, MailDomainChecker
from aime.models import Contact
from aime.timeutil import utcnow

PUBLISHED, MX_OK, VERIFIED, RISKY, INVALID, ROLE_ADDRESS, UNKNOWN = (
    "published", "mx_ok", "verified", "risky", "invalid", "role_address", "unknown")
STATUSES = (PUBLISHED, MX_OK, VERIFIED, RISKY, INVALID, ROLE_ADDRESS, UNKNOWN)
VERIFIED_STATUSES = (PUBLISHED, VERIFIED)
MEANINGS = {
    PUBLISHED: "published by the company on its own domain next to the person, and the domain takes mail",
    MX_OK: "the domain takes mail; the mailbox itself is not checked",
    VERIFIED: "an approved verification service confirmed the mailbox",
    RISKY: "a verification service could not confirm the mailbox",
    INVALID: "cannot receive mail (bad syntax, no mail server, or it bounced)",
    ROLE_ADDRESS: "a company inbox: may be written to, never a verified decision-maker",
    UNKNOWN: "not checked yet, or the check could not tell",
}
# Contact.email_status as the CRM stores it -> the status here.
_STORED = {"published": PUBLISHED, "verified": VERIFIED, "valid": VERIFIED, "mx_ok": MX_OK, "risky": RISKY,
           "role_address": ROLE_ADDRESS, "bounced": INVALID, "invalid": INVALID, "no_mail_server": INVALID}


@dataclass(frozen=True)
class VerificationResult:
    status: str
    detail: str = ""
    verified_by: str = ""


class Verifier(ABC):
    name: str = "verifier"

    @abstractmethod
    def check(self, email: str) -> VerificationResult: ...


def is_role_address(email: str | None) -> bool:
    local = (email or "").split("@", 1)[0].strip().lower()
    return bool(local) and (local in ROLE_LOCALS or local.split("+")[0] in ROLE_LOCALS)


class MXVerifier(Verifier):
    """The free default: syntax, then whether the domain takes mail (aime.contact_validation.MailDomainChecker)."""

    name = "mx_check"

    def __init__(self, checker=None):
        self.checker = checker or MailDomainChecker()

    def check(self, email: str) -> VerificationResult:
        email = (email or "").strip().lower()
        if not EMAIL_SYNTAX.match(email) or ".." in email:
            return VerificationResult(INVALID, "invalid syntax", self.name)
        ok = self.checker(email.split("@", 1)[1])
        if ok is None:
            return VerificationResult(UNKNOWN, "the mail-server check could not tell (DNS)", self.name)
        if not ok:
            return VerificationResult(INVALID, "the domain has no mail server", self.name)
        if is_role_address(email):
            return VerificationResult(ROLE_ADDRESS, "a company inbox on a domain that takes mail", self.name)
        return VerificationResult(MX_OK, "the domain takes mail", self.name)


def status_of(contact: Contact) -> str:
    """What is known about this contact's address, in the statuses above."""
    if not contact.email:
        return UNKNOWN
    stored = _STORED.get(contact.email_status or "")
    if stored == INVALID:
        return INVALID
    if is_role_address(contact.email) or contact.role_category == "inbox" or stored == ROLE_ADDRESS:
        return ROLE_ADDRESS
    return stored or UNKNOWN


def verify_contact(session: Session, contact: Contact, verifier: Verifier, now: datetime | None = None) -> VerificationResult:
    """Check one contact's address and record who checked it and when (verified_by, verified_at, provenance). The stored
    email status changes only where the answer is new evidence: a mailbox `verified` or `risky` by a verification service,
    or an address that cannot receive mail. A bounce stays a bounce, a published address stays published on a plain domain
    check, and an address a person gave us keeps its own status. A role address is recorded as one, never as verified."""
    if not contact.email:
        return VerificationResult(UNKNOWN, "no email address", verifier.name)
    result = verifier.check(contact.email)
    if is_role_address(contact.email) and result.status in (VERIFIED, MX_OK, RISKY):
        result = VerificationResult(ROLE_ADDRESS, f"a company inbox ({result.status} by {verifier.name})", result.verified_by)
    now = now or utcnow()
    contact.verified_by, contact.verified_at = (result.verified_by or verifier.name)[:40], now
    contact.provenance = {**(contact.provenance or {}), "verification": {"status": result.status, "detail": result.detail[:200],
                                                                         "by": contact.verified_by, "at": now.isoformat(timespec="seconds")}}
    if contact.email_status not in ("bounced", "user_provided"):
        if result.status in (VERIFIED, RISKY):
            contact.email_status = result.status
        elif result.status == INVALID:
            contact.email_status = "no_mail_server" if verifier.name == MXVerifier.name else "invalid"
        elif result.status == ROLE_ADDRESS and contact.email_status in (None, "unchecked"):
            contact.email_status = "role_address"
    contact.email_checked_at = now
    session.flush()
    return result
