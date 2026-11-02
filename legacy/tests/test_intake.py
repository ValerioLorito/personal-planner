"""Tests for description-only intake, calibration feedback and the CLI wiring."""
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from unittest import mock

from planner import brain, cli, intake, sources
from planner.store import Store

CFG = {
    "brain": {
        "backend": "heuristic",
        "llm": {"base_url": "", "model": "", "api_key_env": "PLANNER_LLM_KEY",
                "timeout": 5},
    },
    "work_days": [0, 1, 2, 3, 4], "day_start": "09:00", "day_end": "19:00",
    "lunch": ["13:00", "14:00"], "buffer_min": 10, "max_block_min": 90,
    "max_day_min": 300, "deadline_soon_days": 3,
    "complexity_minutes": {"S": 45, "M": 90, "L": 180},
    "complexity_rank": {"S": 1, "M": 3, "L": 5},
}

ANALYSIS_KEYS = {"summary", "why", "area", "complexity", "minutes", "priority",
                 "deep_work", "confidence", "subtasks", "related"}


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _make_repo(root):
    """A miniature commonsense-atlas with the artefacts the globs look for."""
    _write(root, "audits/coref_audit.md",
           "# Coreference audit\n\n## Still open\n\n- Compare itemize on 20 rows\n")
    _write(root, "audits/bridge_audit.md", "# Bridge audit\n\nSome findings.\n")
    _write(root, "src/coreference/coreference_resolve.py",
           '"""Resolve mentions to antecedent spans."""\n\nVALUE = 1\n')
    _write(root, "src/coreference/coreference_itemize.py",
           '"""Itemize resolved mentions."""\n')
    _write(root, "src/utils/coreference/coref_diagnostics.py",
           '"""Coreference diagnostics."""\n')
    _write(root, "src/utils/coreference/audit_coreference.py",
           '"""Audit coreference output."""\n')
    _write(root, "src/utils/atlas/audit_atlas_questions.py",
           '"""Audit atlas questions."""\n')
    _write(root, "data/interim/coref_stage/run1/summary.json", "{}\n")
    _write(root, "data/interim/coref_stage_q27b/run2/summary.json", "{}\n")
    _write(root, "slurm_scripts/llm_battery_chain.sh", "#!/bin/bash\n# battery chain\n")
    _write(root, "slurm_scripts/coref_canary.sh", "#!/bin/bash\n# coref canary\n")
    _write(root, "planner/project_profile.md", "# Project profile\n\nCosts.\n")
    _write(root, "README.md", "# Mini atlas\n\nRead me.\n")


def _snapshot(root):
    """relpath -> (size, mtime_ns): proves a call did not write the repo."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in filenames:
            full = os.path.join(dirpath, name)
            stat = os.stat(full)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out[rel] = (stat.st_size, stat.st_mtime_ns)
    return out


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "repo")
        _make_repo(self.root)
        self.store = Store(os.path.join(self.tmp.name, "planner.db"))
        self.cfg = json.loads(json.dumps(CFG))
        # never let llm tests fall through to the real planner/data/llm_key
        self.cfg["brain"]["llm"]["key_file"] = os.path.join(self.tmp.name,
                                                            "llm_key")

    # ---------- titles / areas / durations ----------
    def test_title_is_short_sentence_case(self):
        self.assertEqual(
            intake.make_title("please create an audit for coreference resolution."),
            "Create an audit for coreference resolution")
        long_title = intake.make_title("x" * 120)
        self.assertLessEqual(len(long_title), intake.TITLE_MAX)
        self.assertEqual(intake.make_title("  fix   the SRL validator. "),
                         "Fix the SRL validator")

    def test_area_inference(self):
        self.assertEqual(intake.infer_area("create an audit for coreference resolution"),
                         "coreference")
        self.assertEqual(intake.infer_area("write the methods section"), "writing")
        self.assertEqual(intake.infer_area("rerun the slurm battery"), "infra")
        self.assertEqual(intake.infer_area("think about the thing"), "general")

    def test_parse_duration_formats(self):
        cases = {"20": 20, "20m": 20, "20 min": 20, "1h30": 90, "1h30m": 90,
                 "1.5h": 90, "90 min": 90, "1:30": 90}
        for text, expected in cases.items():
            self.assertEqual(intake.parse_duration(text), expected, text)
        for bad in ("", "abc", "0", "-5"):
            with self.assertRaises(ValueError, msg=bad):
                intake.parse_duration(bad)

    # ---------- description-only creation ----------
    def test_create_heuristic_derives_everything_and_cites_artefacts(self):
        desc = "create an audit for coreference resolution"
        res = intake.create_from_description(self.store, self.cfg, desc,
                                             repo_root=self.root)
        self.assertEqual(res["status"], "applied")
        self.assertIsNotNone(res["plan"])
        self.assertTrue({"task_id", "status", "analysis", "plan"}
                        <= set(res))
        analysis = res["analysis"]
        self.assertEqual(set(analysis), ANALYSIS_KEYS)
        self.assertEqual(analysis["area"], "coreference")
        self.assertIn(analysis["complexity"], ("S", "M", "L"))
        self.assertGreater(analysis["minutes"], 0)
        self.assertIn(analysis["priority"], (1, 2, 3))
        self.assertGreaterEqual(analysis["confidence"], 0.0)
        self.assertLessEqual(analysis["confidence"], 1.0)
        self.assertTrue(analysis["subtasks"])

        self.assertGreaterEqual(len(analysis["related"]), 1)
        self.assertLessEqual(len(analysis["related"]), 4)
        for item in analysis["related"]:
            self.assertTrue(os.path.exists(os.path.join(self.root, item["path"])),
                            item["path"])
            self.assertTrue(item["note"])
        paths = [item["path"] for item in analysis["related"]]
        self.assertTrue(any(path.startswith("src/coreference/") for path in paths),
                        paths)
        self.assertTrue(any(path.startswith("audits/") for path in paths), paths)
        self.assertTrue(any(path.startswith("data/interim/coref_stage")
                            for path in paths), paths)
        why = " ".join(analysis["why"])
        for path in paths:
            self.assertIn(path, why)

        task = self.store.get_task(res["task_id"])
        self.assertEqual(task["notes"], desc)          # raw sentence kept
        self.assertEqual(task["area"], "coreference")
        self.assertEqual(task["est_source"], "heuristic")
        self.assertEqual(task["est_minutes"], analysis["minutes"])
        self.assertLessEqual(len(task["title"]), intake.TITLE_MAX)
        self.assertEqual(task["title"][0], "C")
        self.assertEqual(json.loads(task["subtasks"]), analysis["subtasks"])
        self.assertIn(paths[0], task["files"])

    def test_audit_description_cites_doc_and_audit_script(self):
        res = intake.create_from_description(
            self.store, self.cfg, "create an audit of the atlas qa",
            repo_root=self.root)
        paths = [item["path"] for item in res["analysis"]["related"]]
        self.assertTrue(any(path.startswith("audits/") for path in paths), paths)
        self.assertTrue(any(path.startswith("src/utils/atlas/audit_")
                            for path in paths), paths)

    def test_llm_failure_falls_back_without_raising(self):
        self.cfg["brain"]["backend"] = "llm"       # no base_url/model configured
        res = intake.create_from_description(
            self.store, self.cfg, "create an audit for coreference resolution",
            repo_root=self.root)
        self.assertEqual(res["status"], "applied")
        self.assertIn("llm backend failed", " ".join(res["analysis"]["why"]))
        self.assertGreater(res["analysis"]["minutes"], 0)
        self.assertEqual(self.store.get_task(res["task_id"])["est_source"],
                         "heuristic")

    def test_llm_success_uses_calibration_in_prompt(self):
        self.store.kv_set("calibration:ratio", 0.5)
        self.store.kv_set("calibration:by_area",
                          json.dumps({"coreference": 0.6}))
        seed = self.store.add_task("Old coref audit", est_minutes=100,
                                   area="coreference")
        self.store.set_feedback(seed, 40, "faster than expected")

        self.cfg["brain"]["backend"] = "llm"
        self.cfg["brain"]["llm"].update(base_url="https://example.invalid/v1",
                                        model="test-model")
        calls = {}

        def fake_post(url, payload, headers, timeout=90):
            calls["url"] = url
            calls["payload"] = payload
            return {"choices": [{"message": {"content": json.dumps({
                "title": "whatever", "area": "analysis", "complexity": "M",
                "estimate_minutes": 135, "priority": 2, "deep_work": True,
                "confidence": 0.8,
                "subtasks": [{"title": "Read data", "minutes": 60}],
                "rationale": "model answer"})}}]}

        original = brain._http_post_json
        brain._http_post_json = fake_post
        try:
            res = intake.create_from_description(
                self.store, self.cfg,
                "create an audit for coreference resolution",
                repo_root=self.root)
        finally:
            brain._http_post_json = original

        self.assertEqual(res["status"], "applied")
        self.assertEqual(res["plan"]["estimate_minutes"], 135)
        task = self.store.get_task(res["task_id"])
        self.assertEqual(task["est_source"], "llm")
        self.assertEqual(task["est_minutes"], 135)
        self.assertEqual(task["area"], "coreference")   # repo inference wins
        prompt = json.dumps(calls["payload"], ensure_ascii=False)
        self.assertIn("by area: coreference x0.60", prompt)
        self.assertIn("recent feedback", prompt)
        self.assertIn("est 100 -> actual 40 min", prompt)
        self.assertIn("instant API estimate via test-model",
                      " ".join(res["analysis"]["why"]))

    def test_repo_is_not_written(self):
        before = _snapshot(self.root)
        intake.create_from_description(
            self.store, self.cfg, "create an audit for coreference resolution",
            repo_root=self.root)
        self.assertEqual(before, _snapshot(self.root))
        with self.assertRaises(RuntimeError):
            intake._open_read(os.path.join(self.root, "README.md"), "a")

    def test_empty_description_and_bad_deadline_raise(self):
        with self.assertRaises(ValueError):
            intake.create_from_description(self.store, self.cfg, "   ",
                                           repo_root=self.root)
        with self.assertRaises(ValueError):
            intake.create_from_description(self.store, self.cfg, "audit coref",
                                           repo_root=self.root,
                                           deadline="next tuesday")

    # ---------- categories + grounded material (phase 2) ----------
    def _make_uni(self, name="uni"):
        """A miniature university folder: one subject with a real pptx."""
        root = os.path.join(self.tmp.name, name)
        subject = os.path.join(root, "NLP Semantics")
        os.makedirs(os.path.join(subject, "readings"), exist_ok=True)
        slides = ("<?xml version='1.0'?><p:sld "
                  "xmlns:p='http://schemas.openxmlformats.org/presentationml/2006/main'>"
                  "<p:cSld><p:spTree><p:sp><p:txBody>"
                  "<a:p xmlns:a='http://schemas.openxmlformats.org/drawingml/2006/main'>"
                  "<a:r><a:t>%s</a:t></a:r></a:p>"
                  "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>")
        with zipfile.ZipFile(os.path.join(subject, "lecture.pptx"), "w") as zf:
            zf.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
            zf.writestr("ppt/slides/slide1.xml", slides % "Week 1: intro")
        with open(os.path.join(subject, "readings", "essay.pdf"), "wb") as fh:
            fh.write(b"not-a-real-pdf")      # exists, but extraction will fail
        with open(os.path.join(subject, "planner-notes.md"), "w") as fh:
            fh.write("Prof wants typicality effects cited.")
        return root

    def test_category_wins_as_area(self):
        uni = self._make_uni()
        res = intake.create_from_description(
            self.store, self.cfg, "prepare the weekly reading response",
            repo_root=self.root, category="NLP Semantics")
        self.assertEqual(res["analysis"]["area"], "NLP Semantics")
        self.assertEqual(self.store.get_task(res["task_id"])["area"],
                         "NLP Semantics")
        self.assertEqual(res["analysis"]["why"][0],
                         "area 'NLP Semantics' (no strong keyword; "
                         "general bucket)")
        # course task: no thesis-repo inspection noise
        self.assertEqual([r for r in res["analysis"]["related"]
                          if not r.get("why")], [])

    def test_category_thesis_is_explicit(self):
        res = intake.create_from_description(
            self.store, self.cfg, "polish the intro section",
            repo_root=self.root, category="Thesis")
        self.assertEqual(res["analysis"]["area"], "Thesis")

    def test_llm_grounds_in_material_two_calls(self):
        uni = self._make_uni()
        self.cfg["university_root"] = uni
        self.cfg["brain"]["backend"] = "llm"
        self.cfg["brain"]["llm"].update(base_url="https://example.invalid/v1",
                                        model="test-model")
        cache = os.path.join(self.tmp.name, "cache.json")
        calls = []

        answer_1 = {"title": "whatever", "area": "NLP Semantics",
                    "complexity": "M", "estimate_minutes": 120, "priority": 2,
                    "confidence": 0.5, "subtasks": [], "rationale": "listing",
                    "significant_files": [
                        {"path": "NLP Semantics/lecture.pptx",
                         "why": "the relevant week's slides"}]}
        answer_2 = dict(answer_1, estimate_minutes=150,
                        rationale="slides show a long reading",
                        significant_files=[])
        answers = [answer_1, answer_2]

        def fake_post(url, payload, headers, timeout=90):
            calls.append(payload)
            ans = answers[min(len(calls) - 1, len(answers) - 1)]
            return {"choices": [{"message": {"content": json.dumps(ans)}}]}

        original_post = brain._http_post_json
        brain._http_post_json = fake_post
        original_cache = sources.CACHE_PATH
        sources.CACHE_PATH = cache
        try:
            res = intake.create_from_description(
                self.store, self.cfg, "prepare the weekly reading response",
                repo_root=self.root, category="NLP Semantics")
        finally:
            brain._http_post_json = original_post
            sources.CACHE_PATH = original_cache

        # call A carries the folder material; call B the picked file's text
        self.assertEqual(len(calls), 2, [json.dumps(c)[:200] for c in calls])
        user_a = calls[0]["messages"][1]["content"]
        user_b = calls[1]["messages"][1]["content"]
        self.assertIn('MATERIAL — subject "NLP Semantics"', user_a)
        self.assertIn("NLP Semantics/lecture.pptx", user_a)
        self.assertIn("Prof wants typicality effects cited.", user_a)
        self.assertIn("MATERIAL EXCERPTS", user_b)
        self.assertIn("Week 1: intro", user_b)     # real pptx text, extracted
        # the refined answer wins and lands on the task
        self.assertEqual(res["plan"]["estimate_minutes"], 150)
        task = self.store.get_task(res["task_id"])
        self.assertEqual(task["est_source"], "llm")
        self.assertEqual(task["est_minutes"], 150)
        self.assertEqual(task["files"], "NLP Semantics/lecture.pptx")
        why = " ".join(res["analysis"]["why"])
        self.assertIn("grounded in 1 file(s): NLP Semantics/lecture.pptx — "
                      "the relevant week's slides", why)
        self.assertIn("read 1 file(s) in full", why)
        self.assertIn("instant API estimate via test-model", why)

    def test_picked_files_outside_the_root_are_ignored(self):
        uni = self._make_uni()
        self.cfg["university_root"] = uni
        self.cfg["brain"]["backend"] = "llm"
        self.cfg["brain"]["llm"].update(base_url="https://example.invalid/v1",
                                        model="test-model")
        calls = []

        def fake_post(url, payload, headers, timeout=90):
            calls.append(payload)
            return {"choices": [{"message": {"content": json.dumps({
                "title": "whatever", "area": "NLP Semantics",
                "complexity": "M", "estimate_minutes": 100, "priority": 2,
                "confidence": 0.5, "subtasks": [], "rationale": "r",
                "significant_files": [
                    {"path": "../../secrets/grades.txt", "why": "sneaky"},
                    {"path": "NLP Sementials/ghost.pptx", "why": "typo"}]})}}]}

        original = brain._http_post_json
        brain._http_post_json = fake_post
        try:
            res = intake.create_from_description(
                self.store, self.cfg, "prepare the weekly reading response",
                repo_root=self.root, category="NLP Semantics")
        finally:
            brain._http_post_json = original

        self.assertEqual(len(calls), 1)          # nothing to read -> no call B
        task = self.store.get_task(res["task_id"])
        self.assertEqual(task["files"], "")
        self.assertNotIn("grounded in", " ".join(res["analysis"]["why"]))

    def test_unreadable_picks_are_reported_not_fatal(self):
        uni = self._make_uni()
        self.cfg["university_root"] = uni
        self.cfg["brain"]["backend"] = "llm"
        self.cfg["brain"]["llm"].update(base_url="https://example.invalid/v1",
                                        model="test-model")
        cache = os.path.join(self.tmp.name, "cache.json")
        calls = []

        def fake_post(url, payload, headers, timeout=90):
            calls.append(payload)
            answer = {"title": "whatever", "area": "NLP Semantics",
                      "complexity": "M", "estimate_minutes": 100,
                      "priority": 2, "confidence": 0.5, "subtasks": [],
                      "rationale": "r", "significant_files": [
                          {"path": "NLP Semantics/readings/essay.pdf",
                           "why": "named in the prompt"}]}
            return {"choices": [{"message": {"content": json.dumps(answer)}}]}

        def fake_extract(paths, budget=sources.DEFAULT_BUDGET):
            # the file exists but has no readable text (scanned/garbled)
            return [{"path": p, "kind": "pdf", "pages": 9, "text": "",
                     "error": "no text layer (likely a scan)", "cached": False,
                     "chars": 0, "truncated": False} for p in paths]

        original_post = brain._http_post_json
        brain._http_post_json = fake_post
        original_cache = sources.CACHE_PATH
        sources.CACHE_PATH = cache
        original_extract = sources.extract_text
        sources.extract_text = fake_extract
        try:
            res = intake.create_from_description(
                self.store, self.cfg, "prepare the weekly reading response",
                repo_root=self.root, category="NLP Semantics")
        finally:
            brain._http_post_json = original_post
            sources.CACHE_PATH = original_cache
            sources.extract_text = original_extract

        self.assertEqual(len(calls), 1)          # extraction gave nothing
        why = " ".join(res["analysis"]["why"])
        self.assertIn("none of the picked files could be read", why)
        self.assertEqual(res["plan"]["estimate_minutes"], 100)  # call A stands


    def test_deadline_sets_priority(self):
        res = intake.create_from_description(
            self.store, self.cfg, "create an audit for coreference resolution",
            repo_root=self.root, deadline="2026-10-28", now="2026-10-27")
        self.assertEqual(res["analysis"]["priority"], 1)
        self.assertEqual(self.store.get_task(res["task_id"])["deadline"],
                         "2026-10-28")

    def test_no_keyword_still_cites_fallback_artefacts(self):
        res = intake.create_from_description(self.store, self.cfg,
                                             "think about the thing",
                                             repo_root=self.root)
        self.assertEqual(res["analysis"]["area"], "general")
        self.assertGreaterEqual(len(res["analysis"]["related"]), 1)
        self.assertTrue(any(item["path"] == "README.md"
                            for item in res["analysis"]["related"]))

    # ---------- feedback + calibration ----------
    def test_apply_feedback_clamps_and_reports_before_after(self):
        tid = self.store.add_task("Create an audit for coreference resolution",
                                  est_minutes=120, area="coreference")
        res = intake.apply_feedback(self.store, self.cfg, tid, "20m",
                                    note="the script already existed")
        task = res["task"]
        self.assertEqual(task["actual_minutes"], 20)
        self.assertEqual(task["feedback_note"], "the script already existed")
        self.assertTrue(task["feedback_at"])
        # One fast task is evidence, not a new law: the ratio is blended with a
        # prior of 1.0 weighted 3:1, so 20/120 = 0.167 becomes ~0.79, not 0.4.
        ratio = float(self.store.kv_get("calibration:ratio"))
        self.assertAlmostEqual(ratio, (3 * 1.0 + (20 / 120)) / 4, places=2)
        self.assertAlmostEqual(ratio, 0.792, places=2)
        self.assertEqual(res["before"]["estimate"], 120)
        self.assertEqual(res["before"]["expected_next"], 120)
        self.assertLess(res["after"]["expected_next"], res["before"]["expected_next"])
        self.assertGreater(res["after"]["expected_next"], 60)   # not a cliff
        self.assertLess(res["after"]["change_pct"], 0)
        self.assertEqual(res["calibration"]["ratio"], ratio)
        self.assertEqual(res["calibration"]["samples"], 1)
        self.assertEqual(res["calibration"]["recent"][0]["task_id"], tid)
        self.assertEqual(res["calibration"]["recent"][0]["actual"], 20)
        kinds = [event["kind"] for event in self.store.recent_events(10)]
        self.assertIn("task_feedback", kinds)
        self.assertIn("calibration_updated", kinds)

    def test_feedback_moves_the_next_similar_estimate(self):
        first = intake.create_from_description(
            self.store, self.cfg, "create an audit for coreference resolution",
            repo_root=self.root)
        before = first["analysis"]["minutes"]
        intake.apply_feedback(self.store, self.cfg, first["task_id"], 20,
                              note="much smaller than feared")
        second = intake.create_from_description(
            self.store, self.cfg, "create an audit for coreference resolution",
            repo_root=self.root)
        after = second["analysis"]["minutes"]
        self.assertLess(after, before)
        self.assertEqual(before, 120)
        # one observation pulls the estimate down by about a fifth, not by 60%
        self.assertGreaterEqual(after, 85)
        self.assertLessEqual(after, 100)

    def test_by_area_ratio_needs_two_samples_and_is_used(self):
        for title, estimate, actual in (("coref one", 100, 50),
                                        ("coref two", 100, 60)):
            tid = self.store.add_task(title, est_minutes=estimate,
                                      area="coreference")
            intake.apply_feedback(self.store, self.cfg, tid, actual)
        writing = self.store.add_task("write one", est_minutes=50,
                                     area="writing")
        intake.apply_feedback(self.store, self.cfg, writing, 100)

        summary = intake.calibration_summary(self.store)
        self.assertEqual(summary["samples"], 3)
        # median of 0.5, 0.6, 2.0 is 0.6; blended 3:1 with the 1.0 prior -> 0.8
        self.assertAlmostEqual(summary["ratio"], 0.8, places=2)
        # area median 0.55 over 2 samples, blended 3:1 with the prior -> 0.82
        self.assertAlmostEqual(summary["by_area"]["coreference"], 0.82, places=2)
        stored = json.loads(self.store.kv_get("calibration:by_area"))
        self.assertEqual(stored, {"coreference": 0.82})

        res = intake.create_from_description(
            self.store, self.cfg, "create an audit for coreference resolution",
            repo_root=self.root)
        # base 120 for an audit, trimmed by the blended ratios (0.8 overall,
        # 0.82 for coreference) -> mid-90s, never a cliff
        self.assertGreaterEqual(res["analysis"]["minutes"], 70)
        self.assertLessEqual(res["analysis"]["minutes"], 105)
        self.assertIn("x0.82",
                      self.store.get_task(res["task_id"])["est_rationale"])

    def test_calibration_summary_empty(self):
        summary = intake.calibration_summary(self.store)
        self.assertEqual(summary, {"ratio": 1.0, "samples": 0,
                                   "by_area": {}, "recent": []})

    def test_feedback_rejects_unknown_task(self):
        with self.assertRaises(ValueError):
            intake.apply_feedback(self.store, self.cfg, 999, 20)

    # ---------- migration / suggestions / CLI ----------
    def test_old_database_gets_feedback_columns(self):
        path = os.path.join(self.tmp.name, "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE tasks ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " title TEXT NOT NULL, notes TEXT DEFAULT '', area TEXT DEFAULT '',"
            " status TEXT DEFAULT 'todo', priority INTEGER DEFAULT 2,"
            " complexity TEXT DEFAULT 'M', est_minutes INTEGER DEFAULT 60,"
            " deadline TEXT, files TEXT DEFAULT '', created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL);")
        conn.commit()
        conn.close()
        store = Store(path)
        columns = {row["name"] for row in
                   store.conn.execute("PRAGMA table_info(tasks)")}
        for column in ("actual_minutes", "feedback_note", "feedback_at"):
            self.assertIn(column, columns)
        tid = store.add_task("old task", est_minutes=60, area="analysis")
        store.set_feedback(tid, 20, "note")
        self.assertEqual(store.get_task(tid)["actual_minutes"], 20)

    def test_suggest_next_is_read_only_and_bounded(self):
        before = _snapshot(self.root)
        count = len(self.store.list_tasks())
        suggestions = intake.suggest_next(self.store, self.cfg,
                                          repo_root=self.root)
        self.assertLessEqual(len(suggestions), 3)
        self.assertGreaterEqual(len(suggestions), 1)
        for item in suggestions:
            self.assertTrue({"title", "area", "complexity", "minutes"} <= set(item))
        self.assertEqual(before, _snapshot(self.root))
        self.assertEqual(count, len(self.store.list_tasks()))
        self.assertTrue(any("itemize" in item["title"].lower()
                            for item in suggestions))

    def test_cli_parser_supports_desc_and_feedback(self):
        parser = cli.build_parser()
        described = parser.parse_args(["add", "--desc",
                                       "create an audit for coref"])
        self.assertEqual(described.desc, "create an audit for coref")
        self.assertIsNone(described.title)
        self.assertIs(described.func, cli.cmd_add)
        old = parser.parse_args(["add", "Old style task", "--est", "90"])
        self.assertEqual(old.title, "Old style task")
        self.assertIsNone(old.desc)
        feedback = parser.parse_args(["feedback", "3", "--took", "20m",
                                      "--note", "quick"])
        self.assertEqual(feedback.id, 3)
        self.assertEqual(feedback.took, "20m")
        self.assertIs(feedback.func, cli.cmd_feedback)

    def test_cli_add_and_feedback_end_to_end(self):
        import contextlib
        import io
        from pathlib import Path

        old_db, old_cfg, old_base = cli.DB, cli.CFG, cli.BASE
        cli.DB = os.path.join(self.tmp.name, "cli.db")
        cli.CFG = os.path.join(self.tmp.name, "cli.json")
        cli.BASE = Path(self.root) / "planner"
        with open(cli.CFG, "w", encoding="utf-8") as handle:
            json.dump({"brain": {"backend": "heuristic"}}, handle)
        try:
            args = cli.build_parser().parse_args(
                ["add", "--desc", "create an audit for coreference resolution",
                 "--json"])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                args.func(args)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["status"], "applied")
            self.assertTrue(payload["analysis"]["related"])
            task_id = payload["task_id"]

            feedback = cli.build_parser().parse_args(
                ["feedback", str(task_id), "--took", "20m",
                 "--note", "script existed"])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                feedback.func(feedback)
            line = out.getvalue()
            self.assertIn("next similar", line)
            self.assertIn("ratio", line)
        finally:
            cli.DB, cli.CFG, cli.BASE = old_db, old_cfg, old_base


if __name__ == "__main__":
    unittest.main()
