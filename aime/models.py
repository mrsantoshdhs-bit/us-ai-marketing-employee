"""SQLite data model for the CRM, content pipeline, and agent operations."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from aime.timeutil import utcnow


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- CRM


class Company(Timestamped, Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200), index=True)
    domain: Mapped[str | None] = mapped_column(String(200), index=True)
    website: Mapped[str | None] = mapped_column(String(300))
    industry: Mapped[str | None] = mapped_column(String(50), index=True)
    state: Mapped[str | None] = mapped_column(String(2), index=True)
    city: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str] = mapped_column(String(2), default="US")
    location_text: Mapped[str | None] = mapped_column(String(300))
    employee_range: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(40))  # only from official filings / the company itself
    source: Mapped[str] = mapped_column(String(50))
    source_url: Mapped[str | None] = mapped_column(String(500))
    source_record_id: Mapped[str | None] = mapped_column(String(120), index=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_competitor: Mapped[bool] = mapped_column(Boolean, default=False)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    lifecycle_stage: Mapped[str] = mapped_column(String(30), default="prospect")
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    # verified | redirected | mismatch | parked | unreachable | blocked | robots_disallowed | None (unchecked)
    website_status: Mapped[str | None] = mapped_column(String(30))
    website_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    website_title: Mapped[str | None] = mapped_column(String(300))
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    # Reading the company's own pages (aime.company_research): done | no_people | no_website | blocked | failed
    research_status: Mapped[str | None] = mapped_column(String(20))
    researched_at: Mapped[datetime | None] = mapped_column(DateTime)

    signals: Mapped[list["Signal"]] = relationship(back_populates="company", cascade="all")
    leads: Mapped[list["Lead"]] = relationship(back_populates="company")
    contacts: Mapped[list["Contact"]] = relationship(back_populates="company")


class Signal(Base):
    """Evidence of a business requirement, always traceable to a source."""

    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("company_id", "fingerprint", name="uq_signal_company_fp"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    signal_type: Mapped[str] = mapped_column(String(30))  # hiring | inbound | filing | import | manual
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(50))
    source_url: Mapped[str | None] = mapped_column(String(500))
    fingerprint: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    matched_services: Mapped[list[Any]] = mapped_column(JSON, default=list)
    keywords: Mapped[list[Any]] = mapped_column(JSON, default=list)
    strength: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    company: Mapped[Company] = relationship(back_populates="signals")


class ResearchSnapshot(Base):
    """One reading of a company's own website (and SEC filings): what it says it does, the services it
    lists, where it may need our services (with quotes), and the people and business contacts it publishes."""

    __tablename__ = "research_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    status: Mapped[str] = mapped_column(String(20))  # done | no_people | no_website | blocked | failed
    pages: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{url, kind, status, title}]
    description: Mapped[str | None] = mapped_column(Text)  # the company's own words, quoted
    description_url: Mapped[str | None] = mapped_column(String(500))
    services_offered: Mapped[list[Any]] = mapped_column(JSON, default=list)  # headings from its services pages
    needs: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{service, keywords, quote, url}]
    emails: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{email, url, near}] as published
    phones: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{phone, url}] as published
    people: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{name, title, confidence, url}]
    summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(String(300))

    company: Mapped[Company] = relationship()


class Contact(Timestamped, Base):
    """People records come from inbound inquiries, manual entry, user-provided imports, or (source "website" /
    "sec_filing") the company's own published pages and SEC filings, always with the page or filing as
    evidence. Names, titles and emails are never guessed."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), index=True)
    first_name: Mapped[str | None] = mapped_column(String(80))
    last_name: Mapped[str | None] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(200), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    title: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(50))
    # inquiry = may reply about their inquiry; opt_in = marketing consent; opted_out = never contact
    consent_status: Mapped[str] = mapped_column(String(20), default="none")
    consent_source: Mapped[str | None] = mapped_column(String(200))
    consent_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    # Decision-maker research (sources "website" and "sec_filing"):
    role_category: Mapped[str | None] = mapped_column(String(20))  # executive | operations | technology | sales | ...
    relevance: Mapped[int | None] = mapped_column(Integer)  # 0-100: how well the role fits who buys our services
    confidence: Mapped[str | None] = mapped_column(String(10))  # high | medium
    evidence: Mapped[str | None] = mapped_column(Text)  # the published text the name and title come from
    evidence_url: Mapped[str | None] = mapped_column(String(500))
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Email provenance and checks (aime.contact_validation): published | role_address | no_mail_server |
    # unchecked | bounced | None (no email)
    email_status: Mapped[str | None] = mapped_column(String(20))
    email_source_url: Mapped[str | None] = mapped_column(String(500))
    email_confidence: Mapped[str | None] = mapped_column(String(10))  # high: next to the name | medium: name match
    email_evidence: Mapped[str | None] = mapped_column(Text)
    email_checked_at: Mapped[datetime | None] = mapped_column(DateTime)

    company: Mapped[Company | None] = relationship(back_populates="contacts")

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or (self.email or "Unknown contact")


class Lead(Timestamped, Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    source: Mapped[str] = mapped_column(String(30))  # research | inbound | import | manual
    # new | qualified | nurture | disqualified | converted | merged
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    grade: Mapped[str | None] = mapped_column(String(1))
    fit_score: Mapped[int] = mapped_column(Integer, default=0)
    need_score: Mapped[int] = mapped_column(Integer, default=0)
    intent_score: Mapped[int] = mapped_column(Integer, default=0)
    data_score: Mapped[int] = mapped_column(Integer, default=0)
    score_breakdown: Mapped[list[Any]] = mapped_column(JSON, default=list)
    matched_services: Mapped[list[Any]] = mapped_column(JSON, default=list)
    primary_service: Mapped[str | None] = mapped_column(String(40))
    category: Mapped[str | None] = mapped_column(String(60), index=True)  # e.g. "warm · AI & Agents"
    disqualify_reason: Mapped[str | None] = mapped_column(String(200))
    last_scored_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Outbound pipeline (aime.outreach): drafted | approved | contacted | replied | interested | meeting |
    # not_interested | no_response | unsubscribed | bounced | stopped | None (not in outreach)
    outreach_status: Mapped[str | None] = mapped_column(String(20), index=True)
    outreach_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    owner: Mapped[str | None] = mapped_column(String(80))  # the Sales Manager who approved its first outreach
    notes: Mapped[str | None] = mapped_column(Text)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    company: Mapped[Company] = relationship(back_populates="leads")
    contact: Mapped[Contact | None] = relationship()
    opportunities: Mapped[list["Opportunity"]] = relationship(back_populates="lead")


class Opportunity(Timestamped, Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(250))
    service: Mapped[str | None] = mapped_column(String(40))
    # new | discovery | proposal | negotiation | won | lost
    stage: Mapped[str] = mapped_column(String(20), default="new", index=True)
    est_value: Mapped[float | None] = mapped_column(Float)  # only when a human enters it
    probability: Mapped[int] = mapped_column(Integer, default=10)
    next_step: Mapped[str | None] = mapped_column(String(300))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    lost_reason: Mapped[str | None] = mapped_column(String(200))

    lead: Mapped[Lead | None] = relationship(back_populates="opportunities")
    company: Mapped[Company] = relationship()


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"))
    type: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class FollowUp(Timestamped, Base):
    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    channel: Mapped[str] = mapped_column(String(20))  # email | task
    step: Mapped[int] = mapped_column(Integer, default=1)
    due_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # pending | awaiting_approval | approved | sending | sent | uncertain | done | cancelled | rejected
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    blocked_reason: Mapped[str | None] = mapped_column(String(300))
    approved_by: Mapped[str | None] = mapped_column(String(80))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    message_id: Mapped[str | None] = mapped_column(String(200))
    # outreach | response (reply to a prospect's email, aime.inbox) | None (reply to someone who contacted us)
    kind: Mapped[str | None] = mapped_column(String(20))
    in_reply_to: Mapped[str | None] = mapped_column(String(300))  # Message-ID of the email this answers
    delivered_to: Mapped[str | None] = mapped_column(String(200))  # actual recipient (test mode redirects)
    to_email: Mapped[str | None] = mapped_column(String(200))  # the address the approval applies to
    evidence: Mapped[list[Any] | None] = mapped_column(JSON, default=list)  # [{fact, url, kind}] used in the email
    quality: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)  # email quality-check report

    lead: Mapped[Lead] = relationship()
    contact: Mapped[Contact | None] = relationship()


class Meeting(Timestamped, Base):
    """A booked meeting: from a Calendly notification in the sales mailbox (aime.meetings) or logged by hand."""

    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"))
    invitee_email: Mapped[str] = mapped_column(String(200), index=True)
    invitee_name: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200), default="Meeting")
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    timezone: Mapped[str | None] = mapped_column(String(60))
    source: Mapped[str] = mapped_column(String(20))  # calendly_email | manual
    status: Mapped[str] = mapped_column(String(20), default="scheduled", index=True)  # scheduled | cancelled | completed | no_show
    notes: Mapped[str | None] = mapped_column(Text)
    inbound_email_id: Mapped[int | None] = mapped_column(Integer)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)

    lead: Mapped["Lead | None"] = relationship()


class InboundEmail(Base):
    """An email that arrived in the monitored sales mailbox (or a clearly labelled simulated one), with its
    classification, the lead it was matched to, and what was done about it."""

    __tablename__ = "inbound_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_uid: Mapped[str | None] = mapped_column(String(60))  # "<uidvalidity>:<uid>"
    message_id: Mapped[str | None] = mapped_column(String(300), unique=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(300))
    references: Mapped[str | None] = mapped_column(Text)
    from_email: Mapped[str] = mapped_column(String(200), index=True)
    from_name: Mapped[str | None] = mapped_column(String(200))
    to_email: Mapped[str | None] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(300), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    body_text: Mapped[str | None] = mapped_column(Text)
    new_text: Mapped[str | None] = mapped_column(Text)  # the reply without the quoted earlier message
    auto_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    bounced_addresses: Mapped[list[Any] | None] = mapped_column(JSON, default=list)
    # interested | not_interested | objection | referral | question | unsubscribe | out_of_office | bounce |
    # calendly | other
    category: Mapped[str | None] = mapped_column(String(30), index=True)
    confidence: Mapped[str | None] = mapped_column(String(10))  # high | medium | low
    reasons: Mapped[list[Any] | None] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new | actioned | needs_review | reviewed | ignored
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    followup_id: Mapped[int | None] = mapped_column(ForeignKey("followups.id"))
    suggested_followup_id: Mapped[int | None] = mapped_column(Integer)
    action_note: Mapped[str | None] = mapped_column(Text)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)  # created by `aime simulate-reply`
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)

    lead: Mapped["Lead | None"] = relationship()
    contact: Mapped["Contact | None"] = relationship()


class Suppression(Base):
    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(10))  # email | domain
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InboundInquiry(Base):
    __tablename__ = "inbound_inquiries"

    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    channel: Mapped[str] = mapped_column(String(30))  # api | web_form | inbox_file | manual
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending | processed | rejected
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    error: Mapped[str | None] = mapped_column(String(300))
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------- Marketing


class Campaign(Timestamped, Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    objective: Mapped[str] = mapped_column(String(300))
    service: Mapped[str | None] = mapped_column(String(40))
    industry: Mapped[str | None] = mapped_column(String(50))
    states: Mapped[list[Any]] = mapped_column(JSON, default=list)
    channel: Mapped[str] = mapped_column(String(30), default="linkedin")
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="active")  # planned | active | paused | completed
    kpis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # targets, never reported as results
    created_by: Mapped[str] = mapped_column(String(60), default="marketing_manager")


class ContentItem(Timestamped, Base):
    __tablename__ = "content_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    platform: Mapped[str] = mapped_column(String(20), default="linkedin")
    service: Mapped[str] = mapped_column(String(40))
    industry: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(250))
    body: Mapped[str] = mapped_column(Text)
    hashtags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    image_path: Mapped[str | None] = mapped_column(String(400))
    image_alt: Mapped[str | None] = mapped_column(String(300))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    # draft | awaiting_approval | approved | publishing | uncertain | published | failed | rejected
    # "publishing" is a durable claim written before the API call; "uncertain" means the outcome
    # is unknown (timeout/crash) and the post is never retried automatically.
    status: Mapped[str] = mapped_column(String(20), default="awaiting_approval", index=True)
    publish_attempt_id: Mapped[str | None] = mapped_column(String(40))
    publish_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    image_urn: Mapped[str | None] = mapped_column(String(200))
    reconciled_via: Mapped[str | None] = mapped_column(String(40))
    generator: Mapped[str] = mapped_column(String(60))
    quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(120), index=True)
    publish_blocked_reason: Mapped[str | None] = mapped_column(String(300))
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_publish_error: Mapped[str | None] = mapped_column(String(500))
    external_id: Mapped[str | None] = mapped_column(String(200))
    external_url: Mapped[str | None] = mapped_column(String(400))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    approved_by: Mapped[str | None] = mapped_column(String(80))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def full_text(self) -> str:
        tags = " ".join(self.hashtags or [])
        return f"{self.body}\n\n{tags}".strip()

    @property
    def qc(self) -> dict:
        """Latest automated quality-check report (see aime.qc), stored in `quality`."""
        return (self.quality or {}).get("qc") or {}

    @property
    def qc_status(self) -> str:
        return self.qc.get("status", "not checked")


class PublishAttempt(Base):
    """Audit trail of every call to the publishing API (one row per attempt)."""

    __tablename__ = "publish_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(40), unique=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("content_items.id"), index=True)
    author_urn: Mapped[str | None] = mapped_column(String(120))
    commentary_sha256: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    # pending | success | failed | uncertain | not_sent | reconciled
    outcome: Mapped[str] = mapped_column(String(20), default="pending")
    http_status: Mapped[int | None] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)


class PolicyDecision(Base):
    """What the auto-publishing policy would have done with a post (shadow mode: it never acts).

    A row is written when a post is first evaluated and again whenever the decision or the post's
    content changes, so the history shows how the policy compared with the person's decision."""

    __tablename__ = "policy_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("content_items.id"), index=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="shadow")
    policy_version: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(20))  # would_publish | would_hold
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)  # rules that held the post
    rules: Mapped[list[Any]] = mapped_column(JSON, default=list)  # every rule with its result
    blockers: Mapped[list[Any]] = mapped_column(JSON, default=list)  # environment: switch off, not connected
    content_sha256: Mapped[str] = mapped_column(String(64))
    post_status: Mapped[str] = mapped_column(String(20))  # the post's status when evaluated

    content: Mapped[ContentItem] = relationship()


class ContentMetric(Base):
    __tablename__ = "content_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int | None] = mapped_column(ForeignKey("content_items.id"), index=True)
    external_ref: Mapped[str | None] = mapped_column(String(400))
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    reactions: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(30))  # csv_import | api
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ResearchTopic(Base):
    __tablename__ = "research_topics"
    __table_args__ = (UniqueConstraint("source_url", name="uq_topic_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(300))
    service: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(50))
    source_url: Mapped[str] = mapped_column(String(500))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MarketSegment(Base):
    __tablename__ = "market_segments"
    __table_args__ = (UniqueConstraint("state", "naics", "year", name="uq_segment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(2))
    naics: Mapped[str] = mapped_column(String(10))
    industry: Mapped[str] = mapped_column(String(50))
    label: Mapped[str | None] = mapped_column(String(200))
    establishments: Mapped[int] = mapped_column(Integer, default=0)
    employees: Mapped[int | None] = mapped_column(Integer)
    year: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(50), default="census_cbp")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MarketingPlan(Base):
    __tablename__ = "marketing_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_date: Mapped[date] = mapped_column(Date, index=True)
    focus_industries: Mapped[list[Any]] = mapped_column(JSON, default=list)
    focus_states: Mapped[list[Any]] = mapped_column(JSON, default=list)
    briefs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    rationale: Mapped[list[Any]] = mapped_column(JSON, default=list)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- Operations


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    agent: Mapped[str] = mapped_column(String(60), index=True)
    job: Mapped[str | None] = mapped_column(String(60))
    job_run_id: Mapped[int | None] = mapped_column(Integer, index=True)  # correlates with job_runs.id
    status: Mapped[str] = mapped_column(String(20))  # running | success | failed | abandoned
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    input_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(60), index=True)
    trigger: Mapped[str] = mapped_column(String(20))  # scheduler | manual | dashboard | cli
    status: Mapped[str] = mapped_column(String(20))  # running | success | partial | failed | skipped | abandoned
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class JobState(Base):
    __tablename__ = "job_state"

    job: Mapped[str] = mapped_column(String(60), primary_key=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(20))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(10))  # info | warning | error
    source: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="daily")  # daily | weekly
    path_md: Mapped[str] = mapped_column(String(400))
    path_html: Mapped[str] = mapped_column(String(400))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), unique=True)
    access_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    scope: Mapped[str] = mapped_column(String(300), default="")
    subject: Mapped[str | None] = mapped_column(String(120))  # e.g. LinkedIn member id
    subject_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Timestamped, Base):
    """Dashboard login. Accounts are created from the command line (`aime user create`), never from the web."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))  # scrypt$n$r$p$salt$hash
    role: Mapped[str] = mapped_column(String(30), default="sales_manager")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)  # bump to sign out every session
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class KeyValue(Base):
    __tablename__ = "kv_store"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
