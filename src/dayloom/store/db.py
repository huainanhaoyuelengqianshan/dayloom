"""SQLite access.

Concurrency contract for the whole app:

* the daemon (``dayloom start``) is the only long-lived writer;
* one-off CLI commands may also write, but only inside short transactions;
* readers open the file read-only (``mode=ro``) and never block writers.

That is why every write goes through :meth:`Db.tx` which uses
``BEGIN IMMEDIATE`` with a process-local lock: it turns "database is locked"
from a rare production mystery into something this layer already handles.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .migrations import MIGRATIONS


class Db:
    """A thin, typed-ish wrapper around one sqlite3 connection."""

    def __init__(self, path: Path | str, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self._lock = threading.RLock()
        if readonly:
            uri = f"file:{self.path.as_posix()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=10.0, isolation_level=None)
            self._conn.execute("PRAGMA query_only=ON")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self.path.as_posix(), timeout=15.0, isolation_level=None, check_same_thread=False
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=8000")
        self._conn.execute("PRAGMA foreign_keys=ON")

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Db:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- queries ---------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        if row is None:
            return default
        return row[0]

    # -- writes ----------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one statement, return lastrowid (0 when not applicable)."""
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return int(cur.lastrowid or 0)

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [tuple(p) for p in seq])

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Exclusive write transaction. Nesting is flattened (outermost wins)."""
        with self._lock:
            if self._conn.in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # -- schema ----------------------------------------------------------
    def migrate(self) -> int:
        """Apply pending migrations. Returns the resulting schema version."""
        if self.readonly:
            raise RuntimeError("只读连接不能迁移数据库")
        with self.tx() as conn:
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            for version, script in MIGRATIONS:
                if version <= current:
                    continue
                for stmt in _split_statements(script):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version={version}")
                current = version
        return current

    def schema_version(self) -> int:
        return int(self.scalar("PRAGMA user_version", default=0))


def _split_statements(script: str) -> list[str]:
    """Split a migration script into statements.

    Simple and safe here because migrations contain no triggers or string
    literals with semicolons; keep it that way when appending migrations.
    """
    parts = [p.strip() for p in script.split(";")]
    return [p for p in parts if p]


def open_db(path: Path | str, readonly: bool = False) -> Db:
    db = Db(path, readonly=readonly)
    if not readonly:
        db.migrate()
    return db
