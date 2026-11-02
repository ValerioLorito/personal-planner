"""
test_web.py — end-to-end tests for planner.web.

Each test class starts the real HTTP server on port 0 (ephemeral) against a
temporary sqlite file, then talks to it with urllib. No mocks: the assertions
are made on the JSON the server actually returns.
"""
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import date, datetime, timedelta

from planner import brain, calendar_mac
from planner import config as config_mod
from planner import scheduler, web
from planner.store import Store


class ServerCase(unittest.TestCase):
    """Shared fixture: temp DB + live server on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmpdir.name, "planner-test.db")
        cls.store = Store(cls.db_path)
        cls.cfg = deepcopy(config_mod.DEFAULT_CONFIG)
        cls.server = web.make_server(cls.store, cls.cfg, port=0, quiet=True)
        cls.port = cls.server.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.store.conn.close()
        cls.tmpdir.cleanup()

    # ---- http helpers ----
    def request(self, path, payload=None, method=None):
        url = self.base + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, (json.loads(raw) if raw else None)

    def get(self, path):
        return self.request(path, method="GET")

    def post(self, path, payload):
        return self.request(path, payload, method="POST")

    def raw_get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")


class WebApiTest(ServerCase):
    """Happy-path plus validation, in a deliberate order (test_0x0 prefixes)."""

    WEEK = "2026-11-02"          # a Monday; asserted below
    OTHER_WEEK = "2026-11-16"    # used by the busy-map test only

    def test_010_page_is_self_contained(self):
        status, html = self.raw_get("/")
        self.assertEqual(status, 200)
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn("Thesis", html)
        # local assets only: the shell references /ui/*, never a CDN
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        for asset in ("/ui/app.css", "/ui/app.js"):
            self.assertIn(asset, html)
            code, body = self.raw_get(asset)
            self.assertEqual(code, 200)
            self.assertTrue(body.strip())
            self.assertNotIn("http://", body)
            self.assertNotIn("https://", body)

    def test_011_state_endpoint_shape(self):
        status, body = self.raw_get("/api/state")
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ("today", "now", "week", "week_days", "tasks", "slots",
                    "calendar", "stats", "calibration", "agent", "settings",
                    "meeting"):
            self.assertIn(key, data)
        self.assertIsNone(data["meeting"])      # nothing flagged yet
        self.assertIn("horizon_weeks", data["settings"])
        self.assertIn("llm_ready", data["settings"])         # llm not configured
        self.assertEqual(len(data["week_days"]), 7)
        self.assertIn("error", data["calendar"])
        self.assertIsInstance(data["calibration"], dict)

    @property
    def WEEK_FOR_NOW(self):
        return scheduler.week_start_for().isoformat()

    def test_020_add_task_persists(self):
        self.assertEqual(date.fromisoformat(self.WEEK).weekday(), 0)
        status, data = self.post("/api/tasks", {
            "title": "Draft methods chapter",
            "notes": "section 3",
            "area": "writing",
            "complexity": "L",
            "est_minutes": 180,
            "priority": 1,
            "deadline": "2026-11-03",
        })
        self.assertEqual(status, 201, data)
        self.assertIsInstance(data["id"], int)
        task = data["task"]
        self.assertEqual(task["title"], "Draft methods chapter")
        self.assertEqual(task["complexity"], "L")
        self.assertEqual(task["est_minutes"], 180)
        self.assertEqual(task["priority"], 1)
        self.assertEqual(task["status"], "todo")
        self.assertEqual(task["deadline"], "2026-11-03")
        self.assertEqual(self.store.get_task(data["id"])["title"], "Draft methods chapter")
        type(self).task_a = data["id"]

    def test_030_list_tasks_and_filter(self):
        status, data = self.get("/api/tasks")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["count"], 1)
        self.assertEqual(data["count"], len(data["tasks"]))
        ids = [t["id"] for t in data["tasks"]]
        self.assertIn(self.task_a, ids)

        status, done = self.get("/api/tasks?status=done")
        self.assertEqual(status, 200)
        self.assertTrue(all(t["status"] == "done" for t in done["tasks"]))

        status, bad = self.get("/api/tasks?status=nonsense")
        self.assertEqual(status, 400)
        self.assertIn("error", bad)

    def test_040_partial_update_keeps_other_fields(self):
        status, data = self.post("/api/tasks/%d" % self.task_a,
                                 {"priority": 2, "notes": "tightened scope"})
        self.assertEqual(status, 200, data)
        task = data["task"]
        self.assertEqual(task["priority"], 2)
        self.assertEqual(task["notes"], "tightened scope")
        self.assertEqual(task["title"], "Draft methods chapter")   # untouched
        self.assertEqual(task["complexity"], "L")

    def test_050_status_endpoint(self):
        status, data = self.post("/api/tasks", {
            "title": "Run baseline eval", "complexity": "M",
            "est_minutes": 90, "priority": 2,
        })
        self.assertEqual(status, 201, data)
        task_b = data["id"]

        status, data = self.post("/api/tasks/%d/status" % task_b,
                                 {"status": "doing", "note": "started"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["task"]["status"], "doing")

        status, data = self.get("/api/tasks?status=doing")
        self.assertEqual([t["id"] for t in data["tasks"]], [task_b])

        status, data = self.post("/api/tasks/%d/status" % task_b,
                                 {"status": "done", "note": "finished"})
        self.assertEqual(data["task"]["status"], "done")

        status, events = self.get("/api/log?limit=10")
        kinds = [e["kind"] for e in events["events"]]
        self.assertIn("task_done", kinds)
        done_event = [e for e in events["events"] if e["kind"] == "task_done"][0]
        self.assertEqual(done_event["task_id"], task_b)
        self.assertEqual(done_event["payload"]["note"], "finished")

        status, bad = self.post("/api/tasks/%d/status" % task_b, {"status": "nope"})
        self.assertEqual(status, 400)

    def test_060_stats(self):
        status, data = self.get("/api/stats")
        self.assertEqual(status, 200, data)
        for key in ("tasks_by_status", "events", "plans", "history", "tasks",
                    "open_tasks", "open_minutes", "overdue", "week"):
            self.assertIn(key, data)
        self.assertGreaterEqual(data["tasks"], 2)
        self.assertGreaterEqual(data["tasks_by_status"].get("todo", 0), 1)
        self.assertGreaterEqual(data["open_tasks"], 1)
        self.assertGreater(data["open_minutes"], 0)
        self.assertEqual(data["week"], self.WEEK_FOR_NOW)
        self.assertIsInstance(data["overdue"], list)

    def test_070_log_orders_newest_first_and_respects_limit(self):
        status, data = self.get("/api/log?limit=3")
        self.assertEqual(status, 200, data)
        self.assertLessEqual(data["count"], 3)
        self.assertEqual(data["count"], len(data["events"]))
        stamps = [e["ts"] for e in data["events"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertIn("kind", data["events"][0])

        status, one = self.get("/api/log?limit=1")
        self.assertEqual(one["count"], 1)

        status, bad = self.get("/api/log?limit=abc")
        self.assertEqual(status, 400)

    def test_080_history_range(self):
        today = date.today()
        self.store.add_history((today - timedelta(days=1)).isoformat(), "commit",
                               "feat: scheduler", "abc1234")
        self.store.add_history(today.isoformat(), "run", "baseline eval", "seed 0")

        status, data = self.get("/api/history?from=%s&to=%s" % (
            (today - timedelta(days=1)).isoformat(), today.isoformat()))
        self.assertEqual(status, 200, data)
        self.assertEqual(data["count"], 2)
        self.assertEqual([r["title"] for r in data["history"]],
                         ["feat: scheduler", "baseline eval"])

        status, narrow = self.get("/api/history?from=%s&to=%s" % (
            today.isoformat(), today.isoformat()))
        self.assertEqual(narrow["count"], 1)

        status, bad = self.get("/api/history?from=yesterday")
        self.assertEqual(status, 400)

    def test_085_busy_map_uses_saved_slots(self):
        week = date.fromisoformat(self.OTHER_WEEK)
        self.store.save_plan(week.isoformat(), [{
            "task_id": self.task_a, "date": self.OTHER_WEEK,
            "start": "09:00", "end": "10:30", "minutes": 90,
            "rationale": "seed",
        }])
        busy = web.busy_by_date(self.store, week, include_calendar=False)
        self.assertEqual(busy[self.OTHER_WEEK], [(9 * 60, 10 * 60 + 30)])

    def test_090_schedule_week_saves_plan(self):
        status, data = self.post("/api/schedule", {"week": self.WEEK, "save": True})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["week"], self.WEEK)
        self.assertTrue(data["slots"], data)
        self.assertIsInstance(data["saved_plan_id"], int)
        self.assertTrue(data["saved"])

        monday = date.fromisoformat(self.WEEK)
        for slot in data["slots"]:
            slot_day = date.fromisoformat(slot["date"])
            self.assertGreaterEqual(slot_day, monday)
            self.assertLessEqual(slot_day, monday + timedelta(days=6))
            self.assertTrue(slot_day.weekday() in self.cfg["work_days"])
            self.assertRegex(slot["start"], r"^\d{2}:\d{2}$")
            self.assertRegex(slot["end"], r"^\d{2}:\d{2}$")
            self.assertGreater(slot["minutes"], 0)
            self.assertEqual(
                scheduler.parse_hhmm(slot["end"]) - scheduler.parse_hhmm(slot["start"]),
                slot["minutes"])
            self.assertTrue(slot["rationale"])
            self.assertIn("task_id", slot)

        placed = {s["task_id"] for s in data["slots"]}
        self.assertIn(self.task_a, placed)
        self.assertGreaterEqual(data["stats"]["planned_minutes"], 180)
        self.assertEqual(data["stats"]["planned_minutes"],
                         sum(s["minutes"] for s in data["slots"]))

        saved = self.store.get_plan(self.WEEK)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["id"], data["saved_plan_id"])
        self.assertEqual(len(saved["slots"]), len(data["slots"]))

        # every saved slot is now busy for that week (DB only — the real
        # Apple Calendar must not leak into this comparison; on machines with
        # a TCC grant it can legitimately hold events in this week)
        busy = web.busy_by_date(self.store, monday, include_calendar=False)
        self.assertTrue(busy)
        self.assertEqual(sorted(busy), sorted({s["date"] for s in data["slots"]}))

    def test_100_plan_endpoint_reads_saved_plan(self):
        status, data = self.get("/api/plan?week=" + self.WEEK)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["exists"])
        self.assertEqual(data["week"], self.WEEK)
        self.assertTrue(data["slots"])
        self.assertIsInstance(data["plan_id"], int)

        status, empty = self.get("/api/plan?week=2027-03-01")
        self.assertEqual(status, 200)
        self.assertFalse(empty["exists"])
        self.assertEqual(empty["slots"], [])
        self.assertIsNone(empty["plan_id"])

    def test_105_schedule_without_save_does_not_persist(self):
        week = "2026-12-07"     # a Monday, untouched by other tests
        status, data = self.post("/api/schedule", {"week": week, "save": False})
        self.assertEqual(status, 200, data)
        self.assertFalse(data["saved"])
        self.assertIsNone(data["saved_plan_id"])
        self.assertIsNone(self.store.get_plan(week))

    def test_110_validation_errors(self):
        status, data = self.post("/api/tasks", {"notes": "no title"})
        self.assertEqual(status, 400)
        self.assertIn("title", data["error"])

        status, data = self.post("/api/tasks", {"title": "x", "complexity": "XL"})
        self.assertEqual(status, 400)
        self.assertIn("complexity", data["error"])

        status, data = self.post("/api/tasks", {"title": "x", "est_minutes": 0})
        self.assertEqual(status, 400)

        status, data = self.post("/api/tasks", {"title": "x", "deadline": "03.11.2026"})
        self.assertEqual(status, 400)

        status, data = self.post("/api/tasks/%d" % self.task_a, {"priority": 9})
        self.assertEqual(status, 400)

        status, data = self.get("/api/tasks/999999")
        self.assertEqual(status, 404)
        self.assertIn("error", data)

        status, data = self.post("/api/tasks/999999", {"priority": 1})
        self.assertEqual(status, 404)

        status, data = self.post("/api/schedule", {"week": "not-a-date"})
        self.assertEqual(status, 400)

    def test_120_unknown_route_and_method(self):
        status, data = self.get("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", data)

        status, data = self.get("/api/schedule")     # POST only
        self.assertEqual(status, 405)

        status, data = self.post("/api/stats", {})   # GET only
        self.assertEqual(status, 405)


class WebUnscheduledTest(ServerCase):
    """A task bigger than the whole week must come back as a warning."""

    WEEK = "2026-11-09"

    def test_unscheduled_task_is_reported(self):
        status, data = self.post("/api/tasks", {
            "title": "Rewrite the whole thesis", "complexity": "L",
            "est_minutes": 4000, "priority": 1,
        })
        self.assertEqual(status, 201, data)
        big_id = data["id"]

        status, result = self.post("/api/schedule", {"week": self.WEEK, "save": False})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["unscheduled"], result)
        reasons = {u["task_id"]: u for u in result["unscheduled"]}
        self.assertIn(big_id, reasons)
        self.assertIn("no free interval", reasons[big_id]["reason"])
        self.assertEqual(reasons[big_id]["minutes"], self.cfg["max_block_min"])

        # the week's whole capacity is used, and the rest is reported, not dropped
        capacity = len(self.cfg["work_days"]) * self.cfg["max_day_min"]
        self.assertLessEqual(result["stats"]["planned_minutes"], capacity)
        self.assertGreater(result["stats"]["planned_minutes"], 0)

        # save=False -> nothing written
        self.assertIsNone(self.store.get_plan(self.WEEK))


class SlotEndpointTest(ServerCase):
    """POST/DELETE /api/slots/<id> — the manual correction of a placed block."""

    WEEK = "2026-11-02"      # a Monday, untouched by the other test classes

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.task_id = cls.store.add_task("Correct the abstract", complexity="M")
        cls.store.save_plan(cls.WEEK, [
            {"task_id": cls.task_id, "date": "2026-11-02", "start": "09:00",
             "end": "10:00", "minutes": 60, "rationale": "auto pick"},
            {"task_id": cls.task_id, "date": "2026-11-03", "start": "14:00",
             "end": "14:30", "minutes": 30, "rationale": "auto pick"},
        ])
        cls.slot_a = cls.store.get_plan(cls.WEEK)["slots"][0]["id"]
        cls.slot_b = cls.store.get_plan(cls.WEEK)["slots"][1]["id"]
        # these blocks carry no calendar uid, so a move now self-heals by
        # writing the missing event — never the real osascript in tests
        cls.orig_write = calendar_mac.write_event
        cls.orig_delete = calendar_mac.delete_event

        def fake_write(summary, start_iso, end_iso, calendar_name,
                       description="", runner=None):
            return "UID-HEALED"

        def fake_delete(uid, calendar_name=None, runner=None):
            return {"found": 1, "deleted": 1, "error": None}

        calendar_mac.write_event = fake_write
        calendar_mac.delete_event = fake_delete

    @classmethod
    def tearDownClass(cls):
        calendar_mac.write_event = cls.orig_write
        calendar_mac.delete_event = cls.orig_delete
        super().tearDownClass()


    def test_010_state_slots_expose_id_and_pinned(self):
        status, data = self.get("/api/state?week=" + self.WEEK)
        self.assertEqual(status, 200, data)
        by_id = {s["id"]: s for s in data["slots"]}
        self.assertIn(self.slot_a, by_id)
        self.assertIn(self.slot_b, by_id)
        self.assertEqual(by_id[self.slot_a]["pinned"], 0)
        self.assertEqual(set(by_id), {self.slot_a, self.slot_b})

    def test_020_move_slot_updates_and_pins(self):
        status, data = self.post("/api/slots/%d" % self.slot_a,
                                 {"date": "2026-11-05", "start": "13:00",
                                  "end": "15:00"})
        self.assertEqual(status, 200, data)
        slot = data["slot"]
        self.assertEqual(slot["id"], self.slot_a)         # id stays stable
        self.assertEqual((slot["date"], slot["start"], slot["end"]),
                         ("2026-11-05", "13:00", "15:00"))
        self.assertEqual(slot["minutes"], 120)
        self.assertEqual(slot["pinned"], 1)
        saved = self.store.get_slot(self.slot_a)
        self.assertEqual((saved["date"], saved["start"], saved["end"]),
                         ("2026-11-05", "13:00", "15:00"))
        # the state endpoint reflects the pin immediately
        status, state = self.get("/api/state?week=" + self.WEEK)
        by_id = {s["id"]: s for s in state["slots"]}
        self.assertEqual(by_id[self.slot_a]["pinned"], 1)
        self.assertEqual(by_id[self.slot_a]["date"], "2026-11-05")

    def test_030_move_slot_time_only_keeps_date(self):
        status, data = self.post("/api/slots/%d" % self.slot_b,
                                 {"start": "15:30"})
        self.assertEqual(status, 200, data)
        slot = data["slot"]
        self.assertEqual(slot["date"], "2026-11-03")
        self.assertEqual((slot["start"], slot["end"]), ("15:30", "16:00"))
        self.assertEqual(slot["minutes"], 30)

    def test_040_move_slot_validation(self):
        # end <= start when both edges are given
        status, data = self.post("/api/slots/%d" % self.slot_b,
                                 {"start": "17:00", "end": "09:00"})
        self.assertEqual(status, 400, data)
        self.assertIn("error", data)
        # malformed HH:MM
        status, data = self.post("/api/slots/%d" % self.slot_b,
                                 {"start": "9am"})
        self.assertEqual(status, 400, data)
        self.assertIn("error", data)
        # no fields at all -> nothing to change
        status, data = self.post("/api/slots/%d" % self.slot_b, {})
        self.assertEqual(status, 400, data)
        # unknown slot -> 404
        status, data = self.post("/api/slots/999999",
                                 {"date": "2026-11-05", "start": "09:00",
                                  "end": "10:00"})
        self.assertEqual(status, 404, data)
        # none of the failed requests changed the block
        self.assertEqual((self.store.get_slot(self.slot_b)["start"],
                          self.store.get_slot(self.slot_b)["end"]),
                         ("15:30", "16:00"))

    def test_050_delete_slot_then_404_again(self):
        status, data = self.request("/api/slots/%d" % self.slot_b,
                                    method="DELETE")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["removed"])
        self.assertIsNone(self.store.get_slot(self.slot_b))
        self.assertIsNotNone(self.store.get_task(self.task_id))
        status, data = self.request("/api/slots/%d" % self.slot_b,
                                    method="DELETE")
        self.assertEqual(status, 404, data)

    def test_060_replanned_week_keeps_pinned_slot(self):
        # a fresh schedule for the same week must carry the pinned slot over
        status, data = self.post("/api/schedule", {"week": self.WEEK,
                                                   "save": True})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["saved"], data)
        saved = self.store.get_plan(self.WEEK)
        ids = [s["id"] for s in saved["slots"]]
        self.assertIn(self.slot_a, ids, data)       # carried over, same id
        carried = next(s for s in saved["slots"] if s["id"] == self.slot_a)
        self.assertEqual((carried["date"], carried["start"], carried["end"]),
                         ("2026-11-05", "13:00", "15:00"))
        self.assertEqual(carried["pinned"], 1)
        # the task itself was re-planned around the pinned block
        self.assertGreater(len(ids), 1, data)
        # and the state endpoint shows the pinned block in the new plan
        status, state = self.get("/api/state?week=" + self.WEEK)
        by_id = {s["id"]: s for s in state["slots"]}
        self.assertEqual(by_id[self.slot_a]["pinned"], 1)
        self.assertEqual(by_id[self.slot_a]["date"], "2026-11-05")

    def test_070_delete_unknown_slot_404(self):
        status, data = self.request("/api/slots/424242", method="DELETE")
        self.assertEqual(status, 404, data)


class TodayEmptyTest(ServerCase):
    """GET /api/today on a database with no tasks and no saved plan."""

    KEYS = {"date", "planned_minutes", "blocks", "slots", "next",
            "open_tasks", "free_minutes_left"}

    def test_010_empty_db_is_zeros_and_no_next(self):
        status, data = self.get("/api/today")
        self.assertEqual(status, 200, data)
        self.assertEqual(set(data), self.KEYS)
        self.assertEqual(data["date"], date.today().isoformat())
        self.assertEqual(data["planned_minutes"], 0)
        self.assertEqual(data["blocks"], 0)
        self.assertEqual(data["slots"], [])
        self.assertIsNone(data["next"])
        self.assertEqual(data["open_tasks"], 0)

        window = (scheduler.parse_hhmm(self.cfg["day_end"]) -
                  scheduler.parse_hhmm(self.cfg["day_start"]))
        self.assertIsInstance(data["free_minutes_left"], int)
        self.assertGreaterEqual(data["free_minutes_left"], 0)
        self.assertLessEqual(data["free_minutes_left"], window)

        # the counters must agree with what the full page shows
        status, stats = self.get("/api/stats")
        self.assertEqual(status, 200, stats)
        self.assertEqual(data["open_tasks"], stats["open_tasks"])

    def test_020_widget_page_is_compact_and_self_contained(self):
        with urllib.request.urlopen(self.base + "/widget", timeout=10) as resp:
            html = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
        self.assertTrue(ctype.startswith("text/html"), ctype)
        self.assertIn("next", html)
        self.assertIn("/api/today", html)
        self.assertIn("prefers-color-scheme", html)
        self.assertIn("max-width: 420px", html)
        self.assertIn("setInterval", html)
        self.assertIn("60000", html)          # 60 s auto-refresh
        for marker in ("http://", "https://", "<link", "src=", "@import"):
            self.assertNotIn(marker, html)


class TodayPlanTest(ServerCase):
    """One saved plan with a block that ended and one still ahead.

    api_today(now=...) is exercised directly so the clock is deterministic;
    test_070 then checks the same plan through the HTTP route.
    """

    def setUp(self):
        self.today = date.today().isoformat()
        self.week = scheduler.week_start_for().isoformat()
        self.past_id = self.store.add_task("Read related work", est_minutes=60)
        self.future_id = self.store.add_task("Draft results section", est_minutes=60)
        self.store.save_plan(self.week, [
            {"task_id": self.past_id, "date": self.today, "start": "09:00",
             "end": "10:00", "minutes": 60,
             "rationale": "covers: read the survey; priority P1"},
            {"task_id": self.future_id, "date": self.today, "start": "15:00",
             "end": "16:00", "minutes": 60, "rationale": "seed"},
        ])
        self.expected_open = sum(1 for t in self.store.list_tasks()
                                 if t["status"] in ("todo", "doing"))

    def at(self, hour, minute=0):
        return datetime.combine(date.today(), datetime.min.time()).replace(
            hour=hour, minute=minute)

    def all_days_cfg(self, **over):
        cfg = deepcopy(self.cfg)
        cfg["work_days"] = list(range(7))
        cfg.update(over)
        return cfg

    def test_010_today_only_with_correct_counts(self):
        data = web.api_today(self.store, self.all_days_cfg(), now=self.at(12))
        self.assertEqual(data["date"], self.today)
        self.assertEqual(data["blocks"], 2)
        self.assertEqual(data["planned_minutes"], 120)
        self.assertEqual([s["start"] for s in data["slots"]], ["09:00", "15:00"])
        self.assertEqual([s["end"] for s in data["slots"]], ["10:00", "16:00"])
        self.assertEqual(data["slots"][0]["title"], "Read related work")
        self.assertEqual(data["slots"][0]["status"], "todo")
        self.assertEqual(data["slots"][0]["subtask"], "read the survey")
        self.assertEqual(data["slots"][1]["subtask"], "")
        self.assertEqual(data["open_tasks"], self.expected_open)
        # 12:00 -> 19:00 leaves 420 min of window, minus 120 planned
        self.assertEqual(data["free_minutes_left"], 420 - 120)

    def test_020_next_is_the_future_block(self):
        data = web.api_today(self.store, self.all_days_cfg(), now=self.at(12))
        self.assertEqual({k: data["next"][k] for k in
                          ("start", "end", "title", "minutes")},
                         {"start": "15:00", "end": "16:00",
                          "title": "Draft results section", "minutes": 60})
        self.assertTrue(data["next"]["is_today"])
        self.assertEqual(data["next"]["date"], data["date"])

    def test_030_next_is_null_once_the_last_block_ended(self):
        data = web.api_today(self.store, self.all_days_cfg(), now=self.at(16, 1))
        self.assertEqual(data["blocks"], 2)
        self.assertIsNone(data["next"])

    def test_040_running_block_still_counts_as_next(self):
        data = web.api_today(self.store, self.all_days_cfg(), now=self.at(9, 30))
        self.assertEqual(data["next"]["start"], "09:00")
        self.assertEqual(data["next"]["end"], "10:00")

    def test_050_free_minutes_floor_at_zero(self):
        cfg = self.all_days_cfg(day_end="13:00")
        data = web.api_today(self.store, cfg, now=self.at(12))
        self.assertEqual(data["free_minutes_left"], 0)   # 60 min - 120 planned

    def test_060_rest_day_has_no_free_window(self):
        cfg = self.all_days_cfg()
        cfg["work_days"] = [d for d in range(7) if d != date.today().weekday()]
        data = web.api_today(self.store, cfg, now=self.at(12))
        self.assertEqual(data["free_minutes_left"], 0)

    def test_070_http_route_reads_the_saved_plan(self):
        status, data = self.get("/api/today")
        self.assertEqual(status, 200, data)
        self.assertEqual(set(data), TodayEmptyTest.KEYS)
        self.assertEqual(data["date"], self.today)
        self.assertEqual(data["blocks"], 2)
        self.assertEqual(data["planned_minutes"], 120)
        self.assertEqual(len(data["slots"]), 2)
        self.assertEqual(data["open_tasks"], self.expected_open)

    def test_080_other_days_are_not_today(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.store.save_plan(self.week, [
            {"task_id": self.future_id, "date": tomorrow, "start": "09:00",
             "end": "09:30", "minutes": 30, "rationale": "not today"},
        ])
        data = web.api_today(self.store, self.all_days_cfg(), now=self.at(12))
        self.assertEqual(data["blocks"], 0)
        self.assertEqual(data["planned_minutes"], 0)
        self.assertEqual(data["slots"], [])
        # today is empty, but a small always-on view is more useful pointing at
        # the next block this week than showing nothing at all
        self.assertIsNotNone(data["next"])
        self.assertFalse(data["next"]["is_today"])
        self.assertEqual(data["next"]["date"], tomorrow)
        self.assertEqual(data["next"]["start"], "09:00")


class EmptyPlanTest(unittest.TestCase):
    def test_scheduling_with_no_tasks_saves_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(os.path.join(tmp.name, "p.db"))
        cfg = deepcopy(config_mod.DEFAULT_CONFIG)
        week = web.api_today(store, cfg)["date"]
        result = web.api_schedule(store, cfg, {"week": week, "save": True})
        self.assertEqual(result["slots"], [])
        self.assertFalse(result["saved"])
        self.assertIsNone(result["saved_plan_id"])
        self.assertIn("nothing to schedule", result["note"])
        self.assertIsNone(store.get_plan(result["week"]))
        self.assertEqual(store.stats()["plans"], 0)


class MeetingEndpointTest(ServerCase):
    """POST /api/meeting — flag / clear the next presentation meeting."""

    def test_010_set_then_visible_in_state(self):
        when = date.today() + timedelta(days=13)
        status, data = self.post("/api/meeting", {
            "date": when.isoformat(), "title": "PI meeting",
            "calendar": "Work", "id": "cal|abc|S|E"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["set"], data)
        self.assertEqual(data["meeting"]["days_until"], 13)
        self.assertFalse(data["meeting"]["passed"])
        stored = self.store.get_next_meeting()
        self.assertEqual(stored["id"], "cal|abc|S|E")
        self.assertIn("flagged_at", stored)

        status, state = self.get("/api/state")
        self.assertEqual(status, 200, state)
        self.assertEqual(state["meeting"]["date"], when.isoformat())
        self.assertEqual(state["meeting"]["title"], "PI meeting")
        self.assertEqual(state["meeting"]["calendar"], "Work")

        status, events = self.get("/api/log?limit=5")
        self.assertIn("meeting_set", [e["kind"] for e in events["events"]])

    def test_020_clear_and_date_validation(self):
        when = date.today() + timedelta(days=2)
        self.store.set_next_meeting({"date": when.isoformat(), "title": "PI"})
        status, data = self.post("/api/meeting", {"clear": True})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["cleared"], data)
        self.assertIsNone(data["meeting"])
        status, state = self.get("/api/state")
        self.assertIsNone(state["meeting"])

        status, bad = self.post("/api/meeting", {"date": "november"})
        self.assertEqual(status, 400)
        self.assertIn("date", bad["error"])

        # no date and no clear flag is also a clear (flag once, clear once)
        self.store.set_next_meeting({"date": when.isoformat(), "title": "PI"})
        status, data = self.post("/api/meeting", {})
        self.assertEqual(status, 200, data)
        self.assertIsNone(self.store.get_next_meeting())

    def test_030_passed_meeting_reports_passed(self):
        past = (date.today() - timedelta(days=3)).isoformat()
        self.store.set_next_meeting({"date": past, "title": "old one"})
        status, state = self.get("/api/state")
        self.assertTrue(state["meeting"]["passed"])
        self.assertLess(state["meeting"]["days_until"], 0)


class IntakeEndpointTest(ServerCase):
    """POST /api/intake — offline mode, week window, meeting-default deadline."""

    def test_010_offline_intake_sets_the_window(self):
        status, data = self.post("/api/intake", {
            "description": "create an audit for coreference resolution",
            "mode": "offline", "within_weeks": 2})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["status"], "applied", data)
        task = self.store.get_task(data["task_id"])
        self.assertEqual(task["within_weeks"], 2)
        self.assertEqual(task["est_minutes"], data["plan"]["estimate_minutes"])
        self.assertTrue(data["analysis"]["why"])

    def test_020_empty_window_means_any_week(self):
        status, data = self.post("/api/intake", {
            "description": "polish the abstract", "mode": "offline",
            "within_weeks": ""})
        self.assertEqual(status, 200, data)
        self.assertIsNone(self.store.get_task(data["task_id"])["within_weeks"])

    def test_030_meeting_becomes_the_default_deadline(self):
        when = (date.today() + timedelta(days=13)).isoformat()
        self.store.set_next_meeting({"date": when, "title": "PI meeting"})
        status, data = self.post("/api/intake", {
            "description": "write the related-work summary", "mode": "offline"})
        self.assertEqual(status, 200, data)
        task = self.store.get_task(data["task_id"])
        self.assertEqual(task["deadline"], when)
        self.assertIn("presentation meeting",
                      " ".join(data["analysis"]["why"]))

    def test_040_missing_description_is_400(self):
        status, data = self.post("/api/intake", {"description": "   "})
        self.assertEqual(status, 400)
        self.assertIn("error", data)


class InstantIntakeTest(ServerCase):
    """The default intake path: one synchronous OpenRouter call, the answer
    applied before the endpoint returns."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cfg["brain"]["llm"].update(
            base_url="https://openrouter.ai/api/v1",
            model="openrouter/free",
            # never fall through to the real planner/data/llm_key
            key_file=os.path.join(cls.tmpdir.name, "llm_key"))

    ANSWER = {"title": "Create an audit for coreference resolution",
              "area": "coreference", "complexity": "M",
              "estimate_minutes": 77, "priority": 1, "confidence": 0.8,
              "deep_work": True,
              "subtasks": [{"title": "Do the audit", "minutes": 77}],
              "rationale": "agent says 77"}

    def test_instant_estimate_applies_the_llm_answer(self):
        calls = {}

        def fake_post(url, payload, headers, timeout=90):
            calls["url"] = url
            calls["model"] = payload.get("model")
            return {"choices": [{"message": {"content": json.dumps(self.ANSWER)}}]}

        original = brain._http_post_json
        brain._http_post_json = fake_post
        try:
            status, data = self.post("/api/intake", {
                "description": "create an audit for coreference resolution"})
        finally:
            brain._http_post_json = original

        self.assertEqual(status, 200, data)
        self.assertEqual(data["status"], "applied", data)
        self.assertEqual(data["plan"]["estimate_minutes"], 77)
        task = self.store.get_task(data["task_id"])
        self.assertEqual(task["est_source"], "llm")
        self.assertEqual(task["est_minutes"], 77)
        self.assertIn("instant API estimate via openrouter/free",
                      " ".join(data["analysis"]["why"]))
        self.assertTrue(calls["url"].startswith(
            "https://openrouter.ai/api/v1/chat/completions"), calls["url"])
        self.assertEqual(calls["model"], "openrouter/free")


class WindowScheduleTest(ServerCase):
    """Scheduling a week respects each task's within_weeks window."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.week_here = scheduler.week_start_for().isoformat()
        cls.week_next = scheduler.week_start_for(offset_weeks=1).isoformat()
        cls.soon = cls.store.add_task("This week only", est_minutes=60,
                                      within_weeks=1)
        cls.later = cls.store.add_task("Any week is fine", est_minutes=60)

    def test_010_window_of_one_is_placed_in_the_current_week(self):
        if date.today().weekday() not in (0, 1, 2, 3, 4):
            self.skipTest("today is not a work day; the week is already over")
        status, data = self.post("/api/schedule", {"week": self.week_here,
                                                   "save": False})
        self.assertEqual(status, 200, data)
        self.assertIn(self.soon, {s["task_id"] for s in data["slots"]})

    def test_020_window_of_one_refuses_next_week(self):
        status, data = self.post("/api/schedule", {"week": self.week_next,
                                                   "save": False})
        self.assertEqual(status, 200, data)
        self.assertEqual({s["task_id"] for s in data["slots"]}, {self.later})


class CalendarWriteTest(ServerCase):
    """write_calendar: one event per block; the replaced plan's events (minus
    carried pinned blocks) are deleted, so repeated submits do not litter."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.week = scheduler.week_start_for(offset_weeks=1).isoformat()
        cls.tid = cls.store.add_task("Write the audit", complexity="M",
                                     est_minutes=90)
        # a previously submitted plan for the same week carries one event uid
        cls.store.save_plan(cls.week, [{
            "task_id": cls.tid, "date": cls.week, "start": "09:00",
            "end": "10:00", "minutes": 60, "rationale": "old"}])
        old_slot = cls.store.get_plan(cls.week)["slots"][0]
        cls.store.set_slot_calendar_id(old_slot["id"], "UID-STALE")

        cls.writes, cls.deletes = [], []
        cls.fake_write = None
        cls.orig_write = calendar_mac.write_event
        cls.orig_delete = calendar_mac.delete_event

        def fake_write(summary, start_iso, end_iso, calendar_name,
                       description="", runner=None):
            uid = "UID-%d" % (len(cls.writes) + 1)
            cls.writes.append((summary, start_iso, end_iso, calendar_name))
            return uid

        cls.fake_write = fake_write

        def fake_delete(uid, calendar_name=None, runner=None):
            cls.deletes.append(uid)
            return {"found": 1, "deleted": 1, "error": None}

        calendar_mac.write_event = fake_write
        calendar_mac.delete_event = fake_delete

    @classmethod
    def tearDownClass(cls):
        calendar_mac.write_event = cls.orig_write
        calendar_mac.delete_event = cls.orig_delete
        super().tearDownClass()

    def test_010_first_submit_writes_events_and_removes_stale_ones(self):
        status, data = self.post("/api/schedule", {"week": self.week,
                                                   "save": True,
                                                   "write_calendar": True})
        self.assertEqual(status, 200, data)
        report = data["calendar_written"]
        self.assertEqual(report["created"], 1, report)
        self.assertEqual(report["removed"], 1, report)
        self.assertIsNone(report["error"])
        self.assertEqual(self.deletes, ["UID-STALE"])
        summary, start_iso, end_iso, calendar_name = self.writes[0]
        self.assertEqual(summary, "Thesis: Write the audit")
        self.assertTrue(start_iso.startswith(self.week))
        self.assertTrue(end_iso > start_iso)
        self.assertEqual(calendar_name, self.cfg["calendar_name"])
        # the new block remembered its event, the stale one is gone
        saved = self.store.get_plan(self.week)["slots"]
        self.assertEqual([s["calendar_event_id"] for s in saved], ["UID-1"])

    def test_020_second_submit_replaces_instead_of_littering(self):
        status, data = self.post("/api/schedule", {"week": self.week,
                                                   "save": True,
                                                   "write_calendar": True})
        self.assertEqual(status, 200, data)
        report = data["calendar_written"]
        self.assertEqual(report["created"], 1, report)
        self.assertEqual(report["removed"], 1, report)
        # run 1's event was deleted, run 2's event was created — still one
        self.assertEqual(self.deletes, ["UID-STALE", "UID-1"])
        saved = self.store.get_plan(self.week)["slots"]
        self.assertEqual([s["calendar_event_id"] for s in saved], ["UID-2"])

    def test_030_write_failure_is_reported_not_raised(self):
        def boom(*args, **kwargs):
            raise Exception("calendar is locked")

        calendar_mac.write_event = boom
        try:
            status, data = self.post("/api/schedule", {"week": self.week,
                                                       "save": True,
                                                       "write_calendar": True})
        finally:
            calendar_mac.write_event = self.fake_write
        self.assertEqual(status, 200, data)
        report = data["calendar_written"]
        self.assertEqual(report["created"], 0)
        self.assertGreaterEqual(report["failed"], 1)
        self.assertIn("calendar is locked", report["error"])
        # the plan itself is saved regardless of calendar luck
        self.assertIsNotNone(self.store.get_plan(self.week))


class TaskCleanupTest(ServerCase):
    """Deleting a task — or dropping it — takes every block off the plan and
    every related event off Apple Calendar (faked here)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.orig_delete = calendar_mac.delete_event
        cls.deletes = []

        def fake_delete(uid, calendar_name=None, runner=None):
            cls.deletes.append(uid)
            return {"found": 1, "deleted": 1, "error": None}

        calendar_mac.delete_event = fake_delete

    @classmethod
    def tearDownClass(cls):
        calendar_mac.delete_event = cls.orig_delete
        super().tearDownClass()

    def _task_with_slots(self, title, uids):
        """One task + one slot per uid ("" = no calendar event)."""
        tid = self.store.add_task(title)
        slots = [{"task_id": tid, "date": self.week,
                  "start": "%02d:00" % (9 + i), "end": "%02d:30" % (9 + i),
                  "minutes": 30, "rationale": "r"}
                 for i in range(len(uids))]
        self.store.save_plan(self.week, slots)
        saved = self.store.get_plan(self.week)["slots"]
        for slot, uid in zip(saved, uids):
            if uid:
                self.store.set_slot_calendar_id(slot["id"], uid)
        return tid

    def setUp(self):
        self.week = scheduler.week_start_for(offset_weeks=1).isoformat()
        type(self).deletes = []

    def test_delete_task_removes_record_blocks_and_events(self):
        tid = self._task_with_slots("Doomed audit", ["UID-A", ""])
        status, data = self.request(f"/api/tasks/{tid}", method="DELETE")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["removed"])
        self.assertEqual(data["unscheduled"]["blocks"], 2)
        self.assertEqual(data["unscheduled"]["calendar_removed"], 1)
        self.assertEqual(self.deletes, ["UID-A"])          # only the uid'ed one
        self.assertIsNone(self.store.get_task(tid))
        self.assertEqual(self.store.slots_for_task(tid), [])
        self.assertEqual(self.store.get_plan(self.week)["slots"], [])
        status, _ = self.request(f"/api/tasks/{tid}", method="DELETE")
        self.assertEqual(status, 404)                      # gone is gone

    def test_drop_unschedules_but_keeps_the_record(self):
        tid = self._task_with_slots("Maybe audit", ["UID-B"])
        status, data = self.request(f"/api/tasks/{tid}/status",
                                    {"status": "dropped"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["task"]["status"], "dropped")
        self.assertEqual(data["unscheduled"]["blocks"], 1)
        self.assertEqual(data["unscheduled"]["calendar_removed"], 1)
        self.assertEqual(self.deletes, ["UID-B"])
        self.assertIsNotNone(self.store.get_task(tid))     # record survives
        self.assertEqual(self.store.slots_for_task(tid), [])

    def test_drop_without_slots_reports_zero(self):
        tid = self.store.add_task("Never scheduled")
        status, data = self.request(f"/api/tasks/{tid}/status",
                                    {"status": "dropped"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["unscheduled"]["blocks"], 0)
        self.assertEqual(self.deletes, [])

    def test_other_status_leaves_slots_alone(self):
        tid = self._task_with_slots("Started audit", ["UID-C"])
        status, data = self.request(f"/api/tasks/{tid}/status",
                                    {"status": "doing"})
        self.assertEqual(status, 200, data)
        self.assertIsNone(data["unscheduled"])
        self.assertEqual(self.deletes, [])
        self.assertEqual(len(self.store.slots_for_task(tid)), 1)


class SlotSelfHealTest(ServerCase):
    """Moving a uid-less block self-heals: the missing Apple Calendar event
    is written on the spot, so blocks created before the calendar existed
    (or by an import that could not write) can never leave Calendar behind."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.week = scheduler.week_start_for(offset_weeks=1).isoformat()
        cls.writes, cls.updates = [], []
        cls.orig_write = calendar_mac.write_event
        cls.orig_update = getattr(calendar_mac, "update_event_times", None)

        def fake_write(summary, start_iso, end_iso, calendar_name,
                       description="", runner=None):
            uid = "UID-HEAL-%d" % (len(cls.writes) + 1)
            cls.writes.append((summary, start_iso, end_iso, description))
            return uid

        def fake_update(uid, start_iso, end_iso, calendar_name=None,
                        runner=None):
            cls.updates.append((uid, start_iso, end_iso))
            return {"found": 1, "updated": 1, "error": None}

        cls.fake_write = fake_write
        calendar_mac.write_event = fake_write
        calendar_mac.update_event_times = fake_update

    @classmethod
    def tearDownClass(cls):
        calendar_mac.write_event = cls.orig_write
        if cls.orig_update is not None:
            calendar_mac.update_event_times = cls.orig_update
        elif hasattr(calendar_mac, "update_event_times"):
            del calendar_mac.update_event_times
        super().tearDownClass()

    def setUp(self):
        type(self).writes = []
        type(self).updates = []

    def _legacy_slot(self, title="Legacy audit", subtasks=None):
        tid = self.store.add_task(title, est_minutes=60)
        if subtasks:
            self.store.update_task(tid, subtasks=json.dumps(subtasks))
        self.store.save_plan(self.week, [
            {"task_id": tid, "date": self.week, "start": "09:00",
             "end": "10:00", "minutes": 60, "rationale": "wrong spot"}])
        return self.store.slots_for_task(tid)[0]

    def test_move_writes_the_missing_event_and_stamps_the_uid(self):
        slot = self._legacy_slot(
            subtasks=[{"title": "Reread section 2", "minutes": 30}])
        self.assertIsNone(slot["calendar_event_id"])
        status, data = self.request(f"/api/slots/{slot['id']}",
                                    {"start": "11:00"})   # duration kept
        self.assertEqual(status, 200, data)
        self.assertEqual(data["calendar"].get("created"), "UID-HEAL-1")
        moved = self.store.get_slot(slot["id"])
        self.assertEqual((moved["start"], moved["end"]), ("11:00", "12:00"))
        self.assertEqual(moved["calendar_event_id"], "UID-HEAL-1")
        summary, start_iso, end_iso, description = self.writes[0]
        self.assertEqual(summary, "Thesis: Legacy audit")
        self.assertEqual(start_iso, "%sT11:00:00" % self.week)
        self.assertIn("Task #%d" % moved["task_id"], description)
        self.assertIn("Reread section 2 (30 min)", description)

    def test_move_with_a_uid_updates_instead_of_creating(self):
        slot = self._legacy_slot()
        self.store.set_slot_calendar_id(slot["id"], "UID-ORIG")
        status, data = self.request(f"/api/slots/{slot['id']}",
                                    {"start": "15:00", "end": "16:00"})
        self.assertEqual(status, 200, data)
        self.assertNotIn("created", data["calendar"])
        self.assertEqual(self.writes, [])               # no self-heal write
        self.assertEqual(self.updates,
                         [("UID-ORIG", "%sT15:00" % self.week,
                           "%sT16:00" % self.week)])


class PastTaskEndpointTest(ServerCase):
    """POST /api/tasks/past: pre-app work lands as done with backdated
    events, feeds calibration, and counts on the task list."""

    def test_010_round_trip(self):
        status, data = self.post("/api/tasks/past", {
            "title": "Pre-app literature sweep", "area": "related work",
            "est_minutes": 120, "actual_minutes": 90, "done": "2026-09-01"})
        self.assertEqual(status, 200, data)
        task = data["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["actual_minutes"], 90)
        self.assertEqual(task["area"], "related work")
        status, body = self.raw_get("/api/tasks")
        listed = {t["id"]: t for t in json.loads(body)["tasks"]}
        self.assertIn(task["id"], listed)
        self.assertEqual(listed[task["id"]]["status"], "done")

    def test_020_defaults(self):
        status, data = self.post("/api/tasks/past", {"title": "Quick fix"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["task"]["est_minutes"], 60)
        self.assertIsNone(data["task"]["actual_minutes"])

    def test_030_validation(self):
        for payload, frag in (({"title": ""}, "title"),
                              ({"title": "x", "est_minutes": -5}, "positive"),
                              ({"title": "x", "actual_minutes": "soon"},
                               "integers"),
                              ({"title": "x", "done": "09/2025"}, "YYYY-MM-DD")):
            status, data = self.post("/api/tasks/past", payload)
            self.assertEqual(status, 400, (payload, data))
            self.assertIn(frag, data["error"])


class ConcurrencyTest(unittest.TestCase):
    """Regression: a browser preconnect must not starve a real request."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "p.db"))
        cfg = deepcopy(config_mod.DEFAULT_CONFIG)
        self.server = web.make_server(self.store, cfg, port=0, quiet=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path="/", timeout=6):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, body
        finally:
            conn.close()

    def test_idle_socket_does_not_block_other_clients(self):
        idle = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(idle.close)
        try:
            status, body = self._get("/api/stats")
            self.assertEqual(status, 200)
            self.assertIn(b"tasks", body)
        finally:
            idle.close()

    def test_parallel_clients_are_all_served(self):
        idle = [socket.create_connection(("127.0.0.1", self.port), timeout=5)
                for _ in range(4)]
        for s in idle:
            self.addCleanup(s.close)
        results = []

        def fetch(path):
            results.append(self._get(path)[0])

        threads = [threading.Thread(target=fetch, args=("/api/stats",))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(results, [200, 200, 200, 200])
        for s in idle:
            s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
