"""Focused regressions for the defects the Option B end-to-end layer found (tests/e2e_ob/test_full_workflow.py and
test_reliability.py hold the scenarios that found them).

E2E-1: a process killed between the rules' commit and the conversation engine's turn lost that turn for good (no state,
       no facts, no escalation, no handover request); the pending turn is now marked in the rules' own commit and taken
       at the next run, once.
E2E-2: the Automation Control Center state was read once at the start of a sending run, so an emergency stop (or a pause)
       pressed while the run was between two emails let the rest of the run go out; it is now read before each email."""

from __future__ import annotations


import pytest
from sqlalchemy import select

from aime import reply_intel
from aime.db import kv_get
from aime.models import AIDecision, Conversation, InboundEmail
from tests.test_inbox import DAVID, FakeIMAP, contacted, mail, run_inbox


def _drafts_mode(ctx):
    from aime import settings_store as ss
    with ctx.db.session() as s:
        ss.save(s, ctx.settings, "conversation", {**ss.get(ctx.settings, "conversation"), "mode": "drafts"}, "owner")


def _turns(ctx, row_id):
    with ctx.db.session() as s:
        return s.execute(select(AIDecision).where(AIDecision.task == "conversation_turn",
                                                  AIDecision.input_ref == f"inbound_email:{row_id}")).scalars().all()


def test_e2e1_a_turn_cut_off_after_the_rules_commit_is_taken_once_at_the_next_run(make_ctx, monkeypatch):
    ctx, _sender, first = contacted(make_ctx)
    _drafts_mode(ctx)
    real = reply_intel._conversation_turn
    monkeypatch.setattr(reply_intel, "_conversation_turn", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {first.subject}", "Sounds interesting. We want to automate invoice entry.",
                                 in_reply_to=first.message_id)}
    with pytest.raises(KeyboardInterrupt):
        run_inbox(ctx)
    with ctx.db.session() as s:
        row = s.execute(select(InboundEmail)).scalar_one()
        assert row.status == "actioned" and kv_get(s, reply_intel.PENDING_TURNS) == [row.id]
    assert _turns(ctx, row.id) == []
    monkeypatch.setattr(reply_intel, "_conversation_turn", real)
    run_inbox(ctx)
    run_inbox(ctx)
    assert len(_turns(ctx, row.id)) == 1
    with ctx.db.session() as s:
        assert s.execute(select(Conversation)).scalar_one() is not None
        assert kv_get(s, reply_intel.PENDING_TURNS) == []


def test_e2e1_an_ordinary_turn_leaves_no_mark_and_off_marks_nothing(make_ctx):
    ctx, _sender, first = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {first.subject}", "Sounds interesting.", in_reply_to=first.message_id)}
    run_inbox(ctx)  # conversation off (the default): nothing is marked, nothing taken
    with ctx.db.session() as s:
        assert not kv_get(s, reply_intel.PENDING_TURNS)
    _drafts_mode(ctx)
    FakeIMAP.messages[2] = mail(f"David Chen <{DAVID}>", f"Re: {first.subject}", "Also, could we talk Thursday?",
                                in_reply_to=first.message_id, msg_id="<second@x.example>")
    run_inbox(ctx)
    with ctx.db.session() as s:
        assert kv_get(s, reply_intel.PENDING_TURNS) == []
        second = s.execute(select(InboundEmail).order_by(InboundEmail.id.desc())).scalars().first()
    assert len(_turns(ctx, second.id)) == 1


def test_e2e1_an_engine_failure_is_final_and_not_retried(make_ctx, monkeypatch):
    from aime.ai import conversation

    ctx, _sender, first = contacted(make_ctx)
    _drafts_mode(ctx)
    calls = {"n": 0}

    def fails(*a, **k):
        calls["n"] += 1
        raise RuntimeError("engine bug (fake)")
    monkeypatch.setattr(conversation, "on_reply", fails)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {first.subject}", "Sounds interesting.", in_reply_to=first.message_id)}
    run_inbox(ctx)
    run_inbox(ctx)
    assert calls["n"] == 1  # the rules' outcome stands; the failed turn is not tried at every run
    with ctx.db.session() as s:
        assert kv_get(s, reply_intel.PENDING_TURNS) == []
