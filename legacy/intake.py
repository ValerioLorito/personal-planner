"""
intake.py — one-sentence task intake + calibration feedback for the planner.

    from planner.intake import create_from_description
    create_from_description(store, cfg, "create an audit for coreference resolution")
    # -> {"task_id": 7, "status": "applied", "analysis": {...}, "plan": {...}}

The user types one sentence; the planner derives the rest *from this repository*:
an imperative title, an area, a complexity/size, minutes, priority, subtasks,
and 1-4 concrete artefacts that already exist (audits, stages, scripts) which
justify the estimate. The raw sentence is kept verbatim in the task's notes.

Then the user reports how long it actually took:

    apply_feedback(store, cfg, 7, "20m", note="the script already existed")

which writes `actual_minutes`/`feedback_note`/`feedback_at` and recomputes the
calibration ratio (median actual/estimated over the last 20 completed or
fed-back tasks), overall and per area. `brain.heuristic_plan` uses the overall
ratio; the agent/llm prompt receives both the per-area ratios and the last five
feedback lines (see brain.project_context).

READ-ONLY GUARANTEE
-------------------
This module never writes to the repository. Every file it opens goes through
`_open_read()`, which rejects any mode containing w/a/x/+. The only writes go
through the Store (SQLite, normally planner/data/planner.db) and through
an LLM estimate via brain.llm_plan (OpenRouter), synchronous.
`inspect_repo()` walks and reads; it never creates, edits or deletes a file.
The one further reader: planner/sources.py reads the user's university
folders — also read-only (its extraction cache lives in planner/data/).
"""
import fnmatch
import json
import os
import re
import statistics
from datetime import date, datetime, timedelta

from planner import brain, sources
from planner.store import now as store_now

READ_ONLY = True                 # explicit: this module only reads the repo
TITLE_MAX = 70
MAX_RELATED = 4
MIN_MINUTES = 5
MAX_MINUTES = 60 * 24 * 5
CAL_WINDOW = 20                  # samples used by the calibration median
CAL_MIN, CAL_MAX = 0.5, 2.0      # clamp for the aggregate ratios
#: How many observations it takes to outweigh the assumption that estimates are
#: right. One task finished fast is evidence, not a new law: with PRIOR_WEIGHT=3
#: a single 0.17x sample moves the ratio to ~0.79x, not to 0.17x.
PRIOR_WEIGHT = 3.0
MAX_READ_BYTES = 200_000
DEFAULT_AREA = "general"

# Directories never worth walking for artefacts.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".zwork", "external", "slurm_logs",
}


# --------------------------------------------------------------------------- #
# read-only guard
# --------------------------------------------------------------------------- #
def _open_read(path, mode="r", *args, **kwargs):
    """Open a repo file for reading only; any write mode is a programming error."""
    if any(flag in str(mode) for flag in "wax+"):
        raise RuntimeError(
            "planner.intake is read-only: refusing to open %r with mode %r"
            % (path, mode))
    return open(path, mode, *args, **kwargs)


def _read_head(path, limit=2000):
    try:
        if os.path.getsize(path) > MAX_READ_BYTES:
            return ""
        with _open_read(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
_LEAD_INS = re.compile(
    r"^(?:please|pls|can you|could you|would you|i need to|i want to|"
    r"i have to|we need to|we should|let'?s|help me(?: to)?)\s+", re.I)
_WORDS = re.compile(r"[a-z0-9]+")


def _words(text):
    return _WORDS.findall(str(text or "").lower())


def make_title(description, limit=TITLE_MAX):
    """A short imperative title (sentence case, <= limit chars)."""
    text = " ".join(str(description or "").split()).strip().strip("`\"'")
    while True:
        trimmed = _LEAD_INS.sub("", text).strip()
        if trimmed == text:
            break
        text = trimmed
    text = text.rstrip(" .!?;:")
    if not text:
        return "Untitled task"
    text = text[0].upper() + text[1:]
    if len(text) > limit:
        cut = text[:limit]
        space = cut.rfind(" ")
        if space >= int(limit * 0.6):
            cut = cut[:space]
        text = cut.rstrip(" ,;:-")
    return text or "Untitled task"


def _short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# --------------------------------------------------------------------------- #
# area inference
# --------------------------------------------------------------------------- #
# Order is the tie-breaker: a description matching one keyword in two areas
# goes to the more specific area first (coreference before the generic analysis).
AREA_RULES = (
    ("coreference", re.compile(
        r"\b(coref(?:erence)?|mentions?|resolv\w*|resolut\w*|antecedent\w*)\b", re.I)),
    ("atlas", re.compile(
        r"\b(atlas|noun[ -]?atlas|knowledge[ -]?base|taxonom\w*|concepts?)\b",
        re.I)),
    ("writing", re.compile(
        r"\b(write|writing|draft|paper|section|chapter|thesis|report|abstract|"
        r"appendix|prose)\b", re.I)),
    ("infra", re.compile(
        r"\b(slurm|hpc|cluster|jobs?|pipeline|docker|deploy|install|"
        r"environment|ci|batter(?:y|ies))\b", re.I)),
    ("improvement", re.compile(
        r"\b(improve\w*|refactor\w*|cleanup|clean[ -]?up|dead[ -]?code|"
        r"harden\w*|fix\w*|bugs?)\b", re.I)),
    ("analysis", re.compile(
        r"\b(analy[sz]\w*|audit\w*|evaluat\w*|diagnos\w*|statistic\w*|"
        r"inspect\w*|review\w*|results?)\b", re.I)),
)


def _area_info(text):
    """(area, matched words) — the best-scoring rule, ties by order above."""
    text = str(text or "")
    best, best_hits = DEFAULT_AREA, []
    for area, pattern in AREA_RULES:
        hits = pattern.findall(text)
        if len(hits) > len(best_hits):
            best, best_hits = area, hits
    return best, [str(h).lower() for h in best_hits]


def infer_area(text):
    """Public convenience wrapper: just the area name."""
    return _area_info(text)[0]


# --------------------------------------------------------------------------- #
# keyword -> existing artefacts (glob table)
# --------------------------------------------------------------------------- #
ARTEFACT_RULES = (
    (re.compile(r"\b(coref(?:erence)?|mentions?|resolv\w*|resolut\w*)\b", re.I), (
        "src/coreference/*.py",
        "src/utils/coreference/*.py",
        "data/interim/coref_stage*",
        "slurm_scripts/coref_*.sh",
    )),
    (re.compile(r"\baudit\w*\b|\breview\w*\b", re.I), (
        "audits/*.md",
        "src/utils/atlas/audit_*.py",
        "src/utils/coreference/audit_*.py",
    )),
    (re.compile(r"\batlas\b|\btaxonom\w*\b", re.I), (
        "src/utils/atlas/*.py",
        "data/interim/atlas_generation*",
        "slurm_scripts/atlas_*.sh",
    )),
    (re.compile(r"\bbatter(y|ies)\b", re.I), (
        "slurm_scripts/llm_battery_chain.sh",
        "slurm_scripts/battery_*.sh",
    )),
    (re.compile(r"\b(slurm|hpc|cluster|jobs?)\b", re.I), (
        "slurm_scripts/*.sh",
    )),
    (re.compile(r"\b(write|writing|draft|paper|section|chapter|thesis)\b", re.I), (
        "audits/*.md",
        "planner/project_profile.md",
        "README.md",
    )),
    (re.compile(r"\b(planner|scheduler|intake|web ui|cli)\b", re.I), (
        "planner/*.py",
        "planner/tests/*.py",
    )),
    (re.compile(r"\b(scan|todo|dead[ -]?code|cleanup|refactor)\b", re.I), (
        "planner/project_scan.py",
        "audits/dead_*.md",
    )),
)

# Ranking aliases: the description says "coreference", the file says "coref".
_ALIASES = {
    "coreference": "coref", "coreferences": "coref",
    "resolution": "resolv", "resolver": "resolv", "resolved": "resolv",
    "resolution.": "resolv", "auditing": "audit", "audits": "audit",
    "evaluation": "evaluat", "writing": "writ", "taxonomy": "taxonom",
}


def _relevance(description, rel_path):
    path = rel_path.lower()
    score = 0
    for word in _words(description):
        if len(word) < 4:
            continue
        key = _ALIASES.get(word, word)
        if key in path:
            score += 3
        elif key[:5] in path:
            score += 1
    return score


def _glob_matches(repo_root, pattern, file_limit=200):
    """[(repo-relative path, is_dir)] for one glob, bounded by the pattern.

    Only the subtree before the first wildcard segment is walked, and the walk
    stops at the pattern's depth, so `data/interim/coref_stage*` never descends
    into the (large) run folders.
    """
    pattern = str(pattern or "").replace("\\", "/").strip().lstrip("./")
    if not pattern:
        return []
    segments = [s for s in pattern.split("/") if s]
    fixed = []
    for segment in segments:
        if any(ch in segment for ch in "*?["):
            break
        fixed.append(segment)
    if not any(any(ch in segment for ch in "*?[") for segment in segments):
        full = os.path.join(repo_root, *segments)
        rel = "/".join(segments)
        if os.path.isfile(full):
            return [(rel, False)]
        if os.path.isdir(full):
            return [(rel, True)]
        return []
    base = os.path.join(repo_root, *fixed) if fixed else repo_root
    if not os.path.isdir(base):
        return []
    base_rel = "/".join(fixed)
    max_depth = max(1, len(segments) - len(fixed))
    files, dirs = [], []
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        if depth + 1 > max_depth:
            dirnames[:] = []
            continue
        if depth + 1 == max_depth:
            rel_part = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            prefix = "/".join(part for part in (base_rel, rel_part) if part)
            for name in dirnames:
                rel = (prefix + "/" + name).strip("/")
                if fnmatch.fnmatchcase(rel, pattern):
                    dirs.append(rel)
            for name in filenames:
                if name.startswith("."):
                    continue
                rel = (prefix + "/" + name).strip("/")
                if fnmatch.fnmatchcase(rel, pattern):
                    files.append(rel)
            dirnames[:] = []
    out = [(path, False) for path in sorted(files)[:file_limit]]
    out += [(path, True) for path in sorted(dirs)[:file_limit]]
    return out


def _first_line(text):
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#!"):
            continue
        line = re.sub(r"^[#>*\-\s]+", "", line).strip("\"'`")
        line = re.sub(r'^(?:"""|\'\'\')', "", line).strip()
        if not line or line.startswith("-*-"):
            continue
        return line[:110]
    return ""


def _note_for(repo_root, rel_path, is_dir):
    """A one-line, concrete note about an artefact that exists."""
    full = os.path.join(repo_root, *rel_path.split("/"))
    if is_dir:
        try:
            count = len(os.listdir(full))
        except OSError:
            count = 0
        return _short("directory — %d entries" % count, 110)
    line = _first_line(_read_head(full, 1500))
    if rel_path.startswith("audits/"):
        prefix = "audit note"
    elif rel_path.endswith(".py"):
        prefix = "module"
    elif rel_path.endswith(".sh"):
        prefix = "slurm script"
    elif rel_path.endswith(".md"):
        prefix = "doc"
    elif rel_path.endswith(".json"):
        prefix = "data file"
    else:
        prefix = "file"
    return _short(("%s: %s" % (prefix, line)) if line else prefix, 110)


def _fallback_related(repo_root, limit=3):
    """Stable artefacts when the description matches no keyword rule."""
    out = []
    for rel in ("README.md", "planner/project_profile.md", "planner/README.md"):
        full = os.path.join(repo_root, *rel.split("/"))
        if os.path.isfile(full):
            out.append({"path": rel, "note": _note_for(repo_root, rel, False)})
            if len(out) >= limit:
                return out
    src = os.path.join(repo_root, "src")
    recent = []
    if os.path.isdir(src):
        seen = 0
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in SKIP_DIRS and not d.startswith("."))
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, repo_root).replace(os.sep, "/")
                recent.append((mtime, rel))
                seen += 1
            if seen > 600:
                break
    for _, rel in sorted(recent, reverse=True):
        if len(out) >= limit:
            break
        out.append({"path": rel, "note": _note_for(repo_root, rel, False)})
    return out[:limit]


def inspect_repo(description, repo_root=".", limit=MAX_RELATED):
    """1-4 existing artefacts matched by the description's keywords.

    Read-only: walks the relevant subtree, reads a small head of each file for
    the note, and never opens anything for writing.
    """
    root = _resolve_root(repo_root)
    description = str(description or "")
    buckets = []
    all_candidates = []
    for pattern, globs in ARTEFACT_RULES:
        if not pattern.search(description):
            continue
        found, seen = [], set()
        for glob in globs:
            exact = not any(ch in glob for ch in "*?[")
            for rel, is_dir in _glob_matches(root, glob):
                if rel in seen:
                    continue
                seen.add(rel)
                found.append((rel, is_dir, 0 if exact else 1))
        found.sort(key=lambda item: (item[2], -_relevance(description, item[0]),
                                     item[0]))
        candidates = [(rel, is_dir) for rel, is_dir, _ in found]
        all_candidates.append(candidates)
        buckets.append(candidates[:6])
    related, used = [], set()
    queues = [list(bucket) for bucket in buckets]
    while len(related) < limit and any(queues):
        progressed = False
        for queue in queues:
            if not queue or len(related) >= limit:
                continue
            rel, is_dir = queue.pop(0)
            if rel in used:
                continue
            used.add(rel)
            related.append({"path": rel,
                            "note": _note_for(root, rel, is_dir)})
            progressed = True
        if not progressed:
            break
    if not related:
        related = _fallback_related(root, min(limit, 3))
    if related and len({item["path"].split("/")[0] for item in related}) == 1:
        # All citations came from one directory (e.g. only audits/*.md): swap
        # the weakest for the best candidate from another glob, so an "audit"
        # task cites both audits/*.md and audit_*.py when both exist.
        top = related[0]["path"].split("/")[0]
        replacement = None
        for candidates in all_candidates:
            for rel, is_dir in candidates:
                if rel in used:
                    continue
                if rel.split("/")[0] != top:
                    replacement = (rel, is_dir)
                    break
            if replacement:
                break
        if replacement:
            rel, is_dir = replacement
            related[-1] = {"path": rel,
                           "note": _note_for(root, rel, is_dir)}
    return related[:limit]


# --------------------------------------------------------------------------- #
# calibration
# --------------------------------------------------------------------------- #
def parse_duration(value):
    """'20' | '20m' | '1h30' | '1.5h' | '90 min' | '1:30' -> minutes (int)."""
    if isinstance(value, bool):
        raise ValueError("actual minutes must be a duration, got a boolean")
    if isinstance(value, (int, float)):
        minutes = float(value)
    else:
        text = str(value or "").strip().lower()
        if not text:
            raise ValueError("actual minutes is required")
        clock = re.match(r"^(\d+(?:\.\d+)?)\s*:\s*(\d{1,2})$", text)
        if clock:
            minutes = float(clock.group(1)) * 60 + float(clock.group(2))
        else:
            parts = re.match(
                r"^(?:(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours))?\s*"
                r"(?:(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)?)?$",
                text)
            if parts and (parts.group(1) is not None
                          or parts.group(2) is not None):
                minutes = (float(parts.group(1) or 0) * 60
                           + float(parts.group(2) or 0))
            else:
                bare = re.match(r"^(\d+(?:\.\d+)?)$", text)
                if not bare:
                    raise ValueError("cannot read duration %r (try 20, 20m, 1h30)"
                                     % value)
                minutes = float(bare.group(1))
    if minutes <= 0:
        raise ValueError("actual minutes must be greater than zero")
    if minutes > 60 * 24 * 30:
        raise ValueError("implausible duration: %s minutes" % minutes)
    return int(round(minutes))


def _kv_float(store, key, default):
    try:
        value = store.kv_get(key)
        return float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _kv_json(store, key, default):
    raw = store.kv_get(key)
    if not raw:
        return dict(default)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return dict(default)
    return data if isinstance(data, dict) else dict(default)


def _float_map(data):
    out = {}
    for key, value in (data or {}).items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _clamp_ratio(value):
    return round(min(CAL_MAX, max(CAL_MIN, float(value))), 3)


_AREA_ALIASES = {
    "coref": "coreference", "corefs": "coreference",
    "coreference-resolution": "coreference", "kb": "atlas",
    "knowledge-base": "atlas", "hpc": "infra", "slurm": "infra",
    "write": "writing", "docs": "writing", "code": "improvement",
}


def _normalise_area(value):
    area = str(value or DEFAULT_AREA).strip().lower() or DEFAULT_AREA
    return _AREA_ALIASES.get(area, area)


def _median_ratio(values):
    """Blend the observed median with the prior of 1.0, weighted by sample count.

    n=1  -> mostly "your estimate was probably right"
    n=10 -> mostly what actually happened
    """
    if not values:
        return None
    observed = statistics.median(values)
    n = float(len(values))
    blended = (PRIOR_WEIGHT * 1.0 + observed * n) / (PRIOR_WEIGHT + n)
    return _clamp_ratio(blended)


def _area_ratio(by_area, area):
    if not area:
        return None
    try:
        value = by_area.get(_normalise_area(area))
        return float(value) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _samples(store, window=CAL_WINDOW):
    """The last `window` completed-or-fed-back tasks with a usable measurement."""
    out = []
    for task in store.feedback_tasks(window):
        estimate = int(task.get("est_minutes") or 0)
        if estimate <= 0:
            continue
        actual = store.measured_minutes(task)
        if actual is None or actual < 0:
            continue
        area = _normalise_area(task.get("area"))
        out.append({
            "task_id": task["id"],
            "title": task.get("title") or "",
            "area": area,
            "estimate": estimate,
            "actual": int(actual),
            "ratio": round(actual / float(estimate), 4),
            "note": task.get("feedback_note") or "",
            "at": task.get("feedback_at") or task.get("updated_at") or "",
        })
    return out


def _aggregate(samples):
    """(overall median, per-area medians with >= 2 samples), both clamped."""
    if not samples:
        return None, {}
    ratio = _median_ratio([s["ratio"] for s in samples])
    groups = {}
    for sample in samples:
        groups.setdefault(sample["area"], []).append(sample["ratio"])
    by_area = {}
    for area, values in sorted(groups.items()):
        if len(values) >= 2:
            by_area[area] = _median_ratio(values)
    return ratio, by_area


def _display_ratio(store):
    ratio = _kv_float(store, "calibration:ratio", 1.0)
    by_area = _float_map(_kv_json(store, "calibration:by_area", {}))
    samples = _samples(store)
    if samples:
        computed, computed_by_area = _aggregate(samples)
        # Always believe the observations over the kv cache: the cache can be
        # stale (written before the blend was introduced, or by an older run)
        # and the UI must never show a number it cannot reproduce.
        if computed is not None:
            ratio = computed
        if computed_by_area:
            by_area = computed_by_area
    return ratio, by_area, samples


def calibration_summary(store):
    """{"ratio", "samples", "by_area", "recent"} — what the UI shows."""
    ratio, by_area, samples = _display_ratio(store)
    recent = [{"task_id": s["task_id"], "title": s["title"], "area": s["area"],
               "estimated": s["estimate"], "actual": s["actual"],
               "ratio": s["ratio"], "note": s["note"], "at": s["at"]}
              for s in samples[:5]]
    return {"ratio": round(float(ratio), 3),
            "samples": len(samples),
            "by_area": by_area,
            "recent": recent}


def _next_estimate(estimate, ratio, area_ratio=None):
    estimate = int(estimate or 0)
    if estimate <= 0:
        return 0
    target = area_ratio if area_ratio is not None else ratio
    if target is None:
        target = 1.0
    target = min(CAL_MAX, max(CAL_MIN, float(target)))
    return max(MIN_MINUTES, min(MAX_MINUTES,
                                int(round(estimate * target / 5.0) * 5)))


# --------------------------------------------------------------------------- #
# heuristic estimation with area calibration
# --------------------------------------------------------------------------- #
def _fallback_plan(task):
    return {
        "title": task.get("title"),
        "area": task.get("area") or DEFAULT_AREA,
        "complexity": "M",
        "estimate_minutes": 90,
        "priority": int(task.get("priority") or 2),
        "deep_work": True,
        "confidence": 0.2,
        "subtasks": [],
        "rationale": "rule of thumb (brain heuristic unavailable)",
    }


def _calibrated_minutes(store, minutes, area):
    """Apply the *area* ratio on top of the overall ratio brain already used."""
    overall = _kv_float(store, "calibration:ratio", 1.0)
    by_area = _kv_json(store, "calibration:by_area", {})
    area_ratio = _area_ratio(by_area, area)
    applied = min(CAL_MAX, max(CAL_MIN, overall))
    target = area_ratio if area_ratio is not None else overall
    target = min(CAL_MAX, max(CAL_MIN, float(target)))
    if applied <= 0 or abs(target - applied) < 0.01:
        return int(minutes), None
    scaled = float(minutes) * target / applied
    return max(MIN_MINUTES, min(MAX_MINUTES,
                                int(round(scaled / 5.0) * 5))), target


def _heuristic(store, task):
    """brain.heuristic_plan + per-area calibration; never raises."""
    try:
        plan = brain.heuristic_plan(store, task)
    except Exception:
        plan = _fallback_plan(task)
    plan = dict(plan)
    minutes, applied_area_ratio = _calibrated_minutes(
        store, plan.get("estimate_minutes") or 90, task.get("area"))
    if applied_area_ratio is not None:
        plan["estimate_minutes"] = minutes
        plan["subtasks"] = []      # regenerate against the rescaled estimate
        plan["rationale"] = ("%s; area '%s' ratio x%.2f"
                             % (str(plan.get("rationale") or "").rstrip(".; "),
                                task.get("area"), applied_area_ratio)).strip("; ")
    return plan


def _synthesize_subtasks(plan, title, related):
    """Give the UI concrete steps even when the rule produced none."""
    if plan.get("subtasks"):
        return plan
    minutes = int(plan.get("estimate_minutes") or 0)
    if minutes < 45:
        return plan
    inspect = "Inspect " + (related[0]["path"] if related else "the relevant files")
    if minutes >= 120:
        steps = [(inspect, 0.25),
                 ("Carry out: " + _short(title, 50), 0.55),
                 ("Verify and record the result", 0.20)]
    else:
        steps = [(inspect, 0.30),
                 ("Carry out: " + _short(title, 50), 0.70)]
    subtasks = [{"title": label,
                 "minutes": max(5, int(round(minutes * frac / 5.0) * 5))}
                for label, frac in steps]
    total = sum(sub["minutes"] for sub in subtasks)
    if total > minutes:
        subtasks[-1]["minutes"] = max(5, subtasks[-1]["minutes"] - (total - minutes))
    plan["subtasks"] = subtasks
    return plan


def _complexity_label(value):
    value = str(value or "M").upper()
    return {"1": "S", "2": "S", "3": "M", "4": "L", "5": "L"}.get(value, value)


def _normalize_plan(plan, title, area, fallback):
    """Defensive normalisation for a model answer (brain.validate_plan output)."""
    plan = dict(plan or {})
    plan["title"] = title
    plan["area"] = area or DEFAULT_AREA
    plan["complexity"] = _complexity_label(
        plan.get("complexity") or fallback.get("complexity"))
    try:
        minutes = int(plan.get("estimate_minutes"))
    except (TypeError, ValueError):
        minutes = int(fallback.get("estimate_minutes") or 90)
    plan["estimate_minutes"] = max(MIN_MINUTES, min(MAX_MINUTES, minutes))
    try:
        priority = int(plan.get("priority"))
    except (TypeError, ValueError):
        priority = int(fallback.get("priority") or 2)
    plan["priority"] = min(3, max(1, priority))
    plan["deep_work"] = bool(plan.get("deep_work", fallback.get("deep_work")))
    try:
        confidence = float(plan.get("confidence"))
    except (TypeError, ValueError):
        confidence = float(fallback.get("confidence") or 0.35)
    plan["confidence"] = min(1.0, max(0.0, confidence))
    subtasks = plan.get("subtasks")
    cleaned = []
    for step in (subtasks if isinstance(subtasks, list) else [])[:6]:
        if not isinstance(step, dict) or not step.get("title"):
            continue
        try:
            step_minutes = max(1, int(step.get("minutes") or 0))
        except (TypeError, ValueError):
            step_minutes = max(5, plan["estimate_minutes"] // 4)
        cleaned.append({"title": str(step["title"])[:200],
                        "minutes": step_minutes})
    plan["subtasks"] = cleaned
    plan["rationale"] = str(plan.get("rationale") or "")[:600]
    return plan


# --------------------------------------------------------------------------- #
# persistence + analysis
# --------------------------------------------------------------------------- #
def _write_plan(store, task_id, plan, source):
    fields = {
        "complexity": _complexity_label(plan.get("complexity")),
        "est_minutes": int(plan.get("estimate_minutes") or 90),
        "priority": int(plan.get("priority") or 2),
        "area": plan.get("area") or DEFAULT_AREA,
        "est_source": source,
        "est_rationale": str(plan.get("rationale") or "")[:600],
        "subtasks": json.dumps(plan.get("subtasks") or [], ensure_ascii=False),
        "confidence": _as_float(plan.get("confidence"), 0.35),
        "deep_work": 1 if plan.get("deep_work") else 0,
        "agent_updated_at": store_now(),
    }
    store.update_task(task_id, **fields)
    store.add_event("agent_estimate", task_id, source=source,
                    complexity=fields["complexity"],
                    minutes=fields["est_minutes"],
                    confidence=fields["confidence"], intake=True)


def _deadline_priority(plan, days_left, cfg):
    if days_left is None:
        return
    try:
        soon = int((cfg or {}).get("deadline_soon_days", 3))
    except (TypeError, ValueError):
        soon = 3
    if days_left <= soon:
        plan["priority"] = 1


def _build_analysis(plan, title, area, area_hits, related, status,
                    deadline, days_left, note=""):
    minutes = int(plan.get("estimate_minutes") or 0)
    complexity = _complexity_label(plan.get("complexity"))
    priority = int(plan.get("priority") or 2)
    why = []
    if area_hits:
        why.append("area '%s' inferred from keywords: %s"
                   % (area, ", ".join(area_hits[:4])))
    else:
        why.append("area '%s' (no strong keyword; general bucket)" % area)
    if related:
        picked = [r for r in related if r.get("why")]
        inspected = [r for r in related if not r.get("why")]
        if picked:
            why.append("grounded in %d file(s): %s"
                       % (len(picked),
                          "; ".join("%s — %s" % (r["path"], r["why"])
                                    for r in picked[:3])))
        if inspected:
            why.append("read-only inspection found %d existing artefact(s): %s"
                       % (len(inspected),
                          ", ".join(item["path"] for item in inspected[:4])))
        if not picked and not inspected:
            why.append("no matching artefact; the estimate rests on the "
                       "keyword rule")
    else:
        why.append("no cited material; the estimate rests on the keyword rule")
    if plan.get("rationale"):
        why.append(str(plan["rationale"])[:240])
    if deadline:
        why.append("deadline %s is %d day(s) away" % (deadline, days_left or 0))
    subtasks = plan.get("subtasks") or []
    if subtasks:
        why.append("%d subtask(s), %d min of the %d min estimate"
                   % (len(subtasks), sum(int(s.get("minutes") or 0)
                                         for s in subtasks), minutes))
    if note:
        why.append(note)
    summary = ("%s — %s work, ~%d min, priority %d, %d related artefact(s)."
               % (title, complexity, minutes, priority, len(related)))
    return {
        "summary": summary,
        "why": why,
        "area": area,
        "complexity": complexity,
        "minutes": minutes,
        "priority": priority,
        "deep_work": bool(plan.get("deep_work")),
        "confidence": min(1.0, max(0.0, _as_float(plan.get("confidence"), 0.3))),
        "subtasks": [{"title": str(s.get("title") or ""),
                      "minutes": int(s.get("minutes") or 0)}
                     for s in subtasks],
        "related": related,
    }


# --------------------------------------------------------------------------- #
# public contract
# --------------------------------------------------------------------------- #
def _resolve_root(repo_root):
    root = os.path.abspath(str(repo_root or "."))
    if os.path.isdir(root):
        return root
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, ".."))


def _as_date(value):
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return date.today()


def _iso_deadline(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        raise ValueError("deadline must be YYYY-MM-DD, got %r" % (value,))


def _sanitize_within(value):
    """'Plan within N weeks' -> int in 1..26, or None for 'any week'."""
    if value is None or value == "":
        return None
    try:
        within = int(value)
    except (TypeError, ValueError):
        return None
    return None if within < 1 else min(26, within)


def _plan_from_task(task):
    """Rebuild an analysis-ready plan dict from the task's own fields."""
    try:
        subtasks = json.loads(task.get("subtasks") or "[]")
    except (TypeError, json.JSONDecodeError):
        subtasks = []
    return {"title": task.get("title"), "area": task.get("area"),
            "complexity": task.get("complexity"),
            "estimate_minutes": task.get("est_minutes"),
            "priority": task.get("priority"),
            "deep_work": bool(task.get("deep_work")),
            "confidence": task.get("confidence"),
            "subtasks": subtasks,
            "rationale": task.get("est_rationale") or ""}


def _join_notes(note, extra):
    """Stitch 'deadline …; wake …' — drops empty parts, no double semicolons."""
    note, extra = str(note or "").strip(), str(extra or "").strip()
    if not extra:
        return note
    return "%s; %s" % (note, extra) if note else extra


def create_from_description(store, cfg, description, repo_root="planner/..",
                            deadline=None, now=None, category=None,
                            backend_override=None, within_weeks=None):
    """Turn ONE sentence into a task, an estimate and cited project artefacts.

    Returns exactly:
        {"task_id": int, "status": "applied", "analysis": {...},
         "plan": dict}

    Never raises because of a backend problem: an llm failure falls back to
    brain.heuristic_plan, and the analysis is always present. Empty input and
    a malformed deadline are caller errors (ValueError).

    `category` ("Thesis" or a subject folder name) wins as the task's area;
    without it the area is inferred from keywords. With the llm backend the
    estimate is grounded in the user's material: the folder inventory goes
    into the prompt, the model picks up to 6 significant files with a reason,
    and when it does, a second call re-estimates with their full text
    (extracted read-only, budgeted, truncation-transparent).

    `backend_override` ("heuristic") forces a purely offline estimate
    regardless of configuration. `within_weeks` sets the task's planning
    window — how many weeks ahead the planner may place it. When no deadline
    is given, the flagged next presentation meeting (if any) becomes the
    default deadline.
    """
    raw = " ".join(str(description or "").split())
    original = str(description or "").strip()
    if not raw:
        raise ValueError("description is required")
    cfg = cfg or {}
    root = _resolve_root(repo_root)
    today = _as_date(now)
    deadline_iso = _iso_deadline(deadline)
    deadline_note = ""
    if not deadline_iso and hasattr(store, "get_next_meeting"):
        meeting = store.get_next_meeting() or {}
        if meeting.get("date") and meeting["date"] >= today.isoformat():
            deadline_iso = meeting["date"]
            deadline_note = ("deadline defaulted to the next presentation "
                             "meeting (%s)" % meeting["date"])
    days_left = None
    if deadline_iso:
        days_left = (date.fromisoformat(deadline_iso) - today).days

    title = make_title(raw)
    category = str(category or "").strip()
    if category and category.lower() != sources.THESIS_CATEGORY.lower():
        area, area_hits = category, []      # the picker wins: a real folder
    elif category:
        area, area_hits = sources.THESIS_CATEGORY, []
    else:
        area, area_hits = _area_info(raw)
    if category and category.lower() != sources.THESIS_CATEGORY.lower():
        related = []                        # course material, not repo code
    else:
        try:
            related = inspect_repo(raw, root, MAX_RELATED)
        except Exception:    # a broken symlink/permission must not block intake
            related = []

    backend = str(((cfg.get("brain") or {}).get("backend") or "llm")).strip().lower()
    if backend_override in ("heuristic", "llm"):
        backend = backend_override
    probe = {"title": title, "area": area, "priority": 2, "est_minutes": 60}
    provisional = _heuristic(store, probe)
    provisional["title"] = title
    provisional["area"] = area
    _synthesize_subtasks(provisional, title, related)
    _deadline_priority(provisional, days_left, cfg)

    task_id = store.add_task(
        title, notes=original[:4000], area=area,
        priority=int(provisional.get("priority") or 2),
        complexity=_complexity_label(provisional.get("complexity")),
        est_minutes=int(provisional.get("estimate_minutes") or 90),
        deadline=deadline_iso,
        files=", ".join(item["path"] for item in related),
        deep_work=1 if provisional.get("deep_work") else 0,
        within_weeks=_sanitize_within(within_weeks))

    status, used, note = "applied", "heuristic", deadline_note
    final = provisional
    picked = []

    if backend == "llm":
        try:
            context = brain.project_context(store, root)
            material, inv = _material_for(cfg, area)
            if material:
                context = context + "\n\n" + material
            answer = brain.llm_plan(raw, context, cfg)
            picked = _picked_files(inv, answer.get("significant_files"))
            if picked:
                excerpts, read_note = _read_picked(cfg, picked)
                if excerpts:
                    try:
                        # second call: same prompt plus the real text — its
                        # answer wins, but call A's answer still stands when
                        # this one fails (never degrades to the heuristic
                        # just because refinement hiccuped)
                        answer = brain.llm_plan(raw, context + "\n\n"
                                                + excerpts, cfg)
                    except Exception as exc:
                        note = _join_notes(
                            note, "full-text refinement failed (%s)" % exc)
                note = _join_notes(note, read_note)
            final = _normalize_plan(answer, title, area, provisional)
            used = "llm"
            model = str(((cfg.get("brain") or {}).get("llm") or {})
                        .get("model") or "").strip()
            note = _join_notes(note, "instant API estimate via %s"
                               % (model or "the configured LLM"))
        except Exception as exc:          # BrainError, transport, bad JSON
            note = ("llm backend failed (%s); used the heuristic%s"
                    % (exc, "; " + deadline_note if deadline_note else ""))
            picked = []
    elif backend != "heuristic":
        note = "unknown backend %r; used the heuristic" % backend

    if picked:
        seen = [item["path"] for item in related]
        for p in picked:
            if p["path"] not in seen:
                related.append({"path": p["path"], "why": p["why"]})
        store.update_task(task_id, files=", ".join(
            dict.fromkeys([item["path"] for item in related])))

    _synthesize_subtasks(final, title, related)
    _deadline_priority(final, days_left, cfg)
    _write_plan(store, task_id, final, "llm" if used == "llm" else "heuristic")
    shown = final

    analysis = _build_analysis(
        shown, title, area, area_hits, related, status, deadline_iso, days_left,
        note=note)
    return {"task_id": task_id, "status": status, "analysis": analysis,
            "plan": final}


def _material_for(cfg, area):
    """(context block, inventory) for the task's category; ('', None) when the
    university root is unset or the area has nothing to show."""
    if not sources.university_root(cfg) and area == sources.THESIS_CATEGORY:
        return "", None
    try:
        inv = sources.inventory(cfg)
    except Exception as exc:            # a broken folder must not block intake
        return ("(university folder unreadable: %s)" % exc), None
    blocks = sources.material_blocks(inv, area)
    return ("\n\n".join(blocks) if blocks else ""), inv


def _picked_files(inv, wanted):
    """LLM file picks -> {"path" (relative), "why", "abs"}; path-safe."""
    if not wanted or not isinstance(wanted, list):
        return []
    out, seen = [], set()
    root = (inv or {}).get("root") or ""
    real_root = os.path.realpath(root) if root else ""
    for item in wanted[:6]:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip()
        why = str(item.get("why") or "").strip()[:300]
        if not rel or rel in seen:
            continue
        seen.add(rel)
        cand = rel if os.path.isabs(rel) else os.path.join(root, rel)
        real = os.path.realpath(cand)
        if root and not (real == real_root or real.startswith(real_root + os.sep)):
            continue                    # never follow picks outside the root
        if not os.path.isfile(real):
            continue                    # a hallucinated path is not material
        out.append({"path": rel, "why": why or "significant for this task",
                    "abs": real})
    return out


def _read_picked(cfg, picked, budget=sources.DEFAULT_BUDGET):
    """Full text for the picked files. Returns (excerpts block, honest note)."""
    try:
        results = sources.extract_text([p["abs"] for p in picked], budget)
    except Exception as exc:            # extraction machinery failure
        return "", "full-text grounding unavailable (%s)" % exc
    for p, r in zip(picked, results):
        p["read"] = bool((r.get("text") or "").strip())
        p["chars"] = r.get("chars", 0)
        p["error"] = r.get("error")
    block = sources.excerpts_block(results)
    ok = [p for p in picked if p["read"]]
    if not ok:
        reasons = "; ".join("%s — %s" % (p["path"], p["error"] or "no text")
                            for p in picked)
        return "", "none of the picked files could be read (%s)" % reasons
    note = "read %d file(s) in full: %s" % (
        len(ok), ", ".join("%s (%s)" % (p["path"], p["why"]) for p in ok))
    failed = [p for p in picked if not p["read"]]
    if failed:
        note += "; could not read: %s" % ", ".join(
            "%s (%s)" % (p["path"], p["error"] or "no text") for p in failed)
    return block, note


def apply_feedback(store, cfg, task_id, actual_minutes, note=""):
    """Record how long a task really took and recalibrate future estimates.

    Stores actual_minutes/feedback_note/feedback_at and an event
    'task_feedback'; recomputes the overall median actual/estimated ratio over
    the last 20 completed-or-fed-back tasks and a per-area ratio for areas with
    at least two samples; persists both to kv. Returns:
        {"task": {...}, "calibration": {...}, "before": {...}, "after": {...}}
    where before/after carry the old/new ratio and the next similar estimate.
    """
    task = store.get_task(task_id)
    if not task:
        raise ValueError("no task #%s" % task_id)
    minutes = parse_duration(actual_minutes)
    note = str(note or "").strip()[:1000]

    old_ratio = _kv_float(store, "calibration:ratio", 1.0)
    old_by_area = _kv_json(store, "calibration:by_area", {})
    old_area_ratio = _area_ratio(old_by_area, task.get("area"))
    estimate = int(task.get("est_minutes") or 0)

    store.set_feedback(task_id, minutes, note)
    samples = _samples(store, CAL_WINDOW)
    ratio, by_area = _aggregate(samples)
    if ratio is None:
        ratio, by_area = old_ratio, old_by_area
    store.kv_set("calibration:ratio", ratio)
    store.kv_set("calibration:by_area", json.dumps(by_area, sort_keys=True))
    store.add_event("calibration_updated", None, ratio=ratio,
                    samples=len(samples), by_area=by_area)

    new_area_ratio = _area_ratio(by_area, task.get("area"))
    before_minutes = _next_estimate(estimate, old_ratio, old_area_ratio)
    before = {"estimate": estimate, "minutes": before_minutes,
              "ratio": round(old_ratio, 3),
              "area_ratio": old_area_ratio,
              "expected_next": before_minutes}
    after_minutes = _next_estimate(estimate, ratio, new_area_ratio)
    after = {"estimate": estimate, "minutes": after_minutes,
             "ratio": round(float(ratio), 3),
             "area_ratio": new_area_ratio,
             "expected_next": after_minutes}
    if before["expected_next"]:
        after["change_pct"] = int(round(
            (after["expected_next"] - before["expected_next"])
            / float(before["expected_next"]) * 100))
    else:
        after["change_pct"] = 0
    return {"task": store.get_task(task_id),
            "calibration": calibration_summary(store),
            "before": before, "after": after}


def suggest_next(store, cfg, repo_root="."):
    """0-3 tasks worth adding next (read-only: does not create anything)."""
    root = _resolve_root(repo_root)
    try:
        from planner.project_scan import suggest as scan_suggest
        found = scan_suggest(store, repo_root=root, add=False)
    except Exception:
        found = []
    if not found:
        return []
    existing = {_short(t.get("title"), 200).lower()
                for t in store.list_tasks()
                if t.get("status") in ("todo", "doing")}
    out = []
    for item in found:
        title = make_title(item.get("title") or "")
        if not title or title.lower() in existing:
            continue
        source = str(item.get("source") or "")
        area, _ = _area_info("%s %s" % (title, source))
        if area == DEFAULT_AREA:
            area = {"audit": "analysis", "code": "improvement",
                    "git": "improvement"}.get(str(item.get("area")), DEFAULT_AREA)
        plan = _heuristic(store, {"title": title, "area": area, "priority": 2})
        out.append({"title": title, "area": area,
                    "complexity": _complexity_label(plan.get("complexity")),
                    "minutes": int(plan.get("estimate_minutes") or 0),
                    "priority": 2, "why": source[:160] or "project scan"})
        existing.add(title.lower())
        if len(out) >= 3:
            break
    return out
