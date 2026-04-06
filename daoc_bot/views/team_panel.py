"""Context-aware panel view for team leaders.

The panel is posted inside each leader's private thread and edited in-place
by the engine whenever state changes.  Leaders never need to reopen a command
— the buttons update automatically.

Button set depends on team state:

  IDLE     → [🔵 Get a Match]
  READY    → [⏸️  Unready]
  MATCHED  → [✅ Accept Match]          (or [⏳ Waiting…] after accepting)
  IN_MATCH → [🏆 We Won]  [💀 We Lost]

All buttons are role-gated to ``settings.team_leader_role_name`` and only
respond to the registered leader of that team.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from daoc_bot import event_log
from daoc_bot.config import settings
from daoc_bot.guild_store import guild_store
from daoc_bot.models import Team, TeamState

if TYPE_CHECKING:
    from daoc_bot.engine import MatchmakingEngine

# ── Role helper ───────────────────────────────────────────────────────────────

def has_leader_role(interaction: discord.Interaction) -> bool:
    """Return True if the caller has the configured Team Leader role.

    If TEAM_LEADER_ROLE_NAME is set to ``*`` or left empty, everyone passes.
    """
    role_name = settings.team_leader_role_name
    if not role_name or role_name == "*":
        return True
    return any(r.name == role_name for r in interaction.user.roles)  # type: ignore[union-attr]


# ── View factory ──────────────────────────────────────────────────────────────

def view_for_state(
    team: Team, engine: MatchmakingEngine, guild_id: int
) -> discord.ui.View:
    """Return the correct button view for the team's current state."""
    if team.state == TeamState.IDLE:
        return _IdleView(team.name, engine, guild_id)
    if team.state == TeamState.READY:
        return _ReadyView(team.name, engine, guild_id)
    if team.state == TeamState.MATCHED:
        if team.has_accepted:
            return _MatchedWaitingView(team.name, engine, guild_id)
        return _MatchedView(team.name, engine, guild_id)
    if team.state == TeamState.IN_MATCH:
        return _InMatchView(team.name, engine, guild_id)
    return discord.ui.View()


# ── Base view ─────────────────────────────────────────────────────────────────

class _BaseLeaderView(discord.ui.View):
    """Shared guard logic for all leader panel views."""

    def __init__(
        self, team_name: str, engine: MatchmakingEngine, guild_id: int
    ) -> None:
        super().__init__(timeout=None)
        self.team_name = team_name
        self.engine = engine
        self.guild_id = guild_id

    async def _resolve(self, interaction: discord.Interaction) -> Team | None:
        """Validate role and leadership; return the Team or None on failure."""
        if not has_leader_role(interaction):
            await interaction.response.send_message(
                f"❌  You need the **{settings.team_leader_role_name}** role.",
                ephemeral=True,
            )
            return None

        team = guild_store.get_team(self.guild_id, self.team_name)
        if not team:
            await interaction.response.send_message(
                "❌  Team not found — it may have been removed by an admin.",
                ephemeral=True,
            )
            return None

        if interaction.user.id != team.leader_id:
            await interaction.response.send_message(
                "❌  Only the **team leader** can use these buttons.",
                ephemeral=True,
            )
            return None

        return team


# ── State-specific views ──────────────────────────────────────────────────────

class _IdleView(_BaseLeaderView):
    @discord.ui.button(
        label="🔵  Get a Match",
        style=discord.ButtonStyle.primary,
        custom_id="panel_ready",
    )
    async def ready(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """Queue the team for a match."""
        team = await self._resolve(interaction)
        if not team:
            return
        if team.state != TeamState.IDLE:
            await interaction.response.send_message(
                f"❌  Cannot ready up — current state is `{team.state.value}`.",
                ephemeral=True,
            )
            return

        team.state = TeamState.READY
        guild_store.enqueue(self.guild_id, team.name)
        guild_store.save_team(self.guild_id, team)

        event_log.queue_entered(self.guild_id, team.name, group_size=team.group_size)

        await interaction.response.defer()
        await self.engine.update_team_panel(self.guild_id, team)
        await self.engine.try_match(self.guild_id)

        # If still waiting after the first try, arm the MMR-relaxation timer
        if team.state == TeamState.READY:
            self.engine.schedule_mmr_relax(self.guild_id, team.name)


class _ReadyView(_BaseLeaderView):
    @discord.ui.button(
        label="⏸️  Unready",
        style=discord.ButtonStyle.secondary,
        custom_id="panel_unready",
    )
    async def unready(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """Remove the team from the queue."""
        team = await self._resolve(interaction)
        if not team:
            return
        if team.state != TeamState.READY:
            await interaction.response.send_message(
                "❌  You are not currently in the queue.", ephemeral=True
            )
            return

        team.state = TeamState.IDLE
        guild_store.dequeue(self.guild_id, team.name)
        guild_store.save_team(self.guild_id, team)
        self.engine._cancel_mmr_relax(self.guild_id, team.name)
        event_log.queue_left(self.guild_id, team.name, reason="unready", group_size=team.group_size)

        await interaction.response.defer()
        await self.engine.update_team_panel(self.guild_id, team)


class _MatchedView(_BaseLeaderView):
    @discord.ui.button(
        label="✅  Accept Match",
        style=discord.ButtonStyle.success,
        custom_id="panel_accept",
    )
    async def accept(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """Accept the proposed match."""
        team = await self._resolve(interaction)
        if not team:
            return

        if team.has_accepted:
            await interaction.response.send_message(
                "⏳  You already accepted — waiting for your opponent.",
                ephemeral=True,
            )
            return

        match = guild_store.get_match(self.guild_id, team.current_match_id or "")
        if not match:
            await interaction.response.send_message(
                "❌  Match not found — it may have timed out.", ephemeral=True
            )
            return

        await interaction.response.defer()
        await self.engine.accept_match(self.guild_id, match, team.name)


class _MatchedWaitingView(_BaseLeaderView):
    """Shown after this leader has accepted but is waiting for the opponent."""

    @discord.ui.button(
        label="⏳  Waiting for opponent…",
        style=discord.ButtonStyle.secondary,
        custom_id="panel_waiting",
        disabled=True,
    )
    async def waiting(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        pass  # disabled — never fires


class _InMatchView(_BaseLeaderView):
    """Shown while a match is active."""

    @discord.ui.button(
        label="🏆  We Won",
        style=discord.ButtonStyle.success,
        custom_id="panel_won",
    )
    async def we_won(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """Report a win for this team and update ratings."""
        team = await self._resolve(interaction)
        if not team:
            return

        match = guild_store.get_match(self.guild_id, team.current_match_id or "")
        if not match or not match.active:
            await interaction.response.send_message(
                "❌  No active match found — it may already be closed.",
                ephemeral=True,
            )
            return

        match.active = False
        await interaction.response.defer()
        await self.engine.end_match(
            self.guild_id, match, ended_by=team.name, winner_name=team.name
        )

    @discord.ui.button(
        label="💀  We Lost",
        style=discord.ButtonStyle.danger,
        custom_id="panel_lost",
    )
    async def we_lost(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """Report a loss for this team and update ratings."""
        team = await self._resolve(interaction)
        if not team:
            return

        match = guild_store.get_match(self.guild_id, team.current_match_id or "")
        if not match or not match.active:
            await interaction.response.send_message(
                "❌  No active match found — it may already be closed.",
                ephemeral=True,
            )
            return

        opponent_name = (
            match.team2_name if team.name == match.team1_name else match.team1_name
        )
        match.active = False
        await interaction.response.defer()
        await self.engine.end_match(
            self.guild_id, match, ended_by=team.name, winner_name=opponent_name
        )
