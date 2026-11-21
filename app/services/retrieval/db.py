"""Postgres connection pool for retrieval.

Serverless instances are many and short-lived, so each keeps a tiny pool that starts empty.
Behind a transaction-mode PgBouncer (Azure built-in, port 6432) server-side prepared statements
must be off.
"""

import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.core.config import Settings

# Borrows a connection for one block of queries: `async with connect() as conn`. Services take
# this instead of a connection so they hold none while waiting for Gemini, OSRM or the solver.
Connect = Callable[[], AbstractAsyncContextManager[AsyncConnection]]


def reuse(conn: AsyncConnection) -> Connect:
    """A Connect handing out one already open connection (tests, scripts)."""

    @asynccontextmanager
    async def connect() -> AsyncIterator[AsyncConnection]:
        yield conn

    return connect

SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")  # spliced into SQL, so letters only


def create_pool(settings: Settings) -> AsyncConnectionPool | None:
    """An unopened pool, or None when DATABASE_URL is not configured."""
    if settings.database_url is None:
        return None
    if not SCHEMA_NAME.fullmatch(settings.database_schema):
        raise ValueError(f"Invalid DATABASE_SCHEMA {settings.database_schema!r}")
    return AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        min_size=0,
        max_size=settings.database_pool_max_size,
        timeout=settings.database_timeout_seconds,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": None if settings.database_pgbouncer else 5,
            # The timeout also stops queries a killed serverless function would leave running.
            "options": f"-c search_path={settings.database_schema},public"
            f" -c statement_timeout={round(settings.database_timeout_seconds * 1000)}",
        },
    )
