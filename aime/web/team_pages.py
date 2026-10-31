"""The sales organisation on the dashboard: user accounts, teams, custom roles, one-time account setup links, and
assigning leads and deals. The sign-in middleware lets only `manage_users` reach /admin/users, /admin/teams and
/admin/roles and only `assign_leads` assign; the rules here stop privilege escalation: nobody can give a role with a
permission they lack, change their own role or account, or change an account that holds more than they do."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from aime.automation import audit
from aime.db import take_write_lock
from aime.knowledge.services import SERVICES
from aime.models import Activity, Assignment, CustomRole, FollowUp, Lead, Opportunity, Team, User
from aime.outreach import STOP_STATUSES
from aime.permissions import BUILTIN_ROLES, PERMISSIONS, all_roles, can_grant, permissions_of
from aime.timeutil import as_aware_utc, local_tz, to_naive_utc, utcnow
from aime.visibility import Scope
from aime.web.auth import form_id

SETUP_HOURS = 48
UNUSABLE_HASH = "setup-pending"  # matches no password: the account works once its owner sets one
USERNAME_RE = re.compile(r"[a-z][a-z0-9._-]{1,59}")
ROLE_NAME_RE = re.compile(r"[a-z][a-z0-9_]{2,29}")


def next_email(settings, lead: Lead, fups: list[FollowUp]) -> dict:
    """What happens next in this lead's email sequence, by the rules the sales workflow applies: {"text", "at"}; `at`, when
    there is one, is the date the text ends on. Only a promise that nothing goes out without a person is ever made."""
    emails = [f for f in fups if f.channel == "email"]
    words = {"awaiting_approval": "is drafted and waits for a person's approval", "approved": "is approved and goes out in the next send window",
             "sending": "is going out now", "uncertain": "may have gone out: check the Sent folder", "pending": "is planned"}
    waiting = [f for f in emails if f.status in words]
    if waiting:
        f = min(waiting, key=lambda x: x.step or 0)
        return {"text": f"Next email: {'their answer' if f.kind == 'response' else f'step {f.step}'} {words[f.status]}", "at": None}
    if lead.status in ("disqualified", "merged"):
        return {"text": f"Next email: none, this lead is {lead.status}", "at": None}
    if lead.outreach_status in STOP_STATUSES:
        return {"text": "Next email: none, the sequence was stopped" if lead.outreach_status == "stopped" else
                f"Next email: none, the sequence stopped ({lead.outreach_status.replace('_', ' ')})", "at": None}
    sent = [f for f in emails if f.status == "sent" and f.sent_at and f.kind in ("outreach", None)]
    if not sent:
        tasks = sorted((f for f in fups if f.channel == "task" and f.status == "pending"), key=lambda f: f.due_at)
        if tasks:
            return {"text": f"Next step: {tasks[0].subject}, due", "at": tasks[0].due_at}
        return {"text": "Next email: none planned yet", "at": None}
    last = max(sent, key=lambda f: f.step or 0)
    intervals = list(settings.section("outreach").get("intervals_days", [0, 4, 10]) if last.kind == "outreach"
                     else settings.section("followup").get("intervals_days", [0, 3, 7]))
    if (last.step or 1) >= len(intervals):
        return {"text": f"Next email: none, all {len(intervals)} emails of the sequence were sent", "at": None}
    if any(f.status == "rejected" and (f.step or 0) > last.step for f in emails):
        return {"text": "Next email: none, a person rejected the next step", "at": None}
    gap = timedelta(days=intervals[last.step] - intervals[last.step - 1])
    return {"text": f"Next email: step {last.step + 1} is drafted for approval around", "at": last.sent_at + gap}


def last_30_days(settings) -> tuple[date, date]:
    """The same "last 30 days" on My work and on Sales performance: whole days on the owner's clock, today included."""
    today = as_aware_utc(utcnow()).astimezone(local_tz(settings.schedule.get("timezone"))).date()
    return today - timedelta(days=30), today


def day_start(day: date, tz) -> datetime:
    # the picker's far ends (0001-01-01, 9999-12-31) would overflow the conversion to UTC
    return to_naive_utc(datetime.combine(max(min(day, date(9000, 1, 1)), date(1900, 1, 1)), datetime.min.time(), tzinfo=tz))


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
            away = ""
            if before[4] is not False and not user.available:
                from aime.assignment import open_work

                away = (f". {user.username} gets no new leads now and still holds {open_work(s, user.username)}: that stays with them "
                        "until a manager reassigns it on each lead's page")
        return back("/admin/users", f"Saved {user.username}{away}")

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
    def my_work(request: Request, view: str = ""):

        from aime.models import InboundEmail, Meeting
        from aime.performance import build
        from aime.visibility import EVERYTHING, lead_condition, opportunity_condition, record_condition

        sc = getattr(request.state, "scope", None) or EVERYTHING
        user = getattr(request.state, "user", None)
        name = user["username"] if user else None
        settings = C().settings
        tz = local_tz(settings.schedule.get("timezone"))
        now = utcnow()
        # Mine: what this person owns. Team: all they may see. Someone who sees everything starts on their own work, as
        # others' leads are not "on you"; a team member starts on the team, whose leads they share.
        has_team = bool(name) and (sc.all or len(sc.usernames) > 1)
        view = view if view in ("mine", "team") and has_team else ("mine" if name and (sc.all or not has_team) else "team")
        shown = Scope(all=False, usernames=frozenset({name}), unassigned=False) if view == "mine" and name else sc
        with C().db.session() as s:
            mine = Lead.owner.is_not(None) if shown.all else Lead.owner.in_(shown.usernames)
            working = Lead.status.not_in(("merged", "disqualified"))
            leads = s.execute(select(Lead).where(lead_condition(shown), mine, working)
                              .order_by(Lead.score.desc()).limit(12)).scalars().all()
            lead_total = s.execute(select(func.count(Lead.id)).where(lead_condition(shown), mine, working)).scalar_one()
            unassigned = s.execute(select(func.count(Lead.id)).where(lead_condition(sc), Lead.owner.is_(None),
                                                                     Lead.status == "qualified")).scalar_one() if sc.unassigned else 0
            emails = defaultdict(list)
            for f in s.execute(select(FollowUp).where(FollowUp.lead_id.in_([lead.id for lead in leads] or [0]))).scalars():
                emails[f.lead_id].append(f)
            lead_rows = [{"lead": lead, "company": lead.company.name, "stage": (lead.outreach_status or "not contacted").replace("_", " "),
                          "last": max((f.sent_at for f in emails[lead.id] if f.sent_at), default=None),
                          "coming": next_email(settings, lead, emails[lead.id])} for lead in leads]
            deals = s.execute(select(Opportunity).where(opportunity_condition(shown), Opportunity.stage.not_in(("won", "lost")))
                              .order_by(Opportunity.expected_close.is_(None), Opportunity.expected_close, Opportunity.id)).scalars().all()
            for d in deals:
                _ = d.company
            approvals = s.execute(select(func.count(FollowUp.id)).where(
                record_condition(shown, FollowUp.lead_id), FollowUp.status == "awaiting_approval")).scalar_one()
            open_tasks = (record_condition(shown, FollowUp.lead_id), FollowUp.channel == "task", FollowUp.status == "pending")
            tasks = s.execute(select(FollowUp).where(*open_tasks).order_by(FollowUp.due_at).limit(10)).scalars().all()
            task_total = s.execute(select(func.count(FollowUp.id)).where(*open_tasks)).scalar_one()
            for t in tasks:
                _ = t.lead.company
            replies = s.execute(select(func.count(InboundEmail.id)).where(
                record_condition(shown, InboundEmail.lead_id), InboundEmail.status == "needs_review")).scalar_one()
            coming = (record_condition(shown, Meeting.lead_id, allow_unlinked=False), Meeting.status == "scheduled",
                      Meeting.ends_at >= now, Meeting.is_simulated.is_(False))
            meetings = s.execute(select(Meeting).where(*coming).order_by(Meeting.starts_at).limit(8)).scalars().all()
            meeting_total = s.execute(select(func.count(Meeting.id)).where(*coming)).scalar_one()
            if view == "mine" or not sc.all:
                people = sorted(shown.usernames)
            else:
                people = list(s.execute(select(User.username).where(User.active.is_(True)).order_by(User.username)).scalars())
            first, last = last_30_days(settings)
            report = (build(s, day_start(first, tz), day_start(last + timedelta(days=1), tz), people, scope=shown,
                            include_unassigned=False) if people else None)
            # told once a week's worth: leads someone else handed to this person
            handed = s.execute(select(Assignment).where(Assignment.to_owner == name, Assignment.actor != name,
                                                        Assignment.lead_id.is_not(None), Assignment.created_at >= now - timedelta(days=7))
                               .order_by(Assignment.id.desc()).limit(6)).scalars().all() if name else []
            handed_rows = [{"at": a.created_at, "from": a.from_owner or "nobody", "by": a.actor, "lead_id": a.lead_id,
                            "company": s.get(Lead, a.lead_id).company.name, "reason": a.reason} for a in handed]
            first_day = bool(name) and s.execute(select(Activity.id).where(Activity.actor == name).limit(1)).first() is None
            today = as_aware_utc(now).astimezone(tz).date()
            # one line, for this person: what waits on the team first, then their own work, never a colleague's task
            own = Scope(all=False, usernames=frozenset({name}), unassigned=False) if name else shown
            overdue = next((d for d in deals if d.expected_close and d.expected_close < today and (not name or d.owner == name)), None)
            own_task = s.execute(select(FollowUp).where(record_condition(own, FollowUp.lead_id), FollowUp.channel == "task", FollowUp.due_at <= now,
                                                        FollowUp.status == "pending").order_by(FollowUp.due_at).limit(1)).scalars().first()
            own_lead = s.execute(select(Lead).where(lead_condition(own), Lead.owner == name if name else mine, working)
                                 .order_by(Lead.score.desc()).limit(1)).scalars().first()
            may_approve = not user or "approve_outbound" in user.get("perms", ())
            if replies:
                next_step = (f"Read the {replies} repl{'ies' if replies != 1 else 'y'} waiting for a person.", "/inbox")
            elif approvals and may_approve:
                next_step = (f"Read and approve or reject the {approvals} email{'s' if approvals != 1 else ''} waiting.", "/approvals")
            elif overdue:
                next_step = (f"Update {overdue.name}: its close date has passed.", f"/leads/{overdue.lead_id}" if overdue.lead_id else "/pipeline")
            elif own_task:
                next_step = (f"Do the task: {own_task.subject}.", f"/leads/{own_task.lead_id}")
            elif own_lead:
                next_step = (f"Open your strongest lead, {own_lead.company.name}, and read its evidence.", f"/leads/{own_lead.id}")
            elif unassigned:
                may_assign = not user or "assign_leads" in user.get("perms", ())
                next_step = (f"No lead is yours yet: {'assign' if may_assign else 'ask your manager to assign'} some of the unassigned ones.",
                             "/leads?status=qualified&owner=none")
            else:
                next_step = ("Nothing waits for you. Leads appear here when they are assigned to you.", None)
        team_label = "everyone" if sc.all else "you and your team"
        return render(request, "my.html", leads=lead_rows, lead_total=lead_total, unassigned=unassigned, deals=deals,
                      approvals=approvals, tasks=tasks, task_total=task_total, replies=replies, meetings=meetings,
                      meeting_total=meeting_total, report=report, view=view, has_team=has_team, handed=handed_rows,
                      first_day=first_day, next_step=next_step,
                      who_label="you" if view == "mine" or not has_team else team_label,
                      pipeline_value=sum(d.est_value or 0 for d in deals), today=today)

    # ------------------------------------------------------------------ sales performance
    @app.get("/performance", response_class=HTMLResponse)
    def performance_page(request: Request, start: str = "", end: str = "", team: str = "", owner: str = "",
                         source: str = ""):
        from aime.performance import build, team_members
        from aime.visibility import EVERYTHING

        tz = local_tz(C().settings.schedule.get("timezone"))  # the days a person reads: every date on screen is in this zone
        default_first, default_last = last_30_days(C().settings)
        try:
            first = date.fromisoformat(start) if start else default_first
            last = date.fromisoformat(end) if end else default_last
        except ValueError:
            return back("/performance", "Dates are YYYY-MM-DD")

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
            report = build(s, day_start(first, tz), day_start(min(last, date(9000, 1, 1)) + timedelta(days=1), tz),
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
