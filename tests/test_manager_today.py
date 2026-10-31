"""Manager Today: what happened today, the exceptions that need a person (each with its reason), failures and
automation health, all read from the records. Emails an active authorization covers are routine work, not
exceptions; nothing is listed that did not happen."""

from datetime import timedelta

from fastapi.testclient import TestClient

from aime.models import Activity, Company, InboundEmail, JobRun, Lead
from aime.timeutil import utcnow
from aime.web.app import create_app, manager_today
from tests.test_automation import authorize, drafted


def test_the_page_has_every_section_and_honest_empty_states(make_ctx):
    ctx = make_ctx()
    page = TestClient(create_app(ctx)).get("/manager").text
    for text in ("Manager Today", "Needs you", "Automation health", "Qualified today", "Hot leads and open opportunities",
                 "Interested replies", "Meeting requests", "Confirmed meetings", "Failures and alerts",
                 "Nothing needs you", "No leads qualified today", "No meetings booked"):
        assert text in page, text
    assert 'href="/manager"' in TestClient(create_app(ctx)).get("/").text  # in the sidebar


def test_only_leads_qualified_today_are_listed(make_ctx):
    ctx = make_ctx()
    now = utcnow()
    with ctx.db.session() as s:
        for name, when in (("Today Co", now), ("Earlier Co", now - timedelta(days=2))):
            co = Company(name=name, normalized_name=name.lower(), domain=f"{name.split()[0].lower()}.example", source="test")
            s.add(co)
            s.flush()
            lead = Lead(company_id=co.id, source="research", status="qualified", grade="B", score=60)
            s.add(lead)
            s.flush()
            s.add(Activity(company_id=co.id, lead_id=lead.id, type="status_change", actor="qualification",
                           subject="Lead nurture → qualified (score 60, grade B)", created_at=when))
    with ctx.db.session() as s:
        view = manager_today(ctx, s)
    assert [q["company"] for q in view["qualified"]] == ["Today Co"]


def test_exceptions_say_why_a_person_is_needed(make_ctx):
    ctx, _sender, fid = drafted(make_ctx)  # REQUIRE_HUMAN_APPROVAL stays true
    with ctx.db.session() as s:
        s.add(InboundEmail(message_id="<p1@x>", from_email="ann@prospect.example", subject="What does it cost?",
                           status="needs_review", category="question", intent="pricing_request", confidence="high",
                           received_at=utcnow()))
        s.add(JobRun(job="research", trigger="scheduler", status="failed", started_at=utcnow() - timedelta(hours=1),
                     finished_at=utcnow() - timedelta(hours=1), error="SEC EDGAR returned 503"))
    with ctx.db.session() as s:
        view = manager_today(ctx, s)
    whys = {n["kind"]: n["why"] for n in view["needs"]}
    assert "pricing" in whys["Reply"] and "every email needs a person" in whys["Email"]
    assert any("SEC EDGAR returned 503" in f["why"] for f in view["failures"])
    page = TestClient(create_app(ctx)).get("/manager").text
    assert "What does it cost?" in page and "Why: a pricing question" in page


def test_an_email_an_authorization_covers_is_routine_not_an_exception(make_ctx):
    ctx, _sender, fid = drafted(make_ctx, require_human_approval=False)
    authorize(ctx)
    with ctx.db.session() as s:
        assert [n for n in manager_today(ctx, s)["needs"] if n["kind"] == "Email"] == []
    ctx2, _sender2, _fid2 = drafted(make_ctx, require_human_approval=False)  # no authorization: a person decides
    with ctx2.db.session() as s:
        [email] = [n for n in manager_today(ctx2, s)["needs"] if n["kind"] == "Email"]
        assert "not on an active campaign authorization" in email["why"]
