"""
proposals_mgr.py — manager for reviewing and applying schedule change proposals.
"""
from __future__ import annotations

import json

from . import tasks_mgr
from .db import now_iso


def list_proposals(app, status: str = "pending") -> list:
    if status == "all":
        rows = app.db.q("SELECT * FROM proposals ORDER BY id DESC LIMIT 50")
    else:
        rows = app.db.q("SELECT * FROM proposals WHERE status=? ORDER BY id DESC", (status,))
    out = []
    for r in rows:
        d = dict(r)
        d["actions"] = json.loads(r.get("actions_json") or "[]")
        out.append(d)
    return out


def apply_proposal(app, proposal_id: int) -> dict:
    p = app.db.one("SELECT * FROM proposals WHERE id=?", (proposal_id,))
    if not p:
        raise ValueError(f"Proposal #{proposal_id} not found")
    if p["status"] != "pending":
        raise ValueError(f"Proposal #{proposal_id} is already {p['status']}")

    actions = json.loads(p.get("actions_json") or "[]")
    results = []

    for a in actions:
        atype = a.get("type")
        if atype == "move_block":
            bid = a["block_id"]
            n_start = a["new_start"]
            n_end = a["new_end"]
            try:
                tasks_mgr.move_block(app, bid, n_start, n_end)
                results.append(f"Moved block #{bid} to {n_start}–{n_end}")
            except Exception as e:
                results.append(f"Failed to move block #{bid}: {e}")
        elif atype == "remove_block":
            bid = a["block_id"]
            try:
                tasks_mgr.remove_block(app, bid)
                results.append(f"Removed block #{bid}")
            except Exception as e:
                results.append(f"Failed to remove block #{bid}: {e}")

    summary = "; ".join(results) or "No actions executed"
    app.db.update("proposals", proposal_id, {
        "status": "applied",
        "result": summary,
        "resolved_at": now_iso()
    })
    app.toast(f"Applied proposal: {p['title']}", "success")
    app.notify("proposals_changed", {"action": "applied", "id": proposal_id})
    return {"ok": True, "result": summary}


def reject_proposal(app, proposal_id: int) -> dict:
    p = app.db.one("SELECT * FROM proposals WHERE id=?", (proposal_id,))
    if not p:
        raise ValueError(f"Proposal #{proposal_id} not found")

    app.db.update("proposals", proposal_id, {
        "status": "rejected",
        "resolved_at": now_iso()
    })
    app.toast(f"Dismissed proposal: {p['title']}", "info")
    app.notify("proposals_changed", {"action": "rejected", "id": proposal_id})
    return {"ok": True}
