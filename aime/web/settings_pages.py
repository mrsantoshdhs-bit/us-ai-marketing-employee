"""Super Admin pages for the settings control plane (aime.settings_store): every section with its active version and history,
a preview of what a change would do before it is saved, one-click restore of any earlier version, the country registry with
its legal-review record, and today's progress towards the daily targets.

Everything lives under /admin/settings, so the auth middleware lets only `configure_system` (Super Admin) reach it, viewing
included, and the same-origin check covers every change. Pages are plain forms (the content policy allows no script). The
env-only switches (approval, sending, publishing) are shown read-only and are in no form.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from aime import settings_store as ss

FORM_MARK = "settings_form"  # every form carries it: a request without it (an empty POST) changes nothing


def _404():
    raise HTTPException(404, "Not found")


def _csv(text) -> list[str]:
    return [x.strip() for x in str(text or "").replace(";", ",").split(",") if x.strip()]


def _int(text, default=0):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return text  # the model says what is wrong with it


def _float(text, default=0.0):
    raw = str(text if text is not None else default).strip()
    try:
        return float(raw)
    except ValueError:
        return raw  # the model says what is wrong with it


def _on(form, name: str) -> bool:
    return (form.get(name) or "") in ("yes", "on", "true", "1")


# --------------------------------------------------------------------------- form <-> section

def parse(name: str, form, settings) -> dict:
    """The section a submitted form describes. Values are passed on as typed; the section's model validates them."""
    current = ss.get(settings, name)
    if name == "countries":
        old = {c["code"]: c for c in current["countries"]}
        rows = []
        for i in range(int(_int(form.get("rows"), 0) or 0) + 1):
            code = (form.get(f"c{i}.code") or "").strip().upper()
            if not code or _on(form, f"c{i}.remove"):
                continue
            before = old.get(code, {})
            rows.append({"code": code, "enabled": _on(form, f"c{i}.enabled"), "name": (form.get(f"c{i}.name") or "").strip(),
                         "timezone": (form.get(f"c{i}.timezone") or "").strip(),
                         "send_window_start": (form.get(f"c{i}.send_window_start") or "").strip(),
                         "send_window_end": (form.get(f"c{i}.send_window_end") or "").strip(),
                         "holidays": _csv(form.get(f"c{i}.holidays")),
                         "compliance_regime": (form.get(f"c{i}.compliance_regime") or "").strip(),
                         "language": (form.get(f"c{i}.language") or "").strip(),
                         # the legal review is not in this form: it is carried over, and changed only by its own form
                         **{k: before.get(k) for k in ("legal_review_confirmed_by", "legal_review_confirmed_at", "legal_review_note")}})
        return {"countries": rows}
    if name == "targets":
        rows = []
        for i in range(int(_int(form.get("rows"), 0) or 0)):
            code = (form.get(f"t{i}.country") or "").strip().upper()
            if code:
                rows.append({"country": code, "daily_verified_leads": _int(form.get(f"t{i}.verified"), 0),
                             "daily_contactable_leads": _int(form.get(f"t{i}.contactable"), 0)})
        return {"targets": rows}
    if name == "sources":
        return {"sources": [{"name": s["name"], "enabled": _on(form, f"{s['name']}.enabled"),
                             "priority": _int(form.get(f"{s['name']}.priority"), s["priority"]),
                             "countries": _csv(form.get(f"{s['name']}.countries"))} for s in current["sources"]]}
    if name == "contact_sources":
        return {"adapters": [{"name": a["name"], "enabled": _on(form, f"{a['name']}.enabled"),
                              "priority": _int(form.get(f"{a['name']}.priority"), a["priority"]),
                              "countries": _csv(form.get(f"{a['name']}.countries")),
                              "monthly_budget_usd": _float(form.get(f"{a['name']}.monthly_budget_usd"), a["monthly_budget_usd"]),
                              "cost_per_call_usd": _float(form.get(f"{a['name']}.cost_per_call_usd"), a["cost_per_call_usd"])}
                             for a in current["adapters"]]}
    if name == "schedules":
        jobs = {}
        for job, js in current["jobs"].items():
            row = {"enabled": _on(form, f"{job}.enabled")}
            if js.get("every_minutes"):
                row["every_minutes"] = _int(form.get(f"{job}.every_minutes"), js["every_minutes"])
            if js.get("daily_at"):
                row["daily_at"] = (form.get(f"{job}.daily_at") or "").strip()
            jobs[job] = row
        return {"jobs": jobs}
    if name == "catalogue":
        out = {}
        for kind in ("industries", "services"):
            rows = []
            for e in current[kind]:
                rows.append({"key": e["key"], "label": (form.get(f"{kind}.{e['key']}.label") or "").strip(),
                             "enabled": _on(form, f"{kind}.{e['key']}.enabled"), "builtin": e["builtin"],
                             "description": (form.get(f"{kind}.{e['key']}.description") or "").strip()})
            key = (form.get(f"{kind}.new.key") or "").strip().lower()
            if key:
                rows.append({"key": key, "label": (form.get(f"{kind}.new.label") or "").strip() or key, "enabled": True,
                             "builtin": False, "description": (form.get(f"{kind}.new.description") or "").strip()})
            out[kind] = rows
        return out
    return _parse_simple(current, form)


def _parse_simple(current: dict, form, prefix: str = "") -> dict:
    """Sections of plain values (deliverability, ai, retention): each value has a field named by its path."""
    out = {}
    for key, value in current.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out[key] = _parse_simple(value, form, path + ".")
        elif isinstance(value, bool):
            out[key] = _on(form, path)
        elif isinstance(value, list):
            items = _csv(form.get(path))
            out[key] = [_int(x) for x in items] if key.endswith("plan") else items
        elif isinstance(value, (int, float)):
            raw = (form.get(path) or "").strip()
            try:
                out[key] = type(value)(raw)
            except ValueError:
                out[key] = raw
        else:
            out[key] = (form.get(path) or "").strip()
    return out


def simple_fields(data: dict, prefix: str = "") -> list[tuple[str, str, object]]:
    """(path, kind, value) for each plain value: kind is bool | list | number | text."""
    out = []
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.extend(simple_fields(value, path + "."))
        elif isinstance(value, bool):
            out.append((path, "bool", value))
        elif isinstance(value, list):
            out.append((path, "list", ", ".join(map(str, value))))
        elif isinstance(value, (int, float)):
            out.append((path, "number", value))
        else:
            out.append((path, "text", value))
    return out


# --------------------------------------------------------------------------- pages

def register(app, C, render, back, who) -> None:
    def section_view(s, ctx, name: str) -> dict:
        from aime import countries, targets
        from aime.agent_matrix import agent_policy
        from aime.automation import ALWAYS_RUN_JOBS
        from aime.sources.registry import SOURCE_CLASSES

        spec = ss.SECTIONS[name]
        data = ss.get(ctx.settings, name)
        view = {"section": name, "spec": spec, "data": data, "stored": ss.stored(ctx.settings, name),
                "active_id": ss.active_version_id(ctx.settings, name), "history": ss.history(s, name),
                "form_mark": FORM_MARK, "simple": simple_fields(data) if name in ("deliverability", "ai", "retention", "conversation") else []}
        if name == "countries":
            view["targets"] = countries.target_countries(ctx.settings)
            view["coverage"] = {c["code"]: countries.sources_for(ctx.settings, c["code"]) for c in data["countries"]}
            view["cleared"] = {c["code"]: countries.cleared(c) for c in data["countries"]}
        if name == "targets":
            known = [c["code"] for c in countries.registry(ctx.settings)]
            have = {t["country"]: t for t in data["targets"]}
            view["rows"] = [have.get(c, {"country": c, "daily_verified_leads": 0, "daily_contactable_leads": 0}) for c in known]
            view["progress"] = targets.progress(s, ctx.settings)
            view["cap"] = ctx.settings.section("research").get("max_new_companies_per_run", 40)
        if name == "sources":
            view["covers"] = {n: ", ".join(getattr(c, "countries", ("US",))) for n, c in SOURCE_CLASSES.items()}
            view["titles"] = {n: getattr(c, "title", n) for n, c in SOURCE_CLASSES.items()}
        if name == "contact_sources":
            from aime.contact_sources.registry import ADAPTER_CLASSES

            view["adapter_meta"] = {n: {"title": c.title, "covers": ", ".join(c.countries) or "none claimed", "paid": c.paid}
                                    for n, c in ADAPTER_CLASSES.items()}
        if name == "schedules":
            view["always_on"] = ALWAYS_RUN_JOBS
            view["descriptions"] = {j: (ss.schedule_base(ctx.settings).get("jobs", {}).get(j) or {}).get("description", "")
                                    for j in data["jobs"]}
            view["floors"] = {j: (ss.schedule_base(ctx.settings).get("jobs", {}).get(j) or {}).get("every_minutes")
                              for j in data["jobs"]}
        if name == "agents":
            view["policies"] = [agent_policy(a, ctx) for a in data["agents"]]
        return view

    @app.get("/admin/settings/system", response_class=HTMLResponse)
    def settings_hub(request: Request):
        from aime import countries, targets

        ctx = C()
        with ctx.db.session() as s:
            rows = [{"name": n, "spec": spec, "stored": ss.stored(ctx.settings, n), "active_id": ss.active_version_id(ctx.settings, n),
                     "last": (ss.history(s, n, 1) or [None])[0]} for n, spec in ss.SECTIONS.items()]
            progress = targets.progress(s, ctx.settings)
        env = {"REQUIRE_HUMAN_APPROVAL": ctx.settings.require_human_approval, "OUTBOUND_EMAIL_ENABLED": ctx.settings.outbound_email_enabled,
               "PUBLISHING_ENABLED": ctx.settings.publishing_enabled}
        return render(request, "settings_hub.html", sections=rows, env=env, progress=progress,
                      targets=countries.target_countries(ctx.settings), discovery=countries.discovery_countries(ctx.settings))

    @app.get("/admin/settings/section/{name}", response_class=HTMLResponse)
    def settings_section(request: Request, name: str):
        if name not in ss.SECTIONS:
            _404()
        ctx = C()
        with ctx.db.session() as s:
            view = section_view(s, ctx, name)
        return render(request, "settings_section.html", **view, preview=None)

    @app.post("/admin/settings/section/{name}")
    async def settings_section_post(request: Request, name: str):
        if name not in ss.SECTIONS:
            _404()
        ctx = C()
        url = f"/admin/settings/section/{name}"
        form = await request.form()
        if not ss.SECTIONS[name].editable:
            return back(url, "Not saved: this section is not changed here")
        if form.get(FORM_MARK) != "1":
            return back(url, "Not saved: the form was incomplete")
        with ctx.db.session() as s:
            data = parse(name, form, ctx.settings)
            if form.get("do") == "preview":
                result = ss.preview(s, ctx.settings, name, data)
                view = section_view(s, ctx, name)
                if result.ok:  # the page shows what was typed, not saved, beside what would change
                    view["data"] = ss.validate_shape(name, data)
                    if name in ("deliverability", "ai", "retention", "conversation"):
                        view["simple"] = simple_fields(view["data"])
                return render(request, "settings_section.html", **view, preview=result)
            try:
                row, changes = ss.save(s, ctx.settings, name, data, who(request), form.get("note"))
            except ValueError as exc:
                s.rollback()
                return back(url, f"Not saved: {exc}"[:600])
        if row is None:
            return back(url, "Nothing changed")
        return back(url, f"Saved as version {row.id}: {len(changes)} change(s), in effect now and on the next scheduled run")

    @app.post("/admin/settings/section/{name}/reset")
    def settings_section_reset(request: Request, name: str):
        if name not in ss.SECTIONS:
            _404()
        ctx = C()
        url = f"/admin/settings/section/{name}"
        with ctx.db.session() as s:
            try:
                changes = ss.reset_to_defaults(s, ctx.settings, name, who(request))
            except ValueError as exc:
                s.rollback()
                return back(url, f"Not changed: {exc}")
        return back(url, f"Back to the defaults: {len(changes)} change(s)" if changes else "Already at the defaults")

    @app.post("/admin/settings/versions/{vid}/revert")
    def settings_revert(request: Request, vid: int):
        from aime.models import SettingsVersion

        ctx = C()
        with ctx.db.session() as s:
            old = s.get(SettingsVersion, vid) or _404()
            url = "/admin/settings" if old.section == "sales" else f"/admin/settings/section/{old.section}"
            try:
                row, changes = ss.revert(s, ctx.settings, vid, who(request))
            except (ValueError, LookupError) as exc:
                s.rollback()
                return back(url, f"Not restored: {exc}"[:600])
        if not changes:
            return back(url, f"Version {vid} is what is in effect already")
        return back(url, f"Version {vid} restored{f' as version {row.id}' if row is not None else ''}: {len(changes)} change(s)")

    @app.get("/admin/settings/contact-sources", response_class=HTMLResponse)
    def contact_sources_page(request: Request):
        from aime import funnel, provenance
        from aime.contact_sources.registry import overview

        ctx = C()
        with ctx.db.session() as s:
            rows = overview(s, ctx)
            stages = funnel.summary(s)
            legacy = provenance.pending_legacy(s)
        return render(request, "contact_sources.html", rows=rows, funnel=stages, legacy_pending=legacy, form_mark=FORM_MARK)

    @app.post("/admin/settings/contact-sources/approval")
    async def contact_source_approval(request: Request):
        from aime.contact_sources import budget

        ctx = C()
        url = "/admin/settings/contact-sources"
        form = await request.form()
        if form.get(FORM_MARK) != "1":
            return back(url, "Not recorded: the form was incomplete")
        name = (form.get("name") or "").strip()
        with ctx.db.session() as s:
            if form.get("do") == "withdraw":
                done = budget.withdraw_approval(s, name, who(request))
                return back(url, f"Approval of {name} withdrawn" if done else "There was no approval to withdraw")
            if not _on(form, "confirm"):
                return back(url, "Not recorded: tick the box to confirm the owner approved this source")
            try:
                budget.record_approval(s, name, form.get("approved_by") or "", who(request), form.get("note") or "")
            except ValueError as exc:
                s.rollback()
                return back(url, f"Not recorded: {exc}")
        return back(url, f"Approval of {name} recorded. Nothing is called until its credentials, price and monthly budget are "
                         "set, and a vendor integration exists")

    @app.post("/admin/settings/countries/review")
    async def settings_country_review(request: Request):
        from aime import countries

        ctx = C()
        url = "/admin/settings/section/countries"
        form = await request.form()
        if form.get(FORM_MARK) != "1":
            return back(url, "Not recorded: the form was incomplete")
        if not _on(form, "confirm"):
            return back(url, "Not recorded: tick the box to confirm the legal review was done")
        with ctx.db.session() as s:
            try:
                countries.record_legal_review(s, ctx.settings, form.get("code") or "", form.get("confirmed_by") or "", who(request),
                                              form.get("note") or "")
            except ValueError as exc:
                s.rollback()
                return back(url, f"Not recorded: {exc}")
        return back(url, f"Legal review of {(form.get('code') or '').upper()} recorded. The country can be enabled now; "
                         "nothing is enabled until you save that")
