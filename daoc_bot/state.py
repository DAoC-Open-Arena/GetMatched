"""In-memory state store for the bot's current session.

All mutable global state lives here and is accessed through the :data:`store`
singleton. Keeping state in one place makes it straightforward to replace the
in-memory backend with a persistent store (e.g. SQLite, Redis) in the future
without touching any other module.

Typical usage::

    from daoc_bot.state import store

    store.add_team(team)
    team = store.get_team("Dragons")
    store.enqueue("Dragons")
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from daoc_bot.models import Match, Team

logger = logging.getLogger(__name__)


class BotState:
    """Thread-safe* in-memory store for teams, matches, and the ready queue.

    (*) Thread-safety is provided implicitly by asyncio's single-threaded event
    loop. No additional locking is required as long as all access happens from
    coroutines scheduled on the same loop.
    """

    def __init__(self) -> None:
        self._teams: dict[str, Team] = {}
        self._matches: dict[str, Match] = {}
        self._queue: list[str] = []
        self._leader_index: dict[int, str] = {}        # user_id → team_name
        self._queue_timestamps: dict[str, datetime] = {}  # team_name → enqueue time

    # ── Teams ─────────────────────────────────────────────────────────────────

    def add_team(self, team: Team) -> None:
        """Register a new team and index it by leader ID."""
        self._teams[team.name] = team
        self._leader_index[team.leader_id] = team.name
        logger.info("Team registered: %s (leader=%d)", team.name, team.leader_id)

    def remove_team(self, team_name: str) -> None:
        """Remove a team and clean up all associated indexes."""
        team = self._teams.pop(team_name, None)
        if team:
            self._leader_index.pop(team.leader_id, None)
            if team_name in self._queue:
                self._queue.remove(team_name)
            self._queue_timestamps.pop(team_name, None)
        logger.info("Team removed: %s", team_name)

    def get_team(self, name: str) -> Team | None:
        """Return the team with the given name, or ``None``."""
        return self._teams.get(name)

    def get_team_by_leader(self, user_id: int) -> Team | None:
        """Return the team whose leader has the given Discord user ID."""
        name = self._leader_index.get(user_id)
        return self._teams.get(name) if name else None

    def all_teams(self) -> list[Team]:
        """Return a snapshot list of all registered teams."""
        return list(self._teams.values())

    def team_exists(self, name: str) -> bool:
        """Return ``True`` if a team with the given name is registered."""
        return name in self._teams

    def is_leader(self, user_id: int) -> bool:
        """Return ``True`` if the user is already a leader of any team."""
        return user_id in self._leader_index

    # ── Matches ───────────────────────────────────────────────────────────────

    def add_match(self, match: Match) -> None:
        """Store a newly created match."""
        self._matches[match.id] = match
        logger.info("Match stored: %s (%s vs %s)", match.id, match.team1_name, match.team2_name)

    def remove_match(self, match_id: str) -> None:
        """Remove a finished or cancelled match."""
        self._matches.pop(match_id, None)
        logger.info("Match removed: %s", match_id)

    def get_match(self, match_id: str) -> Match | None:
        """Return the match with the given ID, or ``None``."""
        return self._matches.get(match_id)

    def active_matches(self) -> list[Match]:
        """Return all matches that are currently live."""
        return [m for m in self._matches.values() if m.active]

    # ── Ready queue ───────────────────────────────────────────────────────────

    def enqueue(self, team_name: str) -> None:
        """Add a team to the ready queue, recording the entry timestamp."""
        if team_name not in self._queue:
            self._queue.append(team_name)
            self._queue_timestamps[team_name] = datetime.now(timezone.utc)
            logger.info("Enqueued: %s  |  queue=%s", team_name, self._queue)

    def dequeue(self, team_name: str) -> None:
        """Remove a team from the ready queue (no-op if not present)."""
        if team_name in self._queue:
            self._queue.remove(team_name)
            self._queue_timestamps.pop(team_name, None)
            logger.info("Dequeued: %s  |  queue=%s", team_name, self._queue)

    def queue_wait_seconds(self, team_name: str) -> float:
        """Return how many seconds ``team_name`` has been in the queue.

        Returns 0.0 if the team is not currently queued.
        """
        ts = self._queue_timestamps.get(team_name)
        if ts is None:
            return 0.0
        return (datetime.now(timezone.utc) - ts).total_seconds()

    @property
    def queue(self) -> list[str]:
        """A *copy* of the current ready queue (oldest first)."""
        return list(self._queue)

    @property
    def queue_size(self) -> int:
        """Number of teams currently in the ready queue."""
        return len(self._queue)

    def clear_last_opponents(self, name1: str, name2: str) -> None:
        """Remove the instant-rematch block between two teams."""
        t1 = self._teams.get(name1)
        t2 = self._teams.get(name2)
        if t1:
            t1.last_opponent = None
        if t2:
            t2.last_opponent = None


#: Module-level singleton — import this everywhere.
store = BotState()
