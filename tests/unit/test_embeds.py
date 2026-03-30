"""Unit tests for Discord embed builders."""

from __future__ import annotations

import discord
import pytest

from daoc_bot.embeds import active_match, match_ended, match_proposal, team_panel
from daoc_bot.models import Match, Team, TeamState
from tests.conftest import make_team


@pytest.fixture()
def alpha() -> Team:
    return make_team("Alpha", leader_id=1001, mmr=1100)


@pytest.fixture()
def bravo() -> Team:
    return make_team("Bravo", leader_id=2002, mmr=950)


@pytest.fixture()
def basic_match(alpha: Team, bravo: Team) -> Match:
    return Match(id="EMBED001", team1_name="Alpha", team2_name="Bravo")


class TestTeamPanelEmbed:
    def test_returns_embed(self, alpha: Team) -> None:
        embed = team_panel(alpha)
        assert isinstance(embed, discord.Embed)

    def test_title_contains_team_name(self, alpha: Team) -> None:
        embed = team_panel(alpha)
        assert "Alpha" in (embed.title or "")

    def test_idle_state_has_grey_colour(self, alpha: Team) -> None:
        alpha.state = TeamState.IDLE
        embed = team_panel(alpha)
        assert embed.color == discord.Color.greyple()

    def test_ready_state_has_blue_colour(self, alpha: Team) -> None:
        alpha.state = TeamState.READY
        embed = team_panel(alpha)
        assert embed.color == discord.Color.blue()

    def test_matched_state_has_gold_colour(self, alpha: Team) -> None:
        alpha.state = TeamState.MATCHED
        embed = team_panel(alpha)
        assert embed.color == discord.Color.gold()

    def test_mmr_appears_in_fields(self, alpha: Team) -> None:
        embed = team_panel(alpha)
        field_values = " ".join(f.value or "" for f in embed.fields)
        assert "1100" in field_values

    def test_win_rate_shown_when_record_exists(self, alpha: Team) -> None:
        alpha.wins = 3
        alpha.losses = 1
        embed = team_panel(alpha)
        field_values = " ".join(f.value or "" for f in embed.fields)
        assert "75%" in field_values

    def test_no_win_rate_shown_when_no_games(self, alpha: Team) -> None:
        embed = team_panel(alpha)
        field_values = " ".join(f.value or "" for f in embed.fields)
        assert "no matches" in field_values

    def test_last_opponent_shows_in_footer(self, alpha: Team) -> None:
        alpha.last_opponent = "Bravo"
        embed = team_panel(alpha)
        assert embed.footer is not None
        assert "Bravo" in (embed.footer.text or "")


class TestMatchProposalEmbed:
    def test_returns_embed(
        self, basic_match: Match, alpha: Team, bravo: Team
    ) -> None:
        embed = match_proposal(basic_match, {"Alpha": alpha, "Bravo": bravo})
        assert isinstance(embed, discord.Embed)

    def test_title_indicates_match_found(
        self, basic_match: Match, alpha: Team, bravo: Team
    ) -> None:
        embed = match_proposal(basic_match, {"Alpha": alpha, "Bravo": bravo})
        assert "Match Found" in (embed.title or "")

    def test_mmr_difference_shown(
        self, basic_match: Match, alpha: Team, bravo: Team
    ) -> None:
        embed = match_proposal(basic_match, {"Alpha": alpha, "Bravo": bravo})
        diff = abs(alpha.mmr - bravo.mmr)
        assert str(diff) in (embed.description or "")

    def test_match_id_in_footer(
        self, basic_match: Match, alpha: Team, bravo: Team
    ) -> None:
        embed = match_proposal(basic_match, {"Alpha": alpha, "Bravo": bravo})
        assert embed.footer is not None
        assert "EMBED001" in (embed.footer.text or "")


class TestActiveMatchEmbed:
    def test_returns_embed(
        self, basic_match: Match, alpha: Team, bravo: Team
    ) -> None:
        embed = active_match(basic_match, {"Alpha": alpha, "Bravo": bravo})
        assert isinstance(embed, discord.Embed)

    def test_both_team_names_in_title(
        self, basic_match: Match, alpha: Team, bravo: Team
    ) -> None:
        embed = active_match(basic_match, {"Alpha": alpha, "Bravo": bravo})
        assert "Alpha" in (embed.title or "")
        assert "Bravo" in (embed.title or "")


class TestMatchEndedEmbed:
    def test_returns_embed(
        self, basic_match: Match, alpha: Team, bravo: Team
    ) -> None:
        embed = match_ended(basic_match, {"Alpha": alpha, "Bravo": bravo})
        assert isinstance(embed, discord.Embed)

    def test_title_indicates_match_over(
        self, basic_match: Match, alpha: Team, bravo: Team
    ) -> None:
        embed = match_ended(basic_match, {"Alpha": alpha, "Bravo": bravo})
        assert "Match Over" in (embed.title or "") or "Over" in (embed.title or "")
