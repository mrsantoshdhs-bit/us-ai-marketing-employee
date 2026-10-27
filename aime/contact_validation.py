"""Business contact enrichment from what companies publish, and email validation. Nothing is guessed.

An email is used only if the company's own website publishes it, on the company's own domain:
- printed next to a named person and its local part fits that person's name -> that person's email, high;
- its local part matches exactly one named person's name (jane.doe@, jdoe@, jane@) -> that person, medium;
- a general business inbox (info@, hello@, sales@, contact@ ...)    -> a "company inbox" contact, even when
  it is printed under someone's name.
An address printed next to one person that does not fit their name is given to nobody: it may be theirs,
an assistant's or a colleague's, and a wrong owner would put the wrong name in an email.
Support, jobs, privacy, no-reply and similar addresses, placeholders and free-mail addresses are not used.
Each domain is checked for a mail server (MX, or an A record as the implicit MX; a "null MX" means the
domain accepts no mail). Statuses: published | role_address | no_mail_server | unchecked (DNS unknown);
the inbox later adds bounced.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.models import Company, Contact
from aime.timeutil import utcnow

EMAIL_SYNTAX = re.compile(r"^[a-z0-9](?:[a-z0-9._%+-]{0,62}[a-z0-9])?@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
ROLE_LOCALS = {"info", "hello", "hi", "contact", "contactus", "sales", "office", "team", "inquiries", "inquiry",
               "enquiries", "business", "partners", "partnerships", "connect", "general", "letstalk", "work",
               "newbusiness", "bd", "biz"}
EXCLUDED_LOCALS = {"noreply", "no-reply", "donotreply", "do-not-reply", "support", "help", "helpdesk", "service",
                   "customerservice", "care", "jobs", "careers", "career", "hr", "recruiting", "recruitment", "talent",
                   "hiring", "privacy", "legal", "abuse", "dmca", "security", "compliance", "press", "media", "pr",
                   "billing", "accounts", "ap", "ar", "invoices", "invoice", "payments", "webmaster", "postmaster",
                   "hostmaster", "unsubscribe", "bounce", "bounces", "mailer-daemon", "notifications", "alerts",
                   "example", "test", "name", "you", "your", "yourname", "email", "user", "username", "firstname",
                   "first.last"}
PLACEHOLDER_DOMAINS = {"example.com", "domain.com", "yourdomain.com", "email.com", "company.com", "yourcompany.com",
                       "website.com", "sentry.io", "wixpress.com"}
STATUS_OK = ("published", "role_address")


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if c.isalpha())


def name_patterns(first: str | None, last: str | None) -> set[str]:
    f, l = fold((first or "").split()[0] if first else ""), fold(last or "")  # noqa: E741
    if not f or not l:
        return set()
    return {f, l, f + l, f"{f}.{l}", f"{f}_{l}", f"{f}-{l}", f[0] + l, f"{f[0]}.{l}", f + l[0], f"{f}.{l[0]}",
            l + f, f"{l}.{f}", l + f[0], f"{l}.{f[0]}"}


def usable(email: str) -> tuple[bool, str]:
    email = email.strip().lower()
    if not EMAIL_SYNTAX.match(email) or ".." in email:
        return False, "invalid syntax"
    local, domain = email.split("@", 1)
    if local in EXCLUDED_LOCALS or local.split("+")[0] in EXCLUDED_LOCALS:
        return False, "not a sales or person address"
    if domain in PLACEHOLDER_DOMAINS:
        return False, "placeholder or third-party address"
    return True, ""


class MailDomainChecker:
    """Does the domain accept email? Cached per run. True / False / None (could not tell)."""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self.cache: dict[str, bool | None] = {}

    def __call__(self, domain: str) -> bool | None:
        if domain not in self.cache:
            self.cache[domain] = self._lookup(domain)
        return self.cache[domain]

    def _lookup(self, domain: str) -> bool | None:
        import dns.exception
        import dns.resolver

        resolver = dns.resolver.Resolver()
        resolver.lifetime = self.timeout
        try:
            answers = resolver.resolve(domain, "MX")
            exchanges = [str(r.exchange).rstrip(".") for r in answers]
            return any(exchanges)  # RFC 7505 null MX ("0 .") means the domain accepts no mail
        except dns.resolver.NXDOMAIN:
            return False
        except dns.resolver.NoAnswer:
            try:
                resolver.resolve(domain, "A")  # no MX: the A record is the implicit mail host (RFC 5321)
                return True
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                return False
            except dns.exception.DNSException:
                return None
        except dns.exception.DNSException:
            return None


def _status(checker, domain: str, role: bool) -> str:
    ok = checker(domain)
    if ok is None:
        return "unchecked"
    if not ok:
        return "no_mail_server"
    return "role_address" if role else "published"


def assign_published_emails(session: Session, company: Company, published: list[dict], checker) -> dict:
    """Attach the company's published emails to its researched people or to a company-inbox contact."""
    stats = {"person_emails": 0, "inboxes": 0, "skipped": 0}
    contacts = session.execute(select(Contact).where(Contact.company_id == company.id)).scalars().all()
    people = [c for c in contacts if c.source in ("website", "sec_filing") and c.last_name]
    taken = {c.email for c in contacts if c.email}
    now = utcnow()
    for item in published:
        email = item["email"].strip().lower()
        if email in taken:
            continue
        ok, _ = usable(email)
        if not ok or not item.get("on_domain"):
            stats["skipped"] += 1
            continue
        local, domain = email.split("@", 1)
        person, confidence, evidence = None, None, None
        near = None
        if item.get("near"):
            folded = fold(item["near"])
            near = next((p for p in people if fold(f"{p.first_name or ''}{p.last_name or ''}") == folded), None)
        if local in ROLE_LOCALS:
            pass  # a general inbox is the company's, never one person's own address
        elif near is not None:
            if local in name_patterns(near.first_name, near.last_name):
                person, confidence = near, "high"
                evidence = f'Published next to "{item["near"]}" on {item["url"]}'
        else:
            matches = [p for p in people if local in name_patterns(p.first_name, p.last_name)]
            if len(matches) == 1:
                person, confidence = matches[0], "medium"
                evidence = f"Published on {item['url']}; the address matches {person.display_name}'s name"
        if person is not None and not person.email:
            person.email, person.email_source_url = email, item["url"][:500]
            person.email_confidence, person.email_evidence = confidence, evidence
            person.email_status, person.email_checked_at = _status(checker, domain, role=False), now
            taken.add(email)
            stats["person_emails"] += 1
        elif person is None and local in ROLE_LOCALS:
            session.add(Contact(company_id=company.id, email=email, title="Company inbox", source="website",
                                consent_status="none", role_category="inbox", relevance=20, confidence="high",
                                evidence=f"Published on {item['url']}: {email}", evidence_url=item["url"][:500],
                                discovered_at=now, email_source_url=item["url"][:500], email_confidence="high",
                                email_evidence=f"General business inbox published on {item['url']}",
                                email_status=_status(checker, domain, role=True), email_checked_at=now))
            taken.add(email)
            stats["inboxes"] += 1
        else:
            stats["skipped"] += 1
    session.flush()
    return stats


def recheck_emails(session: Session, company: Company, checker, only: tuple[str, ...] | None = None) -> None:
    """Refresh mail-server checks for already assigned, not-bounced researched emails (`only`: just these
    statuses, e.g. "unchecked" after a DNS timeout)."""
    for c in session.execute(select(Contact).where(Contact.company_id == company.id, Contact.email.is_not(None),
                                                   Contact.source.in_(("website", "sec_filing")))).scalars():
        if c.email_status in ("bounced", "invalid") or (only is not None and c.email_status not in only):
            continue
        c.email_status = _status(checker, c.email.split("@", 1)[1], role=c.role_category == "inbox")
        c.email_checked_at = utcnow()
