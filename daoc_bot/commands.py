"""Slash command definitions for the matchmaking bot."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from daoc_bot import event_log
from daoc_bot.config import settings
from daoc_bot.engine import MatchmakingEngine
from daoc_bot.models import Team, TeamState
from daoc_bot.simulation import SimulationSuite
from daoc_bot.state import store
from daoc_bot.views.team_panel import has_leader_role

logger = logging.getLogger(__name__)


def register(bot: commands.Bot, engine: MatchmakingEngine) -> None:
    """Attach all slash commands to ``bot.tree``."""

    # ── Guards ────────────────────────────────────────────────────────────────

    def leader_only(interaction: discord.Interaction) -> bool:
        return has_leader_role(interaction)

    def admin_only(interaction: discord.Interaction) -> bool:
        return (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )

    leader_check = app_commands.check(leader_only)
    admin_check  = app_commands.check(admin_only)

    # ── /register_team ────────────────────────────────────────────────────────

    @bot.tree.command(
        name="register_team",
        description="Register your team for tonight's event",
    )
    @leader_check
    @app_commands.describe(groupleader_character_name="Your in-game character name")
    async def cmd_register_team(
        interaction: discord.Interaction,
        groupleader_character_name: str,
    ) -> None:
        """Register a new team and create a private thread with a live panel."""
        leader = interaction.user

        if store.is_leader(leader.id):
            existing = store.get_team_by_leader(leader.id)
            await interaction.response.send_message(
                f"❌  You are registered as **{existing.name if existing else 'your team'}**. "
                "Use `/unregister_team` first.",
                ephemeral=True,
            )
            return

        if store.team_exists(groupleader_character_name):
            await interaction.response.send_message(
                f"❌  A character named **{groupleader_character_name}** is already registered. "
                "If that's not you, contact an admin.",
                ephemeral=True,
            )
            return

        team = Team(
            name=groupleader_character_name,
            leader_id=leader.id,
            member_ids=[leader.id],
        )
        store.add_team(team)

        await interaction.response.defer(ephemeral=True)

        if not isinstance(leader, discord.Member):
            await interaction.followup.send("❌  This command must be used in a server.", ephemeral=True)
            return
        await engine.create_team_panel(team, leader)

        await interaction.followup.send(
            f"✅  Team **{groupleader_character_name}** registered!\n"
            f"A private thread has been created for you in "
            f"<#{settings.matchmaking_channel_id}> — use the buttons there to "
            f"ready up, accept matches, and report results.",
            ephemeral=True,
        )
        event_log.team_registered(groupleader_character_name, leader.id)
        logger.info("Team registered: %s (leader=%d)", groupleader_character_name, leader.id)

    # ── /unregister_team ──────────────────────────────────────────────────────

    @bot.tree.command(
        name="unregister_team",
        description="Remove your team from tonight's event",
    )
    @leader_check
    async def cmd_unregister_team(interaction: discord.Interaction) -> None:
        """Deregister the calling user's team and delete their private thread."""
        team = store.get_team_by_leader(interaction.user.id)
        if not team:
            await interaction.response.send_message(
                "❌  You don't have a registered team.", ephemeral=True
            )
            return

        if team.state in (TeamState.MATCHED, TeamState.IN_MATCH):
            await interaction.response.send_message(
                "❌  You cannot unregister while a match is active.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        name = team.name
        await engine.delete_team_panel(team)
        event_log.team_unregistered(name, interaction.user.id)
        store.remove_team(name)

        await interaction.followup.send(
            f"✅  Team **{name}** has been removed and your panel thread deleted.",
            ephemeral=True,
        )

    # ── /queue_status ─────────────────────────────────────────────────────────

    @bot.tree.command(
        name="queue_status",
        description="Show teams currently waiting for a match",
    )
    @leader_check
    async def cmd_queue_status(interaction: discord.Interaction) -> None:
        queue = store.queue
        if not queue:
            await interaction.response.send_message(
                "🟡  The queue is empty.", ephemeral=True
            )
            return
        names = "  |  ".join(f"**{n}**" for n in queue)
        await interaction.response.send_message(
            f"🟢  In queue ({len(queue)}): {names}", ephemeral=True
        )

    # ── /match_status ─────────────────────────────────────────────────────────

    @bot.tree.command(
        name="match_status",
        description="Show matches currently in progress",
    )
    @leader_check
    async def cmd_match_status(interaction: discord.Interaction) -> None:
        active = store.active_matches()
        if not active:
            await interaction.response.send_message(
                "No matches in progress right now.", ephemeral=True
            )
            return
        embed = discord.Embed(title="⚔️  Matches in Progress", color=discord.Color.red())
        for m in active:
            embed.add_field(
                name=f"{m.team1_name}  vs  {m.team2_name}",
                value=f"ID: `{m.id}`",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /leaderboard ──────────────────────────────────────────────────────────

    @bot.tree.command(
        name="leaderboard",
        description="[Admin] Show the MMR leaderboard for all registered teams",
    )
    @admin_check
    async def cmd_leaderboard(interaction: discord.Interaction) -> None:
        """Display all teams sorted by MMR, highest first."""
        all_teams = store.all_teams()
        if not all_teams:
            await interaction.response.send_message(
                "No teams registered yet.", ephemeral=True
            )
            return

        ranked = sorted(all_teams, key=lambda t: t.mmr, reverse=True)
        medals = ["🥇", "🥈", "🥉"]

        embed = discord.Embed(
            title="🏆  MMR Leaderboard",
            description="Teams ranked by current ELO rating.",
            color=discord.Color.gold(),
        )
        for i, t in enumerate(ranked):
            total = t.wins + t.losses
            wr_str = f"{t.wins / total * 100:.0f}% WR" if total > 0 else "no matches"
            prefix = medals[i] if i < 3 else f"#{i + 1}"
            state_icon = {
                TeamState.IDLE:     "⏸️",
                TeamState.READY:    "🟢",
                TeamState.MATCHED:  "🟡",
                TeamState.IN_MATCH: "🔴",
            }[t.state]
            embed.add_field(
                name=f"{prefix}  {t.name}  {state_icon}",
                value=(
                    f"⚡ **{t.mmr}** MMR  |  {t.wins}W – {t.losses}L  |  {wr_str}\n"
                    f"Leader: <@{t.leader_id}>"
                ),
                inline=False,
            )
        embed.set_footer(text=f"{len(ranked)} team(s) registered")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /admin_reset_team ─────────────────────────────────────────────────────

    @bot.tree.command(
        name="admin_reset_team",
        description="[Admin] Force a team back to IDLE",
    )
    @app_commands.describe(groupleader_character_name="Name of the team to reset")
    @admin_check
    async def cmd_admin_reset_team(
        interaction: discord.Interaction, groupleader_character_name: str
    ) -> None:
        team = store.get_team(groupleader_character_name)
        if not team:
            await interaction.response.send_message(
                f"❌  No team named **{groupleader_character_name}**.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        old_state = team.state

        if team.current_match_id:
            match = store.get_match(team.current_match_id)
            if match:
                await engine.cancel_match(
                    match, reason=f"admin force-reset of **{groupleader_character_name}**"
                )
                await interaction.followup.send(
                    f"✅  Match cancelled and both teams reset to IDLE.\n"
                    f"*(triggered by admin reset of **{groupleader_character_name}**, "
                    f"previous state: `{old_state.value}`)*",
                    ephemeral=True,
                )
                logger.warning(
                    "ADMIN: %s reset team '%s' (state=%s), match %s cancelled.",
                    interaction.user, groupleader_character_name, old_state.value, match.id,
                )
                return

        store.dequeue(groupleader_character_name)
        engine._cancel_mmr_relax(groupleader_character_name)
        team.state = TeamState.IDLE
        team.current_match_id = None
        await engine.update_team_panel(team)

        await interaction.followup.send(
            f"✅  **{groupleader_character_name}** reset to IDLE. "
            f"*(previous state: `{old_state.value}`)*",
            ephemeral=True,
        )
        logger.warning(
            "ADMIN: %s reset team '%s' (state=%s).",
            interaction.user, groupleader_character_name, old_state.value,
        )

    # ── /admin_cancel_match ───────────────────────────────────────────────────

    @bot.tree.command(
        name="admin_cancel_match",
        description="[Admin] Force-cancel a match by ID",
    )
    @app_commands.describe(match_id="Match ID from /match_status")
    @admin_check
    async def cmd_admin_cancel_match(
        interaction: discord.Interaction, match_id: str
    ) -> None:
        match = store.get_match(match_id.upper())
        if not match:
            await interaction.response.send_message(
                f"❌  No match with ID `{match_id}`.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await engine.cancel_match(
            match,
            reason=f"cancelled by admin {interaction.user.display_name}",
        )
        await interaction.followup.send(
            f"✅  Match `{match_id}` cancelled. Both teams reset to IDLE.",
            ephemeral=True,
        )
        logger.warning(
            "ADMIN: %s force-cancelled match %s (%s vs %s).",
            interaction.user, match_id, match.team1_name, match.team2_name,
        )

    # ── /admin_remove_team ────────────────────────────────────────────────────

    @bot.tree.command(
        name="admin_remove_team",
        description="[Admin] Completely remove a team from the event",
    )
    @app_commands.describe(groupleader_character_name="Name of the team to remove")
    @admin_check
    async def cmd_admin_remove_team(
        interaction: discord.Interaction, groupleader_character_name: str
    ) -> None:
        team = store.get_team(groupleader_character_name)
        if not team:
            await interaction.response.send_message(
                f"❌  No team named **{groupleader_character_name}**.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        if team.current_match_id:
            match = store.get_match(team.current_match_id)
            if match:
                await engine.cancel_match(
                    match, reason=f"team **{groupleader_character_name}** removed by admin"
                )

        engine._cancel_mmr_relax(groupleader_character_name)
        await engine.delete_team_panel(team)
        store.remove_team(groupleader_character_name)

        await interaction.followup.send(
            f"✅  Team **{groupleader_character_name}** removed and their panel thread deleted.",
            ephemeral=True,
        )
        logger.warning("ADMIN: %s removed team '%s'.", interaction.user, groupleader_character_name)

    # ── /admin_list_teams ─────────────────────────────────────────────────────

    @bot.tree.command(
        name="admin_list_teams",
        description="[Admin] Show all registered teams and their current state",
    )
    @admin_check
    async def cmd_admin_list_teams(interaction: discord.Interaction) -> None:
        all_teams = store.all_teams()
        if not all_teams:
            await interaction.response.send_message(
                "No teams registered.", ephemeral=True
            )
            return

        state_icon = {
            TeamState.IDLE:     "⏸️",
            TeamState.READY:    "🟢",
            TeamState.MATCHED:  "🟡",
            TeamState.IN_MATCH: "🔴",
        }
        embed = discord.Embed(
            title="🛡️  All Registered Teams",
            color=discord.Color.blurple(),
        )
        for t in sorted(all_teams, key=lambda x: x.mmr, reverse=True):
            icon = state_icon[t.state]
            total = t.wins + t.losses
            wr_str = f"{t.wins / total * 100:.0f}% WR" if total > 0 else "—"
            match_info = f"  |  match `{t.current_match_id}`" if t.current_match_id else ""
            last_opp   = f"  |  last opp: {t.last_opponent}" if t.last_opponent else ""
            embed.add_field(
                name=f"{icon}  {t.name}  (MMR {t.mmr})",
                value=(
                    f"Leader: <@{t.leader_id}>  |  `{t.state.value}`"
                    f"  |  {t.wins}W – {t.losses}L  |  {wr_str}"
                    f"{match_info}{last_opp}"
                ),
                inline=False,
            )
        embed.set_footer(
            text=f"Queue: {store.queue}  |  Active matches: {len(store.active_matches())}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /admin_clear_rematch ──────────────────────────────────────────────────

    @bot.tree.command(
        name="admin_clear_rematch",
        description="[Admin] Clear the instant-rematch block between two teams",
    )
    @app_commands.describe(team1="First team name", team2="Second team name")
    @admin_check
    async def cmd_admin_clear_rematch(
        interaction: discord.Interaction, team1: str, team2: str
    ) -> None:
        t1 = store.get_team(team1)
        t2 = store.get_team(team2)
        missing = [n for n, t in [(team1, t1), (team2, t2)] if t is None]
        if missing:
            await interaction.response.send_message(
                f"❌  Team(s) not found: {', '.join(f'**{n}**' for n in missing)}",
                ephemeral=True,
            )
            return

        store.clear_last_opponents(team1, team2)
        await interaction.response.send_message(
            f"✅  Rematch block cleared between **{team1}** and **{team2}**.",
            ephemeral=True,
        )
        logger.warning(
            "ADMIN: %s cleared rematch block between '%s' and '%s'.",
            interaction.user, team1, team2,
        )

    # ── /admin_set_mmr ────────────────────────────────────────────────────────

    @bot.tree.command(
        name="admin_set_mmr",
        description="[Admin] Manually override a team's MMR",
    )
    @app_commands.describe(
        groupleader_character_name="Name of the team",
        mmr="New MMR value (positive integer)",
    )
    @admin_check
    async def cmd_admin_set_mmr(
        interaction: discord.Interaction,
        groupleader_character_name: str,
        mmr: int,
    ) -> None:
        if mmr <= 0:
            await interaction.response.send_message(
                "❌  MMR must be a positive integer.", ephemeral=True
            )
            return
        team = store.get_team(groupleader_character_name)
        if not team:
            await interaction.response.send_message(
                f"❌  No team named **{groupleader_character_name}**.", ephemeral=True
            )
            return

        old_mmr = team.mmr
        team.mmr = mmr
        await engine.update_team_panel(team)

        await interaction.response.send_message(
            f"✅  **{groupleader_character_name}** MMR set to **{mmr}** (was {old_mmr}).",
            ephemeral=True,
        )
        logger.warning(
            "ADMIN: %s set MMR for '%s': %d → %d.",
            interaction.user, groupleader_character_name, old_mmr, mmr,
        )

    # ── Global error handler ──────────────────────────────────────────────────

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                f"❌  You need the **{settings.team_leader_role_name}** role "
                "to use this command.",
                ephemeral=True,
            )
            return
        logger.exception(
            "Unhandled error in command '%s'",
            interaction.command.name if interaction.command else "unknown",
            exc_info=error,
        )
        msg = f"❌  Internal error: `{error}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

    # ── /run_tests ────────────────────────────────────────────────────────────

    @bot.tree.command(
        name="run_tests",
        description="[Admin] Run the full simulation suite and post live results",
    )
    @admin_check
    async def cmd_run_tests(interaction: discord.Interaction) -> None:
        """Run all matchmaking scenarios and post a live results embed."""
        await interaction.response.send_message(
            "🧪  Starting simulation suite — a live results embed will appear "
            "in this channel. Full detail is in the terminal log.",
            ephemeral=True,
        )

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send("❌  This command must be used in a text channel.", ephemeral=True)
            return
        suite   = SimulationSuite(channel=interaction.channel, engine=engine)
        results = await suite.run()

        passed = sum(1 for r in results if r.passed)
        total  = len(results)

        if passed == total:
            await interaction.followup.send(
                f"✅  All **{total}** scenarios passed.", ephemeral=True
            )
        else:
            failed = [r.name for r in results if not r.passed]
            await interaction.followup.send(
                f"❌  **{total - passed}/{total}** scenario(s) failed: "
                + ", ".join(f"**{n}**" for n in failed),
                ephemeral=True,
            )