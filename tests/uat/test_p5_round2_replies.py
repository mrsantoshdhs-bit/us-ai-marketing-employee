"""Phase 5, second round: what a reply means and what it does. Synthetic data only; never a real email."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from aime.crm_ops import is_suppressed, lift_suppression
from aime.inbox import new_text
from aime.models import Alert, FollowUp, InboundEmail, Lead, Meeting, Opportunity
from aime.outreach import reopen_refusal
from aime.reply_intel import BOUNCED_WROTE_AGAIN, classify
from aime.timeutil import utcnow
from aime.web.app import INTERESTED_LEADS
from tests.test_inbox import DAVID, bounce_for, contacted, latest_reply
from tests.uat.test_p5_replies_acting import ORIGIN, SAM, deliver, force_merged, reply, second_lead_by_inquiry, status_of, web


def reading(text: str) -> tuple[str, str]:
    row = InboundEmail(from_email="pat@firm.example", subject="Re: Question", body_text=text, new_text=new_text(text), auto_submitted=False, bounced_addresses=[])
    return classify(row)[:2]


# ------------------------------------------------------------------ item 1: "don't send more"
@pytest.mark.parametrize("text", ["Don't send anything else. Please call me Thursday.", "Don't send more, please just call me.", "Never send more, thanks to our filter it bounces."])
def test_more_words_behind_dont_send_more_are_no_opt_out(text):
    assert reading(text)[0] != "unsubscribe"


@pytest.mark.parametrize("text", ["Don't send more.", "Don't send more, thanks.", "Don't send more of these, please.", "Do not send anything else, thank you", "Never send me any more"])
def test_dont_send_more_with_only_a_closing_word_is_one(text):
    assert reading(text)[0] == "unsubscribe"


# ------------------------------------------------------------------ item 2: an address is no rate
@pytest.mark.parametrize("text", ["Please unsubscribe rates@lead.example", "Unsubscribe rate@lead.example please", "Please unsubscribe rate.desk@lead.example"])
def test_an_address_that_starts_like_a_rate_still_opts_out(text):
    assert reading(text)[0] == "unsubscribe"


def test_their_own_unsubscribe_rate_still_is_not_one():
    assert reading("Our unsubscribe rate is too high; can you help?")[0] != "unsubscribe"


# ------------------------------------------------------------------ item 3: warm replies stay with a person
@pytest.mark.parametrize("text", ["Let's stop with the emails and just get on a call.", "We should stop with the emails back and forth and just meet.", "Don't forget to call or email me again next month.",
                                  "Do not be afraid to call or contact us again.", "Never mind the price or contact us again next week.", "Don't wait too long or contact me again next week.",
                                  "If the trial goes over budget, please stop.", "When the budget is reached please stop.", "We'll discontinue mailing statements in Q3, can your tool help?",
                                  "We plan to discontinue the emails our old vendor sends; can you replace them?"])
def test_a_warm_reply_is_read_by_a_person_and_nothing_is_suppressed(make_ctx, text):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, text)
    assert latest_reply(ctx).category != "unsubscribe" and latest_reply(ctx).status == "needs_review"
    with ctx.db.session() as s:
        assert not is_suppressed(s, DAVID)


@pytest.mark.parametrize("text", ["Stop with the emails.", "Please stop the emails.", "Don't call or email me again.", "Don't ever contact or email us again.", "Please do not reply to this or contact me again.",
                                  "If you keep emailing, please stop.", "Please discontinue all emails.", "Please stop.", "Stop emailing me."])
def test_the_opt_outs_beside_them_stay(text):
    assert reading(text)[0] == "unsubscribe"


def test_a_resend_request_reads_as_interest_again():
    assert reading("We never got your email about pricing, please resend.")[0] == "interested"


# ------------------------------------------------------------------ item 4: a notice or a footer under what they say
NOTICES = ["If received in error, please delete it from your system.",
           "If you have received this email in error, please notify us and delete it from your system. We are not liable for any viruses.",
           "This message is confidential. If you received it in error, delete it from your system. We are not responsible for its content.",
           "This email is intended only for the addressee. If this message has reached you in error, please delete it from your system.",
           "If this was sent to you by mistake, please remove it from your system.",
           "If this e-mail is not intended for you, delete it from your system.",
           "This message may contain privileged information. If received in error, please contact the sender and erase it from your system and destroy all copies.",
           "Our company | Privacy | Unsubscribe from our newsletter | 1 Main St",
           "Unsubscribe from our newsletter"]


@pytest.mark.parametrize("notice", NOTICES)
@pytest.mark.parametrize("words", ["Yes please.", "Can we talk Tuesday?", "Please send me a calendar link."])
def test_a_business_reply_above_a_notice_or_footer_suppresses_nothing(make_ctx, words, notice):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, f"{words}\n\n{notice}")
    assert latest_reply(ctx).category != "unsubscribe"
    with ctx.db.session() as s:
        assert not is_suppressed(s, DAVID)


@pytest.mark.parametrize("notice", NOTICES)
@pytest.mark.parametrize("words", ["Please remove me from your list.", "Stop emailing me.", "Go away.", "No contact please.", "Never contact this company again.", "Please do not contact our staff.",
                                   "Add us to your do-not-email list.", "stopp emailing", "take me of your list", "Unsubscribe", "Where did you get this address? Delete it.",
                                   "Abmelden", "Baja", "Graag uitschrijven", "no quiero mas correos", "Beni listeden çıkarın", "请不要再发邮件"])
def test_a_real_opt_out_above_a_notice_or_footer_stays_suppressed(words, notice):
    assert reading(f"{words}\n\n{notice}")[0] == "unsubscribe"


@pytest.mark.parametrize("text", ["Take us off old spreadsheets you bought.", "Customer unsubscribe rate means nothing to me. I'm done with you. Go.", "Add an opt out for our customers? No: add one for us.",
                                  "Don't email me the report until never."])
def test_their_own_business_wording_with_a_refusal_in_it_stays_an_opt_out(text):
    assert reading(text)[0] == "unsubscribe"


@pytest.mark.parametrize("words", ["Do not send anything else.", "No thank you.", "I do not want this.", "Not interested. Don't write back.", "Don't bother me again.", "Please stop it now.", "Enough emails from you."])
def test_a_refusal_that_only_counted_above_a_notice_still_does(words):
    assert reading(f"{words}\n\nThanks,\nPat Ruiz\n\nThis message is confidential. If you received it in error, please delete it from your system.")[0] == "unsubscribe"


def test_a_notice_alone_with_nothing_said_suppresses_nothing_and_someone_speaking_for_themselves_counts():
    assert reading("If you are not the intended recipient of this confidential message, delete it from your system.")[0] != "unsubscribe"
    assert reading("This is my old address. Remove it from your system.")[0] == "unsubscribe"
    assert reading("I am not the intended recipient, please remove me.")[0] == "unsubscribe"


# ------------------------------------------------------------------ a date, and a refusal behind it
@pytest.mark.parametrize("text", ["Please stop emailing me until March and after that too.", "No more emails until January or I will file a complaint.", "Don't email me until March and keep it that way.",
                                  "Stop emailing me until March, and then forever.", "Don't email me until May 2099.", "Don't contact us until 12/25/2099.", "Don't email me until April or we will go to the FTC.",
                                  "Don't email me until April. I mean it, this is your last warning."])
def test_a_refusal_behind_the_date_keeps_the_opt_out(text):
    assert reading(text)[0] == "unsubscribe"


@pytest.mark.parametrize("text", ["Don't email me until April.", "Don't email me until April, please.", "Stop emailing me until March, I'm on leave.", "Don't email me until April, and after that feel free.",
                                  "Do not contact me until 15 March 2027."])
def test_a_plain_date_stays_the_owners_reminder(text):
    assert reading(text)[0] == "objection"


# ------------------------------------------------------------------ item 5: an address that only bounced
def bounce_then_writes(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    with ctx.db.session() as s:
        assert is_suppressed(s, DAVID)
    reply(ctx, sent, "Sorry our mail server was down. Sounds interesting, can we talk Thursday?")
    return ctx


def test_an_address_that_only_bounced_is_not_called_opted_out_and_a_person_still_decides(make_ctx):
    ctx = bounce_then_writes(make_ctx)
    r = latest_reply(ctx)
    assert r.status == "needs_review" and "bounced earlier" in r.action_note and "opted out" not in r.action_note
    assert "bounced earlier" in r.recommended_status and "owner decides" in r.recommended_status
    with ctx.db.session() as s:
        told = [a.message for a in s.execute(select(Alert)).scalars()]
        assert any("bounced earlier and now wrote" in a for a in told) and not any("opted out earlier" in a for a in told)
        assert is_suppressed(s, DAVID)
        assert not s.execute(select(FollowUp).where(FollowUp.kind == "response")).first(), "still no draft: the address stays blocked"


def test_a_real_opt_out_on_top_of_a_bounce_still_says_opted_out(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    reply(ctx, sent, "Please unsubscribe me.")
    reply(ctx, sent, "Sorry, actually call me, we are interested.")
    r = latest_reply(ctx)
    assert "opted out earlier" in r.action_note and "bounced earlier" not in r.action_note


def test_a_bounced_deal_step_says_so_too(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?")
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    reply(ctx, sent, "Sorry, the server was down. Still interested, can we talk Friday?")
    with ctx.db.session() as s:
        assert s.execute(select(Opportunity)).scalars().first().next_step == BOUNCED_WROTE_AGAIN


# ------------------------------------------------------------------ item 6: a merged lead
def test_a_late_bounce_on_a_merged_lead_leaves_the_interested_reply_reachable(make_ctx):
    ctx, _sender, sent, _merged, keeper, _first = force_merged(make_ctx)
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?")
    wrote = latest_reply(ctx).id
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    report = latest_reply(ctx)
    assert report.category == "bounce" and report.status == "needs_review" and f"reply #{wrote}" in report.action_note
    with ctx.db.session() as s:
        assert s.get(InboundEmail, wrote).lead_id == keeper


def test_a_bounce_on_a_merged_lead_with_no_reply_changes_nothing_new(make_ctx):
    ctx, _sender, sent, _merged, _keeper, _first = force_merged(make_ctx)
    deliver(ctx, bounce_for(DAVID, sent.message_id))
    assert latest_reply(ctx).status == "actioned"


def card_and_list(ctx) -> tuple[int, str]:
    with ctx.db.session() as s:
        card = s.execute(select(func.count(Lead.id)).where(*INTERESTED_LEADS)).scalar_one()
    return card, web(ctx).get("/leads?outreach=interested").text


def test_an_interested_reply_on_a_merged_lead_is_on_the_interested_card_and_list(make_ctx):
    ctx, _sender, sent, merged, keeper, first = force_merged(make_ctx)
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?")
    card, page = card_and_list(ctx)
    assert card == 1 and f"/leads/{keeper}" in page and f"/leads/{merged}\"" not in page
    assert latest_reply(ctx).lead_id == keeper, "the lead the list opens shows the reply"
    assert f"{DAVID}" in web(ctx).get(f"/leads/{keeper}").text
    assert status_of(ctx, first) == "cancelled", "the keeper's own contact still gets nothing"


def test_interest_from_a_merged_lead_does_not_override_a_no_or_an_opt_out_at_the_company(make_ctx):
    ctx, _sender, sent, _merged, keeper, _first = force_merged(make_ctx)
    with ctx.db.session() as s:
        s.get(Lead, keeper).outreach_status = "not_interested"
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?")
    assert card_and_list(ctx)[0] == 0
    ctx, _sender, sent, _merged, keeper, _first = force_merged(make_ctx)
    reply(ctx, sent, "Please unsubscribe me.")
    reply(ctx, sent, "Sounds interesting, can we talk Thursday?", frm=SAM)
    with ctx.db.session() as s:
        assert s.get(Lead, keeper).outreach_status != "interested"


# ------------------------------------------------------------------ item 7: the owner's lift
def stopped_colleague(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    _first, other = second_lead_by_inquiry(ctx)
    reply(ctx, sent, "Please remove me from your list.")
    with ctx.db.session() as s:
        lead = s.get(Lead, other)
        assert lead.outreach_status == "stopped"
        assert reopen_refusal(s, lead)
    assert web(ctx).post(f"/inbox/{latest_reply(ctx).id}/reviewed", headers=ORIGIN, follow_redirects=False).status_code < 400
    with ctx.db.session() as s:  # the fixture's mail is dated later today: as anywhere else, the lift comes after what it lifts
        s.get(InboundEmail, latest_reply(ctx).id).received_at = utcnow() - timedelta(days=1)
    return ctx, other


def test_lifting_david_lets_the_colleagues_lead_reopen(make_ctx):
    ctx, other = stopped_colleague(make_ctx)
    with ctx.db.session() as s:
        assert reopen_refusal(s, s.get(Lead, other)), "not before the lift"
        assert lift_suppression(s, DAVID, "owner", "They wrote and asked to hear from us") is None
    with ctx.db.session() as s:
        assert reopen_refusal(s, s.get(Lead, other)) is None


def test_any_other_stop_left_standing_still_refuses(make_ctx):
    ctx, other = stopped_colleague(make_ctx)
    third = f"lee@{DAVID.rsplit('@', 1)[-1]}"
    with ctx.db.session() as s:
        sent_row = s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalars().first()
        s.expunge(sent_row)
    reply(ctx, sent_row, "Please stop emailing us.", frm=third)
    web(ctx).post(f"/inbox/{latest_reply(ctx).id}/reviewed", headers=ORIGIN, follow_redirects=False)
    with ctx.db.session() as s:
        assert lift_suppression(s, DAVID, "owner", "Asked again") is None
    with ctx.db.session() as s:
        assert reopen_refusal(s, s.get(Lead, other)), "someone else's no has not been lifted"


def test_a_stop_written_after_the_lift_refuses_again(make_ctx):
    ctx, other = stopped_colleague(make_ctx)
    with ctx.db.session() as s:
        lift_suppression(s, DAVID, "owner", "Asked again")
    with ctx.db.session() as s:
        sent_row = s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalars().first()
        s.expunge(sent_row)
    reply(ctx, sent_row, "No, remove me after all. Please remove me from your list.")
    with ctx.db.session() as s:
        assert reopen_refusal(s, s.get(Lead, other))


def test_the_own_leads_stop_with_nobody_lifted_still_refuses(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    reply(ctx, sent, "Please remove me from your list.")
    with ctx.db.session() as s:
        assert reopen_refusal(s, s.get(Lead, sent.lead_id))


# ------------------------------------------------------------------ item 8: another time named
def booked_reply(make_ctx, text):
    ctx, _s, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, sent.lead_id)
        s.add(Meeting(lead_id=lead.id, company_id=lead.company_id, invitee_email=DAVID, starts_at=utcnow() + timedelta(days=5), ends_at=utcnow() + timedelta(days=5, minutes=30), source="manual", status="scheduled"))
    reply(ctx, sent, text)
    with ctx.db.session() as s:
        return s.get(FollowUp, latest_reply(ctx).suggested_followup_id).body


@pytest.mark.parametrize("text", ["Let's talk on the 14th.", "Sounds good, let's talk in two weeks.", "Sounds good. I'm travelling that week."])
def test_no_confirmation_when_they_name_another_time(make_ctx, text):
    body = booked_reply(make_ctx, text)
    assert "look forward to it" not in body and "[Write your answer here]" in body


def test_a_plain_yes_still_gets_the_confirmation(make_ctx):
    assert "look forward to it" in booked_reply(make_ctx, "Sounds good, thanks.")
