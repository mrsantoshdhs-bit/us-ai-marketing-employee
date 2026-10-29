"""Roles as sets of permissions, enforced by the server on every request (aime.web.auth) and on every record a page
lists or changes (aime.visibility). Built-in roles cover the sales organisation; an administrator can add custom
roles (table `roles`) from Administration › Roles, choosing permissions from the same list. Nobody can grant a
permission they do not hold themselves."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

PERMISSIONS: dict[str, str] = {
    "view_all": "See every lead, deal, reply, meeting and email",
    "view_team": "See the records of their team and of the people who report to them",
    "view_own": "See the records assigned to them",
    "view_unassigned": "See leads nobody owns yet",
    "edit_records": "Work records: notes, statuses, meetings, replies, drafts, imports",
    "approve_outbound": "Approve, confirm or resend what goes out to customers (emails, posts) and run campaigns",
    "assign_leads": "Assign and reassign leads and deals (within what they can see)",
    "receive_leads": "Receive new leads and deals from automatic assignment",
    "manage_users": "Create and change user accounts, teams and custom roles",
    "admin_ops": "Run jobs, change the automation mode and authorisations, connect integrations",
    "configure_system": "Configure mailboxes, sales settings and LinkedIn Lead Sync",
}

# rank orders the built-in roles for display; permissions are what is enforced
BUILTIN_ROLES: dict[str, dict] = {
    "super_admin": {"label": "Super Admin", "rank": 100, "permissions": set(PERMISSIONS) - {"receive_leads"}},
    "admin": {"label": "Administrator", "rank": 90,
              "permissions": {"view_all", "view_unassigned", "edit_records", "approve_outbound", "assign_leads",
                              "manage_users", "admin_ops"}},
    "sales_director": {"label": "Sales Director", "rank": 80,
                       "permissions": {"view_all", "view_unassigned", "edit_records", "approve_outbound", "assign_leads",
                                       "manage_users", "receive_leads"}},
    "sales_manager": {"label": "Sales Manager", "rank": 70,
                      "permissions": {"view_team", "view_own", "view_unassigned", "edit_records", "approve_outbound",
                                      "assign_leads", "receive_leads"}},
    "team_leader": {"label": "Sales Team Leader", "rank": 60,
                    "permissions": {"view_team", "view_own", "view_unassigned", "edit_records", "approve_outbound",
                                    "assign_leads", "receive_leads"}},
    "sales_executive": {"label": "Sales Executive", "rank": 50,
                        "permissions": {"view_own", "view_unassigned", "edit_records", "receive_leads"}},
    "bde": {"label": "Business Development Executive", "rank": 50,
            "permissions": {"view_own", "view_unassigned", "edit_records", "receive_leads"}},
    "lead_gen_executive": {"label": "Lead Generation Executive", "rank": 40,
                           "permissions": {"view_own", "view_unassigned", "edit_records"}},
    "analyst": {"label": "Read-Only Analyst", "rank": 10, "permissions": {"view_all", "view_unassigned"}},
}
for _role in BUILTIN_ROLES.values():
    _role["permissions"] = frozenset(_role["permissions"])


def custom_roles(session: Session) -> dict[str, dict]:
    from aime.models import CustomRole

    return {r.name: {"label": r.label, "rank": 0, "permissions": frozenset(p for p in (r.permissions or []) if p in PERMISSIONS)}
            for r in session.execute(select(CustomRole).where(CustomRole.active.is_(True))).scalars()}


def all_roles(session: Session) -> dict[str, dict]:
    return {**custom_roles(session), **BUILTIN_ROLES}  # a custom role can never shadow a built-in one


def permissions_of(session: Session, role: str) -> frozenset[str]:
    if role in BUILTIN_ROLES:
        return BUILTIN_ROLES[role]["permissions"]
    return custom_roles(session).get(role, {}).get("permissions", frozenset())


def role_label(session: Session | None, role: str) -> str:
    if role in BUILTIN_ROLES:
        return BUILTIN_ROLES[role]["label"]
    if session is not None and role in (custom := custom_roles(session)):
        return custom[role]["label"]
    return role.replace("_", " ").title()


def effective(perms) -> frozenset[str]:
    """What a set of permissions covers: seeing everything includes a team's, one's own and unassigned records."""
    out = set(perms)
    if "view_all" in out:
        out |= {"view_team", "view_own", "view_unassigned"}
    if "view_team" in out:
        out.add("view_own")
    return frozenset(out)


def can_grant(granter: frozenset[str], role_permissions: frozenset[str]) -> bool:
    """A user may give a role only if it holds no permission they lack (no privilege escalation). Receiving leads
    is a routing choice, not a privilege."""
    return role_permissions - {"receive_leads"} <= effective(granter)
