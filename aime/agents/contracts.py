"""Shared contract models passed between agents."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ContentBrief(BaseModel):
    service: str
    industry: str
    angle: int = 0
    pain: int = 0
    cta: int = 0
    campaign_id: int | None = None
    reason: str = ""


class SourceReport(BaseModel):
    name: str
    configured: bool
    ok: bool
    records: int = 0
    message: str = ""


class BlockedItem(BaseModel):
    id: int
    reason: str


class Counter(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
