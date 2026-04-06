"""Standalone simulation runner — no database or Discord gateway required.

Patches ``daoc_bot.guild_store.guild_store`` with a fully in-memory store,
suppresses the event log, wires a fake Discord channel, then runs the real
:class:`~daoc_bot.simulation.SimulationSuite` end-to-end.

This script does NOT touch or modify ``simulation.py`` in any way.
The production ``/run_tests`` slash command is completely unaffected.

Usage::

    python scripts/run_simulation.py            # all scenarios, concise output
    python scripts/run_simulation.py --verbose  # include per-check detail
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

# ── Env stubs — must come before any daoc_bot import ─────────────────────────
os.environ.setdefault("DISCORD_TOKEN", "simulation")
os.environ.setdefault("MATCHMAKING_CHANNEL_ID", "111111111111111111")
os.environ.setdefault("BROADCAST_CHANNEL_ID",  "222222222222222222")

# Ensure we load daoc_bot from the repo root (not a stale installed copy).
# This script lives in scripts/, so one level up is the repo root.
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import daoc_bot.event_log as _ev_module          # noqa: E402
import daoc_bot.guild_store as _gs_module         # noqa: E402
import daoc_bot.simulation as _sim_module         # noqa: E402
from daoc_bot.engine import MatchmakingEngine     # noqa: E402
from daoc_bot.guild_store import EventConfig      # noqa: E402
from daoc_bot.models import Match, Team, TeamState  # noqa: E402
from daoc_bot.simulation import SimulationSuite   # noqa: E402

# ── ANSI colours ──────────────────────────────────────────────────────────────

BOLD  = "\033[1m"
GREEN = "\033[32m"
RED   = "\033[31m"
CYAN  = "\033[36m"
GREY  = "\033[90m"
RESET = "\033[0m"

# Force UTF-8 output on Windows so box-drawing chars don't crash
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s  %(name)s  %(message)s",
)

# ── Fake Discord channel ──────────────────────────────────────────────────────

class _FakeMessage:
    def __init__(self, id: int) -> None:
        self.id = id
        self._embed: Any = None

    async def delete(self) -> None:
        pass

    async def edit(self, **kwargs: Any) -> None:
        self._embed = kwargs.get("embed")


class _FakeChannel:
    def __init__(self) -> None:
        self._next_id = 1
        self.id = 111111111111111111

    def _new_msg(self) -> _FakeMessage:
        msg = _FakeMessage(self._next_id)
        self._next_id += 1
        return msg

    async def send(self, *_args: Any, **_kwargs: Any) -> _FakeMessage:
        return self._new_msg()

    async def fetch_message(self, mid: int) -> _FakeMessage:
        return _FakeMessage(mid)

    async def create_thread(self, **_kwargs: Any) -> Any:
        thread = MagicMock()
        thread.id = self._next_id
        self._next_id += 1
        thread.send = AsyncMock(return_value=self._new_msg())
        thread.add_user = AsyncMock()
        thread.delete = AsyncMock()
        thread.fetch_message = AsyncMock(return_value=self._new_msg())
        return thread


# ── In-memory GuildStore ──────────────────────────────────────────────────────

class _MemGuildStore:
    """Full in-memory replacement for GuildStore used by the standalone runner.

    Implements every method called by SimulationSuite and MatchmakingEngine,
    including event management and rematch-cooldown helpers.
    """

    _DEFAULT_CFG = EventConfig(
        composition_type="fixed",
        min_group_size=1,
        max_group_size=1,
        mmr_enabled=True,
        rematch_cooldown_seconds=0,
        mmr_k_value=32,
        mmr_match_threshold=200,
        mmr_relax_seconds=120,
        match_accept_timeout=60,
        matchmaking_channel_id=111111111111111111,
        broadcast_channel_id=222222222222222222,
    )

    def __init__(self) -> None:
        self._events: dict[int, EventConfig] = {}          # guild_id → config
        self._teams:  dict[int, dict[str, Team]] = {}      # guild_id → {name → Team}
        self._matches: dict[int, dict[str, Match]] = {}    # guild_id → {id → Match}
        self._queues:  dict[int, list[str]] = {}
        self._queue_timestamps: dict[int, dict[str, datetime]] = {}
        self._last_match_times: dict[int, dict[str, datetime]] = {}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _queue(self, guild_id: int) -> list[str]:
        return self._queues.setdefault(guild_id, [])

    def _queue_ts(self, guild_id: int) -> dict[str, datetime]:
        return self._queue_timestamps.setdefault(guild_id, {})

    def _last_match_ts(self, guild_id: int) -> dict[str, datetime]:
        return self._last_match_times.setdefault(guild_id, {})

    def _team_store(self, guild_id: int) -> dict[str, Team]:
        return self._teams.setdefault(guild_id, {})

    def _match_store(self, guild_id: int) -> dict[str, Match]:
        return self._matches.setdefault(guild_id, {})

    # ── Event management ──────────────────────────────────────────────────────

    def get_active_event_id(self, guild_id: int) -> Optional[int]:
        return guild_id if guild_id in self._events else None

    def create_event(self, guild_id: int, config: EventConfig) -> int:
        if guild_id in self._events:
            raise ValueError(f"Guild {guild_id} already has an active event.")
        self._events[guild_id] = config
        return guild_id  # use guild_id as the fake event id

    def get_event_config(self, guild_id: int) -> Optional[EventConfig]:
        return self._events.get(guild_id)

    def update_event_config(self, guild_id: int, **kwargs: object) -> bool:
        cfg = self._events.get(guild_id)
        if cfg is None:
            return False
        allowed = {
            "mmr_enabled", "rematch_cooldown_seconds", "mmr_k_value",
            "mmr_match_threshold", "mmr_relax_seconds", "match_accept_timeout",
            "matchmaking_channel_id", "broadcast_channel_id",
        }
        for k, v in kwargs.items():
            if k in allowed:
                object.__setattr__(cfg, k, v)
        return True

    def end_event(self, guild_id: int) -> None:
        self._events.pop(guild_id, None)
        self._queues.pop(guild_id, None)
        self._queue_timestamps.pop(guild_id, None)

    # ── Teams ─────────────────────────────────────────────────────────────────

    def add_team(self, guild_id: int, team: Team) -> None:
        self._team_store(guild_id)[team.name] = team

    def save_team(self, guild_id: int, team: Team) -> None:
        self._team_store(guild_id)[team.name] = team

    def remove_team(self, guild_id: int, team_name: str) -> None:
        self._team_store(guild_id).pop(team_name, None)
        q = self._queue(guild_id)
        if team_name in q:
            q.remove(team_name)
        self._queue_ts(guild_id).pop(team_name, None)

    def get_team(self, guild_id: int, name: str) -> Optional[Team]:
        return self._team_store(guild_id).get(name)

    def get_team_by_leader(self, guild_id: int, user_id: int) -> Optional[Team]:
        return next(
            (t for t in self._team_store(guild_id).values() if t.leader_id == user_id),
            None,
        )

    def all_teams(self, guild_id: int) -> list[Team]:
        return list(self._team_store(guild_id).values())

    def team_exists(self, guild_id: int, name: str) -> bool:
        return name in self._team_store(guild_id)

    def is_leader(self, guild_id: int, user_id: int) -> bool:
        return any(
            t.leader_id == user_id for t in self._team_store(guild_id).values()
        )

    def record_match_end(self, guild_id: int, team_name: str) -> None:
        self._last_match_ts(guild_id)[team_name] = datetime.now(timezone.utc)

    def seconds_since_last_match(self, guild_id: int, team_name: str) -> float:
        ts = self._last_match_ts(guild_id).get(team_name)
        if ts is None:
            return float("inf")
        return (datetime.now(timezone.utc) - ts).total_seconds()

    def clear_last_opponents(self, guild_id: int, name1: str, name2: str) -> None:
        for name in (name1, name2):
            t = self._team_store(guild_id).get(name)
            if t:
                t.last_opponent = None

    # ── Matches ───────────────────────────────────────────────────────────────

    def add_match(self, guild_id: int, match: Match) -> None:
        self._match_store(guild_id)[match.id] = match

    def save_match(self, guild_id: int, match: Match) -> None:
        self._match_store(guild_id)[match.id] = match

    def remove_match(self, guild_id: int, match_id: str) -> None:
        self._match_store(guild_id).pop(match_id, None)

    def get_match(self, guild_id: int, match_id: str) -> Optional[Match]:
        return self._match_store(guild_id).get(match_id)

    def active_matches(self, guild_id: int) -> list[Match]:
        return [m for m in self._match_store(guild_id).values() if m.active]

    # ── Queue ─────────────────────────────────────────────────────────────────

    def enqueue(self, guild_id: int, team_name: str) -> None:
        q = self._queue(guild_id)
        if team_name not in q:
            q.append(team_name)
            self._queue_ts(guild_id)[team_name] = datetime.now(timezone.utc)

    def dequeue(self, guild_id: int, team_name: str) -> None:
        q = self._queue(guild_id)
        if team_name in q:
            q.remove(team_name)
            self._queue_ts(guild_id).pop(team_name, None)

    def get_queue(self, guild_id: int) -> list[str]:
        return list(self._queue(guild_id))

    def queue_size(self, guild_id: int) -> int:
        return len(self._queue(guild_id))

    def queue_wait_seconds(self, guild_id: int, team_name: str) -> float:
        ts = self._queue_ts(guild_id).get(team_name)
        if ts is None:
            return 0.0
        return (datetime.now(timezone.utc) - ts).total_seconds()

    def queue_by_group_size(self, guild_id: int) -> dict[int, list[str]]:
        grouped: dict[int, list[str]] = {}
        for name in self._queue(guild_id):
            team = self._team_store(guild_id).get(name)
            if team is not None:
                grouped.setdefault(team.group_size, []).append(name)
        return grouped

    def recover_guild(self, guild_id: int) -> None:
        pass  # nothing to recover for an in-memory store


# ── Runner ────────────────────────────────────────────────────────────────────

async def main(verbose: bool) -> None:
    mem_store = _MemGuildStore()
    channel   = _FakeChannel()

    mock_bot = MagicMock()
    mock_bot.get_channel = MagicMock(return_value=channel)
    mock_bot.fetch_channel = AsyncMock(return_value=channel)

    engine = MatchmakingEngine(mock_bot)

    # Patch the module-level singletons so every import sees the fake store
    _gs_module.guild_store = mem_store          # type: ignore[assignment]
    _sim_module.guild_store = mem_store         # type: ignore[assignment]

    # Redirect engine's own reference
    import daoc_bot.engine as _eng_module
    _eng_module.guild_store = mem_store         # type: ignore[assignment]

    # Suppress event log DB writes
    _ev_module._write = lambda *_a, **_kw: None  # type: ignore[assignment]

    # Silence expected engine noise (channel lookups always succeed via mock_bot)
    logging.getLogger("daoc_bot.engine").setLevel(logging.CRITICAL)
    logging.getLogger("daoc_bot.guild_store").setLevel(logging.CRITICAL)
    logging.getLogger("daoc_bot.simulation").setLevel(logging.WARNING)

    suite = SimulationSuite(channel=channel, engine=engine)  # type: ignore[arg-type]

    # _set_modular_config / _set_fixed_config call get_db() directly.
    # Override them on the instance to use our in-memory store instead.
    def _set_modular_config(min_size: int = 1, max_size: int = 5) -> None:
        cfg = mem_store.get_event_config(suite.guild_id)
        if cfg:
            object.__setattr__(cfg, "composition_type", "modular")
            object.__setattr__(cfg, "min_group_size", min_size)
            object.__setattr__(cfg, "max_group_size", max_size)

    def _set_fixed_config() -> None:
        cfg = mem_store.get_event_config(suite.guild_id)
        if cfg:
            object.__setattr__(cfg, "composition_type", "fixed")
            object.__setattr__(cfg, "min_group_size", 1)
            object.__setattr__(cfg, "max_group_size", 1)

    suite._set_modular_config = _set_modular_config  # type: ignore[method-assign]
    suite._set_fixed_config   = _set_fixed_config    # type: ignore[method-assign]

    results = await suite.run()

    # ── Print results ─────────────────────────────────────────────────────────
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    print()
    print(f"{BOLD}{'═' * 62}{RESET}")
    print(f"{BOLD}  SIMULATION RESULTS — {passed}/{len(results)} passed{RESET}")
    print(f"{BOLD}{'═' * 62}{RESET}")

    for r in results:
        icon  = f"{GREEN}✅{RESET}" if r.passed else f"{RED}❌{RESET}"
        print(f"  {icon}  {r.name}")
        if verbose or not r.passed:
            for line in r.checks:
                colour = GREEN if line.startswith("✅") else RED
                print(f"       {colour}{line}{RESET}")
        if r.error:
            print(f"       {RED}⚠  {r.error}{RESET}")

    print(f"{BOLD}{'═' * 62}{RESET}")
    if failed:
        print(f"{RED}{BOLD}  {failed} scenario(s) FAILED{RESET}")
        sys.exit(1)
    else:
        print(f"{GREEN}{BOLD}  All scenarios passed ✓{RESET}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the SimulationSuite offline (no DB or Discord required)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print every check result, not just failures",
    )
    args = parser.parse_args()
    asyncio.run(main(args.verbose))
