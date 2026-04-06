"""Slash command definitions for the matchmaking bot."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from daoc_bot import event_log
from daoc_bot.config import settings
from daoc_bot.engine import MatchmakingEngine
from daoc_bot.guild_store import EventConfig, guild_store
from daoc_bot.models import Team, TeamState
from daoc_bot.views.team_panel import has_leader_role

logger = logging.getLogger(__name__)


# ── Temporary storage for multi-step /start_event flow ───────────────────────
# Keyed by (guild_id, user_id).  Each entry is a dict of partial EventConfig
# fields collected from the first modal, awaiting the optional second step.
# Entries older than 10 minutes are stale and should be ignored.

_pending_event_configs: dict[tuple[int, int], dict[str, Any]] = {}
_pending_event_config_ts: dict[tuple[int, int], float] = {}
_PENDING_TTL = 600  # 10 minutes


def _store_pending(guild_id: int, user_id: int, data: dict[str, Any]) -> None:
    key = (guild_id, user_id)
    _pending_event_configs[key] = data
    _pending_event_config_ts[key] = time.monotonic()


def _pop_pending(guild_id: int, user_id: int) -> Optional[dict[str, Any]]:
    key = (guild_id, user_id)
    ts = _pending_event_config_ts.get(key, 0.0)
    if time.monotonic() - ts > _PENDING_TTL:
        _pending_event_configs.pop(key, None)
        _pending_event_config_ts.pop(key, None)
        return None
    _pending_event_config_ts.pop(key, None)
    return _pending_event_configs.pop(key, None)


# ── Modal: basic event config (step 1 of /start_event) ───────────────────────

class _StartEventBasicModal(discord.ui.Modal, title="Start New Event — Basic Config"):
    composition_type: discord.ui.TextInput = discord.ui.TextInput(
        label="Composition type (fixed / modular)",
        default="fixed",
        max_length=8,
        required=True,
    )
    min_group_size: discord.ui.TextInput = discord.ui.TextInput(
        label="Min group size (modular only)",
        default="1",
        max_length=3,
        required=False,
    )
    max_group_size: discord.ui.TextInput = discord.ui.TextInput(
        label="Max group size (modular only)",
        default="1",
        max_length=3,
        required=False,
    )
    mmr_enabled: discord.ui.TextInput = discord.ui.TextInput(
        label="Enable MMR? (yes / no)",
        default="yes",
        max_length=3,
        required=True,
    )
    matchmaking_channel_id: discord.ui.TextInput = discord.ui.TextInput(
        label="Matchmaking channel ID",
        max_length=20,
        required=True,
    )

    def __init__(
        self,
        engine: MatchmakingEngine,
        default_mm_ch: int,
        default_bc_ch: int,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._default_bc_ch = default_bc_ch
        if default_mm_ch:
            self.matchmaking_channel_id.default = str(default_mm_ch)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "❌  This command must be used inside a server.", ephemeral=True
            )
            return

        # Parse and validate step-1 fields
        comp = self.composition_type.value.strip().lower()
        if comp not in ("fixed", "modular"):
            await interaction.response.send_message(
                "❌  Composition type must be `fixed` or `modular`.", ephemeral=True
            )
            return

        try:
            min_gs = int(self.min_group_size.value.strip() or "1")
            max_gs = int(self.max_group_size.value.strip() or "1")
        except ValueError:
            await interaction.response.send_message(
                "❌  Group sizes must be integers.", ephemeral=True
            )
            return

        if min_gs < 1 or max_gs < min_gs:
            await interaction.response.send_message(
                "❌  Group sizes must be ≥ 1 and min ≤ max.", ephemeral=True
            )
            return

        mmr_on = self.mmr_enabled.value.strip().lower() in ("yes", "y", "true", "1")

        try:
            mm_ch_id = int(self.matchmaking_channel_id.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "❌  Matchmaking channel ID must be a number.", ephemeral=True
            )
            return

        # Store partial config and show the "configure advanced / start with defaults" prompt
        partial: dict[str, Any] = {
            "composition_type": comp,
            "min_group_size": min_gs,
            "max_group_size": max_gs,
            "mmr_enabled": mmr_on,
            "matchmaking_channel_id": mm_ch_id,
            "broadcast_channel_id": self._default_bc_ch,
        }
        _store_pending(guild_id, interaction.user.id, partial)

        view = _StartEventStep2View(self._engine, self._default_bc_ch)
        comp_line = f"**Composition:** `{comp}`"
        if comp == "modular":
            comp_line += f"  |  group size: {min_gs}–{max_gs}"
        await interaction.response.send_message(
            f"Step 1 complete.\n"
            f"{comp_line}\n"
            f"**MMR:** {'enabled' if mmr_on else 'disabled'}\n"
            f"**Matchmaking channel:** <#{mm_ch_id}>\n\n"
            "Choose an option below to finish setup:",
            view=view,
            ephemeral=True,
        )


# ── Modal: advanced event config (step 2 of /start_event — optional) ─────────

class _StartEventAdvancedModal(discord.ui.Modal, title="Start New Event — Advanced Config"):
    broadcast_channel_id: discord.ui.TextInput = discord.ui.TextInput(
        label="Broadcast channel ID",
        max_length=20,
        required=True,
    )
    mmr_k_value: discord.ui.TextInput = discord.ui.TextInput(
        label="MMR K-factor (default 32)",
        default="32",
        max_length=4,
        required=False,
    )
    mmr_match_threshold: discord.ui.TextInput = discord.ui.TextInput(
        label="MMR match threshold (default 200)",
        default="200",
        max_length=5,
        required=False,
    )
    mmr_relax_seconds: discord.ui.TextInput = discord.ui.TextInput(
        label="MMR relax seconds (default 120)",
        default="120",
        max_length=5,
        required=False,
    )
    match_accept_timeout: discord.ui.TextInput = discord.ui.TextInput(
        label="Match accept timeout (default 60)",
        default="60",
        max_length=4,
        required=False,
    )

    def __init__(self, engine: MatchmakingEngine, default_bc_ch: int) -> None:
        super().__init__()
        self._engine = engine
        if default_bc_ch:
            self.broadcast_channel_id.default = str(default_bc_ch)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "❌  This command must be used inside a server.", ephemeral=True
            )
            return

        partial = _pop_pending(guild_id, interaction.user.id)
        if partial is None:
            await interaction.response.send_message(
                "❌  Setup session expired. Please run `/start_event` again.",
                ephemeral=True,
            )
            return

        try:
            bc_ch_id = int(self.broadcast_channel_id.value.strip())
            k = int(self.mmr_k_value.value.strip() or "32")
            threshold = int(self.mmr_match_threshold.value.strip() or "200")
            relax = int(self.mmr_relax_seconds.value.strip() or "120")
            timeout = int(self.match_accept_timeout.value.strip() or "60")
        except ValueError:
            await interaction.response.send_message(
                "❌  All advanced fields must be integers.", ephemeral=True
            )
            return

        errors = []
        if k < 1:
            errors.append("K-factor must be ≥ 1")
        if threshold < 0:
            errors.append("MMR threshold must be ≥ 0")
        if relax < 0:
            errors.append("MMR relax seconds must be ≥ 0")
        if timeout < 5:
            errors.append("Accept timeout must be ≥ 5 seconds")
        if errors:
            await interaction.response.send_message(
                "❌  " + "  |  ".join(errors), ephemeral=True
            )
            return

        partial.update({
            "broadcast_channel_id": bc_ch_id,
            "mmr_k_value": k,
            "mmr_match_threshold": threshold,
            "mmr_relax_seconds": relax,
            "match_accept_timeout": timeout,
        })

        await _finalize_event(interaction, guild_id, partial)


# ── Step-2 button view ────────────────────────────────────────────────────────

class _StartEventStep2View(discord.ui.View):
    def __init__(self, engine: MatchmakingEngine, default_bc_ch: int) -> None:
        super().__init__(timeout=300)
        self._engine = engine
        self._default_bc_ch = default_bc_ch

    @discord.ui.button(
        label="Configure advanced settings",
        style=discord.ButtonStyle.secondary,
    )
    async def advanced(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            _StartEventAdvancedModal(self._engine, self._default_bc_ch)
        )

    @discord.ui.button(
        label="Start with defaults",
        style=discord.ButtonStyle.success,
    )
    async def start_defaults(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "❌  This command must be used inside a server.", ephemeral=True
            )
            return

        partial = _pop_pending(guild_id, interaction.user.id)
        if partial is None:
            await interaction.response.send_message(
                "❌  Setup session expired. Please run `/start_event` again.",
                ephemeral=True,
            )
            return

        # Fill in advanced defaults if not already set
        partial.setdefault("broadcast_channel_id", self._default_bc_ch)
        partial.setdefault("mmr_k_value", 32)
        partial.setdefault("mmr_match_threshold", 200)
        partial.setdefault("mmr_relax_seconds", 120)
        partial.setdefault("match_accept_timeout", 60)
        partial.setdefault("rematch_cooldown_seconds", 0)

        await _finalize_event(interaction, guild_id, partial)


# ── Shared finalizer ──────────────────────────────────────────────────────────

async def _finalize_event(
    interaction: discord.Interaction,
    guild_id: int,
    data: dict[str, Any],
) -> None:
    """Create the event from collected config data and confirm to the admin."""
    cfg = EventConfig(
        composition_type=data.get("composition_type", "fixed"),
        min_group_size=data.get("min_group_size", 1),
        max_group_size=data.get("max_group_size", 1),
        mmr_enabled=data.get("mmr_enabled", True),
        rematch_cooldown_seconds=data.get("rematch_cooldown_seconds", 0),
        mmr_k_value=data.get("mmr_k_value", 32),
        mmr_match_threshold=data.get("mmr_match_threshold", 200),
        mmr_relax_seconds=data.get("mmr_relax_seconds", 120),
        match_accept_timeout=data.get("match_accept_timeout", 60),
        matchmaking_channel_id=data.get("matchmaking_channel_id", 0),
        broadcast_channel_id=data.get("broadcast_channel_id", 0),
    )

    try:
        guild_store.create_event(guild_id, cfg)
    except ValueError as exc:
        await interaction.response.send_message(f"❌  {exc}", ephemeral=True)
        return

    comp_line = f"`{cfg.composition_type}`"
    if cfg.composition_type == "modular":
        comp_line += f"  (group size {cfg.min_group_size}–{cfg.max_group_size})"

    embed = discord.Embed(
        title="✅  Event Started",
        color=discord.Color.green(),
    )
    embed.add_field(name="Composition", value=comp_line, inline=True)
    embed.add_field(
        name="MMR",
        value="enabled" if cfg.mmr_enabled else "disabled",
        inline=True,
    )
    embed.add_field(
        name="Matchmaking channel",
        value=f"<#{cfg.matchmaking_channel_id}>" if cfg.matchmaking_channel_id else "not set",
        inline=False,
    )
    embed.add_field(
        name="Broadcast channel",
        value=f"<#{cfg.broadcast_channel_id}>" if cfg.broadcast_channel_id else "not set",
        inline=False,
    )
    if cfg.mmr_enabled:
        embed.add_field(
            name="MMR settings",
            value=(
                f"K-factor: **{cfg.mmr_k_value}**  |  "
                f"Threshold: **{cfg.mmr_match_threshold}**  |  "
                f"Relax: **{cfg.mmr_relax_seconds}s**"
            ),
            inline=False,
        )
    embed.add_field(
        name="Accept timeout",
        value=f"**{cfg.match_accept_timeout}s**",
        inline=True,
    )
    embed.add_field(
        name="Rematch cooldown",
        value=(
            f"**{cfg.rematch_cooldown_seconds}s**"
            if cfg.rematch_cooldown_seconds > 0
            else "immediate (1-rematch block)"
        ),
        inline=True,
    )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as exc:
        logger.error("Failed to send event-started confirmation: %s", exc)

    logger.info("Guild %d: event started by %s.", guild_id, interaction.user)


# ── Command registration ──────────────────────────────────────────────────────

def register(bot: commands.Bot, engine: MatchmakingEngine) -> None:
    """Attach all slash commands to ``bot.tree``."""

    # ── Guards ────────────────────────────────────────────────────────────────

    def leader_only(interaction: discord.Interaction) -> bool:
        return has_leader_role(interaction)

    def admin_only(interaction: discord.Interaction) -> bool:
        return (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.administrator
        )

    leader_check = app_commands.check(leader_only)
    admin_check  = app_commands.check(admin_only)

    def _guild_id(interaction: discord.Interaction) -> Optional[int]:
        return interaction.guild_id

    async def _no_guild(interaction: discord.Interaction) -> bool:
        await interaction.response.send_message(
            "❌  This bot only works inside a server.", ephemeral=True
        )
        return False

    async def _no_event(interaction: discord.Interaction) -> bool:
        await interaction.response.send_message(
            "❌  No active event in this server. An admin must run `/start_event` first.",
            ephemeral=True,
        )
        return False

    # ── /start_event ──────────────────────────────────────────────────────────

    @bot.tree.command(
        name="start_event",
        description="[Admin] Start a new matchmaking event for this server",
    )
    @admin_check
    async def cmd_start_event(interaction: discord.Interaction) -> None:
        """Interactive multi-step flow to configure and start a guild event."""
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        if guild_store.get_active_event_id(guild_id) is not None:
            await interaction.response.send_message(
                "❌  An event is already running in this server.\n"
                "Use `/end_event` to close it before starting a new one.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            _StartEventBasicModal(
                engine,
                settings.default_matchmaking_channel_id,
                settings.default_broadcast_channel_id,
            )
        )

    # ── /end_event ────────────────────────────────────────────────────────────

    @bot.tree.command(
        name="end_event",
        description="[Admin] End the active event and clear all state",
    )
    @admin_check
    async def cmd_end_event(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        if guild_store.get_active_event_id(guild_id) is None:
            await interaction.response.send_message(
                "❌  No active event in this server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Cancel all active matches
        for match in guild_store.active_matches(guild_id):
            await engine.cancel_match(
                guild_id, match,
                reason="event ended by admin"
            )

        # Reset all teams to IDLE and remove from queue
        for team in guild_store.all_teams(guild_id):
            if team.state != TeamState.IDLE:
                team.state = TeamState.IDLE
                team.current_match_id = None
                team.current_opponent = None
                team.has_accepted = False
                guild_store.save_team(guild_id, team)

        guild_store.end_event(guild_id)
        await interaction.followup.send(
            "✅  Event ended. All teams reset.", ephemeral=True
        )
        logger.warning("Guild %d: event ended by %s.", guild_id, interaction.user)

    # ── /event_status ─────────────────────────────────────────────────────────

    @bot.tree.command(
        name="event_status",
        description="Show the current event configuration",
    )
    async def cmd_event_status(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        cfg = guild_store.get_event_config(guild_id)
        if cfg is None:
            await interaction.response.send_message(
                "ℹ️  No active event in this server.", ephemeral=True
            )
            return

        comp_line = f"`{cfg.composition_type}`"
        if cfg.composition_type == "modular":
            comp_line += f"  (group size {cfg.min_group_size}–{cfg.max_group_size})"

        embed = discord.Embed(title="📋  Current Event Config", color=discord.Color.blurple())
        embed.add_field(name="Composition", value=comp_line, inline=True)
        embed.add_field(
            name="MMR",
            value="enabled" if cfg.mmr_enabled else "disabled",
            inline=True,
        )
        embed.add_field(
            name="Matchmaking channel",
            value=f"<#{cfg.matchmaking_channel_id}>" if cfg.matchmaking_channel_id else "not set",
            inline=False,
        )
        embed.add_field(
            name="Broadcast channel",
            value=f"<#{cfg.broadcast_channel_id}>" if cfg.broadcast_channel_id else "not set",
            inline=False,
        )
        embed.add_field(
            name="MMR settings",
            value=(
                f"Enabled: **{'yes' if cfg.mmr_enabled else 'no'}**  |  "
                f"K: **{cfg.mmr_k_value}**  |  "
                f"Threshold: **{cfg.mmr_match_threshold}**  |  "
                f"Relax: **{cfg.mmr_relax_seconds}s**"
            ),
            inline=False,
        )
        embed.add_field(name="Accept timeout", value=f"**{cfg.match_accept_timeout}s**", inline=True)
        embed.add_field(
            name="Rematch cooldown",
            value=(
                f"**{cfg.rematch_cooldown_seconds}s**"
                if cfg.rematch_cooldown_seconds > 0
                else "immediate (1-rematch block)"
            ),
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /set_config ───────────────────────────────────────────────────────────

    @bot.tree.command(
        name="set_config",
        description="[Admin] Update configuration for the active event",
    )
    @admin_check
    @app_commands.describe(
        mmr_enabled="Enable or disable MMR ratings (true/false)",
        mmr_match_threshold="Max MMR gap for an instant match (e.g. 200)",
        mmr_relax_seconds="Seconds before MMR threshold is lifted (e.g. 120)",
        mmr_k_value="ELO K-factor — controls rating volatility (e.g. 32)",
        rematch_cooldown_seconds="Seconds before a rematch is allowed (0 = 1-match block)",
        match_accept_timeout="Seconds leaders have to accept a match proposal (e.g. 60)",
        matchmaking_channel_id="Discord channel ID for queue pings and private threads",
        broadcast_channel_id="Discord channel ID for match announcements",
    )
    async def cmd_set_config(
        interaction: discord.Interaction,
        mmr_enabled: Optional[bool] = None,
        mmr_match_threshold: Optional[int] = None,
        mmr_relax_seconds: Optional[int] = None,
        mmr_k_value: Optional[int] = None,
        rematch_cooldown_seconds: Optional[int] = None,
        match_accept_timeout: Optional[int] = None,
        matchmaking_channel_id: Optional[str] = None,
        broadcast_channel_id: Optional[str] = None,
    ) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        if guild_store.get_active_event_id(guild_id) is None:
            await _no_event(interaction)
            return

        # Validate
        errors = []
        if mmr_match_threshold is not None and mmr_match_threshold < 0:
            errors.append("`mmr_match_threshold` must be ≥ 0")
        if mmr_relax_seconds is not None and mmr_relax_seconds < 0:
            errors.append("`mmr_relax_seconds` must be ≥ 0")
        if mmr_k_value is not None and mmr_k_value < 1:
            errors.append("`mmr_k_value` must be ≥ 1")
        if rematch_cooldown_seconds is not None and rematch_cooldown_seconds < 0:
            errors.append("`rematch_cooldown_seconds` must be ≥ 0")
        if match_accept_timeout is not None and match_accept_timeout < 5:
            errors.append("`match_accept_timeout` must be ≥ 5 seconds")

        mm_ch_id: Optional[int] = None
        bc_ch_id: Optional[int] = None
        if matchmaking_channel_id is not None:
            try:
                mm_ch_id = int(matchmaking_channel_id.strip())
            except ValueError:
                errors.append("`matchmaking_channel_id` must be a number")
        if broadcast_channel_id is not None:
            try:
                bc_ch_id = int(broadcast_channel_id.strip())
            except ValueError:
                errors.append("`broadcast_channel_id` must be a number")

        if errors:
            await interaction.response.send_message(
                "❌  " + "\n".join(errors), ephemeral=True
            )
            return

        updates: dict[str, Any] = {}
        if mmr_enabled is not None:
            updates["mmr_enabled"] = int(mmr_enabled)
        if mmr_match_threshold is not None:
            updates["mmr_match_threshold"] = mmr_match_threshold
        if mmr_relax_seconds is not None:
            updates["mmr_relax_seconds"] = mmr_relax_seconds
        if mmr_k_value is not None:
            updates["mmr_k_value"] = mmr_k_value
        if rematch_cooldown_seconds is not None:
            updates["rematch_cooldown_seconds"] = rematch_cooldown_seconds
        if match_accept_timeout is not None:
            updates["match_accept_timeout"] = match_accept_timeout
        if mm_ch_id is not None:
            updates["matchmaking_channel_id"] = mm_ch_id
        if bc_ch_id is not None:
            updates["broadcast_channel_id"] = bc_ch_id

        if not updates:
            await interaction.response.send_message(
                "ℹ️  No changes specified.", ephemeral=True
            )
            return

        guild_store.update_event_config(guild_id, **updates)
        changed = ", ".join(f"`{k}` → `{v}`" for k, v in updates.items())
        await interaction.response.send_message(
            f"✅  Config updated: {changed}", ephemeral=True
        )
        logger.warning(
            "Guild %d: config updated by %s: %s", guild_id, interaction.user, updates
        )

    # ── /register_team ────────────────────────────────────────────────────────

    @bot.tree.command(
        name="register_team",
        description="Register your team for tonight's event",
    )
    @leader_check
    @app_commands.describe(
        groupleader_character_name="Your in-game character name",
        group_size="Group size (only required for modular events)",
    )
    async def cmd_register_team(
        interaction: discord.Interaction,
        groupleader_character_name: str,
        group_size: Optional[int] = None,
    ) -> None:
        """Register a new team and create a private thread with a live panel."""
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        cfg = guild_store.get_event_config(guild_id)
        if cfg is None:
            await _no_event(interaction)
            return

        leader = interaction.user

        if guild_store.is_leader(guild_id, leader.id):
            existing = guild_store.get_team_by_leader(guild_id, leader.id)
            await interaction.response.send_message(
                f"❌  You are registered as **{existing.name if existing else 'your team'}**. "
                "Use `/unregister_team` first.",
                ephemeral=True,
            )
            return

        if guild_store.team_exists(guild_id, groupleader_character_name):
            await interaction.response.send_message(
                f"❌  A character named **{groupleader_character_name}** is already registered. "
                "If that's not you, contact an admin.",
                ephemeral=True,
            )
            return

        # Group size validation for modular events
        if cfg.composition_type == "modular":
            if group_size is None:
                await interaction.response.send_message(
                    f"❌  This event uses modular composition. "
                    f"Please provide `group_size` (between {cfg.min_group_size} and {cfg.max_group_size}).",
                    ephemeral=True,
                )
                return
            if not (cfg.min_group_size <= group_size <= cfg.max_group_size):
                await interaction.response.send_message(
                    f"❌  Group size must be between **{cfg.min_group_size}** and "
                    f"**{cfg.max_group_size}** for this event.",
                    ephemeral=True,
                )
                return
        else:
            group_size = 1  # fixed events don't use group_size

        team = Team(
            name=groupleader_character_name,
            leader_id=leader.id,
            member_ids=[leader.id],
            group_size=group_size,
        )
        guild_store.add_team(guild_id, team)

        await interaction.response.defer(ephemeral=True)

        if not isinstance(leader, discord.Member):
            await interaction.followup.send(
                "❌  This command must be used in a server.", ephemeral=True
            )
            return
        await engine.create_team_panel(guild_id, team, leader)

        ch_mention = (
            f"<#{cfg.matchmaking_channel_id}>"
            if cfg.matchmaking_channel_id
            else "your matchmaking channel"
        )
        size_info = f"  (group size: {group_size})" if cfg.composition_type == "modular" else ""
        await interaction.followup.send(
            f"✅  Team **{groupleader_character_name}**{size_info} registered!\n"
            f"A private thread has been created for you in {ch_mention} — "
            f"use the buttons there to ready up, accept matches, and report results.",
            ephemeral=True,
        )
        event_log.team_registered(guild_id, groupleader_character_name, leader.id)
        logger.info(
            "Guild %d: team registered: %s (leader=%d)", guild_id, groupleader_character_name, leader.id
        )

    # ── /unregister_team ──────────────────────────────────────────────────────

    @bot.tree.command(
        name="unregister_team",
        description="Remove your team from tonight's event",
    )
    @leader_check
    async def cmd_unregister_team(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        team = guild_store.get_team_by_leader(guild_id, interaction.user.id)
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
        await engine.delete_team_panel(guild_id, team)
        event_log.team_unregistered(guild_id, name, interaction.user.id)
        guild_store.remove_team(guild_id, name)

        await interaction.followup.send(
            f"✅  Team **{name}** has been removed and your panel thread deleted.",
            ephemeral=True,
        )

    # ── /change_group_size ───────────────────────────────────────────────────

    @bot.tree.command(
        name="change_group_size",
        description="Change your team's group size without re-registering (modular events only)",
    )
    @leader_check
    @app_commands.describe(group_size="New group size for your team")
    async def cmd_change_group_size(
        interaction: discord.Interaction,
        group_size: int,
    ) -> None:
        """Change a registered team's group size in-place.

        Allowed states: IDLE, READY.
        Blocked when MATCHED or IN_MATCH — the group size must stay fixed for
        the duration of any accepted or in-progress match to keep both sides
        consistent.

        If the team is currently READY (queued), it is dequeued before the
        change and re-enqueued afterwards so it rejoins the queue at the back
        with the new size.  try_match() is called to give the resized team an
        immediate pairing opportunity.
        """
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        cfg = guild_store.get_event_config(guild_id)
        if cfg is None:
            await _no_event(interaction)
            return

        if cfg.composition_type != "modular":
            await interaction.response.send_message(
                "❌  This event uses **fixed** composition — group size cannot be changed.",
                ephemeral=True,
            )
            return

        team = guild_store.get_team_by_leader(guild_id, interaction.user.id)
        if not team:
            await interaction.response.send_message(
                "❌  You don't have a registered team.", ephemeral=True
            )
            return

        if team.state in (TeamState.MATCHED, TeamState.IN_MATCH):
            await interaction.response.send_message(
                "❌  You cannot change group size while a match is active or pending acceptance.",
                ephemeral=True,
            )
            return

        if not (cfg.min_group_size <= group_size <= cfg.max_group_size):
            await interaction.response.send_message(
                f"❌  Group size must be between **{cfg.min_group_size}** and "
                f"**{cfg.max_group_size}** for this event.",
                ephemeral=True,
            )
            return

        if team.group_size == group_size:
            await interaction.response.send_message(
                f"ℹ️  Your team is already group size **{group_size}**. No change made.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        old_size = team.group_size
        was_ready = team.state == TeamState.READY

        # If in queue, remove from queue before changing size so the engine
        # never sees an inconsistent (queued + stale group_size) state.
        if was_ready:
            guild_store.dequeue(guild_id, team.name)

        team.group_size = group_size
        guild_store.save_team(guild_id, team)

        # Re-enqueue at the back with fresh timestamp so matchmaking picks
        # up the new size.  The queue position is reset — this is the same
        # trade-off as unregister+re-register, but without losing MMR/W-L.
        if was_ready:
            guild_store.enqueue(guild_id, team.name)
            await engine.try_match(guild_id)

        await engine.update_team_panel(guild_id, team)

        size_note = (
            "  You have been re-queued with the new group size."
            if was_ready
            else ""
        )
        await interaction.followup.send(
            f"✅  **{team.name}** group size changed from **{old_size}** → **{group_size}**."
            f"{size_note}",
            ephemeral=True,
        )
        event_log._write(
            "group_size_changed", guild_id,
            team=team.name,
            leader_id=interaction.user.id,
            old_size=old_size,
            new_size=group_size,
            was_queued=was_ready,
        )
        logger.info(
            "Guild %d: team '%s' group size changed %d → %d (was_queued=%s).",
            guild_id, team.name, old_size, group_size, was_ready,
        )

    # ── /queue_status ─────────────────────────────────────────────────────────

    @bot.tree.command(
        name="queue_status",
        description="Show teams currently waiting for a match",
    )
    @leader_check
    async def cmd_queue_status(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        queue = guild_store.get_queue(guild_id)
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
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        active = guild_store.active_matches(guild_id)
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
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        all_teams = guild_store.all_teams(guild_id)
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
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        team = guild_store.get_team(guild_id, groupleader_character_name)
        if not team:
            await interaction.response.send_message(
                f"❌  No team named **{groupleader_character_name}**.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        old_state = team.state

        if team.current_match_id:
            match = guild_store.get_match(guild_id, team.current_match_id)
            if match:
                await engine.cancel_match(
                    guild_id, match,
                    reason=f"admin force-reset of **{groupleader_character_name}**",
                )
                await interaction.followup.send(
                    f"✅  Match cancelled and both teams reset to IDLE.\n"
                    f"*(triggered by admin reset of **{groupleader_character_name}**, "
                    f"previous state: `{old_state.value}`)*",
                    ephemeral=True,
                )
                logger.warning(
                    "Guild %d: ADMIN %s reset team '%s' (state=%s), match %s cancelled.",
                    guild_id, interaction.user, groupleader_character_name,
                    old_state.value, match.id,
                )
                return

        guild_store.dequeue(guild_id, groupleader_character_name)
        engine._cancel_mmr_relax(guild_id, groupleader_character_name)
        team.state = TeamState.IDLE
        team.current_match_id = None
        team.current_opponent = None
        team.has_accepted = False
        guild_store.save_team(guild_id, team)
        await engine.update_team_panel(guild_id, team)

        await interaction.followup.send(
            f"✅  **{groupleader_character_name}** reset to IDLE. "
            f"*(previous state: `{old_state.value}`)*",
            ephemeral=True,
        )
        logger.warning(
            "Guild %d: ADMIN %s reset team '%s' (state=%s).",
            guild_id, interaction.user, groupleader_character_name, old_state.value,
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
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        match = guild_store.get_match(guild_id, match_id.upper())
        if not match:
            await interaction.response.send_message(
                f"❌  No match with ID `{match_id}`.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await engine.cancel_match(
            guild_id, match,
            reason=f"cancelled by admin {interaction.user.display_name}",
        )
        await interaction.followup.send(
            f"✅  Match `{match_id}` cancelled. Both teams reset to IDLE.",
            ephemeral=True,
        )
        logger.warning(
            "Guild %d: ADMIN %s force-cancelled match %s (%s vs %s).",
            guild_id, interaction.user, match_id, match.team1_name, match.team2_name,
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
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        team = guild_store.get_team(guild_id, groupleader_character_name)
        if not team:
            await interaction.response.send_message(
                f"❌  No team named **{groupleader_character_name}**.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        if team.current_match_id:
            match = guild_store.get_match(guild_id, team.current_match_id)
            if match:
                await engine.cancel_match(
                    guild_id, match,
                    reason=f"team **{groupleader_character_name}** removed by admin",
                )

        engine._cancel_mmr_relax(guild_id, groupleader_character_name)
        await engine.delete_team_panel(guild_id, team)
        guild_store.remove_team(guild_id, groupleader_character_name)

        await interaction.followup.send(
            f"✅  Team **{groupleader_character_name}** removed and their panel thread deleted.",
            ephemeral=True,
        )
        logger.warning(
            "Guild %d: ADMIN %s removed team '%s'.",
            guild_id, interaction.user, groupleader_character_name,
        )

    # ── /admin_list_teams ─────────────────────────────────────────────────────

    @bot.tree.command(
        name="admin_list_teams",
        description="[Admin] Show all registered teams and their current state",
    )
    @admin_check
    async def cmd_admin_list_teams(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        all_teams = guild_store.all_teams(guild_id)
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
        cfg = guild_store.get_event_config(guild_id)
        for t in sorted(all_teams, key=lambda x: x.mmr, reverse=True):
            icon = state_icon[t.state]
            total = t.wins + t.losses
            wr_str = f"{t.wins / total * 100:.0f}% WR" if total > 0 else "—"
            match_info = f"  |  match `{t.current_match_id}`" if t.current_match_id else ""
            last_opp   = f"  |  last opp: {t.last_opponent}" if t.last_opponent else ""
            size_info  = (
                f"  |  size: {t.group_size}"
                if cfg and cfg.composition_type == "modular"
                else ""
            )
            embed.add_field(
                name=f"{icon}  {t.name}  (MMR {t.mmr})",
                value=(
                    f"Leader: <@{t.leader_id}>  |  `{t.state.value}`"
                    f"  |  {t.wins}W – {t.losses}L  |  {wr_str}"
                    f"{match_info}{last_opp}{size_info}"
                ),
                inline=False,
            )
        queue = guild_store.get_queue(guild_id)
        embed.set_footer(
            text=f"Queue: {queue}  |  Active matches: {len(guild_store.active_matches(guild_id))}"
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
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        t1 = guild_store.get_team(guild_id, team1)
        t2 = guild_store.get_team(guild_id, team2)
        missing = [n for n, t in [(team1, t1), (team2, t2)] if t is None]
        if missing:
            await interaction.response.send_message(
                f"❌  Team(s) not found: {', '.join(f'**{n}**' for n in missing)}",
                ephemeral=True,
            )
            return

        guild_store.clear_last_opponents(guild_id, team1, team2)
        await interaction.response.send_message(
            f"✅  Rematch block cleared between **{team1}** and **{team2}**.",
            ephemeral=True,
        )
        logger.warning(
            "Guild %d: ADMIN %s cleared rematch block between '%s' and '%s'.",
            guild_id, interaction.user, team1, team2,
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
        guild_id = interaction.guild_id
        if guild_id is None:
            await _no_guild(interaction)
            return

        if mmr <= 0:
            await interaction.response.send_message(
                "❌  MMR must be a positive integer.", ephemeral=True
            )
            return
        team = guild_store.get_team(guild_id, groupleader_character_name)
        if not team:
            await interaction.response.send_message(
                f"❌  No team named **{groupleader_character_name}**.", ephemeral=True
            )
            return

        old_mmr = team.mmr
        team.mmr = mmr
        guild_store.save_team(guild_id, team)
        await engine.update_team_panel(guild_id, team)

        await interaction.response.send_message(
            f"✅  **{groupleader_character_name}** MMR set to **{mmr}** (was {old_mmr}).",
            ephemeral=True,
        )
        logger.warning(
            "Guild %d: ADMIN %s set MMR for '%s': %d → %d.",
            guild_id, interaction.user, groupleader_character_name, old_mmr, mmr,
        )

    # ── Global error handler ──────────────────────────────────────────────────

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                f"❌  You don't have permission to use this command.",
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
            await interaction.followup.send(
                "❌  This command must be used in a text channel.", ephemeral=True
            )
            return
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.followup.send("❌  Must be used inside a server.", ephemeral=True)
            return
        from daoc_bot.simulation import SimulationSuite
        suite   = SimulationSuite(channel=interaction.channel, engine=engine, guild_id=guild_id)
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
