"""Tests for agentic scheduling: subtask-aligned blocks, deep-work fragments,
and re-planning that does not treat its own previous slots as busy."""
import json
import os
import tempfile
import unittest
from datetime import date

from planner.scheduler import schedule_week, task_chunks, week_start_for

MON = date(2026, 11, 2)
CFG = {
    "work_days": [0, 1, 2, 3, 4], "day_start": "09:00", "day_end": "19:00",
    "lunch": ["13:00", "14:00"], "buffer_min": 10, "max_block_min": 90,
    "max_day_min": 300, "deep_min_block": 45,
    "complexity_minutes": {"S": 45, "M": 90, "L": 180},
    "complexity_rank": {"S": 1, "M": 3, "L": 5},
}


def task(**kw):
    base = {"id": 1, "title": "t", "status": "todo", "priority": 2,
            "complexity": "M", "est_minutes": 90, "deadline": None,
            "subtasks": "[]", "deep_work": 0}
    base.update(kw)
    return base


class ChunkTest(unittest.TestCase):
    def test_chunks_follow_subtask_boundaries(self):
        t = task(est_minutes=150, subtasks=json.dumps([
            {"title": "read", "minutes": 30},
            {"title": "sample", "minutes": 45},
            {"title": "write", "minutes": 60},
            {"title": "commit", "minutes": 15}]))
        chunks = task_chunks(CFG, t, 150, 90)
        # greedy grouping up to max_block: 30+45=75, then 60+15=75
        self.assertEqual([c["minutes"] for c in chunks], [75, 75])
        self.assertEqual(sum(c["minutes"] for c in chunks), 150)
        self.assertIn("read", chunks[0]["label"])
        self.assertIn("sample", chunks[0]["label"])
        self.assertIn("commit", chunks[1]["label"])

    def test_chunks_clamp_to_estimate(self):
        t = task(est_minutes=60, subtasks=json.dumps([
            {"title": "a", "minutes": 50}, {"title": "b", "minutes": 50}]))
        chunks = task_chunks(CFG, t, 60, 90)
        self.assertEqual(sum(c["minutes"] for c in chunks), 60)

    def test_no_subtasks_falls_back_to_size_split(self):
        chunks = task_chunks(CFG, task(est_minutes=200, subtasks="[]"), 200, 90)
        self.assertEqual([c["minutes"] for c in chunks], [90, 90, 20])

    def test_broken_subtask_json_is_ignored(self):
        chunks = task_chunks(CFG, task(est_minutes=90, subtasks="{not json"), 90, 90)
        self.assertEqual([c["minutes"] for c in chunks], [90])


class PlacementTest(unittest.TestCase):
    def test_blocks_land_on_subtask_minutes(self):
        t = task(id=7, est_minutes=150, priority=1, complexity="L", deep_work=1,
                 subtasks=json.dumps([{"title": "read", "minutes": 30},
                                      {"title": "sample", "minutes": 45},
                                      {"title": "write", "minutes": 60},
                                      {"title": "commit", "minutes": 15}]))
        res = schedule_week([t], MON, {}, CFG)
        self.assertEqual(res["unscheduled"], [])
        self.assertEqual(sorted(s["minutes"] for s in res["slots"]), [75, 75])
        self.assertTrue(all(s.get("subtask") for s in res["slots"]))

    def test_deep_work_avoids_a_small_leftover_when_a_bigger_slot_exists(self):
        # Monday nearly full: one ~30-min window; the rest of the week is free.
        busy = {"2026-11-02": [(540, 720), (750, 1140)]}
        t = task(id=3, est_minutes=90, priority=1, complexity="L", deep_work=1)
        res = schedule_week([t], MON, busy, CFG)
        self.assertEqual(res["unscheduled"], [])
        first = res["slots"][0]
        self.assertNotEqual(first["date"], "2026-11-02")   # not the 30-min sliver
        self.assertGreaterEqual(first["minutes"], 45)

    def test_short_block_is_flagged_honestly_when_it_is_a_fallback(self):
        # Whole week tiny: only 30-minute windows each day -> fallback happens.
        busy = {d.isoformat(): [(540, 700), (740, 1140)] for d in
                [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4),
                 date(2026, 11, 5), date(2026, 11, 6)]}
        t = task(id=4, est_minutes=30, priority=1, complexity="L", deep_work=1)
        res = schedule_week([t], MON, busy, CFG)
        self.assertEqual(len(res["slots"]), 1)
        self.assertIn("no slot >= 45min left", res["slots"][0]["rationale"])

    def test_deep_work_rationale_matches_the_actual_slot(self):
        t = task(id=5, est_minutes=90, priority=1, complexity="L", deep_work=1)
        busy = {"2026-11-02": [(540, 780)]}      # Monday morning gone
        res = schedule_week([t], MON, busy, CFG)
        slot = res["slots"][0]
        if slot["date"] == "2026-11-02":
            self.assertIn("afternoon", slot["rationale"])
            self.assertNotIn("morning slot (deep work)", slot["rationale"])
        else:
            self.assertIn("morning slot (deep work)", slot["rationale"])


class PastDaysTest(unittest.TestCase):
    def test_current_week_never_plans_before_today(self):
        from datetime import date as _d
        today = _d.today()
        ws = week_start_for(today)
        t = task(id=9, est_minutes=90, priority=1)
        res = schedule_week([t], ws, {}, CFG)
        for s in res["slots"]:
            self.assertGreaterEqual(_d.fromisoformat(s["date"]), today)
        if today.weekday() not in CFG["work_days"]:
            # nothing may land before today, and today is not a work day:
            # the task is honestly unscheduled instead of back-dated
            self.assertEqual(res["unscheduled"][0]["task_id"], 9)
        else:
            self.assertEqual(res["unscheduled"], [])

    def test_future_week_uses_monday(self):
        from datetime import date as _d
        ws = week_start_for(_d.today(), offset_weeks=1)
        res = schedule_week([task(id=10, est_minutes=45)], ws, {}, CFG)
        self.assertEqual(res["slots"][0]["date"], ws.isoformat())


class NotBeforeNowTest(unittest.TestCase):
    """A free gap two hours ago is not a slot for today's work."""

    today = date.today()

    def at(self, hour, minute=0):
        from datetime import datetime as _dt
        return _dt.combine(self.today, _dt.min.time()).replace(hour=hour,
                                                               minute=minute)

    def setUp(self):
        if self.today.weekday() not in CFG["work_days"]:
            self.skipTest("the clamp only applies on a work day")

    def test_gap_from_this_morning_is_not_used(self):
        from planner.scheduler import parse_hhmm
        ws = week_start_for(self.today)
        res = schedule_week([task(id=21, est_minutes=60, priority=1)],
                            ws, {}, CFG, now=self.at(14))
        today_slots = [s for s in res["slots"] if s["date"] == self.today.isoformat()]
        self.assertTrue(today_slots)
        first = min(parse_hhmm(s["start"]) for s in today_slots)
        # 14:00 + 5 min margin, rounded up to the next 5-minute step
        self.assertGreaterEqual(first, 14 * 60 + 5)

    def test_margin_and_5min_rounding(self):
        from planner.scheduler import parse_hhmm
        ws = week_start_for(self.today)
        # 16:32 -> earliest is 16:37 -> rounds to 16:40
        res = schedule_week([task(id=22, est_minutes=30, priority=1)],
                            ws, {}, CFG, now=self.at(16, 32))
        today_slots = [s for s in res["slots"] if s["date"] == self.today.isoformat()]
        self.assertTrue(today_slots)
        self.assertGreaterEqual(min(parse_hhmm(s["start"]) for s in today_slots),
                                16 * 60 + 40)

    def test_clamped_today_slot_say_so(self):
        ws = week_start_for(self.today)
        res = schedule_week([task(id=23, est_minutes=60, priority=1)],
                            ws, {}, CFG, now=self.at(10, 10))
        today_slots = [s for s in res["slots"] if s["date"] == self.today.isoformat()]
        self.assertTrue(today_slots)
        self.assertIn("not before now", today_slots[0]["rationale"])

    def test_all_day_work_days_still_get_the_whole_window(self):
        from planner.scheduler import parse_hhmm
        # 00:05: everything from 09:00 on is still ahead of us
        ws = week_start_for(self.today)
        res = schedule_week([task(id=24, est_minutes=60, priority=1)],
                            ws, {}, CFG, now=self.at(0, 5))
        today_slots = [s for s in res["slots"] if s["date"] == self.today.isoformat()]
        self.assertTrue(today_slots)
        self.assertGreaterEqual(min(parse_hhmm(s["start"]) for s in today_slots),
                                9 * 60 - 1)

    def test_calendar_busy_that_ended_hours_ago_cannot_reuse_its_gap(self):
        from planner.scheduler import parse_hhmm
        ws = week_start_for(self.today)
        # morning meeting (09:00-11:00) already over at 14:00, morning free
        busy = {self.today.isoformat(): [(9 * 60, 11 * 60)]}
        res = schedule_week([task(id=25, est_minutes=60, priority=1)],
                            ws, busy, CFG, now=self.at(14))
        for s in res["slots"]:
            self.assertGreaterEqual(parse_hhmm(s["start"]), 14 * 60 + 5)


class WindowTest(unittest.TestCase):
    """within_weeks: schedule_week(weeks_out=N) skips tasks whose window
    ends before week N (0 = current week, 1 = next week, …)."""

    def test_current_week_places_window_of_one(self):
        t = task(id=31, est_minutes=60, within_weeks=1)
        res = schedule_week([t], MON, {}, CFG, weeks_out=0)
        self.assertEqual(len(res["slots"]), 1)
        self.assertEqual(res["unscheduled"], [])

    def test_next_week_refuses_window_of_one(self):
        t = task(id=32, est_minutes=60, within_weeks=1)
        res = schedule_week([t], MON, {}, CFG, weeks_out=1)
        self.assertEqual(res["slots"], [])
        self.assertEqual(res["stats"]["tasks"], 0)
        # being out of window is not an error: the task is simply carried,
        # so nothing is reported as unschedulable either
        self.assertEqual(res["unscheduled"], [])

    def test_window_of_two_fits_next_week_but_not_two_out(self):
        t = task(id=33, est_minutes=60, within_weeks=2)
        self.assertEqual(
            len(schedule_week([t], MON, {}, CFG, weeks_out=1)["slots"]), 1)
        self.assertEqual(schedule_week([t], MON, {}, CFG, weeks_out=2)["slots"], [])

    def test_unwindowed_task_reaches_any_week(self):
        t = task(id=34, est_minutes=60, within_weeks=None)
        for weeks_out in (0, 3):
            res = schedule_week([t], MON, {}, CFG, weeks_out=weeks_out)
            self.assertEqual(len(res["slots"]), 1, weeks_out)

    def test_window_is_ignored_when_weeks_out_is_unknown(self):
        t = task(id=35, est_minutes=60, within_weeks=1)
        self.assertEqual(len(schedule_week([t], MON, {}, CFG)["slots"]), 1)

    def test_windowed_and_open_tasks_coexist(self):
        soon = task(id=36, est_minutes=60, within_weeks=1, priority=1)
        later = task(id=37, est_minutes=60, within_weeks=4, priority=3)
        res = schedule_week([soon, later], MON, {}, CFG, weeks_out=0)
        self.assertEqual({s["task_id"] for s in res["slots"]}, {36, 37})
        # same pair, one week out: only the wide-window task shows up
        res1 = schedule_week([soon, later], MON, {}, CFG, weeks_out=1)
        self.assertEqual({s["task_id"] for s in res1["slots"]}, {37})


class BusyMapTest(unittest.TestCase):
    def test_replanning_ignores_the_plan_it_replaces(self):
        from planner import cli
        from planner.store import Store
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = Store(os.path.join(tmp.name, "p.db"))
        st.save_plan(MON.isoformat(),
                     [{"task_id": 1, "date": MON.isoformat(), "start": "09:00",
                       "end": "12:00", "minutes": 180, "rationale": ""}])
        with_plan = cli._busy_map(st, MON, CFG, use_calendar=False, include_plan=True)
        without = cli._busy_map(st, MON, CFG, use_calendar=False)
        self.assertEqual(len(with_plan[MON.isoformat()]), 1)
        self.assertEqual(without[MON.isoformat()], [])


class WeekStartTest(unittest.TestCase):
    def test_week_start_is_monday(self):
        self.assertEqual(week_start_for(date(2026, 10, 20)).weekday(), 0)


if __name__ == "__main__":
    unittest.main()
