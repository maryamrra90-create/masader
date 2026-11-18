"""PostgreSQL connection pool and the schema migration runner.

Migrations are plain, forward-only SQL files in store/migrations, applied in name order and recorded in
schema_migrations. This replaces Alembic (architecture B.4): the schema is hand-written SQL either way,
and a 40-line runner is easier to audit than an extra framework for two files.
"""
from __future__ import annotations

import logging
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..settings import settings

log = logging.getLogger("masader.db")
MIGRATIONS = Path(__file__).parent / "migrations"
_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """One pool per process (Uvicorn worker or Celery child), created on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=settings.db_pool_max, timeout=settings.db_pool_timeout_s,
            kwargs={"row_factory": dict_row, "autocommit": True,
                    "options": f"-c search_path=masader,public -c statement_timeout={settings.statement_timeout_ms}"},
            open=True)
    return _pool


def reset_pool() -> None:
    """Close the pool (used after fork in Celery children and between tests)."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def migrate(conninfo: str | None = None) -> list[str]:
    """Apply pending migrations; returns the names applied. Safe to run concurrently (advisory lock)."""
    import psycopg

    applied = []
    with psycopg.connect(conninfo or settings.database_url, autocommit=True) as c:
        c.execute("SELECT pg_advisory_lock(7001)")
        try:
            c.execute("CREATE TABLE IF NOT EXISTS public.schema_migrations "
                      "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            done = {r[0] for r in c.execute("SELECT name FROM public.schema_migrations")}
            for f in sorted(MIGRATIONS.glob("*.sql")):
                if f.name in done:
                    continue
                with c.transaction():
                    c.execute(f.read_text(encoding="utf-8"))
                    c.execute("INSERT INTO public.schema_migrations (name) VALUES (%s)", (f.name,))
                applied.append(f.name)
                log.info("applied migration %s", f.name)
        finally:
            c.execute("SELECT pg_advisory_unlock(7001)")
    return applied
