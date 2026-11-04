"""The work queue as the roadmap names it: its seven states, a person's hands on an item (block, unblock, cancel, revive,
audited), recovery when the worker's process or the PC stopped, no item ever taken twice by two workers at once, and the
monitor's alerts for dead letters, abandoned leases and a queue that does not move. The queue contacts no one."""

from __future__ import annotations

import socket
import threading
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from aime import admin_settings, work_queue
from aime.cli import cmd_queue
from aime.models import Alert, AuditEvent, WorkItem
from aime.monitoring import check_health
from aime.timeutil import utcnow

W1, W2 = "pc-a:101", "pc-b:202"  # workers on other PCs: never judged dead from here


def _item(s, kind, key):
    return s.execute(select(WorkItem).where(WorkItem.kind == kind, WorkItem.key == key)).scalar_one()


def test_every_state_of_the_roadmap_is_reached_and_named(ctx):
    now = utcnow()
    with ctx.db.session() as s:
        for key in ("p", "r", "run", "ok", "dead", "b", "c"):
            work_queue.enqueue(s, "k", key, now=now)
        names = {}
        claimed = {i.key: i for i in work_queue.claim(s, "k", 7, W1, now=now)}
        work_queue.complete(s, claimed["ok"].id, W1)
        work_queue.fail(s, claimed["r"].id, W1, "timeout", now=now)
        work_queue.fail(s, claimed["dead"].id, W1, "boom", max_attempts=1, now=now)
        work_queue.release(s, [claimed[k].id for k in ("p", "b", "c")], W1)
        assert work_queue.block(s, claimed["b"].id, "owner review", now=now)
        assert work_queue.cancel(s, claimed["c"].id, "not needed", now=now)
        for key in ("p", "r", "run", "ok", "dead", "b", "c"):
            names[key] = work_queue.state_of(_item(s, "k", key))
    assert names == {"p": "PENDING", "r": "RETRYING", "run": "RUNNING", "ok": "COMPLETED", "dead": "FAILED", "b": "BLOCKED",
                     "c": "CANCELLED"}


def test_a_blocked_item_is_never_claimed_until_unblocked_and_a_cancelled_one_stays_closed(ctx):
    now = utcnow()
    with ctx.db.session() as s:
        work_queue.enqueue(s, "k", "b", now=now)
        work_queue.enqueue(s, "k", "c", now=now)
        b, c = _item(s, "k", "b"), _item(s, "k", "c")
        assert work_queue.block(s, b.id, "held") and work_queue.cancel(s, c.id, "dropped")
        assert work_queue.claim(s, "k", 5, W1, now=now) == []
        assert not work_queue.enqueue(s, "k", "c", now=now)  # enqueueing the same work again does not reopen it
        assert not work_queue.enqueue(s, "k", "b", now=now)
        assert _item(s, "k", "c").status == "cancelled" and _item(s, "k", "b").status == "blocked"
        assert not work_queue.unblock(s, c.id) and not work_queue.cancel(s, c.id, "again")
        assert work_queue.unblock(s, b.id)
        assert [i.key for i in work_queue.claim(s, "k", 5, W1, now=utcnow())] == ["b"]


def test_cancelling_an_item_a_worker_holds_means_that_worker_records_nothing(ctx):
    now = utcnow()
    with ctx.db.session() as s:
        work_queue.enqueue(s, "k", "x", now=now)
        (item,) = work_queue.claim(s, "k", 1, W1, now=now)
        assert work_queue.cancel(s, item.id, "owner stopped it", now=now)
        assert not work_queue.complete(s, item.id, W1, {"late": True})
        assert work_queue.fail(s, item.id, W1, "late failure") is None
        assert work_queue.release(s, [item.id], W1) == 0
        item = _item(s, "k", "x")
        assert item.status == "cancelled" and item.result is None and item.locked_by is None


def test_work_held_by_a_process_that_ended_is_taken_again_at_once_after_a_restart(ctx):
    """An app or PC restart: the old process's lease names a pid on this PC that no longer runs."""
    now = utcnow()
    gone = f"{socket.gethostname()}:999999"
    with ctx.db.session() as s:
        work_queue.enqueue(s, "k", "x", now=now)
        work_queue.claim(s, "k", 1, gone, now=now)
    with ctx.db.session() as s:
        assert not work_queue.busy(s, "k", W1, now)  # a dead holder does not count
        (again,) = work_queue.claim(s, "k", 1, W1, now=now + timedelta(minutes=1))  # long before the 45-minute lease ends
        assert again.key == "x" and again.attempts == 2 and again.locked_by == W1


def test_two_workers_claiming_at_the_same_moment_never_take_the_same_item(ctx):
    with ctx.db.session() as s:
        for n in range(40):
            work_queue.enqueue(s, "k", f"i{n}")
    got: dict[str, list[str]] = {}
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def worker(name):
        try:
            start.wait()
            taken = []
            for _ in range(10):
                with ctx.db.session() as s:
                    taken += [i.key for i in work_queue.claim(s, "k", 3, name, one_worker=False)]
            got[name] = taken
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in (W1, W2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    both = got[W1] + got[W2]
    assert len(both) == len(set(both)) == 40


def test_health_names_dead_letters_abandoned_leases_and_a_queue_that_does_not_move(ctx):
    now = utcnow()
    with ctx.db.session() as s:
        work_queue.enqueue(s, "k", "dead", now=now)
        (item,) = work_queue.claim(s, "k", 1, W1, now=now)
        work_queue.fail(s, item.id, W1, "boom", max_attempts=1, now=now)
        work_queue.enqueue(s, "k", "held", now=now)
        work_queue.claim(s, "k", 1, W1, now=now)
        work_queue.enqueue(s, "k", "old", now=now - timedelta(days=3))
        later = now + work_queue.LEASE + timedelta(minutes=1)
        keys = [k for k, _ in work_queue.health(s, now=later)]
        assert keys == ["queue_dead:k", "queue_stuck:k", "queue_stalled:k"]
        assert [k for k, _ in work_queue.health(s, {"k": False}, now=later)] == ["queue_dead:k", "queue_stuck:k"]  # queue off
        row = work_queue.status(s, later)[0]
        assert (row["dead"], row["stuck"], row["pending"]) == (1, 1, 1)


def test_the_monitor_raises_and_then_resolves_the_dead_letter_alert(ctx):
    now = utcnow()
    with ctx.db.session() as s:
        work_queue.enqueue(s, "company_research", "7", now=now)
        (item,) = work_queue.claim(s, "company_research", 1, W1, now=now)
        work_queue.fail(s, item.id, W1, "site down", max_attempts=1, now=now)
    with ctx.db.session() as s:
        summary = check_health(s, ctx.settings, llm_probe=lambda: True)
        assert any("dead letters" in i for i in summary["issues"])
    with ctx.db.session() as s:
        open_ = s.execute(select(Alert).where(Alert.message.like("%Work queue 'company_research'%"), Alert.acknowledged.is_(False))).scalars().all()
        assert len(open_) == 1 and open_[0].level == "warning"
        assert work_queue.revive(s, item.id)
    with ctx.db.session() as s:
        check_health(s, ctx.settings, llm_probe=lambda: True)
    with ctx.db.session() as s:
        assert not s.execute(select(Alert).where(Alert.message.like("%Work queue 'company_research'%"), Alert.acknowledged.is_(False))).first()


def test_the_queue_command_shows_the_state_and_audits_a_persons_hand(ctx, capsys):
    with ctx.db.session() as s:
        work_queue.enqueue(s, "company_research", "7")
        item_id = _item(s, "company_research", "7").id
    args = SimpleNamespace(action="block", id=item_id, kind=None, reason="checking the site first")
    assert cmd_queue(args, ctx) == 0
    assert cmd_queue(SimpleNamespace(action="block", id=item_id, kind=None, reason=None), ctx) == 1  # already blocked
    assert cmd_queue(SimpleNamespace(action="status", id=None, kind="company_research", reason=None), ctx) == 0
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "blocked 1" in out and "Nothing changed" in out
    with ctx.db.session() as s:
        ev = s.execute(select(AuditEvent).where(AuditEvent.action == "work_queue_block")).scalar_one()
        assert ev.target == f"work item #{item_id}" and "checking the site" in (ev.detail or "")


def test_the_owner_turns_the_queue_on_in_sales_settings_and_research_reads_it(ctx):
    assert ctx.settings.section("enrichment").get("work_queue") is False  # off by default
    with ctx.db.session() as s:
        form = {f"{sec}.{key}": v for (sec, key), v in
                ((tuple(k.split(".", 1)), v) for k, v in admin_settings.current(ctx.settings).items())}
        for name, value in list(form.items()):
            if isinstance(value, list):
                form[name] = value
            elif isinstance(value, bool):
                form[name] = "on" if value else ""
            elif value is None:
                form[name] = ""
            else:
                form[name] = str(value)
        form["outreach.intervals_days"] = ", ".join(str(d) for d in ctx.settings.section("outreach").get("intervals_days"))
        form["enrichment.work_queue"] = "on"

        class Form(dict):
            def getlist(self, k):
                v = self.get(k)
                return v if isinstance(v, list) else ([v] if v else [])

        changes = admin_settings.save(s, ctx.settings, "owner", Form(form))
        assert any("work_queue" in c for c in changes), changes
    assert ctx.settings.section("enrichment").get("work_queue") is True
    with ctx.db.session() as s:
        assert s.execute(select(AuditEvent).where(AuditEvent.actor == "owner")).first()
