"""
test_llm.py — unit tests for LLM client configuration, presets, and repair logic.
"""
import os
import unittest
from planner.config import LLM_PRESETS, apply_preset, DEFAULTS
from planner.db import DB
from planner.llm import LLM, extract_json, _try_repair_truncated_json


class TestLLM(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_presets_exist(self):
        self.assertIn("gemini", LLM_PRESETS)
        self.assertIn("gemini-3.5", LLM_PRESETS)
        self.assertIn("gemini-3.8", LLM_PRESETS)
        self.assertIn("openrouter", LLM_PRESETS)

        gemini = LLM_PRESETS["gemini"]
        self.assertEqual(gemini["model"], "gemini-3.5-flash")
        self.assertIn("generativelanguage.googleapis.com", gemini["base_url"])
        self.assertEqual(gemini["daily_budget"], 1500)

    def test_apply_preset(self):
        cfg = {"llm": {"model": "old-model"}}
        apply_preset(cfg, "gemini")
        self.assertEqual(cfg["llm"]["model"], "gemini-3.5-flash")
        self.assertEqual(cfg["llm"]["provider"], "gemini")
        self.assertEqual(cfg["llm"]["daily_budget"], 1500)

        apply_preset(cfg, "openrouter")
        self.assertEqual(cfg["llm"]["model"], "openrouter/free")
        self.assertEqual(cfg["llm"]["daily_budget"], 50)

    def test_llm_budget_and_provider_detection(self):
        cfg = {"llm": {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "model": "gemini-3.5-flash",
            "daily_budget": 1500,
            "watch_budget": 300,
            "key_file": "/tmp/non_existent_key_file_test"
        }}
        llm = LLM(cfg, self.db)
        b = llm.budget()
        self.assertEqual(b["provider"], "gemini")
        self.assertEqual(b["limit"], 1500)
        self.assertEqual(b["watch_limit"], 300)

    def test_api_key_fallbacks(self):
        cfg = {"llm": {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "model": "gemini-2.5-flash",
            "api_key_env": "CUSTOM_ENV_KEY",
            "key_file": "/tmp/non_existent_test_key"
        }}
        llm = LLM(cfg, self.db)

        # Set GEMINI_API_KEY in environment
        os.environ["GEMINI_API_KEY"] = "AIzaSyTestGeminiKey123"
        try:
            self.assertEqual(llm.api_key(), "AIzaSyTestGeminiKey123")
        finally:
            del os.environ["GEMINI_API_KEY"]

    def test_json_extraction_and_repair(self):
        truncated = '{"title": "Test Task", "summary": "Cut off mid'
        repaired = _try_repair_truncated_json(truncated)
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.get("title"), "Test Task")

        text = 'Here is the plan:\n```json\n{"category": "thesis", "subtasks": []}\n```'
        extracted = extract_json(text)
        self.assertEqual(extracted.get("category"), "thesis")


if __name__ == "__main__":
    unittest.main()
