"""
Tests for planner.calendar_mac.

Rules of engagement: NOTHING here may start a real process.  Every code path
that would shell out takes an injected `runner`; a FakeRunner replays canned
osascript output instead.  One test goes further and makes the real
subprocess.run explode, proving the injection is honoured.
"""
import json
import os
import subprocess
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from planner import calendar_mac
from planner.calendar_mac import CalendarError

WEEK_START = date(2026, 10, 26)   # Monday
WEEK_END = date(2026, 11, 1)      # Sunday


# --------------------------------------------------------------------- helpers
class FakeRunner:
    """Stands in for subprocess.run: records the call, returns canned output."""

    def __init__(self, stdout="", stderr="", returncode=0, raises=None):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(list(argv), self.returncode,
                                           self.stdout, self.stderr)

    @property
    def script(self):
        return self.calls[0][0][4]


def unfold(text):
    """Undo RFC 5545 line folding so assertions can target logical lines."""
    return text.replace("\r\n ", "")


def logical_lines(text):
    return unfold(text).split("\r\n")


# -------------------------------------------------------------- the process layer
class TestRunJs(unittest.TestCase):
    def test_passes_expected_argv_and_returns_stdout(self):
        runner = FakeRunner(stdout="hello\n")
        self.assertEqual(calendar_mac._run_js("1 + 1", runner=runner), "hello")

        argv, kwargs = runner.calls[0]
        self.assertEqual(argv[:4], ["osascript", "-l", "JavaScript", "-e"])
        self.assertEqual(argv[4], "1 + 1")
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["timeout"], 20)

    def test_custom_timeout_is_forwarded(self):
        runner = FakeRunner(stdout="")
        calendar_mac._run_js("1", timeout=5, runner=runner)
        self.assertEqual(runner.calls[0][1]["timeout"], 5)

    def test_nonzero_exit_raises_actionable_error(self):
        runner = FakeRunner(returncode=1,
                            stderr="execution error: Not authorized. (-1743)")
        with self.assertRaises(CalendarError) as ctx:
            calendar_mac._run_js("Application('Calendar')", runner=runner)
        message = str(ctx.exception)
        self.assertIn("System Settings > Privacy & Security > Automation", message)
        self.assertIn("plan export --ics", message)
        self.assertIn("-1743", message)  # the raw OS detail is preserved

    def test_timeout_raises_calendar_error(self):
        runner = FakeRunner(raises=subprocess.TimeoutExpired("osascript", 20))
        with self.assertRaises(CalendarError) as ctx:
            calendar_mac._run_js("1", runner=runner)
        self.assertIn("timed out", str(ctx.exception))
        self.assertIn("plan export --ics", str(ctx.exception))

    def test_missing_osascript_raises_calendar_error(self):
        runner = FakeRunner(raises=FileNotFoundError("osascript"))
        with self.assertRaises(CalendarError) as ctx:
            calendar_mac._run_js("1", runner=runner)
        self.assertIn("osascript", str(ctx.exception))
        self.assertIn("Automation", str(ctx.exception))

    def test_empty_stdout_is_empty_string(self):
        self.assertEqual(calendar_mac._run_js("1", runner=FakeRunner(stdout="")), "")

    def test_calendar_error_is_a_runtime_error(self):
        self.assertTrue(issubclass(CalendarError, RuntimeError))


# -------------------------------------------------------------------- read_busy
BUSY_PAYLOAD = {
    "calendars": ["Thesis", "Home"],
    "events": [
        {"calendar": "Thesis", "title": "PI meeting",
         "start": "2026-10-27T10:00", "end": "2026-10-27T11:00"},
        {"calendar": "Home", "title": "Dentist",
         "start": "2026-10-27T15:00", "end": "2026-10-27T16:00"},
        {"calendar": "Thesis", "title": "Next month",
         "start": "2026-11-10T10:00", "end": "2026-11-10T11:00"},
    ],
}


class TestReadBusy(unittest.TestCase):
    def test_filters_to_the_named_calendar(self):
        runner = FakeRunner(stdout=json.dumps(BUSY_PAYLOAD))
        events = calendar_mac.read_busy(WEEK_START, WEEK_END, "Thesis",
                                        runner=runner)
        self.assertEqual(events, [{
            "title": "PI meeting",
            "start": "2026-10-27T10:00",
            "end": "2026-10-27T11:00",
        }])

    def test_calendar_match_is_case_insensitive(self):
        runner = FakeRunner(stdout=json.dumps(BUSY_PAYLOAD))
        events = calendar_mac.read_busy(WEEK_START, WEEK_END, "  thesis ",
                                        runner=runner)
        self.assertEqual([e["title"] for e in events], ["PI meeting"])

    def test_unknown_calendar_falls_back_to_all_calendars(self):
        runner = FakeRunner(stdout=json.dumps(BUSY_PAYLOAD))
        events = calendar_mac.read_busy(WEEK_START, WEEK_END, "Nonexistent",
                                        runner=runner)
        self.assertEqual([e["title"] for e in events], ["PI meeting", "Dentist"])
        self.assertEqual(events, sorted(events, key=lambda e: e["start"]))

    def test_events_outside_the_week_are_dropped(self):
        runner = FakeRunner(stdout=json.dumps(BUSY_PAYLOAD))
        events = calendar_mac.read_busy(WEEK_START, WEEK_END, "Thesis",
                                        runner=runner)
        self.assertNotIn("Next month", [e["title"] for e in events])

    def test_no_calendars_returns_empty_list(self):
        runner = FakeRunner(stdout=json.dumps({"calendars": [], "events": []}))
        self.assertEqual(
            calendar_mac.read_busy(WEEK_START, WEEK_END, "Thesis", runner=runner),
            [])

    def test_empty_stdout_returns_empty_list(self):
        self.assertEqual(
            calendar_mac.read_busy(WEEK_START, WEEK_END, "Thesis",
                                   runner=FakeRunner(stdout="")),
            [])

    def test_payload_without_events_returns_empty_list(self):
        runner = FakeRunner(stdout=json.dumps({"calendars": ["Thesis"]}))
        self.assertEqual(
            calendar_mac.read_busy(WEEK_START, WEEK_END, "Thesis", runner=runner),
            [])

    def test_skips_malformed_and_duplicate_events(self):
        payload = {
            "calendars": ["Thesis", "Home"],
            "events": [
                {"calendar": "Thesis", "title": "Standup",
                 "start": "2026-10-27T09:00", "end": "2026-10-27T09:15"},
                {"calendar": "Home", "title": "Standup",
                 "start": "2026-10-27T09:00", "end": "2026-10-27T09:15"},
                {"calendar": "Thesis", "title": "Broken",
                 "start": "", "end": "2026-10-27T09:15"},
                "not-an-event",
            ],
        }
        runner = FakeRunner(stdout=json.dumps(payload))
        events = calendar_mac.read_busy(WEEK_START, WEEK_END, "Nonexistent",
                                        runner=runner)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Standup")

    def test_script_is_jxa_over_the_requested_week(self):
        runner = FakeRunner(stdout=json.dumps({"calendars": [], "events": []}))
        calendar_mac.read_busy(WEEK_START, WEEK_END, "Thesis", runner=runner)
        script = runner.script
        self.assertIn("Application('Calendar')", script)
        self.assertIn("new Date(2026, 9, 26, 0, 0, 0)", script)   # Oct 26
        self.assertIn("new Date(2026, 10, 1, 23, 59, 59)", script)  # Nov 1
        self.assertIn("startDate", script)
        self.assertTrue(script.rstrip().endswith("JSON.stringify(result);"))

    def test_warning_lines_before_the_json_are_tolerated(self):
        runner = FakeRunner(
            stdout="warning: something odd\n" + json.dumps({"calendars": [],
                                                            "events": []}))
        self.assertEqual(
            calendar_mac.read_busy(WEEK_START, WEEK_END, "Thesis", runner=runner),
            [])

    def test_inverted_week_raises(self):
        with self.assertRaises(ValueError):
            calendar_mac.read_busy(WEEK_END, WEEK_START, "Thesis",
                                   runner=FakeRunner(stdout="{}"))


# ------------------------------------------------------------------ write_event
class TestWriteEvent(unittest.TestCase):
    def test_returns_the_event_uid(self):
        runner = FakeRunner(stdout=json.dumps({"uid": "ABC-123"}))
        uid = calendar_mac.write_event(
            "Chapter 2 draft", "2026-10-28T10:00", "2026-10-28T12:00",
            "Thesis", description="deep work", runner=runner)
        self.assertEqual(uid, "ABC-123")

        script = runner.script
        self.assertIn('var wanted = "Thesis";', script)
        self.assertIn('summary: "Chapter 2 draft"', script)
        self.assertIn('description: "deep work"', script)
        self.assertIn("new Date(2026, 9, 28, 10, 0, 0)", script)
        self.assertIn("new Date(2026, 9, 28, 12, 0, 0)", script)
        self.assertIn("target.events.push", script)
        self.assertIn("Application('Calendar')", script)

    def test_missing_calendar_raises_a_clear_error(self):
        runner = FakeRunner(stdout=json.dumps({
            "error": "CALENDAR_NOT_FOUND",
            "calendar": "Nope",
            "calendars": ["Home", "Work"],
        }))
        with self.assertRaises(CalendarError) as ctx:
            calendar_mac.write_event("x", "2026-10-28T10:00",
                                     "2026-10-28T11:00", "Nope", runner=runner)
        message = str(ctx.exception)
        self.assertIn("no calendar named 'Nope'", message)
        self.assertIn("Home, Work", message)
        self.assertIn("File > New Calendar", message)

    def test_calendar_is_never_created_silently(self):
        runner = FakeRunner(stdout=json.dumps({
            "error": "CALENDAR_NOT_FOUND", "calendars": []}))
        with self.assertRaises(CalendarError):
            calendar_mac.write_event("x", "2026-10-28T10:00",
                                     "2026-10-28T11:00", "Ghost", runner=runner)
        self.assertIn("target.events.push", runner.script)  # script has no create path
        self.assertNotIn("app.Calendar(", runner.script)

    def test_empty_uid_returns_none(self):
        runner = FakeRunner(stdout=json.dumps({"uid": ""}))
        self.assertIsNone(calendar_mac.write_event(
            "x", "2026-10-28T10:00", "2026-10-28T11:00", "Thesis",
            runner=runner))

    def test_bare_string_output_is_treated_as_the_uid(self):
        runner = FakeRunner(stdout='"XYZ-9"')
        self.assertEqual(calendar_mac.write_event(
            "x", "2026-10-28T10:00", "2026-10-28T11:00", "Thesis",
            runner=runner), "XYZ-9")

    def test_permission_failure_raises_with_hint(self):
        runner = FakeRunner(returncode=1,
                            stderr="execution error: Not authorized. (-1743)")
        with self.assertRaises(CalendarError) as ctx:
            calendar_mac.write_event("x", "2026-10-28T10:00",
                                     "2026-10-28T11:00", "Thesis", runner=runner)
        self.assertIn("System Settings > Privacy & Security > Automation",
                      str(ctx.exception))
        self.assertIn("plan export --ics", str(ctx.exception))

    def test_end_before_start_is_rejected_before_any_call(self):
        runner = FakeRunner(stdout=json.dumps({"uid": "nope"}))
        with self.assertRaises(CalendarError) as ctx:
            calendar_mac.write_event("x", "2026-10-28T12:00",
                                     "2026-10-28T10:00", "Thesis", runner=runner)
        self.assertIn("before start", str(ctx.exception))
        self.assertEqual(runner.calls, [])

    def test_blank_calendar_name_is_rejected(self):
        with self.assertRaises(CalendarError):
            calendar_mac.write_event("x", "2026-10-28T10:00",
                                     "2026-10-28T11:00", "  ",
                                     runner=FakeRunner(stdout="{}"))


# ------------------------------------------------------------------- export_ics
TASKS = {
    7: {"id": 7, "title": "Draft intro, part 1; and notes"},
    9: {"id": 9, "title": "Read papers"},
}
SLOTS = [
    {"task_id": 7, "date": "2026-10-27", "start": "09:00", "end": "10:30",
     "minutes": 90, "rationale": "Mon 09:00-10:30; priority P1"},
    {"task_id": 9, "date": "2026-10-28", "start": "14:00", "end": "15:00",
     "minutes": 60, "rationale": "afternoon\nsecond line"},
]


class TestExportIcs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.path = os.path.join(self.tmp, "plan.ics")
        self.stamp = datetime(2026, 10, 26, 12, 0, 0)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, slots=None, tasks=None, **kwargs):
        calendar_mac.export_ics(slots if slots is not None else SLOTS,
                                TASKS if tasks is None else tasks,
                                self.path, stamp=self.stamp, **kwargs)
        with open(self.path, encoding="utf-8", newline="") as handle:
            return handle.read()

    def test_one_vevent_per_slot(self):
        text = self._write()
        self.assertEqual(logical_lines(text).count("BEGIN:VEVENT"), 2)
        self.assertEqual(text.count("BEGIN:VEVENT"), 2)
        self.assertEqual(text.count("END:VEVENT"), 2)

    def test_calendar_envelope(self):
        text = self._write()
        lines = logical_lines(text)
        self.assertEqual(lines[0], "BEGIN:VCALENDAR")
        self.assertIn("VERSION:2.0", lines)
        self.assertIn("X-WR-CALNAME:Thesis", lines)
        self.assertEqual(lines[-2], "END:VCALENDAR")
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))

    def test_uids_are_derived_from_date_task_and_start(self):
        text = self._write()
        self.assertIn("UID:planner-2026-10-27-7-09:00", logical_lines(text))
        self.assertIn("UID:planner-2026-10-28-9-14:00", logical_lines(text))
        uids = [line for line in logical_lines(text) if line.startswith("UID:")]
        self.assertEqual(len(uids), len(set(uids)))

    def test_dtstart_dtend_are_local_and_correct(self):
        lines = logical_lines(self._write())
        self.assertIn("DTSTART:20261027T090000", lines)
        self.assertIn("DTEND:20261027T103000", lines)
        self.assertIn("DTSTART:20261028T140000", lines)
        self.assertIn("DTEND:20261028T150000", lines)
        self.assertIn("DTSTAMP:20261026T120000Z", lines)
        self.assertNotIn("TZID", " ".join(lines))  # floating by default

    def test_summary_and_description_content(self):
        lines = logical_lines(self._write())
        self.assertIn(r"SUMMARY:Thesis: Draft intro\, part 1\; and notes", lines)
        self.assertIn(r"DESCRIPTION:Mon 09:00-10:30\; priority P1\nTask #7",
                      lines)
        self.assertIn(r"DESCRIPTION:afternoon\nsecond line\nTask #9", lines)

    def test_escapes_commas_semicolons_newlines_and_backslashes(self):
        text = self._write(
            slots=[{"task_id": 1, "date": "2026-10-27", "start": "08:00",
                    "end": "09:00", "minutes": 60, "rationale": "a, b; c"}],
            tasks={1: {"title": "A, B; C\nD \\ E"}})
        lines = logical_lines(text)
        self.assertIn(r"SUMMARY:Thesis: A\, B\; C\nD \\ E", lines)
        self.assertIn(r"DESCRIPTION:a\, b\; c\nTask #1", lines)
        self.assertNotIn("\n", text.replace("\r\n", ""))  # CRLF only

    def test_lines_are_folded_to_75_octets(self):
        long_title = ("Deep work " * 40).strip()
        text = self._write(
            slots=[{"task_id": 3, "date": "2026-10-27", "start": "09:00",
                    "end": "10:00", "minutes": 60, "rationale": "r" * 200}],
            tasks={3: {"title": long_title}})
        physical = text.split("\r\n")
        for line in physical:
            self.assertLessEqual(len(line.encode("utf-8")), 75,
                                 "unfolded line too long: %r" % line[:90])
        self.assertIn("SUMMARY:Thesis: %s" % long_title, logical_lines(text))

    def test_missing_task_falls_back_to_the_id(self):
        text = self._write(
            slots=[{"task_id": 42, "date": "2026-10-27", "start": "09:00",
                    "end": "10:00", "minutes": 60, "rationale": ""}],
            tasks={})
        self.assertIn("SUMMARY:Thesis: Task #42", logical_lines(text))
        self.assertIn("DESCRIPTION:Task #42", logical_lines(text))

    def test_creates_the_parent_directory(self):
        nested = os.path.join(self.tmp, "sub", "dir", "plan.ics")
        calendar_mac.export_ics(SLOTS, TASKS, nested, stamp=self.stamp)
        self.assertTrue(os.path.isfile(nested))

    def test_returns_the_written_path(self):
        result = calendar_mac.export_ics(SLOTS, TASKS, self.path, stamp=self.stamp)
        self.assertEqual(result, self.path)

    def test_slot_crossing_midnight_ends_next_day(self):
        text = self._write(
            slots=[{"task_id": 5, "date": "2026-10-28", "start": "23:30",
                    "end": "00:30", "minutes": 60, "rationale": "late"}],
            tasks={5: {"title": "Push"}})
        lines = logical_lines(text)
        self.assertIn("DTSTART:20261028T233000", lines)
        self.assertIn("DTEND:20261029T003000", lines)

    def test_tzid_is_optional(self):
        text = self._write(tzid="Europe/Berlin")
        lines = logical_lines(text)
        self.assertIn("DTSTART;TZID=Europe/Berlin:20261027T090000", lines)

    def test_tasks_keyed_by_string_still_resolve(self):
        text = self._write(tasks={"7": {"title": "String keyed"}})
        self.assertIn("SUMMARY:Thesis: String keyed", logical_lines(text))

    def test_empty_slot_list_still_writes_a_valid_calendar(self):
        text = self._write(slots=[], tasks={})
        self.assertEqual(text.count("BEGIN:VEVENT"), 0)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))

    def test_malformed_slot_raises(self):
        with self.assertRaises(ValueError):
            self._write(slots=[{"task_id": 1, "date": "2026-10-27",
                                "start": "09:00", "minutes": 60}], tasks={})


# --------------------------------------------------------------- busy_to_minutes
class TestBusyToMinutes(unittest.TestCase):
    def test_merges_overlapping_intervals(self):
        events = [
            {"title": "A", "start": "2026-10-27T09:00", "end": "2026-10-27T10:30"},
            {"title": "B", "start": "2026-10-27T10:00", "end": "2026-10-27T11:00"},
            {"title": "C", "start": "2026-10-27T13:00", "end": "2026-10-27T13:45"},
        ]
        self.assertEqual(calendar_mac.busy_to_minutes(events, "2026-10-27"),
                         [(540, 660), (780, 825)])

    def test_clips_multiday_events_to_the_day(self):
        events = [
            {"title": "Trip", "start": "2026-10-26T22:00",
             "end": "2026-10-27T08:30"},
            {"title": "Late", "start": "2026-10-27T23:00",
             "end": "2026-10-28T01:00"},
        ]
        self.assertEqual(calendar_mac.busy_to_minutes(events, "2026-10-27"),
                         [(0, 510), (1380, 1440)])

    def test_event_ending_at_midnight_does_not_block_the_next_day(self):
        events = [{"title": "Evening", "start": "2026-10-27T22:00",
                   "end": "2026-10-28T00:00"}]
        self.assertEqual(calendar_mac.busy_to_minutes(events, "2026-10-27"),
                         [(1320, 1440)])
        self.assertEqual(calendar_mac.busy_to_minutes(events, "2026-10-28"), [])

    def test_ignores_other_days_and_malformed_events(self):
        events = [
            {"title": "Other", "start": "2026-10-29T09:00",
             "end": "2026-10-29T10:00"},
            {"title": "No end", "start": "2026-10-27T09:00"},
            {"title": "Bad", "start": "nope", "end": "2026-10-27T10:00"},
            "not-a-dict",
        ]
        self.assertEqual(calendar_mac.busy_to_minutes(events, "2026-10-27"), [])

    def test_accepts_second_precision(self):
        events = [{"title": "S", "start": "2026-10-27T09:00:00",
                   "end": "2026-10-27T09:30:59"}]
        self.assertEqual(calendar_mac.busy_to_minutes(events, "2026-10-27"),
                         [(540, 570)])

    def test_empty_input(self):
        self.assertEqual(calendar_mac.busy_to_minutes([], "2026-10-27"), [])
        self.assertEqual(calendar_mac.busy_to_minutes(None, "2026-10-27"), [])

    def test_accepts_a_date_object_for_the_day(self):
        events = [{"title": "A", "start": "2026-10-27T09:00",
                   "end": "2026-10-27T10:00"}]
        self.assertEqual(calendar_mac.busy_to_minutes(events, date(2026, 10, 27)),
                         [(540, 600)])

    def test_read_busy_output_is_accepted_directly(self):
        runner = FakeRunner(stdout=json.dumps(BUSY_PAYLOAD))
        events = calendar_mac.read_busy(WEEK_START, WEEK_END, "Thesis",
                                        runner=runner)
        self.assertEqual(
            calendar_mac.busy_to_minutes(events, "2026-10-27"), [(600, 660)])


# ------------------------------------------------------------- no real osascript
class TestNoRealProcesses(unittest.TestCase):
    def test_injected_runner_means_subprocess_run_is_never_called(self):
        payload = json.dumps({"calendars": [], "events": []})
        with mock.patch("planner.calendar_mac.subprocess.run",
                        side_effect=AssertionError("real osascript call!")):
            events = calendar_mac.read_busy(WEEK_START, WEEK_END, "Thesis",
                                            runner=FakeRunner(stdout=payload))
            self.assertEqual(events, [])
            uid = calendar_mac.write_event(
                "x", "2026-10-28T10:00", "2026-10-28T11:00", "Thesis",
                runner=FakeRunner(stdout=json.dumps({"uid": "u1"})))
            self.assertEqual(uid, "u1")


# ----------------------------------------------------- the real week-read API
# The UI imports week_events/week_busy/check_access; these tests pin the exact
# shapes it will index into.  Every payload below mimics what _READ_EVENTS_JS
# prints (including `readable` and `errors`), so the fake runner stays honest.

def make_payload(events, calendars=("Work", "Home"), errors=(), readable=None):
    calendars = list(calendars)
    errors = list(errors)
    return {
        "calendars": calendars,
        "events": list(events),
        "errors": errors,
        "readable": (len(calendars) - len(errors)) if readable is None
        else readable,
    }


def ev(calendar, title, start, end, all_day=False, uid="", location=""):
    """One event exactly as the JXA script reports it."""
    return {"calendar": calendar, "id": uid, "title": title,
            "start": start, "end": end, "allDay": all_day,
            "location": location}


def week_of(events, **kwargs):
    runner = FakeRunner(stdout=json.dumps(make_payload(events, **kwargs)))
    return calendar_mac.week_events(WEEK_START, WEEK_END, runner=runner), runner


class TestWeekEvents(unittest.TestCase):
    def test_returns_the_documented_event_shape(self):
        result, _ = week_of([ev("Work", "Standup", "2026-10-27T09:30",
                                "2026-10-27T09:45", uid="w1",
                                location="Zoom")])
        self.assertEqual(result["events"], [{
            "id": "w1",
            "date": "2026-10-27",
            "start": "09:30",
            "end": "09:45",
            "title": "Standup",
            "calendar": "Work",
            "all_day": False,
            "location": "Zoom",
        }])
        self.assertEqual(result["calendars"], ["Work", "Home"])
        self.assertIsNone(result["error"])

    def test_events_from_several_calendars_are_merged_and_sorted(self):
        result, _ = week_of([
            ev("Work", "Review", "2026-10-27T13:00", "2026-10-27T14:00",
               uid="a"),
            ev("Home", "Dentist", "2026-10-27T09:30", "2026-10-27T10:15",
               uid="b"),
            ev("Work", "Standup", "2026-10-27T10:00", "2026-10-27T11:00",
               uid="c"),
        ])
        self.assertEqual([e["title"] for e in result["events"]],
                         ["Dentist", "Standup", "Review"])
        self.assertEqual([e["calendar"] for e in result["events"]],
                         ["Home", "Work", "Work"])

    def test_noise_calendars_are_skipped_by_default(self):
        result, _ = week_of([
            ev("Birthdays", "Ada Lovelace", "2026-10-27T00:00",
               "2026-10-28T00:00", all_day=True, uid="b1"),
            ev("US Holidays", "Columbus Day", "2026-10-27T00:00",
               "2026-10-28T00:00", all_day=True, uid="h1"),
            ev("Siri Suggestions", "Call Mom", "2026-10-27T18:00",
               "2026-10-27T18:30", uid="s1"),
            ev("Work", "Standup", "2026-10-27T09:30", "2026-10-27T09:45",
               uid="w1"),
        ], calendars=("Work", "Birthdays", "US Holidays", "Siri Suggestions"))
        self.assertEqual([e["title"] for e in result["events"]], ["Standup"])
        self.assertEqual(result["calendars"], ["Work"])
        self.assertIsNone(result["error"])

    def test_noise_matching_is_case_insensitive_and_substring_based(self):
        result, _ = week_of([
            ev("Work", "Standup", "2026-10-27T09:30", "2026-10-27T09:45"),
            ev("Miriam's BIRTHDAY", "Cake", "2026-10-27T00:00",
               "2026-10-28T00:00", all_day=True),
            ev("Bank Holidays 2026", "Bank", "2026-10-27T00:00",
               "2026-10-28T00:00", all_day=True),
        ], calendars=("Work", "Miriam's BIRTHDAY", "Bank Holidays 2026"))
        self.assertEqual([e["title"] for e in result["events"]], ["Standup"])

    def test_a_noise_calendar_can_be_requested_explicitly(self):
        runner = FakeRunner(stdout=json.dumps(make_payload(
            [ev("Birthdays", "Ada Lovelace", "2026-10-27T00:00",
                "2026-10-28T00:00", all_day=True, uid="b1")],
            calendars=("Work", "Birthdays"))))
        result = calendar_mac.week_events(WEEK_START, WEEK_END,
                                          calendar_names="Birthdays",
                                          runner=runner)
        self.assertEqual([e["title"] for e in result["events"]],
                         ["Ada Lovelace"])
        self.assertEqual(result["calendars"], ["Birthdays"])
        self.assertIsNone(result["error"])

    def test_calendar_names_accepts_a_list(self):
        runner = FakeRunner(stdout=json.dumps(make_payload([
            ev("Work", "Standup", "2026-10-27T09:30", "2026-10-27T09:45"),
            ev("Home", "Dentist", "2026-10-27T15:00", "2026-10-27T16:00"),
        ], calendars=("Work", "Home"))))
        result = calendar_mac.week_events(WEEK_START, WEEK_END,
                                          calendar_names=["home"],
                                          runner=runner)
        self.assertEqual([e["title"] for e in result["events"]], ["Dentist"])
        self.assertEqual(result["calendars"], ["Home"])

    def test_one_osascript_call_covers_the_whole_week(self):
        result, runner = week_of([])
        self.assertEqual(result["events"], [])
        self.assertEqual(len(runner.calls), 1)
        script = runner.script
        self.assertIn("Application('Calendar')", script)
        self.assertIn("new Date(2026, 9, 26, 0, 0, 0)", script)    # Mon Oct 26
        self.assertIn("new Date(2026, 9, 19, 0, 0, 0)", script)    # scan start
        self.assertIn("new Date(2026, 10, 1, 23, 59, 59)", script)  # Sun Nov 1
        self.assertIn("startDate", script)
        self.assertIn("alldayEvent", script)
        self.assertTrue(script.rstrip().endswith("JSON.stringify(result);"))

    def test_the_read_script_never_creates_an_event(self):
        _result, runner = week_of([])
        self.assertNotIn("app.Event(", runner.script)

    def test_a_midnight_spanning_event_keeps_its_real_dates(self):
        result, _ = week_of([ev("Work", "Night shift", "2026-10-27T23:00",
                                "2026-10-28T01:30", uid="n1")])
        event = result["events"][0]
        self.assertEqual(event["date"], "2026-10-27")
        self.assertEqual((event["start"], event["end"]), ("23:00", "01:30"))

    def test_an_event_started_before_the_range_is_kept(self):
        result, _ = week_of([ev("Home", "Party", "2026-10-25T22:00",
                                "2026-10-26T08:00")])
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["date"], "2026-10-25")

    def test_an_event_ending_at_the_range_start_is_dropped(self):
        result, _ = week_of([ev("Home", "Sleep", "2026-10-25T22:00",
                                "2026-10-26T00:00")])
        self.assertEqual(result["events"], [])

    def test_an_event_after_the_range_is_dropped(self):
        result, _ = week_of([ev("Work", "Next month", "2026-11-10T10:00",
                                "2026-11-10T11:00")])
        self.assertEqual(result["events"], [])

    def test_all_day_is_inferred_when_the_flag_is_missing(self):
        result, _ = week_of([
            {"calendar": "Work", "title": "Conference",
             "start": "2026-10-28T00:00", "end": "2026-10-29T00:00"},
            {"calendar": "Work", "title": "Standup",
             "start": "2026-10-28T09:00", "end": "2026-10-28T09:15"},
        ])
        flags = {e["title"]: e["all_day"] for e in result["events"]}
        self.assertEqual(flags, {"Conference": True, "Standup": False})

    def test_missing_uid_gets_a_stable_synthetic_id(self):
        result, _ = week_of([ev("Work", "Standup", "2026-10-27T10:00",
                                "2026-10-27T11:00")])
        self.assertEqual(result["events"][0]["id"],
                         "Work|2026-10-27T10:00|2026-10-27T11:00")

    def test_permission_failure_returns_empty_data_and_actions(self):
        runner = FakeRunner(returncode=1,
                            stderr="execution error: Not authorized. (-1743)")
        result = calendar_mac.week_events(WEEK_START, WEEK_END, runner=runner)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["calendars"], [])
        error = result["error"] or ""
        self.assertIn("System Settings > Privacy & Security > Automation", error)
        self.assertIn("> Calendars", error)
        self.assertIn("plan export --ics", error)
        self.assertIn("-1743", error)

    def test_unreadable_calendars_do_not_look_like_a_free_week(self):
        runner = FakeRunner(stdout=json.dumps({
            "calendars": ["Work", "Home"],
            "events": [],
            "errors": [{"calendar": "Work", "error": "Not authorized"},
                       {"calendar": "Home", "error": "Not authorized"}],
            "readable": 0,
        }))
        result = calendar_mac.week_events(WEEK_START, WEEK_END, runner=runner)
        self.assertEqual(result["events"], [])
        error = result["error"] or ""
        self.assertIn("no events from any of its 2 calendar(s)", error)
        self.assertIn("Automation", error)

    def test_unknown_calendar_falls_back_and_says_so(self):
        runner = FakeRunner(stdout=json.dumps(make_payload([
            ev("Work", "Standup", "2026-10-27T09:30", "2026-10-27T09:45"),
            ev("Home", "Dentist", "2026-10-27T15:00", "2026-10-27T16:00"),
        ])))
        result = calendar_mac.week_events(WEEK_START, WEEK_END,
                                          calendar_names="Thesis",
                                          runner=runner)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["calendars"], ["Work", "Home"])
        self.assertTrue((result["error"] or "").startswith("partial: "))
        self.assertIn("'Thesis'", result["error"])

    def test_bad_dates_come_back_as_an_error(self):
        result, _ = week_of([])
        self.assertIsNone(result["error"])  # sanity: the good path is clean
        bad = calendar_mac.week_events("someday", WEEK_END,
                                       runner=FakeRunner(stdout="{}"))
        self.assertEqual(bad["events"], [])
        self.assertIn("ValueError", bad["error"] or "")

    def test_inverted_range_is_an_error_not_an_exception(self):
        result = calendar_mac.week_events(WEEK_END, WEEK_START,
                                          runner=FakeRunner(stdout="{}"))
        self.assertEqual(result["events"], [])
        self.assertIn("before start_date", result["error"] or "")

    def test_a_broken_payload_does_not_raise(self):
        result = calendar_mac.week_events(
            WEEK_START, WEEK_END, runner=FakeRunner(stdout="not json at all"))
        self.assertEqual(result["events"], [])
        self.assertIn("unexpected output", result["error"] or "")


class TestWeekBusy(unittest.TestCase):
    def busy_for(self, events, **kwargs):
        runner = kwargs.pop("runner", None) or FakeRunner(
            stdout=json.dumps(make_payload(events, **kwargs)))
        return calendar_mac.week_busy(WEEK_START, WEEK_END, runner=runner)

    def test_every_day_of_the_range_is_present(self):
        result = self.busy_for([])
        self.assertEqual(list(result["busy"]), [
            "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29",
            "2026-10-30", "2026-10-31", "2026-11-01"])
        self.assertTrue(all(spans == [] for spans in result["busy"].values()))
        self.assertIsNone(result["error"])

    def test_clips_a_midnight_spanning_event_to_both_days(self):
        result = self.busy_for([ev("Work", "Night shift", "2026-10-27T23:00",
                                   "2026-10-28T01:30", uid="n1")])
        busy = result["busy"]
        self.assertEqual(busy["2026-10-26"], [])
        self.assertEqual(busy["2026-10-27"], [["23:00", "24:00"]])
        self.assertEqual(busy["2026-10-28"], [["00:00", "01:30"]])
        self.assertEqual(busy["2026-10-29"], [])

    def test_clips_a_multi_day_event_to_three_days(self):
        result = self.busy_for([ev("Home", "Trip", "2026-10-30T22:00",
                                   "2026-11-01T08:00", uid="t1")])
        busy = result["busy"]
        self.assertEqual(busy["2026-10-30"], [["22:00", "24:00"]])
        self.assertEqual(busy["2026-10-31"], [["00:00", "24:00"]])
        self.assertEqual(busy["2026-11-01"], [["00:00", "08:00"]])
        self.assertEqual(busy["2026-10-29"], [])

    def test_merges_overlapping_events_from_two_calendars(self):
        result = self.busy_for([
            ev("Work", "Standup", "2026-10-27T10:00", "2026-10-27T11:00"),
            ev("Home", "Dentist", "2026-10-27T09:30", "2026-10-27T10:15"),
            ev("Work", "Review", "2026-10-27T13:00", "2026-10-27T14:00"),
        ])
        self.assertEqual(result["busy"]["2026-10-27"],
                         [["09:30", "11:00"], ["13:00", "14:00"]])

    def test_all_day_events_are_excluded_by_default(self):
        result = self.busy_for([ev("Work", "Conference", "2026-10-28T00:00",
                                   "2026-10-29T00:00", all_day=True,
                                   uid="c1")])
        self.assertEqual(result["busy"]["2026-10-28"], [])
        self.assertEqual(result["busy"]["2026-10-27"], [])

    def test_all_day_events_can_be_included(self):
        runner = FakeRunner(stdout=json.dumps(make_payload(
            [ev("Work", "Conference", "2026-10-28T00:00",
                "2026-10-29T00:00", all_day=True, uid="c1")],
            calendars=("Work",))))
        result = calendar_mac.week_busy(WEEK_START, WEEK_END,
                                        ignore_all_day=False, runner=runner)
        self.assertEqual(result["busy"]["2026-10-28"], [["00:00", "24:00"]])
        self.assertEqual(result["busy"]["2026-10-29"], [])

    def test_a_three_day_all_day_event_blocks_each_day(self):
        runner = FakeRunner(stdout=json.dumps(make_payload([
            ev("Work", "Offsite", "2026-10-28T00:00", "2026-10-31T00:00",
               all_day=True)], calendars=("Work",))))
        result = calendar_mac.week_busy(WEEK_START, WEEK_END,
                                        ignore_all_day=False, runner=runner)
        for key in ("2026-10-28", "2026-10-29", "2026-10-30"):
            self.assertEqual(result["busy"][key], [["00:00", "24:00"]])
        self.assertEqual(result["busy"]["2026-10-31"], [])

    def test_noise_calendars_never_make_the_week_busy(self):
        result = self.busy_for([
            ev("Birthdays", "Ada", "2026-10-27T00:00", "2026-10-28T00:00",
               all_day=True),
            ev("Holidays", "Day off", "2026-10-27T00:00",
               "2026-10-28T00:00", all_day=True),
        ], calendars=("Birthdays", "Holidays"))
        self.assertTrue(all(spans == [] for spans in result["busy"].values()))

    def test_permission_failure_propagates_with_empty_lists(self):
        runner = FakeRunner(returncode=1,
                            stderr="execution error: Not authorized. (-1743)")
        result = calendar_mac.week_busy(WEEK_START, WEEK_END, runner=runner)
        self.assertEqual(len(result["busy"]), 7)
        self.assertTrue(all(spans == [] for spans in result["busy"].values()))
        self.assertIn("System Settings > Privacy & Security > Automation",
                      result["error"] or "")

    def test_one_osascript_call_per_week(self):
        runner = FakeRunner(stdout=json.dumps(make_payload([])))
        calendar_mac.week_busy(WEEK_START, WEEK_END, runner=runner)
        self.assertEqual(len(runner.calls), 1)

    def test_inverted_range_returns_empty_busy(self):
        result = calendar_mac.week_busy(WEEK_END, WEEK_START,
                                        runner=FakeRunner(stdout="{}"))
        self.assertEqual(result["busy"], {})
        self.assertIn("before start_date", result["error"] or "")


class TestCheckAccess(unittest.TestCase):
    def test_ok_reports_calendars(self):
        runner = FakeRunner(stdout=json.dumps(
            {"calendars": ["Work", "Home"], "probe": True, "error": None}))
        result = calendar_mac.check_access(runner=runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["calendars"], ["Work", "Home"])
        self.assertIn("2 calendar(s)", result["detail"])
        self.assertEqual(len(runner.calls), 1)

    def test_denied_automation_is_reported_not_raised(self):
        runner = FakeRunner(returncode=1,
                            stderr="execution error: Not authorized. (-1743)")
        result = calendar_mac.check_access(runner=runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["calendars"], [])
        self.assertIn("System Settings > Privacy & Security > Automation",
                      result["detail"])
        self.assertIn("plan export --ics", result["detail"])

    def test_names_readable_but_events_denied_is_not_ok(self):
        runner = FakeRunner(stdout=json.dumps({
            "calendars": ["Work"], "probe": False,
            "error": "Not authorized to read events"}))
        result = calendar_mac.check_access(runner=runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["calendars"], ["Work"])
        self.assertIn("reading events failed", result["detail"])
        self.assertIn("> Calendars", result["detail"])

    def test_no_calendars_at_all_is_still_ok(self):
        runner = FakeRunner(stdout=json.dumps(
            {"calendars": [], "probe": True, "error": None}))
        result = calendar_mac.check_access(runner=runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["calendars"], [])
        self.assertIn("no calendars", result["detail"])

    def test_silent_osascript_is_not_success(self):
        result = calendar_mac.check_access(runner=FakeRunner(stdout=""))
        self.assertFalse(result["ok"])
        self.assertIn("no output", result["detail"])

    def test_garbage_output_is_reported_not_raised(self):
        result = calendar_mac.check_access(
            runner=FakeRunner(stdout="<<<not json>>>"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["calendars"], [])


class TestEnsureCalendar(unittest.TestCase):
    """The mirror's healing primitive: idempotent calendar creation."""

    def test_existing_calendar_is_left_alone(self):
        runner = FakeRunner(stdout=json.dumps({"exists": True,
                                               "created": False}))
        self.assertTrue(calendar_mac.ensure_calendar("Habits", runner=runner))
        self.assertEqual(len(runner.calls), 1)
        self.assertIn('var wanted = "Habits";', runner.script)
        self.assertIn("found = calendars[i];", runner.script)  # lookup first

    def test_missing_calendar_is_created(self):
        runner = FakeRunner(stdout=json.dumps({"exists": True,
                                               "created": True}))
        self.assertTrue(calendar_mac.ensure_calendar("Habits", runner=runner))
        self.assertIn("app.make", runner.script)
        self.assertIn("withProperties: { name: wanted }", runner.script)

    def test_failure_is_false_not_an_exception(self):
        runner = FakeRunner(returncode=1, stderr="execution error: (-1743)")
        self.assertFalse(calendar_mac.ensure_calendar("Habits", runner=runner))

    def test_empty_name_is_refused_without_running_anything(self):
        self.assertFalse(calendar_mac.ensure_calendar("  ", runner=None))


class TestListCalendars(unittest.TestCase):
    def test_returns_names_in_calendar_order(self):
        runner = FakeRunner(stdout=json.dumps(["Work", "Home", "Thesis"]))
        self.assertEqual(calendar_mac.list_calendars(runner=runner),
                         ["Work", "Home", "Thesis"])
        self.assertIn("Application('Calendar')", runner.script)

    def test_dedupes_case_insensitively_and_drops_blanks(self):
        runner = FakeRunner(stdout=json.dumps(["Work", "", "work", " Home "]))
        self.assertEqual(calendar_mac.list_calendars(runner=runner),
                         ["Work", "Home"])

    def test_failure_raises_for_the_caller_that_wants_it(self):
        runner = FakeRunner(returncode=1, stderr="Not authorized. (-1743)")
        with self.assertRaises(CalendarError) as ctx:
            calendar_mac.list_calendars(runner=runner)
        self.assertIn("Automation", str(ctx.exception))


class TestNoRealProcessesForTheWeekApi(unittest.TestCase):
    def test_the_week_api_never_shells_out_when_a_runner_is_injected(self):
        payload = json.dumps(make_payload([]))
        with mock.patch("planner.calendar_mac.subprocess.run",
                        side_effect=AssertionError("real osascript call!")):
            self.assertEqual(
                calendar_mac.week_events(WEEK_START, WEEK_END,
                                         runner=FakeRunner(stdout=payload))["events"],
                [])
            busy = calendar_mac.week_busy(
                WEEK_START, WEEK_END,
                runner=FakeRunner(stdout=payload))["busy"]
            self.assertEqual(len(busy), 7)
            self.assertTrue(all(spans == [] for spans in busy.values()))
            self.assertFalse(calendar_mac.check_access(
                runner=FakeRunner(stdout=""))["ok"])


class AppleScriptReaderTest(unittest.TestCase):
    """The fast path: one AppleScript pass, tab-separated lines back."""

    def runner(self, stdout="", returncode=0, stderr=""):
        def run(argv, **kwargs):
            run.argv = argv
            return type("P", (), {"stdout": stdout, "stderr": stderr,
                                  "returncode": returncode})()
        return run

    def test_parses_lines_into_records(self):
        out = "\n".join([
            "SAPIENZA\t194.207 Generative AI\t2026-10-28T15:00:00\t2026-10-28T17:00:00\tfalse",
            "Casa\tDentist\t2026-10-27T09:00:00\t2026-10-27T09:45:00\tfalse",
            "Casa\tOgnissanti\t2026-11-01T00:00:00\t2026-11-01T23:59:59\ttrue",
            "Festività in Italia\tTutti i Santi\t2026-11-01T00:00:00\t2026-11-01T23:59:59\ttrue",
        ])
        run = self.runner(out)
        records, cals, err = calendar_mac._read_week_apple(
            date(2026, 10, 26), date(2026, 11, 1), None, runner=run)
        self.assertIsNone(err)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["title"], "Dentist")      # sorted by date/start
        self.assertEqual(records[0]["start"], "09:00")
        self.assertEqual(records[1]["date"], "2026-10-28")
        self.assertTrue(records[2]["all_day"])
        self.assertIn("SAPIENZA", cals)
        # noise calendars never reach the planner
        self.assertNotIn("Festività in Italia", {r["calendar"] for r in records})

    def test_noise_calendars_are_filtered_out_of_the_query(self):
        run = self.runner("")
        calendar_mac._read_week_apple(date(2026, 10, 26), date(2026, 11, 1),
                                      ["Casa", "Birthdays", "Festività in Italia"],
                                      runner=run)
        wanted = run.argv[-1]
        self.assertIn("Casa", wanted)
        self.assertNotIn("Birthdays", wanted)
        self.assertNotIn("Festività", wanted)

    def test_request_is_exclusive_of_the_day_after(self):
        run = self.runner("")
        calendar_mac._read_week_apple(date(2026, 10, 26), date(2026, 11, 1), None, runner=run)
        self.assertEqual(run.argv[-2], "2026-11-02")

    def test_duplicate_lines_are_collapsed(self):
        line = "Casa\tStandup\t2026-10-27T09:00:00\t2026-10-27T09:15:00\tfalse"
        run = self.runner("\n".join([line, line]))
        records, _, _ = calendar_mac._read_week_apple(
            date(2026, 10, 26), date(2026, 11, 1), None, runner=run)
        self.assertEqual(len(records), 1)

    def test_osascript_failure_raises(self):
        run = self.runner("", returncode=1, stderr="not authorised (-1743)")
        with self.assertRaises(calendar_mac.CalendarError):
            calendar_mac._read_week_apple(date(2026, 10, 26), date(2026, 11, 1),
                                          None, runner=run)


if __name__ == "__main__":
    unittest.main()
