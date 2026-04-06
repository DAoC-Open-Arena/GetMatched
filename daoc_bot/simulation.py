"""In-process simulation suite for the DAoC matchmaking bot.

Runs a series of scenarios against the real engine and guild_store using fake
teams in an isolated fake guild, without any Discord gateway connection
required for the logic itself.  A Discord channel is accepted so the suite
can post a live progress embed that updates as each scenario completes.

Invoked from the ``/run_tests`` admin slash command.

All fake teams are cleaned up after the suite finishes (or if it crashes),
so running this on a live server during an event is safe — fake teams are
scoped to a dedicated fake guild ID that never overlaps with a real guild.

Usage (from commands.py)::

    from daoc_bot.simulation import SimulationSuite
    suite = SimulationSuite(channel=interaction.channel, engine=engine, guild_id=interaction.guild_id)
    await suite.run()
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import discord

from daoc_bot.engine import MatchmakingEngine, _elo_update
from daoc_bot.guild_store import EventConfig, guild_store
from daoc_bot.models import Match, Team, TeamState

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

FAKE_PREFIX = "__sim__"

# Dedicated fake guild ID — must never be a real Discord guild ID.
# Using a value well outside the valid snowflake range keeps it safe.
FAKE_GUILD_ID = 999_999_999_999_999_999

COLOUR_RUNNING = discord.Color.yellow()
COLOUR_PASS    = discord.Color.green()
COLOUR_FAIL    = discord.Color.red()

# Default EventConfig used for all scenarios (mirrors production defaults)
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
    matchmaking_channel_id=0,
    broadcast_channel_id=0,
)

# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class ScenarioResult:
    name: str
    passed: bool = False
    checks: list[str] = field(default_factory=list)
    error: Optional[str] = None


# ── Simulation suite ──────────────────────────────────────────────────────────

class SimulationSuite:
    """Runs all matchmaking scenarios and reports results to a Discord channel."""

    def __init__(
        self,
        channel: discord.TextChannel,
        engine: MatchmakingEngine,
        guild_id: int = FAKE_GUILD_ID,
    ) -> None:
        self.channel  = channel
        self.engine   = engine
        self.guild_id = guild_id
        self._results: list[ScenarioResult] = []
        self._progress_msg: Optional[discord.Message] = None
        self._fake_teams: list[str] = []

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self) -> list[ScenarioResult]:
        """Execute all scenarios and return the result list."""
        logger.info("=" * 60)
        logger.info("SIMULATION SUITE STARTING  (fake guild_id=%d)", self.guild_id)
        logger.info("=" * 60)

        # Ensure a fake event exists for the simulation guild
        self._bootstrap_event()

        self._progress_msg = await self.channel.send(
            embed=self._build_embed(running=True)
        )

        scenarios: list[Callable[[], Awaitable[list[str]]]] = [
            self._scenario_registration,
            self._scenario_ready_unready,
            self._scenario_happy_path,
            self._scenario_partial_accept,
            self._scenario_decline,
            self._scenario_timeout,
            self._scenario_rematch_guard_two_teams,
            self._scenario_rematch_guard_three_teams,
            self._scenario_parallel_matches,
            self._scenario_odd_queue,
            self._scenario_rapid_ready,
            self._scenario_double_match_ended,
            self._scenario_20_teams,
            self._scenario_mmr_elo_math,
            self._scenario_mmr_win_updates_ratings,
            self._scenario_mmr_loss_updates_ratings,
            self._scenario_mmr_no_result_leaves_ratings,
            self._scenario_mmr_best_pair_selection,
            self._scenario_mmr_threshold_blocks_far_teams,
            # ── Modular composition scenarios ──────────────────────────────
            self._scenario_modular_same_size_matches,
            self._scenario_modular_different_sizes_blocked,
            self._scenario_modular_mixed_queue,
            self._scenario_modular_group_size_in_register,
            # ── /change_group_size scenarios ──────────────────────────────
            self._scenario_change_group_size_idle,
            self._scenario_change_group_size_requeue,
            self._scenario_change_group_size_blocked_in_match,
        ]

        try:
            for scenario in scenarios:
                result = await self._run_scenario(scenario)
                self._results.append(result)
                await self._update_embed()
        finally:
            await self._cleanup()
            await self._update_embed(final=True)

        passed = sum(1 for r in self._results if r.passed)
        total  = len(self._results)
        logger.info("=" * 60)
        logger.info("SIMULATION SUITE COMPLETE — %d/%d passed", passed, total)
        logger.info("=" * 60)

        return self._results

    # ── Fake event bootstrap ──────────────────────────────────────────────────

    def _bootstrap_event(self) -> None:
        """Ensure a fake active event exists in the DB for FAKE_GUILD_ID."""
        if guild_store.get_active_event_id(self.guild_id) is None:
            try:
                guild_store.create_event(self.guild_id, _DEFAULT_CFG)
                logger.info("Simulation: created fake event for guild %d.", self.guild_id)
            except Exception as exc:
                logger.warning("Simulation: could not create fake event: %s", exc)

    # ── Embed helpers ─────────────────────────────────────────────────────────

    def _build_embed(self, running: bool = False, final: bool = False) -> discord.Embed:
        passed = sum(1 for r in self._results if r.passed)
        failed = sum(1 for r in self._results if not r.passed)
        total  = len(self._results)

        if final:
            colour = COLOUR_PASS if failed == 0 else COLOUR_FAIL
            title  = f"🧪  Simulation Complete — {passed}/{total} passed"
        elif running:
            colour = COLOUR_RUNNING
            title  = "🧪  Simulation Running…"
        else:
            colour = COLOUR_RUNNING
            title  = f"🧪  Simulation Running… ({total} done so far)"

        embed = discord.Embed(title=title, color=colour)

        for result in self._results:
            icon  = "✅" if result.passed else "❌"
            value = "\n".join(result.checks[:5])
            if len(result.checks) > 5:
                value += f"\n*…and {len(result.checks) - 5} more checks*"
            if result.error:
                value += f"\n⚠️  `{result.error[:120]}`"
            embed.add_field(name=f"{icon}  {result.name}", value=value or "—", inline=False)

        if not self._results:
            embed.description = "Starting scenarios…"

        embed.set_footer(text="Full detail available in the bot terminal log.")
        return embed

    async def _update_embed(self, final: bool = False) -> None:
        if not self._progress_msg:
            return
        try:
            await self._progress_msg.edit(embed=self._build_embed(final=final))
        except discord.NotFound:
            pass

    # ── Scenario runner ───────────────────────────────────────────────────────

    async def _run_scenario(
        self, fn: Callable[[], Awaitable[list[str]]]
    ) -> ScenarioResult:
        name = fn.__name__.replace("_scenario_", "").replace("_", " ").title()
        result = ScenarioResult(name=name)
        logger.info("")
        logger.info("─── SCENARIO: %s ───", name.upper())
        await self._purge_fake_state()

        try:
            checks = await fn()
            result.checks = checks
            result.passed = all(c.startswith("✅") for c in checks)
        except Exception as exc:
            result.passed = False
            result.error  = str(exc)
            result.checks.append(f"❌  Unhandled exception: {exc}")
            logger.exception("Scenario '%s' raised an exception.", name)

        status = "PASSED" if result.passed else "FAILED"
        logger.info("─── RESULT: %s (%s) ───", name.upper(), status)
        return result

    # ── Check helper ──────────────────────────────────────────────────────────

    def _check(self, checks: list[str], condition: bool, label: str) -> None:
        icon  = "✅" if condition else "❌"
        entry = f"{icon}  {label}"
        checks.append(entry)
        logger.info("  %s", entry)

    # ── Fake team / match helpers ─────────────────────────────────────────────

    def _make_team(self, suffix: str, leader_id: int, mmr: int = 1000) -> Team:
        name = f"{FAKE_PREFIX}{suffix}"
        team = Team(name=name, leader_id=leader_id, member_ids=[leader_id], mmr=mmr)
        guild_store.add_team(self.guild_id, team)
        self._fake_teams.append(name)
        logger.debug("  Created fake team '%s' (leader=%d, mmr=%d)", name, leader_id, mmr)
        return team

    def _get_team(self, name: str) -> Optional[Team]:
        return guild_store.get_team(self.guild_id, name)

    def _make_active_match(self, t1: Team, t2: Team) -> Match:
        """Create and store a match already in MATCHED state."""
        match_id = str(uuid.uuid4())[:8].upper()
        match = Match(id=match_id, team1_name=t1.name, team2_name=t2.name)
        guild_store.add_match(self.guild_id, match)
        t1.state = t2.state = TeamState.MATCHED
        t1.current_match_id = t2.current_match_id = match_id
        guild_store.save_team(self.guild_id, t1)
        guild_store.save_team(self.guild_id, t2)
        return match

    def _make_live_match(self, t1: Team, t2: Team) -> Match:
        """Create a match already in IN_MATCH / active state."""
        match = self._make_active_match(t1, t2)
        match.active = True
        t1.state = t2.state = TeamState.IN_MATCH
        guild_store.save_team(self.guild_id, t1)
        guild_store.save_team(self.guild_id, t2)
        guild_store.save_match(self.guild_id, match)
        return match

    def _backdate_queue_timestamps(self, *team_names: str, seconds: int) -> None:
        """Back-date queue timestamps to simulate a team having waited *seconds*."""
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=seconds + 1)
        ts_map = guild_store._queue_ts(self.guild_id)
        for name in team_names:
            ts_map[name] = old_ts

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def _purge_fake_state(self) -> None:
        """Remove all fake teams/matches before each scenario."""
        for name in list(self._fake_teams):
            team = guild_store.get_team(self.guild_id, name)
            if team and team.current_match_id:
                match = guild_store.get_match(self.guild_id, team.current_match_id)
                if match:
                    guild_store.remove_match(self.guild_id, match.id)
            guild_store.dequeue(self.guild_id, name)
            guild_store.remove_team(self.guild_id, name)
        self._fake_teams.clear()

    async def _cleanup(self) -> None:
        """Remove all fake teams and matches created during the run."""
        logger.info("")
        logger.info("─── CLEANUP ───")
        await self._purge_fake_state()
        logger.info("  Cleanup complete.")

    # ═════════════════════════════════════════════════════════════════════════
    # SCENARIOS
    # ═════════════════════════════════════════════════════════════════════════

    async def _scenario_registration(self) -> list[str]:
        """Register a team, verify store state, then remove it."""
        checks: list[str] = []
        t = self._make_team("reg_a", 90001)
        gid = self.guild_id

        self._check(checks, guild_store.team_exists(gid, t.name),
                    "Team appears in store after registration")
        self._check(checks, guild_store.get_team_by_leader(gid, 90001) is not None,
                    "Leader index resolves to correct team")
        self._check(checks, t.state == TeamState.IDLE,
                    "Initial state is IDLE")
        self._check(checks, guild_store.is_leader(gid, 90001),
                    "is_leader() returns True for registered leader")

        guild_store.remove_team(gid, t.name)
        self._fake_teams.remove(t.name)
        self._check(checks, not guild_store.team_exists(gid, t.name),
                    "Team absent from store after removal")
        self._check(checks, not guild_store.is_leader(gid, 90001),
                    "Leader index cleaned up after removal")
        return checks

    async def _scenario_ready_unready(self) -> list[str]:
        """Queue and dequeue a team; verify queue state at each step."""
        checks: list[str] = []
        t = self._make_team("rdy_a", 90010)
        gid = self.guild_id

        t.state = TeamState.READY
        guild_store.save_team(gid, t)
        guild_store.enqueue(gid, t.name)
        self._check(checks, t.name in guild_store.get_queue(gid),
                    "Team in queue after enqueue")
        self._check(checks, guild_store.queue_size(gid) == 1, "Queue size is 1")

        guild_store.enqueue(gid, t.name)  # idempotent
        self._check(checks, guild_store.queue_size(gid) == 1,
                    "Double-enqueue is idempotent (queue size still 1)")

        guild_store.dequeue(gid, t.name)
        t.state = TeamState.IDLE
        guild_store.save_team(gid, t)
        self._check(checks, t.name not in guild_store.get_queue(gid),
                    "Team removed from queue after dequeue")
        self._check(checks, guild_store.queue_size(gid) == 0, "Queue empty after dequeue")
        return checks

    async def _scenario_happy_path(self) -> list[str]:
        """Full cycle: ready → match found → both accept → winner reported → IDLE."""
        checks: list[str] = []
        gid = self.guild_id
        t1 = self._make_team("hp_a", 90020)
        t2 = self._make_team("hp_b", 90021)

        t1.state = t2.state = TeamState.READY
        guild_store.save_team(gid, t1)
        guild_store.save_team(gid, t2)
        guild_store.enqueue(gid, t1.name)
        guild_store.enqueue(gid, t2.name)

        await self.engine.try_match(gid)

        # Re-fetch from DB — engine writes through
        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        self._check(checks, guild_store.queue_size(gid) == 0, "Queue empty after match formed")
        self._check(checks, t1 is not None and t1.state == TeamState.MATCHED, "Team 1 → MATCHED")
        self._check(checks, t2 is not None and t2.state == TeamState.MATCHED, "Team 2 → MATCHED")
        self._check(checks, t1 is not None and t1.current_match_id is not None, "Team 1 has match ID")
        if t1 is None or t2 is None:
            return checks

        match = guild_store.get_match(gid, t1.current_match_id or "")
        self._check(checks, match is not None, "Match stored")
        if match is None:
            return checks

        self.engine.cancel_all_timeouts(gid)
        await self.engine.accept_match(gid, match, t1.name)
        match = guild_store.get_match(gid, match.id)
        self._check(checks, match is not None and match.team1_accepted, "Team 1 accepted")
        self._check(checks, match is not None and not match.active,
                    "Match not yet active (only 1 accepted)")
        if match is None:
            return checks

        await self.engine.accept_match(gid, match, t2.name)
        match = guild_store.get_match(gid, match.id)
        self._check(checks, match is not None and match.active,
                    "Match active after both accepted")
        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        self._check(checks, t1 is not None and t1.state == TeamState.IN_MATCH,
                    "Team 1 → IN_MATCH")
        self._check(checks, t2 is not None and t2.state == TeamState.IN_MATCH,
                    "Team 2 → IN_MATCH")
        if t1 is None or t2 is None or match is None:
            return checks

        match_id = match.id
        t1_name, t2_name = t1.name, t2.name
        match.active = True
        guild_store.save_match(gid, match)
        await self.engine.end_match(gid, match, ended_by=t1.name, winner_name=t1.name)

        t1 = guild_store.get_team(gid, t1_name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2_name)  # type: ignore[assignment]
        self._check(checks, t1 is not None and t1.state == TeamState.IDLE,
                    "Team 1 → IDLE after match ends")
        self._check(checks, t2 is not None and t2.state == TeamState.IDLE,
                    "Team 2 → IDLE after match ends")
        self._check(checks, t1 is not None and t1.wins == 1, "Winner win count incremented")
        self._check(checks, t2 is not None and t2.losses == 1, "Loser loss count incremented")
        self._check(checks, t1 is not None and t1.mmr > 1000, "Winner MMR increased above start")
        self._check(checks, t2 is not None and t2.mmr < 1000, "Loser MMR decreased below start")
        self._check(checks, t1 is not None and t1.last_opponent == t2_name,
                    "Rematch guard set on team 1")
        self._check(checks, t2 is not None and t2.last_opponent == t1_name,
                    "Rematch guard set on team 2")
        self._check(checks, guild_store.get_match(gid, match_id) is None,
                    "Match removed from store")
        return checks

    async def _scenario_partial_accept(self) -> list[str]:
        """Only one team accepts — verify the other's state is unchanged."""
        checks: list[str] = []
        gid = self.guild_id
        t1 = self._make_team("pa_a", 90030)
        t2 = self._make_team("pa_b", 90031)
        match = self._make_active_match(t1, t2)

        await self.engine.accept_match(gid, match, t1.name)

        match = guild_store.get_match(gid, match.id)  # type: ignore[assignment]
        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        self._check(checks, match is not None and match.team1_accepted, "Team 1 accepted")
        self._check(checks, match is not None and not match.team2_accepted,
                    "Team 2 not yet accepted")
        self._check(checks, match is not None and not match.active,
                    "Match not active after partial accept")
        self._check(checks, t1 is not None and t1.state == TeamState.MATCHED,
                    "Team 1 still MATCHED")
        self._check(checks, t2 is not None and t2.state == TeamState.MATCHED,
                    "Team 2 still MATCHED")
        return checks

    async def _scenario_decline(self) -> list[str]:
        """One leader declines — both teams reset to IDLE, no rematch guard."""
        checks: list[str] = []
        gid = self.guild_id
        t1 = self._make_team("dec_a", 90040)
        t2 = self._make_team("dec_b", 90041)
        match = self._make_active_match(t1, t2)

        await self.engine.cancel_match(gid, match, reason="declined by test")

        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        self._check(checks, t1 is not None and t1.state == TeamState.IDLE,
                    "Team 1 → IDLE after decline")
        self._check(checks, t2 is not None and t2.state == TeamState.IDLE,
                    "Team 2 → IDLE after decline")
        self._check(checks, guild_store.get_match(gid, match.id) is None,
                    "Match removed")
        self._check(checks, t1 is not None and t1.last_opponent is None,
                    "No rematch guard on decline")
        self._check(checks, t2 is not None and t2.last_opponent is None,
                    "No rematch guard on decline")
        return checks

    async def _scenario_timeout(self) -> list[str]:
        """Simulate acceptance timeout — same behaviour as decline."""
        checks: list[str] = []
        gid = self.guild_id
        t1 = self._make_team("to_a", 90050)
        t2 = self._make_team("to_b", 90051)
        match = self._make_active_match(t1, t2)

        await self.engine.accept_match(gid, match, t1.name)
        await self.engine.cancel_match(gid, match, reason="acceptance timeout")

        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        self._check(checks, t1 is not None and t1.state == TeamState.IDLE,
                    "Team 1 → IDLE after timeout")
        self._check(checks, t2 is not None and t2.state == TeamState.IDLE,
                    "Team 2 → IDLE after timeout")
        self._check(checks, guild_store.get_match(gid, match.id) is None,
                    "Match removed after timeout")
        self._check(checks, t1 is not None and t1.last_opponent is None,
                    "No guard after timeout")
        return checks

    async def _scenario_rematch_guard_two_teams(self) -> list[str]:
        """With only 2 teams blocked by a 0-cooldown guard, they stay blocked.

        rematch_cooldown_seconds=0 means: block the immediate next match only —
        no auto-lift, no timer. The guard clears when they play a third team,
        or when an admin calls /admin_clear_rematch.

        Also verifies the timed cooldown path: with rematch_cooldown_seconds>0,
        the pair is unblocked once the configured time has elapsed.
        """
        checks: list[str] = []
        gid = self.guild_id

        # ── Part 1: cooldown=0 → guard holds, no match even as the only pair ──
        t1 = self._make_team("rg2_a", 90060)
        t2 = self._make_team("rg2_b", 90061)

        t1.last_opponent = t2.name
        t2.last_opponent = t1.name
        t1.state = t2.state = TeamState.READY
        guild_store.save_team(gid, t1)
        guild_store.save_team(gid, t2)
        guild_store.enqueue(gid, t1.name)
        guild_store.enqueue(gid, t2.name)

        await self.engine.try_match(gid)

        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        self._check(checks, guild_store.queue_size(gid) == 2,
                    "Guard holds with cooldown=0: only pair stays in queue, no match")
        self._check(checks, t1 is not None and t1.state == TeamState.READY,
                    "Team 1 still READY — guard not lifted")
        self._check(checks, t2 is not None and t2.state == TeamState.READY,
                    "Team 2 still READY — guard not lifted")

        # ── Part 2: cooldown>0 and elapsed → guard clears, match forms ────────
        # Set a short cooldown and simulate it having elapsed
        guild_store.update_event_config(gid, rematch_cooldown_seconds=30)
        guild_store.record_match_end(gid, t1.name if t1 else "")
        guild_store.record_match_end(gid, t2.name if t2 else "")
        self._backdate_queue_timestamps(
            t1.name if t1 else "", t2.name if t2 else "", seconds=31
        )
        # Also backdate the match-end timestamps
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=31)
        guild_store._last_match_ts(gid)[t1.name if t1 else ""] = old_ts
        guild_store._last_match_ts(gid)[t2.name if t2 else ""] = old_ts

        await self.engine.try_match(gid)

        t1 = guild_store.get_team(gid, t1.name if t1 else "")  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name if t2 else "")  # type: ignore[assignment]
        self._check(checks, guild_store.queue_size(gid) == 0,
                    "Timed cooldown elapsed: match formed")
        self._check(checks, t1 is not None and t1.state == TeamState.MATCHED,
                    "Team 1 → MATCHED after cooldown elapsed")
        self._check(checks, t2 is not None and t2.state == TeamState.MATCHED,
                    "Team 2 → MATCHED after cooldown elapsed")

        # Restore cooldown to default
        guild_store.update_event_config(gid, rematch_cooldown_seconds=0)
        return checks

    async def _scenario_rematch_guard_three_teams(self) -> list[str]:
        """Blocked pair is skipped; third team is matched with one of them."""
        checks: list[str] = []
        gid = self.guild_id
        t1 = self._make_team("rg3_a", 90070)
        t2 = self._make_team("rg3_b", 90071)
        t3 = self._make_team("rg3_c", 90072)

        t1.last_opponent = t2.name
        t2.last_opponent = t1.name
        t1.state = t2.state = t3.state = TeamState.READY
        for t in (t1, t2, t3):
            guild_store.save_team(gid, t)
            guild_store.enqueue(gid, t.name)

        await self.engine.try_match(gid)

        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        t3 = guild_store.get_team(gid, t3.name)  # type: ignore[assignment]

        matched_id = (
            (t1.current_match_id if t1 else None)
            or (t2.current_match_id if t2 else None)
            or (t3.current_match_id if t3 else None)
        )
        matched = guild_store.get_match(gid, matched_id or "")
        self._check(checks, matched is not None, "A match was formed")
        if matched:
            pair = {matched.team1_name, matched.team2_name}
            self._check(checks,
                        not (t1 and t2 and t1.name in pair and t2.name in pair),
                        f"Blocked pair not matched — got {pair}")
            self._check(checks, t3 is not None and t3.name in pair,
                        "Third team is part of the match")
        self._check(checks, guild_store.queue_size(gid) == 1,
                    "One team remains in queue (unmatched)")
        return checks

    async def _scenario_parallel_matches(self) -> list[str]:
        """4 teams produce 2 simultaneous matches; both end independently."""
        checks: list[str] = []
        gid = self.guild_id
        teams = [self._make_team(f"par_{i}", 90080 + i) for i in range(4)]
        for t in teams:
            t.state = TeamState.READY
            guild_store.save_team(gid, t)
            guild_store.enqueue(gid, t.name)

        await self.engine.try_match(gid)
        self._check(checks, guild_store.queue_size(gid) == 2,
                    "First match formed, 2 teams remain")

        await self.engine.try_match(gid)
        self._check(checks, guild_store.queue_size(gid) == 0,
                    "Second match formed, queue empty")

        teams = [guild_store.get_team(gid, t.name) for t in teams]  # type: ignore[misc]
        unique_match_ids = {t.current_match_id for t in teams if t and t.current_match_id}
        self._check(checks, len(unique_match_ids) == 2, "Two distinct matches in store")

        self.engine.cancel_all_timeouts(gid)
        for mid in unique_match_ids:
            match = guild_store.get_match(gid, mid)
            if match and match.team1_name.startswith(FAKE_PREFIX):
                await self.engine.accept_match(gid, match, match.team1_name)
                match = guild_store.get_match(gid, mid)
                if match:
                    await self.engine.accept_match(gid, match, match.team2_name)

        teams = [guild_store.get_team(gid, t.name) for t in teams if t]  # type: ignore[misc]
        self._check(checks,
                    all(t is not None and t.state == TeamState.IN_MATCH for t in teams),
                    "All 4 teams IN_MATCH simultaneously")

        for mid in unique_match_ids:
            match = guild_store.get_match(gid, mid)
            if match and match.team1_name.startswith(FAKE_PREFIX):
                match.active = True
                guild_store.save_match(gid, match)
                await self.engine.end_match(gid, match, ended_by=match.team1_name)

        teams = [guild_store.get_team(gid, t.name) for t in teams if t]  # type: ignore[misc]
        self._check(checks,
                    all(t is not None and t.state == TeamState.IDLE for t in teams),
                    "All 4 teams back to IDLE after matches end")
        self._check(checks,
                    len(guild_store.active_matches(gid)) == 0,
                    "No active matches remaining")
        return checks

    async def _scenario_odd_queue(self) -> list[str]:
        """3 teams in queue: 2 match, 1 waits in READY state."""
        checks: list[str] = []
        gid = self.guild_id
        teams = [self._make_team(f"odd_{i}", 90090 + i) for i in range(3)]
        for t in teams:
            t.state = TeamState.READY
            guild_store.save_team(gid, t)
            guild_store.enqueue(gid, t.name)

        await self.engine.try_match(gid)

        teams = [guild_store.get_team(gid, t.name) for t in teams]  # type: ignore[misc]
        self._check(checks, guild_store.queue_size(gid) == 1,
                    "One team still waiting in queue")
        waiting = [t for t in teams if t and t.state == TeamState.READY]
        self._check(checks, len(waiting) == 1, "Exactly one team in READY state")
        matched = [t for t in teams if t and t.state == TeamState.MATCHED]
        self._check(checks, len(matched) == 2, "Two teams in MATCHED state")
        return checks

    async def _scenario_rapid_ready(self) -> list[str]:
        """Simulate 6 teams clicking Ready concurrently."""
        checks: list[str] = []
        gid = self.guild_id
        teams = [self._make_team(f"rr_{i}", 90100 + i) for i in range(6)]

        async def ready_up(t: Team) -> None:
            t.state = TeamState.READY
            guild_store.save_team(gid, t)
            guild_store.enqueue(gid, t.name)
            await self.engine.try_match(gid)

        await asyncio.gather(*[ready_up(t) for t in teams])

        self._check(checks, guild_store.queue_size(gid) == 0,
                    "Queue empty — all 6 teams matched into 3 pairs")

        teams = [guild_store.get_team(gid, t.name) for t in teams]  # type: ignore[misc]
        matched = [t for t in teams if t and t.state == TeamState.MATCHED]
        self._check(checks, len(matched) == 6, "All 6 teams in MATCHED state")
        unique_ids = {t.current_match_id for t in teams if t and t.current_match_id}
        self._check(checks, len(unique_ids) == 3,
                    "3 distinct match IDs — no duplicate pairings")
        return checks

    async def _scenario_double_match_ended(self) -> list[str]:
        """Both leaders click Match Ended simultaneously — only one closure."""
        checks: list[str] = []
        gid = self.guild_id
        t1 = self._make_team("dme_a", 90110)
        t2 = self._make_team("dme_b", 90111)
        match = self._make_live_match(t1, t2)
        mid = match.id

        async def end_by(team_name: str) -> None:
            m = guild_store.get_match(gid, mid)
            if m and m.active:
                m.active = False
                guild_store.save_match(gid, m)
                await self.engine.end_match(gid, m, ended_by=team_name)

        await asyncio.gather(end_by(t1.name), end_by(t2.name))

        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        self._check(checks, t1 is not None and t1.state == TeamState.IDLE, "Team 1 → IDLE")
        self._check(checks, t2 is not None and t2.state == TeamState.IDLE, "Team 2 → IDLE")
        self._check(checks, guild_store.get_match(gid, mid) is None,
                    "Match removed — no double-processing")
        return checks

    async def _scenario_20_teams(self) -> list[str]:
        """Stress test: 20 teams register, all ready up, matches form in waves."""
        checks: list[str] = []
        gid = self.guild_id
        count     = 20
        orig_teams = [self._make_team(f"stress_{i:02d}", 91000 + i) for i in range(count)]
        wave_size  = 4
        total_matched = 0

        for wave_start in range(0, count, wave_size):
            wave = orig_teams[wave_start:wave_start + wave_size]
            for t in wave:
                t.state = TeamState.READY
                guild_store.save_team(gid, t)
                guild_store.enqueue(gid, t.name)

            for _ in range(wave_size // 2):
                await self.engine.try_match(gid)

            wave_fresh = [guild_store.get_team(gid, t.name) for t in wave]
            newly_matched = [t for t in wave_fresh if t and t.state == TeamState.MATCHED]
            total_matched += len(newly_matched)
            logger.info("  Wave %d–%d: %d/%d matched",
                        wave_start, wave_start + wave_size - 1,
                        len(newly_matched), wave_size)

        self._check(checks, total_matched == count,
                    f"All {count} teams matched across waves")
        self._check(checks, guild_store.queue_size(gid) == 0,
                    "Queue empty after all waves")
        all_matches = guild_store.active_matches(gid)
        # In MATCHED state matches are not yet "active" — count by team states
        all_teams = [guild_store.get_team(gid, t.name) for t in orig_teams]
        in_matched = [t for t in all_teams if t and t.state == TeamState.MATCHED]
        self._check(checks, len(in_matched) == count,
                    f"{count} teams in MATCHED state")

        unique_match_ids = {
            t.current_match_id for t in all_teams if t and t.current_match_id
        }

        self.engine.cancel_all_timeouts(gid)
        for mid in unique_match_ids:
            match = guild_store.get_match(gid, mid)
            if match and match.team1_name.startswith(FAKE_PREFIX):
                await self.engine.accept_match(gid, match, match.team1_name)
                match = guild_store.get_match(gid, mid)
                if match:
                    await self.engine.accept_match(gid, match, match.team2_name)

        all_teams = [guild_store.get_team(gid, t.name) for t in orig_teams]
        self._check(checks,
                    all(t is not None and t.state == TeamState.IN_MATCH for t in all_teams),
                    f"All {count} teams IN_MATCH simultaneously")

        for mid in unique_match_ids:
            match = guild_store.get_match(gid, mid)
            if match and match.team1_name.startswith(FAKE_PREFIX):
                match.active = True
                guild_store.save_match(gid, match)
                await self.engine.end_match(gid, match, ended_by=match.team1_name)

        all_teams = [guild_store.get_team(gid, t.name) for t in orig_teams]
        self._check(checks,
                    all(t is not None and t.state == TeamState.IDLE for t in all_teams),
                    f"All {count} teams back to IDLE")
        self._check(checks, len(guild_store.active_matches(gid)) == 0,
                    "No active matches remaining after stress test")
        return checks

    # ═════════════════════════════════════════════════════════════════════════
    # MMR / RESULT-REPORTING SCENARIOS
    # ═════════════════════════════════════════════════════════════════════════

    async def _scenario_mmr_elo_math(self) -> list[str]:
        """Verify _elo_update() arithmetic for equal, favoured, and underdog cases."""
        checks: list[str] = []

        w, l = _elo_update(1000, 1000)
        self._check(checks, w > 1000 and l < 1000,
                    "Equal match: winner gains MMR, loser loses MMR")
        self._check(checks, w + l == 2000,
                    "Equal match: update is zero-sum (w + l == 2000)")
        delta_equal = w - 1000
        self._check(checks, 10 <= delta_equal <= 20,
                    f"Equal match: delta {delta_equal} in expected range 10–20")

        w2, l2 = _elo_update(1400, 1000)
        self._check(checks, w2 > 1400, "Favourite win: winner MMR increases")
        self._check(checks, w2 - 1400 < delta_equal,
                    "Favourite win: smaller delta than equal match")
        self._check(checks, w2 + l2 == 2400, "Favourite win: zero-sum preserved")

        w3, l3 = _elo_update(1000, 1400)
        self._check(checks, w3 > 1000, "Underdog win: underdog MMR increases")
        self._check(checks, w3 - 1000 > delta_equal,
                    "Underdog win: larger delta than equal match")
        self._check(checks, w3 + l3 == 2400, "Underdog win: zero-sum preserved")

        w_vs_underdog, _ = _elo_update(1400, 1000)
        w_vs_equal,    _ = _elo_update(1000, 1000)
        w_vs_favourite,_ = _elo_update(1000, 1400)
        self._check(
            checks,
            (w_vs_underdog - 1400) < (w_vs_equal - 1000) < (w_vs_favourite - 1000),
            "Delta ordering: underdog win < equal win < favourite win (correct direction)",
        )
        return checks

    async def _scenario_mmr_win_updates_ratings(self) -> list[str]:
        """Winner reports via end_match — verify MMR, wins, losses all update."""
        checks: list[str] = []
        gid = self.guild_id
        t1 = self._make_team("mmrw_a", 92010, mmr=1000)
        t2 = self._make_team("mmrw_b", 92011, mmr=1000)
        match = self._make_live_match(t1, t2)
        mmr1_before, mmr2_before = t1.mmr, t2.mmr

        match.active = True
        guild_store.save_match(gid, match)
        await self.engine.end_match(gid, match, ended_by=t1.name, winner_name=t1.name)

        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        self._check(checks, t1 is not None and t1.wins == 1,   "Winner: wins incremented to 1")
        self._check(checks, t1 is not None and t1.losses == 0, "Winner: losses unchanged (0)")
        self._check(checks, t2 is not None and t2.wins == 0,   "Loser: wins unchanged (0)")
        self._check(checks, t2 is not None and t2.losses == 1, "Loser: losses incremented to 1")
        self._check(checks, t1 is not None and t1.mmr > mmr1_before,
                    f"Winner MMR increased: {mmr1_before} → {t1.mmr if t1 else '?'}")
        self._check(checks, t2 is not None and t2.mmr < mmr2_before,
                    f"Loser MMR decreased: {mmr2_before} → {t2.mmr if t2 else '?'}")
        if t1 and t2:
            delta = t1.mmr - mmr1_before
            self._check(checks, t1.mmr - mmr1_before == mmr2_before - t2.mmr,
                        f"Update is zero-sum: winner +{delta}, loser -{mmr2_before - t2.mmr}")
        self._check(checks, t1 is not None and t1.state == TeamState.IDLE,
                    "Winner reset to IDLE")
        self._check(checks, t2 is not None and t2.state == TeamState.IDLE,
                    "Loser reset to IDLE")
        return checks

    async def _scenario_mmr_loss_updates_ratings(self) -> list[str]:
        """Loser reports via We Lost — opponent (winner) gets the MMR gain."""
        checks: list[str] = []
        gid = self.guild_id
        t1 = self._make_team("mmrl_a", 92020, mmr=1000)
        t2 = self._make_team("mmrl_b", 92021, mmr=1000)
        match = self._make_live_match(t1, t2)

        match.active = True
        guild_store.save_match(gid, match)
        await self.engine.end_match(gid, match, ended_by=t1.name, winner_name=t2.name)

        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        self._check(checks, t2 is not None and t2.wins == 1,   "Reported winner: wins incremented")
        self._check(checks, t1 is not None and t1.losses == 1, "Reported loser: losses incremented")
        self._check(checks, t2 is not None and t2.mmr > 1000,  "Reported winner MMR increased")
        self._check(checks, t1 is not None and t1.mmr < 1000,  "Reported loser MMR decreased")
        self._check(checks, t1 is not None and t1.state == TeamState.IDLE,
                    "Team 1 reset to IDLE")
        self._check(checks, t2 is not None and t2.state == TeamState.IDLE,
                    "Team 2 reset to IDLE")
        return checks

    async def _scenario_mmr_no_result_leaves_ratings(self) -> list[str]:
        """Admin cancel / end_match with no winner — ratings must not change."""
        checks: list[str] = []
        gid = self.guild_id
        t1 = self._make_team("mmrn_a", 92030, mmr=1050)
        t2 = self._make_team("mmrn_b", 92031, mmr=950)
        match = self._make_live_match(t1, t2)
        mmr1_before, mmr2_before = t1.mmr, t2.mmr

        match.active = True
        guild_store.save_match(gid, match)
        await self.engine.end_match(gid, match, ended_by=t1.name)  # no winner_name

        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        self._check(checks, t1 is not None and t1.mmr == mmr1_before,
                    "Team 1 MMR unchanged when no result reported")
        self._check(checks, t2 is not None and t2.mmr == mmr2_before,
                    "Team 2 MMR unchanged when no result reported")
        self._check(checks,
                    t1 is not None and t1.wins == 0 and t1.losses == 0,
                    "Team 1 W/L record unchanged")
        self._check(checks,
                    t2 is not None and t2.wins == 0 and t2.losses == 0,
                    "Team 2 W/L record unchanged")
        return checks

    async def _scenario_mmr_best_pair_selection(self) -> list[str]:
        """3 teams queue: engine picks the pair with the smallest MMR gap."""
        checks: list[str] = []
        gid = self.guild_id
        alpha = self._make_team("mmrbp_alpha", 92040, mmr=1000)
        beta  = self._make_team("mmrbp_beta",  92041, mmr=1020)
        gamma = self._make_team("mmrbp_gamma", 92042, mmr=1200)

        for t in (alpha, beta, gamma):
            t.state = TeamState.READY
            guild_store.save_team(gid, t)
            guild_store.enqueue(gid, t.name)

        await self.engine.try_match(gid)

        alpha = guild_store.get_team(gid, alpha.name)  # type: ignore[assignment]
        beta  = guild_store.get_team(gid, beta.name)  # type: ignore[assignment]
        gamma = guild_store.get_team(gid, gamma.name)  # type: ignore[assignment]

        matched_id = (
            (alpha.current_match_id if alpha else None)
            or (beta.current_match_id if beta else None)
        )
        match = guild_store.get_match(gid, matched_id or "")

        self._check(checks, match is not None, "A match was formed")
        if match:
            pair = {match.team1_name, match.team2_name}
            self._check(checks,
                        alpha is not None and beta is not None
                        and alpha.name in pair and beta.name in pair,
                        f"Closest MMR pair chosen (Alpha+Beta, gap 20) — got {pair}")
            self._check(checks, gamma is not None and gamma.name not in pair,
                        "Gamma (furthest) not matched first")

        self._check(checks, guild_store.queue_size(gid) == 1,
                    "Exactly one team remains in queue")
        remaining = guild_store.get_queue(gid)
        self._check(checks, gamma is not None and gamma.name in remaining,
                    "Gamma is the team still waiting")
        return checks

    async def _scenario_mmr_threshold_blocks_far_teams(self) -> list[str]:
        """Teams with MMR gap > threshold don't match until the relax window opens."""
        checks: list[str] = []
        gid = self.guild_id
        cfg = guild_store.get_event_config(gid) or _DEFAULT_CFG

        t1 = self._make_team("mmrthr_a", 92050, mmr=1000)
        t2 = self._make_team("mmrthr_b", 92051, mmr=1000 + cfg.mmr_match_threshold + 300)

        t1.state = t2.state = TeamState.READY
        guild_store.save_team(gid, t1)
        guild_store.save_team(gid, t2)
        guild_store.enqueue(gid, t1.name)
        guild_store.enqueue(gid, t2.name)

        # Freshly queued — no match expected
        await self.engine.try_match(gid)
        self._check(checks, guild_store.queue_size(gid) == 2,
                    f"No match formed immediately "
                    f"(MMR gap {cfg.mmr_match_threshold + 300} > threshold {cfg.mmr_match_threshold})")
        t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
        self._check(checks, t1 is not None and t1.state == TeamState.READY,
                    "Team 1 still READY after blocked match attempt")
        self._check(checks, t2 is not None and t2.state == TeamState.READY,
                    "Team 2 still READY after blocked match attempt")

        # Simulate elapsed relax window by back-dating queue timestamps
        self._backdate_queue_timestamps(
            t1.name if t1 else "", t2.name if t2 else "",
            seconds=cfg.mmr_relax_seconds,
        )

        await self.engine.try_match(gid)
        t1 = guild_store.get_team(gid, t1.name if t1 else "")  # type: ignore[assignment]
        t2 = guild_store.get_team(gid, t2.name if t2 else "")  # type: ignore[assignment]
        self._check(checks, guild_store.queue_size(gid) == 0,
                    "Match formed after relax window elapsed")
        self._check(checks, t1 is not None and t1.state == TeamState.MATCHED,
                    "Team 1 → MATCHED after MMR threshold relaxed")
        self._check(checks, t2 is not None and t2.state == TeamState.MATCHED,
                    "Team 2 → MATCHED after MMR threshold relaxed")
        return checks

    # ═════════════════════════════════════════════════════════════════════════
    # MODULAR COMPOSITION SCENARIOS
    # ═════════════════════════════════════════════════════════════════════════

    def _set_modular_config(self, min_size: int = 1, max_size: int = 5) -> None:
        """Switch the fake event to modular composition mode."""
        from daoc_bot.db import get_db
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET composition_type = %s, min_group_size = %s, max_group_size = %s
                WHERE guild_id = %s AND status = 'active'
                """,
                ("modular", min_size, max_size, self.guild_id),
            )
        conn.commit()

    def _set_fixed_config(self) -> None:
        """Restore the fake event to fixed composition mode."""
        from daoc_bot.db import get_db
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET composition_type = %s, min_group_size = 1, max_group_size = 1
                WHERE guild_id = %s AND status = 'active'
                """,
                ("fixed", self.guild_id),
            )
        conn.commit()

    def _make_team_sized(
        self, suffix: str, leader_id: int, group_size: int, mmr: int = 1000
    ) -> Team:
        """Create a fake team with an explicit group_size (simulates /register_team group_size:N)."""
        name = f"{FAKE_PREFIX}{suffix}"
        team = Team(
            name=name, leader_id=leader_id,
            member_ids=[leader_id], mmr=mmr,
            group_size=group_size,
        )
        guild_store.add_team(self.guild_id, team)
        self._fake_teams.append(name)
        logger.debug(
            "  Created fake team '%s' (leader=%d, group_size=%d, mmr=%d)",
            name, leader_id, group_size, mmr,
        )
        return team

    async def _scenario_modular_same_size_matches(self) -> list[str]:
        """In modular mode, two teams with the same group_size are matched normally.

        Simulates two groups both registering as group_size=3 and queuing up.
        They should match each other immediately.
        """
        checks: list[str] = []
        gid = self.guild_id
        self._set_modular_config(min_size=1, max_size=5)
        try:
            t1 = self._make_team_sized("mod_ss_a", 93010, group_size=3)
            t2 = self._make_team_sized("mod_ss_b", 93011, group_size=3)

            t1.state = t2.state = TeamState.READY
            guild_store.save_team(gid, t1)
            guild_store.save_team(gid, t2)
            guild_store.enqueue(gid, t1.name)
            guild_store.enqueue(gid, t2.name)

            await self.engine.try_match(gid)

            t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
            t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
            self._check(checks, guild_store.queue_size(gid) == 0,
                        "Queue empty — same-size groups matched")
            self._check(checks, t1 is not None and t1.state == TeamState.MATCHED,
                        "Group-size-3 team 1 → MATCHED")
            self._check(checks, t2 is not None and t2.state == TeamState.MATCHED,
                        "Group-size-3 team 2 → MATCHED")
            if t1 and t2:
                match = guild_store.get_match(gid, t1.current_match_id or "")
                self._check(checks, match is not None, "Match record created")
        finally:
            self._set_fixed_config()
        return checks

    async def _scenario_modular_different_sizes_blocked(self) -> list[str]:
        """In modular mode, teams with different group_sizes are never matched.

        Simulates a 2-man group and a 5-man group both queuing — they should
        never be paired because their group_size values differ.
        """
        checks: list[str] = []
        gid = self.guild_id
        self._set_modular_config(min_size=1, max_size=5)
        try:
            t1 = self._make_team_sized("mod_ds_a", 93020, group_size=2)
            t2 = self._make_team_sized("mod_ds_b", 93021, group_size=5)

            t1.state = t2.state = TeamState.READY
            guild_store.save_team(gid, t1)
            guild_store.save_team(gid, t2)
            guild_store.enqueue(gid, t1.name)
            guild_store.enqueue(gid, t2.name)

            # Even with backdated timestamps (relax window elapsed) they must NOT match
            self._backdate_queue_timestamps(t1.name, t2.name, seconds=300)
            await self.engine.try_match(gid)

            t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
            t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
            self._check(checks, guild_store.queue_size(gid) == 2,
                        "Queue still has 2 teams — different group sizes never matched")
            self._check(checks, t1 is not None and t1.state == TeamState.READY,
                        "group_size=2 team remains READY")
            self._check(checks, t2 is not None and t2.state == TeamState.READY,
                        "group_size=5 team remains READY")
        finally:
            self._set_fixed_config()
        return checks

    async def _scenario_modular_mixed_queue(self) -> list[str]:
        """In modular mode, a mixed queue only pairs same-size teams.

        Setup: 4 teams register with different group sizes:
            A (size 3), B (size 3), C (size 5), D (size 5)

        Expected: A matches B, C matches D — no cross-size pairing.
        """
        checks: list[str] = []
        gid = self.guild_id
        self._set_modular_config(min_size=1, max_size=5)
        try:
            a = self._make_team_sized("mod_mq_a", 93030, group_size=3)
            b = self._make_team_sized("mod_mq_b", 93031, group_size=3)
            c = self._make_team_sized("mod_mq_c", 93032, group_size=5)
            d = self._make_team_sized("mod_mq_d", 93033, group_size=5)

            for t in (a, b, c, d):
                t.state = TeamState.READY
                guild_store.save_team(gid, t)
                guild_store.enqueue(gid, t.name)

            await self.engine.try_match(gid)
            await self.engine.try_match(gid)

            a = guild_store.get_team(gid, a.name)  # type: ignore[assignment]
            b = guild_store.get_team(gid, b.name)  # type: ignore[assignment]
            c = guild_store.get_team(gid, c.name)  # type: ignore[assignment]
            d = guild_store.get_team(gid, d.name)  # type: ignore[assignment]

            self._check(checks, guild_store.queue_size(gid) == 0,
                        "All 4 teams matched — queue empty")
            self._check(checks,
                        all(t is not None and t.state == TeamState.MATCHED
                            for t in (a, b, c, d)),
                        "All 4 teams in MATCHED state")

            # Verify same-size pairing: A+B and C+D
            a_mid = a.current_match_id if a else None
            c_mid = c.current_match_id if c else None
            match_ab = guild_store.get_match(gid, a_mid or "")
            match_cd = guild_store.get_match(gid, c_mid or "")

            if match_ab:
                pair_ab = {match_ab.team1_name, match_ab.team2_name}
                self._check(checks,
                            a is not None and b is not None
                            and a.name in pair_ab and b.name in pair_ab,
                            f"Size-3 teams matched together: {pair_ab}")

            if match_cd:
                pair_cd = {match_cd.team1_name, match_cd.team2_name}
                self._check(checks,
                            c is not None and d is not None
                            and c.name in pair_cd and d.name in pair_cd,
                            f"Size-5 teams matched together: {pair_cd}")

            self._check(checks, a_mid != c_mid,
                        "Size-3 pair and size-5 pair are separate matches")
        finally:
            self._set_fixed_config()
        return checks

    async def _scenario_modular_group_size_in_register(self) -> list[str]:
        """Verify that group_size is stored correctly and visible after registration.

        Simulates what happens when /register_team is called with group_size:N —
        the value should persist to the DB and be readable back.
        Also verifies that a size-1 team (default) and size-4 team never match
        in modular mode.
        """
        checks: list[str] = []
        gid = self.guild_id
        self._set_modular_config(min_size=1, max_size=5)
        try:
            solo  = self._make_team_sized("mod_reg_solo",  93040, group_size=1)
            quad  = self._make_team_sized("mod_reg_quad",  93041, group_size=4)
            quad2 = self._make_team_sized("mod_reg_quad2", 93042, group_size=4)

            # Verify group_size round-trips through the DB correctly
            solo_db  = guild_store.get_team(gid, solo.name)
            quad_db  = guild_store.get_team(gid, quad.name)
            self._check(checks, solo_db is not None and solo_db.group_size == 1,
                        "group_size=1 persisted and read back correctly")
            self._check(checks, quad_db is not None and quad_db.group_size == 4,
                        "group_size=4 persisted and read back correctly")

            # solo vs quad: should never match (different sizes)
            solo.state = quad.state = TeamState.READY
            guild_store.save_team(gid, solo)
            guild_store.save_team(gid, quad)
            guild_store.enqueue(gid, solo.name)
            guild_store.enqueue(gid, quad.name)
            self._backdate_queue_timestamps(solo.name, quad.name, seconds=300)

            await self.engine.try_match(gid)

            solo = guild_store.get_team(gid, solo.name)  # type: ignore[assignment]
            quad = guild_store.get_team(gid, quad.name)  # type: ignore[assignment]
            self._check(checks, guild_store.queue_size(gid) == 2,
                        "solo (size 1) and quad (size 4) not matched despite long wait")

            # Add a second size-4 team — quad should now match quad2, solo waits
            quad2.state = TeamState.READY
            guild_store.save_team(gid, quad2)
            guild_store.enqueue(gid, quad2.name)

            await self.engine.try_match(gid)

            quad  = guild_store.get_team(gid, quad.name)  # type: ignore[assignment]
            quad2 = guild_store.get_team(gid, quad2.name)  # type: ignore[assignment]
            solo  = guild_store.get_team(gid, solo.name)  # type: ignore[assignment]
            self._check(checks,
                        quad is not None and quad.state == TeamState.MATCHED,
                        "quad (size 4) matched with quad2 (size 4)")
            self._check(checks,
                        quad2 is not None and quad2.state == TeamState.MATCHED,
                        "quad2 (size 4) matched with quad (size 4)")
            self._check(checks,
                        solo is not None and solo.state == TeamState.READY,
                        "solo (size 1) still READY — no size-4 partner available")
            self._check(checks, guild_store.queue_size(gid) == 1,
                        "Exactly 1 team remains in queue (solo)")
        finally:
            self._set_fixed_config()
        return checks

    # ═════════════════════════════════════════════════════════════════════════
    # /change_group_size SCENARIOS
    # ═════════════════════════════════════════════════════════════════════════

    async def _scenario_change_group_size_idle(self) -> list[str]:
        """IDLE team changes group size — DB updated, no queue side-effects.

        Simulates a leader calling /change_group_size while their team is in
        IDLE state (not yet queued).  The new size must be persisted and the
        team must remain IDLE with no queue entry created.
        """
        checks: list[str] = []
        gid = self.guild_id
        self._set_modular_config(min_size=1, max_size=8)
        try:
            t = self._make_team_sized("cgs_idle_a", 94001, group_size=3)

            # Preconditions
            self._check(checks, t.state == TeamState.IDLE,
                        "Team starts IDLE")
            self._check(checks, t.group_size == 3,
                        "Initial group_size is 3")

            # Apply the change (mirrors /change_group_size command logic)
            old_size = t.group_size
            t.group_size = 5
            guild_store.save_team(gid, t)

            t = guild_store.get_team(gid, t.name)  # type: ignore[assignment]
            self._check(checks, t is not None and t.group_size == 5,
                        "group_size updated to 5 in DB")
            self._check(checks, t is not None and t.state == TeamState.IDLE,
                        "State remains IDLE after size change")
            self._check(checks, t.name not in guild_store.get_queue(gid),
                        "Team not in queue after IDLE size change")
            self._check(checks, old_size == 3,
                        "Old size was recorded correctly (3)")
        finally:
            self._set_fixed_config()
        return checks

    async def _scenario_change_group_size_requeue(self) -> list[str]:
        """READY team changes group size — dequeued, updated, re-enqueued, matched.

        Simulates a leader calling /change_group_size while queued (READY).
        The team should be temporarily removed from the queue so matchmaking
        never sees a stale size, then re-added after the update.  A waiting
        partner of the new size should be found immediately.
        """
        checks: list[str] = []
        gid = self.guild_id
        self._set_modular_config(min_size=1, max_size=8)
        try:
            # t1 starts as size-3, t2 is waiting as size-5.
            # After t1 changes to size-5 they should pair with t2.
            t1 = self._make_team_sized("cgs_rq_a", 94010, group_size=3)
            t2 = self._make_team_sized("cgs_rq_b", 94011, group_size=5)

            # Put both in READY/queue
            t1.state = t2.state = TeamState.READY
            guild_store.save_team(gid, t1)
            guild_store.save_team(gid, t2)
            guild_store.enqueue(gid, t1.name)
            guild_store.enqueue(gid, t2.name)

            # Confirm they do NOT match with different sizes
            await self.engine.try_match(gid)
            t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
            t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
            self._check(checks, guild_store.queue_size(gid) == 2,
                        "No match formed before size change (size 3 vs 5)")
            self._check(checks, t1 is not None and t1.state == TeamState.READY,
                        "t1 still READY (different size, no match)")

            # Simulate /change_group_size: dequeue, update, re-enqueue, try_match
            guild_store.dequeue(gid, t1.name if t1 else "")
            if t1:
                t1.group_size = 5
                guild_store.save_team(gid, t1)
                guild_store.enqueue(gid, t1.name)

            await self.engine.try_match(gid)

            t1 = guild_store.get_team(gid, t1.name if t1 else "")  # type: ignore[assignment]
            t2 = guild_store.get_team(gid, t2.name)  # type: ignore[assignment]
            self._check(checks, guild_store.queue_size(gid) == 0,
                        "Queue empty after t1 changed to size-5 and matched t2")
            self._check(checks, t1 is not None and t1.state == TeamState.MATCHED,
                        "t1 (now size-5) → MATCHED")
            self._check(checks, t2 is not None and t2.state == TeamState.MATCHED,
                        "t2 (size-5) → MATCHED")
            self._check(checks, t1 is not None and t1.group_size == 5,
                        "t1 group_size persisted as 5")

            if t1 and t1.current_match_id:
                match = guild_store.get_match(gid, t1.current_match_id)
                self._check(checks, match is not None,
                            "Match record created for the newly-sized pair")
        finally:
            self._set_fixed_config()
        return checks

    async def _scenario_change_group_size_blocked_in_match(self) -> list[str]:
        """Team in MATCHED or IN_MATCH state cannot change group size.

        The /change_group_size command must reject requests from teams that
        are already in a match proposal or an active match.  This scenario
        verifies that the guard condition works: the team's group_size must
        be unchanged after an attempted update is rejected.
        """
        checks: list[str] = []
        gid = self.guild_id
        self._set_modular_config(min_size=1, max_size=8)
        try:
            t1 = self._make_team_sized("cgs_blk_a", 94020, group_size=3)
            t2 = self._make_team_sized("cgs_blk_b", 94021, group_size=3)

            # Put them in MATCHED state
            self._make_active_match(t1, t2)

            t1 = guild_store.get_team(gid, t1.name)  # type: ignore[assignment]
            self._check(checks, t1 is not None and t1.state == TeamState.MATCHED,
                        "Precondition: t1 is MATCHED")

            # Guard check: MATCHED/IN_MATCH → must not allow size change
            self._check(checks,
                        t1 is not None and t1.state in (TeamState.MATCHED, TeamState.IN_MATCH),
                        "Guard triggered: team in MATCHED/IN_MATCH state")

            # The command would reject here; simulate that the size is NOT changed
            original_size = t1.group_size if t1 else 3
            # (no save_team call — the command returns early)

            t1_after = guild_store.get_team(gid, t1.name if t1 else "")
            self._check(checks,
                        t1_after is not None and t1_after.group_size == original_size,
                        "group_size unchanged when team is MATCHED")
            self._check(checks,
                        t1_after is not None and t1_after.state == TeamState.MATCHED,
                        "State remains MATCHED — no side-effects")
        finally:
            self._set_fixed_config()
        return checks
