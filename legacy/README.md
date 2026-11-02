# Thesis planner (`planner/`)

A local, dependency-free app that turns “what I still owe my PI” into a **scheduled week**, while keeping a log of everything you did. It knows three things:

1. **your tasks** — title, priority, complexity, estimated minutes, deadline (usually the next PI meeting), area, files;
2. **your Apple Calendar** — busy blocks are read to find free slots, and the plan can be written back as events;
3. **your project folder** — `commonsense-atlas` is scanned for TODOs, uncommitted changes and open audit items, and its git history can be back-filled into a work timeline since April.

Everything is local: SQLite in `planner/data/planner.db`, config in `planner/data/config.json` (both gitignored). No pip installs, no API keys, Python 3 stdlib only.

---

## Quick start

```bash
# 1. add the things you owe your PI
python3 -m planner.cli add "Fix itemize VALIDATOR E edge cases" \
    --complexity M --est 90 --priority 1 --deadline 2026-11-03 --area coref
python3 -m planner.cli add "Rerun 27B coref + consec" \
    --complexity L --est 180 --priority 1 --deadline 2026-11-03 --area coref
python3 -m planner.cli add "Write methods section" \
    --complexity L --est 240 --priority 2 --deadline 2026-11-03 --area writing

# 2. see the list
python3 -m planner.cli list

# 3. plan the week (dry run first)
python3 -m planner.cli schedule --week next --dry-run
python3 -m planner.cli schedule --week next          # saves the plan

# 4. look at the saved plan / mark things done
python3 -m planner.cli plan --week next
python3 -m planner.cli done 3 --note "validator merged"
```

## The interface

A single-page app served from `planner/ui/` (`index.html`, `app.css`, `app.js`) —
no build step, no CDN, no framework. It reads one endpoint (`GET /api/state`)
and writes five (`/api/intake`, `/api/tasks/<id>/feedback`, `/api/schedule`,
`/api/slots/<id>` for manual moves/removes, `/api/meeting` for the
presentation-meeting flag).

- **Week** — a ledger: one ruled track per day from `day_start` to `day_end`,
  task blocks in ink, calendar meetings as hatched bars, a red now-line, past
  days muted. One pinned hour ruler instead of seven.
- **Today rail** — the day in order with meetings, blocks and the gaps between
  them (`1h40 free`), plus what is next. The ⚑ next to a meeting flags it as
  the next presentation meeting (see above).
- **Tasks** — every task with estimate, real time (once reported) and area.
  The drawer's **Placed** rows are editable:
  change date/time and **Move** (pins the block; re-plans respect it), or
  **Remove** to unschedule it. Changing the date and/or start shifts the end
  automatically so the block's duration is preserved — change the end only
  when you mean to change the duration. The drawer's **Plan within** select
  changes how far ahead the task may be placed. Each row also has a hover
  **✕** that deletes the task for good (see *Drop vs Delete* below). The view
  header's
  **+ past task** registers work completed before this app existed (see
  *Tell it how long it really took*).
- **Calibration** — how wrong the estimates have been, overall and per area,
  and the feedback that produced it.
- **Theme** — auto / light / dark (the sun button), remembered locally.
- Deep links: `/?desc=write+the+audit&plan=1` pre-fills (and plans) a task,
  `/?task=7` opens one, `/?view=calibration` opens a view.

## Describe it, don't fill forms

```bash
python3 -m planner.cli add --desc "create an audit for coreference resolution"
# or, for a course task grounded in its subject folder:
python3 -m planner.cli add --desc "prepare the weekly reading response" \
     --category "NLP Semantics"
```

`planner/intake.py` turns one sentence into a task: it reads the repository
**read-only** (keyword → glob table, then `os.walk`) — or the subject folder
under `university_root` when a `--category` is given — cites the artefacts it
found, derives area/complexity/minutes/priority/subtasks, and hands them to
the brain backend:

```
50m  Write the audit for the coreference funnel — M work, ~50 min, priority 2
     area 'coreference' inferred from keywords
     read-only inspection found 4 artefacts: src/utils/coreference/audit_wsd_funnel.py,
       audits/coref_audit.md, src/utils/coreference/audit_coreference.py, README.md
     Inspect … (15m) · Carry out … (35m)
```

Nothing outside `planner/data/` is ever written.

### Estimate first, place when you say so

`Plan it` only *derives* the idea — a task joins the calendar when you submit
it. The analysis card ends with **Place this week** (`POST /api/schedule`),
whose **Write events to Apple Calendar** checkbox also creates the events —
and deletes the events of the plan it replaces (carried pinned blocks
survive), so re-submitting never litters the calendar.

The estimate step itself is not a dumb placeholder — the two selects under the
box choose how it runs and how far it may reach:

- **Who estimates** — *Instant estimate (LLM)* (default): one synchronous
  call to the configured `brain.llm` endpoint (OpenRouter), and the full
  plan idea is on the task before the box clears — see the next section.
  *Offline estimate only* uses the offline rules. From the CLI,
  `add --desc` is the instant estimate; `--backend heuristic` forces the
  offline rules.
- **Plan within** — how many weeks ahead the planner may place the task
  (default `within 1 week` = this week only; *any week* removes the cap).
  `add --within N`, `edit --within N` (`0` = any week) and the drawer's
  **Plan within** select set the same `within_weeks` field. Re-plans never
  move a task past its window, and the auto cycle plans further weeks
  (`horizon_weeks`) only with whatever did not fit earlier.

### The instant estimate — the only path

Every estimate is one synchronous `/chat/completions` call to the configured
`brain.llm` endpoint carrying the full project context — the planner computes
the estimate itself, no external agent in the loop. When the task is grounded
in university material (see next section) and the model picks files whose full
text would change the plan, a **second call** re-estimates with those files'
actual text. So: 1 call normally, 2 when material grounding kicks in.

```bash
# OpenRouter — "openrouter/free" is a router across the current $0 models:
# it reroutes around per-model congestion (429s) and dead slugs
# (z-ai/glm-5.2:free died in Oct 2026). Prefer one specific model? Browse
# https://openrouter.ai/models?max_price=0 (e.g. google/gemma-4-31b-it:free)
# — a pinned slug can hit upstream 429s or disappear; either way intake
# falls back to the heuristic with the endpoint's exact reason
python3 -m planner.cli config \
     --set brain.llm.base_url="https://openrouter.ai/api/v1" \
     --set brain.llm.model="openrouter/free" \
     --set brain.llm.api_key_env="OPENROUTER_API_KEY"
export OPENROUTER_API_KEY=sk-or-…          # store it in your shell profile

# Z.ai own API — glm-4.7-flash is $0 input/output (permanent free tier)
python3 -m planner.cli config \
     --set brain.llm.base_url="https://api.z.ai/api/paas/v4" \
     --set brain.llm.model="glm-4.7-flash" \
     --set brain.llm.api_key_env="ZAI_API_KEY"
```

**Where the key lives** — the env var works only for servers started from a
shell that has it exported. Apps launched from Finder (the Dock app) never
read shell profiles, so give them the **key file** instead — it works for
every launch method:

```bash
printf 'sk-or-…' > planner/data/llm_key && chmod 600 planner/data/llm_key
```

The file is read per call, stripped, and never leaves the machine
(`planner/data/` is gitignored). Resolution order: env var first, then key
file, so the shell can always override; a custom path via
`brain.llm.key_file` (leading `~/` expands).

The analysis cites the model (`instant API estimate via openrouter/free`)
and the estimate lands as `est_source=llm`, so the same feedback/calibration
loop keeps working. A failing or mis-configured endpoint falls back to the
heuristic with the reason shown — it never crashes intake. `/api/state` calls
the endpoint fully configured `settings.llm_ready`: base_url + model, and a
resolvable key (env var or key file) — or `brain.llm.api_key_env=""` for a
keyless server, which opts out of the key check on purpose.

OpenRouter specifics: 20 req/min; `:free` models allow 50 requests/day until
your account has $10 lifetime top-ups (1000/day afterwards — the credit
stays unspent), and free-tier prompts may be used for training unless you
disable that in OpenRouter's Privacy settings. Z.ai's flash tier is $0
outright. Either way, **never point `brain.llm` at the HPC vLLM link**.

### Flag the next presentation meeting

The ⚑ button next to a calendar event in the Today rail flags it as the next
presentation/PI meeting — or set it by hand:

```bash
python3 -m planner.cli meeting set --date 2026-11-10 --title "PI meeting"
python3 -m planner.cli meeting show      # / clear
```

The masthead shows `⚑ <meeting> · in 13d` (click to clear), and the flag is
included in **every brain prompt** as a `## NEXT PRESENTATION MEETING`
section: date, days out, title, and how many minutes are already scheduled
before it — so the agent's estimates and priorities reflect how the plan is
proceeding toward the meeting, not just an abstract calendar. New intake
without a deadline defaults to the meeting date.

## University categories — grounded course work

The planner is no longer thesis-only. Point it at your university folder (an
iCloud Drive folder with **one subfolder per subject**) and every task can be
grounded in the actual course material:

```bash
python3 -m planner.cli config \
     --set university_root="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Uni"
```

* **Categories** — `Thesis` plus one entry per subject folder (`NLP Semantics`,
  `Formal Methods`, …). The UI intake gains a picker; the CLI gets
  `add --desc "…" --category "NLP Semantics"`. The chosen category wins as the
  task's area; without it the area is inferred from the wording as before.
* **Material awareness** — intake walks the subject folder (depth ≤ 2, ≤ 400
  entries, `.DS_Store` and dotfiles skipped; iCloud placeholders like
  `.essay.pdf.icloud` are listed as "not downloaded, content unknown") and puts
  the listing plus your `planner-notes.md` for that subject into the prompt.
  Write `planner-notes.md` in a subject folder to inject standing guidance
  ("Prof wants typicality effects cited", office hours, grading quirks).
* **Significant files** — the model returns up to 6 `significant_files`
  (`{path, why}`) whose **full text** would change the estimate. Those files
  are then read (see below) and a second LLM call refines the plan with the
  real text. The analysis names every grounded file with its why:
  `grounded in 1 file(s): NLP Semantics/lecture.pptx — the relevant week's slides`.
* **Full-text extraction** — PDFs through macOS PDFKit (osascript, 30 s hard
  timeout per file), PPTX/DOCX through stdlib zipfile. Scanned PDFs with no
  text layer and Keynote files degrade to an honest
  "content unknown" with the reason, never a hallucinated reading.
* **Budget & cache** — extraction shares a ~60k-character budget across the
  picked files (water-filling: a one-page syllabus never starves a 40-page
  paper), every truncation is marked in the text itself, and results are
  cached in `planner/data/extract_cache.json` keyed by path+mtime+size, so
  re-intakes don't re-parse.
* **Calendar naming** — planned blocks for a subject task are written as
  `NLP Semantics: <title>` in the Thesis calendar, and the two-way sync
  imports events with any known category prefix back into the right subject
  area (`Thesis: ` events keep working exactly as before).

Everything is read-only: `sources.py` never writes to your folders — the only
file it creates is its own extraction cache. Files not yet downloaded from
iCloud are listed but honestly marked; open them once (or right-click →
*Keep Downloaded*) to make their text available.

## Habits — recurring commitments

Not everything is a task with a finish line. **Gym three mornings a week,
20 minutes of Spanish every evening** — these repeat, and the scheduler has to
route around them as much as the thesis blocks.

* **Templates + instances** — a habit is a template (name, weekdays 0=Mon..6=Sun,
  either a fixed clock window like 07:00–08:00 or a flexible duration with a
  preferred part of the day). Every instance in the rolling window
  (`habits.weeks_ahead`, default: current + 1 week) is materialized into the
  database — one row per habit per day, with done/skipped state.
* **Chat intake, then review** — in the *Habits* tab, type one loose sentence
  ("gym mon wed fri 7-8am, spanish 20min every evening"). ONE LLM call turns it
  into proposals; you tick what to keep and save. Parsing never writes —
  nothing exists until you approve it. Rate cost: ~1 call per week.
* **Busy routing** — materialized habit spans are unioned into the busy map,
  so task blocks never collide with them. Re-plans respect habit spans the
  same way they respect pinned blocks.
* **Flexible placement** — a flexible habit takes the first free gap in its
  preferred part of the day (respecting lunch and existing plan slots; evening
  habits scan from day-end backwards). When nothing fits, the instance is
  reported as unplaced instead of silently dropped.
* **One-way mirror, self-healing** — habit instances are written to the Apple
  Calendar named `habits_calendar_name` (default **"Habits"**). One-way by
  design: Calendar.app is a mirror for these, not an editor (the two-way sync
  only reads the planner's own calendar). The mirror converges: every cycle
  re-reads what the calendar actually holds (one event query, no per-slot
  checks) and re-creates anything that vanished — a hand-deleted event, a
  wiped calendar, even a deleted *Habits* calendar is re-created on the next
  cycle or by the **Re-sync** button in the Instances card. A moved habit
  updates its event in place (same event id). Re-running with nothing
  changed is a no-op (span stamps skip osascript entirely). The planner's
  Habits window shows the database, not the calendar: deleting the calendar
  in Calendar.app never deletes your commitments — only deleting the habit
  in the planner does.
* **Deleting means deleted** — deleting a habit (✕ in Templates, two-click)
  removes the template, every materialized instance (past weeks included)
  and all its calendar events, at once. There is no soft state; nothing
  lingers greyed-out in the list.
* **Marking done** — each instance has ✓ done / ✕ skip buttons in the UI.
  Both survive re-materialization: the planner never re-marks a day it
  already asked you about. Every addition and state change confirms itself
  with a toast — tasks added by intake, habits saved, mirror problems
  (missing calendar, denied Automation grant) surface in the same way.

## The plan reviews itself — with a why

Once a day at most (and only when something real happened), the heartbeat
re-reads its own plan and asks: *does this still deserve its shape?* The
review is **one LLM call** over the current plan, the open tasks, the habit
load, and measured signals:

* **Triggers** — the calibration ratio moved ≥ 15% since the last review;
  busy minutes in the planned weeks changed (calendar moved); a task is due
  within `deadline_soon_days` with nothing scheduled; tasks were added or
  changed; the plan is older than 48 h. No trigger, no call — silence is the
  normal state.
* **Verdicts, each with its why** — `reestimate` (the estimate contradicts the
  evidence → the task is re-estimated by the next cycle), `rewindow` (it
  cannot honestly fit its window → `within_weeks` grows), `reprioritize`
  (deadline evidence changed), `keep` (examined and fine). Every verdict
  carries a one-line why, and `keep` is a first-class answer.
* **Safe application** — verdicts never touch blocks directly: pinned slots
  stay pinned, the normal re-plan acts on the new numbers. The whole report
  lands in the *Calibration* tab's **Last review** card — triggers, verdicts,
  what changed — with a *Review now* button (one call, on demand).

Tuning (rarely needed): `auto.review.enabled`, `auto.review.every_hours`
(default 24), `auto.review.stale_hours` (48), `auto.review.calibration_shift`
(0.15). Rate cost: ~1 call/day, well inside the free tier.

## Tell it how long it really took

Every task's drawer has **“How long did it really take?”** — presets plus
`25m` / `1h20`. Saving it recalibrates the estimator:

```bash
python3 -m planner.cli feedback 7 --took 20m --note "the table already existed"
```

- The ratio is the median of `actual/estimated`, **blended 3:1 with a prior of
  1.0**, so one fast task moves future estimates by ~20%, not by 60%.
- Per-area ratios need two samples before they are used, and are applied on top
  of the overall ratio.
- Everything is clamped to 0.5×–2.0×. The number on the Calibration tab is
  always recomputed from the samples, never read from a stale cache.

### Past tasks

Work finished *before* this app existed counts too: the Tasks view's
**+ past task** form (or `POST /api/tasks/past`) registers a task with its
real duration and completion date. It lands as `done` with backdated
doing→done events, so it appears in history, feeds the calibration ratio and
totals, and occupies a normal place in the task numbering — past and
live-tracked work form one continuous list.

```bash
curl -X POST localhost:8765/api/tasks/past -d '{"title": "Lit review",
  "actual_minutes": 220, "est_minutes": 300, "done": "2026-08-14"}'
```

## Apple Calendar

The planner reads your real availability, so blocks land *between* meetings
rather than on top of them.

```bash
python3 -m planner.cli calendar-sync     # once, in a terminal, to grant access
```

- `planner/calendar_probe.py` does the read in a subprocess with a hard timeout:
  a TCC prompt can block an Apple Event forever, and the UI must not care.
- The week is cached in `planner/data/calendar_cache.json`; `/api/state` serves
  the cache instantly and refreshes behind it.
- `POST /api/schedule` takes `{"wait": 20}` when the caller can afford to wait
  for a fresh read (the UI does; tests don't).
- Noise calendars (Birthdays, Holidays, Festività, Scheduled Reminders, Siri
  Suggestions) are ignored; 24-hour placeholder events are treated as all-day.
- Without permission the app still works — the rail says what to run, and
  scheduling simply ignores meetings.

### Two-way sync

The sync is genuinely two-way: the planner writes plan blocks as events,
and a reconciliation pass reads the calendar back and makes the plan match
what it sees (rolling window: 7 days back, 21 ahead, in the configured
calendar only):

- **Event deleted in Calendar.app** → the planner block is removed.
- **Event moved/resized in Calendar.app** → the block moves with it and is
  **pinned**, so a re-plan respects your hand edit instead of undoing it.
- **Event created in Calendar.app** (`Thesis: <title>`) → imported. If the
  title matches a task, the block is attached to it — an existing block at
  the same time just gets stamped with the event id, so nothing duplicates;
  otherwise a new todo task is created from the event's duration.
- Events in other calendars are ignored; all-day events are noise.

The web server reconciles about every 30 s (piggybacking on the cycle
watcher's ticks), so an
edit made in either place shows up in the other within ~half a minute:
app-side writes/edits hit Calendar immediately; Calendar-side edits appear
in the app on the next reconciliation or page refresh (the UI polls
`/api/state` every 20 s). Disable with `auto.calendar_sync=false`. Failures
(a lost Automation permission, a renamed calendar) become reported errors,
never crashes — the DB move always wins.

## Open it as an app, not a terminal

Two native bundles (Swift + AppKit/WebKit, no Xcode project, no dependencies):

```bash
bash packaging/macos/build_dock_app.sh --install      # → ~/Applications/Thesis Planner.app
bash packaging/macos/build_menubar_app.sh --install   # → ~/Applications/Thesis Planner Bar.app
```

- **Thesis Planner.app** — double-click; a 1280×900 window with the UI, starting
  the Python server itself if nothing is on :8765 (and stopping only a server it
  started). Reload ⌘R, open in browser ⇧⌘O.
- **Thesis Planner Bar.app** — a menu-bar status item showing the next block
  (`▸14:10 coref audit`), today's blocks in the menu, and Open / Re-plan now /
  Quit. It reads `planner.db` read-only, so it works with no server running.
- `http://127.0.0.1:8765/widget` — a 420 px auto-refreshing page for a small
  always-on window.

Details, verification steps and limitations: [`packaging/macos/README.md`](../packaging/macos/README.md).

## The brain — agentic estimation

You type a task in plain language; the **brain** reads the project and decides
what it really costs:

```bash
python3 -m planner.cli add "create an audit for coreference resolution" --ai
# → complexity M · 135 min · priority 1 · confidence 0.7
#   1. Sample 20 resolved rows (45 min)
#   2. Grade + write the audit (90 min)
#   why: reads resolved_chunks + audits/*.md; the last coref audit took ~2 h
```

What it reads (`planner/brain.py:project_context`): `planner/project_profile.md`
(the four pipeline phases, where work happens, the cost model), the last 15
commits, the repo tree, open threads from `audits/*.md`, your current tasks and
this week's plan, plus **calibration** — the median actual/estimated ratio from
tasks you already completed (`task_doing` → `task_done`).

Two backends (`plan config --set brain.backend=…`):

| backend | what happens | needs |
|---|---|---|
| **llm** (default) | one synchronous call to any OpenAI-compatible `/chat/completions` endpoint (`brain.llm.base_url`, `brain.llm.model`; key via `$PLANNER_LLM_KEY`-style env var **or** the `planner/data/llm_key` file) — drives intake and `brain enrich` | your own API key — **never the HPC link** |
| **heuristic** | offline keyword rules × calibration ratio | nothing |

`plan.json` — the strict object the model must return — is:
`{"title","area","complexity","estimate_minutes","priority","deep_work",
"confidence","subtasks":[{"title","minutes"}],"rationale"}`.

**Continuous planning** — the part that makes it autonomous:

```bash
python3 -m planner.cli auto --watch --interval 60     # every minute: estimate new tasks → re-plan → sync
python3 -m planner.cli auto --once                    # one cycle
python3 -m planner.cli tick                           # what launchd runs
```

Each cycle (shared by `auto`, `tick` and the web server through
`planner/auto.py:run_cycle`): estimates up to `--limit` tasks that still
lack an estimate (the LLM only when `auto.llm_estimates` is on — the
OpenRouter quota otherwise serves real intakes; the heuristic fills idle
ticks), updates the calibration ratio, and re-plans the current week when
anything changed (or the week has no plan). It never places a block before
*now*, and it never moves a slot you pinned by hand. Every step is logged
(`plan log`).

Two ways it runs itself:

- **While the web app is open**, a cycle watcher inside the server runs the
  cheap DB-only part of the cycle every tick (`auto.watch_seconds`, default
  20 s) — estimates, calibration, re-plans — and about every 30 s the Apple
  Calendar reconciliation rides along (see *Two-way sync*). Set
  `auto.watch=false` in the config to turn it off.
- **Without the app**, a launchd agent calls `tick` periodically:

```bash
python3 -m planner.cli install-launchd             # default: every 1 hour
python3 -m planner.cli install-launchd --minutes 15 # or every 15 minutes
launchctl load -w ~/Library/LaunchAgents/com.thesis.planner.plist
```

## How the scheduler decides

Ordering: **deadline → priority → complexity → size**. Blocks are 90 min max,
10-min buffers, lunch protected, 300 min/day cap, and `deep_min_block`
(default 45) stops a deep-work task from being shredded into a 20-minute
leftover — if nothing bigger is left that week it still lands, but the
rationale says so.

When the agent left subtasks, **block boundaries follow subtask boundaries**
(grouped up to `max_block_min`), so a block is a single thing you can finish and
the plan names it:

```
09:00-10:15  #2 rerun the 27B battery on the fixed consec gate  [75min]
   Mon 09:00-10:15; 75min block 1/2; complexity L (rank 5); priority P1;
   covers: Verify consec_error marker + heads_null counters; Smoke run on one chunk
```

Re-planning never treats its own previous slots as busy (`include_plan=False`),
so `auto`/`tick`/`schedule` always produce a fresh week instead of squeezing
into the gaps of the plan they replace. Two things survive every re-plan:

- **The clock.** Days before today are never used, and within today only the
  time after *now* (+ `now_margin_min`, rounded up to the next 5 minutes) is —
  a free gap two hours ago is not a slot for today's work. Clamped slots say
  so in their rationale (`…; not before now`).
- **Your corrections.** A slot you moved by hand is marked `pinned`; it counts
  as busy time for the re-plan and is carried over into the new plan unchanged
  (`save_plan` keeps its id, rationale and calendar event). An automatic cycle
  can therefore never undo a manual move.
- **The window.** A task may carry `within_weeks` — how many weeks (counting
  from this one) the planner may place it in. `schedule_week(…, weeks_out=N)`
  uses it to refuse tasks whose window ends before week *N*: week 0 is the
  current week, so `within_weeks=1` means *this week only* and further re-plans
  will never push the task out. Overflow is carried week by week — each task
  lands whole in the first week of its window where it fits.

## Move a block by hand

The agent's placement is a proposal; the drawer editor is where you overrule
it. Open a task, edit the **Placed** row (date / start / end), then **Move**:

- the block is re-planned at the new time and marked **pinned** (chip in the
  drawer) — future re-plans work around it and never drop it;
- the block keeps its slot id and rationale; moving across a Monday re-homes
  it into that week's plan (created with note "manual move" when absent);
- if the block was written to Apple Calendar, the event is **moved too**
  (best-effort; a denied permission becomes a message, never a crash);
- **Remove** unschedules just this block (the task stays) and deletes the
  matching calendar event if there is one.

### Drop vs Delete

Two ways to take a task out of play, and they clean up after themselves the
same way — both remove the task's scheduled blocks and delete their calendar
events — but they differ in what they remember:

- **Drop** (drawer) — *annul*. The task keeps a strikethrough record in the
  database and history (so you remember it existed and why), it disappears
  from the Tasks/week lists, and future plans never resurrect it. Its
  scheduled blocks come off the plan and their calendar events are deleted.
- **Delete** (hover **✕** on a Tasks row; `DELETE /api/tasks/<id>`) — *erase*.
  Blocks and calendar events go as with Drop, and the task record itself is
  removed. A `task_deleted` history event keeps the breadcrumb (title, area,
  estimate) so even a deletion is auditable. The id list is **compacted**:
  tasks above the deleted one shift down by one, so deleted tasks are not
  counted in the task identifiers and the numbering stays gap-free (gaps
  left by older deletions can still be closed with
  `python3 -m planner.cli renumber`). The button arms on the first
  click ("Delete?") and acts on the second — `confirm()` dialogs don't work
  inside the Dock app's WebView.

Same thing from the CLI:

```bash
python3 -m planner.cli slot move 12 --date 2026-10-29 --start 14:00 --end 15:30
python3 -m planner.cli slot drop 12
```

Under the hood both are `POST /api/slots/<id>` and `DELETE /api/slots/<id>` —
re-plans (`POST /api/schedule`) automatically respect pinned slots.

## How the scheduler decides (reference)

Deterministic and explainable — same inputs, same plan:

- **Order**: deadline (soonest first) → priority (1 = high) → complexity (L before M before S) → estimated minutes.
- **Blocks**: tasks longer than `max_block_min` (90) are split into chunks; each chunk is placed in a free interval with a `buffer_min` (10) gap.
- **Day window**: `work_days` (Mon–Fri), `day_start`–`day_end` (09:00–19:00), lunch blocked (13:00–14:00), daily cap `max_day_min` (300 min).
- **Placement**: high-complexity blocks prefer **morning** slots; low-complexity/admin prefer afternoons. Every slot carries a **rationale** string (e.g. “Mon 11:10–12:40; 90min block 1/2; complexity L (rank 5); priority P1; deadline 2026-11-03 in 2d; morning slot preferred for deep work”).
- **Overflow** is reported, not hidden: unscheduled tasks list the chunk and the reason (week full / daily cap).

Tune it in `planner/data/config.json` (or `config --set key=value`):

| key | default | meaning |
|---|---|---|
| `work_days` | `[0,1,2,3,4]` | Mon=0 … Sun=6 |
| `day_start` / `day_end` | `09:00` / `19:00` | daily window |
| `lunch` | `["13:00","14:00"]` | blocked every work day |
| `buffer_min` | `10` | gap between blocks |
| `max_block_min` | `90` | split longer tasks |
| `max_day_min` | `300` | planned work per day |
| `calendar_name` | `Thesis` | Apple Calendar to read/write |
| `habits_calendar_name` | `Habits` | mirror calendar for habit events (create it once in Calendar.app) |
| `habits.enabled` | `True` | habits materialize into the plan and mirror |
| `habits.weeks_ahead` | `1` | rolling window: current + N weeks get instances |
| `university_root` | `""` | folder with one subfolder per subject — enables the per-subject categories and folder-grounded estimates (iCloud Drive works; see *University categories*) |
| `thesis_root` | `""` | the thesis repository; empty = this checkout |
| `pi_meeting` | `null` | default deadline for `add` (informational) |
| `complexity_minutes` | `{S:45,M:90,L:180}` | default estimates |
| `now_margin_min` | `5` | today's earliest slot is *now* + this many minutes |
| `horizon_weeks` | `1` | weeks ahead the auto cycle may fill (a task's `within_weeks` may reach deeper) |
| `auto.review.every_hours` | `24` | wall-clock gate for the periodic plan review (see *The plan reviews itself*) |
| `auto.watch` / `auto.watch_seconds` / `auto.notify` | `True` / `20` / `False` | cycle watcher (seconds between ticks: estimates, re-plans, calendar sync), optional macOS notification |
| `auto.llm_estimates` | `False` | background estimation through the LLM; off, the OpenRouter quota serves real intakes and idle ticks fall back to the heuristic |
| `brain.llm.base_url` / `brain.llm.model` | `""` / `""` | OpenAI-compatible endpoint + model id for the `llm` backend and the instant *API estimate* mode (e.g. `https://openrouter.ai/api/v1` + `openrouter/free` — a router across the current $0 models; free slugs rotate, current list at `openrouter.ai/models?max_price=0`) |
| `brain.llm.api_key_env` | `PLANNER_LLM_KEY` | env var that carries the LLM key — rename per provider (e.g. `OPENROUTER_API_KEY`); set it to `""` to declare a keyless endpoint |
| `brain.llm.key_file` | `""` (→ `planner/data/llm_key`) | key file used when the env var is unset — the reliable way to hand Finder-launched servers a key (`printf 'sk-…' > planner/data/llm_key && chmod 600 …`); leading `~/` expands |
| `auto.calendar_sync` | `True` | the watcher's two-way Apple Calendar reconciliation (see *Two-way sync*); calendar_name selects the calendar |

## Apple Calendar integration

**Reading busy blocks** (used by `schedule`):

```bash
python3 -m planner.cli calendar-check --week this      # first run triggers a permission prompt
```

macOS asks once for permission: *System Settings › Privacy & Security › Automation › Terminal (or iTerm) → Calendar*. If you deny it, scheduling still works from the tasks alone — the planner just prints `[calendar] skipped: …` and plans into empty days.

**Writing the plan back** as calendar events:

```bash
python3 -m planner.cli schedule --week next --write-calendar
```

Each block becomes an event `Thesis: <task title>` in the configured calendar,
with the rationale, task number and the **subtasks that block covers** in the
description (the named subtask when the agent split the task across blocks,
else the full list). The slot remembers its event id, so a later **manual move
or remove updates/deletes the same event** (best-effort, see *Move a block by
hand*) instead of leaving a stale ghost behind — and a move of a block that
somehow has no event yet writes it on the spot (self-heal), so the calendar can
never fall behind. Events are created in the named calendar (create that
calendar once in Calendar.app; the planner does not create calendars).

**No permission? Export instead:**

```bash
python3 -m planner.cli export --week next --ics planner/data/exports/week.ics
open planner/data/exports/week.ics        # double-click → Calendar imports it
```

## Project awareness (optional helpers)

```bash
python3 -m planner.cli scan          # suggests tasks (does not add them)
python3 -m planner.cli scan --add    # adds them as small tasks (S, 30 min)
```

Sources: `TODO/FIXME/XXX` in `src/` + `slurm_scripts/`, uncommitted files (`git status`), and open items found in `audits/*.md` under “Still open / open question / deferred” headings. Every suggestion records its source in the task notes.

## Timeline / backfill (April → now)

```bash
python3 -m planner.cli backfill --since 2026-04-01     # idempotent: one row per commit
python3 -m planner.cli history                          # monthly digest on screen
python3 -m planner.cli history --md audits/work_history.md
```

For work that predates the repository (your git history starts **2026-08-17**), add
notes by hand — they join the same timeline and appear in the digest:

```bash
python3 -m planner.cli note "Built the Gemini→WordNet KB generation pipeline" --date 2026-04-20 --kind milestone
python3 -m planner.cli note "First multi-srl baseline on VerbAtlas" --date 2026-05-12
```

`backfill` ingests every commit (`kind=commit`, `source=git:<hash>`, deduplicated), so the digest reconstructs the whole project arc — April through today — instead of only the last few weeks. Run folders and audit reports can be added with the same `add_history` API (see `planner/backfill.py`).

## Web UI (the “app”)

```bash
python3 -m planner.cli web --open        # http://127.0.0.1:8765
```

One page: add a task, the task table with a **Done** button, **Schedule this week** (saves the plan and shows the week grid + unscheduled warnings), and the recent log. It is a plain `http.server` app — no build step, no JS framework.

**Make it feel like an app:** open `http://127.0.0.1:8765` in Safari → *File › Add to Dock* (macOS Sonoma+). It gets its own icon/window and behaves like a widget. Alternatives: a Shortcuts.app shortcut that runs `python3 -m planner.cli tick`, or a menu-bar item with `pip install rumps` (optional, not required).

## Periodic mode (runs itself)

```bash
python3 -m planner.cli tick --notify          # plan the current week if not planned yet + notification
python3 -m planner.cli install-launchd        # every hour by default
launchctl load -w ~/Library/LaunchAgents/com.thesis.planner.plist
```

`install-launchd` only **writes** the plist (it prints the `launchctl` lines); loading it is your call. `tick` is idempotent: an already-planned week is left alone unless `--force`. The default cadence is **1 hour** (`--hours 1`); use `--minutes 15` for a tighter loop while working.

## Logging and progress

Every action appends to `events` (task added/updated/done, plan created, scan, backfill) with a timestamp — `python3 -m planner.cli log --tail 50`. The `history` table is the long arc (commits, audits, milestones). Both are queryable from the web UI and exportable (`history --md`).

## Data model (SQLite)

| table | purpose |
|---|---|
| `tasks` | id, title, notes, area, status, priority, complexity, est_minutes, deadline, files, `within_weeks` (planning window), timestamps |
| `events` | append-only log: ts, kind, task_id, payload JSON (`meeting_set` / `meeting_cleared` included) |
| `plans` / `slots` | a saved week and its concrete blocks (date, start, end, minutes, rationale, `pinned` for hand-moved blocks, calendar_event_id) |
| `kv` | small long-lived flags — the `next_meeting` flag and the calibration ratio |
| `history` | dated timeline rows (commit / audit / run / note / milestone) |

## Tests

```bash
python3 -m unittest discover -s planner/tests -v
```

Core tests cover the store (CRUD, plans, log, history) and the scheduler (lunch/busy handling, splitting, daily cap, overflow, morning preference). Module tests cover the web endpoints, the calendar adapter (with a fake runner — no real AppleScript), and backfill/scan (in a temporary git repo).

## Files

```
planner/
  config.py        defaults + load/save
  store.py         SQLite persistence
  scheduler.py     deterministic week planner (never plans before now)
  auto.py          shared auto cycle + cycle watcher (estimates, re-plans,
                   calendar sync)
  calendar_mac.py  Apple Calendar (JXA) + ICS export
  sources.py       university folder inventory + full-text extraction (PDF/
                   PPTX/DOCX) for grounded estimates
  habits.py        recurring commitments: materialization, flexible
                   placement, one-way Habits-calendar mirror, chat parsing
  review.py        periodic plan review: real triggers, one LLM call,
                   why-verdicts (reestimate/rewindow/reprioritize/keep)
  project_scan.py  TODO / git / audit scanning
  backfill.py      git history → timeline + digests
  cli.py           command line (all commands above)
  web.py           local web UI
  tests/           unittest suite
  data/            planner.db, config.json, extract_cache.json, exports
                   (gitignored)
```
