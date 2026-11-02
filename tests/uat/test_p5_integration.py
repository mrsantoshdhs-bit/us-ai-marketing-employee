"""Phase 5, what testing the combined fixes found: an opt-out's wordings kept, a colleague's email at a company that said no, a new-PC restore run twice.
Synthetic data only; fake SMTP and IMAP."""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from sqlalchemy import select

from aime import restore, send_journal
from aime.context import create_context
from aime.crm_ops import is_suppressed
from aime.reply_intel import reply_footer
from aime.models import Activity, FollowUp, InboundEmail, Lead
from aime.restore import RestoreRefused, restore_database
from aime.timeutil import utcnow
from tests.test_inbox import DAVID, FakeIMAP, contacted, mail, run_inbox
from tests.uat.test_p2_recovery import FakeSMTP, approved_email, send_run
from tests.uat.test_p5_backup import bare_pc, confirm, pc, snapshot, status  # noqa: F401
from tests.uat.test_p5_replies_acting import second_lead_by_inquiry, web
from tests.uat.test_uat_email import ORIGIN
from tests.uat.test_uat_sales_journey import where

SIGNED = "\n\nThanks,\nJanet Ruiz\nVP Operations\nwww.synthetic-co.example"
NOTICE = "\n\nThis message is confidential. If you received it in error, please delete it from your system."


def read(make_ctx, words):
    ctx, _sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David <{DAVID}>", f"Re: {sent.subject}", words, in_reply_to=sent.message_id, msg_id="<i1@mail.example>")}
    run_inbox(ctx)
    return ctx


def check_backs(ctx):
    with ctx.db.session() as s:
        return [t.subject for t in s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending")).scalars() if "Check back" in t.subject]


@pytest.mark.parametrize("words", [
    "Take us off our vendor list.", "Take us off old vendor lists please.", "Get us off our old provider list.", "Take us off our software list, thanks.",
    "Opt out of our contract with you, and all emails.", "Opt out of the plan you sent. And the emails.", "Please opt out of the trial and contact us no further.",
    "Unsubscribe rate on this must be awful. Stop.",
    "If you emailed this address by mistake, delete it from your records.", "If you got this address by mistake, remove it from your records.",
    "If you intended this for someone else, delete it from your records.",
    "Don't email me after January.", "Do not contact me after next year.", "No more emails before January, none after either.", "Do not write to me before Q3. Do not write after it either.",
    *[w + SIGNED + NOTICE for w in ("Abmelden", "Baja", "Désinscription", "Cancellatemi dalla vostra lista.", "Non scrivetemi più", "Rimuovetemi", "Favor remover meu e-mail da lista.",
                                    "Descadastrar", "Cancelar minha inscrição", "Please go away.", "Bitte entfernen Sie mich", "Por favor, no me contacte", "Merci de ne plus me contacter",
                                    "Wrong person. Remove this address from your system.")],
])
def test_a_real_opt_out_keeps_its_suppression(make_ctx, words):
    ctx = read(make_ctx, words)
    with ctx.db.session() as s:
        assert is_suppressed(s, DAVID), s.execute(select(InboundEmail)).scalars().first().reasons
    assert not check_backs(ctx)


@pytest.mark.parametrize("words", [
    "We could opt out of the current plan at renewal in March.", "Please help get us off spreadsheets.", "Take us off our old ERP.",
    "Our unsubscribe rate went up. How do we stop it?",
    "Thanks for this, happy to chat next week." + SIGNED + NOTICE,
    "Baja California is our main region." + SIGNED + NOTICE,
    "Baja\n\nCalifornia is where we mostly work, so that is the region to look at.",
])
def test_their_own_business_still_suppresses_nothing(make_ctx, words):
    ctx = read(make_ctx, words)
    with ctx.db.session() as s:
        assert not is_suppressed(s, DAVID)


@pytest.mark.parametrize("words", ["Don't email me until April.", "Do not contact me until after January.", "Stop emailing me before Q3.",
                                   "Don't email me until April. After that we can talk."])
def test_a_date_to_write_on_still_sets_a_reminder_and_suppresses_nothing(make_ctx, words):
    ctx = read(make_ctx, words)
    with ctx.db.session() as s:
        assert not is_suppressed(s, DAVID)
    assert check_backs(ctx)


def colleagues_draft(make_ctx, text):
    """A reply on the first lead, then the answer drafted for a colleague's inquiry at the same company."""
    ctx, _sender, sent = contacted(make_ctx)
    _first, other = second_lead_by_inquiry(ctx)
    FakeIMAP.messages = {1: mail(f"David <{DAVID}>", f"Re: {sent.subject}", text, in_reply_to=sent.message_id, msg_id="<i2@mail.example>")}
    run_inbox(ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, other)
        draft = FollowUp(lead_id=other, company_id=lead.company_id, contact_id=lead.contact_id, channel="email", kind="response", status="awaiting_approval", due_at=utcnow(),
                         to_email=lead.contact.email, subject="Re: your inquiry", body=f"Hello, happy to help.\n\n{reply_footer(ctx.settings)}")
        s.add(draft)
        s.flush()
        return ctx, draft.id


def test_an_email_to_a_colleague_at_a_company_that_said_no_waits_for_a_person_who_has_read_that(make_ctx):
    ctx, fid = colleagues_draft(make_ctx, "Please remove me from your list. Do not contact me or anyone at our company again.")
    assert fid
    c = web(ctx)
    c.headers.update({**ORIGIN, "accept": "text/html"})
    assert "said not to contact them" in c.get("/followups").text
    refused = c.post(f"/followups/{fid}/approve", data={})
    assert "said not to contact them" in unquote(where(refused)) + refused.text
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "awaiting_approval"
    c.post(f"/followups/{fid}/approve", data={"company_said_no": "yes"})
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "approved"
        assert s.execute(select(Activity).where(Activity.subject.like(f"Email #{fid} approved although%"))).scalars().first()


def test_a_colleagues_email_with_no_opt_out_is_approved_as_before(make_ctx):
    ctx, fid = colleagues_draft(make_ctx, "Thanks, I passed this on.")
    assert fid
    c = web(ctx)
    c.headers.update({**ORIGIN, "accept": "text/html"})
    assert "said not to contact them" not in c.get("/followups").text
    c.post(f"/followups/{fid}/approve", data={})
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "approved"


def full_disk(*a, **k):
    raise OSError(28, "No space left on device")


@pytest.mark.parametrize("target, name", [(send_journal.os, "fsync"), (send_journal, "record_new_pc"), (restore, "_carry_the_moment")],
                         ids=["copying the journal", "after the copy", "carrying the moment"])
def test_a_new_pc_restore_stopped_and_run_again_still_knows_it_is_a_new_pc(pc, tmp_path, monkeypatch, target, name):  # noqa: F811
    old = pc()
    approved_email(old, tag="seen")
    send_run(old)  # so the journal in the copy has seen an email: on its own that reads as this PC's journal
    fid = approved_email(old)
    copy = snapshot(old, tmp_path, "copy.zip")
    send_run(old)
    went = ["p@seen.example", "p@a.example"]
    assert FakeSMTP.sent == went
    new = bare_pc(pc)
    with monkeypatch.context() as stopped:
        stopped.setattr(target, name, full_disk)
        with pytest.raises(RestoreRefused, match="run this again"):
            restore_database(new.settings, copy)
    assert not (new.settings.data_dir / "sent_journal.jsonl.partial").exists()
    done = restore_database(new.settings, copy)
    assert done["unknown_sent"] == [fid] and done["approved_waiting"] == 0
    again = create_context(new.settings)
    assert status(again, fid) == "uncertain"
    send_run(again)
    confirm(again)
    send_run(again)
    assert FakeSMTP.sent == went  # the old PC's, only
    again.db.dispose()


def test_a_copys_journal_cut_short_does_not_swallow_the_new_pc_line(tmp_path):
    (tmp_path / "copy.jsonl").write_text('{"event": "claim", "followup_id": 1, "to": "x", "message_id": null, "at": "2026-01-01T00:00:00"}', encoding="utf-8")
    send_journal.take_from_copy(tmp_path, tmp_path / "copy.jsonl")
    view = send_journal.read(tmp_path)
    assert view.new_pc_at and view.claims
