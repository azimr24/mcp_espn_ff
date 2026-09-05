import unittest

from scripts.draft_conductor import DraftTracker


def scheduled_picks():
    return [
        {
            "overallPickNumber": 1,
            "roundId": 1,
            "roundPickNumber": 1,
            "teamId": 5,
        },
        {
            "overallPickNumber": 2,
            "roundId": 1,
            "roundPickNumber": 2,
            "teamId": 3,
        },
        {
            "overallPickNumber": 3,
            "roundId": 1,
            "roundPickNumber": 3,
            "teamId": 7,
        },
    ]


class DraftTrackerTests(unittest.TestCase):
    def tracker(self):
        return DraftTracker(
            league_id=364366361,
            year=2026,
            team_id=3,
            scheduled_picks=scheduled_picks(),
            player_map={101: "Player One", 102: "Player Two"},
        )

    def test_rest_snapshot_records_picks_and_sets_next_team(self):
        tracker = self.tracker()
        picks = scheduled_picks()
        picks[0]["playerId"] = 101

        new_picks = tracker.merge_rest(
            {"inProgress": True, "drafted": False, "picks": picks}
        )

        self.assertEqual(len(new_picks), 1)
        self.assertEqual(new_picks[0]["player_name"], "Player One")
        self.assertEqual(tracker.snapshot()["current_team_id"], 3)
        self.assertEqual(tracker.snapshot()["next_pick"]["overall_pick"], 2)

    def test_stream_selection_records_pick_and_advances(self):
        tracker = self.tracker()

        tracker.apply_message("SELECTING 5 30000")
        event = tracker.apply_message("SELECTED 5 101 0")

        self.assertEqual(event["pick"]["overall_pick"], 1)
        self.assertEqual(event["pick"]["source"], "draft_stream")
        self.assertEqual(tracker.snapshot()["current_team_id"], 3)
        self.assertIsNone(tracker.snapshot()["time_to_pick_ms"])

    def test_live_selecting_state_wins_over_stale_rest_state(self):
        tracker = self.tracker()
        tracker.apply_message("SELECTED 5 101 0")
        tracker.apply_message("SELECTING 3 28000")

        tracker.merge_rest(
            {"inProgress": True, "drafted": False, "picks": scheduled_picks()}
        )

        self.assertEqual(tracker.snapshot()["current_team_id"], 3)
        self.assertEqual(tracker.snapshot()["time_to_pick_ms"], 28000)

    def test_repeated_rest_snapshot_does_not_duplicate_pick(self):
        tracker = self.tracker()
        picks = scheduled_picks()
        picks[0]["playerId"] = 101
        draft = {"inProgress": True, "drafted": False, "picks": picks}

        first = tracker.merge_rest(draft)
        second = tracker.merge_rest(draft)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(tracker.snapshot()["completed_pick_count"], 1)

    def test_restore_recovers_completed_picks(self):
        tracker = self.tracker()
        tracker.restore(
            {
                "league_id": 364366361,
                "year": 2026,
                "team_id": 3,
                "completed_picks": [
                    {
                        "overall_pick": 1,
                        "team_id": 5,
                        "player_id": 101,
                        "player_name": "Player One",
                        "slot_id": 0,
                    }
                ],
            }
        )

        state = tracker.snapshot()

        self.assertEqual(state["completed_pick_count"], 1)
        self.assertEqual(state["next_pick"]["overall_pick"], 2)

    def test_effective_draft_includes_stream_only_pick(self):
        tracker = self.tracker()
        tracker.apply_message("SELECTED 5 101 0")

        draft = {
            "inProgress": True,
            "drafted": False,
            "picks": scheduled_picks(),
        }
        effective = tracker.effective_draft(draft)

        self.assertEqual(effective["picks"][0]["playerId"], 101)

    def test_error_message_does_not_copy_remote_content(self):
        tracker = self.tracker()

        event = tracker.apply_message("ERROR secret-token server-details")

        self.assertEqual(event["message"], "ESPN rejected a draft command.")
        self.assertNotIn("secret-token", event["message"])


if __name__ == "__main__":
    unittest.main()
