"""Every move of a deal is on record (from, to, who, why), a win is credited to whoever won it, expected close dates
are kept, and the sales performance report counts only recorded work: each won deal once, for the person who won
it, and as AI-sourced or human-sourced by who opened it."""

import re
from datetime import date, timedelta

from sqlalchemy import select

from aime.models import Company, InboundEmail, Lead, Meeting, Opportunity, StageChange, Team
from aime.performance import build
from aime.timeutil import utcnow
from tests.test_dashboard_auth import ORIGIN
from tests.test_opportunity_close import answer, opp_of
from tests.test_pilot_test_mode import ANN, pilot
from tests.test_sales_team import client, org  # noqa: F401  (pytest fixture)


def history(ctx, opp_id):
    with ctx.db.session() as s:
        return [(c.from_stage, c.to_stage, c.actor, c.reason) for c in s.execute(
            select(StageChange).where(StageChange.opportunity_id == opp_id).order_by(StageChange.id)).scalars()]


def test_the_stage_history_records_opening_a_no_and_a_reopening(make_ctx):
    ctx, _ = pilot(make_ctx)
    oid = opp_of(ctx, ANN).id
    assert history(ctx, oid) == [(None, "new", "sales_workflow", "a qualified lead who contacted us")]
    answer(ctx, 1, ANN, "No thanks, we're all set.")
    answer(ctx, 2, ANN, "Actually we changed our minds. Can we talk Thursday?")
    steps = history(ctx, oid)
    assert [(a, b) for a, b, _, _ in steps] == [(None, "new"), ("new", "lost"), ("lost", "discovery")]
    assert steps[1][3] == "Not interested" and "reopened by an interested reply" in steps[2][3]
    assert opp_of(ctx, ANN).origin == "ai"


def test_winning_on_the_pipeline_page_credits_the_person_and_keeps_the_close_date(org):  # noqa: F811
    ctx, leads = org
    with ctx.db.session() as s:
        co = s.execute(select(Company)).scalars().first()
        o = Opportunity(company_id=co.id, lead_id=leads["eva"], name="Deal", stage="proposal", owner="eva")
        s.add(o)
        s.flush()
        oid = o.id
    eva = client(ctx, "eva")
    r = eva.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "negotiation", "expected_close": "not a date"},
                 follow_redirects=False)
    assert "not a date" in r.headers["location"].replace("%20", " ")
    eva.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "negotiation", "expected_close": "2026-12-15"})
    assert "Close by 2026-12-15" in eva.get("/pipeline").text
    eva.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "won", "est_value": "12,000"})
    with ctx.db.session() as s:
        o = s.get(Opportunity, oid)
        assert (o.stage, o.won_by, o.est_value, o.probability, o.expected_close) == ("won", "eva", 12000, 100, date(2026, 12, 15))
        assert o.closed_at and s.get(Lead, leads["eva"]).status == "converted"
    assert history(ctx, oid)[-1][:3] == ("negotiation", "won", "eva")
    eva.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "negotiation"})  # reopened: no credit, no close date
    with ctx.db.session() as s:
        o = s.get(Opportunity, oid)
        assert (o.won_by, o.closed_at) == (None, None)


def test_the_performance_report_counts_recorded_work_once(org):  # noqa: F811
    ctx, leads = org
    now = utcnow()
    with ctx.db.session() as s:
        co = s.execute(select(Company)).scalars().first()
        deals = [("eva", "won", 10000, "ai", "eva"), ("eva", "won", 5000, "human", "mia"), ("ed", "lost", None, "ai", None),
                 ("ed", "won", None, "ai", "ed"), ("zoe", "new", 7000, "ai", None)]
        for owner, stage, value, origin, won_by in deals:
            s.add(Opportunity(company_id=co.id, lead_id=leads[owner], name=f"{owner} {stage}", stage=stage, owner=owner,
                              est_value=value, origin=origin, won_by=won_by,
                              closed_at=now if stage in ("won", "lost") else None))
        s.add_all([InboundEmail(from_email="x@acme.example", subject="Re", lead_id=leads["eva"], category="interested",
                                received_at=now, message_id="<a@x>"),
                   InboundEmail(from_email="y@acme.example", subject="Re", lead_id=leads["ed"], category="not_interested",
                                received_at=now, message_id="<b@x>"),
                   Meeting(lead_id=leads["eva"], company_id=co.id, invitee_email="x@acme.example", title="Intro", source="manual",
                           starts_at=now + timedelta(days=2), ends_at=now + timedelta(days=2, minutes=30))])
    with ctx.db.session() as s:
        rep = build(s, now - timedelta(days=1), now + timedelta(days=1), ["eva", "ed", "mia", "zoe"])
        rows = {r.username: r for r in rep.rows}
    # the deal mia won on eva's lead counts for mia; eva keeps her own win; ed's win has no value: no revenue invented
    assert (rows["eva"].won, rows["eva"].won_value, rows["eva"].won_value_ai_sourced) == (1, 10000, 10000)
    assert (rows["mia"].won, rows["mia"].won_value, rows["mia"].won_value_ai_sourced) == (1, 5000, 0)
    assert (rows["ed"].won, rows["ed"].lost, rows["ed"].won_value, rows["ed"].conversion) == (1, 1, 0, 0.5)
    assert rows["zoe"].conversion is None and rows["zoe"].deals_opened == 1  # nothing closed: no rate
    assert (rows["eva"].positive_replies, rows["ed"].negative_replies, rows["eva"].meetings) == (1, 1, 1)
    assert rep.total.won == 3 and rep.total.won_value == 15000
    assert rep.ai["won_value_ai_sourced"] + rep.ai["won_value_human_sourced"] == rep.total.won_value  # never twice
    names = lambda html: set(re.findall(r'href="/performance\?owner=([a-z]+)', html))  # noqa: E731
    assert names(client(ctx, "eva").get("/performance").text) == {"eva"}  # her own row only
    assert names(client(ctx, "mia").get("/performance").text) == {"mia", "eva", "ed"}  # her team
    boss = client(ctx, "boss")
    assert {"eva", "ed", "mia", "zoe", "ana", "boss"} <= names(boss.get("/performance").text)
    with ctx.db.session() as s:
        team_id = s.execute(select(Team.id).where(Team.name == "US East")).scalar_one()
    assert names(boss.get(f"/performance?team={team_id}").text) == {"eva", "ed"}  # filtered to one team
