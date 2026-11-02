"""
calendar_mac.py — Apple Calendar bridge for the thesis planner (macOS, stdlib).

What the planner needs from Calendar.app is simple and strict: the real
meetings of the week, so tasks land in the gaps between them.  This module is
the only place that talks to Calendar.app; everything else consumes dicts.

Public API (the UI imports exactly these):

  week_events(start_date, end_date, calendar_names=None, runner=None) -> dict
      {"events": [{"id", "date", "start", "end", "title", "calendar",
                   "all_day", "location"}, ...],
       "calendars": [str], "error": str|None}

  week_busy(start_date, end_date, calendar_names=None, ignore_all_day=True,
            runner=None) -> dict
      {"busy": {"YYYY-MM-DD": [["HH:MM", "HH:MM"], ...]}, "error": str|None}

  check_access(runner=None) -> {"ok": bool, "detail": str, "calendars": [str]}
  list_calendars(runner=None) -> [str]

  read_busy(week_start, week_end, calendar_name, runner=None) -> [ {title,
      start, end} ]            legacy shape, kept for cli.py
  write_event(...) -> uid | None
  export_ics(...) -> path written (no Automation permission required)

Design rules that the rest of the planner relies on:

  * ONE osascript round-trip per week.  A week is a single JXA script that
    walks every calendar once; nothing is fetched per day.  Nothing is cached
    across processes — a stale cache would silently double-book the user.
  * Time zones never enter the picture: JXA `new Date(y, m, d, hh, mm)` is
    local wall-clock, and we compare local wall-clock on both sides.
  * Permission failure is data, not an exception.  week_events/week_busy/
    check_access return an actionable `error` string and empty results; only
    the low-level primitives (list_calendars, read_busy) still raise, because
    their callers (cli.py) already print the message.
  * Noise calendars are excluded by default: "Birthdays", "Holidays",
    "Siri Suggestions" and anything whose name contains "birthday" or
    "holiday" (that also catches "US Holidays", "Facebook Birthdays").  Pass
    them in `calendar_names` to read them anyway.
  * Timed events that cross midnight are clipped per day in week_busy, so a
    23:00–01:30 meeting blocks 23:00–24:00 today and 00:00–01:30 tomorrow.
  * All-day events are dropped from week_busy by default: they mark a whole
    day, and a planner that treats "Conference" as 24h busy would schedule
    nothing.  week_events still reports them with all_day=True.

Known limits (documented, not hidden):

  * Recurring series are visible only if Calendar.app expands occurrences for
    `events whose startDate ...`; the query is start-date indexed, so an
    occurrence that Calendar keeps on its master event can be missed.  There is
    no way to fix this over Apple Events without EventKit.
  * The scan starts LOOKBACK_DAYS before `start_date`, which is what lets a
    Sunday-night event bleed into Monday.  An event that started more than a
    week before the range and is still running is not reported.

Failure modes and what to tell the user: a non-zero osascript exit is nearly
always a denied Automation grant (-1743) or a modal dialog in Calendar.app, so
the message names the macOS privacy panes and the permission-free ICS export.
"""
from __future__ import annotations

import json
import os
import re
import string
import subprocess
from datetime import date, datetime, timedelta

try:  # normal case: imported as planner.calendar_mac
    from .scheduler import fmt_hhmm, parse_hhmm
except ImportError:  # pragma: no cover - allows `python3 planner/calendar_mac.py`
    from scheduler import fmt_hhmm, parse_hhmm


OSASCRIPT = "osascript"
DEFAULT_TIMEOUT = 20
#: A week read walks every calendar; 20s is tight on a large iCloud account.
READ_TIMEOUT = 45
#: How far before the requested week the JXA query starts, so that an event
#: beginning late on the previous Sunday is still seen (and clipped) on Monday.
LOOKBACK_DAYS = 7

#: Calendars that describe the world, not the user's time.
NOISE_CALENDARS = frozenset({
    "birthdays", "birthday", "holidays", "holiday",
    "siri suggestions", "siri suggestion",
})
NOISE_CALENDAR_SUBSTRINGS = ("birthday", "holiday", "festivit", "festività",
                              "scheduled reminders", "siri suggestions")

#: Shown on every Apple Events failure — the fix is a settings toggle, and there
#: is a permission-free way to get the plan out.
PERMISSION_HINT = (
    "Grant access in System Settings > Privacy & Security > Automation "
    "(let your terminal / Python control \"Calendar\") and, on macOS 14+, in "
    "System Settings > Privacy & Security > Calendars (Full Calendar Access); "
    "then retry. No permission yet? `plan export --ics` works without "
    "permission and writes an .ics file you can import into Calendar by hand."
)


class CalendarError(RuntimeError):
    """Apple Calendar could not be reached, read, or written."""


def event_summary(task, cfg=None):
    """The event summary for a planned block: `<category>: <task title>`.

    The prefix is the task's area when that area is a real category ("Thesis"
    or a subject folder name under university_root), so a course task shows
    up in Calendar.app as "NLP Semantics: reading response". Anything else
    (legacy keyword areas like "coreference", empty areas) keeps the
    "Thesis: " prefix those events have always had. With no cfg the prefix
    is "Thesis" — the historical default.
    """
    title = str((task or {}).get("title") or "").strip() or "task"
    area = str((task or {}).get("area") or "").strip()
    prefix = "Thesis"
    if cfg is not None and area:
        try:
            from planner import sources
            if area in sources.categories(cfg):
                prefix = area
        except Exception:
            pass
    return "%s: %s" % (prefix, title)


# ---------------------------------------------------------------- process layer
def _default_runner(argv, **kwargs):
    return subprocess.run(argv, **kwargs)


def _run_js(script, timeout=DEFAULT_TIMEOUT, runner=None):
    """Run a JXA script through osascript and return stdout (stripped)."""
    argv = [OSASCRIPT, "-l", "JavaScript", "-e", script]
    run = runner or _default_runner
    try:
        proc = run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CalendarError(
            "osascript timed out after %ss with no answer from Calendar.app. "
            "Calendar may be showing a modal dialog (a permission prompt or a "
            "sync warning) — bring it to the front, dismiss it, and retry.\n%s"
            % (timeout, PERMISSION_HINT)) from exc
    except FileNotFoundError as exc:
        raise CalendarError(
            "`osascript` was not found on PATH; the calendar bridge needs macOS "
            "with Apple Calendar installed.\n%s" % PERMISSION_HINT) from exc
    except OSError as exc:
        raise CalendarError(
            "failed to launch osascript: %s\n%s" % (exc, PERMISSION_HINT)) from exc

    code = getattr(proc, "returncode", 0) or 0
    out = getattr(proc, "stdout", "") or ""
    err = getattr(proc, "stderr", "") or ""
    if code != 0:
        detail = (err or out).strip() or "no error output"
        raise CalendarError(
            "Apple Calendar is not reachable (osascript exit %s): %s\n%s"
            % (code, detail, PERMISSION_HINT))
    return out.strip()


# ------------------------------------------------------------------ conversion
def _js_str(value):
    """Render a Python string as a JS string literal (JSON strings are JS)."""
    s = "" if value is None else str(value)
    s = s.replace("\u2028", " ").replace("\u2029", " ")
    return json.dumps(s)


def _js_date(dt):
    """JXA `new Date(...)` source in LOCAL time (month is 0-based in JS)."""
    return "new Date(%d, %d, %d, %d, %d, 0)" % (
        dt.year, dt.month - 1, dt.day, dt.hour, dt.minute)


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(
            "expected a date or 'YYYY-MM-DD', got %r" % (value,)) from exc


def _as_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    raise CalendarError(
        "cannot parse %r as a date/time (expected 'YYYY-MM-DDTHH:MM')" % (value,))


def _as_minutes(value):
    """'HH:MM' (or an int of minutes) -> minutes since midnight."""
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not re.match(r"^\d{1,2}:\d{2}", text):
        raise ValueError("expected 'HH:MM', got %r" % (value,))
    return parse_hhmm(text[:5])


_STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})")


def _parse_stamp(value):
    """'YYYY-MM-DDTHH:MM[:SS]' -> (date_str, minutes) or None."""
    if not value:
        return None
    match = _STAMP_RE.match(str(value).strip())
    if not match:
        return None
    return match.group(1), parse_hhmm("%s:%s" % (match.group(2), match.group(3)))


def _stamp_text(stamp):
    return "%sT%s" % (stamp[0], fmt_hhmm(stamp[1]))


def _next_day(stamp):
    """(date_str, minutes) -> the same clock time on the following day."""
    day = date.fromisoformat(stamp[0]) + timedelta(days=1)
    return day.isoformat(), stamp[1]


def _parse_json(raw, what):
    """Parse osascript output, tolerating stray warning lines before the JSON."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line[:1] in ("{", "["):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise CalendarError(
        "unexpected output from Calendar while running %s: %r"
        % (what, text[:400]))


def _error_text(exc):
    """One-line-ish failure text for the `error` field (never None)."""
    if isinstance(exc, CalendarError):
        return str(exc).strip()
    return "%s: %s" % (type(exc).__name__, exc)


def _merge_spans(spans):
    """Merge overlapping/touching (start, end) minute spans."""
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


# --------------------------------------------------------------- calendar names
def _is_noise_calendar(name):
    """True for Birthdays / Holidays / Siri Suggestions-style calendars."""
    text = str(name or "").strip().lower()
    if not text:
        return False  # unnamed: cannot classify, so do not hide its events
    if text in NOISE_CALENDARS:
        return True
    return any(part in text for part in NOISE_CALENDAR_SUBSTRINGS)


def _normalize_calendar_names(names):
    """None | str | iterable -> list of names, or None for "no filter"."""
    if names is None:
        return None
    if isinstance(names, str):
        text = names.strip()
        return [text] if text else None
    try:
        items = list(names)
    except TypeError:
        text = str(names).strip()
        return [text] if text else None
    out = []
    seen = set()
    for item in items:
        text = str(item or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out or None


# --------------------------------------------------------------- JXA templates
_READ_EVENTS_JS = string.Template("""
function pad(n) { return (n < 10 ? '0' : '') + n; }
function isoMinute(d) {
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
    + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}
var app = Application('Calendar');
var from = new Date($from_y, $from_m, $from_d, 0, 0, 0);
var scan = new Date($scan_y, $scan_m, $scan_d, 0, 0, 0);
var to = new Date($to_y, $to_m, $to_d, 23, 59, 59);
var result = { calendars: [], events: [], errors: [], readable: 0 };
var calendars = app.calendars();
for (var i = 0; i < calendars.length; i++) {
  var calName = '';
  try { calName = calendars[i].name(); } catch (nameErr) { calName = ''; }
  if (calName === null || calName === undefined) { calName = ''; }
  calName = String(calName);
  result.calendars.push(calName);
  var events = null;
  try {
    events = calendars[i].events.whose({
      startDate: {
        _greaterThanEquals: scan,
        _lessThanEquals: to
      }
    })();
  } catch (readErr) {
    result.errors.push({ calendar: calName, error: String(readErr) });
    events = null;
  }
  if (events === null) { continue; }
  result.readable = result.readable + 1;
  for (var j = 0; j < events.length; j++) {
    var ev = events[j];
    var start = null, end = null, title = '', location = '', uid = '';
    var allDay = null;
    try { start = ev.startDate(); } catch (startErr) { start = null; }
    try { end = ev.endDate(); } catch (endErr) { end = null; }
    if (!start || !end) { continue; }
    try { title = ev.summary(); } catch (titleErr) { title = ''; }
    try { location = ev.location(); } catch (locErr) { location = ''; }
    try { uid = ev.uid(); } catch (uidErr) { uid = ''; }
    try { allDay = ev.alldayEvent(); } catch (allErr) {
      try { allDay = ev.allDayEvent(); } catch (allErr2) { allDay = null; }
    }
    if (end <= from || start > to) { continue; }
    result.events.push({
      calendar: calName,
      id: (uid === null || uid === undefined) ? '' : String(uid),
      title: (title === null || title === undefined) ? '' : String(title),
      location: (location === null || location === undefined) ? '' : String(location),
      start: isoMinute(start),
      end: isoMinute(end),
      allDay: (allDay === null || allDay === undefined) ? null : !!allDay
    });
  }
}
JSON.stringify(result);
""")

_LIST_CALENDARS_JS = string.Template("""
var app = Application('Calendar');
var out = [];
var calendars = app.calendars();
for (var i = 0; i < calendars.length; i++) {
  var name = '';
  try { name = calendars[i].name(); } catch (nameErr) { name = ''; }
  out.push((name === null || name === undefined) ? '' : String(name));
}
JSON.stringify(out);
""")

#: Names prove Automation works; a tiny 2000-01-01 event query proves that the
#: Calendars privacy service (macOS 14+) lets us read events too.  The probe is
#: one day in 2000, so it cannot be slow and cannot expose anything current.
_CHECK_ACCESS_JS = string.Template("""
var app = Application('Calendar');
var result = { calendars: [], probe: false, error: null };
var calendars = app.calendars();
for (var i = 0; i < calendars.length; i++) {
  var name = '';
  try { name = calendars[i].name(); } catch (nameErr) { name = ''; }
  result.calendars.push((name === null || name === undefined) ? '' : String(name));
}
if (calendars.length === 0) {
  result.probe = true;
} else {
  try {
    calendars[0].events.whose({
      startDate: {
        _greaterThanEquals: new Date(2000, 0, 1, 0, 0, 0),
        _lessThanEquals: new Date(2000, 0, 2, 0, 0, 0)
      }
    })();
    result.probe = true;
  } catch (probeErr) {
    result.probe = false;
    result.error = String(probeErr);
  }
}
JSON.stringify(result);
""")

_WRITE_EVENT_JS = string.Template("""
var app = Application('Calendar');
var wanted = $calendar;
var wantedKey = String(wanted).trim().toLowerCase();
var calendars = app.calendars();
var names = [];
var target = null;
for (var i = 0; i < calendars.length; i++) {
  var calName = '';
  try { calName = calendars[i].name(); } catch (err) { calName = ''; }
  names.push(String(calName));
  if (target === null && String(calName).trim().toLowerCase() === wantedKey) {
    target = calendars[i];
  }
}
if (target === null) {
  JSON.stringify({ error: 'CALENDAR_NOT_FOUND', calendar: wanted, calendars: names });
} else {
  var event = app.Event({
    summary: $summary,
    description: $description,
    startDate: $start,
    endDate: $end,
    alldayEvent: false
  });
  target.events.push(event);
  var uid = '';
  try { uid = event.uid(); } catch (err) { uid = ''; }
  JSON.stringify({ uid: (uid === null || uid === undefined) ? '' : String(uid) });
}
""")

# Moving/deleting a slot the user corrected by hand: find the event by its
# uid (scoped to the configured calendar when given) and change it in place.
_UPDATE_EVENT_JS = string.Template("""
var app = Application('Calendar');
var uidKey = String($uid);
var wantedKey = String($calendar).trim().toLowerCase();
var startAt = $start;
var endAt = $end;
var out = { found: 0, changed: 0, error: null };
var calendars = app.calendars();
for (var i = 0; i < calendars.length; i++) {
  var calName = '';
  try { calName = calendars[i].name(); } catch (nameErr) { calName = ''; }
  calName = String(calName);
  if (wantedKey && calName.trim().toLowerCase() !== wantedKey) { continue; }
  var evs = null;
  try { evs = calendars[i].events.whose({ uid: uidKey })(); } catch (qErr) {
    if (!out.error) { out.error = String(qErr); }
    continue;
  }
  for (var j = 0; j < evs.length; j++) {
    var ev = evs[j];
    try {
      ev.startDate = startAt;
      ev.endDate = endAt;
      out.found = out.found + 1;
      out.changed = out.changed + 1;
    } catch (setErr) {
      if (!out.error) { out.error = String(setErr); }
    }
  }
}
JSON.stringify(out);
""")

_DELETE_EVENT_JS = string.Template("""
var app = Application('Calendar');
var uidKey = String($uid);
var wantedKey = String($calendar).trim().toLowerCase();
var out = { found: 0, deleted: 0, error: null };
var calendars = app.calendars();
for (var i = 0; i < calendars.length; i++) {
  var calName = '';
  try { calName = calendars[i].name(); } catch (nameErr) { calName = ''; }
  calName = String(calName);
  if (wantedKey && calName.trim().toLowerCase() !== wantedKey) { continue; }
  var evs = null;
  try { evs = calendars[i].events.whose({ uid: uidKey })(); } catch (qErr) {
    if (!out.error) { out.error = String(qErr); }
    continue;
  }
  for (var j = 0; j < evs.length; j++) {
    var ev = evs[j];
    out.found = out.found + 1;
    try {
      app.delete(ev);
      out.deleted = out.deleted + 1;
    } catch (delErr) {
      if (!out.error) { out.error = String(delErr); }
    }
  }
}
JSON.stringify(out);
""")


# --------------------------------------------------------------- the week read
def _read_week_payload(start, end, runner=None):
    """One osascript call -> the raw {calendars, events, errors} payload."""
    scan = start - timedelta(days=LOOKBACK_DAYS)
    script = _READ_EVENTS_JS.substitute(
        from_y=start.year, from_m=start.month - 1, from_d=start.day,
        scan_y=scan.year, scan_m=scan.month - 1, scan_d=scan.day,
        to_y=end.year, to_m=end.month - 1, to_d=end.day)
    raw = _run_js(script, timeout=READ_TIMEOUT, runner=runner)
    payload = _parse_json(raw, "week_events")
    if isinstance(payload, list):  # tolerate a bare event array
        payload = {"calendars": [], "events": payload}
    if not isinstance(payload, dict):
        raise CalendarError("unexpected week_events payload: %r" % (payload,))
    return payload


def _payload_calendars(payload):
    names, seen = [], set()
    for raw in payload.get("calendars") or []:
        name = str(raw or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def _payload_readable(payload, calendars):
    readable = payload.get("readable")
    errors = payload.get("errors") or []
    if readable is None:
        return max(0, len(calendars) - len(errors))
    try:
        return int(readable)
    except (TypeError, ValueError):
        return max(0, len(calendars) - len(errors))


def _select_calendars(all_calendars, calendar_names):
    """Pick the calendars to read; returns (selected, note|None)."""
    requested = _normalize_calendar_names(calendar_names)
    if requested:
        wanted = {name.lower() for name in requested}
        matched = [name for name in all_calendars if name.lower() in wanted]
        if matched:
            return matched, None
        if not all_calendars:
            return [], None  # nothing to fall back to; an empty week, not an error
        # A renamed/typo'd calendar must not make the planner think the week is
        # empty: fall back to every calendar (minus noise) and say so.
        selected = [name for name in all_calendars
                    if not _is_noise_calendar(name)]
        return selected, (
            "no calendar named %s; read %d calendar(s) instead (%s)"
            % (", ".join(repr(n) for n in requested), len(selected),
               ", ".join(selected) or "none"))

    selected = [name for name in all_calendars if not _is_noise_calendar(name)]
    if all_calendars and not selected:
        return selected, (
            "every calendar (%s) was filtered as noise (Birthdays / Holidays / "
            "Siri Suggestions); pass calendar_names to read one anyway"
            % ", ".join(all_calendars))
    return selected, None


def _shape_week(payload, start, end, calendar_names):
    """Payload -> (records, calendars, error).  Shared by every reader.

    A record holds the public event fields plus `end_date`, the private date of
    the end clock.  That is what lets a three-day event be clipped to the right
    days instead of being guessed from `end < start`.
    """
    all_calendars = _payload_calendars(payload)
    selected, note = _select_calendars(all_calendars, calendar_names)
    notes = [note] if note else []

    # A payload without a calendar list (or a fake one) must not be filtered to
    # death: only filter when we actually know the calendar names.
    allowed = {name.lower() for name in selected} if all_calendars else None

    errors = [item for item in (payload.get("errors") or [])
              if isinstance(item, dict)]
    readable = _payload_readable(payload, all_calendars)

    if all_calendars and readable == 0 and errors:
        detail = "; ".join(
            "%s: %s" % (str(item.get("calendar") or "(unnamed)"),
                        str(item.get("error") or "").strip() or "no detail")
            for item in errors)
        return [], selected, (
            "Apple Calendar returned no events from any of its %d calendar(s) "
            "(%s).\n%s" % (len(all_calendars), detail, PERMISSION_HINT))

    for item in errors:
        cal = str(item.get("calendar") or "").strip()
        if allowed is not None and cal and cal.lower() not in allowed:
            continue
        notes.append("%s could not be read: %s"
                     % (cal or "(unnamed)",
                        str(item.get("error") or "").strip() or "unknown error"))

    from_key = (start.isoformat(), 0)
    to_key = (end.isoformat(), 24 * 60)
    events, seen = [], set()
    for raw in payload.get("events") or []:
        if not isinstance(raw, dict):
            continue
        cal = str(raw.get("calendar") or "").strip()
        if allowed is not None and cal and cal.lower() not in allowed:
            continue
        start_at = _parse_stamp(raw.get("start"))
        end_at = _parse_stamp(raw.get("end"))
        if not start_at or not end_at:
            continue
        if end_at <= start_at:  # 24h event, or an end that lost its date
            end_at = _next_day(end_at)
        if end_at <= from_key or start_at >= to_key:
            continue  # no overlap with the requested range

        all_day = raw.get("allDay")
        if all_day is None:
            # Older payloads / an alldayEvent getter that is not exposed:
            # a midnight-to-midnight span of whole days is an all-day event.
            all_day = (start_at[1] == 0 and end_at[1] == 0
                       and end_at[0] > start_at[0])
        else:
            all_day = bool(all_day)

        title = str(raw.get("title") or "")
        location = str(raw.get("location") or "")
        uid = str(raw.get("id") or "").strip()
        key = (cal.lower(), uid) if uid else (cal.lower(), title,
                                             start_at, end_at)
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "id": uid or "%s|%s|%s" % (cal or "-", _stamp_text(start_at),
                                       _stamp_text(end_at)),
            "date": start_at[0],
            "end_date": end_at[0],
            "start": fmt_hhmm(start_at[1]),
            "end": fmt_hhmm(end_at[1]),
            "title": title,
            "calendar": cal,
            "all_day": bool(all_day),
            "location": location,
        })

    events.sort(key=lambda ev: (ev["date"], ev["start"], ev["end"],
                                ev["title"], ev["calendar"]))
    if notes:
        return events, selected, "partial: " + "; ".join(notes)
    return events, selected, None


EVENT_FIELDS = ("id", "date", "start", "end", "title", "calendar",
                "all_day", "location")


_READ_WEEK_AS = """
on run argv
	set isoFrom to item 1 of argv
	set isoTo to item 2 of argv
	set wanted to item 3 of argv

	set fromDate to my makeDate(isoFrom)
	set toDate to my makeDate(isoTo)
	set outLines to {}
	tell application id "com.apple.iCal"
		repeat with c in (every calendar)
			set nm to name of c
			if wanted is "" or wanted contains nm then
				set evs to (every event of c whose start date ≥ fromDate and start date < toDate)
				repeat with e in evs
					set end of outLines to nm & tab & (summary of e) & tab & ¬
						((start date of e) as «class isot» as string) & tab & ¬
						((end date of e) as «class isot» as string) & tab & ¬
						(allday event of e as string)
				end repeat
			end if
		end repeat
	end tell
	set AppleScript's text item delimiters to linefeed
	return (outLines as string)
end run

on makeDate(iso)
	set y to (text 1 thru 4 of iso) as integer
	set m to (text 6 thru 7 of iso) as integer
	set d to (text 9 thru 10 of iso) as integer
	set dt to current date
	set day of dt to 1
	set year of dt to y
	set month of dt to m
	set day of dt to d
	set time of dt to 0
	return dt
end makeDate
"""


def _spans_a_whole_day(start_hm, end_hm, start_day, end_day):
    """A '00:00 -> 23:59' event is a calendar annotation, not time to protect."""
    try:
        minutes = (parse_hhmm(end_hm) - parse_hhmm(start_hm))
    except (TypeError, ValueError):
        return False
    if end_day != start_day:
        minutes += 24 * 60
    return minutes >= 20 * 60


def _iso_minutes(stamp):
    """"2026-10-27T17:30:00" -> ("2026-10-27", "17:30"). AppleScript gives
    local wall-clock time, which is exactly what the planner schedules in."""
    stamp = (stamp or "").strip()
    if len(stamp) < 16 or "T" not in stamp:
        return None, None
    day, _, clock = stamp.partition("T")
    return day[:10], clock[:5]


def _read_week_apple(start, end, calendar_names, runner=None):
    """One AppleScript pass. ~1.3 s per calendar, much faster than JXA whose."""
    names = []
    if isinstance(calendar_names, str):
        names = [calendar_names]
    elif calendar_names:
        names = list(calendar_names)
    wanted = "\n".join(n for n in names if not _is_noise_calendar(n)) if names else ""
    # exclusive end: the day after the last requested day
    to_date = (end + timedelta(days=1)).isoformat()
    script = _READ_WEEK_AS
    argv = [OSASCRIPT, "-e", script, start.isoformat(), to_date, wanted]
    proc = (runner or _default_runner)(
        argv, capture_output=True, text=True, timeout=READ_TIMEOUT)
    out = (getattr(proc, "stdout", "") or "").strip()
    if getattr(proc, "returncode", 0) != 0:
        err = (getattr(proc, "stderr", "") or "").strip()
        raise CalendarError(err or "osascript failed reading Calendar")

    records, seen = [], set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        cal, title, s_iso, e_iso, all_day = parts[0], parts[1], parts[2], parts[3], parts[4]
        start_day, start_hm = _iso_minutes(s_iso)
        end_day, end_hm = _iso_minutes(e_iso)
        if not start_day or not end_day:
            continue
        key = (cal, title, s_iso, e_iso)
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "id": "|".join(key),
            "date": start_day,
            "end_date": end_day,
            "start": start_hm,
            "end": end_hm,
            "title": title,
            "calendar": cal,
            "all_day": (all_day.strip().lower() == "true"
                        or _spans_a_whole_day(start_hm, end_hm, start_day, end_day)),
            "location": "",
        })
    if not names:                  # "everything" still means everything useful
        records = [r for r in records if not _is_noise_calendar(r["calendar"])]
    records.sort(key=lambda r: (r["date"], r["start"]))
    calendars = sorted({r["calendar"] for r in records})
    return records, calendars, None


def _read_records(start, end, calendar_names, runner=None):
    """Read + shape one week; never raises -> (records, calendars, error)."""
    if runner is None:             # production path; tests inject a fake runner
        try:
            return _read_week_apple(start, end, calendar_names, runner=runner)
        except Exception:
            pass                   # fall through to the JXA implementation
    try:
        payload = _read_week_payload(start, end, runner=runner)
        return _shape_week(payload, start, end, calendar_names)
    except Exception as exc:  # permission, parse, unreadable payload
        return [], [], _error_text(exc)


def week_events(start_date, end_date, calendar_names=None, runner=None):
    """Every event overlapping [start_date, end_date] — ONE osascript call.

    Returns {"events": [...], "calendars": [...], "error": str|None}:

      id        Calendar uid, or "<calendar>|<start>|<end>" when Calendar.app
                hands none back (stable across reads either way)
      date      the event's real start date, "YYYY-MM-DD".  An event that
                starts before `start_date` but runs into the range keeps its
                true date — that is how a Sunday-night slot stays visible.
      start/end "HH:MM" wall clock.  `end` < `start` means the event crosses
                midnight (week_busy clips those; week_events reports them raw).
      all_day   bool; true also when inferred from a midnight-to-midnight span.
      calendar  the calendar the event lives in.

    Noise calendars are excluded unless named in `calendar_names` (a string or
    an iterable).  If none of the requested names exist, every non-noise
    calendar is read and `error` explains the substitution.

    Never raises: a denied Automation grant, a missing osascript or an empty
    week all come back as data.  `error` may be set while `events` is non-empty
    (prefix "partial: ") — the events that were read are still usable.
    """
    try:
        start = _as_date(start_date)
        end = _as_date(end_date)
        if end < start:
            raise ValueError(
                "end_date %s is before start_date %s" % (end, start))
    except Exception as exc:  # bad input — still data, never an exception
        return {"events": [], "calendars": [], "error": _error_text(exc)}

    records, calendars, error = _read_records(start, end, calendar_names,
                                              runner=runner)
    events = [{key: rec.get(key) for key in EVENT_FIELDS} for rec in records]
    return {"events": events, "calendars": calendars, "error": error}


def week_busy(start_date, end_date, calendar_names=None, ignore_all_day=True,
              runner=None):
    """Busy intervals per day for the scheduler — ONE osascript call.

    {"busy": {"YYYY-MM-DD": [["HH:MM", "HH:MM"], ...]}, "error": str|None}

    Every day in the range is present (empty list when free), in date order.
    Intervals are clipped to the day (`"24:00"` = midnight at the end of that
    day, the only honest way to say "until midnight"), overlapping blocks are
    merged, and all-day events are excluded when ignore_all_day is true.
    On failure `busy` is all-empty and `error` says why — never mistake a
    permission error for a free week.
    """
    try:
        start = _as_date(start_date)
        end = _as_date(end_date)
        if end < start:
            raise ValueError(
                "end_date %s is before start_date %s" % (end, start))
        records, _calendars, error = _read_records(start, end, calendar_names,
                                                   runner=runner)

        days, cursor = {}, start
        while cursor <= end:
            days[cursor.isoformat()] = []
            cursor += timedelta(days=1)

        for ev in records:
            if ignore_all_day and ev["all_day"]:
                continue
            day0 = date.fromisoformat(ev["date"])
            end_day = date.fromisoformat(ev["end_date"])
            start_min = parse_hhmm(ev["start"])
            end_min = parse_hhmm(ev["end"])

            cursor = max(day0, start)
            last = min(end_day, end)
            while cursor <= last:
                day_start = 0 if cursor > day0 else start_min
                day_end = 24 * 60 if cursor < end_day else end_min
                if day_end > day_start:
                    days[cursor.isoformat()].append(
                        (max(0, day_start), min(24 * 60, day_end)))
                cursor += timedelta(days=1)

        busy = {}
        for key, spans in days.items():
            busy[key] = [[fmt_hhmm(s), fmt_hhmm(e)]
                         for s, e in _merge_spans(spans)]
        return {"busy": busy, "error": error}
    except Exception as exc:
        return {"busy": {}, "error": _error_text(exc)}


def read_busy(week_start, week_end, calendar_name, runner=None):
    """Legacy flat list: [{"title", "start": "YYYY-MM-DDTHH:MM", "end": ...}].

    Sorted by start, duplicates (the same meeting on two calendars) reported
    once.  Events overlapping the range are included, so an event starting the
    previous evening is clipped into the week by busy_to_minutes().  Raises
    CalendarError when nothing could be read, because cli.py prints the message.
    """
    start = _as_date(week_start)
    end = _as_date(week_end)
    if end < start:
        raise ValueError("week_end %s is before week_start %s" % (end, start))

    records, _calendars, error = _read_records(start, end, calendar_name,
                                               runner=runner)
    if error and not records and not error.startswith("partial: "):
        raise CalendarError(error)

    events, seen = [], set()
    for ev in records:
        start_iso = "%sT%s" % (ev["date"], ev["start"])
        end_iso = "%sT%s" % (ev["end_date"], ev["end"])
        key = (ev["title"], start_iso, end_iso)
        if key in seen:
            continue
        seen.add(key)
        events.append({"title": ev["title"], "start": start_iso, "end": end_iso})
    events.sort(key=lambda ev: (ev["start"], ev["end"], ev["title"]))
    return events


# ------------------------------------------------------------- access & listing
def list_calendars(runner=None):
    """Names of every calendar in Calendar.app, in Calendar's own order.

    Raises CalendarError when Calendar.app cannot be reached — use
    check_access() for a non-raising probe.
    """
    payload = _parse_json(_run_js(_LIST_CALENDARS_JS.substitute(), runner=runner),
                          "list_calendars")
    if isinstance(payload, dict):
        payload = payload.get("calendars") or []
    if isinstance(payload, str):
        payload = [payload]
    names, seen = [], set()
    for item in payload or []:
        name = str(item or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def check_access(runner=None):
    """Can this machine really read Calendar.app?  Never raises.

    {"ok": bool, "detail": str, "calendars": [str]}

    ok is true only when osascript can both list calendars and run an event
    query (the macOS 14+ Calendars grant is separate from Automation).  The
    detail is written for the UI banner: on failure it names the privacy pane
    and the permission-free `plan export --ics` fallback.
    """
    try:
        raw = _run_js(_CHECK_ACCESS_JS.substitute(), runner=runner)
        payload = _parse_json(raw, "check_access")
    except Exception as exc:
        return {"ok": False, "detail": _error_text(exc), "calendars": []}

    if isinstance(payload, list):
        payload = {"calendars": payload}
    if not payload:
        return {"ok": False,
                "detail": ("osascript returned no output while asking "
                           "Calendar.app for its calendars.\n%s"
                           % PERMISSION_HINT),
                "calendars": []}
    if not isinstance(payload, dict):
        return {"ok": False,
                "detail": "unexpected check_access output: %r" % (payload,),
                "calendars": []}

    calendars, seen = [], set()
    for item in payload.get("calendars") or []:
        name = str(item or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            calendars.append(name)

    probe = payload.get("probe")
    if probe is None:  # fake/older payload: assume the query worked
        probe = True
    if not probe:
        return {"ok": False,
                "detail": ("osascript can list %d calendar(s) but reading "
                           "events failed: %s\n%s"
                           % (len(calendars),
                              str(payload.get("error") or "no detail").strip(),
                              PERMISSION_HINT)),
                "calendars": calendars}
    if not calendars:
        return {"ok": True,
                "detail": ("Calendar.app answered but has no calendars — "
                           "create one (File > New Calendar) or enable iCloud "
                           "calendars. Nothing to schedule around yet."),
                "calendars": []}

    noise = [name for name in calendars if _is_noise_calendar(name)]
    detail = "Calendar.app reachable: %d calendar(s), events readable." % len(calendars)
    if noise:
        detail += (" %d filtered as noise by default (%s)."
                   % (len(noise), ", ".join(noise)))
    return {"ok": True, "detail": detail, "calendars": calendars}


#: The Habits mirror owns its calendar: when it was wiped in Calendar.app (or
#: never existed), the mirror heals itself by re-creating it instead of
#: failing forever.  Idempotent: an existing calendar is left alone.
_ENSURE_CALENDAR_JS = string.Template("""
var app = Application('Calendar');
var wanted = $calendar;
var wantedKey = String(wanted).trim().toLowerCase();
var calendars = app.calendars();
var found = null;
for (var i = 0; i < calendars.length; i++) {
  var calName = '';
  try { calName = calendars[i].name(); } catch (err) { calName = ''; }
  if (found === null && String(calName).trim().toLowerCase() === wantedKey) {
    found = calendars[i];
  }
}
if (found !== null) {
  JSON.stringify({ exists: true, created: false });
} else {
  app.make({ new: app.Calendar, withProperties: { name: wanted } });
  JSON.stringify({ exists: true, created: true });
}
""")


def ensure_calendar(name, runner=None):
    """Make sure a calendar named `name` exists, creating it if needed.

    Never raises; True when the calendar exists afterwards.  One osascript,
    so callers only pay for it on the healing path (after a write failed).
    """
    if not str(name or "").strip():
        return False
    script = _ENSURE_CALENDAR_JS.substitute(calendar=_js_str(name))
    try:
        payload = _parse_json(_run_js(script, runner=runner), "ensure_calendar")
    except Exception:
        return False
    return bool(isinstance(payload, dict) and payload.get("exists"))


# ----------------------------------------------------------------- write_event
def write_event(summary, start_iso, end_iso, calendar_name,
                description="", runner=None):
    """Create one event in `calendar_name`; return its uid (or None).

    The calendar must already exist — we do not silently create calendars, so a
    typo cannot litter Calendar.app.  A missing calendar raises CalendarError
    listing the calendars that do exist.
    """
    start = _as_datetime(start_iso)
    end = _as_datetime(end_iso)
    if end < start:
        raise CalendarError(
            "event end %s is before start %s" % (end_iso, start_iso))
    if not str(calendar_name or "").strip():
        raise CalendarError("write_event needs a calendar name")

    script = _WRITE_EVENT_JS.substitute(
        calendar=_js_str(calendar_name),
        summary=_js_str(summary),
        description=_js_str(description or ""),
        start=_js_date(start),
        end=_js_date(end))
    payload = _parse_json(_run_js(script, runner=runner), "write_event")

    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None
    if payload.get("error") == "CALENDAR_NOT_FOUND":
        have = [str(c) for c in (payload.get("calendars") or []) if str(c)]
        raise CalendarError(
            "Apple Calendar has no calendar named %r (available: %s). "
            "Create it in Calendar.app (File > New Calendar) or pass "
            "--calendar with an existing name."
            % (calendar_name, ", ".join(have) or "none"))
    uid = payload.get("uid")
    return str(uid) if uid else None


# ------------------------------------------------------- update / delete event
def _event_mutation_payload(raw, what):
    if isinstance(raw, dict):
        return raw
    return {"found": 0, "changed": 0, "deleted": 0,
            "error": "unexpected %s output" % what}


def update_event_times(uid, start_iso, end_iso, calendar_name=None,
                       runner=None):
    """Best-effort: move one existing Calendar event to new times.

    Looks the event up by uid (scoped to `calendar_name` when given — the
    planner only creates events there). Never raises: {"found", "changed",
    "error"} comes back as data, because a denied Automation grant or a
    renamed calendar must not break an otherwise-finished slot move.
    """
    try:
        start = _as_datetime(start_iso)
        end = _as_datetime(end_iso)
    except Exception as exc:
        return {"found": 0, "changed": 0, "error": _error_text(exc)}
    if end < start:
        return {"found": 0, "changed": 0, "error": "end is before start"}
    script = _UPDATE_EVENT_JS.substitute(
        uid=_js_str(uid), calendar=_js_str(calendar_name or ""),
        start=_js_date(start), end=_js_date(end))
    try:
        raw = _run_js(script, runner=runner)
    except Exception as exc:
        return {"found": 0, "changed": 0, "error": _error_text(exc)}
    return _event_mutation_payload(_parse_json(raw, "update_event_times"),
                                   "update_event_times")


def delete_event(uid, calendar_name=None, runner=None):
    """Best-effort: delete one existing Calendar event by uid.

    Same contract as update_event_times(): {"found", "deleted", "error"},
    never raises. `found > 0` with `deleted == 0` means Calendar kept the
    event (permission or sync problem) — the caller should say so.
    """
    script = _DELETE_EVENT_JS.substitute(uid=_js_str(uid),
                                         calendar=_js_str(calendar_name or ""))
    try:
        raw = _run_js(script, runner=runner)
    except Exception as exc:
        return {"found": 0, "deleted": 0, "error": _error_text(exc)}
    return _event_mutation_payload(_parse_json(raw, "delete_event"),
                                   "delete_event")


# ------------------------------------------------------------------ export_ics
def _ics_escape(value):
    """RFC 5545 TEXT escaping: backslash, semicolon, comma, newline."""
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\n", "\\n")


def _fold(line, limit=75):
    """Fold a content line to <= `limit` octets (RFC 5545 §3.1)."""
    if len(line.encode("utf-8")) <= limit:
        return line
    chunks, current, size = [], "", 0
    for ch in line:
        width = len(ch.encode("utf-8"))
        if size + width > limit:
            chunks.append(current)
            current, size = " " + ch, 1 + width  # continuation space counts
        else:
            current += ch
            size += width
    chunks.append(current)
    return "\r\n".join(chunks)


def _lookup_task(tasks_by_id, task_id):
    if not tasks_by_id:
        return {}
    for key in (task_id, str(task_id), _maybe_int(task_id)):
        if key is None:
            continue
        task = tasks_by_id.get(key)
        if isinstance(task, dict):
            return task
    return {}


def _maybe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def export_ics(slots, tasks_by_id, path, calendar_name="Thesis", tzid=None,
               stamp=None, cfg=None):
    """Write the plan as an RFC 5545 .ics file; return the path written.

    One VEVENT per slot, UID `planner-<date>-<task id>-<start>`, floating local
    DTSTART/DTEND (or TZID=<tzid> if given), SUMMARY "<category>: <task title>"
    (see event_summary), DESCRIPTION "<rationale>\\nTask #<id>".  Works with no
    Automation permission, so it is the fallback when Calendar.app access is
    denied.
    """
    if not slots:
        slots = []
    stamp_dt = stamp or datetime.utcnow()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//thesis-planner//calendar_mac//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:%s" % _ics_escape(calendar_name or "Thesis"),
    ]
    if tzid:
        lines.append("X-WR-TIMEZONE:%s" % _ics_escape(tzid))

    for index, slot in enumerate(slots):
        if not isinstance(slot, dict):
            raise ValueError("slot #%d is not a dict: %r" % (index, slot))
        for required in ("date", "start", "end"):
            if not slot.get(required):
                raise ValueError(
                    "slot #%d is missing %r: %r" % (index, required, slot))

        day = _as_date(slot["date"])
        start_min = _as_minutes(slot["start"])
        end_min = _as_minutes(slot["end"])
        midnight = datetime(day.year, day.month, day.day)
        start_dt = midnight + timedelta(minutes=start_min)
        end_dt = midnight + timedelta(minutes=end_min)
        if end_dt < start_dt:  # slot running past midnight
            end_dt += timedelta(days=1)

        task_id = slot.get("task_id")
        task = _lookup_task(tasks_by_id, task_id)
        if not task:
            task = {"title": "Task #%s" % (task_id,)}
        rationale = str(slot.get("rationale") or "").strip()
        description = ("%s\nTask #%s" % (rationale, task_id)) if rationale \
            else "Task #%s" % (task_id,)

        start_text = start_dt.strftime("%Y%m%dT%H%M%S")
        end_text = end_dt.strftime("%Y%m%dT%H%M%S")
        if tzid:
            dtstart = "DTSTART;TZID=%s:%s" % (tzid, start_text)
            dtend = "DTEND;TZID=%s:%s" % (tzid, end_text)
        else:
            dtstart = "DTSTART:%s" % start_text
            dtend = "DTEND:%s" % end_text

        lines.extend([
            "BEGIN:VEVENT",
            "UID:planner-%s-%s-%s" % (day.isoformat(), task_id,
                                      fmt_hhmm(start_min)),
            "DTSTAMP:%s" % stamp_dt.strftime("%Y%m%dT%H%M%SZ"),
            dtstart,
            dtend,
            "SUMMARY:%s" % _ics_escape(event_summary(task, cfg)),
            "DESCRIPTION:%s" % _ics_escape(description),
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")

    body = "\r\n".join(_fold(line) for line in lines) + "\r\n"
    target = str(path)
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(body)
    return target


# ------------------------------------------------------------- busy_to_minutes
def busy_to_minutes(events, day_iso):
    """Clip read_busy() events to one day and return merged (start, end) minutes.

    Events spanning several days are clipped to [0, 1440]; events outside the
    day are dropped; overlapping intervals are merged so the scheduler sees one
    busy block instead of fragments.
    """
    day = _as_date(day_iso)
    key = day.isoformat()
    spans = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        start_at = _parse_stamp(event.get("start"))
        end_at = _parse_stamp(event.get("end"))
        if not start_at or not end_at:
            continue
        start_day, start_min = start_at
        end_day, end_min = end_at
        if end_day < key or start_day > key:
            continue
        if start_day != key:
            start_min = 0
        if end_day != key:
            end_min = 24 * 60
        start_min = max(0, min(24 * 60, start_min))
        end_min = max(0, min(24 * 60, end_min))
        if end_min <= start_min:  # e.g. an event ending exactly at midnight
            continue
        spans.append((start_min, end_min))
    return _merge_spans(spans)


# ------------------------------------------------------------------- self-check
def _main():  # pragma: no cover - manual verification helper
    report = check_access()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not report["ok"]:
        return 1
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    week = week_events(monday, monday + timedelta(days=6))
    print("week %s..%s: %d event(s), calendars=%s, error=%s"
          % (monday, monday + timedelta(days=6), len(week["events"]),
             ", ".join(week["calendars"]) or "-", week["error"]))
    for ev in week["events"][:5]:
        print("  %s %s-%s  %s  [%s]%s"
              % (ev["date"], ev["start"], ev["end"], ev["title"] or "(no title)",
                 ev["calendar"], "  (all day)" if ev["all_day"] else ""))
    busy = week_busy(monday, monday + timedelta(days=6))
    for day_key in sorted(busy["busy"]):
        print("  busy %s: %s" % (day_key, busy["busy"][day_key]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
