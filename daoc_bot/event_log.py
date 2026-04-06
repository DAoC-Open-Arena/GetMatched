"""Structured event logger for post-event statistics.

Writes one row per event to the ``event_log`` Postgres table:

    (id, event_type, guild_id, ts, payload JSONB)

The payload column stores all event-specific fields as JSON, mirroring
the original JSONL file format so existing analytics queries can be
ported with minimal changes.

Consuming the log::

    SELECT payload FROM event_log
    WHERE guild_id = 123 AND event_type = 'team_registered'
    ORDER BY ts;

Event types
-----------
team_registered
    leader_id, leader_name, team

team_unregistered
    leader_id, team

match_proposed
    match_id, team1, team2

match_accepted_partial
    match_id, team, elapsed_since_proposal_s

match_accepted_both
    match_id, team1, team2, elapsed_since_proposal_s

match_timeout
    match_id, team1, team2,
    team1_accepted, team2_accepted,
    elapsed_since_proposal_s

match_declined
    match_id, team, elapsed_since_proposal_s

match_cancelled_admin
    match_id, team1, team2, reason

match_started
    match_id, team1, team2, elapsed_since_proposal_s

match_ended
    match_id, team1, team2, ended_by,
    duration_s (time from match_started to match_ended)

mmr_updated
    winner, winner_mmr_before, winner_mmr_after, winner_delta,
    loser,  loser_mmr_before,  loser_mmr_after,  loser_delta

queue_entered
    team, group_size

queue_left
    team, reason  (unready | matched | timeout_requeue), group_size
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from daoc_bot.db import get_db

logger = logging.getLogger(__name__)

# ── In-memory timing state (not persisted — survives only within one process) ─

_match_proposal_times: dict[str, datetime] = {}   # match_id → proposal ts
_match_start_times: dict[str, datetime] = {}       # match_id → start ts


def _elapsed(since: datetime) -> float:
    """Return seconds elapsed since *since*."""
    return round((datetime.now(timezone.utc) - since).total_seconds(), 1)


# ── Internal writer ───────────────────────────────────────────────────────────

def _write(event_type: str, guild_id: int, **kwargs: Any) -> None:
    """Insert one row into the ``event_log`` table."""
    payload = json.dumps(kwargs, ensure_ascii=False, default=str)
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_log (event_type, guild_id, payload)
                VALUES (%s, %s, %s::jsonb)
                """,
                (event_type, guild_id, payload),
            )
        conn.commit()
    except Exception as exc:
        logger.error("Failed to write event log entry '%s': %s", event_type, exc)


# ── Public API ────────────────────────────────────────────────────────────────

def team_registered(guild_id: int, leader_name: str, leader_id: int) -> None:
    _write("team_registered", guild_id,
           team=leader_name,
           leader_id=leader_id,
           leader_name=leader_name)


def team_unregistered(guild_id: int, team_name: str, leader_id: int) -> None:
    _write("team_unregistered", guild_id, team=team_name, leader_id=leader_id)


def queue_entered(guild_id: int, team_name: str, group_size: int = 1) -> None:
    _write("queue_entered", guild_id, team=team_name, group_size=group_size)


def queue_left(guild_id: int, team_name: str, reason: str, group_size: int = 1) -> None:
    """reason: 'unready' | 'matched' | 'timeout_requeue'"""
    _write("queue_left", guild_id, team=team_name, reason=reason, group_size=group_size)


def match_proposed(guild_id: int, match_id: str, team1: str, team2: str) -> None:
    _match_proposal_times[match_id] = datetime.now(timezone.utc)
    _write("match_proposed", guild_id, match_id=match_id, team1=team1, team2=team2)


def match_accepted_partial(guild_id: int, match_id: str, team: str) -> None:
    proposal_ts = _match_proposal_times.get(match_id)
    elapsed = _elapsed(proposal_ts) if proposal_ts else None
    _write("match_accepted_partial", guild_id,
           match_id=match_id,
           team=team,
           elapsed_since_proposal_s=elapsed)


def match_accepted_both(guild_id: int, match_id: str, team1: str, team2: str) -> None:
    proposal_ts = _match_proposal_times.get(match_id)
    elapsed = _elapsed(proposal_ts) if proposal_ts else None
    _write("match_accepted_both", guild_id,
           match_id=match_id,
           team1=team1,
           team2=team2,
           elapsed_since_proposal_s=elapsed)


def match_timeout(
    guild_id: int,
    match_id: str,
    team1: str,
    team2: str,
    team1_accepted: bool,
    team2_accepted: bool,
) -> None:
    proposal_ts = _match_proposal_times.pop(match_id, None)
    elapsed = _elapsed(proposal_ts) if proposal_ts else None
    _write("match_timeout", guild_id,
           match_id=match_id,
           team1=team1,
           team2=team2,
           team1_accepted=team1_accepted,
           team2_accepted=team2_accepted,
           elapsed_since_proposal_s=elapsed)


def match_declined(guild_id: int, match_id: str, team: str) -> None:
    proposal_ts = _match_proposal_times.pop(match_id, None)
    elapsed = _elapsed(proposal_ts) if proposal_ts else None
    _write("match_declined", guild_id,
           match_id=match_id,
           team=team,
           elapsed_since_proposal_s=elapsed)


def match_cancelled_admin(
    guild_id: int, match_id: str, team1: str, team2: str, reason: str
) -> None:
    _match_proposal_times.pop(match_id, None)
    _match_start_times.pop(match_id, None)
    _write("match_cancelled_admin", guild_id,
           match_id=match_id,
           team1=team1,
           team2=team2,
           reason=reason)


def match_started(guild_id: int, match_id: str, team1: str, team2: str) -> None:
    proposal_ts = _match_proposal_times.pop(match_id, None)
    elapsed = _elapsed(proposal_ts) if proposal_ts else None
    _match_start_times[match_id] = datetime.now(timezone.utc)
    _write("match_started", guild_id,
           match_id=match_id,
           team1=team1,
           team2=team2,
           elapsed_since_proposal_s=elapsed)


def match_ended(
    guild_id: int, match_id: str, team1: str, team2: str, ended_by: str
) -> None:
    start_ts = _match_start_times.pop(match_id, None)
    duration = _elapsed(start_ts) if start_ts else None
    _match_proposal_times.pop(match_id, None)
    _write("match_ended", guild_id,
           match_id=match_id,
           team1=team1,
           team2=team2,
           ended_by=ended_by,
           duration_s=duration)


def mmr_updated(
    guild_id: int,
    winner: str,
    winner_mmr_before: int,
    winner_mmr_after: int,
    loser: str,
    loser_mmr_before: int,
    loser_mmr_after: int,
) -> None:
    _write(
        "mmr_updated", guild_id,
        winner=winner,
        winner_mmr_before=winner_mmr_before,
        winner_mmr_after=winner_mmr_after,
        winner_delta=winner_mmr_after - winner_mmr_before,
        loser=loser,
        loser_mmr_before=loser_mmr_before,
        loser_mmr_after=loser_mmr_after,
        loser_delta=loser_mmr_after - loser_mmr_before,
    )
