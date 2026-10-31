"""Is the PC's clock believable? A wrong clock changes records for good: one tick a year ahead moved 37 of 40 qualified
leads to nurture, and correcting the clock did not bring them back. So a check-in stops before any work when:

- the time since the last check-in is longer than GAP_LIMIT. This PC sleeps: nights, and three-day weekends, are normal.
  Fourteen days lets a two-week absence pass without asking and stays well under the 30 days after which the stale-lead
  rule moves leads (the damage above); what a smaller wrong jump can do (a "stalled" task, a "no response" stage) is
  visible and a person can undo it. A real absence longer than that needs one command;
- or the clock is earlier than the newest record, by more than a small correction.

The stop never gets stuck: it ends by itself when the clock is corrected, and `aime confirm-clock` tells it the clock is
right (a long holiday, or a clock that used to be wrong and left records in the future). The confirmation is a file
outside the database, so restoring a backup does not undo it.

A wrong clock under those limits passes the stop, so what web servers say is compared too: the Date header of the requests
research and the website checks make anyway (aime.context; no request is made for it). When most of the sites heard in a run
disagree with this PC by the same amount, one warning names the clock. Alert only: it stops nothing, and one wrong server, or a
page kept by a cache, is not enough. The mail server's times are not used: a message's arrival time says nothing about a clock
that runs ahead.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from sqlalchemy import func, select

from aime.dedupe import registrable_domain
from aime.models import JobRun
from aime.timeutil import to_naive_utc, utcnow

GAP_LIMIT = timedelta(days=14)
BACK_MARGIN = timedelta(minutes=5)  # a time-server correction is no reason to stop
ALERT_KEY = "scheduler:clock"
HOLD_STATE = "clock_hold"
FILENAME = "CLOCK_CONFIRMED.json"
ADVICE = ("If the clock is wrong, correct it (Windows Settings, Date and time) and the work resumes by itself. If it is "
          "right, run `python -m aime confirm-clock`.")


SERVERS_ALERT_KEY = "clock:servers"  # not under ALERT_KEY: closing the stop's alert closes everything that starts with it
SERVER_LIMIT = timedelta(hours=1)
SERVER_SPREAD = timedelta(minutes=10)  # a wrong clock is off by the same amount from every server; stale pages are not
SERVERS_NEEDED = 2
SERVERS_HEARD = 4  # a warning needs this many sites heard in one run: two kept pages among two requests prove nothing
SERVER_KEPT_AGE = timedelta(minutes=5)  # a page a cache has kept this long says little about the time
MAX_SERVERS = 200
CONFIRM_QUIET = timedelta(hours=24)  # after `aime confirm-clock` a person has looked: servers do not reopen it for a day
_servers: dict[str, timedelta] = {}  # this process's requests: this PC's clock minus what the server said, per site
_servers_lock = threading.Lock()


def note_server_date(host: str, date_header: str | None, age_header: str | None = None, last_modified: str | None = None,
                     cache_status: str | None = None) -> None:
    """A server's Date header against this PC's clock. Never raises: it runs inside every request, and these are a stranger's
    headers. A page that looks kept by a cache is left out, as its Date may be old: a long Age, a cache saying it had the page,
    or a Date right beside its own Last-Modified. A short Age is taken off."""
    try:
        said = to_naive_utc(parsedate_to_datetime(date_header))
        age = timedelta(seconds=int(age_header)) if age_header and age_header.strip().isdigit() else timedelta()
        if age > SERVER_KEPT_AGE or "hit" in (cache_status or "").lower():
            return
        if last_modified and abs(said - to_naive_utc(parsedate_to_datetime(last_modified))) <= SERVER_KEPT_AGE:
            return
        site, gap = registrable_domain(host), utcnow() - said - age
    except Exception:  # noqa: BLE001 - whatever it holds, the request goes on
        return
    with _servers_lock:
        if site in _servers or len(_servers) < MAX_SERVERS:
            _servers[site] = gap


def _agreeing(gaps: list[timedelta]) -> list[timedelta]:
    """The largest group of the given gaps that lie close together."""
    gaps = sorted(gaps)
    best: list[timedelta] = []
    for i, first in enumerate(gaps):
        group = [g for g in gaps[i:] if g - first <= SERVER_SPREAD]
        best = group if len(group) > len(best) else best
    return best


def check_servers(session, settings) -> None:
    """Judge what this process's requests heard, then forget it. The majority of the sites heard decides. A warning needs
    SERVERS_HEARD sites, most of them off by more than an hour and by the same amount; it closes when two or more sites, and
    most of those heard, agree with this PC: one stale site does not hold it open."""
    from aime.monitoring import alert_once, resolve_alerts

    with _servers_lock:
        gaps = list(_servers.values())
        _servers.clear()
    wrong = _agreeing([g for g in gaps if abs(g) > SERVER_LIMIT])
    right = sum(abs(g) <= SERVER_LIMIT for g in gaps)
    confirmed = _parse(_confirmation(settings.data_dir).get("at"))
    if confirmed is not None and timedelta() <= utcnow() - confirmed <= CONFIRM_QUIET:
        resolve_alerts(session, (SERVERS_ALERT_KEY,), set())
    elif len(gaps) >= SERVERS_HEARD and len(wrong) >= SERVERS_NEEDED and len(wrong) * 2 > len(gaps):
        gap = wrong[len(wrong) // 2]
        hours = abs(gap).total_seconds() / 3600
        alert_once(session, "warning", "scheduler", f"The PC's clock looks wrong: it is about {hours:.1f} hours "
                   f"{'ahead of' if gap > timedelta() else 'behind'} the time {len(wrong)} of {len(gaps)} web servers gave. Work goes on, "
                   f"but dates on a wrong clock can move leads and emails. {ADVICE}", settings, key=SERVERS_ALERT_KEY)
    elif right >= SERVERS_NEEDED and right * 2 > len(gaps):
        resolve_alerts(session, (SERVERS_ALERT_KEY,), set())


def _parse(stamp) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(stamp).rstrip("Z"))
    except ValueError:
        return None
    return value if value.tzinfo is None else None


def _confirmation(data_dir: Path) -> dict:
    try:
        info = json.loads((Path(data_dir) / FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def _newest_record(ctx) -> datetime | None:
    with ctx.db.session() as s:
        return s.execute(select(func.max(JobRun.started_at))).scalar()


def last_good(beat: dict | None) -> datetime | None:
    """The last check-in on a clock that looked right: a check-in that stopped for the clock does not count."""
    if not beat:
        return None
    return _parse(beat.get("last_good_at") or beat.get("at"))


def _known(ctx, beat: dict | None) -> list[datetime]:
    return [t for t in (last_good(beat), _newest_record(ctx)) if t is not None]


def problem(ctx, beat: dict | None, now: datetime) -> str | None:
    """What is wrong with the clock, in words for the owner, or None."""
    done = _confirmation(ctx.settings.data_dir)
    confirmed_at, accepted = _parse(done.get("at")), _parse(done.get("newest"))
    known = _known(ctx, beat)
    ahead = [t for t in known if t > now + BACK_MARGIN and (accepted is None or t > accepted)]
    if ahead:
        return (f"The PC's clock looks wrong: it says {now:%Y-%m-%d %H:%M} UTC, but records exist up to {max(ahead):%Y-%m-%d %H:%M} "
                f"UTC. No work was done in this check-in, so nothing is changed on a wrong date. {ADVICE}")
    last = max([t for t in known if t <= now + BACK_MARGIN] + ([confirmed_at] if confirmed_at else []), default=None)
    if last is not None and now - last > GAP_LIMIT:
        return (f"The PC's clock looks wrong: it says {now:%Y-%m-%d %H:%M} UTC, {(now - last).days} days after the last check-in "
                f"({last:%Y-%m-%d %H:%M} UTC), and sleep rarely explains more than {GAP_LIMIT.days} days. No work was done in "
                f"this check-in, so nothing is changed on a wrong date. {ADVICE}")
    return None


def anchor(ctx, beat: dict | None) -> str | None:
    """What a check-in that stopped keeps as its reference, so the next one is judged against the same record and not
    against the stopped one's wrong time."""
    done = _parse(_confirmation(ctx.settings.data_dir).get("at"))
    latest = max(_known(ctx, beat) + ([done] if done else []), default=None)
    return latest.isoformat() if latest else None


def confirm(ctx, beat: dict | None) -> datetime:
    """A person says the clock is right now. Records that lie ahead of it stay, and stop counting."""
    now = utcnow()
    path = Path(ctx.settings.data_dir) / FILENAME
    path.write_text(json.dumps({"at": now.isoformat(), "newest": max([*_known(ctx, beat), now]).isoformat()}), encoding="utf-8")
    return now
