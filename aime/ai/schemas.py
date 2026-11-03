"""The typed answers a language model may give the AI core: an analysis of a prospect's reply, and proposals.

A proposal is only ever a suggestion for the policy engine (aime.ai.policy). The kinds are closed: a reply draft, an
internal field update, a meeting offer, a handover to a person, an internal note. There is no kind that sends, books,
publishes or changes a setting, so a model that asks for one fails validation and its proposal is blocked.

Small local models write "null", "N/A" or "none" for an empty field and overlong text: the validators here read those
as empty and shorten text, so a sensible answer is not lost to a formality; anything else that does not fit is a
validation error."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, field_validator

INTENTS = ("interested", "not_interested", "question", "objection", "referral", "unsubscribe", "complaint",
           "out_of_office", "other")
OBJECTIONS = ("none", "price", "timing", "competitor", "no_need", "authority", "trust", "other")
_EMPTY = {"", "none", "null", "n/a", "na", "not mentioned", "not specified", "unknown", "not stated", "no", "-"}


def _empty_to_none(v: Any) -> Any:
    if isinstance(v, str) and v.strip().lower().strip(".") in _EMPTY:
        return None
    return v


def _short(v: Any, limit: int) -> Any:
    return v[:limit] if isinstance(v, str) else v


class ReplyAnalysis(BaseModel):
    """What a prospect's email says, as the model reads it. The rule floor (aime.reply_intel) is read alongside and wins."""

    intent: Literal[INTENTS]
    meeting_intent: bool = False
    requirements: list[str] = Field(default_factory=list)
    budget: str | None = None
    timeline: str | None = None
    objection: Literal[OBJECTIONS] = "none"
    upset: bool = False
    asks_for_human: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = ""

    @field_validator("intent", mode="before")
    @classmethod
    def _intent(cls, v):
        return v.strip().lower().replace(" ", "_").replace("-", "_") if isinstance(v, str) else v

    @field_validator("objection", mode="before")
    @classmethod
    def _objection(cls, v):
        v = _empty_to_none(v)
        return "none" if v is None else (v.strip().lower().replace(" ", "_") if isinstance(v, str) else v)

    @field_validator("budget", "timeline", mode="before")
    @classmethod
    def _optional_text(cls, v):
        v = _empty_to_none(v)
        return _short(str(v), 200) if v is not None and not isinstance(v, str) else _short(v, 200)

    @field_validator("requirements", mode="before")
    @classmethod
    def _requirements(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        return [_short(str(x), 200) for x in v if _empty_to_none(x) is not None][:8] if isinstance(v, list) else v

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v):
        return _short(v or "", 500)


class _Proposal(BaseModel):
    lead_id: int
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""

    @field_validator("rationale", mode="before")
    @classmethod
    def _rationale(cls, v):
        return _short(v or "", 500)


class ReplyDraft(_Proposal):
    """An answer to the prospect, for a person to read, edit and approve. Never sent from here."""

    kind: Literal["reply"] = "reply"
    body: str = Field(min_length=1, max_length=4000)
    claims_used: list[str] = Field(default_factory=list)


class FieldUpdate(_Proposal):
    """One internal fact about the conversation (aime.ai.policy.AI_WRITABLE_FIELDS), reversible and logged."""

    kind: Literal["field_update"] = "field_update"
    field: str
    value: str | bool | None = None
    evidence: str = ""


class MeetingOffer(_Proposal):
    """Times to offer the prospect. A person approves; booking stays with the existing DKIM-verified Calendly flow."""

    kind: Literal["meeting"] = "meeting"
    slots: list[str] = Field(default_factory=list, max_length=5)
    message: str = ""


class Handover(_Proposal):
    """Hand the conversation to a person (a Sales Manager)."""

    kind: Literal["handover"] = "handover"
    reason: str
    summary: str = ""


class InternalNote(_Proposal):
    kind: Literal["internal_note"] = "internal_note"
    text: str = Field(min_length=1, max_length=2000)


Proposal = Annotated[Union[ReplyDraft, FieldUpdate, MeetingOffer, Handover, InternalNote], Field(discriminator="kind")]
PROPOSAL = TypeAdapter(Proposal)
KINDS = ("reply", "field_update", "meeting", "handover", "internal_note")
EXTERNAL_KINDS = frozenset({"reply", "meeting"})  # a prospect would see it
INTERNAL_KINDS = frozenset({"field_update", "internal_note"})  # only our CRM, reversible


def proposal_text(p: BaseModel) -> str:
    """The words in a proposal that someone would read: what the content checks look at."""
    parts = [getattr(p, a, "") for a in ("body", "message", "text", "summary", "reason")]
    if isinstance(getattr(p, "value", None), str):
        parts.append(p.value)
    return "\n".join(x for x in parts if isinstance(x, str) and x)
