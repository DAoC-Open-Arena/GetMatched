"""Integration tests — full matchmaking flows exercising engine + state together.

These tests use the real MatchmakingEngine with a fresh in-memory store per test.
Discord is mocked at the bot/channel level so no gateway connection is needed,
but all state-machine transitions, ELO updates, and rematch guards are real.
"""

from __future__ import annotations

import pytest

from daoc_bot.engine import MatchmakingEngine, _elo_update
from daoc_bot.guild_store import EventConfig
from daoc_bot.models import Match, TeamState
from tests.conftest import TEST_GUILD_ID, _InMemoryGuildStore, make_team

GID = TEST_GUILD_ID
MMR_MATCH_THRESHOLD = EventConfig().mmr_match_threshold


class TestHappyPath:
    """Full round-trip: register → queue → match → accept → report result."""

    async def test_two_teams_full_round_trip(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1, mmr=1000)
        bravo = make_team("Bravo", 2, mmr=1000)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)

        # Queue both
        alpha.state = bravo.state = TeamState.READY
        mem_store.enqueue(GID, "Alpha")
        mem_store.enqueue(GID, "Bravo")

        # Match is proposed
        await engine.try_match(GID)
        assert alpha.state == TeamState.MATCHED
        assert bravo.state == TeamState.MATCHED

        match_id = alpha.current_match_id
        assert match_id is not None
        match = mem_store.get_match(GID, match_id)
        assert match is not None

        # Both accept
        await engine.accept_match(GID, match, "Alpha")
        await engine.accept_match(GID, match, "Bravo")
        assert match.active is True
        assert alpha.state == TeamState.IN_MATCH
        assert bravo.state == TeamState.IN_MATCH

        # Alpha wins
        expected_new_w, expected_new_l = _elo_update(alpha.mmr, bravo.mmr)
        await engine.end_match(GID, match, ended_by="Alpha", winner_name="Alpha")

        assert alpha.state == TeamState.IDLE
        assert bravo.state == TeamState.IDLE
        assert mem_store.get_match(GID, match_id) is None
        assert alpha.wins == 1
        assert alpha.losses == 0
        assert bravo.wins == 0
        assert bravo.losses == 1
        assert alpha.mmr == expected_new_w
        assert bravo.mmr == expected_new_l

    async def test_loser_reports_result_correctly(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        """If the loser clicks 'We Lost', the opponent must be credited with the win."""
        alpha = make_team("Alpha", 1, mmr=1000)
        bravo = make_team("Bravo", 2, mmr=1000)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        match = Match(id="INT00001", team1_name="Alpha", team2_name="Bravo", active=True)
        mem_store.add_match(GID, match)
        alpha.state = bravo.state = TeamState.IN_MATCH
        alpha.current_match_id = bravo.current_match_id = match.id

        # Bravo clicks "We Lost" → Alpha is the winner
        await engine.end_match(GID, match, ended_by="Bravo", winner_name="Alpha")

        assert alpha.wins == 1
        assert bravo.losses == 1


class TestDeclineFlow:
    async def test_decline_resets_both_teams_no_guard(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        alpha.state = bravo.state = TeamState.READY
        mem_store.enqueue(GID, "Alpha")
        mem_store.enqueue(GID, "Bravo")

        await engine.try_match(GID)
        match_id = alpha.current_match_id
        assert match_id is not None
        match = mem_store.get_match(GID, match_id)
        assert match is not None

        await engine.cancel_match(GID, match, reason="declined by Bravo")

        assert alpha.state == TeamState.IDLE
        assert bravo.state == TeamState.IDLE
        assert mem_store.get_match(GID, match_id) is None
        # Decline must NOT set the rematch guard
        assert alpha.last_opponent is None
        assert bravo.last_opponent is None

    async def test_teams_can_rematch_immediately_after_decline(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        """After a decline, the same pair should be matchable again."""
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        alpha.state = bravo.state = TeamState.READY
        mem_store.enqueue(GID, "Alpha")
        mem_store.enqueue(GID, "Bravo")

        await engine.try_match(GID)
        match = mem_store.get_match(GID, alpha.current_match_id or "")
        assert match is not None
        await engine.cancel_match(GID, match, reason="declined")

        # Re-queue
        alpha.state = bravo.state = TeamState.READY
        mem_store.enqueue(GID, "Alpha")
        mem_store.enqueue(GID, "Bravo")
        await engine.try_match(GID)

        assert alpha.state == TeamState.MATCHED
        assert bravo.state == TeamState.MATCHED


class TestRematchGuard:
    async def test_guard_prevents_immediate_rematch_with_third_team(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha",   1, mmr=1000)
        bravo = make_team("Bravo",   2, mmr=1000)
        charlie = make_team("Charlie", 3, mmr=1000)
        alpha.last_opponent = "Bravo"
        bravo.last_opponent = "Alpha"
        for t in (alpha, bravo, charlie):
            mem_store.add_team(GID, t)
            t.state = TeamState.READY
            mem_store.enqueue(GID, t.name)

        await engine.try_match(GID)

        # Charlie must have been matched with one of Alpha / Bravo
        assert charlie.state == TeamState.MATCHED
        match_id = charlie.current_match_id
        assert match_id is not None
        match = mem_store.get_match(GID, match_id)
        assert match is not None
        pair = {match.team1_name, match.team2_name}
        assert "Charlie" in pair
        assert pair != {"Alpha", "Bravo"}

    async def test_guard_blocks_when_only_two_teams_cooldown_zero(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        """With only two teams that are rematch-blocked and cooldown=0, no match forms."""
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        alpha.last_opponent = "Bravo"
        bravo.last_opponent = "Alpha"
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        alpha.state = bravo.state = TeamState.READY
        mem_store.enqueue(GID, "Alpha")
        mem_store.enqueue(GID, "Bravo")

        await engine.try_match(GID)

        assert mem_store.queue_size(GID) == 2
        assert alpha.state == TeamState.READY
        assert bravo.state == TeamState.READY


class TestParallelMatches:
    async def test_four_teams_produce_two_concurrent_matches(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        teams = [
            make_team("Alpha",   1, mmr=1000),
            make_team("Bravo",   2, mmr=1010),
            make_team("Charlie", 3, mmr=1020),
            make_team("Delta",   4, mmr=1030),
        ]
        for t in teams:
            mem_store.add_team(GID, t)
            t.state = TeamState.READY
            mem_store.enqueue(GID, t.name)

        await engine.try_match(GID)
        assert mem_store.queue_size(GID) == 2, "First call should match one pair"

        await engine.try_match(GID)
        assert mem_store.queue_size(GID) == 0, "Second call should match the remaining pair"

        # Accept both matches
        for match in list(mem_store._matches.values()):
            await engine.accept_match(GID, match, match.team1_name)
            await engine.accept_match(GID, match, match.team2_name)

        assert len(mem_store.active_matches(GID)) == 2

        # End both matches independently
        for match in list(mem_store._matches.values()):
            await engine.end_match(GID, match, ended_by=match.team1_name)

        assert len(mem_store.active_matches(GID)) == 0
        for t in teams:
            assert t.state == TeamState.IDLE


class TestMMRThreshold:
    async def test_far_mmr_pair_not_matched_immediately(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1, mmr=1000)
        bravo = make_team("Bravo", 2, mmr=1000 + MMR_MATCH_THRESHOLD + 100)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        alpha.state = bravo.state = TeamState.READY
        mem_store.enqueue(GID, "Alpha")
        mem_store.enqueue(GID, "Bravo")

        await engine.try_match(GID)

        # Should remain in queue — gap is too large and neither team has waited
        assert alpha.state == TeamState.READY
        assert bravo.state == TeamState.READY

    async def test_closest_pair_preferred_over_wider_gap(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        """Given three teams, the closest MMR pair should always be selected."""
        a = make_team("Alpha",   1, mmr=1000)
        b = make_team("Bravo",   2, mmr=1050)  # gap=50 with Alpha
        c = make_team("Charlie", 3, mmr=1180)  # gap=180 with Alpha, 130 with Bravo
        for t in (a, b, c):
            mem_store.add_team(GID, t)
            t.state = TeamState.READY
            mem_store.enqueue(GID, t.name)

        await engine.try_match(GID)

        assert a.state == TeamState.MATCHED, "Alpha (closest pair) should be matched"
        assert b.state == TeamState.MATCHED, "Bravo (closest pair) should be matched"
        assert c.state == TeamState.READY,   "Charlie left in queue"


class TestQueueReentryAfterMatch:
    async def test_teams_can_queue_again_after_match_ends(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        charlie = make_team("Charlie", 3)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        mem_store.add_team(GID, charlie)

        alpha.state = bravo.state = TeamState.READY
        mem_store.enqueue(GID, "Alpha")
        mem_store.enqueue(GID, "Bravo")

        await engine.try_match(GID)
        match = mem_store.get_match(GID, alpha.current_match_id or "")
        assert match is not None

        await engine.accept_match(GID, match, "Alpha")
        await engine.accept_match(GID, match, "Bravo")
        await engine.end_match(GID, match, ended_by="Alpha", winner_name="Alpha")

        # Alpha and Bravo are now IDLE — Charlie queues with Alpha
        charlie.state = TeamState.READY
        mem_store.enqueue(GID, "Charlie")
        # Alpha has last_opponent = Bravo, so can match Charlie
        alpha.state = TeamState.READY
        mem_store.enqueue(GID, "Alpha")

        await engine.try_match(GID)

        assert alpha.state == TeamState.MATCHED
        assert charlie.state == TeamState.MATCHED
