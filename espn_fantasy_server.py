import datetime
import logging
import os
import sys
import time
import uuid
import weakref
from collections import OrderedDict
from typing import Any

import anyio
from espn_api.football import League
from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNInvalidLeague
from mcp.server.fastmcp import Context, FastMCP


logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("espn-fantasy-football")

mcp = FastMCP("espn-fantasy-football", dependencies=["espn-api"])

CURRENT_YEAR = datetime.datetime.now().year
if datetime.datetime.now().month < 7:
    CURRENT_YEAR -= 1


class ESPNFantasyFootballAPI:
    """Store credentials and short-lived league objects for each MCP session."""

    def __init__(
        self,
        cache_ttl_seconds: int = 300,
        max_cached_leagues: int = 128,
        max_credential_sessions: int = 32,
    ):
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_cached_leagues = max_cached_leagues
        self.max_credential_sessions = max_credential_sessions
        self.leagues: dict[tuple[str, int, int, int], tuple[float, League]] = {}
        self.credentials: OrderedDict[str, dict[str, str]] = OrderedDict()
        self.credential_versions: dict[str, int] = {}

    def _remove_session_leagues(self, session_id: str) -> None:
        keys = [key for key in self.leagues if key[0] == session_id]
        for key in keys:
            del self.leagues[key]

    def get_league(
        self,
        session_id: str,
        league_id: int,
        year: int = CURRENT_YEAR,
        refresh: bool = False,
    ) -> League:
        credentials = self.credentials.get(session_id)
        if credentials is None:
            env_espn_s2 = os.environ.get("ESPN_S2")
            env_swid = os.environ.get("ESPN_SWID")
            credentials = (
                {"espn_s2": env_espn_s2, "swid": env_swid}
                if env_espn_s2 and env_swid
                else {}
            )
        credential_version = self.credential_versions.get(session_id, 0)
        cache_key = (session_id, league_id, year, credential_version)
        cached = self.leagues.get(cache_key)

        if cached and not refresh:
            cached_at, league = cached
            if time.monotonic() - cached_at < self.cache_ttl_seconds:
                return league

        league = League(
            league_id=league_id,
            year=year,
            espn_s2=credentials.get("espn_s2"),
            swid=credentials.get("swid"),
        )
        if cache_key not in self.leagues and len(self.leagues) >= self.max_cached_leagues:
            oldest_key = min(self.leagues, key=lambda key: self.leagues[key][0])
            del self.leagues[oldest_key]
        self.leagues[cache_key] = (time.monotonic(), league)
        return league

    def store_credentials(self, session_id: str, espn_s2: str, swid: str) -> None:
        self._remove_session_leagues(session_id)
        self.credentials.pop(session_id, None)
        self.credentials[session_id] = {"espn_s2": espn_s2, "swid": swid}
        while len(self.credentials) > self.max_credential_sessions:
            expired_session_id, _ = self.credentials.popitem(last=False)
            self._remove_session_leagues(expired_session_id)
            self.credential_versions.pop(expired_session_id, None)
        self.credential_versions[session_id] = (
            self.credential_versions.get(session_id, 0) + 1
        )

    def clear_credentials(self, session_id: str) -> None:
        self._remove_session_leagues(session_id)
        self.credentials.pop(session_id, None)
        self.credential_versions.pop(session_id, None)


api = ESPNFantasyFootballAPI()
_SESSION_IDS: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()


def _session_id(ctx: Context) -> str:
    """Return an identifier that remains stable for one MCP connection."""
    session = ctx.session
    try:
        session_id = _SESSION_IDS.get(session)
        if session_id is None:
            session_id = uuid.uuid4().hex
            _SESSION_IDS[session] = session_id
        return session_id
    except TypeError:
        session_id = getattr(session, "_espn_session_id", None)
        if session_id is None:
            session_id = uuid.uuid4().hex
            setattr(session, "_espn_session_id", session_id)
        return session_id


async def _get_league(
    ctx: Context,
    league_id: int,
    year: int,
    refresh: bool = False,
) -> League:
    if league_id <= 0:
        raise ValueError("league_id must be positive")
    if year < 2000 or year > CURRENT_YEAR + 1:
        raise ValueError(f"year must be between 2000 and {CURRENT_YEAR + 1}")
    return await anyio.to_thread.run_sync(
        api.get_league, _session_id(ctx), league_id, year, refresh
    )


def _team_by_id(league: League, team_id: int):
    team = league.get_team_data(team_id)
    if team is None:
        valid_ids = [candidate.team_id for candidate in league.teams]
        raise ValueError(f"Unknown team_id {team_id}. Valid IDs: {valid_ids}")
    return team


def _owner_names(owners: list[Any]) -> list[str]:
    names = []
    for owner in owners:
        if isinstance(owner, dict):
            name = owner.get("displayName") or owner.get("firstName")
            names.append(name or "Unknown")
        else:
            names.append(str(owner))
    return names


def _raise_api_error(action: str, error: Exception) -> None:
    if isinstance(error, (ValueError, PermissionError)):
        raise error
    if isinstance(error, ESPNAccessDenied):
        raise PermissionError(
            "ESPN denied access. Authenticate this MCP session for a private league."
        ) from error
    if isinstance(error, ESPNInvalidLeague):
        raise ValueError("ESPN did not find the requested league.") from error
    logger.exception("%s failed", action)
    message = str(error).replace("\n", " ")[:300]
    raise RuntimeError(f"{action} failed: {message}") from error


@mcp.tool()
async def authenticate(espn_s2: str, swid: str, ctx: Context) -> dict[str, bool]:
    """Store ESPN cookies for only the current MCP connection."""
    if not espn_s2.strip() or not swid.strip():
        raise ValueError("Both ESPN cookies are required.")
    api.store_credentials(_session_id(ctx), espn_s2, swid)
    return {"authenticated": True}


@mcp.tool()
async def logout(ctx: Context) -> dict[str, bool]:
    """Remove credentials and cached private data for this MCP connection."""
    api.clear_credentials(_session_id(ctx))
    return {"authenticated": False}


@mcp.tool()
async def refresh_league(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
) -> dict[str, Any]:
    """Refresh cached ESPN data for one league."""
    try:
        league = await _get_league(ctx, league_id, year, refresh=True)
        return {
            "league_id": league.league_id,
            "year": league.year,
            "current_week": league.current_week,
            "refreshed": True,
        }
    except Exception as error:
        _raise_api_error("League refresh", error)


@mcp.tool()
async def get_league_info(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
) -> dict[str, Any]:
    """Get compact league metadata and valid ESPN team IDs."""
    try:
        league = await _get_league(ctx, league_id, year)
        return {
            "league_id": league.league_id,
            "name": league.settings.name,
            "year": league.year,
            "current_week": league.current_week,
            "nfl_week": league.nfl_week,
            "team_count": len(league.teams),
            "teams": [
                {"team_id": team.team_id, "team_name": team.team_name}
                for team in league.teams
            ],
            "scoring_type": league.settings.scoring_type,
        }
    except Exception as error:
        _raise_api_error("League retrieval", error)


@mcp.tool()
async def get_team_roster(
    league_id: int,
    team_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    stats_week: int | None = None,
) -> dict[str, Any]:
    """Get a compact roster. Set stats_week to include statistics for one week."""
    try:
        league = await _get_league(ctx, league_id, year)
        team = _team_by_id(league, team_id)
        if stats_week is not None and not 1 <= stats_week <= league.finalScoringPeriod:
            raise ValueError(
                f"stats_week must be between 1 and {league.finalScoringPeriod}"
            )
        roster = []
        for player in team.roster:
            player_data = {
                "player_id": player.playerId,
                "name": player.name,
                "position": player.position,
                "pro_team": player.proTeam,
                "points": player.total_points,
                "projected_points": player.projected_total_points,
                "injured": player.injured,
            }
            if stats_week is not None:
                player_data["week"] = stats_week
                player_data["week_stats"] = player.stats.get(stats_week, {})
            roster.append(player_data)
        return {
            "team_id": team.team_id,
            "team_name": team.team_name,
            "owners": _owner_names(team.owners),
            "wins": team.wins,
            "losses": team.losses,
            "roster": roster,
        }
    except Exception as error:
        _raise_api_error("Roster retrieval", error)


@mcp.tool()
async def get_team_info(
    league_id: int,
    team_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
) -> dict[str, Any]:
    """Get team results and transaction totals."""
    try:
        league = await _get_league(ctx, league_id, year)
        team = _team_by_id(league, team_id)
        return {
            "team_id": team.team_id,
            "team_name": team.team_name,
            "owners": _owner_names(team.owners),
            "wins": team.wins,
            "losses": team.losses,
            "ties": team.ties,
            "points_for": team.points_for,
            "points_against": team.points_against,
            "acquisitions": team.acquisitions,
            "drops": team.drops,
            "trades": team.trades,
            "playoff_pct": team.playoff_pct,
            "final_standing": team.final_standing,
            "outcomes": team.outcomes,
        }
    except Exception as error:
        _raise_api_error("Team retrieval", error)


@mcp.tool()
async def get_player_stats(
    league_id: int,
    player_name: str,
    ctx: Context,
    year: int = CURRENT_YEAR,
    week: int | None = None,
) -> dict[str, Any]:
    """Get season totals or one week of statistics for one rostered player."""
    try:
        league = await _get_league(ctx, league_id, year)
        players = [player for team in league.teams for player in team.roster]
        exact = [
            player
            for player in players
            if player.name.casefold() == player_name.casefold()
        ]
        matches = exact or [
            player
            for player in players
            if player_name.casefold() in player.name.casefold()
        ]
        if not matches:
            raise ValueError(f"Player '{player_name}' is not on a league roster.")
        if len(matches) > 1:
            names = sorted({player.name for player in matches})
            raise ValueError(f"Player name is ambiguous. Matches: {names}")

        player = matches[0]
        if week is not None and not 1 <= week <= league.finalScoringPeriod:
            raise ValueError(f"week must be between 1 and {league.finalScoringPeriod}")
        result = {
            "player_id": player.playerId,
            "name": player.name,
            "position": player.position,
            "pro_team": player.proTeam,
            "points": player.total_points,
            "projected_points": player.projected_total_points,
            "injured": player.injured,
        }
        if week is not None:
            result["week"] = week
            result["week_stats"] = player.stats.get(week, {})
        return result
    except Exception as error:
        _raise_api_error("Player retrieval", error)


@mcp.tool()
async def get_league_standings(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
) -> list[dict[str, Any]]:
    """Get compact league standings."""
    try:
        league = await _get_league(ctx, league_id, year)
        sorted_teams = sorted(
            league.teams, key=lambda team: (team.wins, team.points_for), reverse=True
        )
        return [
            {
                "rank": rank,
                "team_id": team.team_id,
                "team_name": team.team_name,
                "owners": _owner_names(team.owners),
                "wins": team.wins,
                "losses": team.losses,
                "ties": team.ties,
                "points_for": team.points_for,
                "points_against": team.points_against,
            }
            for rank, team in enumerate(sorted_teams, start=1)
        ]
    except Exception as error:
        _raise_api_error("Standings retrieval", error)


@mcp.tool()
async def get_matchup_info(
    league_id: int,
    ctx: Context,
    week: int | None = None,
    year: int = CURRENT_YEAR,
) -> list[dict[str, Any]]:
    """Get compact matchup results for one valid league week."""
    try:
        league = await _get_league(ctx, league_id, year)
        selected_week = week or league.current_week
        final_week = league.finalScoringPeriod
        if selected_week < 1 or selected_week > final_week:
            raise ValueError(f"week must be between 1 and {final_week}")
        matchups = await anyio.to_thread.run_sync(league.box_scores, selected_week)
        return [
            {
                "week": selected_week,
                "home_team_id": matchup.home_team.team_id,
                "home_team": matchup.home_team.team_name,
                "home_score": matchup.home_score,
                "away_team_id": (
                    matchup.away_team.team_id if matchup.away_team else None
                ),
                "away_team": (
                    matchup.away_team.team_name if matchup.away_team else "BYE"
                ),
                "away_score": matchup.away_score if matchup.away_team else 0,
                "winner": (
                    "HOME"
                    if matchup.home_score > matchup.away_score
                    else "AWAY"
                    if matchup.away_score > matchup.home_score
                    else "TIE"
                ),
            }
            for matchup in matchups
        ]
    except Exception as error:
        _raise_api_error("Matchup retrieval", error)


if __name__ == "__main__":
    logger.info("Starting ESPN fantasy football MCP server for season %s", CURRENT_YEAR)
    mcp.run()
