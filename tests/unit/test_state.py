"""Unit tests for BotState — the in-memory store."""

from __future__ import annotations

import pytest

from daoc_bot.models import Match, Team, TeamState
from daoc_bot.state import BotState


@pytest.fixture()
def s() -> BotState:
    return BotState()


def _team(name: str = "Alpha", leader_id: int = 1) -> Team:
    return Team(name=name, leader_id=leader_id, member_ids=[leader_id])


def _match(match_id: str = "AAAA0001") -> Match:
    return Match(id=match_id, team1_name="Alpha", team2_name="Bravo")


class TestTeamOperations:
    def test_add_and_get_team(self, s: BotState) -> None:
        team = _team()
        s.add_team(team)
        assert s.get_team("Alpha") is team

    def test_get_team_missing_returns_none(self, s: BotState) -> None:
        assert s.get_team("Nonexistent") is None

    def test_team_exists(self, s: BotState) -> None:
        s.add_team(_team("Alpha"))
        assert s.team_exists("Alpha") is True
        assert s.team_exists("Bravo") is False

    def test_is_leader(self, s: BotState) -> None:
        s.add_team(_team("Alpha", leader_id=42))
        assert s.is_leader(42) is True
        assert s.is_leader(99) is False

    def test_get_team_by_leader(self, s: BotState) -> None:
        team = _team("Alpha", leader_id=42)
        s.add_team(team)
        assert s.get_team_by_leader(42) is team
        assert s.get_team_by_leader(99) is None

    def test_remove_team_cleans_leader_index(self, s: BotState) -> None:
        s.add_team(_team("Alpha", leader_id=42))
        s.remove_team("Alpha")
        assert s.is_leader(42) is False
        assert s.get_team("Alpha") is None

    def test_remove_team_removes_from_queue(self, s: BotState) -> None:
        s.add_team(_team("Alpha"))
        s.enqueue("Alpha")
        s.remove_team("Alpha")
        assert "Alpha" not in s.queue

    def test_all_teams_returns_snapshot(self, s: BotState) -> None:
        s.add_team(_team("Alpha"))
        s.add_team(_team("Bravo", leader_id=2))
        teams = s.all_teams()
        assert len(teams) == 2
        # Mutation of the returned list must not affect the store
        teams.clear()
        assert len(s.all_teams()) == 2

    def test_remove_nonexistent_team_is_noop(self, s: BotState) -> None:
        s.remove_team("Ghost")  # must not raise


class TestMatchOperations:
    def test_add_and_get_match(self, s: BotState) -> None:
        m = _match("AAAA0001")
        s.add_match(m)
        assert s.get_match("AAAA0001") is m

    def test_get_match_missing_returns_none(self, s: BotState) -> None:
        assert s.get_match("ZZZZZZZZ") is None

    def test_remove_match(self, s: BotState) -> None:
        m = _match()
        s.add_match(m)
        s.remove_match(m.id)
        assert s.get_match(m.id) is None

    def test_active_matches_only_returns_active(self, s: BotState) -> None:
        m1 = Match(id="M1", team1_name="A", team2_name="B", active=True)
        m2 = Match(id="M2", team1_name="C", team2_name="D", active=False)
        s.add_match(m1)
        s.add_match(m2)
        active = s.active_matches()
        assert m1 in active
        assert m2 not in active


class TestQueueOperations:
    def test_enqueue_dequeue(self, s: BotState) -> None:
        s.add_team(_team("Alpha"))
        s.enqueue("Alpha")
        assert s.queue_size == 1
        assert "Alpha" in s.queue

        s.dequeue("Alpha")
        assert s.queue_size == 0

    def test_enqueue_is_idempotent(self, s: BotState) -> None:
        s.add_team(_team("Alpha"))
        s.enqueue("Alpha")
        s.enqueue("Alpha")
        assert s.queue_size == 1

    def test_dequeue_missing_is_noop(self, s: BotState) -> None:
        s.dequeue("Ghost")  # must not raise
        assert s.queue_size == 0

    def test_queue_property_returns_copy(self, s: BotState) -> None:
        s.add_team(_team("Alpha"))
        s.enqueue("Alpha")
        snapshot = s.queue
        snapshot.clear()
        assert s.queue_size == 1  # original unaffected

    def test_queue_wait_seconds_zero_when_not_queued(self, s: BotState) -> None:
        assert s.queue_wait_seconds("Alpha") == 0.0

    def test_queue_wait_seconds_positive_when_queued(self, s: BotState) -> None:
        s.add_team(_team("Alpha"))
        s.enqueue("Alpha")
        wait = s.queue_wait_seconds("Alpha")
        assert wait >= 0.0

    def test_fifo_ordering(self, s: BotState) -> None:
        for name, lid in [("Alpha", 1), ("Bravo", 2), ("Charlie", 3)]:
            s.add_team(_team(name, lid))
            s.enqueue(name)
        assert s.queue == ["Alpha", "Bravo", "Charlie"]


class TestClearLastOpponents:
    def test_clears_both_directions(self, s: BotState) -> None:
        alpha = _team("Alpha")
        bravo = _team("Bravo", leader_id=2)
        alpha.last_opponent = "Bravo"
        bravo.last_opponent = "Alpha"
        s.add_team(alpha)
        s.add_team(bravo)

        s.clear_last_opponents("Alpha", "Bravo")

        assert alpha.last_opponent is None
        assert bravo.last_opponent is None

    def test_safe_when_team_missing(self, s: BotState) -> None:
        s.clear_last_opponents("Ghost1", "Ghost2")  # must not raise
