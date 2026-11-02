"""Tests for the agentic brain: context, validation, backends, calibration."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from planner import brain
from planner.store import Store

CFG = {
    "brain": {"backend": "llm",
              "llm": {"base_url": "", "model": "", "api_key_env": "X", "timeout": 5}},
    "work_days": [0, 1, 2, 3, 4], "day_start": "09:00", "day_end": "19:00",
    "lunch": ["13:00", "14:00"], "buffer_min": 10, "max_block_min": 90,
    "max_day_min": 300, "complexity_minutes": {"S": 45, "M": 90, "L": 180},
    "complexity_rank": {"S": 1, "M": 3, "L": 5},
}


class BrainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "p.db"))
        self.cfg = json.loads(json.dumps(CFG))
        # never let llm tests fall through to the real planner/data/llm_key
        self.cfg["brain"]["llm"]["key_file"] = os.path.join(self.tmp.name,
                                                            "llm_key")

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- validation ----------
    def test_validate_normalises_and_rejects(self):
        p = brain.validate_plan({"complexity": "m", "estimate_minutes": "120",
                                 "priority": 9, "confidence": 3,
                                 "subtasks": [{"title": "a", "minutes": 60}]})
        self.assertEqual(p["complexity"], "M")
        self.assertEqual(p["estimate_minutes"], 120)
        self.assertEqual(p["priority"], 3)          # clamped
        self.assertEqual(p["confidence"], 1.0)      # clamped
        for bad in ({"complexity": "X", "estimate_minutes": 10},
                    {"complexity": "S", "estimate_minutes": -5},
                    {"complexity": "S", "estimate_minutes": 10, "subtasks": "nope"},
                    "not a dict"):
            with self.assertRaises(brain.BrainError):
                brain.validate_plan(bad)

    # ---------- heuristic ----------
    def test_heuristic_write_task_is_large_with_subtasks(self):
        plan = brain.heuristic_plan(self.store, {"title": "Write the methods section",
                                                 "priority": 2})
        self.assertEqual(plan["complexity"], "L")
        self.assertGreaterEqual(plan["estimate_minutes"], 120)
        self.assertTrue(plan["subtasks"])
        self.assertEqual(plan["subtasks"][0]["minutes"] > 0, True)

    def test_heuristic_applies_calibration_ratio(self):
        self.store.kv_set("calibration:ratio", 1.5)
        plan = brain.heuristic_plan(self.store, {"title": "quick read", "priority": 3})
        self.assertGreaterEqual(plan["estimate_minutes"], 45)   # 30 * 1.5

    # ---------- context ----------
    def test_context_mentions_project_and_tasks(self):
        self.store.add_task("Rerun the 27B coref arm")
        ctx = brain.project_context(self.store, repo_root=".")
        self.assertIn("PROJECT PROFILE", ctx)
        self.assertIn("Rerun the 27B coref arm", ctx)
        self.assertIn("RECENT COMMITS", ctx)

    # ---------- heuristic backend end-to-end ----------
    def test_enrich_heuristic_writes_estimation(self):
        self.cfg["brain"]["backend"] = "heuristic"
        tid = self.store.add_task("Create an audit for coreference resolution")
        res = brain.enrich_task(self.store, self.cfg, tid, repo_root=".")
        self.assertEqual(res["status"], "applied")
        t = self.store.get_task(tid)
        self.assertEqual(t["est_source"], "heuristic")
        self.assertTrue(t["est_rationale"])
        self.assertIsNotNone(t["confidence"])
        self.assertIsInstance(json.loads(t["subtasks"]), list)
        kinds = [e["kind"] for e in self.store.recent_events(10)]
        self.assertIn("agent_estimate", kinds)

    # ---------- llm backend (mocked transport) ----------
    def test_llm_backend_uses_transport_and_applies(self):
        self.cfg["brain"]["backend"] = "llm"
        self.cfg["brain"]["llm"].update(base_url="https://example.invalid/v1",
                                        model="test-model")
        tid = self.store.add_task("Do a thing")
        calls = {}

        def fake_post(url, payload, headers, timeout=90):
            calls.update(url=url, payload=payload, headers=headers)
            return {"choices": [{"message": {"content": json.dumps({
                "title": "Do a thing", "area": "analysis", "complexity": "S",
                "estimate_minutes": 45, "priority": 2, "deep_work": False,
                "confidence": 0.8, "subtasks": [],
                "rationale": "small analysis task"})}}]}

        orig = brain._http_post_json
        brain._http_post_json = fake_post
        try:
            res = brain.enrich_task(self.store, self.cfg, tid, repo_root=".")
        finally:
            brain._http_post_json = orig
        self.assertEqual(res["status"], "applied")
        self.assertIn("/chat/completions", calls["url"])
        self.assertEqual(self.store.get_task(tid)["est_source"], "llm")
        self.assertEqual(self.store.get_task(tid)["est_minutes"], 45)

    def test_llm_backend_without_config_raises(self):
        self.cfg["brain"]["backend"] = "llm"
        tid = self.store.add_task("x")
        with self.assertRaises(brain.BrainError):
            brain.enrich_task(self.store, self.cfg, tid, repo_root=".")

    def test_llm_plan_sends_file_key_as_bearer(self):
        """With no env var, the key file is read and sent as the bearer token."""
        with open(self.cfg["brain"]["llm"]["key_file"], "w",
                  encoding="utf-8") as fh:
            fh.write("file-key\n")
        self.cfg["brain"]["backend"] = "llm"
        self.cfg["brain"]["llm"].update(base_url="https://example.invalid/v1",
                                        model="test-model")
        tid = self.store.add_task("Keyed task")
        captured = {}

        def fake_post(url, payload, headers, timeout=90):
            captured["headers"] = headers
            return {"choices": [{"message": {"content": json.dumps({
                "title": "Keyed task", "area": "analysis", "complexity": "S",
                "estimate_minutes": 45, "priority": 2, "deep_work": False,
                "confidence": 0.8, "subtasks": [], "rationale": "r"})}}]}

        orig = brain._http_post_json
        brain._http_post_json = fake_post
        try:
            with mock.patch.dict(os.environ, {}, clear=True):   # env unset
                brain.enrich_task(self.store, self.cfg, tid, repo_root=".")
        finally:
            brain._http_post_json = orig
        self.assertEqual(captured["headers"].get("Authorization"),
                         "Bearer file-key")

    # ---------- calibration ----------
    def test_calibration_uses_actual_minutes(self):
        tid = self.store.add_task("measured task", est_minutes=60)
        t0 = (datetime.now() - timedelta(minutes=90)).isoformat(timespec="seconds")
        self.store.conn.execute(
            "INSERT INTO events (ts, kind, task_id, payload) VALUES (?,?,?,?)",
            (t0, "task_doing", tid, "{}"))
        self.store.conn.commit()
        self.store.set_status(tid, "done")
        self.assertEqual(self.store.actual_minutes(tid) >= 85, True)
        tid2 = self.store.add_task("second measured", est_minutes=60)
        t1 = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        self.store.conn.execute(
            "INSERT INTO events (ts, kind, task_id, payload) VALUES (?,?,?,?)",
            (t1, "task_doing", tid2, "{}"))
        self.store.conn.commit()
        self.store.set_status(tid2, "done")
        ratio = brain.update_calibration(self.store)
        self.assertIsNotNone(ratio)
        self.assertGreater(ratio, 0.5)
        self.assertEqual(self.store.kv_get("calibration:ratio"), str(ratio))


class LlmKeySourceTest(unittest.TestCase):
    """brain.llm_key_source: env var first, key file second, keyless opt-out.
    Every path is pinned to a tmp key_file so the real planner/data/llm_key
    (once the user creates it) can never leak into these tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_file = os.path.join(self.tmp.name, "llm_key")
        self.cfg = {"brain": {"llm": {
            "base_url": "https://x/v1", "model": "m",
            "api_key_env": "MY_KEY", "key_file": self.key_file}}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_env_var_wins_over_file(self):
        with open(self.key_file, "w", encoding="utf-8") as fh:
            fh.write("file-key\n")
        with mock.patch.dict(os.environ, {"MY_KEY": "env-key"}, clear=True):
            self.assertEqual(brain.llm_key_source(self.cfg),
                             ("env-key", "env"))

    def test_file_used_when_env_unset(self):
        with open(self.key_file, "w", encoding="utf-8") as fh:
            fh.write("  file-key \n")        # surrounding whitespace is stripped
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(brain.llm_key_source(self.cfg),
                             ("file-key", "file"))

    def test_no_key_anywhere(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(brain.llm_key_source(self.cfg), ("", ""))

    def test_declared_keyless_endpoint(self):
        self.cfg["brain"]["llm"]["api_key_env"] = ""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(brain.llm_key_source(self.cfg), ("", "keyless"))

    def test_default_key_file_is_cwd_independent(self):
        """No key_file configured → planner/data/llm_key next to brain.py,
        regardless of the process CWD (Finder apps start at /). The spy
        refuses to open anything, so the real key file's contents are never
        read — hermetic whether or not the user's key file exists."""
        self.cfg["brain"]["llm"]["key_file"] = ""
        attempted = []

        def spy_open(path, *args, **kwargs):
            attempted.append(os.fspath(path))
            raise FileNotFoundError(path)   # OSError → no key; contents unread

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("builtins.open", side_effect=spy_open):
                cwd = tempfile.TemporaryDirectory()
                try:
                    old = os.getcwd()
                    os.chdir(cwd.name)               # Finder apps start at /
                    try:
                        self.assertEqual(brain.llm_key_source(self.cfg),
                                         ("", ""))
                    finally:
                        os.chdir(old)
                finally:
                    cwd.cleanup()
        self.assertEqual(attempted, [os.path.join(
            os.path.dirname(os.path.abspath(brain.__file__)),
            "data", "llm_key")])


class MeetingContextTest(unittest.TestCase):
    """The flagged next presentation meeting reaches every estimate prompt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "p.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_meeting_section_reaches_the_prompt(self):
        when = datetime.now().date() + timedelta(days=13)
        self.store.set_next_meeting({"date": when.isoformat(),
                                     "title": "PI meeting", "calendar": "Work"})
        self.store.add_task("Rerun the 27B coref arm")
        ctx = brain.project_context(self.store, repo_root=".")
        self.assertIn("## NEXT PRESENTATION MEETING", ctx)
        self.assertIn(when.isoformat(), ctx)
        self.assertIn("13 day(s) out", ctx)
        self.assertIn("PI meeting", ctx)

    def test_meeting_section_counts_already_scheduled_work(self):
        today = datetime.now().date()
        when = today + timedelta(days=21)
        self.store.set_next_meeting({"date": when.isoformat(), "title": "PI"})
        tid = self.store.add_task("Draft the results section")
        self.store.save_plan(today.isoformat(), [
            {"task_id": tid, "date": today.isoformat(), "start": "09:00",
             "end": "10:30", "minutes": 90, "rationale": "draft"}])
        ctx = brain.project_context(self.store, repo_root=".")
        self.assertIn("already scheduled before the meeting", ctx)
        self.assertIn("90 min", ctx)

    def test_stale_past_meeting_is_ignored(self):
        self.store.set_next_meeting({"date": "2020-01-01", "title": "long past"})
        ctx = brain.project_context(self.store, repo_root=".")
        self.assertNotIn("NEXT PRESENTATION MEETING", ctx)

    def test_no_meeting_no_section(self):
        ctx = brain.project_context(self.store, repo_root=".")
        self.assertNotIn("NEXT PRESENTATION MEETING", ctx)


if __name__ == "__main__":
    unittest.main()
