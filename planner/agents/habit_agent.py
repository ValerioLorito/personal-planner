"""
habit_agent.py — agent for the Habits window.

Learns the user's recurring habits and patterns:
  - Interprets conversational requests to create, adjust, or remove habits.
  - Recalibrates habit durations and scheduling beliefs based on user feedback
    (e.g., "gym took only 60m today instead of 90m").
  - Inspects existing habits, past checkins, and "Habits" calendar events.
"""
from __future__ import annotations

import json
from datetime import datetime

from .. import memory
from .runtime import Agent
from .tools import calendar_tools, fmt_event, memory_tools

SYSTEM = """You are the Habits & Routine Agent of "Planner".
You help the user manage recurring weekly habits, routines, and commitments.
The user's habits are mirrored in Apple Calendar under the "Habits" calendar as recurring events.

Your capabilities:
1. Parse new habits or changes to existing habits from user messages.
   Days are 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun.
2. Recalibrate habits when the user provides feedback (e.g. "I only did 45 min", "it takes longer than 30m"):
   - Update habit duration if needed.
   - Record durable beliefs using the `remember` tool (scope 'habits').
   - Explain how future planning adjusts.
3. Handle requests to delete or pause habits.
4. Answer questions about current routines, consistency, and calendar alignment:
   - When asked about schedules or dynamic adjustments, use `calendar_events` to inspect what is on the user's calendars.
   - Note on Dynamic / Relative Habits: Apple Calendar's native recurring series (EKRecurrenceRule) requires a fixed baseline start time (e.g. 08:00). When the user sets a dynamic relative rule (e.g. "75 minutes before first morning lecture, else 08:00"), store it as a belief using `remember`. Clearly explain that the recurring series in Apple Calendar holds the 08:00 fallback baseline, while Planner's scheduler uses the learned belief to align each day's actual start time relative to their morning events.

Response Protocol:
Respond with a JSON object:
{
  "thought": "<brief reasoning>",
  "final": {
    "reply": "<friendly, clear conversational answer to user>",
    "actions": [
      {
        "type": "create_habit" | "update_habit" | "delete_habit" | "recalibrate" | "resync" | "none",
        "habit_id": <int or null>,
        "name": "<string>",
        "days": [0, 1, 2, ...],
        "start": "HH:MM",
        "minutes": <int>,
        "notes": "<optional context>"
      }
    ],
    "learned_beliefs": ["<belief string to store or confirm>"]
  }
}
If no action is needed (just answering a question), set actions to []."""


def build_habit_context(app, user_message: str) -> str:
    now = datetime.now()
    habits = app.db.q("SELECT * FROM habits WHERE status='active' ORDER BY start, name")
    logs = app.db.q("SELECT l.*, h.name FROM habit_logs l JOIN habits h ON l.habit_id=h.id "
                    "ORDER BY l.date DESC LIMIT 15")
    samples = app.db.q("SELECT * FROM samples WHERE kind='habit' ORDER BY id DESC LIMIT 10")
    beliefs_txt = memory.beliefs_text(app.db, ["global", "habits"])

    cal_name = app.cfg["calendars"]["habits"]
    try:
        win_start = now.replace(hour=0, minute=0)
        win_end = now.replace(hour=23, minute=59) + __import__('datetime').timedelta(days=10)
        all_win_evs = app.cal.events(win_start, win_end)
        habit_evs = [e for e in all_win_evs if e.get("calendar", "").strip().lower() == cal_name.strip().lower()]
        ext_evs = [e for e in all_win_evs if e.get("calendar", "").strip().lower() != cal_name.strip().lower()]
        cal_txt = "\n".join(fmt_event(e) for e in habit_evs[:30]) or "(no events on Habits calendar)"
        ext_txt = "\n".join(fmt_event(e) for e in ext_evs[:45]) or "(no external commitments in next 10 days)"
    except Exception as e:
        cal_txt = f"(error fetching Habits calendar: {e})"
        ext_txt = f"(error fetching external calendars: {e})"

    h_lines = []
    days_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for h in habits:
        days = json.loads(h.get("days_json") or "[]")
        day_str = ", ".join(days_names[d] for d in days if 0 <= d <= 6)
        h_lines.append(f"- #{h['id']} '{h['name']}': {day_str} at {h['start']} ({h['minutes']} min) [series: {h.get('series_id') or 'none'}]")

    parts = [
        "# Current Local Time",
        now.strftime("%A %Y-%m-%d %H:%M"),
        "",
        "# User Input",
        user_message.strip(),
        "",
        "# Active Habits in DB",
        "\n".join(h_lines) or "(no active habits yet)",
        "",
        "# Recent Habit Check-ins / Logs",
        "\n".join(f"- {l['date']} '{l['name']}': {l['status']} (planned {l.get('planned_minutes')}m, actual {l.get('actual_minutes')}m) {l.get('note','')}" for l in logs) or "(none)",
        "",
        "# Recent Calibration Samples (Actual vs Est)",
        "\n".join(f"- '{s['label']}': estimated {s['estimated']}m, took {s['actual']}m" for s in samples) or "(none)",
        "",
        "# Learned Beliefs about Habits",
        beliefs_txt,
        "",
        "# Events currently in Apple Calendar ('Habits')",
        cal_txt,
        "",
        "# Incoming Commitments Across Inherited Calendars (iCloud / Google / University — next 10 days)",
        ext_txt
    ]
    return "\n".join(parts)


def run_habit_chat(app, user_message: str, on_trace=None) -> tuple[dict, list]:
    tools = calendar_tools(app) + memory_tools(app, ["global", "habits"])
    agent = Agent(app.llm, "habits", SYSTEM, tools, max_steps=3, on_trace=on_trace)
    context = build_habit_context(app, user_message)
    plan = agent.run(context)
    return plan, agent.trace
