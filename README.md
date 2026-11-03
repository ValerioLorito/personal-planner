# Planner — Native macOS Agentic Calendar & Task Planner

**Planner** is a local-first, zero-dependency agentic planner for macOS that bridges your local project folders, documents, and Apple Calendar into an autonomous scheduling workflow.

Instead of estimating tasks in a vacuum, Planner's AI agents inspect your actual project repositories, PDFs, slides, and notes (read-only), cross-reference your live iCloud / Google / Exchange calendars via native macOS `EventKit`, break work into realistic time-calibrated subtasks, and place work blocks directly into your calendar.

---

## Key Features

### 1. Grounded AI Task Planning & Natural-Language Rescheduling
- **Modular Folder Grounding**: Connect any number of local folders from the UI (**📁 Folders & Calendars**) using the native macOS Finder picker:
  - **Single Project / Repository Mode**: Grounds tasks in a codebase, thesis, or project directory.
  - **Subfolders Mode**: Automatically treats each subfolder as its own category (ideal for university courses, client projects, or research topics).
- **Multi-Format Document Inspection**: Reads source code, Markdown, Jupyter notebooks (`.ipynb`), `.docx`, `.pptx` slides, and extracts text from `.pdf` files natively via macOS `PDFKit`.
- **Natural-Language Editing**: Ask the Task Agent to create new plans (*"Plan Assignment 2 and schedule it before Friday"*) or reschedule existing tasks (*"Move Quiz 3 to Friday at 15:00"*, *"Reschedule the evaluation task to Thursday afternoon"*).

### 2. Conversational Habits & Routines Agent
- **Interactive Habit Agent**: Chat in natural language to create, shift, or recalibrate weekly routines (*"Gym Mon/Wed/Fri 19:00–20:30"*, *"Daily reading at 08:30 for 45m"*).
- **Native Recurring Series**: Writes true `EKRecurrenceRule` recurring series to your chosen Habits calendar in Apple Calendar.
- **Clean Lifecycle & Self-Healing Sync**: Editing or deleting a habit cleanly updates or wipes the entire recurring series in Apple Calendar without leaving orphan events.

### 3. Full Apple Calendar (`EventKit`) Integration
- **Two-Way Native Bridge (`plannerkit`)**: Built on a compiled Swift helper using macOS `EventKit` — no third-party cloud middleman or OAuth setup required.
- **Inherits All Your Calendars**: Automatically discovers your iCloud, Google, Exchange, and subscribed calendars so the scheduler respects all your existing meetings, classes, and all-day commitments.
- **Custom Target Calendars**: Route planned work blocks and habits into any writable Apple Calendar (or let Planner create dedicated calendars automatically).

### 4. Real-Time Conflict Watcher & Inbox
- **Live `EKEventStoreChanged` Stream**: Watches Apple Calendar in real time via Server-Sent Events (SSE).
- **Automatic Conflict Proposals**: When a newly added meeting overlaps with a planned work block, the Watcher Agent drafts a repair proposal in your **Inbox** that you can review and apply with one click.

### 5. Personal Calibration & Memory Engine
- **Effort Calibration**: Tracks planned vs. actual duration across completed tasks, subtasks, and habit check-ins to compute per-category effort multipliers (`actual / estimated`).
- **Durable Agent Beliefs**: Stores your scheduling preferences and workflow rules (globally or per category) so future plans automatically respect how you work.

---

## Requirements

- **macOS 14.0+** (Sonoma or newer, Apple Silicon / Intel)
- **Xcode Command Line Tools** (`xcode-select --install`, for `/usr/bin/swiftc`)
- **Python 3.9+** (uses only the Python standard library — **zero `pip` dependencies**)
- **LLM API Key**: Supports **Google Gemini** (recommended; free tier provides 1,500 requests/day via Google AI Studio), **OpenRouter**, or any **OpenAI-compatible** endpoint (including local models via Ollama / LM Studio).

---

## Installation & Quick Start

### 1. Clone the Repository
```bash
git clone https://github.com/<your-username>/personal-planner.git
cd personal-planner
```

### 2. Build & Launch the Native macOS App (`Planner.app`)
Run the build script to compile the Swift `EventKit` helper (`plannerkit`), bundle the native `WKWebView` macOS app, install it to `~/Applications/Planner.app`, and launch it:
```bash
bash native/build_app.sh
```
On first launch:
1. Click **Grant Calendar Access** when prompted so macOS grants Full Calendar Access.
2. Click the **AI status pill** in the top-right header to paste your **Google Gemini** or **OpenRouter** API key.
3. Click **📁 Folders & Calendars** in the top header to select your local project/course folders, choose your target Apple Calendars, and set your working hours.

---

## Running from the Terminal (Optional)

You can also run the web server directly from the terminal and use Planner in any browser:

```bash
# Start the web server on http://127.0.0.1:8765
python3 -m planner.cli web --port 8765
```

Useful CLI commands:
```bash
python3 -m planner.cli status          # Check Apple Calendar auth, folders, and LLM budget
python3 -m planner.cli resync-habits   # Reconcile recurring habits with Apple Calendar
```

---

## Architecture & Project Structure

Planner is designed to be lightweight, inspectable, and completely dependency-free:

```text
personal-planner/
├── planner/
│   ├── agents/
│   │   ├── runtime.py        # Multi-turn JSON tool-calling agent loop
│   │   ├── task_agent.py     # Task grounding, estimation, decomposition & editing
│   │   ├── habit_agent.py    # Conversational habit creation & recalibration
│   │   ├── watcher_agent.py  # Real-time calendar conflict resolution agent
│   │   └── tools.py          # Read-only folder search, file extraction & calendar tools
│   ├── app.py                # Shared application state, pub/sub bus & busy-time computation
│   ├── calendar.py           # Python bridge to the native Swift EventKit helper
│   ├── config.py             # Defaults & JSON configuration management
│   ├── db.py                 # Local SQLite storage (WAL mode)
│   ├── habits_mgr.py         # Recurring habit lifecycle & check-in tracking
│   ├── llm.py                # Zero-dependency OpenAI-compatible HTTP client & budget tracker
│   ├── memory.py             # Historical effort calibration & durable user beliefs
│   ├── proposals_mgr.py      # Conflict resolution inbox management
│   ├── scheduler.py          # Deterministic gap-finding & work-block placement engine
│   ├── sources.py            # Sandboxed read-only file listing, search & PDF/Office extraction
│   ├── tasks_mgr.py          # Task lifecycle, block rescheduling & calendar publishing
│   ├── watcher.py            # Live EventKit change listener daemon
│   ├── web.py                # ThreadingHTTPServer REST API + SSE stream
│   └── cli.py                # CLI entry points
├── native/
│   ├── helper/
│   │   ├── main.swift        # Native Swift CLI for EventKit (Calendars) & PDFKit
│   │   └── Info.plist        # macOS Calendar permission metadata
│   ├── app/
│   │   ├── Launcher.swift    # Native macOS AppKit + WKWebView wrapper
│   │   ├── Info.plist        # App bundle configuration
│   │   └── icon/             # Native macOS app icon assets & generator
│   └── build_app.sh          # One-step compiler & installer for Planner.app
├── ui/
│   ├── index.html            # Single-page app layout & structured modals
│   ├── app.css               # Multi-theme styling (Ink, Ledger, Midnight)
│   └── app.js                # Reactive frontend state, week grid & SSE client
└── tests/                    # Offline unit & integration test suite
```

---

## Privacy & Security

- **100% Local Data**: Your tasks, habits, calibration history, and configuration live locally in `data/planner.db` and `data/config.json` (git-ignored).
- **Read-Only File Grounding**: Every file path requested by an agent is resolved with `os.path.realpath` and verified to stay strictly within your configured source folders. Planner never modifies your project or course files.
- **Secret Isolation**: Your LLM API key is stored locally in `data/llm_key` (`chmod 600`) and excluded from version control via `.gitignore`.

---

## Running Tests

All unit tests run offline using temporary SQLite databases and mock folders:

```bash
python3 -m unittest discover -s tests -v
```
