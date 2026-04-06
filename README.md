# DAoC Open Arena — XvX Matchmaking Bot

A Discord bot for managing competitive XvX events in *Dark Age of Camelot*.
Teams register, queue up, get paired by ELO-aware matchmaking, and report results — all through Discord button panels with no manual admin work between matches.

Supports **multiple Discord servers simultaneously**. Each server runs its own independent event with its own config, teams, and match history — all persisted in a shared PostgreSQL database.

---

## Features

- **Multi-guild** — any number of Discord servers can run concurrent events
- **Per-event configuration** — composition type, group sizes, MMR settings, timeouts, all set via `/start_event`
- **Fixed or modular composition** — fixed 3v3/5v5/etc., or modular (any group size within a range)
- **ELO matchmaking** — standard ELO with configurable K-factor; MMR can be disabled per event for casual play
- **MMR relaxation** — after a configurable wait, the MMR threshold lifts so no team waits forever
- **Rematch guard** — teams can't face the same opponent back-to-back; configurable cooldown via `rematch_cooldown_seconds` (`0` = block the immediate next match only, `>0` = enforce a timed window before the pair can meet again)
- **Private control-room threads** — every team leader gets a dedicated thread with a live state panel
- **Acceptance flow** — both leaders must accept within the timeout window or the match cancels without penalty
- **Admin commands** — force-reset teams, cancel matches, override MMR, view leaderboard, end events
- **Structured event logging** — every action is stored as JSONB in PostgreSQL for post-event analytics

---

## Project Structure

```
matchmaking_ladder_bot/
├── daoc_bot/                    # Main package
│   ├── __main__.py              # Bot entry point, on_ready, slash command sync
│   ├── config.py                # Settings loaded from environment variables
│   ├── db.py                    # PostgreSQL connection + schema migrations
│   ├── guild_store.py           # Guild-scoped DB-backed state store + EventConfig
│   ├── models.py                # Domain models: Team, Match, TeamState
│   ├── engine.py                # Matchmaking engine and match lifecycle
│   ├── commands.py              # All slash command definitions
│   ├── embeds.py                # Discord embed builders
│   ├── event_log.py             # Structured event logging → PostgreSQL
│   ├── simulation.py            # In-process test suite (invoked by /run_tests)
│   ├── state.py                 # Legacy in-memory store (simulation use only)
│   └── views/
│       └── team_panel.py        # Context-aware leader control panel + buttons
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_models.py
│   │   ├── test_state.py
│   │   ├── test_engine.py
│   │   ├── test_embeds.py
│   │   └── test_event_log.py
│   └── integration/
│       └── test_matchmaking_flow.py
├── scripts/
│   └── simulate.py              # Standalone offline simulation CLI
├── docs/
│   └── testing_checklist.md     # Manual pre-event testing checklist
├── .env.example                 # Environment variable reference
├── Procfile                     # Railway process type declaration
├── railway.toml                 # Railway build + deploy config
├── pyproject.toml               # Package metadata, deps, tool config
└── requirements.txt             # Runtime dependencies (pip-installable)
```

---

## Commands

### Permission levels

| Level | Who has it |
|---|---|
| **Admin** | Server members with the Discord **Administrator** permission |
| **Team Leader** | Members with the role matching `TEAM_LEADER_ROLE_NAME` (default `Team Leader`). Set `TEAM_LEADER_ROLE_NAME=*` to allow everyone. |
| **Everyone** | No role required |

---

### Event management

| Command | Permission | Description |
|---|---|---|
| `/start_event` | Admin | Opens an interactive setup flow to configure and start a new matchmaking event for this server. Collects composition type (`fixed` or `modular`), group size range (modular only), MMR settings, and channel IDs. A server can only have one active event at a time. In **modular** mode, team leaders must supply a `group_size` when registering — only teams of the same size will be matched together. |
| `/end_event` | Admin | Ends the active event, removes all teams and matches, and resets all state. Cannot be undone. |
| `/event_status` | Everyone | Shows the current event's configuration — composition type, MMR settings, cooldowns, timeouts, and channel IDs. |
| `/set_config` | Admin | Updates one or more config values on the active event without restarting it. All parameters are optional; only supplied values are changed. |
| `/leaderboard` | Admin | Shows the MMR leaderboard for all teams registered in the active event, sorted by MMR. |

**`/set_config` parameters:**

| Parameter | Description |
|---|---|
| `mmr_enabled` | Enable or disable ELO matchmaking (`true`/`false`) |
| `mmr_match_threshold` | Max MMR gap for an instant match (e.g. `200`) |
| `mmr_relax_seconds` | Seconds in queue before the MMR threshold is lifted (e.g. `120`) |
| `mmr_k_value` | ELO K-factor — controls how much MMR changes per match (e.g. `32`) |
| `rematch_cooldown_seconds` | How long two teams are blocked from rematching after playing each other. `0` (default) = block only their immediate next match — the guard lifts once either team plays a third opponent. `>0` = enforce a timed window (e.g. `300` = 5 min); the pair stays blocked regardless of queue size until the timer expires. |
| `match_accept_timeout` | Seconds leaders have to accept a match proposal before it auto-cancels (e.g. `60`) |
| `matchmaking_channel_id` | Channel ID for queue pings and private leader threads |
| `broadcast_channel_id` | Channel ID for match-started and match-ended announcements |

---

### Team commands

| Command | Permission | Description |
|---|---|---|
| `/register_team team_name:<name> [group_size:<n>]` | Team Leader | Registers your team for the active event and creates a private control-room thread with live state buttons. Each Discord account can only lead one team. `group_size` is required when the event is in **modular** mode — it must be a value within the event's configured `min_group_size`–`max_group_size` range. Ignored in fixed mode. |
| `/unregister_team` | Team Leader | Removes your team from the event and deletes your private thread. Cannot be used while a match is active. |
| `/change_group_size group_size:<n>` | Team Leader | Changes your team's group size without re-registering. **Modular events only.** Blocked while a match proposal or active match is in progress. If you are currently queued (READY), you are temporarily removed from the queue, the new size is applied, and you re-enter the queue at the back — your MMR and win/loss record are not affected. |
| `/queue_status` | Team Leader | Shows all teams currently in the ready queue with their wait time. Ephemeral (only you see it). |
| `/match_status` | Team Leader | Shows all matches currently in progress with team names and match IDs. Ephemeral. |

---

### Admin — team & match management

| Command | Permission | Description |
|---|---|---|
| `/admin_list_teams` | Admin | Lists all registered teams with their current state, MMR, wins, losses, and whether they are queued or in a match. |
| `/admin_reset_team groupleader_character_name:<name>` | Admin | Force-resets a team back to IDLE state. Useful if a team gets stuck after a bot restart or a bug. |
| `/admin_remove_team groupleader_character_name:<name>` | Admin | Completely removes a team from the event, cleans up their thread, and removes them from any active match. |
| `/admin_cancel_match match_id:<id>` | Admin | Force-cancels a match by its ID (visible in `/match_status`). Both teams are reset to IDLE with no MMR penalty. |
| `/admin_clear_rematch team1:<name> team2:<name>` | Admin | Manually clears the rematch block between two specific teams so they can be paired again immediately. |
| `/admin_set_mmr groupleader_character_name:<name> mmr:<value>` | Admin | Manually overrides a team's MMR rating. Useful for seeding teams at the start of an event or correcting errors. |

---

### Developer

| Command | Permission | Description |
|---|---|---|
| `/run_tests` | Admin | Runs the full simulation suite (26 scenarios) against the live engine using fake teams in an isolated guild context. Posts a live results embed. Safe to run during an active event — fake teams never clash with real ones. |

---

## Architecture

### Per-guild state machine

Each `Team` moves through four states:

```
IDLE ──(Get a Match)──► READY ──(pair found)──► MATCHED ──(both accept)──► IN_MATCH
  ▲                                                                              │
  └─────────────────────── (match ends / cancelled / timeout) ──────────────────┘
```

### Matchmaking algorithm

`try_match()` scans all valid pairs in the ready queue for a guild and picks the pair with the smallest MMR difference that is **eligible**:

- Eligible immediately if `|mmr_a − mmr_b| ≤ mmr_match_threshold` (default 200)
- Eligible after `mmr_relax_seconds` (default 120 s) regardless of MMR gap
- Pairs blocked by the instant-rematch guard are skipped
- In `modular` mode, only same-`group_size` pairs are considered
- When `mmr_enabled = false`, all MMR checks are bypassed (FIFO pairing)

### ELO formula

```
expected_winner = 1 / (1 + 10^((loser_mmr − winner_mmr) / 400))
delta           = round(K × (1 − expected_winner))      # K default: 32
winner_mmr     += delta
loser_mmr      -= delta
```

### Database schema (PostgreSQL)

| Table | Purpose |
|---|---|
| `events` | One row per guild event; holds all `EventConfig` values |
| `teams` | Teams scoped to an event; persists MMR, state, panel IDs |
| `matches` | Match proposals and results |
| `event_log` | Append-only structured log; `payload` is JSONB |

---

## How to Deploy (Railway)

### 1. Set up the Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**
2. Left sidebar → **Bot** → **Add Bot**
3. Under **Token** → **Reset Token** → copy it (this is `DISCORD_TOKEN`)
4. Enable **Privileged Gateway Intents**:
   - Server Members Intent
   - Message Content Intent
5. Under **Authorization Flow** → turn **Public Bot ON** (allows any server to invite the bot)
6. Left sidebar → **OAuth2 → URL Generator**:
   - Scopes: `bot` + `applications.commands`
   - Permissions: `Send Messages`, `Send Messages in Threads`, `Create Private Threads`, `Manage Threads`, `Read Message History`, `Embed Links`, `View Channels`
7. Copy the generated URL — share it so server admins can invite the bot

### 2. Create the Railway project

1. Push this repo to GitHub
2. Go to [railway.com](https://railway.com) → **New Project**
3. **Add PostgreSQL**: click **+ New** → **Database** → **Add PostgreSQL**
   - Railway injects `DATABASE_URL` automatically into all services in the project — you don't need to set it manually
4. **Add the bot service**: click **+ New** → **GitHub Repo** → select this repo
5. In the bot service → **Variables** tab → add:
   | Variable | Value |
   |---|---|
   | `DISCORD_TOKEN` | your bot token from step 1 |
   | `TEAM_LEADER_ROLE_NAME` | `*` (anyone) or a specific role name |
6. Click **Deploy** — Railway builds via nixpacks and starts `python -m daoc_bot`

On first boot the bot runs schema migrations automatically. No manual database setup needed.

### 3. Per-server setup (for each Discord server that invites the bot)

1. Invite the bot using the URL from step 1.7
2. An admin runs `/start_event` in Discord — this opens an interactive setup flow to configure composition type, MMR settings, and channel IDs for that server's event
3. That's it — the bot is ready to use

### Environment variables reference

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Bot token from the Discord Developer Portal |
| `DATABASE_URL` | ✅ | PostgreSQL connection string — injected automatically by Railway |
| `TEAM_LEADER_ROLE_NAME` | ❌ | Role name that gates leader commands. Use `*` to allow everyone (default: `Team Leader`) |
| `MATCHMAKING_CHANNEL_ID` | ❌ | Default channel ID pre-filled in `/start_event` (single-guild convenience) |
| `BROADCAST_CHANNEL_ID` | ❌ | Default broadcast channel ID pre-filled in `/start_event` |
| `LOG_LEVEL` | ❌ | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default: `INFO`) |

---

## How to Collaborate

### Local development setup

```bash
# 1. Clone the repo
git clone <your-repo-url>
cd matchmaking_ladder_bot

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\activate           # Windows

# 3. Install in editable mode with dev extras
pip install -e ".[dev]"

# 4. Set up environment
cp .env.example .env
# Edit .env — set DISCORD_TOKEN and DATABASE_URL (local Postgres instance)
```

You need a local PostgreSQL instance for development. The bot will create all tables automatically on first run.

### Running the bot locally

```bash
python -m daoc_bot
```

### Running tests

```bash
# All tests
pytest

# Unit tests only
pytest tests/unit/

# Integration tests only
pytest tests/integration/

# With coverage
pytest --cov=daoc_bot --cov-report=term-missing

# Offline simulation (no Discord connection, no database needed)
python scripts/simulate.py                   # all scenarios
python scripts/simulate.py --scenario basic  # single scenario
python scripts/simulate.py --verbose         # dump channel message log
```

### Type checking and linting

```bash
mypy daoc_bot/
ruff check daoc_bot/ tests/
ruff format daoc_bot/ tests/
```

### Adding a slash command

1. Add a `@bot.tree.command` block inside `commands.register()` in [daoc_bot/commands.py](daoc_bot/commands.py)
2. Use the existing `leader_only` / `admin_only` guards at the top of that function
3. Extract `guild_id = interaction.guild_id` and guard with `_no_guild()` / `_no_event()` like the other commands
4. Add a unit test in `tests/unit/`

### Adding a new EventConfig field

1. Add the field to the `EventConfig` dataclass in [daoc_bot/guild_store.py](daoc_bot/guild_store.py)
2. Add the column to the `events` table DDL in [daoc_bot/db.py](daoc_bot/db.py)
3. Update `create_event`, `get_event_config`, and `update_event_config` in `guild_store.py`
4. Expose it in the `/start_event` modal or `/set_config` command in `commands.py`

### Branch and PR conventions

- Branch from `main`: `feature/`, `fix/`, `chore/` prefixes
- Keep PRs focused — one feature or fix per PR
- All tests must pass before merging

### Querying the event log

Every match event is stored as JSONB in the `event_log` table. Example queries:

```sql
-- All matches played in a guild
SELECT ts, payload FROM event_log
WHERE guild_id = 123456789 AND event_type = 'match_ended'
ORDER BY ts;

-- MMR changes for a specific team
SELECT ts, payload FROM event_log
WHERE guild_id = 123456789
  AND event_type = 'mmr_updated'
  AND (payload->>'winner' = 'Gandalf' OR payload->>'loser' = 'Gandalf')
ORDER BY ts;

-- All events for one match
SELECT event_type, ts, payload FROM event_log
WHERE payload->>'match_id' = 'A1B2C3D4'
ORDER BY ts;
```

---

## License

MIT — see `LICENSE` for details.
