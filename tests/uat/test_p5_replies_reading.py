"""Phase 5: how a reply is read. Each test states what a person reading the email would expect. Synthetic data only.

Opt-outs are the point of most of them: a wording may only stop suppressing on positive evidence that it is not one, and what stops
suppressing goes to a person with the sequence stopped."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from aime import reply_intel as ri
from aime.agents.inbox_agent import looks_like_calendly
from aime.inbox import between_header_copies, new_text, parse_message, rest_of_reply
from aime.models import FollowUp, InboundEmail, Lead, Opportunity, Suppression
from aime.reply_intel import check_back_when, classify, intent_of, named_addresses, recommended_for
from tests.test_company_research import SITE
from tests.test_inbox import DAVID, FakeIMAP, contacted, latest_reply, mail, run_inbox
from tests.uat import p2_mail_corpus as K

DISCLAIMER = ("CONFIDENTIALITY NOTICE: This e-mail is for the sole use of the intended recipient. If you have received this e-mail in error, "
              "please notify the sender immediately and delete it from your system.")
DISCLAIMER2 = "If you are not the intended recipient, you must not read, use or disclose it. Please tell us by return and remove it from your system."
WRAPPED = ("This message is confidential. If you are not the addressee please notify\nthe sender and delete it from\nyour systems. Any unauthorised "
           "use is prohibited.")
QUOTE = "On Fri, Oct 30, 2026 at 1:09 PM Sales <sales@us.example> wrote:\n\n> Hi David, a short question."
PLAIN_OPT_OUTS = ["Not for us. Please stop emailing me. Thanks.", "Thanks for the note but please remove me from your list. We use another vendor.",
                  "Stop emailing me. I've told you already.", "We're not interested. Do not contact us again. Regards, Sam", "Hi, unsubscribe me please. Have a good one.",
                  "Please remove our company from your list. Nobody here wants this.", "I am retired now. Take me off your list.",
                  "We already use a partner so no more emails please. Thanks.", "Is this a joke? Stop emailing me.", "How did you get this address? Remove me now."]


def suppressed(ctx):
    with ctx.db.session() as s:
        return {x.value for x in s.execute(select(Suppression)).scalars()}


def answer(make_ctx, text, frm=None, subject=None):
    ctx, _sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(frm or f"David Chen <{DAVID}>", subject or f"Re: {sent.subject}", text, in_reply_to=sent.message_id)}
    run_inbox(ctx)
    return ctx, latest_reply(ctx)


def row(text, **fields):
    return InboundEmail(**{"from_email": "p@x.example", "subject": "Re: Question", "body_text": text, "new_text": new_text(text), "auto_submitted": False,
                           "bounced_addresses": [], **fields})


def read(text, **fields):
    return classify(row(text, **fields))


def outreach_status(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Lead).order_by(Lead.id)).scalars().first().outreach_status


def stops_and_waits_for_a_person(ctx, r):
    assert DAVID not in suppressed(ctx) and r.status == "needs_review" and outreach_status(ctx) == "replied"
    with ctx.db.session() as s:
        assert not s.execute(select(Opportunity)).scalars().all()
        assert not s.execute(select(FollowUp).where(FollowUp.kind == "response")).scalars().all()


# ------------------------------------------------------------------ a legal notice is nobody's opt-out
@pytest.mark.parametrize("text", [
    f"Sounds interesting, lets talk next week.\n\nThanks,\nJanet\nVP Operations\n\n{DISCLAIMER}",
    f"Do you work with companies our size?\n\nThanks,\nJanet\nVP Operations\n\n{DISCLAIMER2}",
    f"Do you work with companies our size?\n\nThanks,\nJanet\n\n{WRAPPED}",
])
def test_a_legal_disclaimer_under_a_real_reply_is_no_opt_out(make_ctx, text):
    ctx, r = answer(make_ctx, text)
    assert r.category != "unsubscribe" and DAVID not in suppressed(ctx)


@pytest.mark.parametrize("text", ["Yes please.", "Thursday 10am ET is good."])
def test_a_gateways_notice_below_the_quote_leaves_a_short_answer_for_a_person(make_ctx, text):
    ctx, r = answer(make_ctx, f"{text}\n\n{QUOTE}\n\n{DISCLAIMER}")
    assert (r.category, r.confidence) == ("other", "low")
    stops_and_waits_for_a_person(ctx, r)


def test_thanks_above_a_notice_is_for_a_person(make_ctx):
    ctx, r = answer(make_ctx, f"Thanks\n\n{DISCLAIMER}")
    assert (r.category, r.confidence) == ("other", "low")
    stops_and_waits_for_a_person(ctx, r)


@pytest.mark.parametrize("text", [
    "I am not the intended recipient. Please remove me from your list.", "I received this in error, please remove me from your list.",
    "My address is confidential, please take me off your list.", "This address is confidential. Stop emailing me.",
    "Not the intended recipient. Do not contact me again.", "Remove me from your list. CONFIDENTIAL: for the intended recipient only.",
    "This is my old address. Remove it from your system.", "We are not the intended recipient. Remove it from your system.",
    "Wrong person. Delete it from your records.", "You sent this by mistake. Remove it from your system.",
    f"Please remove me from your list.\n\n{DISCLAIMER}", f"Stop emailing me.\n\n{QUOTE}\n\n{DISCLAIMER2}",
])
def test_someone_who_says_it_themselves_is_still_an_opt_out_beside_any_notice(text):
    assert read(text)[0] == "unsubscribe", read(text)


def test_a_footer_and_a_notice_with_nothing_else_said_still_count_as_today():
    assert read(f"Click here to unsubscribe\n\n{DISCLAIMER}")[:2] == ("unsubscribe", "medium")


@pytest.mark.parametrize("text", ["Wrong person. Delete it from your records.", "You sent this by mistake. Remove it from your system.", "Thanks but remove it from your system."])
def test_their_own_removal_in_the_same_paragraph_ahead_of_a_notice_still_counts(text):
    assert read(f"{text} {DISCLAIMER}")[0] == "unsubscribe", read(f"{text} {DISCLAIMER}")


@pytest.mark.parametrize("text", ["Abmelden", "Baja", "Por favor no me escriba más."])
def test_a_foreign_opt_out_over_a_notice_is_still_all_they_say(text):
    assert read(f"{text}\n\n{DISCLAIMER}")[:2] == ("unsubscribe", "medium")


@pytest.mark.parametrize("text", ["We're good to go. Send the invite for Friday.", "We're good for now, thanks."])
def test_a_weak_no_over_a_company_footer_does_not_become_all_it_says(text):
    category, confidence, _why = read(f"{text}\n\nClick here to unsubscribe from our newsletter.")
    assert category != "unsubscribe" and confidence == "low"


# ------------------------------------------------------------------ their own business in opt-out words
@pytest.mark.parametrize("text", [
    "Our newsletter unsubscribe rate has doubled this year. Is that something you can help with?",
    "We want to stop emailing spreadsheets back and forth. Can you show me how this would work?",
    "Customers keep asking for an opt out on our SMS reminders. Can your team build that?",
    "If you can get us off our old ERP I'm all ears. Let's talk.",
    "We've had no further contact with our old vendor since June, so the timing is right. Can we talk this week?",
])
def test_their_own_business_in_opt_out_words_does_not_close_a_prospect(make_ctx, text):
    ctx, r = answer(make_ctx, text)
    assert r.category != "unsubscribe" and DAVID not in suppressed(ctx)
    assert (r.category, r.confidence) == ("other", "low")
    stops_and_waits_for_a_person(ctx, r)


@pytest.mark.parametrize("text", [
    "We need to stop sending invoices by hand.", "Our unsubscribe page is broken and customers complain.", "Take us off spreadsheets and I'll be happy",
    "Don't email me the deck, just bring it to the call", "How do I opt out of the annual plan if we sign up?", "We have had no further contact with the vendor since June.",
])
def test_more_of_their_own_business_goes_to_a_person(text):
    category, confidence, why = read(text)
    assert (category, confidence) == ("other", "low") and why[0].startswith(ri.OWN_BUSINESS_REASON)


def test_their_own_business_below_the_quote_suppresses_nothing_either():
    category, confidence, why = read(f"Thanks.\n\n{QUOTE}\n\nWe need to stop sending invoices by hand.")
    assert category != "unsubscribe" and confidence == "low" and any(w.startswith(ri.OWN_BUSINESS_REASON) for w in why)


@pytest.mark.parametrize("text", [
    *PLAIN_OPT_OUTS, "Stop sending emails.", "Take us off.", "Stop sending junk.", "Take me off.", "Your unsubscribe link does not work.", "Where is the unsubscribe link?",
    "Stop sending them.", "stop emailing our team", "We want you to stop emailing us.", "I need you to stop emailing me.", "Take us off this thread", "I want to stop emailing you.",
    "We need to stop sending invoices by hand. Separately, please stop emailing me.", "Don't email me the deck. In fact, don't email me at all.",
    "No further contact with the company please.", "No further contact with our company.", "Please opt me out of the plan.", "Take us off our list.",
    # stopping something with no paperwork named is them stopping us
    "We want to stop emailing with you.", "We want to stop emailing with your company.", "I just want to stop emailing back and forth with you. Thanks.", "I want to stop emailing now.",
    "I'd like to stop emailing altogether, thanks.", "We need to stop sending replies to you people.", "We want to stop sending reports to you.",
    # whose link it is: only theirs is their business
    "Your newsletter unsubscribe link does not work.", "Your newsletter unsubscribe link is broken. Why do I keep getting these?", "The newsletter unsubscribe link does not work.",
    "Newsletter unsubscribe link is broken", "Our unsubscribe link did nothing, I still get your mail.", "Your unsubscribe page is broken.", "Your unsubscribe rate must be huge.",
    # no further contact with someone, unless it is an account of the past
    "No further contact with the CEO please.", "No further contact with the owner of this address.", "We want no further contact with the people at your firm.",
    "No further contact with the person you emailed.", "No further contact with the recipient.", "I want to have no further contact with the vendor.",
    # off a list of any age, whoever's they call it
    "Take us off old lists please.", "Take us off old contacts.", "Take us off our distribution.", "Take us off our marketing.", "Take us off our old customer list.", "Take us off our system.",
    # a file, and everything else with it
    "Don't email me the report or anything else again.", "Don't email me the proposals. Or anything else.", "Do not email us the reports ever again", "Don't email me the deck. Stop.",
    "Don't email me the newsletter.", "Don't email me those offers.",
    # an opt out for the writer's own company asks for one: only a product of theirs makes it their business
    "Please record an opt out for our company.", "We are requesting an opt out for our company. Do not reply.", "I want an opt-out for our domain.",
    "This is an opt out for our whole team.", "Consider this an opt out for our firm.", "Please process an opt out on our account.", "We are asking for an opt out for our company.",
    "Please add an opt out for our company.", "We would like an opt out for our customers from your emails.", "Please record an opt out for our website.",
    # refusing what we send: no other way of getting it is named
    "Don't email me the reports you keep sending.", "Don't email me those invoices you keep sending, I'm not a customer.", "Do not email us the quotes you keep pushing, we did not ask.",
    "Don't email me the proposals, I did not ask for them.", "Don't email me the proposal. Take a hint.", "Don't email me the reports.", "Do not email us the invoices. Nobody here wants them.",
    "Don't email me the deck, I never asked.", "Don't email me the deck, we are not interested.", "Don't email me the file, wrong person.", "Don't email me the quotes, I'm not your customer.",
])
def test_a_plain_opt_out_stays_one(text):
    assert read(text)[0] == "unsubscribe", read(text)


@pytest.mark.parametrize("text", ["We need to stop writing reports by hand.", "We want to stop sending out our invoices by hand, can you help?", "Our newsletter unsubscribe link is broken and customers complain.",
                                  "Get us off our old billing tool.", "Don't email me the report, just bring it to the call.", "We need to add an opt-out on our signup form.",
                                  "Can you build an opt out for our customers?", "Users keep asking for an opt out on our app.", "Don't email me the invoice, send it to accounting.",
                                  "Don't email me the deck yet, I need to check with my boss.", "Don't email me the file, share it on the portal instead."])
def test_what_names_their_own_paperwork_or_tools_goes_to_a_person(text):
    category, confidence, why = read(text)
    assert (category, confidence) == ("other", "low") and why[0].startswith(ri.OWN_BUSINESS_REASON)


@pytest.mark.parametrize("text", ["I'll never hear from your team again if I sign?", "I never want to hear from you again?"])
def test_a_question_about_never_hearing_from_us_suppresses_nobody(text):
    assert read(text)[:2] == ("question", "medium")


@pytest.mark.parametrize("text", ["I never want to hear from you again.", "We'd like to never hear from your company again."])
def test_never_hearing_from_us_said_outright_is_an_opt_out(text):
    assert read(text)[0] == "unsubscribe"


@pytest.mark.parametrize("text", ["Please record an opt out for our company.", "Don't email me the reports you keep sending."])
def test_a_refused_report_or_a_company_opt_out_is_suppressed_through_the_inbox(make_ctx, text):
    ctx, r = answer(make_ctx, text)
    assert r.category == "unsubscribe" and DAVID in suppressed(ctx)


@pytest.mark.parametrize("text", PLAIN_OPT_OUTS)
def test_a_plain_opt_out_among_other_sentences_is_honoured_at_once(make_ctx, text):
    ctx, r = answer(make_ctx, text)
    assert r.category == "unsubscribe" and DAVID in suppressed(ctx)


@pytest.mark.parametrize("text", ["Stop.", "Remove.", "We need to stop sending invoices by hand.\nStop"])
def test_a_line_that_only_says_stop_is_never_demoted(text):
    assert read(text)[0] == "unsubscribe"


def test_a_complaint_is_never_demoted():
    assert read("We need to stop sending invoices by hand. This is spam, how did you get this address?")[0] == "unsubscribe"


# ------------------------------------------------------------------ "don't email me until April": the owner's decision of 1 Nov 2026
@pytest.mark.parametrize("text", [
    "Please don't email me until after Q1, we're in the middle of a migration. Try me again in April.",
    "Don't contact us before January. Things will have calmed down by then and I'd like to hear more.",
])
def test_not_until_a_date_is_no_opt_out_for_good(make_ctx, text):
    ctx, r = answer(make_ctx, text)
    assert r.category != "unsubscribe" and DAVID not in suppressed(ctx)


@pytest.mark.parametrize("text, month", [
    ("Don't email me until April.", 4), ("Do not contact me until next year.", 1), ("Please stop emailing me until the new year.", 1), ("No more emails until April.", 4),
    ("Don't email me again until Q3.", 7), ("Don't contact us before January.", 1), ("Don't email me after Q1.", 3), ("Stop sending me emails until April 15.", 4),
    ("Do not contact me until 15 March 2027.", 3),
])
def test_a_date_after_the_opt_out_words_is_an_objection_with_a_reminder(text, month):
    r = row(text)
    category, confidence, why = classify(r)
    assert (category, confidence) == ("objection", "low") and why[0].startswith(ri.NOT_UNTIL_REASON)
    assert check_back_when(r)["month"] == month


@pytest.mark.parametrize("text", [
    "Don't email me until I contact you", "Don't email me until further notice", "Don't email me until I say so", "Remove me. Not until April, not ever",
    "Stop emailing me. We close the books on April 15.", "Stop emailing me. See you at the expo in March.", "Don't email me until April, or ever.", "Don't email me until April if at all.",
    "Stop emailing me until further notice.", "Stop sending me emails until I ask.", "Do not email me until hell freezes over.", "No more emails until I ask for them.",
    "Please do not contact me again.", "Never email me after April, or before it.", "Don't email me. April is our busy month.", "Don't email me until April. Actually, take me off your list.",
    "Don't email me until April. Actually never.", "Don't email me until April. Or rather never.", "No more emails until April please. Actually no more at all.",
    "Do not contact me until next year. If at all.", "Don't email me until April. Not even then.", "Don't email me until April. Actually, don't.", "Don't email me until April. On second thought, don't.",
])
def test_an_opt_out_without_a_date_of_its_own_stays_one(text):
    assert read(text)[0] == "unsubscribe", read(text)


@pytest.mark.xfail(reason="suggest() belongs to the replies-acting group: drop this mark when the objection branch gives no draft for a dated no", strict=False)
def test_a_date_leaves_no_draft_that_writes_to_them_now(make_ctx):
    ctx, r = answer(make_ctx, "Don't email me until April.")
    assert r.suggested_followup_id is None
    with ctx.db.session() as s:
        assert not s.execute(select(FollowUp).where(FollowUp.kind == "response")).scalars().all()


def test_a_date_stops_the_sequence_suppresses_nothing_and_leaves_a_reminder(make_ctx):
    ctx, r = answer(make_ctx, "Don't email me until April.")
    assert (r.category, r.confidence, r.status) == ("objection", "low", "needs_review")
    assert DAVID not in suppressed(ctx) and outreach_status(ctx) == "replied"
    with ctx.db.session() as s:
        task = s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending", FollowUp.subject.like("Check back%"))).scalars().one()
        assert task.subject.endswith("in April") and task.due_at.month == 4


def test_a_date_below_the_quote_suppresses_nothing_and_still_leaves_a_reminder(make_ctx):
    ctx, r = answer(make_ctx, f"Thanks.\n\n{QUOTE}\n\nDon't email me until April.")
    assert r.category != "unsubscribe" and r.confidence == "low" and DAVID not in suppressed(ctx)
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending", FollowUp.subject.like("Check back%"))).scalars().one().due_at.month == 4


# ------------------------------------------------------------------ we're good to go
def test_we_are_good_to_go_is_not_a_no(make_ctx):
    ctx, r = answer(make_ctx, "We're good to go. Send the invite for Friday.")
    assert r.category != "not_interested"


def test_were_good_for_now_is_for_a_person_with_the_sequence_stopped(make_ctx):
    ctx, r = answer(make_ctx, "We're good for now, thanks.")
    assert r.status == "needs_review" and outreach_status(ctx) == "replied"


def test_all_set_for_tuesday_does_not_close_an_open_deal(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds interesting, can we talk Thursday?", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    FakeIMAP.messages[2] = mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "We're all set for Tuesday, send the invite.", in_reply_to=sent.message_id)
    run_inbox(ctx)
    with ctx.db.session() as s:
        assert s.execute(select(Opportunity)).scalars().one().stage != "lost"


@pytest.mark.parametrize("text", ["No thanks, we're all set", "We're good, thanks.", "We're all set.", "Thanks but we're all set.", "No need.", "We're good"])
def test_the_same_words_that_end_their_sentence_are_still_a_no(text):
    assert read(text)[0] == "not_interested", read(text)


@pytest.mark.parametrize("text", ["No need to schedule a call.", "We're good, no need to schedule a call.", "No need to set up a meeting."])
def test_no_need_to_meet_opens_no_deal(text):
    assert read(text)[0] != "interested", read(text)


# ------------------------------------------------------------------ "interested" said as a no
@pytest.mark.parametrize("text", ["Why would we be interested in this? We do all of it ourselves.", "Nobody here is interested.",
                                  "I doubt we'd be interested, we're a very small outfit."])
def test_interested_said_as_a_no_opens_no_deal_and_tells_nobody_good_news(make_ctx, text):
    ctx, r = answer(make_ctx, text)
    with ctx.db.session() as s:
        assert r.category != "interested" and not s.execute(select(Opportunity)).scalars().all()


@pytest.mark.parametrize("text", ["No one here is interested.", "None of us are interested.", "Hardly interested.", "Not sure anyone would be interested",
                                  "I can't imagine anyone here being interested.", "Unlikely that we are interested", "Nothing here we're interested in.",
                                  "We’d never really be that interested."])
def test_more_ways_to_say_no(text):
    assert read(text)[0] != "interested", read(text)


@pytest.mark.parametrize("text", ["No doubt we'd be interested.", "Without a doubt we're interested.", "Not now but we are interested in Q2.", "Not sure about timing but very interested.",
                                  "Not sure about budget but can we talk Tuesday?", "Don't have long but can we talk Friday?", "Not cheap but I'm interested.",
                                  "We don't have one and we're interested.", "Nothing to lose so we're interested.", "Can't wait to hear your pricing.", "Not only are we interested, we are ready."])
def test_a_real_yes_stays_a_yes(text):
    assert read(text)[0] == "interested", read(text)


def test_a_curly_apostrophe_in_a_meeting_intent_is_still_read_as_a_no():
    r = row("We don’t really want to schedule a call.")
    r.category = "other"
    assert intent_of(r) != "meeting_request"


# ------------------------------------------------------------------ opt-outs a person had to catch
@pytest.mark.parametrize("text", [
    "Not interested, please stop.", "Just stop.", "Stop this.", "Please un subscribe me", "usubscribe",
    "This is the second one of these. I want you to forget this address and I expect never to hear from your firm again.",
    "Por favor no me escriba más.", "Cancelar suscripción", "Désinscription", "Cancellatemi dalla vostra lista, grazie.",
    "Favor remover meu e-mail da lista.",
])
def test_opt_outs_the_wordings_of_test_p2_mail_miss(make_ctx, text):
    ctx, r = answer(make_ctx, text)
    assert r.category == "unsubscribe" and DAVID in suppressed(ctx)


@pytest.mark.parametrize("text", ["I said stop", "STOP STOP STOP", "Stop pls", "Remove pls", "No. Stop.", "Stop, thanks", "No thanks, please stop.", "Not interested. Stop.",
                                  "no thanks, please don't send more of these", "Don't send more of these", "Never send anything else.", "unsusbscribe", "unscubscribe",
                                  "I never want to hear from you again.", "Please forget my email address."])
def test_short_and_mistyped_opt_outs(text):
    assert read(text)[0] == "unsubscribe", read(text)


@pytest.mark.parametrize("text", ["Stop by our booth at the expo and we can talk", "We need to stop losing leads. Can you help?", "Thanks for not giving up on us, let's talk",
                                  "I'd like to remove the manual step from our invoicing. Can you do that?", "Don't email me the deck, just bring it to the call",
                                  "Please don't send the proposal yet, I need to check with my boss first", "Do not contact our old vendor about this please",
                                  "Please delete the earlier version of the file you sent, here's the right one", "Can you take me through how it works?",
                                  "Our unsubscribe page is broken, can you fix that kind of thing?", "How do I opt out of the annual plan if we sign up?",
                                  "Please don't send me more info.", "Don't send more details, just call me Thursday.", "Please don't send anything else until I've spoken to my boss.",
                                  "Don't send me anything further for now, I'll review the deck and come back to you.", "Do not send further documents to me, use my colleague instead.",
                                  "Never send more than one attachment, our filter blocks them.", "We never hear from your team after the sale, is that normal?", "I never hear from Sam any more.",
                                  "Baja California is where we are based.", "Remove this."])
def test_what_is_not_an_opt_out(text):
    assert read(text)[0] != "unsubscribe", read(text)


def test_a_no_that_speaks_of_being_removed_is_not_filed_away():
    assert read("No thanks, we just removed that line item.")[:2] == ("not_interested", "low")
    assert read("No thanks.")[:2] == ("not_interested", "high")


# ------------------------------------------------------------------ the middle of a forwarded chain
def chain(top, middle, tail="Hi Janet, a short question.\n\nTo stop receiving emails from us, reply \"unsubscribe\"."):
    return (f"{top}\n\nJanet\n\n---------- Forwarded message ---------\nFrom: Boss <boss@lead.example>\nDate: Mon, Nov 2, 2026 at 8:02 AM\nSubject: Re: FW: Question\nTo: Janet <janet@lead.example>\n\n"
            f"{middle}\n\n---------- Forwarded message ---------\nFrom: Sales <sales@us.example>\nDate: Fri, Oct 30, 2026 at 1:09 PM\nSubject: Question\nTo: Janet <janet@lead.example>\n\n{tail}")


def test_an_opt_out_in_the_middle_of_a_chain_counts_at_medium_so_a_person_checks():
    assert read(chain("See below from our owner.", "Tell them to stop emailing us. We don't want this."))[:2] == ("unsubscribe", "medium")


def test_an_opt_out_in_the_chain_beside_interest_suppresses_nothing_and_a_person_reads_it():
    assert read(chain("Sounds interesting, can we talk Thursday?", "Tell them to stop emailing us."))[:2] == ("interested", "low")


@pytest.mark.parametrize("middle", ["Looks fine to me, you decide.", "Click here to unsubscribe", f"Thanks\n\n{DISCLAIMER}", "Sounds interesting, let's talk."])
def test_the_rest_of_a_chain_is_read_for_nothing_else(middle):
    assert read(chain("See below from our owner.", middle))[0] == "other", read(chain("See below from our owner.", middle))


@pytest.mark.parametrize("tail", ["Please stop emailing me if this is not for you.", "If not, I'll stop emailing."])
def test_our_own_email_at_the_end_of_a_chain_is_not_their_words(tail):
    assert read(chain("See below from our owner.", "Looks fine.", tail=tail))[0] != "unsubscribe"


def test_a_part_holding_our_footer_is_our_own_email():
    body = chain("FYI", "Please stop emailing me if this is not for you.\n\nTo stop receiving emails from us, reply \"unsubscribe\".")
    assert between_header_copies(body) == []


def test_an_automatic_mail_or_a_newsletters_chain_is_not_read():
    text = chain("See below.", "Tell them to stop emailing us.")
    assert classify(row(text, auto_submitted=True))[0] != "unsubscribe"
    assert classify(row(text, mail_kind="list"))[0] != "unsubscribe"


def test_an_attached_email_is_not_the_writers():
    assert between_header_copies(f"-----Attached message-----\n{chain('x', 'Stop emailing us.')}") == []


# ------------------------------------------------------------------ price, passing, a plain yes
def classified(text):
    r = InboundEmail(mailbox_uid="x", message_id="<x@y>", from_email="p@x.example", subject="Re: Question", body_text=text, new_text=text, status="new")
    r.category, _conf, _why = classify(r)
    return r


@pytest.mark.parametrize("text", ["Thanks for the details. That is over our budget for this year, so we will pass for now.", "That's above our budget, we'll pass.", "Over budget this year sadly."])
def test_a_no_on_price_is_not_called_a_pricing_question(text):
    assert intent_of(classified(text)) != "pricing_request"


@pytest.mark.parametrize("text", ["That's over our budget unless you can do 5k?", "What does it cost?"])
def test_a_question_about_price_still_is_one(text):
    assert intent_of(classified(text)) == "pricing_request"


@pytest.mark.parametrize("text", ["We will pass, thanks.", "That's above our budget, we'll pass."])
def test_we_will_pass_is_a_no(text):
    assert classified(text).category in ("not_interested", "objection")


@pytest.mark.parametrize("text", ["We'll pass this along to our CTO.", "We will pass your note to the right team.", "I'll pass this on.", "We will pass it on and get back to you",
                                  "We'll pass the deck to the board."])
def test_passing_it_on_is_not_a_no(text):
    assert classified(text).category not in ("not_interested", "objection")


@pytest.mark.parametrize("text", ["Yes, worth a chat. How does Wednesday look?", "Yes please, send me a time.", "Worth a conversation. When are you free?"])
def test_a_plain_yes_is_interested(text):
    assert classified(text).category == "interested"


def test_worth_a_talk_with_a_question_about_when_is_interest():
    assert read("Probably worth a conversation, when are you free?")[0] == "interested"


@pytest.mark.parametrize("text", ["Worth a call? No.", "Is this worth a chat?", "Is this worth a chat with your team?", "Would it be worth a call, do you think?"])
def test_asking_whether_it_is_worth_a_talk_is_no_good_news(text):
    assert read(text)[:2] == ("question", "medium")


@pytest.mark.parametrize("text", ["Yes", "Yes please", "Not worth a call."])
def test_a_bare_yes_stays_for_a_person(text):
    assert classified(text).category != "interested"


# ------------------------------------------------------------------ reports, quotes, quarters
def test_a_plain_sendmail_report_names_the_address_that_failed():
    v = SimpleNamespace(to="gone@lead.example", dom="lead.example", mid="<ours-1@own.example>", subj="Question", own="sales@own.example", own_dom="own.example")
    assert parse_message(K.sendmail_no_domain(v), {v.own}).bounced_addresses == [v.to]


@pytest.mark.parametrize("kind, words", [("delay", "Still being tried: nothing to do yet"), ("sending_problem", "Our own sending was refused: check the mail server")])
def test_a_delay_or_a_refusal_of_our_sending_does_not_call_the_address_invalid(kind, words):
    r = SimpleNamespace(category="bounce", mail_kind=kind, recommended_status="Address invalid: find another contact")
    assert recommended_for(r) == words


def test_a_real_bounce_still_calls_the_address_invalid():
    r = SimpleNamespace(category="bounce", mail_kind="bounce", recommended_status="Address invalid: find another contact")
    assert recommended_for(r) == "Address invalid: find another contact"


WRAPPED_QUOTE = "You should talk to Sam who does this.\n\nOn Fri, Oct 30, 2026 at 1:09 PM Sales <sales@us.example>\nwrote:\n\n> Hello\n"


def test_a_wrapped_gmail_quote_line_ends_the_reply():
    assert new_text(WRAPPED_QUOTE) == "You should talk to Sam who does this."
    assert "sales@us.example" not in rest_of_reply(WRAPPED_QUOTE) and "wrote:" not in rest_of_reply(WRAPPED_QUOTE)


def test_our_own_address_is_not_one_they_gave():
    assert named_addresses(SimpleNamespace(from_email="p@x.example", to_email="sales@us.example", new_text="Ask sam@x.example or sales@us.example or p@x.example", body_text="")) == ["sam@x.example"]


def test_an_opt_out_below_a_wrapped_quote_line_is_still_read():
    assert read("Thanks.\n\nOn Fri, Oct 30, 2026 at 1:09 PM Sales <sales@us.example>\nwrote:\n\n> Hello\n\nPlease stop emailing me.")[0] == "unsubscribe"


@pytest.mark.parametrize("text, due, words", [
    ("Not now but try in Q2", datetime(2027, 4, 1, 14), "in Q2"), ("Try again in Q2", datetime(2027, 4, 1, 14), "in Q2"), ("Check back next quarter", datetime(2027, 1, 1, 14), "next quarter"),
    ("Reach out in the new year", datetime(2027, 1, 1, 14), "in the new year"), ("Try me again in March.", datetime(2027, 3, 1, 14), "in March"), ("Please check back after Q1.", datetime(2027, 4, 1, 14), "after Q1"),
    ("Check back in Q4.", datetime(2026, 11, 5, 9), "in Q4"), ("Check back in Q3 2028.", datetime(2028, 7, 1, 14), "in Q3 2028"),
])
def test_a_quarter_or_the_new_year_gets_a_date(text, due, words, monkeypatch):
    now = datetime(2026, 11, 5, 9)
    monkeypatch.setattr(ri, "utcnow", lambda: now)
    when = check_back_when(row(text))
    assert when["words"] == words
    assert ri._month_due(when["month"], now, when["year"], when["after"], when["part"]) == due


@pytest.mark.parametrize("text", ["We try in March to close the books.", "Do not check back in Q2.", "Our CFO will get back to you next quarter.", "Our year ends in Q2."])
def test_nobody_asked_for_a_quarter_here(text):
    assert check_back_when(row(text)) is None


def test_not_now_but_try_in_q2_gets_a_task_and_a_draft_that_does_not_ask_again(make_ctx):
    ctx, r = answer(make_ctx, "Not now but try in Q2")
    assert r.category == "objection"
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending", FollowUp.subject.like("Check back%"))).scalars().one().due_at.month in (4, 5, 6)
        draft = s.execute(select(FollowUp).where(FollowUp.kind == "response")).scalars().one()
        assert "check back in Q2" in draft.body and "checked back in a few months" not in draft.body


# ------------------------------------------------------------------ Calendly: who it is from, and what they typed themselves
SITE_DAVID = f"david.chen@{SITE}"
NOTICE_BODY = ("Calendly\n\nEvent Type:\n30 Minute Meeting\n\nInvitee:\nDavid Chen\n\nInvitee Email:\n" + SITE_DAVID + "\n\nEvent Date/Time:\n8:30pm - Tuesday, 8 December 2026 (India Standard Time)\n\n"
               "Location:\nGoogle Meet\n\nInvitee Time Zone:\nEastern Time - US & Canada\n\nView event in Calendly\nhttps://calendly.com/app\n")
NOTICE_SUBJECT = "New Event: David Chen - 8:30pm Tue, 8 Dec 2026 - 30 Minute Meeting"
FORWARD = "---------- Forwarded message ---------\nFrom: Calendly <notifications@calendly.com>\nSubject: " + NOTICE_SUBJECT + "\n\n"
UNREADABLE = "Something about https://calendly.com/app with no time in it\n\nUnsubscribe from these emails"


def calendly_ctx(make_ctx, verified_only=False):
    ctx, _sender, sent = contacted(make_ctx, calendly_verified_only=verified_only, calendly_url="https://calendly.com/owner-example/30min")
    return ctx, sent


def deliver(ctx, raw):
    FakeIMAP.messages[max(FakeIMAP.messages, default=0) + 1] = raw
    run_inbox(ctx)
    return latest_reply(ctx)


def meetings(ctx):
    from aime.models import Meeting
    with ctx.db.session() as s:
        return s.execute(select(Meeting)).scalars().all()


@pytest.mark.parametrize("address", ["pat@mycalendly.com", "pat@calendly.com.example", "pat@calendly.com", "pat@team.calendly.com"])
def test_an_in_thread_opt_out_from_a_domain_containing_calendly_com_is_honoured(make_ctx, address):
    ctx, sent = calendly_ctx(make_ctx)
    r = deliver(ctx, mail(f"Pat <{address}>", f"Re: {sent.subject}", "Please unsubscribe me and do not contact me again.", in_reply_to=sent.message_id, msg_id="<o0@mail.example>"))
    assert r.category == "unsubscribe" and address in suppressed(ctx)


def test_an_opt_out_whose_subject_starts_cancelled_and_signature_has_a_calendly_link_is_honoured(make_ctx):
    ctx, _sent = calendly_ctx(make_ctx)
    r = deliver(ctx, mail(f"David Chen <{DAVID}>", "Cancelled: our trial. Please remove me from your list",
                          "Please remove me from your list and stop emailing me.\n\nDavid\nBook me: https://calendly.com/david-chen/15min", msg_id="<o1@mail.example>"))
    assert r.category == "unsubscribe" and DAVID in suppressed(ctx)


def test_an_aw_reply_quoting_a_notice_with_an_opt_out_above_it_is_honoured(make_ctx):
    ctx, _sent = calendly_ctx(make_ctx)
    r = deliver(ctx, mail(f"David Chen <{DAVID}>", "AW: Canceled: 30 Minute Meeting with David Chen", f"Bitte keine Werbung. Please remove me from your list.\n\n{FORWARD}{NOTICE_BODY}", msg_id="<o2@mail.example>"))
    assert r.category == "unsubscribe" and DAVID in suppressed(ctx)


def test_an_invitee_forwarding_a_readable_notice_with_an_opt_out_above_it_is_honoured(make_ctx):
    ctx, _sent = calendly_ctx(make_ctx)
    r = deliver(ctx, mail(f"David Chen <{DAVID}>", f"Fwd: {NOTICE_SUBJECT}", f"Cancelling this. Please remove me from your list.\n\n{FORWARD}{NOTICE_BODY}", msg_id="<o3@mail.example>"))
    assert r.category == "unsubscribe" and DAVID in suppressed(ctx) and not meetings(ctx)


def test_a_real_notice_from_calendly_is_still_applied(make_ctx):
    ctx, _sent = calendly_ctx(make_ctx)
    r = deliver(ctx, mail("Calendly <notifications@calendly.com>", NOTICE_SUBJECT, NOTICE_BODY, msg_id="<c1@calendly.example>"))
    assert r.category == "calendly" and len(meetings(ctx)) == 1 and not suppressed(ctx)


def test_a_notice_nobody_signed_waits_for_a_person(make_ctx):
    ctx, _sent = calendly_ctx(make_ctx, verified_only=True)
    r = deliver(ctx, mail("Calendly <notifications@calendly.com>", NOTICE_SUBJECT, NOTICE_BODY, msg_id="<c2@calendly.example>"))
    assert (r.category, r.status) == ("calendly", "needs_review") and not meetings(ctx) and not suppressed(ctx)


def test_a_forwarded_notice_with_an_ordinary_note_is_a_notice_exactly_as_before(make_ctx):
    ctx, _sent = calendly_ctx(make_ctx)
    r = deliver(ctx, mail(f"David Chen <{DAVID}>", f"Fwd: {NOTICE_SUBJECT}", f"See you then.\n\n{FORWARD}{NOTICE_BODY}", msg_id="<c3@calendly.example>"))
    assert r.category == "calendly" and DAVID not in suppressed(ctx)


def test_our_own_mailbox_forwarding_an_unreadable_notice_is_not_suppressed(make_ctx):
    ctx, _sent = calendly_ctx(make_ctx)
    r = deliver(ctx, mail("Sales <sales@us.example>", "Fwd: Canceled: something", f"{FORWARD}{UNREADABLE}", msg_id="<c4@calendly.example>"))
    assert (r.category, r.status) == ("calendly", "needs_review") and "sales@us.example" not in suppressed(ctx) and not suppressed(ctx)


@pytest.mark.parametrize("words", ["He said to remove him from your list.", "Please unsubscribe him."])
def test_a_trusted_forwarders_typed_line_never_suppresses_them_and_a_person_reads_it(make_ctx, words):
    ctx, _sent = calendly_ctx(make_ctx)
    r = deliver(ctx, mail("Sales <sales@us.example>", f"Fwd: {NOTICE_SUBJECT}", f"{words}\n\n{FORWARD}{NOTICE_BODY}", msg_id="<c5@calendly.example>"))
    assert (r.category, r.status) == ("calendly", "needs_review") and not suppressed(ctx)
    assert ri.TYPED_ABOVE_NOTICE in r.reasons


def test_anothers_typed_answer_inside_the_notice_is_never_read_as_the_forwarders():
    text = f"{FORWARD}{NOTICE_BODY}\nQuestions:\nPlease remove me from your list."
    r = row(text, subject=f"Fwd: {NOTICE_SUBJECT}", from_email="someone@else.example")
    assert classify(r)[0] == "calendly"


@pytest.mark.parametrize("address, is_calendly", [("notifications@calendly.com", True), ("NOTIFICATIONS@Calendly.COM", True), ("x@team.calendly.com", True), ("pat@mycalendly.com", False),
                                                  ("pat@calendly.com.example", False), ("calendly.com", False), ("", False), (None, False), ("calendly.com@evil.example", False)])
def test_the_sender_is_calendly_only_by_its_whole_domain(address, is_calendly):
    assert ri.calendly_sender(address) is is_calendly


@pytest.mark.parametrize("address, subject, body, expected", [
    ("notifications@calendly.com", "New Event: x", "", True), ("pat@mycalendly.com", "New Event: x", "", False), ("pat@calendly.com.example", "Canceled: x", "", False),
    ("pat@mycalendly.com", "New Event: x", "see calendly.com", True), ("notifications@calendly.com", "Weekly tips", "", False),
])
def test_what_the_mailbox_keeps_as_a_notice(address, subject, body, expected):
    assert looks_like_calendly(SimpleNamespace(from_email=address, subject=subject, body_text=body)) is expected
