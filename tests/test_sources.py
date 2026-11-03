import unittest
import os
import shutil
import tempfile
from planner.app import App
from planner import sources

class TestSources(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.thesis_dir = os.path.join(self.temp_dir, "thesis_repo")
        os.makedirs(os.path.join(self.thesis_dir, "src"), exist_ok=True)
        with open(os.path.join(self.thesis_dir, "src", "eval.py"), "w") as f:
            f.write("def evaluate_accuracy():\n    return 0.95\n")
        
        self.cfg = {
            "thesis": {"label": "Thesis", "root": self.thesis_dir, "skip_dirs": []},
            "university": {"root": "", "exclude": [], "aliases": {}},
            "calendars": {"thesis": "Thesis", "university": "University", "habits": "Habits"}
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_resolve_and_read(self):
        cats = sources.categories(self.cfg)
        t_cat = [c for c in cats if c["id"] == "thesis"][0]
        
        # Safe resolution
        resolved = sources.resolve(t_cat, "src/eval.py")
        self.assertTrue(os.path.isfile(resolved))

        # Path traversal prevention
        with self.assertRaises(sources.SourceError):
            sources.resolve(t_cat, "../../etc/passwd")

        # Read file
        content = sources.read_file(self.cfg, t_cat, "src/eval.py")
        self.assertIn("evaluate_accuracy", content)

        # Search
        hits = sources.search(self.cfg, t_cat, "evaluate")
        self.assertIn("eval.py", hits)

    def test_modular_sources_single_and_subfolders(self):
        proj_dir = os.path.join(self.temp_dir, "custom_project")
        courses_dir = os.path.join(self.temp_dir, "my_courses")
        os.makedirs(proj_dir, exist_ok=True)
        os.makedirs(os.path.join(courses_dir, "Quantum Computing"), exist_ok=True)
        os.makedirs(os.path.join(courses_dir, "Archive"), exist_ok=True)

        modular_cfg = {
            "sources": [
                {
                    "id": "research",
                    "label": "Research Repo",
                    "path": proj_dir,
                    "mode": "single",
                    "calendar": "ResearchCal",
                    "enabled": True,
                },
                {
                    "id": "courses",
                    "label": "Courses",
                    "path": courses_dir,
                    "mode": "subfolders",
                    "calendar": "CoursesCal",
                    "exclude": ["Archive"],
                    "enabled": True,
                },
            ],
            "calendars": {"thesis": "Thesis", "university": "University", "habits": "Habits"},
        }
        cats = sources.categories(modular_cfg)
        ids = [c["id"] for c in cats]
        self.assertIn("research", ids)
        self.assertIn("courses:Quantum Computing", ids)
        self.assertNotIn("courses:Archive", ids)
        c_map = {c["id"]: c for c in cats}
        self.assertEqual(c_map["research"]["calendar"], "ResearchCal")
        self.assertEqual(c_map["courses:Quantum Computing"]["calendar"], "CoursesCal")

if __name__ == "__main__":
    unittest.main()
