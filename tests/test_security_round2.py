"""The remaining findings of the 29 Oct security review: forged Calendly notices, calendar invitations that bypass the
email rules, inquiry replies that quote a stranger's text, query strings in the access log, sessions that outlive
sign-out, and sign-in messages that reveal which accounts exist."""

import logging

from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.booking import invite_problem
from aime.cli import PathOnlyAccessLog
from aime.crm_ops import suppress
from aime.email_templates import _linkedin_event, draft_reply
from aime.inbox import parse_message
from aime.mail_auth import passing_domains
from aime.models import InboundEmail, Meeting, User
from aime.web.app import create_app
from aime.web.auth import SIGN_IN_FAILED
from tests.test_dashboard_auth import GOOD, ORIGIN, _login, auth_ctx  # noqa: F401  (pytest fixture)
from tests.test_inbox import FakeIMAP, contacted, mail, run_inbox
from tests.test_meetings_pipeline import BOOKED_BODY, BOOKED_SUBJECT

CALENDLY = "Calendly <notifications@calendly.com>"
SIGNED = "mx.us.example; dkim=pass header.d=calendly.com header.s=s1; spf=pass smtp.mailfrom=calendly.com"


def notice(uid, frm=CALENDLY, auth=None, subject=BOOKED_SUBJECT, body=BOOKED_BODY):
    FakeIMAP.messages[uid] = mail(frm, subject, body, msg_id=f"<n{uid}@cal.example>",
                                  headers={"Authentication-Results": auth} if auth else None)
    run_inbox(FakeIMAP.ctx)
    with FakeIMAP.ctx.db.session() as s:
        return s.execute(select(InboundEmail).where(InboundEmail.message_id == f"<n{uid}@cal.example>")).scalar_one()


def meetings(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Meeting)).scalars().all()


def test_only_a_calendly_notice_the_mail_server_vouches_for_changes_the_crm(make_ctx):
    ctx, _, _ = contacted(make_ctx, calendly_verified_only=True)
    FakeIMAP.ctx, FakeIMAP.messages = ctx, {}
    forged = notice(1)  # "From: Calendly" with no authentication result: anyone can write that
    assert (forged.category, forged.status) == ("calendly", "needs_review") and "not applied" in forged.action_note
    assert meetings(ctx) == []
    wrong = notice(2, auth="mx.us.example; dkim=pass header.d=evil.example", subject=BOOKED_SUBJECT.replace("10:00am", "11:00am"))
    assert wrong.status == "needs_review" and meetings(ctx) == []  # signed, but not by calendly.com
    real = notice(3, auth=SIGNED, subject=BOOKED_SUBJECT.replace("10:00am", "9:00am"),
                  body=BOOKED_BODY.replace("10:00am - 10:20am", "9:00am - 9:20am"))
    assert real.status == "actioned" and len(meetings(ctx)) == 1


def test_a_person_can_apply_a_notice_after_checking_it_in_calendly(make_ctx):
    ctx, _, _ = contacted(make_ctx, calendly_verified_only=True)
    FakeIMAP.ctx, FakeIMAP.messages = ctx, {}
    row = notice(1)
    c = TestClient(create_app(ctx))
    r = c.post(f"/inbox/{row.id}/apply_calendly", headers=ORIGIN, follow_redirects=False)
    assert "Applied" in r.headers["location"].replace("%20", " ")
    assert len(meetings(ctx)) == 1
    with ctx.db.session() as s:
        assert "confirmed by dashboard user" in s.get(InboundEmail, row.id).action_note


def test_only_the_topmost_authentication_result_counts():
    raw = (b"Authentication-Results: mx.us.example; dkim=fail header.d=calendly.com\r\n"
           b"Authentication-Results: forged.example; dkim=pass header.d=calendly.com\r\n"
           b"From: Calendly <notifications@calendly.com>\r\nSubject: New Event: x\r\nMessage-ID: <a@b>\r\n\r\nhello\r\n")
    p = parse_message(raw)
    assert "dkim=fail" in p.auth_results and passing_domains(p.auth_results) == set()
    assert passing_domains("mx; dkim=pass header.i=@mail.calendly.com; dmarc=pass header.from=calendly.com") == {
        "mail.calendly.com", "calendly.com"}
    assert passing_domains("mx; spf=pass smtp.mailfrom=calendly.com") == set()  # SPF alone is not enough


def test_a_notice_forwarded_by_the_owner_counts_only_with_the_owners_signature(make_ctx):
    owner = "owner@owner-test.example"
    ctx, _, _ = contacted(make_ctx, calendly_verified_only=True, outbound_test_recipients=owner)
    FakeIMAP.ctx, FakeIMAP.messages = ctx, {}
    fwd = f"---------- Forwarded message ---------\nFrom: Calendly <notifications@calendly.com>\n\n{BOOKED_BODY}"
    unsigned = notice(1, frm=f"Owner <{owner}>", subject=f"Fwd: {BOOKED_SUBJECT}", body=fwd)
    assert unsigned.status == "needs_review"
    stranger = notice(2, frm="Someone <x@stranger.example>", auth="mx; dkim=pass header.d=stranger.example",
                      subject=f"Fwd: {BOOKED_SUBJECT.replace('10:00am', '11:00am')}", body=fwd.replace("10:00am - 10:20am", "11:00am - 11:20am"))
    assert stranger.status in ("needs_review", None) and meetings(ctx) == []
    signed = notice(3, frm=f"Owner <{owner}>", auth="mx.us.example; dkim=pass header.d=owner-test.example",
                    subject=f"Fwd: {BOOKED_SUBJECT}", body=fwd)
    assert signed.status == "actioned" and len(meetings(ctx)) == 1


def test_calendar_invitations_follow_the_email_rules(make_ctx):
    ctx = make_ctx(calendar_send_invites=True)
    with ctx.db.session() as s:
        assert invite_problem(s, ctx.settings, "ann@acme.example") is None
        suppress(s, "ann@acme.example", "test")
        assert "do-not-contact" in invite_problem(s, ctx.settings, "ann@acme.example")
    test_mode = make_ctx(calendar_send_invites=True, outbound_test_recipients="owner@owner-test.example")
    with test_mode.db.session() as s:
        assert "test mode" in invite_problem(s, test_mode.settings, "bob@acme.example")
        assert invite_problem(s, test_mode.settings, "owner@owner-test.example") is None
    off = make_ctx()  # invitations off: only our own approved email goes out, nothing to check here
    with off.db.session() as s:
        assert invite_problem(s, off.settings, "ann@acme.example") is None


def test_inquiry_replies_do_not_quote_what_a_stranger_typed(make_ctx):
    ctx = make_ctx()
    excerpt = "Buy cheap pills at http://spam.example now"
    subject, body = draft_reply(ctx.settings, 1, "Ann", "custom_crm", excerpt)
    assert "spam.example" not in body and "pills" not in body and "custom CRM" in body
    event = _linkedin_event("LinkedIn event: AI Summit <script> visit www.evil.example or mail x@evil.example")
    assert event == "AI Summit script visit or mail" and "evil" not in event


def test_the_access_log_keeps_the_path_but_not_the_query():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5000", "GET", "/setup?token=secret-value", "1.1", 200), None)
    assert PathOnlyAccessLog().filter(record) and record.getMessage() == '127.0.0.1:5000 - "GET /setup HTTP/1.1" 200'


def test_signing_out_ends_the_session_even_for_a_copied_cookie(auth_ctx):  # noqa: F811
    c = TestClient(create_app(auth_ctx))
    _login(c)
    copied = dict(c.cookies)
    assert c.get("/leads", follow_redirects=False).status_code == 200
    c.post("/logout", headers=ORIGIN, follow_redirects=False)
    thief = TestClient(create_app(auth_ctx))
    thief.cookies.update(copied)
    assert thief.get("/leads", follow_redirects=False).status_code == 303  # back to sign-in


def test_sign_in_failures_all_read_the_same(auth_ctx):  # noqa: F811
    c = TestClient(create_app(auth_ctx))
    messages = set()
    for _ in range(6):  # the fifth wrong password locks the account; the sixth try meets the lock
        r = _login(c, password="wrong password!!")
        messages.add(r.headers["location"])
    messages.add(_login(c, username="nobody-here", password=GOOD).headers["location"])
    assert len({m.split("&next")[0] for m in messages}) == 1  # locked, wrong or unknown: one message
    assert SIGN_IN_FAILED.split(".")[0].replace(" ", "%20") in next(iter(messages))
    with auth_ctx.db.session() as s:
        assert s.execute(select(User).where(User.username == "manager")).scalar_one().locked_until is not None
