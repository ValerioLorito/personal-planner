"""Tests for planner/sources.py: categories, inventory, material blocks,
budgeted full-text extraction with cache, and honest degradation."""
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from planner import sources


def _make_uni(root):
    """One subject with a reading (pdf stub), a real pptx, notes, noise."""
    subject = os.path.join(root, "NLP Semantics")
    os.makedirs(os.path.join(subject, "readings"), exist_ok=True)
    os.makedirs(os.path.join(root, ".Hidden"), exist_ok=True)
    with open(os.path.join(subject, "readings", "essay.pdf"), "wb") as fh:
        fh.write(b"not-a-real-pdf")          # extraction will fail honestly
    slides = ("<?xml version='1.0'?><p:sld "
              "xmlns:p='http://schemas.openxmlformats.org/presentationml/2006/main'>"
              "<p:cSld><p:spTree><p:sp><p:txBody>"
              "<a:p xmlns:a='http://schemas.openxmlformats.org/drawingml/2006/main'>"
              "<a:r><a:t>%s</a:t></a:r></a:p>"
              "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>")
    with zipfile.ZipFile(os.path.join(subject, "lecture.pptx"), "w") as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        zf.writestr("ppt/slides/slide1.xml", slides % "Week 1: intro")
        zf.writestr("ppt/slides/slide2.xml", slides % "Week 2: implicature")
    with open(os.path.join(subject, ".essay.pdf.icloud"), "w") as fh:
        fh.write("placeholder stub")
    with open(os.path.join(subject, "planner-notes.md"), "w") as fh:
        fh.write("Prof wants typicality effects cited.")
    with open(os.path.join(subject, ".DS_Store"), "w") as fh:
        fh.write("junk")
    with open(os.path.join(root, "Loose file.txt"), "w") as fh:
        fh.write("x")                        # not a subject folder: ignored
    return subject


class CategoriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "uni")
        os.makedirs(self.root)
        self.cfg = {"university_root": self.root}

    def test_categories_is_thesis_plus_subject_folders(self):
        _make_uni(self.root)
        self.assertEqual(sources.categories(self.cfg),
                         ["Thesis", "NLP Semantics"])

    def test_no_root_or_missing_root_gives_thesis_only(self):
        self.assertEqual(sources.categories({}), ["Thesis"])
        self.assertEqual(sources.categories({"university_root": "/no/such/dir"}),
                         ["Thesis"])
        self.assertEqual(sources.university_root(self.cfg), self.root)
        self.assertEqual(sources.university_root({"university_root": "~nope"}), "")

    def test_hidden_and_dot_folders_are_not_subjects(self):
        os.makedirs(os.path.join(self.root, ".stFolder"))
        self.assertEqual(sources.categories(self.cfg), ["Thesis"])

    def test_icloud_placeholder_names_are_unaliased(self):
        self.assertEqual(sources._unalias(".essay.pdf.icloud"), "essay.pdf")
        self.assertEqual(sources._unalias("plain"), "plain")
        self.assertEqual(sources._unalias(".icloud"), ".icloud")


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "uni")
        os.makedirs(self.root)
        self.subject = _make_uni(self.root)
        self.cfg = {"university_root": self.root}

    def test_inventory_lists_files_folders_and_placeholders(self):
        inv = sources.inventory(self.cfg)
        self.assertEqual(inv["categories"], ["NLP Semantics"])
        self.assertFalse(inv["truncated"])
        by_path = {e["path"]: e for e in inv["entries"]}
        self.assertIn("NLP Semantics/lecture.pptx", by_path)
        self.assertIn("NLP Semantics/readings", by_path)
        self.assertEqual(by_path["NLP Semantics/readings"]["kind"], "dir")
        ph = by_path["NLP Semantics/.essay.pdf.icloud"]
        self.assertTrue(ph["placeholder"])
        self.assertIsNone(ph["size"])
        # noise never shows up
        self.assertNotIn(".DS_Store", str(by_path))
        self.assertNotIn("Loose file.txt", str(by_path))

    def test_notes_are_read_and_capped(self):
        with open(os.path.join(self.subject, "planner-notes.md"), "w") as fh:
            fh.write("x" * 9000)
        inv = sources.inventory(self.cfg)
        self.assertEqual(len(inv["notes"]["NLP Semantics"]),
                         sources.MAX_NOTE_CHARS)

    def test_entry_cap_marks_truncation(self):
        subject = os.path.join(self.root, "Crowded")
        os.makedirs(subject)
        for i in range(sources.MAX_ENTRIES + 40):
            with open(os.path.join(subject, "file_%03d.txt" % i), "w") as fh:
                fh.write("x")
        inv = sources.inventory(self.cfg)
        self.assertTrue(inv["truncated"])
        self.assertLessEqual(len(inv["entries"]), sources.MAX_ENTRIES)


class BlocksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "uni")
        os.makedirs(self.root)
        _make_uni(self.root)
        self.cfg = {"university_root": self.root}
        self.inv = sources.inventory(self.cfg)

    def test_category_block_lists_files_notes_and_placeholders(self):
        block = sources.category_block(self.inv, "NLP Semantics")
        self.assertIn('MATERIAL — subject "NLP Semantics"', block)
        self.assertIn("- NLP Semantics/lecture.pptx", block)
        self.assertIn("- NLP Semantics/readings/ (folder)", block)
        self.assertIn("iCloud placeholder", block)
        self.assertIn("Prof wants typicality effects cited.", block)

    def test_empty_category_gives_no_block(self):
        self.assertEqual(sources.category_block(self.inv, "Ghost"), "")

    def test_material_blocks_category_plus_map(self):
        os.makedirs(os.path.join(self.root, "Formal Methods"))
        inv = sources.inventory(self.cfg)
        blocks = sources.material_blocks(inv, "NLP Semantics")
        self.assertEqual(len(blocks), 2)
        self.assertIn('subject "NLP Semantics"', blocks[0])
        self.assertIn("university folder map", blocks[1])
        self.assertIn("Formal Methods (0 item(s) on file)", blocks[1])

    def test_material_blocks_for_thesis_is_just_the_map(self):
        blocks = sources.material_blocks(self.inv, "Thesis")
        self.assertEqual(len(blocks), 1)
        self.assertIn("university folder map", blocks[0])


class ExtractBudgetTest(unittest.TestCase):
    """Water-filling of the character budget, with extraction faked."""

    def fake_extract(self, texts):
        """texts: path -> full text (or None -> error entry)."""
        def one(path):
            text = texts.get(path)
            base = {"path": path, "kind": "txt", "pages": None,
                    "error": None, "cached": False}
            if text is None:
                base["error"] = "unreadable"
                return base
            base["text"] = text
            return base
        return one

    def run_extract(self, texts, budget):
        with mock.patch.object(sources, "extract_one",
                               side_effect=self.fake_extract(texts)):
            return sources.extract_text(list(texts), budget)

    def test_everything_fits_is_untouched(self):
        res = self.run_extract({"a.txt": "hello", "b.txt": "world"}, budget=60000)
        self.assertEqual([r["text"] for r in res], ["hello", "world"])
        self.assertFalse(any(r["truncated"] for r in res))

    def test_budget_is_respected_and_marked(self):
        big = "x" * 50000
        res = self.run_extract({"a.txt": big, "b.txt": big}, budget=60000)
        self.assertLessEqual(sum(r["chars"] for r in res), 60000)
        self.assertTrue(all(r["truncated"] for r in res))
        for r in res:
            self.assertIn("[truncated:", r["text"])
            self.assertIn("%d chars]" % 50000, r["text"])

    def test_small_files_do_not_starve_the_big_one(self):
        small = "y" * 1000
        big = "z" * 50000
        res = self.run_extract({"small.txt": small, "big.txt": big},
                               budget=30000)
        self.assertEqual(res[0]["text"], small)       # small: untouched
        self.assertFalse(res[0]["truncated"])
        self.assertTrue(res[1]["truncated"])
        self.assertGreater(res[1]["chars"], 20000)    # big: got the rest
        self.assertLessEqual(res[0]["chars"] + res[1]["chars"], 30000)

    def test_failed_files_cost_nothing(self):
        small = "y" * 100
        big = "z" * 9000
        res = self.run_extract({"bad.txt": None, "small.txt": small,
                                "big.txt": big}, budget=5000)
        self.assertTrue(res[0]["error"])
        self.assertEqual(res[1]["text"], small)
        self.assertEqual(res[2]["chars"], 5000 - 100)

    def test_empty_paths_is_empty(self):
        self.assertEqual(sources.extract_text([]), [])


class ExtractOneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "uni")
        os.makedirs(self.root)
        self.subject = _make_uni(self.root)
        self.cache = os.path.join(self.tmp.name, "cache.json")
        patcher = mock.patch.object(sources, "CACHE_PATH", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pptx_text_is_extracted_and_cached(self):
        path = os.path.join(self.subject, "lecture.pptx")
        first = sources.extract_one(path)
        self.assertIsNone(first["error"])
        self.assertEqual(first["pages"], 2)
        self.assertIn("Week 1: intro", first["text"])
        self.assertIn("Week 2: implicature", first["text"])
        self.assertFalse(first["cached"])
        second = sources.extract_one(path)
        self.assertTrue(second["cached"])
        self.assertEqual(second["text"], first["text"])
        self.assertTrue(os.path.exists(self.cache))

    def test_broken_pdf_fails_honestly(self):
        res = sources.extract_one(os.path.join(self.subject, "readings",
                                               "essay.pdf"))
        self.assertTrue(res["error"], res)
        self.assertFalse(res["text"])

    def test_missing_and_unsupported_files_degrade(self):
        missing = sources.extract_one(os.path.join(self.subject, "nope.pdf"))
        self.assertIn("not found", missing["error"])
        key_path = os.path.join(self.subject, "slides.key")
        with open(key_path, "w") as fh:
            fh.write("keynote bundle stub")
        key = sources.extract_one(key_path)
        self.assertIn("Keynote", key["error"])
        # the error result must not be poisoned into the cache
        again = sources.extract_one(os.path.join(self.subject, "nope.pdf"))
        self.assertFalse(again["cached"])

    def test_icloud_placeholder_says_so(self):
        res = sources.extract_one(os.path.join(self.subject,
                                               ".essay.pdf.icloud"))
        self.assertIn("iCloud placeholder", res["error"])

    def test_excerpts_block_lists_readable_and_failed(self):
        results = sources.extract_text(
            [os.path.join(self.subject, "lecture.pptx"),
             os.path.join(self.subject, "readings", "essay.pdf")])
        block = sources.excerpts_block(results)
        self.assertIn("MATERIAL EXCERPTS", block)
        self.assertIn("lecture.pptx", block)
        self.assertIn("Week 1: intro", block)
        self.assertIn("NOT readable", block)
        self.assertIn("essay.pdf", block.split("NOT readable")[1])


if __name__ == "__main__":
    unittest.main()
