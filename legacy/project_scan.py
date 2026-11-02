"""
project_scan.py — read the repository, propose planner tasks.

Three cheap, deterministic signals (no LLM, no writes to the repo):
1. Marker words (todo, fixme, xxx — uppercase only) in *.py, *.sh, *.md
   outside the busy directories.
2. Open questions left in `audits/*.md` under headings that mention
   "Still open", "open question" or "deferred".
3. Uncommitted changes (`git status --porcelain`) as one review task.

Public API
    scan_todos(repo_root=".", max_files=400) -> [{"path", "line", "text"}, ...]
    git_state(repo_root=".") -> {"branch", "dirty": [...], "last_commit": {...}}
    suggest(store, repo_root=".", add=False) -> [{"title", "area", "source"}, ...]

With add=True each suggestion becomes a small task (complexity 'S', 30 min,
notes = source) and one 'scan_suggested' event records the batch.

`audits/` is skipped by the marker scan (those files are prose, and the audit
parser reads them instead) but is read explicitly by `suggest`.
"""
import os
import re
import subprocess

TEXT_EXTS = (".py", ".sh", ".md")
SKIP_DIRS = {
    # required by the planner's layout
    ".git", "data", "external", ".zwork", "audits", "node_modules",
    # the planner's own code/docs/test fixtures are tooling, not thesis TODOs
    "planner", "tests",
    # noise that never holds thesis TODOs
    "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
}
TODO_RE = re.compile(r"\b(TODO|FIXME|XXX)\b")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET_RE = re.compile(r"^\s*-\s+(.*)$")
OPEN_HEADING_RE = re.compile(r"still\s+open|open\s+question|deferred", re.I)

MAX_FILE_BYTES = 1_500_000   # generated/lock blobs are not worth reading
TEXT_MAX = 200               # characters kept from a marker line
TITLE_MAX = 160              # task titles stay one line in the CLI
MAX_TODOS = 8
MAX_AUDIT_ITEMS = 10
MAX_DIRTY_IN_SOURCE = 10


# ---------- file helpers ----------
def _read_text(path):
    """File text, or None when missing / binary / oversized / unreadable."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    return raw.decode("utf-8", "replace")


def _git(repo_root, *args):
    """Run git in repo_root. Returns stdout, or None when git fails."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            errors="replace",
        )
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


# ---------- TODOs ----------
def _candidate_files(root):
    """Repo-relative-ish walk in a stable order, skipping SKIP_DIRS."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(TEXT_EXTS):
                yield os.path.join(dirpath, name)


def scan_todos(repo_root=".", max_files=400):
    """Marker hits, sorted by path then line. `path` is repo-relative, posix.

    `max_files` caps how many candidate files are opened (not how many hits
    are returned), so a scan is bounded on a large tree.
    """
    root = os.path.abspath(str(repo_root))
    limit = max(0, int(max_files))
    hits = []
    for examined, full in enumerate(_candidate_files(root)):
        if examined >= limit:
            break
        text = _read_text(full)
        if not text:
            continue
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        for lineno, line in enumerate(text.splitlines(), 1):
            if TODO_RE.search(line):
                hits.append({"path": rel, "line": lineno,
                             "text": line.strip()[:TEXT_MAX]})
    hits.sort(key=lambda h: (h["path"], h["line"]))
    return hits


# ---------- git state ----------
def _parse_porcelain(text):
    """`git status --porcelain` -> list of paths (rename target, unquoted)."""
    paths = []
    for line in text.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:            # rename/copy: keep the new name
            path = path.split(" -> ", 1)[1]
        path = path.strip()
        if len(path) >= 2 and path[0] == path[-1] == '"':
            path = path[1:-1]
        if path:
            paths.append(path)
    return paths


def _parse_commit_line(line):
    parts = line.split("\x1f")
    if len(parts) < 3:
        return {}
    subject = "\x1f".join(parts[2:]).strip()
    return {"hash": parts[0].strip(), "date": parts[1].strip(),
            "subject": subject}


def git_state(repo_root="."):
    """Branch, dirty paths and the last commit. Empty values outside a repo."""
    branch = (_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    porcelain = _git(repo_root, "status", "--porcelain")
    last = _git(repo_root, "log", "-1", "--date=short",
                "--pretty=format:%h\x1f%ad\x1f%s")
    return {
        "branch": branch,
        "dirty": _parse_porcelain(porcelain) if porcelain else [],
        "last_commit": _parse_commit_line(last) if last else {},
    }


# ---------- audits ----------
def _audit_items(repo_root, cap=MAX_AUDIT_ITEMS):
    """'- ' bullets under headings matching OPEN_HEADING_RE, in file order."""
    items = []
    audit_dir = os.path.join(str(repo_root), "audits")
    if not os.path.isdir(audit_dir):
        return items
    for name in sorted(os.listdir(audit_dir)):
        if not name.lower().endswith(".md"):
            continue
        full = os.path.join(audit_dir, name)
        if not os.path.isfile(full):
            continue
        text = _read_text(full)
        if not text:
            continue
        in_section = False
        section_level = 0
        for lineno, raw in enumerate(text.splitlines(), 1):
            heading = HEADING_RE.match(raw.strip())
            if heading:
                level = len(heading.group(1))
                if OPEN_HEADING_RE.search(heading.group(2)):
                    in_section, section_level = True, level
                elif in_section and level <= section_level:
                    in_section = False       # next sibling/parent heading
                continue
            if not in_section:
                continue
            bullet = BULLET_RE.match(raw)
            if not bullet:
                continue
            title = bullet.group(1).strip()
            if not title:
                continue
            items.append({"title": title[:TITLE_MAX], "area": "audit",
                          "source": f"audits/{name}:{lineno}"})
            if len(items) >= cap:
                return items
    return items


# ---------- suggestions ----------
def suggest(store, repo_root=".", add=False):
    """Proposed tasks as {title, area, source}; add=True also creates them."""
    root = str(repo_root)
    items = _audit_items(root)

    state = git_state(root)
    dirty = state.get("dirty") or []
    if dirty:
        listed = ", ".join(dirty[:MAX_DIRTY_IN_SOURCE])
        if len(dirty) > MAX_DIRTY_IN_SOURCE:
            listed += f" (+{len(dirty) - MAX_DIRTY_IN_SOURCE} more)"
        items.append({"title": "Review uncommitted changes", "area": "git",
                      "source": "git status: " + listed})

    for hit in scan_todos(root)[:MAX_TODOS]:
        items.append({
            "title": f"{hit['path']}:{hit['line']} — {hit['text']}"[:TITLE_MAX],
            "area": "code",
            "source": f"{hit['path']}:{hit['line']}",
        })

    unique, seen = [], set()
    for item in items:
        key = (item["title"], item["area"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    if add:
        for item in unique:
            store.add_task(item["title"], notes=item["source"],
                           area=item["area"], complexity="S", est_minutes=30)
        store.add_event("scan_suggested", None,
                        added=len(unique),
                        branch=state.get("branch", ""),
                        dirty=len(dirty),
                        titles=[i["title"][:80] for i in unique][:20])
    return unique
