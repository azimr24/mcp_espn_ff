import unittest
from types import SimpleNamespace
from unittest import mock

import espn_fantasy_server as server


class CacheTests(unittest.TestCase):
    @mock.patch.object(server, "League")
    @mock.patch.dict(
        server.os.environ,
        {"ESPN_S2": "environment-s2", "ESPN_SWID": "environment-swid"},
        clear=True,
    )
    def test_environment_credentials_are_used_when_session_has_none(
        self, league_class
    ):
        api = server.ESPNFantasyFootballAPI()

        api.get_league("session-1", 123, 2026)

        league_class.assert_called_once_with(
            league_id=123,
            year=2026,
            espn_s2="environment-s2",
            swid="environment-swid",
        )

    @mock.patch.object(server, "League")
    def test_cache_key_does_not_contain_credentials(self, league_class):
        api = server.ESPNFantasyFootballAPI()
        api.store_credentials("session-1", "secret-s2", "secret-swid")

        api.get_league("session-1", 123, 2025)

        self.assertEqual(len(api.leagues), 1)
        cache_key = next(iter(api.leagues))
        self.assertNotIn("secret-s2", repr(cache_key))
        self.assertNotIn("secret-swid", repr(cache_key))
        league_class.assert_called_once_with(
            league_id=123,
            year=2025,
            espn_s2="secret-s2",
            swid="secret-swid",
        )

    @mock.patch.object(server, "League")
    def test_logout_removes_credentials_and_cached_leagues(self, league_class):
        api = server.ESPNFantasyFootballAPI()
        api.store_credentials("session-1", "secret-s2", "secret-swid")
        api.get_league("session-1", 123, 2025)
        api.create_write_confirmation("session-1", {"kind": "ADD_DROP"})

        api.clear_credentials("session-1")

        self.assertNotIn("session-1", api.credentials)
        self.assertFalse(api.leagues)
        self.assertFalse(api.drafts)
        self.assertFalse(api.write_confirmations)

    @mock.patch.object(server, "League")
    def test_expired_league_is_reloaded(self, league_class):
        api = server.ESPNFantasyFootballAPI(cache_ttl_seconds=10)
        with mock.patch.object(server.time, "monotonic", side_effect=[1, 20, 20]):
            api.get_league("session-1", 123, 2025)
            api.get_league("session-1", 123, 2025)

        self.assertEqual(league_class.call_count, 2)

    @mock.patch.object(server, "League")
    def test_short_draft_cache_reuses_one_espn_response(self, league_class):
        league = SimpleNamespace(
            espn_request=SimpleNamespace(
                get_league_draft=mock.Mock(return_value={"draftDetail": {"picks": []}})
            )
        )
        league_class.return_value = league
        api = server.ESPNFantasyFootballAPI(draft_cache_ttl_seconds=10)

        api.get_draft("session-1", 123, 2026)
        api.get_draft("session-1", 123, 2026)

        league.espn_request.get_league_draft.assert_called_once_with()

    def test_credential_sessions_have_a_fixed_limit(self):
        api = server.ESPNFantasyFootballAPI(max_credential_sessions=2)
        api.store_credentials("session-1", "s2-1", "swid-1")
        api.store_credentials("session-2", "s2-2", "swid-2")
        api.store_credentials("session-3", "s2-3", "swid-3")

        self.assertNotIn("session-1", api.credentials)
        self.assertEqual(list(api.credentials), ["session-2", "session-3"])

    def test_write_confirmation_is_bound_and_single_use(self):
        api = server.ESPNFantasyFootballAPI()
        action = {"kind": "ADD_DROP", "team_id": 3}
        token = api.create_write_confirmation("session-1", action)

        with self.assertRaises(PermissionError):
            api.consume_write_confirmation(
                "session-1", token, {"kind": "ADD_DROP", "team_id": 7}
            )
        with self.assertRaises(PermissionError):
            api.consume_write_confirmation("session-1", token, action)

    def test_write_confirmation_expires(self):
        api = server.ESPNFantasyFootballAPI()
        action = {"kind": "LINEUP_SWAP", "team_id": 3}
        with mock.patch.object(server.time, "monotonic", return_value=1):
            token = api.create_write_confirmation("session-1", action)

        with (
            mock.patch.object(
                server.time,
                "monotonic",
                return_value=server.WRITE_CONFIRMATION_TTL_SECONDS + 2,
            ),
            self.assertRaises(PermissionError),
        ):
            api.consume_write_confirmation("session-1", token, action)


class SessionIdTests(unittest.TestCase):
    class Session:
        pass

    def test_session_id_is_stable_and_unique(self):
        first = SimpleNamespace(session=self.Session())
        second = SimpleNamespace(session=self.Session())

        self.assertEqual(server._session_id(first), server._session_id(first))
        self.assertNotEqual(server._session_id(first), server._session_id(second))


class ToolTests(unittest.IsolatedAsyncioTestCase):
    class Session:
        pass

    @classmethod
    def context(cls):
        return SimpleNamespace(session=cls.Session())

    @staticmethod
    def draft_league():
        teams = {
            3: SimpleNamespace(team_id=3, team_name="azim_the_dream"),
            7: SimpleNamespace(team_id=7, team_name="Opponent"),
        }
        return SimpleNamespace(
            league_id=364366361,
            year=2026,
            teams=list(teams.values()),
            player_map={101: "Player One", 102: "Player Two"},
            get_team_data=lambda team_id: teams.get(team_id),
        )

    @staticmethod
    def draft_detail():
        return {
            "drafted": False,
            "inProgress": True,
            "picks": [
                {
                    "overallPickNumber": 1,
                    "roundId": 1,
                    "roundPickNumber": 1,
                    "teamId": 7,
                    "playerId": 101,
                },
                {
                    "overallPickNumber": 2,
                    "roundId": 1,
                    "roundPickNumber": 2,
                    "teamId": 3,
                },
                {
                    "overallPickNumber": 3,
                    "roundId": 2,
                    "roundPickNumber": 1,
                    "teamId": 3,
                },
                {
                    "overallPickNumber": 4,
                    "roundId": 2,
                    "roundPickNumber": 2,
                    "teamId": 7,
                },
            ],
        }

    @staticmethod
    def write_league():
        def league_get(params=None, headers=None):
            if params == {"view": "mSettings"}:
                return {"settings": {"draftSettings": {"type": "SNAKE"}}}
            return {
                "players": [
                    {
                        "status": "FREEAGENT",
                        "player": {"id": 303},
                    }
                ]
            }

        starter = SimpleNamespace(
            playerId=201,
            name="Starter",
            lineupSlot="RB",
            eligibleSlots=["RB", "RB/WR/TE", "BE"],
        )
        bench = SimpleNamespace(
            playerId=202,
            name="Bench Player",
            lineupSlot="BE",
            eligibleSlots=["RB", "RB/WR/TE", "BE"],
        )
        team = SimpleNamespace(
            team_id=3,
            team_name="azim_the_dream",
            roster=[starter, bench],
            owners=[{"id": "private-swid"}],
        )
        response = SimpleNamespace(status_code=200, json=lambda: {})
        security_response = SimpleNamespace(status_code=200, json=lambda: 12345678901)
        request = SimpleNamespace(
            cookies={"espn_s2": "private-s2", "SWID": "private-swid"},
            timeout=(3.05, 30),
            LEAGUE_ENDPOINT=(
                "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
                "seasons/2026/segments/0/leagues/364366361"
            ),
            league_get=mock.Mock(side_effect=league_get),
            session=SimpleNamespace(
                get=mock.Mock(side_effect=[security_response, response]),
                post=mock.Mock(return_value=response),
            ),
        )
        league = SimpleNamespace(
            league_id=364366361,
            year=2026,
            scoringPeriodId=1,
            finalScoringPeriod=17,
            teams=[team],
            player_map={201: "Starter", 202: "Bench Player", 303: "Free Agent"},
            get_team_data=lambda team_id: team if team_id == 3 else None,
            espn_request=request,
        )
        return league

    async def test_roster_uses_real_team_id_and_omits_full_stats(self):
        player = SimpleNamespace(
            playerId=99,
            name="Example Player",
            position="QB",
            proTeam="BUF",
            total_points=100,
            projected_total_points=110,
            injured=False,
            stats={1: {"points": 20}, 2: {"points": 25}},
        )
        team = SimpleNamespace(
            team_id=7,
            team_name="Example Team",
            owners=[],
            wins=1,
            losses=0,
            roster=[player],
        )
        league = SimpleNamespace(
            teams=[team],
            get_team_data=lambda team_id: team if team_id == 7 else None,
        )

        with mock.patch.object(
            server, "_get_league", new=mock.AsyncMock(return_value=league)
        ):
            result = await server.get_team_roster(
                league_id=123,
                team_id=7,
                ctx=SimpleNamespace(),
                year=2025,
            )

        self.assertEqual(result["team_id"], 7)
        self.assertEqual(result["roster"][0]["player_id"], 99)
        self.assertNotIn("week_stats", result["roster"][0])

    async def test_roster_rejects_unknown_team_id(self):
        league = SimpleNamespace(teams=[], get_team_data=lambda team_id: None)
        with mock.patch.object(
            server, "_get_league", new=mock.AsyncMock(return_value=league)
        ):
            with self.assertRaises(ValueError):
                await server.get_team_roster(
                    league_id=123,
                    team_id=4,
                    ctx=SimpleNamespace(),
                    year=2025,
                )

    async def test_draft_state_includes_current_and_last_pick(self):
        league = self.draft_league()
        with mock.patch.object(
            server,
            "_get_draft",
            new=mock.AsyncMock(return_value=(league, self.draft_detail())),
        ):
            result = await server.get_draft_state(
                league_id=364366361,
                ctx=SimpleNamespace(),
                year=2026,
            )

        self.assertTrue(result["in_progress"])
        self.assertEqual(result["completed_pick_count"], 1)
        self.assertEqual(result["next_pick"]["overall_pick"], 2)
        self.assertEqual(result["next_pick"]["team_name"], "azim_the_dream")
        self.assertEqual(result["last_pick"]["player_name"], "Player One")

    async def test_draft_picks_default_to_completed_and_support_scheduled(self):
        league = self.draft_league()
        with mock.patch.object(
            server,
            "_get_draft",
            new=mock.AsyncMock(return_value=(league, self.draft_detail())),
        ):
            completed = await server.get_draft_picks(
                league_id=364366361,
                ctx=SimpleNamespace(),
                year=2026,
            )
            scheduled = await server.get_draft_picks(
                league_id=364366361,
                ctx=SimpleNamespace(),
                year=2026,
                limit=2,
                offset=1,
                include_scheduled=True,
            )

        self.assertEqual(completed["total"], 1)
        self.assertEqual(completed["picks"][0]["player_name"], "Player One")
        self.assertEqual(scheduled["count"], 2)
        self.assertTrue(scheduled["has_more"])
        self.assertEqual(scheduled["picks"][0]["overall_pick"], 2)

    async def test_draft_pool_returns_only_compact_player_fields(self):
        league = self.draft_league()
        players = [{"player_id": 10, "name": "Available Player"}]
        with (
            mock.patch.object(
                server,
                "_get_league",
                new=mock.AsyncMock(return_value=league),
            ),
            mock.patch.object(
                server,
                "_get_draft_pool_data",
                return_value=players,
            ) as pool,
        ):
            result = await server.get_draft_pool(
                league_id=364366361,
                ctx=SimpleNamespace(),
                year=2026,
                limit=15,
                position="WR",
            )

        pool.assert_called_once_with(league, 2026, 15, 0, "WR")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["players"], players)

    async def test_draft_context_combines_team_picks_and_available_players(self):
        league = self.draft_league()
        players = [{"player_id": 10, "name": "Available Player"}]
        with (
            mock.patch.object(
                server,
                "_get_draft",
                new=mock.AsyncMock(return_value=(league, self.draft_detail())),
            ),
            mock.patch.object(
                server,
                "_get_draft_pool_data",
                return_value=players,
            ),
        ):
            result = await server.get_draft_context(
                league_id=364366361,
                team_id=3,
                ctx=SimpleNamespace(),
                year=2026,
            )

        self.assertEqual(result["next_team_pick"]["overall_pick"], 2)
        self.assertEqual(result["picks_until_team"], 0)
        self.assertEqual(result["available_players"], players)

    async def test_draft_query_validation_rejects_large_payloads(self):
        with self.assertRaises(ValueError):
            await server.get_draft_pool(
                league_id=364366361,
                ctx=SimpleNamespace(),
                year=2026,
                limit=201,
            )

    async def test_execute_write_fails_closed_when_disabled(self):
        get_league = mock.AsyncMock()
        with (
            mock.patch.dict(server.os.environ, {}, clear=True),
            mock.patch.object(server, "_get_league", new=get_league),
            self.assertRaises(PermissionError),
        ):
            await server.execute_add_drop(
                league_id=364366361,
                team_id=3,
                add_player_id=303,
                drop_player_id=202,
                confirmation_token="invalid",
                ctx=self.context(),
                year=2026,
            )

        get_league.assert_not_awaited()

    async def test_draft_write_needs_both_enable_flags(self):
        get_draft = mock.AsyncMock()
        with (
            mock.patch.dict(
                server.os.environ, {"ESPN_WRITE_ENABLED": "true"}, clear=True
            ),
            mock.patch.object(server, "_get_draft", new=get_draft),
            self.assertRaises(PermissionError),
        ):
            await server.execute_draft_pick(
                league_id=364366361,
                team_id=3,
                player_id=303,
                confirmation_token="invalid",
                ctx=self.context(),
                year=2026,
            )

        get_draft.assert_not_awaited()

    async def test_preview_and_execute_draft_pick_use_live_select_protocol(self):
        league = self.write_league()
        ctx = self.context()
        with (
            mock.patch.dict(
                server.os.environ,
                {
                    "ESPN_WRITE_ENABLED": "true",
                    "ESPN_DRAFT_WRITE_ENABLED": "true",
                },
                clear=True,
            ),
            mock.patch.object(
                server,
                "_get_draft",
                new=mock.AsyncMock(return_value=(league, self.draft_detail())),
            ),
        ):
            preview = await server.preview_draft_pick(
                league_id=364366361,
                team_id=3,
                player_id=303,
                ctx=ctx,
                year=2026,
            )
            result = await server.execute_draft_pick(
                league_id=364366361,
                team_id=3,
                player_id=303,
                confirmation_token=preview["confirmation_token"],
                ctx=ctx,
                year=2026,
            )

        security_call, select_call = league.espn_request.session.get.call_args_list
        self.assertTrue(security_call.args[0].endswith("/teams/3/draftSecurity"))
        self.assertEqual(
            select_call.args[0],
            "https://fantasydraft.espn.com/game-ffl/league-364366361/SELECT",
        )
        self.assertEqual(select_call.kwargs["params"]["1"], 303)
        self.assertEqual(
            select_call.kwargs["params"]["token"],
            "1:364366361:3:private-swid:12345678901",
        )
        self.assertEqual(preview["overall_pick"], 2)
        self.assertTrue(result["submitted"])
        self.assertNotIn("confirmation_token", result)

    async def test_preview_and_execute_add_drop_use_confirmed_body(self):
        league = self.write_league()
        ctx = self.context()
        with (
            mock.patch.dict(
                server.os.environ, {"ESPN_WRITE_ENABLED": "true"}, clear=True
            ),
            mock.patch.object(
                server, "_get_league", new=mock.AsyncMock(return_value=league)
            ),
        ):
            preview = await server.preview_add_drop(
                league_id=364366361,
                team_id=3,
                add_player_id=303,
                drop_player_id=202,
                ctx=ctx,
                year=2026,
            )
            result = await server.execute_add_drop(
                league_id=364366361,
                team_id=3,
                add_player_id=303,
                drop_player_id=202,
                confirmation_token=preview["confirmation_token"],
                ctx=ctx,
                year=2026,
            )

        call = league.espn_request.session.post.call_args
        self.assertEqual(
            call.kwargs["json"],
            {
                "isLeagueManager": False,
                "teamId": 3,
                "scoringPeriodId": 1,
                "executionType": "EXECUTE",
                "type": "FREEAGENT",
                "items": [
                    {"playerId": 303, "type": "ADD", "toTeamId": 3},
                    {"playerId": 202, "type": "DROP", "fromTeamId": 3},
                ],
            },
        )
        self.assertTrue(result["executed"])
        self.assertNotIn("confirmation_token", result)
        with self.assertRaises(PermissionError):
            server.api.consume_write_confirmation(
                server._session_id(ctx), preview["confirmation_token"], {}
            )

    async def test_preview_and_execute_lineup_swap_use_confirmed_body(self):
        league = self.write_league()
        ctx = self.context()
        with (
            mock.patch.dict(
                server.os.environ, {"ESPN_WRITE_ENABLED": "true"}, clear=True
            ),
            mock.patch.object(
                server, "_get_league", new=mock.AsyncMock(return_value=league)
            ),
        ):
            preview = await server.preview_lineup_swap(
                league_id=364366361,
                team_id=3,
                starter_player_id=201,
                bench_player_id=202,
                ctx=ctx,
                year=2026,
            )
            result = await server.execute_lineup_swap(
                league_id=364366361,
                team_id=3,
                starter_player_id=201,
                bench_player_id=202,
                confirmation_token=preview["confirmation_token"],
                ctx=ctx,
                year=2026,
            )

        call = league.espn_request.session.post.call_args
        self.assertEqual(call.kwargs["json"]["type"], "ROSTER")
        self.assertEqual(call.kwargs["json"]["memberId"], "private-swid")
        self.assertEqual(
            call.kwargs["json"]["items"],
            [
                {
                    "playerId": 202,
                    "type": "LINEUP",
                    "fromLineupSlotId": 20,
                    "toLineupSlotId": 2,
                    "fromTeamId": 0,
                    "toTeamId": 0,
                },
                {
                    "playerId": 201,
                    "type": "LINEUP",
                    "fromLineupSlotId": 2,
                    "toLineupSlotId": 20,
                    "fromTeamId": 0,
                    "toTeamId": 0,
                },
            ],
        )
        self.assertTrue(result["executed"])


if __name__ == "__main__":
    unittest.main()
