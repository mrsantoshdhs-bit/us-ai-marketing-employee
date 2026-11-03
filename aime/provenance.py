"""Where each contact came from, under which licence and on which lawful basis: needed for compliance, for answering
"where did you get my details?" and for erasure.

`stamp` runs on every new contact, whichever path creates it (a mapper hook on aime.models.Contact): it looks up the
contact-source adapter by the contact's source (aime.contact_sources.registry.adapter_for_source) and records

- source_licence and lawful_basis: the tags that adapter declares;
- source_ref: the page, filing, register entry or inquiry the record rests on;
- provenance: {"adapter", "source", "ref", "recorded_at"}.

A path that knows more (an inquiry id) is read from what it already wrote (consent_source, evidence_url). Contacts from
before these columns are filled lazily by `backfill_legacy` as "legacy: <source>", in batches, from the company research
run; nothing about them is invented.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.models import Contact
from aime.timeutil import utcnow

LEGACY = "legacy: "
UNKNOWN = "unknown"
_INQUIRY = re.compile(r"Inbound inquiry #(\d+)")


def reference(contact: Contact) -> str | None:
    """The record a contact rests on: its evidence page or filing, the page its email was published on, or its inquiry."""
    if contact.source == "inbound":
        m = _INQUIRY.search(contact.consent_source or "")
        if m:
            return f"inbound_inquiry:{m.group(1)}"
    ref = contact.evidence_url or contact.email_source_url
    if ref:
        return ref[:500]
    if contact.source == "manual":
        return "entered by a person"
    return None


def stamp(contact: Contact) -> None:
    """Fill the provenance of a new contact (a value a path set itself is kept)."""
    if contact.source_licence and contact.lawful_basis:
        return
    from aime.contact_sources.registry import adapter_for_source

    cls = adapter_for_source(contact.source, contact.consent_source)
    contact.source_licence = contact.source_licence or (cls.licence if cls else f"{UNKNOWN}: {contact.source}")[:120]
    contact.lawful_basis = contact.lawful_basis or (cls.lawful_basis if cls else UNKNOWN)[:120]
    contact.source_ref = contact.source_ref or reference(contact)
    contact.provenance = contact.provenance or {"adapter": cls.name if cls else None, "source": contact.source,
                                                "ref": contact.source_ref, "recorded_at": utcnow().isoformat(timespec="seconds")}


def is_legacy(contact: Contact) -> bool:
    return (contact.source_licence or "").startswith(LEGACY)


def view(contact: Contact) -> dict:
    """What the record says of its origin, for a page: a row not backfilled yet reads as legacy."""
    if not contact.source_licence:
        return {"adapter": None, "licence": f"{LEGACY}{contact.source}", "lawful_basis": f"{LEGACY}{contact.source}",
                "ref": reference(contact), "legacy": True}
    return {"adapter": (contact.provenance or {}).get("adapter"), "licence": contact.source_licence,
            "lawful_basis": contact.lawful_basis, "ref": contact.source_ref, "legacy": is_legacy(contact)}


def backfill_legacy(session: Session, limit: int = 500) -> int:
    """Mark up to `limit` contacts from before provenance existed as "legacy: <source>", with the reference they already
    carry. Their licence and lawful basis are not known, so none is claimed."""
    rows = session.execute(select(Contact).where(Contact.source_licence.is_(None)).order_by(Contact.id).limit(max(0, limit))).scalars().all()
    for c in rows:
        label = f"{LEGACY}{c.source}"[:120]
        c.source_licence, c.lawful_basis = label, label
        c.source_ref = c.source_ref or reference(c)
        c.provenance = {"adapter": None, "source": c.source, "ref": c.source_ref, "legacy": True,
                        "recorded_at": utcnow().isoformat(timespec="seconds")}
    if rows:
        session.flush()
    return len(rows)


def pending_legacy(session: Session) -> int:
    from sqlalchemy import func

    return session.execute(select(func.count(Contact.id)).where(Contact.source_licence.is_(None))).scalar_one()
