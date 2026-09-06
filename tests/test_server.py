import asyncio
import json
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import espn_fantasy_server as server


def run(coro):
    return asyncio.run(coro)


class ReadApiTests(unittest.TestCase):
    def setUp(self):
        server.api = server.ESPNReadAPI()

    def test_credentials_never_enter_cache_keys(self):
        league = SimpleNamespace(league_id=123, year=2026)
        with (
            mock.patch.dict(
                os.environ,
                {"ESPN_S2": "private-s2", "ESPN_SWID": "private-swid"},
                clear=True,
            ),
            mock.patch.object(server, "League", return_value=league),
        ):
            server.api.get_league(123, 2026)
        self.assertEqual(list(server.api.leagues), [(123, 2026)])
        self.assertNotIn("private", repr(server.api.leagues))

    def test_missing_credentials_fail_closed(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaises(PermissionError),
        ):
            server.api.get_league(123, 2026)

    def test_view_cache_uses_one_loader_call(self):
        league = SimpleNamespace(league_id=123, year=2026)
        loader = mock.Mock(return_value={"ok": True})
        first = server.api.get_view(league, "settings", loader)
        second = server.api.get_view(league, "settings", loader)
        self.assertEqual(first, second)
        loader.assert_called_once()

    def test_scope_invalidation_does_not_clear_other_views(self):
        server.api.views[(123, 2026, "pool:1")] = server.CacheEntry(
            time.monotonic(), {}
        )
        server.api.views[(123, 2026, "settings")] = server.CacheEntry(
            time.monotonic(), {}
        )
        removed = server.api.invalidate(123, 2026, "pool")
        self.assertEqual(removed, 1)
        self.assertIn((123, 2026, "settings"), server.api.views)


class SerializationTests(unittest.TestCase):
    def test_raw_player_is_compact_and_includes_availability(self):
        entry = {
            "status": "WAIVERS",
            "waiverProcessDate": 1_800_000_000_000,
            "player": {
                "id": 10,
                "fullName": "Player One",
                "defaultPositionId": 2,
                "eligibleSlots": [2, 20, 23, 25],
                "proTeamId": 2,
                "injuryStatus": "QUESTIONABLE",
                "injured": True,
                "ownership": {"percentOwned": 45.2, "percentStarted": 4.1},
                "stats": [
                    {
                        "seasonId": 2026,
                        "statSourceId": 1,
                        "statSplitTypeId": 1,
                        "scoringPeriodId": 1,
                        "appliedTotal": 12.345,
                    }
                ],
            },
        }
        result = server._raw_player(entry, 2026, 1)
        self.assertEqual(result["position"], "RB")
        self.assertEqual(result["availability"], "WAIVERS")
        self.assertEqual(result["projected_points"], 12.35)
        self.assertNotIn("stats", result)

    def test_error_handler_does_not_copy_remote_text(self):
        error = server.requests.RequestException(
            "GET https://example.test/path?token=secret-token"
        )
        with self.assertRaisesRegex(RuntimeError, "request did not complete") as raised:
            server._raise_api_error("Read", error)
        self.assertNotIn("secret-token", str(raised.exception))


class ToolTests(unittest.TestCase):
    def setUp(self):
        server.api = server.ESPNReadAPI()

    @staticmethod
    def team(team_id=3, name="azim_the_dream"):
        player = SimpleNamespace(
            playerId=10,
            name="Player One",
            position="RB",
            proTeam="BUF",
            lineupSlot="RB",
            eligibleSlots=["RB", "RB/WR/TE", "BE"],
            injuryStatus="ACTIVE",
            injured=False,
            total_points=0,
            projected_total_points=200,
            stats={1: {"projected_points": 12.3, "points": 0}},
        )
        return SimpleNamespace(
            team_id=team_id,
            team_name=name,
            owners=[{"displayName": "Owner"}],
            wins=0,
            losses=0,
            ties=0,
            points_for=0,
            points_against=0,
            waiver_rank=3,
            acquisitions=0,
            drops=0,
            trades=0,
            playoff_pct=25,
            standing=1,
            roster=[player],
        )

    @classmethod
    def league(cls):
        team = cls.team()
        request = SimpleNamespace(
            league_get=mock.Mock(),
            get_league_offers=mock.Mock(return_value={"transactions": []}),
        )
        settings = SimpleNamespace(name="Wafergate", scoring_type="H2H_POINTS")
        return SimpleNamespace(
            league_id=364366361,
            year=2026,
            current_week=1,
            nfl_week=1,
            scoringPeriodId=1,
            finalScoringPeriod=16,
            teams=[team],
            settings=settings,
            player_map={10: "Player One", "Player One": 10, 20: "Player Two"},
            get_team_data=lambda team_id: team if team_id == 3 else None,
            espn_request=request,
        )

    def test_health_does_not_return_credentials(self):
        with mock.patch.dict(
            os.environ,
            {"ESPN_S2": "private-s2", "ESPN_SWID": "private-swid"},
            clear=True,
        ):
            result = run(
                server.get_connection_health(
                    league_id=364366361,
                    ctx=SimpleNamespace(),
                    year=2026,
                    live_check=False,
                )
            )
        encoded = json.dumps(result)
        self.assertTrue(result["credentials_present"])
        self.assertTrue(result["read_only"])
        self.assertNotIn("private-s2", encoded)
        self.assertNotIn("private-swid", encoded)

    def test_expired_credentials_return_safe_health_error(self):
        with (
            mock.patch.dict(
                os.environ,
                {"ESPN_S2": "expired-s2", "ESPN_SWID": "expired-swid"},
                clear=True,
            ),
            mock.patch.object(
                server,
                "_get_league",
                new=mock.AsyncMock(
                    side_effect=server.ESPNAccessDenied("remote secret")
                ),
            ),
        ):
            result = run(
                server.get_connection_health(
                    league_id=364366361,
                    ctx=SimpleNamespace(),
                    year=2026,
                    live_check=True,
                )
            )
        self.assertFalse(result["accessible"])
        self.assertEqual(result["error_type"], "ESPNAccessDenied")
        self.assertNotIn("expired-s2", json.dumps(result))
        self.assertNotIn("remote secret", json.dumps(result))

    def test_roster_returns_week_fields_without_full_stats(self):
        league = self.league()
        with mock.patch.object(
            server, "_get_league", new=mock.AsyncMock(return_value=league)
        ):
            result = run(
                server.get_team_roster(
                    364366361, 3, SimpleNamespace(), 2026, stats_week=1
                )
            )
        row = result["roster"][0]
        self.assertEqual(row["projected_points"], 12.3)
        self.assertNotIn("stats", row)

    def test_settings_returns_live_sections(self):
        league = self.league()
        league.espn_request.league_get.return_value = {
            "status": {"finalScoringPeriod": 16, "isActive": True},
            "settings": {
                "rosterSettings": {"lineupLocktimeType": "INDIVIDUAL_GAME"},
                "acquisitionSettings": {"acquisitionType": "WAIVERS_TRADITIONAL"},
                "tradeSettings": {"revisionHours": 24},
                "scheduleSettings": {"playoffTeamCount": 4},
                "scoringSettings": {
                    "scoringItems": [
                        {"statId": 53, "points": 1},
                        {"statId": 99, "points": 0},
                    ]
                },
            },
        }
        with mock.patch.object(
            server, "_get_league", new=mock.AsyncMock(return_value=league)
        ):
            result = run(server.get_league_settings(364366361, SimpleNamespace(), 2026))
        self.assertEqual(result["schedule"]["playoffTeamCount"], 4)
        self.assertEqual(result["scoring"], [{"stat_id": 53, "points": 1}])

    def test_player_pool_honors_page_and_position(self):
        league = self.league()
        league.espn_request.league_get.return_value = {
            "players": [
                {
                    "status": "FREEAGENT",
                    "player": {
                        "id": 20,
                        "fullName": "Player Two",
                        "defaultPositionId": 3,
                        "eligibleSlots": [4, 20],
                        "proTeamId": 1,
                    },
                }
            ]
        }
        with mock.patch.object(
            server, "_get_league", new=mock.AsyncMock(return_value=league)
        ):
            result = run(
                server.get_player_pool(
                    364366361,
                    SimpleNamespace(),
                    2026,
                    week=1,
                    limit=10,
                    position="WR",
                )
            )
        self.assertEqual(result["players"][0]["name"], "Player Two")
        header = league.espn_request.league_get.call_args.kwargs["headers"]
        self.assertIn('"value": [4]', header["x-fantasy-filter"])

    def test_week_context_includes_both_lineups_and_locks(self):
        league = self.league()
        home = self.team()
        away = self.team(2, "Opponent")
        box_player = SimpleNamespace(
            playerId=10,
            name="Player One",
            position="RB",
            eligibleSlots=["RB", "BE"],
            proTeam="BUF",
            slot_position="RB",
            projected_points=12.3,
            points=0,
            injuryStatus="ACTIVE",
            injured=False,
            pro_opponent="NE",
            pro_pos_rank=10,
            game_date=None,
            game_played=0,
            on_bye_week=False,
        )
        box = SimpleNamespace(
            home_team=home,
            away_team=away,
            home_score=0,
            away_score=0,
            home_projected=100,
            away_projected=99,
            home_lineup=[box_player],
            away_lineup=[box_player],
        )
        league.box_scores = mock.Mock(return_value=[box])
        with mock.patch.object(
            server, "_get_league", new=mock.AsyncMock(return_value=league)
        ):
            result = run(
                server.get_week_context(364366361, 3, SimpleNamespace(), 2026, 1)
            )
        self.assertFalse(result["home"]["lineup"][0]["locked"])
        self.assertEqual(result["away"]["team_name"], "Opponent")

    def test_transactions_are_paged_and_compact(self):
        league = self.league()
        league.espn_request.league_get.return_value = {
            "transactions": [
                {
                    "id": "tx-1",
                    "type": "WAIVER",
                    "status": "EXECUTED",
                    "teamId": 3,
                    "scoringPeriodId": 1,
                    "items": [{"type": "ADD", "playerId": 20, "toTeamId": 3}],
                }
            ]
        }
        with mock.patch.object(
            server, "_get_league", new=mock.AsyncMock(return_value=league)
        ):
            result = run(
                server.get_transactions(364366361, SimpleNamespace(), 2026, limit=10)
            )
        self.assertEqual(
            result["transactions"][0]["items"][0]["player_name"], "Player Two"
        )

    def test_activity_cursor_returns_only_new_rows(self):
        league = self.league()
        activity = SimpleNamespace(
            date=1_800_000_000_000,
            actions=[
                (
                    self.team(),
                    "ADDED",
                    SimpleNamespace(playerId=20, name="Player Two"),
                    0,
                )
            ],
        )
        league.recent_activity = mock.Mock(return_value=[activity])
        with mock.patch.object(
            server, "_get_league", new=mock.AsyncMock(return_value=league)
        ):
            first = run(server.get_activity(364366361, SimpleNamespace(), 2026))
            second = run(
                server.get_activity(
                    364366361,
                    SimpleNamespace(),
                    2026,
                    after_cursor=first["next_cursor"],
                )
            )
        self.assertEqual(first["count"], 1)
        self.assertEqual(second["count"], 0)

    def test_schedule_includes_nfl_kickoff_times(self):
        league = self.league()
        home = self.team()
        away = self.team(2, "Opponent")
        matchup = SimpleNamespace(
            home_team=home,
            away_team=away,
            home_score=0,
            away_score=0,
            home_lineup=[SimpleNamespace(proTeam="BUF", game_date=None)],
            away_lineup=[SimpleNamespace(proTeam="NE", game_date=None)],
        )
        league.scoreboard = mock.Mock(return_value=[matchup])
        league.box_scores = mock.Mock(return_value=[matchup])
        with mock.patch.object(
            server, "_get_league", new=mock.AsyncMock(return_value=league)
        ):
            result = run(server.get_schedule(364366361, SimpleNamespace(), 2026, 1))
        self.assertEqual(
            result["nfl_kickoffs"],
            [
                {"pro_team": "BUF", "game_time": None},
                {"pro_team": "NE", "game_time": None},
            ],
        )

    def test_waiver_report_hides_opponent_pending_claim(self):
        league = self.league()
        league.espn_request.get_league_offers.return_value = {
            "transactions": [
                {"id": "mine", "status": "PENDING", "teamId": 3, "items": []},
                {"id": "theirs", "status": "PENDING", "teamId": 2, "items": []},
                {"id": "done", "status": "EXECUTED", "teamId": 2, "items": []},
            ]
        }
        with mock.patch.object(
            server, "_get_league", new=mock.AsyncMock(return_value=league)
        ):
            result = run(
                server.get_waiver_report(364366361, 3, SimpleNamespace(), 2026, 1)
            )
        self.assertEqual(
            [row["transaction_id"] for row in result["waivers"]], ["mine", "done"]
        )

    def test_default_payload_is_under_twelve_kilobytes(self):
        league = self.league()
        with mock.patch.object(
            server, "_get_league", new=mock.AsyncMock(return_value=league)
        ):
            result = run(server.get_team_roster(364366361, 3, SimpleNamespace(), 2026))
        self.assertLess(len(json.dumps(result).encode("utf-8")), 12_000)


class SafetyTests(unittest.TestCase):
    def test_server_has_no_write_or_draft_join_code(self):
        root = Path(__file__).resolve().parents[1]
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [root / "espn_fantasy_server.py", root / "pyproject.toml"]
        )
        for banned in ("websockets", "JOIN_DRAFT", "session.post(", "requests.post("):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
