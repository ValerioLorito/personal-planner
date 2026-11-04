"""
memory.py — what the agents have learned.

Two kinds of memory feed every prompt:
  * samples  — numeric evidence: estimated vs actual minutes, per category
               (tasks) or per habit. `ratio()` turns them into a calibrated
               multiplier (median, blended with a 1.0 prior, clamped 0.5–2.0).
  * beliefs  — short free-text statements the agents write and revise
               ("KR exercise sheets take ~2h each", "gym is best at 19:00").
"""
from __future__ import annotations

import statistics

from .db import now_iso

PRIOR_WEIGHT = 3


def deduplicate_samples(db) -> int:
    """Removes historical duplicate calibration samples that share the same (kind, ref), keeping only the latest entry."""
    dups = db.q(
        "SELECT kind, ref, MAX(id) AS keep_id, COUNT(*) AS cnt "
        "FROM samples WHERE ref IS NOT NULL AND ref != '' "
        "GROUP BY kind, ref HAVING cnt > 1"
    )
    removed = 0
    for d in dups:
        db.x("DELETE FROM samples WHERE kind=? AND ref=? AND id!=?", (d["kind"], d["ref"], d["keep_id"]))
        removed += int(d["cnt"]) - 1
    return removed


def add_sample(db, kind: str, scope: str, estimated: int, actual: int, label: str = "",
               complexity: str = "", ref: str = "") -> int:
    if not estimated or not actual or estimated <= 0 or actual <= 0:
        return 0
    ref_clean = (ref or "").strip()
    if ref_clean:
        existing = db.q("SELECT id FROM samples WHERE kind=? AND ref=? ORDER BY id DESC", (kind, ref_clean))
        if existing:
            keep_id = existing[0]["id"]
            db.update("samples", keep_id, {
                "scope": scope,
                "estimated": int(estimated),
                "actual": int(actual),
                "label": label[:200],
                "complexity": complexity,
                "created_at": now_iso()
            })
            if len(existing) > 1:
                db.x("DELETE FROM samples WHERE kind=? AND ref=? AND id!=?", (kind, ref_clean, keep_id))
            return keep_id
    return db.insert("samples", {"kind": kind, "scope": scope, "estimated": int(estimated),
                                 "actual": int(actual), "label": label[:200], "complexity": complexity,
                                 "ref": ref_clean, "created_at": now_iso()})


def remove_sample_by_ref(db, kind: str, ref: str) -> None:
    ref_clean = (ref or "").strip()
    if not ref_clean:
        return
    db.x("DELETE FROM samples WHERE kind=? AND ref=?", (kind, ref_clean))


def update_sample(db, sample_id: int, actual: int, estimated: int = None) -> dict:
    row = db.one("SELECT * FROM samples WHERE id=?", (int(sample_id),))
    if not row:
        raise ValueError(f"Sample #{sample_id} not found")
    fields = {"actual": max(1, int(actual)), "created_at": now_iso()}
    if estimated is not None and int(estimated) > 0:
        fields["estimated"] = int(estimated)
    db.update("samples", int(sample_id), fields)
    # Keep linked subtask or habit_log in sync if referenced
    ref = (row.get("ref") or "").strip()
    if ref.startswith("subtask:"):
        try:
            sid = int(ref.split(":", 1)[1])
            db.update("subtasks", sid, {"actual_minutes": fields["actual"]})
        except Exception:
            pass
    elif ref.startswith("habit_log:"):
        try:
            parts = ref.split(":")
            hid = int(parts[1])
            date_str = parts[2]
            db.x("UPDATE habit_logs SET actual_minutes=? WHERE habit_id=? AND date=?",
                 (fields["actual"], hid, date_str))
        except Exception:
            pass
    return db.one("SELECT * FROM samples WHERE id=?", (int(sample_id),))


def delete_sample(db, sample_id: int) -> None:
    db.x("DELETE FROM samples WHERE id=?", (int(sample_id),))


def ratio(db, kind: str, scope: str = None) -> dict:
    deduplicate_samples(db)
    if scope:
        rows = db.q("SELECT estimated, actual FROM samples WHERE kind=? AND scope=? ORDER BY id DESC LIMIT 30",
                    (kind, scope))
    else:
        rows = db.q("SELECT estimated, actual FROM samples WHERE kind=? ORDER BY id DESC LIMIT 60", (kind,))
    n = len(rows)
    if not n:
        return {"n": 0, "ratio": 1.0, "raw": None}
    raw = statistics.median(r["actual"] / r["estimated"] for r in rows)
    blended = (raw * n + 1.0 * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)
    return {"n": n, "ratio": round(max(0.5, min(2.0, blended)), 2), "raw": round(raw, 2)}


def calibration_text(db, scope: str) -> str:
    overall = ratio(db, "task")
    local = ratio(db, "task", scope)
    lines = []
    if overall["n"]:
        lines.append("- Across all tasks: actual/estimated median %.2f over %d samples → calibrated multiplier %.2f"
                     % (overall["raw"], overall["n"], overall["ratio"]))
    if local["n"]:
        lines.append("- In this category: median %.2f over %d samples → multiplier %.2f"
                     % (local["raw"], local["n"], local["ratio"]))
        recent = db.q("SELECT label, estimated, actual FROM samples WHERE kind='task' AND scope=? "
                      "ORDER BY id DESC LIMIT 6", (scope,))
        for r in recent:
            lines.append("  · '%s': estimated %d min, took %d min" % (r["label"], r["estimated"], r["actual"]))
    return "\n".join(lines) or "- No completed work recorded yet (no calibration data)."


# ---------------------------------------------------------------- beliefs
def beliefs(db, scopes: list) -> list:
    if not scopes:
        return []
    marks = ",".join("?" * len(scopes))
    return db.q("SELECT id, scope, text, updated_at FROM beliefs WHERE scope IN (%s) ORDER BY id" % marks,
                tuple(scopes))


def beliefs_text(db, scopes: list) -> str:
    rows = beliefs(db, scopes)
    return "\n".join("- [b%d · %s] %s" % (r["id"], r["scope"], r["text"]) for r in rows) or "- (none yet)"


def remember(db, scope: str, text: str, replace_id=None, source: str = "agent") -> int:
    text = (text or "").strip()[:400]
    if not text:
        return 0
    if replace_id:
        row = db.one("SELECT id FROM beliefs WHERE id=?", (int(replace_id),))
        if row:
            db.update("beliefs", row["id"], {"text": text, "scope": scope, "updated_at": now_iso()})
            return row["id"]
    dup = db.one("SELECT id FROM beliefs WHERE scope=? AND lower(text)=lower(?)", (scope, text))
    if dup:
        return dup["id"]
    return db.insert("beliefs", {"scope": scope, "text": text, "source": source,
                                 "created_at": now_iso(), "updated_at": now_iso()})


def forget(db, belief_id: int) -> None:
    db.x("DELETE FROM beliefs WHERE id=?", (int(belief_id),))
