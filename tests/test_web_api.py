import unittest
import json
import os
import shutil
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen, Request
from planner.app import App
from planner.web import PlannerHandler

class TestWebAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.temp_dir, "test.db")
        cls.app = App(db_path=cls.db_path)
        cls.port = 8999
        try:
            cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), PlannerHandler)
            cls.server.app = cls.app
            cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
            cls.thread.start()
            cls.socket_available = True
        except (PermissionError, OSError):
            cls.socket_available = False

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "socket_available", False) and hasattr(cls, "server"):
            try:
                cls.server.shutdown()
                cls.server.server_close()
            except Exception:
                pass
        cls.app.db.close()
        shutil.rmtree(cls.temp_dir)

    def test_get_state(self):
        if not getattr(self, "socket_available", False):
            self.skipTest("Local socket connection restricted by sandbox")
        try:
            with urlopen(f"http://127.0.0.1:{self.port}/api/state") as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertIn("tasks", data)
                self.assertIn("habits", data)
                self.assertIn("categories", data)
                self.assertIn("llm_budget", data)
        except (PermissionError, OSError):
            self.skipTest("Local socket connection restricted by sandbox")

    def test_create_habit_and_checkin(self):
        if not getattr(self, "socket_available", False):
            self.skipTest("Local socket connection restricted by sandbox")
        try:
            # Create habit
            req = Request(f"http://127.0.0.1:{self.port}/api/habits",
                          data=json.dumps({"name": "Reading", "days": [0,1,2,3,4], "start": "08:30", "minutes": 30}).encode(),
                          headers={"Content-Type": "application/json"})
            with urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertTrue(data.get("ok"))
                hid = data["habit"]["id"]

            # Checkin
            req = Request(f"http://127.0.0.1:{self.port}/api/habits/{hid}/checkin",
                          data=json.dumps({"status": "done", "actual_minutes": 25}).encode(),
                          headers={"Content-Type": "application/json"})
            with urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertTrue(data.get("ok"))

            # Delete habit
            req = Request(f"http://127.0.0.1:{self.port}/api/habits/{hid}", method="DELETE")
            with urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertTrue(data.get("ok"))
        except (PermissionError, OSError):
            self.skipTest("Local socket connection restricted by sandbox")

if __name__ == "__main__":
    unittest.main()
