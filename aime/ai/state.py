"""The per-lead conversation state machine.

    new -> contacted -> engaged -> discovering -> qualified -> scheduling -> handed_over
                         \\-> nurture / closed_lost (from most states)      opted_out (rules only, from any state)

Who may move a lead: the AI may only propose a move (a FieldUpdate of `conversation_state`), and the policy engine
accepts it only along the arrows below. `opted_out` is set by the rule floor alone (an opt-out, a complaint, a
suppression) and nothing leaves it here: lifting an opt-out stays a person's act on the suppression list. Moving out
of `handed_over` or `closed_lost` is a person's decision. Until a conversations table exists (Phase 3) the current
state is derived from the lead's outreach status (`state_of_lead`); the AI decision log keeps every proposed move."""

from __future__ import annotations

STATES = ("new", "contacted", "engaged", "discovering", "qualified", "scheduling", "handed_over", "closed_lost", "nurture",
          "opted_out")
TERMINAL_FOR_AI = frozenset({"handed_over", "closed_lost", "opted_out"})  # the AI proposes no move out of these
RULES_ONLY = frozenset({"opted_out"})
PERSON_ONLY = frozenset({"handed_over"})  # the AI proposes a Handover; a person accepting it moves the lead

TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"contacted", "engaged", "closed_lost", "opted_out"}),
    "contacted": frozenset({"engaged", "nurture", "closed_lost", "opted_out"}),
    "engaged": frozenset({"discovering", "scheduling", "handed_over", "nurture", "closed_lost", "opted_out"}),
    "discovering": frozenset({"qualified", "scheduling", "handed_over", "nurture", "closed_lost", "opted_out"}),
    "qualified": frozenset({"scheduling", "handed_over", "nurture", "closed_lost", "opted_out"}),
    "scheduling": frozenset({"discovering", "handed_over", "nurture", "closed_lost", "opted_out"}),
    "nurture": frozenset({"contacted", "engaged", "closed_lost", "opted_out"}),
    "handed_over": frozenset({"closed_lost", "opted_out"}),
    "closed_lost": frozenset({"nurture", "opted_out"}),
    "opted_out": frozenset(),
}

_FROM_OUTREACH = {None: "new", "drafted": "new", "approved": "new", "contacted": "contacted", "no_response": "nurture",
                  "replied": "engaged", "interested": "discovering", "meeting": "scheduling", "not_interested": "closed_lost",
                  "unsubscribed": "opted_out", "bounced": "closed_lost", "stopped": "closed_lost"}


def state_of_lead(lead) -> str:
    if lead is None:
        return "new"
    if getattr(lead, "status", None) == "converted":
        return "handed_over"
    return _FROM_OUTREACH.get(getattr(lead, "outreach_status", None), "engaged")


def check_transition(current: str, target: str, actor: str = "ai") -> tuple[bool, str]:
    """(allowed, why). `actor` is "ai", "rules" or "person"."""
    if target not in STATES:
        return False, f"{target!r} is not a conversation state"
    if current not in STATES:
        return False, f"{current!r} is not a conversation state"
    if current == target:
        return True, "no change"
    if target in RULES_ONLY and actor != "rules":
        return False, "only the opt-out rules set opted_out"
    if target in PERSON_ONLY and actor == "ai":
        return False, "a person accepts a handover: propose a handover instead"
    if current == "opted_out":
        return False, "an opted-out lead stays opted out unless a person lifts the opt-out on the suppression list"
    if actor == "ai" and current in TERMINAL_FOR_AI:
        return False, f"moving a lead out of {current} is a person's decision"
    if actor == "person":
        return True, "a person's decision"
    if target not in TRANSITIONS[current]:
        return False, f"{current} cannot move to {target}"
    return True, f"{current} -> {target}"
