"""Repro of confirmed defects, each through the real InboxAgent with RFC822 bytes. """
from __future__ import annotations

import pytest

from tests.uat import p6_reply_harness as H

from aime.crm_ops import is_suppressed
from aime.models import FollowUp, Lead
from aime.timeutil import utcnow
from tests.test_inbox import DAVID, FakeIMAP, contacted, run_inbox
from tests.uat.test_p5_replies_acting import second_lead_by_inquiry

DESTROY = ("\n\nCONFIDENTIALITY NOTICE: This e-mail message is for the sole use of the intended recipient(s) and may contain confidential and privileged information. "
           "If you are not the intended recipient, please contact the sender by reply e-mail and destroy all copies of the original message.")
DELETE = ("\n\nThis email is confidential and intended solely for the addressee. If you have received this email in error, please notify the sender and remove it from your system.")


def go(make_ctx, text, **kw):
    return H.run_one(make_ctx, lambda s: H.raw_mail(s, text, **kw))


# D1 critical: a clear no with a legal threat or "not interested" behind a soft wording is not suppressed, and the company's other lead keeps going
LEGAL_NO = ["Stop with the emails, talk to my lawyer", "Enough emails, I'm calling the FTC", "Enough of this, talk to our counsel", "No need to contact us again, speak with our attorney",
            "No need to write again, talk to my lawyer", "Enough of your emails. I will call the police", "Stop with the emails. Not interested.", "Stop with the emails, I'm not interested",
            "No need to email me again, I'm not interested", "Enough emails. Not interested.", "Stop with the messages. Call me again and I sue.",
            "Stop with the emails, talk to my lawyer" + DESTROY, "Stop with the emails, talk to my lawyer" + DELETE, "Stop with the emails. Not interested." + DESTROY]


@pytest.mark.parametrize("text", LEGAL_NO)
def test_d1_soft_no_with_legal_threat_or_no_interest_is_suppressed(make_ctx, text):
    r = go(make_ctx, text)
    assert r["cat"] == "unsubscribe" and r["susp"]


def test_d1_and_the_colleagues_queued_email_is_left_standing(make_ctx):
    ctx, _s, sent = contacted(make_ctx)
    _first, other = second_lead_by_inquiry(ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, other)
        f = FollowUp(lead_id=other, company_id=lead.company_id, channel="email", kind="outreach", step=1, due_at=utcnow(), status="approved", subject="Hello", body="A note", to_email="sam@x.example")
        s.add(f)
        s.flush()
        fid = f.id
    FakeIMAP.messages[max(FakeIMAP.messages, default=0) + 1] = H.raw_mail(sent, "Stop with the emails, talk to my lawyer")
    run_inbox(ctx)
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "cancelled"
        assert is_suppressed(s, DAVID)


WARM_CONTROL = ["Stop with the emails, have your attorney call ours and let's close the contract.", "Stop with the emails, talk to our counsel about the contract and let's close this.",
                "Stop with the emails, I'm interested, call me.", "Stop with the emails, let's meet."]


@pytest.mark.parametrize("text", WARM_CONTROL)
def test_control_d1_warm_with_a_lawyer_stays_unsuppressed(make_ctx, text):
    assert not go(make_ctx, text)["susp"]


# D2: stop the email but ask for a talk: the opt-out stands until the owner rules otherwise, and a person now reads it
WARM_STOP = ["Stop the emails and call me", "Can we stop emailing and just talk?", "Let's stop emailing and just talk.", "Stop emailing and call me.", "Stop emailing me and call me instead",
             "Let's stop the email tag and get on a call.", "Please stop emailing and give me a call Tuesday.", "Stop sending emails, just call me at 312-555-0142.", "Quit emailing and pick up the phone.",
             "Don't email me, call me.", "Don't send more emails, just call."]


@pytest.mark.parametrize("text", WARM_STOP)
def test_d2_stop_emailing_and_call_me_reaches_a_person(make_ctx, text):
    r = go(make_ctx, text)
    assert r["status"] == "needs_review" and r["susp"], r


@pytest.mark.parametrize("text", ["Stop the emails and call off your team.", "Stop the emails and talk to someone else.", "Stop emailing me.", "Don't email me, don't call me.", "Stop emailing and call me never.",
                                  "Stop emailing me or I will call my lawyer", "Stop emailing and call your manager, I want to complain."])
def test_control_d2_real_opt_outs_stay(make_ctx, text):
    assert go(make_ctx, text)["susp"]


# D3 high: suppressed and marked done with no person told, though they name a new address or person to reach
@pytest.mark.parametrize("text", ["Stop emailing me at this address, use david@home.example", "Please don't email this address anymore, I moved to david@newco.example.", "Remove me and contact Sara instead.",
                                  "Wrong person, please remove me and talk to Sara.", "Please take me off this thread and talk to Sara."])
def test_d3_opt_out_naming_where_to_go_reaches_a_person(make_ctx, text):
    r = go(make_ctx, text)
    assert r["susp"] and r["status"] == "needs_review"


# D4 critical (known limit): a non-English yes above a delete-style notice suppresses a warm buyer
@pytest.mark.parametrize("text", ["Oui, ça m'intéresse." + DELETE, "Sí, me interesa." + DELETE, "हाँ, मुझे दिलचस्पी है" + DELETE, "Oui" + DELETE, "Sí, por favor" + DELETE])
def test_d4_foreign_yes_above_a_notice_is_not_suppressed(make_ctx, text):
    r = go(make_ctx, text)
    assert not r["susp"] and r["status"] == "needs_review"


@pytest.mark.parametrize("text", ["Abmelden" + DELETE, "Graag uitschrijven" + DELETE, "no quiero mas correos" + DELETE, "Non, merci de me retirer" + DELETE, "No me interesa, baja" + DELETE])
def test_control_d4_foreign_no_above_a_notice_stays(make_ctx, text):
    assert go(make_ctx, text)["susp"]


# D5 critical: our own email, quoted without > under "On ... wrote:" (Yahoo, AOL), is read as their words
OUR_ASK = "Hi David,\n\nWould you be interested in a short call next week? What does your team use today?\n\n---\nTo stop receiving emails from us, reply with \"unsubscribe\" or write to sales@us.example.\n"


@pytest.mark.parametrize("words", ["Thanks, will read this later.", "Got it."])
def test_d5_an_unprefixed_quote_causes_no_question_draft_or_deal(make_ctx, words):
    r = H.run_one(make_ctx, lambda s: H.raw_mail(s, f"{words}\n\nSent from Yahoo Mail on Android\n\n  On Tue, Oct 27, 2026 at 11:02 AM, Sales<sales@us.example> wrote:\n  {OUR_ASK}", whole=True))
    assert r["cat"] == "other" and not r["resp"] and not r["opp"], r


def test_control_d5_a_bottom_post_opt_out_under_a_quote_still_counts(make_ctx):
    r = H.run_one(make_ctx, lambda s: H.raw_mail(s, f"On Tue, Oct 27, 2026 at 11:02 AM, Sales<sales@us.example> wrote:\n  {OUR_ASK}\nPlease stop emailing me.\n", whole=True))
    assert r["susp"]


# D6 high: asked to come back in a quarter, no reminder exists
@pytest.mark.parametrize("text", ["Not now, try Q2.", "not now, try me in Q2", "Not right now, try us in Q3."])
def test_d6_not_now_try_q2_makes_a_task(make_ctx, text):
    assert go(make_ctx, text)["tasks"]


# D7 high: clear refusals read as nothing: not suppressed, company's other leads go on
@pytest.mark.parametrize("text", ["Fuck off", "F off", "Piss off", "Get off my back", "for the last time, stop", "How many times do I have to tell you to stop?", "I've asked you three times to quit it",
                                  "unsubscirbe", "rmeove me", "remove em", "do not emial me", "pleas stop", "no more emial", "dnc", "Lose this address.", "Any further contact will be referred to counsel.",
                                  "Direct all further communication to our legal counsel.", "Next email and I file a complaint with the attorney general."])
def test_d7_plain_english_refusals_count(make_ctx, text):
    assert go(make_ctx, text)["susp"]


@pytest.mark.parametrize("text,fmt", [("Eliminen mi correo de su lista", "es_gmail"), ("baja" + DESTROY, "es_gmail"), ("मुझे ईमेल मत भेजो", "hi_gmail"), ("कृपया मुझे अपनी सूची से हटा दें", "hi_gmail"), ("बंद करो", "hi_gmail"), ("hata do list se", "hi_gmail"), ("stop merci", "fr_gmail")])
def test_d7_foreign_refusals_count(make_ctx, text, fmt):
    assert go(make_ctx, text, fmt=fmt)["susp"]


# D8 medium: category
@pytest.mark.parametrize("text", ["Call me Tuesday.", "Send me a calendar invite.", "Please call me."])
def test_d8_asks_to_talk_reads_as_interest(make_ctx, text):
    assert go(make_ctx, text)["cat"] == "interested"


@pytest.mark.parametrize("text,fmt", [("No thank you", "gmail"), ("No, thank you.", "gmail")])
def test_d8_plain_no_reads_as_not_interested(make_ctx, text, fmt):
    assert go(make_ctx, text, fmt=fmt)["cat"] == "not_interested"


# D10 medium: text on Replies keeps the quote header or our email
@pytest.mark.parametrize("fmt,bad", [("gmail_wrap", "On Tue"), ("hi_gmail", "ने लिखा"), ("bare_wrote", "Sales wrote"), ("yahoo", "Sent from Yahoo"), ("outlook_ios", "Get Outlook"), ("begin_fwd", "Begin forwarded")])
def test_d10_replies_shows_only_their_words(make_ctx, fmt, bad):
    assert bad not in go(make_ctx, "Yes, let's talk Tuesday.", fmt=fmt)["new"]


# Known limit: reads as a date
def test_known_pest_is_no_date(make_ctx):
    r = go(make_ctx, "Don't email me until April, I'm busy and you're a pest")
    assert r["susp"] and not r["tasks"]


def test_plain_date_control(make_ctx):
    r = go(make_ctx, "Don't email me until April, I'm busy.")
    assert not r["susp"] and r["tasks"]


# Extra probes
@pytest.mark.parametrize("text", ["How did you get my email? Anyway, yes, let's talk Tuesday.", "Who gave you my email? Sounds interesting though, can we talk?"])
def test_probe_how_did_you_get_with_interest_reaches_a_person(make_ctx, text):
    assert go(make_ctx, text)["status"] == "needs_review"


def test_probe_vendor_footer_in_the_middle_of_a_chain(make_ctx):
    body = ("Thanks.\n\nFrom: Bob Vendor <bob@vendor.example>\nSent: Monday\nTo: David\nSubject: fyi\n\nSee our new catalog.\nUnsubscribe from this list\n\nFrom: Sales <sales@us.example>\nSent: Tuesday\nTo: David\nSubject: x\n\n{B}\n")
    r = H.run_one(make_ctx, lambda s: H.raw_mail(s, body, whole=True))
    assert not r["susp"], r
