# SPDX-License-Identifier: MIT
"""Shared DB pool bridge — ABStudio reuses the platform's single pool.

``SHARED_POOL`` is a drop-in replacement for ``psycopg_pool.ConnectionPool``
that sources every connection from the platform's SQLAlchemy engine
(``db.database.engine``) instead of opening a pool of its own. So the whole
in-process AiNxt application shares ONE set of Postgres connections.

The one non-obvious part is the driver bridge: the platform engine is
**psycopg2**, whose connection has no ``connection.execute(...)`` — that is a
psycopg **v3** API. ABStudio's repos call ``conn.execute(...).fetchone()`` and
``with conn.transaction():`` directly on the connection, so ``_Psycopg2Compat``
adapts a psycopg2 connection to that psycopg3-style surface.
"""

from __future__ import annotations

from contextlib import contextmanager

from db.database import pg_raw_connection


class _Psycopg2Compat:
    """Adapt a raw psycopg2 connection to ABStudio's psycopg3-style call sites."""

    def __init__(self, raw) -> None:
        self._raw = raw
        self._sp_seq = 0

    def execute(self, sql, params=None):
        # psycopg3 returns a managed cursor from conn.execute(); psycopg2 needs
        # an explicit cursor. The cursor is left for the connection to reap on
        # return-to-pool (psycopg2 cursors are client-side and cheap), matching
        # the short, bounded query counts per borrowed connection here.
        cur = self._raw.cursor()
        cur.execute(sql, params if params is not None else ())
        return cur

    def cursor(self):
        return self._raw.cursor()

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    @contextmanager
    def transaction(self):
        """Savepoint-scoped unit of work, mirroring psycopg3's ``transaction()``.

        psycopg2 aborts the ENTIRE transaction on any error, so a SAVEPOINT is
        used to isolate the block: on error we ``ROLLBACK TO SAVEPOINT`` and
        re-raise, leaving the surrounding transaction usable. Callers that wrap
        this in their own try/except (ABStudio's per-row seeding loops do) thus
        keep sibling work intact; an exception that escapes uncaught instead
        reaches ``_SharedPoolShim.connection()`` which rolls the whole thing back.
        """
        self._sp_seq += 1
        sp_name = f"abs_sp_{id(self)}_{self._sp_seq}"
        cur = self._raw.cursor()
        try:
            cur.execute(f"SAVEPOINT {sp_name}")
            yield self
        except Exception:
            cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
            raise
        else:
            cur.execute(f"RELEASE SAVEPOINT {sp_name}")
        finally:
            cur.close()

    def __getattr__(self, name):
        # Delegate unknown attributes (.closed, .info, ...) to psycopg2. Guard
        # against recursion if _raw isn't set yet (e.g. during a failed init).
        if name == "_raw":
            raise AttributeError(name)
        return getattr(self._raw, name)


class _SharedPoolShim:
    """Drop-in stand-in for ``psycopg_pool.ConnectionPool``.

    Exposes only the surface ABStudio uses — ``.connection()`` and ``.close()``.
    Connections come from the platform's shared pool via ``pg_raw_connection``;
    ``.close()`` is a no-op because that pool is owned by ``db.database`` and
    must outlive ABStudio.
    """

    @contextmanager
    def connection(self):
        with pg_raw_connection() as raw:
            yield _Psycopg2Compat(raw)

    def close(self) -> None:
        return None


# The single shared-pool instance every ABStudio subsystem points at.
SHARED_POOL = _SharedPoolShim()
