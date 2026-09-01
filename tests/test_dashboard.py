"""Tests for the dashboard's data access layer.

Why these exist
---------------
`queries._connection` is cached with `st.cache_resource`, which means one
connection is held open for the life of the Streamlit process. That is the
right call for latency, but it collides with where this app is deployed:

  * Neon's free tier suspends compute after a few minutes idle.
  * Streamlit Community Cloud apps are idle almost all the time.

So the cached connection *will* be dead when the next visitor arrives. The
failure is nastier than it sounds: once a connection is broken, even
`conn.rollback()` raises, so the error handler meant to recover from a bad
query raises a second exception on its way out — and `cache_resource` keeps
handing the same dead object to every subsequent request. The app stays broken
until someone restarts it.

These tests pin the recovery: a dead connection is discarded and replaced once,
transparently, while a genuine SQL error still propagates.
"""

from __future__ import annotations

import collections

import psycopg2
import pytest

from src.dashboard import queries

# psycopg2 exposes cursor.description entries with a .name attribute; the
# dashboard reads that, so the fake has to provide it too.
_Column = collections.namedtuple("_Column", ["name"])


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if self._conn.dead:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        self._conn.executed.append((sql, params))

    @property
    def description(self):
        return [_Column("n")]

    def fetchall(self):
        return [(1,)]


class _FakeConnection:
    """Stands in for a psycopg2 connection, alive or dead."""

    def __init__(self, dead=False):
        self.dead = dead
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        if self.dead:
            raise psycopg2.InterfaceError("connection already closed")
        self.commits += 1

    def rollback(self):
        # The behaviour that turns one dead connection into a permanently
        # broken app: recovery itself raises.
        if self.dead:
            raise psycopg2.InterfaceError("connection already closed")
        self.rollbacks += 1


class TestConnectionRecovery:
    """A dead cached connection must not persist across requests."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        queries._connection.clear()
        yield
        queries._connection.clear()

    def test_dead_connection_is_replaced_and_query_succeeds(self, monkeypatch):
        """The Neon-suspend case: first connection is dead, second works."""
        handed_out = []

        def fake_connect(*args, **kwargs):
            # First call returns a corpse, every call after returns a live one.
            conn = _FakeConnection(dead=len(handed_out) == 0)
            handed_out.append(conn)
            return conn

        monkeypatch.setattr(queries.psycopg2, "connect", fake_connect)

        df = queries._query("SELECT 1 AS n")

        assert len(handed_out) == 2, "should have discarded the dead connection and reconnected"
        assert handed_out[0].dead and not handed_out[1].dead
        assert df["n"].tolist() == [1]

    def test_live_connection_is_reused(self, monkeypatch):
        """The normal path must not reconnect on every query."""
        handed_out = []

        def fake_connect(*args, **kwargs):
            conn = _FakeConnection(dead=False)
            handed_out.append(conn)
            return conn

        monkeypatch.setattr(queries.psycopg2, "connect", fake_connect)

        queries._query("SELECT 1 AS n")
        queries._query("SELECT 1 AS n")

        assert len(handed_out) == 1, "cached connection should be reused"
        assert handed_out[0].commits == 2

    def test_sql_error_still_raises(self, monkeypatch):
        """A bad query is not a dead connection — it must still surface."""

        class _BadCursor(_FakeCursor):
            def execute(self, sql, params=()):
                raise psycopg2.ProgrammingError('relation "nope" does not exist')

        class _BadConnection(_FakeConnection):
            def cursor(self):
                return _BadCursor(self)

        made = []

        def fake_connect(*args, **kwargs):
            conn = _BadConnection()
            made.append(conn)
            return conn

        monkeypatch.setattr(queries.psycopg2, "connect", fake_connect)

        with pytest.raises(psycopg2.ProgrammingError):
            queries._query("SELECT * FROM nope")

        assert made[0].rollbacks == 1, "transaction should be reset, not left aborted"
        assert len(made) == 1, "a SQL error must not trigger a reconnect loop"
