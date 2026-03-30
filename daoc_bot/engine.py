"""Matchmaking engine — all core business logic lives here.

The engine is responsible for:

* Pairing teams from the ready queue, preferring close MMR matches and
  relaxing the MMR constraint after a configurable wait time.
* Editing each leader's private thread panel whenever state changes.
* Sending channel pings and broadcast embeds at key moments.
* Updating ELO ratings when a match result is reported.

Matchmaking algorithm
---------------------
When ``try_match`` is called the engine scans all valid pairs in the queue
(rematch guard still applies) and picks the pair with the **smallest MMR
difference** that is also **eligible** under the current threshold:

* A pair is eligible if ``|mmr_a - mmr_b| <= MMR_MATCH_THRESHOLD`` **or**
  at least one team has been waiting at least ``MMR_RELAX_SECONDS``.
* If no eligible pair exists the engine exits silently. Each team that joins
  the queue starts a background timer; when it fires the engine retries with
  the expanded eligibility window.

ELO ratings
-----------
Standard ELO with K=32 and a starting rating of 1000.  The winning team
gains points and the losing team loses the same number (zero-sum per match).

Panel update strategy
---------------------
Each leader has a private thread in #matchmaking created at registration.
The bot edits a single message in that thread whenever state changes.
Because it is a normal (non-ephemeral) message the bot can update it
at any time — no user action required.

Channel messages
----------------
#matchmaking  — match-found ping, cancellation notice (both auto-delete)
#broadcast    — match-started embed, match-ended embed (persistent)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

import discord

from daoc_bot import embeds, event_log
from daoc_bot.config import settings
from daoc_bot.models import Match, Team, TeamState
from daoc_bot.state import store

logger = logging.getLogger(__name__)

# ── MMR / matchmaking constants ────────────────────────────────────────────────

#: Maximum MMR difference allowed for an immediate match.
MMR_MATCH_THRESHOLD: int = 200

#: Seconds a team must wait before the MMR threshold is lifted entirely.
MMR_RELAX_SECONDS: int = 120

#: ELO K-factor — controls how much a single result shifts ratings.
ELO_K: int = 32


# ── ELO helper ────────────────────────────────────────────────────────────────

def _elo_update(winner_mmr: int, loser_mmr: int) -> tuple[int, int]:
    """Return ``(new_winner_mmr, new_loser_mmr)`` after one match.

    Uses the standard ELO formula with :data:`ELO_K` as the K-factor.
    The update is zero-sum: points gained by the winner equal points lost
    by the loser.

    Args:
        winner_mmr: Current MMR of the winning team.
        loser_mmr:  Current MMR of the losing team.

    Returns:
        A ``(new_winner_mmr, new_loser_mmr)`` tuple of integers.
    """
    expected_winner = 1.0 / (1.0 + 10.0 ** ((loser_mmr - winner_mmr) / 400.0))
    delta = round(ELO_K * (1.0 - expected_winner))
    return winner_mmr + delta, loser_mmr - delta


# ── Engine ────────────────────────────────────────────────────────────────────

class MatchmakingEngine:
    """Orchestrates the full match lifecycle."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self._match_lock: Optional[asyncio.Lock] = None
        self._pending_timeouts: dict[str, asyncio.Task[None]] = {}
        self._mmr_relax_tasks: dict[str, asyncio.Task[None]] = {}

    def _get_lock(self) -> asyncio.Lock:
        """Return the match lock, creating it lazily on the running event loop."""
        if self._match_lock is None:
            self._match_lock = asyncio.Lock()
        return self._match_lock

    # ── Channel helpers ───────────────────────────────────────────────────────

    async def _get_matchmaking_channel(self) -> Optional[discord.TextChannel]:
        channel = self.bot.get_channel(settings.matchmaking_channel_id)
        if channel is None:
            logger.error(
                "Matchmaking channel ID %d not found in cache.",
                settings.matchmaking_channel_id,
            )
        return channel  # type: ignore[return-value]

    async def _get_broadcast_channel(self) -> Optional[discord.TextChannel]:
        channel = self.bot.get_channel(settings.broadcast_channel_id)
        if channel is None:
            logger.error(
                "Broadcast channel ID %d not found in cache.",
                settings.broadcast_channel_id,
            )
        return channel  # type: ignore[return-value]

    # ── Panel management ──────────────────────────────────────────────────────

    async def create_team_panel(self, team: Team, leader: discord.Member) -> None:
        """Create a private thread for the leader and post the initial panel.

        Called once when a team registers.  The thread is private so only the
        leader (and the bot) can see it.  The panel message inside the thread
        is edited in-place from this point on — the leader never needs to do
        anything to see updated buttons.

        Args:
            team:   The newly registered team.
            leader: The Discord member who is the team leader.
        """
        ch = await self._get_matchmaking_channel()
        if not ch:
            logger.error("Matchmaking channel not found — cannot create panel thread.")
            return

        thread = await ch.create_thread(
            name=f"Control Room — {team.name}",
            type=discord.ChannelType.private_thread,
            reason=f"Matchmaking panel for team {team.name}",
        )
        await thread.add_user(leader)

        # Local import breaks the circular dependency:
        # engine ← views/team_panel ← engine
        from daoc_bot.views.team_panel import view_for_state

        msg = await thread.send(
            embed=embeds.team_panel(team),
            view=view_for_state(team, self),
        )
        team.panel_thread_id = thread.id
        team.panel_message_id = msg.id
        logger.info(
            "Panel thread created for team '%s' (thread=%d)", team.name, thread.id
        )

    async def update_team_panel(self, team: Team) -> None:
        """Edit the panel message in the leader's private thread.

        Safe to call from any state change — silently no-ops if the thread or
        message no longer exists.

        Args:
            team: The team whose panel should be refreshed.
        """
        if not team.panel_thread_id or not team.panel_message_id:
            return

        # Local import breaks the circular dependency:
        # engine ← views/team_panel ← engine
        from daoc_bot.views.team_panel import view_for_state

        thread = self.bot.get_channel(team.panel_thread_id)
        if not thread:
            try:
                thread = await self.bot.fetch_channel(team.panel_thread_id)
            except discord.NotFound:
                logger.warning("Panel thread for '%s' not found.", team.name)
                return

        try:
            assert isinstance(thread, discord.Thread)
            msg = await thread.fetch_message(team.panel_message_id)
            await msg.edit(
                embed=embeds.team_panel(team),
                view=view_for_state(team, self),
            )
        except discord.NotFound:
            logger.warning("Panel message for '%s' not found.", team.name)

    async def delete_team_panel(self, team: Team) -> None:
        """Delete the leader's private thread when a team unregisters.

        Args:
            team: The team being removed.
        """
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

    def schedule_mmr_relax(self, team_name: str) -> None:
        """Start (or restart) the MMR-relaxation timer for ``team_name``.

        After :data:`MMR_RELAX_SECONDS` the engine retries :meth:`try_match`
        with the full eligibility window open for this team, accepting any
        valid opponent regardless of MMR difference.

        Args:
            team_name: Name of the team that just entered the queue.
        """
        self._cancel_mmr_relax(team_name)
        task: asyncio.Task[None] = asyncio.ensure_future(
            self._delayed_try_match(team_name)
        )
        self._mmr_relax_tasks[team_name] = task

    def _cancel_mmr_relax(self, team_name: str) -> None:
        """Cancel the MMR-relaxation timer for ``team_name`` if it is pending."""
        task = self._mmr_relax_tasks.pop(team_name, None)
        if task and not task.done():
            task.cancel()
            logger.debug("MMR relax timer cancelled for '%s'.", team_name)

    async def _delayed_try_match(self, team_name: str) -> None:
        """Fire :meth:`try_match` after the relaxation window has elapsed."""
        try:
            await asyncio.sleep(MMR_RELAX_SECONDS)
        except asyncio.CancelledError:
            return
        self._mmr_relax_tasks.pop(team_name, None)
        team = store.get_team(team_name)
        if team and team.state == TeamState.READY:
            logger.info(
                "MMR relaxation triggered for '%s' — retrying match.", team_name
            )
            await self.try_match()

    # ── Queue management ──────────────────────────────────────────────────────

    async def try_match(self) -> None:
        """Attempt to pair two teams from the ready queue.

        Serialised by ``_match_lock`` so concurrent Ready clicks cannot
        produce duplicate pairings.
        """
        async with self._get_lock():
            await self._try_match_locked()

    async def _try_match_locked(self) -> None:
        """Inner implementation — must only be called while holding _match_lock.

        Scans every valid pair in the queue and selects the one with the
        smallest MMR difference that is also currently eligible:

        * Eligible immediately if ``|mmr_a - mmr_b| <= MMR_MATCH_THRESHOLD``.
        * Eligible after waiting if ``max(wait_a, wait_b) >= MMR_RELAX_SECONDS``.

        Among all eligible pairs the pair with the smallest MMR gap is chosen.
        If no pair is eligible the method returns silently; the per-team relax
        timers will call :meth:`try_match` again when the window opens.
        """
        if store.queue_size < 2:
            return

        queue = store.queue
        logger.debug("try_match: queue=%s", queue)

        best_pair: Optional[tuple[str, str]] = None
        best_diff: float = float("inf")
        has_mmr_blocked_pair = False  # valid pair exists but both fresh & far apart

        for i in range(len(queue)):
            for j in range(i + 1, len(queue)):
                t1_name, t2_name = queue[i], queue[j]
                t1 = store.get_team(t1_name)
                t2 = store.get_team(t2_name)

                if t1 is None or t2 is None:
                    continue

                # Rematch guard
                if t1.last_opponent == t2_name or t2.last_opponent == t1_name:
                    continue

                mmr_diff = abs(t1.mmr - t2.mmr)
                max_wait = max(
                    store.queue_wait_seconds(t1_name),
                    store.queue_wait_seconds(t2_name),
                )
                within_threshold = mmr_diff <= MMR_MATCH_THRESHOLD
                relaxed = max_wait >= MMR_RELAX_SECONDS

                if not within_threshold and not relaxed:
                    has_mmr_blocked_pair = True
                    continue  # not yet eligible

                if mmr_diff < best_diff:
                    best_diff = mmr_diff
                    best_pair = (t1_name, t2_name)

        if best_pair is None:
            # Special case: only 2 teams and they are rematch-blocked
            if store.queue_size == 2:
                t1_name, t2_name = store.queue
                t1 = store.get_team(t1_name)
                t2 = store.get_team(t2_name)
                if t1 and t2 and (
                    t1.last_opponent == t2_name or t2.last_opponent == t1_name
                ):
                    logger.info(
                        "Only pair in queue is instant-rematch (%s vs %s). "
                        "Lifting restriction.",
                        t1_name, t2_name,
                    )
                    store.clear_last_opponents(t1_name, t2_name)
                    await self._try_match_locked()
                    return

            if has_mmr_blocked_pair:
                logger.debug(
                    "Pairs exist but MMR gap > %d and no team has waited %ds yet. "
                    "Relax timers will retry.",
                    MMR_MATCH_THRESHOLD, MMR_RELAX_SECONDS,
                )
            return

        t1_name, t2_name = best_pair
        t1 = store.get_team(t1_name)
        t2 = store.get_team(t2_name)
        if t1 is None or t2 is None:
            return

        # Cancel relaxation timers — these teams are being matched now
        self._cancel_mmr_relax(t1_name)
        self._cancel_mmr_relax(t2_name)

        # Commit state before any await
        store.dequeue(t1_name)
        store.dequeue(t2_name)

        match_id = str(uuid.uuid4())[:8].upper()
        match = Match(id=match_id, team1_name=t1_name, team2_name=t2_name)
        store.add_match(match)

        t1.state = t2.state = TeamState.MATCHED
        t1.current_match_id = t2.current_match_id = match_id

        event_log.queue_left(t1_name, reason="matched")
        event_log.queue_left(t2_name, reason="matched")

        logger.info(
            "Match %s proposed: %s (MMR %d) vs %s (MMR %d)  |  diff=%d",
            match_id, t1_name, t1.mmr, t2_name, t2.mmr, int(best_diff),
        )

        event_log.match_proposed(match_id, t1_name, t2_name)

        results = await asyncio.gather(
            self.update_team_panel(t1),
            self.update_team_panel(t2),
            self._send_proposal_ping(match, t1, t2),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.error("Error during match proposal gather: %s", r, exc_info=r)

        task: asyncio.Task[None] = asyncio.ensure_future(
            self._accept_timeout(match_id, t1_name, t2_name)
        )
        self._pending_timeouts[match_id] = task

    # ── Match lifecycle ───────────────────────────────────────────────────────

    async def accept_match(self, match: Match, accepting_team_name: str) -> None:
        """Record one team's acceptance and activate the match if both accepted.

        Args:
            match:               The match being responded to.
            accepting_team_name: Name of the team whose leader pressed Accept.
        """
        if accepting_team_name == match.team1_name:
            match.team1_accepted = True
        else:
            match.team2_accepted = True

        team = store.get_team(accepting_team_name)
        if team:
            team.has_accepted = True

        if match.both_accepted:
            self._cancel_timeout(match.id)
            await self._activate_match(match)
        else:
            if team:
                await self.update_team_panel(team)

    async def cancel_match(self, match: Match, reason: str) -> None:
        """Cancel a pending or active match without any rating penalty.

        Resets both teams to IDLE and updates their panels.

        Args:
            match:  The match to cancel.
            reason: Human-readable explanation posted in #matchmaking.
        """
        await self._delete_message(
            await self._get_matchmaking_channel(), match.proposal_message_id
        )

        t1 = store.get_team(match.team1_name)
        t2 = store.get_team(match.team2_name)

        for team in filter(None, [t1, t2]):
            team.state = TeamState.IDLE
            team.current_match_id = None

        t1_name, t2_name = match.team1_name, match.team2_name
        store.remove_match(match.id)

        await asyncio.gather(
            self.update_team_panel(t1) if t1 else asyncio.sleep(0),
            self.update_team_panel(t2) if t2 else asyncio.sleep(0),
            return_exceptions=True,
        )

        ch = await self._get_matchmaking_channel()
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

        logger.info("Match %s cancelled: %s", match.id, reason)

    async def end_match(
        self,
        match: Match,
        ended_by: str,
        winner_name: Optional[str] = None,
    ) -> None:
        """Close a live match, apply ELO changes, and reset both teams.

        If ``winner_name`` is provided the winning team gains ELO and the
        losing team loses the same amount (zero-sum).  If omitted (e.g. an
        admin force-close or a simulation call) ratings are left unchanged.

        Args:
            match:       The match to close.
            ended_by:    Display name of the team that triggered the end.
            winner_name: Name of the winning team, or ``None`` for no result.
        """
        match.active = False

        t1 = store.get_team(match.team1_name)
        t2 = store.get_team(match.team2_name)

        # ── Rematch guard ─────────────────────────────────────────────────────
        if t1:
            t1.last_opponent = match.team2_name
        if t2:
            t2.last_opponent = match.team1_name

        # ── ELO update ────────────────────────────────────────────────────────
        if winner_name and t1 and t2:
            loser_name = (
                match.team2_name if winner_name == match.team1_name else match.team1_name
            )
            winner = store.get_team(winner_name)
            loser  = store.get_team(loser_name)
            if winner and loser:
                old_w, old_l = winner.mmr, loser.mmr
                new_w, new_l = _elo_update(winner.mmr, loser.mmr)
                winner.mmr = new_w
                winner.wins += 1
                loser.mmr = new_l
                loser.losses += 1
                logger.info(
                    "MMR: %s %d → %d (%+d)  |  %s %d → %d (%+d)",
                    winner_name, old_w, new_w, new_w - old_w,
                    loser_name,  old_l, new_l, new_l - old_l,
                )
                event_log.mmr_updated(
                    winner_name, old_w, new_w,
                    loser_name,  old_l, new_l,
                )

        # ── Clean up broadcast embed ──────────────────────────────────────────
        await self._delete_message(
            await self._get_broadcast_channel(), match.active_message_id
        )

        # ── Broadcast result ──────────────────────────────────────────────────
        bc = await self._get_broadcast_channel()
        if bc and t1 and t2:
            teams_map = {t1.name: t1, t2.name: t2}
            await bc.send(
                content=f"<@{t1.leader_id}>  <@{t2.leader_id}>",
                embed=embeds.match_ended(match, teams_map),
            )

        # ── Reset teams ───────────────────────────────────────────────────────
        for team in filter(None, [t1, t2]):
            team.state = TeamState.IDLE
            team.current_match_id = None
            team.has_accepted = False

        event_log.match_ended(match.id, match.team1_name, match.team2_name, ended_by)

        store.remove_match(match.id)

        await asyncio.gather(
            self.update_team_panel(t1) if t1 else asyncio.sleep(0),
            self.update_team_panel(t2) if t2 else asyncio.sleep(0),
            return_exceptions=True,
        )

        asyncio.ensure_future(self.try_match())
        logger.info("Match %s ended by %s (winner=%s).", match.id, ended_by, winner_name)

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _send_proposal_ping(self, match: Match, t1: Team, t2: Team) -> None:
        """Post a short ping in #matchmaking so leaders get a notification."""
        ch = await self._get_matchmaking_channel()
        if not ch:
            return
        msg = await ch.send(
            f"<@{t1.leader_id}>  <@{t2.leader_id}>\n"
            f"⚔️  **Match found!**  {t1.name}  vs  {t2.name}\n"
            f"Check your private thread — your panel has been updated."
        )
        match.proposal_message_id = msg.id

    async def _activate_match(self, match: Match) -> None:
        """Transition a proposed match to active once both leaders accepted."""
        t1 = store.get_team(match.team1_name)
        t2 = store.get_team(match.team2_name)
        if not t1 or not t2:
            return

        match.active = True
        t1.state = t2.state = TeamState.IN_MATCH

        await self._delete_message(
            await self._get_matchmaking_channel(), match.proposal_message_id
        )

        teams_map = {t1.name: t1, t2.name: t2}

        bc = await self._get_broadcast_channel()

        await asyncio.gather(
            self.update_team_panel(t1),
            self.update_team_panel(t2),
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
                logger.error("Failed to send match-started broadcast: %s", exc)

        logger.info("Match %s is now active.", match.id)
        event_log.match_started(match.id, match.team1_name, match.team2_name)

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

    def _cancel_timeout(self, match_id: str) -> None:
        """Cancel the acceptance timeout task for a match if still pending."""
        task = self._pending_timeouts.pop(match_id, None)
        if task and not task.done():
            task.cancel()
            logger.debug("Acceptance timeout cancelled for match %s.", match_id)

    async def _accept_timeout(
        self, match_id: str, t1_name: str, t2_name: str
    ) -> None:
        """Requeue teams that accepted if the timeout expires before both respond."""
        try:
            await asyncio.sleep(settings.match_accept_timeout)
        except asyncio.CancelledError:
            return

        self._pending_timeouts.pop(match_id, None)
        match = store.get_match(match_id)
        if not match or match.active or match.both_accepted:
            return

        logger.info(
            "Match %s timed out — requeuing %s and %s.", match_id, t1_name, t2_name
        )

        await self._delete_message(
            await self._get_matchmaking_channel(), match.proposal_message_id
        )

        t1 = store.get_team(t1_name)
        t2 = store.get_team(t2_name)

        for team in filter(None, [t1, t2]):
            team.state = TeamState.IDLE
            team.current_match_id = None
            team.current_opponent = None
            team.has_accepted = False

        if t1:
            if match.team1_accepted:
                t1.state = TeamState.READY
                store.enqueue(t1_name)
                self.schedule_mmr_relax(t1_name)
            else:
                t1.state = TeamState.IDLE

        if t2:
            if match.team2_accepted:
                t2.state = TeamState.READY
                store.enqueue(t2_name)
                self.schedule_mmr_relax(t2_name)
            else:
                t2.state = TeamState.IDLE

        store.remove_match(match_id)

        event_log.match_timeout(
            match_id, t1_name, t2_name,
            match.team1_accepted, match.team2_accepted,
        )

        await asyncio.gather(
            self.update_team_panel(t1) if t1 else asyncio.sleep(0),
            self.update_team_panel(t2) if t2 else asyncio.sleep(0),
            return_exceptions=True,
        )

        ch = await self._get_matchmaking_channel()
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

        asyncio.ensure_future(self.try_match())
