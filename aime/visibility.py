"""Which records a signed-in user may see and work: everything (view_all), their team's and their reports'
(view_team), their own (view_own), plus leads nobody owns yet (view_unassigned). A lead belongs to whoever owns it
or one of its deals. Enforced on lists (a SQL condition) and on single records (a check before showing or changing).
With dashboard sign-in off (a local preview or the tests) there is no user and everything is visible."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import false, or_, select, true
from sqlalchemy.orm import Session

from aime.models import Lead, Opportunity, User


@dataclass(frozen=True)
class Scope:
    all: bool = True
    usernames: frozenset[str] = field(default_factory=frozenset)
    unassigned: bool = True


EVERYTHING = Scope()


def team_of(session: Session, username: str) -> set[str]:
    """The user, everyone in a team they manage or belong to, and everyone who reports to them (at any depth)."""
    me = session.execute(select(User).where(User.username == username)).scalars().first()
    names = {username}
    if me is None:
        return names
    from aime.models import Team

    team_ids = set(session.execute(select(Team.id).where(Team.manager == username)).scalars())
    if me.team_id:
        team_ids.add(me.team_id)
    if team_ids:
        names |= set(session.execute(select(User.username).where(User.team_id.in_(team_ids))).scalars())
    frontier = {username}
    while frontier:  # people who report to them, and to those, ...
        below = set(session.execute(select(User.username).where(User.manager.in_(frontier))).scalars()) - names
        names |= below
        frontier = below
    return names


def scope_for(session: Session, user: dict | None) -> Scope:
    if not user:
        return EVERYTHING
    perms = set(user.get("perms") or ())
    if "view_all" in perms:
        return EVERYTHING
    names = team_of(session, user["username"]) if "view_team" in perms else (
        {user["username"]} if "view_own" in perms else set())
    return Scope(all=False, usernames=frozenset(names), unassigned="view_unassigned" in perms)


def lead_condition(scope: Scope):
    """A WHERE condition on Lead for the leads this scope may see."""
    if scope.all:
        return true()
    parts = []
    if scope.usernames:
        parts += [Lead.owner.in_(scope.usernames),
                  Lead.id.in_(select(Opportunity.lead_id).where(Opportunity.owner.in_(scope.usernames)))]
    if scope.unassigned:
        parts.append(Lead.owner.is_(None) & ~Lead.id.in_(
            select(Opportunity.lead_id).where(Opportunity.owner.is_not(None), Opportunity.lead_id.is_not(None))))
    return or_(*parts) if parts else false()


def lead_ids(scope: Scope):
    """A subquery of visible lead ids, for records that carry a lead_id (emails, replies, meetings, deals)."""
    return select(Lead.id).where(lead_condition(scope))


def record_condition(scope: Scope, column, allow_unlinked: bool | None = None):
    """For a table with a lead_id column: visible when its lead is visible; records without a lead count as
    unassigned (a reply nobody has matched yet)."""
    if scope.all:
        return true()
    unlinked = scope.unassigned if allow_unlinked is None else allow_unlinked
    return or_(column.in_(lead_ids(scope)), column.is_(None)) if unlinked else column.in_(lead_ids(scope))


def company_condition(scope: Scope):
    """Companies with a lead this scope sees, plus companies without any lead (research not yet a lead) for those
    who see unassigned work."""
    from aime.models import Company

    if scope.all:
        return true()
    parts = [Company.id.in_(select(Lead.company_id).where(lead_condition(scope)))]
    if scope.unassigned:
        parts.append(~Company.id.in_(select(Lead.company_id)))
    return or_(*parts)


def contact_condition(scope: Scope):
    from aime.models import Company, Contact

    if scope.all:
        return true()
    return Contact.company_id.in_(select(Company.id).where(company_condition(scope)))


# Pages that show the whole organisation's numbers and work: only for roles that see everything (view_all). Others
# get "My work" (/my), built from what they may see.
ORG_EXACT = ("/", "/manager", "/audit", "/system", "/automation", "/api/stats")
ORG_PREFIXES = ("/command-center", "/reports", "/agents", "/jobs/", "/jobs")


def org_page(path: str) -> bool:
    return path in ORG_EXACT or any(path == p or path.startswith(p.rstrip("/") + "/") for p in ORG_PREFIXES)


def opportunity_condition(scope: Scope):
    if scope.all:
        return true()
    parts = [Opportunity.lead_id.in_(lead_ids(scope))]
    if scope.usernames:
        parts.append(Opportunity.owner.in_(scope.usernames))
    return or_(*parts)


def record_visible(session: Session, scope: Scope, kind: str, rid: int) -> bool:
    """Whether one record named in a URL (/leads/7, /followups/12/approve, ...) is within the scope. A record that
    does not exist counts as visible: the page itself answers 404."""
    from aime.models import FollowUp, InboundEmail, Meeting, MeetingProposal

    if scope.all:
        return True
    if kind == "leads":
        return session.get(Lead, rid) is None or can_see_lead(session, scope, rid)
    if kind == "opportunities":
        opp = session.get(Opportunity, rid)
        if opp is None:
            return True
        if opp.owner in scope.usernames:
            return True
        return can_see_lead(session, scope, opp.lead_id) if opp.lead_id else (opp.owner is None and scope.unassigned)
    model = {"followups": FollowUp, "inbox": InboundEmail, "meetings": Meeting, "proposals": MeetingProposal,
             "requests": InboundEmail}.get(kind)
    row = session.get(model, rid) if model else None
    return row is None or can_see_lead(session, scope, row.lead_id)


def can_see_lead(session: Session, scope: Scope, lead_id: int | None) -> bool:
    if scope.all:
        return True
    if lead_id is None:
        return scope.unassigned
    return session.execute(select(Lead.id).where(Lead.id == lead_id, lead_condition(scope))).first() is not None
