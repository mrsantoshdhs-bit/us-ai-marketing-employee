"""A small durable work queue in the SQLite database (Option B Phase 8): chunked work that takes several ticks.

- enqueue: idempotent per (kind, key). A pending or claimed item is left as it is (its place in the queue kept); a done
  one is made pending again (the work is due again); a dead one stays dead until a person revives it.
- claim: up to `limit` available items of one kind, by priority then age, each with a lease. Claiming counts an
  attempt; an item already claimed `max_attempts` times without finishing goes to the dead letters instead. One
  worker per kind at a time (`one_worker`): two runs never read the same sources in parallel.
- complete / fail / release: done with a result; failed, retried after an exponential backoff, dead after
  `max_attempts`; or handed back untouched (the run stopped early), without counting the attempt.
- block / unblock / cancel / revive: a person's hands. A blocked item is held (never claimed) until unblocked; a
  cancelled one is closed for good, and enqueueing the same work again leaves it closed; a dead one is retried.
- health: what the monitor job alerts on: dead letters, a lease whose worker stopped, a queue that does not move.

States, in the roadmap's words (`state_of`): PENDING (pending), RETRYING (pending again after a failure, waiting out
its backoff), RUNNING (claimed), COMPLETED (done), FAILED (dead: the dead letters), BLOCKED (blocked), CANCELLED
(cancelled).

Every call works inside the caller's session and commits nothing itself, so the work and its queue item change in
one transaction. The queue holds no secrets and contacts no one.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aime.db import take_write_lock
from aime.models import WorkItem
from aime.timeutil import utcnow

LEASE = timedelta(minutes=45)  # longer than a job's 30-minute budget: a slow run keeps its items
MAX_ATTEMPTS = 5
BACKOFF = timedelta(minutes=30)
MAX_BACKOFF = timedelta(hours=24)
STALLED = timedelta(hours=48)  # work available this long and never taken: the queue is not moving
STATES = {"pending": "PENDING", "claimed": "RUNNING", "done": "COMPLETED", "dead": "FAILED", "blocked": "BLOCKED",
          "cancelled": "CANCELLED"}
CLOSED = ("done", "dead", "cancelled")


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:80]


def enqueue(session: Session, kind: str, key: str, payload: dict | None = None, priority: int = 0,
            available_at: datetime | None = None, now: datetime | None = None, reprioritize: bool = False) -> bool:
    """Add the work unless it is already waiting or being done. Returns True when an item was added or made pending again.
    `reprioritize`: a waiting item takes the new priority and payload (a fresh scan has ranked the work again)."""
    now = now or utcnow()
    values = dict(kind=kind, key=str(key)[:200], payload=payload or {}, priority=priority, status="pending", attempts=0,
                  available_at=available_at or now, created_at=now, updated_at=now)
    stmt = sqlite_insert(WorkItem).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["kind", "key"],
        set_={"status": "pending", "attempts": 0, "payload": stmt.excluded.payload, "priority": stmt.excluded.priority,
              "available_at": stmt.excluded.available_at, "locked_by": None, "locked_at": None, "lease_until": None,
              "result": None, "last_error": None, "finished_at": None, "updated_at": now},
        where=WorkItem.status == "done")
    if session.execute(stmt).rowcount:
        return True
    if reprioritize:
        session.execute(update(WorkItem).where(WorkItem.kind == kind, WorkItem.key == values["key"], WorkItem.status == "pending")
                        .values(priority=priority, payload=values["payload"], updated_at=now))
    return False


def _available(kind: str, now: datetime, dead_holders: list[str] = ()):
    expired = and_(WorkItem.status == "claimed", or_(WorkItem.lease_until < now, WorkItem.locked_by.in_(list(dead_holders) or [""])))
    return and_(WorkItem.kind == kind, or_(and_(WorkItem.status == "pending", WorkItem.available_at <= now), expired))


def _holder_alive(worker: str | None) -> bool:
    """A lease's holder still runs: always true for another PC's worker (it cannot be told from here)."""
    host, _, pid = (worker or "").rpartition(":")
    if host != socket.gethostname() or not pid.isdigit():
        return True
    from aime.scheduler.runner import pid_alive

    return pid_alive(int(pid))


def busy(session: Session, kind: str, worker: str, now: datetime | None = None) -> bool:
    """Another live worker holds a lease on this kind. A lease left by a process that has ended does not count."""
    now = now or utcnow()
    holders = session.execute(select(WorkItem.locked_by).where(WorkItem.kind == kind, WorkItem.status == "claimed", WorkItem.lease_until >= now,
                                                               WorkItem.locked_by != worker).distinct()).scalars().all()
    return any(_holder_alive(h) for h in holders)


def claim(session: Session, kind: str, limit: int, worker: str | None = None, lease: timedelta = LEASE,
          max_attempts: int = MAX_ATTEMPTS, one_worker: bool = True, now: datetime | None = None) -> list[WorkItem]:
    """Claim up to `limit` items, in order. Takes the database's write lock first, so two workers never claim the same item."""
    now = now or utcnow()
    worker = worker or worker_id()
    if limit <= 0:
        return []
    take_write_lock(session)
    if one_worker and busy(session, kind, worker, now):
        return []
    dead = [h for h in session.execute(select(WorkItem.locked_by).where(WorkItem.kind == kind, WorkItem.status == "claimed",
                                                                        WorkItem.locked_by != worker).distinct()).scalars()
            if not _holder_alive(h)]
    out: list[WorkItem] = []
    while len(out) < limit:
        rows = session.execute(select(WorkItem).where(_available(kind, now, dead), WorkItem.id.not_in([w.id for w in out] or [-1]))
                               .order_by(WorkItem.priority, WorkItem.id).limit(limit - len(out))).scalars().all()
        if not rows:
            break
        for item in rows:
            if item.attempts >= max_attempts:  # claimed that often and never finished: a person looks at it
                item.status, item.locked_by, item.lease_until, item.finished_at = "dead", None, None, now
                item.last_error = (item.last_error or f"claimed {item.attempts} times without finishing")[:500]
                continue
            item.status, item.locked_by, item.locked_at, item.lease_until = "claimed", worker, now, now + lease
            item.attempts += 1
            out.append(item)
    session.flush()
    return out


def _mine(session: Session, item_id: int, worker: str) -> WorkItem | None:
    item = session.get(WorkItem, item_id)
    return item if item is not None and item.status == "claimed" and item.locked_by == worker else None


def complete(session: Session, item_id: int, worker: str, result: dict | None = None, now: datetime | None = None) -> bool:
    item = _mine(session, item_id, worker)
    if item is None:  # the lease ended and another worker has it now: that one records the outcome
        return False
    item.status, item.result, item.finished_at = "done", result or {}, now or utcnow()
    item.locked_by = item.locked_at = item.lease_until = None
    return True


def retry_delay(attempts: int, base: timedelta = BACKOFF) -> timedelta:
    return min(MAX_BACKOFF, base * (2 ** max(0, attempts - 1)))


def fail(session: Session, item_id: int, worker: str, error: str, max_attempts: int = MAX_ATTEMPTS, base: timedelta = BACKOFF,
         priority: int | None = None, now: datetime | None = None) -> str | None:
    """Record a failure: pending again after the backoff, or dead after `max_attempts`. Returns the new status."""
    now = now or utcnow()
    item = _mine(session, item_id, worker)
    if item is None:
        return None
    item.last_error = (error or "failed")[:500]
    item.locked_by = item.locked_at = item.lease_until = None
    if item.attempts >= max_attempts:
        item.status, item.finished_at = "dead", now
    else:
        item.status, item.available_at = "pending", now + retry_delay(item.attempts, base)
        if priority is not None:
            item.priority = priority
    return item.status


def release(session: Session, item_ids: list[int], worker: str) -> int:
    """Hand claimed items back untouched: they keep their place and the attempt is not counted."""
    if not item_ids:
        return 0
    return session.execute(update(WorkItem).where(WorkItem.id.in_(item_ids), WorkItem.status == "claimed", WorkItem.locked_by == worker)
                           .values(status="pending", attempts=func.max(0, WorkItem.attempts - 1), locked_by=None, locked_at=None,
                                   lease_until=None)).rowcount


def revive(session: Session, item_id: int) -> bool:
    """A person's retry of a dead item."""
    item = session.get(WorkItem, item_id)
    if item is None or item.status != "dead":
        return False
    item.status, item.attempts, item.available_at, item.finished_at = "pending", 0, utcnow(), None
    return True


def block(session: Session, item_id: int, reason: str, now: datetime | None = None) -> bool:
    """Hold a waiting item: it is not claimed until unblocked. A running one is left to finish."""
    item = session.get(WorkItem, item_id)
    if item is None or item.status != "pending":
        return False
    item.status, item.last_error, item.updated_at = "blocked", f"blocked: {reason}"[:500], now or utcnow()
    return True


def unblock(session: Session, item_id: int, now: datetime | None = None) -> bool:
    item = session.get(WorkItem, item_id)
    if item is None or item.status != "blocked":
        return False
    now = now or utcnow()
    item.status, item.available_at, item.last_error, item.updated_at = "pending", now, None, now
    return True


def cancel(session: Session, item_id: int, reason: str, now: datetime | None = None) -> bool:
    """Close an item that is not finished. A worker still holding it records nothing afterwards (its lease is gone)."""
    item = session.get(WorkItem, item_id)
    if item is None or item.status in ("done", "cancelled"):
        return False
    now = now or utcnow()
    item.status, item.finished_at, item.last_error = "cancelled", now, f"cancelled: {reason}"[:500]
    item.locked_by = item.locked_at = item.lease_until = None
    return True


def state_of(item: WorkItem) -> str:
    """The item's state in the roadmap's words."""
    if item.status == "pending" and item.attempts and item.last_error:
        return "RETRYING"
    return STATES.get(item.status, item.status.upper())


def pending_count(session: Session, kind: str) -> int:
    return session.execute(select(func.count(WorkItem.id)).where(WorkItem.kind == kind, WorkItem.status.in_(("pending", "claimed")))).scalar_one()


def status(session: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """Per kind: how many items wait, are being worked, are done and dead; the next one available; live leases."""
    now = now or utcnow()
    rows = session.execute(select(WorkItem.kind, WorkItem.status, func.count(WorkItem.id), func.min(WorkItem.available_at),
                                  func.max(WorkItem.finished_at)).group_by(WorkItem.kind, WorkItem.status)).all()
    kinds: dict[str, dict[str, Any]] = {}
    for kind, st, n, first_at, last_done in rows:
        k = kinds.setdefault(kind, {"kind": kind, "pending": 0, "claimed": 0, "done": 0, "dead": 0, "blocked": 0, "cancelled": 0,
                                    "retrying": 0, "stuck": 0, "next_at": None, "last_done_at": None})
        k[st] = n
        if st == "pending":
            k["next_at"] = first_at
        if st == "done":
            k["last_done_at"] = last_done
    for k in kinds.values():
        k["leased_by"] = session.execute(select(WorkItem.locked_by).where(WorkItem.kind == k["kind"], WorkItem.status == "claimed",
                                                                         WorkItem.lease_until >= now).distinct()).scalars().all()
        k["retrying"] = session.execute(select(func.count(WorkItem.id)).where(
            WorkItem.kind == k["kind"], WorkItem.status == "pending", WorkItem.attempts > 0, WorkItem.last_error.is_not(None))).scalar_one()
        k["stuck"] = session.execute(select(func.count(WorkItem.id)).where(
            WorkItem.kind == k["kind"], WorkItem.status == "claimed", WorkItem.lease_until < now)).scalar_one()
    return sorted(kinds.values(), key=lambda k: k["kind"])


def health(session: Session, queue_on: dict[str, bool] | None = None, now: datetime | None = None) -> list[tuple[str, str]]:
    """(alert key, message) for each problem: dead letters, leases whose worker stopped, and, for a kind whose queue is on
    (`queue_on`; a kind not named counts as on), work that has been available for STALLED without anyone taking it."""
    now = now or utcnow()
    out: list[tuple[str, str]] = []
    for k in status(session, now):
        kind = k["kind"]
        if k["dead"]:
            out.append((f"queue_dead:{kind}", f"Work queue '{kind}': {k['dead']} item(s) failed {MAX_ATTEMPTS} times and wait for a "
                                              f"person (dead letters; `aime queue status --kind {kind}`)"))
        if k["stuck"]:
            out.append((f"queue_stuck:{kind}", f"Work queue '{kind}': {k['stuck']} item(s) held past their lease (the worker "
                                               "stopped); the next run takes them again"))
        if (queue_on or {}).get(kind, True) and k["pending"]:
            oldest = session.execute(select(func.min(WorkItem.available_at)).where(WorkItem.kind == kind,
                                                                                   WorkItem.status == "pending")).scalar_one()
            if oldest is not None and now - oldest > STALLED:
                out.append((f"queue_stalled:{kind}", f"Work queue '{kind}': work has waited since {oldest:%Y-%m-%d %H:%M} UTC "
                                                     "without being taken"))
    return out
