"""A deal's own page: its stage history, quotes, documents, owner and activity. The sign-in middleware lets a user
reach /opportunities/<id>/... only for deals they may see (aime.visibility). Quotes are written by people only: the
agents never propose a price, a discount or terms."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date
from typing import Annotated

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import or_, select

from aime.crm_ops import log_activity
from aime.models import Activity, Assignment, Attachment, Opportunity, Quote, StageChange, User
from aime.timeutil import utcnow

MAX_BYTES = 10 * 1024 * 1024
# documents a sales team shares; never anything a browser or Windows would run
ALLOWED = {".pdf": "application/pdf", ".doc": "application/msword",
           ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           ".xls": "application/vnd.ms-excel", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
           ".ppt": "application/vnd.ms-powerpoint",
           ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
           ".csv": "text/csv", ".txt": "text/plain", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
QUOTE_ACTIONS = {"send": "sent", "accept": "accepted", "reject": "rejected", "expire": "expired"}


def safe_name(name: str) -> str:
    base = re.sub(r"[^\w .()-]", "_", (name or "file").replace("\\", "/").rsplit("/", 1)[-1]).strip(" .") or "file"
    return base[:200]


def register(app, C, render, back, who) -> None:
    def folder():
        path = C().settings.data_dir / "attachments"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _opp(s, opp_id: int) -> Opportunity:
        opp = s.get(Opportunity, opp_id)
        if opp is None:
            raise HTTPException(404)
        return opp

    @app.get("/opportunities/{opp_id}", response_class=HTMLResponse)
    def deal_page(request: Request, opp_id: int):
        from aime.stages import stage_names

        with C().db.session() as s:
            opp = _opp(s, opp_id)
            _ = opp.company, opp.lead
            history = s.execute(select(StageChange).where(StageChange.opportunity_id == opp.id)
                                .order_by(StageChange.id.desc())).scalars().all()
            quotes = s.execute(select(Quote).where(Quote.opportunity_id == opp.id).order_by(Quote.id.desc())).scalars().all()
            files = s.execute(select(Attachment).where(Attachment.opportunity_id == opp.id)
                              .order_by(Attachment.id.desc())).scalars().all()
            owners = s.execute(select(Assignment).where(Assignment.opportunity_id == opp.id)
                               .order_by(Assignment.id.desc()).limit(20)).scalars().all()
            acts = s.execute(select(Activity).where(or_(Activity.opportunity_id == opp.id,
                                                        (Activity.lead_id == opp.lead_id) & (Activity.lead_id.is_not(None))))
                             .order_by(Activity.id.desc()).limit(40)).scalars().all()
            sc = getattr(request.state, "scope", None)
            assignees = [u for u in s.execute(select(User.username).where(User.active.is_(True)).order_by(User.username)).scalars()
                         if sc is None or sc.all or u in sc.usernames]
            return render(request, "deal.html", opp=opp, history=history, quotes=quotes, files=files, owners=owners,
                          acts=acts, stages=stage_names(C().settings), assignees=assignees, allowed=", ".join(ALLOWED),
                          max_mb=MAX_BYTES // (1024 * 1024))

    @app.post("/opportunities/{opp_id}/quotes")
    def add_quote(request: Request, opp_id: int, amount: str = Form(...), currency: str = Form("USD"),
                  valid_until: str = Form(""), terms: str = Form("")):
        try:
            value = float(amount.replace(",", "").replace("$", "").strip())
            if not 0 < value < 1e9:
                raise ValueError
            until = date.fromisoformat(valid_until) if valid_until.strip() else None
        except ValueError:
            return back(f"/opportunities/{opp_id}", "Give the amount as a number (and the date as YYYY-MM-DD)")
        cur = currency.strip().upper()[:3]
        if not re.fullmatch(r"[A-Z]{3}", cur):
            return back(f"/opportunities/{opp_id}", "Currency is a 3-letter code such as USD")
        with C().db.session() as s:
            opp = _opp(s, opp_id)
            q = Quote(opportunity_id=opp.id, amount=value, currency=cur, valid_until=until,
                      terms=" ".join(terms.split())[:500] or None, created_by=who(request))
            s.add(q)
            s.flush()
            log_activity(s, f"Quote #{q.id} drafted: {cur} {value:,.2f}", who(request), type_="quote",
                         company_id=opp.company_id, lead_id=opp.lead_id, opportunity_id=opp.id)
        return back(f"/opportunities/{opp_id}", "Quote saved as a draft")

    @app.post("/opportunities/{opp_id}/quotes/{quote_id}/{action}")
    def quote_action(request: Request, opp_id: int, quote_id: int, action: str):
        from aime.stages import move, stage_names

        if action not in QUOTE_ACTIONS:
            raise HTTPException(400)
        with C().db.session() as s:
            opp = _opp(s, opp_id)
            q = s.get(Quote, quote_id)
            if q is None or q.opportunity_id != opp.id:
                raise HTTPException(404)
            q.status = QUOTE_ACTIONS[action]
            q.decided_at = utcnow() if action in ("accept", "reject", "expire") else q.decided_at
            note = f"Quote #{q.id} {q.status}: {q.currency} {q.amount:,.2f}"
            if action == "send" and opp.stage in ("new", "discovery") and "proposal" in stage_names(C().settings):
                move(s, opp, "proposal", who(request), f"quote #{q.id} sent", settings=C().settings)
            if action == "accept" and not opp.est_value:
                opp.est_value = q.amount  # the value a person agreed, not an estimate
            log_activity(s, note, who(request), type_="quote", company_id=opp.company_id, lead_id=opp.lead_id,
                         opportunity_id=opp.id)
        return back(f"/opportunities/{opp_id}", note)

    @app.post("/opportunities/{opp_id}/attachments")
    async def upload(request: Request, opp_id: int, file: Annotated[UploadFile, File()]):
        name = safe_name(file.filename or "")
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext not in ALLOWED:
            return back(f"/opportunities/{opp_id}", f"Not uploaded: allowed types are {', '.join(ALLOWED)}")
        data = await file.read(MAX_BYTES + 1)
        if not data:
            return back(f"/opportunities/{opp_id}", "Not uploaded: the file is empty")
        if len(data) > MAX_BYTES:
            return back(f"/opportunities/{opp_id}", f"Not uploaded: files are limited to {MAX_BYTES // (1024 * 1024)} MB")
        stored = f"{uuid.uuid4().hex}{ext}"
        with C().db.session() as s:
            opp = _opp(s, opp_id)
            (folder() / stored).write_bytes(data)
            a = Attachment(opportunity_id=opp.id, filename=name, stored_as=stored, content_type=ALLOWED[ext],
                           size=len(data), sha256=hashlib.sha256(data).hexdigest(), uploaded_by=who(request))
            s.add(a)
            s.flush()
            log_activity(s, f"Document added: {name} ({len(data) // 1024 or 1} KB)", who(request), type_="document",
                         company_id=opp.company_id, lead_id=opp.lead_id, opportunity_id=opp.id)
        return back(f"/opportunities/{opp_id}", f"Uploaded {name}")

    @app.get("/opportunities/{opp_id}/attachments/{att_id}")
    def download(request: Request, opp_id: int, att_id: int):
        with C().db.session() as s:
            a = s.get(Attachment, att_id)
            if a is None or a.opportunity_id != opp_id:
                raise HTTPException(404)
            path = folder() / a.stored_as
            if not path.is_file() or path.resolve().parent != folder().resolve():
                raise HTTPException(404)
            return FileResponse(path, media_type=a.content_type, filename=a.filename,
                                headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
