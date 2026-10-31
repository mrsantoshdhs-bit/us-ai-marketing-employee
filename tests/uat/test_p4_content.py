"""Phase 4 content review: what a person reads, on synthetic data. The first group is the tester's, each failing on release #74; the
rest is what those did not cover. Nothing here sends, approves by itself or publishes: mail goes to the fake SMTP server."""
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.agents.marketing_manager import MarketingManagerAgent
from aime.config import Settings
from aime.content import title_belongs, write_post
from aime.integrations.email_smtp import compliance_footer
from aime.models import Activity, Campaign, Company, Contact, ContentItem, FollowUp, InboundEmail, Lead, Opportunity
from aime.outreach import draft_outreach, sent_earlier
from aime.qc.checks import check_claims
from aime.qc.email_checks import run_email_checks
from aime.reply_intel import (QUESTION_PLACEHOLDER, _month_due, check_back_month, classify, first_name, intent_of, note_now, offered_day, recommended_for, reply_footer,
                              sentiment_and_recommendation, suggest)
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.conftest import FakeLLM
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import DAVID as TO
from tests.test_inbox import FakeIMAP, bounce_for, contacted, latest_reply, mail, run_inbox
from tests.test_outreach import drafts, lead_of
from tests.test_automation import followup, run
from tests.uat.test_p2_truth import ORIGIN, add_lead, client, org  # noqa: F401
from tests.uat.test_uat_email import deliver, in_thread

@pytest.fixture()
def st(tmp_path):
    (tmp_path / "data").mkdir()
    return Settings.load(env_file=tmp_path / "none.env", data_dir=tmp_path / "data", sender_name="Sam",
                         company_postal_address="Digital Hub Solution, 1 Main Street, Austin, TX 78701", unsubscribe_email="out@sender.example",
                         calendly_url="https://calendly.com/uat/30min")


def lead(name="acmewidgets.com", title="Acme Widgets | Widgets for all", domain="acmewidgets.com", service="custom_software"):
    return NS(company=NS(name=name, website_title=title, domain=domain), primary_service=service)


def inbox():
    return NS(role_category="inbox", first_name=None, source="website")


def row(text, from_name="Pat Lee"):
    return InboundEmail(from_email="pat@acmewidgets.com", from_name=from_name, subject="Re: Question about internal tools", new_text=text,
                        body_text=text, bounced_addresses=[], auto_submitted=False, is_simulated=False)


def answer(st, text, from_name="Pat Lee", lead_=None):
    r = row(text, from_name)
    r.category = classify(r)[0]
    r.intent = intent_of(r)
    return r, suggest(st, r.category, r, None, lead_ or lead())


def test_step_2_keeps_the_subject_of_step_1_so_it_stays_in_the_thread(st):
    one, _ = draft_outreach(st, 1, lead(), inbox(), [NS(kind="description", fact="We make widgets for all.")])
    two, _ = draft_outreach(st, 2, lead(), inbox(), [])
    assert re.sub(r"^re:\s*", "", two, flags=re.I) == one


def test_answer_to_interest_names_the_company_as_step_2_does(st):
    _, (_, body) = answer(st, "Sounds interesting.")
    _, step2 = draft_outreach(st, 2, lead(), inbox(), [])
    assert "Acme Widgets" in step2
    assert "acmewidgets.com" not in body and "Custom Software Development" not in body


@pytest.mark.parametrize("name,hello", [("Lee, Pat", "Hi Pat,"), ("Dr. Pat Lee", "Hi Pat,"), ("P. Lee", "Hi there,"), ("PAT LEE", "Hi Pat,"),
                                        ("Acme Widgets", "Hi there,")])
def test_greeting_from_the_display_name(st, name, hello):
    _, (_, body) = answer(st, "Sounds interesting.", from_name=name, lead_=lead(name="Acme Widgets", title=None))
    assert body.startswith(hello + "\n")


def test_a_day_they_refused_is_not_the_day_we_say_they_mentioned(st):
    _, (_, body) = answer(st, "Interested. Friday is impossible for me, but Tuesday works.")
    assert "You mentioned Friday" not in body


def test_not_the_right_person_gave_no_direction(st):
    r, suggested = answer(st, "I'm not the right person for this.")
    assert suggested is None  # corrected: the owner's decision is no draft at all, as for "Wrong person, sorry"
    assert "referred" not in sentiment_and_recommendation(r.category, r.intent)[1]


@pytest.mark.parametrize("text", ["You should talk to Sam Ray, sam@acmewidgets.com, who runs engineering.", "I've forwarded your note to our CTO."])
def test_a_referral_is_not_asked_for_what_it_just_gave(st, text):
    _, (_, body) = answer(st, text)
    assert "best way to reach them" not in body and "forward my note" not in body


def test_check_back_in_march_is_not_asked_again(st):
    _, (_, body) = answer(st, "Not a priority this year. Please check back in March.")
    assert "or would you rather I did not" not in body


def test_no_budget_is_no_pricing_question(st):
    r, _ = answer(st, "No budget at the moment.")
    assert r.intent != "pricing_request"
    assert sentiment_and_recommendation(r.category, r.intent)[0] == "negative"


def test_reply_footer_writes_the_company_once(st):
    _, (_, body) = answer(st, "Sounds interesting.")
    assert f"{st.company_name} · {st.company_name}" not in body


def test_asked_for_details_first_is_not_answered_with_a_booking_link(st):
    r, (_, body) = answer(st, "Interested, please send me some details first. We don't do calls before reading something.")
    assert r.intent == "more_info"
    assert "calendly.com" not in body


# =================================================== beyond the tester's cases: the thread


def checks(subject, sent=None, step=2):
    body = "Hello team,\n\n" + "A short and plain note about the way your team works today. " * 5 + "\n---\nreply unsubscribe"
    report = run_email_checks(subject, body, [], None, False, step=step, sent_subjects=sent)
    return next(c for c in report["checks"] if c["name"] == "subject line")


@pytest.mark.parametrize("subject, sent, status", [
    ("Re: Question about CRM", None, "fail"),  # a fake one on a first email
    ("Re: Question about CRM", [], "fail"),
    ("Re: Question about CRM", ["Something else"], "fail"),  # answers nothing we sent under that subject
    ("Fwd: Question about CRM", ["Question about CRM"], "fail"),
    ("Re: Question about CRM", ["Question about CRM"], "pass"),
    ("RE:  question about  crm", ["Question about CRM"], "pass"),
    ("Question about CRM", None, "pass"),
])
def test_re_passes_only_on_an_answer_to_an_email_really_sent(subject, sent, status):
    assert checks(subject, sent)["status"] == status


def test_the_two_characters_of_re_do_not_push_a_full_length_subject_over_the_limit():
    first = "Question about tools " * 3 + "abcdefg"
    assert len(first) == 70 and checks(f"Re: {first}", [first])["status"] == "pass"
    assert checks("x" * 71, [first])["status"] == "fail"


def test_step_3_keeps_the_subject_too_and_the_one_step_1_really_went_out_under(st):
    one, _ = draft_outreach(st, 1, lead(), inbox(), [NS(kind="description", fact="We make widgets for all.")])
    assert draft_outreach(st, 3, lead(), inbox(), [])[0] == f"Re: {one}"
    assert draft_outreach(st, 2, lead(), inbox(), [], first_subject="A person's own words")[0] == "Re: A person's own words"
    assert draft_outreach(st, 2, lead(), inbox(), [], first_subject="Re: Already one")[0] == "Re: Already one"


def test_step_3_does_not_thank_people_who_never_answered(st):
    _, body = draft_outreach(st, 3, lead(), inbox(), [])
    assert "Thank you" not in body and "last note" in body


def test_real_drafts_keep_the_thread_pass_their_checks_and_pass_again_when_edited(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    run(ctx, sender, TUESDAY_11AM_ET + timedelta(days=4, minutes=5))
    step2 = drafts(ctx)[-1]
    assert (step2.step, step2.subject, step2.in_reply_to) == (2, f"Re: {sent.subject}", sent.message_id)
    assert step2.quality["qc"]["status"] == "passed", step2.quality["qc"]
    TestClient(create_app(ctx)).post(f"/followups/{step2.id}/save", data={"subject": step2.subject, "body": step2.body + " "}, headers=ORIGIN)
    assert followup(ctx, step2.id).quality["qc"]["status"] == "passed"
    TestClient(create_app(ctx)).post(f"/followups/{step2.id}/save", data={"subject": "Re: Your invoice", "body": step2.body}, headers=ORIGIN)
    assert followup(ctx, step2.id).quality["qc"]["status"] == "failed"  # a person cannot turn it into a fake reply unnoticed


def test_an_email_already_waiting_keeps_its_stored_subject(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        s.add(FollowUp(lead_id=sent.lead_id, contact_id=sent.contact_id, company_id=sent.company_id, channel="email", kind="outreach", step=2,
                       due_at=utcnow(), status="awaiting_approval", subject="Following up: manual data entry", body="x", to_email=sent.to_email,
                       in_reply_to=sent.message_id))
    run(ctx, sender, TUESDAY_11AM_ET + timedelta(days=5))
    assert [d.subject for d in drafts(ctx) if d.step == 2] == ["Following up: manual data entry"]


def test_what_was_sent_before_comes_only_from_this_lead_and_only_if_it_went_out(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        assert [f.id for f in sent_earlier(s, sent.lead_id, sent.message_id)] == [sent.id]
        assert sent_earlier(s, sent.lead_id + 999, sent.message_id) == []
        assert sent_earlier(s, sent.lead_id, "<someone-elses@x.example>") == [] and sent_earlier(s, sent.lead_id, None) == []
        s.get(FollowUp, sent.id).status = "uncertain"
        assert sent_earlier(s, sent.lead_id, sent.message_id) == []
        s.rollback()


def test_the_approval_page_shows_the_sent_text_read_only_in_both_kinds_of_draft(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    run(ctx, sender, TUESDAY_11AM_ET + timedelta(days=4, minutes=5))
    page = TestClient(create_app(ctx)).get("/followups").text
    part = page[page.index("Already sent in this thread"):]
    assert "Already sent in this thread (1)" in page and sent.subject in part[:part.index("</details>")]
    shown = part[:part.index("</details>")]
    assert "<textarea" not in shown and "<form" not in shown
    assert re.sub(r"\s+", " ", sent.body.split("\n\n")[1]).replace('"', "&#34;")[:60] in re.sub(r"\s+", " ", shown)
    # an answer to their reply: what we sent before is beside it too
    deliver(ctx, in_thread(sent, "How long would this take?"))
    page = TestClient(create_app(ctx)).get("/followups").text
    assert "Already sent in this thread (1)" in page


# =================================================== beyond the tester's cases: referrals
def test_wrong_person_gets_the_same_path_with_or_without_the_right_words(make_ctx):
    for text in ("I'm not the right person for this.", "Sorry, I don't think I'm the right person.", "Wrong person, sorry."):
        ctx, _, sent = contacted(make_ctx)
        deliver(ctx, in_thread(sent, text))
        r = latest_reply(ctx)
        assert (r.category, r.status, r.suggested_followup_id, r.sentiment) == ("other", "needs_review", None, "neutral"), text
        assert "referred" not in r.recommended_status


@pytest.mark.parametrize("text, category", [("Sam is the right person for this.", "referral"), ("I'm not the right person, you should talk to Sam Ray.", "referral"),
                                            ("Not the right person. Please remove me.", "unsubscribe")])
def test_a_real_referral_or_opt_out_beside_those_words_stays_what_it_is(text, category):
    assert classify(row(text))[0] == category


def test_a_named_address_is_a_persons_decision_and_nothing_goes_there(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    other = "sam.ray@brightline.example"
    deliver(ctx, in_thread(sent, f"You should talk to Sam Ray, {other}, who runs engineering."))
    r = latest_reply(ctx)
    assert r.status == "needs_review" and other in r.action_note and "a person decides" in r.action_note
    assert "nothing goes there by itself" in r.recommended_status.lower()
    with ctx.db.session() as s:
        draft = s.get(FollowUp, r.suggested_followup_id)
        assert draft.to_email != other and draft.status == "awaiting_approval" and "best way to reach" not in draft.body
        assert not s.execute(select(FollowUp).where(FollowUp.to_email == other)).first()
        assert not s.execute(select(Contact).where(Contact.email == other)).first()


def test_a_referral_with_nothing_to_go_on_still_asks(st):
    _, (_, body) = answer(st, "Please reach out to our head of operations.")
    assert "best way to reach them" in body


@pytest.mark.parametrize("text", ["I've forwarded your note to our CTO.", "Looping in Sam from our IT team, cc'd.", "I'm forwarding this to our CTO."])
def test_someone_who_passed_it_on_is_thanked_and_not_asked_anything(st, text):
    r, (_, body) = answer(st, text)
    assert "Thank you" in body and "?" not in body.split("---")[0]
    r.recommended_status = "x"
    assert recommended_for(r) == "Wait to hear from the person they passed it to"


def test_their_own_address_in_a_signature_is_no_referral_to_it(st):
    r, (_, body) = answer(st, "Please reach out to our head of operations.\n\nPat Lee\npat@acmewidgets.com")
    assert "best way to reach them" in body


# =================================================== beyond the tester's cases: what the answer says
def test_an_interested_answer_uses_plain_words_also_without_a_booking_link(st):
    st.calendly_url = None
    _, (_, body) = answer(st, "Sounds interesting.")
    assert "internal tools at Acme Widgets" in body and "two or three times that suit you" in body


def test_with_no_name_fit_to_write_the_answer_names_no_company(st):
    _, (_, body) = answer(st, "Sounds interesting.", lead_=lead(name="acmewidgets.com", title=None))
    assert "acmewidgets" not in body.split("---")[0]


@pytest.mark.parametrize("text, day", [
    ("Can we talk Thursday?", "Thursday"), ("Friday is impossible for me, but Tuesday works.", "Tuesday"), ("Tuesday works, not Friday.", "Tuesday"),
    ("How about tomorrow afternoon?", None), ("Can we talk today?", None), ("Friday is impossible.", None), ("We can't do Monday.", None),
    ("Monday or Tuesday works.", None), ("Monday works. Tuesday works too.", None), ("We had a board review on Monday.", None), ("I am away until Friday.", None),
    ("Anytime except Friday works.", None), ("Not Thursday, sorry.", None), ("Interested.", None), ("Can we talk next week?", "next week"),
    ("Not this week, sorry.", None), ("Thursday morning is good.", "Thursday"), ("Friday doesn’t work for me.", None),
])
def test_a_day_is_named_only_when_the_text_clearly_offers_that_one(text, day):
    assert offered_day(text) == day


@pytest.mark.parametrize("calendly", [True, False])
@pytest.mark.parametrize("text", ["Interested. How about tomorrow afternoon?", "Interested. Today works.", "Interested. Friday is impossible."])
def test_the_answer_echoes_no_refused_or_relative_day(st, text, calendly):
    if not calendly:
        st.calendly_url = None
    _, (_, body) = answer(st, text)
    low = body.lower()
    assert "tomorrow" not in low and "today" not in low and "friday" not in low and "you mentioned" not in low


@pytest.mark.parametrize("name, first", [
    ("Lee, Pat", "Pat"), ("Lee, Pat M.", "Pat"), ("Pat Lee, CEO", "Pat"), ("Pat Lee, PhD", "Pat"), ("Dr. Pat Lee", "Pat"), ("Prof Dr Pat Lee", "Pat"), ("Lee, Dr. Pat", "Pat"),
    ("PAT LEE", "Pat"), ("pat lee", "Pat"), ("Pat", "Pat"), ("Mary-Jane O'Neil", "Mary-Jane"), ("MARY-JANE LEE", "Mary-Jane"), ("José Núñez", "José"), ("Pat Lee (Sales)", "Pat"),
    ("Pat Lee | Acme", "Pat"), ("Pat Lee via LinkedIn", "Pat"), ('"Pat Lee"', "Pat"),
    ("P. Lee", None), ("P Lee", None), ("J.R. Lee", None), ("TJ Lee", None), ("Mr. Lee", None), ("Dr. Lee", None), ("Lee, P.", None), ("ACME", None), ("hello@acme.example", None), ("", None), (None, None),
    ("Sales Team", None), ("Acme Inc.", None), ("Acme, Inc.", None), ("Northwind Solutions", None), ("Accounts Payable", None), ("123 Corp", None),
])
def test_first_name_only_when_it_plainly_is_one(name, first):
    assert first_name(name) == first


@pytest.mark.parametrize("name", ["Acme Widgets", "ACME WIDGETS", "Acme", "acmewidgets"])
def test_the_companys_own_name_is_no_first_name(name):
    assert first_name(name, lead(name="Acme Widgets", title=None)) is None


def test_no_display_name_falls_back_to_the_contact_and_then_to_neutral(st):
    r = row("Sounds interesting.", None)
    r.category, r.intent = "interested", None
    assert suggest(st, "interested", r, NS(first_name="Dana", email="d@x.example"), lead())[1].startswith("Hi Dana,\n")
    assert suggest(st, "interested", r, None, lead())[1].startswith("Hi there,\n")


def test_every_answer_writes_the_company_once_also_when_the_address_does_not_start_with_it(st):
    st.company_postal_address = "1 Main Street, Austin, TX 78701"
    assert reply_footer(st).count(st.company_name) == 1 and compliance_footer(st).count("· 1 Main Street") == 1


# =================================================== beyond the tester's cases: check back
@pytest.mark.parametrize("text, month", [
    ("Please check back in March.", 3), ("Check back with me in early May.", 5), ("Try us again in mid-September.", 9), ("Can we reconnect in June?", 6),
    ("Do not check back in March.", None), ("Don't follow up in March.", None), ("We will get back to you in March.", None), ("I'll reach out in March.", None),
    ("Our year ends in March.", None), ("Check back later.", None), ("You may check back.", None),
])
def test_only_their_own_request_with_a_month_counts(text, month):
    assert check_back_month(row(text)) == month


def test_the_month_due_is_the_coming_one():
    now = datetime(2026, 10, 31, 9)
    assert _month_due(3, now) == datetime(2027, 3, 1, 14)
    assert _month_due(11, now) == datetime(2026, 11, 1, 14)
    assert _month_due(10, now) == now


def test_check_back_in_march_leaves_a_task_for_a_person_and_nothing_else(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    sent_before = len(FakeSMTP.sent)
    deliver(ctx, in_thread(sent, "Not a priority this year. Please check back in March."))
    r = latest_reply(ctx)
    assert (r.category, r.status) == ("objection", "needs_review") and "March" in r.action_note
    with ctx.db.session() as s:
        tasks = s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.lead_id == sent.lead_id)).scalars().all()
        assert [(t.status, t.due_at.month, t.due_at.day) for t in tasks] == [("pending", 3, 1)]
        assert tasks[0].to_email is None and "March" in tasks[0].subject and "Nothing goes out by itself" in tasks[0].body
        draft = s.get(FollowUp, r.suggested_followup_id)
        assert draft.status == "awaiting_approval" and "would you rather" not in draft.body and "check back in March" in draft.body
        waiting = s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status.in_(("approved", "sending")))).scalars().all()
        assert waiting == []
    run(ctx, sender, datetime(2027, 3, 2, 16))  # the month comes: a task is all there is
    assert len(FakeSMTP.sent) == sent_before
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "approved")).first() is None


def test_a_person_rereading_it_does_not_add_a_second_task_and_an_opt_out_closes_it(make_ctx):
    from aime.reply_intel import act

    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, in_thread(sent, "Please check back in March."))
    with ctx.db.session() as s:
        again = s.execute(select(InboundEmail).order_by(InboundEmail.id.desc())).scalars().first()
        act(s, ctx.settings, again, "sam")
        assert len(s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending")).scalars().all()) == 1
    deliver(ctx, in_thread(sent, "Please remove me from your mailing list."))
    with ctx.db.session() as s:
        task = s.execute(select(FollowUp).where(FollowUp.channel == "task")).scalars().one()
        assert task.status == "cancelled"


def test_a_task_due_in_march_is_not_todays_next_step(org):  # noqa: F811
    lead_id = add_lead(org, "Sam Co", owner="sam")
    with org.db.session() as s:
        s.add(FollowUp(lead_id=lead_id, company_id=s.get(Lead, lead_id).company_id, channel="task", step=1, status="pending",
                       subject="Check back with Sam Co in March", body="x", due_at=utcnow() + timedelta(days=120)))
    assert "Do the task" not in client(org, "sam").get("/my").text


# =================================================== beyond the tester's cases: budget, details, availability
@pytest.mark.parametrize("text, intent", [
    ("No budget at the moment.", None), ("We don't have the budget.", None), ("We don’t have any budget.", None), ("Budget is tight right now.", None), ("There is a budget freeze.", None),
    ("No budget. How much is it?", "pricing_request"), ("Our budget is 40k. Would that work?", "pricing_request"), ("What budget do you need?", "pricing_request"),
    ("How much does it cost?", "pricing_request"),
])
def test_no_budget_asks_no_price_and_a_real_question_still_does(text, intent):
    r = row(text)
    r.category = classify(r)[0]
    assert intent_of(r) == intent


@pytest.mark.parametrize("text", ["No budget at the moment.", "We don't have the budget."])
def test_no_budget_is_an_objection_to_a_person(make_ctx, text):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, in_thread(sent, text))
    r = latest_reply(ctx)
    assert (r.category, r.intent, r.sentiment, r.status) == ("objection", None, "negative", "needs_review")
    assert "pricing" not in (r.action_note or "") and "pricing" not in r.recommended_status


def test_a_request_for_details_waits_for_a_persons_words_and_opens_no_booking(make_ctx):
    ctx, _, sent = contacted(make_ctx, calendly_url="https://calendly.com/uat/30min")
    deliver(ctx, in_thread(sent, "Interested, please send me some details first. We don't do calls before reading something."))
    r = latest_reply(ctx)
    assert r.intent == "more_info" and r.status == "needs_review" and "a person writes the answer" in r.action_note
    with ctx.db.session() as s:
        d = s.get(FollowUp, r.suggested_followup_id)
        assert QUESTION_PLACEHOLDER in d.body and d.blocked_reason and "calendly" not in d.body.lower() and d.purpose != "booking_link"
        deal = s.execute(select(Opportunity).where(Opportunity.lead_id == r.lead_id)).scalars().one()
        assert "booking link" not in deal.next_step
    response = TestClient(create_app(ctx)).post(f"/followups/{d.id}/approve", headers=ORIGIN, follow_redirects=False)
    assert response.status_code in (302, 303) and followup(ctx, d.id).status == "awaiting_approval"


def test_interest_with_no_request_for_details_still_gets_the_booking_link(st):
    _, (_, body) = answer(st, "Sounds interesting. Can we talk Thursday?")
    assert "calendly.com" in body and "You mentioned Thursday" in body


@pytest.mark.parametrize("text, category", [
    ("What is your availability in January?", "question"), ("What's your availability?", "question"), ("Do you have any availability in March?", "question"),
    ("Sounds interesting. What is your availability?", "interested"), ("What is your availability for a call next week?", "interested"),
    ("Are you available Tuesday?", "interested"), ("When are you available?", "interested"),
])
def test_asking_when_we_are_free_is_a_question_and_interest_stays_interest(text, category):
    assert classify(row(text))[0] == category


def test_the_availability_question_stays_on_the_review_list(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, in_thread(sent, "What is your availability in January?"))
    r = latest_reply(ctx)
    assert (r.category, r.status) == ("question", "needs_review")
    with ctx.db.session() as s:
        assert QUESTION_PLACEHOLDER in s.get(FollowUp, r.suggested_followup_id).body


def test_an_opt_out_beside_those_words_stays_an_opt_out_and_a_person_still_checks():
    for text in ("What is your availability? Also, please unsubscribe me.", "No budget, please remove me.", "Please check back in March or better, do not contact me again."):
        assert classify(row(text))[0] == "unsubscribe", text
    assert classify(row("What is your availability? Also, please unsubscribe me."))[1] == "medium"


# =================================================== beyond the tester's cases: posts
CLAIM = "We've seen firsthand how a shared inbox slows a team down."


def stored_post(ctx, body, status="awaiting_approval", qc="passed", title="Shared inboxes"):
    with ctx.db.session() as s:
        item = ContentItem(title=title, body=body, status=status, hashtags=[], service="custom_crm", industry="real_estate", scheduled_for=utcnow() + timedelta(days=1),
                           generator="template", fingerprint=f"p4-{title}", quality={"words": 9, "qc": {"status": qc, "decision": "pass", "checked_at": "2026-01-01T00:00:00",
                                                                             "content_sha256": "x", "checks": [{"name": "factual claims", "status": "pass", "detail": "old"}]}})
        s.add(item)
        s.flush()
        return item.id


def test_a_post_page_shows_todays_claims_and_approving_needs_the_tick(make_ctx):
    ctx = make_ctx()
    pid = stored_post(ctx, CLAIM)
    web = TestClient(create_app(ctx), headers=ORIGIN, follow_redirects=False)
    page = web.get(f"/content/{pid}").text
    assert "claims first-hand experience" in page and 'name="qc_override"' in page
    web.post(f"/content/{pid}/approve")
    with ctx.db.session() as s:
        item = s.get(ContentItem, pid)
        assert item.status == "awaiting_approval" and item.approved_by is None
        assert item.quality["qc"]["status"] == "passed" and item.quality["qc"]["checks"][0]["detail"] == "old"  # the stored row is not rewritten
    web.post(f"/content/{pid}/approve", data={"qc_override": "yes"})
    with ctx.db.session() as s:
        assert s.get(ContentItem, pid).status == "approved"
        assert s.execute(select(Activity).where(Activity.subject.like("%approved despite quality findings%first-hand%"))).first()


def test_a_post_that_passes_today_is_as_it_was_and_a_stored_failure_is_not_lifted(make_ctx):
    ctx = make_ctx()
    clean = stored_post(ctx, "Plain practical advice only.")
    failed = stored_post(ctx, "Plain practical advice only.", qc="failed", title="Another title")
    failed_too = stored_post(ctx, CLAIM, qc="failed", title="A third title")
    with ctx.db.session() as s:
        assert s.get(ContentItem, clean).qc_status == "passed" and s.get(ContentItem, clean).qc["checks"][0]["detail"] == "old"
        assert s.get(ContentItem, failed).qc_status == "failed"
        assert s.get(ContentItem, failed_too).qc_status == "failed"
    web = TestClient(create_app(ctx), headers=ORIGIN, follow_redirects=False)
    assert web.post(f"/content/{clean}/approve").status_code in (302, 303)
    with ctx.db.session() as s:
        assert s.get(ContentItem, clean).status == "approved"


def test_a_post_never_checked_that_makes_a_claim_is_held_too(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        item = ContentItem(title="T", body=CLAIM, status="awaiting_approval", hashtags=[], service="custom_crm", industry="real_estate",
                           scheduled_for=utcnow() + timedelta(days=1), generator="template", fingerprint="p4-unchecked", quality={})
        s.add(item)
        s.flush()
        assert item.qc_status == "held" and item.quality == {}
        assert check_claims(item.title, item.body).status == "uncertain"
    with ctx.db.session() as s:
        pid = s.execute(select(ContentItem.id)).scalar_one()
    assert TestClient(create_app(ctx)).get(f"/content/{pid}").status_code == 200


TITLE = "Give sales reps their selling time back"
OFF_TITLE = ("Discrepancies between inventory data are a common challenge for shops.\n\nWhen stock counts drift, orders fail and buyers complain about delays every week.\n\n"
             "• Count stock every day\n• Keep one list of products\n• Check each order against that list\n\nGood records keep shelves and websites in step with one another over the whole year.\n\n"
             "A weekly review of the counts catches the small errors early, before they reach a customer, and it gives the warehouse one clear job to finish before Friday.\n\n"
             "What is the hardest count to keep right in your warehouse?")


def test_a_model_post_about_something_else_falls_back_to_the_template():
    llm = FakeLLM([OFF_TITLE, OFF_TITLE])
    draft = write_post(llm, "Co", "sales_automation", "b2b_sales", 0, 0, 0)
    assert draft.generator == "template" and draft.title == TITLE and "not about its title" in draft.quality["llm_rejected"][0]
    assert title_belongs(draft.title, draft.body)


@pytest.mark.parametrize("title, body, fits", [
    ("From internal tool to SaaS product", "Order exceptions eat the day. Our SaaS expertise builds products.", False),
    ("A practical first step into AI", "Discrepancies in inventory data. AI can find patterns.", False),
    ("Give sales reps their selling time back", "Most sales teams lose selling time to manual steps.", True),
    ("AI agents that do real work, not just chat", "Spreadsheets drift. Keep one list of products.", False),
    ("AI agents that do real work, not just chat", "A chat answers. AI agents finish tasks.", True),
    ("The", "Anything at all.", True),
])
def test_a_title_belongs_when_most_of_what_it_speaks_of_is_in_the_text(title, body, fits):
    assert title_belongs(title, body) is fits


# =================================================== beyond the tester's cases: the report, the home page, the campaign
def kpis(ctx):
    from aime.agents.analytics import sales_kpis

    with ctx.db.session() as s:
        def count(model, *where):
            from sqlalchemy import func
            return s.execute(select(func.count()).select_from(model).where(*where)).scalar_one()
        return sales_kpis(count, utcnow() - timedelta(days=1))


def test_decision_makers_do_not_count_people_who_decide_nothing_and_drafted_no_cancelled_unseen_draft(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        co = Company(name="Co", normalized_name="co", source="test", domain="co.example")
        s.add(co)
        s.flush()
        titles = ["CEO", None, "Chief Performance Officer", "Former CEO", "CEO (former)", "Formerly CTO", "Executive Assistant", "Office Manager",
                  "Social Media Manager", "Ex-CEO", "Co-founder, former CTO"]
        for i, title in enumerate(titles):
            s.add(Contact(company_id=co.id, first_name="A", last_name=f"B{i}", email=f"a{i}@co.example", source="website", title=title))
        lead = Lead(company_id=co.id, source="research", status="qualified", score=70, grade="B")
        s.add(lead)
        s.flush()
        for status, approved in (("awaiting_approval", None), ("sent", utcnow()), ("cancelled", None), ("cancelled", utcnow()), ("rejected", None)):
            s.add(FollowUp(lead_id=lead.id, company_id=co.id, channel="email", kind="outreach", step=1, due_at=utcnow(), status=status, subject="s", body="b", approved_at=approved))
    k = kpis(ctx)
    assert k["decision_makers_total"] == k["decision_makers_period"] == 3
    assert k["outreach_drafted_total"] == k["outreach_drafted_period"] == 4


def test_someone_who_can_assign_is_not_told_to_ask_their_manager(org):  # noqa: F811
    add_lead(org, "Free Co")
    owner = client(org, "owner").get("/my").text
    assert "assign some of the unassigned ones" in owner and "ask your manager" not in owner


def campaign(ctx, published, target=12, start=date(2026, 10, 1), end=date(2026, 10, 31)):
    with ctx.db.session() as s:
        c = Campaign(name="Oct", objective="x", service="custom_crm", industry="real_estate", states=[], channel="linkedin", start_date=start, end_date=end, status="active",
                     kpis={"target_posts": target} if target else {})
        s.add(c)
        s.flush()
        for i in range(published):
            s.add(ContentItem(title=f"T{i}", body="b", status="published", hashtags=[], service="custom_crm", industry="real_estate", campaign_id=c.id,
                              scheduled_for=utcnow(), generator="template", fingerprint=f"p4-c{i}", quality={}))
        return c.id


@pytest.mark.parametrize("published, target, status, words", [(0, 12, "ended", "0 of 12 planned posts published"), (11, 12, "ended", "11 of 12 planned"), (12, 12, "completed", "12 of 12 planned"),
                                                              (0, None, "ended", "0 posts published"), (2, None, "completed", "2 posts published")])
def test_a_campaign_past_its_dates_says_what_happened(make_ctx, published, target, status, words):
    ctx = make_ctx()
    cid = campaign(ctx, published, target)
    why: list[str] = []
    with ctx.db.session() as s:
        MarketingManagerAgent(ctx)._ensure_campaign(s, date(2026, 11, 2), "custom_crm", "real_estate", why)
        assert s.get(Campaign, cid).status == status
    assert words in why[0] and f"marked {status}" in why[0]


def test_a_person_can_still_set_a_campaign_that_ended(make_ctx):
    ctx = make_ctx()
    cid = campaign(ctx, 0)
    web = TestClient(create_app(ctx), headers=ORIGIN, follow_redirects=False)
    assert web.post(f"/campaigns/{cid}/status", data={"status": "ended"}).status_code in (302, 303)
    assert '<option selected>ended</option>' in web.get("/campaigns").text


def test_nothing_here_sends_or_approves_by_itself(make_ctx):
    """Every kind of reply this change reads differently, read in one run: emails wait for a person, addresses named in them get nothing."""
    ctx, sender, sent = contacted(make_ctx)
    sent_before = len(FakeSMTP.sent)
    for i, text in enumerate(["I'm not the right person.", "You should talk to sam.ray@brightline.example.", "I've forwarded your note to our CTO.", "Please check back in March.",
                              "No budget at the moment.", "Send me some details first.", "What is your availability in January?"]):
        deliver(ctx, in_thread(sent, text, msg_id=f"<r{i}@brightline.example>"))
    run(ctx, sender, TUESDAY_11AM_ET + timedelta(days=30))
    assert len(FakeSMTP.sent) == sent_before
    with ctx.db.session() as s:
        statuses = {f.status for f in s.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.id != sent.id)).scalars()}
        assert statuses <= {"awaiting_approval", "cancelled"}


# =================================================== a second small reply (tester's case, then what must still stop everything)
def answer_of(ctx, lead_id):
    with ctx.db.session() as s:
        return [(f.id, f.status) for f in s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.kind == "response")).scalars()]


def interested(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{TO}>", f"Re: {sent.subject}", "Sounds good, can we talk on Thursday?", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    lead = lead_of(ctx)
    assert lead.outreach_status == "interested" and [s for _i, s in answer_of(ctx, lead.id)] == ["awaiting_approval"]
    return ctx, sent, lead.id


def second(ctx, sent, text, frm=None, in_reply_to=True, msg_id="<second@x.example>", subject=None, headers=None):
    FakeIMAP.messages[2] = mail(frm or f"David Chen <{TO}>", subject or f"Re: {sent.subject}", text, in_reply_to=sent.message_id if in_reply_to else None, msg_id=msg_id, headers=headers)
    run_inbox(ctx)
    return latest_reply(ctx)


def test_a_second_small_reply_does_not_cancel_the_answer_or_the_interest(make_ctx):
    """Someone who asked for a call adds one line. The waiting answer with the booking link must survive, and the lead stays interested."""
    ctx, sent, lead_id = interested(make_ctx)
    r = second(ctx, sent, "Sorry, one more thing, my colleague will join us.")
    assert r.category == "other" and r.status == "needs_review"  # it still goes on the review list
    assert "awaiting_approval" in [s for _i, s in answer_of(ctx, lead_id)]
    assert lead_of(ctx).outreach_status == "interested"
    assert "still waits for your approval" in r.action_note and r.suggested_followup_id is None


def test_a_small_reply_keeps_no_follow_up_only_the_answer_to_the_same_person(make_ctx):
    ctx, sent, lead_id = interested(make_ctx)
    with ctx.db.session() as s:
        s.add(FollowUp(lead_id=lead_id, contact_id=sent.contact_id, company_id=sent.company_id, channel="email", kind="outreach", step=2, due_at=utcnow(),
                       status="awaiting_approval", subject="Re: x", body="b", to_email=sent.to_email, in_reply_to=sent.message_id))
    second(ctx, sent, "My colleague will join us.")
    with ctx.db.session() as s:
        assert s.execute(select(FollowUp.status).where(FollowUp.lead_id == lead_id, FollowUp.step == 2)).scalar_one() == "cancelled"
    assert [s for _i, s in answer_of(ctx, lead_id)] == ["awaiting_approval"]


def test_an_answer_already_approved_is_cancelled_as_before(make_ctx):
    """Sending stays as strict as it was: an email approved before this reply came in does not go out on its strength."""
    ctx, sent, lead_id = interested(make_ctx)
    with ctx.db.session() as s:
        s.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.kind == "response")).scalar_one().status = "approved"
    r = second(ctx, sent, "My colleague will join us.")
    assert [s for _i, s in answer_of(ctx, lead_id)] == ["cancelled"] and "1 queued follow-up(s) cancelled" in r.action_note
    assert lead_of(ctx).outreach_status == "interested"


@pytest.mark.parametrize("text, category, stage", [
    ("Please remove me from your mailing list.", "unsubscribe", "unsubscribed"), ("Stop", "unsubscribe", "unsubscribed"),
    ("Not interested, thanks.", "not_interested", "not_interested"), ("No thanks.", "not_interested", "not_interested"),
])
def test_a_clear_no_still_cancels_the_waiting_answer(make_ctx, text, category, stage):
    ctx, sent, lead_id = interested(make_ctx)
    assert second(ctx, sent, text).category == category
    assert [s for _i, s in answer_of(ctx, lead_id)] == ["cancelled"] and lead_of(ctx).outreach_status == stage


def test_a_bounce_still_cancels_the_waiting_answer(make_ctx):
    ctx, sent, lead_id = interested(make_ctx)
    FakeIMAP.messages[2] = bounce_for(TO, sent.message_id)
    run_inbox(ctx)
    assert latest_reply(ctx).category == "bounce"
    assert [s for _i, s in answer_of(ctx, lead_id)] == ["cancelled"] and lead_of(ctx).outreach_status == "bounced"


@pytest.mark.parametrize("text", ["You can stop now", "Never mind.", "Changed my mind.", "Take off", "Forget it, we don't need this.", "This is a ridiculous waste of my time, you know",
                                  "Kein Bedarf.", "No gracias.", "My attorney will join us."])
def test_a_line_that_may_mean_no_stops_everything_as_before(make_ctx, text):
    ctx, sent, lead_id = interested(make_ctx)
    r = second(ctx, sent, text)
    assert r.status == "needs_review" and r.category != "interested", (r.category, r.status)
    assert [s for _i, s in answer_of(ctx, lead_id)] == ["cancelled"], r.category
    assert lead_of(ctx).outreach_status != "interested"


def test_a_stranger_writing_with_their_address_and_no_thread_keeps_nothing(make_ctx):
    """A From line proves nothing: outside our thread and unsigned, the reply stops everything as it always did."""
    ctx, sent, lead_id = interested(make_ctx)
    r = second(ctx, sent, "My colleague will join us.", in_reply_to=False, subject="Hello")
    assert r.followup_id is None
    assert [s for _i, s in answer_of(ctx, lead_id)] == ["cancelled"]


def test_a_colleagues_small_reply_does_not_keep_an_answer_meant_for_someone_else(make_ctx):
    ctx, sent, lead_id = interested(make_ctx)
    second(ctx, sent, "My colleague will join us.", frm=f"Jane Smith <jane.smith@{TO.split('@')[1]}>")
    assert [s for _i, s in answer_of(ctx, lead_id)] == ["cancelled"]


def test_mail_from_the_junk_folder_is_held_as_before(make_ctx):
    from aime.reply_intel import act

    ctx, sent, lead_id = interested(make_ctx)
    with ctx.db.session() as s:
        row = InboundEmail(mailbox_uid="t:junk", message_id="<junk@x.example>", from_email=TO, from_name="David Chen", subject="Hello", received_at=utcnow(),
                           body_text="My colleague will join us.", new_text="My colleague will join us.", status="new", in_junk=True, bounced_addresses=[],
                           category="other", confidence="low", reasons=["no clear signal"], auto_submitted=False, is_simulated=False)
        s.add(row)
        s.flush()
        act(s, ctx.settings, row, "inbox_agent")
        assert row.holds_sending is True and row.status == "needs_review"
    assert [s for _i, s in answer_of(ctx, lead_id)] == ["awaiting_approval"]  # as today: its emails wait, and nothing is changed on its word


@pytest.mark.parametrize("reason, keeps", [("AI (local) suggests: not interested (please check)", False), ("AI (local) suggests: unsubscribe (please check)", False),
                                           ("AI (local) suggests: other (please check)", True), ("possible opt-out in a footer: x", False)])
def test_what_else_was_read_about_it_decides(make_ctx, reason, keeps):
    from aime.reply_intel import act

    ctx, sent, lead_id = interested(make_ctx)
    with ctx.db.session() as s:
        row = InboundEmail(mailbox_uid="t:ai", message_id="<ai@x.example>", in_reply_to=sent.message_id, references=sent.message_id, from_email=TO, from_name="David Chen",
                           subject=f"Re: {sent.subject}", received_at=utcnow(), body_text="My colleague will join us.", new_text="My colleague will join us.", status="new",
                           bounced_addresses=[], category="other", confidence="low", reasons=["no clear signal", reason], auto_submitted=False, is_simulated=False)
        s.add(row)
        s.flush()
        act(s, ctx.settings, row, "inbox_agent")
    assert [s for _i, s in answer_of(ctx, lead_id)] == (["awaiting_approval"] if keeps else ["cancelled"])


def test_a_person_setting_it_to_other_keeps_the_answer_too(make_ctx):
    ctx, sent, lead_id = interested(make_ctx)
    r = second(ctx, sent, "My colleague will join us.")
    TestClient(create_app(ctx), headers=ORIGIN).post(f"/inbox/{r.id}/reclassify", data={"category": "other"})
    assert "awaiting_approval" in [s for _i, s in answer_of(ctx, lead_id)] and lead_of(ctx).outreach_status == "interested"


def test_an_earlier_note_stops_saying_it_waits_when_it_no_longer_does(make_ctx):
    ctx, sent, lead_id = interested(make_ctx)
    first = latest_reply(ctx)
    assert "suggested reply waiting for your approval" in first.action_note
    web = TestClient(create_app(ctx), headers=ORIGIN)
    assert "suggested reply waiting for your approval" in web.get("/inbox?show=all").text
    second(ctx, sent, "Not interested, thanks.")
    page = web.get("/inbox?show=all").text
    assert "suggested reply waiting for your approval" not in page and "the suggested reply no longer waits: it is cancelled" in page
    with ctx.db.session() as s:
        assert "waiting for your approval" in s.get(InboundEmail, first.id).action_note  # the stored note is left as it was
    assert note_now(None, "cancelled") is None and note_now("other words", "sent") == "other words" and note_now("suggested reply waiting for your approval", "awaiting_approval").endswith("approval")
