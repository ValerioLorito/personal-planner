"""
test_config.py — unit tests for configuration loading, saving, and argument tolerance.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from planner import config
from legacy import config as legacy_config


class TestConfigSave(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cfg_file = os.path.join(self.tmp_dir.name, "config.json")
        self.data = {"test_key": "test_val", "nested": {"num": 42}}

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_planner_save_standard_order(self):
        config.save(self.data, self.cfg_file)
        self.assertTrue(os.path.exists(self.cfg_file))
        loaded = config.load(self.cfg_file)
        self.assertEqual(loaded.get("test_key"), "test_val")

    def test_planner_save_swapped_order(self):
        # Swapped order: save(path, cfg)
        config.save(self.cfg_file, self.data)
        self.assertTrue(os.path.exists(self.cfg_file))
        loaded = config.load(self.cfg_file)
        self.assertEqual(loaded.get("test_key"), "test_val")

    def test_legacy_save_standard_order(self):
        legacy_config.save(self.cfg_file, self.data)
        self.assertTrue(os.path.exists(self.cfg_file))
        loaded = legacy_config.load(self.cfg_file)
        self.assertEqual(loaded.get("test_key"), "test_val")

    def test_legacy_save_swapped_order(self):
        legacy_config.save(self.data, self.cfg_file)
        self.assertTrue(os.path.exists(self.cfg_file))
        loaded = legacy_config.load(self.cfg_file)
        self.assertEqual(loaded.get("test_key"), "test_val")


if __name__ == "__main__":
    unittest.main()
