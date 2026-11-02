"""
watcher.py — live Apple Calendar watcher via native plannerkit watch.

Monitors EKEventStoreChanged events in real time. When the calendar changes:
  1. Re-fetches the active calendar window.
  2. Auto-removes blocks whose calendar events were manually deleted.
  3. Detects collisions between scheduled blocks and external events.
  4. If conflicts exist, calls watcher_agent to draft a repair proposal
     in the user's inbox.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime, timedelta

from .agents import watcher_agent
from .calendar import dt, event_key, tag_of
from .db import now_iso


class CalendarWatcher:
    def __init__(self, app):
        self.app = app
        self.proc = None
        self.thread = None
        self._stop = False
        self.last_agent_call = None
        self._previous_events = {}  # key -> event dict

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop = False
        self.thread = threading.Thread(target=self._run_loop, name="CalendarWatcher", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop = True
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _run_loop(self):
        kit = self.app.cal.kit
        if not kit:
            return

        while not self._stop:
            try:
                self.proc = subprocess.Popen([kit, "watch"], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             text=True, bufsize=1)
                # First fetch snapshot
                self._handle_change(initial=True)

                for line in self.proc.stdout:
                    if self._stop:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except ValueError:
                        continue
                    if data.get("type") == "changed":
                        time.sleep(self.app.cfg["watch"].get("debounce_sec", 2))
                        self._handle_change(initial=False)

            except Exception as e:
                time.sleep(5)
            finally:
                if self.proc:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                    self.proc = None

    def _handle_change(self, initial: bool = False):
        self.app.cal.invalidate()
        try:
            current_events = self.app.events(max_age=0)
        except Exception:
            return

        new_map = {event_key(e): e for e in current_events if e.get("id")}

        if initial or not self._previous_events:
            self._previous_events = new_map
            return

        # Calculate deltas
        added = [e for k, e in new_map.items() if k not in self._previous_events]
        removed = [e for k, e in self._previous_events.items() if k not in new_map]
        modified = []
        for k, e in new_map.items():
            if k in self._previous_events:
                old = self._previous_events[k]
                if (old.get("start") != e.get("start") or old.get("end") != e.get("end") or
                        old.get("title") != e.get("title")):
                    modified.append(e)

        self._previous_events = new_map
        deltas = {"added": added, "removed": removed, "modified": modified}

        # 1. Check for blocks whose events were deleted externally in Calendar.app
        removed_ids = {e["id"] for e in removed if e.get("id")}
        if removed_ids:
            orphans = self.app.db.q("SELECT id, task_id, title FROM blocks WHERE event_id IN (%s)" %
                                    ",".join("?" * len(removed_ids)), tuple(removed_ids))
            for o in orphans:
                self.app.db.update("blocks", o["id"], {"status": "removed", "event_id": ""})
                self.app.toast(f"Event for block '{o['title']}' was deleted in Calendar — block unscheduled", "info")
                self.app.notify("tasks_changed", {"action": "block_unscheduled", "id": o["task_id"]})

        # 2. Check collisions with scheduled blocks
        scheduled_blocks = self.app.db.q("SELECT * FROM blocks WHERE status='scheduled'")
        conflicts = []
        now = datetime.now()

        for b in scheduled_blocks:
            bs = dt(b["start"])
            be = dt(b["end"])
            if be < now:
                continue
            b_ev_id = b.get("event_id")

            # Check overlap against all non-block calendar events
            for e in current_events:
                if e.get("all_day") or not e.get("start") or not e.get("end"):
                    continue
                if e.get("id") == b_ev_id:
                    continue
                k, ref = tag_of(e)
                if k == "block" and ref == b["id"]:
                    continue

                es = dt(e["start"])
                ee = dt(e["end"])
                # Overlap test
                if max(bs, es) < min(be, ee):
                    conflicts.append({"block": b, "event": e})

        if not conflicts:
            return

        # 3. Call watcher agent if cooldown and budget permit
        cooldown_min = self.app.cfg["watch"].get("agent_cooldown_min", 15)
        if self.last_agent_call and (datetime.now() - self.last_agent_call).total_seconds() < cooldown_min * 60:
            return

        if not self.app.llm.can_call("watch"):
            return

        self.last_agent_call = datetime.now()
        try:
            proposal = watcher_agent.run_watcher_agent(self.app, conflicts, deltas)
            actions = proposal.get("actions") or []
            if actions:
                pid = self.app.db.insert("proposals", {
                    "created_at": now_iso(),
                    "source": "watcher",
                    "title": proposal.get("title", "Resolve calendar conflict"),
                    "rationale": proposal.get("rationale", ""),
                    "actions_json": json.dumps(actions),
                    "status": "pending"
                })
                self.app.toast(f"Calendar conflict detected: repair proposal #{pid} created", "warning")
                self.app.notify("proposals_changed", {"action": "new", "id": pid})
        except Exception as e:
            self.app.db.log("watcher_error", f"Watcher agent failed: {e}")
