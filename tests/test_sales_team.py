"""The sales organisation: accounts made on the dashboard with one-time setup links, built-in and custom roles,
teams and reporting lines, who sees which records (enforced by the server), automatic assignment (workload, round
robin, skill, availability, capacity), manual reassignment with history, and escalation of untouched deals."""

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from aime.assignment import assign_new, escalations
from aime.cli import cmd_user
from aime.models import Alert, Assignment, AuditEvent, Company, CustomRole, Lead, Opportunity, Team, User
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.test_dashboard_auth import GOOD, ORIGIN, Args, _login


@pytest.fixture
def org(make_ctx):
    """An administrator, a Sales Manager with a team of two executives, an executive outside the team, an analyst."""
    ctx = make_ctx(dashboard_auth=True)
    for name, role in (("boss", "admin"), ("mia", "sales_manager"), ("eva", "sales_executive"), ("ed", "sales_executive"),
                       ("zoe", "sales_executive"), ("ana", "analyst")):
        assert cmd_user(Args("create", name, role=role), ctx, prompt_fn=lambda _p: GOOD) == 0
    with ctx.db.session() as s:
        team = Team(name="US East", manager="mia")
        s.add(team)
        s.flush()
        for u in s.execute(select(User).where(User.username.in_(("eva", "ed")))).scalars():
            u.team_id = team.id
        co = Company(name="Acme", normalized_name="acme", source="test")
        s.add(co)
        s.flush()
        leads = {}
        for owner in ("eva", "ed", "zoe", None):
            lead = Lead(company_id=co.id, source="inbound", status="qualified", owner=owner)
            s.add(lead)
            s.flush()
            leads[owner or "nobody"] = lead.id
    return ctx, leads


def client(ctx, username):
    c = TestClient(create_app(ctx))
    assert _login(c, username=username).status_code == 303
    return c


def lead_ids_on_page(html):
    return {int(x) for x in re.findall(r'href="/leads/(\d+)"', html)}


def test_each_person_sees_their_own_their_teams_or_every_lead(org):
    ctx, leads = org
    seen = {u: lead_ids_on_page(client(ctx, u).get("/leads").text) for u in ("eva", "mia", "zoe", "ana", "boss")}
    assert seen["eva"] == {leads["eva"], leads["nobody"]}  # her own and the unassigned
    assert seen["mia"] == {leads["eva"], leads["ed"], leads["nobody"]}  # her team's: eva and ed, not zoe
    assert seen["zoe"] == {leads["zoe"], leads["nobody"]}
    assert seen["ana"] == seen["boss"] == set(leads.values())  # the analyst and the admin see all


def test_someone_elses_record_does_not_exist_for_viewing_or_changing(org):
    ctx, leads = org
    eva = client(ctx, "eva")
    assert eva.get(f"/leads/{leads['zoe']}").status_code == 404
    r = eva.post(f"/leads/{leads['zoe']}/note", headers=ORIGIN, data={"note": "x"}, follow_redirects=False)
    assert r.status_code == 404
    assert eva.get(f"/leads/{leads['eva']}").status_code == 200
    assert eva.post(f"/leads/{leads['eva']}/note", headers=ORIGIN, data={"note": "called"},
                    follow_redirects=False).status_code == 303
    api = {row["id"] for row in eva.get("/api/leads").json()}
    assert api == {leads["eva"], leads["nobody"]}


def test_a_new_account_is_made_on_the_dashboard_and_its_owner_sets_the_password(org):
    ctx, _ = org
    boss = client(ctx, "boss")
    page = boss.post("/admin/users", headers=ORIGIN, data={"username": "nina", "display_name": "Nina", "role": "bde"}).text
    path = re.search(r"(/setup\?token=[A-Za-z0-9_\-]+)", page).group(1)
    with ctx.db.session() as s:
        nina = s.execute(select(User).where(User.username == "nina")).scalar_one()
        assert nina.role == "bde" and nina.setup_token_hash and path.split("=")[1] not in (nina.setup_token_hash or "")
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "user_created")).first()
    anon = TestClient(create_app(ctx))
    assert _login(anon, username="nina").headers["location"].startswith("/login")  # no password until she sets one
    assert "Account: <b>nina</b>" in anon.get(path).text
    assert "differ" in anon.post("/setup", headers=ORIGIN, data={"token": path.split("=")[1], "password": GOOD,
                                                                  "confirm": GOOD + "x"}).text
    r = anon.post("/setup", headers=ORIGIN, data={"token": path.split("=")[1], "password": GOOD, "confirm": GOOD},
                  follow_redirects=False)
    assert r.status_code == 303 and "Password set" in r.headers["location"].replace("%20", " ")
    assert "not valid" in anon.get(path).text  # used once
    assert _login(anon, username="nina").headers["location"] == "/"
    assert "Business Development Executive" in anon.get("/").text


def test_nobody_can_give_more_than_they_hold_or_change_themselves(org):
    ctx, _ = org
    boss = client(ctx, "boss")  # admin: no configure_system
    assert "cannot give" in boss.post("/admin/users", headers=ORIGIN, data={"username": "x1", "role": "super_admin"},
                                      follow_redirects=False).headers["location"].replace("%20", " ")
    r = boss.post("/admin/roles", headers=ORIGIN, data={"name": "power", "label": "Power", "permissions": ["configure_system"]},
                  follow_redirects=False)
    assert "do not have" in r.headers["location"].replace("%20", " ")
    with ctx.db.session() as s:
        me = s.execute(select(User).where(User.username == "boss")).scalar_one().id
    r = boss.post(f"/admin/users/{me}/edit", headers=ORIGIN, data={"role": "admin"}, follow_redirects=False)
    assert "own account" in r.headers["location"].replace("%20", " ")
    assert client(ctx, "mia").get("/admin/users").status_code == 403  # a Sales Manager does not manage accounts
    assert client(ctx, "ana").get("/admin/roles").status_code == 403


def test_a_custom_role_grants_exactly_its_permissions(org):
    ctx, leads = org
    boss = client(ctx, "boss")
    boss.post("/admin/roles", headers=ORIGIN, data={"name": "inside_sales", "label": "Inside Sales",
                                                     "permissions": ["view_own", "edit_records"]})
    with ctx.db.session() as s:
        assert s.execute(select(CustomRole).where(CustomRole.name == "inside_sales")).scalar_one().label == "Inside Sales"
        uid = s.execute(select(User).where(User.username == "zoe")).scalar_one().id
    boss.post(f"/admin/users/{uid}/edit", headers=ORIGIN, data={"role": "inside_sales", "available": "yes", "capacity": "0"})
    zoe = client(ctx, "zoe")
    assert "Inside Sales" in zoe.get("/").text
    assert lead_ids_on_page(zoe.get("/leads").text) == {leads["zoe"]}  # no view_unassigned
    assert zoe.post(f"/leads/{leads['zoe']}/note", headers=ORIGIN, data={"note": "ok"}, follow_redirects=False).status_code == 303


def test_deactivating_signs_the_person_out_and_a_new_setup_link_resets_the_password(org):
    ctx, _ = org
    eva = client(ctx, "eva")
    boss = client(ctx, "boss")
    with ctx.db.session() as s:
        uid = s.execute(select(User).where(User.username == "eva")).scalar_one().id
    boss.post(f"/admin/users/{uid}/deactivate", headers=ORIGIN)
    assert eva.get("/leads", follow_redirects=False).status_code == 303  # signed out
    boss.post(f"/admin/users/{uid}/activate", headers=ORIGIN)
    page = boss.post(f"/admin/users/{uid}/reset", headers=ORIGIN).text
    assert "/setup?token=" in page and _login(TestClient(create_app(ctx)), username="eva").headers["location"] == "/"


def test_managers_reassign_within_their_team_with_history_and_executives_cannot(org):
    ctx, leads = org
    mia = client(ctx, "mia")
    r = mia.post(f"/leads/{leads['eva']}/assign", headers=ORIGIN, data={"owner": "ed", "reason": "eva on leave"},
                 follow_redirects=False)
    assert "from eva to ed" in r.headers["location"].replace("%20", " ")
    r = mia.post(f"/leads/{leads['nobody']}/assign", headers=ORIGIN, data={"owner": "zoe"}, follow_redirects=False)
    assert "not in your team" in r.headers["location"].replace("%20", " ")
    assert client(ctx, "eva").post(f"/leads/{leads['nobody']}/assign", headers=ORIGIN, data={"owner": "eva"},
                                   follow_redirects=False).status_code == 403
    with ctx.db.session() as s:
        assert s.get(Lead, leads["eva"]).owner == "ed" and s.get(Lead, leads["nobody"]).owner is None
        [a] = s.execute(select(Assignment)).scalars().all()
        assert (a.from_owner, a.to_owner, a.method, a.reason, a.actor) == ("eva", "ed", "manual", "eva on leave", "mia")
    assert "eva → ed (manual: eva on leave) by mia" in client(ctx, "boss").get(f"/leads/{leads['eva']}").text


def new_deal(s, name="Deal", service=None):
    co = s.execute(select(Company)).scalars().first()
    opp = Opportunity(company_id=co.id, name=name, stage="new", probability=10, service=service)
    s.add(opp)
    s.flush()
    return opp


def test_automatic_assignment_rules(org):
    ctx, _ = org
    ctx.settings.business.setdefault("handover", {})["sales_manager"] = ["eva", "ed", "zoe"]
    with ctx.db.session() as s:  # workload (default): the fewest open deals, ties in order
        assert [assign_new(s, ctx.settings, new_deal(s), "test") for _ in range(4)] == ["eva", "ed", "zoe", "eva"]
    ctx.settings.business["handover"]["strategy"] = "round_robin"
    with ctx.db.session() as s:
        assert [assign_new(s, ctx.settings, new_deal(s), "test") for _ in range(3)] == ["eva", "ed", "zoe"]
        assert assign_new(s, ctx.settings, new_deal(s), "test") == "eva"
    ctx.settings.business["handover"]["strategy"] = "skill"
    with ctx.db.session() as s:
        s.execute(select(User).where(User.username == "zoe")).scalar_one().skills = ["custom_crm"]
        assert assign_new(s, ctx.settings, new_deal(s, service="custom_crm"), "test") == "zoe"
        assert s.execute(select(Assignment).order_by(Assignment.id.desc())).scalars().first().method == "skill"
    ctx.settings.business["handover"]["strategy"] = "workload"
    with ctx.db.session() as s:  # unavailable and full people are skipped
        for u in s.execute(select(User).where(User.username.in_(("eva", "ed")))).scalars():
            u.available = u.username != "eva"
            u.capacity = 1 if u.username == "ed" else 0
        assert assign_new(s, ctx.settings, new_deal(s), "test") == "zoe"
        s.execute(select(User).where(User.username == "zoe")).scalar_one().available = False
        opp = new_deal(s, "Nobody")
        assert assign_new(s, ctx.settings, opp, "test") is None and opp.owner is None
        assert s.execute(select(Alert).where(Alert.message.like("Unassigned deal: Nobody%"))).first()


def test_without_a_configured_list_everyone_whose_role_receives_leads_shares_them(org):
    ctx, _ = org
    with ctx.db.session() as s:
        owners = {assign_new(s, ctx.settings, new_deal(s), "test") for _ in range(4)}
    assert owners == {"mia", "eva", "ed", "zoe"}  # not the admin or the analyst


def test_an_untouched_deal_is_escalated_to_the_owners_manager_once(org):
    ctx, _ = org
    ctx.settings.business.setdefault("handover", {}).update(sales_manager=["eva"], escalate_after_hours=24)
    with ctx.db.session() as s:
        opp = new_deal(s, "Quiet deal")
        assign_new(s, ctx.settings, opp, "test")
        s.flush()
        assert escalations(s, ctx.settings, utcnow() + timedelta(hours=2), "test") == 0
        assert escalations(s, ctx.settings, utcnow() + timedelta(hours=25), "test") == 1
        assert escalations(s, ctx.settings, utcnow() + timedelta(hours=50), "test") == 0  # once per assignment
        alert = s.execute(select(Alert).where(Alert.message.like("Escalated to mia%"))).scalar_one()
        assert "no action by eva in 24 h" in alert.message and opp.owner == "eva"  # told, not moved


def test_missing_indexes_are_added_to_an_existing_database(tmp_path):
    from aime.migrations import migrate

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    migrate(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_leads_owner"))
    assert "ix_leads_owner" not in {i["name"] for i in inspect(engine).get_indexes("leads")}
    assert migrate(engine) == ["CREATE INDEX ix_leads_owner"]
    assert "ix_leads_owner" in {i["name"] for i in inspect(engine).get_indexes("leads")}
    # expression indexes (lower(email)) are invisible to SQLAlchemy's inspector: a later start must not recreate them
    assert migrate(engine) == []


def test_the_administration_pages_render_for_an_administrator(org):
    ctx, leads = org
    boss = client(ctx, "boss")
    with ctx.db.session() as s:
        uid = s.execute(select(User).where(User.username == "eva")).scalar_one().id
    boss.post("/admin/roles", headers=ORIGIN, data={"name": "inside_sales", "label": "Inside Sales", "permissions": ["view_own"]})
    pages = {"/admin/users": "Add a user", f"/admin/users?edit={uid}": "Edit eva", "/admin/teams": "US East",
             "/admin/roles": "Inside Sales", f"/leads/{leads['eva']}": "Assign lead and its open deals"}
    for path, text_ in pages.items():
        r = boss.get(path)
        assert r.status_code == 200 and text_ in r.text, path
    assert "Users &amp; teams" in boss.get("/").text and "Users &amp; teams" not in client(ctx, "mia").get("/").text
    boss.post("/admin/teams", headers=ORIGIN, data={"name": "US West", "manager": "zoe"})
    with ctx.db.session() as s:
        assert s.execute(select(Team).where(Team.name == "US West")).scalar_one().manager == "zoe"


def test_approving_makes_the_approver_the_owner_only_if_they_work_leads(org):
    from aime.models import FollowUp
    from aime.reply_intel import reply_footer

    ctx, leads = org
    with ctx.db.session() as s:
        co = s.execute(select(Company)).scalars().first()
        extra = Lead(company_id=co.id, source="research", status="qualified")
        s.add(extra)
        s.flush()
        drafts = {}
        for name, lead_id in (("boss", leads["nobody"]), ("mia", extra.id)):
            f = FollowUp(lead_id=lead_id, company_id=co.id, channel="email", kind="outreach", step=1, due_at=utcnow(),
                         status="awaiting_approval", subject="Hello", body="Hi" + reply_footer(ctx.settings),
                         to_email=f"{name}-lead@acme.example")
            s.add(f)
            s.flush()
            drafts[name] = (f.id, lead_id)
    for name, (fid, _lead) in drafts.items():
        client(ctx, name).post(f"/followups/{fid}/approve", headers=ORIGIN, data={"qc_override": "yes"})
    with ctx.db.session() as s:
        assert s.get(FollowUp, drafts["boss"][0]).approved_by == "boss"
        assert s.get(Lead, drafts["boss"][1]).owner is None  # an administrator approving does not take the lead
        assert s.get(Lead, drafts["mia"][1]).owner == "mia"  # a Sales Manager approving does


def test_organisation_dashboards_are_for_roles_that_see_everything(org):
    ctx, leads = org
    eva = client(ctx, "eva")
    for path in ("/", "/manager", "/command-center", "/reports", "/agents", "/jobs", "/audit", "/system"):
        r = eva.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/my", path
    assert eva.get("/api/stats").status_code == 403
    page = eva.get("/my").text
    assert "My work" in page and 'href="/manager"' not in page and 'href="/reports"' not in page
    for user in ("boss", "ana"):  # an administrator and the read-only analyst see the organisation
        assert client(ctx, user).get("/", follow_redirects=False).status_code == 200


def test_companies_and_contacts_follow_the_leads_a_person_may_see(org):
    from aime.models import Contact

    ctx, leads = org
    with ctx.db.session() as s:
        other = Company(name="Zeta Corp", normalized_name="zeta corp", source="test")
        fresh = Company(name="Fresh Research Inc", normalized_name="fresh research inc", source="test")
        s.add_all([other, fresh])
        s.flush()
        s.add(Lead(company_id=other.id, source="inbound", status="qualified", owner="zoe"))
        s.add_all([Contact(company_id=other.id, email="cfo@zeta.example", first_name="Zed", source="website",
                           consent_status="none"),
                   Contact(company_id=fresh.id, email="ops@fresh.example", first_name="Fay", source="website",
                           consent_status="none")])
    eva, zoe = client(ctx, "eva"), client(ctx, "zoe")
    companies = eva.get("/companies").text
    assert "Acme" in companies and "Zeta Corp" not in companies and "Fresh Research Inc" in companies  # no lead yet
    contacts = eva.get("/contacts").text
    assert "zeta.example" not in contacts and "fresh.example" in contacts
    assert "Zeta Corp" in zoe.get("/companies").text and "zeta.example" in zoe.get("/contacts").text
    assert "Zeta" not in eva.get("/search?q=zeta").text.split("<main", 1)[-1]


def test_a_deal_goes_to_the_salesperson_already_working_its_lead(org):
    ctx, leads = org
    ctx.settings.business.setdefault("handover", {})["sales_manager"] = ["eva", "ed"]
    with ctx.db.session() as s:
        ed_deal = Opportunity(company_id=s.get(Lead, leads["eva"]).company_id, name="ed busy", stage="new", owner="ed")
        s.add(ed_deal)  # the workload rule alone would pick eva next; ed already works the lead below
        s.flush()
        s.get(Lead, leads["nobody"]).owner = "ed"
        opp = Opportunity(company_id=ed_deal.company_id, lead_id=leads["nobody"], name="Ed's customer", stage="new")
        s.add(opp)
        s.flush()
        assert assign_new(s, ctx.settings, opp, "test") == "ed"
        assert s.execute(select(Assignment).where(Assignment.opportunity_id == opp.id)).scalar_one().method == "lead_owner"
        s.execute(select(User).where(User.username == "ed")).scalar_one().available = False  # on leave: the rule decides
        other = Opportunity(company_id=ed_deal.company_id, lead_id=leads["nobody"], name="second deal", stage="new")
        s.add(other)
        s.flush()
        assert assign_new(s, ctx.settings, other, "test") == "eva"


def test_approving_an_inquiry_reply_makes_the_salesperson_its_owner_on_record(org):
    from aime.models import FollowUp
    from aime.reply_intel import reply_footer

    ctx, leads = org
    with ctx.db.session() as s:
        lead = s.get(Lead, leads["nobody"])
        f = FollowUp(lead_id=lead.id, company_id=lead.company_id, channel="email", kind=None, step=1, due_at=utcnow(),
                     status="awaiting_approval", subject="Re: your inquiry", body="Hi" + reply_footer(ctx.settings),
                     to_email="asker@acme.example")
        s.add(f)
        s.flush()
        fid = f.id
    client(ctx, "mia").post(f"/followups/{fid}/approve", headers=ORIGIN)
    with ctx.db.session() as s:
        assert s.get(Lead, leads["nobody"]).owner == "mia"
        a = s.execute(select(Assignment).where(Assignment.lead_id == leads["nobody"])).scalar_one()
        assert (a.method, a.to_owner, a.actor) == ("approval", "mia", "mia")
