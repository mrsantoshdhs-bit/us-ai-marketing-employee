"""Least privilege below a Sales Manager, enforced by the server for every request, whatever the page shows:
a Read-Only Analyst sees the pages and changes nothing; a Sales Executive (BDE) works leads and drafts but cannot
approve what goes out to customers."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.cli import cmd_user
from aime.models import Activity, Company, ContentItem, FollowUp, Lead
from aime.reply_intel import reply_footer
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.test_dashboard_auth import GOOD, ORIGIN, Args, _login

EDITED = "Hi there, edited by the BDE"


@pytest.fixture
def team(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    for name, role in (("manager", "sales_manager"), ("ana", "analyst"), ("bde", "sales_executive")):
        assert cmd_user(Args("create", name, role=role), ctx, prompt_fn=lambda _p: GOOD) == 0
    with ctx.db.session() as s:
        co = Company(name="Brightpath", normalized_name="brightpath", source="test")
        s.add(co)
        s.flush()
        lead = Lead(company_id=co.id, source="inbound", status="qualified")
        s.add(lead)
        s.flush()
        f = FollowUp(lead_id=lead.id, company_id=co.id, channel="email", step=1, due_at=utcnow(),
                     status="awaiting_approval", subject="Hello", body="Hi there" + reply_footer(ctx.settings),
                     to_email="x@lead.example")
        post = ContentItem(service="ai_agents", industry="saas", title="T", body="Body?", hashtags=[],
                           scheduled_for=utcnow() + timedelta(days=1), status="awaiting_approval",
                           generator="template", fingerprint="fp")
        s.add_all([f, post])
        s.flush()
        ids = {"lead": lead.id, "followup": f.id, "post": post.id}
    return ctx, ids


def signed_in(ctx, username):
    client = TestClient(create_app(ctx))
    assert _login(client, username=username).status_code == 303
    return client


def test_a_read_only_analyst_sees_every_sales_page_and_changes_nothing(team):
    ctx, ids = team
    ana = signed_in(ctx, "ana")
    for path in ("/", "/leads", f"/leads/{ids['lead']}", "/pipeline", "/followups", "/content", "/reports", "/inbox",
                 "/meetings", "/api/stats", "/api/leads"):
        assert ana.get(path).status_code == 200, path
    assert "Read-Only Analyst" in ana.get("/").text
    for path in (f"/leads/{ids['lead']}/note", f"/followups/{ids['followup']}/approve",
                 f"/followups/{ids['followup']}/save", f"/content/{ids['post']}/approve", "/suppress",
                 "/automation/state/pause", f"/leads/{ids['lead']}/stop_outreach", "/opportunities/1"):
        r = ana.post(path, headers=ORIGIN, data={"note": "x", "body": "edited", "value": "a@b.example"},
                     follow_redirects=False)
        assert r.status_code == 403 and "Read-only" in r.json()["detail"], path
    for path in ("/admin/mailboxes", "/admin/settings"):
        assert ana.get(path).status_code == 403  # Super Admin pages stay closed
    with ctx.db.session() as s:
        assert s.get(FollowUp, ids["followup"]).status == "awaiting_approval"
        assert s.get(FollowUp, ids["followup"]).body == "Hi there" + reply_footer(ctx.settings)  # not edited
        assert not s.execute(select(Activity).where(Activity.actor == "ana")).first()
    assert ana.post("/logout", headers=ORIGIN, follow_redirects=False).status_code == 303  # signing out still works


def test_a_sales_executive_works_leads_and_drafts_but_a_manager_approves_what_goes_out(team):
    ctx, ids = team
    bde = signed_in(ctx, "bde")
    assert "Sales Executive" in bde.get("/").text
    assert bde.post(f"/leads/{ids['lead']}/note", headers=ORIGIN, data={"note": "Called, asked for a deck"},
                    follow_redirects=False).status_code == 303
    assert bde.post(f"/followups/{ids['followup']}/save", headers=ORIGIN,
                    data={"subject": "Hello", "body": EDITED + reply_footer(ctx.settings)},
                    follow_redirects=False).status_code == 303
    for path in (f"/followups/{ids['followup']}/approve", f"/followups/{ids['followup']}/reconfirm",
                 f"/followups/{ids['followup']}/requeue", f"/followups/{ids['followup']}/mark_sent",
                 f"/content/{ids['post']}/approve", f"/content/{ids['post']}/confirm_published", "/campaigns",
                 "/campaigns/1/status"):
        r = bde.post(path, headers=ORIGIN, follow_redirects=False)
        assert r.status_code == 403 and "Sales Manager approves" in r.json()["detail"], path
    assert bde.post("/jobs/monitor/run", headers=ORIGIN, follow_redirects=False).status_code == 403  # admin only
    with ctx.db.session() as s:
        f = s.get(FollowUp, ids["followup"])
        assert f.status == "awaiting_approval" and f.body.startswith(EDITED)
        assert s.get(ContentItem, ids["post"]).status == "awaiting_approval"
    manager = signed_in(ctx, "manager")
    manager.post(f"/followups/{ids['followup']}/approve", headers=ORIGIN, follow_redirects=False)
    manager.post(f"/content/{ids['post']}/approve", headers=ORIGIN, follow_redirects=False)
    with ctx.db.session() as s:
        assert (s.get(FollowUp, ids["followup"]).status, s.get(FollowUp, ids["followup"]).approved_by) == ("approved", "manager")
        assert s.get(ContentItem, ids["post"]).approved_by == "manager"


def test_roles_are_given_only_from_the_command_line(team, capsys):
    ctx, _ = team
    assert cmd_user(Args("set-role", "bde", role="analyst"), ctx) == 0
    assert "bde is now analyst (was sales_executive)" in capsys.readouterr().out
    assert cmd_user(Args("set-role", "bde", role=None), ctx) == 1
    out = capsys.readouterr().out  # every built-in role is offered
    assert all(r in out for r in ("super_admin", "sales_director", "team_leader", "sales_executive", "bde",
                                  "lead_gen_executive", "analyst"))
    bde = signed_in(ctx, "bde")
    assert bde.post(f"/leads/{1}/note", headers=ORIGIN, data={"note": "x"}, follow_redirects=False).status_code == 403
