import unittest
import os
import shutil
import tempfile
from planner.app import App
from planner import tasks_mgr, memory

class TestTasks(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        from planner.config import load
        cfg = load()
        cfg["llm"]["base_url"] = "" # Keep unit tests offline and fast
        self.app = App(cfg=cfg, db_path=self.db_path)
        self.app.events = lambda **k: []

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.temp_dir)

    def test_task_lifecycle_and_feedback(self):
        # Create task via heuristic fallback / planning
        task = tasks_mgr.submit_task_input(self.app, "Implement coreference evaluation metrics", category="thesis")
        self.assertIsNotNone(task)
        self.assertEqual(task["category"], "thesis")
        self.assertGreater(len(task["subtasks"]), 0)
        self.assertGreater(len(task["blocks"]), 0)

        # Move block
        b0 = task["blocks"][0]
        moved = tasks_mgr.move_block(self.app, b0["id"], "2026-11-04T10:00", "2026-11-04T11:00")
        self.assertEqual(moved["start"], "2026-11-04T10:00")
        self.assertEqual(moved["pinned"], 1)

        # Complete subtask with actual duration
        s0 = task["subtasks"][0]
        res = tasks_mgr.complete_subtask(self.app, s0["id"], actual_minutes=40)
        self.assertEqual(res["subtasks"][0]["status"], "done")

        # Feedback for entire task
        fb = tasks_mgr.task_feedback(self.app, task["id"], actual_minutes=75, note="done quickly")
        self.assertTrue(fb["ok"])
        r = memory.ratio(self.app.db, "task", "thesis")
        self.assertGreater(r["n"], 0)

        # Delete task
        deleted = tasks_mgr.delete_task(self.app, task["id"])
        self.assertTrue(deleted)
        self.assertIsNone(tasks_mgr.get_task_full(self.app, task["id"]))

    def test_rename_task(self):
        task = tasks_mgr.submit_task_input(self.app, "Initial Task Name", category="thesis")
        tid = task["id"]
        renamed = tasks_mgr.rename_task(self.app, tid, "Updated Task Name")
        self.assertEqual(renamed["title"], "Updated Task Name")
        blocks = self.app.db.q("SELECT * FROM blocks WHERE task_id=?", (tid,))
        for b in blocks:
            self.assertIn("Updated Task Name", b["title"])

    def test_collision_detection_and_auto_nudge(self):
        mock_events = [{
            "id": "habit:lunch",
            "title": "Lunch Break",
            "calendar": "Habits",
            "start": "2026-11-04T13:00:00",
            "end": "2026-11-04T14:00:00",
            "all_day": False
        }]
        self.app.events = lambda **k: mock_events

        conflicts = tasks_mgr.find_conflicting_events(self.app, "2026-11-04T13:30", "2026-11-04T14:30")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["title"], "Lunch Break")

        task = tasks_mgr.submit_task_input(self.app, "Test Collision Task", category="thesis")
        b0 = task["blocks"][0]
        moved = tasks_mgr.move_block(self.app, b0["id"], "2026-11-04T13:00", "2026-11-04T14:00")
        self.assertNotEqual(moved["start"], "2026-11-04T13:00")
        self.assertEqual(moved["start"], "2026-11-04T14:10")

    def test_reschedule_inside_task_drawer(self):
        # Create Task A and Task B
        task_a = tasks_mgr.submit_task_input(self.app, "Exercise Sheet 3 completion", category="thesis")
        task_b = tasks_mgr.submit_task_input(self.app, "Study Hybrid Quantum Lesson 5", category="thesis")
        tid_a = task_a["id"]
        tid_b = task_b["id"]

        from planner.agents import task_agent
        ctx = task_agent.build_context(self.app, {
            "input_text": "Reschedule to Monday at 09:00",
            "target_task_id": tid_a,
            "category": "thesis"
        })
        self.assertIn(f"ACTIVE TARGET TASK (Opened in Drawer): Task #{tid_a}", ctx)
        self.assertIn("Exercise Sheet 3 completion", ctx)

        # Execute rescheduling targeting Task A
        res = tasks_mgr.submit_task_input(self.app, "Reschedule to Monday at 09:00", target_task_id=tid_a)
        self.assertEqual(res["task_id"], tid_a)
        self.assertIn(f"Task #{tid_a}", res["reply"])
        self.assertIn("Exercise Sheet 3 completion", res["reply"])

        # Ensure Task B blocks were NOT touched
        b_blocks = self.app.db.q("SELECT * FROM blocks WHERE task_id=?", (tid_b,))
        for b in b_blocks:
            self.assertIn("Study Hybrid Quantum Lesson 5", b["title"])

    def test_reschedule_subtasks_set_from_calendar_inspector(self):
        task = tasks_mgr.submit_task_input(self.app, "Implement LLM pipeline and evaluate", category="thesis")
        tid = task["id"]
        blocks_before = self.app.db.q("SELECT * FROM blocks WHERE task_id=? ORDER BY start", (tid,))
        self.assertGreaterEqual(len(blocks_before), 1)

        # Reschedule entire subtask set starting tomorrow
        res = tasks_mgr.submit_task_input(self.app, "Reschedule subtasks starting tomorrow morning", target_task_id=tid)
        self.assertEqual(res["task_id"], tid)
        blocks_after = self.app.db.q("SELECT * FROM blocks WHERE task_id=? ORDER BY start", (tid,))
        self.assertGreaterEqual(len(blocks_after), 1)
        for b in blocks_after:
            self.assertEqual(b["task_id"], tid)
            self.assertTrue(b["start"])
            self.assertTrue(b["end"])

    def test_weekend_scheduling_with_earliest_datetime(self):
        task = tasks_mgr.submit_task_input(self.app, "Validate audit results and write report", category="thesis")
        tid = task["id"]
        # Reschedule to Saturday 2026-11-07 at 11:20
        res = tasks_mgr.reschedule_task_blocks(self.app, tid, earliest="2026-11-07T11:20", preferred_time="morning")
        blocks = self.app.db.q("SELECT * FROM blocks WHERE task_id=? ORDER BY start", (tid,))
        self.assertTrue(len(blocks) >= 1)
        self.assertEqual(blocks[0]["start"], "2026-11-07T11:20")

    def test_clean_ghost_calendar_events_on_reschedule(self):
        task = tasks_mgr.submit_task_input(self.app, "Manual validation of audits", category="thesis")
        tid = task["id"]
        deleted_events = []
        self.app.cal.delete = lambda eid: deleted_events.append(eid) or True
        # Simulate live calendar containing a ghost event from previous schedule
        ghost_eid = "GHOST-EVENT-1234"
        self.app.events = lambda **k: [
            {"id": ghost_eid, "title": "Manual validation of audits", "notes": f"Planner Task #{tid}: Manual validation\nplanner:block=999"}
        ]
        tasks_mgr.reschedule_task_blocks(self.app, tid, earliest="2026-11-07T11:20")
        self.assertIn(ghost_eid, deleted_events)

if __name__ == "__main__":
    unittest.main()
