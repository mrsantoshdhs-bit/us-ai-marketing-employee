"""The sales organisation on the dashboard: user accounts, teams, custom roles, one-time account setup links, and
assigning leads and deals. The sign-in middleware lets only `manage_users` reach /admin/users, /admin/teams and
/admin/roles and only `assign_leads` assign; the rules here stop privilege escalation: nobody can give a role with a
permission they lack, change their own role or account, or change an account that holds more than they do."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from aime.automation import audit
from aime.db import take_write_lock
from aime.knowledge.services import SERVICES
from aime.models import Assignment, CustomRole, Lead, Opportunity, Team, User
from aime.permissions import BUILTIN_ROLES, PERMISSIONS, all_roles, can_grant, permissions_of
from aime.timeutil import as_aware_utc, to_naive_utc, utcnow
from aime.web.auth import form_id

SETUP_HOURS = 48
UNUSABLE_HASH = "setup-pending"  # matches no password: the account works once its owner sets one
USERNAME_RE = re.compile(r"[a-z][a-z0-9._-]{1,59}")
ROLE_NAME_RE = re.compile(r"[a-z][a-z0-9_]{2,29}")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_setup_link(user: User) -> str:
    """A new one-time link for the user to choose their password; any earlier link and every session end."""
    token = secrets.token_urlsafe(32)
    user.setup_token_hash, user.setup_expires_at = token_hash(token), utcnow() + timedelta(hours=SETUP_HOURS)
    user.session_version = (user.session_version or 1) + 1
    return token


def register(app, C, render, back, who) -> None:
    def me(request: Request) -> dict:
        return getattr(request.state, "user", None) or {"username": "dashboard user", "perms": frozenset(PERMISSIONS)}

    def grantable(s, request) -> dict[str, dict]:
        mine = frozenset(me(request)["perms"])
        return {k: v for k, v in all_roles(s).items() if can_grant(mine, v["permissions"])}

    def may_manage(s, request, user: User) -> str | None:
        """Why this signed-in user may not change this account, or None."""
        if user.username == me(request)["username"]:
            return "You cannot change your own account here: ask another administrator"
        if not can_grant(frozenset(me(request)["perms"]), permissions_of(s, user.role)):
            return f"{user.username} holds permissions you do not have"
        return None

    def user_rows(s):
        load = dict(s.execute(select(Opportunity.owner, func.count(Opportunity.id)).where(
            Opportunity.stage.not_in(("won", "lost"))).group_by(Opportunity.owner)).all())
        teams = {t.id: t.name for t in s.execute(select(Team)).scalars()}
        roles = all_roles(s)
        return [{"u": u, "role": roles.get(u.role, {}).get("label", u.role), "team": teams.get(u.team_id), "open": load.get(u.username, 0),
                 "receives": "receive_leads" in roles.get(u.role, {}).get("permissions", ())}
                for u in s.execute(select(User).order_by(User.active.desc(), User.username)).scalars()]

    def users_view(request, s, **extra):
        return render(request, "users.html", rows=user_rows(s), roles=grantable(s, request),
                      teams=s.execute(select(Team).where(Team.active.is_(True)).order_by(Team.name)).scalars().all(),
                      usernames=list(s.execute(select(User.username).where(User.active.is_(True)).order_by(User.username)).scalars()),
                      services=SERVICES, **{"edit": None, "setup": None, **extra})

    @app.get("/admin/users", response_class=HTMLResponse)
    def users_page(request: Request, edit: int = 0):
        with C().db.session() as s:
            target = s.get(User, edit) if edit else None
            return users_view(request, s, **({"edit": target} if target else {}))

    def _team_and_manager(s, team_id: str, manager: str):
        number = form_id(team_id)
        team = s.get(Team, number) if number is not None else None
        boss = manager.strip().lower() or None
        if boss and not s.execute(select(User.id).where(User.username == boss, User.active.is_(True))).first():
            raise ValueError(f"manager {boss} is not an active user")
        return (team.id if team else None), boss

    @app.post("/admin/users", response_class=HTMLResponse)
    def create_user(request: Request, username: str = Form(...), display_name: str = Form(""), role: str = Form(...),
                    team_id: str = Form(""), manager: str = Form("")):
        name = username.strip().lower()
        with C().db.session() as s:
            if not USERNAME_RE.fullmatch(name):
                return back("/admin/users", "Username: 2-60 characters, lower case letters, digits, dot, dash or underscore, "
                                            "starting with a letter")
            if s.execute(select(User.id).where(User.username == name)).first():
                return back("/admin/users", f"{name} already exists")
            if role not in grantable(s, request):
                return back("/admin/users", "You cannot give that role")
            try:
                team, boss = _team_and_manager(s, team_id, manager)
            except ValueError as exc:
                return back("/admin/users", str(exc))
            # no usable password until the person opens their setup link and chooses one
            user = User(username=name, password_hash=UNUSABLE_HASH, role=role, display_name=display_name.strip()[:120] or None,
                        team_id=team, manager=boss)
            s.add(user)
            s.flush()
            token = issue_setup_link(user)
            audit(s, who(request), "user_created", f"user {name}", f"role {role}; setup link issued, valid {SETUP_HOURS} h")
            return users_view(request, s, setup={"username": name, "path": f"/setup?token={token}", "hours": SETUP_HOURS})

    @app.post("/admin/users/{uid}/edit")
    def edit_user(request: Request, uid: int, role: str = Form(...), display_name: str = Form(""), team_id: str = Form(""),
                  manager: str = Form(""), skills: Annotated[list[str] | None, Form()] = None, available: str = Form(""), capacity: str = Form("0")):
        with C().db.session() as s:
            user = s.get(User, uid) or _404()
            if why := may_manage(s, request, user):
                return back("/admin/users", why)
            if role not in grantable(s, request):
                return back(f"/admin/users?edit={uid}#edit", "You cannot give that role")
            try:
                team, boss = _team_and_manager(s, team_id, manager)
                cap = int(capacity or 0)
                if not 0 <= cap <= 1000:
                    raise ValueError("capacity must be 0 (no limit) to 1000 open deals")
            except ValueError as exc:
                return back(f"/admin/users?edit={uid}#edit", str(exc).replace("invalid literal for int() with base 10", "capacity is not a number"))
            if boss == user.username:
                return back(f"/admin/users?edit={uid}#edit", "Nobody reports to themselves")
            before = (user.role, user.team_id, user.manager, list(user.skills or []), user.available, user.capacity)
            user.role, user.team_id, user.manager = role, team, boss
            user.display_name = display_name.strip()[:120] or None
            user.skills = [x for x in skills or [] if x in SERVICES]
            user.available, user.capacity = available == "yes", cap
            if role != before[0]:
                user.session_version = (user.session_version or 1) + 1  # new permissions from the next sign-in
            audit(s, who(request), "user_changed", f"user {user.username}",
                  f"{before} -> {(user.role, user.team_id, user.manager, user.skills, user.available, user.capacity)}"[:2000])
        return back("/admin/users", f"Saved {user.username}")

    @app.post("/admin/users/{uid}/{action}", response_class=HTMLResponse)
    def user_action(request: Request, uid: int, action: str):
        if action not in ("deactivate", "activate", "reset"):
            raise HTTPException(400)
        with C().db.session() as s:
            user = s.get(User, uid) or _404()
            if why := may_manage(s, request, user):
                return back("/admin/users", why)
            if action == "deactivate":
                user.active = False
                user.session_version = (user.session_version or 1) + 1
                audit(s, who(request), "user_deactivated", f"user {user.username}", "signed out everywhere")
                return back("/admin/users", f"{user.username} deactivated and signed out; their leads and deals keep "
                                            "their owner until you reassign them")
            if action == "activate":
                user.active = True
                audit(s, who(request), "user_activated", f"user {user.username}", "")
                return back("/admin/users", f"{user.username} activated")
            token = issue_setup_link(user)
            audit(s, who(request), "user_reset", f"user {user.username}", f"setup link issued, valid {SETUP_HOURS} h")
            return users_view(request, s, setup={"username": user.username, "path": f"/setup?token={token}",
                                                  "hours": SETUP_HOURS})

    # ------------------------------------------------------------------ one-time setup link (public path)
    def _by_token(s, token: str) -> User | None:
        if not token or len(token) > 100:
            return None
        user = s.execute(select(User).where(User.setup_token_hash == token_hash(token))).scalars().first()
        if user is None or not user.active or not user.setup_expires_at or user.setup_expires_at < utcnow():
            return None
        return user

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, token: str = ""):
        with C().db.session() as s:
            user = _by_token(s, token)
            return render(request, "setup.html", username=user.username if user else None, token=token if user else "",
                          nav_status=None)

    @app.post("/setup", response_class=HTMLResponse)
    def setup_submit(request: Request, token: str = Form(""), password: str = Form(""), confirm: str = Form("")):
        from aime.web.auth import LOGIN_LOCK, hash_password

        with LOGIN_LOCK, C().db.session() as s:
            user = _by_token(s, token)
            if user is None:
                return render(request, "setup.html", username=None, token="", nav_status=None)
            if password != confirm:
                return render(request, "setup.html", username=user.username, token=token, nav_status=None,
                              error="The two passwords differ")
            try:
                user.password_hash = hash_password(password)
            except ValueError as exc:
                return render(request, "setup.html", username=user.username, token=token, nav_status=None, error=str(exc))
            user.setup_token_hash, user.setup_expires_at = None, None
            user.session_version = (user.session_version or 1) + 1
            user.failed_logins, user.locked_until = 0, None
            audit(s, user.username, "password_set", f"user {user.username}", "through a one-time setup link")
        return back("/login", "Password set: sign in now")

    # ------------------------------------------------------------------ teams
    @app.get("/admin/teams", response_class=HTMLResponse)
    def teams_page(request: Request):
        with C().db.session() as s:
            teams = s.execute(select(Team).order_by(Team.active.desc(), Team.name)).scalars().all()
            members = {t.id: list(s.execute(select(User.username).where(User.team_id == t.id, User.active.is_(True))
                                            .order_by(User.username)).scalars()) for t in teams}
            usernames = list(s.execute(select(User.username).where(User.active.is_(True)).order_by(User.username)).scalars())
            return render(request, "teams.html", teams=teams, members=members, usernames=usernames)

    @app.post("/admin/teams")
    def team_save(request: Request, name: str = Form(...), manager: str = Form(""), team_id: str = Form(""),
                  active: str = Form("yes")):
        label = " ".join(name.split())[:80]
        if not label:
            return back("/admin/teams", "Give the team a name")
        with C().db.session() as s:
            boss = manager.strip().lower() or None
            if boss and not s.execute(select(User.id).where(User.username == boss, User.active.is_(True))).first():
                return back("/admin/teams", f"{boss} is not an active user")
            number = form_id(team_id)
            team = s.get(Team, number) if number is not None else None
            clash = s.execute(select(Team.id).where(Team.name == label)).scalar()
            if clash and (team is None or clash != team.id):
                return back("/admin/teams", f"A team called {label} exists")
            if team is None:
                team = Team(name=label)
                s.add(team)
            team.name, team.manager, team.active = label, boss, active == "yes"
            audit(s, who(request), "team_saved", f"team {label}", f"manager {boss or 'none'}; active {team.active}")
        return back("/admin/teams", f"Saved team {label}")

    # ------------------------------------------------------------------ custom roles
    @app.get("/admin/roles", response_class=HTMLResponse)
    def roles_page(request: Request, edit: int = 0):
        with C().db.session() as s:
            custom = s.execute(select(CustomRole).order_by(CustomRole.name)).scalars().all()
            used = dict(s.execute(select(User.role, func.count(User.id)).group_by(User.role)).all())
            mine = frozenset(me(request)["perms"])
            return render(request, "roles.html", builtin=BUILTIN_ROLES, custom=custom, used=used, permissions=PERMISSIONS,
                          grantable=[p for p in PERMISSIONS if p in mine],
                          edit=s.get(CustomRole, edit) if edit else None)

    @app.post("/admin/roles")
    def role_save(request: Request, name: str = Form(...), label: str = Form(...), permissions: Annotated[list[str] | None, Form()] = None,
                  role_id: str = Form(""), active: str = Form("yes")):
        slug = name.strip().lower()
        with C().db.session() as s:
            number = form_id(role_id)
            role = s.get(CustomRole, number) if number is not None else None
            if role is None and (not ROLE_NAME_RE.fullmatch(slug) or slug in BUILTIN_ROLES):
                return back("/admin/roles", "Role name: 3-30 lower case letters, digits or underscores, not a built-in role")
            if role is None and s.execute(select(CustomRole.id).where(CustomRole.name == slug)).first():
                return back("/admin/roles", f"A role called {slug} exists")
            picked = [p for p in permissions or [] if p in PERMISSIONS]
            if not can_grant(frozenset(me(request)["perms"]), frozenset(picked)):
                return back("/admin/roles", "You cannot give a role permissions you do not have")
            if role is not None and active != "yes" and s.execute(select(User.id).where(User.role == role.name,
                                                                                        User.active.is_(True))).first():
                return back("/admin/roles", f"Active users have the role {role.name}: give them another role first")
            if role is None:
                role = CustomRole(name=slug)
                s.add(role)
            elif not can_grant(frozenset(me(request)["perms"]), frozenset(role.permissions or [])):
                return back("/admin/roles", "That role holds permissions you do not have")
            role.label, role.permissions = " ".join(label.split())[:80] or role.name, picked
            role.active, role.updated_by = active == "yes", who(request)
            audit(s, who(request), "role_saved", f"role {role.name}", f"permissions {picked}; active {role.active}")
        return back("/admin/roles", f"Saved role {role.name}")

    # ------------------------------------------------------------------ assigning leads and deals
    def _assignable(s, request, owner: str) -> str | None:
        """Why this user may not give a record to `owner`, or None. Managers assign within their team."""
        if not owner:
            return None
        if not s.execute(select(User.id).where(User.username == owner, User.active.is_(True))).first():
            return f"{owner} is not an active user"
        sc = getattr(request.state, "scope", None)
        if sc is not None and not sc.all and owner not in sc.usernames:
            return f"{owner} is not in your team"
        return None

    @app.post("/leads/{lead_id}/assign")
    def assign_lead(request: Request, lead_id: int, owner: str = Form(""), reason: str = Form("")):
        from aime.assignment import reassign

        with C().db.session() as s:
            take_write_lock(s)  # a double click: the second one finds the owner the first one set
            lead = s.get(Lead, lead_id) or _404()
            target = owner.strip().lower() or None
            if why := _assignable(s, request, target or ""):
                return back(f"/leads/{lead_id}", why)
            note = reassign(s, to_owner=target, actor=who(request), reason=" ".join(reason.split())[:200], lead=lead)
        return back(f"/leads/{lead_id}", note)

    @app.post("/opportunities/{opp_id}/assign")
    def assign_opp(request: Request, opp_id: int, owner: str = Form(""), reason: str = Form("")):
        from aime.assignment import reassign

        with C().db.session() as s:
            take_write_lock(s)
            opp = s.get(Opportunity, opp_id) or _404()
            target = owner.strip().lower() or None
            if why := _assignable(s, request, target or ""):
                return back("/pipeline", why)
            note = reassign(s, to_owner=target, actor=who(request), reason=" ".join(reason.split())[:200], opp=opp)
        return back("/pipeline", note)


    # ------------------------------------------------------------------ my work
    @app.get("/my", response_class=HTMLResponse)
    def my_work(request: Request):

        from aime.models import FollowUp, InboundEmail, Meeting
        from aime.performance import build
        from aime.visibility import EVERYTHING, lead_condition, opportunity_condition, record_condition

        sc = getattr(request.state, "scope", None) or EVERYTHING
        user = getattr(request.state, "user", None)
        name = user["username"] if user else None
        now = utcnow()
        with C().db.session() as s:
            mine = Lead.owner.in_(sc.usernames) if not sc.all else Lead.owner.is_not(None)
            leads = s.execute(select(Lead).where(lead_condition(sc), mine, Lead.status.not_in(("merged", "disqualified")))
                              .order_by(Lead.score.desc()).limit(12)).scalars().all()
            for lead in leads:
                _ = lead.company
            lead_total = s.execute(select(func.count(Lead.id)).where(lead_condition(sc), mine, Lead.status.not_in(
                ("merged", "disqualified")))).scalar_one()
            unassigned = s.execute(select(func.count(Lead.id)).where(lead_condition(sc), Lead.owner.is_(None),
                                                                     Lead.status == "qualified")).scalar_one() if sc.unassigned else 0
            deals = s.execute(select(Opportunity).where(opportunity_condition(sc), Opportunity.stage.not_in(("won", "lost")))
                              .order_by(Opportunity.expected_close.is_(None), Opportunity.expected_close, Opportunity.id)).scalars().all()
            for d in deals:
                _ = d.company
            approvals = s.execute(select(func.count(FollowUp.id)).where(
                record_condition(sc, FollowUp.lead_id), FollowUp.status == "awaiting_approval")).scalar_one()
            tasks = s.execute(select(FollowUp).where(record_condition(sc, FollowUp.lead_id), FollowUp.channel == "task",
                                                     FollowUp.status == "pending").order_by(FollowUp.due_at).limit(10)).scalars().all()
            for t in tasks:
                _ = t.lead.company
            replies = s.execute(select(func.count(InboundEmail.id)).where(
                record_condition(sc, InboundEmail.lead_id), InboundEmail.status == "needs_review")).scalar_one()
            meetings = s.execute(select(Meeting).where(record_condition(sc, Meeting.lead_id, allow_unlinked=False),
                                                       Meeting.status == "scheduled", Meeting.ends_at >= now)
                                 .order_by(Meeting.starts_at).limit(8)).scalars().all()
            people = sorted(sc.usernames) if not sc.all else ([name] if name else [])
            report = build(s, now - timedelta(days=30), now, people, scope=sc, include_unassigned=False) if people else None
        return render(request, "my.html", leads=leads, lead_total=lead_total, unassigned=unassigned, deals=deals,
                      approvals=approvals, tasks=tasks, replies=replies, meetings=meetings, report=report,
                      who_label=("you and your team" if sc.usernames and len(sc.usernames) > 1 else "you"),
                      pipeline_value=sum(d.est_value or 0 for d in deals), today=now.date())

    # ------------------------------------------------------------------ sales performance
    @app.get("/performance", response_class=HTMLResponse)
    def performance_page(request: Request, start: str = "", end: str = "", team: str = "", owner: str = "",
                         source: str = ""):
        from datetime import date, datetime

        from aime.performance import build, team_members
        from aime.timeutil import local_tz
        from aime.visibility import EVERYTHING

        tz = local_tz()  # the days a person reads: every date on screen is in this zone (fmt_local)
        today = as_aware_utc(utcnow()).astimezone(tz).date()
        try:
            first = date.fromisoformat(start) if start else today - timedelta(days=30)
            last = date.fromisoformat(end) if end else today
        except ValueError:
            return back("/performance", "Dates are YYYY-MM-DD")

        def midnight(day: date) -> datetime:
            # the picker's far ends (0001-01-01, 9999-12-31) would overflow the conversion to UTC
            return to_naive_utc(datetime.combine(max(min(day, date(9000, 1, 1)), date(1900, 1, 1)), datetime.min.time(), tzinfo=tz))

        sc = getattr(request.state, "scope", None)
        everyone = sc is None or sc.all
        team_id = form_id(team)
        with C().db.session() as s:
            # also people who have left: what they won stays in the report, and their row shows while it has numbers
            people = list(s.execute(select(User.username).order_by(User.username)).scalars())
            active = set(s.execute(select(User.username).where(User.active.is_(True))).scalars())
            if not everyone:
                people = [p for p in people if p in sc.usernames]  # their own, or their team's
            teams = s.execute(select(Team).where(Team.active.is_(True)).order_by(Team.name)).scalars().all()
            if team_id is not None:
                members = set(team_members(s).get(team_id, []))
                people = [p for p in people if p in members]
            if owner:
                people = [p for p in people if p == owner]
            narrowed = not everyone or team_id is not None or bool(owner)
            report = build(s, midnight(first), midnight(min(last, date(9000, 1, 1)) + timedelta(days=1)),
                           people if narrowed else None, source or None, scope=sc or EVERYTHING, include_unassigned=not narrowed)
            report.rows = [r for r in report.rows if r.username in active or r.has_numbers()]
            if narrowed:
                report.unassigned = None  # not part of this report's Total: no row for it
            sources = sorted(x for x in s.execute(select(Lead.source).distinct()).scalars() if x)
        return render(request, "performance.html", report=report, teams=teams, sources=sources,
                      f={"start": first.isoformat(), "end": last.isoformat(), "team": team, "owner": owner, "source": source},
                      everyone=everyone)


def assignment_history(s, lead_id: int) -> list[Assignment]:
    return s.execute(select(Assignment).where(Assignment.lead_id == lead_id).order_by(Assignment.id.desc()).limit(30)).scalars().all()


def _404():
    raise HTTPException(404)
