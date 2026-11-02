import unittest
from datetime import datetime, timedelta
from planner.scheduler import Scheduler, parse_deadline, merge

class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "work": {
                "days": [0, 1, 2, 3, 4],
                "weekend_if_needed": True,
                "day_start": "09:00",
                "day_end": "19:00",
                "max_block_min": 90,
                "min_block_min": 25,
                "buffer_min": 10,
                "max_day_min": 360,
                "max_task_day_min": 180,
                "deadline_margin_hours": 12,
                "now_margin_min": 10,
                "horizon_days": 14,
            }
        }
        self.fixed_now = datetime(2026, 11, 2, 8, 0, 0) # Monday 08:00

    def test_placement_in_gaps(self):
        # Meeting 10:00 to 12:00
        busy = [(datetime(2026, 11, 2, 10, 0), datetime(2026, 11, 2, 12, 0))]
        sch = Scheduler(self.cfg, busy=busy, now=self.fixed_now)

        subtasks = [
            {"key": 1, "title": "Subtask 1", "minutes": 45},
            {"key": 2, "title": "Subtask 2", "minutes": 60}
        ]
        res = sch.place(subtasks, deadline="2026-11-03T18:00")
        self.assertEqual(res["unplaced"], 0)
        self.assertEqual(len(res["blocks"]), 2)
        
        # Block 1 should start at 09:00 (first slot)
        b1 = res["blocks"][0]
        self.assertEqual(b1["start"], "2026-11-02T09:00")
        self.assertEqual(b1["end"], "2026-11-02T09:45")

        # Block 2 cannot fit before 10:00 (due to 10m buffer before 10:00 meeting),
        # so it must land after meeting + 10m buffer -> 12:10
        b2 = res["blocks"][1]
        self.assertEqual(b2["start"], "2026-11-02T12:10")
        self.assertEqual(b2["end"], "2026-11-02T13:10")

    def test_split_long_subtask(self):
        sch = Scheduler(self.cfg, busy=[], now=self.fixed_now)
        subtasks = [{"key": 1, "title": "Long study session", "minutes": 150}]
        res = sch.place(subtasks, max_block=90)
        self.assertEqual(res["unplaced"], 0)
        # Should be split into 90m + 60m
        self.assertEqual(len(res["blocks"]), 2)
        self.assertEqual(res["blocks"][0]["minutes"], 90)
        self.assertEqual(res["blocks"][1]["minutes"], 60)

if __name__ == "__main__":
    unittest.main()
