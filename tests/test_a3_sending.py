"""A3, sending: SMTP sign-in with OAuth (XOAUTH2) for Gmail or Microsoft 365, a per-mailbox daily limit across every
kind of email, delivery tracking from the mailbox (bounced / replied), the B2B switch holding approved outreach
instead of cancelling it, and a stop switch on the command line. Fake SMTP and IMAP servers and a mock token server
only: nothing is sent and no account is used. Every email still needs a person's approval."""

from types import SimpleNamespace

import httpx
from sqlalchemy import select

from aime import automation as am
from aime.cli import cmd_automation
from aime.integrations.email_smtp import EmailSender
from aime.models import AuditEvent, FollowUp
from tests.test_a2_mailbox import GOOGLE, sign_in, token_server
from tests.test_automation import NOW, drafted, followup, run
from tests.test_inbound_sales import FakeSMTP
from tests.test_inbox import DAVID, MAIL, FakeIMAP, bounce_for, contacted, mail, run_inbox
from tests.test_outreach import approve_all


class OAuthSMTP(FakeSMTP):
    auths: list = []

    def login(self, u, p):
        raise AssertionError("password sign-in must not be used with MAIL_AUTH=oauth")

    def ehlo(self):
        pass

    def auth(self, mechanism, authobject):
        OAuthSMTP.auths.append((mechanism, authobject()))


def oauth_draft(make_ctx, connect=True):
    ctx, _sender, fid = drafted(make_ctx, **{**MAIL, **GOOGLE, "mail_auth": "oauth", "smtp_password": "",
                                             "imap_password": ""})
    ctx.http_transport = httpx.MockTransport(token_server)  # research is done: from here on, the mock sign-in service
    if connect:
        sign_in(ctx)
    approve_all(ctx)
    FakeSMTP.sent, OAuthSMTP.auths = [], []
    return ctx, EmailSender(ctx.settings, smtp_factory=OAuthSMTP), fid


def test_sending_signs_in_with_xoauth2_when_the_account_is_connected(make_ctx):
    ctx, sender, fid = oauth_draft(make_ctx)
    assert sender.status()[0] is True
    run(ctx, sender)
    assert followup(ctx, fid).status == "sent" and [m["To"] for m in FakeSMTP.sent] == [DAVID]
    assert OAuthSMTP.auths == [("XOAUTH2", f"user={ctx.settings.smtp_username}\x01auth=Bearer at-1\x01\x01")]


def test_without_a_connected_account_nothing_is_sent_and_it_waits(make_ctx):
    ctx, sender, fid = oauth_draft(make_ctx, connect=False)
    run(ctx, sender)
    f = followup(ctx, fid)
    assert FakeSMTP.sent == [] and f.status == "approved" and "not delivered" in f.blocked_reason
    assert "not connected" in f.blocked_reason
    unconfigured = EmailSender(type(ctx.settings).load(env_file=None, **{**MAIL, "mail_auth": "oauth"}))
    assert unconfigured.status() == (False, "NOT CONFIGURED: MAIL_AUTH=oauth needs the OAuth client in .env and "
                                            "the account connected")


def test_the_mailbox_daily_limit_counts_every_kind_of_email(make_ctx):
    ctx, sender, fid = drafted(make_ctx)
    approve_all(ctx)
    ctx.settings.business["outreach"]["mailbox_daily_limit"] = 1
    with ctx.db.session() as s:  # an answer to an inquiry already went out from the mailbox today
        f = s.get(FollowUp, fid)
        s.add(FollowUp(lead_id=f.lead_id, channel="email", kind="response", status="sent", subject="Re: hello",
                       to_email="someone@other.example", sent_at=NOW))
    FakeSMTP.sent = []
    run(ctx, sender)
    f = followup(ctx, fid)
    assert FakeSMTP.sent == [] and f.status == "approved" and "its limit" in f.blocked_reason


def test_delivery_is_tracked_from_bounces_and_replies(make_ctx):
    ctx, _sender, sent = contacted(make_ctx)
    assert followup(ctx, sent.id).delivery_status == "sent"
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Thanks, tell me more.",
                                 in_reply_to=sent.message_id)}
    run_inbox(ctx)
    assert followup(ctx, sent.id).delivery_status == "replied"

    ctx2, _sender2, sent2 = contacted(make_ctx)
    FakeIMAP.messages = {1: bounce_for(DAVID, sent2.message_id)}
    run_inbox(ctx2)
    assert followup(ctx2, sent2.id).delivery_status == "bounced"
    from fastapi.testclient import TestClient
    from aime.web.app import create_app
    assert '>bounced</span>' in TestClient(create_app(ctx2)).get("/followups?show=all").text


def test_turning_b2b_outreach_off_holds_approved_emails_instead_of_cancelling_them(make_ctx):
    ctx, sender, fid = drafted(make_ctx)
    approve_all(ctx)
    ctx.settings.business["outreach"]["b2b_enabled"] = False
    FakeSMTP.sent = []
    run(ctx, sender)
    f = followup(ctx, fid)
    assert f.status == "approved" and f.blocked_reason.startswith("Held: B2B outreach is turned off") and FakeSMTP.sent == []
    ctx.settings.business["outreach"]["b2b_enabled"] = True
    run(ctx, sender)
    assert followup(ctx, fid).status == "sent"


def test_the_stop_switch_works_from_the_command_line(make_ctx, capsys):
    ctx = make_ctx()
    assert cmd_automation(SimpleNamespace(action="emergency-stop", reason="test"), ctx) == 0
    with ctx.db.session() as s:
        assert am.get_state(s) == "emergency_stop"
        event = s.execute(select(AuditEvent).where(AuditEvent.action == "state_emergency_stop")).scalars().first()
        assert event is not None and event.actor == "command line"
    assert "Emergency stop" in capsys.readouterr().out
    assert cmd_automation(SimpleNamespace(action="pause", reason="test"), ctx) == 1  # not from an emergency stop
