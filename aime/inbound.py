"""Inbound inquiry intake: API/web form/manual submissions and files dropped in data/inbox/."""

from __future__ import annotations

import csv
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.crm_ops import fingerprint
from aime.knowledge.services import SERVICES
from aime.models import InboundInquiry

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class InboundPayload(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    company: str | None = None
    website: str | None = None
    state: str | None = None
    city: str | None = None
    country: str | None = None
    industry: str | None = None
    message: str = ""
    services_interested: list[str] = Field(default_factory=list)
    budget: str | None = None
    timeline: str | None = None
    consent_marketing: bool = False
    source_page: str | None = None
    hp: str | None = None  # honeypot field: real people leave it empty
    is_sample: bool = False

    @field_validator("services_interested", mode="before")
    @classmethod
    def _split_services(cls, v):
        if isinstance(v, str):
            v = [x for x in re.split(r"[;,|]", v) if x.strip()]
        out = []
        by_name = {s.name.lower(): s.id for s in SERVICES.values()}
        for item in v or []:
            key = str(item).strip()
            if key in SERVICES:
                out.append(key)
            elif key.lower() in by_name:
                out.append(by_name[key.lower()])
        return out

    @field_validator("consent_marketing", "is_sample", mode="before")
    @classmethod
    def _boolish(cls, v):
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "on", "y"}
        return bool(v)

    def split_name(self) -> tuple[str | None, str | None]:
        if self.first_name or self.last_name:
            return self.first_name, self.last_name
        if self.name:
            parts = self.name.strip().split()
            return parts[0], " ".join(parts[1:]) or None
        return None, None


def validate_inquiry(p: InboundPayload) -> str | None:
    """Return a rejection reason, or None if the inquiry looks legitimate."""
    if p.hp:
        return "Spam: honeypot field filled"
    msg = (p.message or "").strip()
    if len(msg) < 5:
        return "Message is empty"
    if len(msg) > 5000:
        return "Message too long"
    if msg.lower().count("http") > 5:
        return "Spam: too many links"
    if p.email and not EMAIL_RE.match(p.email.strip()):
        return "Invalid email address"
    if not p.email and not p.phone:
        return "No way to reply (email or phone required)"
    if not (p.company or p.email):
        return "Company or email required"
    return None


def record_inquiry(session: Session, payload: dict, channel: str) -> tuple[InboundInquiry | None, bool]:
    """Store a raw inquiry once (deduplicated by email+company+message). Returns (row, created)."""
    p = InboundPayload.model_validate(payload)
    fp = fingerprint(p.email, p.company, p.message)
    existing = session.execute(select(InboundInquiry).where(InboundInquiry.fingerprint == fp)).scalars().first()
    if existing:
        return existing, False
    row = InboundInquiry(channel=channel, payload=p.model_dump(), fingerprint=fp, is_sample=p.is_sample)
    session.add(row)
    session.flush()
    return row, True


def load_inbox_files(session: Session, inbox: Path) -> int:
    """Read *.json / *.csv files from the inbox folder into inbound_inquiries, then archive them."""
    count = 0
    processed = inbox / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    for path in sorted(list(inbox.glob("*.json")) + list(inbox.glob("*.csv"))):
        records: list[dict] = []
        if path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            records = data if isinstance(data, list) else [data]
        else:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                records = [{(k or "").strip().lower(): v for k, v in row.items()} for row in csv.DictReader(fh)]
        for rec in records:
            _, created = record_inquiry(session, rec, "inbox_file")
            count += int(created)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.move(str(path), processed / f"{stamp}_{path.name}")
    return count
