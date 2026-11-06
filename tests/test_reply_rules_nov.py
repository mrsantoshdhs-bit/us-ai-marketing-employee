"""Gaps the local-model benchmark showed in the rules (6 Nov 2026): plain meeting requests ("Can we do a call Wednesday?",
"Tuesday at 10am works for me") must be read without a model, and annoyance at our emails must reach a person."""

import pytest

from aime.ai.escalation import assess
from aime.ai.guard import rule_floor


def test_annoyance_at_how_often_we_write_goes_to_a_person_without_a_model():
    """The rules alone hand it to a person (escalation "upset"): no AI message, the Sales Manager decides. It is not
    suppressed automatically, as the owner-approved angry-customer persona requires."""
    for text in ("This is the third email this week. I find this really annoying.",
                 "This is the fourth email you've sent. It's ridiculous and frankly annoying."):
        assert "upset" in [e.code for e in assess(text, rule_floor(text))], text


@pytest.mark.parametrize("text", [
    "Mostly me and our ops lead, Priya. Can we do a call Wednesday at 2pm Eastern?",
    "Great, Tuesday at 10am works for me. Please send the invite.",
    "Can we do a call next week?",
    "Could we have a quick chat?",
    "Thursday works for me.",
    "Friday is good for us.",
    "Please send me a calendar invite.",
])
def test_plain_meeting_requests_are_read_by_the_rules(text):
    floor = rule_floor(text)
    assert floor.intent == "meeting_request" and not floor.hit, (text, floor)


@pytest.mark.parametrize("text", [
    "Tuesday doesn't work for me, and we're not interested.",
    "Please don't send an invite, we're not interested.",
    "Stop emailing and call me.",  # an opt-out first: no AI message, a person decides
])
def test_a_refusal_or_an_opt_out_is_not_a_meeting_request(text):
    assert rule_floor(text).intent != "meeting_request", text
