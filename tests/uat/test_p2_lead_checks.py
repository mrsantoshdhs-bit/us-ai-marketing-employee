"""Phase 2: small fixes made while merging the fix teams."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.agents.qualification import LeadQualificationAgent
from aime.models import Activity, Alert, Company, Contact, FollowUp, Lead, ResearchSnapshot, SkippedMail, User
from aime.reply_intel import suggest
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.web.auth import COOKIE, hash_password, make_cookie, session_secret

ORIGIN = {"origin": "http://testserver"}


@pytest.mark.parametrize("name, greeting", [("Sales Team", "Hi there,"), ("Customer Support", "Hi there,"), ("info", "Hi there,"),
                                            ("Dana Fox", "Hi Dana,"), ("Salesforce Admin", "Hi there,")])
def test_ok_a_mailbox_name_is_not_a_first_name(make_ctx, name, greeting):
    ctx = make_ctx()
    row = SimpleNamespace(from_name=name, subject="Question", new_text="Tell me more", body_text="", intent=None)
    assert suggest(ctx.settings, "objection", row, None)[1].startswith(greeting)


def signed_in(ctx, app, username):
    client = TestClient(app, follow_redirects=False, headers=ORIGIN)
    with ctx.db.session() as s:
        client.cookies.set(COOKIE, make_cookie(session_secret(ctx.settings.data_dir), s.execute(
            select(User).where(User.username == username)).scalar_one()))
    return client


def test_ok_mail_read_as_not_sales_is_listed_only_for_a_role_that_sees_everything(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    with ctx.db.session() as s:
        for name, role in (("boss", "super_admin"), ("eva", "sales_executive"), ("ana", "analyst")):
            s.add(User(username=name, password_hash=hash_password("a long test password"), role=role))
        s.add(SkippedMail(mailbox_uid="1", message_id="<a@x.example>", from_email="news@unrelated.example",
                          subject="Weekly offers", skipped_at=utcnow()))
    app = create_app(ctx)
    boss = signed_in(ctx, app, "boss").get("/inbox?show=skipped").text
    assert "news@unrelated.example" in boss and "Weekly offers" in boss
    assert "news@unrelated.example" in signed_in(ctx, app, "ana").get("/inbox?show=skipped").text
    eva = signed_in(ctx, app, "eva").get("/inbox?show=skipped")
    assert eva.status_code == 200 and "news@unrelated.example" not in eva.text and "Mail read as not sales" not in eva.text


def research_lead(ctx, **fields):
    with ctx.db.session() as s:
        co = Company(name="Acme", normalized_name="acme", source="test", domain="acme.example")
        s.add(co)
        s.flush()
        lead = Lead(company_id=co.id, source="research", status="qualified", score=60, grade="B", **fields)
        s.add(lead)
        s.flush()
        return co.id, lead.id


def row(ctx, model, pk, *names):
    with ctx.db.session() as s:
        found = s.get(model, pk)
        return tuple(getattr(found, n) for n in names)


def test_ok_a_running_sequence_keeps_the_topic_of_the_email_already_sent(make_ctx):
    ctx = make_ctx()
    _, lid = research_lead(ctx, outreach_status="contacted", primary_service="custom_software", matched_services=["custom_software"])
    assert LeadQualificationAgent(ctx).run({}).ok
    assert row(ctx, Lead, lid, "primary_service", "matched_services", "status") == ("custom_software", ["custom_software"], "qualified")


def test_ok_a_lead_the_score_parks_takes_its_unsent_first_email_out_of_the_queue(make_ctx):
    ctx = make_ctx()
    co, lid = research_lead(ctx, outreach_status="drafted")
    with ctx.db.session() as s:
        f = FollowUp(lead_id=lid, company_id=co, channel="email", step=1, kind="outreach", status="awaiting_approval",
                     subject="Hello", body="Hello", to_email="info@acme.example")
        s.add(f)
        s.flush()
        fid = f.id
    assert LeadQualificationAgent(ctx).run({}).ok
    assert row(ctx, Lead, lid, "status", "outreach_status") == ("nurture", None)
    status, why = row(ctx, FollowUp, fid, "status", "blocked_reason")
    assert status == "cancelled" and "Score fell" in why
    with ctx.db.session() as s:
        moved = s.execute(select(Alert).where(Alert.source == "qualification", Alert.message.like("Scoring moved%"))).scalars().all()
        assert len(moved) == 1 and "1 to nurture" in moved[0].message


def test_ok_ticking_off_a_review_task_is_not_working_the_lead_but_a_note_is(make_ctx):
    ctx = make_ctx()
    co, lid = research_lead(ctx)
    with ctx.db.session() as s:
        s.add(Activity(subject="Follow-up #1 done", actor="admin", type="followup", company_id=co, lead_id=lid))
    assert LeadQualificationAgent(ctx).run({}).ok
    assert row(ctx, Lead, lid, "status") == ("nurture",)
    with ctx.db.session() as s:
        lead = s.get(Lead, lid)
        lead.status = "qualified"
        s.add(Activity(subject="Note", actor="admin", type="note", company_id=co, lead_id=lid))
    assert LeadQualificationAgent(ctx).run({}).ok
    assert row(ctx, Lead, lid, "status") == ("qualified",)


def test_ok_page_junk_is_not_named_as_the_person_found(make_ctx):
    from aime.agents.sales_workflow import SalesWorkflowAgent

    ctx = make_ctx()
    co, lid = research_lead(ctx)
    with ctx.db.session() as s:
        s.add(Contact(company_id=co, first_name="HomeAbout", last_name="UsCareersTestimonialsBlogContact", source="website"))
        s.flush()
        lead = s.get(Lead, lid)
        snap = ResearchSnapshot(company_id=co, status="done", pages=[], people=[{"name": "HomeAbout UsCareers"}, {"name": "Dana Fox", "title": "CEO"}])
        assert SalesWorkflowAgent._person_found(s, lead, snap)[0] == "Dana Fox"
