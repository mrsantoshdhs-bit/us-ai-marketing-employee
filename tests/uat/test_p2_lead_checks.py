"""Phase 2: small fixes made while merging the fix teams."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.models import SkippedMail, User
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
