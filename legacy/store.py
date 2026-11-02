"""
store.py — SQLite persistence for the thesis planner.

Single file: planner/data/planner.db (gitignored).
Tables: tasks, events (append-only log), plans, slots, history.

All methods return plain dicts (sqlite3.Row -> dict) so the CLI and the web UI
can share them without an ORM.
"""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    notes       TEXT DEFAULT '',
    area        TEXT DEFAULT '',
    status      TEXT DEFAULT 'todo',        -- todo|doing|done|blocked|dropped
    priority    INTEGER DEFAULT 2,          -- 1 (high) .. 3 (low)
    complexity  TEXT DEFAULT 'M',           -- S|M|L (or 1..5)
    est_minutes INTEGER DEFAULT 60,
    deadline    TEXT,                       -- YYYY-MM-DD (usually the PI meeting)
    files       TEXT DEFAULT '',            -- comma-separated paths/globs
    est_source  TEXT,                       -- agent_llm|agent_heuristic|manual
    est_rationale TEXT DEFAULT '',
    deep_work   INTEGER DEFAULT 0,          -- agent: needs uninterrupted focus
    subtasks    TEXT DEFAULT '[]',          -- JSON list of {title, est_minutes}
    confidence  REAL,
    agent_updated_at TEXT,
    actual_minutes INTEGER,                 -- reported by the user after finishing
    feedback_note  TEXT DEFAULT '',
    feedback_at    TEXT,
    within_weeks  INTEGER,                  -- planner may place in the next N weeks
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    task_id INTEGER,
    payload TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    week       TEXT NOT NULL,               -- ISO date of the Monday
    created_at TEXT NOT NULL,
    note       TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS slots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id           INTEGER NOT NULL,
    task_id           INTEGER NOT NULL,
    date              TEXT NOT NULL,        -- YYYY-MM-DD
    start             TEXT NOT NULL,        -- HH:MM
    end               TEXT NOT NULL,
    minutes           INTEGER NOT NULL,
    rationale         TEXT DEFAULT '',
    subtask           TEXT DEFAULT '',      -- which agent subtask this block covers
    pinned            INTEGER DEFAULT 0,    -- user moved this slot by hand
    calendar_event_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_slots_date ON slots(date);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS history (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    date   TEXT NOT NULL,                   -- YYYY-MM-DD
    kind   TEXT NOT NULL,                   -- commit|audit|run|note|milestone
    title  TEXT NOT NULL,
    detail TEXT DEFAULT '',
    source TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_date ON history(date);
CREATE TABLE IF NOT EXISTS habits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    weekdays   TEXT NOT NULL,               -- JSON list, 0=Mon .. 6=Sun
    start      TEXT DEFAULT '',             -- fixed window start "07:30"
    end        TEXT DEFAULT '',             -- fixed window end   "08:00"
    minutes    INTEGER NOT NULL,            -- duration (flexible habits)
    prefer     TEXT DEFAULT 'day',          -- flexible anchor: morning|day|evening
    active     INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS habit_slots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id         INTEGER NOT NULL,
    date             TEXT NOT NULL,         -- YYYY-MM-DD
    start            TEXT NOT NULL,         -- HH:MM
    end              TEXT NOT NULL,         -- HH:MM
    minutes          INTEGER NOT NULL,
    done             INTEGER DEFAULT 0,
    skipped          INTEGER DEFAULT 0,
    calendar_event_id TEXT,
    mirrored_span    TEXT DEFAULT '',       -- "start|end" last written to Apple Calendar
    UNIQUE(habit_id, date)
);
CREATE INDEX IF NOT EXISTS idx_habit_slots_date ON habit_slots(date);
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


def _fmt_hhmm(minutes):
    return "%02d:%02d" % (int(minutes) // 60, int(minutes) % 60)


class Store:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Additive column migrations for databases created by older versions."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        for col, ddl in (("est_source", "TEXT"), ("est_rationale", "TEXT DEFAULT ''"),
                         ("subtasks", "TEXT DEFAULT '[]'"), ("confidence", "REAL"),
                         ("agent_updated_at", "TEXT"),
                         ("deep_work", "INTEGER DEFAULT 0"),
                         ("actual_minutes", "INTEGER"),
                         ("feedback_note", "TEXT DEFAULT ''"),
                         ("feedback_at", "TEXT"),
                         ("within_weeks", "INTEGER")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
        slot_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(slots)")}
        if "subtask" not in slot_cols:
            self.conn.execute("ALTER TABLE slots ADD COLUMN subtask TEXT DEFAULT ''")
        if "pinned" not in slot_cols:
            self.conn.execute("ALTER TABLE slots ADD COLUMN pinned INTEGER DEFAULT 0")
        self.conn.commit()

    # ---------- tasks ----------
    def add_task(self, title, notes="", area="", priority=2, complexity="M",
                 est_minutes=60, deadline=None, files="", deep_work=0,
                 within_weeks=None):
        ts = now()
        cur = self.conn.execute(
            "INSERT INTO tasks (title, notes, area, status, priority, complexity,"
            " est_minutes, deadline, files, deep_work, within_weeks,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title, notes, area, "todo", int(priority), str(complexity),
             int(est_minutes), deadline, files, int(deep_work),
             within_weeks, ts, ts))
        self.conn.commit()
        tid = cur.lastrowid
        self.add_event("task_added", tid, title=title)
        return tid

    def get_task(self, tid):
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, status=None, order="status, deadline IS NULL, deadline, priority, id"):
        q = "SELECT * FROM tasks"
        args = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += f" ORDER BY {order}"
        return [dict(r) for r in self.conn.execute(q, args).fetchall()]

    def update_task(self, tid, **fields):
        allowed = {"title", "notes", "area", "status", "priority", "complexity",
                   "est_minutes", "deadline", "files", "est_source",
                   "est_rationale", "subtasks", "confidence", "agent_updated_at",
                   "deep_work", "actual_minutes", "feedback_note", "feedback_at",
                   "within_weeks"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        sets["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in sets)
        self.conn.execute(f"UPDATE tasks SET {cols} WHERE id=?",
                          (*sets.values(), tid))
        self.conn.commit()
        self.add_event("task_updated", tid, **{k: str(v) for k, v in fields.items()})

    def set_status(self, tid, status, note=""):
        self.update_task(tid, status=status)
        self.add_event(f"task_{status}", tid, note=note)

    # ---------- events ----------
    def add_event(self, kind, task_id=None, **payload):
        self.conn.execute(
            "INSERT INTO events (ts, kind, task_id, payload) VALUES (?,?,?,?)",
            (now(), kind, task_id, json.dumps(payload, ensure_ascii=False)))
        self.conn.commit()

    def recent_events(self, limit=50):
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                pass
            out.append(d)
        return out

    # ---------- plans ----------
    def delete_plan(self, week):
        """Remove every saved plan (and its slots) for a week."""
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM plans WHERE week=?", (week,)).fetchall()]
        for pid in ids:
            self.conn.execute("DELETE FROM slots WHERE plan_id=?", (pid,))
        self.conn.execute("DELETE FROM plans WHERE week=?", (week,))
        self.conn.commit()
        return len(ids)

    def save_plan(self, week, slots, note="", replace=True):
        """Save (replacing by default) the plan for a week; returns the plan id.

        Pinned slots — the ones the user moved by hand — are never deleted by
        a re-plan: they are carried over into the new plan unchanged, so the
        automatic planner can neither erase nor double-book them (callers pass
        them to schedule_week() as busy). The input `slot` dicts get their
        database `id` stamped in, so callers can follow up with calendar
        writes (set_slot_calendar_id()) straight after saving.
        """
        # Re-planning a week must not stack onto the previous plan: stale slots
        # would otherwise be counted as busy time and squeeze the new plan.
        carried = []
        if replace:
            carried = [dict(r) for r in self.conn.execute(
                "SELECT s.* FROM slots s JOIN plans p ON p.id = s.plan_id"
                " WHERE p.week=? AND s.pinned=1 ORDER BY s.date, s.start",
                (week,))]
            self.delete_plan(week)
        ts = now()
        cur = self.conn.execute(
            "INSERT INTO plans (week, created_at, note) VALUES (?,?,?)",
            (week, ts, note))
        pid = cur.lastrowid
        for s in slots:
            cur = self.conn.execute(
                "INSERT INTO slots (plan_id, task_id, date, start, end, minutes,"
                " rationale, subtask, pinned) VALUES (?,?,?,?,?,?,?,?,0)",
                (pid, s["task_id"], s["date"], s["start"], s["end"],
                 int(s["minutes"]), s.get("rationale", ""),
                 s.get("subtask", "")))
            if isinstance(s, dict):
                s["id"] = cur.lastrowid        # caller may need the slot id
        for s in carried:
            # Explicit id: a carried slot keeps the id it had before the
            # re-plan, so the user's drawer row (and its calendar event
            # follow-up) keeps pointing at the same block.
            self.conn.execute(
                "INSERT INTO slots (id, plan_id, task_id, date, start, end,"
                " minutes, rationale, subtask, pinned, calendar_event_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (s["id"], pid, s["task_id"], s["date"], s["start"], s["end"],
                 int(s["minutes"]), s.get("rationale", ""),
                 s.get("subtask", ""), s.get("calendar_event_id")))
        self.conn.commit()
        self.add_event("plan_created", None, week=week,
                       slots=len(slots) + len(carried), pinned=len(carried))
        return pid

    def get_plan(self, week):
        p = self.conn.execute(
            "SELECT * FROM plans WHERE week=? ORDER BY id DESC LIMIT 1",
            (week,)).fetchone()
        if not p:
            return None
        plan = dict(p)
        plan["slots"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM slots WHERE plan_id=? ORDER BY date, start",
            (plan["id"],)).fetchall()]
        return plan

    def set_slot_calendar_id(self, slot_id, event_id):
        self.conn.execute("UPDATE slots SET calendar_event_id=? WHERE id=?",
                          (event_id, slot_id))
        self.conn.commit()

    def get_slot(self, slot_id):
        row = self.conn.execute("SELECT * FROM slots WHERE id=?",
                                (slot_id,)).fetchone()
        return dict(row) if row else None

    def add_slot(self, task_id, date_iso, start, end, minutes=60,
                 rationale="", subtask="", pinned=1, calendar_event_id=None,
                 plan_note="calendar import"):
        """Insert one block outside a (re-)plan — the week's plan row is
        created when absent (note `plan_note`). Used by the Apple Calendar
        import path, where events exist before any plan does. Returns the
        new slot dict."""
        week = self._week_key(date_iso)
        plan = self.get_plan(week)
        if plan:
            pid = plan["id"]
        else:
            cur = self.conn.execute(
                "INSERT INTO plans (week, created_at, note) VALUES (?,?,?)",
                (week, now(), plan_note))
            pid = cur.lastrowid
        cur = self.conn.execute(
            "INSERT INTO slots (plan_id, task_id, date, start, end, minutes,"
            " rationale, subtask, pinned, calendar_event_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, task_id, date_iso, start, end, int(minutes), rationale,
             subtask, int(bool(pinned)), calendar_event_id))
        self.conn.commit()
        return self.get_slot(cur.lastrowid)

    @staticmethod
    def _hhmm_minutes(value):
        """'HH:MM' -> minutes since midnight; raises ValueError on junk."""
        h, m = str(value).split(":")[:2]
        return int(h) * 60 + int(m)

    def _week_key(self, iso_day):
        d = date.fromisoformat(str(iso_day)[:10])
        return (d - timedelta(days=d.weekday())).isoformat()

    def move_slot(self, slot_id, new_date=None, new_start=None, new_end=None):
        """Move one saved block; mark it pinned so re-plans keep it in place.

        Give the new date and/or start and/or end. Giving only one of
        start/end shifts the whole block while keeping its duration (like
        dragging it); giving both resizes it. Moving across a Monday re-homes
        the slot into that week's plan (created, with note "manual move", when
        the week has none yet). The slot keeps its id and its artefacts
        (rationale, subtask, calendar id). Raises LookupError for an unknown
        slot, ValueError for a bad date or an end that does not come after
        the start.
        """
        row = self.get_slot(slot_id)
        if not row:
            raise LookupError("no slot #%s" % slot_id)
        try:
            target_day = date.fromisoformat(str(new_date or row["date"]).strip()[:10])
        except (TypeError, ValueError):
            raise ValueError("date must be YYYY-MM-DD, got %r" % (new_date,))
        target_date = target_day.isoformat()
        old_start = self._hhmm_minutes(row["start"])
        old_end = self._hhmm_minutes(row["end"])
        duration = old_end - old_start
        if new_start is not None and new_end is None:
            end_min = self._hhmm_minutes(new_start) + duration
            start_min = self._hhmm_minutes(new_start)
        elif new_end is not None and new_start is None:
            end_min = self._hhmm_minutes(new_end)
            start_min = end_min - duration
        else:
            start_min = self._hhmm_minutes(new_start if new_start is not None
                                           else row["start"])
            end_min = self._hhmm_minutes(new_end if new_end is not None
                                         else row["end"])
            if end_min <= start_min:
                raise ValueError("end (%s) must be after start (%s)"
                                 % (new_end or row["end"],
                                    new_start or row["start"]))
        if start_min < 0 or end_min > 24 * 60:
            raise ValueError("block %s-%s leaves the day"
                             % (_fmt_hhmm(start_min), _fmt_hhmm(end_min)))
        minutes = end_min - start_min
        start_txt = _fmt_hhmm(start_min)
        end_txt = _fmt_hhmm(end_min)

        old = {"date": row["date"], "start": row["start"], "end": row["end"]}
        current_plan = self.conn.execute(
            "SELECT week FROM plans WHERE id=?", (row["plan_id"],)).fetchone()
        target_week = self._week_key(target_date)
        plan_id = row["plan_id"]
        if current_plan is None or current_plan["week"] != target_week:
            plan = self.get_plan(target_week)
            plan_id = plan["id"] if plan else None
            if plan_id is None:
                plan_id = self.conn.execute(
                    "INSERT INTO plans (week, created_at, note)"
                    " VALUES (?,?,?)",
                    (target_week, now(), "manual move")).lastrowid
                self.add_event("plan_created", None, week=target_week,
                               slots=0, pinned=1, note="manual move")
            self.conn.execute(
                "UPDATE slots SET plan_id=?, date=?, start=?, end=?,"
                " minutes=?, pinned=1 WHERE id=?",
                (plan_id, target_date, start_txt, end_txt, minutes, slot_id))
        else:
            self.conn.execute(
                "UPDATE slots SET date=?, start=?, end=?, minutes=?, pinned=1"
                " WHERE id=?",
                (target_date, start_txt, end_txt, minutes, slot_id))
        self.conn.commit()
        self.add_event("slot_moved", row["task_id"], slot_id=slot_id,
                       frm=f"{old['date']} {old['start']}-{old['end']}",
                       to=f"{target_date} {start_txt}-{end_txt}",
                       minutes=minutes)
        return self.get_slot(slot_id)

    def delete_slot(self, slot_id):
        """Remove one saved block (the task stays); returns the removed row."""
        row = self.get_slot(slot_id)
        if not row:
            raise LookupError("no slot #%s" % slot_id)
        self.conn.execute("DELETE FROM slots WHERE id=?", (slot_id,))
        self.conn.commit()
        self.add_event("slot_removed", row["task_id"], slot_id=slot_id,
                       date=row["date"], start=row["start"], end=row["end"])
        return row

    def pinned_slots_between(self, d1, d2):
        """User-pinned slots in [d1, d2] — busy time for any re-plan."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM slots WHERE pinned=1 AND date BETWEEN ? AND ?"
            " ORDER BY date, start", (d1, d2)).fetchall()]

    def slots_between(self, d1, d2):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM slots WHERE date BETWEEN ? AND ? ORDER BY date, start",
            (d1, d2)).fetchall()]

    def slots_for_task(self, task_id):
        """All saved blocks of one task, any week — the calendar cleanup
        path (drop / delete) needs them regardless of which plan holds them."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM slots WHERE task_id=? ORDER BY date, start",
            (task_id,)).fetchall()]

    def delete_task(self, task_id, note=""):
        """Remove the task and every block scheduled for it. The calendar
        sync itself is the caller's job (needs cfg); the deletion is logged
        in history with the task's identity so nothing vanishes silently.
        The id list is compacted afterwards: every task above the removed
        one (and its slots/events) shifts down by one, so deleted tasks are
        not counted in the task identifier list. Returns the removed task
        row (None if already gone)."""
        row = self.get_task(task_id)
        if not row:
            return None
        for slot in self.slots_for_task(task_id):
            self.delete_slot(slot["id"])
        self.add_event("task_deleted", title=row["title"], area=row["area"],
                       est_minutes=row["est_minutes"], note=note)
        self.conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self._renumber_after_delete(task_id)
        return row

    def _renumber_after_delete(self, removed_id):
        """Compact the task id list: shift ids > removed_id down by one and
        keep every reference (slots, events) pointing at the same task.
        Events of the removed task itself keep their now-dangling id — their
        payload carries the title, so history stays readable. All statements
        join the open implicit transaction; sqlite_sequence is resynced so
        AUTOINCREMENT does not burn the freed number."""
        self.conn.execute("UPDATE slots SET task_id=task_id-1"
                          " WHERE task_id>?", (removed_id,))
        self.conn.execute("UPDATE events SET task_id=task_id-1"
                          " WHERE task_id>?", (removed_id,))
        self.conn.execute("UPDATE tasks SET id=id-1 WHERE id>?",
                          (removed_id,))
        self.conn.execute(
            "UPDATE sqlite_sequence SET seq=(SELECT MAX(id) FROM tasks)"
            " WHERE name='tasks'")
        self.conn.commit()

    def renumber_tasks(self):
        """One-shot compaction of the whole id list (gaps left by older
        deletions). Returns (before_max, after_max)."""
        before = self.conn.execute("SELECT MAX(id) FROM tasks").fetchone()[0]
        for offset, (tid,) in enumerate(
                self.conn.execute("SELECT id FROM tasks ORDER BY id")
                .fetchall(), start=1):
            if tid != offset:
                self.conn.execute("UPDATE slots SET task_id=? WHERE task_id=?",
                                  (offset, tid))
                self.conn.execute("UPDATE events SET task_id=? WHERE task_id=?",
                                  (offset, tid))
                self.conn.execute("UPDATE tasks SET id=? WHERE id=?",
                                  (offset, tid))
        self.conn.execute(
            "UPDATE sqlite_sequence SET seq=(SELECT MAX(id) FROM tasks)"
            " WHERE name='tasks'")
        self.conn.commit()
        after = self.conn.execute("SELECT MAX(id) FROM tasks").fetchone()[0]
        return before, after

    def add_past_task(self, title, area="", est_minutes=60,
                      actual_minutes=None, done_date=None, priority=2,
                      complexity="M", notes=""):
        """Register a task completed *before* this app existed (or outside
        it). The task lands as `done`; its doing→done events are backdated
        around `done_date` (default: today), so the real time counts for
        calibration and history exactly like a task tracked live. Returns
        the task id."""
        tid = self.add_task(title, notes=notes, area=area, priority=priority,
                            complexity=complexity, est_minutes=est_minutes)
        done = done_date or date.today().isoformat()
        actual = int(actual_minutes) if actual_minutes else None
        doing_ts = done
        if actual:
            base = datetime.fromisoformat(done)
            doing_ts = (base - timedelta(minutes=actual)).isoformat(
                timespec="minutes")
        self.conn.execute(
            "INSERT INTO events (ts, kind, task_id, payload) VALUES (?,?,?,?)",
            (doing_ts, "task_doing", tid, "{}"))
        self.conn.execute(
            "INSERT INTO events (ts, kind, task_id, payload) VALUES (?,?,?,?)",
            (done, "task_done", tid,
             json.dumps({"note": "past task (added by hand)"})))
        self.update_task(tid, status="done", actual_minutes=actual)
        self.conn.commit()
        return tid

    # ---------- history ----------
    def add_history(self, date, kind, title, detail="", source=""):
        self.conn.execute(
            "INSERT INTO history (date, kind, title, detail, source) VALUES (?,?,?,?,?)",
            (date, kind, title, detail, source))
        self.conn.commit()

    def list_history(self, date_from=None, date_to=None, limit=2000):
        q = "SELECT * FROM history WHERE 1=1"
        args = []
        if date_from:
            q += " AND date >= ?"
            args.append(date_from)
        if date_to:
            q += " AND date <= ?"
            args.append(date_to)
        q += " ORDER BY date, id LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(q, args).fetchall()]

    # ---------- agent state / calibration ----------
    def kv_get(self, key, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key, value):
        self.conn.execute("INSERT INTO kv (key, value) VALUES (?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, str(value)))
        self.conn.commit()

    # ------------------------------------------------------------ habits
    @staticmethod
    def _habit_row(row):
        h = dict(row)
        try:
            h["weekdays"] = json.loads(h.get("weekdays") or "[]")
        except (ValueError, TypeError):
            h["weekdays"] = []
        return h

    def add_habit(self, name, weekdays, minutes, start="", end="",
                  prefer="day", active=1):
        """A recurring commitment template. weekdays: [0..6], 0 = Monday."""
        ts = now()
        cur = self.conn.execute(
            "INSERT INTO habits (name, weekdays, start, end, minutes, prefer,"
            " active, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(name).strip()[:120], json.dumps(sorted({int(w) for w in
                                                         weekdays})),
             str(start or "").strip(), str(end or "").strip(),
             max(1, int(minutes or 20)), str(prefer or "day"),
             1 if active else 0, ts, ts))
        self.conn.commit()
        return cur.lastrowid

    def get_habit(self, habit_id):
        row = self.conn.execute("SELECT * FROM habits WHERE id=?",
                                (habit_id,)).fetchone()
        return self._habit_row(row) if row else None

    def habits(self, active_only=True):
        sql = "SELECT * FROM habits" + (" WHERE active=1" if active_only else "")
        sql += " ORDER BY name COLLATE NOCASE"
        return [self._habit_row(r) for r in self.conn.execute(sql)]

    def update_habit(self, habit_id, **fields):
        allowed = {"name", "weekdays", "start", "end", "minutes", "prefer",
                   "active"}
        sets, vals = [], []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key == "weekdays":
                value = json.dumps(sorted({int(w) for w in value}))
            elif key == "active":
                value = 1 if value else 0
            elif key == "minutes":
                value = max(1, int(value))
            sets.append("%s=?" % key)
            vals.append(value)
        if not sets:
            return self.get_habit(habit_id)
        sets.append("updated_at=?")
        vals.extend([now(), habit_id])
        self.conn.execute("UPDATE habits SET %s WHERE id=?" % ", ".join(sets),
                          vals)
        self.conn.commit()
        return self.get_habit(habit_id)

    def delete_habit(self, habit_id):
        """Delete the template and ALL its instances, past weeks included.

        "Delete" must mean deleted — no soft state, nothing resurfacing in
        the week view.  Returns the removed slot rows (with their calendar
        event ids) so the caller can drop the mirror events too; the store
        itself never touches Calendar.app.
        """
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM habit_slots WHERE habit_id=?", (habit_id,))]
        self.conn.execute("DELETE FROM habit_slots WHERE habit_id=?",
                          (habit_id,))
        self.conn.execute("DELETE FROM habits WHERE id=?", (habit_id,))
        self.conn.commit()
        return rows

    def set_habit_slots(self, habit_id, spans, window=None):
        """Materialize one habit's instances; preserves done/skipped.

        `spans`: [{"date", "start", "end", "minutes"}]. `window` bounds the
        materialized range: rows inside it that `spans` no longer contain are
        removed (habit deactivated or weekday dropped) and returned so the
        caller can delete their calendar events. Rows outside the window —
        past weeks — are never touched. The event id survives a time change;
        the mirror then updates the event in place.

        Returns {"removed": [old rows...], "added": int, "moved": int} —
        only real deltas, so an idempotent re-materialization reports zeros.
        """
        existing = {r["date"]: r for r in self.conn.execute(
            "SELECT * FROM habit_slots WHERE habit_id=?", (habit_id,))}
        removed, added, moved = [], 0, 0
        keep = set()
        for s in spans:
            date_iso = s["date"]
            keep.add(date_iso)
            old = existing.get(date_iso)
            done = old["done"] if old else 0
            skipped = old["skipped"] if old else 0
            uid = old["calendar_event_id"] if old else None
            if old is None:
                added += 1
            elif (old["start"], old["end"]) != (s["start"], s["end"]):
                moved += 1
            self.conn.execute(
                "INSERT INTO habit_slots (habit_id, date, start, end,"
                " minutes, done, skipped, calendar_event_id, mirrored_span)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(habit_id, date) DO UPDATE SET"
                " start=excluded.start, end=excluded.end,"
                " minutes=excluded.minutes, done=excluded.done,"
                " skipped=excluded.skipped,"
                " calendar_event_id=excluded.calendar_event_id,"
                " mirrored_span=excluded.mirrored_span",
                (habit_id, date_iso, s["start"], s["end"],
                 int(s["minutes"] or 0), done, skipped, uid,
                 old["mirrored_span"] if (old and
                     (old["start"], old["end"]) == (s["start"], s["end"]))
                 else ""))
        for date_iso, old in existing.items():
            if date_iso in keep:
                continue
            if window and not (window[0] <= date_iso <= window[1]):
                continue              # outside the materialized window: history
            removed.append(dict(old))
            self.conn.execute(
                "DELETE FROM habit_slots WHERE id=?", (old["id"],))
        self.conn.commit()
        return {"removed": removed, "added": added, "moved": moved}

    def habit_slots_between(self, d1, d2, active_only=True):
        """Habit instances in [d1, d2], joined with their template name."""
        sql = ("SELECT hs.*, h.name, h.active FROM habit_slots hs"
               " JOIN habits h ON h.id = hs.habit_id"
               " WHERE hs.date BETWEEN ? AND ?")
        if active_only:
            sql += " AND h.active=1"
        sql += " ORDER BY hs.date, hs.start"
        return [dict(r) for r in self.conn.execute(sql, (d1, d2))]

    def habit_slot(self, slot_id):
        row = self.conn.execute(
            "SELECT hs.*, h.name, h.active FROM habit_slots hs"
            " JOIN habits h ON h.id = hs.habit_id WHERE hs.id=?",
            (slot_id,)).fetchone()
        return dict(row) if row else None

    def set_habit_slot_state(self, slot_id, done=None, skipped=None):
        """Mark one instance done or skipped (mutually exclusive states)."""
        row = self.habit_slot(slot_id)
        if not row:
            return None
        if done is not None:
            self.conn.execute(
                "UPDATE habit_slots SET done=?, skipped=0 WHERE id=?",
                (1 if done else 0, slot_id))
        if skipped is not None:
            self.conn.execute(
                "UPDATE habit_slots SET skipped=?, done=0 WHERE id=?",
                (1 if skipped else 0, slot_id))
        self.conn.commit()
        return self.habit_slot(slot_id)

    def mark_habit_mirrored(self, slot_id, span, uid=None):
        """Stamp what the mirror last wrote, so unchanged spans skip osascript."""
        if uid:
            self.conn.execute(
                "UPDATE habit_slots SET calendar_event_id=?, mirrored_span=?"
                " WHERE id=?", (uid, span, slot_id))
        else:
            self.conn.execute(
                "UPDATE habit_slots SET mirrored_span=? WHERE id=?",
                (span, slot_id))
        self.conn.commit()

    def clear_habit_mirror(self, slot_id):
        """Forget the mirror stamps: the calendar lost this event (the
        calendar was wiped or the event deleted by hand), so the next
        mirror pass re-creates it from scratch instead of trusting a lie."""
        self.conn.execute(
            "UPDATE habit_slots SET calendar_event_id=NULL, mirrored_span=''"
            " WHERE id=?", (slot_id,))
        self.conn.commit()

    def get_next_meeting(self):
        """The user-flagged 'next presentation meeting', or None.

        Stored in kv as JSON: {"date", "title", "calendar", "id",
        "flagged_at"}. The agent context, intake default deadlines and the UI
        all read it — flag wherever the meeting comes from (a calendar event
        or a hand-set date) and every consumer follows.
        """
        raw = self.kv_get("next_meeting")
        try:
            info = json.loads(raw) if raw else None
        except (TypeError, json.JSONDecodeError):
            info = None
        return info if isinstance(info, dict) and info.get("date") else None

    def set_next_meeting(self, info=None):
        """Flag (or clear with None) the next presentation meeting."""
        if info is None:
            self.kv_set("next_meeting", "")
            self.add_event("meeting_cleared")
            return None
        clean = {"date": str(info.get("date") or "")[:10],
                 "title": str(info.get("title") or "PI meeting")[:200],
                 "calendar": str(info.get("calendar") or "")[:120],
                 "id": str(info.get("id") or "")[:200],
                 "flagged_at": now()}
        self.kv_set("next_meeting", json.dumps(clean, ensure_ascii=False))
        self.add_event("meeting_set", date=clean["date"], title=clean["title"])
        return self.get_next_meeting()

    def set_feedback(self, tid, actual_minutes, note=""):
        """Record the user's reported time (columns + a task_feedback event)."""
        ts = now()
        self.conn.execute(
            "UPDATE tasks SET actual_minutes=?, feedback_note=?, feedback_at=?,"
            " updated_at=? WHERE id=?",
            (int(actual_minutes), str(note or "")[:1000], ts, ts, tid))
        self.conn.commit()
        self.add_event("task_feedback", tid, actual_minutes=int(actual_minutes),
                       note=str(note or "")[:500])
        return self.get_task(tid)

    def feedback_tasks(self, limit=20):
        """Tasks with a reported actual time or a done status, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE actual_minutes IS NOT NULL OR status='done'"
            " ORDER BY COALESCE(feedback_at, updated_at, created_at) DESC, id DESC"
            " LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def measured_minutes(self, task):
        """Reported feedback first, else the task_doing -> task_done gap."""
        if task.get("actual_minutes") is not None:
            try:
                return int(task["actual_minutes"])
            except (TypeError, ValueError):
                pass
        return self.actual_minutes(task["id"])

    def actual_minutes(self, tid):
        """Minutes between the task_doing and task_done events, or None."""
        rows = self.conn.execute(
            "SELECT kind, ts FROM events WHERE task_id=? AND kind IN "
            "('task_doing','task_done') ORDER BY id", (tid,)).fetchall()
        start = None
        for r in rows:
            if r["kind"] == "task_doing":
                start = r["ts"]
            elif r["kind"] == "task_done" and start:
                try:
                    a = datetime.fromisoformat(start)
                    b = datetime.fromisoformat(r["ts"])
                    return round((b - a).total_seconds() / 60)
                except ValueError:
                    return None
        return None

    def needs_estimate(self, limit=100):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM tasks WHERE est_source IS NULL AND status IN "
            "('todo','doing') ORDER BY id LIMIT ?", (limit,)).fetchall()]

    # ---------- misc ----------
    def stats(self):
        t = {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
        return {"tasks_by_status": t,
                "events": self.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
                "plans": self.conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"],
                "history": self.conn.execute("SELECT COUNT(*) c FROM history").fetchone()["c"]}
