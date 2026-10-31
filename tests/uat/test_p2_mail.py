"""Phase 2: mail as it really arrives, through the real InboxAgent. Synthetic data only (make_ctx, fake or local IMAP).

test_defect_* assert the correct behaviour and fail on the current code (release #72); test_ok_* hold today.
The hand-written corpus is in p2_mail_corpus.py. The worst defect here is a real reply or opt-out that vanishes silently."""

from __future__ import annotations

import imaplib
import socket
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from aime.agents.inbox_agent import InboxAgent
from aime.inbox import Mailbox
from aime.models import Alert, Contact, FollowUp, InboundEmail
from tests.test_automation import followup, run
from tests.test_company_research import SITE
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import FakeIMAP, contacted, latest_reply, run_inbox
from tests.test_outreach import drafts, lead_of
from tests.uat import p2_mail_corpus as K
from tests.uat.test_uat_email import approve, deliver, meetings_of, notice, signed_ctx, suppressed_values, web

DAVID = f"david.chen@{SITE}"
JANE = f"jane.smith@{SITE}"
NOW = TUESDAY_11AM_ET


def vars_for(sent):
    return SimpleNamespace(to=DAVID, dom=SITE, mid=sent.message_id, subj=sent.subject, own="sales@us.example", own_dom="us.example")


def scene(make_ctx, build, **over):
    """The first email went to DAVID; `build` is what arrives, read by the real inbox job."""
    ctx, sender, sent = contacted(make_ctx, **over)
    v = vars_for(sent)
    deliver(ctx, build(v))
    return ctx, sender, sent, v


def next_steps(ctx, sender):
    """A sequence tick after the waiting time: the next emails of the sequence drafted for this lead."""
    run(ctx, sender, NOW + timedelta(days=6))
    return [(f.step, f.to_email) for f in drafts(ctx) if f.step > 1 and f.kind == "outreach"]


def alerts(ctx):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert)).scalars()]


def contact_status(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one().email_status


def corpus_cases(group: dict, names):
    return [pytest.param(group[n], id=n) for n in names]


# ============================================================ who really answers
LOST_BY_SALES_RELATED = ["gmail_subject_only_hot", "gmail_subject_only_opt", "jane_subdomain_subject_only", "jane_parent_domain_subject_only"]


@pytest.mark.parametrize("build", corpus_cases(K.WHO, LOST_BY_SALES_RELATED) + corpus_cases(K.AUTO, ["helpdesk_agent_foreign_domain", "shared_inbox_tool"]))
def test_defect_an_answer_carrying_our_subject_from_an_unknown_domain_is_not_dropped_as_unrelated(make_ctx, build):
    """No thread headers (or a tool's own), a sender outside the company's domain, but "Re: <the subject we sent>" and often
    our quoted email. Today: skipped_unrelated, never stored, and the mailbox position moves on: nobody will ever see it."""
    ctx, _, _, _ = scene(make_ctx, build)
    r = latest_reply(ctx)
    assert r is not None, "the reply vanished: read once, not stored, no alert"
    assert r.status == "needs_review" or r.lead_id is not None


@pytest.mark.parametrize("build", corpus_cases(K.WHO, ["jane_subject_only_hot", "jane_subject_only_no", "jane_subject_only_question", "jane_forward_thread_new_ids"])
                         + corpus_cases(K.STRUCTURE, ["forward_as_attachment"]) + corpus_cases(K.AUTO, ["helpdesk_agent_hot_company_domain_no_thread"]))
def test_defect_a_colleagues_answer_without_our_thread_headers_reaches_the_lead_and_stops_the_sequence(make_ctx, build):
    """jane@ of a contacted company answers with our subject but without our Message-ID (Outlook forward, then a reply).
    Today it is stored as 'Could not match this email to a lead': no draft, no deal, and the next step is still drafted to the
    role inbox, also after "yes, call me" or "not interested"."""
    ctx, sender, _, _ = scene(make_ctx, build)
    r, lead = latest_reply(ctx), lead_of(ctx)
    assert r.lead_id == lead.id, (r.category, r.action_note)
    assert lead.outreach_status != "contacted"
    assert next_steps(ctx, sender) == []


def test_defect_the_person_who_answered_is_remembered_for_her_next_email(make_ctx):
    """jane@ answered in the thread and we answered her. Her next email has no thread headers: it must still find the lead."""
    ctx, _, sent, v = scene(make_ctx, K.WHO["jane_thread_hot"])
    deliver(ctx, K.msg(K.jane(v), "A question from our side", "One more thing: who else uses this?", tag="next"))
    assert latest_reply(ctx).lead_id == lead_of(ctx).id


@pytest.mark.parametrize("name", ["irt_with_comment", "irt_two_ids", "refs_no_spaces"])
def test_defect_thread_headers_in_older_valid_forms_are_understood(make_ctx, name):
    """RFC 5322 allows more than one id and comments in In-Reply-To; some mailers omit the space in References."""
    ctx, _, sent, _ = scene(make_ctx, K.WHO[name])
    r = latest_reply(ctx)
    assert r is not None and r.followup_id == sent.id


def test_ok_colleague_in_thread_reaches_the_lead_her_answer_goes_to_her_and_the_sequence_stops(make_ctx):
    ctx, sender, sent, v = scene(make_ctx, K.WHO["jane_thread_hot"])
    r, lead = latest_reply(ctx), lead_of(ctx)
    assert (r.category, r.followup_id, r.lead_id) == ("interested", sent.id, lead.id)
    assert lead.outreach_status == "interested"
    assert followup(ctx, r.suggested_followup_id).to_email == JANE
    assert any("Interested reply" in a and JANE in a for a in alerts(ctx))
    assert next_steps(ctx, sender) == []


def test_ok_colleague_answer_is_really_sent_to_her_in_the_thread(make_ctx):
    ctx, sender, sent, v = scene(make_ctx, K.WHO["jane_thread_hot"])
    r = latest_reply(ctx)
    assert approve(web(ctx), r.suggested_followup_id).status_code == 303
    before = len(FakeSMTP.sent)
    run(ctx, sender, NOW + timedelta(minutes=30))
    out = FakeSMTP.sent[before:]
    assert [m["To"] for m in out] == [JANE] and out[0]["In-Reply-To"] == r.message_id


@pytest.mark.parametrize("name", ["jane_refs_only_hot", "gmail_thread_hot", "gmail_refs_only_hot"])
def test_ok_in_thread_answer_from_another_address_or_with_references_only(make_ctx, name):
    ctx, _, sent, _ = scene(make_ctx, K.WHO[name])
    r = latest_reply(ctx)
    assert r.followup_id == sent.id and lead_of(ctx).outreach_status == "interested"
    assert followup(ctx, r.suggested_followup_id).to_email == r.from_email


def test_ok_opt_out_from_a_colleague_in_the_thread_also_stops_the_address_we_wrote_to(make_ctx):
    ctx, sender, _, _ = scene(make_ctx, K.WHO["jane_thread_opt"])
    assert {JANE, DAVID} <= suppressed_values(ctx) and lead_of(ctx).outreach_status == "unsubscribed"
    assert next_steps(ctx, sender) == []


def test_ok_opt_out_from_a_colleague_by_subject_only_stops_the_company_and_a_person_decides(make_ctx):
    ctx, sender, _, _ = scene(make_ctx, K.WHO["jane_subject_only_opt"])
    r = latest_reply(ctx)
    assert r.status == "needs_review" and JANE in suppressed_values(ctx) and lead_of(ctx).outreach_status == "stopped"
    assert next_steps(ctx, sender) == []


def test_ok_booking_by_the_colleague_who_answered_lands_on_the_lead(make_ctx, monkeypatch):
    ctx, _, sent = signed_ctx(make_ctx, monkeypatch)
    deliver(ctx, K.WHO["jane_thread_hot"](vars_for(sent)))
    deliver(ctx, notice(email=JANE, name="Jane Smith"))
    assert [m.lead_id for m in meetings_of(ctx)] == [lead_of(ctx).id]


# ============================================================ a person's words read as something else
@pytest.mark.parametrize("build", corpus_cases(K.AUTO, ["helpdesk_agent_hot_auto_header", "human_hot_precedence_bulk"]))
def test_defect_a_person_answering_through_a_helpdesk_is_not_an_automatic_reply(make_ctx, build):
    """Helpdesks and gateways put Auto-Submitted or Precedence: bulk on people's mail. "Yes, can we talk Thursday?" is read as
    out_of_office/high/actioned: off the review list, no draft, no deal, and the sequence continues."""
    ctx, *_ = scene(make_ctx, build)
    r = latest_reply(ctx)
    assert r.status == "needs_review" or r.category == "interested", (r.category, r.confidence, r.status)
    assert lead_of(ctx).outreach_status != "contacted"


@pytest.mark.parametrize("build", corpus_cases(K.STRUCTURE, ["human_marketing_signature_hot", "human_sig_newsletter", "human_objection_opt_out_disclaimer"])
                         + corpus_cases(K.AUTO, ["autoreply_company_footer"]) + corpus_cases(K.STRUCTURE, ["forward_as_attachment_inner"]))
def test_defect_a_companys_own_marketing_footer_above_the_quote_does_not_silently_opt_out_a_prospect(make_ctx, build):
    """ "Sounds interesting, can we talk Thursday?" over a signature with "unsubscribe from our newsletter" is read as an
    opt-out with high confidence: the address is suppressed for good, the status is 'actioned', no person ever looks.
    The same footer below the quote already goes to a person (test_uat_email)."""
    ctx, *_ = scene(make_ctx, build)
    r = latest_reply(ctx)
    assert r.status == "needs_review", (r.category, r.confidence, r.status)


@pytest.mark.parametrize("build", corpus_cases(K.AUTO, ["no_longer_monitored", "left_company", "challenge_thread"]))
def test_defect_an_automatic_reply_that_names_a_new_contact_or_a_dead_mailbox_reaches_a_person(make_ctx, build):
    """ "This mailbox is no longer monitored, write to jane@", "has left the company", "click to verify": filed as an ordinary
    out-of-office with status actioned. Nobody sees the new contact, and the sequence keeps writing to the dead mailbox."""
    ctx, *_ = scene(make_ctx, build)
    assert latest_reply(ctx).status == "needs_review"


@pytest.mark.parametrize("text", [t for k, t in K.LANG.items() if "_opt" in k])
def test_defect_an_opt_out_in_another_language_is_honoured(make_ctx, text):
    """Spanish, German, French and Hindi in Latin script: today 'other / no clear signal'; the sequence stops and a person may
    read it, but the address is not suppressed until someone reclassifies it."""
    ctx, *_ = scene(make_ctx, lambda v: K.thr(v, text + K.q_gmail(v), tag="lang"))
    assert latest_reply(ctx).category == "unsubscribe" and DAVID in suppressed_values(ctx)


def test_ok_a_non_english_reply_is_never_lost(make_ctx):
    for key in ("es_hot", "de_no", "hi_no"):  # an opt-out in another language is honoured: test_defect_an_opt_out_in_another_language
        ctx, sender, _, _ = scene(make_ctx, lambda v, key=key: K.thr(v, K.LANG[key] + K.q_gmail(v), tag="lang"))
        assert latest_reply(ctx).status == "needs_review" and lead_of(ctx).outreach_status == "replied"
        assert next_steps(ctx, sender) == []


# ============================================================ structure
@pytest.mark.parametrize("name", ["alt_empty_text_opt", "alt_space_text_opt"])
def test_defect_an_empty_text_part_does_not_hide_an_opt_out_in_the_html_part(make_ctx, name):
    """multipart/alternative with an empty text/plain: _text_of returns the empty text, the HTML is never read. The opt-out is
    'other / no new text', not suppressed, and the Replies page shows an empty message."""
    ctx, *_ = scene(make_ctx, K.STRUCTURE[name])
    r = latest_reply(ctx)
    assert "remove me" in (r.body_text or "") and r.category == "unsubscribe" and DAVID in suppressed_values(ctx)


def test_defect_an_empty_text_part_does_not_hide_interest_in_the_html_part(make_ctx):
    ctx, *_ = scene(make_ctx, K.STRUCTURE["alt_empty_text_hot"])
    r = latest_reply(ctx)
    assert "Thursday" in (r.body_text or "") and r.category == "interested"


OPT_OUT_STRUCTURES = ["html_only_opt", "qp_opt", "b64_opt", "latin1", "cp1252", "shift_jis", "undeclared_latin1", "tnef_plain", "iphone_first_opt",
                      "outlook_de_opt", "mb_body_plain_opt", "smime_signed_opt", "gateway_disclaimer_opt", "caution_banner_opt"]


@pytest.mark.parametrize("build", corpus_cases(K.STRUCTURE, OPT_OUT_STRUCTURES))
def test_ok_an_opt_out_is_read_in_every_transfer_form(make_ctx, build):
    ctx, sender, _, _ = scene(make_ctx, build)
    assert latest_reply(ctx).category == "unsubscribe" and DAVID in suppressed_values(ctx)
    assert next_steps(ctx, sender) == []


def test_ok_a_large_attachment_does_not_hide_an_opt_out(make_ctx):
    ctx, *_ = scene(make_ctx, lambda v: K.huge_attachment(v, mb=4))
    assert latest_reply(ctx).category == "unsubscribe" and DAVID in suppressed_values(ctx)


@pytest.mark.parametrize("build", corpus_cases(K.STRUCTURE, ["iphone_hot", "wrapped_gmail_header_hot", "outlook_top_hot", "outlook_es_hot", "outlook_fr_hot",
                                                             "gmail_es_hot", "gmail_fr_hot", "gmail_de_hot", "apple_de_hot", "mb_body_html_hot"]))
def test_ok_quote_headers_of_every_mail_program_keep_our_footer_out_of_the_reply(make_ctx, build):
    ctx, *_ = scene(make_ctx, build)
    r = latest_reply(ctx)
    assert r.category == "interested" and DAVID not in suppressed_values(ctx) and "unsubscribe" not in (r.new_text or "").lower()


def test_ok_a_hostile_one_megabyte_html_body_does_not_hold_the_job(make_ctx):
    started = time.perf_counter()
    ctx, *_ = scene(make_ctx, K.STRUCTURE["mb_body_nested_quotes"])
    assert latest_reply(ctx).category == "interested" and time.perf_counter() - started < 60


@pytest.mark.parametrize("build", corpus_cases(K.STRUCTURE, ["tnef_only"]) + corpus_cases(K.CALENDAR, ["ics_only_in_thread"]))
def test_ok_a_reply_with_no_readable_text_still_stops_the_sequence_and_waits_for_a_person(make_ctx, build):
    ctx, *_ = scene(make_ctx, build)
    assert latest_reply(ctx).status == "needs_review" and lead_of(ctx).outreach_status == "replied"


# ============================================================ bounces
@pytest.mark.parametrize("why", [K.USER_UNKNOWN, "Unrouteable address"], ids=["user_unknown", "unrouteable"])
def test_defect_a_plain_text_exim_bounce_from_our_own_server_is_understood(make_ctx, why):
    """Exim's classic text bounce lists the failed address on an indented line and in X-Failed-Recipients, and the original's
    Message-ID in the copy below. None of it is read: bounce/low, tied to no lead, nothing suppressed, and the next step is still
    drafted to the dead address."""
    ctx, sender, sent, _ = scene(make_ctx, lambda v: K.exim_plain(v, why))
    r = latest_reply(ctx)
    assert r.bounced_addresses == [DAVID] and r.followup_id == sent.id
    assert DAVID in suppressed_values(ctx) and lead_of(ctx).outreach_status == "bounced"
    assert next_steps(ctx, sender) == []


def test_defect_a_gateways_plain_notice_is_tied_to_the_lead(make_ctx):
    """A postmaster notice with "Recipient: <address>" and our subject: a person sees it, but with no lead and the sequence running."""
    ctx, *_ = scene(make_ctx, K.BOUNCES["mimecast_plain_notice_postmaster"])
    assert latest_reply(ctx).lead_id == lead_of(ctx).id


@pytest.mark.parametrize("build", corpus_cases(K.BOUNCES, ["proofpoint_third_party_notice", "sendmail_no_domain"]))
def test_defect_a_delivery_failure_notice_naming_our_recipient_is_not_dropped_as_unrelated(make_ctx, build):
    ctx, *_ = scene(make_ctx, build)
    assert latest_reply(ctx) is not None, "the failure notice vanished: the sequence writes on, nobody knows"


@pytest.mark.parametrize("build", corpus_cases(K.BOUNCES, ["exim_hourly_limit_dsn", "proofpoint_ip_block_in_exim_dsn"]))
def test_defect_our_own_servers_limit_or_a_blocklist_does_not_condemn_the_prospects_address(make_ctx, build):
    """"Domain <ours> has exceeded the max emails per hour" and "Blocked - see ipcheck ..." say something about US. Today the
    prospect's good address is marked bounced and suppressed for good, 'actioned', with no alert. A blocklisted IP would
    quietly burn every lead it touches."""
    ctx, *_ = scene(make_ctx, build)
    assert contact_status(ctx) != "bounced" and DAVID not in suppressed_values(ctx)
    assert any("block" in a.lower() or "limit" in a.lower() or "exceeded" in a.lower() for a in alerts(ctx))


@pytest.mark.parametrize("build", corpus_cases(K.BOUNCES, ["m365_delay_noauto"]) + corpus_cases(K.AUTO, ["read_receipt_outlook", "delivery_receipt"]))
def test_defect_a_delay_notice_or_receipt_is_not_counted_as_the_prospect_replying(make_ctx, build):
    """multipart/report from a sender not called postmaster, without Auto-Submitted: read as a person's words. The lead becomes
    'replied', the sequence stops for good and the sent email's delivery status says replied, which also feeds the reply numbers."""
    ctx, _, sent, _ = scene(make_ctx, build)
    assert lead_of(ctx).outreach_status == "contacted" and followup(ctx, sent.id).delivery_status != "replied"


def test_defect_a_read_receipt_is_not_a_bounce(make_ctx):
    """Thunderbird's receipt says "the mail that you sent to <address>": the plain-text rule reads that as a failed recipient. The
    person who opened our email is marked bounced and suppressed for good."""
    ctx, *_ = scene(make_ctx, K.AUTO["read_receipt_thunderbird"])
    assert DAVID not in suppressed_values(ctx) and contact_status(ctx) != "bounced" and lead_of(ctx).outreach_status != "bounced"


@pytest.mark.parametrize("build", corpus_cases(K.BOUNCES, ["exim_dsn", "postfix_dsn", "gmail_dsn", "m365_ndr_exchange_sender", "m365_ndr_postmaster", "qmail", "yahoo"]))
def test_ok_a_hard_bounce_in_each_real_format_stops_the_lead(make_ctx, build):
    ctx, sender, sent, _ = scene(make_ctx, build)
    r = latest_reply(ctx)
    assert (r.category, r.status, r.followup_id) == ("bounce", "actioned", sent.id)
    assert DAVID in suppressed_values(ctx) and contact_status(ctx) == "bounced" and lead_of(ctx).outreach_status == "bounced"
    assert next_steps(ctx, sender) == []


def test_ok_a_mail_systems_bounce_quoting_no_id_stops_the_lead_and_a_person_decides(make_ctx):
    """Anyone can send a report, and this one quotes nothing of ours (test_p2_hostile forbids blind suppression). A mail system
    naming an address we wrote to stops the sequence; nothing is suppressed for good until a person confirms it."""
    ctx, sender, sent, _ = scene(make_ctx, K.BOUNCES["postfix_dsn_no_id"])
    r = latest_reply(ctx)
    assert (r.category, r.status, r.followup_id) == ("bounce", "needs_review", sent.id)
    assert DAVID not in suppressed_values(ctx) and contact_status(ctx) != "bounced" and lead_of(ctx).outreach_status == "stopped"
    assert next_steps(ctx, sender) == []


@pytest.mark.parametrize("build", corpus_cases(K.BOUNCES, ["exim_delay_plain", "exim_delay_dsn", "postfix_delay", "gmail_delay", "m365_delay_auto"]))
def test_ok_a_delay_notice_never_stops_the_lead_for_good(make_ctx, build):
    ctx, sender, _, _ = scene(make_ctx, build)
    assert DAVID not in suppressed_values(ctx) and contact_status(ctx) != "bounced" and lead_of(ctx).outreach_status == "contacted"
    assert [s for s, _ in next_steps(ctx, sender)] == [2]


# ============================================================ automatic replies that work
@pytest.mark.parametrize("build", corpus_cases(K.AUTO, ["outlook_ooo", "gmail_vacation", "zendesk_confirm_thread"]))
def test_ok_real_automatic_replies_leave_the_sequence_running(make_ctx, build):
    ctx, sender, _, _ = scene(make_ctx, build)
    assert latest_reply(ctx).category == "out_of_office" and lead_of(ctx).outreach_status == "contacted"
    assert [s for s, _ in next_steps(ctx, sender)] == [2]


def test_ok_left_the_company_written_by_a_person_goes_to_review(make_ctx):
    ctx, *_ = scene(make_ctx, K.AUTO["left_company_no_header"])
    assert latest_reply(ctx).status == "needs_review"


# ============================================================ calendar
def test_defect_a_calendly_notice_forwarded_from_our_own_mailbox_is_not_dropped(make_ctx):
    """Forwarded by hand from the sales mailbox to itself: skipped as "our own mail" before anything looks at it, with no trace."""
    ctx, *_ = scene(make_ctx, K.CALENDAR["calendly_fwd_own_mailbox"])
    assert latest_reply(ctx) is not None


@pytest.mark.parametrize("name", ["calendly_fwd_colleague", "calendly_fwd_owner_gmail"])
def test_ok_a_forwarded_calendly_notice_waits_for_a_person_and_books_nothing_by_itself(make_ctx, name):
    ctx, *_ = scene(make_ctx, K.CALENDAR[name], calendly_verified_only=True)
    r = latest_reply(ctx)
    assert (r.category, r.status) == ("calendly", "needs_review") and meetings_of(ctx) == []


# ============================================================ IMAP
def test_defect_a_message_that_cannot_be_fetched_does_not_block_the_mailbox_for_ever(make_ctx):
    """FETCH raises for one message (BAD from the server, or a timeout on a huge one). fetch_new has no tries for this, only for a
    "NO" answer, and it returns nothing when it raises. So the opt-out before it is never handled, and no mail after it is ever read."""
    ctx, _, sent = contacted(make_ctx)
    v = vars_for(sent)

    class Broken(FakeIMAP):
        def uid(self, command, *args):
            if command == "FETCH" and int(args[0]) == 2:
                raise imaplib.IMAP4.error("FETCH command error: BAD [b'internal error']")
            return super().uid(command, *args)

    Broken.messages = {1: K.thr(v, K.OPT), 2: b"x", 3: K.WHO["gmail_thread_hot"](v)}
    for _ in range(4):
        InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=Broken)).run({})
    assert DAVID in suppressed_values(ctx), "the opt-out ahead of the bad message was never handled"
    with ctx.db.session() as s:
        assert s.execute(select(InboundEmail).where(InboundEmail.from_email == "x@gmail.com")).first(), "mail behind it never read"


def test_defect_an_unread_reply_is_not_lost_when_uidvalidity_changes(make_ctx):
    """The mailbox is migrated or rebuilt after the job could not read it for a while. The position restarts with "the last 14 days":
    an unread reply older than that is gone, and nothing tells anyone the position was reset."""
    ctx, _, sent = contacted(make_ctx)
    v = vars_for(sent)

    class Honest(FakeIMAP):
        def uid(self, command, *args):
            if command == "SEARCH" and "SINCE" in args:
                since = datetime.strptime(args[-1], "%d-%b-%Y")
                dated = {10: datetime(2000, 1, 1)}
                return "OK", [" ".join(str(u) for u in sorted(self.messages) if dated.get(u, datetime.now()) >= since).encode()]
            return super().uid(command, *args)

    Honest.validity, Honest.messages = "777", {1: K.thr(v, "Thanks, more soon.", mid="<seen@x.example>")}
    InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=Honest)).run({})
    Honest.validity, Honest.messages = "888", {10: K.thr(v, K.OPT, mid="<old-unread@x.example>"), 11: K.thr(v, "Later.", mid="<new@x.example>")}
    InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=Honest)).run({})
    with ctx.db.session() as s:
        stored = set(s.execute(select(InboundEmail.message_id)).scalars())
    assert "<old-unread@x.example>" in stored or any("position" in a.lower() or "uidvalidity" in a.lower() for a in alerts(ctx))


def test_ok_an_opt_out_behind_many_unrelated_emails_is_read_in_the_first_run(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    v = vars_for(sent)
    FakeIMAP.messages = {i: K.msg("News <news@unrelated.example>", f"Offer {i}", "Buy now.", tag="news") for i in range(1, 121)}
    FakeIMAP.messages[121] = K.thr(v, K.OPT)
    out = run_inbox(ctx)
    assert (out.fetched, out.read_to_end, DAVID in suppressed_values(ctx)) == (121, True, True)


def test_ok_a_fetch_answered_no_is_retried_then_skipped_with_an_alert(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    v = vars_for(sent)

    class Refuses(FakeIMAP):
        def uid(self, command, *args):
            if command == "FETCH" and int(args[0]) == 2:
                return "NO", [b"message gone"]
            return super().uid(command, *args)

    Refuses.messages = {1: K.thr(v, "Thanks, more soon."), 2: b"x", 3: K.thr(v, K.OPT)}
    for expected in (False, False, True):
        assert InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=Refuses)).run({}).ok
        assert (DAVID in suppressed_values(ctx)) is expected
    assert any("could not be fetched" in a for a in alerts(ctx))


def test_ok_uidvalidity_change_reads_again_without_duplicates(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    v = vars_for(sent)
    FakeIMAP.messages = {1: K.thr(v, K.HOT, mid="<once@x.example>")}
    FakeIMAP.validity = "777"
    run_inbox(ctx)
    try:
        FakeIMAP.validity = "888"
        FakeIMAP.messages = {50: K.thr(v, K.HOT, mid="<once@x.example>")}
        run_inbox(ctx)
    finally:
        FakeIMAP.validity = "777"
    with ctx.db.session() as s:
        assert len(s.execute(select(InboundEmail.id)).all()) == 1
        assert len(s.execute(select(FollowUp.id).where(FollowUp.kind == "response")).all()) == 1


class LocalIMAP(threading.Thread):
    """A tiny IMAP server on 127.0.0.1, so the real imaplib parses the replies real servers send."""

    FETCH = {
        "dovecot": lambda seq, uid, n: (f"* {seq} FETCH (UID {uid} FLAGS (\\Seen) BODY[] {{{n}}}\r\n", ")\r\n"),
        "courier": lambda seq, uid, n: (f"* {seq} FETCH (UID {uid} BODY[] {{{n}}}\r\n", ")\r\n"),
        "gmail": lambda seq, uid, n: (f"* {seq} FETCH (X-GM-THRID 1 X-GM-MSGID 2 X-GM-LABELS () UID {uid} BODY[] {{{n}}}\r\n", " FLAGS (\\Seen))\r\n"),
        "flags_first": lambda seq, uid, n: (f"* {seq} FETCH (FLAGS (\\Seen))\r\n* {seq} FETCH (UID {uid} BODY[] {{{n}}}\r\n", ")\r\n"),
        "new_mail_meanwhile": lambda seq, uid, n: (f"* 9 EXISTS\r\n* {seq} FETCH (UID {uid} BODY[] {{{n}}}\r\n", ")\r\n"),
    }

    def __init__(self, messages, style):
        super().__init__(daemon=True)
        self.messages, self.style = messages, style
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.commands: list[str] = []

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn, conn.makefile("rb") as lines:
                conn.sendall(b"* OK [CAPABILITY IMAP4rev1] ready\r\n")
                for raw in lines:
                    if self._answer(conn, raw.decode().strip()):
                        break

    def _answer(self, conn, line) -> bool:
        tag, _, rest = line.partition(" ")
        self.commands.append(rest)
        cmd = rest.upper()
        out = b""
        if cmd.startswith("CAPABILITY"):
            out = b"* CAPABILITY IMAP4rev1\r\n"
        elif cmd.startswith("EXAMINE"):
            out = (f"* {len(self.messages)} EXISTS\r\n* 0 RECENT\r\n* FLAGS (\\Seen)\r\n* OK [UIDVALIDITY 1700000001] UIDs valid\r\n* OK [UIDNEXT 99] next\r\n").encode()
        elif cmd.startswith("UID SEARCH"):
            out = ("* SEARCH " + " ".join(str(u) for u in sorted(self.messages)) + "\r\n").encode()
        elif cmd.startswith("UID FETCH"):
            uid = int(rest.split()[2])
            raw = self.messages[uid]
            head, tail = self.FETCH[self.style](sorted(self.messages).index(uid) + 1, uid, len(raw))
            out = head.encode() + raw + tail.encode()
        elif cmd.startswith("LOGOUT"):
            conn.sendall(f"* BYE\r\n{tag} OK done\r\n".encode())
            return True
        ok = "[READ-ONLY] " if cmd.startswith("EXAMINE") else ""
        conn.sendall(out + f"{tag} OK {ok}done\r\n".encode())
        return False


@pytest.mark.parametrize("style", sorted(LocalIMAP.FETCH))
def test_ok_real_imaplib_reads_each_servers_fetch_reply(make_ctx, style):
    ctx, _, sent = contacted(make_ctx)
    v = vars_for(sent)
    server = LocalIMAP({5: K.thr(v, "Thanks, more soon.\n" + "x" * 3000), 9: K.thr(v, K.OPT)}, style)
    server.start()

    def factory(host, port, ssl_context=None, timeout=None):
        return imaplib.IMAP4("127.0.0.1", server.port, timeout=timeout)

    try:
        out = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=factory)).run({}).output
    finally:
        server.sock.close()
    assert out.fetched == 2 and out.stored == 2 and DAVID in suppressed_values(ctx)
    assert any(c.upper().startswith("EXAMINE") for c in server.commands), "the mailbox must be opened read-only"
    assert all("PEEK" in c.upper() for c in server.commands if c.upper().startswith("UID FETCH")), "reading must not mark mail as read"
    from aime.db import kv_get

    with ctx.db.session() as s:
        assert kv_get(s, "inbox.state")["uidvalidity"] == "1700000001"
