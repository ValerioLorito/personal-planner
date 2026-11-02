"""
auto.py — the autonomous planning cycle, shared by `cli auto`, `cli tick`
(launchd) and the web server's agent-queue watcher.

One cycle:

  1. expire agent requests that were never answered (heuristic fallback),
  2. apply every answered request waiting in planner/data/agent_queue/,
  3. estimate tasks that still have no estimate (heuristic / llm / queued),
  4. update the calibration ratio,
  5. run the periodic plan review when a real trigger fires (see review.py):
     one LLM call, verdicts with their why, safe ones applied, re-estimates
     folded straight back into this cycle's enrichment,
  6. materialize + mirror habits (see habits.py),
  7. re-plan the current week when anything changed (or no plan exists).

Re-planning is honest about time and about the user:

  * never places a block before "now" (see scheduler.schedule_week), so
    yesterday's free gaps are never used for today's work;
  * slots the user moved by hand are `pinned`: they count as busy for the
    re-plan and are carried over into the new plan, so an automatic cycle
    cannot undo a manual correction.

The cycle is idempotent: running it twice in a row changes nothing the
second time, which is why it is safe for both a 20-second watcher and a
1-hour launchd tick.
"""
import os
import subprocess
import sys
import threading
import time
from datetime import date, timedelta

from planner.brain import enrich_task, update_calibration
from planner.scheduler import parse_hhmm, schedule_week, week_dates, week_start_for

CYCLE_LOCK = threading.Lock()      # one cycle at a time, everywhere


def busy_map(store, ws, cfg, use_calendar=True, calendar_name=None,
             include_plan=False, pinned=True):
    """Busy intervals for the week: {"YYYY-MM-DD": [(start_min, end_min), ...]}.

    include_plan  include *every* saved slot of the week (views/exports).
    pinned        include saved slots the user pinned by hand: a re-plan must
                  route around them, because save_plan() carries them over.

    `include_plan` must stay False whenever the caller is about to replace
    this week's plan — otherwise the new plan is squeezed into the gaps left
    by the plan it replaces.
    """
    busy = {}
    for d in week_dates(ws):
        key = d.isoformat()
        day = []
        if include_plan:
            for s in store.slots_between(key, key):
                day.append((parse_hhmm(s["start"]), parse_hhmm(s["end"])))
        elif pinned:
            for s in store.pinned_slots_between(key, key):
                day.append((parse_hhmm(s["start"]), parse_hhmm(s["end"])))
        # habits are commitments too: their materialized spans are busy
        try:
            for h in store.habit_slots_between(key, key):
                day.append((parse_hhmm(h["start"]), parse_hhmm(h["end"])))
        except Exception:
            pass                    # an old DB without the tables: ignore
        busy[key] = day
    if use_calendar:
        try:
            from planner.calendar_mac import busy_to_minutes, read_busy
            evs = read_busy(ws, ws + timedelta(days=6),
                            calendar_name or cfg["calendar_name"])
            for d in week_dates(ws):
                key = d.isoformat()
                busy[key] += busy_to_minutes(evs, key)
        except Exception as e:  # permission missing, no calendar, etc.
            print(f"[calendar] skipped: {e}", file=sys.stderr)
    return busy


def _event_prefixes(cfg):
    """Summaries this planner owns: '<category>: ' for every known category
    (Thesis + subject folders), plus the bare legacy 'Thesis:' fallback."""
    prefixes = ["Thesis: ", "Thesis:"]
    try:
        from planner import sources
        for c in sources.categories(cfg or {}):
            for p in ("%s: " % c, "%s:" % c):
                if p not in prefixes:
                    prefixes.append(p)
    except Exception:
        pass
    return prefixes


def _strip_prefix(title, cfg):
    """("NLP Semantics", "reading response") — the category and the bare
    title, or ("", title) when no known prefix matches."""
    for prefix in _event_prefixes(cfg):
        if title.startswith(prefix):
            return prefix.rstrip().rstrip(":").strip(), title[len(prefix):]
    return "", title


def sync_calendar(store, cfg, days_back=7, days_ahead=21):
    """Pull Apple Calendar's side of the story back into the plan.

    Apple Calendar is a mirror the user also edits by hand, so the sync is
    genuinely two-way. For the configured calendar (default `Thesis`) over a
    rolling window around today:

      * event deleted in Calendar.app            -> its slot is removed;
      * event moved/edited in Calendar.app       -> the slot moves with it
        and is pinned (a re-plan must respect the user's hand correction);
      * event created in Calendar.app            -> imported: attached to the
        task its `<category>: <title>` summary names (any known category
        prefix matches — Thesis or a subject folder; existing block at the
        same time just gets its event id stamped in), otherwise added as a
        new todo task estimated from the event's duration.

    Returns a report dict; every failure becomes data (`{"error": ...}`),
    never an exception — the same contract as the calendar write path.
    """
    report = {"moved": 0, "removed": 0, "imported": 0, "error": None}
    cal = (cfg or {}).get("calendar_name") or "Thesis"
    start = date.today() - timedelta(days=days_back)
    end = date.today() + timedelta(days=days_ahead)
    try:
        from planner.calendar_mac import week_events
        payload = week_events(start, end, calendar_names=[cal])
    except Exception as exc:
        report["error"] = str(exc)
        return report
    if not isinstance(payload, dict) or payload.get("error"):
        report["error"] = (payload or {}).get("error") or "no calendar payload"
        return report

    by_uid = {}
    for e in payload.get("events") or []:
        if e.get("all_day") or not e.get("start") or not e.get("end"):
            continue                       # all-day/noise: not plan material
        uid = str(e.get("id") or "")
        if uid:
            by_uid[uid] = e

    # uid matching runs over ALL event-bearing slots, not just the window:
    # an event read inside the window may belong to a block planned further
    # out, and deleting it would be wrong while moving it is right. Only the
    # delete detection needs the window (week_events simply cannot see
    # events outside it, so absence proves nothing there).
    lo, hi = start.isoformat(), end.isoformat()
    slots = [dict(r) for r in store.conn.execute(
        "SELECT * FROM slots WHERE calendar_event_id IS NOT NULL")]
    seen = set()
    for s in slots:
        uid = s["calendar_event_id"]
        e = by_uid.get(uid)
        if e is None:
            if lo <= s["date"] <= hi:      # deleted in Calendar.app
                store.delete_slot(s["id"])
                report["removed"] += 1
            continue
        seen.add(uid)
        if (e.get("date"), e.get("start"), e.get("end")) != \
                (s["date"], s["start"], s["end"]):
            try:
                store.move_slot(s["id"], e.get("date"), e.get("start"),
                                e.get("end"))
            except ValueError:
                continue                   # midnight-crossing oddity: skip
            store.conn.execute("UPDATE slots SET pinned=1 WHERE id=?",
                               (s["id"],))
            store.conn.commit()
            report["moved"] += 1

    for uid, e in by_uid.items():
        if uid in seen:
            continue
        category, title = _strip_prefix(str(e.get("title") or ""), cfg)
        title = title.strip()
        if not title:
            continue
        dur = (parse_hhmm(e["end"]) - parse_hhmm(e["start"])) % (24 * 60)
        if dur <= 0:
            continue
        date_iso, start_hm, end_hm = e["date"], e["start"], e["end"]
        tasks = {t["title"].strip().lower(): t for t in store.list_tasks()}
        task = tasks.get(title.lower())
        if task:
            twin = [x for x in store.slots_for_task(task["id"])
                    if (x["date"], x["start"], x["end"])
                    == (date_iso, start_hm, end_hm)]
            if twin:                       # the block exists: stamp its event
                if not twin[0].get("calendar_event_id"):
                    store.set_slot_calendar_id(twin[0]["id"], uid)
                continue
        tid = task["id"] if task else store.add_task(
            title, est_minutes=dur, notes="imported from Apple Calendar",
            area=category, within_weeks=1)
        store.add_slot(tid, date_iso, start_hm, end_hm, minutes=dur,
                       rationale="from Apple Calendar", pinned=1,
                       calendar_event_id=uid)
        report["imported"] += 1
    return report


def _notify(msg):
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "Thesis planner"'],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def run_cycle(store, cfg, repo_root=".", limit=5, enrich=True, force=False,
              use_calendar=True, notify=False, reason=None, calendar_sync=True):
    """One planning cycle; returns a summary dict (never raises).

    {"enriched", "calibration", "replanned", "unscheduled", "plan", "plans",
     "planned_weeks", "skipped", "calendar_sync"}

    The planner may reach further than one week: `cfg["horizon_weeks"]` (and
    each task's own `within_weeks` window, which may reach deeper) defines the
    weeks the cycle is allowed to fill — week 0 (current) first, then the
    following ones with whatever did not fit earlier.

    `calendar_sync` pulls Apple Calendar's side of the story into the plan
    (see sync_calendar); it needs `use_calendar` too. The cycle watcher passes
    False on most ticks to keep the osascript load off the cheap DB-only pass.

    Background estimation runs on the configured backend — except that the
    LLM is only used when `auto.llm_estimates` is on, so the OpenRouter
    quota is spent on real intakes, not on idle ticks (heuristic fills the
    gap otherwise; intake always estimates properly).

    `skipped` is True when another cycle is already running (a poll that
    arrived while a manual cycle was in flight simply backs off).
    """
    if not CYCLE_LOCK.acquire(blocking=False):
        return {"enriched": 0, "calibration": None, "replanned": False,
                "unscheduled": 0, "plan": None, "plans": [],
                "planned_weeks": [], "skipped": True, "calendar_sync": None}
    try:
        enriched = 0
        if enrich:
            est_cfg = cfg
            if not (cfg.get("auto") or {}).get("llm_estimates") \
                    and ((cfg.get("brain") or {}).get("backend")) == "llm":
                est_cfg = dict(cfg)
                est_cfg["brain"] = dict(cfg.get("brain") or {})
                est_cfg["brain"]["backend"] = "heuristic"
            for t in store.needs_estimate(limit=limit):
                try:
                    res = enrich_task(store, est_cfg, t["id"],
                                      repo_root=repo_root)
                except Exception as exc:   # llm down/misconfigured: heuristic
                    print("[auto] estimate for #%s fell back: %s"
                          % (t["id"], exc), file=sys.stderr, flush=True)
                    continue
                if res["status"] == "applied":
                    enriched += 1
        ratio = update_calibration(store)

        # periodic plan review: real triggers only, at most every_hours,
        # ONE LLM call, safe verdicts only — the re-plan below acts on them
        review_report = None
        review_applied = 0
        if (cfg.get("auto") or {}).get("review", {}).get("enabled", True):
            try:
                from planner import review as review_mod
                review_report = review_mod.run_review(
                    store, cfg, use_calendar=use_calendar)
                review_applied = int(review_report.get("applied") or 0)
            except Exception as exc:
                print("[review] skipped: %s" % exc, file=sys.stderr)

        # tasks the review sent back for re-estimation: fold them into this
        # cycle's enrichment instead of waiting a full tick
        if review_applied and enrich:
            for t in store.needs_estimate(limit=limit):
                try:
                    res = enrich_task(store, est_cfg, t["id"],
                                      repo_root=repo_root)
                except Exception:
                    continue
                if res["status"] == "applied":
                    enriched += 1

        # habits: materialize the rolling window and keep the Habits calendar
        # in step (pure DB unless something changed)
        habits_report = None
        if (cfg.get("habits") or {}).get("enabled", True):
            try:
                from planner import habits as habits_mod
                habits_report = habits_mod.materialize_and_mirror(
                    store, cfg,
                    weeks_ahead=int((cfg.get("habits") or {})
                                    .get("weeks_ahead", 1)),
                    mirror_cal=use_calendar)
            except Exception as exc:
                print("[habits] skipped: %s" % exc, file=sys.stderr)

        cal_report = None
        if use_calendar and calendar_sync \
                and (cfg.get("auto") or {}).get("calendar_sync", True):
            cal_report = sync_calendar(store, cfg)

        ws = week_start_for()
        habit_delta = 0
        if habits_report:
            m = habits_report.get("mirror") or {}
            habit_delta = (habits_report.get("added", 0)
                           + habits_report.get("moved", 0)
                           + habits_report.get("removed_events", 0)
                           + m.get("mirrored", 0) + m.get("updated", 0))
        changed = enriched or force \
            or store.get_plan(ws.isoformat()) is None \
            or habit_delta > 0 \
            or review_applied > 0 \
            or bool(cal_report and (cal_report.get("moved")
                                    or cal_report.get("removed")
                                    or cal_report.get("imported")))
        result = {"enriched": enriched, "calibration": ratio,
                  "replanned": False, "unscheduled": 0, "plan": None,
                  "plans": [], "planned_weeks": [],
                  "skipped": False, "week": ws.isoformat(), "any_tasks": True,
                  "review": review_report, "habits": habits_report,
                  "calendar_sync": cal_report}

        tasks = store.list_tasks()
        if changed and tasks:
            plans = plan_horizon(store, cfg, ws, tasks, use_calendar=use_calendar)
            result["plans"] = plans
            result["planned_weeks"] = [p["week"] for p in plans]
            placed = next((p for p in plans if p["slots"]), None)
            if placed:
                # "replanned" means blocks were actually written — an
                # unplaceable week (weekend with horizon 1) must not claim a
                # re-plan, or every tick would report one forever
                result["replanned"] = True
                result["unscheduled"] = len(placed["unscheduled"])
                result["plan"] = placed
        elif not tasks:
            result["any_tasks"] = False
        if notify and (applied or enriched):
            _notify("%d task(s) estimated, %d answer(s) applied"
                    % (enriched, applied))
        if reason:
            result["reason"] = reason
        return result
    finally:
        CYCLE_LOCK.release()


def plan_horizon(store, cfg, week_start, tasks, use_calendar=True):
    """Plan weeks week_start .. week_start + horizon - 1, first-fit.

    A task lands whole in the first week of its window where it fits; if a
    week cannot absorb it (calendar full, daily cap), it is carried into the
    following weeks. Each task's `within_weeks` caps how deep it may go; when
    no task reaches beyond cfg["horizon_weeks"], that config value alone
    decides how many weeks are planned.
    """
    try:
        horizon = max(1, int(cfg.get("horizon_weeks", 1) or 1))
    except (TypeError, ValueError):
        horizon = 1
    windows = [int(t.get("within_weeks") or 0) for t in tasks
               if t.get("within_weeks")]
    weeks = min(26, max(horizon, max(windows) if windows else 1))

    remaining = list(tasks)
    plans = []
    for wi in range(weeks):
        if not remaining:
            break
        week = week_start + timedelta(days=7 * wi)
        res = schedule_week(remaining, week,
                            busy_map(store, week, cfg, use_calendar=use_calendar),
                            cfg, weeks_out=wi)
        if res["slots"]:
            store.save_plan(res["week"], res["slots"], note="auto")
        placed = {s["task_id"] for s in res["slots"]}
        remaining = [t for t in remaining
                     if t["id"] not in placed
                     and t.get("status") in ("todo", "doing")]
        plans.append(res)
    return plans


# ------------------------------------------------------------------ watcher
def cycle_watcher(api_root, db_path, cfg_path, poll_seconds=None, quiet=False):
    """Generator-free background job: the planner's heartbeat.

    Every `poll_seconds` (or the config's `auto.watch_seconds`) one cheap
    DB-only run_cycle() runs: tasks left without an estimate get the
    heuristic one, calibration is refreshed, and the week is re-planned when
    anything changed. Apple Calendar reconciliation rides the same loop on
    its own ~30 s cadence — the osascript read is too costly for every tick.
    """
    from planner import config as config_mod
    from planner.store import Store

    if poll_seconds is None:
        try:
            poll_seconds = float(
                (config_mod.load(cfg_path).get("auto") or {}).get(
                    "watch_seconds", 20))
        except Exception:
            poll_seconds = 20.0
    poll_seconds = max(5.0, poll_seconds)

    # Apple Calendar reconciliation is one osascript read per pass — run it
    # on its own ~30 s cadence, not on every cheap DB-only tick.
    sync_every = max(1, int(round(30.0 / poll_seconds)))
    tick = 0
    while True:
        time.sleep(poll_seconds)
        tick += 1
        try:
            cfg = config_mod.load(cfg_path)
        except Exception:
            continue
        if not (cfg.get("auto") or {}).get("watch", True):
            continue
        try:
            store = Store(db_path)
            res = run_cycle(store, cfg, repo_root=api_root,
                            notify=bool((cfg.get("auto") or {}).get("notify")),
                            calendar_sync=tick % sync_every == 0)
            cs = res.get("calendar_sync") or {}
            if not quiet and (res["enriched"] or res["replanned"]
                              or cs.get("moved") or cs.get("removed")
                              or cs.get("imported")):
                print("[watch] enriched=%d replanned=%s unscheduled=%d"
                      " calendar(moved=%s removed=%s imported=%s)"
                      % (res["enriched"], res["replanned"],
                         res["unscheduled"], cs.get("moved"),
                         cs.get("removed"), cs.get("imported")),
                      flush=True)
        except Exception as exc:  # a bad tick must not kill the watcher
            print("[watch] cycle failed: %s" % exc, file=sys.stderr, flush=True)


# backwards-compatible alias (web.serve called it start_queue_watcher)
def start_cycle_watcher(api_root, db_path, cfg_path, poll_seconds=None,
                        quiet=False, _state={"thread": None}):
    """Start (once per process) the cycle watcher in a daemon thread."""
    if _state["thread"] is not None and _state["thread"].is_alive():
        return _state["thread"]
    _state["thread"] = threading.Thread(
        target=cycle_watcher,
        args=(api_root, db_path, cfg_path, poll_seconds, quiet),
        daemon=True, name="planner-cycle-watcher")
    _state["thread"].start()
    return _state["thread"]
