"""
review.py — the periodic plan review: the planner re-reads its own state and
answers "does this plan still deserve its shape?" with why-verdicts.

The heartbeat (auto.run_cycle) calls run_review() at most once per
auto.review.every_hours, and only when a REAL trigger fires — a calibration
shift, busy minutes moved in the planned weeks, deadline pressure, tasks added
or changed, or a plan older than stale_hours. The review is ONE LLM call over
the current plan + signals; its verdicts:

  reestimate   the estimate no longer matches the evidence — the task is
               re-estimated by the next cycle (est_source cleared);
  rewindow     the task cannot honestly fit its window — within_weeks grows;
  reprioritize deadline/order evidence changed — priority adjusts (1..3);
  keep         examined and fine — recorded, nothing touched.

Verdicts never touch slots directly: pins stay pinned, the normal re-plan
picks the new numbers up. Everything lands in kv "review:last" for the UI's
"Last review" card; an LLM failure is data, never an exception.
"""
import json
from datetime import datetime, timedelta

REVIEW_SYSTEM_PROMPT = """You are the reviewer of a university planner's
automatic weekly plan (thesis + per-subject course work + habits). You get the
plan, the open tasks, and signals measured from real state. Return STRICT JSON
only:
{"verdicts": [{"kind": "reestimate"|"rewindow"|"reprioritize"|"keep",
               "task_id": int, "value": null|int, "why": str}],
 "notes": str}
Rules
- Judge ONLY the tasks listed; one verdict per task you have something to say
  about. "keep" is a valid and expected verdict — say why it still holds.
- reestimate: the estimate contradicts the evidence (calibration moved against
  it, material grounding changed). value: null.
- rewindow: the task cannot honestly fit its remaining window. value: new
  within_weeks (current..current+2); null means +1 week.
- reprioritize: deadline or ordering evidence changed. value: new priority
  1 (high) .. 3 (low); null means one notch toward the deadline.
- You may NOT reschedule blocks, delete tasks, or touch pinned/habit spans.
  The scheduler re-plans around the new numbers.
- notes: ONE sentence about the week as a whole. No prose outside the JSON.
"""

VERDICT_KINDS = ("reestimate", "rewindow", "reprioritize", "keep")

REVIEW_KV = "review:last"


# ------------------------------------------------------------------- signals
def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def last_review(store):
    """The stored report dict, or None. Corrupt values degrade to None."""
    raw = store.kv_get(REVIEW_KV)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def signals(store, cfg, now_dt=None, use_calendar=True):
    """Measured triggers with a human why for each. Data only, no LLM.

    Returns {"triggers": {name: why}, "ratio": float|None,
             "busy_delta_minutes": int|None, "tasks": [ids]} — tasks lists
    open todo/doing ids (the review's judging scope).
    """
    now_dt = now_dt or datetime.now()
    review_cfg = (cfg.get("auto") or {}).get("review") or {}
    stale_hours = int(review_cfg.get("stale_hours", 48))
    shift_gate = float(review_cfg.get("calibration_shift", 0.15))
    soon_days = int(cfg.get("deadline_soon_days", 3))

    tasks = [t for t in store.list_tasks()
             if t.get("status") in ("todo", "doing")]
    triggers = {}
    last = last_review(store)

    # 1. staleness — never reviewed, or older than stale_hours
    at = _parse_ts(last.get("at")) if last else None
    if at is None:
        triggers["stale"] = "never reviewed"
    else:
        age_h = (now_dt - at).total_seconds() / 3600.0
        if age_h >= stale_hours:
            triggers["stale"] = "last review %.0fh ago (gate %dh)" % (
                age_h, stale_hours)

    # 2. calibration moved since the review saw it
    try:
        ratio = store.kv_get("calibration:ratio")
        ratio = float(ratio) if ratio is not None else None
    except (TypeError, ValueError):
        ratio = None
    if ratio is not None:
        old_ratio = last.get("ratio") if last else None
        try:
            old_ratio = float(old_ratio) if old_ratio is not None else None
        except (TypeError, ValueError):
            old_ratio = None
        if old_ratio and abs(ratio - old_ratio) / old_ratio >= shift_gate:
            triggers["calibration"] = (
                "calibration ratio moved %.2f -> %.2f (>= %d%%)" % (
                    old_ratio, ratio, round(shift_gate * 100)))

    # 3. deadline pressure: due soon but nothing scheduled ahead
    horizon = (now_dt + timedelta(days=soon_days)).date()
    pressing = []
    for t in tasks:
        deadline = t.get("deadline")
        if not deadline:
            continue
        try:
            due = datetime.fromisoformat(str(deadline)).date()
        except ValueError:
            continue
        if due <= horizon and not store.slots_for_task(t["id"]):
            pressing.append(t["id"])
    if pressing:
        triggers["deadlines"] = ("%d task(s) due within %dd with no "
                                 "scheduled block: %s" % (
                                     len(pressing), soon_days,
                                     ", ".join("#%s" % i for i in pressing)))

    # 4. tasks added or materially changed since the last review
    if last:
        cut = _parse_ts(last.get("at"))
        if cut:
            changed = [t["id"] for t in tasks
                       if (_parse_ts(t.get("updated_at")) or now_dt) > cut]
            if changed:
                triggers["tasks"] = ("%d open task(s) added/changed since the "
                                     "last review: %s" % (
                                         len(changed), ", ".join(
                                             "#%s" % i for i in changed[:8])))

    # 5. busy minutes in the planned weeks moved since the review snapshotted
    busy_delta = None
    if use_calendar:
        old_busy = last.get("busy") if last else None
        if isinstance(old_busy, dict) and old_busy:
            now_busy = _busy_snapshot(store, cfg, now_dt.date())
            if now_busy is not None:
                busy_delta = abs(sum(now_busy.values())
                                 - sum(v for v in old_busy.values()
                                       if isinstance(v, (int, float))))
                if busy_delta >= 60:
                    triggers["busy"] = ("busy minutes in the planned weeks "
                                        "moved by %dmin since the review" %
                                        busy_delta)

    return {"triggers": triggers, "ratio": ratio,
            "busy_delta_minutes": busy_delta,
            "tasks": [t["id"] for t in tasks]}


def _busy_snapshot(store, cfg, today):
    """{week: busy minutes} for weeks with a saved plan; None w/o calendar."""
    try:
        from planner.scheduler import week_start_for
    except Exception:
        return None
    weeks = sorted({r["week"] for r in store.conn.execute(
        "SELECT DISTINCT week FROM plans")} |
        {week_start_for().isoformat()})
    if not weeks:
        return None
    out = {}
    for week in weeks:
        try:
            monday = datetime.fromisoformat(week).date()
        except ValueError:
            continue
        if monday < today - timedelta(days=7):
            continue                     # the past is not the plan's problem
        busy = _week_busy_minutes(store, cfg, monday)
        if busy is not None:
            out[week] = busy
    return out or None


def _week_busy_minutes(store, cfg, monday):
    """Total busy minutes the calendar reports for the week of `monday`."""
    try:
        from planner.calendar_mac import read_busy
        from planner.scheduler import parse_hhmm
        evs = read_busy(monday, monday + timedelta(days=6),
                        calendar_name=cfg.get("calendar_name") or "Thesis")
        if evs.get("error"):
            return None
        total = 0
        for e in evs.get("events") or []:
            try:
                total += (parse_hhmm(e["end"]) - parse_hhmm(e["start"])) \
                    % (24 * 60)
            except Exception:
                continue
        return total
    except Exception:
        return None


# ------------------------------------------------------------------- review
def should_review(store, cfg, now_dt=None, use_calendar=True):
    """(ok, signals) — the every_hours wall-clock gate comes FIRST (it must
    be cheap: no calendar reads), then the real triggers.

    (False, None)   gated out — disabled, or reviewed too recently;
    (False, sig)    triggers were measured and none fired;
    (True, sig)     review due.
    """
    review_cfg = (cfg.get("auto") or {}).get("review") or {}
    if not review_cfg.get("enabled", True):
        return False, None
    last = last_review(store)
    at = _parse_ts(last.get("at")) if last else None
    every = int(review_cfg.get("every_hours", 24))
    if at and (now_dt or datetime.now()) - at < timedelta(hours=every):
        return False, None               # too soon: no signal work at all
    sig = signals(store, cfg, now_dt=now_dt, use_calendar=use_calendar)
    if not sig["triggers"]:
        return False, sig
    return True, sig


def _clean_verdicts(answer, tasks_by_id):
    """Validated verdicts only: known kind, real task, bounded why/value."""
    out = []
    raw = answer.get("verdicts") if isinstance(answer, dict) else None
    for v in (raw or [])[:12]:
        if not isinstance(v, dict):
            continue
        kind = v.get("kind")
        if kind not in VERDICT_KINDS:
            continue
        try:
            tid = int(v.get("task_id"))
        except (TypeError, ValueError):
            continue
        if tid not in tasks_by_id:
            continue
        value = v.get("value")
        if value is not None:
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = None
        if kind == "reprioritize" and value is not None \
                and not 1 <= value <= 3:
            continue
        out.append({"kind": kind, "task_id": tid, "value": value,
                    "why": str(v.get("why") or "")[:300]})
    return out


def _apply_verdicts(store, verdicts, cfg, tasks_by_id):
    """Safe adjustments only; slots and pins are never touched. Returns the
    per-task records with what actually changed."""
    applied = 0
    records = []
    for v in verdicts:
        tid, kind, value, why = (v["task_id"], v["kind"], v.get("value"),
                                 v["why"])
        task = tasks_by_id[tid]
        rec = {"kind": kind, "task_id": tid, "title": task.get("title") or "",
               "why": why, "change": None}
        if kind == "keep":
            records.append(rec)
            continue
        if kind == "reestimate":
            store.update_task(tid, est_source=None)
            rec["change"] = "estimate cleared — the next cycle re-estimates"
        elif kind == "rewindow":
            current = int(task.get("within_weeks") or 1)
            target = value if value is not None else current + 1
            target = max(current, min(26, target))
            if target != current:
                store.update_task(tid, within_weeks=target)
                rec["change"] = "within_weeks %d -> %d" % (current, target)
        elif kind == "reprioritize":
            current = int(task.get("priority") or 2)
            if value is not None:
                target = value
            else:
                target = current - 1 if current > 1 else current
            target = max(1, min(3, target))
            if target != current:
                store.update_task(tid, priority=target)
                rec["change"] = "priority %d -> %d" % (current, target)
        if rec["change"]:
            applied += 1
        records.append(rec)
    return records, applied


def run_review(store, cfg, repo_root=".", use_calendar=True, now_dt=None,
               force=False):
    """One gated review: signals -> one LLM call -> safe verdicts -> kv.

    force=True (the UI's "review now") skips the every_hours gate and runs
    even with no triggers — still one call, still safe verdicts only.
    Returns the report (also stored under kv "review:last"). Every failure
    becomes data; the plan is never touched by this module itself.
    """
    now_dt = now_dt or datetime.now()
    if force:
        sig = signals(store, cfg, now_dt=now_dt, use_calendar=use_calendar)
        ok = True
    else:
        ok, sig = should_review(store, cfg, now_dt=now_dt,
                                use_calendar=use_calendar)
    report = {"at": now_dt.isoformat(timespec="seconds"), "triggers": [],
              "verdicts": [], "applied": 0, "notes": "", "error": None,
              "skipped": None}
    if not ok:
        if sig is None:
            report["skipped"] = "gated (disabled or reviewed recently)"
        elif sig["triggers"]:
            report["skipped"] = "reviewed too recently"
        else:
            report["skipped"] = "no triggers"
        return report
    report["triggers"] = ["%s: %s" % (k, v)
                          for k, v in sorted(sig["triggers"].items())]

    backend = (cfg.get("brain") or {}).get("backend") or "heuristic"
    if backend != "llm":
        report["skipped"] = "review needs brain.backend=llm"
        return report

    # ---- one LLM call over the plan + signals ----
    tasks = [t for t in store.list_tasks()
             if t["id"] in set(sig["tasks"])]
    tasks_by_id = {t["id"]: t for t in tasks}
    try:
        from planner.scheduler import week_start_for
        weeks = sorted({r["week"] for r in store.conn.execute(
            "SELECT DISTINCT week FROM plans")})
        plan_lines = []
        for week in weeks[-3:]:
            plan = store.get_plan(week)
            if not plan:
                continue
            plan_lines.append("week %s:" % week)
            for s in plan["slots"]:
                t = tasks_by_id.get(s["task_id"])
                name = (t or {}).get("title") or "(removed task)"
                plan_lines.append("  %s %s-%s %s%s (%dmin) — %s" % (
                    s["date"], s["start"], s["end"], name,
                    " [pinned]" if s.get("pinned") else "", s["minutes"],
                    (s.get("rationale") or "")[:80]))
        habit_rows = store.habit_slots_between(
            week_start_for().isoformat(),
            (week_start_for() + timedelta(days=13)).isoformat())
        habit_lines = ["  %s %s-%s %s" % (h["date"], h["start"], h["end"],
                                          h["name"]) for h in habit_rows[:30]]
        task_lines = []
        for t in tasks:
            task_lines.append(
                "#%d %s — area %s, est %dmin, priority %d, within %s weeks, "
                "deadline %s, status %s" % (
                    t["id"], t["title"], t.get("area") or "—",
                    int(t.get("est_minutes") or 0), int(t.get("priority") or 2),
                    t.get("within_weeks") or "-", t.get("deadline") or "none",
                    t.get("status")))
        user = ("SIGNALS\n%s\n\nOPEN TASKS\n%s\n\nCURRENT PLAN\n%s\n\nHABITS "
                "(next 2 weeks)\n%s\n\nReview the plan now."
                % ("\n".join("- %s" % t for t in report["triggers"]),
                   "\n".join(task_lines) or "(none)",
                   "\n".join(plan_lines) or "(no saved plans)",
                   "\n".join(habit_lines) or "(none)"))
        from planner import brain
        answer = brain.llm_json(REVIEW_SYSTEM_PROMPT, user, cfg)
    except Exception as exc:                 # BrainError, transport, JSON
        report["error"] = str(exc)
        report["triggers"] = report["triggers"]  # keep the why for the UI
        store.kv_set(REVIEW_KV, json.dumps(report, ensure_ascii=False))
        return report

    report["verdicts"] = _clean_verdicts(answer, tasks_by_id)
    records, report["applied"] = _apply_verdicts(store, report["verdicts"],
                                                 cfg, tasks_by_id)
    report["records"] = records
    notes = str((answer.get("notes") if isinstance(answer, dict) else "")
                or "")[:400]
    report["notes"] = notes
    report["ratio"] = sig["ratio"]
    report["busy"] = (_busy_snapshot(store, cfg, now_dt.date())
                      if use_calendar else None)
    store.add_event("plan_reviewed", None, applied=report["applied"],
                    verdicts=len(report["verdicts"]))
    store.kv_set(REVIEW_KV, json.dumps(report, ensure_ascii=False))
    return report
