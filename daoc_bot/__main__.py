"""Entry point for the DAoC 3v3 Matchmaking Bot.

Run with::

    python -m daoc_bot

Or directly::

    python daoc_bot/__main__.py
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

# Kill switch — set RUN_BOT=false in Railway's environment variables tab to
# take the bot offline without stopping the Railway service entirely.
if os.getenv("RUN_BOT", "true").strip().lower() != "true":
    print("RUN_BOT is not 'true' — bot disabled. Exiting cleanly.")
    sys.exit(0)

import discord
from discord import app_commands
from discord.ext import commands

from daoc_bot import commands as cmd_module
from daoc_bot.config import settings
from daoc_bot.db import init_db
from daoc_bot.engine import MatchmakingEngine
from daoc_bot.guild_store import guild_store

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s  [%(levelname)-8s]  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("daoc_bot")

# Quieten noisy third-party loggers
logging.getLogger("discord.gateway").setLevel(logging.INFO)
logging.getLogger("discord.client").setLevel(logging.INFO)
logging.getLogger("discord.http").setLevel(logging.INFO)

# ── Bot instance ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
engine = MatchmakingEngine(bot)

# Register all slash commands onto bot.tree
cmd_module.register(bot, engine)

# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    """Initialise DB, sync slash commands, and recover per-guild state."""
    assert bot.user is not None  # always true after on_ready
    logger.info("Logged in as %s (ID: %d)", bot.user, bot.user.id)
    logger.info("TEAM_LEADER_ROLE_NAME = %r", settings.team_leader_role_name)

    # Initialise PostgreSQL (no-op if tables already exist; safe to call every restart)
    init_db(settings.database_url)

    # Recover in-memory queue state for every guild this bot is in
    for guild in bot.guilds:
        guild_store.recover_guild(guild.id)
        logger.info("Guild recovered: %s (ID: %d)", guild.name, guild.id)

    try:
        synced = await bot.tree.sync()
        logger.info("Synced %d slash command(s) to Discord.", len(synced))
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to sync commands: %s", exc)

    # ── Debug: channel visibility & permissions ──────────────────────────────
    logger.info("---- Channel access debug ----")

    for guild in bot.guilds:
        logger.info("Guild: %s (ID: %d)", guild.name, guild.id)

        bot_member = guild.me
        if bot_member is None:
            logger.warning("Could not resolve bot member in guild %s", guild.name)
            continue

        for channel in guild.text_channels:
            perms = channel.permissions_for(bot_member)
            logger.info(
                " #%s | view=%s read=%s send=%s",
                channel.name,
                perms.view_channel,
                perms.read_messages,
                perms.send_messages,
            )

    logger.info("---- End channel access debug ----")


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    """Recover state when the bot joins a new guild (or re-joins after downtime)."""
    guild_store.recover_guild(guild.id)
    logger.info("Joined guild: %s (ID: %d) — state recovered.", guild.name, guild.id)


@bot.event
async def on_error(event: str, *args: Any, **kwargs: Any) -> None:
    """Log unhandled exceptions from event handlers."""
    logger.exception("Unhandled exception in event '%s'.", event)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(settings.discord_token, log_handler=None)  # logging already configured above
