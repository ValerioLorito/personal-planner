"""
brain.py — the agentic layer: understand a task in project context, estimate it,
break it down, and let the scheduler place it.

Design
------
The planner is *not* a linter. When you type

    plan add "create an audit for coreference resolution"

the brain reads the PROJECT (profile + git history + repo layout + prior
estimates + observed actual times), asks a model for a structured judgement
(complexity, minutes, priority, subtasks, rationale, confidence), writes it
back onto the task (`est_source`, `est_rationale`, `subtasks`, `confidence`),
and the deterministic scheduler then puts the blocks in your week.

Two backends (`brain.backend` in config.json):

  llm         (default) any OpenAI-compatible /chat/completions endpoint —
              OpenRouter in practice. Point it at
              OpenAI / Anthropic-compatible gateways / a local server — NOT at
              the HPC vLLM link, which is deliberately unsupported here.
              The key comes from the env var named by brain.llm.api_key_env
              or, failing that, the key file brain.llm.key_file (default
              planner/data/llm_key) — the file also reaches Finder-launched
              servers, which never read shell profiles.
  heuristic   offline keyword/scale rules + calibration; always available.

Calibration: every done task with a task_doing→task_done gap contributes to a
running ratio (actual/estimated), stored in the kv table and applied to future
heuristic estimates. The llm backend sees it in the prompt.
"""
import json
import os
import re
import statistics
import subprocess
import urllib.error
import urllib.request
from datetime import date, datetime

from planner import config as config_mod
from planner.store import now

ALLOWED_COMPLEXITY = {"S", "M", "L", "1", "2", "3", "4", "5"}
DEFAULT_AREA = "general"


# ==========================================================
# 1. PROJECT CONTEXT  (what the model is allowed to know)
# ==========================================================
def _read(path, limit=2000):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(limit)
    except OSError:
        return ""


def _git(repo_root, args, limit=4000):
    import subprocess
    try:
        out = subprocess.run(["git", "-C", repo_root] + args, capture_output=True,
                             text=True, timeout=20)
        return out.stdout[:limit] if out.returncode == 0 else ""
    except Exception:
        return ""


def _calibration_by_area(store):
    """Per-area actual/estimated ratios stored by planner.intake.apply_feedback."""
    raw = store.kv_get("calibration:by_area")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    out = {}
    for key, value in (data or {}).items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _feedback_lines(store, limit=5):
    """One line per recent user-reported actual, for the estimating prompt."""
    try:
        rows = store.feedback_tasks(limit) if hasattr(store, "feedback_tasks") else []
    except Exception:
        rows = []
    lines = []
    for t in rows:
        est = int(t.get("est_minutes") or 0)
        actual = (store.measured_minutes(t)
                  if hasattr(store, "measured_minutes") else None)
        if not est or actual is None:
            continue
        note = " ".join(str(t.get("feedback_note") or "").split())
        line = (f"#{t['id']} {str(t.get('title') or '')[:48]}: "
                f"est {est} -> actual {actual} min (x{actual / float(est):.2f})")
        if note:
            line += f" — {note[:80]}"
        lines.append(line[:190])
    return lines[:limit]


def project_context(store, repo_root=".", max_chars=9000):
    """A compact, honest description of the project for the estimating model."""
    parts = []

    profile = _read(os.path.join(repo_root, "planner", "project_profile.md"), 3000)
    if profile:
        parts.append("## PROJECT PROFILE\n" + profile.strip())
    else:
        parts.append("## PROJECT README\n" +
                     _read(os.path.join(repo_root, "README.md"), 800).strip())

    log = _git(repo_root, ["log", "-15", "--date=short",
                           "--pretty=format:%ad %s"])
    if log:
        parts.append("## RECENT COMMITS (newest first)\n" + log.strip())

    counts = {}
    for sub in ("src", "slurm_scripts", "audits", "planner"):
        p = os.path.join(repo_root, sub)
        if os.path.isdir(p):
            n = sum(1 for r, _, fs in os.walk(p) for f in fs
                    if f.endswith((".py", ".sh", ".md")))
            counts[sub] = n
    if counts:
        parts.append("## TREE\n" + ", ".join(f"{k}/ ({v} files)"
                                             for k, v in sorted(counts.items())))

    open_items = _open_audit_items(repo_root)
    if open_items:
        parts.append("## OPEN THREADS (from audits/)\n" + "\n".join(open_items[:8]))

    tasks = [t for t in store.list_tasks() if t["status"] in ("todo", "doing")][:25]
    if tasks:
        lines = [f"#{t['id']} [{t['status']}] {t['complexity']} {t['est_minutes']}min"
                 f"{' P' + str(t['priority']) if t['priority'] != 2 else ''}"
                 f" — {t['title']}" for t in tasks]
        parts.append("## CURRENT OPEN TASKS\n" + "\n".join(lines))

    week = _this_monday()
    plan = store.get_plan(week)
    if plan:
        per_day = {}
        for s in plan["slots"]:
            per_day[s["date"]] = per_day.get(s["date"], 0) + s["minutes"]
        parts.append("## THIS WEEK'S PLAN\n" +
                     ", ".join(f"{d}: {m}min" for d, m in sorted(per_day.items())))

    meeting = _next_meeting_section(store)
    if meeting:
        parts.append(meeting)

    ratio = store.kv_get("calibration:ratio")
    by_area = _calibration_by_area(store)
    feedback = _feedback_lines(store, 5)
    done = [t for t in store.list_tasks("done")][:10]
    if ratio or by_area or feedback or done:
        lines = []
        if ratio:
            try:
                lines.append(f"observed actual/estimated ratio: {float(ratio):.2f}")
            except (TypeError, ValueError):
                lines.append(f"observed actual/estimated ratio: {ratio}")
        if by_area:
            lines.append("by area: " + ", ".join(
                f"{a} x{v:.2f}" for a, v in sorted(by_area.items())))
        if feedback:
            lines.append("recent feedback (user-reported actuals):")
            lines.extend("  " + line for line in feedback)
        for t in done[:5]:
            actual = store.actual_minutes(t["id"])
            if actual and t["est_minutes"]:
                lines.append(f"#{t['id']} estimated {t['est_minutes']}min → "
                             f"actual {actual}min: {t['title'][:60]}")
        if lines:
            parts.append("## CALIBRATION\n" + "\n".join(lines))

    text = "\n\n".join(parts)
    return text[:max_chars]


def _this_monday():
    d = date.today()
    return (d - __import__("datetime").timedelta(days=d.weekday())).isoformat()


def _next_meeting_section(store):
    """## NEXT PRESENTATION MEETING — the milestone for planning agents.

    The user flags the next PI/presentation meeting (from Apple Calendar in the
    UI, or `cli meeting set`). A planning agent must know that date: tasks
    with deadline == meeting are P1, and the section also says how much work
    is already scheduled between now and the meeting, so the estimate/priority
    reflect how the plan is proceeding rather than an abstract calendar.
    """
    try:
        meeting = store.get_next_meeting()
    except Exception:
        return ""
    if not meeting or not meeting.get("date"):
        return ""
    try:
        mdate = date.fromisoformat(str(meeting["date"])[:10])
    except ValueError:
        return ""
    today = date.today()
    days = (mdate - today).days
    if days < 0:
        return ""               # stale flag; the next one will be re-flagged
    lines = [f"date: {meeting['date']} ({days} day(s) out; this Monday is {_this_monday()})"]
    title = (meeting.get("title") or "").strip()
    if title:
        lines.append(f"title: {title}")
    try:
        slots = [s for s in store.slots_between(today.isoformat(),
                                               meeting["date"])]
        minutes = sum(int(s.get("minutes") or 0) for s in slots)
        lines.append("already scheduled before the meeting: %d block(s), "
                     "%d min across %d day(s)"
                     % (len(slots), minutes,
                        len({s["date"] for s in slots})))
    except Exception:
        pass
    return "## NEXT PRESENTATION MEETING\n" + "\n".join(lines)


def _open_audit_items(repo_root):
    """'- ' bullets under 'still open / open question / deferred' headings."""
    items, in_block = [], False
    audits = os.path.join(repo_root, "audits")
    if not os.path.isdir(audits):
        return items
    for name in sorted(os.listdir(audits)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(audits, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    s = line.rstrip()
                    if re.match(r"^#{1,6}\s", s):
                        in_block = bool(re.search(
                            r"still\s+open|open\s+question|deferred", s, re.I))
                        continue
                    if in_block and re.match(r"^\s*[-*]\s+", s):
                        items.append(f"[{name}] {s.strip()[:160]}")
        except OSError:
            continue
    return items


# ==========================================================
# 2. PROMPT
# ==========================================================
SYSTEM_PROMPT = """You are the planning agent for a university worker: a PhD
thesis repository plus per-subject university course folders (readings,
lecture slides, problem sets), all grounded in the context below.

You receive ONE task in natural language plus a compact PROJECT CONTEXT
(which may include MATERIAL blocks listing the user's real files and
MATERIAL EXCERPTS with full text of files picked earlier).
Decide how it should be executed and how long it really takes *in this project*.

Rules
- Judge effort against the actual material in the context, not generic advice:
  the codebase for Thesis work, the listed/excerpted files for course work.
- complexity: "S" (small, <45 min), "M" (medium, 45-150 min), "L" (large, >150 min).
- estimate_minutes: realistic hands-on time for THIS material (reruns on the HPC,
  code edits, audits, writing, reading a named PDF end to end). Prefer
  under-estimating scope, not time.
- priority: 1 = blocks the next PI meeting, 2 = normal, 3 = nice to have.
- deep_work: true when the task needs uninterrupted focus (design, coding, analysis).
- subtasks: 0-6 concrete steps, each with its own minutes (sum <= estimate_minutes).
- area: the task's own bucket. When the context lists task categories (e.g.
  "Thesis" or subject names like "NLP Semantics"), use exactly one of those —
  verbatim. Only when no category list is present, fall back to: improvement,
  coreference, atlas, analysis, writing, infra, general.
- calibration: a ratio below 1 means past tasks took LESS than estimated. When
  the context lists a per-area ratio for this task's area, prefer it over the
  overall ratio. Recent feedback lines are the strongest evidence.
- confidence: 0..1 for your estimate.
- rationale: one or two sentences, naming the project artefacts or files that
  justify the size.
- significant_files: 0-6 entries, only when MATERIAL blocks are present. Pick
  the files whose FULL TEXT would change your estimate or the plan (a named
  paper to read, the slides of the relevant week, the spec of the assignment).
  Each entry: {"path": exactly as listed in MATERIAL, "why": one short
  sentence}. An empty list means the listings above already told you enough —
  do not pad it.

Return STRICT JSON only, no prose, matching:
{"title": str, "area": str, "complexity": "S|M|L", "estimate_minutes": int,
 "priority": 1|2|3, "deep_work": bool, "confidence": float,
 "subtasks": [{"title": str, "minutes": int}], "rationale": str,
 "significant_files": [{"path": str, "why": str}]}
"""


def build_prompt(task_text, context):
    """The estimation prompt: the task, the grounded project context."""
    user = (f"TASK:\n{task_text}\n\n"
            f"PROJECT CONTEXT:\n{context}\n\n"
            "Estimate this task for this repository. Return the JSON object only.")
    return SYSTEM_PROMPT, user


# ==========================================================
# 3. BACKENDS
# ==========================================================
class BrainError(RuntimeError):
    pass


def _parse_json(text):
    """Tolerant JSON extraction from an LLM response."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise BrainError("model did not return valid JSON")


def validate_plan(plan):
    """Normalise + validate a plan dict; raises BrainError on garbage."""
    if not isinstance(plan, dict):
        raise BrainError("plan is not an object")
    complexity = str(plan.get("complexity", "M")).upper()
    if complexity not in ALLOWED_COMPLEXITY:
        raise BrainError(f"bad complexity: {complexity!r}")
    try:
        minutes = int(plan.get("estimate_minutes", 0))
    except (TypeError, ValueError):
        raise BrainError("estimate_minutes is not an integer")
    if minutes <= 0 or minutes > 60 * 24 * 5:
        raise BrainError(f"implausible estimate: {minutes}")
    subtasks = plan.get("subtasks") or []
    if not isinstance(subtasks, list):
        raise BrainError("subtasks is not a list")
    clean_sub = []
    for s in subtasks[:6]:
        if not isinstance(s, dict) or not s.get("title"):
            continue
        try:
            m = max(1, int(s.get("minutes", minutes // max(1, len(subtasks)))))
        except (TypeError, ValueError):
            m = minutes // max(1, len(subtasks))
        clean_sub.append({"title": str(s["title"])[:200], "minutes": m})
    try:
        priority = int(plan.get("priority", 2))
    except (TypeError, ValueError):
        priority = 2
    try:
        confidence = float(plan.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "title": str(plan.get("title") or "").strip()[:200] or None,
        "area": str(plan.get("area") or DEFAULT_AREA).strip().lower()[:40],
        "complexity": complexity,
        "estimate_minutes": minutes,
        "priority": min(3, max(1, priority)),
        "deep_work": bool(plan.get("deep_work", False)),
        "confidence": min(1.0, max(0.0, confidence)),
        "subtasks": clean_sub,
        "rationale": str(plan.get("rationale") or "")[:600],
        "significant_files": [
            {"path": str(f.get("path") or "").strip()[:300],
             "why": str(f.get("why") or "").strip()[:300]}
            for f in (plan.get("significant_files") or [])[:6]
            if isinstance(f, dict) and str(f.get("path") or "").strip()
        ],
    }


# ---------- heuristic ----------
RULES = [
    (r"\b(audit|review|inspect|analyse|analyze|diagnos)", 120, "M"),
    (r"\b(rerun|re-run|rerun|experiment|benchmark|evaluate|run)\b", 180, "L"),
    (r"\b(implement|refactor|fix|debug|patch|bug)\b", 90, "M"),
    (r"\b(write|draft|paper|section|chapter|thesis|report)\b", 240, "L"),
    (r"\b(slurm|hpc|job|cluster)\b", 60, "S"),
    (r"\b(read|check|look|verify|small|quick|typo)\b", 30, "S"),
]


def heuristic_plan(store, task, context=""):
    """Offline floor: keyword rules × observed calibration ratio."""
    text = (task.get("title") or "").lower()
    minutes, complexity = 90, "M"
    for pattern, m, c in RULES:
        if re.search(pattern, text, re.I):
            minutes, complexity = m, c
            break
    ratio = store.kv_get("calibration:ratio")
    try:
        ratio = float(ratio) if ratio else 1.0
    except ValueError:
        ratio = 1.0
    ratio = min(2.5, max(0.4, ratio))
    minutes = max(15, int(round(minutes * ratio / 5.0) * 5))
    deep = complexity != "S"
    subtasks = []
    if minutes > 120:
        n = 3 if minutes <= 240 else 4
        each = max(20, round(minutes / n / 5) * 5)
        subtasks = [{"title": f"{task.get('title', 'task')} — step {i + 1}",
                     "minutes": each} for i in range(n)]
    return validate_plan({
        "title": task.get("title"),
        "area": task.get("area") or DEFAULT_AREA,
        "complexity": complexity,
        "estimate_minutes": minutes,
        "priority": task.get("priority", 2),
        "deep_work": deep,
        "confidence": 0.35,
        "subtasks": subtasks,
        "rationale": (f"heuristic rule for '{complexity}' work"
                      f"{'; calibration ×' + str(round(ratio, 2)) if ratio != 1 else ''}"),
    })


# ---------- llm ----------
def _http_post_json(url, payload, headers, timeout=90):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise BrainError(f"LLM endpoint {url} returned HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise BrainError(f"LLM endpoint unreachable ({url}): {e.reason}")


def llm_key_source(cfg):
    """The llm backend's API key, resolved locally in a fixed order.

    1. the env var named by brain.llm.api_key_env (default PLANNER_LLM_KEY;
       skipped entirely when api_key_env is "" — a declared keyless endpoint),
    2. the key file brain.llm.key_file (default planner/data/llm_key —
       gitignored, read per call, CWD-independent).

    Finder/launchd-launched apps (the Dock app) never read shell profiles, so
    the key file is the reliable way to hand them a key. Returns (key, source)
    with source in {"env", "file", "keyless", ""}; "" means no key anywhere —
    callers then proceed without an Authorization header.
    """
    llm = (cfg.get("brain") or {}).get("llm") or {}
    # api_key_env absent → default env name; present-but-empty → keyless endpoint
    raw_env = llm.get("api_key_env")
    env_name = "PLANNER_LLM_KEY" if raw_env is None else str(raw_env).strip()
    if env_name:
        key = os.environ.get(env_name, "").strip()
        if key:
            return key, "env"
    key_file = str(llm.get("key_file") or "").strip()
    path = (os.path.expanduser(key_file) if key_file
            else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "llm_key"))
    try:
        with open(path, encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key, "file"
    except OSError:
        pass
    return "", ("keyless" if not env_name else "")


def llm_json(system, user, cfg):
    """One synchronous chat completion, answer parsed as strict JSON.

    The generic transport behind llm_plan (task estimates) and the habits
    chat parser — same endpoint, same key resolution, same error contract
    (BrainError on misconfiguration, transport failure or bad JSON).
    """
    llm = (cfg.get("brain") or {}).get("llm") or {}
    base = (llm.get("base_url") or "").rstrip("/")
    model = llm.get("model") or ""
    if not base or not model:
        raise BrainError("brain.llm.base_url and brain.llm.model must be set "
                         "(config --set brain.llm.base_url=...)")
    key, _ = llm_key_source(cfg)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = {"model": model, "temperature": 0.2,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    data = _http_post_json(f"{base}/chat/completions", payload, headers,
                           timeout=int(llm.get("timeout", 90)))
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise BrainError("unexpected LLM response shape")
    return _parse_json(content)


def llm_plan(task_text, context, cfg):
    """OpenAI-compatible chat completion. Deliberately generic: the user points
    base_url at whatever they trust; the HPC vLLM link is not used by default."""
    system, user = build_prompt(task_text, context)
    return validate_plan(llm_json(system, user, cfg))


# ==========================================================
# 4. APPLY + CALIBRATION
# ==========================================================
def _apply_plan(store, tid, plan, source):
    task = store.get_task(tid)
    if not task:
        return None
    fields = {
        "complexity": plan["complexity"],
        "est_minutes": plan["estimate_minutes"],
        "priority": plan["priority"],
        "area": plan["area"] or task.get("area") or DEFAULT_AREA,
        "est_source": source,
        "est_rationale": plan["rationale"],
        "subtasks": json.dumps(plan["subtasks"], ensure_ascii=False),
        "confidence": plan["confidence"],
        "deep_work": 1 if plan.get("deep_work") else 0,
        "agent_updated_at": now(),
    }
    if plan.get("title") and not task.get("title"):
        fields["title"] = plan["title"]
    store.update_task(tid, **fields)
    store.add_event("agent_estimate", tid, source=source,
                    complexity=plan["complexity"],
                    minutes=plan["estimate_minutes"],
                    confidence=plan["confidence"])
    return store.get_task(tid)


def enrich_task(store, cfg, tid, repo_root=".", task_text=None):
    """Estimate one task with the configured backend. Returns a status dict."""
    task = store.get_task(tid)
    if not task:
        raise BrainError(f"no task #{tid}")
    text = task_text or task.get("title") or ""
    context = project_context(store, repo_root)
    backend = ((cfg.get("brain") or {}).get("backend") or "llm").lower()

    if backend == "llm":
        plan = llm_plan(text, context, cfg)
        _apply_plan(store, tid, plan, source="llm")
        return {"status": "applied", "backend": backend, "plan": plan}

    if backend == "heuristic":
        plan = heuristic_plan(store, task, context)
        _apply_plan(store, tid, plan, source="heuristic")
        return {"status": "applied", "backend": backend, "plan": plan}

    raise BrainError(f"unknown brain backend {backend!r} "
                     f"(use llm|heuristic)")


def update_calibration(store, window=20):
    """Median actual/estimated ratio over recent completed or fed-back tasks."""
    rows = (store.feedback_tasks(window) if hasattr(store, "feedback_tasks")
            else store.list_tasks("done")[:window])
    ratios = []
    for t in rows:
        actual = (store.measured_minutes(t) if hasattr(store, "measured_minutes")
                  else store.actual_minutes(t["id"]))
        if actual and t.get("est_minutes"):
            ratios.append(actual / float(t["est_minutes"]))
    if len(ratios) >= 2:
        ratio = round(min(2.5, max(0.4, statistics.median(ratios))), 3)
        store.kv_set("calibration:ratio", ratio)
        store.add_event("calibration_updated", None, ratio=ratio, n=len(ratios))
        return ratio
    return None
