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


def add_sample(db, kind: str, scope: str, estimated: int, actual: int, label: str = "",
               complexity: str = "", ref: str = "") -> None:
    if not estimated or not actual or estimated <= 0 or actual <= 0:
        return
    db.insert("samples", {"kind": kind, "scope": scope, "estimated": int(estimated),
                          "actual": int(actual), "label": label[:200], "complexity": complexity,
                          "ref": ref, "created_at": now_iso()})


def ratio(db, kind: str, scope: str = None) -> dict:
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
