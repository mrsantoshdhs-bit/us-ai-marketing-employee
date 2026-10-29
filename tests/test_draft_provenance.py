"""Every email draft says who wrote its text: a template (fixed wording filled with the lead's facts; no AI model
writes emails) or a person who edited it. A template is never shown as an AI's own reasoning."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.models import FollowUp
from aime.web.app import create_app
from tests.test_dashboard_auth import ORIGIN
from tests.test_pilot_test_mode import ANN, first_email, pilot


def test_drafts_are_labelled_template_until_a_person_edits_them(make_ctx):
    ctx, _ = pilot(make_ctx)
    with ctx.db.session() as s:
        assert {f.generator for f in s.execute(select(FollowUp).where(FollowUp.channel == "email")).scalars()} == {"template"}
    c = TestClient(create_app(ctx))
    assert "Template draft · no AI model" in c.get("/followups?show=all").text
    with ctx.db.session() as s:  # a fresh draft to edit
        f = first_email(ctx, ANN)
        draft = FollowUp(lead_id=f.lead_id, contact_id=f.contact_id, company_id=f.company_id, channel="email", kind="response",
                         step=1, due_at=f.due_at, status="awaiting_approval", subject="Re: hello", body=f.body, to_email=f.to_email)
        s.add(draft)
        s.flush()
        did = draft.id
    c.post(f"/followups/{did}/save", headers=ORIGIN, data={"subject": "Re: hello", "body": "Hi Ann, a personal note.\n" + f.body})
    with ctx.db.session() as s:
        assert s.get(FollowUp, did).generator == "template, edited by dashboard user"
    assert "Template · edited by dashboard user" in c.get("/followups").text
