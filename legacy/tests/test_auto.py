"""test_auto.py — the autonomous planning cycle (planner/auto.py).

Covers the shortened agent loop end to end against a temp DB + queue dir:
an answered request is applied, the task is re-planned — never before
"now" — and a second cycle changes nothing (idempotence).
"""
import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta
from unittest import mock

from planner import auto, brain as brain_mod, calendar_mac, config as config_mod, scheduler
from planner.scheduler import parse_hhmm
from planner.store import Store


PLAN = {
    "title": "Fix the validator edge cases",
    "area": "improvement",
    "complexity": "M",
    "estimate_minutes": 90,
    "priority": 1,
    "deep_work": True,
    "confidence": 0.7,
    "subtasks": [{"title": "Write the failing test", "minutes": 30},
                 {"title": "Fix + rerun", "minutes": 60}],
    "rationale": "validator has known edge cases",
}


class AutoCycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "p.db"))
        self.cfg = deepcopy(config_mod.DEFAULT_CONFIG)
        self.cfg["brain"]["backend"] = "heuristic"   # deterministic, offline
        # cycles must be able to place blocks on ANY day the suite runs
        # (Sundays included); the calendar rules are exercised elsewhere
        self.cfg["work_days"] = [0, 1, 2, 3, 4, 5, 6]

    # -- helpers ----------------------------------------------------------
    def add_unestimated_task(self):
        return self.store.add_task("Fix the validator edge cases",
                                   complexity="M", est_minutes=90, priority=2)

    # -- tests ------------------------------------------------------------
    def test_estimate_applied_and_replanned(self):
        self.tid = self.add_unestimated_task()
        res = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        self.assertEqual(res["enriched"], 1)
        task = self.store.get_task(self.tid)
        self.assertEqual(task["est_source"], "heuristic")
        self.assertEqual(task["est_minutes"], PLAN["estimate_minutes"])
        self.assertIsInstance(json.loads(task["subtasks"]), list)

        # the week plan now exists and mentions only tasks that exist
        plan = self.store.get_plan(res["week"])
        self.assertIsNotNone(plan)
        self.assertTrue(res["replanned"])
        ids = {s["task_id"] for s in plan["slots"]}
        self.assertIn(self.tid, ids)

    def test_second_cycle_is_a_noop(self):
        self.tid = self.add_unestimated_task()
        first = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        self.assertEqual(first["enriched"], 1)
        before = self.store.get_plan(first["week"])["slots"]
        second = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        self.assertEqual(second["enriched"], 0)
        self.assertFalse(second["replanned"])
        after = self.store.get_plan(second["week"])["slots"]
        self.assertEqual(before, after)

    def test_cycle_never_plans_before_now(self):
        today = date.today()
        if today.weekday() not in self.cfg["work_days"]:
            self.skipTest("today is not a work day; nothing lands today")
        self.tid = self.add_unestimated_task()
        res = auto.run_cycle(self.store, self.cfg, use_calendar=False,
                             force=True)
        plan = self.store.get_plan(res["week"])
        now_min = datetime.now().hour * 60 + datetime.now().minute
        for slot in plan["slots"]:
            self.assertGreaterEqual(slot["date"], today.isoformat())
            if slot["date"] == today.isoformat():
                self.assertGreater(parse_hhmm(slot["start"]), now_min - 5,
                                   "a fresh plan must not use the past")

    def test_pinned_slot_survives_replan(self):
        self.tid = self.add_unestimated_task()
        res = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        plan = self.store.get_plan(res["week"])
        if not plan["slots"]:
            self.skipTest("no blocks planned (non-work week)")
        slot = plan["slots"][0]
        # the user moves the first block by hand: a re-plan must keep it
        # exactly where it was pinned.
        self.store.move_slot(slot["id"], slot["date"], slot["start"],
                             slot["end"])
        before = self.store.get_plan(res["week"])["slots"][0]
        self.assertEqual(before["pinned"], 1)
        res2 = auto.run_cycle(self.store, self.cfg, use_calendar=False,
                              force=True)
        kept = [s for s in
                self.store.get_plan(res2["week"])["slots"]
                if s["pinned"]]
        self.assertTrue(kept)
        self.assertEqual((kept[0]["date"], kept[0]["start"], kept[0]["end"]),
                         (before["date"], before["start"], before["end"]))

    def test_cycle_lock_backs_off(self):
        self.tid = self.add_unestimated_task()
        with auto.CYCLE_LOCK:
            res = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        self.assertTrue(res["skipped"])

    def test_cycle_llm_estimates_default_off_uses_heuristic(self):
        # backend llm (unconfigured) + quota guard: the cycle must fall back
        # to the heuristic quietly, never raise
        self.cfg["brain"]["backend"] = "llm"
        self.tid = self.add_unestimated_task()
        res = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        self.assertEqual(res["enriched"], 1)
        self.assertEqual(self.store.get_task(self.tid)["est_source"],
                         "heuristic")

    def test_cycle_llm_estimates_opt_in_uses_the_llm(self):
        self.cfg["brain"]["backend"] = "llm"
        self.cfg["brain"]["llm"].update(base_url="https://example.invalid/v1",
                                        model="test-model")
        self.cfg["auto"]["llm_estimates"] = True

        def fake_post(url, payload, headers, timeout=90):
            return {"choices": [{"message": {"content": json.dumps(PLAN)}}]}

        original = brain_mod._http_post_json
        brain_mod._http_post_json = fake_post
        try:
            self.tid = self.add_unestimated_task()
            res = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        finally:
            brain_mod._http_post_json = original
        self.assertEqual(res["enriched"], 1)
        self.assertEqual(self.store.get_task(self.tid)["est_source"], "llm")


class HorizonTest(unittest.TestCase):
    """plan_horizon: a task lands whole in the first week of its window where
    it fits; later weeks carry the remainder, never past the window."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "p.db"))
        self.cfg = deepcopy(config_mod.DEFAULT_CONFIG)
        self.cfg["horizon_weeks"] = 3
        self.cfg["brain"]["agent_queue_dir"] = os.path.join(self.tmp.name, "queue")
        os.makedirs(self.cfg["brain"]["agent_queue_dir"], exist_ok=True)
        # A Monday far in the future: the whole horizon lies ahead of "today",
        # so no "not before now" clamping interferes.
        self.start = date(2031, 3, 3)
        self.assertEqual(self.start.weekday(), 0)

    def plan(self):
        return auto.plan_horizon(self.store, self.cfg, self.start,
                                 self.store.list_tasks(), use_calendar=False)

    def fill_week(self, week_start):
        """Make the work days of `week_start` unusable via pinned blocks."""
        tid = self.store.add_task("Existing commitments")
        self.store.save_plan(week_start.isoformat(), [
            {"task_id": tid, "date": (week_start + timedelta(days=d)).isoformat(),
             "start": "09:00", "end": "19:00", "minutes": 600,
             "rationale": "busy"} for d in range(5)])
        for s in self.store.get_plan(week_start.isoformat())["slots"]:
            self.store.move_slot(s["id"], s["date"], s["start"], "18:50")
        self.store.set_status(tid, "done")     # the filler itself is not up for planning
        return tid

    def test_small_task_lands_in_week_zero_and_the_loop_stops(self):
        self.store.add_task("Sample twenty resolved rows", complexity="S",
                            est_minutes=45)
        plans = self.plan()
        self.assertEqual(len(plans), 1)            # nothing left -> later weeks skipped
        self.assertEqual(plans[0]["week"], self.start.isoformat())
        self.assertEqual(len(plans[0]["slots"]), 1)
        saved = self.store.get_plan(self.start.isoformat())
        self.assertIsNotNone(saved)
        self.assertEqual(saved["note"], "auto")
        self.assertEqual(len(saved["slots"]), 1)

    def test_task_carries_to_the_next_week_when_this_one_is_full(self):
        self.fill_week(self.start)
        tid = self.store.add_task("Write the audit", complexity="M",
                                  est_minutes=90, within_weeks=2)
        plans = self.plan()
        self.assertEqual([p["week"] for p in plans],
                         [self.start.isoformat(),
                          (self.start + timedelta(days=7)).isoformat()])
        # the filled week kept its own (pinned) blocks; the task went to week 1
        self.assertEqual(
            {s["task_id"] for s in
             self.store.get_plan(self.start.isoformat())["slots"]} & {tid},
            set())
        target = self.store.get_plan((self.start + timedelta(days=7)).isoformat())
        self.assertIsNotNone(target)
        self.assertIn(tid, {s["task_id"] for s in target["slots"]})

    def test_window_of_one_is_never_pushed_deeper(self):
        self.fill_week(self.start)
        tid = self.store.add_task("This week only", complexity="M",
                                  est_minutes=90, within_weeks=1)
        plans = self.plan()                        # horizon 3, task fits nowhere
        self.assertEqual(len(plans), 3)
        self.assertIn(tid, {u["task_id"] for u in plans[0]["unscheduled"]})
        # no week ever received the task, and weeks without blocks are not saved
        for offset in (7, 14):
            self.assertIsNone(
                self.store.get_plan((self.start + timedelta(days=offset)).isoformat()))

    def test_task_window_extends_the_default_horizon(self):
        self.cfg["horizon_weeks"] = 1
        self.fill_week(self.start)
        tid = self.store.add_task("Two weeks out", complexity="M",
                                  est_minutes=90, within_weeks=2)
        plans = self.plan()
        self.assertIn((self.start + timedelta(days=7)).isoformat(),
                      [p["week"] for p in plans])
        target = self.store.get_plan((self.start + timedelta(days=7)).isoformat())
        self.assertIsNotNone(target)
        self.assertIn(tid, {s["task_id"] for s in target["slots"]})

    def test_run_cycle_reports_the_horizon(self):
        self.store.add_task("Fix the validator edge cases", complexity="M",
                            est_minutes=90, priority=2)
        # the real current week is planned: on weekends nothing lands in the
        # default Mon-Fri horizon, so make every day placeable for this test
        self.cfg["work_days"] = [0, 1, 2, 3, 4, 5, 6]
        res = auto.run_cycle(self.store, self.cfg, use_calendar=False, force=True)
        self.assertTrue(res["replanned"], res)
        self.assertTrue(res["plans"])
        self.assertEqual(res["planned_weeks"][0], res["week"])
        self.assertEqual(res["plan"], res["plans"][0])


class CalendarSyncTest(unittest.TestCase):
    """Two-way sync: Apple Calendar's side of the story lands in the plan.

    week_events is faked (one dict in, one dict out) — the osascript itself
    is covered by test_calendar.py. All dates sit inside the rolling
    [today-7, today+21] window sync_calendar reads.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "p.db"))
        self.cfg = deepcopy(config_mod.DEFAULT_CONFIG)
        self.cfg["calendar_name"] = "Thesis"
        self.events = []
        self.payload_error = None
        patcher = mock.patch.object(calendar_mac, "week_events",
                                    side_effect=self._week_events)
        patcher.start()
        self.addCleanup(patcher.stop)
        # run_cycle(use_calendar=True) reads busy time too — keep it fake
        patcher2 = mock.patch.object(calendar_mac, "read_busy",
                                     return_value={"events": [],
                                                   "calendars": [], "error": None})
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def _week_events(self, start, end, calendar_names=None, runner=None):
        return {"events": list(self.events),
                "calendars": calendar_names or ["Thesis"],
                "error": self.payload_error}

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def ev(uid, date_, start, end, title="Thesis: Write paper"):
        return {"id": uid, "date": date_, "start": start, "end": end,
                "all_day": False, "calendar": "Thesis", "title": title,
                "location": ""}

    def seed_slot(self, date_="2026-11-03", start="09:00", end="10:00",
                  title="Write paper"):
        tid = self.store.add_task(title, est_minutes=60)
        self.store.save_plan("2026-11-02", [
            {"task_id": tid, "date": date_, "start": start, "end": end,
             "minutes": parse_hhmm(end) - parse_hhmm(start)}])
        slot = self.store.slots_for_task(tid)[0]
        return tid, slot

    def sync(self):
        return auto.sync_calendar(self.store, self.cfg)

    # -- tests ------------------------------------------------------------
    def test_external_move_pulls_the_slot_and_pins_it(self):
        tid, slot = self.seed_slot()
        self.store.set_slot_calendar_id(slot["id"], "UID-M")
        self.events = [self.ev("UID-M", "2026-11-03", "14:00", "15:00")]
        report = self.sync()
        moved = self.store.get_slot(slot["id"])
        self.assertEqual((moved["date"], moved["start"], moved["end"]),
                         ("2026-11-03", "14:00", "15:00"))
        self.assertEqual(moved["pinned"], 1)     # re-plans must keep the hand edit
        self.assertEqual(report["moved"], 1)

    def test_external_delete_removes_the_slot(self):
        tid, slot = self.seed_slot()
        self.store.set_slot_calendar_id(slot["id"], "UID-D")
        self.events = []                          # gone in Calendar.app
        report = self.sync()
        self.assertIsNone(self.store.get_slot(slot["id"]))
        self.assertEqual(report["removed"], 1)

    def test_unknown_event_imports_a_new_task(self):
        self.events = [self.ev("UID-N", "2026-11-04", "10:00", "12:00",
                               title="Thesis: Read survey papers")]
        report = self.sync()
        self.assertEqual(report["imported"], 1)
        task = next(t for t in self.store.list_tasks()
                    if t["title"] == "Read survey papers")
        self.assertEqual(task["status"], "todo")
        self.assertEqual(task["est_minutes"], 120)
        slot = self.store.slots_for_task(task["id"])[0]
        self.assertEqual((slot["date"], slot["start"], slot["end"]),
                         ("2026-11-04", "10:00", "12:00"))
        self.assertEqual(slot["calendar_event_id"], "UID-N")
        self.assertEqual(slot["pinned"], 1)
        # idempotent: a second pass sees the uid and changes nothing
        again = self.sync()
        self.assertEqual((again["moved"], again["removed"], again["imported"]),
                         (0, 0, 0))

    def test_matching_title_and_time_stamps_the_existing_block(self):
        # a block written before the calendar existed: no uid, same time as
        # the event the user created by hand in Calendar.app
        tid, slot = self.seed_slot(title="Polish intro")
        self.events = [self.ev("UID-T", "2026-11-03", "09:00", "10:00",
                               title="Thesis: Polish intro")]
        report = self.sync()
        self.assertEqual(report["imported"], 0)
        tasks = [t for t in self.store.list_tasks() if t["id"] != tid]
        self.assertEqual(tasks, [])               # no duplicate task either
        stamped = self.store.get_slot(slot["id"])
        self.assertEqual(stamped["calendar_event_id"], "UID-T")

    def test_subject_prefixed_event_imports_with_the_subject_area(self):
        uni = os.path.join(self.tmp.name, "uni")
        os.makedirs(os.path.join(uni, "NLP Semantics"), exist_ok=True)
        self.cfg["university_root"] = uni
        self.events = [self.ev("UID-S", "2026-11-05", "10:00", "11:00",
                               title="NLP Semantics: reading response")]
        report = self.sync()
        self.assertEqual(report["imported"], 1)
        task = next(t for t in self.store.list_tasks()
                    if t["title"] == "reading response")
        self.assertEqual(task["area"], "NLP Semantics")

    def test_unknown_prefix_imports_with_the_prefix_in_the_title(self):
        # anything in the planner's calendar is planner material; a summary
        # without a known category prefix keeps the whole title
        self.events = [self.ev("UID-X", "2026-11-05", "10:00", "11:00",
                               title="Dentist: cleaning")]
        report = self.sync()
        self.assertEqual(report["imported"], 1)
        task = self.store.list_tasks()[0]
        self.assertEqual(task["title"], "Dentist: cleaning")
        self.assertEqual(task["area"], "")

    def test_event_summary_uses_the_category_when_it_is_one(self):
        uni = os.path.join(self.tmp.name, "uni")
        os.makedirs(os.path.join(uni, "NLP Semantics"), exist_ok=True)
        self.cfg["university_root"] = uni
        # a legacy keyword area is not a category: Thesis prefix stands
        self.assertEqual(
            calendar_mac.event_summary({"title": "polish intro",
                                        "area": "coreference"}, self.cfg),
            "Thesis: polish intro")
        self.assertEqual(
            calendar_mac.event_summary({"title": "reading",
                                        "area": "NLP Semantics"}, self.cfg),
            "NLP Semantics: reading")
        self.assertEqual(
            calendar_mac.event_summary({"title": "polish intro"}, None),
            "Thesis: polish intro")

    def test_all_day_events_are_ignored(self):
        self.events = [dict(self.ev("UID-A", "2026-11-04", "00:00", "00:00"),
                            all_day=True, title="Thesis: deadline")]
        report = self.sync()
        self.assertEqual((report["moved"], report["removed"],
                          report["imported"]), (0, 0, 0))
        self.assertEqual(self.store.list_tasks(), [])

    def test_calendar_error_becomes_data_not_an_exception(self):
        self.payload_error = "Automation permission lost"
        report = self.sync()
        self.assertIn("permission", report["error"])
        self.assertEqual(self.store.list_tasks(), [])

    def test_run_cycle_carries_the_sync_report_and_replans_on_deltas(self):
        tid, slot = self.seed_slot()
        self.store.set_slot_calendar_id(slot["id"], "UID-R")
        self.events = [self.ev("UID-R", "2026-11-03", "14:00", "15:00")]
        # the sync delta must show up as a re-plan with blocks written: the
        # real current week has to be placeable even on weekends
        self.cfg["work_days"] = [0, 1, 2, 3, 4, 5, 6]
        res = auto.run_cycle(self.store, self.cfg, use_calendar=True,
                             force=True)
        self.assertIsNotNone(res["calendar_sync"])
        self.assertEqual(res["calendar_sync"]["moved"], 1)
        self.assertTrue(res["replanned"])         # sync deltas force re-plan


if __name__ == "__main__":
    unittest.main(verbosity=2)
