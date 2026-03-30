"""Structured event logger for post-event statistics.
 
Writes one JSON object per line to ``logs/event_YYYY-MM-DD.jsonl``.
Each entry has at minimum:
 
    {"event": "<type>", "ts": "<ISO timestamp>", ...}
 
Consuming the log after an event::
 
    import json
    events = [json.loads(l) for l in open("logs/event_2026-03-20.jsonl")]
    registrations = [e for e in events if e["event"] == "team_registered"]
 
Event types
-----------
team_registered
    team, leader_id, leader_name, ts
 
team_unregistered
    team, leader_id, ts
 
match_proposed
    match_id, team1, team2, ts
 
match_accepted_partial
    match_id, team, ts, elapsed_since_proposal_s
 
match_accepted_both
    match_id, team1, team2, ts, elapsed_since_proposal_s
 
match_timeout
    match_id, team1, team2,
    team1_accepted, team2_accepted,
    ts, elapsed_since_proposal_s
 
match_declined
    match_id, team, ts, elapsed_since_proposal_s
 
match_cancelled_admin
    match_id, team1, team2, reason, ts
 
match_started
    match_id, team1, team2, ts, elapsed_since_proposal_s
 
match_ended
    match_id, team1, team2, ended_by, ts,
    duration_s (time from match_started to match_ended)
 
mmr_updated
    winner, winner_mmr_before, winner_mmr_after,
    loser,  loser_mmr_before,  loser_mmr_after,
    ts
 
queue_entered
    team, ts
 
queue_left
    team, ts, reason  (unready | matched | timeout_requeue)
"""
 
from __future__ import annotations
 
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
 
logger = logging.getLogger(__name__)
 
# ── Setup ─────────────────────────────────────────────────────────────────────
 
_LOG_DIR = Path("logs")
_log_file: Optional[Path] = None
_match_proposal_times: dict[str, datetime] = {}   # match_id → proposal ts
_match_start_times: dict[str, datetime] = {}       # match_id → start ts
 
 
def _get_log_file() -> Path:
    global _log_file
    if _log_file is None:
        _LOG_DIR.mkdir(exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _log_file = _LOG_DIR / f"event_{date_str}.jsonl"
        logger.info("Event log: %s", _log_file.resolve())
    return _log_file
 
 
def _write(event: str, **kwargs: Any) -> None:
    """Append a JSON event line to the log file."""
    entry = {
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }
    try:
        with _get_log_file().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.error("Failed to write event log: %s", exc)
 
 
def _elapsed(since: datetime) -> float:
    """Return seconds elapsed since *since*."""
    return round((datetime.now(timezone.utc) - since).total_seconds(), 1)
 
 
# ── Public API ────────────────────────────────────────────────────────────────
 
def team_registered(leader_name: str, leader_id: int) -> None:
    _write("team_registered",
           team=leader_name,
           leader_id=leader_id,
           leader_name=leader_name)
 
 
def team_unregistered(team_name: str, leader_id: int) -> None:
    _write("team_unregistered", team=team_name, leader_id=leader_id)
 
 
def queue_entered(team_name: str) -> None:
    _write("queue_entered", team=team_name)
 
 
def queue_left(team_name: str, reason: str) -> None:
    """reason: 'unready' | 'matched' | 'timeout_requeue'"""
    _write("queue_left", team=team_name, reason=reason)
 
 
def match_proposed(match_id: str, team1: str, team2: str) -> None:
    _match_proposal_times[match_id] = datetime.now(timezone.utc)
    _write("match_proposed", match_id=match_id, team1=team1, team2=team2)
 
 
def match_accepted_partial(match_id: str, team: str) -> None:
    proposal_ts = _match_proposal_times.get(match_id)
    elapsed = _elapsed(proposal_ts) if proposal_ts else None
    _write("match_accepted_partial",
           match_id=match_id,
           team=team,
           elapsed_since_proposal_s=elapsed)
 
 
def match_accepted_both(match_id: str, team1: str, team2: str) -> None:
    proposal_ts = _match_proposal_times.get(match_id)
    elapsed = _elapsed(proposal_ts) if proposal_ts else None
    _write("match_accepted_both",
           match_id=match_id,
           team1=team1,
           team2=team2,
           elapsed_since_proposal_s=elapsed)
 
 
def match_timeout(
    match_id: str,
    team1: str,
    team2: str,
    team1_accepted: bool,
    team2_accepted: bool,
) -> None:
    proposal_ts = _match_proposal_times.pop(match_id, None)
    elapsed = _elapsed(proposal_ts) if proposal_ts else None
    _write("match_timeout",
           match_id=match_id,
           team1=team1,
           team2=team2,
           team1_accepted=team1_accepted,
           team2_accepted=team2_accepted,
           elapsed_since_proposal_s=elapsed)
 
 
def match_declined(match_id: str, team: str) -> None:
    proposal_ts = _match_proposal_times.pop(match_id, None)
    elapsed = _elapsed(proposal_ts) if proposal_ts else None
    _write("match_declined",
           match_id=match_id,
           team=team,
           elapsed_since_proposal_s=elapsed)
 
 
def match_cancelled_admin(match_id: str, team1: str, team2: str, reason: str) -> None:
    _match_proposal_times.pop(match_id, None)
    _match_start_times.pop(match_id, None)
    _write("match_cancelled_admin",
           match_id=match_id,
           team1=team1,
           team2=team2,
           reason=reason)
 
 
def match_started(match_id: str, team1: str, team2: str) -> None:
    proposal_ts = _match_proposal_times.pop(match_id, None)
    elapsed = _elapsed(proposal_ts) if proposal_ts else None
    _match_start_times[match_id] = datetime.now(timezone.utc)
    _write("match_started",
           match_id=match_id,
           team1=team1,
           team2=team2,
           elapsed_since_proposal_s=elapsed)
 
 
def match_ended(match_id: str, team1: str, team2: str, ended_by: str) -> None:
    start_ts = _match_start_times.pop(match_id, None)
    duration = _elapsed(start_ts) if start_ts else None
    _match_proposal_times.pop(match_id, None)
    _write("match_ended",
           match_id=match_id,
           team1=team1,
           team2=team2,
           ended_by=ended_by,
           duration_s=duration)
 
 
def mmr_updated(
    winner: str,
    winner_mmr_before: int,
    winner_mmr_after: int,
    loser: str,
    loser_mmr_before: int,
    loser_mmr_after: int,
) -> None:
    """Log an ELO rating change after a reported match result."""
    _write(
        "mmr_updated",
        winner=winner,
        winner_mmr_before=winner_mmr_before,
        winner_mmr_after=winner_mmr_after,
        winner_delta=winner_mmr_after - winner_mmr_before,
        loser=loser,
        loser_mmr_before=loser_mmr_before,
        loser_mmr_after=loser_mmr_after,
        loser_delta=loser_mmr_after - loser_mmr_before,
    )