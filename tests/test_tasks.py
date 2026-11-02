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

if __name__ == "__main__":
    unittest.main()
