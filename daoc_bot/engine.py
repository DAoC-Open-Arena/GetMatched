"""Matchmaking engine — all core business logic lives here.

The engine is responsible for:

* Pairing teams from the per-guild ready queue, preferring close MMR matches
  and relaxing the MMR constraint after a configurable wait time.
* Editing each leader's private thread panel whenever state changes.
* Sending channel pings and broadcast embeds at key moments.
* Updating ELO ratings when a match result is reported.

All public methods accept ``guild_id`` as their first argument so a single
engine instance can drive multiple guilds concurrently.

Matchmaking algorithm
---------------------
When ``try_match`` is called the engine scans all valid pairs in the guild's
queue and picks the pair with the **smallest MMR difference** that is also
**eligible** under the current threshold:

* A pair is eligible if ``|mmr_a - mmr_b| <= cfg.mmr_match_threshold`` **or**
  at least one team has been waiting at least ``cfg.mmr_relax_seconds``.
* When ``cfg.mmr_enabled`` is ``False`` every non-rematch-blocked pair is
  eligible regardless of MMR.
* When ``cfg.composition_type`` is ``"modular"`` only teams with the same
  ``group_size`` are considered.

ELO ratings
-----------
Standard ELO with a configurable K-factor (default 32) and a starting rating
of 1000.  When ``cfg.mmr_enabled`` is ``False`` ELO updates are skipped
entirely.

Panel update strategy
---------------------
Each leader has a private thread in the guild's matchmaking channel created
at registration.  The bot edits a single message in that thread whenever
state changes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

import discord

from daoc_bot import embeds, event_log
from daoc_bot.guild_store import guild_store
from daoc_bot.models import Match, Team, TeamState

logger = logging.getLogger(__name__)


# ── ELO helper ────────────────────────────────────────────────────────────────

def _elo_update(winner_mmr: int, loser_mmr: int, k: int = 32) -> tuple[int, int]:
    """Return ``(new_winner_mmr, new_loser_mmr)`` after one match.

    Args:
        winner_mmr: Current MMR of the winning team.
        loser_mmr:  Current MMR of the losing team.
        k:          ELO K-factor (controls rating volatility).

    Returns:
        A ``(new_winner_mmr, new_loser_mmr)`` tuple of integers.
    """
    expected_winner = 1.0 / (1.0 + 10.0 ** ((loser_mmr - winner_mmr) / 400.0))
    delta = round(k * (1.0 - expected_winner))
    return winner_mmr + delta, loser_mmr - delta


# ── Engine ────────────────────────────────────────────────────────────────────

class MatchmakingEngine:
    """Orchestrates the full match lifecycle across multiple guilds."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        # Keyed by guild_id so each guild gets its own lock
        self._match_locks: dict[int, asyncio.Lock] = {}
        # Keyed by (guild_id, team_name)
        self._pending_timeouts: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._mmr_relax_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        """Return (or lazily create) the per-guild match lock."""
        if guild_id not in self._match_locks:
            self._match_locks[guild_id] = asyncio.Lock()
        return self._match_locks[guild_id]

    # ── Channel helpers ───────────────────────────────────────────────────────

    async def _get_matchmaking_channel(
        self, guild_id: int
    ) -> Optional[discord.TextChannel]:
        cfg = guild_store.get_event_config(guild_id)
        if cfg is None or cfg.matchmaking_channel_id == 0:
            logger.error(
                "Guild %d: matchmaking_channel_id not configured.", guild_id
            )
            return None
        channel = self.bot.get_channel(cfg.matchmaking_channel_id)
        if channel is None:
            logger.error(
                "Guild %d: matchmaking channel ID %d not found in cache.",
                guild_id, cfg.matchmaking_channel_id,
            )
        return channel  # type: ignore[return-value]

    async def _get_broadcast_channel(
        self, guild_id: int
    ) -> Optional[discord.TextChannel]:
        cfg = guild_store.get_event_config(guild_id)
        if cfg is None or cfg.broadcast_channel_id == 0:
            logger.error(
                "Guild %d: broadcast_channel_id not configured.", guild_id
            )
            return None
        channel = self.bot.get_channel(cfg.broadcast_channel_id)
        if channel is None:
            logger.error(
                "Guild %d: broadcast channel ID %d not found in cache.",
                guild_id, cfg.broadcast_channel_id,
            )
        return channel  # type: ignore[return-value]

    # ── Panel management ──────────────────────────────────────────────────────

    async def create_team_panel(
        self, guild_id: int, team: Team, leader: discord.Member
    ) -> None:
        """Create a private thread for the leader and post the initial panel."""
        ch = await self._get_matchmaking_channel(guild_id)
        if not ch:
            logger.error(
                "Guild %d: matchmaking channel not found — cannot create panel thread.",
                guild_id,
            )
            return

        thread = await ch.create_thread(
            name=f"Control Room — {team.name}",
            type=discord.ChannelType.private_thread,
            reason=f"Matchmaking panel for team {team.name}",
        )
        await thread.add_user(leader)

        from daoc_bot.views.team_panel import view_for_state

        msg = await thread.send(
            embed=embeds.team_panel(team),
            view=view_for_state(team, self, guild_id),
        )
        team.panel_thread_id = thread.id
        team.panel_message_id = msg.id
        guild_store.save_team(guild_id, team)
        logger.info(
            "Guild %d: panel thread created for team '%s' (thread=%d)",
            guild_id, team.name, thread.id,
        )

    async def update_team_panel(self, guild_id: int, team: Team) -> None:
        """Edit the panel message in the leader's private thread."""
        if not team.panel_thread_id or not team.panel_message_id:
            return

        from daoc_bot.views.team_panel import view_for_state

        thread = self.bot.get_channel(team.panel_thread_id)
        if not thread:
            try:
                thread = await self.bot.fetch_channel(team.panel_thread_id)
            except discord.NotFound:
                logger.warning(
                    "Guild %d: panel thread for '%s' not found.", guild_id, team.name
                )
                return

        try:
            assert isinstance(thread, discord.Thread)
            msg = await thread.fetch_message(team.panel_message_id)
            await msg.edit(
                embed=embeds.team_panel(team),
                view=view_for_state(team, self, guild_id),
            )
        except discord.NotFound:
            logger.warning(
                "Guild %d: panel message for '%s' not found.", guild_id, team.name
            )

    async def delete_team_panel(self, guild_id: int, team: Team) -> None:
        """Delete the leader's private thread when a team unregisters."""
        if not team.panel_thread_id:
            return

        thread = self.bot.get_channel(team.panel_thread_id)
        if not thread:
            try:
                thread = await self.bot.fetch_channel(team.panel_thread_id)
            except discord.NotFound:
                return
        try:
            assert isinstance(thread, discord.Thread)
            await thread.delete()
        except discord.NotFound:
            pass

    # ── MMR relaxation helpers ────────────────────────────────────────────────

    def schedule_mmr_relax(self, guild_id: int, team_name: str) -> None:
        """Start (or restart) the MMR-relaxation timer for *team_name*."""
        self._cancel_mmr_relax(guild_id, team_name)
        key = (guild_id, team_name)
        task: asyncio.Task[None] = asyncio.ensure_future(
            self._delayed_try_match(guild_id, team_name)
        )
        self._mmr_relax_tasks[key] = task

    def _cancel_mmr_relax(self, guild_id: int, team_name: str) -> None:
        """Cancel the MMR-relaxation timer for *team_name* if pending."""
        key = (guild_id, team_name)
        task = self._mmr_relax_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            logger.debug(
                "Guild %d: MMR relax timer cancelled for '%s'.", guild_id, team_name
            )

    async def _delayed_try_match(self, guild_id: int, team_name: str) -> None:
        """Fire :meth:`try_match` after the guild's relaxation window elapses."""
        cfg = guild_store.get_event_config(guild_id)
        relax_secs = cfg.mmr_relax_seconds if cfg else 120
        try:
            await asyncio.sleep(relax_secs)
        except asyncio.CancelledError:
            return
        self._mmr_relax_tasks.pop((guild_id, team_name), None)
        team = guild_store.get_team(guild_id, team_name)
        if team and team.state == TeamState.READY:
            logger.info(
                "Guild %d: MMR relaxation triggered for '%s' — retrying match.",
                guild_id, team_name,
            )
            await self.try_match(guild_id)

    # ── Queue management ──────────────────────────────────────────────────────

    async def try_match(self, guild_id: int) -> None:
        """Attempt to pair two teams from the guild's ready queue.

        Serialised by a per-guild lock so concurrent Ready clicks cannot
        produce duplicate pairings.
        """
        async with self._get_lock(guild_id):
            await self._try_match_locked(guild_id)

    async def _try_match_locked(self, guild_id: int) -> None:
        """Inner implementation — must only be called while holding the guild lock."""
        cfg = guild_store.get_event_config(guild_id)
        if cfg is None:
            return  # no active event

        if guild_store.queue_size(guild_id) < 2:
            return

        queue = guild_store.get_queue(guild_id)
        if cfg.composition_type == "modular":
            logger.debug(
                "Guild %d: try_match queue by size=%s",
                guild_id, guild_store.queue_by_group_size(guild_id),
            )
        else:
            logger.debug("Guild %d: try_match queue=%s", guild_id, queue)

        best_pair: Optional[tuple[str, str]] = None
        best_diff: float = float("inf")
        has_mmr_blocked_pair = False

        for i in range(len(queue)):
            for j in range(i + 1, len(queue)):
                t1_name, t2_name = queue[i], queue[j]
                t1 = guild_store.get_team(guild_id, t1_name)
                t2 = guild_store.get_team(guild_id, t2_name)

                if t1 is None or t2 is None:
                    continue

                # Rematch guard
                if t1.last_opponent == t2_name or t2.last_opponent == t1_name:
                    if cfg.rematch_cooldown_seconds > 0:
                        # Timed cooldown: block until both teams have waited
                        t1_wait = guild_store.seconds_since_last_match(guild_id, t1_name)
                        t2_wait = guild_store.seconds_since_last_match(guild_id, t2_name)
                        if min(t1_wait, t2_wait) < cfg.rematch_cooldown_seconds:
                            continue
                        # Cooldown elapsed — clear the guard and allow the match
                        guild_store.clear_last_opponents(guild_id, t1_name, t2_name)
                    else:
                        # Default: block only the immediate next match (1-match block)
                        continue

                # Modular composition: only match same group size
                if cfg.composition_type == "modular" and t1.group_size != t2.group_size:
                    continue

                mmr_diff = abs(t1.mmr - t2.mmr)
                max_wait = max(
                    guild_store.queue_wait_seconds(guild_id, t1_name),
                    guild_store.queue_wait_seconds(guild_id, t2_name),
                )

                if cfg.mmr_enabled:
                    within_threshold = mmr_diff <= cfg.mmr_match_threshold
                    relaxed = max_wait >= cfg.mmr_relax_seconds
                    if not within_threshold and not relaxed:
                        has_mmr_blocked_pair = True
                        continue
                # else: MMR disabled — all non-rematch-blocked pairs are eligible

                if mmr_diff < best_diff:
                    best_diff = mmr_diff
                    best_pair = (t1_name, t2_name)

        if best_pair is None:
            if has_mmr_blocked_pair:
                logger.debug(
                    "Guild %d: pairs exist but MMR gap > %d and no team has "
                    "waited %ds yet. Relax timers will retry.",
                    guild_id, cfg.mmr_match_threshold, cfg.mmr_relax_seconds,
                )
            return

        t1_name, t2_name = best_pair
        t1 = guild_store.get_team(guild_id, t1_name)
        t2 = guild_store.get_team(guild_id, t2_name)
        if t1 is None or t2 is None:
            return

        # Cancel relaxation timers — these teams are being matched now
        self._cancel_mmr_relax(guild_id, t1_name)
        self._cancel_mmr_relax(guild_id, t2_name)

        # Commit state before any await
        guild_store.dequeue(guild_id, t1_name)
        guild_store.dequeue(guild_id, t2_name)

        match_id = str(uuid.uuid4())[:8].upper()
        match = Match(id=match_id, team1_name=t1_name, team2_name=t2_name)
        guild_store.add_match(guild_id, match)

        t1.state = t2.state = TeamState.MATCHED
        t1.current_match_id = t2.current_match_id = match_id
        t1.current_opponent = t2_name
        t2.current_opponent = t1_name
        guild_store.save_team(guild_id, t1)
        guild_store.save_team(guild_id, t2)

        event_log.queue_left(guild_id, t1_name, reason="matched", group_size=t1.group_size)
        event_log.queue_left(guild_id, t2_name, reason="matched", group_size=t2.group_size)

        logger.info(
            "Guild %d: match %s proposed: %s (MMR %d) vs %s (MMR %d)  |  diff=%d",
            guild_id, match_id, t1_name, t1.mmr, t2_name, t2.mmr, int(best_diff),
        )

        event_log.match_proposed(guild_id, match_id, t1_name, t2_name)

        results = await asyncio.gather(
            self.update_team_panel(guild_id, t1),
            self.update_team_panel(guild_id, t2),
            self._send_proposal_ping(guild_id, match, t1, t2),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.error(
                    "Guild %d: error during match proposal gather: %s", guild_id, r,
                    exc_info=r,
                )

        # Save proposal_message_id that was set during _send_proposal_ping
        guild_store.save_match(guild_id, match)

        task: asyncio.Task[None] = asyncio.ensure_future(
            self._accept_timeout(guild_id, match_id, t1_name, t2_name)
        )
        self._pending_timeouts[(guild_id, match_id)] = task

    # ── Match lifecycle ───────────────────────────────────────────────────────

    async def accept_match(
        self, guild_id: int, match: Match, accepting_team_name: str
    ) -> None:
        """Record one team's acceptance and activate the match if both accepted."""
        if accepting_team_name == match.team1_name:
            match.team1_accepted = True
        else:
            match.team2_accepted = True

        team = guild_store.get_team(guild_id, accepting_team_name)
        if team:
            team.has_accepted = True
            guild_store.save_team(guild_id, team)

        guild_store.save_match(guild_id, match)

        if match.both_accepted:
            self._cancel_timeout(guild_id, match.id)
            await self._activate_match(guild_id, match)
        else:
            if team:
                await self.update_team_panel(guild_id, team)

    async def cancel_match(
        self, guild_id: int, match: Match, reason: str
    ) -> None:
        """Cancel a pending or active match without any rating penalty."""
        await self._delete_message(
            await self._get_matchmaking_channel(guild_id), match.proposal_message_id
        )

        t1 = guild_store.get_team(guild_id, match.team1_name)
        t2 = guild_store.get_team(guild_id, match.team2_name)

        for team in filter(None, [t1, t2]):
            team.state = TeamState.IDLE
            team.current_match_id = None
            team.current_opponent = None
            team.has_accepted = False
            guild_store.save_team(guild_id, team)

        t1_name, t2_name = match.team1_name, match.team2_name
        guild_store.remove_match(guild_id, match.id)

        await asyncio.gather(
            self.update_team_panel(guild_id, t1) if t1 else asyncio.sleep(0),
            self.update_team_panel(guild_id, t2) if t2 else asyncio.sleep(0),
            return_exceptions=True,
        )

        ch = await self._get_matchmaking_channel(guild_id)
        if ch:
            leader_tags = ""
            if t1 and t2:
                leader_tags = f"<@{t1.leader_id}>  <@{t2.leader_id}>\n"
            await ch.send(
                f"{leader_tags}"
                f"❌  Match **{t1_name} vs {t2_name}** cancelled — {reason}.\n"
                f"Your panel has been updated — check your thread to queue again.",
                delete_after=30,
            )

        logger.info("Guild %d: match %s cancelled: %s", guild_id, match.id, reason)

    async def end_match(
        self,
        guild_id: int,
        match: Match,
        ended_by: str,
        winner_name: Optional[str] = None,
    ) -> None:
        """Close a live match, optionally apply ELO, and reset both teams."""
        cfg = guild_store.get_event_config(guild_id)
        match.active = False

        t1 = guild_store.get_team(guild_id, match.team1_name)
        t2 = guild_store.get_team(guild_id, match.team2_name)

        # ── Rematch guard ─────────────────────────────────────────────────────
        if t1:
            t1.last_opponent = match.team2_name
            guild_store.record_match_end(guild_id, t1.name)
        if t2:
            t2.last_opponent = match.team1_name
            guild_store.record_match_end(guild_id, t2.name)

        # ── ELO update ────────────────────────────────────────────────────────
        # Apply MMR changes directly to t1/t2 so the reset loop below saves
        # the correct final values in a single write.
        if winner_name and t1 and t2 and cfg and cfg.mmr_enabled:
            loser_name = (
                match.team2_name if winner_name == match.team1_name else match.team1_name
            )
            winner = t1 if t1.name == winner_name else t2
            loser  = t1 if t1.name == loser_name  else t2
            old_w, old_l = winner.mmr, loser.mmr
            new_w, new_l = _elo_update(winner.mmr, loser.mmr, k=cfg.mmr_k_value)
            winner.mmr = new_w
            winner.wins += 1
            loser.mmr = new_l
            loser.losses += 1
            logger.info(
                "Guild %d: MMR: %s %d → %d (%+d)  |  %s %d → %d (%+d)",
                guild_id,
                winner_name, old_w, new_w, new_w - old_w,
                loser_name,  old_l, new_l, new_l - old_l,
            )
            event_log.mmr_updated(
                guild_id, winner_name, old_w, new_w,
                loser_name, old_l, new_l,
            )

        # ── Clean up broadcast embed ──────────────────────────────────────────
        await self._delete_message(
            await self._get_broadcast_channel(guild_id), match.active_message_id
        )

        # ── Broadcast result ──────────────────────────────────────────────────
        bc = await self._get_broadcast_channel(guild_id)
        if bc and t1 and t2:
            teams_map = {t1.name: t1, t2.name: t2}
            await bc.send(
                content=f"<@{t1.leader_id}>  <@{t2.leader_id}>",
                embed=embeds.match_ended(match, teams_map),
            )

        # ── Reset teams (single write per team, includes MMR changes above) ──
        for team in filter(None, [t1, t2]):
            team.state = TeamState.IDLE
            team.current_match_id = None
            team.current_opponent = None
            team.has_accepted = False
            guild_store.save_team(guild_id, team)

        event_log.match_ended(guild_id, match.id, match.team1_name, match.team2_name, ended_by)

        guild_store.remove_match(guild_id, match.id)

        await asyncio.gather(
            self.update_team_panel(guild_id, t1) if t1 else asyncio.sleep(0),
            self.update_team_panel(guild_id, t2) if t2 else asyncio.sleep(0),
            return_exceptions=True,
        )

        asyncio.ensure_future(self.try_match(guild_id))
        logger.info(
            "Guild %d: match %s ended by %s (winner=%s).",
            guild_id, match.id, ended_by, winner_name,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _send_proposal_ping(
        self, guild_id: int, match: Match, t1: Team, t2: Team
    ) -> None:
        """Post a short ping in #matchmaking so leaders get a notification."""
        ch = await self._get_matchmaking_channel(guild_id)
        if not ch:
            return
        msg = await ch.send(
            f"<@{t1.leader_id}>  <@{t2.leader_id}>\n"
            f"⚔️  **Match found!**  {t1.name}  vs  {t2.name}\n"
            f"Check your private thread — your panel has been updated."
        )
        match.proposal_message_id = msg.id

    async def _activate_match(self, guild_id: int, match: Match) -> None:
        """Transition a proposed match to active once both leaders accepted."""
        t1 = guild_store.get_team(guild_id, match.team1_name)
        t2 = guild_store.get_team(guild_id, match.team2_name)
        if not t1 or not t2:
            return

        match.active = True
        t1.state = t2.state = TeamState.IN_MATCH
        guild_store.save_team(guild_id, t1)
        guild_store.save_team(guild_id, t2)

        await self._delete_message(
            await self._get_matchmaking_channel(guild_id), match.proposal_message_id
        )

        teams_map = {t1.name: t1, t2.name: t2}
        bc = await self._get_broadcast_channel(guild_id)

        await asyncio.gather(
            self.update_team_panel(guild_id, t1),
            self.update_team_panel(guild_id, t2),
            return_exceptions=True,
        )

        if bc:
            try:
                msg = await bc.send(
                    content=(
                        f"🔔  **MATCH STARTED!**\n"
                        f"<@{t1.leader_id}>  (**{t1.name}**)  ⚔️  "
                        f"<@{t2.leader_id}>  (**{t2.name}**)"
                    ),
                    embed=embeds.active_match(match, teams_map),
                )
                match.active_message_id = msg.id
            except Exception as exc:
                logger.error(
                    "Guild %d: failed to send match-started broadcast: %s", guild_id, exc
                )

        guild_store.save_match(guild_id, match)
        logger.info("Guild %d: match %s is now active.", guild_id, match.id)
        event_log.match_started(guild_id, match.id, match.team1_name, match.team2_name)

    @staticmethod
    async def _delete_message(
        channel: Optional[discord.TextChannel],
        message_id: Optional[int],
    ) -> None:
        """Silently delete a message by ID, ignoring 404 errors."""
        if not channel or not message_id:
            return
        try:
            msg = await channel.fetch_message(message_id)
            await msg.delete()
        except discord.NotFound:
            pass

    def _cancel_timeout(self, guild_id: int, match_id: str) -> None:
        """Cancel the acceptance timeout task for a match if still pending."""
        key = (guild_id, match_id)
        task = self._pending_timeouts.pop(key, None)
        if task and not task.done():
            task.cancel()
            logger.debug(
                "Guild %d: acceptance timeout cancelled for match %s.", guild_id, match_id
            )

    def cancel_all_timeouts(self, guild_id: int) -> None:
        """Cancel all pending acceptance timeout tasks for a guild.

        Used by the simulation suite before bulk-accepting matches to prevent
        the real timeout timer from firing mid-test.
        """
        keys = [k for k in self._pending_timeouts if k[0] == guild_id]
        for key in keys:
            task = self._pending_timeouts.pop(key, None)
            if task and not task.done():
                task.cancel()
        if keys:
            logger.debug("Guild %d: cancelled %d pending timeout(s).", guild_id, len(keys))

    async def _accept_timeout(
        self, guild_id: int, match_id: str, t1_name: str, t2_name: str
    ) -> None:
        """Requeue teams that accepted if the timeout expires before both respond."""
        cfg = guild_store.get_event_config(guild_id)
        timeout = cfg.match_accept_timeout if cfg else 60
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return

        self._pending_timeouts.pop((guild_id, match_id), None)
        match = guild_store.get_match(guild_id, match_id)
        if not match or match.active or match.both_accepted:
            return

        logger.info(
            "Guild %d: match %s timed out — requeuing %s and %s.",
            guild_id, match_id, t1_name, t2_name,
        )

        await self._delete_message(
            await self._get_matchmaking_channel(guild_id), match.proposal_message_id
        )

        t1 = guild_store.get_team(guild_id, t1_name)
        t2 = guild_store.get_team(guild_id, t2_name)

        for team in filter(None, [t1, t2]):
            team.state = TeamState.IDLE
            team.current_match_id = None
            team.current_opponent = None
            team.has_accepted = False

        if t1:
            if match.team1_accepted:
                t1.state = TeamState.READY
                guild_store.enqueue(guild_id, t1_name)
                self.schedule_mmr_relax(guild_id, t1_name)
            else:
                t1.state = TeamState.IDLE
            guild_store.save_team(guild_id, t1)

        if t2:
            if match.team2_accepted:
                t2.state = TeamState.READY
                guild_store.enqueue(guild_id, t2_name)
                self.schedule_mmr_relax(guild_id, t2_name)
            else:
                t2.state = TeamState.IDLE
            guild_store.save_team(guild_id, t2)

        guild_store.remove_match(guild_id, match_id)

        event_log.match_timeout(
            guild_id, match_id, t1_name, t2_name,
            match.team1_accepted, match.team2_accepted,
        )

        await asyncio.gather(
            self.update_team_panel(guild_id, t1) if t1 else asyncio.sleep(0),
            self.update_team_panel(guild_id, t2) if t2 else asyncio.sleep(0),
            return_exceptions=True,
        )

        ch = await self._get_matchmaking_channel(guild_id)
        if ch and t1 and t2:
            lines = []
            if not match.team1_accepted:
                lines.append(
                    f"<@{t1.leader_id}> (**{t1_name}**) did not respond → back to IDLE"
                )
            else:
                lines.append(
                    f"<@{t1.leader_id}> (**{t1_name}**) accepted → back in queue"
                )
            if not match.team2_accepted:
                lines.append(
                    f"<@{t2.leader_id}> (**{t2_name}**) did not respond → back to IDLE"
                )
            else:
                lines.append(
                    f"<@{t2.leader_id}> (**{t2_name}**) accepted → back in queue"
                )
            await ch.send(
                f"⏱️  Match **{t1_name} vs {t2_name}** timed out.\n"
                + "\n".join(lines),
                delete_after=30,
            )

        asyncio.ensure_future(self.try_match(guild_id))
