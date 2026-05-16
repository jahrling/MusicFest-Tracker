"""Kuzu database connection — one connection per process, lazily initialized."""

from __future__ import annotations

import logging

import kuzu
from config import GRAPH_DB_PATH
from graph.schema import SCHEMA_DDL

log = logging.getLogger("festival_tracker.db")

_db: kuzu.Database | None = None
_conn: kuzu.Connection | None = None


def get_connection() -> kuzu.Connection:
    global _db, _conn
    if _conn is None:
        _db = kuzu.Database(GRAPH_DB_PATH)
        _conn = kuzu.Connection(_db)
        _apply_schema(_conn)
    return _conn


def _apply_schema(conn: kuzu.Connection) -> None:
    for stmt in SCHEMA_DDL:
        try:
            conn.execute(stmt)
        except Exception as e:
            log.debug("Schema DDL skipped: %s", str(e)[:100])


def close() -> None:
    global _db, _conn
    _conn = None
    _db = None
