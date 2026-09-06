# ESPN Fantasy Football Read MCP

This MCP server returns compact ESPN Fantasy Football data for analysis.

ESPN does not publish the Fantasy endpoints used by this project. The server is
read-only. It never changes a draft, roster, lineup, waiver claim, or trade.

## Tools

- `get_connection_health`: Check local credentials and optional live access.
- `refresh_league`: Clear one cache scope.
- `get_league_info`: Get league identity and team IDs.
- `get_league_settings`: Get roster, waiver, trade, schedule, playoff, and scoring rules.
- `get_team_roster`: Get a compact roster and optional weekly projections.
- `get_league_standings`: Get standings, waiver ranks, and transaction totals.
- `get_week_context`: Get both lineups, game times, locks, injuries, and projections.
- `get_player_pool`: Get paged free-agent and waiver candidates.
- `get_players`: Get selected players by ESPN ID or exact name.
- `get_activity`: Get incremental league activity through a stable cursor.
- `get_transactions`: Get completed, failed, and pending transactions.
- `get_waiver_report`: Get public results and only the managed team's pending claims.
- `get_schedule`: Get one week of fantasy matchups and NFL kickoff times.

Every data response includes a UTC capture time, a source label, and freshness.
Default responses omit large raw stat maps. Player lists use pagination.

## Credentials

Set the two private ESPN cookies in the server process:

```text
ESPN_S2=...
ESPN_SWID=...
```

Do not pass cookies through tool arguments. Do not commit cookie files.

The `get_connection_health` tool reports only whether credentials exist and work.
It never returns cookie values.

## Cache scopes

`refresh_league` accepts `league`, `settings`, `week`, `pool`, `activity`,
`transactions`, or `all`.

The league object cache lasts five minutes. Small view caches last one minute.

## Run

```sh
uv sync
uv run python espn_fantasy_server.py
```

## Test

```sh
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

The tests never send ESPN write requests.

## Terms and fair play

Review the Disney Terms of Use before programmatic access. Get written permission
before unattended ESPN access. Follow ESPN fair-play rules for every recommendation.

The server hides pending waiver claims from other teams. The server never controls
another team.

## Dependency

The project pins the maintained API fork to a confirmed Git commit. The lock file
records the same commit for repeatable installs.
