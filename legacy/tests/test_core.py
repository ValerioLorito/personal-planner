"""Unit tests for the planner core: store + scheduler."""
import os
import tempfile
import unittest
from datetime import date

from planner.scheduler import (free_intervals_for_day, parse_hhmm,
                               schedule_week, week_dates, week_start_for)
from planner.store import Store

CFG = {
    "work_days": [0, 1, 2, 3, 4],
    "day_start": "09:00", "day_end": "19:00",
    "lunch": ["13:00", "14:00"],
    "buffer_min": 10, "max_block_min": 90, "max_day_min": 300,
    "complexity_minutes": {"S": 45, "M": 90, "L": 180},
    "complexity_rank": {"S": 1, "M": 3, "L": 5},
}

MON = date(2026, 11, 2)          # a Monday


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "p.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_task_crud_and_events(self):
        tid = self.store.add_task("Write validator fix", complexity="M",
                                  est_minutes=90, priority=1,
                                  deadline="2026-11-03", area="coref")
        t = self.store.get_task(tid)
        self.assertEqual(t["title"], "Write validator fix")
        self.assertEqual(t["status"], "todo")
        self.assertEqual(t["priority"], 1)
        self.store.update_task(tid, est_minutes=45, notes="note")
        self.assertEqual(self.store.get_task(tid)["est_minutes"], 45)
        self.store.set_status(tid, "done", note="merged")
        self.assertEqual(self.store.get_task(tid)["status"], "done")
        kinds = [e["kind"] for e in self.store.recent_events(10)]
        self.assertIn("task_added", kinds)
        self.assertIn("task_done", kinds)
        self.assertEqual(self.store.stats()["tasks_by_status"]["done"], 1)

    def test_plan_roundtrip_and_slots_between(self):
        tid = self.store.add_task("A")
        pid = self.store.save_plan("2026-11-02", [
            {"task_id": tid, "date": "2026-11-02", "start": "09:00",
             "end": "10:00", "minutes": 60, "rationale": "r1"},
            {"task_id": tid, "date": "2026-11-03", "start": "14:00",
             "end": "14:30", "minutes": 30, "rationale": "r2"},
        ])
        self.assertGreater(pid, 0)
        plan = self.store.get_plan("2026-11-02")
        self.assertEqual(len(plan["slots"]), 2)
        self.assertEqual(plan["slots"][0]["start"], "09:00")
        got = self.store.slots_between("2026-11-02", "2026-11-02")
        self.assertEqual(len(got), 1)
        self.store.set_slot_calendar_id(got[0]["id"], "UID-1")
        self.assertEqual(self.store.slots_between("2026-11-02", "2026-11-02")[0]
                         ["calendar_event_id"], "UID-1")

    def test_replan_replaces_previous_week(self):
        tid = self.store.add_task("A")
        slot = {"task_id": tid, "date": "2026-11-02", "start": "09:00",
                "end": "10:00", "minutes": 60, "rationale": "old"}
        self.store.save_plan("2026-11-02", [slot])
        self.store.save_plan("2026-11-02", [dict(slot, start="11:00", end="12:00",
                                                 rationale="new")])
        plan = self.store.get_plan("2026-11-02")
        self.assertEqual(len(plan["slots"]), 1)
        self.assertEqual(plan["slots"][0]["rationale"], "new")
        self.assertEqual(len(self.store.slots_between("2026-11-02", "2026-11-02")), 1)

    def test_history(self):
        self.store.add_history("2026-10-21", "commit", "dead-code cleanup",
                               detail="d048829", source="git:d048829")
        rows = self.store.list_history("2026-10-01", "2026-10-31")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "commit")


class SchedulerTest(unittest.TestCase):
    def test_free_intervals_respect_lunch_and_busy(self):
        day = MON
        iv = free_intervals_for_day(day, [(540, 660)], CFG)   # 09:00-11:00 busy
        self.assertTrue(iv)
        for s, e in iv:
            self.assertFalse(s < 780 and e > 780 and s >= 540,
                             "interval must not cross lunch (13:00)")
            self.assertGreaterEqual(s, 660, "must start after busy block")
        self.assertTrue(any(s >= 660 and e <= 780 for s, e in iv))

    def test_schedule_splits_and_respects_constraints(self):
        tasks = [
            {"id": 1, "title": "Big", "status": "todo", "priority": 1,
             "complexity": "L", "est_minutes": 120, "deadline": "2026-11-05"},
            {"id": 2, "title": "Small", "status": "todo", "priority": 2,
             "complexity": "S", "est_minutes": 45, "deadline": "2026-11-03"},
        ]
        busy = {"2026-11-02": [(540, 660)]}          # Monday 09:00-11:00
        res = schedule_week(tasks, MON, busy, CFG)
        slots = res["slots"]
        self.assertEqual(res["unscheduled"], [])
        # 120 min split into 90 + 30 -> 2 blocks for task 1, 1 for task 2
        self.assertEqual(len([s for s in slots if s["task_id"] == 1]), 2)
        self.assertEqual(len([s for s in slots if s["task_id"] == 2]), 1)
        # task 1 (deep work) gets the morning block after the busy window;
        # the urgent small task (deadline first) takes the afternoon.
        t1 = [s for s in slots if s["task_id"] == 1]
        self.assertEqual(t1[0]["date"], "2026-11-02")
        self.assertEqual(t1[0]["start"], "11:10")
        self.assertEqual(t1[0]["minutes"], 90)
        t2 = [s for s in slots if s["task_id"] == 2]
        self.assertEqual(t2[0]["start"], "14:10")
        # slots are returned chronologically
        self.assertEqual(slots, sorted(slots, key=lambda s: (s["date"], s["start"])))
        # no block overlaps lunch or the busy block
        for s in slots:
            st, en = parse_hhmm(s["start"]), parse_hhmm(s["end"])
            self.assertFalse(st < 780 < en, f"lunch overlap: {s}")
            if s["date"] == "2026-11-02":
                self.assertFalse(st < 660 and en > 540, f"busy overlap: {s}")
        # daily cap respected
        for d, mins in res["stats"]["per_day_minutes"].items():
            self.assertLessEqual(mins, 300)

    def test_unschedulable_task_reported(self):
        tasks = [{"id": 9, "title": "Huge", "status": "todo", "priority": 1,
                  "complexity": "L", "est_minutes": 5000, "deadline": None}]
        res = schedule_week(tasks, MON, {}, CFG)
        self.assertTrue(res["unscheduled"])
        self.assertIn("no free interval", res["unscheduled"][0]["reason"])

    def test_week_helpers(self):
        self.assertEqual(MON.weekday(), 0)
        self.assertEqual(week_start_for(MON), MON)
        self.assertEqual(len(week_dates(MON)), 7)
        self.assertEqual(week_dates(MON)[-1], date(2026, 11, 8))


class SlotMoveTest(unittest.TestCase):
    """Manual correction of planned blocks: move, pin, carry over, remove."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "p.db"))
        self.tid = self.store.add_task("Audit the coref funnel")
        self.store.save_plan("2026-11-02", [
            {"task_id": self.tid, "date": "2026-11-02", "start": "09:00",
             "end": "10:00", "minutes": 60, "rationale": "seed"},
            {"task_id": self.tid, "date": "2026-11-03", "start": "14:00",
             "end": "14:30", "minutes": 30, "rationale": "seed 2"},
        ])
        self.slots = self.store.get_plan("2026-11-02")["slots"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_move_within_week_keeps_id_and_pins(self):
        slot = self.slots[0]
        moved = self.store.move_slot(slot["id"], "2026-11-04", "14:00", "15:30")
        self.assertEqual(moved["id"], slot["id"])          # id stays stable
        self.assertEqual((moved["date"], moved["start"], moved["end"]),
                         ("2026-11-04", "14:00", "15:30"))
        self.assertEqual(moved["minutes"], 90)
        self.assertEqual(moved["pinned"], 1)
        self.assertEqual(moved["rationale"], "seed")
        # still part of this week's plan
        plan = self.store.get_plan("2026-11-02")
        self.assertIn(slot["id"], [s["id"] for s in plan["slots"]])

    def test_move_time_only(self):
        slot = self.slots[0]
        moved = self.store.move_slot(slot["id"], new_start="11:00")
        self.assertEqual(moved["date"], "2026-11-02")
        self.assertEqual((moved["start"], moved["end"]), ("11:00", "12:00"))
        self.assertEqual(moved["minutes"], 60)

    def test_move_to_next_week_rehomes_to_that_plan(self):
        slot = self.slots[0]
        moved = self.store.move_slot(slot["id"], "2026-11-10", "09:00", "10:00")
        self.assertEqual(moved["id"], slot["id"])
        self.assertEqual(moved["date"], "2026-11-10")
        # original week no longer holds it; next week does
        self.assertNotIn(slot["id"],
                         [s["id"] for s in self.store.get_plan("2026-11-02")["slots"]])
        target = self.store.get_plan("2026-11-09")
        self.assertIsNotNone(target)
        self.assertEqual([s["id"] for s in target["slots"]], [slot["id"]])

    def test_move_validation(self):
        slot = self.slots[0]
        with self.assertRaises(ValueError):
            self.store.move_slot(slot["id"], "2026-11-04", "15:00", "15:00")
        with self.assertRaises(ValueError):
            self.store.move_slot(slot["id"], "2026-11-04", "16:00", "15:00")
        with self.assertRaises(ValueError):
            self.store.move_slot(slot["id"], "04/11/2026", "09:00", "10:00")
        with self.assertRaises(LookupError):
            self.store.move_slot(999999)

    def test_delete_slot_removes_and_logs(self):
        slot = self.slots[0]
        removed = self.store.delete_slot(slot["id"])
        self.assertEqual(removed["id"], slot["id"])
        self.assertIsNone(self.store.get_slot(slot["id"]))
        self.assertEqual(len(self.store.get_plan("2026-11-02")["slots"]), 1)
        kinds = [e["kind"] for e in self.store.recent_events(10)]
        self.assertIn("slot_removed", kinds)
        with self.assertRaises(LookupError):
            self.store.delete_slot(slot["id"])

    def test_delete_task_compacts_the_id_list(self):
        pre = len(self.store.list_tasks())          # setUp already seeded one
        a = self.store.add_task("A")
        b = self.store.add_task("B")
        c = self.store.add_task("C")
        self.store.save_plan("2026-11-09", [
            {"task_id": c, "date": "2026-11-10", "start": "09:00",
             "end": "10:00", "minutes": 60}])
        self.store.delete_task(b)
        ids = [t["id"] for t in self.store.list_tasks()]
        self.assertEqual(ids, list(range(1, pre + 3)))   # no gap where B was
        self.assertEqual(self.store.get_task(c - 1)["title"], "C")
        # c's slot followed the renumbering
        slots = self.store.get_plan("2026-11-09")["slots"]
        self.assertEqual([s["task_id"] for s in slots], [c - 1])
        # and the next insert reuses the freed number, not a burnt one
        self.assertEqual(self.store.add_task("D"), pre + 3)

    def test_renumber_tasks_closes_old_gaps(self):
        pre = len(self.store.list_tasks())
        ids = [self.store.add_task(f"T{i}") for i in range(4)]
        self.store.conn.execute("DELETE FROM tasks WHERE id=?", (ids[1],))
        self.store.conn.commit()
        before, after = self.store.renumber_tasks()
        self.assertEqual([t["id"] for t in self.store.list_tasks()],
                         list(range(1, pre + 4)))   # 4 added, 1 deleted
        self.assertEqual((before, after), (pre + 4, pre + 3))
        self.assertEqual(self.store.add_task("fresh"), pre + 4)

    def test_add_past_task_counts_for_calibration(self):
        self.store.add_past_task("Slow reading sweep", est_minutes=100,
                                 actual_minutes=140, done_date="2026-09-14")
        tid = self.store.add_past_task("Old coref audit", area="coreference",
                                       est_minutes=100, actual_minutes=40,
                                       done_date="2026-09-15")
        task = self.store.get_task(tid)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["actual_minutes"], 40)
        self.assertEqual(self.store.actual_minutes(tid), 40)
        kinds = [e["kind"] for e in self.store.recent_events(10)]
        self.assertIn("task_doing", kinds)
        self.assertIn("task_done", kinds)
        ratio = __import__("planner.brain", fromlist=["brain"]) \
            .update_calibration(self.store)
        self.assertIsNotNone(ratio)               # two samples: median exists
        self.assertLess(ratio, 1.0)               # median(1.4, 0.4) = 0.9


    def test_slots_for_task_spans_all_weeks(self):
        tid = self.store.add_task("Cross-week task")
        self.store.save_plan("2026-11-02", [
            {"task_id": tid, "date": "2026-11-03", "start": "09:00",
             "end": "10:00", "minutes": 60}])
        self.store.save_plan("2026-11-09", [
            {"task_id": tid, "date": "2026-11-12", "start": "14:00",
             "end": "15:00", "minutes": 60}])
        slots = self.store.slots_for_task(tid)
        self.assertEqual([s["date"] for s in slots],
                         ["2026-11-03", "2026-11-12"])

    def test_delete_task_removes_task_and_all_slots(self):
        tid = self.store.add_task("Doomed task")
        self.store.save_plan("2026-11-02", [
            {"task_id": tid, "date": "2026-11-03", "start": "09:00",
             "end": "10:00", "minutes": 60},
            {"task_id": tid, "date": "2026-11-04", "start": "11:00",
             "end": "12:30", "minutes": 90}])
        removed = self.store.delete_task(tid)
        self.assertEqual(removed["title"], "Doomed task")
        self.assertIsNone(self.store.get_task(tid))
        self.assertEqual(self.store.slots_for_task(tid), [])
        kinds = [e["kind"] for e in self.store.recent_events(10)]
        self.assertIn("task_deleted", kinds)
        ev = [e for e in self.store.recent_events(10)
              if e["kind"] == "task_deleted"][0]
        self.assertEqual(ev["payload"]["title"], "Doomed task")
        self.assertIsNone(self.store.delete_task(tid))   # idempotent-ish: None

    def test_pinned_slots_are_carried_over_by_replans(self):
        slot = self.slots[0]
        moved = self.store.move_slot(slot["id"], "2026-11-02", "10:30", "12:00")
        # the automatic planner replaces the plan; the pinned block must stay
        self.store.save_plan("2026-11-02", [
            {"task_id": self.tid, "date": "2026-11-05", "start": "09:00",
             "end": "10:30", "minutes": 90, "rationale": "fresh"},
        ])
        plan = self.store.get_plan("2026-11-02")
        ids = {s["id"] for s in plan["slots"]}
        self.assertIn(moved["id"], ids)
        carried = [s for s in plan["slots"] if s["id"] == moved["id"]][0]
        self.assertEqual((carried["date"], carried["start"], carried["end"]),
                         ("2026-11-02", "10:30", "12:00"))
        self.assertEqual(carried["pinned"], 1)

    def test_unpinned_slots_do_not_survive_a_replan(self):
        slot = self.slots[1]                      # never moved, never pinned
        self.store.save_plan("2026-11-02", [
            {"task_id": self.tid, "date": "2026-11-05", "start": "09:00",
             "end": "10:30", "minutes": 90, "rationale": "fresh"},
        ])
        self.assertNotIn(slot["id"],
                         [s["id"] for s in self.store.get_plan("2026-11-02")["slots"]])

    def test_pinned_slots_between_filters(self):
        slot = self.slots[0]
        self.store.move_slot(slot["id"], "2026-11-04", "09:00", "10:00")
        got = self.store.pinned_slots_between("2026-11-03", "2026-11-09")
        self.assertEqual([s["id"] for s in got], [slot["id"]])
        self.assertEqual(self.store.pinned_slots_between("2026-11-01", "2026-11-02"),
                         [])

    def test_calendar_event_id_survives_a_move(self):
        slot = self.slots[0]
        self.store.set_slot_calendar_id(slot["id"], "UID-42")
        moved = self.store.move_slot(slot["id"], "2026-11-10", "09:00", "10:00")
        self.assertEqual(moved["calendar_event_id"], "UID-42")


class WithinWeeksAndMeetingTest(unittest.TestCase):
    """within_weeks (the task's planning window) + the next-meeting kv flag."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "p.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_within_weeks_roundtrip(self):
        tid = self.store.add_task("A", within_weeks=2)
        self.assertEqual(self.store.get_task(tid)["within_weeks"], 2)
        # no window given -> any week is fair game
        plain = self.store.add_task("B")
        self.assertIsNone(self.store.get_task(plain)["within_weeks"])
        # None clears the window; the field survives ordinary edits
        self.store.update_task(tid, within_weeks=None)
        self.assertIsNone(self.store.get_task(tid)["within_weeks"])
        self.store.update_task(tid, within_weeks=8, notes="roughly a quarter")
        row = self.store.get_task(tid)
        self.assertEqual(row["within_weeks"], 8)
        self.assertEqual(row["notes"], "roughly a quarter")

    def test_next_meeting_roundtrip_and_clear(self):
        info = self.store.set_next_meeting(
            {"date": "2026-11-10", "title": "PI meeting", "calendar": "Work",
             "id": "cal|x|DTSTART|DTEND"})
        self.assertEqual(info["date"], "2026-11-10")
        self.assertEqual(info["title"], "PI meeting")
        got = self.store.get_next_meeting()
        self.assertEqual(got["calendar"], "Work")
        self.assertIn("flagged_at", got)
        kinds = [e["kind"] for e in self.store.recent_events(10)]
        self.assertIn("meeting_set", kinds)
        self.store.set_next_meeting(None)
        self.assertIsNone(self.store.get_next_meeting())
        kinds = [e["kind"] for e in self.store.recent_events(10)]
        self.assertIn("meeting_cleared", kinds)

    def test_next_meeting_survives_corruption_and_reflags(self):
        self.store.set_next_meeting({"date": "2026-11-10", "title": "x"})
        self.store.kv_set("next_meeting", "{not json")
        self.assertIsNone(self.store.get_next_meeting())
        self.store.set_next_meeting({"date": "2026-11-11"})
        self.assertEqual(self.store.get_next_meeting()["date"], "2026-11-11")


if __name__ == "__main__":
    unittest.main()
