"""Tests for planner/project_scan.py — marker scan, git state, suggestions.

Run from the repo root:
    python3 -m unittest planner.tests.test_scan -v
    python3 -m unittest discover -s planner/tests -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from planner.project_scan import git_state, scan_todos, suggest  # noqa: E402
from planner.store import Store                                  # noqa: E402


def git(cwd, *args, **env_extra):
    """Run git and raise with stderr on failure, so a broken fixture is loud."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update({k: v for k, v in env_extra.items() if v is not None})
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, errors="replace")
    if proc.returncode != 0:
        raise AssertionError("git %s failed: %s"
                             % (" ".join(args), proc.stderr.strip()))
    return proc.stdout


class ScanTest(unittest.TestCase):
    """A small synthetic repo: 4 markers, 2 open audit items, 1 dirty file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="planner-scan-")
        self.repo = os.path.join(self.tmp, "repo")
        self._write("notes.md", "Keep notes\n\nXXX revisit the heading scheme\n")
        self._write("src/enrich.py",
                    "def enrich():\n"
                    "    return []  # TODO: wire in the loader\n"
                    "\n"
                    "LIMIT = 3  # FIXME: magic number\n")
        self._write("src/pipeline.sh",
                    "#!/bin/sh\n"
                    "# TODO: pass --since through\n")
        self._write("data/skip.py", "# TODO: data dir must never be scanned\n")
        self._write("audits/old.md",
                    "# Audit\n"
                    "\n"
                    "## Still open\n"
                    "\n"
                    "- Confirm the root-synset counting unit\n"
                    "- Ask the PI about the WSD split\n"
                    "\n"
                    "## Closed\n"
                    "\n"
                    "- Already done thing\n")
        self._write("external/vendor.py", "# TODO: third-party, never scanned\n")
        self._write(".zwork/scratch.py", "# TODO: agent scratch, never scanned\n")
        self._write("node_modules/pkg/index.md", "TODO: dependency noise\n")
        self._write("src/node_modules/dep.md", "TODO: nested dependency noise\n")
        self.db = os.path.join(self.tmp, "db", "planner.db")
        self.store = None

    def tearDown(self):
        if self.store is not None:
            self.store.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- helpers ----------
    def _write(self, rel, text):
        path = os.path.join(self.repo, *rel.split("/"))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _init_repo(self):
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Planner Test")
        git(self.repo, "config", "user.email", "planner@example.invalid")
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "Initial planner notes",
            GIT_AUTHOR_DATE="2026-02-01T09:00:00",
            GIT_COMMITTER_DATE="2026-02-01T09:00:00")

    def _open_store(self):
        self.store = Store(self.db)
        return self.store

    def _items(self, **kwargs):
        kwargs.setdefault("repo_root", self.repo)
        return suggest(self._open_store(), **kwargs)

    # ---------- scan_todos ----------
    def test_scan_finds_markers_sorted_and_skips_busy_dirs(self):
        hits = scan_todos(self.repo)
        self.assertEqual(["notes.md", "src/enrich.py", "src/enrich.py",
                          "src/pipeline.sh"], [h["path"] for h in hits])
        self.assertEqual([3, 2, 4, 2], [h["line"] for h in hits])
        self.assertIn("TODO: wire in the loader", hits[1]["text"])
        self.assertIn("FIXME: magic number", hits[2]["text"])
        for hit in hits:
            self.assertEqual({"path", "line", "text"}, set(hit))
            self.assertEqual(hit["text"], hit["text"].strip())
        for skipped in ("data/", "audits/", "external/", ".zwork/", "node_modules/"):
            self.assertFalse([h for h in hits if skipped in h["path"]], skipped)

    def test_scan_ignores_lowercase_and_partial_markers(self):
        self._write("src/noise.md", "todo lower\nTODOS list\nXXXX placeholder\n")
        self.assertEqual(4, len(scan_todos(self.repo)))

    def test_scan_max_files_caps_work(self):
        self.assertEqual(1, len(scan_todos(self.repo, max_files=1)))
        self.assertEqual([], scan_todos(self.repo, max_files=0))
        self.assertEqual([], scan_todos(os.path.join(self.tmp, "missing")))

    # ---------- git_state ----------
    def test_git_state_reports_branch_dirty_and_last_commit(self):
        self._init_repo()
        state = git_state(self.repo)
        self.assertTrue(state["branch"])
        self.assertEqual([], state["dirty"])
        self.assertEqual("Initial planner notes", state["last_commit"]["subject"])
        self.assertEqual("2026-02-01", state["last_commit"]["date"])
        self.assertRegex(state["last_commit"]["hash"], r"^[0-9a-f]{7,}$")

        self._write("new_note.md", "draft\n")
        self._write("notes.md", "Keep notes\n\nXXX revisit the heading scheme\nTODO: later\n")
        state = git_state(self.repo)
        self.assertIn("new_note.md", state["dirty"])
        self.assertIn("notes.md", state["dirty"])

    def test_git_state_reports_renamed_file_target(self):
        self._init_repo()
        git(self.repo, "mv", "notes.md", "journal.md")
        state = git_state(self.repo)
        self.assertIn("journal.md", state["dirty"])
        self.assertNotIn("notes.md", state["dirty"])

    def test_git_state_outside_a_repo_is_empty(self):
        state = git_state(self.tmp)
        self.assertEqual({"branch": "", "dirty": [], "last_commit": {}}, state)

    # ---------- suggest ----------
    def test_suggest_reads_audits_dirty_files_and_todos(self):
        self._init_repo()
        self._write("new_note.md", "draft\n")
        store = self._open_store()

        items = suggest(store, repo_root=self.repo)
        self.assertEqual(0, len(store.list_tasks()))   # add=False writes nothing
        areas = [item["area"] for item in items]
        self.assertEqual(2, areas.count("audit"))
        self.assertEqual(1, areas.count("git"))
        self.assertEqual(4, areas.count("code"))

        audit = [item for item in items if item["area"] == "audit"]
        self.assertEqual("Confirm the root-synset counting unit", audit[0]["title"])
        self.assertEqual("audits/old.md:5", audit[0]["source"])
        self.assertEqual("Ask the PI about the WSD split", audit[1]["title"])
        self.assertEqual("audits/old.md:6", audit[1]["source"])

        dirty_item = [item for item in items if item["area"] == "git"][0]
        self.assertEqual("Review uncommitted changes", dirty_item["title"])
        self.assertIn("new_note.md", dirty_item["source"])

        code = [item for item in items if item["area"] == "code"]
        self.assertEqual("notes.md:3 — XXX revisit the heading scheme",
                         code[0]["title"])
        self.assertEqual("notes.md:3", code[0]["source"])
        self.assertEqual("src/enrich.py:2 — return []  # TODO: wire in the loader",
                         code[1]["title"])
        self.assertFalse([item for item in items if "skip.py" in item["title"]])

    def test_suggest_add_creates_small_tasks_and_records_event(self):
        self._init_repo()
        self._write("new_note.md", "draft\n")
        store = self._open_store()

        items = suggest(store, repo_root=self.repo, add=True)
        tasks = store.list_tasks()
        self.assertEqual(len(items), len(tasks))
        self.assertEqual({i["title"] for i in items}, {t["title"] for t in tasks})
        by_title = {t["title"]: t for t in tasks}
        for item in items:
            task = by_title[item["title"]]
            self.assertEqual(item["source"], task["notes"])
            self.assertEqual(item["area"], task["area"])
            self.assertEqual("S", task["complexity"])
            self.assertEqual(30, task["est_minutes"])
            self.assertEqual("todo", task["status"])

        events = [e for e in store.recent_events() if e["kind"] == "scan_suggested"]
        self.assertEqual(1, len(events))
        self.assertEqual(len(items), events[0]["payload"]["added"])
        self.assertEqual(1, events[0]["payload"]["dirty"])

    def test_suggest_caps_todos_at_eight(self):
        only = os.path.join(self.tmp, "todocap")
        os.makedirs(os.path.join(only, "src"))
        with open(os.path.join(only, "src", "many.py"), "w", encoding="utf-8") as fh:
            fh.write("\n".join("x%d = 1  # TODO: item %d" % (i, i)
                               for i in range(10)) + "\n")

        code = [i for i in self._items(repo_root=only) if i["area"] == "code"]
        self.assertEqual(8, len(code))
        self.assertTrue(all(i["title"].startswith("src/many.py:") for i in code))
        self.assertTrue(all(i["source"].startswith("src/many.py:") for i in code))

    def test_suggest_caps_audit_items_at_ten(self):
        other = os.path.join(self.tmp, "auditcap")
        os.makedirs(os.path.join(other, "audits"))
        bullets = "\n".join("- open item %02d" % i for i in range(12))
        with open(os.path.join(other, "audits", "q.md"), "w", encoding="utf-8") as fh:
            fh.write("# Audit\n\n## Deferred\n\n" + bullets + "\n")

        items = self._items(repo_root=other)
        self.assertEqual(10, len(items))
        self.assertTrue(all(i["area"] == "audit" for i in items))
        self.assertEqual("open item 00", items[0]["title"])

    def test_suggest_on_empty_dir_is_empty(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        self.assertEqual([], self._items(repo_root=empty))


if __name__ == "__main__":
    unittest.main()
