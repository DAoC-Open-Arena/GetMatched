# DAoC Open Arena — XvX Matchmaking Bot

A Discord bot for managing competitive XvX events in *Dark Age of Camelot*.
Teams register, queue, get paired by ELO-aware matchmaking, and report results — all through interactive Discord UI buttons with no manual admin work between matches.

---

## Features

- **Team registration** — each group leader creates a team with their in-game character name
- **Private control-room threads** — every leader gets a dedicated private thread with a live panel that auto-updates as their team's state changes
- **ELO matchmaking** — standard ELO with K = 32, starting MMR 1000; closest-MMR pairs are preferred
- **MMR relaxation** — after 120 s in queue, the MMR threshold is lifted so no team waits forever
- **Instant-rematch guard** — teams cannot face the same opponent back-to-back; auto-lifts when only two teams are queued
- **Acceptance flow** — both leaders must press **Accept** within the configured timeout (default 60 s) or the match is cancelled without penalty
- **Result reporting** — leaders click **We Won** / **We Lost**; ELO updates are applied immediately
- **Admin commands** — force-reset teams, cancel matches, override MMR, view full leaderboard
- **Structured event logging** — every key action is written to a newline-delimited JSON log for post-event analytics
- **In-process simulation suite** — `/run_tests` runs 19 automated scenarios against the live engine without a Discord connection

---

## Quick Start

### Prerequisites

- Python 3.11+
- A Discord bot application with the following **Privileged Gateway Intents** enabled:
  - Server Members Intent
  - Message Content Intent

### Installation

```bash
# 1. Clone the repository
git clone <your-repo-url>
cd matchmaking_ladder_bot

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\activate           # Windows

# 3. Install the package (editable mode includes dev extras)
pip install -e ".[dev]"

# 4. Copy the example environment file and fill in your values
cp .env.example .env
```

### Configuration

Edit `.env` with your bot credentials and Discord channel IDs:

```dotenv
DISCORD_TOKEN=your-bot-token-here
MATCHMAKING_CHANNEL_ID=123456789012345678
BROADCAST_CHANNEL_ID=987654321098765432

# Optional — defaults shown
TEAM_LEADER_ROLE_NAME=Team Leader
MATCH_ACCEPT_TIMEOUT=60
LOG_LEVEL=INFO
```

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Bot token from the Discord Developer Portal |
| `MATCHMAKING_CHANNEL_ID` | ✅ | Channel where queue pings and private threads are created |
| `BROADCAST_CHANNEL_ID` | ✅ | Public channel where match-started / match-ended embeds are posted |
| `TEAM_LEADER_ROLE_NAME` | ❌ | Discord role name that gates all leader commands (use `*` to disable during testing) |
| `MATCH_ACCEPT_TIMEOUT` | ❌ | Seconds both leaders have to accept before the match auto-cancels (default `60`) |
| `LOG_LEVEL` | ❌ | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default `INFO`) |

### Discord Server Setup

1. Create a `#matchmaking` text channel — restrict it so only **Team Leader** role members and the bot can send messages; the bot creates private threads here.
2. Create a `#broadcast` text channel — public or semi-public; the bot posts match announcements here.
3. Create a **Team Leader** role and assign it to event participants.
4. Invite the bot with the `bot` and `applications.commands` OAuth2 scopes and the following permissions:
   - Send Messages, Send Messages in Threads
   - Create Private Threads
   - Manage Threads (to delete control-room threads on unregister)
   - Read Message History, Embed Links

### Running the Bot

```bash
python -m daoc_bot
```

---

## Running Tests

```bash
# All tests
pytest

# Unit tests only
pytest tests/unit/

# Integration tests only
pytest tests/integration/

# With coverage report
pytest --cov=daoc_bot --cov-report=term-missing

# Type checking
mypy daoc_bot/

# Standalone simulation (no Discord connection required)
python simulate.py                  # all scenarios
python simulate.py --scenario basic
python simulate.py --verbose        # dump channel message log
```

---

## Project Structure

```
matchmaking_ladder_bot/
├── daoc_bot/                        # Main package
│   ├── __init__.py
│   ├── __main__.py                  # Entry point, bot setup, on_ready handler
│   ├── config.py                    # Settings loaded from environment variables
│   ├── models.py                    # Domain models: Team, Match, TeamState
│   ├── state.py                     # In-memory store (BotState singleton)
│   ├── engine.py                    # Matchmaking engine and match lifecycle
│   ├── commands.py                  # Slash command definitions
│   ├── embeds.py                    # Discord embed builders
│   ├── event_log.py                 # Structured JSONL event logging
│   ├── simulation.py                # In-process test suite (used by /run_tests)
│   └── views/
│       ├── __init__.py
│       └── team_panel.py            # Context-aware leader control panel
├── tests/
│   ├── conftest.py                  # Shared fixtures
│   ├── unit/
│   │   ├── test_models.py
│   │   ├── test_state.py
│   │   ├── test_engine.py
│   │   ├── test_embeds.py
│   │   └── test_event_log.py
│   └── integration/
│       └── test_matchmaking_flow.py
├── simulate.py                      # Standalone simulation CLI (no Discord needed)
├── pyproject.toml
├── requirements.txt
└── .env.example
```

---

## Architecture

### State Machine

Each `Team` moves through four states:

```
IDLE ──(Get a Match)──► READY ──(pair found)──► MATCHED ──(both accept)──► IN_MATCH
  ▲                                                                              │
  └─────────────────────── (match ends / cancelled / timeout) ──────────────────┘
```

### Matchmaking Algorithm

`try_match()` scans all valid pairs in the ready queue and selects the one with the smallest MMR difference that is **eligible**:

- Eligible immediately if `|mmr_a − mmr_b| ≤ 200`
- Eligible after waiting if `max(wait_a, wait_b) ≥ 120 s`

Pairs blocked by the instant-rematch guard are skipped; the guard auto-lifts if those two teams are the only pair available.

### ELO Formula

```
expected_winner = 1 / (1 + 10^((loser_mmr − winner_mmr) / 400))
delta           = round(32 × (1 − expected_winner))
winner_mmr     += delta
loser_mmr      -= delta
```

### Event Log

Every action is appended to `logs/event_YYYY-MM-DD.jsonl` as a JSON line, e.g.:

```json
{"event": "match_ended", "ts": "2026-03-20T21:05:12.345Z", "match_id": "A1B2C3D4", "team1": "Gandalf", "team2": "Merlin", "ended_by": "Gandalf", "duration_s": 423.1}
```

---

## Development

### Type Checking

The package ships a `py.typed` marker. Run `mypy daoc_bot/` to verify.

### Linting

```bash
ruff check daoc_bot/ tests/
ruff format daoc_bot/ tests/
```

### Adding a New Command

1. Add a new `@bot.tree.command` block inside `commands.register()`.
2. Apply `@leader_check` or `@admin_check` as appropriate.
3. Add a corresponding unit test in `tests/unit/test_commands.py`.

---

## License

MIT — see `LICENSE` for details.
