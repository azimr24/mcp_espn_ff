# Claude instructions

Use this server only for read-only ESPN Fantasy Football analysis.

- Read credentials from the process environment.
- Never request ESPN cookies in chat.
- Call `get_connection_health` before a private league session.
- Use `get_week_context` for one matchup decision.
- Use `get_player_pool` with a small limit and a position filter.
- Use `refresh_league` only before a time-sensitive decision.
- Never claim that this server uses an official ESPN API.
- Never submit or simulate an ESPN write request.
- Never reveal another team's pending waiver claim.
