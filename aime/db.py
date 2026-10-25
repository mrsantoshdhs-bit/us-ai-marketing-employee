"""Database engine and session management (SQLite, WAL mode)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, select
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
        session.add(KeyValue(key=key, value={"v": value}))
    else:
        row.value = {"v": value}


def first(session: Session, stmt: Any) -> Any:
    return session.execute(stmt).scalars().first()


__all__ = ["Database", "kv_get", "kv_set", "first", "select"]
