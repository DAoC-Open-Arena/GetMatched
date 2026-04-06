"""Guild-scoped, DB-backed state store.

Replaces the global :data:`~daoc_bot.state.store` singleton with a
multi-guild-aware store that persists all durable state to PostgreSQL while
keeping the ready-queue in memory (rebuilt on bot restart).

Typical usage::

    from daoc_bot.guild_store import guild_store, EventConfig

    cfg = EventConfig(composition_type="modular", min_group_size=3, max_group_size=6)
    guild_store.create_event(guild_id, cfg)

    team = guild_store.get_team(guild_id, "Gandalf")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from daoc_bot.db import get_db
from daoc_bot.models import Match, Team, TeamState

logger = logging.getLogger(__name__)


# ── EventConfig ───────────────────────────────────────────────────────────────

@dataclass
class EventConfig:
    """All tunable parameters for one guild event.

    Attributes:
        composition_type:         ``"fixed"`` or ``"modular"``.
        min_group_size:           Minimum group size for modular events.
        max_group_size:           Maximum group size for modular events.
        mmr_enabled:              Whether ELO ratings are used.
        rematch_cooldown_seconds: ``0`` = only block immediate rematch (legacy);
                                  ``>0`` = block rematch for this many seconds.
        mmr_k_value:              ELO K-factor (default 32).
        mmr_match_threshold:      Max MMR diff for an instant match (default 200).
        mmr_relax_seconds:        Seconds before the MMR threshold is lifted (default 120).
        match_accept_timeout:     Seconds leaders have to accept a proposal (default 60).
        matchmaking_channel_id:   Discord channel ID for queue pings / private threads.
        broadcast_channel_id:     Discord channel ID for match announcements.
    """

    composition_type: str = "fixed"
    min_group_size: int = 1
    max_group_size: int = 1
    mmr_enabled: bool = True
    rematch_cooldown_seconds: int = 0
    mmr_k_value: int = 32
    mmr_match_threshold: int = 200
    mmr_relax_seconds: int = 120
    match_accept_timeout: int = 60
    matchmaking_channel_id: int = 0
    broadcast_channel_id: int = 0


# ── Internal helpers ──────────────────────────────────────────────────────────

def _team_from_row(row: dict) -> Team:
    """Convert a psycopg2 RealDictRow to a :class:`~daoc_bot.models.Team`."""
    return Team(
        name=row["name"],
        leader_id=row["leader_id"],
        member_ids=json.loads(row["member_ids"] or "[]"),
        state=TeamState(row["state"]),
        mmr=row["mmr"],
        wins=row["wins"],
        losses=row["losses"],
        last_opponent=row["last_opponent"],
        current_match_id=row["current_match_id"],
        current_opponent=row["current_opponent"],
        has_accepted=bool(row["has_accepted"]),
        panel_thread_id=row["panel_thread_id"],
        panel_message_id=row["panel_message_id"],
        group_size=row["group_size"],
    )


def _match_from_row(row: dict) -> Match:
    """Convert a psycopg2 RealDictRow to a :class:`~daoc_bot.models.Match`."""
    return Match(
        id=row["id"],
        team1_name=row["team1_name"],
        team2_name=row["team2_name"],
        team1_accepted=bool(row["team1_accepted"]),
        team2_accepted=bool(row["team2_accepted"]),
        active=bool(row["active"]),
        winner_name=row["winner_name"],
        proposal_message_id=row["proposal_message_id"],
        active_message_id=row["active_message_id"],
    )


# ── GuildStore ────────────────────────────────────────────────────────────────

class GuildStore:
    """DB-backed, guild-scoped replacement for :class:`~daoc_bot.state.BotState`.

    All methods accept a ``guild_id`` as their first argument and route to the
    correct event/teams/matches rows in PostgreSQL.  An in-memory queue (list
    of team names, oldest-first) and team/match caches per guild are maintained
    to avoid DB round-trips on every button click.

    The queue is transient — it is rebuilt from team states on
    :meth:`recover_guild` when the bot restarts.
    """

    def __init__(self) -> None:
        # guild_id → ordered list of team names (FIFO queue)
        self._queues: dict[int, list[str]] = {}
        # guild_id → {team_name → enqueue timestamp}
        self._queue_timestamps: dict[int, dict[str, datetime]] = {}
        # guild_id → {team_name → last match end timestamp}
        # Used to enforce rematch_cooldown_seconds. Transient — resets on restart.
        self._last_match_times: dict[int, dict[str, datetime]] = {}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _queue(self, guild_id: int) -> list[str]:
        return self._queues.setdefault(guild_id, [])

    def _queue_ts(self, guild_id: int) -> dict[str, datetime]:
        return self._queue_timestamps.setdefault(guild_id, {})

    def _last_match_ts(self, guild_id: int) -> dict[str, datetime]:
        return self._last_match_times.setdefault(guild_id, {})

    # ── Event management ──────────────────────────────────────────────────────

    def get_active_event_id(self, guild_id: int) -> Optional[int]:
        """Return the ID of the active event for *guild_id*, or ``None``."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM events WHERE guild_id = %s AND status = 'active'",
                (guild_id,),
            )
            row = cur.fetchone()
        return row["id"] if row else None

    def create_event(self, guild_id: int, config: EventConfig) -> int:
        """Insert a new active event row and return its ID.

        Raises:
            ValueError: If an active event already exists for this guild.
        """
        if self.get_active_event_id(guild_id) is not None:
            raise ValueError(f"Guild {guild_id} already has an active event.")
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO events (
                    guild_id, status, composition_type,
                    min_group_size, max_group_size, mmr_enabled,
                    rematch_cooldown_seconds, mmr_k_value,
                    mmr_match_threshold, mmr_relax_seconds, match_accept_timeout,
                    matchmaking_channel_id, broadcast_channel_id
                ) VALUES (%s, 'active', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    guild_id,
                    config.composition_type,
                    config.min_group_size,
                    config.max_group_size,
                    config.mmr_enabled,
                    config.rematch_cooldown_seconds,
                    config.mmr_k_value,
                    config.mmr_match_threshold,
                    config.mmr_relax_seconds,
                    config.match_accept_timeout,
                    config.matchmaking_channel_id,
                    config.broadcast_channel_id,
                ),
            )
            event_id: int = cur.fetchone()["id"]
        conn.commit()
        logger.info("Event created for guild %d (id=%d).", guild_id, event_id)
        return event_id

    def get_event_config(self, guild_id: int) -> Optional[EventConfig]:
        """Return :class:`EventConfig` for the active event, or ``None``."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM events WHERE guild_id = %s AND status = 'active'",
                (guild_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return EventConfig(
            composition_type=row["composition_type"],
            min_group_size=row["min_group_size"],
            max_group_size=row["max_group_size"],
            mmr_enabled=bool(row["mmr_enabled"]),
            rematch_cooldown_seconds=row["rematch_cooldown_seconds"],
            mmr_k_value=row["mmr_k_value"],
            mmr_match_threshold=row["mmr_match_threshold"],
            mmr_relax_seconds=row["mmr_relax_seconds"],
            match_accept_timeout=row["match_accept_timeout"],
            matchmaking_channel_id=row["matchmaking_channel_id"],
            broadcast_channel_id=row["broadcast_channel_id"],
        )

    def update_event_config(self, guild_id: int, **kwargs: object) -> bool:
        """Partially update the active event's config columns.

        Only the keyword arguments provided are updated.  Returns ``True`` if
        a row was updated, ``False`` if no active event exists.

        Valid keys mirror :class:`EventConfig` field names (except
        ``composition_type``, ``min_group_size``, ``max_group_size`` which are
        structural and cannot be changed mid-event).
        """
        allowed = {
            "mmr_enabled", "rematch_cooldown_seconds", "mmr_k_value",
            "mmr_match_threshold", "mmr_relax_seconds", "match_accept_timeout",
            "matchmaking_channel_id", "broadcast_channel_id",
        }
        cols = {k: v for k, v in kwargs.items() if k in allowed}
        if not cols:
            return False
        set_clause = ", ".join(f"{k} = %s" for k in cols)
        values = list(cols.values()) + [guild_id]
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE events SET {set_clause} WHERE guild_id = %s AND status = 'active'",
                values,
            )
            updated: bool = cur.rowcount > 0
        conn.commit()
        return updated

    def end_event(self, guild_id: int) -> None:
        """Mark the active event as ended and clear the in-memory queue."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE events SET status = 'ended' WHERE guild_id = %s AND status = 'active'",
                (guild_id,),
            )
        conn.commit()
        self._queues.pop(guild_id, None)
        self._queue_timestamps.pop(guild_id, None)
        logger.info("Event ended for guild %d.", guild_id)

    # ── Teams ─────────────────────────────────────────────────────────────────

    def _require_event_id(self, guild_id: int) -> int:
        eid = self.get_active_event_id(guild_id)
        if eid is None:
            raise ValueError(f"No active event for guild {guild_id}.")
        return eid

    def add_team(self, guild_id: int, team: Team) -> None:
        """Insert a new team row for the active event."""
        event_id = self._require_event_id(guild_id)
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO teams (
                    event_id, guild_id, name, leader_id, member_ids,
                    state, mmr, wins, losses, last_opponent,
                    current_match_id, current_opponent, has_accepted,
                    panel_thread_id, panel_message_id, group_size
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event_id, guild_id, team.name, team.leader_id,
                    json.dumps(team.member_ids),
                    team.state.value, team.mmr, team.wins, team.losses,
                    team.last_opponent, team.current_match_id,
                    team.current_opponent, team.has_accepted,
                    team.panel_thread_id, team.panel_message_id,
                    team.group_size,
                ),
            )
        conn.commit()
        logger.info("Team added: %s (guild=%d)", team.name, guild_id)

    def save_team(self, guild_id: int, team: Team) -> None:
        """Write all mutable fields of an existing team to the DB."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE teams SET
                    state = %s, mmr = %s, wins = %s, losses = %s,
                    last_opponent = %s, current_match_id = %s,
                    current_opponent = %s, has_accepted = %s,
                    panel_thread_id = %s, panel_message_id = %s,
                    member_ids = %s, group_size = %s
                WHERE guild_id = %s AND name = %s
                  AND event_id = (
                      SELECT id FROM events WHERE guild_id = %s AND status = 'active'
                  )
                """,
                (
                    team.state.value, team.mmr, team.wins, team.losses,
                    team.last_opponent, team.current_match_id,
                    team.current_opponent, team.has_accepted,
                    team.panel_thread_id, team.panel_message_id,
                    json.dumps(team.member_ids), team.group_size,
                    guild_id, team.name, guild_id,
                ),
            )
        conn.commit()

    def remove_team(self, guild_id: int, team_name: str) -> None:
        """Delete a team and clean up the in-memory queue."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM teams WHERE guild_id = %s AND name = %s
                  AND event_id = (
                      SELECT id FROM events WHERE guild_id = %s AND status = 'active'
                  )
                """,
                (guild_id, team_name, guild_id),
            )
        conn.commit()
        q = self._queue(guild_id)
        if team_name in q:
            q.remove(team_name)
        self._queue_ts(guild_id).pop(team_name, None)
        logger.info("Team removed: %s (guild=%d)", team_name, guild_id)

    def get_team(self, guild_id: int, name: str) -> Optional[Team]:
        """Return the named team for the active event, or ``None``."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.* FROM teams t
                JOIN events e ON e.id = t.event_id
                WHERE t.guild_id = %s AND t.name = %s AND e.status = 'active'
                """,
                (guild_id, name),
            )
            row = cur.fetchone()
        return _team_from_row(row) if row else None

    def get_team_by_leader(self, guild_id: int, user_id: int) -> Optional[Team]:
        """Return the team whose leader has the given Discord user ID."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.* FROM teams t
                JOIN events e ON e.id = t.event_id
                WHERE t.guild_id = %s AND t.leader_id = %s AND e.status = 'active'
                """,
                (guild_id, user_id),
            )
            row = cur.fetchone()
        return _team_from_row(row) if row else None

    def all_teams(self, guild_id: int) -> list[Team]:
        """Return all teams for the active event."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.* FROM teams t
                JOIN events e ON e.id = t.event_id
                WHERE t.guild_id = %s AND e.status = 'active'
                """,
                (guild_id,),
            )
            rows = cur.fetchall()
        return [_team_from_row(r) for r in rows]

    def team_exists(self, guild_id: int, name: str) -> bool:
        """Return ``True`` if a team with *name* is registered for the active event."""
        return self.get_team(guild_id, name) is not None

    def is_leader(self, guild_id: int, user_id: int) -> bool:
        """Return ``True`` if the user is already a leader of any team."""
        return self.get_team_by_leader(guild_id, user_id) is not None

    def record_match_end(self, guild_id: int, team_name: str) -> None:
        """Record the current time as the last match-end time for *team_name*.

        Used by the engine to enforce ``rematch_cooldown_seconds``.
        """
        self._last_match_ts(guild_id)[team_name] = datetime.now(timezone.utc)

    def seconds_since_last_match(self, guild_id: int, team_name: str) -> float:
        """Return seconds elapsed since *team_name*'s last match ended.

        Returns ``float('inf')`` if no match has been recorded (i.e. no cooldown
        applies — the team has never played or the bot restarted).
        """
        ts = self._last_match_ts(guild_id).get(team_name)
        if ts is None:
            return float("inf")
        return (datetime.now(timezone.utc) - ts).total_seconds()

    def clear_last_opponents(self, guild_id: int, name1: str, name2: str) -> None:
        """Remove the instant-rematch block between two teams."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE teams SET last_opponent = NULL
                WHERE guild_id = %s AND name IN (%s, %s)
                  AND event_id = (
                      SELECT id FROM events WHERE guild_id = %s AND status = 'active'
                  )
                """,
                (guild_id, name1, name2, guild_id),
            )
        conn.commit()

    # ── Matches ───────────────────────────────────────────────────────────────

    def add_match(self, guild_id: int, match: Match) -> None:
        """Insert a new match row for the active event."""
        event_id = self._require_event_id(guild_id)
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO matches (
                    id, event_id, guild_id,
                    team1_name, team2_name,
                    team1_accepted, team2_accepted, active,
                    winner_name, proposal_message_id, active_message_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    match.id, event_id, guild_id,
                    match.team1_name, match.team2_name,
                    match.team1_accepted, match.team2_accepted,
                    match.active, match.winner_name,
                    match.proposal_message_id, match.active_message_id,
                ),
            )
        conn.commit()

    def save_match(self, guild_id: int, match: Match) -> None:
        """Write all mutable fields of an existing match to the DB."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE matches SET
                    team1_accepted = %s, team2_accepted = %s, active = %s,
                    winner_name = %s, proposal_message_id = %s, active_message_id = %s
                WHERE id = %s AND guild_id = %s
                """,
                (
                    match.team1_accepted, match.team2_accepted,
                    match.active, match.winner_name,
                    match.proposal_message_id, match.active_message_id,
                    match.id, guild_id,
                ),
            )
        conn.commit()

    def remove_match(self, guild_id: int, match_id: str) -> None:
        """Delete a finished or cancelled match."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM matches WHERE id = %s AND guild_id = %s",
                (match_id, guild_id),
            )
        conn.commit()

    def get_match(self, guild_id: int, match_id: str) -> Optional[Match]:
        """Return the match with the given ID, or ``None``."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM matches WHERE id = %s AND guild_id = %s",
                (match_id, guild_id),
            )
            row = cur.fetchone()
        return _match_from_row(row) if row else None

    def active_matches(self, guild_id: int) -> list[Match]:
        """Return all currently-active matches for the guild."""
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM matches WHERE guild_id = %s AND active = TRUE",
                (guild_id,),
            )
            rows = cur.fetchall()
        return [_match_from_row(r) for r in rows]

    # ── Ready queue (in-memory) ───────────────────────────────────────────────

    def enqueue(self, guild_id: int, team_name: str) -> None:
        """Add a team to the ready queue."""
        q = self._queue(guild_id)
        if team_name not in q:
            q.append(team_name)
            self._queue_ts(guild_id)[team_name] = datetime.now(timezone.utc)
            cfg = self.get_event_config(guild_id)
            if cfg and cfg.composition_type == "modular":
                grouped = self.queue_by_group_size(guild_id)
                logger.info(
                    "Enqueued: %s (guild=%d)  |  queue by size=%s",
                    team_name, guild_id, grouped,
                )
            else:
                logger.info("Enqueued: %s (guild=%d)  |  queue=%s", team_name, guild_id, q)

    def dequeue(self, guild_id: int, team_name: str) -> None:
        """Remove a team from the ready queue (no-op if not present)."""
        q = self._queue(guild_id)
        if team_name in q:
            q.remove(team_name)
            self._queue_ts(guild_id).pop(team_name, None)
            cfg = self.get_event_config(guild_id)
            if cfg and cfg.composition_type == "modular":
                grouped = self.queue_by_group_size(guild_id)
                logger.info(
                    "Dequeued: %s (guild=%d)  |  queue by size=%s",
                    team_name, guild_id, grouped,
                )
            else:
                logger.info("Dequeued: %s (guild=%d)  |  queue=%s", team_name, guild_id, q)

    def queue_wait_seconds(self, guild_id: int, team_name: str) -> float:
        """Return how many seconds *team_name* has been in the queue."""
        ts = self._queue_ts(guild_id).get(team_name)
        if ts is None:
            return 0.0
        return (datetime.now(timezone.utc) - ts).total_seconds()

    def get_queue(self, guild_id: int) -> list[str]:
        """Return a *copy* of the current ready queue (oldest first)."""
        return list(self._queue(guild_id))

    def queue_size(self, guild_id: int) -> int:
        """Number of teams currently in the ready queue."""
        return len(self._queue(guild_id))

    def queue_by_group_size(self, guild_id: int) -> dict[int, list[str]]:
        """Return the ready queue partitioned by group_size.

        Returns a dict mapping ``group_size → [team_names]`` (oldest first).
        Teams whose DB row cannot be found are omitted.
        Used for modular-event logging only.
        """
        q = self._queue(guild_id)
        if not q:
            return {}
        conn = get_db()
        names_literal = ", ".join(["%s"] * len(q))
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT t.name, t.group_size FROM teams t
                JOIN events e ON e.id = t.event_id
                WHERE t.guild_id = %s AND e.status = 'active'
                  AND t.name IN ({names_literal})
                """,
                (guild_id, *q),
            )
            rows = cur.fetchall()
        size_map = {row["name"]: row["group_size"] for row in rows}
        grouped: dict[int, list[str]] = {}
        for name in q:
            gs = size_map.get(name)
            if gs is not None:
                grouped.setdefault(gs, []).append(name)
        return grouped

    # ── Recovery ──────────────────────────────────────────────────────────────

    def recover_guild(self, guild_id: int) -> None:
        """Rebuild in-memory queue from DB state after a bot restart.

        Teams with state ``ready`` are re-added to the queue (timestamp
        approximated as *now*).  Teams in ``matched`` or ``in_match`` keep
        their DB state; their UI panels will be stale until the engine
        naturally resets them (e.g. on next interaction).
        """
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.name, t.state FROM teams t
                JOIN events e ON e.id = t.event_id
                WHERE t.guild_id = %s AND e.status = 'active'
                  AND t.state IN ('ready', 'matched', 'in_match')
                """,
                (guild_id,),
            )
            rows = cur.fetchall()

        q = self._queue(guild_id)
        ts = self._queue_ts(guild_id)
        now = datetime.now(timezone.utc)

        for row in rows:
            if row["state"] == "ready" and row["name"] not in q:
                q.append(row["name"])
                ts[row["name"]] = now

        if rows:
            logger.info(
                "Guild %d recovered: %d team(s) in active states.",
                guild_id, len(rows),
            )


#: Module-level singleton — import this everywhere instead of the old ``store``.
guild_store = GuildStore()
