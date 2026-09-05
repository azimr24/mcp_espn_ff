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

        api.clear_credentials("session-1")

        self.assertNotIn("session-1", api.credentials)
        self.assertFalse(api.leagues)

    @mock.patch.object(server, "League")
    def test_expired_league_is_reloaded(self, league_class):
        api = server.ESPNFantasyFootballAPI(cache_ttl_seconds=10)
        with mock.patch.object(server.time, "monotonic", side_effect=[1, 20, 20]):
            api.get_league("session-1", 123, 2025)
            api.get_league("session-1", 123, 2025)

        self.assertEqual(league_class.call_count, 2)

    def test_credential_sessions_have_a_fixed_limit(self):
        api = server.ESPNFantasyFootballAPI(max_credential_sessions=2)
        api.store_credentials("session-1", "s2-1", "swid-1")
        api.store_credentials("session-2", "s2-2", "swid-2")
        api.store_credentials("session-3", "s2-3", "swid-3")

        self.assertNotIn("session-1", api.credentials)
        self.assertEqual(list(api.credentials), ["session-2", "session-3"])


class SessionIdTests(unittest.TestCase):
    class Session:
        pass

    def test_session_id_is_stable_and_unique(self):
        first = SimpleNamespace(session=self.Session())
        second = SimpleNamespace(session=self.Session())

        self.assertEqual(server._session_id(first), server._session_id(first))
        self.assertNotEqual(server._session_id(first), server._session_id(second))


class ToolTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
