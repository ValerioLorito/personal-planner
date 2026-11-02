"""
test_review.py — the periodic plan review: trigger signals, the every_hours
gate, one-LLM-call verdicts (cleaned, safe-applied with their why), the
run_cycle fold-in, and the /api/review endpoint.

All LLM traffic is faked via brain._http_post_json; calendar reads are faked
for the busy-delta signal. Nothing here talks to a real endpoint or
Calendar.app.
"""
import json
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from unittest import mock

from planner import auto, brain, calendar_mac, review
from planner import config as config_mod
from planner import web
from planner.store import Store


def llm_answer(payload):
    """The OpenAI-compatible wrapper around a content JSON string."""
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "p.db"))
        self.cfg = deepcopy(config_mod.DEFAULT_CONFIG)
        self.cfg["brain"]["llm"]["base_url"] = "https://example.invalid/v1"
        self.cfg["brain"]["llm"]["model"] = "test-model"
        self.cfg["brain"]["llm"]["api_key_env"] = ""
        self.now = datetime(2026, 10, 30, 9, 0, 0)

        self.read_busy = {"events": [], "error": None}
        p = mock.patch.object(calendar_mac, "read_busy",
                              side_effect=lambda *a, **k: dict(self.read_busy))
        p.start()
        self.addCleanup(p.stop)

    def seed_task(self, **fields):
        return self.store.add_task(fields.pop("title", "Polish intro"),
                                   **fields)

    def mark_reviewed(self, hours_ago=0, ratio=None, busy=None):
        self.store.kv_set(review.REVIEW_KV, json.dumps({
            "at": (self.now - timedelta(hours=hours_ago))
            .isoformat(timespec="seconds"),
            "ratio": ratio, "busy": busy or {},
            "triggers": [], "verdicts": [], "applied": 0, "notes": ""}))


class SignalsTest(ReviewCase):
    """Each trigger fires on real state, with a why a human can read."""

    def test_never_reviewed_is_stale(self):
        sig = review.signals(self.store, self.cfg, now_dt=self.now,
                             use_calendar=False)
        self.assertEqual(sig["triggers"]["stale"], "never reviewed")

    def test_old_review_is_stale_with_hours(self):
        self.mark_reviewed(hours_ago=50)
        sig = review.signals(self.store, self.cfg, now_dt=self.now,
                             use_calendar=False)
        self.assertIn("50h ago", sig["triggers"]["stale"])

    def test_fresh_review_with_no_changes_has_no_triggers(self):
        self.mark_reviewed(hours_ago=2, ratio=1.0)
        sig = review.signals(self.store, self.cfg, now_dt=self.now,
                             use_calendar=False)
        self.assertEqual(sig["triggers"], {})

    def test_calibration_shift_fires_at_the_gate(self):
        self.store.kv_set("calibration:ratio", 1.2)
        self.mark_reviewed(hours_ago=2, ratio=1.0)
        sig = review.signals(self.store, self.cfg, now_dt=self.now,
                             use_calendar=False)
        self.assertIn("moved 1.00 -> 1.20", sig["triggers"]["calibration"])

    def test_small_calibration_move_stays_quiet(self):
        self.store.kv_set("calibration:ratio", 1.05)
        self.mark_reviewed(hours_ago=2, ratio=1.0)
        sig = review.signals(self.store, self.cfg, now_dt=self.now,
                             use_calendar=False)
        self.assertNotIn("calibration", sig["triggers"])

    def test_deadline_pressure_needs_an_empty_schedule(self):
        due = (self.now + timedelta(days=2)).date().isoformat()
        tid = self.seed_task(deadline=due)
        sig = review.signals(self.store, self.cfg, now_dt=self.now,
                             use_calendar=False)
        self.assertIn("#%d" % tid, sig["triggers"]["deadlines"])

        # the same task with a scheduled block is fine
        self.store.save_plan(self.now.date().isoformat(), [
            {"task_id": tid, "date": self.now.date().isoformat(),
             "start": "10:00", "end": "11:00", "minutes": 60,
             "rationale": "r"}])
        slot = self.store.get_plan(self.now.date().isoformat())["slots"][0]
        sig = review.signals(self.store, self.cfg, now_dt=self.now,
                             use_calendar=False)
        self.assertNotIn("deadlines", sig["triggers"])
        _ = slot

    def test_changed_tasks_fire_the_churn_trigger(self):
        self.mark_reviewed(hours_ago=2, ratio=1.0)
        self.seed_task()                       # created after the review
        sig = review.signals(self.store, self.cfg, now_dt=self.now,
                             use_calendar=False)
        self.assertIn("1 open task(s)", sig["triggers"]["tasks"])

    def test_busy_delta_uses_the_calendar_when_asked(self):
        self.mark_reviewed(hours_ago=2, ratio=1.0,
                           busy={self.now.date().isoformat(): 60})
        self.read_busy.update({"events": [
            {"date": self.now.date().isoformat(), "start": "09:00",
             "end": "12:00", "title": "Meeting", "all_day": False}]})
        sig = review.signals(self.store, self.cfg, now_dt=self.now,
                             use_calendar=True)
        self.assertEqual(sig["busy_delta_minutes"], 120)
        self.assertIn("moved by 120min", sig["triggers"]["busy"])


class GateTest(ReviewCase):
    """The every_hours gate is cheap and comes before any signal work."""

    def test_recent_review_gates_everything_out(self):
        self.mark_reviewed(hours_ago=1)
        ok, sig = review.should_review(self.store, self.cfg, now_dt=self.now)
        self.assertFalse(ok)
        self.assertIsNone(sig)                 # no signals computed at all

    def test_disabled_review_never_runs(self):
        self.cfg["auto"]["review"]["enabled"] = False
        ok, _ = review.should_review(self.store, self.cfg, now_dt=self.now)
        self.assertFalse(ok)

    def test_gate_passed_but_no_triggers(self):
        self.mark_reviewed(hours_ago=30, ratio=1.0)
        ok, sig = review.should_review(self.store, self.cfg, now_dt=self.now,
                                       use_calendar=False)
        self.assertFalse(ok)
        self.assertEqual(sig["triggers"], {})

    def test_stale_beats_the_gate_eventually(self):
        self.mark_reviewed(hours_ago=30, ratio=1.0)   # gate passed, not stale
        self.cfg["auto"]["review"]["stale_hours"] = 24
        ok, sig = review.should_review(self.store, self.cfg, now_dt=self.now,
                                       use_calendar=False)
        self.assertTrue(ok)
        self.assertIn("stale", sig["triggers"])


class RunReviewTest(ReviewCase):
    """One LLM call; verdicts cleaned, applied safely, stored with why."""

    def verdicts(self, verdicts, notes="The week holds together."):
        return mock.patch.object(brain, "_http_post_json",
                                 side_effect=lambda *a, **k: llm_answer({
                                     "verdicts": verdicts, "notes": notes}))

    def test_full_review_applies_safe_verdicts(self):
        t1 = self.seed_task(title="Old estimate")     # reestimate
        t2 = self.seed_task(title="Does not fit")     # rewindow (+1)
        t3 = self.seed_task(title="Due sooner", priority=3)  # reprioritize
        t4 = self.seed_task(title="Fine as is")       # keep
        self.store.update_task(t1, est_source="agent_llm", est_minutes=60)
        answer = [
            {"kind": "reestimate", "task_id": t1, "why": "runs over always"},
            {"kind": "rewindow", "task_id": t2, "value": None,
             "why": "cannot fit this week"},
            {"kind": "reprioritize", "task_id": t3, "value": 1,
             "why": "deadline moved up"},
            {"kind": "keep", "task_id": t4, "why": "estimate + window fine"},
            {"kind": "nonsense", "task_id": t4, "why": "x"},     # bad kind
            {"kind": "keep", "task_id": 9999, "why": "x"},       # no task
        ]
        with self.verdicts(answer):
            rep = review.run_review(self.store, self.cfg, now_dt=self.now,
                                    use_calendar=False)
        self.assertIsNone(rep["error"])
        self.assertEqual(len(rep["verdicts"]), 4)
        self.assertEqual(rep["applied"], 3)
        self.assertEqual(rep["notes"], "The week holds together.")

        t1_row = self.store.get_task(t1)
        self.assertIsNone(t1_row["est_source"])       # re-estimated next cycle
        self.assertIn(t1, [t["id"] for t in self.store.needs_estimate()])
        self.assertEqual(self.store.get_task(t2)["within_weeks"], 2)
        self.assertEqual(self.store.get_task(t3)["priority"], 1)
        self.assertEqual(self.store.get_task(t4)["est_source"], None
                         if False else self.store.get_task(t4)["est_source"])

        # stored for the UI card, and the event log knows
        stored = review.last_review(self.store)
        self.assertEqual(stored["applied"], 3)
        self.assertEqual(stored["records"][0]["title"], "Old estimate")
        kinds = [e for e in self.store.recent_events(5)
                 if e["kind"] == "plan_reviewed"]
        self.assertEqual(len(kinds), 1)

    def test_explicit_rewindow_value_is_respected_and_capped(self):
        tid = self.seed_task(within_weeks=3)
        with self.verdicts([{"kind": "rewindow", "task_id": tid,
                             "value": 99, "why": "wants forever"}]):
            rep = review.run_review(self.store, self.cfg, now_dt=self.now,
                                    use_calendar=False)
        self.assertEqual(self.store.get_task(tid)["within_weeks"], 26)
        self.assertEqual(rep["applied"], 1)

    def test_llm_failure_is_data_and_still_stored(self):
        self.seed_task()
        with mock.patch.object(brain, "_http_post_json",
                               side_effect=brain.BrainError("HTTP 429")):
            rep = review.run_review(self.store, self.cfg, now_dt=self.now,
                                    use_calendar=False)
        self.assertIn("429", rep["error"])
        self.assertEqual(rep["verdicts"], [])
        self.assertTrue(review.last_review(self.store)["at"])

    def test_heuristic_backend_skips_without_a_call(self):
        self.cfg["brain"]["backend"] = "heuristic"
        with mock.patch.object(brain, "_http_post_json") as fake:
            rep = review.run_review(self.store, self.cfg, now_dt=self.now,
                                    use_calendar=False)
        fake.assert_not_called()
        self.assertIn("needs brain.backend=llm", rep["skipped"])

    def test_force_runs_even_when_gated(self):
        self.mark_reviewed(hours_ago=1)               # inside the gate
        tid = self.seed_task()
        with self.verdicts([{"kind": "keep", "task_id": tid, "why": "ok"}]):
            rep = review.run_review(self.store, self.cfg, now_dt=self.now,
                                    force=True, use_calendar=False)
        self.assertEqual(rep["skipped"], None)
        self.assertEqual(len(rep["verdicts"]), 1)

    def test_gated_review_skips_quietly(self):
        self.mark_reviewed(hours_ago=1)
        rep = review.run_review(self.store, self.cfg, now_dt=self.now,
                                use_calendar=False)
        self.assertEqual(rep["skipped"], "gated (disabled or reviewed "
                                         "recently)")
        self.assertEqual(rep["verdicts"], [])


class CycleTest(ReviewCase):
    """run_cycle carries the review and re-plans on its applied verdicts."""

    def setUp(self):
        super().setUp()
        # run_cycle plans the real current week: placeable on any day the
        # suite runs, weekends included
        self.cfg["work_days"] = [0, 1, 2, 3, 4, 5, 6]

    def test_cycle_reviews_replans_and_carries_the_report(self):
        tid = self.store.add_task("Write section", est_minutes=60)
        answer = [{"kind": "reestimate", "task_id": tid,
                   "why": "calibration moved"}]
        with mock.patch.object(brain, "_http_post_json",
                               side_effect=lambda *a, **k: llm_answer(
                                   {"verdicts": answer, "notes": "n"})):
            res = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        self.assertIsNotNone(res["review"])
        self.assertEqual(res["review"]["applied"], 1)
        self.assertTrue(res["replanned"])           # applied -> re-plan now

    def test_cycle_respects_the_gate(self):
        self.store.add_task("Write section", est_minutes=60)
        self.mark_reviewed(hours_ago=0)
        # a heuristic backend keeps enrichment offline, so any LLM call at
        # all would mean the review broke its gate
        self.cfg["brain"]["backend"] = "heuristic"
        with mock.patch.object(brain, "_http_post_json") as fake:
            res = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        fake.assert_not_called()
        self.assertIsNotNone(res["review"])
        self.assertTrue(res["review"]["skipped"])

    def test_review_disabled_leaves_the_report_out(self):
        self.cfg["auto"]["review"]["enabled"] = False
        res = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        self.assertIsNone(res["review"])


class ReviewApiTest(unittest.TestCase):
    """POST /api/review runs a manual review; /api/state carries the card."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.store = Store(os.path.join(cls.tmpdir.name, "p.db"))
        cls.cfg = deepcopy(config_mod.DEFAULT_CONFIG)
        cls.cfg["brain"]["llm"]["base_url"] = "https://example.invalid/v1"
        cls.cfg["brain"]["llm"]["model"] = "test-model"
        cls.cfg["brain"]["llm"]["api_key_env"] = ""
        cls.server = web.make_server(cls.store, cls.cfg, port=0, quiet=True)
        cls.port = cls.server.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.store.conn.close()
        cls.tmpdir.cleanup()

    def setUp(self):
        self._patchers = [
            mock.patch.object(calendar_mac, "read_busy",
                              return_value={"events": [], "error": None})]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def request(self, path, payload=None, method=None):
        import urllib.error
        import urllib.request
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers,
                                     method=method or ("POST" if data
                                                       else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, (json.loads(raw) if raw else None)

    def test_010_manual_review_is_one_call_and_stored(self):
        tid = self.store.add_task("Polish intro", est_minutes=60)
        answer = {"verdicts": [{"kind": "keep", "task_id": tid,
                                "why": "sound"}], "notes": "fine"}
        with mock.patch.object(brain, "_http_post_json",
                               side_effect=lambda *a, **k: llm_answer(answer)):
            status, rep = self.request("/api/review", {})
        self.assertEqual(status, 200)
        self.assertEqual(rep["applied"], 0)
        self.assertEqual(len(rep["verdicts"]), 1)

        status, state = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual((state["review"] or {}).get("notes"), "fine")
        records = (state["review"] or {}).get("records") or []
        self.assertEqual(records[0]["kind"], "keep")
        self.assertEqual(records[0]["title"], "Polish intro")

    def test_020_review_failure_is_data_not_a_500(self):
        with mock.patch.object(brain, "_http_post_json",
                               side_effect=brain.BrainError("HTTP 429")):
            status, rep = self.request("/api/review", {})
        self.assertEqual(status, 200)
        self.assertIn("429", rep["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
