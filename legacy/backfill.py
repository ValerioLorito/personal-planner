"""
backfill.py — turn git history into planner history rows.

Reads `git log` (read-only) and writes rows into the `history` table so the
digest can show "what actually moved in the repo" next to "what is planned".

Public API
    git_commits(repo_root=".", since="2026-04-01")
        -> [{"hash": "2d050c9", "date": "2026-10-26", "subject": "..."}, ...]
    ingest(store, repo_root=".", since="2026-04-01") -> int
        inserts kind='commit' rows, returns the number actually inserted
    digest(store, date_from=None, date_to=None) -> markdown str

Design notes
- Stdlib only; the `git` binary is called through subprocess with a fixed
  argv (no shell), so commit subjects can contain anything.
- `ingest` is idempotent on `history.source == "git:<hash>"`: re-running it
  after a pull only inserts the new commits.
- Merge commits are kept; they are part of the record.
- Hashes come from `%h` (git's abbreviated form). If the repository grows
  enough for git to lengthen the abbreviation, a later run stores a new
  `git:<hash>` string for the same commit and it would be inserted again.
  For a permanent key, clear the commit rows and re-ingest, or switch
  LOG_FORMAT to `%H`.
"""
import subprocess

FIELD_SEP = "\x1f"
LOG_FORMAT = "%h%x1f%ad%x1f%s"
DEFAULT_SINCE = "2026-04-01"
MAX_TITLE = 120          # history.title is a one-liner; keep rows readable
MAX_SUBJECTS = 8         # "8 most recent subjects" per month in the digest
HISTORY_SCAN_LIMIT = 200000   # safety cap when reading history back


# ---------- git ----------
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


def _parse_commit_line(line):
    """'hash\\x1fdate\\x1fsubject' -> dict, or None when the line is malformed."""
    parts = line.split(FIELD_SEP)
    if len(parts) < 3:
        return None
    # A subject containing the separator would be split; rejoin the tail.
    subject = FIELD_SEP.join(parts[2:]).strip()
    commit = {"hash": parts[0].strip(), "date": parts[1].strip(),
              "subject": subject}
    if not commit["hash"] or not commit["date"]:
        return None
    return commit


def git_commits(repo_root=".", since=DEFAULT_SINCE):
    """Commits since `since` (git's own --since parsing), newest first."""
    args = ["log", "--date=short", "--pretty=format:" + LOG_FORMAT]
    if since:
        args.append("--since=" + str(since))
    out = _git(repo_root, *args)
    if out is None:
        return []
    commits = []
    for line in out.splitlines():
        commit = _parse_commit_line(line)
        if commit:
            commits.append(commit)
    return commits


# ---------- store ----------
def _known_git_sources(store):
    """history.source values already used for commits."""
    rows = store.list_history(limit=HISTORY_SCAN_LIMIT)
    return {str(r.get("source") or "") for r in rows
            if str(r.get("source") or "").startswith("git:")}


def ingest(store, repo_root=".", since=DEFAULT_SINCE):
    """Insert missing commits as history rows; return how many were added.

    Rows are written oldest first so history ids grow with time. A commit is
    identified by `git:<hash>`; anything already stored is skipped, which is
    what makes repeated runs safe.
    """
    commits = git_commits(repo_root, since)
    if not commits:
        return 0
    known = _known_git_sources(store)
    # `git log` prints newest first; insert oldest first so ids (and therefore
    # "first/last commit of the month") follow real time, including within a
    # single day.
    added = 0
    for commit in reversed(commits):
        source = "git:" + commit["hash"]
        if source in known:
            continue
        store.add_history(
            date=commit["date"],
            kind="commit",
            title=commit["subject"][:MAX_TITLE],
            detail=commit["hash"],
            source=source,
        )
        known.add(source)
        added += 1
    return added


# ---------- digest ----------
def _span(date_from, date_to):
    if not date_from and not date_to:
        return ""
    return f" — {date_from or 'start'} → {date_to or 'today'}"


def _month_of(row):
    date = str(row.get("date") or "")
    return date[:7] if len(date) >= 7 else "unknown"


def _kind_counts(rows):
    counts = {}
    for row in rows:
        kind = str(row.get("kind") or "?")
        counts[kind] = counts.get(kind, 0) + 1
    return ", ".join(f"{k} {v}" for k, v in
                     sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _commit_line(row):
    return f"{row.get('date', '')} `{row.get('detail') or ''}` {row.get('title', '')}"


def digest(store, date_from=None, date_to=None):
    """Markdown digest of history rows grouped by YYYY-MM, newest month first.

    Per month: commit count, first and last commit, the most recent subjects,
    plus any non-commit rows (audit / run / note / milestone) when present.
    """
    rows = store.list_history(date_from=date_from, date_to=date_to,
                              limit=HISTORY_SCAN_LIMIT)
    if not rows:
        return ("# History digest" + _span(date_from, date_to) + "\n\n"
                "No history rows yet. Run `ingest()` to backfill commits.\n")

    by_month = {}
    for row in rows:
        by_month.setdefault(_month_of(row), []).append(row)

    commits = [r for r in rows if r.get("kind") == "commit"]
    others = [r for r in rows if r.get("kind") != "commit"]

    lines = ["# History digest" + _span(date_from, date_to), ""]
    lines.append(f"{len(commits)} commit(s) and {len(others)} other history "
                 f"row(s) across {len(by_month)} month(s).")
    lines.append("")

    for month in sorted(by_month, reverse=True):
        month_rows = by_month[month]
        month_commits = [r for r in month_rows if r.get("kind") == "commit"]
        month_others = [r for r in month_rows if r.get("kind") != "commit"]

        header = f"## {month} — {len(month_commits)} commit(s)"
        if month_others:
            header += f", {len(month_others)} other"
        lines.append(header)
        lines.append("")

        if month_commits:
            # list_history is ordered by (date, id) ascending.
            lines.append("- first: " + _commit_line(month_commits[0]))
            lines.append("- last: " + _commit_line(month_commits[-1]))
            recent = list(reversed(month_commits[-MAX_SUBJECTS:]))
            lines.append(f"- latest {len(recent)} subject(s), newest first:")
            lines.extend("  - " + _commit_line(r) for r in recent)
        else:
            lines.append("- no commits this month")

        if month_others:
            lines.append("- other kinds: " + _kind_counts(month_others))
            for row in month_others[-5:]:
                lines.append(f"  - {row.get('date', '')} "
                             f"[{row.get('kind', '?')}] {row.get('title', '')}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
