"""The AI quality page (/agents/ai-quality): decisions per agent, the verdict mix, escalations, blocks, latency, model
health and agreement with people, for the roles that see the whole organisation; server-rendered, no script."""

from __future__ import annotations

from fastapi.testclient import TestClient

from aime.ai import decisions
from aime.cli import cmd_user
from aime.web.app import create_app
from tests.test_dashboard_auth import GOOD, Args, _login


def test_the_ai_quality_page_shows_decisions(ctx):
    with ctx.db.session() as s:
        for verdict, esc in (("block", ["injection_suspected"]), ("human_review", ["upset"]), ("auto_internal", [])):
            decisions.record(s, agent="conversation_shadow", task="reply_analysis", verdict=verdict, provider="ollama",
                             model="llama3.2:latest", proposal={"kind": "reply"}, kind="reply", reasons=[f"why {verdict}"],
                             escalations=esc, structured_ok=verdict != "block", tokens=100, ms=1200, lead_id=7)
        decisions.record_human_action(s, 3, "accepted", "manager")
    page = TestClient(create_app(ctx)).get("/agents/ai-quality")
    assert page.status_code == 200
    for words in ("AI quality", "conversation_shadow", "injection suspected", "why block", "ollama:llama3.2:latest", "67%",
                  "agreement 100%", "reply_analysis", "every paid provider is refused"):
        assert words in page.text, words
    assert "<script" not in page.text.split("</head>", 1)[1]


def test_the_ai_quality_page_is_for_roles_that_see_the_organisation(make_ctx):
    ctx = make_ctx(dashboard_auth=True)
    for name, role in (("boss", "super_admin"), ("ana", "analyst"), ("bde", "sales_executive")):
        assert cmd_user(Args("create", name, role=role), ctx, prompt_fn=lambda _p: GOOD) == 0
    for name, ok in (("boss", True), ("ana", True), ("bde", False)):
        client = TestClient(create_app(ctx))
        assert _login(client, username=name).status_code == 303
        r = client.get("/agents/ai-quality", follow_redirects=False)
        assert (r.status_code == 200) is ok, name
