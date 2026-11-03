"""The tools a language model may call, for one lead at a time.

Two kinds only:
- read tools: CRM queries about this conversation's lead (the lead, its company, the latest research snapshot, the thread,
  the approved claims, free calendar slots). Every read is scoped to the registry's lead: asking for another lead is
  refused, so no customer's records reach another customer's conversation. Personal contact details (email addresses,
  phone numbers) are left out: the model needs a first name and a job title, not a way to reach anyone.
- proposal tools: propose_reply, propose_field_update, propose_meeting, propose_handover. They validate the proposal and
  add it to `registry.proposals` for the policy engine. They return "recorded for review"; they never act.

There is no tool that sends, books, publishes, changes a setting or writes a record, and the registry checks that: a
tool runs with autoflush off, and one that leaves anything new, changed or deleted in the session raises ToolWriteRefused
and its change is discarded (the caller's own pending work is left as it was). Calendar free slots are a stub in this phase (no calendar is read): it returns the slots given to the
registry, or none, with a note that a person picks the times."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.ai import claims as claims_mod
from aime.ai.schemas import PROPOSAL
from aime.models import Company, FollowUp, InboundEmail, Lead, ResearchSnapshot

TOOL_KINDS = ("read", "proposal")


class ToolError(Exception):
    pass


class ToolWriteRefused(ToolError):
    pass


class NoArgs(BaseModel):
    lead_id: int | None = None  # optional: if given it must be this conversation's lead


class HistoryArgs(NoArgs):
    limit: int = Field(10, ge=1, le=30)


class SlotArgs(NoArgs):
    days: int = Field(5, ge=1, le=14)


class ReplyArgs(BaseModel):
    body: str
    claims_used: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class FieldArgs(BaseModel):
    field: str
    value: str | bool | None = None
    evidence: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class MeetingArgs(BaseModel):
    slots: list[str] = Field(default_factory=list)
    message: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class HandoverArgs(BaseModel):
    reason: str
    summary: str = ""
    confidence: float = Field(0.9, ge=0.0, le=1.0)
    rationale: str = ""


@dataclass(frozen=True)
class Tool:
    name: str
    kind: Literal["read", "proposal"]
    description: str
    params: type[BaseModel]
    fn: Callable[..., Any]


@dataclass
class ToolRegistry:
    session: Session
    settings: Any
    lead_id: int
    free_slots: list[str] = field(default_factory=list)  # the stub's answer: offered times, ISO or as a person wrote them
    proposals: list[dict] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    tools: dict[str, Tool] = field(default_factory=dict)

    def __post_init__(self):
        for t in (
            Tool("get_lead", "read", "This lead: status, grade, service of interest, the contact's first name and title.", NoArgs, self._lead),
            Tool("get_company", "read", "The lead's company: name, industry, state, size, its own description.", NoArgs, self._company),
            Tool("get_research_snapshot", "read", "The latest reading of the company's own website: what it does and needs.",
                 NoArgs, self._research),
            Tool("get_thread_history", "read", "The emails in this conversation, oldest first.", HistoryArgs, self._history),
            Tool("get_approved_claims", "read", "What we may say about ourselves, and the topics we never raise.", NoArgs,
                 self._claims),
            Tool("get_calendar_free_slots", "read", "Times a meeting could be offered (a person confirms them).", SlotArgs,
                 self._slots),
            Tool("propose_reply", "proposal", "Propose an email answer for a person to review. Nothing is sent.", ReplyArgs,
                 self._propose("reply")),
            Tool("propose_field_update", "proposal", "Propose one internal fact (requirements, budget, timeline, objection, "
                 "meeting_intent, summary, next_step, conversation_state).", FieldArgs, self._propose("field_update")),
            Tool("propose_meeting", "proposal", "Propose meeting times from get_calendar_free_slots. Nothing is booked.",
                 MeetingArgs, self._propose("meeting")),
            Tool("propose_handover", "proposal", "Propose that a person takes this conversation over.", HandoverArgs,
                 self._propose("handover")),
        ):
            assert t.kind in TOOL_KINDS
            self.tools[t.name] = t

    # ------------------------------------------------------------------ the interface aime.llm.structured uses
    def describe(self) -> list[dict]:
        out = []
        for t in self.tools.values():
            props = {k: {kk: vv for kk, vv in v.items() if kk in ("type", "description", "items", "anyOf", "enum")}
                     for k, v in t.params.model_json_schema().get("properties", {}).items() if k != "lead_id"}
            out.append({"name": t.name, "kind": t.kind, "description": t.description, "arguments": props})
        return out

    def call(self, name: str, arguments: dict | None = None) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError(f"no tool named {name!r}; the tools are: {', '.join(self.tools)}")
        try:
            args = tool.params.model_validate(arguments or {})
        except ValidationError as exc:
            raise ToolError("bad arguments: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                                                          for e in exc.errors()[:4])) from exc
        if getattr(args, "lead_id", None) not in (None, self.lead_id):
            raise ToolError(f"this conversation is about lead {self.lead_id} only")
        self.calls.append(name)
        s = self.session
        before = {id(o) for o in (*s.new, *s.dirty, *s.deleted)}  # the caller's own pending work is left alone
        with s.no_autoflush:  # nothing a tool touches reaches the database while it runs
            result = tool.fn(args)
            extra = [o for o in (*s.new, *s.dirty, *s.deleted) if id(o) not in before]
            if extra:
                for o in extra:
                    if o in s.new or o in s.deleted:
                        s.expunge(o)
                    else:
                        s.expire(o)
                raise ToolWriteRefused(f"the tool {name} tried to change the database; the change was undone")
        return result

    # ------------------------------------------------------------------ read tools
    def _lead_row(self) -> Lead:
        lead = self.session.get(Lead, self.lead_id)
        if lead is None:
            raise ToolError(f"lead {self.lead_id} does not exist")
        return lead

    def _lead(self, _args) -> dict:
        lead = self._lead_row()
        c = lead.contact
        return {"lead_id": lead.id, "status": lead.status, "outreach_status": lead.outreach_status, "grade": lead.grade,
                "score": lead.score, "primary_service": lead.primary_service, "matched_services": list(lead.matched_services or []),
                "contact_first_name": getattr(c, "first_name", None), "contact_title": getattr(c, "title", None),
                "company": lead.company.name if lead.company else None}

    def _company(self, _args) -> dict:
        co: Company | None = self._lead_row().company
        if co is None:
            return {}
        return {"name": co.name, "industry": co.industry, "state": co.state, "city": co.city, "employee_range": co.employee_range,
                "description": (co.description or "")[:800], "website": co.website}

    def _research(self, _args) -> dict:
        co_id = self._lead_row().company_id
        snap = self.session.execute(select(ResearchSnapshot).where(ResearchSnapshot.company_id == co_id)
                                    .order_by(ResearchSnapshot.id.desc()).limit(1)).scalars().first()
        if snap is None:
            return {"found": False}
        return {"found": True, "status": snap.status, "description": (snap.description or "")[:800],
                "services_offered": list(snap.services_offered or [])[:10],
                "needs": [{"service": n.get("service"), "quote": (n.get("quote") or "")[:300]} for n in (snap.needs or [])[:6]
                          if isinstance(n, dict)],
                "summary": (snap.summary or "")[:600]}

    def _history(self, args: HistoryArgs) -> list[dict]:
        ours = self.session.execute(select(FollowUp).where(FollowUp.lead_id == self.lead_id, FollowUp.channel == "email",
                                                           FollowUp.status == "sent")).scalars().all()
        theirs = self.session.execute(select(InboundEmail).where(InboundEmail.lead_id == self.lead_id)).scalars().all()
        rows = [(f.sent_at or f.created_at, "us", f.subject, f.body or "") for f in ours]
        rows += [(r.received_at, "prospect", r.subject, r.new_text or r.body_text or "") for r in theirs
                 if r.category not in ("bounce",)]
        rows.sort(key=lambda r: r[0])
        return [{"from": who, "at": at.isoformat() if at else None, "subject": (subj or "")[:200], "text": (text or "")[:1500]}
                for at, who, subj, text in rows[-args.limit:]]

    def _claims(self, _args) -> dict:
        claims, topics = claims_mod.load(self.settings, self.session)
        return {"approved_claims": [c.text for c in claims], "never_raise": [label for label, _ in topics.values()]}

    def _slots(self, args: SlotArgs) -> dict:
        return {"slots": list(self.free_slots), "connected": False,
                "note": "No calendar is read in this phase: these are the times a person made available, or none; "
                        "a person confirms any time before it is offered."}

    # ------------------------------------------------------------------ proposal tools
    def _propose(self, kind: str):
        def record(args: BaseModel) -> dict:
            raw = {"kind": kind, "lead_id": self.lead_id, **args.model_dump()}
            try:
                PROPOSAL.validate_python(raw)
            except ValidationError as exc:
                raise ToolError("the proposal is not valid: " + "; ".join(e["msg"] for e in exc.errors()[:3])) from exc
            self.proposals.append(raw)
            return {"recorded": True, "number": len(self.proposals),
                    "note": "recorded for review: the policy engine and a person decide; nothing was sent or booked"}

        return record
