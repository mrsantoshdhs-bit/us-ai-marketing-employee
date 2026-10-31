"""What has gone to the mail server, kept outside the database, and the hold that stops sending until a person has looked.

A database restored from a backup has forgotten every email sent after that backup: they are "approved" again and the
next run would send them twice. So every email is written to data/sent_journal.jsonl (append-only, fsync'd, like
publish_journal.jsonl) before the mail server is contacted, and that file is not part of aime.db: restoring aime.db
leaves it alone.

Events, the last one for an email counts:
- claim: the email is being handed to the mail server, so it may have been delivered from here on.
- released: it certainly was not delivered (the server refused it, or a person checked the Sent folder and requeued it).
- confirmed: a person looked after a hold; what was claimed before this is no longer evidence of a new restore.

No address is stored, only a short hash: the journal outlives an erasure request, and must not hold the person's address.

Two gates read it. An approved email with a claim is not sent again and goes to a person ("uncertain": check the Sent
folder, then mark sent or requeue). A claim the database knows nothing about means the database is older than the journal:
it was restored, so sending is held (data/SENDING_HOLD.json, also outside the database) until `aime confirm-sending`.
On an ordinary day neither can fire: a claim's row is "sending", "sent" or "uncertain", and every way back to "approved"
writes "released" first. A failed integrity check holds sending the same way.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aime.models import Alert, FollowUp
from aime.timeutil import utcnow

log = logging.getLogger("aime.send_journal")
HOLD_FILE = "SENDING_HOLD.json"
STARTED_KEY = "sent_journal.started"  # in the database: this database has been journalled, so the file should exist
HOLD_ALERT = "sending:hold"
ACCOUNTED_FOR = ("sending", "sent", "uncertain")  # where a claimed email can be on an ordinary day
NOT_YET_SENT = ("pending", "awaiting_approval", "approved")  # a claimed email here: this database is older than the claim
ALREADY_SENT = ("This email already went to the mail server on {at} UTC, after the backup this database was restored from. "
                "It was not sent again: check your Sent folder, then mark sent or requeue")
CONFIRM = "run `python -m aime confirm-sending`"


def journal_path(data_dir: Path) -> Path:
    return Path(data_dir) / "sent_journal.jsonl"


def hold_path(data_dir: Path) -> Path:
    return Path(data_dir) / HOLD_FILE


def _mark(address: str | None) -> str | None:
    clean = (address or "").strip().lower()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16] if clean else None


def _append(data_dir: Path, *records: dict) -> None:
    path = journal_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in records)
    with path.open("a+b") as fh:
        if fh.seek(0, os.SEEK_END):
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":  # a power cut cut the last line short: it must not swallow this one
                text = "\n" + text
        fh.write(text.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


def _record(event: str, followup_id: int, to: str | None, message_id: str | None, at=None) -> dict:
    return {"event": event, "followup_id": followup_id, "to": _mark(to), "message_id": message_id,
            "at": (at or utcnow()).isoformat(timespec="seconds")}


def record_claim(data_dir: Path, followup_id: int, to: str | None, message_id: str | None) -> None:
    _append(data_dir, _record("claim", followup_id, to, message_id))


def record_released(data_dir: Path, followup_id: int, to: str | None) -> None:
    _append(data_dir, _record("released", followup_id, to, None))


def record_confirmed(data_dir: Path, who: str, integrity: str | None = None) -> None:
    _append(data_dir, {"event": "confirmed", "who": who, "integrity": integrity, "at": utcnow().isoformat(timespec="seconds")})


@dataclass
class View:
    claims: dict[tuple[int, str | None], dict] = field(default_factory=dict)  # (follow-up, address hash): the last claim
    by_message_id: dict[str, dict] = field(default_factory=dict)
    since_confirmed: dict[tuple[int, str | None], dict] = field(default_factory=dict)  # claims made since a person last looked
    accepted_damage: str | None = None  # the integrity result a person last looked at


def read(data_dir: Path) -> View:
    view = View()
    path = journal_path(data_dir)
    if not path.exists():
        return view
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
            event, key = rec.get("event"), (rec.get("followup_id"), rec.get("to"))
        except (ValueError, AttributeError):
            continue  # a torn line from a crash is ignored
        if event == "claim":
            view.claims[key] = rec
            view.since_confirmed[key] = rec
            if rec.get("message_id"):
                view.by_message_id[rec["message_id"]] = rec
        elif event == "released":
            gone = view.claims.pop(key, None)
            view.since_confirmed.pop(key, None)
            if gone and gone.get("message_id"):
                view.by_message_id.pop(gone["message_id"], None)
        elif event == "confirmed":
            view.since_confirmed, view.accepted_damage = {}, rec.get("integrity")
    return view


def _address(f: FollowUp) -> str | None:
    return f.to_email or (f.contact.email if f.contact else None)


def claim_for(view: View, f: FollowUp) -> dict | None:
    """The journal's record of this email going out, if the last word on it is that it did. A record with no address
    is from a row erased on request: its number is all that is left to match on."""
    return (view.claims.get((f.id, _mark(_address(f)))) or view.claims.get((f.id, None))
            or (view.by_message_id.get(f.message_id) if f.message_id else None))


# ---------------------------------------------------------------------------------------- the hold
def hold(data_dir: Path) -> dict | None:
    """The hold in force, if any. A file that cannot be read still holds: only a person ends it."""
    path = hold_path(data_dir)
    if not path.exists():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        return info if isinstance(info, dict) and info.get("reason") else {"kind": "unknown", "reason": "the hold file is unreadable"}
    except (OSError, ValueError):
        return {"kind": "unknown", "reason": "the hold file is unreadable"}


def set_hold(data_dir: Path, kind: str, reason: str) -> dict:
    """The first reason stands; a later one is added to it. Written whole or not at all."""
    current = hold(data_dir)
    if current and reason in current["reason"]:
        return current
    info = {"kind": kind, "reason": f"{current['reason']}; {reason}"[:1000] if current else reason[:1000],
            "since": (current or {}).get("since") or utcnow().isoformat(timespec="seconds")}
    path = hold_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(info), encoding="utf-8")
    partial.replace(path)
    return info


def clear_hold(data_dir: Path) -> bool:
    try:
        hold_path(data_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def held_message(info: dict) -> str:
    return f"ON HOLD: {info['reason']}. Nothing is sent until a person has looked: {CONFIRM}"


def row_message() -> str:
    return f"On hold: nothing is sent until a person has looked; see the alert, then {CONFIRM}"


# ---------------------------------------------------------------------------------------- finding a restored database
def restore_evidence(session: Session, view: View) -> list[dict]:
    """Claims the database cannot account for. A row may be gone only if its number is beyond the newest row: nothing
    deletes follow-ups, but an erased or merged record must never be able to stop sending, so a hole counts for nothing."""
    if not view.since_confirmed:
        return []
    ids = {i for i, _ in view.since_confirmed}
    rows = {i: (st, to) for i, st, to in session.execute(select(FollowUp.id, FollowUp.status, FollowUp.to_email)
                                                         .where(FollowUp.id.in_(ids)))}
    newest = session.execute(select(func.max(FollowUp.id))).scalar() or 0
    found = []
    for rec in view.since_confirmed.values():
        row = rows.get(rec["followup_id"])
        if row is None:
            if rec["followup_id"] > newest:
                found.append(rec)
        elif row[0] in NOT_YET_SENT or (row[1] and rec.get("to") and _mark(row[1]) != rec["to"]):
            found.append(rec)
    return found


def mark_for_a_person(session: Session, f: FollowUp, rec: dict, source: str) -> None:
    """Not sent again. An approved email goes to a person; a draft says so, for whoever opens it next."""
    f.blocked_reason = ALREADY_SENT.format(at=rec["at"][:16].replace("T", " "))[:300]
    if f.status == "approved":
        f.status = "uncertain"
        session.add(Alert(level="error", source=source, message=f"Follow-up #{f.id}: {f.blocked_reason}"[:500]))


def review_rows(session: Session, view: View, source: str) -> list[int]:
    """Every email still open in this database that the journal says went out. Returns their ids."""
    marked = []
    for f in session.execute(select(FollowUp).where(FollowUp.channel == "email",
                                                    FollowUp.status.in_(NOT_YET_SENT))).scalars():
        rec = claim_for(view, f)
        if rec:
            mark_for_a_person(session, f, rec, source)
            marked.append(f.id)
    return marked


def why_held(ctx, session: Session, check_integrity: bool, source: str = "sales_workflow") -> str | None:
    """Why nothing may go out now, finding a new reason if there is one; None on an ordinary day."""
    from aime.backup import integrity_check
    from aime.monitoring import alert_once

    settings = ctx.settings
    try:
        view = read(settings.data_dir)
    except OSError as exc:
        return f"ON HOLD: the record of sent emails cannot be read ({type(exc).__name__}); it is tried again on the next run"
    reasons: list[tuple[str, str]] = []
    evidence = restore_evidence(session, view)
    if evidence:
        marked = review_rows(session, view, source)
        reasons.append(("restored", f"this database is older than the record of sent emails ({len(evidence)} email(s) went out "
                        f"that it does not know; {len(marked)} of its open emails already went out), so it was restored"))
    if check_integrity:
        check = integrity_check(ctx.db)
        if check["integrity"] != "ok" and check["integrity"] != view.accepted_damage:
            reasons.append(("integrity", "the database failed its integrity check, so its opt-outs and its record of what "
                            f"was sent cannot be trusted ({str(check['integrity'])[:120]})"))
    info = hold(settings.data_dir)
    for kind, reason in reasons:
        try:
            info = set_hold(settings.data_dir, kind, reason)
        except OSError:  # cannot be written (a full disk): it still holds this run, and is found again on the next
            log.exception("Could not write the sending hold")
            info = {"kind": kind, "reason": reason}
    if info is None:
        return None
    message = held_message(info)
    alert_once(session, "error", "recovery", message, settings, key=HOLD_ALERT)
    return message


# ---------------------------------------------------------------------------------------- start-up
def start(db, settings) -> None:
    """On every start. The first time, the journal is started from what the database says was sent, so emails sent
    before this code existed are protected too. A journal that was started and is now gone means this database came
    without it (a new PC, a copied folder), so what went out since its backup is unknown: sending is held."""
    from aime.db import kv_get, kv_set

    path = journal_path(settings.data_dir)
    try:
        with db.session() as s:
            started = kv_get(s, STARTED_KEY)  # read before looking for the file: another process writes the file first
            exists = path.exists()
            if started and exists:
                return
            if not exists:
                if started:
                    set_hold(settings.data_dir, "restored", "the record of sent emails is missing, so what went out after "
                             "this database's backup is unknown: check the Sent folder")
                rows = s.execute(select(FollowUp.id, FollowUp.to_email, FollowUp.message_id, FollowUp.sent_at, FollowUp.send_started_at)
                                 .where(FollowUp.channel == "email", FollowUp.status.in_(ACCOUNTED_FOR))).all()
                _append(settings.data_dir, {"event": "started", "at": utcnow().isoformat(timespec="seconds")},
                        *[_record("claim", i, to, mid, sent or started) for i, to, mid, sent, started in rows])
            kv_set(s, STARTED_KEY, True)
    except (OSError, SQLAlchemyError):  # the send gate reports what matters; this must never stop a start
        log.exception("Could not start the record of sent emails")
