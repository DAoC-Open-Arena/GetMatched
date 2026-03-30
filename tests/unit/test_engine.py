"""Unit tests for the matchmaking engine — ELO math, pairing algorithm, match lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from daoc_bot.engine import ELO_K, MMR_MATCH_THRESHOLD, MatchmakingEngine, _elo_update
from daoc_bot.models import Match, Team, TeamState
from daoc_bot.state import BotState
from tests.conftest import make_team


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
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        store.add_team(alpha)
        store.add_team(bravo)
        alpha.state = bravo.state = TeamState.READY
        store.enqueue("Alpha")
        store.enqueue("Bravo")

        await engine.try_match()

        assert alpha.state == TeamState.MATCHED
        assert bravo.state == TeamState.MATCHED
        assert store.queue_size == 0
        assert alpha.current_match_id is not None
        assert alpha.current_match_id == bravo.current_match_id

    async def test_fewer_than_two_teams_is_noop(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        store.add_team(make_team("Alpha", 1))
        store.enqueue("Alpha")
        await engine.try_match()
        assert store.queue_size == 1  # unchanged

    async def test_closest_mmr_pair_selected(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        """With three teams, the closest MMR pair should be matched."""
        a = make_team("Alpha",   1, mmr=1000)
        b = make_team("Bravo",   2, mmr=1050)  # closest to Alpha
        c = make_team("Charlie", 3, mmr=1300)
        for t in (a, b, c):
            store.add_team(t)
            t.state = TeamState.READY
            store.enqueue(t.name)

        await engine.try_match()

        assert a.state == TeamState.MATCHED
        assert b.state == TeamState.MATCHED
        assert c.state == TeamState.READY  # left in queue

    async def test_mmr_threshold_blocks_fresh_far_teams(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        """Pairs outside MMR_MATCH_THRESHOLD must not be matched immediately."""
        a = make_team("Alpha", 1, mmr=1000)
        b = make_team("Bravo", 2, mmr=1000 + MMR_MATCH_THRESHOLD + 50)
        store.add_team(a)
        store.add_team(b)
        a.state = b.state = TeamState.READY
        store.enqueue("Alpha")
        store.enqueue("Bravo")

        await engine.try_match()

        assert a.state == TeamState.READY   # not matched yet
        assert b.state == TeamState.READY

    async def test_rematch_guard_skips_last_opponent(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        """Teams that just played each other must not be immediately rematched."""
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        charlie = make_team("Charlie", 3)
        alpha.last_opponent = "Bravo"
        bravo.last_opponent = "Alpha"
        for t in (alpha, bravo, charlie):
            store.add_team(t)
            t.state = TeamState.READY
            store.enqueue(t.name)

        await engine.try_match()

        # Charlie must be matched with one of Alpha or Bravo — not Alpha vs Bravo
        match_id = charlie.current_match_id
        assert match_id is not None, "Charlie should be matched"
        match = store.get_match(match_id)
        assert match is not None
        pair = {match.team1_name, match.team2_name}
        assert pair != {"Alpha", "Bravo"}

    async def test_rematch_guard_auto_lifts_when_only_pair(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        """With only two teams that are rematch-blocked, the guard must lift."""
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        alpha.last_opponent = "Bravo"
        bravo.last_opponent = "Alpha"
        store.add_team(alpha)
        store.add_team(bravo)
        alpha.state = bravo.state = TeamState.READY
        store.enqueue("Alpha")
        store.enqueue("Bravo")

        await engine.try_match()

        assert alpha.state == TeamState.MATCHED
        assert bravo.state == TeamState.MATCHED
        assert alpha.last_opponent is None
        assert bravo.last_opponent is None


# ── Accept / cancel / end ────────────────────────────────────────────────────

class TestAcceptMatch:
    async def test_partial_accept_does_not_activate(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        store.add_team(alpha)
        store.add_team(bravo)
        match = Match(id="TST00001", team1_name="Alpha", team2_name="Bravo")
        store.add_match(match)
        alpha.state = bravo.state = TeamState.MATCHED
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.accept_match(match, "Alpha")

        assert match.team1_accepted is True
        assert match.active is False
        assert alpha.has_accepted is True

    async def test_both_accept_activates_match(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        store.add_team(alpha)
        store.add_team(bravo)
        match = Match(id="TST00002", team1_name="Alpha", team2_name="Bravo")
        store.add_match(match)
        alpha.state = bravo.state = TeamState.MATCHED
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.accept_match(match, "Alpha")
        await engine.accept_match(match, "Bravo")

        assert match.active is True
        assert alpha.state == TeamState.IN_MATCH
        assert bravo.state == TeamState.IN_MATCH


class TestCancelMatch:
    async def test_cancel_resets_both_teams_to_idle(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        store.add_team(alpha)
        store.add_team(bravo)
        match = Match(id="CAN00001", team1_name="Alpha", team2_name="Bravo")
        store.add_match(match)
        alpha.state = bravo.state = TeamState.MATCHED
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.cancel_match(match, reason="declined by Bravo")

        assert alpha.state == TeamState.IDLE
        assert bravo.state == TeamState.IDLE
        assert store.get_match(match.id) is None

    async def test_cancel_does_not_set_rematch_guard(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        store.add_team(alpha)
        store.add_team(bravo)
        match = Match(id="CAN00002", team1_name="Alpha", team2_name="Bravo")
        store.add_match(match)
        alpha.state = bravo.state = TeamState.MATCHED
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.cancel_match(match, reason="test")

        assert alpha.last_opponent is None
        assert bravo.last_opponent is None


class TestEndMatch:
    async def test_end_match_resets_teams_to_idle(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        store.add_team(alpha)
        store.add_team(bravo)
        match = Match(id="END00001", team1_name="Alpha", team2_name="Bravo", active=True)
        store.add_match(match)
        alpha.state = bravo.state = TeamState.IN_MATCH
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.end_match(match, ended_by="Alpha", winner_name="Alpha")

        assert alpha.state == TeamState.IDLE
        assert bravo.state == TeamState.IDLE
        assert store.get_match(match.id) is None

    async def test_end_match_applies_elo(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1, mmr=1000)
        bravo = make_team("Bravo", 2, mmr=1000)
        store.add_team(alpha)
        store.add_team(bravo)
        match = Match(id="END00002", team1_name="Alpha", team2_name="Bravo", active=True)
        store.add_match(match)
        alpha.state = bravo.state = TeamState.IN_MATCH
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.end_match(match, ended_by="Alpha", winner_name="Alpha")

        assert alpha.mmr > 1000
        assert bravo.mmr < 1000
        assert alpha.wins == 1
        assert bravo.losses == 1

    async def test_end_match_without_winner_leaves_mmr_unchanged(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1, mmr=1000)
        bravo = make_team("Bravo", 2, mmr=1000)
        store.add_team(alpha)
        store.add_team(bravo)
        match = Match(id="END00003", team1_name="Alpha", team2_name="Bravo", active=True)
        store.add_match(match)
        alpha.state = bravo.state = TeamState.IN_MATCH
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.end_match(match, ended_by="Alpha")  # no winner_name

        assert alpha.mmr == 1000
        assert bravo.mmr == 1000
        assert alpha.wins == 0
        assert bravo.losses == 0

    async def test_end_match_sets_rematch_guard(
        self, store: BotState, engine: MatchmakingEngine
    ) -> None:
        alpha = make_team("Alpha", 1)
        bravo = make_team("Bravo", 2)
        store.add_team(alpha)
        store.add_team(bravo)
        match = Match(id="END00004", team1_name="Alpha", team2_name="Bravo", active=True)
        store.add_match(match)
        alpha.state = bravo.state = TeamState.IN_MATCH
        alpha.current_match_id = bravo.current_match_id = match.id

        await engine.end_match(match, ended_by="Alpha", winner_name="Alpha")

        assert alpha.last_opponent == "Bravo"
        assert bravo.last_opponent == "Alpha"
