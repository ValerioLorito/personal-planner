"""
test_habits.py — recurring commitments: templates, materialized instances,
flexible placement, the one-way Habits-calendar mirror, chat parsing, and the
web endpoints.

Calendar functions are always patched — the real ones would drive osascript;
the mirror's error paths are exercised with fakes that raise or return
failures, never against Calendar.app.
"""
import json
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import date, timedelta
from unittest import mock

from planner import auto, brain, calendar_mac, habits
from planner import config as config_mod
from planner import web
from planner.scheduler import week_start_for
from planner.store import Store


class HabitCase(unittest.TestCase):
    """Temp store + default config, calendar functions patched out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "p.db"))
        self.cfg = deepcopy(config_mod.DEFAULT_CONFIG)
        # the parse tests go through brain.llm_json, which demands an endpoint
        self.cfg["brain"]["llm"]["base_url"] = "https://example.invalid/v1"
        self.cfg["brain"]["llm"]["model"] = "test-model"
        self.cfg["brain"]["llm"]["api_key_env"] = ""   # keyless test endpoint

        self.written, self.updated, self.deleted = [], [], []
        self.write_result = "UID-%d"       # format string or None (failure)
        self.update_result = {"found": 1, "changed": 1}
        self.delete_result = {"found": 1, "deleted": 1}
        self.write_raises = None
        self.ensure_heals = True           # creating the calendar fixes writes

        def fake_write(summary, start, end, cal, description=""):
            if self.write_raises:
                raise self.write_raises
            if not self.write_result:
                return None
            self.written.append((summary, start, end, cal))
            return self.write_result % (len(self.written),)

        def fake_update(uid, start, end, cal):
            if self.update_result is None:
                raise RuntimeError("update failed")
            self.updated.append((uid, start, end, cal))
            return dict(self.update_result)

        def fake_delete(uid, cal):
            self.deleted.append((uid, cal))
            return dict(self.delete_result)

        # the mirror's self-heal path: ensure_calendar and the verification
        # read (week_events).  calendar_uids=None means "verification
        # unavailable" — the mirror then trusts its stamps like before.
        self.ensured = []
        self.ensure_result = True
        self.calendar_uids = None

        def fake_ensure(cal):
            self.ensured.append(cal)
            if self.ensure_heals and self.write_raises is not None:
                self.write_raises = None   # the calendar exists now
            return self.ensure_result

        p1 = mock.patch.object(calendar_mac, "write_event",
                               side_effect=fake_write)
        p2 = mock.patch.object(calendar_mac, "update_event_times",
                               side_effect=fake_update)
        p3 = mock.patch.object(calendar_mac, "delete_event",
                               side_effect=fake_delete)
        p4 = mock.patch.object(calendar_mac, "ensure_calendar",
                               side_effect=fake_ensure)
        p5 = mock.patch.object(calendar_mac, "week_events",
                               side_effect=lambda *a, **k: self.calendar_uids)
        for p in (p1, p2, p3, p4, p5):
            p.start()
            self.addCleanup(p.stop)

    def week(self, offset=0):
        return week_start_for() + timedelta(days=7 * offset)


class StoreHabitTest(HabitCase):
    """Template CRUD and instance materialization at the store layer."""

    def test_add_get_update_roundtrip(self):
        hid = self.store.add_habit("Gym", [0, 2, 4], 60, start="07:00",
                                   end="08:00")
        h = self.store.get_habit(hid)
        self.assertEqual((h["name"], h["weekdays"], h["minutes"],
                          h["start"], h["end"]), ("Gym", [0, 2, 4], 60,
                                                  "07:00", "08:00"))
        self.store.update_habit(hid, minutes=45, weekdays=[1])
        h = self.store.get_habit(hid)
        self.assertEqual((h["minutes"], h["weekdays"]), (45, [1]))

    def test_delete_removes_the_template_and_all_instances(self):
        hid = self.store.add_habit("Read", [0], 20)
        ws = self.week()
        self.store.set_habit_slots(hid, [
            {"date": ws.isoformat(), "start": "07:00", "end": "07:20",
             "minutes": 20}])
        slot = self.store.habit_slots_between(ws.isoformat(), ws.isoformat())[0]
        self.store.mark_habit_mirrored(slot["id"], "07:00|07:20", "UID-5")

        removed = self.store.delete_habit(hid)
        self.assertIsNone(self.store.get_habit(hid))
        self.assertEqual(self.store.habits(active_only=False), [])
        # the removed rows come back so the caller can drop their events
        self.assertEqual(removed[0]["calendar_event_id"], "UID-5")
        self.assertEqual(self.store.habit_slots_between("2000-01-01",
                                                        "2999-12-31"), [])

    def test_set_habit_slots_creates_and_reports_deltas(self):
        hid = self.store.add_habit("Gym", [0], 60, start="07:00", end="08:00")
        ws = self.week()
        spans = [{"date": (ws + timedelta(days=i)).isoformat(),
                  "start": "07:00", "end": "08:00", "minutes": 60}
                 for i in (0, 2, 4)]
        res = self.store.set_habit_slots(hid, spans)
        self.assertEqual((res["added"], res["moved"], res["removed"]),
                         (3, 0, []))

        # idempotent: same spans again -> all zeros
        res = self.store.set_habit_slots(hid, spans)
        self.assertEqual((res["added"], res["moved"], res["removed"]),
                         (0, 0, []))

    def test_done_and_skipped_survive_rematerialization(self):
        hid = self.store.add_habit("Gym", [0], 60, start="07:00", end="08:00")
        ws = self.week()
        span = {"date": ws.isoformat(), "start": "07:00", "end": "08:00",
                "minutes": 60}
        self.store.set_habit_slots(hid, [span])
        slot = self.store.habit_slots_between(ws.isoformat(), ws.isoformat())[0]
        self.store.set_habit_slot_state(slot["id"], done=True)

        span["start"], span["end"] = "07:30", "08:30"   # moved by re-plan
        self.store.set_habit_slots(hid, [span])
        slot = self.store.habit_slots_between(ws.isoformat(), ws.isoformat())[0]
        self.assertEqual((slot["done"], slot["start"], slot["end"]),
                         (1, "07:30", "08:30"))

    def test_moved_instance_resets_the_mirror_stamp_but_keeps_the_uid(self):
        hid = self.store.add_habit("Gym", [0], 60, start="07:00", end="08:00")
        ws = self.week()
        span = {"date": ws.isoformat(), "start": "07:00", "end": "08:00",
                "minutes": 60}
        self.store.set_habit_slots(hid, [span])
        slot = self.store.habit_slots_between(ws.isoformat(), ws.isoformat())[0]
        self.store.mark_habit_mirrored(slot["id"], "07:00|08:00", "UID-9")

        span["start"], span["end"] = "08:00", "09:00"
        self.store.set_habit_slots(hid, [span])
        slot = self.store.habit_slots_between(ws.isoformat(), ws.isoformat())[0]
        self.assertEqual(slot["calendar_event_id"], "UID-9")  # survives
        self.assertEqual(slot["mirrored_span"], "")           # must re-mirror

    def test_window_bounds_removal_past_weeks_are_untouched(self):
        hid = self.store.add_habit("Gym", [0], 60, start="07:00", end="08:00")
        ws = self.week()
        past = (ws - timedelta(days=7)).isoformat()
        this = ws.isoformat()
        self.store.set_habit_slots(hid, [
            {"date": past, "start": "07:00", "end": "08:00", "minutes": 60},
            {"date": this, "start": "07:00", "end": "08:00", "minutes": 60}],
            window=(past, past))
        # mark past done, then drop Monday from the template: only the
        # in-window row may be removed
        past_slot = self.store.habit_slots_between(past, past)[0]
        self.store.set_habit_slot_state(past_slot["id"], done=True)
        self.store.update_habit(hid, weekdays=[])          # nothing repeats
        gone = self.store.set_habit_slots(hid, [], window=(this, this))
        self.assertEqual([g["date"] for g in gone["removed"]], [this])
        kept = self.store.habit_slots_between(past, past)
        self.assertEqual(len(kept), 1)                     # history survives
        self.assertEqual(kept[0]["done"], 1)

    def test_state_marks_are_mutually_exclusive(self):
        hid = self.store.add_habit("Gym", [0], 60, start="07:00", end="08:00")
        ws = self.week()
        self.store.set_habit_slots(hid, [
            {"date": ws.isoformat(), "start": "07:00", "end": "08:00",
             "minutes": 60}])
        slot = self.store.habit_slots_between(ws.isoformat(), ws.isoformat())[0]
        self.store.set_habit_slot_state(slot["id"], done=True)
        self.store.set_habit_slot_state(slot["id"], skipped=True)
        row = self.store.habit_slot(slot["id"])
        self.assertEqual((row["done"], row["skipped"]), (0, 1))
        self.store.set_habit_slot_state(slot["id"], done=True)
        row = self.store.habit_slot(slot["id"])
        self.assertEqual((row["done"], row["skipped"]), (1, 0))


class WeekSpansTest(HabitCase):
    """Fixed windows land on the clock; flexible ones take the first gap."""

    def test_fixed_window_is_exact_and_fills_occupied(self):
        hid = self.store.add_habit("Gym", [0, 2], 60, start="07:00",
                                   end="08:00")
        ws = self.week()
        res = habits.week_spans([self.store.get_habit(hid)], ws, self.cfg)
        self.assertEqual(res["unplaced"], [])
        days = {s["date"] for s in res["spans"]}
        self.assertEqual(days, {(ws + timedelta(days=i)).isoformat()
                                for i in (0, 2)})
        s = res["spans"][0]
        self.assertEqual((s["start"], s["end"], s["minutes"]),
                         (420, 480, 60))

    def test_flexible_takes_the_first_gap_and_respects_lunch(self):
        # day 09:00-19:00, lunch 13:00-14:00: 20min "day" habit -> 09:00
        hid = self.store.add_habit("Stretch", [0], 20, prefer="day")
        ws = self.week()
        res = habits.week_spans([self.store.get_habit(hid)], ws, self.cfg)
        s = res["spans"][0]
        self.assertEqual((s["start"], s["end"]), (540, 560))

    def test_flexible_routes_around_occupied_spans(self):
        hid = self.store.add_habit("Stretch", [0], 60, prefer="day")
        ws = self.week()
        iso = ws.isoformat()
        # 09:00-10:30 occupied -> first free gap starts 10:30
        res = habits.week_spans([self.store.get_habit(hid)], ws, self.cfg,
                                occupied={iso: [(540, 630)]})
        self.assertEqual(res["spans"][0]["start"], 630)

    def test_morning_preference_starts_the_day_evening_takes_the_last_gap(self):
        hm = self.store.add_habit("Journal", [0], 30, prefer="morning")
        he = self.store.add_habit("Read", [1], 30, prefer="evening")
        ws = self.week()
        res = habits.week_spans([self.store.get_habit(hm),
                                 self.store.get_habit(he)], ws, self.cfg)
        by_habit = {s["habit_id"]: s for s in res["spans"]}
        self.assertEqual(by_habit[hm]["start"], 540)            # 09:00
        self.assertEqual(by_habit[he]["end"], 1140)             # 19:00

    def test_full_day_reports_unplaced_with_a_reason(self):
        hid = self.store.add_habit("Stretch", [0], 660, prefer="day")
        ws = self.week()
        iso = ws.isoformat()
        # the whole work day minus lunch is busy
        res = habits.week_spans([self.store.get_habit(hid)], ws, self.cfg,
                                occupied={iso: [(540, 1140)]})
        self.assertEqual(res["spans"], [])
        self.assertEqual(len(res["unplaced"]), 1)
        self.assertIn("no free gap", res["unplaced"][0]["reason"])

    def test_inactive_templates_are_skipped(self):
        hid = self.store.add_habit("Gym", [0], 60, start="07:00", end="08:00",
                                   active=0)
        ws = self.week()
        res = habits.week_spans([self.store.get_habit(hid)], ws, self.cfg)
        self.assertEqual(res["spans"], [])


class MirrorTest(HabitCase):
    """One-way mirror: create, update in place, stamp, never re-write."""

    def seed(self, done=0):
        hid = self.store.add_habit("Gym", [0, 1], 60, start="07:00",
                                   end="08:00")
        ws = self.week()
        self.store.set_habit_slots(hid, [
            {"date": (ws + timedelta(days=i)).isoformat(),
             "start": "07:00", "end": "08:00", "minutes": 60}
            for i in (0, 1)])
        return hid, ws

    def test_creates_events_in_the_habits_calendar_and_stamps(self):
        self.seed()
        rep = habits.mirror(self.store, self.cfg, self.week().isoformat(),
                            (self.week() + timedelta(days=1)).isoformat())
        self.assertEqual((rep["mirrored"], rep["updated"], rep["errors"]),
                         (2, 0, []))
        self.assertEqual(self.written[0][0], "Habit: Gym")
        self.assertEqual(self.written[0][3], "Habits")
        slot = self.store.habit_slots_between(self.week().isoformat(),
                                              self.week().isoformat())[0]
        self.assertEqual((slot["calendar_event_id"],
                          slot["mirrored_span"]), ("UID-1", "07:00|08:00"))

    def test_second_mirror_is_a_noop(self):
        self.seed()
        window = (self.week().isoformat(),
                  (self.week() + timedelta(days=1)).isoformat())
        habits.mirror(self.store, self.cfg, *window)
        n = len(self.written)
        rep = habits.mirror(self.store, self.cfg, *window)
        self.assertEqual((len(self.written) - n, rep["mirrored"],
                          rep["updated"]), (0, 0, 0))

    def test_time_change_updates_the_event_in_place(self):
        self.seed()
        window = (self.week().isoformat(),
                  (self.week() + timedelta(days=1)).isoformat())
        habits.mirror(self.store, self.cfg, *window)
        uid = self.store.habit_slots_between(*window)[0]["calendar_event_id"]
        # the instance moves (template edit): same uid, new times
        slot = self.store.habit_slots_between(*window)[0]
        self.store.set_habit_slots(slot["habit_id"], [
            {"date": slot["date"], "start": "08:00", "end": "09:00",
             "minutes": 60}], window=window)
        n = len(self.written)
        rep = habits.mirror(self.store, self.cfg, *window)
        self.assertEqual((rep["mirrored"], rep["updated"]), (0, 1))
        self.assertEqual(len(self.written) - n, 0)          # no new events
        self.assertEqual(self.updated[0][0], uid)
        self.assertIn("T08:00", self.updated[0][1])

    def test_update_failure_is_data_not_an_exception(self):
        self.seed()
        window = (self.week().isoformat(),
                  (self.week() + timedelta(days=1)).isoformat())
        habits.mirror(self.store, self.cfg, *window)
        slot = self.store.habit_slots_between(*window)[0]
        self.store.set_habit_slots(slot["habit_id"], [
            {"date": slot["date"], "start": "09:00", "end": "10:00",
             "minutes": 60}], window=window)
        self.update_result = None                          # raises inside
        rep = habits.mirror(self.store, self.cfg, *window)
        self.assertEqual(rep["updated"], 0)
        self.assertEqual(len(rep["errors"]), 1)            # only Mon moved
        # the stamp was not written: the next mirror retries
        slot = self.store.habit_slots_between(*window)[0]
        self.assertEqual(slot["mirrored_span"], "")

    def test_write_failure_is_reported_and_not_stamped(self):
        self.seed()
        self.write_result = None                           # no uid came back
        rep = habits.mirror(self.store, self.cfg, self.week().isoformat(),
                            self.week().isoformat())
        self.assertEqual(rep["mirrored"], 0)
        self.assertEqual(len(rep["errors"]), 1)
        slot = self.store.habit_slots_between(self.week().isoformat(),
                                              self.week().isoformat())[0]
        self.assertIsNone(slot["calendar_event_id"])

    def test_calendar_unavailable_becomes_data(self):
        self.seed()
        with mock.patch.object(calendar_mac, "write_event",
                               side_effect=RuntimeError("no permission")):
            rep = habits.mirror(self.store, self.cfg,
                                self.week().isoformat(),
                                self.week().isoformat())
        self.assertEqual(rep["mirrored"], 0)
        self.assertIn("no permission", rep["errors"][0])

    # -- self-healing: the mirror must converge after Calendar.app changes --
    def test_lost_events_are_recreated_when_the_calendar_is_verified(self):
        self.seed()
        window = (self.week().isoformat(),
                  (self.week() + timedelta(days=1)).isoformat())
        habits.mirror(self.store, self.cfg, *window)     # stamped + on cal
        n = len(self.written)
        # the user wiped the Habits calendar: verification finds nothing
        rep = habits.mirror(self.store, self.cfg, *window, verify_uids=set())
        self.assertEqual(rep["remirrored"], 2)
        self.assertEqual(len(self.written) - n, 2)       # re-created
        slot = self.store.habit_slots_between(*window)[0]
        self.assertEqual(slot["mirrored_span"], "07:00|08:00")  # re-stamped
        self.assertNotEqual(slot["calendar_event_id"], "UID-1") # fresh uid

    def test_verified_uids_keep_the_noop_fast_path(self):
        self.seed()
        window = (self.week().isoformat(),
                  (self.week() + timedelta(days=1)).isoformat())
        habits.mirror(self.store, self.cfg, *window)
        uids = {s["calendar_event_id"]
                for s in self.store.habit_slots_between(*window)}
        n = len(self.written)
        rep = habits.mirror(self.store, self.cfg, *window, verify_uids=uids)
        self.assertEqual((rep["mirrored"], rep["updated"], rep["remirrored"]),
                         (0, 0, 0))
        self.assertEqual(len(self.written) - n, 0)       # nothing re-written

    def test_update_not_found_recreates_the_event(self):
        self.seed()
        window = (self.week().isoformat(),
                  (self.week() + timedelta(days=1)).isoformat())
        habits.mirror(self.store, self.cfg, *window)
        slot = self.store.habit_slots_between(*window)[0]
        self.store.set_habit_slots(slot["habit_id"], [
            {"date": slot["date"], "start": "08:00", "end": "09:00",
             "minutes": 60}], window=window)
        self.update_result = {"found": 0, "changed": 0}  # the event is gone
        n = len(self.written)
        rep = habits.mirror(self.store, self.cfg, *window)
        self.assertEqual(rep["remirrored"], 1)
        self.assertEqual(len(self.written) - n, 1)       # re-created
        self.assertEqual(len(self.updated), 1)  # tried in place, then healed

    def test_missing_calendar_is_created_and_the_write_retried(self):
        self.seed()
        self.write_raises = calendar_mac.CalendarError(
            "no calendar named 'Habits'")
        rep = habits.mirror(self.store, self.cfg, self.week().isoformat(),
                            (self.week() + timedelta(days=1)).isoformat())
        self.assertEqual(rep["mirrored"], 2)             # healed, then written
        self.assertEqual(self.ensured, ["Habits"])       # calendar re-created


class MaterializeTest(HabitCase):
    """The cycle step: fill the rolling window, mirror, report true deltas."""

    def setUp(self):
        super().setUp()
        # run_cycle plans the real current week: placeable on any day the
        # suite runs, weekends included
        self.cfg["work_days"] = [0, 1, 2, 3, 4, 5, 6]

    def test_reports_additions_then_zeros_on_the_second_run(self):
        self.store.add_habit("Gym", [0, 2, 4], 60, start="07:00", end="08:00")
        rep1 = habits.materialize_and_mirror(self.store, self.cfg,
                                             weeks_ahead=1)
        self.assertEqual(rep1["materialized"], 6)
        self.assertGreaterEqual(rep1["added"], 6)
        self.assertEqual(rep1["mirror"]["mirrored"], 6)
        rep2 = habits.materialize_and_mirror(self.store, self.cfg,
                                             weeks_ahead=1)
        self.assertEqual((rep2["added"], rep2["moved"],
                          rep2["mirror"]["mirrored"]), (0, 0, 0))
        self.assertEqual(rep2["materialized"], 6)   # still filled, just no delta

    def test_no_habits_means_a_quiet_report(self):
        rep = habits.materialize_and_mirror(self.store, self.cfg,
                                            weeks_ahead=1)
        self.assertEqual((rep["materialized"], rep["mirror"], rep["error"]),
                         (0, None, None))

    def test_weekday_drop_removes_instances_and_their_events(self):
        hid = self.store.add_habit("Gym", [0], 60, start="07:00", end="08:00")
        rep = habits.materialize_and_mirror(self.store, self.cfg,
                                            weeks_ahead=1)
        self.assertEqual(rep["materialized"], 2)    # this week + next Monday
        self.store.update_habit(hid, weekdays=[6])  # now Sunday only
        rep = habits.materialize_and_mirror(self.store, self.cfg,
                                            weeks_ahead=1)
        self.assertEqual(rep["removed_events"], 2)
        self.assertEqual(len(self.deleted), 2)

    def test_deactivated_template_sweeps_even_without_active_habits(self):
        # the old early return stranded the LAST deleted habit's instances
        # in the week view forever (nothing active left -> no sweep ran)
        hid = self.store.add_habit("Gym", [0], 60, start="07:00", end="08:00")
        rep = habits.materialize_and_mirror(self.store, self.cfg,
                                            weeks_ahead=1)
        self.assertGreaterEqual(rep["materialized"], 2)
        self.store.update_habit(hid, active=0)   # legacy soft-deleted state
        n = len(self.deleted)
        rep = habits.materialize_and_mirror(self.store, self.cfg,
                                            weeks_ahead=1)
        self.assertEqual(len(self.deleted) - n, 2)  # instances + events go
        self.assertGreaterEqual(rep["removed_events"], 2)
        from planner.scheduler import week_start_for
        ws = week_start_for()
        self.assertEqual(self.store.habit_slots_between(
            ws.isoformat(), (ws + timedelta(days=13)).isoformat()), [])

    def test_busy_map_unions_habit_spans(self):
        hid = self.store.add_habit("Gym", [0], 60, start="07:00", end="08:00")
        ws = week_start_for()
        self.store.set_habit_slots(hid, [
            {"date": ws.isoformat(), "start": "07:00", "end": "08:00",
             "minutes": 60}])
        busy = auto.busy_map(self.store, ws, self.cfg, use_calendar=False)
        self.assertIn((420, 480), busy[ws.isoformat()])

    def test_run_cycle_replans_on_new_habits_and_stays_quiet_after(self):
        self.store.add_task("Write section", est_minutes=60)
        res1 = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        self.assertTrue(res1["replanned"], res1)
        self.assertIsNotNone(res1["habits"])
        self.assertGreaterEqual(res1["habits"]["added"], 0)

        res2 = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        if res2.get("habits"):
            self.assertEqual((res2["habits"]["added"],
                              res2["habits"]["moved"],
                              res2["habits"]["removed_events"]), (0, 0, 0))
        # the second cycle found nothing to do: no forced re-plan
        self.assertFalse(res2["replanned"])

    def test_habits_disabled_skips_the_step(self):
        self.cfg["habits"]["enabled"] = False
        res = auto.run_cycle(self.store, self.cfg, use_calendar=False)
        self.assertIsNone(res["habits"])


class ParseProposalsTest(HabitCase):
    """One LLM call, strictly cleaned output, errors as data."""

    def answer(self, habits_list):
        return {"choices": [{"message": {
            "content": json.dumps({"habits": habits_list})}}]}

    def test_text_becomes_cleaned_proposals(self):
        raw = [{"name": "Gym", "weekdays": [0, 2, 4], "minutes": 60,
                "start": "07:00", "end": "08:00", "prefer": "day"},
               {"name": "Spanish drill", "weekdays": [0, 1, 2, 3, 4],
                "minutes": 20, "start": "", "end": "", "prefer": "evening"}]
        with mock.patch.object(brain, "_http_post_json",
                               return_value=self.answer(raw)):
            res = habits.parse_proposals("gym mon wed fri 7-8, spanish "
                                         "20min every evening", self.cfg)
        self.assertIsNone(res["error"])
        self.assertEqual(res["proposals"][0]["start"], "07:00")
        self.assertEqual(res["proposals"][1]["prefer"], "evening")
        self.assertEqual(res["proposals"][1]["start"], "")

    def test_dirty_fields_are_cleaned(self):
        raw = [{"name": "  ", "weekdays": [], "minutes": 60},        # no name
               {"name": "ok", "weekdays": [0, 9, "x", 6, 6],        # junk days
                "minutes": "30", "start": "07:00"},                 # half window
               {"name": "late", "weekdays": [1], "minutes": 5,
                "start": "22:00", "end": "22:20", "prefer": "weird"}]
        with mock.patch.object(brain, "_http_post_json",
                               return_value=self.answer(raw)):
            res = habits.parse_proposals("anything", self.cfg)
        ps = res["proposals"]
        self.assertEqual(len(ps), 2)               # the nameless one is gone
        self.assertEqual(ps[0]["weekdays"], [0, 6])
        self.assertEqual(ps[0]["start"], "")       # half window dropped
        self.assertEqual(ps[0]["minutes"], 30)
        self.assertEqual(ps[1]["prefer"], "day")   # normalized

    def test_llm_failure_is_data_not_an_exception(self):
        with mock.patch.object(brain, "_http_post_json",
                               side_effect=brain.BrainError("HTTP 429")):
            res = habits.parse_proposals("gym daily", self.cfg)
        self.assertEqual(res["proposals"], [])
        self.assertIn("429", res["error"])

    def test_empty_text_is_rejected_before_any_call(self):
        res = habits.parse_proposals("   ", self.cfg)
        self.assertEqual(res["proposals"], [])
        self.assertIn("say what the habit should be", res["error"])


# ---------------------------------------------------------------- web layer
class HabitsApiTest(unittest.TestCase):
    """The habits endpoints against the real HTTP server, calendar faked."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.store = Store(os.path.join(cls.tmpdir.name, "p.db"))
        cls.cfg = deepcopy(config_mod.DEFAULT_CONFIG)
        # /api/habits/parse goes through brain.llm_json: give it an endpoint
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
        self.written, self.deleted = [], []

        def fake_write(summary, start, end, cal, description=""):
            self.written.append((summary, start, cal))
            return "UID-%d" % len(self.written)

        patchers = [
            mock.patch.object(calendar_mac, "write_event",
                              side_effect=fake_write),
            mock.patch.object(calendar_mac, "update_event_times",
                              side_effect=lambda u, a, b, c:
                                  {"found": 1, "changed": 1}),
            mock.patch.object(calendar_mac, "delete_event",
                              side_effect=lambda u, c:
                                  (self.deleted.append((u, c)),
                                   {"found": 1, "deleted": 1})[1]),
            # None = verification unavailable: the mirror trusts its stamps
            mock.patch.object(calendar_mac, "week_events",
                              side_effect=lambda *a, **k: None),
        ]
        for p in patchers:
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
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, (json.loads(raw) if raw else None)

    def test_010_add_template_materializes_the_week_at_once(self):
        status, res = self.request("/api/habits", {
            "name": "Gym", "weekdays": [0, 2], "start": "07:00",
            "end": "08:00", "minutes": 60})
        self.assertEqual(status, 200)
        self.assertEqual(res["habit"]["name"], "Gym")
        status, res = self.request("/api/habits")
        self.assertEqual(status, 200)
        self.assertEqual(len(res["days"]), 7)
        placed = [s for d in res["days"] for s in d["slots"]]
        self.assertEqual(len(placed), 2)          # Mon + Wed of the shown week
        self.assertTrue(self.written)             # mirrored immediately
        self.assertEqual(self.written[0][2], "Habits")

    def test_020_validation_rejects_bad_templates(self):
        for body in ({"name": " ", "weekdays": [0], "minutes": 20},
                     {"name": "ok", "weekdays": [9], "minutes": 20},
                     {"name": "ok", "weekdays": "daily", "minutes": 20},
                     {"name": "ok", "weekdays": [0], "start": "07:00",
                      "minutes": 20}):            # half window
            status, _ = self.request("/api/habits", body)
            self.assertEqual(status, 400)

    def test_030_chat_parse_then_review_then_save(self):
        answer = {"habits": [{"name": "Gym", "weekdays": [0, 2, 4],
                              "minutes": 60, "start": "07:00", "end": "08:00",
                              "prefer": "day"}]}
        with mock.patch.object(brain, "_http_post_json",
                               return_value={"choices": [{"message": {
                                   "content": json.dumps(answer)}}]}):
            status, res = self.request("/api/habits/parse",
                                       {"text": "gym mon wed fri 7-8"})
        self.assertEqual(status, 200)
        self.assertEqual(len(res["proposals"]), 1)

        status, res = self.request("/api/habits/save",
                                   {"habits": res["proposals"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(res["saved"]), 1)

    def test_040_parse_failure_is_data_not_a_crash(self):
        with mock.patch.object(brain, "_http_post_json",
                               side_effect=brain.BrainError("HTTP 429")):
            status, res = self.request("/api/habits/parse",
                                       {"text": "gym daily"})
        self.assertEqual(status, 200)
        self.assertIn("429", res["error"])
        self.assertEqual(res["proposals"], [])

    def test_050_mark_done_and_skip(self):
        status, res = self.request("/api/habits", {
            "name": "Read", "weekdays": [0], "minutes": 20})
        hid = res["habit"]["id"]
        status, week = self.request("/api/habits")
        slot = next(s for d in week["days"] for s in d["slots"])
        status, res = self.request("/api/habits/slots/%s" % slot["id"],
                                   {"done": True})
        self.assertEqual((status, res["slot"]["done"]), (200, 1))
        status, res = self.request("/api/habits/slots/%s" % slot["id"],
                                   {"skipped": True})
        self.assertEqual((res["slot"]["done"], res["slot"]["skipped"]),
                         (0, 1))
        status, _ = self.request("/api/habits/slots/%s" % slot["id"], {})
        self.assertEqual(status, 400)
        status, _ = self.request("/api/habits/slots/99999", {"done": True})
        self.assertEqual(status, 404)
        _ = hid

    def test_060_delete_removes_template_instances_and_events(self):
        status, res = self.request("/api/habits", {
            "name": "Walk", "weekdays": [1], "minutes": 30})
        hid = res["habit"]["id"]
        status, res = self.request("/api/habits/%s" % hid, method="DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(res["deleted"], hid)
        self.assertEqual(res["removed_events"], 2)   # Tue of both weeks
        status, week = self.request("/api/habits")
        # the template is really gone — not soft-deleted, not greyed out
        self.assertEqual([h for h in week["habits"] if h["id"] == hid], [])
        # its instances left the week view
        self.assertEqual([s for d in week["days"] for s in d["slots"]
                          if s["habit_id"] == hid], [])

    def test_065_sync_rematerials_and_reports(self):
        status, _ = self.request("/api/habits", {
            "name": "Errand", "weekdays": [2], "minutes": 30})
        status, res = self.request("/api/habits/sync", method="POST")
        self.assertEqual(status, 200)
        self.assertIn("materialize", res)
        self.assertEqual(res["materialize"]["error"], None)

    def test_070_unknown_habit_404s(self):
        status, _ = self.request("/api/habits/4242", method="DELETE")
        self.assertEqual(status, 404)
        status, _ = self.request("/api/habits/4242", {"minutes": 20})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
