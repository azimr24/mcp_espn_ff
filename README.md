# ESPN Fantasy Football MCP Server

## Overview

This MCP server lets language models use ESPN Fantasy Football data. It provides
league, roster, player, matchup, draft, and guarded team-management tools. It works
with public and private ESPN leagues.

## Features (MCP Tools)

- **Authentication**: Use process environment credentials or store session credentials
- **League Info**: Get basic information about fantasy football leagues
- **Team Rosters**: View current team rosters and player details
- **Player Stats**: Find and display stats for specific players
- **League Standings**: View current team rankings and performance metrics
- **Matchup Information**: Get details about weekly matchups
- **Refresh**: Replace cached league data on demand
- **Draft State**: Get the live draft order, current pick, and last completed pick
- **Draft Picks**: Get completed or scheduled picks with pagination and team filters
- **Draft Pool**: Get available players in compact ESPN PPR order
- **Draft Context**: Get recent picks, team picks, and top players together
- **Draft Refresh**: Refresh draft data without rebuilding unrelated league data
- **Live Draft Pick**: Preview and submit one snake or linear draft selection
- **Free-Agent Move**: Preview and execute one atomic player add and drop
- **Lineup Swap**: Preview and execute one starter and bench swap

Tools return structured JSON-compatible data. Roster results omit large weekly stat
maps unless the caller requests one week with `stats_week`.

League objects expire after five minutes. The `logout` tool removes credentials and
all cached private league objects for the current connection.

Draft reads cannot change draft settings. The two draft-pick tools can submit one
live selection. The server does not expose waiver or trade writes because their
request formats lack sufficient confirmation.

Draft state uses a two-second cache. Call `refresh_draft` before a time-sensitive
recommendation. Use `get_draft_context` to avoid separate pick and player calls.

For private leagues, set both `ESPN_S2` and `ESPN_SWID` in the server process.
The environment option keeps cookie values outside MCP tool arguments and chat logs.
Session credentials from the `authenticate` tool override environment credentials.

### Write safeguards

Write tools use ESPN's undocumented transaction endpoint. ESPN can change this
endpoint without notice.

The server disables all writes by default. Set `ESPN_WRITE_ENABLED=true` in the
server process to enable writes. Keep this value `false` when you only need reads.

Live draft picks need a second flag. Set `ESPN_DRAFT_WRITE_ENABLED=true` only for
the draft. Turn this flag off after the draft.

Each write needs two tool calls:

1. Call the matching `preview_*` tool.
2. Review the returned team, players, period, and action.
3. Pass the returned token to the matching `execute_*` tool within 120 seconds.

Use `preview_draft_pick` and `execute_draft_pick` for each live pick. The server
confirms the current team, pick number, player availability, draft type, and team
ownership before each submission. The draft tool supports snake and linear drafts.
The draft tool does not support salary-cap drafts.

The server binds each token to one exact action. The server accepts each token once.
The server clears tokens after logout or credential changes. A successful write
also clears cached league and draft data.

The live draft tool follows ESPN's current web draft protocol. ESPN does not publish
this protocol. The test suite never sends a real draft selection.

Copy `.env.example` to `.env` for local setup. Never commit real ESPN cookies.

### Draft conductor

The draft conductor keeps one live monitor active during a draft. The conductor
combines ESPN draft-room events with one-second REST checks. The conductor stores
a recovery snapshot and an append-only pick ledger in `.draft-state/`.

Start the conductor before the first pick. The ESPN stream can reject connections
before the draft room opens. The conductor retries the connection automatically.

```bash
ESPN_WRITE_ENABLED=true ESPN_DRAFT_WRITE_ENABLED=true \
uv run python scripts/draft_conductor.py \
  --league-id 364366361 \
  --team-id 3 \
  --year 2026 \
  --env-file /absolute/path/to/espn.env
```

The conductor prints compact JSON events. Send one JSON command per input line.

```json
{"command":"state","request_id":"state-1"}
{"command":"pool","limit":15,"position":"WR","request_id":"pool-1"}
{"command":"preview_pick","player_id":12345,"request_id":"preview-1"}
{"command":"submit_pick","player_id":12345,"confirmation_token":"TOKEN"}
{"command":"quit"}
```

The `preview_pick` command returns a token that expires after 120 seconds. The
`submit_pick` command rebuilds and confirms the action before ESPN receives it.
The command fails if the team is not on the clock or the player is unavailable.

Use `--once` for one read-only startup check. Use `--no-stream` only when the ESPN
draft stream is unavailable. The REST checks remain active in that mode.

The conductor does not decode ESPN's `INIT` recovery payload. Start the conductor
before pick one for complete stream recovery. REST snapshots still repair known
picks when ESPN includes those picks in the draft response.

## Installation

### Prerequisites

- Python 3.12 or higher
- `uv` package manager
- [Claude Desktop](https://claude.ai/download) for the best experience

### Usage with Claude Desktop

1. Update the Claude Desktop config:
- MacOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Include reference to the MCP server
  ```json
  {
  "mcpServers": {
    "espn-fantasy-football": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/directory",
        "run",
        "espn_fantasy_server.py"
        ]
      }
    } 
  }
2. Restart Claude Desktop


## Acknowledgements

[cwendt94/espn-api](https://github.com/cwendt94/espn-api) provides the Python ESPN
API wrapper.
