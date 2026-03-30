"""Shared pytest fixtures for the DAoC matchmaking bot test suite.

All fixtures that involve Discord objects use MagicMock / AsyncMock so no
real gateway connection is needed.  Engine tests that rely on the module-level
``store`` singleton use ``monkeypatch`` to swap it with a fresh ``BotState``
for the duration of each test.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# ── Env stub — config.py reads vars at import time ───────────────────────────
os.environ.setdefault("DISCORD_TOKEN", "test-token-placeholder")
os.environ.setdefault("MATCHMAKING_CHANNEL_ID", "111111111111111111")
os.environ.setdefault("BROADCAST_CHANNEL_ID", "222222222222222222")

import pytest  # noqa: E402

from daoc_bot.engine import MatchmakingEngine  # noqa: E402
from daoc_bot.models import Team, TeamState  # noqa: E402
from daoc_bot.state import BotState  # noqa: E402


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
    """A minimal mock :class:`discord.Client`.

    Both ``get_channel`` calls (matchmaking and broadcast) return the same
    ``fake_channel`` so all outgoing messages flow into one inspectable object.
    """
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=fake_channel)
    bot.fetch_channel = AsyncMock(return_value=fake_channel)
    return bot


# ── State / engine ────────────────────────────────────────────────────────────

@pytest.fixture()
def store() -> BotState:
    """A fresh, empty :class:`~daoc_bot.state.BotState` for each test."""
    return BotState()


@pytest.fixture()
def engine(mock_bot: MagicMock, store: BotState, monkeypatch: pytest.MonkeyPatch) -> MatchmakingEngine:
    """A :class:`~daoc_bot.engine.MatchmakingEngine` wired to fake Discord objects.

    The module-level ``store`` singleton inside ``daoc_bot.engine`` is patched
    to the fixture's fresh ``BotState`` so engine methods see isolated state.
    """
    monkeypatch.setattr("daoc_bot.engine.store", store)
    return MatchmakingEngine(mock_bot)


# ── Event-log suppression ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def suppress_event_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from writing to the JSONL event log on disk."""
    monkeypatch.setattr("daoc_bot.event_log._write", lambda *_a, **_kw: None)


# ── Convenience team builder ──────────────────────────────────────────────────

def make_team(name: str, leader_id: int, mmr: int = 1000) -> Team:
    """Return a :class:`~daoc_bot.models.Team` in the default IDLE state."""
    return Team(name=name, leader_id=leader_id, member_ids=[leader_id], mmr=mmr)
