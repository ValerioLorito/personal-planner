"""Tests for planner/backfill.py — git log parsing, idempotent ingest, digest.

Run from the repo root (Python 3.9 does not treat planner/ as a package, so
`python3 -m unittest` with no arguments finds nothing):
    python3 -m unittest planner.tests.test_backfill -v
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

from planner.backfill import digest, git_commits, ingest  # noqa: E402
from planner.store import Store                           # noqa: E402


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


class BackfillTest(unittest.TestCase):
    """Two commits in January 2026, then ingest them into a throwaway store."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="planner-backfill-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Planner Test")
        git(self.repo, "config", "user.email", "planner@example.invalid")
        git(self.repo, "config", "commit.gpgsign", "false")
        self._commit("chapter1.md", "# Chapter 1\n",
                     "Add first chapter notes", "2026-01-10T10:00:00")
        self._commit("chapter2.md", "# Chapter 2\n",
                     "Write method section draft", "2026-01-20T10:00:00")
        self.db = os.path.join(self.tmp, "db", "planner.db")
        self.store = None

    def tearDown(self):
        if self.store is not None:
            self.store.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- helpers ----------
    def _commit(self, name, body, message, stamp):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as fh:
            fh.write(body)
        git(self.repo, "add", name)
        git(self.repo, "commit", "-q", "-m", message,
            GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)

    def _open_store(self):
        self.store = Store(self.db)
        return self.store

    # ---------- git_commits ----------
    def test_git_commits_parses_hash_date_subject(self):
        commits = git_commits(self.repo, since="2026-01-01")
        self.assertEqual(2, len(commits))
        self.assertEqual(["Write method section draft", "Add first chapter notes"],
                         [c["subject"] for c in commits])
        self.assertEqual(["2026-01-20", "2026-01-10"],
                         [c["date"] for c in commits])
        for commit in commits:
            self.assertEqual({"hash", "date", "subject"}, set(commit))
            self.assertRegex(commit["hash"], r"^[0-9a-f]{7,}$")

    def test_git_commits_honours_since(self):
        commits = git_commits(self.repo, since="2026-01-15")
        self.assertEqual(1, len(commits))
        self.assertEqual("Write method section draft", commits[0]["subject"])

    def test_git_commits_outside_a_repo_is_empty(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        with open(os.path.join(plain, "note.txt"), "w", encoding="utf-8") as fh:
            fh.write("not a repository\n")
        self.assertEqual([], git_commits(plain, since="2026-01-01"))
        self.assertEqual([], git_commits(os.path.join(self.tmp, "missing"), since="2026-01-01"))

    # ---------- ingest ----------
    def test_ingest_is_idempotent(self):
        store = self._open_store()
        self.assertEqual(2, ingest(store, self.repo, since="2026-01-01"))
        self.assertEqual(0, ingest(store, self.repo, since="2026-01-01"))
        self.assertEqual(0, ingest(store, self.repo, since="2026-01-01"))

        rows = store.list_history()
        self.assertEqual(2, len(rows))
        self.assertEqual(["2026-01-10", "2026-01-20"], [r["date"] for r in rows])
        by_source = {r["source"]: r for r in rows}
        for commit in git_commits(self.repo, since="2026-01-01"):
            row = by_source["git:" + commit["hash"]]
            self.assertEqual("commit", row["kind"])
            self.assertEqual(commit["subject"], row["title"])
            self.assertEqual(commit["hash"], row["detail"])

    def test_ingest_skips_hash_already_in_history(self):
        store = self._open_store()
        first = [c for c in git_commits(self.repo, since="2026-01-01")
                 if c["subject"].startswith("Add first")][0]
        store.add_history(date=first["date"], kind="commit", title="manual row",
                          detail=first["hash"], source="git:" + first["hash"])

        self.assertEqual(1, ingest(store, self.repo, since="2026-01-01"))
        rows = [r for r in store.list_history() if r["kind"] == "commit"]
        self.assertEqual(2, len(rows))
        self.assertEqual(1, len([r for r in rows if r["title"] == "manual row"]))

    def test_ingest_keeps_chronological_order_within_a_day(self):
        repo = os.path.join(self.tmp, "sameday")
        os.makedirs(repo)
        git(repo, "init", "-q")
        git(repo, "config", "user.name", "Planner Test")
        git(repo, "config", "user.email", "planner@example.invalid")
        git(repo, "config", "commit.gpgsign", "false")
        for i, (message, stamp) in enumerate([
                ("Morning: write the intro", "2026-01-05T09:00:00"),
                ("Evening: fix the intro", "2026-01-05T18:00:00")]):
            with open(os.path.join(repo, "f%d.txt" % i), "w", encoding="utf-8") as fh:
                fh.write("x\n")
            git(repo, "add", "f%d.txt" % i)
            git(repo, "commit", "-q", "-m", message,
                GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)

        store = self._open_store()
        self.assertEqual(2, ingest(store, repo, since="2026-01-01"))
        self.assertEqual(["Morning: write the intro", "Evening: fix the intro"],
                         [r["title"] for r in store.list_history()])
        text = digest(store)
        self.assertRegex(text, r"- first: 2026-01-05 `[0-9a-f]+` Morning: write the intro")
        self.assertRegex(text, r"- last: 2026-01-05 `[0-9a-f]+` Evening: fix the intro")

    def test_ingest_without_commits_adds_nothing(self):
        store = self._open_store()
        self.assertEqual(0, ingest(store, self.repo, since="2026-06-01"))
        self.assertEqual([], store.list_history())

    # ---------- digest ----------
    def test_digest_groups_by_month(self):
        store = self._open_store()
        ingest(store, self.repo, since="2026-01-01")
        text = digest(store)

        self.assertTrue(text.startswith("# History digest"))
        self.assertIn("2 commit(s) and 0 other history row(s) across 1 month(s).", text)
        self.assertIn("## 2026-01 — 2 commit(s)", text)
        self.assertRegex(text, r"- first: 2026-01-10 `[0-9a-f]+` Add first chapter notes")
        self.assertRegex(text, r"- last: 2026-01-20 `[0-9a-f]+` Write method section draft")
        self.assertIn("latest 2 subject(s), newest first:", text)
        block = text.split("latest 2 subject(s), newest first:")[1]
        self.assertLess(block.index("Write method section draft"),
                        block.index("Add first chapter notes"))

    def test_digest_respects_date_window(self):
        store = self._open_store()
        ingest(store, self.repo, since="2026-01-01")
        text = digest(store, date_from="2026-01-15")

        self.assertIn("1 commit(s) and 0 other history row(s) across 1 month(s).", text)
        self.assertIn("## 2026-01 — 1 commit(s)", text)
        self.assertIn("Write method section draft", text)
        self.assertNotIn("Add first chapter notes", text)

    def test_digest_includes_other_kinds(self):
        store = self._open_store()
        ingest(store, self.repo, since="2026-01-01")
        store.add_history(date="2026-02-03", kind="audit",
                          title="Schema audit pass", source="audits/schema_audit.md")
        store.add_history(date="2026-02-04", kind="note",
                          title="PI meeting: narrow the scope")
        text = digest(store)

        self.assertIn("## 2026-02 — 0 commit(s), 2 other", text)
        self.assertIn("- no commits this month", text)
        self.assertIn("- other kinds: audit 1, note 1", text)
        self.assertIn("[audit] Schema audit pass", text)
        self.assertLess(text.index("## 2026-02"), text.index("## 2026-01"))

    def test_digest_shows_only_eight_subjects_per_month(self):
        store = self._open_store()
        for i in range(10):
            store.add_history(date="2026-03-%02d" % (i + 1), kind="commit",
                              title="commit %02d" % i, detail="h%02d" % i,
                              source="git:h%02d" % i)
        text = digest(store)

        self.assertIn("## 2026-03 — 10 commit(s)", text)
        block = text.split("latest 8 subject(s), newest first:")[1]
        listed = [ln for ln in block.splitlines() if ln.startswith("  - 2026-03-")]
        self.assertEqual(8, len(listed))
        self.assertIn("commit 09", listed[0])
        self.assertIn("commit 02", listed[-1])

    def test_digest_on_empty_history(self):
        store = self._open_store()
        text = digest(store)
        self.assertIn("No history rows yet", text)
        self.assertIn("No history rows yet", digest(store, date_from="2026-01-01",
                                                    date_to="2026-12-31"))


if __name__ == "__main__":
    unittest.main()
