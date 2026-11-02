# Planner — Agentic Personal Planner

An autonomous, agentic planner for macOS that grounds tasks in your local code repositories and university course materials, learns your weekly habits and routines, schedules your work into Apple Calendar, and monitors calendar changes in real time.

---

## Highlights

1. **Agentic Task Planning**
   - Grounded in your project and course folders:
     - **Thesis**: `~/Desktop/commonsense-atlas`
     - **University**: `~/Desktop/iCloud/SAPIENZA/ERASMUS` (with course subfolders like `GENERATIVE AI`, `HYBRID QUANTUM`, `KNOWLEDGE REPRESENTATION`, `PROJECT IN LOGIC AND AI`).
   - The planning agent inspects relevant files (code, assignment PDFs, lecture slides), discovers deadlines from assignment text or your calendar, estimates realistic completion times (calibrated by your past history), breaks work into actionable subtasks, and finds optimal free slots in your schedule.

2. **Agentic Habits & Routines**
   - Interactive Habit Agent: chat to add, adjust, or recalibrate habits ("gym mon wed fri 19:00-20:30", "gym took only 60m today, recalibrate").
   - True recurring series: habits are created as native `EKRecurrenceRule` series in the **Habits** calendar.
   - Clean deletion: deleting a habit permanently removes the recurring series from Apple Calendar (no orphan events).
   - Self-healing **Resync**: reconciles database habits with the "Habits" calendar, restoring deleted series and importing external recurring events.
   - Feedback loop: check-ins record actual vs. planned duration, feeding the calibration engine.

3. **Multi-Calendar Native Integration**
   - Works directly on your Apple Calendar via native Swift EventKit (`plannerkit`):
     - **Thesis**: Planned research and coding blocks.
     - **University**: Planned coursework, study sessions, and homework blocks.
     - **Habits**: Recurring weekly habits and commitments.
     - **UNIVERSITÀ **: Read-only access to existing lectures, seminars, and university deadlines.
   - All other personal calendars (`Festività in Italia`, etc.) are respected as busy blocks.

4. **Real-time Calendar Watcher & Inbox**
   - Listens to macOS `EKEventStoreChanged` events in real time.
   - If an external meeting moves or collides with a scheduled work block or habit, the Watcher Agent evaluates the conflict and drafts a repair proposal in your **Inbox**.
   - Review proposals in the UI and click **Approve & Apply** to update your schedule with one click.

---

## Quick Start

### Launching the Native Dock App
Double-click `Planner.app` in `~/Applications` (or run in terminal):
```bash
open "$HOME/Applications/Planner.app"
```
The native app automatically starts the local backend server on `127.0.0.1:8765` and displays the interface in a native macOS window.

### Running from the Terminal
```bash
cd ~/Desktop/Planner
python3 -m planner.cli web --port 8765
```
Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in Safari or Chrome.

### CLI Commands
```bash
python3 -m planner.cli status          # Check calendar permissions and LLM budget
python3 -m planner.cli resync-habits   # Force sync habits with Apple Calendar
```

---

## Project Structure

```
Planner/
  planner/
    agents/
      runtime.py       # Resilient agent loop (JSON tool-calling protocol)
      task_agent.py    # Grounding, estimation, subtask decomposition
      habit_agent.py   # Habit understanding, duration recalibration
      watcher_agent.py # Real-time calendar conflict resolution
      tools.py         # Read-only folder search, file extraction, calendar tools
    app.py             # Shared application context & bus
    bus.py             # In-memory pub/sub for Server-Sent Events (SSE)
    calendar.py        # Python bridge to native plannerkit
    config.py          # Configuration loader & defaults
    db.py              # SQLite storage with WAL mode
    habits_mgr.py      # Habits lifecycle, check-ins, Apple Calendar sync
    llm.py             # OpenRouter client with daily request budget
    memory.py          # Estimation multipliers (median actual/est) & durable beliefs
    scheduler.py       # Deterministic gap placement into free calendar time
    sources.py         # Safe read-only inspection of Thesis and Uni folders
    tasks_mgr.py       # Task planning, block movement, feedback recalibration
    watcher.py         # Real-time EventKit watcher daemon
    web.py             # HTTP server with REST API & SSE live streaming
    cli.py             # Command-line entry points
  native/
    helper/
      main.swift       # EventKit & PDFKit Swift CLI (JSON in / JSON out)
      Info.plist       # Full calendar access usage descriptions
    app/
      Launcher.swift   # macOS AppKit / WKWebView application shell
      Info.plist       # Bundle metadata for Planner.app
    build/
      plannerkit       # Compiled EventKit binary
      Planner.app      # Compiled native application bundle
    build_app.sh       # Rebuilds helper and Planner.app
  ui/
    index.html         # Single-page interface
    app.css            # Dark ledger aesthetic
    app.js             # Live reactive UI logic (SSE, schedule grid, drawers)
  tests/               # Unit and integration test suite
  data/                # Database (planner.db), config.json, llm_key
```

---

## Testing

Run the test suite:
```bash
python3 -m unittest discover -s tests -v
```
All unit tests run offline in ~1 second.
