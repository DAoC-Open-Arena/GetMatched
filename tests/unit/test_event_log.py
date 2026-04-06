"""Unit tests for the structured event logger.

The conftest autouse fixture suppresses ``_write`` globally.  This file tests
the public API by capturing calls to ``_write`` via a local override, verifying
that the correct event_type and payload kwargs are passed without hitting the DB.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import daoc_bot.event_log as ev


@pytest.fixture()
def captured_writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, dict[str, Any]]]:
    """Capture all calls to ``_write`` as (event_type, guild_id, kwargs) tuples."""
    calls: list[tuple[str, int, dict[str, Any]]] = []

    def _capture(event_type: str, guild_id: int, **kwargs: Any) -> None:
        calls.append((event_type, guild_id, kwargs))

    # Override the global suppression from conftest so real logic runs here.
    monkeypatch.setattr(ev, "_write", _capture)
    return calls


class TestPublicAPI:
    def test_team_registered(
        self, captured_writes: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        ev.team_registered(guild_id=1, leader_name="Gandalf", leader_id=99)
        assert len(captured_writes) == 1
        etype, gid, payload = captured_writes[0]
        assert etype == "team_registered"
        assert gid == 1
        assert payload["leader_id"] == 99
        assert payload["team"] == "Gandalf"

    def test_team_unregistered(
        self, captured_writes: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        ev.team_unregistered(guild_id=2, team_name="Dragons", leader_id=42)
        etype, gid, payload = captured_writes[0]
        assert etype == "team_unregistered"
        assert gid == 2
        assert payload["team"] == "Dragons"
        assert payload["leader_id"] == 42

    def test_queue_entered(
        self, captured_writes: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        ev.queue_entered(guild_id=1, team_name="Alpha")
        etype, _, payload = captured_writes[0]
        assert etype == "queue_entered"
        assert payload["team"] == "Alpha"
        assert payload["group_size"] == 1  # default

    def test_queue_entered_with_group_size(
        self, captured_writes: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        ev.queue_entered(guild_id=1, team_name="Alpha", group_size=4)
        etype, _, payload = captured_writes[0]
        assert etype == "queue_entered"
        assert payload["group_size"] == 4

    def test_queue_left(
        self, captured_writes: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        ev.queue_left(guild_id=1, team_name="Alpha", reason="unready")
        etype, _, payload = captured_writes[0]
        assert etype == "queue_left"
        assert payload["reason"] == "unready"
        assert payload["team"] == "Alpha"
        assert payload["group_size"] == 1  # default

    def test_queue_left_with_group_size(
        self, captured_writes: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        ev.queue_left(guild_id=1, team_name="Alpha", reason="matched", group_size=3)
        etype, _, payload = captured_writes[0]
        assert etype == "queue_left"
        assert payload["group_size"] == 3

    def test_match_proposed_records_timestamp(
        self, captured_writes: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        ev._match_proposal_times.clear()
        ev.match_proposed(guild_id=1, match_id="M001", team1="Alpha", team2="Bravo")
        assert "M001" in ev._match_proposal_times
        etype, _, payload = captured_writes[0]
        assert etype == "match_proposed"
        assert payload["match_id"] == "M001"

    def test_match_started_records_elapsed(
        self, captured_writes: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        ev._match_proposal_times.clear()
        ev._match_start_times.clear()
        ev.match_proposed(guild_id=1, match_id="M002", team1="Alpha", team2="Bravo")
        ev.match_started(guild_id=1, match_id="M002", team1="Alpha", team2="Bravo")
        started_call = next(
            (c for c in captured_writes if c[0] == "match_started"), None
        )
        assert started_call is not None
        assert "elapsed_since_proposal_s" in started_call[2]
        assert started_call[2]["elapsed_since_proposal_s"] is not None

    def test_mmr_updated_includes_deltas(
        self, captured_writes: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        ev.mmr_updated(
            guild_id=1,
            winner="Alpha", winner_mmr_before=1000, winner_mmr_after=1016,
            loser="Bravo",  loser_mmr_before=1000,  loser_mmr_after=984,
        )
        etype, _, payload = captured_writes[0]
        assert etype == "mmr_updated"
        assert payload["winner_delta"] == 16
        assert payload["loser_delta"] == -16

    def test_match_cancelled_admin_clears_timestamps(
        self, captured_writes: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        ev._match_proposal_times.clear()
        ev._match_start_times.clear()
        ev.match_proposed(guild_id=1, match_id="M003", team1="Alpha", team2="Bravo")
        ev.match_started(guild_id=1, match_id="M003", team1="Alpha", team2="Bravo")
        ev.match_cancelled_admin(
            guild_id=1, match_id="M003", team1="Alpha", team2="Bravo", reason="admin"
        )
        assert "M003" not in ev._match_proposal_times
        assert "M003" not in ev._match_start_times
