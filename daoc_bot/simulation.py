"""In-process simulation suite for the DAoC matchmaking bot.

Runs a series of scenarios against the real engine and store using fake teams,
without any Discord gateway connection required for the logic itself.  A
Discord channel is accepted so the simulation can post a live progress embed
that updates as each scenario completes.

Invoked from the ``/run_tests`` admin slash command.

All fake teams are cleaned up after the suite finishes (or if it crashes),
so running this on a live server during an event is safe — it will not
interfere with real teams as long as none of the fake names clash.

Usage (from commands.py)::

    from daoc_bot.simulation import SimulationSuite
    suite = SimulationSuite(channel)
    await suite.run()
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import discord

from daoc_bot.engine import MatchmakingEngine, MMR_MATCH_THRESHOLD, _elo_update
from daoc_bot.models import Match, Team, TeamState
from daoc_bot.state import BotState, store as global_store

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

FAKE_PREFIX = "__sim__"

COLOUR_RUNNING = discord.Color.yellow()
COLOUR_PASS    = discord.Color.green()
COLOUR_FAIL    = discord.Color.red()

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
        store: Optional[BotState] = None,
    ) -> None:
        self.channel = channel
        self.engine  = engine
        # When no custom store is provided the suite uses (and mutates) the
        # live global store, which is intentional for /run_tests on a live bot.
        # Fake teams all share the FAKE_PREFIX so they never clash with real ones.
        self.store   = store or global_store
        self._results: list[ScenarioResult] = []
        self._progress_msg: Optional[discord.Message] = None
        self._fake_teams: list[str] = []

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self) -> list[ScenarioResult]:
        """Execute all scenarios and return the result list."""
        logger.info("=" * 60)
        logger.info("SIMULATION SUITE STARTING")
        logger.info("=" * 60)

        self._progress_msg = await self.channel.send(
            embed=self._build_embed(running=True)
        )

        scenarios: list[Callable[[], Awaitable[list[str]]]] = [
            # ── Original scenarios ─────────────────────────────────────────
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
            # ── MMR / result-reporting scenarios ──────────────────────────
            self._scenario_mmr_elo_math,
            self._scenario_mmr_win_updates_ratings,
            self._scenario_mmr_loss_updates_ratings,
            self._scenario_mmr_no_result_leaves_ratings,
            self._scenario_mmr_best_pair_selection,
            self._scenario_mmr_threshold_blocks_far_teams,
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

    async def _run_scenario(self, fn: Callable[[], Awaitable[list[str]]]) -> ScenarioResult:
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

    # ── Fake team factory ─────────────────────────────────────────────────────

    def _make_team(self, suffix: str, leader_id: int, mmr: int = 1000) -> Team:
        name = f"{FAKE_PREFIX}{suffix}"
        team = Team(name=name, leader_id=leader_id, member_ids=[leader_id], mmr=mmr)
        self.store.add_team(team)
        self._fake_teams.append(name)
        logger.debug("  Created fake team '%s' (leader=%d, mmr=%d)", name, leader_id, mmr)
        return team

    def _make_active_match(self, t1: Team, t2: Team) -> Match:
        """Helper: create and store a match already in MATCHED state."""
        match_id = str(uuid.uuid4())[:8].upper()
        match = Match(id=match_id, team1_name=t1.name, team2_name=t2.name)
        self.store.add_match(match)
        t1.state = t2.state = TeamState.MATCHED
        t1.current_match_id = t2.current_match_id = match_id
        return match

    def _make_live_match(self, t1: Team, t2: Team) -> Match:
        """Helper: create a match already in IN_MATCH / active state."""
        match = self._make_active_match(t1, t2)
        match.active = True
        t1.state = t2.state = TeamState.IN_MATCH
        return match

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def _purge_fake_state(self) -> None:
        """Remove all fake teams/matches from the store before each scenario."""
        for name in list(self._fake_teams):
            team = self.store.get_team(name)
            if team:
                if team.current_match_id:
                    match = self.store.get_match(team.current_match_id)
                    if match:
                        for tname in (match.team1_name, match.team2_name):
                            t = self.store.get_team(tname)
                            if t:
                                t.state = TeamState.IDLE
                                t.current_match_id = None
                        self.store.remove_match(match.id)
                self.store.dequeue(name)
                self.store.remove_team(name)
        self._fake_teams.clear()

    async def _cleanup(self) -> None:
        """Remove all fake teams and matches created during the run."""
        logger.info("")
        logger.info("─── CLEANUP ───")
        removed = 0
        for name in list(self._fake_teams):
            team = self.store.get_team(name)
            if team:
                if team.current_match_id:
                    match = self.store.get_match(team.current_match_id)
                    if match:
                        for tname in (match.team1_name, match.team2_name):
                            t = self.store.get_team(tname)
                            if t:
                                t.state = TeamState.IDLE
                                t.current_match_id = None
                        self.store.remove_match(match.id)
                self.store.dequeue(name)
                self.store.remove_team(name)
                removed += 1
        logger.info("  Removed %d fake team(s).", removed)
        self._fake_teams.clear()

    # ═════════════════════════════════════════════════════════════════════════
    # ORIGINAL SCENARIOS (unchanged)
    # ═════════════════════════════════════════════════════════════════════════

    async def _scenario_registration(self) -> list[str]:
        """Register a team, verify store state, then remove it."""
        checks: list[str] = []
        t = self._make_team("reg_a", 90001)

        self._check(checks, self.store.team_exists(t.name),
                    "Team appears in store after registration")
        self._check(checks, self.store.get_team_by_leader(90001) is t,
                    "Leader index resolves to correct team")
        self._check(checks, t.state == TeamState.IDLE,
                    "Initial state is IDLE")
        self._check(checks, self.store.is_leader(90001),
                    "is_leader() returns True for registered leader")

        self.store.remove_team(t.name)
        self._fake_teams.remove(t.name)
        self._check(checks, not self.store.team_exists(t.name),
                    "Team absent from store after removal")
        self._check(checks, not self.store.is_leader(90001),
                    "Leader index cleaned up after removal")
        return checks

    async def _scenario_ready_unready(self) -> list[str]:
        """Queue and dequeue a team; verify queue state at each step."""
        checks: list[str] = []
        t = self._make_team("rdy_a", 90010)

        t.state = TeamState.READY
        self.store.enqueue(t.name)
        self._check(checks, t.name in self.store.queue, "Team in queue after enqueue")
        self._check(checks, self.store.queue_size == 1,  "Queue size is 1")

        self.store.enqueue(t.name)
        self._check(checks, self.store.queue_size == 1,
                    "Double-enqueue is idempotent (queue size still 1)")

        self.store.dequeue(t.name)
        t.state = TeamState.IDLE
        self._check(checks, t.name not in self.store.queue, "Team removed from queue after dequeue")
        self._check(checks, self.store.queue_size == 0,     "Queue empty after dequeue")
        return checks

    async def _scenario_happy_path(self) -> list[str]:
        """Full cycle: ready → match found → both accept → winner reported → IDLE."""
        checks: list[str] = []
        t1 = self._make_team("hp_a", 90020)
        t2 = self._make_team("hp_b", 90021)

        t1.state = t2.state = TeamState.READY
        self.store.enqueue(t1.name)
        self.store.enqueue(t2.name)

        await self.engine.try_match()

        self._check(checks, self.store.queue_size == 0,      "Queue empty after match formed")
        self._check(checks, t1.state == TeamState.MATCHED,   "Team 1 → MATCHED")
        self._check(checks, t2.state == TeamState.MATCHED,   "Team 2 → MATCHED")
        self._check(checks, t1.current_match_id is not None, "Team 1 has match ID")
        match = self.store.get_match(t1.current_match_id or "")
        self._check(checks, match is not None, "Match stored")
        if match is None:
            return checks

        await self.engine.accept_match(match, t1.name)
        self._check(checks, match.team1_accepted,  "Team 1 accepted")
        self._check(checks, not match.active,      "Match not yet active (only 1 accepted)")

        with _patch_store(self.store):
            await self.engine.accept_match(match, t2.name)
        self._check(checks, match.active,                    "Match active after both accepted")
        self._check(checks, t1.state == TeamState.IN_MATCH,  "Team 1 → IN_MATCH")
        self._check(checks, t2.state == TeamState.IN_MATCH,  "Team 2 → IN_MATCH")

        # Report result — team 1 wins
        match.active = True
        with _patch_store(self.store):
            await self.engine.end_match(match, ended_by=t1.name, winner_name=t1.name)

        self._check(checks, t1.state == TeamState.IDLE,  "Team 1 → IDLE after match ends")
        self._check(checks, t2.state == TeamState.IDLE,  "Team 2 → IDLE after match ends")
        self._check(checks, t1.wins == 1,                "Winner win count incremented")
        self._check(checks, t2.losses == 1,              "Loser loss count incremented")
        self._check(checks, t1.mmr > 1000,               "Winner MMR increased above start")
        self._check(checks, t2.mmr < 1000,               "Loser MMR decreased below start")
        self._check(checks, t1.last_opponent == t2.name, "Rematch guard set on team 1")
        self._check(checks, t2.last_opponent == t1.name, "Rematch guard set on team 2")
        self._check(checks, self.store.get_match(match.id) is None, "Match removed from store")
        return checks

    async def _scenario_partial_accept(self) -> list[str]:
        """Only one team accepts — verify the other's state is unchanged."""
        checks: list[str] = []
        t1 = self._make_team("pa_a", 90030)
        t2 = self._make_team("pa_b", 90031)
        match = self._make_active_match(t1, t2)

        await self.engine.accept_match(match, t1.name)

        self._check(checks, match.team1_accepted,           "Team 1 accepted")
        self._check(checks, not match.team2_accepted,       "Team 2 not yet accepted")
        self._check(checks, not match.active,               "Match not active after partial accept")
        self._check(checks, t1.state == TeamState.MATCHED,  "Team 1 still MATCHED (not IN_MATCH)")
        self._check(checks, t2.state == TeamState.MATCHED,  "Team 2 still MATCHED")
        return checks

    async def _scenario_decline(self) -> list[str]:
        """One leader declines — both teams reset to IDLE, no rematch guard."""
        checks: list[str] = []
        t1 = self._make_team("dec_a", 90040)
        t2 = self._make_team("dec_b", 90041)
        match = self._make_active_match(t1, t2)

        await self.engine.cancel_match(match, reason="declined by test")

        self._check(checks, t1.state == TeamState.IDLE,
                    "Team 1 → IDLE after decline")
        self._check(checks, t2.state == TeamState.IDLE,
                    "Team 2 → IDLE after decline")
        self._check(checks, self.store.get_match(match.id) is None, "Match removed")
        self._check(checks, t1.last_opponent is None, "No rematch guard on decline")
        self._check(checks, t2.last_opponent is None, "No rematch guard on decline")
        return checks

    async def _scenario_timeout(self) -> list[str]:
        """Simulate acceptance timeout — same behaviour as decline."""
        checks: list[str] = []
        t1 = self._make_team("to_a", 90050)
        t2 = self._make_team("to_b", 90051)
        match = self._make_active_match(t1, t2)

        await self.engine.accept_match(match, t1.name)
        await self.engine.cancel_match(match, reason="acceptance timeout (2 min)")

        self._check(checks, t1.state == TeamState.IDLE,
                    "Team 1 → IDLE after timeout")
        self._check(checks, t2.state == TeamState.IDLE,
                    "Team 2 → IDLE after timeout")
        self._check(checks, self.store.get_match(match.id) is None,
                    "Match removed after timeout")
        self._check(checks, t1.last_opponent is None, "No guard after timeout")
        return checks

    async def _scenario_rematch_guard_two_teams(self) -> list[str]:
        """With only 2 teams and both blocked, guard auto-lifts and they match."""
        checks: list[str] = []
        t1 = self._make_team("rg2_a", 90060)
        t2 = self._make_team("rg2_b", 90061)

        t1.last_opponent = t2.name
        t2.last_opponent = t1.name
        t1.state = t2.state = TeamState.READY
        self.store.enqueue(t1.name)
        self.store.enqueue(t2.name)

        await self.engine.try_match()

        self._check(checks, t1.last_opponent is None, "Guard lifted on team 1")
        self._check(checks, t2.last_opponent is None, "Guard lifted on team 2")
        self._check(checks, t1.state == TeamState.MATCHED,
                    "Teams matched after guard lifted")
        self._check(checks, self.store.queue_size == 0, "Queue empty")
        return checks

    async def _scenario_rematch_guard_three_teams(self) -> list[str]:
        """Blocked pair is skipped; third team is matched with one of them."""
        checks: list[str] = []
        t1 = self._make_team("rg3_a", 90070)
        t2 = self._make_team("rg3_b", 90071)
        t3 = self._make_team("rg3_c", 90072)

        t1.last_opponent = t2.name
        t2.last_opponent = t1.name
        t1.state = t2.state = t3.state = TeamState.READY
        for t in (t1, t2, t3):
            self.store.enqueue(t.name)

        await self.engine.try_match()

        matched = self.store.get_match(
            t1.current_match_id or t2.current_match_id or t3.current_match_id or ""
        )
        self._check(checks, matched is not None, "A match was formed")
        if matched:
            pair = {matched.team1_name, matched.team2_name}
            self._check(checks,
                        not (t1.name in pair and t2.name in pair),
                        f"Blocked pair not matched — got {pair}")
            self._check(checks, t3.name in pair, "Third team is part of the match")
        self._check(checks, self.store.queue_size == 1,
                    "One team remains in queue (unmatched)")
        return checks

    async def _scenario_parallel_matches(self) -> list[str]:
        """4 teams produce 2 simultaneous matches; both end independently."""
        checks: list[str] = []
        teams = [self._make_team(f"par_{i}", 90080 + i) for i in range(4)]
        for t in teams:
            t.state = TeamState.READY
            self.store.enqueue(t.name)

        await self.engine.try_match()
        self._check(checks, self.store.queue_size == 2, "First match formed, 2 teams remain")

        await self.engine.try_match()
        self._check(checks, self.store.queue_size == 0, "Second match formed, queue empty")

        active_matches = [
            self.store.get_match(t.current_match_id)
            for t in teams if t.current_match_id
        ]
        unique_matches = {m.id for m in active_matches if m}
        self._check(checks, len(unique_matches) == 2, "Two distinct matches in store")

        for match in list(self.store._matches.values()):
            if match.team1_name.startswith(FAKE_PREFIX):
                await self.engine.accept_match(match, match.team1_name)
                await self.engine.accept_match(match, match.team2_name)

        self._check(checks,
                    all(t.state == TeamState.IN_MATCH for t in teams),
                    "All 4 teams IN_MATCH simultaneously")

        for match in list(self.store._matches.values()):
            if match.team1_name.startswith(FAKE_PREFIX):
                match.active = True
                with _patch_store(self.store):
                    await self.engine.end_match(match, ended_by=match.team1_name)

        self._check(checks,
                    all(t.state == TeamState.IDLE for t in teams),
                    "All 4 teams back to IDLE after matches end")
        self._check(checks,
                    len(self.store.active_matches()) == 0,
                    "No active matches remaining")
        return checks

    async def _scenario_odd_queue(self) -> list[str]:
        """3 teams in queue: 2 match, 1 waits in READY state."""
        checks: list[str] = []
        teams = [self._make_team(f"odd_{i}", 90090 + i) for i in range(3)]
        for t in teams:
            t.state = TeamState.READY
            self.store.enqueue(t.name)

        await self.engine.try_match()

        self._check(checks, self.store.queue_size == 1, "One team still waiting in queue")
        waiting = [t for t in teams if t.state == TeamState.READY]
        self._check(checks, len(waiting) == 1, "Exactly one team in READY state")
        matched = [t for t in teams if t.state == TeamState.MATCHED]
        self._check(checks, len(matched) == 2, "Two teams in MATCHED state")
        return checks

    async def _scenario_rapid_ready(self) -> list[str]:
        """Simulate 6 teams clicking Ready concurrently."""
        checks: list[str] = []
        teams = [self._make_team(f"rr_{i}", 90100 + i) for i in range(6)]

        async def ready_up(t: Team) -> None:
            t.state = TeamState.READY
            self.store.enqueue(t.name)
            await self.engine.try_match()

        await asyncio.gather(*[ready_up(t) for t in teams])

        self._check(checks, self.store.queue_size == 0,
                    "Queue empty — all 6 teams matched into 3 pairs")
        matched = [t for t in teams if t.state == TeamState.MATCHED]
        self._check(checks, len(matched) == 6, "All 6 teams in MATCHED state")
        unique_ids = {t.current_match_id for t in teams}
        self._check(checks, len(unique_ids) == 3,
                    "3 distinct match IDs — no duplicate pairings")
        return checks

    async def _scenario_double_match_ended(self) -> list[str]:
        """Both leaders click Match Ended simultaneously — only one closure."""
        checks: list[str] = []
        t1 = self._make_team("dme_a", 90110)
        t2 = self._make_team("dme_b", 90111)
        match = self._make_active_match(t1, t2)
        match.active = True
        t1.state = t2.state = TeamState.IN_MATCH

        async def end_by(team_name: str) -> None:
            m = self.store.get_match(match.id)
            if m and m.active:
                m.active = False
                with _patch_store(self.store):
                    await self.engine.end_match(m, ended_by=team_name)

        await asyncio.gather(end_by(t1.name), end_by(t2.name))

        self._check(checks, t1.state == TeamState.IDLE, "Team 1 → IDLE")
        self._check(checks, t2.state == TeamState.IDLE, "Team 2 → IDLE")
        self._check(checks, self.store.get_match(match.id) is None,
                    "Match removed — no double-processing")
        return checks

    async def _scenario_20_teams(self) -> list[str]:
        """Stress test: 20 teams register, all ready up, matches form in waves."""
        checks: list[str] = []
        count     = 20
        teams     = [self._make_team(f"stress_{i:02d}", 91000 + i) for i in range(count)]
        wave_size = 4

        total_matched = 0

        for wave_start in range(0, count, wave_size):
            wave = teams[wave_start:wave_start + wave_size]
            for t in wave:
                t.state = TeamState.READY
                self.store.enqueue(t.name)

            for _ in range(wave_size // 2):
                await self.engine.try_match()

            newly_matched = [t for t in wave if t.state == TeamState.MATCHED]
            total_matched += len(newly_matched)
            logger.info("  Wave %d–%d: %d/%d matched",
                        wave_start, wave_start + wave_size - 1,
                        len(newly_matched), wave_size)

        self._check(checks, total_matched == count,
                    f"All {count} teams matched across waves")
        self._check(checks, self.store.queue_size == 0,
                    "Queue empty after all waves")
        self._check(checks, len(list(self.store._matches.values())) == count // 2,
                    f"{count // 2} matches in store")

        for match in list(self.store._matches.values()):
            if match.team1_name.startswith(FAKE_PREFIX):
                await self.engine.accept_match(match, match.team1_name)
                await self.engine.accept_match(match, match.team2_name)

        self._check(checks,
                    all(t.state == TeamState.IN_MATCH for t in teams),
                    f"All {count} teams IN_MATCH simultaneously")

        for match in list(self.store._matches.values()):
            if match.team1_name.startswith(FAKE_PREFIX):
                match.active = True
                with _patch_store(self.store):
                    await self.engine.end_match(match, ended_by=match.team1_name)

        self._check(checks,
                    all(t.state == TeamState.IDLE for t in teams),
                    f"All {count} teams back to IDLE")
        self._check(checks,
                    len(self.store.active_matches()) == 0,
                    "No active matches remaining after stress test")
        return checks

    # ═════════════════════════════════════════════════════════════════════════
    # MMR / RESULT-REPORTING SCENARIOS
    # ═════════════════════════════════════════════════════════════════════════

    async def _scenario_mmr_elo_math(self) -> list[str]:
        """Verify _elo_update() arithmetic for equal, favoured, and underdog cases.

        No Discord or engine calls needed — this is a pure unit test of the
        ELO formula to catch any misconfiguration of K or the expected-score
        calculation before it affects live matches.
        """
        checks: list[str] = []

        # Equal opponents (1000 vs 1000): each side has 50% expected score
        # → winner should gain exactly K/2 = 16 points (with K=32)
        w, l = _elo_update(1000, 1000)
        self._check(checks, w > 1000 and l < 1000,
                    "Equal match: winner gains MMR, loser loses MMR")
        self._check(checks, w + l == 2000,
                    "Equal match: update is zero-sum (w + l == 2000)")
        delta_equal = w - 1000
        self._check(checks, 10 <= delta_equal <= 20,
                    f"Equal match: delta {delta_equal} in expected range 10–20")

        # Heavy favourite (1400 vs 1000): winner expected to win ~91%
        # → small gain (~3) for winner, small loss for loser
        w2, l2 = _elo_update(1400, 1000)
        self._check(checks, w2 > 1400,
                    "Favourite win: winner MMR increases")
        self._check(checks, w2 - 1400 < delta_equal,
                    "Favourite win: smaller delta than equal match (as expected)")
        self._check(checks, w2 + l2 == 2400,
                    "Favourite win: zero-sum preserved")

        # Underdog upset (1000 vs 1400): winner expected to win ~9%
        # → large gain for underdog winner
        w3, l3 = _elo_update(1000, 1400)
        self._check(checks, w3 > 1000,
                    "Underdog win: underdog MMR increases")
        self._check(checks, w3 - 1000 > delta_equal,
                    "Underdog win: larger delta than equal match")
        self._check(checks, w3 + l3 == 2400,
                    "Underdog win: zero-sum preserved")

        # Sanity: a win vs equal opponent is always worth more than a win
        # vs a heavy underdog and less than a win vs a heavy favourite.
        # (This confirms the formula direction is correct.)
        w_vs_underdog, _ = _elo_update(1400, 1000)   # already computed as w2
        w_vs_equal,    _ = _elo_update(1000, 1000)   # already computed as w
        w_vs_favourite,_ = _elo_update(1000, 1400)   # already computed as w3
        self._check(
            checks,
            (w_vs_underdog - 1400) < (w_vs_equal - 1000) < (w_vs_favourite - 1000),
            "Delta ordering: underdog win < equal win < favourite win (correct direction)",
        )
        return checks

    async def _scenario_mmr_win_updates_ratings(self) -> list[str]:
        """Winner reports via end_match — verify MMR, wins, losses all update."""
        checks: list[str] = []
        t1 = self._make_team("mmrw_a", 92010, mmr=1000)
        t2 = self._make_team("mmrw_b", 92011, mmr=1000)
        match = self._make_live_match(t1, t2)

        mmr1_before, mmr2_before = t1.mmr, t2.mmr

        with _patch_store(self.store):
            await self.engine.end_match(match, ended_by=t1.name, winner_name=t1.name)

        self._check(checks, t1.wins == 1,    "Winner: wins incremented to 1")
        self._check(checks, t1.losses == 0,  "Winner: losses unchanged (0)")
        self._check(checks, t2.wins == 0,    "Loser: wins unchanged (0)")
        self._check(checks, t2.losses == 1,  "Loser: losses incremented to 1")
        self._check(checks, t1.mmr > mmr1_before,
                    f"Winner MMR increased: {mmr1_before} → {t1.mmr}")
        self._check(checks, t2.mmr < mmr2_before,
                    f"Loser MMR decreased: {mmr2_before} → {t2.mmr}")
        delta = t1.mmr - mmr1_before
        self._check(checks, t1.mmr - mmr1_before == mmr2_before - t2.mmr,
                    f"Update is zero-sum: winner +{delta}, loser -{mmr2_before - t2.mmr}")
        self._check(checks, t1.state == TeamState.IDLE, "Winner reset to IDLE")
        self._check(checks, t2.state == TeamState.IDLE, "Loser reset to IDLE")
        return checks

    async def _scenario_mmr_loss_updates_ratings(self) -> list[str]:
        """Loser reports via We Lost — opponent (winner) gets the MMR gain."""
        checks: list[str] = []
        t1 = self._make_team("mmrl_a", 92020, mmr=1000)
        t2 = self._make_team("mmrl_b", 92021, mmr=1000)
        match = self._make_live_match(t1, t2)

        # t1 clicks "We Lost" → t2 should be the winner
        with _patch_store(self.store):
            await self.engine.end_match(match, ended_by=t1.name, winner_name=t2.name)

        self._check(checks, t2.wins == 1,   "Reported winner: wins incremented")
        self._check(checks, t1.losses == 1, "Reported loser: losses incremented")
        self._check(checks, t2.mmr > 1000,  "Reported winner MMR increased")
        self._check(checks, t1.mmr < 1000,  "Reported loser MMR decreased")
        self._check(checks, t1.state == TeamState.IDLE, "Team 1 reset to IDLE")
        self._check(checks, t2.state == TeamState.IDLE, "Team 2 reset to IDLE")
        return checks

    async def _scenario_mmr_no_result_leaves_ratings(self) -> list[str]:
        """Admin cancel / end_match with no winner — ratings must not change."""
        checks: list[str] = []
        t1 = self._make_team("mmrn_a", 92030, mmr=1050)
        t2 = self._make_team("mmrn_b", 92031, mmr=950)
        match = self._make_live_match(t1, t2)

        mmr1_before, mmr2_before = t1.mmr, t2.mmr

        with _patch_store(self.store):
            # winner_name omitted (defaults to None)
            await self.engine.end_match(match, ended_by=t1.name)

        self._check(checks, t1.mmr == mmr1_before,
                    "Team 1 MMR unchanged when no result reported")
        self._check(checks, t2.mmr == mmr2_before,
                    "Team 2 MMR unchanged when no result reported")
        self._check(checks, t1.wins == 0 and t1.losses == 0,
                    "Team 1 W/L record unchanged")
        self._check(checks, t2.wins == 0 and t2.losses == 0,
                    "Team 2 W/L record unchanged")
        return checks

    async def _scenario_mmr_best_pair_selection(self) -> list[str]:
        """3 teams queue: engine must pick the pair with the smallest MMR gap.

        Setup:
            Alpha  MMR 1000
            Beta   MMR 1020   ← gap from Alpha: 20  (smallest)
            Gamma  MMR 1200   ← gap from Alpha: 200, from Beta: 180

        Expected: Alpha vs Beta matched (gap 20), Gamma waits.
        """
        checks: list[str] = []
        alpha = self._make_team("mmrbp_alpha", 92040, mmr=1000)
        beta  = self._make_team("mmrbp_beta",  92041, mmr=1020)
        gamma = self._make_team("mmrbp_gamma", 92042, mmr=1200)

        for t in (alpha, beta, gamma):
            t.state = TeamState.READY
            self.store.enqueue(t.name)

        await self.engine.try_match()

        matched_id = alpha.current_match_id or beta.current_match_id
        match = self.store.get_match(matched_id or "")

        self._check(checks, match is not None, "A match was formed")
        if match:
            pair = {match.team1_name, match.team2_name}
            self._check(checks,
                        alpha.name in pair and beta.name in pair,
                        f"Closest MMR pair chosen (Alpha+Beta, gap 20) — got {pair}")
            self._check(checks,
                        gamma.name not in pair,
                        "Gamma (furthest) not matched first")

        self._check(checks, self.store.queue_size == 1,
                    "Exactly one team remains in queue")
        remaining = self.store.queue
        self._check(checks, gamma.name in remaining,
                    "Gamma is the team still waiting")
        return checks

    async def _scenario_mmr_threshold_blocks_far_teams(self) -> list[str]:
        """Teams with MMR gap > threshold do NOT match while both are freshly queued.

        We set team MMRs 500 apart (well above MMR_MATCH_THRESHOLD) and
        call try_match immediately — neither team has waited long enough for
        the relax window to open, so no match should form.

        We then simulate the relax window by manually back-dating the queue
        timestamps and calling try_match again — now they should match.
        """
        checks: list[str] = []
        t1 = self._make_team("mmrthr_a", 92050, mmr=1000)
        t2 = self._make_team("mmrthr_b", 92051, mmr=1000 + MMR_MATCH_THRESHOLD + 300)

        t1.state = t2.state = TeamState.READY
        self.store.enqueue(t1.name)
        self.store.enqueue(t2.name)

        # Both freshly queued → no match expected
        await self.engine.try_match()
        self._check(checks, self.store.queue_size == 2,
                    f"No match formed immediately (MMR gap {MMR_MATCH_THRESHOLD + 300} > threshold {MMR_MATCH_THRESHOLD})")
        self._check(checks, t1.state == TeamState.READY,
                    "Team 1 still READY after blocked match attempt")
        self._check(checks, t2.state == TeamState.READY,
                    "Team 2 still READY after blocked match attempt")

        # Simulate the relax window by back-dating both teams' queue timestamps
        from datetime import datetime, timezone, timedelta
        from daoc_bot.engine import MMR_RELAX_SECONDS
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=MMR_RELAX_SECONDS + 1)
        self.store._queue_timestamps[t1.name] = old_ts
        self.store._queue_timestamps[t2.name] = old_ts

        await self.engine.try_match()
        self._check(checks, self.store.queue_size == 0,
                    "Match formed after relax window elapsed")
        self._check(checks, t1.state == TeamState.MATCHED,
                    "Team 1 → MATCHED after MMR threshold relaxed")
        self._check(checks, t2.state == TeamState.MATCHED,
                    "Team 2 → MATCHED after MMR threshold relaxed")
        return checks


# ── Context manager for patching the store used by the engine ────────────────

class _patch_store:
    """Temporarily replace the module-level ``store`` in ``daoc_bot.engine``."""

    def __init__(self, custom_store: BotState) -> None:
        self._custom: BotState = custom_store
        self._original: BotState | None = None

    def __enter__(self) -> "_patch_store":
        import daoc_bot.engine as eng_mod
        self._original = eng_mod.store
        eng_mod.store  = self._custom
        return self

    def __exit__(self, *_: object) -> None:
        import daoc_bot.engine as eng_mod
        assert self._original is not None
        eng_mod.store = self._original