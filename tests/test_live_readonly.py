import asyncio
import os
import unittest
from types import SimpleNamespace

import espn_fantasy_server as server


@unittest.skipUnless(
    os.environ.get("ESPN_LIVE_READ_TEST") == "1",
    "Set ESPN_LIVE_READ_TEST=1 for explicit live read-only tests.",
)
class LiveReadOnlyTests(unittest.TestCase):
    def test_core_read_tools(self):
        context = SimpleNamespace()

        async def run():
            health = await server.get_connection_health(364366361, context, 2026, True)
            settings = await server.get_league_settings(364366361, context, 2026)
            week = await server.get_week_context(364366361, 3, context, 2026, 1)
            pool = await server.get_player_pool(364366361, context, 2026, 1, 10)
            return health, settings, week, pool

        health, settings, week, pool = asyncio.run(run())
        self.assertTrue(health["accessible"])
        self.assertEqual(settings["league_id"], 364366361)
        self.assertEqual(week["managed_team_id"], 3)
        self.assertLessEqual(pool["count"], 10)


if __name__ == "__main__":
    unittest.main()
