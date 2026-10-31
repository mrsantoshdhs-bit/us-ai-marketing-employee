"""Sales performance from recorded data only, per salesperson and for the AI employee, over a date range.

Who a record counts for: a reply, a meeting or a sent email counts for the owner of its lead's deal, else the lead's
owner. A won deal counts once: for the person who moved it to won (else its owner), and by origin as AI-sourced
(opened by the agents) or human-sourced, so AI and human contributions are never added twice. Nothing is
estimated: a deal without a value adds nothing to revenue, and is named beside the report; a rate with nothing to divide
is shown as none.
The Total adds what the report shows: a report narrowed to some people leaves out everyone else and the unassigned
records. The report on everyone adds what nobody in it can be credited with to the unassigned row, so that revenue
stays in the Total when a salesperson leaves."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.models import Assignment, FollowUp, InboundEmail, Lead, Meeting, Opportunity, Quote, User
from aime.visibility import EVERYTHING, Scope, lead_condition, record_condition

POSITIVE = ("interested",)
NEGATIVE = ("not_interested", "unsubscribe")


@dataclass
class Row:
    username: str
    leads_owned: int = 0
    assigned: int = 0
    emails_sent: int = 0
    positive_replies: int = 0
    negative_replies: int = 0
    meetings: int = 0
    deals_opened: int = 0
    won: int = 0
    lost: int = 0
    won_value: float = 0.0
    won_value_ai_sourced: float = 0.0

    @property
    def conversion(self) -> float | None:
        closed = self.won + self.lost
        return self.won / closed if closed else None

    def has_numbers(self) -> bool:
        return any(value for name, value in vars(self).items() if name != "username")


@dataclass
class Report:
    rows: list[Row]
    total: Row
    ai: dict = field(default_factory=dict)
    unassigned: Row | None = None
    # won deals that add nothing to Won value: {"id", "name", "quote": "EUR 9,000" or None}
    won_without_value: list[dict] = field(default_factory=list)


def owners_by_lead(session: Session) -> dict[int, str | None]:
    lead_owner = dict(session.execute(select(Lead.id, Lead.owner)).all())
    for lead_id, owner in session.execute(select(Opportunity.lead_id, Opportunity.owner).where(
            Opportunity.lead_id.is_not(None), Opportunity.owner.is_not(None)).order_by(Opportunity.id)).all():
        lead_owner[lead_id] = owner  # the deal's owner works the lead now
    return lead_owner


def build(session: Session, start: datetime, end: datetime, people: list[str] | None = None, source: str | None = None,
          scope: Scope = EVERYTHING, include_unassigned: bool = True) -> Report:
    """People: the usernames to report on (None: every user). Source: only leads from this source (e.g. inbound).
    Scope: what the reader may see, for the AI employee's figures, which belong to no one person. Include_unassigned:
    whether records nobody owns count in the Total."""
    known = set(session.execute(select(User.username)).scalars())
    everyone = people is None and include_unassigned
    if people is None:
        people = sorted(known)
    rows = {p: Row(p) for p in people}
    other = Row("(unassigned)")
    owner_of = owners_by_lead(session)
    in_source = None
    if source:
        in_source = set(session.execute(select(Lead.id).where(Lead.source == source)).scalars())

    def row_for(lead_id, owner=None) -> Row | None:
        if in_source is not None and lead_id not in in_source:
            return None
        who = owner if owner is not None else owner_of.get(lead_id)
        if who is None:
            return other if include_unassigned else None
        return rows.get(who) or (other if everyone else None)  # someone outside the report: not counted here

    for lead_id, owner in session.execute(select(Lead.id, Lead.owner).where(
            Lead.owner.is_not(None), Lead.status.not_in(("merged", "disqualified")))).all():
        if (r := row_for(lead_id, owner)) is not None:
            r.leads_owned += 1
    for a in session.execute(select(Assignment).where(Assignment.created_at >= start, Assignment.created_at < end,
                                                      Assignment.method != "escalation",
                                                      Assignment.to_owner.is_not(None))).scalars():
        if a.to_owner in rows and (in_source is None or a.lead_id in in_source):
            rows[a.to_owner].assigned += 1
    for lead_id, in session.execute(select(FollowUp.lead_id).where(
            FollowUp.channel == "email", FollowUp.sent_at >= start, FollowUp.sent_at < end)).all():
        if (r := row_for(lead_id)) is not None:
            r.emails_sent += 1
    for lead_id, category in session.execute(select(InboundEmail.lead_id, InboundEmail.category).where(
            InboundEmail.received_at >= start, InboundEmail.received_at < end, InboundEmail.lead_id.is_not(None),
            InboundEmail.is_simulated.is_(False))).all():
        if (r := row_for(lead_id)) is not None:
            r.positive_replies += category in POSITIVE
            r.negative_replies += category in NEGATIVE
    for lead_id, in session.execute(select(Meeting.lead_id).where(
            Meeting.created_at >= start, Meeting.created_at < end, Meeting.lead_id.is_not(None),
            Meeting.is_simulated.is_(False), Meeting.status != "cancelled")).all():
        if (r := row_for(lead_id)) is not None:
            r.meetings += 1
    ai_opened = ai_won = 0
    no_value: list[dict] = []
    for o in session.execute(select(Opportunity).where(Opportunity.created_at >= start, Opportunity.created_at < end)).scalars():
        if (r := row_for(o.lead_id, o.owner)) is not None:
            r.deals_opened += 1
            ai_opened += o.origin == "ai"
    for o in session.execute(select(Opportunity).where(Opportunity.stage.in_(("won", "lost")),
                                                       Opportunity.closed_at >= start, Opportunity.closed_at < end)).scalars():
        # the person who won it, whoever reads the report; a win by no person (an agent, sign-in off): its owner
        r = row_for(o.lead_id, o.won_by if o.stage == "won" and o.won_by in known else o.owner)
        if r is None:
            continue
        if o.stage == "won":
            r.won += 1
            r.won_value += o.est_value or 0.0
            if not o.est_value:
                quote = session.execute(select(Quote).where(Quote.opportunity_id == o.id, Quote.status == "accepted")
                                        .order_by(Quote.id.desc()).limit(1)).scalars().first()
                no_value.append({"id": o.id, "name": o.name, "quote": f"{quote.currency} {quote.amount:,.0f}" if quote else None})
            if o.origin == "ai":
                r.won_value_ai_sourced += o.est_value or 0.0
                ai_won += 1
        else:
            r.lost += 1
    shown = list(rows.values())
    total = Row("Total")
    for r in [*shown, other] if include_unassigned else shown:
        for name in ("leads_owned", "assigned", "emails_sent", "positive_replies", "negative_replies", "meetings",
                     "deals_opened", "won", "lost", "won_value", "won_value_ai_sourced"):
            setattr(total, name, getattr(total, name) + getattr(r, name))
    drafts = Counter(session.execute(select(FollowUp.kind).where(
        FollowUp.channel == "email", FollowUp.created_at >= start, FollowUp.created_at < end,
        record_condition(scope, FollowUp.lead_id))).scalars())
    classified = session.execute(select(InboundEmail.id).where(
        InboundEmail.processed_at >= start, InboundEmail.processed_at < end,
        record_condition(scope, InboundEmail.lead_id))).all()
    ai = {"drafts_written": sum(drafts.values()), "drafts_by_kind": dict(drafts), "replies_classified": len(classified),
          "deals_opened": ai_opened, "deals_won_ai_sourced": ai_won,
          "won_value_ai_sourced": total.won_value_ai_sourced,
          "won_value_human_sourced": total.won_value - total.won_value_ai_sourced}
    return Report(rows=shown, total=total, ai=ai, unassigned=other, won_without_value=no_value)


def team_members(session: Session) -> dict[int, list[str]]:
    out = defaultdict(list)
    for team_id, username in session.execute(select(User.team_id, User.username).where(User.team_id.is_not(None))).all():
        out[team_id].append(username)
    return dict(out)


def campaign_stats(session: Session, scope: Scope = EVERYTHING) -> dict[int, dict]:
    """Per campaign, from the leads it brought (leads.campaign_id) that the reader may see: leads, emails sent, positive
    replies, meetings, deals opened, won, won value. Recorded data only."""
    from aime.models import Campaign

    out: dict[int, dict] = {c: {"leads": 0, "emails_sent": 0, "positive_replies": 0, "meetings": 0, "deals": 0,
                                 "won": 0, "won_value": 0.0}
                            for c in session.execute(select(Campaign.id)).scalars()}
    lead_campaign = dict(session.execute(select(Lead.id, Lead.campaign_id).where(Lead.campaign_id.is_not(None),
                                                                                 lead_condition(scope))).all())
    for cid in lead_campaign.values():
        if cid in out:
            out[cid]["leads"] += 1

    def add(lead_id, key, amount=1):
        cid = lead_campaign.get(lead_id)
        if cid in out:
            out[cid][key] += amount

    for (lead_id,) in session.execute(select(FollowUp.lead_id).where(FollowUp.channel == "email",
                                                                     FollowUp.sent_at.is_not(None))).all():
        add(lead_id, "emails_sent")
    for (lead_id,) in session.execute(select(InboundEmail.lead_id).where(InboundEmail.category.in_(POSITIVE),
                                                                         InboundEmail.is_simulated.is_(False))).all():
        add(lead_id, "positive_replies")
    for (lead_id,) in session.execute(select(Meeting.lead_id).where(Meeting.is_simulated.is_(False),
                                                                    Meeting.status != "cancelled")).all():
        add(lead_id, "meetings")
    for o in session.execute(select(Opportunity).where(Opportunity.lead_id.is_not(None))).scalars():
        add(o.lead_id, "deals")
        if o.stage == "won":
            add(o.lead_id, "won")
            add(o.lead_id, "won_value", o.est_value or 0.0)
    return out
