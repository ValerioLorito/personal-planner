"""
web.py — lightweight HTTP server with JSON REST API and Server-Sent Events (SSE).
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import time
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import habits_mgr, memory, proposals_mgr, tasks_mgr
from .app import App
from .config import UI_DIR


class PlannerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=UI_DIR, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    @property
    def app(self) -> App:
        return self.server.app

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        if path == "" or path == "/index.html":
            return super().do_GET()
        if path == "/api/stream":
            return self._handle_sse()
        if path == "/api/state":
            return self._send_json(self._get_state())
        if path == "/api/tasks":
            tasks = [tasks_mgr.get_task_full(self.app, r["id"])
                     for r in self.app.db.q("SELECT id FROM tasks ORDER BY id DESC")]
            return self._send_json({"tasks": [t for t in tasks if t]})
        if re.match(r"^/api/tasks/\d+$", path):
            tid = int(path.split("/")[-1])
            t = tasks_mgr.get_task_full(self.app, tid)
            return self._send_json(t) if t else self._send_error(404, "Task not found")
        if path == "/api/habits":
            habits = self.app.db.q("SELECT * FROM habits WHERE status='active' ORDER BY start, name")
            for h in habits:
                h["days"] = json.loads(h.get("days_json") or "[]")
            logs = self.app.db.q("SELECT * FROM habit_logs ORDER BY date DESC LIMIT 40")
            return self._send_json({"habits": habits, "logs": logs})
        if path == "/api/proposals":
            return self._send_json({"proposals": proposals_mgr.list_proposals(self.app)})
        if path == "/api/beliefs":
            return self._send_json({"beliefs": self.app.db.q("SELECT * FROM beliefs ORDER BY id")})
        if path == "/api/calibration":
            cats = [c["id"] for c in self.app.categories()]
            cat_ratios = {c: memory.ratio(self.app.db, "task", c) for c in cats}
            return self._send_json({
                "overall": memory.ratio(self.app.db, "task"),
                "categories": cat_ratios,
                "habits": memory.ratio(self.app.db, "habit"),
                "recent_samples": self.app.db.q("SELECT * FROM samples ORDER BY id DESC LIMIT 20")
            })
        if path == "/api/calendar/status":
            return self._send_json({"auth": self.app.cal.status(), "calendars": self.app.cal.calendars()})
        if path == "/api/config/llm":
            from .config import LLM_PRESETS
            b = self.app.llm.budget()
            return self._send_json({
                "provider": b["provider"],
                "model": b["model"],
                "base_url": b["base_url"],
                "has_key": b["has_key"],
                "limit": b["limit"],
                "used": b["used"],
                "presets": LLM_PRESETS
            })
        return super().do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        body = self._read_json_body()

        try:
            # Task operations
            if path == "/api/tasks":
                input_text = body.get("input_text", "").strip()
                if not input_text:
                    return self._send_error(400, "input_text is required")
                category = body.get("category", "")
                target_task_id = int(body["task_id"]) if body.get("task_id") else None
                # Run planning / editing in background thread, trace streamed via SSE
                def run_plan():
                    def on_trace(step):
                        self.app.notify("agent_trace", step)
                    tasks_mgr.submit_task_input(self.app, input_text, category,
                                                target_task_id=target_task_id, on_trace=on_trace)
                self.app.background("PlanTask", run_plan)
                return self._send_json({"ok": True, "status": "planning_started"})

            m = re.match(r"^/api/tasks/(\d+)/deadline$", path)
            if m:
                tid = int(m.group(1))
                dl = body.get("deadline", "").strip()
                replan = bool(body.get("replan", False))
                res = tasks_mgr.update_task_deadline(self.app, tid, dl, replan=replan)
                return self._send_json({"ok": True, "task": res})

            m = re.match(r"^/api/tasks/(\d+)/schedule$", path)
            if m:
                tid = int(m.group(1))
                res = tasks_mgr.schedule_task(self.app, tid)
                return self._send_json({"ok": True, "task": res})

            m = re.match(r"^/api/tasks/(\d+)/rename$", path)
            if m:
                tid = int(m.group(1))
                new_title = body.get("title", "").strip()
                res = tasks_mgr.rename_task(self.app, tid, new_title)
                return self._send_json({"ok": True, "task": res})

            m = re.match(r"^/api/tasks/(\d+)/move_block$", path)
            if m:
                bid = int(body["block_id"])
                res = tasks_mgr.move_block(self.app, bid, body["new_start"], body["new_end"])
                return self._send_json({"ok": True, "block": res})

            m = re.match(r"^/api/tasks/(\d+)/subtasks/(\d+)/done$", path)
            if m:
                sid = int(m.group(2))
                mins = int(body.get("actual_minutes", 0)) if body.get("actual_minutes") else None
                done_flag = body.get("done", True) is not False
                res = tasks_mgr.complete_subtask(self.app, sid, mins, done=done_flag)
                return self._send_json({"ok": True, "task": res})

            m = re.match(r"^/api/tasks/(\d+)/feedback$", path)
            if m:
                tid = int(m.group(1))
                mins = int(body.get("actual_minutes", 0))
                note = body.get("note", "")
                res = tasks_mgr.task_feedback(self.app, tid, mins, note)
                return self._send_json(res)

            # Habit operations
            if path == "/api/habits/chat":
                msg = body.get("message", "").strip()
                if not msg:
                    return self._send_error(400, "message is required")
                from .agents import habit_agent
                plan, trace = habit_agent.run_habit_chat(self.app, msg)
                # Apply any actions the agent decided to execute
                reply_data = plan.get("reply", "Understood.")
                actions_taken = []
                for a in plan.get("actions", []):
                    if not isinstance(a, dict):
                        continue
                    atype = a.get("type")
                    if atype == "create_habit":
                        h = habits_mgr.create_habit(self.app, a.get("name") or "New Habit", a.get("days", [0, 1, 2, 3, 4]),
                                                    a.get("start", "08:00"), a.get("minutes", 30), a.get("notes", ""))
                        actions_taken.append(f"Created habit '{h['name']}'")
                    elif atype == "update_habit":
                        hid = a.get("habit_id")
                        if not hid and a.get("name"):
                            row = self.app.db.one("SELECT id FROM habits WHERE lower(name)=lower(?) AND status='active'",
                                                  (a["name"].strip(),))
                            if row:
                                hid = row["id"]
                        if hid:
                            clean_fields = {k: v for k, v in a.items() if k not in ("type", "habit_id") and v is not None}
                            h = habits_mgr.update_habit(self.app, int(hid), **clean_fields)
                            actions_taken.append(f"Updated habit '{h['name']}'")
                    elif atype == "delete_habit":
                        hid = a.get("habit_id")
                        hname = (a.get("name") or "").strip()
                        deleted_any = False
                        if hid:
                            if habits_mgr.delete_habit(self.app, int(hid)):
                                actions_taken.append(f"Deleted habit #{hid}")
                                deleted_any = True
                        if hname and not deleted_any:
                            rows = self.app.db.q("SELECT id FROM habits WHERE lower(name)=lower(?) AND status='active'", (hname,))
                            for r in rows:
                                if habits_mgr.delete_habit(self.app, r["id"]):
                                    actions_taken.append(f"Deleted habit '{hname}' (#{r['id']})")
                    elif atype == "resync":
                        habits_mgr.resync_habits(self.app)
                        actions_taken.append("Resynced habits with calendar")
                for b_txt in plan.get("learned_beliefs") or []:
                    if isinstance(b_txt, str) and b_txt.strip():
                        memory.remember(self.app.db, "habits", b_txt.strip())
                return self._send_json({"reply": reply_data, "actions_taken": actions_taken, "trace": trace})

            if path == "/api/habits":
                h = habits_mgr.create_habit(self.app, body["name"], body.get("days", []),
                                            body.get("start", "08:00"), body.get("minutes", 30), body.get("notes", ""))
                return self._send_json({"ok": True, "habit": h})

            if path == "/api/habits/resync":
                res = habits_mgr.resync_habits(self.app)
                return self._send_json({"ok": True, "resync": res})

            m = re.match(r"^/api/habits/(\d+)/rename$", path)
            if m:
                hid = int(m.group(1))
                new_name = body.get("name", "").strip()
                res = habits_mgr.rename_habit(self.app, hid, new_name)
                return self._send_json({"ok": True, "habit": res})

            m = re.match(r"^/api/habits/(\d+)/update$", path)
            if m:
                hid = int(m.group(1))
                clean = {k: body[k] for k in ("name", "days", "start", "minutes", "notes") if k in body}
                res = habits_mgr.update_habit(self.app, hid, **clean)
                return self._send_json({"ok": True, "habit": res})

            m = re.match(r"^/api/habits/(\d+)/checkin$", path)
            if m:
                hid = int(m.group(1))
                date_str = body.get("date", datetime.now().strftime("%Y-%m-%d"))
                status = body.get("status", "done")
                mins = int(body.get("actual_minutes")) if body.get("actual_minutes") else None
                note = body.get("note", "")
                res = habits_mgr.checkin_habit(self.app, hid, date_str, status, mins, note)
                return self._send_json(res)

            # Proposals
            m = re.match(r"^/api/proposals/(\d+)/apply$", path)
            if m:
                pid = int(m.group(1))
                res = proposals_mgr.apply_proposal(self.app, pid)
                return self._send_json(res)

            m = re.match(r"^/api/proposals/(\d+)/reject$", path)
            if m:
                pid = int(m.group(1))
                res = proposals_mgr.reject_proposal(self.app, pid)
                return self._send_json(res)

            # Calendar auth & operations
            if path == "/api/calendar/request":
                res = self.app.cal.request_access()
                return self._send_json(res)

            if path == "/api/calendar/refresh":
                self.app.cal.refresh()
                evs = self.app.events(max_age=0, refresh=True)
                self.app.notify("events_changed", {"action": "refreshed"})
                return self._send_json({"ok": True, "count": len(evs)})

            if path == "/api/calendar/toggle":
                cal_name = body.get("calendar", "")
                if not cal_name:
                    return self._send_error(400, "calendar name is required")
                enabled = bool(body.get("enabled", True))
                ignore_list = list(self.app.cfg["calendars"].get("ignore") or [])
                read_also = list(self.app.cfg["calendars"].get("read_also") or [])
                norm_target = cal_name.strip().lower()
                if enabled:
                    ignore_list = [x for x in ignore_list if x.strip().lower() != norm_target]
                    if not any(x.strip().lower() == norm_target for x in read_also):
                        read_also.append(cal_name)
                else:
                    if not any(x.strip().lower() == norm_target for x in ignore_list):
                        ignore_list.append(cal_name)
                self.app.cfg["calendars"]["ignore"] = ignore_list
                self.app.cfg["calendars"]["read_also"] = read_also
                self.app.save_config()
                self.app.cal.invalidate()
                self.app.notify("events_changed", {"action": "calendar_toggled", "calendar": cal_name, "enabled": enabled})
                return self._send_json({"ok": True, "ignore": ignore_list, "read_also": read_also})

            if path in ("/api/calendar/events/rename", "/api/calendar/events/update"):
                eid = unquote(body.get("event_id", "")).strip()
                new_title = body.get("title", "").strip()
                new_start = (body.get("new_start") or "").strip() or None
                new_end = (body.get("new_end") or "").strip() or None
                span = body.get("span", "this")
                occ = body.get("occurrence")
                item_id = body.get("item_id")
                ext_id = body.get("ext_id")
                if not eid or not new_title:
                    return self._send_error(400, "event_id and title are required")
                ev = self.app.cal.update(
                    eid,
                    occurrence=occ,
                    span=span,
                    item_id=item_id,
                    ext_id=ext_id,
                    title=new_title,
                    start=new_start,
                    end=new_end
                )
                self.app.db.x("UPDATE blocks SET title=? WHERE event_id=?", (new_title, eid))
                if body.get("block_id"):
                    self.app.db.x("UPDATE blocks SET title=? WHERE id=?", (new_title, int(body["block_id"])))
                    if new_start and new_end:
                        self.app.db.x("UPDATE blocks SET start=?, end=?, pinned=1 WHERE id=?",
                                      (new_start[:16], new_end[:16], int(body["block_id"])))
                elif new_start and new_end:
                    self.app.db.x("UPDATE blocks SET start=?, end=?, pinned=1 WHERE event_id=?",
                                  (new_start[:16], new_end[:16], eid))
                self.app.db.x("UPDATE habits SET name=? WHERE series_id=?", (new_title, eid))
                if body.get("habit_id"):
                    self.app.db.x("UPDATE habits SET name=? WHERE id=?", (new_title, int(body["habit_id"])))
                self.app.notify("events_changed", {"action": "updated", "id": eid})
                self.app.notify("tasks_changed", {"action": "event_updated", "id": eid})
                return self._send_json({"ok": True, "event": ev})

            if path == "/api/calendar/events/move":
                if body.get("block_id"):
                    bid = int(body["block_id"])
                    res = tasks_mgr.move_block(self.app, bid, body["new_start"], body["new_end"])
                    return self._send_json({"ok": True, "block": res})
                eid = unquote(body.get("event_id", "")).strip()
                if not eid:
                    return self._send_error(400, "event_id is required")
                ev = self.app.cal.update(
                    eid,
                    occurrence=body.get("occurrence"),
                    span=body.get("span", "this"),
                    item_id=body.get("item_id"),
                    ext_id=body.get("ext_id"),
                    start=body["new_start"],
                    end=body["new_end"]
                )
                self.app.notify("events_changed", {"action": "moved", "id": eid})
                return self._send_json({"ok": True, "event": ev})

            if path == "/api/calendar/events/delete":
                return self._handle_calendar_event_delete(
                    eid=body.get("event_id", ""),
                    span=body.get("span", "this"),
                    occ=body.get("occurrence"),
                    item_id=body.get("item_id"),
                    ext_id=body.get("ext_id"),
                    start=body.get("start"),
                    habit_id=body.get("habit_id"),
                    block_id=body.get("block_id")
                )

            # Beliefs
            if path == "/api/beliefs":
                if body.get("id"):
                    memory.forget(self.app.db, int(body["id"]))
                bid = memory.remember(self.app.db, body.get("scope", "global"), body.get("text", ""))
                self.app.notify("state_changed", {"action": "belief_saved", "id": bid})
                return self._send_json({"ok": True, "id": bid})

            # LLM Config & Test
            if path == "/api/config/llm":
                from .config import LLM_PRESETS, apply_preset, save, DATA_DIR
                provider = body.get("provider", "").strip()
                model = body.get("model", "").strip()
                key = body.get("key", "").strip()
                base_url = body.get("base_url", "").strip()

                if provider in LLM_PRESETS:
                    apply_preset(self.app.cfg, provider)
                if model:
                    self.app.cfg["llm"]["model"] = model
                if base_url:
                    self.app.cfg["llm"]["base_url"] = base_url
                if key:
                    key_path = os.path.join(DATA_DIR, "llm_key")
                    os.makedirs(os.path.dirname(key_path), exist_ok=True)
                    with open(key_path, "w", encoding="utf-8") as f:
                        f.write(key)
                save(self.app.cfg)
                test_res = None
                if body.get("test", True) and self.app.llm.ready():
                    test_res = self.app.llm.test_connection()
                return self._send_json({
                    "ok": True,
                    "budget": self.app.llm.budget(),
                    "test": test_res
                })

            return self._send_error(404, "Endpoint not found")

        except Exception as e:
            return self._send_error(500, str(e))

    def do_DELETE(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        try:
            m = re.match(r"^/api/tasks/(\d+)$", path)
            if m:
                tid = int(m.group(1))
                ok = tasks_mgr.delete_task(self.app, tid)
                return self._send_json({"ok": ok})

            m = re.match(r"^/api/tasks/(\d+)/blocks/(\d+)$", path)
            if m:
                bid = int(m.group(2))
                ok = tasks_mgr.remove_block(self.app, bid)
                return self._send_json({"ok": ok})

            m = re.match(r"^/api/habits/(\d+)$", path)
            if m:
                hid = int(m.group(1))
                ok = habits_mgr.delete_habit(self.app, hid)
                return self._send_json({"ok": ok})

            m = re.match(r"^/api/beliefs/(\d+)$", path)
            if m:
                bid = int(m.group(1))
                memory.forget(self.app.db, bid)
                return self._send_json({"ok": True})

            m = re.match(r"^/api/calendar/events/(.+)$", path)
            if m:
                eid = unquote(m.group(1))
                query = parse_qs(url.query)
                span = query.get("span", ["this"])[0]
                occ = query.get("occurrence", [None])[0]
                item_id = query.get("item_id", [None])[0]
                ext_id = query.get("ext_id", [None])[0]
                start = query.get("start", [None])[0]
                return self._handle_calendar_event_delete(
                    eid=eid, span=span, occ=occ, item_id=item_id, ext_id=ext_id, start=start
                )

            return self._send_error(404, "Endpoint not found")
        except Exception as e:
            return self._send_error(500, str(e))

    def _handle_calendar_event_delete(self, eid: str, span: str = "this", occ: str = None,
                                      item_id: str = None, ext_id: str = None, start: str = None,
                                      habit_id: int = None, block_id: int = None):
        eid = unquote(eid or "").strip()
        if not eid and not item_id and not ext_id and not block_id and not habit_id:
            return self._send_error(400, "event_id is required")

        # 1. If this is a known task block, remove it via tasks_mgr
        if block_id:
            ok = tasks_mgr.remove_block(self.app, int(block_id))
            if eid:
                try:
                    self.app.cal.delete(eid, occurrence=occ, span=span, item_id=item_id, ext_id=ext_id, start=start)
                except Exception:
                    pass
            self.app.notify("events_changed", {"action": "deleted", "id": eid})
            return self._send_json({"ok": True, "deleted_block": block_id})

        matching_blocks = self.app.db.q("SELECT id FROM blocks WHERE event_id=? AND event_id!=''", (eid,)) if eid else []
        if matching_blocks:
            for b in matching_blocks:
                tasks_mgr.remove_block(self.app, b["id"])
            try:
                self.app.cal.delete(eid, occurrence=occ, span=span, item_id=item_id, ext_id=ext_id, start=start)
            except Exception:
                pass
            self.app.notify("events_changed", {"action": "deleted", "id": eid})
            return self._send_json({"ok": True, "deleted_blocks": [b["id"] for b in matching_blocks]})

        # 2. If deleting an entire habit series (span == "all"), also delete the habit from DB
        if span == "all":
            matched_habits = []
            if habit_id:
                h = self.app.db.one("SELECT id FROM habits WHERE id=?", (int(habit_id),))
                if h:
                    matched_habits.append(h["id"])
            if eid:
                for h in self.app.db.q("SELECT id, series_id FROM habits WHERE status='active'"):
                    sid = h.get("series_id") or ""
                    if sid and (sid == eid or (ext_id and ext_id in sid) or ( ":" in eid and sid.endswith(eid.split(":")[-1]) )):
                        if h["id"] not in matched_habits:
                            matched_habits.append(h["id"])
            if matched_habits:
                for hid in matched_habits:
                    habits_mgr.delete_habit(self.app, hid)
                try:
                    self.app.cal.delete(eid, occurrence=occ, span="all", item_id=item_id, ext_id=ext_id, start=start)
                except Exception:
                    pass
                self.app.notify("events_changed", {"action": "deleted", "id": eid})
                return self._send_json({"ok": True, "deleted_habits": matched_habits})

        # 3. Standard Apple Calendar event deletion (single occurrence or series)
        ok = self.app.cal.delete(eid, occurrence=occ or start, span=span,
                                 item_id=item_id, ext_id=ext_id, start=start)
        if not ok:
            return self._send_error(404, "Event could not be found in Apple Calendar (it may have already been removed).")
        self.app.notify("events_changed", {"action": "deleted", "id": eid})
        return self._send_json({"ok": True})

    # ---- helpers ----
    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _send_json(self, obj: dict, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, message: str):
        self._send_json({"error": message}, code=code)

    def _get_state(self) -> dict:
        # Full snapshot of planner state
        tasks = [tasks_mgr.get_task_full(self.app, r["id"])
                 for r in self.app.db.q("SELECT id FROM tasks ORDER BY id DESC")]
        habits = self.app.db.q("SELECT * FROM habits WHERE status='active' ORDER BY start, name")
        for h in habits:
            h["days"] = json.loads(h.get("days_json") or "[]")
        habit_logs = self.app.db.q("SELECT * FROM habit_logs ORDER BY date DESC LIMIT 30")
        proposals = proposals_mgr.list_proposals(self.app)
        beliefs = self.app.db.q("SELECT * FROM beliefs ORDER BY id")
        events = self.app.events(max_age=15)

        return {
            "tasks": [t for t in tasks if t],
            "habits": habits,
            "habit_logs": habit_logs,
            "proposals": proposals,
            "beliefs": beliefs,
            "events": events,
            "calendars_list": self.app.cal.calendars(),
            "categories": self.app.categories(),
            "config": {
                "calendars": self.app.cfg["calendars"],
                "work": self.app.cfg["work"]
            },
            "llm_budget": self.app.llm.budget(),
            "calendar_status": self.app.cal.status(),
            "calendar_error": self.app.calendar_error,
            "now": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        }

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        sub = self.app.bus.subscribe()
        try:
            # Initial ping
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = sub.get(timeout=20)
                    chunk = f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode("utf-8")
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except queue.Empty:
                    # Keepalive ping
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.app.bus.unsubscribe(sub)


def serve(port: int = 8765, app: App = None):
    if app is None:
        app = App()
    app.cal.ensure_planner_calendars()
    from .watcher import CalendarWatcher
    app.watcher = CalendarWatcher(app)
    app.watcher.start()

    server_address = ("127.0.0.1", port)
    httpd = ThreadingHTTPServer(server_address, PlannerHandler)
    httpd.app = app
    print(f"Planner listening on http://127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.watcher:
            app.watcher.stop()
        httpd.server_close()
