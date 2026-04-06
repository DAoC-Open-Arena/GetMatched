"""Unit tests for the matchmaking engine — ELO math, pairing algorithm, match lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from daoc_bot.engine import MatchmakingEngine, _elo_update
from daoc_bot.guild_store import EventConfig
from daoc_bot.models import Match, Team, TeamState
from tests.conftest import TEST_GUILD_ID, _InMemoryGuildStore, make_team

# Default K-factor from EventConfig
ELO_K = EventConfig().mmr_k_value
# Default MMR threshold from EventConfig
MMR_MATCH_THRESHOLD = EventConfig().mmr_match_threshold

GID = TEST_GUILD_ID


# ── ELO formula ───────────────────────────────────────────────────────────────

class TestEloUpdate:
    def test_equal_mmr_gives_half_k(self) -> None:
        new_w, new_l = _elo_update(1000, 1000)
        assert new_w - 1000 == ELO_K // 2
        assert 1000 - new_l == ELO_K // 2

    def test_zero_sum(self) -> None:
        """Points gained by the winner must equal points lost by the loser."""
        for w_mmr, l_mmr in [(1000, 1000), (1200, 800), (800, 1200), (1500, 500)]:
            new_w, new_l = _elo_update(w_mmr, l_mmr)
            delta_w = new_w - w_mmr
            delta_l = l_mmr - new_l
            assert delta_w == delta_l, f"Not zero-sum for ({w_mmr}, {l_mmr})"

    def test_favourite_gains_less(self) -> None:
        """A heavy favourite should gain fewer points than an underdog who upsets."""
        new_fav_w, _ = _elo_update(1400, 1000)   # favourite wins
        new_dog_w, _ = _elo_update(1000, 1400)   # underdog upsets
        assert (new_fav_w - 1400) < (new_dog_w - 1000)

    def test_result_is_integers(self) -> None:
        new_w, new_l = _elo_update(1000, 1000)
        assert isinstance(new_w, int)
        assert isinstance(new_l, int)


# ── Matchmaking algorithm ─────────────────────────────────────────────────────

class TestTryMatch:
    async def test_two_eligible_teams_are_matched(
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

        assert alpha.state == TeamState.MATCHED
        assert bravo.state == TeamState.MATCHED
        assert mem_store.queue_size(GID) == 0
        assert alpha.current_match_id is not None
        assert alpha.current_match_id == bravo.current_match_id

    async def test_fewer_than_two_teams_is_noop(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        mem_store.add_team(GID, make_team("Alpha", 1))
        mem_store.enqueue(GID, "Alpha")
        await engine.try_match(GID)
        assert mem_store.queue_size(GID) == 1  # unchanged

    async def test_closest_mmr_pair_selected(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        """With three teams, the closest MMR pair should be matched."""
        a = make_team("Alpha",   1, mmr=1000)
        b = make_team("Bravo",   2, mmr=1050)  # closest to Alpha
        c = make_team("Charlie", 3, mmr=1300)
        for t in (a, b, c):
            mem_store.add_team(GID, t)
            t.state = TeamState.READY
            mem_store.enqueue(GID, t.name)

        await engine.try_match(GID)

        assert a.state == TeamState.MATCHED
        assert b.state == TeamState.MATCHED
        assert c.state == TeamState.READY  # left in queue

    async def test_mmr_threshold_blocks_fresh_far_teams(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        """Pairs outside MMR_MATCH_THRESHOLD must not be matched immediately."""
        a = make_team("Alpha", 1, mmr=1000)
        b = make_team("Bravo", 2, mmr=1000 + MMR_MATCH_THRESHOLD + 50)
        mem_store.add_team(GID, a)
        mem_store.add_team(GID, b)
        a.state = b.state = TeamState.READY
        mem_store.enqueue(GID, "Alpha")
        mem_store.enqueue(GID, "Bravo")

        await engine.try_match(GID)

        assert a.state == TeamState.READY   # not matched yet
        assert b.state == TeamState.READY

    async def test_rematch_guard_skips_last_opponent(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        """Teams that just played each other must not be immediately rematched."""
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        charlie = make_team("Charlie", 3)
        alpha.last_opponent = "Bravo"
        bravo.last_opponent = "Alpha"
        for t in (alpha, bravo, charlie):
            mem_store.add_team(GID, t)
            t.state = TeamState.READY
            mem_store.enqueue(GID, t.name)

        await engine.try_match(GID)

        # Charlie must be matched with one of Alpha or Bravo — not Alpha vs Bravo
        match_id = charlie.current_match_id
        assert match_id is not None, "Charlie should be matched"
        match = mem_store.get_match(GID, match_id)
        assert match is not None
        pair = {match.team1_name, match.team2_name}
        assert pair != {"Alpha", "Bravo"}

    async def test_rematch_guard_with_only_pair_stays_blocked(
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

        # cooldown=0 means the guard blocks, teams stay in queue
        assert mem_store.queue_size(GID) == 2
        assert alpha.state == TeamState.READY
        assert bravo.state == TeamState.READY

    async def test_modular_same_group_size_matched(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        """In modular mode, two teams with the same group_size are matched."""
        mem_store.set_cfg(EventConfig(
            composition_type="modular",
            min_group_size=1, max_group_size=5,
            mmr_enabled=False,
            matchmaking_channel_id=111111111111111111,
            broadcast_channel_id=222222222222222222,
        ))
        alpha = make_team("Alpha", 1, group_size=3)
        bravo = make_team("Bravo", 2, group_size=3)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        alpha.state = bravo.state = TeamState.READY
        mem_store.enqueue(GID, "Alpha")
        mem_store.enqueue(GID, "Bravo")

        await engine.try_match(GID)

        assert alpha.state == TeamState.MATCHED
        assert bravo.state == TeamState.MATCHED
        assert mem_store.queue_size(GID) == 0

    async def test_modular_different_group_size_not_matched(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        """In modular mode, teams with different group_sizes are never paired."""
        mem_store.set_cfg(EventConfig(
            composition_type="modular",
            min_group_size=1, max_group_size=5,
            mmr_enabled=False,
            matchmaking_channel_id=111111111111111111,
            broadcast_channel_id=222222222222222222,
        ))
        alpha = make_team("Alpha", 1, group_size=2)
        bravo = make_team("Bravo", 2, group_size=5)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        alpha.state = bravo.state = TeamState.READY
        mem_store.enqueue(GID, "Alpha")
        mem_store.enqueue(GID, "Bravo")

        await engine.try_match(GID)

        assert alpha.state == TeamState.READY
        assert bravo.state == TeamState.READY
        assert mem_store.queue_size(GID) == 2

    async def test_modular_queue_by_group_size_reflects_queue(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        """queue_by_group_size returns correct per-size buckets."""
        alpha = make_team("Alpha", 1, group_size=3)
        bravo = make_team("Bravo", 2, group_size=3)
        charlie = make_team("Charlie", 3, group_size=5)
        for t in (alpha, bravo, charlie):
            mem_store.add_team(GID, t)
            t.state = TeamState.READY
            mem_store.enqueue(GID, t.name)

        grouped = mem_store.queue_by_group_size(GID)
        assert set(grouped.keys()) == {3, 5}
        assert set(grouped[3]) == {"Alpha", "Bravo"}
        assert grouped[5] == ["Charlie"]


# ── Accept / cancel / end ────────────────────────────────────────────────────

class TestAcceptMatch:
    async def test_partial_accept_does_not_activate(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        match = Match(id="TST00001", team1_name="Alpha", team2_name="Bravo")
        mem_store.add_match(GID, match)
        alpha.state = bravo.state = TeamState.MATCHED
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.accept_match(GID, match, "Alpha")

        assert match.team1_accepted is True
        assert match.active is False
        assert alpha.has_accepted is True

    async def test_both_accept_activates_match(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        match = Match(id="TST00002", team1_name="Alpha", team2_name="Bravo")
        mem_store.add_match(GID, match)
        alpha.state = bravo.state = TeamState.MATCHED
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.accept_match(GID, match, "Alpha")
        await engine.accept_match(GID, match, "Bravo")

        assert match.active is True
        assert alpha.state == TeamState.IN_MATCH
        assert bravo.state == TeamState.IN_MATCH


class TestCancelMatch:
    async def test_cancel_resets_both_teams_to_idle(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        match = Match(id="CAN00001", team1_name="Alpha", team2_name="Bravo")
        mem_store.add_match(GID, match)
        alpha.state = bravo.state = TeamState.MATCHED
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.cancel_match(GID, match, reason="declined by Bravo")

        assert alpha.state == TeamState.IDLE
        assert bravo.state == TeamState.IDLE
        assert mem_store.get_match(GID, match.id) is None

    async def test_cancel_does_not_set_rematch_guard(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        match = Match(id="CAN00002", team1_name="Alpha", team2_name="Bravo")
        mem_store.add_match(GID, match)
        alpha.state = bravo.state = TeamState.MATCHED
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.cancel_match(GID, match, reason="test")

        assert alpha.last_opponent is None
        assert bravo.last_opponent is None


class TestEndMatch:
    async def test_end_match_resets_teams_to_idle(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        match = Match(id="END00001", team1_name="Alpha", team2_name="Bravo", active=True)
        mem_store.add_match(GID, match)
        alpha.state = bravo.state = TeamState.IN_MATCH
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.end_match(GID, match, ended_by="Alpha", winner_name="Alpha")

        assert alpha.state == TeamState.IDLE
        assert bravo.state == TeamState.IDLE
        assert mem_store.get_match(GID, match.id) is None

    async def test_end_match_applies_elo(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1, mmr=1000)
        bravo = make_team("Bravo", 2, mmr=1000)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        match = Match(id="END00002", team1_name="Alpha", team2_name="Bravo", active=True)
        mem_store.add_match(GID, match)
        alpha.state = bravo.state = TeamState.IN_MATCH
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.end_match(GID, match, ended_by="Alpha", winner_name="Alpha")

        assert alpha.mmr > 1000
        assert bravo.mmr < 1000
        assert alpha.wins == 1
        assert bravo.losses == 1

    async def test_end_match_without_winner_leaves_mmr_unchanged(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1, mmr=1000)
        bravo = make_team("Bravo", 2, mmr=1000)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        match = Match(id="END00003", team1_name="Alpha", team2_name="Bravo", active=True)
        mem_store.add_match(GID, match)
        alpha.state = bravo.state = TeamState.IN_MATCH
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.end_match(GID, match, ended_by="Alpha")  # no winner_name

        assert alpha.mmr == 1000
        assert bravo.mmr == 1000
        assert alpha.wins == 0
        assert bravo.losses == 0

    async def test_end_match_sets_rematch_guard(
        self, mem_store: _InMemoryGuildStore, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        mem_store.add_team(GID, alpha)
        mem_store.add_team(GID, bravo)
        match = Match(id="END00004", team1_name="Alpha", team2_name="Bravo", active=True)
        mem_store.add_match(GID, match)
        alpha.state = bravo.state = TeamState.IN_MATCH
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.end_match(GID, match, ended_by="Alpha", winner_name="Alpha")

        assert alpha.last_opponent == "Bravo"
        assert bravo.last_opponent == "Alpha"
