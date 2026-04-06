"""Application configuration loaded from environment variables.

Settings are loaded lazily on first access so that importing this module
during a build step (e.g. Railway's nixpacks scan) does not raise errors
before runtime environment variables are injected.

Channel IDs (``MATCHMAKING_CHANNEL_ID`` and ``BROADCAST_CHANNEL_ID``) are
**optional** env vars.  They serve as default values pre-populated when an
admin runs ``/start_event``, reducing setup friction for single-guild
deployments.  Multi-guild deployments supply channel IDs per event via that
command instead.

Typical usage::

    from daoc_bot.config import settings
    print(settings.discord_token)
"""

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Immutable application settings.

    Attributes:
        discord_token:                   Bot token from the Discord developer portal.
        database_url:                    libpq connection string for PostgreSQL.
                                         Railway injects this automatically as
                                         ``DATABASE_URL`` when a Postgres service is
                                         attached to the project.
        default_matchmaking_channel_id:  Optional default channel ID for matchmaking
                                         threads; pre-fills the ``/start_event`` form.
        default_broadcast_channel_id:    Optional default channel ID for match
                                         announcements; pre-fills the ``/start_event`` form.
        team_leader_role_name:           Exact name of the Discord role that grants
                                         access to bot commands and buttons.
        log_level:                       Python logging level string (e.g. ``"INFO"``).
    """

    discord_token: str
    database_url: str
    default_matchmaking_channel_id: int
    default_broadcast_channel_id: int
    team_leader_role_name: str
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
        database_url=_require("DATABASE_URL"),
        default_matchmaking_channel_id=int(os.getenv("MATCHMAKING_CHANNEL_ID", "0")),
        default_broadcast_channel_id=int(os.getenv("BROADCAST_CHANNEL_ID", "0")),
        team_leader_role_name=os.getenv("TEAM_LEADER_ROLE_NAME", "Team Leader"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )


class _LazySettings:
    """Proxy that loads :class:`Settings` on first attribute access.

    This prevents ``_load()`` from running at import time, which would cause
    Railway's build phase to fail with a missing-env-var error before the
    runtime environment is available.
    """

    _instance: Optional[Settings] = None

    def _get(self) -> Settings:
        if self._instance is None:
            self._instance = _load()
        return self._instance

    def __getattr__(self, name: str) -> object:
        return getattr(self._get(), name)


#: Module-level singleton — import this everywhere instead of calling ``_load``.
settings: Settings = _LazySettings()  # type: ignore[assignment]
