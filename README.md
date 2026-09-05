# ESPN Fantasy Football MCP Server

## Overview

This MCP (Model Context Protocol) server allows LLMs like Claude to interact with the ESPN Fantasy Football API. It provides tools for accessing league data, team rosters, player statistics, and more through a standardized interface. It can work with both public and private ESPN Leagues.

## Features (MCP Tools)

- **Authentication**: Store ESPN credentials for only the current MCP connection
- **League Info**: Get basic information about fantasy football leagues
- **Team Rosters**: View current team rosters and player details
- **Player Stats**: Find and display stats for specific players
- **League Standings**: View current team rankings and performance metrics
- **Matchup Information**: Get details about weekly matchups
- **Refresh**: Replace cached league data on demand

Tools return structured JSON-compatible data. Roster results omit large weekly stat
maps unless the caller requests one week with `stats_week`.

League objects expire after five minutes. The `logout` tool removes credentials and
all cached private league objects for the current connection.

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

[cwendt94/espn-api](https://github.com/cwendt94/espn-api) for the nifty python wrapper around the ESPN Fantasy API
