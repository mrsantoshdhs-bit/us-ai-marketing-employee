"""Phase 5, round 2: deal document copies that cannot be opened, and what pages say about a company that said no, a dead draft answer,
an unanswered question and a corrected name. Synthetic data, temp folders, fake mail; everything through the real routes."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import backup as B
from aime.backup import bring_back_documents, write_offsite
from aime.crm_ops import suppress
from aime.doctor import run_checks
from aime.models import Attachment, Company, FollowUp, InboundEmail, Lead, Opportunity, Suppression
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.conftest import make_ctx  # noqa: F401
from tests.test_inbox import DAVID, FakeIMAP, contacted, latest_reply, mail, run_inbox
from tests.uat.test_uat_sales_journey import add_people, client, make_lead, post, where

PASS = "synthetic-passphrase-1"
OTHER = "synthetic-passphrase-2"
BASE = "http://127.0.0.1:8765"
ORIGIN = {"origin": BASE}
SAID_NO = "Someone at this company said not to contact them."
YOURSELF = "write to them from the mailbox yourself, then log it"
HOSTILE = "<script>alert(1)</script>"


def browser(ctx, username=None):
    c = TestClient(create_app(ctx), base_url=BASE, raise_server_exceptions=False, follow_redirects=False)
    if username:
        signed = client(ctx, username)
        c.cookies.update(signed.cookies)
    return c


def send(c, url, **data):
    return c.post(url, headers=ORIGIN, data=data)


# ------------------------------------------------------------------ deal document copies
@pytest.fixture
def docs(make_ctx, tmp_path):  # noqa: F811
    off = tmp_path / "off"
    ctx = make_ctx(backup_offsite_dir=str(off), backup_passphrase=PASS)
    folder = ctx.settings.data_dir / "attachments"
    folder.mkdir()
    with ctx.db.session() as s:
        co = Company(name="Synth deal", normalized_name="synth deal", source="test", domain="deal.example")
        s.add(co)
        s.flush()
        opp = Opportunity(company_id=co.id, name="Synth deal", stage="new")
        s.add(opp)
        s.flush()
        opp_id = opp.id
    for name, body in (("a.pdf", b"%PDF contract a"), ("b.pdf", b"%PDF contract b")):
        (folder / name).write_bytes(body)
        with ctx.db.session() as s:
            s.add(Attachment(opportunity_id=opp_id, filename=f"named-{name}", stored_as=name, content_type="application/pdf", size=len(body),
                             sha256=B._sha256(body), uploaded_by="p5"))
    return ctx, folder, off / "attachments"


def write(ctx):
    made = write_offsite(ctx.settings, Path(ctx.db.engine.url.database))
    assert made["status"] == "ok" and not made["documents_skipped"]
    return made


def check(ctx):
    return next(c for c in run_checks(ctx) if c["name"] == "Deal documents are safe")


def rows(ctx):
    with ctx.db.session() as s:
        return [tuple(r) for r in s.execute(select(Attachment.stored_as, Attachment.sha256, Attachment.filename))]


@pytest.mark.parametrize("damage", [b"", b"AIMEBAK1" + b"x" * 50, b"not ours at all"])
def test_a_damaged_copy_is_reported_then_written_again_from_the_original(docs, damage):
    ctx, folder, copies = docs
    write(ctx)
    assert check(ctx)["ok"]
    (copies / "a.pdf.aimebak").write_bytes(damage)
    got = check(ctx)
    assert not got["ok"] and "named-a.pdf" in got["detail"] and "named-b.pdf" not in got["detail"]
    write(ctx)
    assert B.decrypt_bytes((copies / "a.pdf.aimebak").read_bytes(), PASS) == b"%PDF contract a"
    assert check(ctx)["ok"]
    (folder / "a.pdf").unlink()
    assert bring_back_documents(ctx.settings, rows(ctx)) == (1, [])


def test_a_passphrase_change_is_told_and_the_next_copy_writes_the_documents_again(docs):
    ctx, _folder, copies = docs
    write(ctx)
    ctx.settings.backup_passphrase = OTHER
    got = check(ctx)
    assert not got["ok"] and "passphrase" in got["detail"] and "named-a.pdf" in got["detail"]
    assert "never age out" in got["detail"] and "30 days" not in got["detail"]
    write(ctx)
    assert B.decrypt_bytes((copies / "a.pdf.aimebak").read_bytes(), OTHER) == b"%PDF contract a"
    assert check(ctx)["ok"]


def test_a_copy_whose_original_is_gone_is_kept_and_named(docs):
    ctx, folder, copies = docs
    write(ctx)
    stamp = (copies / "a.pdf.aimebak").read_bytes()
    (folder / "a.pdf").unlink()
    ctx.settings.backup_passphrase = OTHER
    write(ctx)
    assert (copies / "a.pdf.aimebak").read_bytes() == stamp  # the old passphrase still opens it: the only way back
    got = check(ctx)
    assert not got["ok"] and "keep the old passphrase" in got["detail"] and "named-a.pdf" in got["detail"]
    ctx.settings.backup_passphrase = PASS
    assert bring_back_documents(ctx.settings, rows(ctx))[0] == 1


def test_a_changed_original_never_replaces_a_copy_of_the_real_document(docs):
    ctx, folder, copies = docs
    write(ctx)
    stamp = (copies / "a.pdf.aimebak").read_bytes()
    (folder / "a.pdf").write_bytes(b"%PDF contract X")  # the same size: only its digest tells
    ctx.settings.backup_passphrase = OTHER
    write(ctx)
    assert (copies / "a.pdf.aimebak").read_bytes() == stamp
    assert B.decrypt_bytes((copies / "b.pdf.aimebak").read_bytes(), OTHER) == b"%PDF contract b"
    assert not check(ctx)["ok"]


def test_a_document_no_row_vouches_for_keeps_its_copy(docs):
    ctx, folder, copies = docs
    (folder / "orphan.pdf").write_bytes(b"%PDF orphan")
    write(ctx)
    ctx.settings.backup_passphrase = OTHER
    stamp = (copies / "orphan.pdf.aimebak").read_bytes()
    write(ctx)
    assert (copies / "orphan.pdf.aimebak").read_bytes() == stamp


def test_a_copy_that_opens_is_not_written_again_and_a_cloud_only_one_is_not_read(docs, monkeypatch):
    ctx, _folder, copies = docs
    write(ctx)
    stamp = (copies / "a.pdf.aimebak").read_bytes()
    write(ctx)
    assert (copies / "a.pdf.aimebak").read_bytes() == stamp
    monkeypatch.setattr(B, "_cloud_only", lambda path: path.parent == copies)
    ctx.settings.backup_passphrase = OTHER  # cannot be told without a download: no alarm and no rewrite
    assert check(ctx)["ok"]
    write(ctx)
    assert (copies / "a.pdf.aimebak").read_bytes() == stamp


def test_the_older_copies_notice_says_documents_do_not_age_out(docs):
    from aime.backup import OFFSITE_KEY_CHANGED
    from aime.db import kv_set
    ctx, _folder, _copies = docs
    made = write(ctx)
    # copies are named by the second: a second write in the same second would replace the first rather than follow it
    first = Path(ctx.settings.backup_offsite_dir) / made["file"]
    made["file"] = f"{B.OFFSITE_PREFIX}{datetime.strptime(made['file'][len(B.OFFSITE_PREFIX):-8], '%Y%m%d-%H%M%S') - timedelta(minutes=1):%Y%m%d-%H%M%S}.aimebak"
    first.rename(first.with_name(made["file"]))
    ctx.settings.backup_passphrase = OTHER
    write_again = write(ctx)
    assert write_again["key_changed_after"] == made["file"]
    with ctx.db.session() as s:
        kv_set(s, OFFSITE_KEY_CHANGED, {"file": made["file"], "at": utcnow().isoformat()})
    got = next(c for c in run_checks(ctx) if c["name"] == "Older off-machine copies open")
    assert "about 30 days" in got["detail"] and "Deal document copies never age out" in got["detail"]


# ------------------------------------------------------------------ a company that said no
def company_said_no(ctx, domain="no.example", name="Refusing Co"):
    lead_id = make_lead(ctx, company=name, domain=domain, email=f"dana@{domain}")
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        suppress(s, f"dana@{domain}", "asked")
        return lead_id, lead.company_id


def test_the_lead_page_and_the_list_say_the_company_said_no(make_ctx):  # noqa: F811
    ctx = make_ctx()
    lead_id, _ = company_said_no(ctx)
    other = make_lead(ctx, company="Happy Co", domain="yes.example", email="sam@yes.example")
    c = browser(ctx)
    page = c.get(f"/leads/{lead_id}").text
    assert SAID_NO in page and page.index(SAID_NO) < page.index("Score ")
    assert SAID_NO not in c.get(f"/leads/{other}").text
    listing = c.get("/leads").text
    assert listing.count("Company said no") == 1
    with ctx.db.session() as s:
        assert s.get(Lead, lead_id).status == "qualified"  # display only


def inbound(ctx, sender, category="interested", company_id=None, lead_id=None, **over):
    with ctx.db.session() as s:
        row = InboundEmail(from_email=sender, subject="Hello", body_text="We are interested, can we talk next week?", category=category, status="needs_review",
                           recommended_status="Opportunity: book the meeting they asked for", received_at=utcnow(), message_id=f"<{sender}-{category}@x.example>",
                           company_id=company_id, lead_id=lead_id, **over)
        s.add(row)
        s.flush()
        return row.id


def test_mail_from_an_unknown_address_at_that_company_warns_and_recommends_no_deal(make_ctx):  # noqa: F811
    ctx = make_ctx()
    company_said_no(ctx)
    make_lead(ctx, company="Happy Co", domain="yes.example", email="sam@yes.example")
    stranger = inbound(ctx, "new.person@no.example")
    fine = inbound(ctx, "new.person@yes.example")
    page = browser(ctx).get("/inbox").text
    theirs = page[page.index(f'id="r{stranger}"'):]
    theirs = theirs[:theirs.index(f'id="r{fine}"')] if f'id="r{fine}"' in theirs else theirs
    mine = page[page.index(f'id="r{fine}"'):]
    mine = mine[:mine.index(f'id="r{stranger}"')] if f'id="r{stranger}"' in mine else mine
    assert SAID_NO in theirs and "Opportunity: book" not in theirs
    assert SAID_NO not in mine and "Opportunity: book" in mine
    with ctx.db.session() as s:
        row = s.get(InboundEmail, stranger)
        assert row.status == "needs_review" and row.recommended_status.startswith("Opportunity") and s.execute(select(Opportunity)).first() is None


# ------------------------------------------------------------------ a draft answer that was rejected or cancelled
def said_yes(make_ctx, **over):  # noqa: F811
    ctx, _sender, sent = contacted(make_ctx, **over)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Yes, interested. Please tell me more.", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    row = latest_reply(ctx)
    assert row.suggested_followup_id
    return ctx, row


def test_after_a_rejected_answer_the_row_and_lead_page_say_how_to_answer(make_ctx):  # noqa: F811
    ctx, row = said_yes(make_ctx)
    c = browser(ctx)
    assert YOURSELF not in c.get("/inbox?show=all").text and YOURSELF not in c.get(f"/leads/{row.lead_id}").text  # the draft still waits
    assert send(c, f"/followups/{row.suggested_followup_id}/reject").status_code == 303
    assert YOURSELF in c.get("/inbox?show=all").text and YOURSELF in c.get(f"/leads/{row.lead_id}").text
    with ctx.db.session() as s:
        s.get(FollowUp, row.suggested_followup_id).status = "cancelled"
    assert YOURSELF in c.get(f"/leads/{row.lead_id}").text


def test_no_such_advice_once_something_went_or_the_company_said_no(make_ctx):  # noqa: F811
    ctx, row = said_yes(make_ctx)
    c = browser(ctx)
    send(c, f"/followups/{row.suggested_followup_id}/reject")
    with ctx.db.session() as s:
        suppress(s, DAVID, "asked")
    assert YOURSELF not in c.get(f"/leads/{row.lead_id}").text and YOURSELF not in c.get("/inbox?show=all").text


def test_no_such_advice_when_a_new_answer_waits(make_ctx):  # noqa: F811
    ctx, row = said_yes(make_ctx)
    c = browser(ctx)
    send(c, f"/followups/{row.suggested_followup_id}/reject")
    with ctx.db.session() as s:
        old = s.get(FollowUp, row.suggested_followup_id)
        s.add(FollowUp(lead_id=old.lead_id, company_id=old.company_id, contact_id=old.contact_id, channel="email", kind="response", status="awaiting_approval",
                       subject="s", body="b", to_email=DAVID, due_at=utcnow(), step=old.step))
    assert YOURSELF not in c.get(f"/leads/{row.lead_id}").text


# ------------------------------------------------------------------ small ones
def test_a_corrected_name_warns_on_a_card_that_still_uses_the_old_one(make_ctx):  # noqa: F811
    ctx, _sender, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        lead = s.get(Lead, sent.lead_id)
        cid, old_name = lead.company_id, lead.company.name
        s.add(FollowUp(lead_id=lead.id, company_id=cid, contact_id=sent.contact_id, channel="email", kind="outreach", status="awaiting_approval", step=3,
                       subject=f"Again about {old_name}", body="Hello", to_email=DAVID, due_at=utcnow(), evidence=[{"kind": "x", "fact": "a fact", "url": "https://x.example"}]))
    c = browser(ctx)
    assert "was corrected" not in c.get("/followups").text
    assert send(c, f"/companies/{cid}/name", name=f"Better {HOSTILE}").status_code == 303
    page = c.get("/followups").text
    assert "was corrected" in page and old_name in page and "subject and evidence" in page and HOSTILE not in page


def test_not_competitor_takes_the_write_lock(make_ctx, monkeypatch):  # noqa: F811
    import aime.web.app as web_app
    ctx = make_ctx()
    make_lead(ctx, company="Flagged", domain="flag.example")
    with ctx.db.session() as s:
        co = s.execute(select(Company)).scalars().first()
        co.is_competitor = True
        cid = co.id
    taken = []
    real = web_app.take_write_lock
    monkeypatch.setattr(web_app, "take_write_lock", lambda s: (taken.append(1), real(s))[1])
    assert send(browser(ctx), f"/companies/{cid}/not_competitor").status_code == 303
    assert taken


@pytest.mark.parametrize("name, category, code", [("eva", "calendly", 403), ("mia", "calendly", 303), ("eva", "interested", 303)])
def test_only_who_may_apply_a_calendly_notice_marks_it_reviewed(make_ctx, name, category, code):  # noqa: F811
    ctx = make_ctx(dashboard_auth=True)
    add_people(ctx)
    lead_id = make_lead(ctx, owner="eva", company="Acme", domain="acme.example", email="dana@acme.example")
    rid = inbound(ctx, "notifications@calendly.com", category=category, lead_id=lead_id)
    r = post(client(ctx, name), f"/inbox/{rid}/reviewed")
    assert r.status_code == code, where(r)
    with ctx.db.session() as s:
        assert s.get(InboundEmail, rid).status == ("needs_review" if code == 403 else "reviewed")


def test_the_lift_keeps_the_address_out_of_the_url(make_ctx):  # noqa: F811
    ctx = make_ctx()
    with ctx.db.session() as s:
        suppress(s, "pat@list.example", "asked")
        sid = s.execute(select(Suppression.id)).scalar_one()
    r = send(browser(ctx), f"/suppressions/{sid}/lift", reason="a mistake")
    assert "pat@list.example" not in where(r) and "pat%40list" not in r.headers["location"] and "Nothing was drafted or sent" in where(r)


# ------------------------------------------------------------------ the question an objection replaced
def asked_then_objected(make_ctx, first="Can you send pricing?"):  # noqa: F811
    ctx, _sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", first, in_reply_to=sent.message_id, msg_id="<q1@mail.example>")}
    run_inbox(ctx)
    FakeIMAP.messages[2] = mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "That sounds too expensive. Why would this be different?",
                                in_reply_to=sent.message_id, msg_id="<q2@mail.example>")
    run_inbox(ctx)
    row = latest_reply(ctx)
    return ctx, row


def test_the_card_shows_the_earlier_unanswered_question_and_approve_behaves_as_before(make_ctx):  # noqa: F811
    ctx, row = asked_then_objected(make_ctx, f"Can you send pricing? {HOSTILE}")
    assert row.suggested_followup_id
    c = browser(ctx)
    page = c.get("/followups").text
    assert "Asked earlier, not answered yet" in page and "Can you send pricing?" in page and HOSTILE not in page
    r = send(c, f"/followups/{row.suggested_followup_id}/approve", qc_override="yes")
    assert r.status_code == 303
    with ctx.db.session() as s:
        assert s.get(FollowUp, row.suggested_followup_id).status == "approved"


def test_a_question_already_answered_is_not_shown_again(make_ctx):  # noqa: F811
    ctx, row = asked_then_objected(make_ctx)
    with ctx.db.session() as s:
        older = s.execute(select(InboundEmail).where(InboundEmail.id != row.id).order_by(InboundEmail.id.desc())).scalars().first()
        was = s.get(FollowUp, row.suggested_followup_id)
        s.add(FollowUp(lead_id=was.lead_id, company_id=was.company_id, contact_id=was.contact_id, channel="email", kind="response", status="sent", subject="Answer",
                       body="b", to_email=DAVID, due_at=older.received_at, sent_at=older.received_at, step=was.step))
    assert "Asked earlier, not answered yet" not in browser(ctx).get("/followups").text
