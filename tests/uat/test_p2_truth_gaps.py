"""Phase 2, the real-world gaps of the "truth" report: what a person needs on their first days. Synthetic data only."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from aime.db import kv_get
from aime.models import Alert, Assignment, FollowUp, InboundEmail, JobRun, Lead, User
from aime.timeutil import utcnow
from aime.web.app import manager_today, plain, zone_name
from tests.uat.test_p2_truth import (PW, _no_task_scheduler, add_email, add_lead, client, org, words)  # noqa: F401


@pytest.fixture(autouse=True)
def _scheduler(_no_task_scheduler):  # noqa: F811
    yield


def test_mine_and_team_show_what_is_whose(org):  # noqa: F811
    add_lead(org, "Sam Co", owner="sam")
    add_lead(org, "Kim Co", owner="kim")
    c = client(org, "kim")
    team, mine = words(c.get("/my").text), words(c.get("/my?view=mine").text)
    assert "Leads: 2" in team and "Sam Co" in team
    assert "Leads: 1" in mine and "Kim Co" in mine and "Sam Co" not in mine
    assert 'href="/my?view=team"' in c.get("/my").text
    assert re.search(r"(\d+) matching", words(c.get("/leads?owner=me&group=working").text)).group(1) == "1"


def test_do_this_next_is_never_a_colleagues_task(org):  # noqa: F811
    lead_id = add_lead(org, "Sam Co", owner="sam")
    with org.db.session() as s:
        lead = s.get(Lead, lead_id)
        s.add(FollowUp(lead_id=lead_id, company_id=lead.company_id, channel="task", step=1, status="pending",
                       subject="Review Sam Co", body="x", due_at=utcnow()))
    add_lead(org, "Free Co")
    kim = words(client(org, "kim").get("/my").text)
    sam = words(client(org, "sam").get("/my").text)
    assert "Do this next: No lead is yours yet" in kim and "Do this next: Do the task: Review Sam Co" in sam
    assert "Your first day" in kim


def test_the_new_owner_is_told_of_a_handover(org):  # noqa: F811
    lead_id = add_lead(org, "Handed Co", owner="sam")
    with org.db.session() as s:
        s.add(Assignment(lead_id=lead_id, from_owner="owner", to_owner="sam", method="manual", reason="covering", actor="owner"))
    assert "Handed over to you" in words(client(org, "sam").get("/my").text)
    assert "Handed over to you" not in words(client(org, "kim").get("/my").text)


def test_emails_sent_opens_sent_mail_and_only_that(org):  # noqa: F811
    lead = add_lead(org)
    add_email(org, lead, subject="Went out")
    add_email(org, lead, status="awaiting_approval", subject="Still waiting")
    c = client(org, "owner")
    assert 'href="/followups?show=sent"' in c.get("/").text
    sent = words(c.get("/followups?show=sent").text)
    assert "1 sent email" in sent and "Went out" in sent and "Still waiting" not in sent


def test_a_waiting_draft_says_how_long_it_has_waited(org):  # noqa: F811
    fid = add_email(org, add_lead(org), status="awaiting_approval")
    with org.db.session() as s:
        s.get(FollowUp, fid).created_at = utcnow() - timedelta(days=39)
    assert "waiting 39 days" in words(client(org, "owner").get("/followups").text)


def test_failures_since_the_last_sign_in_survive_the_weekend(org):  # noqa: F811
    with org.db.session() as s:
        s.add(JobRun(job="enrich", trigger="scheduler", status="failed", started_at=utcnow() - timedelta(hours=40), error="x"))
        s.add(JobRun(job="enrich", trigger="scheduler", status="failed", started_at=utcnow() - timedelta(hours=50), error="x"))
    with org.db.session() as s:
        earlier = manager_today(org, s, utcnow() - timedelta(days=3))["earlier"]
        assert [(e["what"], e["n"]) for e in earlier] == [("enrich job failed", 2)]
        assert manager_today(org, s, utcnow() - timedelta(hours=45))["earlier"][0]["n"] == 1
        assert manager_today(org, s)["earlier"] == []


def test_signing_in_keeps_the_previous_sign_in(org):  # noqa: F811
    then = utcnow() - timedelta(days=3)
    with org.db.session() as s:
        user = s.execute(select(User).where(User.username == "owner")).scalar_one()
        user.last_login_at = then
        uid = user.id
    r = client(org).post("/login", data={"username": "owner", "password": PW, "next": "/manager"})
    assert r.status_code == 200
    with org.db.session() as s:
        assert kv_get(s, f"signin.previous.{uid}") == then.isoformat()


def test_a_scheduler_absence_is_one_line_and_one_acknowledgement(org):  # noqa: F811
    with org.db.session() as s:
        for job in ("research", "enrich", "content"):
            s.add(Alert(level="warning", source="monitor", message=f"Job '{job}' has not succeeded since 2026-10-30 10:00 UTC",
                        key=f"job_stale:{job}"))
        s.add(Alert(level="warning", source="monitor", message="Something else"))
    c = client(org, "owner")
    page = words(c.get("/system").text)
    assert "The scheduler was away: 3 scheduled jobs are late (content, enrich, research)" in page and "has not succeeded" not in page
    c.post("/alerts/ack_all", data={"group": "job_stale:"})
    with org.db.session() as s:
        left = [a.message for a in s.execute(select(Alert).where(Alert.acknowledged.is_(False))).scalars()]
    assert left == ["Something else"]
    c.post("/alerts/ack_all")
    with org.db.session() as s:
        assert not s.execute(select(Alert).where(Alert.acknowledged.is_(False))).first()


def test_a_salesperson_cannot_acknowledge_every_alert(org):  # noqa: F811
    with org.db.session() as s:
        s.add(Alert(level="warning", source="monitor", message="x"))
    assert client(org, "sam").post("/alerts/ack_all").status_code == 403
    with org.db.session() as s:
        assert s.execute(select(Alert).where(Alert.acknowledged.is_(False))).first()


def test_a_refused_sign_in_stays_failed_after_the_alert_is_acknowledged(org):  # noqa: F811
    org.settings.outbound_email_enabled = True
    with org.db.session() as s:
        s.add(Alert(level="error", source="sales_workflow", message="refused", key="mail:sign_in", acknowledged=True))
    with org.db.session() as s:
        row = next(b for b in manager_today(org, s)["board"] if b["name"] == "Email sending")
    assert row["status"] == "FAILED" and "refused the sign-in" in row["detail"]


def test_a_stored_reply_to_is_shown_beside_its_draft_when_one_exists(org):  # noqa: F811
    lead = add_lead(org)
    fid = add_email(org, lead, status="awaiting_approval", kind="response")
    with org.db.session() as s:
        s.add(InboundEmail(message_id="<q@x.example>", from_email="noreply@x.example", subject="Re", received_at=utcnow(),
                           status="actioned", lead_id=lead, suggested_followup_id=fid, category="question",
                           reply_to="person@x.example"))
    assert "person@x.example" in words(client(org, "owner").get("/followups").text)


@pytest.mark.parametrize("raw, want", [
    ("DISABLED: sending is switched off", "sending is switched off"),
    ("NOT CONFIGURED: SMTP_HOST / SMTP_FROM missing", "no mail server or sender address is set"),
    ("REQUIRE_HUMAN_APPROVAL=true: every email needs a person", "the owner's rule: every email needs a person"),
    ("Blocked: PUBLISHING_ENABLED=false", "Blocked: publishing is switched off"),
    ("set BACKUP_OFFSITE_DIR first", "set backup offsite dir first"),
    ("Nothing to translate here (CAN-SPAM)", "Nothing to translate here (CAN-SPAM)"),
    ("", ""),
])
def test_plain_words_for_machine_reasons(raw, want):
    assert plain(raw) == want


def test_zone_names_are_short_on_every_platform():
    class Windows:
        def __init__(self, name):
            self.name = name

        def tzname(self):
            return self.name

    assert zone_name(Windows("India Standard Time")) == "IST"
    assert zone_name(Windows("IST")) == "IST"
    assert zone_name(Windows("Coordinated Universal Time")) == "UTC"
