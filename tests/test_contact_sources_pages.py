"""The Contact sources pages (Option B, Phase 4): the adapter list with status, licence, lawful basis, cost and usage, and the
lead funnel, for the Super Admin only; the owner's approval of a paid source recorded and withdrawn; the settings section
that switches a register on. Plain forms, no script."""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.contact_sources import budget, registry
from aime.contact_sources.base import NEEDS_CREDENTIALS
from aime.contact_sources.paid import PaidEmailVerifier
from aime.models import User
from aime.web.app import create_app
from aime.web.auth import COOKIE, hash_password, make_cookie, session_secret



@pytest.fixture
def web(make_ctx):
    ctx = make_ctx(dashboard_auth=True, network_enabled=False)
    pw = "contact-sources " + "test passphrase"  # synthetic
    with ctx.db.session() as s:
        for name, role in (("boss", "super_admin"), ("ops", "admin"), ("mgr", "sales_manager")):
            s.add(User(username=name, password_hash=hash_password(pw), role=role))
    app = create_app(ctx)

    def client(username):
        c = TestClient(app, raise_server_exceptions=True, follow_redirects=False, headers={"origin": "http://testserver"})
        with ctx.db.session() as s:
            user = s.execute(select(User).where(User.username == username)).scalar_one()
            c.cookies.set(COOKIE, make_cookie(session_secret(ctx.settings.data_dir), user))
        return c
    return ctx, client


def test_the_contact_sources_page_is_super_admin_only_and_lists_every_adapter(web):
    ctx, client = web
    boss = client("boss")
    page = boss.get("/admin/settings/contact-sources")
    assert page.status_code == 200 and "<script" not in page.text
    for name, cls in registry.ADAPTER_CLASSES.items():
        assert name in page.text and cls.title.replace("'", "&#39;") in page.text
    for words in ("blocked_needs_credentials", "blocked_needs_owner_approval", "legitimate_interest_b2b", "never guessed",
                  "Outreach-ready lead", "to be confirmed by owner"):
        assert words in page.text, words
    for who in ("ops", "mgr"):
        assert client(who).get("/admin/settings/contact-sources").status_code in (403, 303)
        assert client(who).post("/admin/settings/contact-sources/approval", data={
            "settings_form": "1", "name": "paid_contact_provider", "approved_by": "Mallory", "confirm": "yes"}).status_code in (403, 303)
    with ctx.db.session() as s:
        assert budget.approval(s, "paid_contact_provider") is None


def test_the_owner_records_and_withdraws_an_approval_on_the_page(web):
    ctx, client = web
    boss = client("boss")
    form = {"settings_form": "1", "name": "paid_email_verifier", "approved_by": "Owner", "note": "quote 2026-11"}
    r = boss.post("/admin/settings/contact-sources/approval", data=form)
    assert "tick the box" in unquote(r.headers["location"])
    r = boss.post("/admin/settings/contact-sources/approval", data={**form, "confirm": "yes"})
    assert "recorded" in unquote(r.headers["location"])
    with ctx.db.session() as s:
        assert budget.approval(s, "paid_email_verifier")["approved_by"] == "Owner"
        assert PaidEmailVerifier(ctx).status(s).status == NEEDS_CREDENTIALS  # approval alone runs nothing: its key comes next
    assert "Approved by Owner" in boss.get("/admin/settings/contact-sources").text
    boss.post("/admin/settings/contact-sources/approval", data={**form, "do": "withdraw"})
    with ctx.db.session() as s:
        assert budget.approval(s, "paid_email_verifier") is None


def test_the_settings_section_page_switches_a_register_on(web):
    ctx, client = web
    boss = client("boss")
    page = boss.get("/admin/settings/section/contact_sources")
    assert page.status_code == 200 and "UK Companies House" in page.text and "<script" not in page.text
    form = {"settings_form": "1", "do": "save", "companies_house.enabled": "yes", "companies_house.priority": "5",
            "companies_house.countries": "GB", "paid_contact_provider.priority": "20", "paid_email_verifier.priority": "30"}
    r = boss.post("/admin/settings/section/contact_sources", data=form)
    assert "Saved as version" in unquote(r.headers["location"])
    assert registry.adapter_setting(ctx.settings, "companies_house")["enabled"] is True
    bad = {**form, "paid_contact_provider.monthly_budget_usd": "-5"}
    assert "Not saved" in unquote(boss.post("/admin/settings/section/contact_sources", data=bad).headers["location"])
