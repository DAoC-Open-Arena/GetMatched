"""Discord embed builders for the matchmaking bot.

Each function returns a fully constructed :class:`discord.Embed` ready to be
passed to a ``send`` or ``edit`` call. Keeping embed construction here means
the rest of the codebase never has to deal with colour codes, field layout, or
display copy.
"""

from __future__ import annotations

import discord

from daoc_bot.models import Match, Team, TeamState


# ── Colour palette ────────────────────────────────────────────────────────────

_COLOUR: dict[TeamState, discord.Color] = {
    TeamState.IDLE:     discord.Color.greyple(),
    TeamState.READY:    discord.Color.blue(),
    TeamState.MATCHED:  discord.Color.gold(),
    TeamState.IN_MATCH: discord.Color.blue(),
}

_STATE_LABEL: dict[TeamState, str] = {
    TeamState.IDLE:     "⏸️  Idle — not in queue",
    TeamState.READY:    "🔵  In queue — looking for opponent",
    TeamState.MATCHED:  "🟡  Match proposed — waiting for acceptance",
    TeamState.IN_MATCH: "🔵  Match in progress",
}


# ── Public builders ───────────────────────────────────────────────────────────

def team_panel(team: Team) -> discord.Embed:
    """Build the live status panel embed for a registered team.

    Args:
        team: The team whose current state should be reflected.

    Returns:
        A :class:`discord.Embed` suitable for posting or editing in the
        matchmaking channel.
    """
    embed = discord.Embed(
        title=f"🛡️  {team.name}",
        color=_COLOUR[team.state],
    )
    embed.add_field(name="Status", value=_STATE_LABEL[team.state], inline=False)
    embed.add_field(name="Leader", value=f"<@{team.leader_id}>",   inline=True)

    # ── MMR / record ──────────────────────────────────────────────────────────
    total = team.wins + team.losses
    wr_str = f"{team.wins / total * 100:.0f}% WR" if total > 0 else "no matches yet"
    embed.add_field(
        name="Rating",
        value=f"⚡ **{team.mmr}** MMR  |  {team.wins}W – {team.losses}L  |  {wr_str}",
        inline=False,
    )

    if team.state in (TeamState.MATCHED, TeamState.IN_MATCH) and team.current_opponent:
        embed.add_field(
            name="Opponent",
            value=f"⚔️  **{team.current_opponent}**",
            inline=False,
        )

    if team.last_opponent:
        embed.set_footer(
            text=f"Last opponent: {team.last_opponent}  |  instant rematch prevention active"
        )
    return embed


def match_proposal(match: Match, teams: dict[str, Team]) -> discord.Embed:
    """Build the accept/decline prompt shown when a match is proposed.

    Args:
        match: The newly created match awaiting acceptance.
        teams: Mapping of team name → :class:`Team` (used to resolve leaders).

    Returns:
        A :class:`discord.Embed` with per-team acceptance status and MMR info.
    """
    t1 = teams[match.team1_name]
    t2 = teams[match.team2_name]
    mmr_diff = abs(t1.mmr - t2.mmr)

    def status(accepted: bool) -> str:
        return "✅  Accepted" if accepted else "⏳  Waiting"

    embed = discord.Embed(
        title="⚔️  Match Found!",
        description=(
            f"**{match.team1_name}**  vs  **{match.team2_name}**\n"
            f"MMR difference: **{mmr_diff}**\n\n"
            "Both team leaders must accept to start the match."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name=f"{match.team1_name}  (MMR {t1.mmr})",
        value=f"<@{t1.leader_id}>\n{status(match.team1_accepted)}",
        inline=True,
    )
    embed.add_field(
        name=f"{match.team2_name}  (MMR {t2.mmr})",
        value=f"<@{t2.leader_id}>\n{status(match.team2_accepted)}",
        inline=True,
    )
    embed.set_footer(text=f"Match ID: {match.id}  |  You have 2 minutes to respond")
    return embed


def active_match(match: Match, teams: dict[str, Team]) -> discord.Embed:
    """Build the live match panel shown once both leaders have accepted.

    Args:
        match: The currently active match.
        teams: Mapping of team name → :class:`Team`.

    Returns:
        A :class:`discord.Embed` showing both team leaders and match ID.
        MMR is intentionally omitted from this public embed.
    """
    t1 = teams[match.team1_name]
    t2 = teams[match.team2_name]

    embed = discord.Embed(
        title=f"⚔️  {match.team1_name}  vs  {match.team2_name}",
        description=(
            "Match in progress — have fun! ⚔️\n"
            "When you're done, click **🏆 We Won** or **💀 We Lost** in your panel."
        ),
        color=discord.Color.blue(),
    )
    embed.add_field(name=match.team1_name, value=f"<@{t1.leader_id}>", inline=True)
    embed.add_field(name="VS",             value="\u200b",             inline=True)
    embed.add_field(name=match.team2_name, value=f"<@{t2.leader_id}>", inline=True)
    embed.set_footer(text=f"Match ID: {match.id}")
    return embed


def match_ended(match: Match, teams: dict[str, Team]) -> discord.Embed:
    """Build the announcement embed posted when a match finishes.

    Winner, MMR changes, and W/L records are intentionally omitted from this
    public embed — that information is private to each team's own panel and
    the server-side event log.

    Args:
        match: The concluded match.
        teams: Mapping of team name → :class:`Team`.

    Returns:
        A neutral, score-free conclusion embed.
    """
    t1 = teams[match.team1_name]
    t2 = teams[match.team2_name]

    embed = discord.Embed(
        title="🏁  Match Over!",
        description=(
            f"**{match.team1_name}**  vs  **{match.team2_name}**\n\n"
            "Great game! Click **🔵 Get a Match** in your panel whenever "
            "you want to queue again. ⚔️"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name=t1.name, value=f"<@{t1.leader_id}>", inline=True)
    embed.add_field(name=t2.name, value=f"<@{t2.leader_id}>", inline=True)
    return embed