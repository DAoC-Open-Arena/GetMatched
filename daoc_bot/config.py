"""Application configuration loaded from environment variables.

All settings are read once at import time. Missing required values raise a
``RuntimeError`` early so the process fails fast before connecting to Discord.

Typical usage::

    from daoc_bot.config import settings
    print(settings.matchmaking_channel_id)
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Immutable application settings.

    Attributes:
        discord_token: Bot token from the Discord developer portal.
        matchmaking_channel_id: ID of the channel where match proposals and
            queue pings are posted.
        broadcast_channel_id: ID of the channel where match-started
            announcements are broadcast to everyone.
        team_leader_role_name: Exact name of the Discord role that grants
            access to bot commands and buttons (case-sensitive).
        match_accept_timeout: Seconds both team leaders have to accept a
            proposed match before it is automatically cancelled.
        log_level: Python logging level string (e.g. ``"INFO"``, ``"DEBUG"``).
    """

    discord_token: str
    matchmaking_channel_id: int
    broadcast_channel_id: int
    team_leader_role_name: str
    match_accept_timeout: int
    log_level: str


def _require(key: str) -> str:
    """Return the value of an environment variable or raise if absent."""
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            "Copy .env.example to .env and fill in the values."
        )
    return value


def _load() -> Settings:
    """Build a :class:`Settings` instance from the current environment."""
    return Settings(
        discord_token=_require("DISCORD_TOKEN"),
        matchmaking_channel_id=int(_require("MATCHMAKING_CHANNEL_ID")),
        broadcast_channel_id=int(_require("BROADCAST_CHANNEL_ID")),
        team_leader_role_name=os.getenv("TEAM_LEADER_ROLE_NAME", "Team Leader"),
        match_accept_timeout=int(os.getenv("MATCH_ACCEPT_TIMEOUT", "60")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )


#: Module-level singleton — import this everywhere instead of calling ``_load``.
settings: Settings = _load()
