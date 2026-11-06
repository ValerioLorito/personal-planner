"""
task_agent.py — understands a task, grounds it in its folder, estimates it,
splits it, and decides scheduling constraints. Placement itself is done by
scheduler.py from the returned constraints.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from .. import memory, sources
from ..calendar import dt, tag_of
from ..scheduler import free_minutes_by_day, parse_deadline
from .runtime import Agent
from .tools import calendar_tools, fmt_event, source_tools

SYSTEM = """You are the planning agent of "Planner", a personal agentic planner
that grounds tasks in the user's configured project, course, and workspace folders (`# Categories`) and schedules work around their Apple Calendar commitments.

You support TWO modes depending on the user's request:

=== MODE A: MODIFY / RESCHEDULE EXISTING TASK(S) ===
If the user asks to move, reschedule, change the date/time/deadline, rename, or remove an EXISTING open task or its blocks (listed under "# Existing open tasks & their blocks"), do NOT create a new task. Respond immediately with:
{
  "mode": "edit_existing",
  "reply": "<concise confirmation of what was changed, including new dates/times>",
  "edits": [
    {
      "action": "move_block" | "swap_blocks" | "realign_subtasks" | "reschedule_task" | "update_deadline" | "rename_task" | "delete_task" | "remove_block" | "add_block",
      "task_id": <int>,
      "block_id": <int or null>,
      "other_block_id": <int or null>,
      "new_start": "YYYY-MM-DDTHH:MM" or null,
      "new_end": "YYYY-MM-DDTHH:MM" or null,
      "deadline": "YYYY-MM-DDTHH:MM" or null,
      "earliest": "YYYY-MM-DD" or null,
      "preferred_time": "morning|afternoon|evening|any" or null,
      "title": "<optional new title>"
    }
  ]
}
Rules for MODE A:
- CRITICAL BORDER INSTRUCTION: Work blocks must NEVER overlap with any calendar commitment, habit (e.g. Daily Prep, Lunch Break, Gym), or other task block. They must sit strictly within the listed "# Free working time windows per day".
- Use "move_block" when moving a specific block (or a single-block task) to a specific date/time. Verify that "new_start".."new_end" falls completely inside one of the listed Free windows for that day. If the requested time is busy or overlaps with an event/habit, adjust to the closest available Free window or use "reschedule_task" with "earliest" and "preferred_time". Note: moving a block of a multi-subtask task automatically realigns the task's subtask sequence (1 -> 2 -> 3 -> 4) in chronological order!
- Use "reschedule_task" when the user wants to reschedule the entire subtask set of a task (or push remaining subtasks forward): set "task_id", and optionally "earliest" (e.g. "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM") and "preferred_time" ("morning"|"afternoon"|"evening"|"any"). The scheduler will automatically place all remaining subtasks sequentially into free calendar gaps without collisions.
- Use "swap_blocks" when the user wants to switch/swap the time slots of two scheduled blocks in the calendar (either between two different tasks or within the same task): set "block_id" and "other_block_id".
- Use "realign_subtasks" when the user asks to fix or reorder a task's blocks so that subtasks follow their strict sequential order (1, 2, 3...) chronologically: set "task_id".
- Use "update_deadline" to change a task's deadline, "rename_task" to rename it, "remove_block" to delete one block, "add_block" to add a new block at new_start..new_end, or "delete_task" to delete the task.
- TARGET TASK ENFORCEMENT: When "# ACTIVE TARGET TASK (Opened in Drawer): Task #<id>" is present, the user is inside that specific task's detail window. All edits ("move_block", "reschedule_task", etc.) MUST use this exact task_id and its block_id(s). You must NEVER select, edit, or reschedule any other task from other existing open tasks.
- TASK MATCHING: When no target task is specified and the user asks to move/reschedule a task by name, course, or topic (e.g. "Exercise Sheet 3", "Hybrid Quantum"), find the best matching task in "# Existing open tasks & their blocks" and use its exact "id" as "task_id".
- CRITICAL FOR "reply": Always use the EXACT title of the task from "# Existing open tasks" when referencing it in "reply" (e.g. "Rescheduled Task #<id> (<exact title>)"). Never attribute a different task's name to a task ID.

=== MODE B: PLAN A NEW TASK ===
Given a new task description you must:
1. Decide its category: "thesis" or one of the course ids listed below.
2. Understand the task concretely by inspecting that category's folder with the read-only tools:
   locate the relevant material (assignment sheets, slides, papers, code, audits, notes) and read what matters.
   Prefer 1–3 targeted reads over broad exploration. Never invent files you did not see.
3. Find the deadline: from the user's words, from the material (e.g. due dates in an assignment PDF),
   or from calendar events (exams, submissions, presentations, PI meetings). If none exists, infer a sensible
   target date and say so.
4. Estimate the effort in minutes realistically for THIS user — apply the calibration data and beliefs given.
5. Decide whether to split it into ordered, concrete subtasks (each 20–180 min, each a thing you can finish),
   its complexity, priority and when it should be worked on (scheduling preferences). If the user explicitly requested a specific date and time slot for the new task (e.g. "on Wednesday from 15:00 to 16:30"), verify that it falls within an available Free window for that day and does not collide with any habit or meeting, then set "fixed_blocks": [{"title": "...", "start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM"}] inside "scheduling".

The final object for MODE B must have this shape:
{
  "mode": "create_task",
  "category": "<category id>",
  "title": "<short imperative title, max 70 chars>",
  "summary": "<what the task really involves, 2–4 sentences grounded in the material>",
  "files": [{"path": "<path relative to the category root>", "why": "<what it told you>"}],
  "complexity": "S|M|L|XL",
  "estimate_minutes": <int, total>,
  "confidence": <0..1>,
  "deadline": "YYYY-MM-DDTHH:MM" or null,
  "deadline_source": "<'user' | 'calendar: <event>' | 'file: <path>' | 'inferred: <why>'>",
  "priority": 1|2|3,
  "deep_work": true|false,
  "subtasks": [{"title": "...", "minutes": <int>, "notes": "<what exactly to do>"}],
  "scheduling": {"earliest": "YYYY-MM-DD" or null, "preferred_time": "morning|afternoon|evening|any",
                 "max_block_minutes": <30..180>, "spread": true|false, "notes": "<timing reasoning>",
                 "fixed_blocks": [{"title": "...", "start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM"}] or null},
  "rationale": "<why this estimate, split and timing; mention calibration and deadline>"
}
Complexity guide: S < 1h, M 1–3h, L 3–8h, XL > 8h. Priority 1 = urgent/important.
The subtasks' minutes must sum to estimate_minutes (one subtask if it should not be split).
CRITICAL: Keep output compact and direct. Do NOT solve assignment problems, write proofs, or output long essays. Keep 'summary' and 'rationale' to 2 sentences max so the JSON fits comfortably."""


def _looks_final(obj: dict) -> bool:
    if obj.get("mode") == "edit_existing" and isinstance(obj.get("edits"), list):
        return True
    return "estimate_minutes" in obj and "subtasks" in obj


def _top_level(cat: dict, cap: int = 14) -> str:
    try:
        names = sorted(n for n in os.listdir(cat["root"]) if not n.startswith("."))
    except OSError:
        return "(folder not found)"
    shown = [n + ("/" if os.path.isdir(os.path.join(cat["root"], n)) else "") for n in names[:cap]]
    more = " …(+%d)" % (len(names) - cap) if len(names) > cap else ""
    return ", ".join(shown) + more


def build_context(app, task: dict) -> str:
    cfg = app.cfg
    now = datetime.now()
    cats = app.categories()
    hint = task.get("category") or ""
    target_tid = task.get("target_task_id")
    target_row = app.db.one("SELECT * FROM tasks WHERE id=?", (int(target_tid),)) if target_tid else None
    if target_row and not hint:
        hint = target_row.get("category") or ""

    parts = ["# User Request in Task Planner"]
    if target_row:
        t_title = target_row["title"] or target_row.get("input_text") or f"Task #{target_tid}"
        parts.append(
            f"The user has opened Task #{target_tid} (\"{t_title}\") in the task drawer and submitted this adjustment command:\n"
            f"> \"{task['input_text'].strip()}\"\n\n"
            f"(CRITICAL INSTRUCTION: You are adjusting THIS specific task: Task #{target_tid} (\"{t_title}\"). "
            f"You MUST use MODE A 'edit_existing'. Every edit you output MUST have 'task_id': {target_tid}. "
            f"Do NOT invent or modify any other task!)"
        )
    else:
        parts.append(task["input_text"].strip())
        if hint:
            parts.append("\n(The user selected category: %s.)" % hint)
        else:
            parts.append("\n(Decide whether this edits an existing task [MODE A] or plans a new task [MODE B].)")
    parts += ["", "# Now", now.strftime("%A %Y-%m-%d %H:%M (local time)")]

    if target_row:
        t_title = target_row["title"] or target_row.get("input_text") or f"Task #{target_tid}"
        parts += [
            "",
            f"# ACTIVE TARGET TASK (Opened in Drawer): Task #{target_tid} [{target_row['category']}] \"{t_title}\"",
            f"- Status: {target_row['status']}",
            f"- Estimate: {target_row.get('estimate_minutes') or 0} min",
            f"- Deadline: {target_row.get('deadline') or 'none'}",
        ]
        if target_row.get("summary"):
            parts.append(f"- Summary: {target_row['summary']}")
        target_subs = app.db.q("SELECT * FROM subtasks WHERE task_id=? ORDER BY idx, id", (int(target_tid),))
        if target_subs:
            parts.append("- Subtasks breakdown for this task:")
            for s in target_subs:
                parts.append(f"  * Subtask #{int(s.get('idx', 0)) + 1} (id={s['id']}): \"{s['title']}\" ({s['minutes']}m, status: {s.get('status', 'todo')})")
        target_blocks = app.db.q(
            "SELECT b.id, b.title, b.start, b.end, b.status, b.subtask_id, s.idx AS sub_idx, s.title AS sub_title "
            "FROM blocks b LEFT JOIN subtasks s ON b.subtask_id = s.id "
            "WHERE b.task_id=? ORDER BY b.start", (int(target_tid),)
        )
        if target_blocks:
            parts.append("- Scheduled Calendar Blocks for this task:")
            for b in target_blocks:
                sub_label = f" [Subtask #{int(b['sub_idx']) + 1}: {b['sub_title']}]" if b.get("sub_idx") is not None else ""
                parts.append(f"  * Block #{b['id']}{sub_label}: {b['start']} to {b['end']} \"{b['title']}\" ({b['status']})")
        else:
            parts.append("- Scheduled Calendar Blocks: (none yet)")

    parts += ["", "# Categories (id — label — top-level contents)"]
    for c in cats:
        parts.append("- %s — %s — %s" % (c["id"], c["label"], _top_level(c) if c["exists"] else "(missing folder)"))

    cat = app.category(hint) if hint else None
    if cat and cat["exists"]:
        parts += ["", "# Folder of %s (depth 2)" % cat["label"],
                  sources.list_dir(cfg, cat, "", depth=2, cap=140)]
        if cat["kind"] == "thesis":
            for readme in ("README.md", "readme.md"):
                p = os.path.join(cat["root"], readme)
                if os.path.exists(p):
                    try:
                        with open(p, encoding="utf-8", errors="replace") as f:
                            parts += ["", "# Thesis README (head)", f.read(2500)]
                    except Exception:
                        pass
                    break

    # upcoming events across all inherited calendars (iCloud, Google, University, Thesis, Habits)
    evs = app.events()
    upcoming = [e for e in evs if tag_of(e)[0] != "block" and dt(e["end"]) >= now]
    if cat and cat["kind"] == "course":
        rel = sources.related_events(cat, upcoming)
        other = [e for e in upcoming if e not in rel]
        upcoming = rel + other
    lines = [fmt_event(e) for e in upcoming[:80]]
    parts += ["", "# Upcoming commitments, habits, and deadlines (next %d days; use calendar_events for more)"
              % cfg["watch"]["days_ahead"], "\n".join(lines) or "(none)"]

    # existing work + blocks
    rows = app.db.q("SELECT id, title, category, deadline, estimate_minutes, status FROM tasks "
                    "WHERE status IN ('draft','scheduled') AND id!=? ORDER BY id DESC", (int(target_tid) if target_tid else 0,))
    section_title = "# Other existing open tasks & their blocks" if target_row else "# Existing open tasks & their blocks"
    parts += ["", section_title]
    if rows:
        for r in rows[:20]:
            parts.append("- Task #%d [%s] \"%s\" — %d min, deadline %s (%s)" % (
                r["id"], r["category"], r["title"] or r.get("input_text") or "(untitled)",
                r["estimate_minutes"] or 0, r["deadline"] or "none", r["status"]))
            b_rows = app.db.q(
                "SELECT b.id, b.title, b.start, b.end, b.status, b.subtask_id, s.idx AS sub_idx "
                "FROM blocks b LEFT JOIN subtasks s ON b.subtask_id = s.id "
                "WHERE b.task_id=? ORDER BY b.start", (r["id"],)
            )
            for b in b_rows:
                sub_tag = (" [Subtask #%d]" % (int(b["sub_idx"]) + 1)) if b.get("sub_idx") is not None else ""
                parts.append("    * Block #%d%s: %s to %s \"%s\" (%s)" % (
                    b["id"], sub_tag, b["start"], b["end"], b["title"], b["status"]))
    else:
        parts.append("(none)")

    scope = hint or "thesis"
    parts += ["", "# Calibration (how long things really took this user)", memory.calibration_text(app.db, scope),
              "", "# Beliefs about this user", memory.beliefs_text(app.db, ["global", "habits", scope])]

    from ..scheduler import Scheduler
    parts += ["", "# Free working time windows per day (strictly within work days %s, %s–%s; non-overlapping)" % (
        ",".join("MTWTFSS"[d] for d in cfg["work"]["days"]), cfg["work"]["day_start"], cfg["work"]["day_end"])]
    busy_all = app.busy()
    sch = Scheduler(cfg, busy=busy_all, now=now)
    for day_i in range(14):
        d_day = (now + timedelta(days=day_i)).replace(hour=0, minute=0, second=0, microsecond=0)
        d_is_work = d_day.weekday() in cfg["work"]["days"]
        if not d_is_work and not cfg["work"].get("weekend_if_needed", True):
            continue
        gaps = sch.gaps(d_day)
        day_str = d_day.strftime("%Y-%m-%d (%a)")
        if gaps:
            gaps_str = ", ".join(f"{g[0].strftime('%H:%M')}–{g[1].strftime('%H:%M')}" for g in gaps)
            total_m = sum(int((g[1] - g[0]).total_seconds() // 60) for g in gaps)
            parts.append(f"- {day_str}: Free [{gaps_str}] ({total_m}m available)")
        else:
            parts.append(f"- {day_str}: Fully booked")
    return "\n".join(parts)


def _extract_partial_from_trace(trace: list) -> dict:
    partial = {"files": []}
    for step in trace:
        if step.get("type") == "actions":
            for a in step.get("actions", []):
                if a.get("tool") == "read_file":
                    args = a.get("args") or {}
                    if args.get("path"):
                        partial["files"].append({"path": args["path"], "why": "Inspected file"})
                    if args.get("category"):
                        partial["category"] = args["category"]
        elif step.get("type") == "error":
            txt = step.get("text", "")
            if "reply was not JSON:" in txt:
                body = txt.split("reply was not JSON:", 1)[-1].strip()
                from ..llm import extract_json
                obj = extract_json(body)
                if obj and isinstance(obj, dict):
                    if "final" in obj and isinstance(obj["final"], dict):
                        obj = obj["final"]
                    if obj.get("mode") == "edit_existing" and isinstance(obj.get("edits"), list):
                        return {"mode": "edit_existing", "reply": obj.get("reply", ""), "edits": obj["edits"]}
                    for k in ("title", "category", "summary", "estimate_minutes", "deadline", "subtasks", "complexity"):
                        if k in obj and obj[k]:
                            partial[k] = obj[k]
    return partial


def run(app, task: dict, on_trace=None) -> tuple[dict, list]:
    agent = Agent(app.llm, "task", SYSTEM, source_tools(app) + calendar_tools(app),
                  max_steps=int(app.cfg["llm"].get("max_steps", 4)), on_trace=on_trace,
                  looks_final=_looks_final)
    try:
        plan = agent.run(build_context(app, task))
        return normalize(app, plan, task), agent.trace
    except Exception as e:
        partial = _extract_partial_from_trace(agent.trace)
        plan = heuristic(app, task, str(e), partial=partial)
        trace = list(agent.trace) + [{"type": "error", "text": str(e)}, {"type": "fallback", "text": "Heuristic fallback"}]
        return plan, trace


def _conf(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


def normalize(app, plan: dict, task: dict) -> dict:
    target_tid = task.get("target_task_id")
    if plan.get("mode") == "edit_existing" and isinstance(plan.get("edits"), list):
        edits = []
        valid_bids = set()
        if target_tid:
            valid_bids = {b["id"] for b in app.db.q("SELECT id FROM blocks WHERE task_id=?", (int(target_tid),))}
        for e in plan["edits"]:
            if not isinstance(e, dict):
                continue
            if target_tid:
                e["task_id"] = int(target_tid)
                if e.get("block_id") and e["block_id"] not in valid_bids and valid_bids:
                    e["block_id"] = sorted(valid_bids)[0]
            edits.append(e)
        return {
            "mode": "edit_existing",
            "reply": str(plan.get("reply") or "Updated existing task schedule.")[:600],
            "edits": edits
        }
    cats = {c["id"]: c for c in app.categories()}
    default_cat = next(iter(cats.keys()), "thesis")
    cat = task.get("category") or plan.get("category") or default_cat
    if cat not in cats and cats:
        low = {k.lower(): k for k in cats}
        cat = low.get(str(cat).lower()) or low.get("uni:" + str(cat).lower()) or \
            next((k for k, c in cats.items() if c["label"].lower() == str(cat).lower()), default_cat)
    subs = []
    for s in plan.get("subtasks") or []:
        if not isinstance(s, dict):
            continue
        try:
            m = int(float(s.get("minutes", 0)))
        except (TypeError, ValueError):
            m = 0
        if m <= 0:
            continue
        subs.append({"title": str(s.get("title", "Work"))[:140], "minutes": max(10, min(m, 600)),
                     "notes": str(s.get("notes", ""))[:600]})
    try:
        est = int(float(plan.get("estimate_minutes") or 0))
    except (TypeError, ValueError):
        est = 0
    if not subs:
        subs = [{"title": str(plan.get("title") or task["input_text"])[:140], "minutes": max(est, 30), "notes": ""}]
    est = sum(s["minutes"] for s in subs)
    deadline = parse_deadline(str(plan.get("deadline") or ""))
    sched = plan.get("scheduling") if isinstance(plan.get("scheduling"), dict) else {}
    cx = str(plan.get("complexity", "")).upper()
    if cx not in ("S", "M", "L", "XL"):
        cx = "S" if est < 60 else "M" if est <= 180 else "L" if est <= 480 else "XL"
    try:
        prio = max(1, min(3, int(plan.get("priority", 2))))
    except (TypeError, ValueError):
        prio = 2
    files = [f for f in plan.get("files") or [] if isinstance(f, dict) and f.get("path")][:10]
    return {
        "category": cat,
        "title": str(plan.get("title") or task["input_text"])[:120],
        "summary": str(plan.get("summary", ""))[:1500],
        "files": files,
        "complexity": cx,
        "estimate_minutes": est,
        "confidence": _conf(plan.get("confidence")),
        "deadline": deadline.strftime("%Y-%m-%dT%H:%M") if deadline else "",
        "deadline_source": str(plan.get("deadline_source") or "")[:200],
        "priority": prio,
        "deep_work": bool(plan.get("deep_work")),
        "subtasks": subs,
        "prefs": {"earliest": sched.get("earliest"),
                  "preferred_time": sched.get("preferred_time") if sched.get("preferred_time") in
                  ("morning", "afternoon", "evening", "any") else ("morning" if plan.get("deep_work") else "any"),
                  "max_block_minutes": sched.get("max_block_minutes"),
                  "spread": sched.get("spread", True) is not False,
                  "fixed_blocks": sched.get("fixed_blocks") if isinstance(sched.get("fixed_blocks"), list) else None,
                  "notes": str(sched.get("notes", ""))[:400]},
        "rationale": str(plan.get("rationale", ""))[:1500],
    }


# ---------------------------------------------------------------- offline
WORDS_L = re.compile(r"\b(implement|write|project|chapter|report|refactor|rerun|experiment|study for|exam)\b", re.I)
WORDS_S = re.compile(r"\b(read|email|check|fix typo|review|send|submit|upload)\b", re.I)


def _parse_relative_target_time(raw_text: str, now: datetime) -> tuple[str | None, str | None]:
    """Extracts date (YYYY-MM-DD) and optional start time (HH:MM) from natural language."""
    low = raw_text.lower()
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    found_date = None
    if "tomorrow" in low:
        found_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        for idx, day_name in enumerate(days):
            if re.search(rf"\b{day_name}\b", low):
                cur_wd = now.weekday()
                delta = (idx - cur_wd) % 7
                if delta == 0:
                    delta = 7
                found_date = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
                break
    if not found_date:
        m_d = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", raw_text)
        if m_d:
            found_date = m_d.group(1)

    found_time = None
    m_t = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", raw_text)
    if m_t:
        found_time = f"{m_t.group(1).zfill(2)}:{m_t.group(2)}"
    else:
        m_ampm = re.search(r"\b([1-9]|1[0-2])(?::([0-5]\d))?\s*(am|pm)\b", raw_text, re.I)
        if m_ampm:
            hr = int(m_ampm.group(1))
            mn = m_ampm.group(2) or "00"
            if m_ampm.group(3).lower() == "pm" and hr != 12:
                hr += 12
            elif m_ampm.group(3).lower() == "am" and hr == 12:
                hr = 0
            found_time = f"{str(hr).zfill(2)}:{mn}"

    return found_date, found_time


def heuristic(app, task: dict, reason: str, partial: dict = None) -> dict:
    partial = partial or {}
    if partial.get("mode") == "edit_existing":
        return normalize(app, partial, task)

    raw_text = task.get("input_text") or ""
    target_tid = task.get("target_task_id")
    # If the user explicitly targeted an existing task (or said "move/reschedule task #N"), handle as edit_existing
    if not target_tid and re.match(r"^\s*(move|reschedule|shift|postpone|change\s+date|change\s+time)\b", raw_text, re.I):
        m_id = re.search(r"#(\d+)", raw_text)
        if m_id:
            target_tid = int(m_id.group(1))
        else:
            open_tasks = app.db.q("SELECT id, title FROM tasks WHERE status IN ('draft','scheduled')")
            low_in = raw_text.lower()
            for ot in open_tasks:
                words = [w for w in re.split(r"\W+", (ot["title"] or "").lower()) if len(w) >= 4]
                if words and sum(1 for w in words if w in low_in) >= min(2, len(words)):
                    target_tid = ot["id"]
                    break
    if target_tid:
        target_t = app.db.one("SELECT * FROM tasks WHERE id=?", (int(target_tid),))
        t_title = target_t["title"] if target_t else f"Task #{target_tid}"
        target_b = app.db.q("SELECT * FROM blocks WHERE task_id=? ORDER BY start", (int(target_tid),))
        first_bid = target_b[0]["id"] if target_b else None

        found_date, found_time = _parse_relative_target_time(raw_text, datetime.now())
        earliest_val = f"{found_date}T{found_time}" if (found_date and found_time) else found_date
        pref = "morning" if "morning" in raw_text.lower() else ("afternoon" if "afternoon" in raw_text.lower() else ("evening" if "evening" in raw_text.lower() else None))

        is_resched = bool(re.search(r"\b(reschedule|postpone|subtask|all\s+subtasks)\b", raw_text, re.I))
        if len(target_b) > 1 or is_resched or not first_bid:
            return {
                "mode": "edit_existing",
                "reply": f"Rescheduled Task #{target_tid} ({t_title}) starting {earliest_val or ''}.",
                "edits": [{"action": "reschedule_task", "task_id": int(target_tid),
                           "earliest": earliest_val, "preferred_time": pref}]
            }

        if found_date and found_time:
            ns = f"{found_date}T{found_time}"
            return {
                "mode": "edit_existing",
                "reply": f"Moved Task #{target_tid} ({t_title}) to {ns}.",
                "edits": [{"action": "move_block", "task_id": int(target_tid), "block_id": first_bid, "new_start": ns}]
            }
        return {
            "mode": "edit_existing",
            "reply": f"Rescheduled Task #{target_tid} ({t_title}).",
            "edits": [{"action": "reschedule_task", "task_id": int(target_tid),
                       "earliest": earliest_val, "preferred_time": pref}]
        }

    text = partial.get("title") or task["input_text"]
    cat = task.get("category") or partial.get("category") or ""
    all_cats = app.categories()
    if not cat:
        low = text.lower()
        for c in all_cats:
            if any(k in low for k in c.get("keywords", [])):
                cat = c["id"]
                break
        cat = cat or (all_cats[0]["id"] if all_cats else "thesis")
    est = partial.get("estimate_minutes")
    if not est:
        est = 180 if WORDS_L.search(text) else 45 if WORDS_S.search(text) else 90
        est = int(round(est * memory.ratio(app.db, "task", cat)["ratio"] / 5.0) * 5)
    subs = partial.get("subtasks")
    if not subs:
        subs = [{"title": text[:120], "minutes": est, "notes": ""}]
        if est > 120:
            half = est // 2
            subs = [{"title": "Part 1: " + text[:100], "minutes": half, "notes": ""},
                    {"title": "Part 2: " + text[:100], "minutes": est - half, "notes": ""}]
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    deadline = partial.get("deadline") or (m.group(1) if m else None)
    return normalize(app, {
        "category": cat,
        "title": partial.get("title") or text[:70],
        "subtasks": subs,
        "estimate_minutes": est,
        "deadline": deadline,
        "deadline_source": "extracted" if partial.get("deadline") else ("user" if m else ""),
        "summary": partial.get("summary") or ("Offline estimate: %s" % reason),
        "files": partial.get("files") or [],
        "rationale": "Grounded in inspected files; estimated via calibration (%s)." % reason,
        "confidence": 0.4 if partial.get("files") else 0.3
    }, task)
