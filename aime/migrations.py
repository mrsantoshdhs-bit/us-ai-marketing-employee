"""Additive schema migrations for SQLite: new tables via create_all, new columns via ALTER TABLE ADD COLUMN.

Never drops or rewrites columns, so existing lead data is preserved. A backup is taken before any
ALTER runs against a database that already holds data.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from aime.models import Base

log = logging.getLogger("aime.migrations")
SCHEMA_VERSION = 2


def pending_columns(engine: Engine) -> list[tuple[str, object]]:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    out = []
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        out.extend((table.name, col) for col in table.columns if col.name not in have)
    return out


def _default_sql(col) -> str | None:
    default = col.default
    if default is None:
        return None
    if default.is_scalar:
        value = default.arg
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)
        return "'" + str(value).replace("'", "''") + "'"
    if default.is_callable:
        fn = getattr(default.arg, "__wrapped__", default.arg)
        name = getattr(fn, "__name__", "")
        if name in ("dict", "<lambda>") or fn is dict:
            return "'{}'"
        if name == "list" or fn is list:
            return "'[]'"
    return None


def migrate(engine: Engine, backup_dir: Path | None = None, has_data: bool = False) -> list[str]:
    """Create missing tables and add missing columns. Returns the applied statements."""
    Base.metadata.create_all(engine)
    todo = pending_columns(engine)
    if not todo:
        return []
    if has_data and backup_dir is not None and engine.url.database:
        from aime.backup import backup_sqlite_file

        backup_sqlite_file(Path(engine.url.database), backup_dir, label="pre-migration")
    applied = []
    with engine.begin() as conn:
        for table, col in todo:
            ddl = f'ALTER TABLE "{table}" ADD COLUMN "{col.name}" {col.type.compile(dialect=engine.dialect)}'
            default = _default_sql(col)
            if default is not None:
                ddl += f" DEFAULT {default}"
            elif not col.nullable:
                ddl += " DEFAULT ''"
            conn.execute(text(ddl))
            applied.append(ddl)
        conn.execute(text(
            "INSERT INTO kv_store(key, value, updated_at) VALUES ('schema_version', :v, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP"),
            {"v": json.dumps({"v": SCHEMA_VERSION})})
    for ddl in applied:
        log.warning("Schema migration applied: %s", ddl)
    return applied
