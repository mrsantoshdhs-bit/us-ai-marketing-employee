"""What has gone to the mail server, kept outside the database, and the hold that stops sending until a person has looked.

A database restored from a backup has forgotten every email sent after that backup: they are "approved" again and the
next run would send them twice. So every email is written to data/sent_journal.jsonl (append-only, fsync'd, like
publish_journal.jsonl) before the mail server is contacted, and that file is not part of aime.db: restoring aime.db
leaves it alone.

Events, the last one for an email counts:
- claim: the email is being handed to the mail server, so it may have been delivered from here on.
- released: it certainly was not delivered (the server refused it, or a person checked the Sent folder and requeued it).
- confirmed: a person looked after a hold; what was claimed before this is no longer evidence of a new restore.
- new_pc: this journal did not come with the database. What was approved before this moment may have gone from the old PC
  after its copy was made: each such email gets a claim marked "unknown" and goes to a person. The database carries the moment
  too (NEW_PC_KEY), so a copy that does not carry this journal's last moment was made on another PC, and is treated the same.
- started: the first line of a journal that began here. The database carries its id too (JOURNAL_KEY), for the time before any
  new_pc: a database put over a journal that has seen nothing, by a command run before it, is known by not carrying that id.

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
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aime.models import Activity, Alert, FollowUp
from aime.timeutil import utcnow

log = logging.getLogger("aime.send_journal")
HOLD_FILE = "SENDING_HOLD.json"
STARTED_KEY = "sent_journal.started"  # in the database: this database has been journalled, so the file should exist
NEW_PC_KEY = "sent_journal.new_pc"  # in the database: the new_pc moment of the journal it has lived with
JOURNAL_KEY = "sent_journal.journal"  # in the database: the started id of the journal it began with
HOLD_ALERT = "sending:hold"
ACCOUNTED_FOR = ("sending", "sent", "uncertain")  # where a claimed email can be on an ordinary day
NOT_YET_SENT = ("pending", "awaiting_approval", "approved")  # a claimed email here: this database is older than the claim
ALREADY_SENT = ("The record of sent emails says this email already went to the mail server on {at} UTC. "
                "It was not sent again: check your Sent folder, then mark sent or requeue")
UNKNOWN_SENT = "approved in this copy; whether the old PC sent it after the copy is unknown: check the Sent folder, then mark sent or requeue"
UNKNOWN_DRAFT = "a draft in this copy; whether the old PC sent it after the copy is unknown: check the Sent folder before approving it"
SEND_IN_HAND = timedelta(minutes=15)  # a "sending" row this young may not have written its claim yet
REAL_EVENTS = ("claim", "released", "confirmed")  # a journal with none of these has seen nothing: it is as good as absent
LOCK_WAIT = 5.0  # seconds; an append takes milliseconds
IN_LIST = 500  # row numbers asked for at a time: SQLite takes only so many in one statement
CONFIRM = "run `python -m aime confirm-sending`"


def journal_path(data_dir: Path) -> Path:
    return Path(data_dir) / "sent_journal.jsonl"


def hold_path(data_dir: Path) -> Path:
    return Path(data_dir) / HOLD_FILE


def _mark(address: str | None) -> str | None:
    clean = (address or "").strip().lower()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16] if clean else None


def _lock_byte(fh, on: bool) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK if on else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if on else fcntl.LOCK_UN)


@contextmanager
def _one_writer(data_dir: Path):
    """On Windows an append is a seek and a write, so two processes appending at once can overwrite each other's line (51
    of 6,000 in a tight loop). The lock is the operating system's: it ends with the process, so a crash leaves none behind.
    Raises OSError if it cannot be had in LOCK_WAIT seconds, which stops a send the way an unwritable journal does."""
    with (Path(data_dir) / "sent_journal.lock").open("a+b") as fh:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                _lock_byte(fh, True)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        try:
            yield
        finally:
            _lock_byte(fh, False)


def _append(data_dir: Path, *records: dict) -> None:
    path = journal_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in records)
    with _one_writer(data_dir), path.open("a+b") as fh:
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


def record_new_pc(data_dir: Path) -> str:
    """From now on this journal knows what goes out. Returns the moment, for what_was_approved_before."""
    at = utcnow().isoformat(timespec="seconds")
    _append(data_dir, {"event": "new_pc", "at": at})
    return at


def take_from_copy(data_dir: Path, copied: Path) -> None:
    """The journal of a copy, for a PC that has none, with its new_pc line: whole or not at all. A journal with claims and no such line reads as
    this PC's own, so a restore stopped here and run again would forget what it is and let the copy's approved emails go."""
    path = journal_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    held = copied.read_bytes()
    if held and not held.endswith(b"\n"):  # a power cut cut its last line short: it must not swallow this one
        held += b"\n"
    text = held + (json.dumps({"event": "new_pc", "at": utcnow().isoformat(timespec="seconds")}) + "\n").encode("utf-8")
    partial = path.with_name(path.name + ".partial")
    with _one_writer(data_dir):
        try:
            with partial.open("wb") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            partial.replace(path)
        except OSError:
            partial.unlink(missing_ok=True)
            raise


def record_unknown(data_dir: Path, rows: list[tuple[int, str | None, str | None]]) -> None:
    """(follow-up, address, message id) of emails that may have gone out from a PC this journal never heard from."""
    if rows:
        _append(data_dir, *[{**_record("claim", i, to, mid), "unknown": True} for i, to, mid in rows])


def record_known(data_dir: Path, rows: list[tuple[int, str | None, str | None, str | None]]) -> None:
    """(follow-up, address, message id, when) of emails a copy says went out, for a journal that never saw them."""
    if rows:
        _append(data_dir, *[{**_record("claim", i, to, mid), **({"at": str(at).replace(" ", "T")[:19]} if at else {})} for i, to, mid, at in rows])


def looks_new(data_dir: Path) -> bool:
    """Absent, or nothing but a "started" line: this journal has never seen an email, so it did not come with the database."""
    path = journal_path(data_dir)
    if not path.exists():
        return True
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            if json.loads(line).get("event") in REAL_EVENTS:
                return False
        except (ValueError, AttributeError):
            continue
    return True


@dataclass
class View:
    claims: dict[tuple[int, str | None], dict] = field(default_factory=dict)  # (follow-up, address hash): the last claim
    by_message_id: dict[str, dict] = field(default_factory=dict)
    since_confirmed: dict[tuple[int, str | None], dict] = field(default_factory=dict)  # claims made since a person last looked
    accepted_damage: str | None = None  # the integrity result a person last looked at
    new_pc_at: str | None = None  # the last time this journal started on a PC without the old one's record
    started: str | None = None  # the id of its first "started" line (a journal from before ids: that line's date)


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
        elif event == "new_pc":
            view.new_pc_at = rec.get("at")
        elif event == "started":
            view.started = view.started or rec.get("id") or rec.get("at")
    return view


def _address(f: FollowUp) -> str | None:
    return f.to_email or (f.contact.email if f.contact else None)


def claim_for(view: View, f: FollowUp) -> dict | None:
    """The journal's record of this email going out, if the last word on it is that it did. A record with no address
    is from a row erased on request: its number is all that is left to match on."""
    return claim_of(view, f.id, _address(f), f.message_id)


def claim_of(view: View, followup_id: int, address: str | None, message_id: str | None) -> dict | None:
    return (view.claims.get((followup_id, _mark(address))) or view.claims.get((followup_id, None))
            or (view.by_message_id.get(message_id) if message_id else None))


def approved_before(view: View, at: str | None, rows) -> list[tuple[int, str | None, str | None]]:
    """Of (id, address, message id, approved at) rows: those approved before `at` (with None, all of them: a date from
    another PC proves nothing) that nothing in the journal names yet."""
    def stamp(when) -> str:
        return when.isoformat() if hasattr(when, "isoformat") else str(when or "").replace(" ", "T")

    return [(i, to, mid) for i, to, mid, when in rows if (at is None or stamp(when) < at) and not claim_of(view, i, to, mid)]


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
    ids = sorted({i for i, _ in view.since_confirmed})
    rows = {}
    for start in range(0, len(ids), IN_LIST):
        rows.update({i: (st, to) for i, st, to in session.execute(select(FollowUp.id, FollowUp.status, FollowUp.to_email).where(
            FollowUp.id.in_(ids[start:start + IN_LIST])))})
    newest = session.execute(select(func.max(FollowUp.id))).scalar() or 0
    found = []
    for rec in view.since_confirmed.values():
        row = rows.get(rec["followup_id"])
        if row is None:
            if rec["followup_id"] > newest:
                found.append(rec)
        elif row[1] and rec.get("to") and _mark(row[1]) != rec["to"]:
            found.append(rec)
        elif row[0] in NOT_YET_SENT and not _requeued_since(session, rec):
            found.append(rec)
    return found


def _requeued_since(session: Session, rec: dict) -> bool:
    """A requeue on record after the claim: this database is newer than the claim, so it was not restored. Code from before
    this journal writes no "released" (a rollback and forward again). The send gate still gives that email to a person."""
    when = session.execute(select(func.max(Activity.created_at)).where(
        Activity.subject == f"Follow-up #{rec['followup_id']} requeue")).scalar()
    return when is not None and when.isoformat(timespec="seconds") >= str(rec.get("at"))


def mark_for_a_person(session: Session, f: FollowUp, rec: dict, source: str) -> None:
    """Not sent again. An approved email goes to a person; a draft says so, for whoever opens it next. A claim marked
    unknown never says the email went: nobody here knows."""
    f.blocked_reason = UNKNOWN_SENT if rec.get("unknown") else ALREADY_SENT.format(at=rec["at"][:16].replace("T", " "))[:300]
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


def note_drafts(session: Session, view: View, at: str | None) -> int:
    """A draft of the copy may have been approved and sent by the old PC afterwards. It stays a draft, with a note for whoever
    opens it. With `at` only those made before it, with None all of them. Returns how many."""
    noted = 0
    for f in session.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status.in_(("pending", "awaiting_approval")))).scalars():
        if (at is None or f.created_at.isoformat() < at) and not claim_for(view, f):
            f.blocked_reason = UNKNOWN_DRAFT
            noted += 1
    return noted


def not_covered(session: Session, view: View) -> list:
    """Emails that may have gone out with no claim in the journal: it has lost lines. Those a new-PC restore sent to a person
    stay out of this: their status protects them, and they were never known to have gone."""
    claimed = {i for i, _ in view.claims}
    young = utcnow() - SEND_IN_HAND
    return [row for row in session.execute(select(FollowUp.id, FollowUp.to_email, FollowUp.message_id, FollowUp.sent_at, FollowUp.send_started_at,
                                                  FollowUp.status, FollowUp.blocked_reason)
                                           .where(FollowUp.channel == "email", FollowUp.status.in_(ACCOUNTED_FOR))).all()
            if row.id not in claimed and row.blocked_reason != UNKNOWN_SENT
            and not (row.status == "sending" and row.send_started_at and row.send_started_at > young)]


def why_held(ctx, session: Session, check_integrity: bool, source: str = "sales_workflow") -> str | None:
    """Why nothing may go out now, finding a new reason if there is one; None on an ordinary day."""
    from aime.backup import integrity_check
    from aime.monitoring import alert_once

    settings = ctx.settings
    try:
        view = read(settings.data_dir)
    except OSError as exc:
        return f"Waiting: the record of sent emails cannot be read just now ({type(exc).__name__}); it is tried again on the next run"
    reasons: list[tuple[str, str]] = []
    waiting = None
    evidence = restore_evidence(session, view)
    if evidence:
        marked = review_rows(session, view, source)
        reasons.append(("restored", f"this database is older than the record of sent emails ({len(evidence)} email(s) went out "
                        f"that it does not know; {len(marked)} of its open emails already went out), so it was restored"))
    if check_integrity:
        check = integrity_check(ctx.db)
        if check.get("passing"):  # busy, or an I/O error: no finding about the file, so no hold and no alert
            waiting = f"Waiting: the database could not be checked just now ({str(check['integrity'])[:100]}); it is tried again on the next run"
        elif check["integrity"] != "ok" and check["integrity"] != view.accepted_damage:
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
        return waiting
    message = held_message(info)
    alert_once(session, "error", "recovery", message, settings, key=HOLD_ALERT)
    return message


# ---------------------------------------------------------------------------------------- start-up
def start(db, settings) -> None:
    """On every start. The first time, the journal is started from what the database says was sent, so emails sent
    before this code existed are protected too. A journal that was started and is now gone or empty means this database
    came without it (a new PC, a copied folder), so what went out since its backup is unknown: sending is held, and every
    approved email goes to a person. So too for a database that does not carry what names this journal (its last new_pc moment,
    or while it has seen nothing its started id): it was put here by hand from another PC. One that has lost lines gets back
    what the database knows."""
    from aime.db import kv_get, kv_set

    path = journal_path(settings.data_dir)
    try:
        with db.session() as s:
            started = kv_get(s, STARTED_KEY)  # read before looking for the file: another process writes the file first
            exists = path.exists() and path.stat().st_size > 0
            if started and exists:
                if _not_this_journals(s, settings.data_dir, read(settings.data_dir)):
                    _hold_for_another_pc(s, settings)
                _put_back_lost_claims(s, settings)
                return
            if not exists:
                if started:
                    set_hold(settings.data_dir, "restored", "the record of sent emails is missing, so what went out after "
                             "this database's backup is unknown: check the Sent folder")
                rows = s.execute(select(FollowUp.id, FollowUp.to_email, FollowUp.message_id, FollowUp.sent_at, FollowUp.send_started_at)
                                 .where(FollowUp.channel == "email", FollowUp.status.in_(ACCOUNTED_FOR))).all()
                _append(settings.data_dir, {"event": "started", "id": uuid.uuid4().hex, "at": utcnow().isoformat(timespec="seconds")},
                        *[_record("claim", i, to, mid, sent or started) for i, to, mid, sent, started in rows])
                if started:
                    _came_without_its_journal(s, settings)
            elif not started:  # a database new to this journal, with nothing to say where it was
                view = read(settings.data_dir)
                unnamed = [row for row in _approved(s) if not claim_of(view, *row)]  # none on a first start that lost a race
                if unnamed and _not_this_journals(s, settings.data_dir, view):
                    _hold_for_another_pc(s, settings)
                else:  # they belong together from now on
                    kv_set(s, NEW_PC_KEY, view.new_pc_at)
            kv_set(s, JOURNAL_KEY, read(settings.data_dir).started)
            kv_set(s, STARTED_KEY, True)
    except (OSError, SQLAlchemyError):  # the send gate reports what matters; this must never stop a start
        log.exception("Could not start the record of sent emails")


def _not_this_journals(session: Session, data_dir: Path, view: View) -> bool:
    """A journal that has seen an email and has no new_pc moment proves nothing here: a database from before these keys has
    neither, and the journal's claims are what the send gates read. One that has seen nothing has only this to go on."""
    from aime.db import kv_get

    if view.new_pc_at:
        return kv_get(session, NEW_PC_KEY) != view.new_pc_at
    return looks_new(data_dir) and kv_get(session, JOURNAL_KEY) != view.started


def _hold_for_another_pc(session: Session, settings) -> None:
    set_hold(settings.data_dir, "restored", "this database was made on another PC, so what went out after "
             "its copy is unknown: check the Sent folder")
    _came_without_its_journal(session, settings)


def _approved(session: Session) -> list[tuple[int, str | None, str | None]]:
    from aime.models import Contact

    return [tuple(row) for row in session.execute(select(FollowUp.id, func.coalesce(FollowUp.to_email, Contact.email), FollowUp.message_id)
                                                  .outerjoin(Contact, Contact.id == FollowUp.contact_id)
                                                  .where(FollowUp.channel == "email", FollowUp.status == "approved"))]


def _write_back(settings, rows: list) -> None:
    _append(settings.data_dir, *[_record("claim", r.id, r.to_email, r.message_id, r.sent_at or r.send_started_at) for r in rows])


def _came_without_its_journal(session: Session, settings) -> None:
    from aime.db import kv_set

    kv_set(session, NEW_PC_KEY, record_new_pc(settings.data_dir))
    gone = not_covered(session, read(settings.data_dir))  # this journal never saw them: nothing cut it, so no alarm
    if gone:
        _write_back(settings, gone)
    record_unknown(settings.data_dir, _approved(session))
    view = read(settings.data_dir)
    review_rows(session, view, "recovery")
    note_drafts(session, view, None)


def _put_back_lost_claims(session: Session, settings) -> None:
    """Only ever adds claims. A person hears of it: something outside this program cut the file."""
    from aime.monitoring import alert_once

    lost = not_covered(session, read(settings.data_dir))
    if lost:
        _write_back(settings, lost)
        alert_once(session, "error", "recovery", f"The record of sent emails ({journal_path(settings.data_dir).name}) had lost "
                   f"{len(lost)} email(s) the database knows went out. They were written back. Check what else touched that file.", settings)
