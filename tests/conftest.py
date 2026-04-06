"""Shared pytest fixtures for the DAoC matchmaking bot test suite.

All fixtures that involve Discord objects use MagicMock / AsyncMock so no
real gateway connection is needed.  Engine tests use a real GuildStore backed
by a PostgreSQL test database, or they mock the DB layer where not needed.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ── Env stub — config.py reads vars at import time ───────────────────────────
os.environ.setdefault("DISCORD_TOKEN", "test-token-placeholder")
os.environ.setdefault("MATCHMAKING_CHANNEL_ID", "111111111111111111")
os.environ.setdefault("BROADCAST_CHANNEL_ID", "222222222222222222")

import pytest  # noqa: E402

from daoc_bot.engine import MatchmakingEngine  # noqa: E402
from daoc_bot.guild_store import EventConfig, GuildStore  # noqa: E402
from daoc_bot.models import Team, TeamState  # noqa: E402

# Fake guild ID used by all unit tests — must not clash with real guilds.
TEST_GUILD_ID = 888_888_888_888_888_888


# ── Discord fakes ─────────────────────────────────────────────────────────────

@pytest.fixture()
def fake_message() -> MagicMock:
    """A mock :class:`discord.Message` with a numeric ID."""
    msg = MagicMock()
    msg.id = 12345
    msg.delete = AsyncMock()
    msg.edit = AsyncMock()
    return msg


@pytest.fixture()
def fake_thread(fake_message: MagicMock) -> MagicMock:
    """A mock Discord private thread."""
    thread = MagicMock()
    thread.id = 88888
    thread.send = AsyncMock(return_value=fake_message)
    thread.add_user = AsyncMock()
    thread.delete = AsyncMock()
    thread.fetch_message = AsyncMock(return_value=fake_message)
    return thread


@pytest.fixture()
def fake_channel(fake_thread: MagicMock, fake_message: MagicMock) -> MagicMock:
    """A mock Discord text channel that supports send / fetch_message / create_thread."""
    channel = MagicMock()
    channel.id = 111111111111111111
    channel.send = AsyncMock(return_value=fake_message)
    channel.fetch_message = AsyncMock(return_value=fake_message)
    channel.create_thread = AsyncMock(return_value=fake_thread)
    return channel


@pytest.fixture()
def mock_bot(fake_channel: MagicMock) -> MagicMock:
    """A minimal mock :class:`discord.Client`."""
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=fake_channel)
    bot.fetch_channel = AsyncMock(return_value=fake_channel)
    return bot


# ── In-memory GuildStore stub ─────────────────────────────────────────────────

class _InMemoryGuildStore:
    """Minimal in-memory replacement for GuildStore used in unit tests.

    Supports only the subset of the GuildStore API that the engine calls
    directly.  Teams and matches are stored in plain dicts keyed by name/id.
    """

    _DEFAULT_CFG = EventConfig(
        composition_type="fixed",
        min_group_size=1,
        max_group_size=1,
        mmr_enabled=True,
        rematch_cooldown_seconds=0,
        mmr_k_value=32,
        mmr_match_threshold=200,
        mmr_relax_seconds=120,
        match_accept_timeout=60,
        matchmaking_channel_id=111111111111111111,
        broadcast_channel_id=222222222222222222,
    )

    def __init__(self) -> None:
        self._teams: dict[str, Team] = {}
        self._matches: dict[str, Any] = {}
        self._queue: list[str] = []
        self._cfg: EventConfig = self._DEFAULT_CFG

    # ── Config ────────────────────────────────────────────────────────────────

    def get_event_config(self, guild_id: int) -> EventConfig:
        return self._cfg

    def set_cfg(self, cfg: EventConfig) -> None:
        self._cfg = cfg

    # ── Teams ─────────────────────────────────────────────────────────────────

    def add_team(self, guild_id: int, team: Team) -> None:
        self._teams[team.name] = team

    def save_team(self, guild_id: int, team: Team) -> None:
        self._teams[team.name] = team

    def remove_team(self, guild_id: int, team_name: str) -> None:
        self._teams.pop(team_name, None)
        if team_name in self._queue:
            self._queue.remove(team_name)

    def get_team(self, guild_id: int, name: str) -> Team | None:
        return self._teams.get(name)

    def get_team_by_leader(self, guild_id: int, user_id: int) -> Team | None:
        return next((t for t in self._teams.values() if t.leader_id == user_id), None)

    def all_teams(self, guild_id: int) -> list[Team]:
        return list(self._teams.values())

    def team_exists(self, guild_id: int, name: str) -> bool:
        return name in self._teams

    def is_leader(self, guild_id: int, user_id: int) -> bool:
        return any(t.leader_id == user_id for t in self._teams.values())

    def record_match_end(self, guild_id: int, team_name: str) -> None:
        pass

    def seconds_since_last_match(self, guild_id: int, team_name: str) -> float:
        return float("inf")

    def clear_last_opponents(self, guild_id: int, name1: str, name2: str) -> None:
        for name in (name1, name2):
            t = self._teams.get(name)
            if t:
                t.last_opponent = None

    # ── Matches ───────────────────────────────────────────────────────────────

    def add_match(self, guild_id: int, match: Any) -> None:
        self._matches[match.id] = match

    def save_match(self, guild_id: int, match: Any) -> None:
        self._matches[match.id] = match

    def remove_match(self, guild_id: int, match_id: str) -> None:
        self._matches.pop(match_id, None)

    def get_match(self, guild_id: int, match_id: str) -> Any | None:
        return self._matches.get(match_id)

    def active_matches(self, guild_id: int) -> list[Any]:
        return [m for m in self._matches.values() if m.active]

    # ── Queue ─────────────────────────────────────────────────────────────────

    def enqueue(self, guild_id: int, team_name: str) -> None:
        if team_name not in self._queue:
            self._queue.append(team_name)

    def dequeue(self, guild_id: int, team_name: str) -> None:
        if team_name in self._queue:
            self._queue.remove(team_name)

    def get_queue(self, guild_id: int) -> list[str]:
        return list(self._queue)

    def queue_size(self, guild_id: int) -> int:
        return len(self._queue)

    def queue_wait_seconds(self, guild_id: int, team_name: str) -> float:
        return 0.0

    def _queue_ts(self, guild_id: int) -> dict[str, Any]:
        return {}


@pytest.fixture()
def mem_store() -> _InMemoryGuildStore:
    """A fresh in-memory GuildStore substitute for each test."""
    return _InMemoryGuildStore()


@pytest.fixture()
def engine(
    mock_bot: MagicMock,
    mem_store: _InMemoryGuildStore,
    monkeypatch: pytest.MonkeyPatch,
) -> MatchmakingEngine:
    """A :class:`~daoc_bot.engine.MatchmakingEngine` wired to fake Discord objects.

    The module-level ``guild_store`` singleton inside ``daoc_bot.engine`` is
    patched to the fixture's in-memory store so engine methods see isolated state.
    """
    monkeypatch.setattr("daoc_bot.engine.guild_store", mem_store)
    return MatchmakingEngine(mock_bot)


# ── Event-log suppression ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def suppress_event_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from writing to the event log."""
    monkeypatch.setattr("daoc_bot.event_log._write", lambda *_a, **_kw: None)


# ── Convenience team builder ──────────────────────────────────────────────────

def make_team(name: str, leader_id: int, mmr: int = 1000) -> Team:
    """Return a :class:`~daoc_bot.models.Team` in the default IDLE state."""
    return Team(name=name, leader_id=leader_id, member_ids=[leader_id], mmr=mmr)
