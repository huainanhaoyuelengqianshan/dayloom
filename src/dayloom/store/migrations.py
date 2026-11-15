"""Ordered schema migrations.

Rule: never edit a shipped migration. Append a new one and bump the version.
The local database is the user's memory; a broken upgrade is data loss.
"""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE frames (
            id          INTEGER PRIMARY KEY,
            ts          REAL    NOT NULL,
            day         TEXT    NOT NULL,
            app         TEXT    NOT NULL DEFAULT '',
            exe         TEXT    NOT NULL DEFAULT '',
            title       TEXT    NOT NULL DEFAULT '',
            monitor     TEXT    NOT NULL DEFAULT '',
            x           INTEGER,
            y           INTEGER,
            w           INTEGER,
            h           INTEGER,
            file        TEXT,
            bytes       INTEGER,
            dhash       INTEGER,
            batch_id    INTEGER REFERENCES batches(id) ON DELETE SET NULL,
            created_at  REAL    NOT NULL
        );
        CREATE INDEX idx_frames_ts ON frames(ts);
        CREATE INDEX idx_frames_day ON frames(day, ts);
        CREATE INDEX idx_frames_batch ON frames(batch_id);

        CREATE TABLE batches (
            id            INTEGER PRIMARY KEY,
            day           TEXT    NOT NULL,
            start_ts      REAL    NOT NULL,
            end_ts        REAL    NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'pending',
            attempts      INTEGER NOT NULL DEFAULT 0,
            error         TEXT,
            model         TEXT,
            prompt_version TEXT,
            tokens_in     INTEGER NOT NULL DEFAULT 0,
            tokens_out    INTEGER NOT NULL DEFAULT 0,
            created_at    REAL    NOT NULL,
            updated_at    REAL    NOT NULL,
            UNIQUE(day, start_ts)
        );
        CREATE INDEX idx_batches_status ON batches(status, day);

        CREATE TABLE activities (
            id          INTEGER PRIMARY KEY,
            batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            seq         INTEGER NOT NULL,
            start_ts    REAL    NOT NULL,
            end_ts      REAL    NOT NULL,
            app         TEXT    NOT NULL DEFAULT '',
            category    TEXT    NOT NULL DEFAULT 'other',
            title       TEXT    NOT NULL DEFAULT '',
            summary     TEXT    NOT NULL DEFAULT '',
            confidence  REAL    NOT NULL DEFAULT 0.5,
            frame_ids   TEXT    NOT NULL DEFAULT '[]',
            created_at  REAL    NOT NULL,
            UNIQUE(batch_id, seq)
        );
        CREATE INDEX idx_activities_ts ON activities(start_ts, end_ts);

        CREATE TABLE sessions (
            id           INTEGER PRIMARY KEY,
            day          TEXT    NOT NULL,
            start_ts     REAL    NOT NULL,
            end_ts       REAL    NOT NULL,
            app          TEXT    NOT NULL,
            category     TEXT    NOT NULL,
            title        TEXT    NOT NULL DEFAULT '',
            activity_ids TEXT    NOT NULL DEFAULT '[]',
            built_at     REAL    NOT NULL
        );
        CREATE INDEX idx_sessions_day ON sessions(day, start_ts);

        CREATE TABLE daily_reports (
            day             TEXT PRIMARY KEY,
            tracked_seconds REAL NOT NULL DEFAULT 0,
            categories      TEXT NOT NULL DEFAULT '{}',
            apps            TEXT NOT NULL DEFAULT '{}',
            narrative       TEXT NOT NULL DEFAULT '',
            model           TEXT,
            prompt_version  TEXT,
            created_at      REAL NOT NULL
        );

        -- end_ts IS NULL == the gap is still open. At most one open gap per kind.
        CREATE TABLE gaps (
            id       INTEGER PRIMARY KEY,
            kind     TEXT NOT NULL,
            start_ts REAL NOT NULL,
            end_ts   REAL,
            UNIQUE(kind, start_ts)
        );
        CREATE UNIQUE INDEX idx_gaps_one_open ON gaps(kind) WHERE end_ts IS NULL;

        CREATE TABLE settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        """,
    ),
    (
        2,
        """
        ALTER TABLE daily_reports ADD COLUMN highlights TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE daily_reports ADD COLUMN suggestions TEXT NOT NULL DEFAULT '[]';
        """,
    ),
    (
        3,
        """
        CREATE TABLE weekly_reports (
            start_day       TEXT PRIMARY KEY,
            end_day         TEXT NOT NULL,
            tracked_seconds REAL NOT NULL DEFAULT 0,
            categories      TEXT NOT NULL DEFAULT '{}',
            apps            TEXT NOT NULL DEFAULT '{}',
            daily           TEXT NOT NULL DEFAULT '{}',
            narrative       TEXT NOT NULL DEFAULT '',
            highlights      TEXT NOT NULL DEFAULT '[]',
            suggestions     TEXT NOT NULL DEFAULT '[]',
            model           TEXT,
            prompt_version  TEXT,
            created_at      REAL NOT NULL
        );
        """,
    ),
    (
        4,
        """
        ALTER TABLE batches ADD COLUMN images INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        5,
        """
        ALTER TABLE frames ADD COLUMN focus TEXT NOT NULL DEFAULT '';
        """,
    ),
]
