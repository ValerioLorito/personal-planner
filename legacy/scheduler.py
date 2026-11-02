"""
scheduler.py — deterministic, explainable week planner.

Input:  open tasks (with priority / complexity / est_minutes / deadline),
        the week's busy intervals (calendar + already-planned slots),
        and the scheduling config (work days, day window, lunch, buffers,
        max block length, daily cap).
Output: concrete slots (date, start, end, minutes, rationale) + unscheduled
        tasks with a reason. No LLM, no randomness: same inputs -> same plan.

Ordering:  deadline (soonest first) -> priority (1 high) -> complexity (desc)
           -> estimated minutes (desc). Larger, harder tasks are placed earlier
           in the week; high-complexity blocks prefer mornings.

Blocks:    when the agent left subtasks, block boundaries follow subtask
           boundaries (grouped up to max_block_min), so a block is a thing you
           can actually finish. Deep work refuses fragments shorter than
           deep_min_block unless nothing else fits.

Clock:     days before today are never used, and inside today only the time
           after "now" (plus now_margin_min) is — the planner never plans
           back in time, even when the calendar shows a free gap two hours ago.

Window:    a task may carry within_weeks — how many weeks (counting from this
           one) the planner may place it in. schedule_week(…, weeks_out=N)
           refuses tasks whose window ends before week N; the auto cycle walks
           the whole horizon and carries tasks to the first week that fits.
"""
import json
from datetime import date, datetime, timedelta

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------- time helpers ----------
def parse_hhmm(s):
    h, m = str(s).split(":")[:2]
    return int(h) * 60 + int(m)


def fmt_hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _ceil5(minutes):
    """Round minutes up to the next 5-minute boundary."""
    return ((int(minutes) + 4) // 5) * 5


def _drop_before(intervals, earliest):
    """Keep only the interval time that is still ahead of `earliest`."""
    out = []
    for s, e in intervals:
        s = max(int(s), int(earliest))
        if e - s >= 15:
            out.append([s, e])
    return out


def week_start_for(d=None, offset_weeks=0):
    d = d or date.today()
    monday = d - timedelta(days=d.weekday())
    return monday + timedelta(weeks=offset_weeks)


def week_dates(week_start):
    return [week_start + timedelta(days=i) for i in range(7)]


def _rank(cfg, complexity):
    try:
        return int(cfg["complexity_rank"].get(str(complexity).upper(),
                                              complexity))
    except (TypeError, ValueError):
        return 3


def _minutes(cfg, complexity, est):
    if est:
        return int(est)
    return int(cfg["complexity_minutes"].get(str(complexity).upper(), 60))


# ---------- free/busy ----------
def free_intervals_for_day(day, busy, cfg):
    """Work window minus lunch minus busy, as a list of [start, end] minutes."""
    if day.weekday() not in cfg["work_days"]:
        return []
    day_start = parse_hhmm(cfg["day_start"])
    day_end = parse_hhmm(cfg["day_end"])
    blocks = [[day_start, day_end]]

    lunch = [parse_hhmm(cfg["lunch"][0]), parse_hhmm(cfg["lunch"][1])] \
        if cfg.get("lunch") else None

    def cut(interval, hole):
        s, e = interval
        hs, he = hole
        if he <= s or hs >= e:
            return [interval]
        out = []
        if hs > s:
            out.append([s, hs])
        if he < e:
            out.append([he, e])
        return out

    if lunch:
        blocks = [b for iv in blocks for b in cut(iv, lunch)]
    for hs, he in sorted(busy):
        blocks = [b for iv in blocks for b in cut(iv, [hs, he])]
    buf = int(cfg.get("buffer_min", 0))
    return [[s + (buf if s > day_start else 0), e] for s, e in blocks
            if e - s > 15]


def _consume(intervals, idx, start, end, cfg):
    """Place [start,end) inside intervals[idx], shrinking it with a buffer."""
    iv = intervals[idx]
    buf = int(cfg.get("buffer_min", 0))
    left = [iv[0], start - buf] if start - buf - iv[0] >= 10 else None
    right = [end + buf, iv[1]] if iv[1] - (end + buf) >= 10 else None
    intervals.pop(idx)
    insert_at = idx
    if left:
        intervals.insert(insert_at, left)
        insert_at += 1
    if right:
        intervals.insert(insert_at, right)


def _subtask_list(task):
    subs = task.get("subtasks")
    if isinstance(subs, str):
        try:
            subs = json.loads(subs)
        except (json.JSONDecodeError, TypeError):
            subs = []
    if not isinstance(subs, list):
        return []
    return [s for s in subs if isinstance(s, dict) and s.get("title")]


def task_chunks(cfg, task, est, max_block):
    """Split a task into blocks: subtask-aligned when possible, else by size."""
    subs = _subtask_list(task)
    if not subs:
        chunks, remain = [], est
        while remain > 0:
            m = min(max_block, remain)
            chunks.append({"minutes": m, "label": ""})      # no subtask to name
            remain -= m
        return chunks

    blocks, cur = [], None
    for sub in subs:
        try:
            m = max(1, int(sub.get("minutes") or 0))
        except (TypeError, ValueError):
            m = max(1, est // max(1, len(subs)))
        title = str(sub["title"])[:80]
        if cur and cur["minutes"] + m <= max_block:
            cur["minutes"] += m
            cur["labels"].append(title)
        else:
            if cur:
                blocks.append(cur)
            cur = {"minutes": m, "labels": [title]}
    if cur:
        blocks.append(cur)

    # never plan more than the estimate: clamp the tail
    out, acc = [], 0
    for b in blocks:
        room = est - acc
        if room <= 0:
            break
        m = min(b["minutes"], room)
        out.append({"minutes": m, "label": "; ".join(b["labels"])})
        acc += m
    if acc < est:                      # leftover after grouping
        out.append({"minutes": est - acc, "label": "remaining work"})
    return out or [{"minutes": est, "label": ""}]


# ---------- main ----------
def schedule_week(tasks, week_start, busy_by_date, cfg, now=None, weeks_out=None):
    """tasks: list of task dicts; busy_by_date: {"YYYY-MM-DD": [(s,e), ...]}.

    `now` (datetime) is the reference moment. Days before today are already
    spent, and within today only the time that is still ahead is usable:
    free intervals are clamped to start at `now + now_margin_min` (rounded up
    to the next 5-minute step), so a re-plan can never place a block back in
    time — a gap two hours ago is not a slot for today's work.

    `weeks_out` (0 = the current week, 1 = next week, …) applies each task's
    planning window (`within_weeks`): a task may be placed in week `weeks_out`
    only when it allowed at least `weeks_out + 1` weeks (or no window at all).
    Used by the auto cycle, which plans the whole horizon week by week, and by
    the API, so hand-placing into a far week stays inside the user's choice.
    """
    if now is None:
        now = datetime.now()
    elif not isinstance(now, datetime):   # tolerate a bare date (tests, CLI)
        now = datetime(now.year, getattr(now, "month", 1), getattr(now, "day", 1))
    today = now.date()
    margin = max(0, int(cfg.get("now_margin_min", 5) or 0))
    earliest_today = _ceil5(now.hour * 60 + now.minute + margin)

    if weeks_out is not None:
        try:
            weeks_out = max(0, int(weeks_out))
        except (TypeError, ValueError):
            weeks_out = None

    # Never plan into the past: for the current week, days before today are
    # already spent, so a Tuesday re-plan must not fill Monday morning.
    days = [d for d in week_dates(week_start)
            if d.weekday() in cfg["work_days"] and d >= today]
    free = {d.isoformat(): free_intervals_for_day(d, busy_by_date.get(d.isoformat(), []), cfg)
            for d in days}
    tkey = today.isoformat()
    if tkey in free:
        free[tkey] = _drop_before(free[tkey], earliest_today)
    planned_min = {d.isoformat(): 0 for d in days}
    max_day = int(cfg.get("max_day_min", 300))
    max_block = int(cfg.get("max_block_min", 90))

    def sort_key(t):
        dl = t.get("deadline") or "9999-12-31"
        return (dl, int(t.get("priority") or 2), -_rank(cfg, t.get("complexity")),
                -_minutes(cfg, t.get("complexity"), t.get("est_minutes")))

    def in_window(t):
        if weeks_out is None:
            return True
        within = t.get("within_weeks")
        if within is None:
            return True
        try:
            return weeks_out < within
        except (TypeError, ValueError):
            return True

    open_tasks = [t for t in tasks if t.get("status") in ("todo", "doing")]
    open_tasks = [t for t in open_tasks if in_window(t)]
    open_tasks.sort(key=sort_key)

    slots, unscheduled = [], []
    for t in open_tasks:
        est = _minutes(cfg, t.get("complexity"), t.get("est_minutes"))
        if est <= 0:
            continue
        chunks = task_chunks(cfg, t, est, max_block)

        placed_all = True
        for ci, chunk in enumerate(chunks):
            placed = _place_one(t, chunk, ci, len(chunks), days, free,
                                planned_min, max_day, cfg,
                                today=tkey, earliest_today=earliest_today)
            if placed:
                slots.append(placed)
            else:
                placed_all = False
                unscheduled.append({
                    "task_id": t["id"], "title": t.get("title", ""),
                    "chunk": f"{ci + 1}/{len(chunks)}",
                    "minutes": chunk["minutes"],
                    "reason": "no free interval fits (week full or daily cap)",
                })
                break
        if placed_all:
            continue

    per_day = {d.isoformat(): planned_min[d.isoformat()] for d in days}
    slots.sort(key=lambda s: (s["date"], s["start"]))
    return {
        "week": week_start.isoformat(),
        "slots": slots,
        "unscheduled": unscheduled,
        "stats": {
            "tasks": len(open_tasks),
            "slots": len(slots),
            "planned_minutes": sum(s["minutes"] for s in slots),
            "per_day_minutes": per_day,
        },
    }


def _place_one(task, chunk, chunk_idx, n_chunks, days, free, planned_min,
               max_day, cfg, today=None, earliest_today=None):
    """Place one chunk. `chunk` is {"minutes", "label"}.

    Two passes: the first refuses slots smaller than deep_min_block for deep
    work (a 30-min fragment of a writing task is not usable); the second pass
    accepts anything that fits, so the task still lands this week.
    """
    rank = _rank(cfg, task.get("complexity"))
    minutes = int(chunk["minutes"])
    deep = bool(task.get("deep_work")) or rank >= 3
    deep_min = int(cfg.get("deep_min_block", 45))
    passes = [(max(deep_min, 1), False), (1, True)] if deep else [(1, False)]
    for min_fit, fallback in passes:
        slot = _place_pass(task, minutes, chunk, chunk_idx, n_chunks, days, free,
                           planned_min, max_day, cfg, rank, min_fit,
                           fallback=fallback, deep_min=deep_min,
                           today=today, earliest_today=earliest_today)
        if slot:
            return slot
    return None


def _place_pass(task, chunk_min, chunk, chunk_idx, n_chunks, days, free,
                planned_min, max_day, cfg, rank, min_fit, fallback=False,
                deep_min=45, today=None, earliest_today=None):
    deadline = task.get("deadline")
    days_left = None
    if deadline:
        try:
            days_left = (date.fromisoformat(deadline) - date.today()).days
        except ValueError:
            days_left = None

    for d in days:
        key = d.isoformat()
        if planned_min[key] + chunk_min > max_day:
            continue
        intervals = free.get(key, [])
        if not intervals:
            continue
        # high complexity -> morning first; low complexity -> afternoon first
        cand = sorted(range(len(intervals)),
                      key=lambda i: (intervals[i][0] if rank >= 3
                                     else -intervals[i][0]))
        for idx in cand:
            s, e = intervals[idx]
            if e - s < max(chunk_min, min_fit):
                continue
            start = s
            end = start + chunk_min
            _consume(intervals, idx, start, end, cfg)
            planned_min[key] += chunk_min
            why = [f"{d.strftime('%a')} {fmt_hhmm(start)}-{fmt_hhmm(end)}",
                   f"{chunk_min}min block {chunk_idx + 1}/{n_chunks}",
                   f"complexity {task.get('complexity')} (rank {rank})",
                   f"priority P{task.get('priority')}"]
            if chunk.get("label"):
                why.append(f"covers: {chunk['label']}")
            if deadline:
                why.append(f"deadline {deadline}" +
                           (f" in {days_left}d" if days_left is not None else ""))
            if today and d.isoformat() == today and earliest_today is not None \
                    and start <= earliest_today:
                why.append("not before now")
            if rank >= 3:
                morning = start < parse_hhmm("12:00")
                why.append("morning slot (deep work)" if morning
                           else "afternoon slot — no morning capacity left")
            if fallback and chunk_min < deep_min:
                why.append(f"short block: no slot >= {deep_min}min left this week")
            return {"task_id": task["id"], "date": key,
                    "start": fmt_hhmm(start), "end": fmt_hhmm(end),
                    "minutes": chunk_min, "rationale": "; ".join(why),
                    "subtask": chunk.get("label", "")}
    return None
