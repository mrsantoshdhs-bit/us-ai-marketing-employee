"""Database engine and session management (SQLite, WAL mode)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from aime.models import Base, KeyValue


class Database:
    def __init__(self, url: str):
        self.url = url
        connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


def kv_get(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(KeyValue, key)
    if row is None:
        return default
    return row.value.get("v", default)


def kv_set(session: Session, key: str, value: Any) -> None:
    row = session.get(KeyValue, key)
    if row is None:
        # One statement, not read-then-insert: two people writing a key for the first time (the first approvals on a
        # new install) must not collide on its primary key.
        insert = sqlite_insert(KeyValue).values(key=key, value={"v": value})
        session.execute(insert.on_conflict_do_update(index_elements=["key"], set_={"value": insert.excluded.value}))
    else:
        row.value = {"v": value}


def take_write_lock(session: Session) -> None:
    """Take the database's write lock now, before reading what a click is about. Two people clicking at once then run
    one after the other, and the second one reads what the first one saved: nobody decides on a page that is already
    out of date. The lock ends with the session's commit; another writer waits for it (busy_timeout)."""
    session.execute(text(f"UPDATE {KeyValue.__tablename__} SET key = key WHERE 0"))


def first(session: Session, stmt: Any) -> Any:
    return session.execute(stmt).scalars().first()


__all__ = ["Database", "kv_get", "kv_set", "take_write_lock", "first", "select"]
