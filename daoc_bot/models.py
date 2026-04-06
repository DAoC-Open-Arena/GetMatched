"""Domain models for the DAoC matchmaking bot.

This module is intentionally free of Discord and bot-framework imports so that
the core data structures can be tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TeamState(Enum):
    """Lifecycle states of a registered team.

    State transitions::

        IDLE ──(ready)──► READY ──(match found)──► MATCHED ──(both accept)──► IN_MATCH
          ▲                                                                        │
          └────────────────────────── (match ends / cancelled) ───────────────────┘
    """

    IDLE = "idle"
    """Registered but not queued for a match."""

    READY = "ready"
    """Queued and waiting for an opponent."""

    MATCHED = "matched"
    """An opponent has been found; waiting for both leaders to accept."""

    IN_MATCH = "in_match"
    """Match accepted and currently in progress."""


@dataclass
class Team:
    """A registered team.

    Attributes:
        name: Unique display name chosen by the leader.
        leader_id: Discord user ID of the team leader.
        member_ids: Discord user IDs of all members (leader included).
        state: Current lifecycle state.
        mmr: Current ELO-style matchmaking rating (starts at 1000).
        wins: Total wins recorded this session.
        losses: Total losses recorded this session.
        last_opponent: Name of the last team this team played against.
            Used to prevent instant rematches.
        current_match_id: ID of the ongoing :class:`Match`, if any.
        current_opponent: Display name of the current opponent, if any.
        has_accepted: Whether this team's leader has already accepted the
            current match proposal.
        panel_thread_id: ID of the private thread created for this leader in
            #matchmaking.  The bot edits the panel message inside this thread
            whenever state changes, so the leader always sees live buttons
            without having to reopen any command.
        panel_message_id: ID of the panel message inside the private thread.
    """

    name: str
    leader_id: int
    member_ids: list[int] = field(default_factory=list)
    state: TeamState = TeamState.IDLE
    mmr: int = 1000
    wins: int = 0
    losses: int = 0
    last_opponent: str | None = None
    current_match_id: str | None = None
    current_opponent: str | None = None
    has_accepted: bool = False
    panel_thread_id: int | None = None
    panel_message_id: int | None = None
    group_size: int = 1


@dataclass
class Match:
    """A single match between two teams.

    Attributes:
        id: Short unique identifier (8 hex chars) used for logging and display.
        team1_name: Name of the first team.
        team2_name: Name of the second team.
        team1_accepted: Whether team 1's leader has accepted the match proposal.
        team2_accepted: Whether team 2's leader has accepted the match proposal.
        active: ``True`` once both leaders have accepted and the match is live.
        winner_name: Name of the winning team, set when a leader reports the result.
        proposal_message_id: Discord message ID of the match-found ping in
            #matchmaking (deleted once both leaders accept or match is cancelled).
        active_message_id: Discord message ID of the match-started embed in
            #broadcast.
    """

    id: str
    team1_name: str
    team2_name: str
    team1_accepted: bool = False
    team2_accepted: bool = False
    active: bool = False
    winner_name: str | None = None
    proposal_message_id: int | None = None
    active_message_id: int | None = None

    @property
    def both_accepted(self) -> bool:
        """Return ``True`` when both leaders have accepted the match."""
        return self.team1_accepted and self.team2_accepted
