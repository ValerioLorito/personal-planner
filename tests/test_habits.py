import unittest
import os
import shutil
import tempfile
from planner.app import App
from planner import habits_mgr, memory

class TestHabits(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.app = App(db_path=self.db_path)

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.temp_dir)

    def test_create_and_delete_habit(self):
        h = habits_mgr.create_habit(self.app, "Gym Workout", [0, 2, 4], "19:00", 90, "evening fitness")
        self.assertIsNotNone(h)
        self.assertEqual(h["name"], "Gym Workout")
        self.assertEqual(h["minutes"], 90)

        # Check DB presence
        habits = self.app.db.q("SELECT * FROM habits WHERE status='active'")
        self.assertEqual(len(habits), 1)

        # Delete habit
        ok = habits_mgr.delete_habit(self.app, h["id"])
        self.assertTrue(ok)
        habits_after = self.app.db.q("SELECT * FROM habits WHERE status='active'")
        self.assertEqual(len(habits_after), 0)

    def test_checkin_and_calibration(self):
        h = habits_mgr.create_habit(self.app, "Spanish", [0, 1, 2, 3, 4], "08:30", 30)
        # Checkin with actual 20 minutes (finished faster)
        res = habits_mgr.checkin_habit(self.app, h["id"], "2026-11-02", "done", actual_minutes=20)
        self.assertTrue(res["ok"])

        # Calibration ratio should now reflect habit sample
        r = memory.ratio(self.app.db, "habit", f"habit:{h['id']}")
        self.assertEqual(r["n"], 1)
        # Prior of 1.0 blended with 20/30 (0.67)
        self.assertLess(r["ratio"], 1.0)

    def test_rename_habit(self):
        h = habits_mgr.create_habit(self.app, "Initial Habit", [0, 2, 4], "08:00", 30)
        renamed = habits_mgr.rename_habit(self.app, h["id"], "Renamed Morning Routine")
        self.assertEqual(renamed["name"], "Renamed Morning Routine")

if __name__ == "__main__":
    unittest.main()
