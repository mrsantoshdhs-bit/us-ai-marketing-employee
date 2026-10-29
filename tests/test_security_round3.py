"""The last low findings: a crafted link cannot put words on a page (notes carry this app's signature), only approvers
reclassify a reply or apply a Calendly notice (either can make an email or a meeting happen), passwords are hashed
with today's scrypt work factor and older hashes are upgraded at sign-in, and an unknown user costs the same work as
a wrong password."""

from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.cli import cmd_user
from aime.models import InboundEmail, User
from aime.web.app import create_app
from aime.web.auth import DUMMY_HASH, SCRYPT_N, verify_password
from tests.test_dashboard_auth import GOOD, ORIGIN, Args, _login, auth_ctx  # noqa: F401  (pytest fixture)
from tests.test_roles import signed_in, team  # noqa: F401  (pytest fixture)


def test_a_crafted_link_cannot_put_words_on_a_page(make_ctx):
    c = TestClient(create_app(make_ctx()))
    fake = "Your account is suspended: call +1 555 0100 now"
    assert fake not in c.get(f"/leads?msg={quote(fake)}").text
    assert fake not in c.get(f"/login?error={quote(fake)}").text
    r = c.post("/suppress", headers=ORIGIN, data={"value": "%"}, follow_redirects=False)  # one of our own notes
    assert "&n=" in r.headers["location"]
    assert "one email address or one domain" in c.get(r.headers["location"]).text


def test_only_approvers_reclassify_replies_or_apply_calendly_notices(team):  # noqa: F811
    ctx, ids = team
    with ctx.db.session() as s:
        row = InboundEmail(from_email="x@acme.example", subject="Re", category="other", status="needs_review",
                           lead_id=ids["lead"], message_id="<r3@x>")
        s.add(row)
        s.flush()
        rid = row.id
    bde = signed_in(ctx, "bde")
    for action in ("reclassify", "apply_calendly"):
        r = bde.post(f"/inbox/{rid}/{action}", headers=ORIGIN, data={"category": "interested"}, follow_redirects=False)
        assert r.status_code == 403, action
    assert bde.post(f"/inbox/{rid}/reviewed", headers=ORIGIN, follow_redirects=False).status_code == 303  # still allowed
    manager = signed_in(ctx, "manager")
    assert manager.post(f"/inbox/{rid}/reclassify", headers=ORIGIN, data={"category": "other"},
                        follow_redirects=False).status_code == 303


def test_older_password_hashes_are_upgraded_at_sign_in(auth_ctx):  # noqa: F811
    import base64
    import hashlib
    import os

    salt = os.urandom(16)
    old = hashlib.scrypt(GOOD.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    old_hash = f"scrypt${2 ** 14}$8$1${base64.b64encode(salt).decode()}${base64.b64encode(old).decode()}"
    with auth_ctx.db.session() as s:
        s.execute(select(User)).scalar_one().password_hash = old_hash
    c = TestClient(create_app(auth_ctx))
    assert _login(c).headers["location"] == "/"  # the old hash still works
    with auth_ctx.db.session() as s:
        new = s.execute(select(User)).scalar_one().password_hash
    assert new.startswith(f"scrypt${SCRYPT_N}$") and verify_password(GOOD, new)
    assert cmd_user(Args("set-password", "manager"), auth_ctx, prompt_fn=lambda _p: GOOD + "!") == 0
    with auth_ctx.db.session() as s:
        assert s.execute(select(User)).scalar_one().password_hash.startswith(f"scrypt${SCRYPT_N}$")


def test_the_timing_dummy_is_a_real_hash():
    assert len(DUMMY_HASH.split("$")) == 6 and not verify_password("anything at all", DUMMY_HASH)
