"""Entry point for the DAoC 3v3 Matchmaking Bot.

Run with::

    python -m daoc_bot

Or directly::

    python daoc_bot/__main__.py

The module wires together the bot instance, logging, command registration,
and the ``on_ready`` event handler, then starts the Discord gateway connection.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from daoc_bot import commands as cmd_module
from daoc_bot.config import settings
from daoc_bot.engine import MatchmakingEngine

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
    """Sync slash commands and verify the matchmaking channel on startup."""
    assert bot.user is not None  # always true after on_ready
    logger.info("Logged in as %s (ID: %d)", bot.user, bot.user.id)

    try:
        synced = await bot.tree.sync()
        logger.info("Synced %d slash command(s) to Discord.", len(synced))
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to sync commands: %s", exc)

    matchmaking_channel = bot.get_channel(settings.matchmaking_channel_id)
    if matchmaking_channel:
        logger.info("Matchmaking channel: #%s (ID: %d)", matchmaking_channel, settings.matchmaking_channel_id)
    else:
        logger.warning(
            "Matchmaking channel ID %d not found — check MATCHMAKING_CHANNEL_ID.",
            settings.matchmaking_channel_id,
        )
    broadcast_channel = bot.get_channel(settings.broadcast_channel_id)
    if broadcast_channel:
        logger.info("Broadcast channel: #%s (ID: %d)", broadcast_channel, settings.broadcast_channel_id)
    else:
        logger.warning(
            "Broadcast ID %d not found — check BROADCAST_CHANNEL_ID.",
            settings.matchmaking_channel_id,
        )
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
    
    # ── Debug: thread + mention capabilities ────────────────────────────────
    logger.info("---- Thread & mention debug ----")

    for guild in bot.guilds:
        logger.info("Guild: %s (ID: %d)", guild.name, guild.id)

        bot_member = guild.me
        if bot_member is None:
            continue

        for channel in guild.text_channels:
            perms = channel.permissions_for(bot_member)

            can_use = perms.view_channel and perms.send_messages

            if not can_use:
                continue  # skip irrelevant channels

            logger.info(
                " #%s | create_private_threads=%s create_public_threads=%s send_in_threads=%s mention_everyone=%s",
                channel.name,
                perms.create_private_threads,
                perms.create_public_threads,
                perms.send_messages_in_threads,
                perms.mention_everyone,
            )
    
    # ── Debug: test thread creation in matchmaking channel ──────────────────
    if isinstance(matchmaking_channel, discord.TextChannel):
        try:
            thread = await matchmaking_channel.create_thread(
                name="debug-thread",
                type=discord.ChannelType.private_thread,
            )

            await thread.send("✅ Thread creation works")

            logger.info("Successfully created private thread in #%s", matchmaking_channel.name)

            await thread.delete()

        except Exception as e:
            logger.error("Thread creation FAILED in #%s: %s", matchmaking_channel.name, e)

@bot.event
async def on_error(event: str, *args: Any, **kwargs: Any) -> None:
    """Log unhandled exceptions from event handlers."""
    logger.exception("Unhandled exception in event '%s'.", event)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(settings.discord_token, log_handler=None)  # logging already configured above
