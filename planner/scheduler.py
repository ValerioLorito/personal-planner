"""
scheduler.py — deterministic placement of work blocks into free time.

The agent decides *what* (subtasks, minutes, deadline, preferred time of day,
deep work); this module decides *exactly when*, so placements never collide
with the calendar and always respect the deadline when that is possible.

Rules:
  * work days / hours from config (weekends only if a deadline needs them)
  * never before now + margin, never overlapping busy time (+ buffer)
  * blocks ≤ max_block_min and ≥ min_block_min; subtasks stay in order
  * per-day caps: all planned work (max_day_min) and per task (max_task_day_min)
  * finish `deadline_margin_hours` before the deadline when possible;
    otherwise place anyway and warn
"""
from __future__ import annotations

from datetime import datetime, timedelta


def hm(s: str) -> tuple:
    h, m = s.split(":")[:2]
    return int(h), int(m)


def at(day: datetime, s: str) -> datetime:
    h, m = hm(s)
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def round_up(d: datetime, step: int = 5) -> datetime:
    d = d.replace(second=0, microsecond=0)
    extra = (-d.minute) % step
    return d + timedelta(minutes=extra)


def parse_deadline(s: str):
    if not s:
        return None
    s = s.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s[:19], fmt)
            if fmt == "%Y-%m-%d":
                d = d.replace(hour=23, minute=59)
            return d
        except ValueError:
            continue
    return None


def merge(intervals: list) -> list:
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


class Scheduler:
    def __init__(self, cfg: dict, busy: list, load: dict = None, now: datetime = None):
        """busy: [(start, end)] datetimes. load: {'YYYY-MM-DD': planned minutes}."""
        self.w = cfg["work"]
        self.now = now or datetime.now()
        buf = timedelta(minutes=self.w["buffer_min"])
        self.busy = merge([(s - buf, e + buf) for s, e in busy])
        self.load = dict(load or {})
        self.task_load = {}

    # ---- free gaps of one day ----
    def gaps(self, day: datetime) -> list:
        start = at(day, self.w["day_start"])
        end = at(day, self.w["day_end"])
        earliest = round_up(self.now + timedelta(minutes=self.w["now_margin_min"]))
        if end <= earliest:
            return []
        start = max(start, earliest)
        free = [(start, end)]
        for bs, be in self.busy:
            if be <= start or bs >= end:
                continue
            nxt = []
            for fs, fe in free:
                if be <= fs or bs >= fe:
                    nxt.append((fs, fe))
                    continue
                if bs > fs:
                    nxt.append((fs, bs))
                if be < fe:
                    nxt.append((be, fe))
            free = nxt
        return [(s, e) for s, e in free if (e - s).total_seconds() >= self.w["min_block_min"] * 60]

    def _days(self, first: datetime, last: datetime, weekends: bool):
        d = first.replace(hour=0, minute=0, second=0, microsecond=0)
        while d <= last:
            if d.weekday() in self.w["days"] or weekends:
                yield d
            d += timedelta(days=1)

    def _order(self, gaps: list, pref: str) -> list:
        if pref == "afternoon":
            return sorted(gaps, key=lambda g: (g[0].hour < 13, g[0]))
        if pref == "evening":
            return sorted(gaps, key=lambda g: g[0], reverse=True)
        return gaps  # morning / any: earliest first

    # ---- placement ----
    def place(self, subtasks: list, deadline=None, earliest=None, pref: str = "any",
              max_block: int = None, spread: bool = True) -> dict:
        """subtasks: [{'key', 'title', 'minutes'}] in order.
        Returns {'blocks': [{'key','title','start','end','minutes'}], 'warnings': [...],
                 'unplaced': minutes}."""
        w = self.w
        max_block = max(w["min_block_min"], min(int(max_block or w["max_block_min"]), 240))
        deadline_dt = parse_deadline(deadline) if isinstance(deadline, str) else deadline
        margin = timedelta(hours=w["deadline_margin_hours"])
        horizon = self.now + timedelta(days=w["horizon_days"])
        start_day = self.now
        earliest_has_time = False
        if earliest:
            e_str = str(earliest).strip()
            if ":" in e_str:
                e0 = parse_deadline(e_str)
                if e0:
                    earliest_has_time = True
                    start_day = max(self.now, e0)
            else:
                e0 = parse_deadline(e_str[:10])
                if e0:
                    start_day = max(self.now, e0.replace(hour=0, minute=0, second=0, microsecond=0))
        warnings = []

        def attempt(limit, weekends, task_cap):
            # returns blocks or None; works on copies of busy/load
            busy, load, tload = list(self.busy), dict(self.load), {}
            blocks, cursor = [], start_day
            for st in subtasks:
                remaining = int(st["minutes"])
                part = 0
                while remaining > 0:
                    placed = False
                    for day in self._days(cursor, limit, weekends):
                        dkey = day.strftime("%Y-%m-%d")
                        room_day = w["max_day_min"] - load.get(dkey, 0)
                        room_task = task_cap - tload.get(dkey, 0)
                        room = min(room_day, room_task)
                        if room < min(w["min_block_min"], remaining):
                            continue
                        self.busy = busy
                        for gs, ge in self._order(self.gaps(day), pref):
                            if gs < cursor:
                                gs = round_up(cursor)
                                if ge - gs < timedelta(minutes=min(w["min_block_min"], remaining)):
                                    continue
                            gap_min = int((ge - gs).total_seconds() // 60)
                            take = min(remaining, max_block, gap_min, room)
                            # avoid leaving a useless sliver for the next block
                            if 0 < remaining - take < w["min_block_min"]:
                                if remaining <= min(max_block, gap_min, room):
                                    take = remaining
                                else:
                                    take = max(w["min_block_min"], take - w["min_block_min"])
                            if take < min(w["min_block_min"], remaining):
                                continue
                            s, e = gs, gs + timedelta(minutes=take)
                            if pref == "evening" and gap_min > take:
                                s, e = ge - timedelta(minutes=take), ge
                                if s < cursor:
                                    s, e = gs, gs + timedelta(minutes=take)
                            if e > limit:
                                continue
                            part += 1
                            blocks.append({"key": st["key"], "title": st["title"], "part": part,
                                           "start": s.strftime("%Y-%m-%dT%H:%M"),
                                           "end": e.strftime("%Y-%m-%dT%H:%M"), "minutes": take})
                            buf = timedelta(minutes=w["buffer_min"])
                            busy = merge(busy + [(s - buf, e + buf)])
                            load[dkey] = load.get(dkey, 0) + take
                            tload[dkey] = tload.get(dkey, 0) + take
                            remaining -= take
                            cursor = e
                            placed = True
                            break
                        if placed:
                            break
                    if not placed:
                        self.busy = list(self.busy)
                        return None, busy, load
            return blocks, busy, load

        original_busy = list(self.busy)
        earliest_is_weekend = (earliest is not None and start_day.weekday() not in self.w["days"])
        tries = []
        if deadline_dt:
            hard = deadline_dt - margin if deadline_dt - margin > self.now else deadline_dt
            horizon = max(horizon, deadline_dt + timedelta(days=14))
            if earliest_is_weekend:
                tries.append((hard, True, w["max_task_day_min"] if spread else w["max_day_min"], None))
            tries.append((hard, False, w["max_task_day_min"] if spread else w["max_day_min"], None))
            if w.get("weekend_if_needed", True) and not earliest_is_weekend:
                tries.append((hard, True, w["max_task_day_min"], "uses the weekend to meet the deadline"))
            tries.append((deadline_dt, True, w["max_day_min"],
                          "packed tightly (less than %dh margin before the deadline)" % w["deadline_margin_hours"]))
            tries.append((horizon, True, w["max_day_min"], "DOES NOT FIT before the deadline — some work lands after it"))
        else:
            if earliest_is_weekend:
                tries.append((horizon, True, w["max_task_day_min"] if spread else w["max_day_min"], None))
            tries.append((horizon, False, w["max_task_day_min"] if spread else w["max_day_min"], None))
            if not earliest_is_weekend:
                tries.append((horizon, True, w["max_day_min"], "uses weekends (weekdays are full)"))
        for limit, weekends, cap, note in tries:
            self.busy = list(original_busy)
            blocks, busy, load = attempt(limit, weekends, cap)
            if blocks is not None:
                self.busy, self.load = busy, load
                if note:
                    warnings.append(note)
                return {"blocks": blocks, "warnings": warnings, "unplaced": 0}
        self.busy = original_busy
        total = sum(int(s["minutes"]) for s in subtasks)
        return {"blocks": [], "warnings": ["no free time found in the next %d days" % w["horizon_days"]],
                "unplaced": total}


def free_minutes_by_day(cfg: dict, busy: list, days: int = 14, now: datetime = None) -> list:
    """[(date, weekday, free_minutes)] — workload summary for prompts."""
    sch = Scheduler(cfg, busy, now=now)
    now = sch.now
    out = []
    for i in range(days):
        d = (now + timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        free = sum(int((e - s).total_seconds() // 60) for s, e in sch.gaps(d))
        out.append((d.strftime("%Y-%m-%d"), d.strftime("%a"), free,
                    d.weekday() in cfg["work"]["days"]))
    return out
