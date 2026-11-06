"""
cli.py — command line for the thesis planner.

    python -m planner.cli add "Fix itemize validator" --complexity M --est 90 \
        --priority 1 --deadline 2026-11-03
    python -m planner.cli add --desc "create an audit for coreference resolution"
    python -m planner.cli feedback 3 --took 20m --note "the script existed"
    python -m planner.cli list
    python -m planner.cli schedule --week next --write-calendar
    python -m planner.cli plan --week this
    python -m planner.cli done 3 --note "validator merged"
    python -m planner.cli scan --add
    python -m planner.cli backfill && python -m planner.cli history --md
    python -m planner.cli web --open

Everything is local: SQLite in planner/data/planner.db, config in
planner/data/config.json. Apple Calendar is optional (see `calendar-check`).
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from planner import config as config_mod
from planner.scheduler import (schedule_week, week_dates, week_start_for)
from planner.store import Store

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DB = Path(os.environ.get("PLANNER_DB", DATA / "planner.db"))
CFG = Path(os.environ.get("PLANNER_CONFIG", DATA / "config.json"))


# ---------- helpers ----------
def get_store():
    return Store(str(DB))


def get_cfg():
    return config_mod.load(str(CFG))


def parse_week(value):
    if not value or value in ("this", "now"):
        return week_start_for()
    if value == "next":
        return week_start_for(offset_weeks=1)
    if value == "prev":
        return week_start_for(offset_weeks=-1)
    return week_start_for(date.fromisoformat(value))


def _within_arg(raw, flag="--within"):
    """'Plan within N weeks' -> int 1..26; 0 or empty means 'any week'."""
    if raw is None:
        return None
    try:
        weeks = int(raw)
    except (TypeError, ValueError):
        sys.exit(f"{flag}: expected a number of weeks, got {raw!r}")
    if weeks == 0:
        return None
    if not 1 <= weeks <= 26:
        sys.exit(f"{flag}: must be between 1 and 26 (0 = any week)")
    return weeks


def cmd_renumber(args):
    """One-shot compaction of the task id list (deletions already compact
    going forward; this closes the gaps that predate that change)."""
    before, after = get_store().renumber_tasks()
    print(f"task ids compacted: highest id {before} -> {after}")


def cmd_add(args):
    st = get_store()
    if getattr(args, "desc", None):
        return _cmd_add_from_description(st, args)
    if not getattr(args, "title", None):
        sys.exit("add: give a title or --desc \"one sentence\"")
    tid = st.add_task(args.title, notes=args.notes or "", area=args.area or "",
                      priority=args.priority, complexity=args.complexity,
                      est_minutes=args.est, deadline=args.deadline,
                      files=args.files or "",
                      within_weeks=_within_arg(args.within))
    if getattr(args, "json", False):
        print(json.dumps(st.get_task(tid), indent=2, ensure_ascii=False))
        return
    print(f"added task #{tid}: {args.title}")
    if getattr(args, "ai", False):
        _print_enrichment(st, get_cfg(), tid)


def _cmd_add_from_description(st, args):
    """`add --desc` — one sentence in, derived task + cited artefacts out."""
    from planner.intake import create_from_description
    try:
        res = create_from_description(st, get_cfg(), args.desc,
                                      repo_root=str(BASE.parent),
                                      deadline=args.deadline,
                                      category=getattr(args, "category", None),
                                      backend_override=getattr(args, "backend", None) or None,
                                      within_weeks=getattr(args, "within", None))
    except ValueError as exc:
        sys.exit(f"add: {exc}")
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return
    analysis = res["analysis"]
    task = st.get_task(res["task_id"]) or {}
    print(f"added task #{res['task_id']}: {task.get('title') or analysis['summary']}")
    print(f"  estimate: {analysis['complexity']} · {analysis['minutes']} min · "
          f"priority {analysis['priority']} · area {analysis['area']} · "
          f"confidence {analysis['confidence']:.2f} · {res['status']}")
    print(f"  summary:  {analysis['summary']}")
    for line in analysis["why"]:
        print(f"  why:      {line}")
    if analysis["related"]:
        print("  related:")
        for item in analysis["related"]:
            print(f"    - {item['path']} — {item['note']}")
    else:
        print("  related:  (none found)")
    if analysis["subtasks"]:
        print("  subtasks:")
        for index, step in enumerate(analysis["subtasks"], 1):
            print(f"    {index}. {step['title']} ({step['minutes']} min)")
    if res.get("note"):
        print(f"  note:     {res['note']}")


def cmd_feedback(args):
    """`feedback <id> --took 20m` — store the real time, recalibrate."""
    st = get_store()
    from planner.intake import apply_feedback, parse_duration
    try:
        minutes = parse_duration(args.took)
        res = apply_feedback(st, get_cfg(), args.id, minutes,
                             note=args.note or "")
    except ValueError as exc:
        sys.exit(f"feedback: {exc}")
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return
    before, after = res["before"], res["after"]
    calibration = res["calibration"]
    plural = "" if calibration["samples"] == 1 else "s"
    print("task #%d: estimated %d min → actual %d min · ratio %.2f → %.2f "
          "(%d sample%s) · next similar %d → %d min (%+d%%)"
          % (args.id, before["estimate"], res["task"]["actual_minutes"],
             before["ratio"], after["ratio"], calibration["samples"], plural,
             before["expected_next"], after["expected_next"],
             after["change_pct"]))
    if res["task"].get("feedback_note"):
        print("  note: %s" % res["task"]["feedback_note"])


def _print_enrichment(st, cfg, tid):
    from planner.brain import BrainError, enrich_task
    try:
        res = enrich_task(st, cfg, tid, repo_root=str(BASE.parent))
    except BrainError as e:
        print(f"[brain] {e}", file=sys.stderr)
        return None
    plan = res["plan"]
    print(f"[brain/{res['backend']}] complexity {plan['complexity']} · "
          f"{plan['estimate_minutes']} min · priority {plan['priority']} · "
          f"confidence {plan['confidence']:.2f}")
    if plan.get("rationale"):
        print(f"        why: {plan['rationale']}")
    for i, sub in enumerate(plan.get("subtasks") or [], 1):
        print(f"        {i}. {sub['title']} ({sub['minutes']} min)")
    return res


def cmd_list(args):
    st = get_store()
    tasks = st.list_tasks(status=args.status)
    if not tasks:
        print("no tasks")
        return
    print(f"{'id':>3}  {'status':<6} {'P':>1} {'cx':<2} {'est':>4}  {'deadline':<10}  title")
    for t in tasks:
        print(f"{t['id']:>3}  {t['status']:<6} {t['priority']:>1} "
              f"{str(t['complexity']):<2} {t['est_minutes']:>4}  "
              f"{(t['deadline'] or '-'):<10}  {t['title']}")
    print(f"\n{len(tasks)} task(s)")


def cmd_show(args):
    st = get_store()
    t = st.get_task(args.id)
    if not t:
        sys.exit(f"no task #{args.id}")
    for k, v in t.items():
        print(f"{k:>11}: {v}")


def cmd_status(args):
    st = get_store()
    st.set_status(args.id, args.status, note=args.note or "")
    print(f"task #{args.id} -> {args.status}")


def cmd_edit(args):
    st = get_store()
    fields = {}
    for k in ("title", "notes", "area", "priority", "complexity",
              "est_minutes", "deadline", "files", "within_weeks"):
        v = getattr(args, k, None)
        if v is not None:
            fields[k] = _within_arg(v) if k == "within_weeks" else v
    st.update_task(args.id, **fields)
    print(f"task #{args.id} updated: {', '.join(fields) or '(nothing)'}")


def cmd_meeting(args):
    """Flag / clear / show the next presentation meeting the planner plans around.

    `plan meeting set --date 2026-11-10 --title "PI meeting"` — or flag it
    straight from a calendar event in the web UI (`⚑` next to the event).
    The date reaches every estimate prompt (`## NEXT PRESENTATION MEETING`)
    and doubles as the default deadline for new intake without one.
    """
    st = get_store()
    if args.action == "clear":
        st.set_next_meeting(None)
        print("next-meeting flag cleared")
        return
    if args.action == "set":
        if not args.date:
            sys.exit('meeting set needs --date YYYY-MM-DD '
                     '(see: plan calendar-check for upcoming events)')
        try:
            when = date.fromisoformat(args.date)
        except ValueError:
            sys.exit('meeting set: --date must be YYYY-MM-DD')
        info = st.set_next_meeting({
            "date": when.isoformat(),
            "title": args.title or "PI meeting",
            "calendar": args.calendar or "",
            "id": args.id or "",
        })
        days = (date.fromisoformat(info["date"]) - date.today()).days
        print(f"next presentation meeting: {info['date']} ({days} day(s) out) "
              f"— {info['title']}")
        print("the agent now sees it in every estimate prompt; new intake "
              "without a deadline defaults to it")
        return
    info = st.get_next_meeting()
    if not info:
        print("no meeting flagged — set one with: plan meeting set --date YYYY-MM-DD")
        return
    days = (date.fromisoformat(info["date"]) - date.today()).days
    print(f"next presentation meeting: {info['date']} ({days} day(s) out) "
          f"— {info['title']}")
    if info.get("calendar"):
        print(f"  calendar: {info['calendar']}")


def _busy_map(st, ws, cfg, use_calendar=True, calendar_name=None,
              include_plan=False):
    """Busy intervals for the week: (optional) previous plan + Apple Calendar.

    Delegates to planner.auto.busy_map, the implementation shared with the
    web UI and the cycle watcher. `include_plan` must stay False whenever the
    caller is about to replace this week's plan — otherwise the new plan is
    squeezed into the gaps left by the plan it is replacing. Re-plans always
    respect the user's pinned slots (moved by hand) even with include_plan
    False, because save_plan() carries them over into the new plan.
    """
    from planner.auto import busy_map as _shared_busy_map
    return _shared_busy_map(st, ws, cfg, use_calendar=use_calendar,
                            calendar_name=calendar_name,
                            include_plan=include_plan, pinned=True)


def cmd_schedule(args):
    st, cfg = get_store(), get_cfg()
    ws = parse_week(args.week)
    tasks = st.list_tasks()
    busy = _busy_map(st, ws, cfg, use_calendar=not args.no_calendar,
                     calendar_name=args.calendar)
    res = schedule_week(tasks, ws, busy, cfg)
    _print_plan(res, ws)
    if args.dry_run:
        print("\n(dry run — nothing saved)")
        return
    if not res["slots"]:
        print("\nnothing to schedule — add a task first (plan add \"…\")")
        return
    pid = st.save_plan(res["week"], res["slots"], note=args.note or "")
    print(f"\nsaved plan #{pid} for week {res['week']}")
    if args.write_calendar:
        from planner.calendar_mac import event_summary, write_event
        ok = 0
        for s in res["slots"]:
            t = st.get_task(s["task_id"]) or {}
            try:
                uid = write_event(event_summary(t, cfg),
                                  f"{s['date']}T{s['start']}:00",
                                  f"{s['date']}T{s['end']}:00",
                                  args.calendar or cfg["calendar_name"],
                                  description=s["rationale"] +
                                  f"\nTask #{s['task_id']}")
                if uid:
                    ok += 1
                    # Remember which Calendar event belongs to the block, so a
                    # later manual move/delete can update it in place.
                    if s.get("id"):
                        st.set_slot_calendar_id(s["id"], uid)
            except Exception as e:
                print(f"[calendar] write failed for slot {s['date']} "
                      f"{s['start']}: {e}", file=sys.stderr)
        print(f"calendar: {ok}/{len(res['slots'])} events created")


def _print_plan(res, ws):
    print(f"WEEK OF {ws.isoformat()}  |  {res['stats']['planned_minutes']} min "
          f"planned over {res['stats']['slots']} block(s)")
    by_day = {}
    for s in res["slots"]:
        by_day.setdefault(s["date"], []).append(s)
    for d in week_dates(ws):
        key = d.isoformat()
        rows = by_day.get(key, [])
        if not rows:
            continue
        print(f"\n{d.strftime('%a %d %b')}  ({res['stats']['per_day_minutes'].get(key, 0)} min)")
        for s in rows:
            print(f"  {s['start']}-{s['end']}  #{s['task_id']}  {s['rationale']}")
    if res["unscheduled"]:
        print("\nUNSCHEDULED:")
        for u in res["unscheduled"]:
            print(f"  #{u['task_id']} {u['title']} (chunk {u['chunk']}, "
                  f"{u['minutes']}min): {u['reason']}")


def cmd_plan(args):
    st = get_store()
    ws = parse_week(args.week)
    plan = st.get_plan(ws.isoformat())
    if not plan:
        print(f"no saved plan for week {ws.isoformat()} — run: schedule --week {args.week}")
        return
    print(f"PLAN #{plan['id']} — week {plan['week']} (created {plan['created_at']})")
    by_day = {}
    for s in plan["slots"]:
        by_day.setdefault(s["date"], []).append(s)
    for key in sorted(by_day):
        print(f"\n{key}")
        for s in by_day[key]:
            t = st.get_task(s["task_id"]) or {}
            print(f"  {s['start']}-{s['end']}  #{s['task_id']} {t.get('title', '')}"
                  f"  [{s['minutes']}min]")
            if s.get("rationale"):
                print(f"      {s['rationale']}")


def cmd_slot(args):
    """Manual correction of planned blocks: move to a new time, or unschedule.

    `plan slot move 12 --date 2026-10-29 --start 14:00 --end 15:30`
    `plan slot drop 12`
    Reuses the HTTP API helpers so CLI and UI behave identically, including
    the best-effort Apple Calendar follow-up on the slot's event.
    """
    st, cfg = get_store(), get_cfg()
    from planner import web as web_mod
    if args.action == "move":
        if not (args.date and args.start and args.end):
            sys.exit("slot move needs --date, --start and --end")
        data = {"date": args.date, "start": args.start, "end": args.end}
        try:
            payload = web_mod.api_slot_move(st, cfg, args.id, data)
        except web_mod.HttpError as exc:
            sys.exit(f"slot move: {exc.message}")
        slot = payload.get("slot") or {}
        print("moved slot #%s -> %s %s-%s  (pinned: re-plans keep it)"
              % (args.id, slot.get("date"), slot.get("start"), slot.get("end")))
    else:   # drop
        try:
            payload = web_mod.api_slot_remove(st, cfg, args.id)
        except web_mod.HttpError as exc:
            sys.exit(f"slot drop: {exc.message}")
        print("removed slot #%s — task #%s stays"
              % (args.id, payload.get("task_id")))
    report = payload.get("calendar") or {}
    if report.get("skipped"):
        print("calendar: slot had no event to sync")
    elif "changed" in report:
        print("calendar: event moved" if report.get("changed")
              else "calendar: event NOT moved (%s)" % report.get("error"))
    elif "deleted" in report:
        print("calendar: event deleted" if report.get("deleted")
              else "calendar: event NOT deleted (%s)" % report.get("error"))


def cmd_log(args):
    st = get_store()
    for e in st.recent_events(limit=args.tail):
        p = e["payload"]
        extra = " ".join(f"{k}={v}" for k, v in p.items() if k != "title")
        print(f"{e['ts']}  {e['kind']:<16} #{str(e['task_id'] or '-'):<4} {extra}")


def cmd_calendar_check(args):
    st, cfg = get_store(), get_cfg()
    ws = parse_week(args.week)
    from planner.calendar_mac import read_busy
    try:
        evs = read_busy(ws, ws + timedelta(days=6), args.calendar or cfg["calendar_name"])
    except Exception as e:
        sys.exit(f"calendar read failed: {e}")
    print(f"{len(evs)} event(s) in {args.calendar or cfg['calendar_name']} "
          f"for week {ws.isoformat()}:")
    for ev in evs[:40]:
        print(f"  {ev['start']} -> {ev['end']}  {ev['title']}")


def cmd_scan(args):
    st = get_store()
    from planner.project_scan import suggest
    items = suggest(st, repo_root=str(BASE.parent), add=args.add)
    if not items:
        print("nothing to suggest")
        return
    for it in items:
        print(f"  [{it['area']}] {it['title']}   ({it['source']})")
    print(f"\n{len(items)} suggestion(s)" + (" — added as tasks" if args.add else " — use --add to create tasks"))


def cmd_brain(args):
    """`brain enrich <id>` — estimate one task now with the configured
    backend (llm, or heuristic offline)."""
    st, cfg = get_store(), get_cfg()
    if args.action != "enrich":
        sys.exit(f"unknown brain action {args.action!r} "
                 f"(the agent queue was removed; use 'brain enrich')")
    t = st.get_task(args.task_id)
    if not t:
        sys.exit(f"no task #{args.task_id}")
    _print_enrichment(st, cfg, args.task_id)


def _auto_once(args, st, cfg):
    from planner.auto import run_cycle
    horizon = getattr(args, "horizon", None)
    if horizon:
        cfg = dict(cfg)                    # one-off override for this cycle
        cfg["horizon_weeks"] = max(1, int(horizon))
    res = run_cycle(st, cfg, repo_root=str(BASE.parent), limit=args.limit,
                    enrich=not args.no_enrich, force=args.force,
                    use_calendar=not args.no_calendar, notify=args.notify)
    if res["skipped"]:
        print("[auto] another cycle is running; skipped this one")
        return 0, 0, 0
    if res["plan"]:
        stats = res["plan"]["stats"]
        print(f"[auto] plan {res['week']}: {stats['slots']} blocks, "
              f"{stats['planned_minutes']} min"
              + (f", {res['unscheduled']} unscheduled" if res["unscheduled"] else ""))
        weeks = res.get("planned_weeks") or []
        if len(weeks) > 1:
            print(f"[auto] horizon: planned weeks {', '.join(weeks)}")
    elif not res["any_tasks"]:
        print("[auto] no tasks yet — add one with: plan add \"…\" --ai")
    else:
        print(f"[auto] plan {res['week']} unchanged")
    print(f"[auto] enriched={res['enriched']}"
          + (f" calibration={res['calibration']}" if res['calibration'] else ""))
    return res["enriched"]


def cmd_auto(args):
    st, cfg = get_store(), get_cfg()
    if not args.watch:
        _auto_once(args, st, cfg)
        return
    print(f"[auto] watching every {args.interval}s — Ctrl-C to stop")
    try:
        while True:
            _auto_once(args, st, cfg)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[auto] stopped")


def cmd_calendar_sync(args):
    """Populate planner/data/calendar_cache.json from the real calendar."""
    from planner import calendar_probe
    ws = parse_week(getattr(args, "week", None))
    end = ws + timedelta(days=6)
    print(f"reading {ws} … {end} from Calendar (this call may ask for permission)")
    payload = calendar_probe.fetch(ws, end)
    if payload.get("error"):
        sys.exit(f"could not read the calendar: {payload['error']}")
    events = payload.get("events") or []
    print(f"{len(events)} event(s) cached in {calendar_probe.CACHE_PATH}")
    for ev in events[:8]:
        when = "all day" if ev.get("all_day") else f"{ev['start']}-{ev['end']}"
        print(f"  {ev['date']} {when}  [{ev.get('calendar','')}] {ev.get('title','')[:60]}")
    return 0


def cmd_note(args):
    """Manual history entries — for work that predates the git history."""
    st = get_store()
    d = args.date or date.today().isoformat()
    st.add_history(d, args.kind, args.text[:200], detail=args.detail or "",
                   source="manual")
    print(f"history += {d} [{args.kind}] {args.text[:120]}")


def cmd_backfill(args):
    st = get_store()
    from planner.backfill import ingest
    n = ingest(st, repo_root=str(BASE.parent), since=args.since)
    print(f"history: {n} new commit entries ingested (since {args.since})")


def cmd_history(args):
    st = get_store()
    from planner.backfill import digest
    text = digest(st, date_from=args.date_from, date_to=args.date_to)
    if args.md:
        out = args.md
        Path(out).write_text(text, encoding="utf-8")
        print(f"written {out}")
    else:
        print(text)


def cmd_export(args):
    st, cfg = get_store(), get_cfg()
    ws = parse_week(args.week)
    plan = st.get_plan(ws.isoformat())
    if not plan:
        sys.exit(f"no saved plan for week {ws.isoformat()}")
    tasks = {t["id"]: t for t in st.list_tasks()}
    if args.ics:
        from planner.calendar_mac import export_ics
        path = export_ics(plan["slots"], tasks, args.ics,
                          calendar_name=cfg["calendar_name"], cfg=cfg)
        print(f"written {path}")
    if args.md:
        lines = [f"# Week plan — {plan['week']}", ""]
        by_day = {}
        for s in plan["slots"]:
            by_day.setdefault(s["date"], []).append(s)
        for key in sorted(by_day):
            lines.append(f"## {key}")
            for s in by_day[key]:
                t = tasks.get(s["task_id"]) or {}
                lines.append(f"- {s['start']}–{s['end']} — #{s['task_id']} "
                             f"{t.get('title', '')} ({s['minutes']} min)")
                if s.get("rationale"):
                    lines.append(f"  - {s['rationale']}")
            lines.append("")
        Path(args.md).write_text("\n".join(lines), encoding="utf-8")
        print(f"written {args.md}")


def cmd_web(args):
    st, cfg = get_store(), get_cfg()
    from planner.web import serve
    serve(st, cfg, port=args.port, open_browser=args.open)


def cmd_tick(args):
    """Periodic entry point (launchd): one auto cycle."""
    class _A:
        watch = False
        interval = 0
        limit = args.limit
        no_enrich = args.no_enrich
        no_calendar = args.no_calendar
        force = args.force
        notify = args.notify
        horizon = getattr(args, "horizon", None)
    _auto_once(_A(), get_store(), get_cfg())


def cmd_install_launchd(args):
    label = "com.thesis.planner"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    log = DATA / "tick.log"
    DATA.mkdir(parents=True, exist_ok=True)
    if args.minutes is not None:
        interval = max(60, int(args.minutes * 60))
    else:
        interval = max(60, int(round(args.hours * 3600)))
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>
    <string>{sys.executable}</string>
    <string>-m</string><string>planner.cli</string><string>tick</string>
  </array>
  <key>WorkingDirectory</key><string>{BASE.parent}</string>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(body, encoding="utf-8")
    print(f"written {plist}")
    print("load it with:  launchctl load -w " + str(plist))
    print("unload with:   launchctl unload -w " + str(plist))


def cmd_config(args):
    cfg = get_cfg()
    if args.set:
        for pair in args.set:
            if "=" not in pair:
                sys.exit(f"expected key=value, got {pair!r}")
            key, raw = pair.split("=", 1)
            try:
                val = json.loads(raw)
            except json.JSONDecodeError:
                val = raw
            node = cfg
            parts = key.split(".")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = val
        config_mod.save(cfg, str(CFG))
        print(f"saved {CFG}")
    print(json.dumps(cfg, indent=2))


# ---------- parser ----------
def build_parser():
    p = argparse.ArgumentParser(prog="plan", description="Thesis planner")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("renumber",
                       help="compact the task id list (one-shot)")
    a.set_defaults(func=cmd_renumber)
    a = sub.add_parser("add", help="add a task")
    a.add_argument("title", nargs="?", default=None)
    a.add_argument("--desc", default=None,
                   help="one sentence; derive title/area/estimate from the repo")
    a.add_argument("--json", action="store_true",
                   help="print the intake result (or task) as JSON")
    a.add_argument("--notes", default="")
    a.add_argument("--area", default="")
    a.add_argument("--priority", type=int, default=2, choices=[1, 2, 3])
    a.add_argument("--complexity", default="M", choices=["S", "M", "L", "1", "2", "3", "4", "5"])
    a.add_argument("--est", type=int, default=60, help="estimated minutes")
    a.add_argument("--deadline", default=None, help="YYYY-MM-DD (usually the PI meeting)")
    a.add_argument("--files", default="")
    a.add_argument("--within", type=int, default=None, metavar="N",
                   help="let the planner place it within N weeks (1 = this week only,"
                        " 0 = any week)")
    a.add_argument("--category", default=None,
                   help="with --desc: 'Thesis' or a subject folder name — "
                        "grounds the estimate in that folder's material "
                        "(default: inferred from the wording)")
    a.add_argument("--backend", default=None,
                   choices=["heuristic", "llm"],
                   help="override the estimate backend for this task only "
                        "(heuristic = offline, llm = the configured "
                        "OpenAI-compatible endpoint — the default)")
    a.add_argument("--ai", action="store_true",
                   help="let the LLM estimate complexity/time and subtasks")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="list tasks")
    l.add_argument("--status", default=None,
                   choices=["todo", "doing", "done", "blocked", "dropped"])
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="show one task")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_show)

    for name, status in (("done", "done"), ("start", "doing"),
                         ("drop", "dropped"), ("block", "blocked")):
        sp = sub.add_parser(name, help=f"mark task {status}")
        sp.add_argument("id", type=int)
        sp.add_argument("--note", default="")
        sp.set_defaults(func=cmd_status, status=status)

    e = sub.add_parser("edit", help="edit fields")
    e.add_argument("id", type=int)
    e.add_argument("--title")
    e.add_argument("--notes")
    e.add_argument("--area")
    e.add_argument("--priority", type=int, choices=[1, 2, 3])
    e.add_argument("--complexity", choices=["S", "M", "L", "1", "2", "3", "4", "5"])
    e.add_argument("--est", dest="est_minutes", type=int)
    e.add_argument("--deadline")
    e.add_argument("--files")
    e.add_argument("--within", dest="within_weeks", type=int, default=None,
                   metavar="N", help="plan within N weeks (0 = any week)")
    e.set_defaults(func=cmd_edit)

    fb = sub.add_parser("feedback", help="report actual time; recalibrate")
    fb.add_argument("id", type=int)
    fb.add_argument("--took", required=True,
                   help="actual duration: 20, 20m, 1h30")
    fb.add_argument("--note", default="")
    fb.add_argument("--json", action="store_true")
    fb.set_defaults(func=cmd_feedback)

    sc = sub.add_parser("schedule", help="plan a week")
    sc.add_argument("--week", default="next", help="this|next|prev|YYYY-MM-DD")
    sc.add_argument("--dry-run", action="store_true")
    sc.add_argument("--no-calendar", action="store_true")
    sc.add_argument("--calendar", default=None)
    sc.add_argument("--write-calendar", action="store_true")
    sc.add_argument("--note", default="")
    sc.set_defaults(func=cmd_schedule)

    pl = sub.add_parser("plan", help="show a saved plan")
    pl.add_argument("--week", default="this")
    pl.set_defaults(func=cmd_plan)

    lg = sub.add_parser("log", help="recent events")
    lg.add_argument("--tail", type=int, default=30)
    lg.set_defaults(func=cmd_log)

    cc = sub.add_parser("calendar-check", help="read Apple Calendar events for a week")
    cc.add_argument("--week", default="this")
    cc.add_argument("--calendar", default=None)
    cc.set_defaults(func=cmd_calendar_check)

    sn = sub.add_parser("scan", help="suggest tasks from the repo")
    sn.add_argument("--add", action="store_true")
    sn.set_defaults(func=cmd_scan)

    br = sub.add_parser("brain", help="agentic estimation")
    br_sub = br.add_subparsers(dest="action", required=True)
    be = br_sub.add_parser("enrich", help="estimate one task now")
    be.add_argument("task_id", type=int)
    be.add_argument("--force", action="store_true")
    br.set_defaults(func=cmd_brain)

    au = sub.add_parser("auto", help="continuous planning: estimate, re-plan, sync")
    au.add_argument("--watch", action="store_true", help="keep running")
    au.add_argument("--interval", type=int, default=60)
    au.add_argument("--limit", type=int, default=5, help="max tasks estimated per cycle")
    au.add_argument("--no-enrich", action="store_true")
    au.add_argument("--no-calendar", action="store_true")
    au.add_argument("--horizon", type=int, default=None, metavar="N",
                    help="plan N weeks ahead this cycle (overrides horizon_weeks)")
    au.add_argument("--force", action="store_true")
    au.add_argument("--notify", action="store_true")
    au.set_defaults(func=cmd_auto)

    cs = sub.add_parser("calendar-sync",
                        help="read Apple Calendar now and cache the week "
                             "(run this in a terminal to grant access once)")
    cs.add_argument("--week", default=None)
    cs.set_defaults(func=cmd_calendar_sync)

    nt = sub.add_parser("note", help="add a manual history note (pre-git work)")
    nt.add_argument("text")
    nt.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    nt.add_argument("--kind", default="note",
                    choices=["note", "milestone", "audit", "run"])
    nt.add_argument("--detail", default="")
    nt.set_defaults(func=cmd_note)

    bf = sub.add_parser("backfill", help="ingest git history into the timeline")
    bf.add_argument("--since", default="2026-04-01")
    bf.set_defaults(func=cmd_backfill)

    hi = sub.add_parser("history", help="print/export the work timeline")
    hi.add_argument("--from", dest="date_from", default=None)
    hi.add_argument("--to", dest="date_to", default=None)
    hi.add_argument("--md", default=None, help="write markdown to this path")
    hi.set_defaults(func=cmd_history)

    ex = sub.add_parser("export", help="export the saved week plan")
    ex.add_argument("--week", default="this")
    ex.add_argument("--ics", default=None)
    ex.add_argument("--md", default=None)
    ex.set_defaults(func=cmd_export)

    w = sub.add_parser("web", help="local web UI")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--open", action="store_true")
    w.set_defaults(func=cmd_web)

    t = sub.add_parser("tick", help="periodic: ensure the current week is planned")
    t.add_argument("--force", action="store_true")
    t.add_argument("--no-calendar", action="store_true")
    t.add_argument("--horizon", type=int, default=None, metavar="N",
                   help="plan N weeks ahead this cycle (overrides horizon_weeks)")
    t.add_argument("--notify", action="store_true")
    t.add_argument("--no-enrich", action="store_true")
    t.add_argument("--limit", type=int, default=5)
    t.set_defaults(func=cmd_tick)

    il = sub.add_parser("install-launchd", help="write a LaunchAgent running `tick`")
    il.add_argument("--hours", type=float, default=1.0,
                    help="run tick every N hours (default: 1; whole numbers ok)")
    il.add_argument("--minutes", type=int, default=None,
                    help="run tick every N minutes; overrides --hours")
    il.set_defaults(func=cmd_install_launchd)

    sl = sub.add_parser("slot", help="move or remove one planned block")
    sl_sub = sl.add_subparsers(dest="action", required=True)
    smv = sl_sub.add_parser("move", help="give a block a new date/time (pins it)")
    smv.add_argument("id", type=int, help="slot id (see the task drawer or plan)")
    smv.add_argument("--date", required=True, help="YYYY-MM-DD")
    smv.add_argument("--start", required=True, help="HH:MM (24h)")
    smv.add_argument("--end", required=True, help="HH:MM (24h)")
    smv.set_defaults(func=cmd_slot)
    sdr = sl_sub.add_parser("drop", help="unschedule this block (task stays)")
    sdr.add_argument("id", type=int)
    sdr.set_defaults(func=cmd_slot)

    mt = sub.add_parser("meeting",
                        help="flag the next presentation meeting (shown to / clear)")
    mt_sub = mt.add_subparsers(dest="action", required=True)
    mset = mt_sub.add_parser("set", help="flag it")
    mset.add_argument("--date", required=True, help="YYYY-MM-DD")
    mset.add_argument("--title", default="PI meeting")
    mset.add_argument("--calendar", default=None,
                      help="the Apple Calendar it came from (informational)")
    mset.add_argument("--id", default=None,
                      help="calendar event uid, so the UI highlight matches")
    mset.set_defaults(func=cmd_meeting)
    mt_sub.add_parser("clear", help="drop the flag").set_defaults(func=cmd_meeting)
    mt_sub.add_parser("show", help="print the current flag").set_defaults(func=cmd_meeting)

    cf = sub.add_parser("config", help="show/set config")
    cf.add_argument("--set", action="append", default=None,
                    help="key=value (dot paths allowed), repeatable")
    cf.set_defaults(func=cmd_config)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
