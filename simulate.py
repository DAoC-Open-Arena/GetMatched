"""Standalone simulation script for the DAoC matchmaking bot.

Runs entirely in-process — no Discord gateway connection required.
Discord channels are replaced by a ``FakeChannel`` that records every
``send`` / ``edit`` / ``delete`` call so you can inspect exactly what
the bot would have posted.

The real :class:`~daoc_bot.engine.MatchmakingEngine` is used with a fresh
:class:`~daoc_bot.state.BotState` per scenario so tests are fully isolated.

Usage::

    python simulate.py                   # all scenarios
    python simulate.py --scenario basic  # single scenario
    python simulate.py --verbose         # dump channel message log after each run

Available scenarios
-------------------
basic       register → ready → accept → end → ready again
rematch     anti-instant-rematch guard + auto-lift when only 2 teams
parallel    4 teams, 2 simultaneous matches
decline     one leader declines, teams reset to IDLE
timeout     acceptance window expires, match cancelled
all         run all of the above in sequence
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# ── Env stub — config.py reads vars at import time ───────────────────────────
os.environ.setdefault("DISCORD_TOKEN", "simulation")
os.environ.setdefault("MATCHMAKING_CHANNEL_ID", "111111111111111111")
os.environ.setdefault("BROADCAST_CHANNEL_ID", "222222222222222222")

import daoc_bot.engine as _eng_module  # noqa: E402 — must come after env setup
from daoc_bot.engine import MatchmakingEngine  # noqa: E402
from daoc_bot.models import Team, TeamState  # noqa: E402
from daoc_bot.state import BotState  # noqa: E402

# ── ANSI colours ─────────────────────────────────────────────────────────────

BOLD   = "\033[1m"
GREEN  = "\033[32m"
CYAN   = "\033[36m"
YELLOW = "\033[33m"
RED    = "\033[31m"
RESET  = "\033[0m"

_verbose = False


def log(msg: str, color: str = "") -> None:
    print(f"{color}{msg}{RESET}" if color else msg)


def section(title: str) -> None:
    print(f"\n{BOLD}{'─' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{'─' * 60}{RESET}")


def check(condition: bool, label: str) -> None:
    icon = f"{GREEN}✓{RESET}" if condition else f"{RED}✗{RESET}"
    print(f"  {icon}  {label}")
    if not condition:
        sys.exit(f"\n{RED}FAILED: {label}{RESET}")


# ── Fake Discord objects ──────────────────────────────────────────────────────

@dataclass
class FakeMessage:
    """Minimal stand-in for :class:`discord.Message`."""

    id: int
    content: str = ""
    embed_title: str = ""
    deleted: bool = False
    edits: list[str] = field(default_factory=list)

    async def delete(self) -> None:
        self.deleted = True

    async def edit(self, **kwargs: Any) -> None:
        embed = kwargs.get("embed")
        self.edits.append(embed.title if embed else "")


class FakeChannel:
    """Records every message the bot would send, edit, or delete."""

    def __init__(self) -> None:
        self._next_id = 1
        self.messages: list[FakeMessage] = []

    def _new_id(self) -> int:
        mid = self._next_id
        self._next_id += 1
        return mid

    async def send(self, content: str = "", embed: Any = None, **_kwargs: Any) -> FakeMessage:
        title = embed.title if embed else ""
        msg = FakeMessage(id=self._new_id(), content=content, embed_title=title)
        self.messages.append(msg)
        return msg

    async def fetch_message(self, message_id: int) -> FakeMessage:
        for m in self.messages:
            if m.id == message_id:
                return m
        # Return a dummy so _delete_message doesn't raise
        return FakeMessage(id=message_id)

    def visible(self) -> list[FakeMessage]:
        """Messages not yet deleted."""
        return [m for m in self.messages if not m.deleted]


def dump_channel(channel: FakeChannel) -> None:
    if not _verbose:
        return
    print(f"\n  {YELLOW}── Channel messages ──{RESET}")
    for m in channel.messages:
        status = f"{RED}[deleted]{RESET}" if m.deleted else f"{GREEN}[visible]{RESET}"
        print(
            f"    {status}  id={m.id}"
            f"  embed={m.embed_title!r}"
            f"  content={m.content[:60]!r}"
        )


# ── Scenario helpers ──────────────────────────────────────────────────────────

def make_team(name: str, leader_id: int) -> Team:
    return Team(name=name, leader_id=leader_id, member_ids=[leader_id])


def fresh() -> tuple[BotState, MatchmakingEngine, FakeChannel]:
    """Return a clean store, engine, and fake channel for each scenario.

    The fake channel is returned to both ``get_channel`` calls (matchmaking
    and broadcast) so all bot messages flow into one inspectable list.
    """
    channel = FakeChannel()

    mock_bot = MagicMock()
    mock_bot.get_channel = MagicMock(return_value=channel)
    mock_bot.fetch_channel = AsyncMock(return_value=channel)

    fresh_store = BotState()
    # Redirect the module-level ``store`` binding that engine.py uses
    _eng_module.store = fresh_store  # type: ignore[assignment]

    engine = MatchmakingEngine(mock_bot)
    return fresh_store, engine, channel


# ── Scenarios ─────────────────────────────────────────────────────────────────

async def scenario_basic() -> None:
    """Full happy path: register → ready → accept → end → ready again."""
    section("SCENARIO: basic happy path")
    store, engine, channel = fresh()

    alpha = make_team("Alpha", 1001)
    bravo = make_team("Bravo", 2001)
    store.add_team(alpha)
    store.add_team(bravo)
    log("  Teams registered: Alpha, Bravo")

    alpha.state = bravo.state = TeamState.READY
    store.enqueue("Alpha")
    store.enqueue("Bravo")
    await engine.try_match()

    check(store.queue_size == 0,            "Both teams dequeued after match found")
    check(alpha.state == TeamState.MATCHED, "Alpha → MATCHED")
    check(bravo.state == TeamState.MATCHED, "Bravo → MATCHED")

    match_id = alpha.current_match_id
    assert match_id is not None
    match = store.get_match(match_id)
    check(match is not None, "Match stored")
    assert match is not None

    await engine.accept_match(match, "Alpha")
    check(match.team1_accepted,   "Alpha accepted")
    check(not match.active,       "Match not active yet (only one accepted)")

    await engine.accept_match(match, "Bravo")
    check(match.active,                      "Match active after both accepted")
    check(alpha.state == TeamState.IN_MATCH, "Alpha → IN_MATCH")
    check(bravo.state == TeamState.IN_MATCH, "Bravo → IN_MATCH")

    broadcast = next(
        (m for m in channel.messages if "MATCH STARTED" in m.content), None
    )
    check(broadcast is not None, "Broadcast message sent when match starts")

    await engine.end_match(match, ended_by="Alpha", winner_name="Alpha")
    check(alpha.state == TeamState.IDLE,      "Alpha → IDLE after match ends")
    check(bravo.state == TeamState.IDLE,      "Bravo → IDLE after match ends")
    check(store.get_match(match_id) is None,  "Match removed from store")
    check(alpha.last_opponent == "Bravo",     "Alpha's last_opponent set (rematch guard)")
    check(bravo.last_opponent == "Alpha",     "Bravo's last_opponent set (rematch guard)")
    check(alpha.wins == 1,                    "Alpha wins incremented")
    check(bravo.losses == 1,                  "Bravo losses incremented")
    check(alpha.mmr > 1000,                   "Alpha MMR increased after win")
    check(bravo.mmr < 1000,                   "Bravo MMR decreased after loss")

    # Both can re-queue immediately
    alpha.state = bravo.state = TeamState.READY
    store.enqueue("Alpha")
    store.enqueue("Bravo")
    check(store.queue_size == 2, "Both teams can re-queue after match ends")

    dump_channel(channel)
    log("  PASSED", GREEN)


async def scenario_rematch() -> None:
    """Instant-rematch guard: blocked with 2 teams, auto-lifted when only pair."""
    section("SCENARIO: instant-rematch prevention")
    store, engine, channel = fresh()

    alpha = make_team("Alpha", 1001)
    bravo = make_team("Bravo", 2001)
    store.add_team(alpha)
    store.add_team(bravo)
    alpha.last_opponent = "Bravo"
    bravo.last_opponent = "Alpha"
    log("  Alpha and Bravo are each other's last opponent")

    alpha.state = bravo.state = TeamState.READY
    store.enqueue("Alpha")
    store.enqueue("Bravo")

    # With only 2 teams the guard should auto-lift and they should still match
    await engine.try_match()

    check(alpha.last_opponent is None,      "Rematch block lifted (only pair available)")
    check(bravo.last_opponent is None,      "Rematch block lifted on Bravo too")
    check(alpha.state == TeamState.MATCHED, "Alpha matched after block lifted")
    check(bravo.state == TeamState.MATCHED, "Bravo matched after block lifted")

    # --- Three teams: blocked pair skipped in favour of valid third ---
    store2, engine2, channel2 = fresh()

    a = make_team("Alpha",   1001)
    b = make_team("Bravo",   2001)
    c = make_team("Charlie", 3001)
    store2.add_team(a)
    store2.add_team(b)
    store2.add_team(c)
    a.last_opponent = "Bravo"
    b.last_opponent = "Alpha"
    log("\n  Three teams: Alpha↔Bravo blocked, Charlie available")

    a.state = b.state = c.state = TeamState.READY
    store2.enqueue("Alpha")
    store2.enqueue("Bravo")
    store2.enqueue("Charlie")
    await engine2.try_match()

    match_key = next(iter(store2._matches))
    matched = store2.get_match(match_key)
    assert matched is not None
    pair = {matched.team1_name, matched.team2_name}
    check(
        not ({"Alpha", "Bravo"} == pair),
        f"Blocked pair not matched; got {pair} instead",
    )
    check("Charlie" in pair, "Charlie matched with an available team")

    dump_channel(channel2)
    log("  PASSED", GREEN)


async def scenario_parallel() -> None:
    """Four teams produce two simultaneous independent matches."""
    section("SCENARIO: parallel matches")
    store, engine, channel = fresh()

    teams = [
        make_team("Alpha",   1001),
        make_team("Bravo",   2001),
        make_team("Charlie", 3001),
        make_team("Delta",   4001),
    ]
    for t in teams:
        store.add_team(t)
        t.state = TeamState.READY
        store.enqueue(t.name)

    await engine.try_match()
    check(store.queue_size == 2, "First call: two teams matched, two still queued")

    await engine.try_match()
    check(store.queue_size == 0, "Second call: all four teams matched")

    for m in list(store._matches.values()):
        await engine.accept_match(m, m.team1_name)
        await engine.accept_match(m, m.team2_name)

    check(len(store.active_matches()) == 2, "Two matches simultaneously active")

    broadcasts = [m for m in channel.messages if "MATCH STARTED" in m.content]
    check(len(broadcasts) == 2, "Two separate broadcast messages sent")

    for m in list(store._matches.values()):
        await engine.end_match(m, ended_by=m.team1_name)

    check(len(store.active_matches()) == 0, "Both matches ended cleanly")
    for t in teams:
        check(t.state == TeamState.IDLE, f"{t.name} back to IDLE")

    dump_channel(channel)
    log("  PASSED", GREEN)


async def scenario_decline() -> None:
    """One leader declines → match cancelled, both teams reset to IDLE."""
    section("SCENARIO: decline")
    store, engine, channel = fresh()

    alpha = make_team("Alpha", 1001)
    bravo = make_team("Bravo", 2001)
    store.add_team(alpha)
    store.add_team(bravo)

    alpha.state = bravo.state = TeamState.READY
    store.enqueue("Alpha")
    store.enqueue("Bravo")
    await engine.try_match()

    match_id = alpha.current_match_id
    assert match_id is not None
    match = store.get_match(match_id)
    assert match is not None

    await engine.cancel_match(match, reason="declined by **Bravo**")

    check(alpha.state == TeamState.IDLE,    "Alpha → IDLE after decline")
    check(bravo.state == TeamState.IDLE,    "Bravo → IDLE after decline")
    check(store.get_match(match_id) is None,"Match removed after decline")
    check(alpha.last_opponent is None,      "No rematch guard set on decline")
    check(bravo.last_opponent is None,      "No rematch guard set on decline")

    dump_channel(channel)
    log("  PASSED", GREEN)


async def scenario_timeout() -> None:
    """Acceptance timeout: match auto-cancelled, teams back to IDLE."""
    section("SCENARIO: acceptance timeout")
    store, engine, channel = fresh()

    alpha = make_team("Alpha", 1001)
    bravo = make_team("Bravo", 2001)
    store.add_team(alpha)
    store.add_team(bravo)

    alpha.state = bravo.state = TeamState.READY
    store.enqueue("Alpha")
    store.enqueue("Bravo")
    await engine.try_match()

    match_id = alpha.current_match_id
    assert match_id is not None
    match = store.get_match(match_id)
    check(match is not None, "Match proposed")
    assert match is not None

    # One leader accepts, then time runs out
    await engine.accept_match(match, "Alpha")
    check(match.team1_accepted, "Alpha accepted before timeout")
    check(not match.active,     "Match not yet active")

    # Simulate the timeout handler firing
    await engine.cancel_match(match, reason="acceptance timeout")

    check(alpha.state == TeamState.IDLE,    "Alpha → IDLE after timeout")
    check(bravo.state == TeamState.IDLE,    "Bravo → IDLE after timeout")
    check(store.get_match(match_id) is None,"Match removed after timeout")

    dump_channel(channel)
    log("  PASSED", GREEN)


# ── Runner ────────────────────────────────────────────────────────────────────

SCENARIOS = {
    "basic":    scenario_basic,
    "rematch":  scenario_rematch,
    "parallel": scenario_parallel,
    "decline":  scenario_decline,
    "timeout":  scenario_timeout,
}


async def main(scenario: str) -> None:
    targets = list(SCENARIOS.values()) if scenario == "all" else [SCENARIOS[scenario]]
    for fn in targets:
        await fn()
    print(f"\n{BOLD}{GREEN}All scenarios passed ✓{RESET}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DAoC bot offline simulation")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=[*SCENARIOS.keys(), "all"],
        help="Which scenario to run (default: all)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print the full channel message log after each scenario",
    )
    args = parser.parse_args()
    _verbose = args.verbose

    asyncio.run(main(args.scenario))
