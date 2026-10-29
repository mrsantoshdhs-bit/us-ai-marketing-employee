"""Operations: campaign results from recorded data, the agent-run log that rotates instead of growing forever, and
retention for manual backup copies that is off unless the owner sets it and never touches recent or automatic copies."""

import os
import time
from datetime import date

from fastapi.testclient import TestClient
from aime.backup import prune_manual_backups
from aime.logging_setup import append_jsonl, rotate
from aime.models import Campaign, Company, Lead, Opportunity
from aime.performance import campaign_stats
from aime.web.app import create_app


def test_campaign_results_come_from_the_leads_it_brought(make_ctx):
    ctx = make_ctx()
    with ctx.db.session() as s:
        camp = Campaign(name="Q4 CRM push", objective="leads", start_date=date(2026, 10, 1), end_date=date(2026, 12, 31),
                        created_by="test")
        co = Company(name="Acme", normalized_name="acme", source="test")
        s.add_all([camp, co])
        s.flush()
        for i, (stage, value) in enumerate((("won", 9000), ("lost", None))):
            lead = Lead(company_id=co.id, source="campaign", status="qualified", campaign_id=camp.id)
            s.add(lead)
            s.flush()
            s.add(Opportunity(company_id=co.id, lead_id=lead.id, name=f"d{i}", stage=stage, est_value=value))
        s.add(Lead(company_id=co.id, source="research", status="new"))  # not from the campaign
        cid = camp.id
    with ctx.db.session() as s:
        r = campaign_stats(s)[cid]
    assert (r["leads"], r["deals"], r["won"], r["won_value"]) == (2, 2, 1, 9000)
    assert "2 leads" in TestClient(create_app(ctx)).get("/campaigns").text


def test_the_agent_run_log_rotates(tmp_path):
    log = tmp_path / "agent_runs.jsonl"
    log.write_text("x" * 50)
    assert not rotate(log, max_bytes=100) and log.exists()
    log.write_text("x" * 150)
    assert rotate(log, max_bytes=100, keep=2) and not log.exists() and (tmp_path / "agent_runs.jsonl.1").exists()
    append_jsonl(log, {"a": 1})
    assert log.read_text().strip() == '{"a": 1}'


def test_manual_backup_retention_is_opt_in_and_spares_recent_and_automatic_copies(tmp_path):
    old = time.time() - 60 * 86400
    names = [f"aime-predeploy-{i}-2026.db" for i in range(4)] + ["aime-auto-1.db", "aime-recent-label.db"]
    for i, name in enumerate(names):
        p = tmp_path / name
        p.write_bytes(b"db")
        if "recent" not in name:
            os.utime(p, (old + i, old + i))
    (tmp_path / "aime-predeploy-0-2026.db-wal").write_bytes(b"")
    assert prune_manual_backups(tmp_path, keep=0) == []  # the default: nothing removed
    removed = prune_manual_backups(tmp_path, keep=2)
    assert sorted(removed) == ["aime-predeploy-0-2026.db", "aime-predeploy-0-2026.db-wal", "aime-predeploy-1-2026.db",
                               "aime-predeploy-2-2026.db"]  # the newest two manual copies stay
    left = {p.name for p in tmp_path.iterdir()}
    assert {"aime-auto-1.db", "aime-recent-label.db", "aime-predeploy-3-2026.db"} <= left  # automatic and recent stay


def test_a_campaign_audience_finds_its_leads_and_credits_only_unattributed_ones(make_ctx):
    from aime.audiences import parse

    ctx = make_ctx()
    with ctx.db.session() as s:
        camp, other = (Campaign(name=n, objective="leads", start_date=date(2026, 10, 1), end_date=date(2026, 12, 31),
                                created_by="test") for n in ("Texas realty", "Older campaign"))
        tx = Company(name="TX Realty", normalized_name="tx realty", source="test", industry="real_estate", state="TX")
        ca = Company(name="CA Realty", normalized_name="ca realty", source="test", industry="real_estate", state="CA")
        s.add_all([camp, other, tx, ca])
        s.flush()
        leads = [Lead(company_id=tx.id, source="research", status="qualified", grade="A", score=80),
                 Lead(company_id=tx.id, source="research", status="qualified", grade="C", score=40),
                 Lead(company_id=ca.id, source="research", status="qualified", grade="A", score=85),
                 Lead(company_id=tx.id, source="research", status="qualified", grade="A", score=90, campaign_id=other.id)]
        s.add_all(leads)
        s.flush()
        cid, ids = camp.id, [lead.id for lead in leads]
    assert parse({"states": "tx, zz9", "grades": "a,x", "industries": "real_estate, nonsense", "min_score": "70"}) == {
        "industries": ["real_estate"], "states": ["TX"], "grades": ["A"], "services": [], "sources": [], "min_score": 70}
    c = TestClient(create_app(ctx))
    form = {"industries": "real_estate", "states": "TX", "grades": "A", "min_score": "50"}
    r = c.post(f"/campaigns/{cid}/audience", headers={"origin": "http://testserver"}, data=form, follow_redirects=False)
    assert "2 matching leads" in r.headers["location"].replace("%20", " ")  # the A-grade Texas leads
    c.post(f"/campaigns/{cid}/audience", headers={"origin": "http://testserver"}, data={**form, "attach": "yes"})
    with ctx.db.session() as s:
        got = [s.get(Lead, i).campaign_id for i in ids]
    assert got[0] == cid and got[1] is None and got[2] is None  # only the match without a campaign
    assert got[3] != cid  # already credited to another campaign: left as it is
    assert "Audience: 2 leads" in c.get("/campaigns").text
