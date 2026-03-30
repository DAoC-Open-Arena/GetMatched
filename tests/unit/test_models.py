"""Unit tests for domain models (Team, Match, TeamState)."""

from __future__ import annotations

import pytest

from daoc_bot.models import Match, Team, TeamState


class TestTeamState:
    def test_enum_values_are_strings(self) -> None:
        assert TeamState.IDLE.value == "idle"
        assert TeamState.READY.value == "ready"
        assert TeamState.MATCHED.value == "matched"
        assert TeamState.IN_MATCH.value == "in_match"

    def test_all_four_states_exist(self) -> None:
        states = {s for s in TeamState}
        assert states == {TeamState.IDLE, TeamState.READY, TeamState.MATCHED, TeamState.IN_MATCH}


class TestTeam:
    def test_default_state_is_idle(self) -> None:
        team = Team(name="Alpha", leader_id=1, member_ids=[1])
        assert team.state == TeamState.IDLE

    def test_default_mmr_is_1000(self) -> None:
        team = Team(name="Alpha", leader_id=1, member_ids=[1])
        assert team.mmr == 1000

    def test_default_wins_losses_zero(self) -> None:
        team = Team(name="Alpha", leader_id=1, member_ids=[1])
        assert team.wins == 0
        assert team.losses == 0

    def test_optional_fields_default_to_none(self) -> None:
        team = Team(name="Alpha", leader_id=1, member_ids=[1])
        assert team.last_opponent is None
        assert team.current_match_id is None
        assert team.current_opponent is None
        assert team.panel_thread_id is None
        assert team.panel_message_id is None

    def test_has_accepted_defaults_false(self) -> None:
        team = Team(name="Alpha", leader_id=1, member_ids=[1])
        assert team.has_accepted is False

    def test_member_ids_list_not_shared_between_instances(self) -> None:
        """Each Team instance must own its own member_ids list."""
        t1 = Team(name="Alpha", leader_id=1, member_ids=[1])
        t2 = Team(name="Bravo", leader_id=2, member_ids=[2])
        t1.member_ids.append(99)
        assert 99 not in t2.member_ids

    def test_state_mutation(self) -> None:
        team = Team(name="Alpha", leader_id=1, member_ids=[1])
        team.state = TeamState.READY
        assert team.state == TeamState.READY


class TestMatch:
    def test_default_acceptance_flags_false(self) -> None:
        match = Match(id="ABCD1234", team1_name="Alpha", team2_name="Bravo")
        assert match.team1_accepted is False
        assert match.team2_accepted is False

    def test_both_accepted_requires_both_flags(self) -> None:
        match = Match(id="ABCD1234", team1_name="Alpha", team2_name="Bravo")
        assert match.both_accepted is False

        match.team1_accepted = True
        assert match.both_accepted is False

        match.team2_accepted = True
        assert match.both_accepted is True

    def test_active_defaults_false(self) -> None:
        match = Match(id="ABCD1234", team1_name="Alpha", team2_name="Bravo")
        assert match.active is False

    def test_winner_name_defaults_none(self) -> None:
        match = Match(id="ABCD1234", team1_name="Alpha", team2_name="Bravo")
        assert match.winner_name is None

    def test_message_ids_default_none(self) -> None:
        match = Match(id="ABCD1234", team1_name="Alpha", team2_name="Bravo")
        assert match.proposal_message_id is None
        assert match.active_message_id is None
