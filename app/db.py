"""Postgres connection pool (psycopg2).

Usage:
    with get_conn() as conn:
        cur = dict_cursor(conn)
        cur.execute("SELECT ...", params)

The context manager commits on success and rolls back on any exception.
Never hold a connection open while calling an external service (the AI webhook).
"""
import logging
import threading
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

from .config import settings

log = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()
_last_used: dict[int, float] = {}   # id(conn) -> monotonic time it was returned to the pool
PING_AFTER_IDLE_SECONDS = 30

# Keepalives stop Supabase / cloud NATs from silently dropping idle pooled connections.
_CONNECT_KWARGS = dict(keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)


def init_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            return
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not set (see .env.example)")
        _pool = psycopg2.pool.ThreadedConnectionPool(
            settings.db_pool_min, settings.db_pool_max, settings.database_url, **_CONNECT_KWARGS
        )
        log.info("DB pool ready (min=%s max=%s)", settings.db_pool_min, settings.db_pool_max)


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


def _checkout():
    """Get a live connection, replacing it if the server closed it while idle.
    Only connections idle > 30 s are pinged, so busy periods cost no extra round trip."""
    if _pool is None:
        init_pool()
    for _ in range(3):
        conn = _pool.getconn()
        try:
            if conn.closed:
                raise psycopg2.InterfaceError("connection closed")
            if time.monotonic() - _last_used.get(id(conn), 0) < PING_AFTER_IDLE_SECONDS:
                return conn
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()  # leave no transaction open from the ping
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            log.warning("Discarding dead pooled DB connection")
            _last_used.pop(id(conn), None)
            _pool.putconn(conn, close=True)
    raise psycopg2.OperationalError("Could not obtain a working database connection")


@contextmanager
def get_conn():
    conn = _checkout()
    broken = False
    try:
        yield conn
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:  # connection itself died
            broken = True
        if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
            broken = True
        raise
    finally:
        if broken:
            _last_used.pop(id(conn), None)
        else:
            _last_used[id(conn)] = time.monotonic()
        _pool.putconn(conn, close=broken)


def dict_cursor(conn):
    return conn.cursor(cursor_factory=RealDictCursor)


def fetch_all(conn, sql: str, params=None) -> list[dict]:
    cur = dict_cursor(conn)
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def fetch_one(conn, sql: str, params=None) -> dict | None:
    cur = dict_cursor(conn)
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row is not None else None
