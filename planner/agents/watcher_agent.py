"""
watcher_agent.py — agent that evaluates calendar changes and proposes schedule repairs.
"""
from __future__ import annotations

import json
from datetime import datetime

from .. import memory
from ..calendar import dt, tag_of
from .runtime import Agent
from .tools import calendar_tools, fmt_event

SYSTEM = """You are the Schedule Reconciliation Agent of "Planner".
You are called when the user's calendar changes (meetings moved, added, or canceled) and
there is a conflict or change impacting scheduled work blocks or habits.

Your job:
1. Examine the conflicts (work blocks colliding with new/moved events, or deadline changes).
2. Find the cleanest repair:
   - Propose shifting the colliding block to the next available gap on the same day or week.
   - If a deadline was moved, check if the task's blocks still finish in time.
3. Formulate a proposal that the user will review in their proposals inbox.

Response Protocol:
Respond with a JSON object:
{
  "thought": "<reasoning about the conflict and resolution>",
  "final": {
    "title": "<short descriptive summary, e.g. 'Reschedule Block #3 due to moved Team Meeting'>",
    "rationale": "<clear 1-2 sentence explanation of why this repair makes sense>",
    "actions": [
      {
        "type": "move_block",
        "block_id": <int>,
        "new_start": "YYYY-MM-DDTHH:MM",
        "new_end": "YYYY-MM-DDTHH:MM",
        "reason": "<why this slot>"
      }
    ]
  }
}
If no action is necessary (e.g. conflict is negligible or user already solved it), set actions to []."""


def build_watcher_context(app, conflicts: list, deltas: dict) -> str:
    now = datetime.now()
    conflict_lines = []
    for c in conflicts:
        b = c["block"]
        e = c["event"]
        conflict_lines.append(
            f"- Scheduled Block #{b['id']} '{b['title']}' ({b['start']} to {b['end']}) "
            f"COLLIDES with Calendar Event '{e.get('title')}' in [{e.get('calendar')}] ({e.get('start')} to {e.get('end')})"
        )

    parts = [
        "# Current Local Time",
        now.strftime("%A %Y-%m-%d %H:%M"),
        "",
        "# Detected Conflicts",
        "\n".join(conflict_lines) or "(no direct block collisions detected)",
        "",
        "# Recent Calendar Deltas",
        f"Added events: {len(deltas.get('added', []))}, Modified: {len(deltas.get('modified', []))}, Removed: {len(deltas.get('removed', []))}",
        "",
        "# Free Working Time Today & Next Few Days",
    ]
    from ..scheduler import free_minutes_by_day
    free = free_minutes_by_day(app.cfg, app.busy(), days=7, now=now)
    for d, wd, m, work in free:
        parts.append(f"- {d} ({wd}): {m} min free")

    return "\n".join(parts)


def run_watcher_agent(app, conflicts: list, deltas: dict) -> dict:
    tools = calendar_tools(app)
    agent = Agent(app.llm, "watch_reconcile", SYSTEM, tools, max_steps=2)
    ctx = build_watcher_context(app, conflicts, deltas)
    return agent.run(ctx)
