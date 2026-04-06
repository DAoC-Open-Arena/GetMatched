"""PostgreSQL database layer for the DAoC matchmaking bot.

Owns the connection pool, runs migrations on startup, and exposes a thin
``get_db()`` accessor used by GuildStore.  No ORM — just raw psycopg2.

Railway injects a ``DATABASE_URL`` environment variable automatically when a
Postgres service is attached to the project.  That URL is read from
``daoc_bot.config.settings`` and used here.

Typical usage::

    from daoc_bot.db import init_db, get_db

    init_db()           # call once from __main__.on_ready
    conn = get_db()     # returns the module-level connection
"""

from __future__ import annotations

import logging
from typing import Optional

import psycopg2
import psycopg2.extras  # RealDictCursor / DictCursor

logger = logging.getLogger(__name__)

_conn: Optional[psycopg2.extensions.connection] = None

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id                       SERIAL      PRIMARY KEY,
    guild_id                 BIGINT      NOT NULL,
    status                   TEXT        NOT NULL DEFAULT 'active',
    composition_type         TEXT        NOT NULL DEFAULT 'fixed',
    min_group_size           INTEGER     NOT NULL DEFAULT 1,
    max_group_size           INTEGER     NOT NULL DEFAULT 1,
    mmr_enabled              BOOLEAN     NOT NULL DEFAULT TRUE,
    rematch_cooldown_seconds INTEGER     NOT NULL DEFAULT 0,
    mmr_k_value              INTEGER     NOT NULL DEFAULT 32,
    mmr_match_threshold      INTEGER     NOT NULL DEFAULT 200,
    mmr_relax_seconds        INTEGER     NOT NULL DEFAULT 120,
    match_accept_timeout     INTEGER     NOT NULL DEFAULT 60,
    matchmaking_channel_id   BIGINT      NOT NULL DEFAULT 0,
    broadcast_channel_id     BIGINT      NOT NULL DEFAULT 0,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Enforce one active event per guild (partial unique index)
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_guild_active
    ON events (guild_id)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS teams (
    id               SERIAL  PRIMARY KEY,
    event_id         INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    guild_id         BIGINT  NOT NULL,
    name             TEXT    NOT NULL,
    leader_id        BIGINT  NOT NULL,
    member_ids       TEXT    NOT NULL DEFAULT '[]',
    state            TEXT    NOT NULL DEFAULT 'idle',
    mmr              INTEGER NOT NULL DEFAULT 1000,
    wins             INTEGER NOT NULL DEFAULT 0,
    losses           INTEGER NOT NULL DEFAULT 0,
    last_opponent    TEXT,
    current_match_id TEXT,
    current_opponent TEXT,
    has_accepted     BOOLEAN NOT NULL DEFAULT FALSE,
    panel_thread_id  BIGINT,
    panel_message_id BIGINT,
    group_size       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (event_id, name),
    UNIQUE (event_id, leader_id)
);

CREATE TABLE IF NOT EXISTS matches (
    id                  TEXT    PRIMARY KEY,
    event_id            INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    guild_id            BIGINT  NOT NULL,
    team1_name          TEXT    NOT NULL,
    team2_name          TEXT    NOT NULL,
    team1_accepted      BOOLEAN NOT NULL DEFAULT FALSE,
    team2_accepted      BOOLEAN NOT NULL DEFAULT FALSE,
    active              BOOLEAN NOT NULL DEFAULT FALSE,
    winner_name         TEXT,
    proposal_message_id BIGINT,
    active_message_id   BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS event_log (
    id         BIGSERIAL   PRIMARY KEY,
    event_type TEXT        NOT NULL,
    guild_id   BIGINT      NOT NULL,
    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload    JSONB       NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_event_log_guild_ts
    ON event_log (guild_id, ts DESC);
"""


# ── Public API ────────────────────────────────────────────────────────────────

def init_db(database_url: str) -> None:
    """Open the Postgres connection and run all CREATE TABLE migrations.

    Safe to call multiple times — tables/indexes use IF NOT EXISTS.
    Must be called before any call to :func:`get_db`.

    Args:
        database_url: A libpq connection string or ``postgres://`` URL.
                      Railway injects this as ``DATABASE_URL`` automatically.
    """
    global _conn
    # psycopg2 accepts both postgres:// and postgresql:// schemes
    url = database_url.replace("postgres://", "postgresql://", 1)
    _conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    _conn.autocommit = False
    with _conn.cursor() as cur:
        cur.execute(_SCHEMA)
    _conn.commit()
    logger.info("PostgreSQL database initialised.")


def get_db() -> psycopg2.extensions.connection:
    """Return the active database connection.

    Raises:
        RuntimeError: If :func:`init_db` has not been called yet.
    """
    if _conn is None:
        raise RuntimeError("Database not initialised — call init_db() first.")
    # Reconnect transparently if the server closed an idle connection
    # (common after Railway sleeps the Postgres service).
    if _conn.closed:
        raise RuntimeError(
            "Database connection is closed. The bot must be restarted."
        )
    return _conn
