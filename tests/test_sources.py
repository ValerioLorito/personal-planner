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

if __name__ == "__main__":
    unittest.main()
