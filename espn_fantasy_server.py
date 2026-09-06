"""Read-only ESPN Fantasy Football MCP server.

ESPN does not publish the Fantasy endpoints used by this project. This server
therefore exposes reads only. It never submits roster, waiver, trade, lineup,
or draft changes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.metadata
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
import requests
from espn_api.football import League
from espn_api.football.constant import POSITION_MAP, PRO_TEAM_MAP
from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNInvalidLeague
from mcp.server.fastmcp import Context, FastMCP

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("espn-fantasy-football")
mcp = FastMCP("espn-fantasy-football", dependencies=["espn-api"])

_CURRENT_DATE = dt.datetime.now(dt.UTC)
CURRENT_YEAR = _CURRENT_DATE.year
if _CURRENT_DATE.month < 7:
    CURRENT_YEAR -= 1

SOURCE_NAME = "ESPN_UNOFFICIAL"
DEFAULT_CACHE_TTL_SECONDS = 300
VIEW_CACHE_TTL_SECONDS = 60
MAX_CACHE_ENTRIES = 128
MAX_PAGE_SIZE = 100
PRIMARY_POSITION_MAP = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
TRANSACTION_TYPES = {
    "FREEAGENT",
    "ROSTER",
    "TRADE_ACCEPT",
    "TRADE_DECLINE",
    "TRADE_ERROR",
    "TRADE_PROPOSAL",
    "TRADE_UPHOLD",
    "TRADE_VETO",
    "WAIVER",
    "WAIVER_ERROR",
}


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso_from_millis(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return dt.datetime.fromtimestamp(value / 1000, dt.UTC).isoformat()


def _validate_identity(league_id: int, year: int) -> None:
    if league_id <= 0:
        raise ValueError("league_id must be positive")
    if year < 2000 or year > CURRENT_YEAR + 1:
        raise ValueError(f"year must be between 2000 and {CURRENT_YEAR + 1}")


def _validate_page(limit: int, offset: int) -> None:
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if offset < 0:
        raise ValueError("offset must be zero or positive")


def _credentials() -> tuple[str, str]:
    espn_s2 = os.environ.get("ESPN_S2", "").strip()
    swid = os.environ.get("ESPN_SWID", "").strip()
    if not espn_s2 or not swid:
        raise PermissionError(
            "Private ESPN credentials are missing from the server environment."
        )
    return espn_s2, swid


@dataclass
class CacheEntry:
    captured_at: float
    value: Any


class ESPNReadAPI:
    """Cache league objects and small ESPN views without storing credentials."""

    def __init__(self) -> None:
        self.leagues: dict[tuple[int, int], CacheEntry] = {}
        self.views: dict[tuple[int, int, str], CacheEntry] = {}
        self.last_success_at: str | None = None

    @staticmethod
    def _fresh(entry: CacheEntry, ttl: int) -> bool:
        return time.monotonic() - entry.captured_at < ttl

    @staticmethod
    def _trim(cache: dict[Any, CacheEntry]) -> None:
        while len(cache) > MAX_CACHE_ENTRIES:
            oldest = min(cache, key=lambda key: cache[key].captured_at)
            del cache[oldest]

    def get_league(self, league_id: int, year: int, refresh: bool = False) -> League:
        _validate_identity(league_id, year)
        key = (league_id, year)
        cached = self.leagues.get(key)
        if cached and not refresh and self._fresh(cached, DEFAULT_CACHE_TTL_SECONDS):
            return cached.value
        espn_s2, swid = _credentials()
        league = League(
            league_id=league_id,
            year=year,
            espn_s2=espn_s2,
            swid=swid,
        )
        self.leagues[key] = CacheEntry(time.monotonic(), league)
        self._trim(self.leagues)
        self.last_success_at = _utc_now().isoformat()
        return league

    def get_view(
        self,
        league: League,
        cache_name: str,
        loader: Callable[[], Any],
        refresh: bool = False,
        ttl: int = VIEW_CACHE_TTL_SECONDS,
    ) -> Any:
        key = (league.league_id, league.year, cache_name)
        cached = self.views.get(key)
        if cached and not refresh and self._fresh(cached, ttl):
            return cached.value
        value = loader()
        self.views[key] = CacheEntry(time.monotonic(), value)
        self._trim(self.views)
        self.last_success_at = _utc_now().isoformat()
        return value

    def invalidate(self, league_id: int, year: int, scope: str = "all") -> int:
        prefixes = {
            "settings": ("settings",),
            "week": ("box_scores:", "scoreboard:"),
            "pool": ("pool:", "players:"),
            "activity": ("activity:",),
            "transactions": ("transactions:", "waivers:"),
            "all": ("",),
            "league": ("",),
        }
        if scope not in prefixes:
            raise ValueError(f"scope must be one of {sorted(prefixes)}")
        removed = 0
        if scope in {"all", "league"} and self.leagues.pop((league_id, year), None):
            removed += 1
        for key in list(self.views):
            if key[:2] == (league_id, year) and any(
                key[2].startswith(prefix) for prefix in prefixes[scope]
            ):
                del self.views[key]
                removed += 1
        return removed


api = ESPNReadAPI()


def _meta(captured_at: str | None = None) -> dict[str, Any]:
    now = _utc_now()
    captured = captured_at or now.isoformat()
    try:
        age = max(0.0, (now - dt.datetime.fromisoformat(captured)).total_seconds())
    except ValueError:
        age = 0.0
    return {
        "captured_at": captured,
        "source": SOURCE_NAME,
        "freshness_seconds": round(age, 3),
        "freshness_status": "FRESH" if age <= DEFAULT_CACHE_TTL_SECONDS else "STALE",
    }


async def _get_league(league_id: int, year: int, refresh: bool = False) -> League:
    return await anyio.to_thread.run_sync(api.get_league, league_id, year, refresh)


def _team_by_id(league: League, team_id: int) -> Any:
    team = league.get_team_data(team_id)
    if team is None:
        valid_ids = [candidate.team_id for candidate in league.teams]
        raise ValueError(f"Unknown team_id {team_id}. Valid IDs: {valid_ids}")
    return team


def _owner_names(owners: list[Any]) -> list[str]:
    names: list[str] = []
    for owner in owners:
        if isinstance(owner, dict):
            names.append(
                owner.get("displayName") or owner.get("firstName") or "Unknown"
            )
        else:
            names.append(str(owner))
    return names


def _player_name(league: League, player_id: Any) -> str | None:
    return league.player_map.get(player_id) if isinstance(player_id, int) else None


def _projected_points(player: dict[str, Any], year: int, week: int) -> float | None:
    split_type = 0 if week == 0 else 1
    for stats in player.get("stats", []):
        if (
            stats.get("seasonId") == year
            and stats.get("statSourceId") == 1
            and stats.get("statSplitTypeId") == split_type
            and stats.get("scoringPeriodId") == week
        ):
            return round(float(stats.get("appliedTotal") or 0.0), 2)
    return None


def _actual_points(player: dict[str, Any], year: int, week: int) -> float | None:
    for stats in player.get("stats", []):
        if (
            stats.get("seasonId") == year
            and stats.get("statSourceId") == 0
            and stats.get("scoringPeriodId") == week
        ):
            return round(float(stats.get("appliedTotal") or 0.0), 2)
    return None


def _raw_player(entry: dict[str, Any], year: int, week: int) -> dict[str, Any]:
    player = entry.get("player", entry)
    ownership = player.get("ownership", {})
    eligible = [
        POSITION_MAP.get(slot_id, str(slot_id))
        for slot_id in player.get("eligibleSlots", [])
        if slot_id != 25
    ]
    return {
        "player_id": player.get("id"),
        "name": player.get("fullName"),
        "position": PRIMARY_POSITION_MAP.get(player.get("defaultPositionId")),
        "eligible_slots": eligible,
        "pro_team": PRO_TEAM_MAP.get(player.get("proTeamId"), "FA"),
        "availability": entry.get("status"),
        "waiver_process_at": _iso_from_millis(entry.get("waiverProcessDate")),
        "lineup_locked": bool(entry.get("lineupLocked", False)),
        "trade_locked": bool(entry.get("tradeLocked", False)),
        "undroppable": bool(player.get("droppable") is False),
        "injury_status": player.get("injuryStatus", "UNKNOWN"),
        "injured": bool(player.get("injured", False)),
        "percent_owned": round(float(ownership.get("percentOwned") or 0.0), 2),
        "percent_started": round(float(ownership.get("percentStarted") or 0.0), 2),
        "projected_points": _projected_points(player, year, week),
        "season_projected_points": _projected_points(player, year, 0),
        "actual_points": _actual_points(player, year, week),
    }


def _box_player(player: Any, include_eligible_slots: bool = False) -> dict[str, Any]:
    game_date = getattr(player, "game_date", None)
    if isinstance(game_date, dt.datetime) and game_date.tzinfo is None:
        game_date = game_date.astimezone().astimezone(dt.UTC)
    row = {
        "player_id": player.playerId,
        "name": player.name,
        "position": player.position,
        "pro_team": player.proTeam,
        "lineup_slot": player.slot_position,
        "projected_points": player.projected_points,
        "actual_points": player.points,
        "injury_status": player.injuryStatus,
        "injured": bool(player.injured),
        "opponent": player.pro_opponent,
        "opponent_position_rank": player.pro_pos_rank,
        "game_time": game_date.isoformat() if game_date else None,
        "locked": bool(getattr(player, "game_played", 0)),
        "on_bye": bool(player.on_bye_week),
    }
    if include_eligible_slots:
        row["eligible_slots"] = player.eligibleSlots
    return row


def _transaction_item(item: dict[str, Any], league: League) -> dict[str, Any]:
    player_id = item.get("playerId")
    return {
        "type": item.get("type"),
        "player_id": player_id,
        "player_name": _player_name(league, player_id),
        "from_team_id": item.get("fromTeamId"),
        "to_team_id": item.get("toTeamId"),
    }


def _transaction(transaction: dict[str, Any], league: League) -> dict[str, Any]:
    return {
        "transaction_id": transaction.get("id"),
        "type": transaction.get("type"),
        "status": transaction.get("status"),
        "team_id": transaction.get("teamId"),
        "scoring_period": transaction.get("scoringPeriodId"),
        "proposed_at": _iso_from_millis(transaction.get("proposedDate")),
        "process_at": _iso_from_millis(transaction.get("processDate")),
        "bid_amount": transaction.get("bidAmount"),
        "items": [
            _transaction_item(item, league) for item in transaction.get("items", [])
        ],
    }


def _cursor(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _raise_api_error(action: str, error: Exception) -> None:
    if isinstance(error, (ValueError, PermissionError)):
        raise error
    if isinstance(error, ESPNAccessDenied):
        raise PermissionError(
            "ESPN denied access. Replace the local ESPN cookies."
        ) from error
    if isinstance(error, ESPNInvalidLeague):
        raise ValueError("ESPN did not find the requested league.") from error
    if isinstance(error, requests.RequestException):
        logger.error("%s request failed: %s", action, type(error).__name__)
        raise RuntimeError(
            f"{action} failed because the ESPN request did not complete."
        ) from error
    logger.error("%s failed: %s", action, type(error).__name__)
    raise RuntimeError(f"{action} failed with {type(error).__name__}.") from error


@mcp.tool()
async def get_connection_health(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    live_check: bool = True,
) -> dict[str, Any]:
    """Check credentials and optional live read access without returning secrets."""
    del ctx
    _validate_identity(league_id, year)
    credentials_present = bool(
        os.environ.get("ESPN_S2") and os.environ.get("ESPN_SWID")
    )
    accessible = False
    error: str | None = None
    if live_check and credentials_present:
        try:
            league = await _get_league(league_id, year, refresh=True)
            accessible = league.league_id == league_id
        except Exception as caught:
            error = type(caught).__name__
    packages: dict[str, str] = {}
    for package in ("espn-api", "mcp", "requests"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = "unknown"
    return {
        **_meta(),
        "league_id": league_id,
        "year": year,
        "credentials_present": credentials_present,
        "live_check": live_check,
        "accessible": accessible if live_check else None,
        "error_type": error,
        "last_success_at": api.last_success_at,
        "read_only": True,
        "packages": packages,
    }


@mcp.tool()
async def refresh_league(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    scope: str = "all",
) -> dict[str, Any]:
    """Clear one cached ESPN data scope. This tool does not submit changes."""
    del ctx
    _validate_identity(league_id, year)
    removed = api.invalidate(league_id, year, scope)
    league = await _get_league(league_id, year, refresh=scope in {"all", "league"})
    return {
        **_meta(),
        "league_id": league_id,
        "year": year,
        "scope": scope,
        "cache_entries_removed": removed,
        "current_week": league.current_week,
    }


@mcp.tool()
async def get_league_info(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
) -> dict[str, Any]:
    """Get compact league identity and team data."""
    del ctx
    try:
        league = await _get_league(league_id, year)
        return {
            **_meta(api.last_success_at),
            "league_id": league.league_id,
            "name": league.settings.name,
            "year": league.year,
            "current_week": league.current_week,
            "nfl_week": league.nfl_week,
            "final_scoring_period": league.finalScoringPeriod,
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
async def get_league_settings(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
) -> dict[str, Any]:
    """Get roster, waiver, trade, schedule, playoff, and scoring rules."""
    del ctx
    try:
        league = await _get_league(league_id, year)
        raw = await anyio.to_thread.run_sync(
            api.get_view,
            league,
            "settings",
            lambda: league.espn_request.league_get(
                params={"view": ["mSettings", "mStatus"]}
            ),
        )
        settings = raw.get("settings", {})
        status = raw.get("status", {})
        scoring = [
            {"stat_id": item.get("statId"), "points": item.get("points", 0)}
            for item in settings.get("scoringSettings", {}).get("scoringItems", [])
            if item.get("points", 0) != 0
        ]
        return {
            **_meta(api.last_success_at),
            "league_id": league_id,
            "year": year,
            "status": {
                key: status.get(key)
                for key in (
                    "currentMatchupPeriod",
                    "firstScoringPeriod",
                    "finalScoringPeriod",
                    "isActive",
                    "latestScoringPeriod",
                    "transactionScoringPeriod",
                    "waiverLastExecutionDate",
                )
            },
            "roster": settings.get("rosterSettings", {}),
            "acquisition": settings.get("acquisitionSettings", {}),
            "trade": settings.get("tradeSettings", {}),
            "schedule": settings.get("scheduleSettings", {}),
            "scoring": scoring,
        }
    except Exception as error:
        _raise_api_error("League settings retrieval", error)


@mcp.tool()
async def get_team_roster(
    league_id: int,
    team_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    stats_week: int | None = None,
) -> dict[str, Any]:
    """Get a compact roster. Add stats_week for one week of statistics."""
    del ctx
    try:
        league = await _get_league(league_id, year)
        team = _team_by_id(league, team_id)
        if stats_week is not None and not 1 <= stats_week <= league.finalScoringPeriod:
            raise ValueError(
                f"stats_week must be between 1 and {league.finalScoringPeriod}"
            )
        roster = []
        for player in team.roster:
            row = {
                "player_id": player.playerId,
                "name": player.name,
                "position": player.position,
                "pro_team": player.proTeam,
                "lineup_slot": player.lineupSlot,
                "eligible_slots": player.eligibleSlots,
                "injury_status": player.injuryStatus,
                "injured": bool(player.injured),
                "undroppable": bool(getattr(player, "undroppable", False)),
                "season_points": player.total_points,
                "season_projected_points": player.projected_total_points,
            }
            if stats_week is not None:
                stats = player.stats.get(stats_week, {})
                row.update(
                    week=stats_week,
                    projected_points=stats.get("projected_points"),
                    actual_points=stats.get("points"),
                )
            roster.append(row)
        return {
            **_meta(api.last_success_at),
            "team_id": team.team_id,
            "team_name": team.team_name,
            "owners": _owner_names(team.owners),
            "wins": team.wins,
            "losses": team.losses,
            "waiver_rank": team.waiver_rank,
            "roster": roster,
        }
    except Exception as error:
        _raise_api_error("Roster retrieval", error)


@mcp.tool()
async def get_league_standings(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
) -> dict[str, Any]:
    """Get compact standings with waiver rank and transaction totals."""
    del ctx
    try:
        league = await _get_league(league_id, year)
        teams = sorted(league.teams, key=lambda team: team.standing)
        return {
            **_meta(api.last_success_at),
            "standings": [
                {
                    "rank": team.standing,
                    "team_id": team.team_id,
                    "team_name": team.team_name,
                    "wins": team.wins,
                    "losses": team.losses,
                    "ties": team.ties,
                    "points_for": team.points_for,
                    "points_against": team.points_against,
                    "waiver_rank": team.waiver_rank,
                    "acquisitions": team.acquisitions,
                    "drops": team.drops,
                    "trades": team.trades,
                    "playoff_pct": team.playoff_pct,
                }
                for team in teams
            ],
        }
    except Exception as error:
        _raise_api_error("Standings retrieval", error)


@mcp.tool()
async def get_week_context(
    league_id: int,
    team_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    week: int | None = None,
) -> dict[str, Any]:
    """Get both lineups, weekly projections, game times, locks, and injuries."""
    del ctx
    try:
        league = await _get_league(league_id, year)
        team = _team_by_id(league, team_id)
        selected_week = week or league.current_week
        if selected_week < 1 or selected_week > league.finalScoringPeriod:
            raise ValueError(f"week must be between 1 and {league.finalScoringPeriod}")
        boxes = await anyio.to_thread.run_sync(
            api.get_view,
            league,
            f"box_scores:{selected_week}",
            lambda: league.box_scores(selected_week),
        )
        matchup = next(
            (
                box
                for box in boxes
                if getattr(box.home_team, "team_id", None) == team_id
                or getattr(box.away_team, "team_id", None) == team_id
            ),
            None,
        )
        if matchup is None:
            raise ValueError(f"Team {team_id} has no matchup in week {selected_week}.")
        home = matchup.home_team
        away = matchup.away_team
        return {
            **_meta(api.last_success_at),
            "week": selected_week,
            "managed_team_id": team.team_id,
            "home": {
                "team_id": home.team_id if home else None,
                "team_name": home.team_name if home else "BYE",
                "score": matchup.home_score,
                "projected_score": matchup.home_projected,
                "lineup": [
                    _box_player(player, home is not None and home.team_id == team_id)
                    for player in matchup.home_lineup
                ],
            },
            "away": {
                "team_id": away.team_id if away else None,
                "team_name": away.team_name if away else "BYE",
                "score": matchup.away_score,
                "projected_score": matchup.away_projected,
                "lineup": [
                    _box_player(player, away is not None and away.team_id == team_id)
                    for player in matchup.away_lineup
                ],
            },
        }
    except Exception as error:
        _raise_api_error("Week context retrieval", error)


@mcp.tool()
async def get_player_pool(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    week: int | None = None,
    limit: int = 25,
    offset: int = 0,
    position: str | None = None,
    availability: str = "ALL",
) -> dict[str, Any]:
    """Get a compact page of free agents and waiver players."""
    del ctx
    try:
        _validate_page(limit, offset)
        league = await _get_league(league_id, year)
        selected_week = week or league.current_week
        position = position.upper() if position else None
        if position and position not in {"QB", "RB", "WR", "TE", "D/ST", "K"}:
            raise ValueError("position must be QB, RB, WR, TE, D/ST, or K")
        availability = availability.upper()
        statuses = {
            "ALL": ["FREEAGENT", "WAIVERS"],
            "FREEAGENT": ["FREEAGENT"],
            "WAIVERS": ["WAIVERS"],
        }.get(availability)
        if statuses is None:
            raise ValueError("availability must be ALL, FREEAGENT, or WAIVERS")
        filters: dict[str, Any] = {
            "players": {
                "filterStatus": {"value": statuses},
                "filterSlotIds": {
                    "value": [POSITION_MAP[position]] if position else []
                },
                "limit": limit,
                "offset": offset,
                "sortAppliedStatTotal": {
                    "sortPriority": 1,
                    "sortAsc": False,
                    "value": f"{selected_week}:1",
                },
                "sortPercOwned": {"sortPriority": 2, "sortAsc": False},
            }
        }
        cache_name = f"pool:{selected_week}:{position}:{availability}:{limit}:{offset}"
        response = await anyio.to_thread.run_sync(
            api.get_view,
            league,
            cache_name,
            lambda: league.espn_request.league_get(
                params={"view": "kona_player_info", "scoringPeriodId": selected_week},
                headers={"x-fantasy-filter": json.dumps(filters)},
            ),
        )
        players = [
            _raw_player(entry, year, selected_week)
            for entry in response.get("players", [])
        ]
        return {
            **_meta(api.last_success_at),
            "week": selected_week,
            "availability": availability,
            "position": position,
            "offset": offset,
            "count": len(players),
            "has_more": len(players) == limit,
            "players": players,
        }
    except Exception as error:
        _raise_api_error("Player pool retrieval", error)


@mcp.tool()
async def get_players(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    week: int | None = None,
    player_ids: list[int] | None = None,
    exact_name: str | None = None,
) -> dict[str, Any]:
    """Get rostered or available players by ESPN ID or exact name."""
    del ctx
    try:
        league = await _get_league(league_id, year)
        selected_week = week or league.current_week
        ids = list(dict.fromkeys(player_ids or []))
        if exact_name:
            matched = league.player_map.get(exact_name)
            if not isinstance(matched, int):
                raise ValueError(
                    f"ESPN did not find the exact player name '{exact_name}'."
                )
            ids.append(matched)
        ids = list(dict.fromkeys(ids))
        if not ids or len(ids) > 50 or any(player_id <= 0 for player_id in ids):
            raise ValueError(
                "Provide between one and 50 positive player IDs or one exact name."
            )
        filters = {"players": {"filterIds": {"value": ids}, "limit": len(ids)}}
        response = await anyio.to_thread.run_sync(
            api.get_view,
            league,
            f"players:{selected_week}:{','.join(map(str, sorted(ids)))}",
            lambda: league.espn_request.league_get(
                params={"view": "kona_player_info", "scoringPeriodId": selected_week},
                headers={"x-fantasy-filter": json.dumps(filters)},
            ),
        )
        return {
            **_meta(api.last_success_at),
            "week": selected_week,
            "players": [
                _raw_player(entry, year, selected_week)
                for entry in response.get("players", [])
            ],
        }
    except Exception as error:
        _raise_api_error("Player retrieval", error)


@mcp.tool()
async def get_activity(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    limit: int = 25,
    offset: int = 0,
    after_cursor: str | None = None,
) -> dict[str, Any]:
    """Get compact league add, drop, waiver, and trade activity."""
    del ctx
    try:
        _validate_page(limit, offset)
        league = await _get_league(league_id, year)
        activities = await anyio.to_thread.run_sync(
            api.get_view,
            league,
            f"activity:{limit}:{offset}",
            lambda: league.recent_activity(size=limit, offset=offset),
        )
        rows: list[dict[str, Any]] = []
        for activity in activities:
            for index, action in enumerate(activity.actions):
                team, action_name, player, bid_amount = action
                player_id = getattr(
                    player, "playerId", player if isinstance(player, int) else None
                )
                stable = f"{activity.date}:{index}:{getattr(team, 'team_id', None)}:{action_name}:{player_id}"
                rows.append(
                    {
                        "cursor": _cursor(stable),
                        "occurred_at": _iso_from_millis(activity.date),
                        "team_id": getattr(team, "team_id", None),
                        "team_name": getattr(team, "team_name", None),
                        "action": action_name,
                        "player_id": player_id,
                        "player_name": getattr(
                            player, "name", _player_name(league, player_id)
                        ),
                        "bid_amount": bid_amount or None,
                    }
                )
        if after_cursor:
            cursor_index = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row["cursor"] == after_cursor
                ),
                None,
            )
            if cursor_index is not None:
                rows = rows[:cursor_index]
        return {
            **_meta(api.last_success_at),
            "offset": offset,
            "count": len(rows),
            "has_more": len(activities) == limit,
            "next_offset": offset + len(activities),
            "next_cursor": rows[0]["cursor"] if rows else after_cursor,
            "activity": rows,
        }
    except Exception as error:
        _raise_api_error("Activity retrieval", error)


def _transaction_response(
    league: League,
    scoring_period: int,
    types: list[str],
) -> dict[str, Any]:
    filters = {"transactions": {"filterType": {"value": types}}}
    return league.espn_request.league_get(
        params={"view": "mTransactions2", "scoringPeriodId": scoring_period},
        headers={"x-fantasy-filter": json.dumps(filters)},
    )


@mcp.tool()
async def get_transactions(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    scoring_period: int | None = None,
    limit: int = 25,
    offset: int = 0,
    types: list[str] | None = None,
) -> dict[str, Any]:
    """Get completed, failed, and pending league transactions."""
    del ctx
    try:
        _validate_page(limit, offset)
        league = await _get_league(league_id, year)
        period = scoring_period or league.scoringPeriodId
        if period < 1 or period > league.finalScoringPeriod:
            raise ValueError(
                f"scoring_period must be between 1 and {league.finalScoringPeriod}"
            )
        selected_types = sorted(set(types or TRANSACTION_TYPES))
        invalid = set(selected_types) - TRANSACTION_TYPES
        if invalid:
            raise ValueError(f"Unsupported transaction types: {sorted(invalid)}")
        response = await anyio.to_thread.run_sync(
            api.get_view,
            league,
            f"transactions:{period}:{','.join(selected_types)}",
            lambda: _transaction_response(league, period, selected_types),
        )
        rows = [_transaction(row, league) for row in response.get("transactions", [])]
        page = rows[offset : offset + limit]
        return {
            **_meta(api.last_success_at),
            "scoring_period": period,
            "total": len(rows),
            "offset": offset,
            "count": len(page),
            "has_more": offset + len(page) < len(rows),
            "transactions": page,
        }
    except Exception as error:
        _raise_api_error("Transaction retrieval", error)


@mcp.tool()
async def get_waiver_report(
    league_id: int,
    team_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    scoring_period: int | None = None,
) -> dict[str, Any]:
    """Get processed waiver results and only the managed team's pending claims."""
    del ctx
    try:
        league = await _get_league(league_id, year)
        _team_by_id(league, team_id)
        period = scoring_period or league.scoringPeriodId
        response = await anyio.to_thread.run_sync(
            api.get_view,
            league,
            f"waivers:{period}",
            lambda: league.espn_request.get_league_offers(week=period),
        )
        rows = []
        for transaction in response.get("transactions", []):
            if (
                transaction.get("status") == "PENDING"
                and transaction.get("teamId") != team_id
            ):
                continue
            rows.append(_transaction(transaction, league))
        return {
            **_meta(api.last_success_at),
            "scoring_period": period,
            "managed_team_id": team_id,
            "count": len(rows),
            "waivers": rows,
        }
    except Exception as error:
        _raise_api_error("Waiver report retrieval", error)


@mcp.tool()
async def get_schedule(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    week: int | None = None,
    include_kickoffs: bool = True,
) -> dict[str, Any]:
    """Get fantasy matchups for one scoring week."""
    del ctx
    try:
        league = await _get_league(league_id, year)
        selected_week = week or league.current_week
        if selected_week < 1 or selected_week > league.finalScoringPeriod:
            raise ValueError(f"week must be between 1 and {league.finalScoringPeriod}")
        matchups = await anyio.to_thread.run_sync(
            api.get_view,
            league,
            f"scoreboard:{selected_week}",
            lambda: league.scoreboard(selected_week),
        )
        kickoffs: dict[str, str | None] = {}
        if include_kickoffs:
            boxes = await anyio.to_thread.run_sync(
                api.get_view,
                league,
                f"box_scores:{selected_week}",
                lambda: league.box_scores(selected_week),
            )
            for box in boxes:
                for player in list(box.home_lineup) + list(box.away_lineup):
                    game_date = getattr(player, "game_date", None)
                    if isinstance(game_date, dt.datetime) and game_date.tzinfo is None:
                        game_date = game_date.astimezone().astimezone(dt.UTC)
                    if player.proTeam and player.proTeam != "None":
                        kickoffs[player.proTeam] = (
                            game_date.isoformat() if game_date else None
                        )
        return {
            **_meta(api.last_success_at),
            "week": selected_week,
            "matchups": [
                {
                    "home_team_id": matchup.home_team.team_id
                    if matchup.home_team
                    else None,
                    "home_team": matchup.home_team.team_name
                    if matchup.home_team
                    else "BYE",
                    "home_score": matchup.home_score,
                    "away_team_id": matchup.away_team.team_id
                    if matchup.away_team
                    else None,
                    "away_team": matchup.away_team.team_name
                    if matchup.away_team
                    else "BYE",
                    "away_score": matchup.away_score,
                }
                for matchup in matchups
            ],
            "nfl_kickoffs": [
                {"pro_team": team, "game_time": game_time}
                for team, game_time in sorted(kickoffs.items())
            ],
        }
    except Exception as error:
        _raise_api_error("Schedule retrieval", error)


if __name__ == "__main__":
    logger.info(
        "Starting read-only ESPN fantasy football MCP server for %s", CURRENT_YEAR
    )
    mcp.run()
