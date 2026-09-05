import datetime
import json
import logging
import os
import secrets
import sys
import time
import uuid
import weakref
from collections import OrderedDict
from typing import Any

import anyio
from espn_api.football import League
from espn_api.football.constant import POSITION_MAP, PRO_TEAM_MAP
from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNInvalidLeague
from mcp.server.fastmcp import Context, FastMCP


logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("espn-fantasy-football")

mcp = FastMCP("espn-fantasy-football", dependencies=["espn-api"])

CURRENT_YEAR = datetime.datetime.now().year
if datetime.datetime.now().month < 7:
    CURRENT_YEAR -= 1

WRITE_CONFIRMATION_TTL_SECONDS = 120
WRITE_CONFIRMATION_LIMIT = 16
WRITE_ENDPOINT = (
    "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/"
    "{year}/segments/0/leagues/{league_id}/transactions/"
)
DRAFT_GAME_ID = 1
DRAFT_SECURITY_PATH = "/teams/{team_id}/draftSecurity"
DRAFT_SELECT_ENDPOINT = (
    "https://fantasydraft.espn.com/game-ffl/league-{league_id}/SELECT"
)


class ESPNFantasyFootballAPI:
    """Store credentials and short-lived league objects for each MCP session."""

    def __init__(
        self,
        cache_ttl_seconds: int = 300,
        draft_cache_ttl_seconds: int = 2,
        max_cached_leagues: int = 128,
        max_credential_sessions: int = 32,
    ):
        self.cache_ttl_seconds = cache_ttl_seconds
        self.draft_cache_ttl_seconds = draft_cache_ttl_seconds
        self.max_cached_leagues = max_cached_leagues
        self.max_credential_sessions = max_credential_sessions
        self.leagues: dict[tuple[str, int, int, int], tuple[float, League]] = {}
        self.drafts: dict[tuple[str, int, int, int], tuple[float, dict[str, Any]]] = {}
        self.credentials: OrderedDict[str, dict[str, str]] = OrderedDict()
        self.credential_versions: dict[str, int] = {}
        self.write_confirmations: OrderedDict[
            str, OrderedDict[str, tuple[float, dict[str, Any]]]
        ] = OrderedDict()

    def _remove_session_leagues(self, session_id: str) -> None:
        keys = [key for key in self.leagues if key[0] == session_id]
        for key in keys:
            del self.leagues[key]

    def _remove_session_drafts(self, session_id: str) -> None:
        keys = [key for key in self.drafts if key[0] == session_id]
        for key in keys:
            del self.drafts[key]

    def _remove_session_confirmations(self, session_id: str) -> None:
        self.write_confirmations.pop(session_id, None)

    def create_write_confirmation(
        self, session_id: str, action: dict[str, Any]
    ) -> str:
        now = time.monotonic()
        confirmations = self.write_confirmations.pop(session_id, OrderedDict())
        self.write_confirmations[session_id] = confirmations
        while len(self.write_confirmations) > self.max_credential_sessions:
            self.write_confirmations.popitem(last=False)
        expired_tokens = [
            token
            for token, (expires_at, _) in confirmations.items()
            if expires_at <= now
        ]
        for token in expired_tokens:
            del confirmations[token]
        while len(confirmations) >= WRITE_CONFIRMATION_LIMIT:
            confirmations.popitem(last=False)
        token = secrets.token_urlsafe(24)
        confirmations[token] = (now + WRITE_CONFIRMATION_TTL_SECONDS, action)
        return token

    def consume_write_confirmation(
        self,
        session_id: str,
        token: str,
        expected_action: dict[str, Any],
    ) -> None:
        confirmations = self.write_confirmations.get(session_id)
        if confirmations is None or token not in confirmations:
            raise PermissionError("The write confirmation token is invalid or expired.")
        expires_at, action = confirmations.pop(token)
        if not confirmations:
            self.write_confirmations.pop(session_id, None)
        if expires_at <= time.monotonic():
            raise PermissionError("The write confirmation token is invalid or expired.")
        if action != expected_action:
            raise PermissionError(
                "The write confirmation token does not match the action."
            )

    def invalidate_league(self, session_id: str) -> None:
        self._remove_session_leagues(session_id)
        self._remove_session_drafts(session_id)

    def invalidate_draft(self, session_id: str) -> None:
        self._remove_session_drafts(session_id)

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
        if (
            cache_key not in self.leagues
            and len(self.leagues) >= self.max_cached_leagues
        ):
            oldest_key = min(self.leagues, key=lambda key: self.leagues[key][0])
            del self.leagues[oldest_key]
        self.leagues[cache_key] = (time.monotonic(), league)
        return league

    def get_draft(
        self,
        session_id: str,
        league_id: int,
        year: int = CURRENT_YEAR,
        refresh: bool = False,
    ) -> tuple[League, dict[str, Any]]:
        credential_version = self.credential_versions.get(session_id, 0)
        cache_key = (session_id, league_id, year, credential_version)
        cached = self.drafts.get(cache_key)
        league = self.get_league(session_id, league_id, year)

        if cached and not refresh:
            cached_at, draft = cached
            if time.monotonic() - cached_at < self.draft_cache_ttl_seconds:
                return league, draft

        draft = league.espn_request.get_league_draft().get("draftDetail", {})
        if cache_key not in self.drafts and len(self.drafts) >= self.max_cached_leagues:
            oldest_key = min(self.drafts, key=lambda key: self.drafts[key][0])
            del self.drafts[oldest_key]
        self.drafts[cache_key] = (time.monotonic(), draft)
        return league, draft

    def store_credentials(self, session_id: str, espn_s2: str, swid: str) -> None:
        self._remove_session_leagues(session_id)
        self._remove_session_drafts(session_id)
        self._remove_session_confirmations(session_id)
        self.credentials.pop(session_id, None)
        self.credentials[session_id] = {"espn_s2": espn_s2, "swid": swid}
        while len(self.credentials) > self.max_credential_sessions:
            expired_session_id, _ = self.credentials.popitem(last=False)
            self._remove_session_leagues(expired_session_id)
            self._remove_session_drafts(expired_session_id)
            self._remove_session_confirmations(expired_session_id)
            self.credential_versions.pop(expired_session_id, None)
        self.credential_versions[session_id] = (
            self.credential_versions.get(session_id, 0) + 1
        )

    def clear_credentials(self, session_id: str) -> None:
        self._remove_session_leagues(session_id)
        self._remove_session_drafts(session_id)
        self._remove_session_confirmations(session_id)
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


async def _get_draft(
    ctx: Context,
    league_id: int,
    year: int,
    refresh: bool = False,
) -> tuple[League, dict[str, Any]]:
    if league_id <= 0:
        raise ValueError("league_id must be positive")
    if year < 2000 or year > CURRENT_YEAR + 1:
        raise ValueError(f"year must be between 2000 and {CURRENT_YEAR + 1}")
    return await anyio.to_thread.run_sync(
        api.get_draft, _session_id(ctx), league_id, year, refresh
    )


def _completed_pick(pick: dict[str, Any]) -> bool:
    player_id = pick.get("playerId")
    return isinstance(player_id, int) and player_id > 0


def _pick_data(
    pick: dict[str, Any],
    league: League,
) -> dict[str, Any]:
    team_id = pick.get("teamId")
    team = league.get_team_data(team_id) if team_id is not None else None
    player_id = pick.get("playerId")
    return {
        "overall_pick": pick.get("overallPickNumber"),
        "round": pick.get("roundId"),
        "round_pick": pick.get("roundPickNumber"),
        "team_id": team_id,
        "team_name": team.team_name if team else None,
        "player_id": player_id,
        "player_name": league.player_map.get(player_id),
        "bid_amount": pick.get("bidAmount"),
        "keeper": bool(pick.get("keeper", False)),
    }


def _draft_state(draft: dict[str, Any], league: League) -> dict[str, Any]:
    picks = sorted(
        draft.get("picks", []),
        key=lambda pick: pick.get("overallPickNumber") or 0,
    )
    completed = [pick for pick in picks if _completed_pick(pick)]
    remaining = [pick for pick in picks if not _completed_pick(pick)]
    first_round = [pick for pick in picks if pick.get("roundId") == 1]
    next_pick = _pick_data(remaining[0], league) if remaining else None
    last_pick = _pick_data(completed[-1], league) if completed else None
    return {
        "league_id": league.league_id,
        "year": league.year,
        "drafted": bool(draft.get("drafted", False)),
        "in_progress": bool(draft.get("inProgress", False)),
        "completed_pick_count": len(completed),
        "scheduled_pick_count": len(picks),
        "team_order": [pick.get("teamId") for pick in first_round],
        "next_pick": next_pick,
        "last_pick": last_pick,
    }


def _projected_points(
    player: dict[str, Any],
    year: int,
    scoring_period: int = 0,
) -> float | None:
    split_type = 0 if scoring_period == 0 else 1
    for stats in player.get("stats", []):
        if (
            stats.get("seasonId") == year
            and stats.get("statSourceId") == 1
            and stats.get("statSplitTypeId") == split_type
            and stats.get("scoringPeriodId") == scoring_period
        ):
            return round(float(stats.get("appliedTotal") or 0.0), 2)
    return None


def _draft_player_data(player: dict[str, Any], year: int) -> dict[str, Any]:
    ppr = player.get("draftRanksByRankType", {}).get("PPR", {})
    ownership = player.get("ownership", {})
    primary_positions = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
    return {
        "player_id": player.get("id"),
        "name": player.get("fullName"),
        "position": primary_positions.get(player.get("defaultPositionId")),
        "pro_team": PRO_TEAM_MAP.get(player.get("proTeamId"), "FA"),
        "espn_ppr_rank": ppr.get("rank"),
        "adp": round(float(ownership.get("averageDraftPosition") or 0.0), 2),
        "projected_points": _projected_points(player, year),
        "injury_status": player.get("injuryStatus", "UNKNOWN"),
    }


def _validate_draft_query(limit: int, offset: int, position: str | None = None) -> None:
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    if offset < 0:
        raise ValueError("offset must be zero or positive")
    if position is not None and position not in {"QB", "RB", "WR", "TE", "D/ST", "K"}:
        raise ValueError("position must be QB, RB, WR, TE, D/ST, or K")


def _get_draft_pool_data(
    league: League,
    year: int,
    limit: int,
    offset: int,
    position: str | None = None,
) -> list[dict[str, Any]]:
    slot_ids = [POSITION_MAP[position]] if position else []
    filters = {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "filterSlotIds": {"value": slot_ids},
            "limit": limit,
            "offset": offset,
            "sortDraftRanks": {
                "sortPriority": 1,
                "sortAsc": True,
                "value": "PPR",
            },
        }
    }
    response = league.espn_request.league_get(
        params={"view": "kona_player_info", "scoringPeriodId": 0},
        headers={"x-fantasy-filter": json.dumps(filters)},
    )
    return [
        _draft_player_data(entry.get("player", {}), year)
        for entry in response.get("players", [])
    ]


def _write_enabled() -> bool:
    return os.environ.get("ESPN_WRITE_ENABLED", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _draft_write_enabled() -> bool:
    return os.environ.get("ESPN_DRAFT_WRITE_ENABLED", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _scoring_period(league: League, scoring_period_id: int | None) -> int:
    selected = (
        getattr(league, "scoringPeriodId", None)
        if scoring_period_id is None
        else scoring_period_id
    )
    if not isinstance(selected, int) or selected < 1:
        raise ValueError("scoring_period_id must be a positive integer")
    final_period = getattr(league, "finalScoringPeriod", None)
    if isinstance(final_period, int) and selected > final_period:
        raise ValueError(f"scoring_period_id must not exceed {final_period}")
    return selected


def _roster_player(team: Any, player_id: int) -> Any:
    for player in team.roster:
        if player.playerId == player_id:
            return player
    raise ValueError(f"Player {player_id} is not on team {team.team_id}.")


def _assert_team_owner(league: League, team_id: int) -> Any:
    request = league.espn_request
    cookies = request.cookies or {}
    swid = cookies.get("SWID")
    if not cookies.get("espn_s2") or not swid:
        raise PermissionError("Private ESPN credentials are required for writes.")
    team = _team_by_id(league, team_id)
    owner_ids = {
        owner.get("id")
        for owner in team.owners
        if isinstance(owner, dict) and owner.get("id")
    }
    if swid not in owner_ids:
        raise PermissionError("The ESPN credentials do not own the selected team.")
    return team


def _player_name(league: League, player_id: int) -> str:
    name = league.player_map.get(player_id)
    if not isinstance(name, str) or not name:
        raise ValueError(f"ESPN did not find player_id {player_id}.")
    return name


def _player_pool_status(
    league: League, player_id: int, scoring_period_id: int
) -> str | None:
    filters = {"players": {"filterIds": {"value": [player_id]}, "limit": 1}}
    response = league.espn_request.league_get(
        params={"view": "kona_player_info", "scoringPeriodId": scoring_period_id},
        headers={"x-fantasy-filter": json.dumps(filters)},
    )
    for entry in response.get("players", []):
        player = entry.get("player", {})
        if player.get("id") == player_id:
            return entry.get("status")
    return None


def _draft_type(league: League) -> str:
    cached = getattr(league, "_mcp_draft_type", None)
    if isinstance(cached, str):
        return cached
    response = league.espn_request.league_get(params={"view": "mSettings"})
    draft_type = response.get("settings", {}).get("draftSettings", {}).get("type")
    if not isinstance(draft_type, str):
        raise RuntimeError("ESPN did not return the league draft type.")
    league._mcp_draft_type = draft_type
    return draft_type


def _draft_pick_action(
    league: League,
    draft: dict[str, Any],
    team_id: int,
    player_id: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if player_id <= 0:
        raise ValueError("player_id must be positive")
    team = _assert_team_owner(league, team_id)
    draft_type = _draft_type(league)
    if draft_type not in {"SNAKE", "LINEAR"}:
        raise ValueError("Live draft picks support only SNAKE and LINEAR drafts.")
    if not draft.get("inProgress", False):
        raise ValueError("The ESPN draft is not in progress.")
    picks = sorted(
        draft.get("picks", []),
        key=lambda pick: pick.get("overallPickNumber") or 0,
    )
    if any(
        pick.get("playerId") == player_id
        for pick in picks
        if _completed_pick(pick)
    ):
        raise ValueError(f"Player {player_id} is already drafted.")
    remaining = [pick for pick in picks if not _completed_pick(pick)]
    if not remaining:
        raise ValueError("The ESPN draft has no remaining picks.")
    current_pick = remaining[0]
    if current_pick.get("teamId") != team_id:
        raise PermissionError(
            f"Team {team_id} is not on the clock. "
            f"Team {current_pick.get('teamId')} is next."
        )
    status = _player_pool_status(league, player_id, 0)
    if status != "FREEAGENT":
        raise ValueError(
            f"Player {player_id} has ESPN status {status or 'UNKNOWN'}, not FREEAGENT."
        )
    action = {
        "kind": "DRAFT_PICK",
        "league_id": league.league_id,
        "year": league.year,
        "team_id": team_id,
        "player_id": player_id,
        "overall_pick": current_pick.get("overallPickNumber"),
    }
    preview = {
        "action": "DRAFT_PICK",
        "league_id": league.league_id,
        "year": league.year,
        "team_id": team_id,
        "team_name": team.team_name,
        "player_id": player_id,
        "player_name": _player_name(league, player_id),
        "overall_pick": current_pick.get("overallPickNumber"),
        "round": current_pick.get("roundId"),
        "round_pick": current_pick.get("roundPickNumber"),
    }
    return action, preview


def _draft_security_token(league: League, team_id: int) -> str:
    request = league.espn_request
    cookies = request.cookies or {}
    swid = cookies.get("SWID")
    if not cookies.get("espn_s2") or not swid:
        raise PermissionError("Private ESPN credentials are required for draft writes.")
    _assert_team_owner(league, team_id)
    response = request.session.get(
        request.LEAGUE_ENDPOINT + DRAFT_SECURITY_PATH.format(team_id=team_id),
        cookies=cookies,
        timeout=request.timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"ESPN rejected draft authorization with HTTP {response.status_code}."
        )
    try:
        security_value = response.json()
    except ValueError as error:
        raise RuntimeError("ESPN returned invalid draft authorization data.") from error
    if isinstance(security_value, bool) or not isinstance(security_value, (int, str)):
        raise RuntimeError("ESPN returned invalid draft authorization data.")
    return (
        f"{DRAFT_GAME_ID}:{league.league_id}:{team_id}:{swid}:{security_value}"
    )


def _submit_live_draft_pick(league: League, action: dict[str, Any]) -> int:
    token = _draft_security_token(league, action["team_id"])
    request = league.espn_request
    response = request.session.get(
        DRAFT_SELECT_ENDPOINT.format(league_id=league.league_id),
        params={"1": action["player_id"], "token": token},
        cookies=request.cookies,
        timeout=request.timeout,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not 200 <= response.status_code < 300:
        detail = _espn_error_code(payload)
        suffix = f" ESPN code: {detail}" if detail else ""
        raise RuntimeError(
            f"ESPN rejected the draft pick with HTTP {response.status_code}.{suffix}"
        )
    return response.status_code


def _slot_id(slot_name: str) -> int:
    for slot_id, label in POSITION_MAP.items():
        if isinstance(slot_id, int) and label == slot_name:
            return slot_id
    raise ValueError(f"ESPN lineup slot '{slot_name}' is not supported.")


def _add_drop_action(
    league: League,
    team_id: int,
    add_player_id: int,
    drop_player_id: int,
    scoring_period_id: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if add_player_id <= 0 or drop_player_id <= 0:
        raise ValueError("Player IDs must be positive.")
    if add_player_id == drop_player_id:
        raise ValueError("The add and drop player IDs must be different.")
    team = _assert_team_owner(league, team_id)
    drop_player = _roster_player(team, drop_player_id)
    for candidate in league.teams:
        for player in candidate.roster:
            if player.playerId == add_player_id:
                raise ValueError(
                    f"Player {add_player_id} is already on team {candidate.team_id}."
                )
    selected_period = _scoring_period(league, scoring_period_id)
    status = _player_pool_status(league, add_player_id, selected_period)
    if status != "FREEAGENT":
        raise ValueError(
            f"Player {add_player_id} has ESPN status "
            f"{status or 'UNKNOWN'}, not FREEAGENT."
        )
    action = {
        "kind": "ADD_DROP",
        "league_id": league.league_id,
        "year": league.year,
        "team_id": team_id,
        "scoring_period_id": selected_period,
        "add_player_id": add_player_id,
        "drop_player_id": drop_player_id,
    }
    preview = {
        "action": "ADD_DROP",
        "league_id": league.league_id,
        "year": league.year,
        "team_id": team_id,
        "team_name": team.team_name,
        "scoring_period_id": selected_period,
        "add": {
            "player_id": add_player_id,
            "player_name": _player_name(league, add_player_id),
        },
        "drop": {
            "player_id": drop_player_id,
            "player_name": drop_player.name,
        },
    }
    return action, preview


def _lineup_swap_action(
    league: League,
    team_id: int,
    starter_player_id: int,
    bench_player_id: int,
    scoring_period_id: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if starter_player_id <= 0 or bench_player_id <= 0:
        raise ValueError("Player IDs must be positive.")
    if starter_player_id == bench_player_id:
        raise ValueError("The starter and bench player IDs must be different.")
    team = _assert_team_owner(league, team_id)
    starter = _roster_player(team, starter_player_id)
    bench = _roster_player(team, bench_player_id)
    if bench.lineupSlot != "BE":
        raise ValueError(f"Player {bench_player_id} is not in the BE slot.")
    if starter.lineupSlot in {"BE", "IR", ""}:
        raise ValueError(f"Player {starter_player_id} is not in an active slot.")
    if starter.lineupSlot not in bench.eligibleSlots:
        raise ValueError(
            f"Player {bench_player_id} is not eligible for {starter.lineupSlot}."
        )
    selected_period = _scoring_period(league, scoring_period_id)
    starter_slot_id = _slot_id(starter.lineupSlot)
    bench_slot_id = _slot_id(bench.lineupSlot)
    action = {
        "kind": "LINEUP_SWAP",
        "league_id": league.league_id,
        "year": league.year,
        "team_id": team_id,
        "scoring_period_id": selected_period,
        "starter_player_id": starter_player_id,
        "bench_player_id": bench_player_id,
        "starter_slot_id": starter_slot_id,
        "bench_slot_id": bench_slot_id,
    }
    preview = {
        "action": "LINEUP_SWAP",
        "league_id": league.league_id,
        "year": league.year,
        "team_id": team_id,
        "team_name": team.team_name,
        "scoring_period_id": selected_period,
        "start": {
            "player_id": bench_player_id,
            "player_name": bench.name,
            "from_slot": bench.lineupSlot,
            "to_slot": starter.lineupSlot,
        },
        "bench": {
            "player_id": starter_player_id,
            "player_name": starter.name,
            "from_slot": starter.lineupSlot,
            "to_slot": bench.lineupSlot,
        },
    }
    return action, preview


def _transaction_body(league: League, action: dict[str, Any]) -> dict[str, Any]:
    common = {
        "isLeagueManager": False,
        "teamId": action["team_id"],
        "scoringPeriodId": action["scoring_period_id"],
        "executionType": "EXECUTE",
    }
    if action["kind"] == "ADD_DROP":
        return {
            **common,
            "type": "FREEAGENT",
            "items": [
                {
                    "playerId": action["add_player_id"],
                    "type": "ADD",
                    "toTeamId": action["team_id"],
                },
                {
                    "playerId": action["drop_player_id"],
                    "type": "DROP",
                    "fromTeamId": action["team_id"],
                },
            ],
        }
    if action["kind"] == "LINEUP_SWAP":
        cookies = league.espn_request.cookies or {}
        swid = cookies.get("SWID")
        if not swid:
            raise PermissionError("Private ESPN credentials are required for writes.")
        return {
            **common,
            "type": "ROSTER",
            "memberId": swid,
            "items": [
                {
                    "playerId": action["bench_player_id"],
                    "type": "LINEUP",
                    "fromLineupSlotId": action["bench_slot_id"],
                    "toLineupSlotId": action["starter_slot_id"],
                    "fromTeamId": 0,
                    "toTeamId": 0,
                },
                {
                    "playerId": action["starter_player_id"],
                    "type": "LINEUP",
                    "fromLineupSlotId": action["starter_slot_id"],
                    "toLineupSlotId": action["bench_slot_id"],
                    "fromTeamId": 0,
                    "toTeamId": 0,
                },
            ],
        }
    raise ValueError("Unsupported write action.")


def _espn_error_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("code", "message", "error", "details"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value.replace("\n", " ")[:160]
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        return str(messages[0]).replace("\n", " ")[:160]
    return None


def _post_transaction(league: League, action: dict[str, Any]) -> int:
    request = league.espn_request
    cookies = request.cookies or {}
    if not cookies.get("espn_s2") or not cookies.get("SWID"):
        raise PermissionError("Private ESPN credentials are required for writes.")
    response = request.session.post(
        WRITE_ENDPOINT.format(year=league.year, league_id=league.league_id),
        json=_transaction_body(league, action),
        headers={
            "Content-Type": "application/json",
            "x-fantasy-platform": "espn-fantasy-web",
            "x-fantasy-source": "kona",
        },
        cookies=cookies,
        timeout=request.timeout,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not 200 <= response.status_code < 300:
        detail = _espn_error_code(payload)
        suffix = f" ESPN code: {detail}" if detail else ""
        raise RuntimeError(
            f"ESPN rejected the transaction with HTTP {response.status_code}.{suffix}"
        )
    return response.status_code


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
async def refresh_draft(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
) -> dict[str, Any]:
    """Refresh only the live draft data for one league."""
    try:
        league, draft = await _get_draft(ctx, league_id, year, refresh=True)
        state = _draft_state(draft, league)
        state["refreshed"] = True
        return state
    except Exception as error:
        _raise_api_error("Draft refresh", error)


@mcp.tool()
async def get_draft_state(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
) -> dict[str, Any]:
    """Get compact live draft status, order, and current pick data."""
    try:
        league, draft = await _get_draft(ctx, league_id, year)
        return _draft_state(draft, league)
    except Exception as error:
        _raise_api_error("Draft state retrieval", error)


@mcp.tool()
async def get_draft_picks(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    limit: int = 25,
    offset: int = 0,
    team_id: int | None = None,
    include_scheduled: bool = False,
) -> dict[str, Any]:
    """Get paged draft picks. Results include completed picks by default."""
    try:
        _validate_draft_query(limit, offset)
        league, draft = await _get_draft(ctx, league_id, year)
        if team_id is not None:
            _team_by_id(league, team_id)
        picks = sorted(
            draft.get("picks", []),
            key=lambda pick: pick.get("overallPickNumber") or 0,
        )
        if not include_scheduled:
            picks = [pick for pick in picks if _completed_pick(pick)]
        if team_id is not None:
            picks = [pick for pick in picks if pick.get("teamId") == team_id]
        page = picks[offset : offset + limit]
        return {
            "total": len(picks),
            "offset": offset,
            "count": len(page),
            "has_more": offset + len(page) < len(picks),
            "picks": [_pick_data(pick, league) for pick in page],
        }
    except Exception as error:
        _raise_api_error("Draft picks retrieval", error)


@mcp.tool()
async def get_draft_pool(
    league_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    limit: int = 50,
    offset: int = 0,
    position: str | None = None,
) -> dict[str, Any]:
    """Get a compact page of available players in ESPN PPR draft order."""
    try:
        position = position.upper() if position else None
        _validate_draft_query(limit, offset, position)
        league = await _get_league(ctx, league_id, year)
        players = await anyio.to_thread.run_sync(
            _get_draft_pool_data, league, year, limit, offset, position
        )
        return {
            "offset": offset,
            "count": len(players),
            "position": position,
            "players": players,
        }
    except Exception as error:
        _raise_api_error("Draft pool retrieval", error)


@mcp.tool()
async def get_draft_context(
    league_id: int,
    team_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    player_limit: int = 15,
    recent_pick_limit: int = 8,
    position: str | None = None,
) -> dict[str, Any]:
    """Get one compact response for a live recommendation at a team's next pick."""
    try:
        position = position.upper() if position else None
        _validate_draft_query(player_limit, 0, position)
        if recent_pick_limit < 0 or recent_pick_limit > 25:
            raise ValueError("recent_pick_limit must be between 0 and 25")
        league, draft = await _get_draft(ctx, league_id, year)
        team = _team_by_id(league, team_id)
        picks = sorted(
            draft.get("picks", []),
            key=lambda pick: pick.get("overallPickNumber") or 0,
        )
        completed = [pick for pick in picks if _completed_pick(pick)]
        remaining = [pick for pick in picks if not _completed_pick(pick)]
        team_completed = [pick for pick in completed if pick.get("teamId") == team_id]
        team_remaining = [pick for pick in remaining if pick.get("teamId") == team_id]
        next_team_pick = (
            _pick_data(team_remaining[0], league) if team_remaining else None
        )
        players = await anyio.to_thread.run_sync(
            _get_draft_pool_data, league, year, player_limit, 0, position
        )
        state = _draft_state(draft, league)
        current_overall = (
            state["next_pick"].get("overall_pick") if state["next_pick"] else None
        )
        team_overall = next_team_pick.get("overall_pick") if next_team_pick else None
        picks_until_team = (
            max(team_overall - current_overall, 0)
            if isinstance(team_overall, int) and isinstance(current_overall, int)
            else None
        )
        return {
            "draft": state,
            "team_id": team_id,
            "team_name": team.team_name,
            "next_team_pick": next_team_pick,
            "picks_until_team": picks_until_team,
            "team_picks": [_pick_data(pick, league) for pick in team_completed],
            "recent_picks": [
                _pick_data(pick, league)
                for pick in completed[-recent_pick_limit:]
            ] if recent_pick_limit else [],
            "available_players": players,
        }
    except Exception as error:
        _raise_api_error("Draft context retrieval", error)


@mcp.tool()
async def preview_draft_pick(
    league_id: int,
    team_id: int,
    player_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
) -> dict[str, Any]:
    """Preview one live snake-draft pick and create a short-lived token."""
    try:
        league, draft = await _get_draft(ctx, league_id, year, refresh=True)
        action, preview = await anyio.to_thread.run_sync(
            _draft_pick_action, league, draft, team_id, player_id
        )
        token = api.create_write_confirmation(_session_id(ctx), action)
        return {
            **preview,
            "writes_enabled": _write_enabled(),
            "draft_writes_enabled": _draft_write_enabled(),
            "confirmation_token": token,
            "expires_in_seconds": WRITE_CONFIRMATION_TTL_SECONDS,
        }
    except Exception as error:
        _raise_api_error("Draft pick preview", error)


@mcp.tool()
async def execute_draft_pick(
    league_id: int,
    team_id: int,
    player_id: int,
    confirmation_token: str,
    ctx: Context,
    year: int = CURRENT_YEAR,
) -> dict[str, Any]:
    """Submit one previewed live snake-draft pick with a one-time token."""
    try:
        if not _write_enabled() or not _draft_write_enabled():
            raise PermissionError(
                "Draft writes need ESPN_WRITE_ENABLED=true and "
                "ESPN_DRAFT_WRITE_ENABLED=true."
            )
        league, draft = await _get_draft(ctx, league_id, year, refresh=True)
        action, preview = await anyio.to_thread.run_sync(
            _draft_pick_action, league, draft, team_id, player_id
        )
        session_id = _session_id(ctx)
        api.consume_write_confirmation(session_id, confirmation_token, action)
        status_code = await anyio.to_thread.run_sync(
            _submit_live_draft_pick, league, action
        )
        api.invalidate_draft(session_id)
        return {**preview, "submitted": True, "http_status": status_code}
    except Exception as error:
        _raise_api_error("Draft pick", error)


@mcp.tool()
async def preview_add_drop(
    league_id: int,
    team_id: int,
    add_player_id: int,
    drop_player_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    scoring_period_id: int | None = None,
) -> dict[str, Any]:
    """Preview one free-agent add and drop, then create a short-lived token."""
    try:
        league = await _get_league(ctx, league_id, year, refresh=True)
        action, preview = await anyio.to_thread.run_sync(
            _add_drop_action,
            league,
            team_id,
            add_player_id,
            drop_player_id,
            scoring_period_id,
        )
        token = api.create_write_confirmation(_session_id(ctx), action)
        return {
            **preview,
            "writes_enabled": _write_enabled(),
            "confirmation_token": token,
            "expires_in_seconds": WRITE_CONFIRMATION_TTL_SECONDS,
        }
    except Exception as error:
        _raise_api_error("Add and drop preview", error)


@mcp.tool()
async def execute_add_drop(
    league_id: int,
    team_id: int,
    add_player_id: int,
    drop_player_id: int,
    confirmation_token: str,
    ctx: Context,
    year: int = CURRENT_YEAR,
    scoring_period_id: int | None = None,
) -> dict[str, Any]:
    """Execute a previewed free-agent add and drop with a one-time token."""
    try:
        if not _write_enabled():
            raise PermissionError(
                "Writes are disabled. Set ESPN_WRITE_ENABLED=true in the "
                "server process."
            )
        league = await _get_league(ctx, league_id, year, refresh=True)
        action, preview = await anyio.to_thread.run_sync(
            _add_drop_action,
            league,
            team_id,
            add_player_id,
            drop_player_id,
            scoring_period_id,
        )
        session_id = _session_id(ctx)
        api.consume_write_confirmation(session_id, confirmation_token, action)
        status_code = await anyio.to_thread.run_sync(_post_transaction, league, action)
        api.invalidate_league(session_id)
        return {**preview, "executed": True, "http_status": status_code}
    except Exception as error:
        _raise_api_error("Add and drop", error)


@mcp.tool()
async def preview_lineup_swap(
    league_id: int,
    team_id: int,
    starter_player_id: int,
    bench_player_id: int,
    ctx: Context,
    year: int = CURRENT_YEAR,
    scoring_period_id: int | None = None,
) -> dict[str, Any]:
    """Preview one starter and bench swap, then create a short-lived token."""
    try:
        league = await _get_league(ctx, league_id, year, refresh=True)
        action, preview = _lineup_swap_action(
            league,
            team_id,
            starter_player_id,
            bench_player_id,
            scoring_period_id,
        )
        token = api.create_write_confirmation(_session_id(ctx), action)
        return {
            **preview,
            "writes_enabled": _write_enabled(),
            "confirmation_token": token,
            "expires_in_seconds": WRITE_CONFIRMATION_TTL_SECONDS,
        }
    except Exception as error:
        _raise_api_error("Lineup swap preview", error)


@mcp.tool()
async def execute_lineup_swap(
    league_id: int,
    team_id: int,
    starter_player_id: int,
    bench_player_id: int,
    confirmation_token: str,
    ctx: Context,
    year: int = CURRENT_YEAR,
    scoring_period_id: int | None = None,
) -> dict[str, Any]:
    """Execute a previewed starter and bench swap with a one-time token."""
    try:
        if not _write_enabled():
            raise PermissionError(
                "Writes are disabled. Set ESPN_WRITE_ENABLED=true in the "
                "server process."
            )
        league = await _get_league(ctx, league_id, year, refresh=True)
        action, preview = _lineup_swap_action(
            league,
            team_id,
            starter_player_id,
            bench_player_id,
            scoring_period_id,
        )
        session_id = _session_id(ctx)
        api.consume_write_confirmation(session_id, confirmation_token, action)
        status_code = await anyio.to_thread.run_sync(_post_transaction, league, action)
        api.invalidate_league(session_id)
        return {**preview, "executed": True, "http_status": status_code}
    except Exception as error:
        _raise_api_error("Lineup swap", error)


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
                "lineup_slot": getattr(player, "lineupSlot", None),
                "eligible_slots": getattr(player, "eligibleSlots", []),
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
