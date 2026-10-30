"""Customer handover: winning a deal converts the lead and gives the deal's owner one onboarding task with what was
sold, the agreed price and the customer contact. Nothing is sent to the customer."""

from sqlalchemy import select

from aime.models import Activity, Contact, FollowUp, Lead
from tests.test_dashboard_auth import ORIGIN
from tests.test_deals import deal
from tests.test_sales_team import client, org  # noqa: F401  (pytest fixture)


def handover_tasks(ctx):
    with ctx.db.session() as s:
        return s.execute(select(FollowUp).where(FollowUp.channel == "task",
                                                FollowUp.subject.like("Customer handover:%"))).scalars().all()


def test_winning_a_deal_hands_the_customer_over_once(org):  # noqa: F811
    ctx, leads = org
    with ctx.db.session() as s:
        lead = s.get(Lead, leads["eva"])
        c = Contact(company_id=lead.company_id, first_name="Dana", last_name="Lee", email="dana@acme.example",
                    source="inbound")
        s.add(c)
        s.flush()
        lead.contact_id = c.id
    oid = deal(ctx, leads["eva"], stage="negotiation")
    eva = client(ctx, "eva")
    eva.post(f"/opportunities/{oid}/quotes", headers=ORIGIN, data={"amount": "18000", "terms": "50% upfront"})
    eva.post(f"/opportunities/{oid}/quotes/1/accept", headers=ORIGIN)
    eva.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "won"})
    [task] = handover_tasks(ctx)
    assert (task.status, task.lead_id, task.subject) == ("pending", leads["eva"], "Customer handover: Acme CRM")
    assert "Won by eva" in task.body and "Deal owner: eva" in task.body
    assert "USD 18,000.00 (quote #1, accepted); terms: 50% upfront" in task.body
    assert "Dana Lee <dana@acme.example>" in task.body and "kickoff call" in task.body
    assert "Nothing has been sent to the customer" in task.body
    with ctx.db.session() as s:
        assert s.get(Lead, leads["eva"]).status == "converted"
        assert s.execute(select(Activity).where(Activity.subject == "Deal won: customer handover task for eva")).scalar_one()
        assert s.execute(select(FollowUp).where(FollowUp.channel == "email")).first() is None  # nothing to the customer
    assert "Customer handover: Acme CRM" in eva.get("/my").text
    assert "Customer handover" not in client(ctx, "zoe").get("/my").text  # not her customer

    eva.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "negotiation"})  # reopened, then won again
    eva.post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "won"})
    assert len(handover_tasks(ctx)) == 1


def test_a_win_without_a_price_asks_for_one(org):  # noqa: F811
    ctx, leads = org
    oid = deal(ctx, leads["eva"])
    client(ctx, "eva").post(f"/opportunities/{oid}", headers=ORIGIN, data={"stage": "won"})
    [task] = handover_tasks(ctx)
    assert "no price on record" in task.body and "no contact on record" in task.body
