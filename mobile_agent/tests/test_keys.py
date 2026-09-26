"""Bring your own keys: masked, persisted privately, validated, removable. Offline."""

import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from mobile_agent.keys import HELPER_ENV, Keys

JEV = "apikey_" + "a" * 40 + "7a23"
HELPER = "sk-or-v1-" + "b" * 40 + "9f1c"


class KeyTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("TYPESAFE_API_KEY", *HELPER_ENV):
            os.environ.pop(key, None)
        self.env_file = Path(tempfile.mkdtemp()) / "agent.env"
        self.keys = Keys(self.env_file)

    def test_keys_are_saved_privately_and_only_their_tail_is_shown(self):
        state = self.keys.update({"jev": {"key": JEV}, "helper": {"provider": "openrouter", "model": "google/gemini-2.5-flash", "key": HELPER}})
        text = json.dumps(state)
        self.assertNotIn(JEV, text)
        self.assertNotIn(HELPER, text)
        self.assertEqual(state["jev"], {"configured": True, "keyHint": "…7a23"})
        self.assertEqual((state["helper"]["provider"], state["helper"]["keyHint"], state["helper"]["configured"]),
                         ("openrouter", "…9f1c", True))
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        saved = self.env_file.read_text()
        self.assertIn(f"TYPESAFE_API_KEY={JEV}", saved)
        self.assertIn("TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1", saved)
        self.assertEqual(os.environ["TEXT_MODEL"], "google/gemini-2.5-flash")

    def test_changing_only_the_model_keeps_the_key_and_switching_provider_needs_one(self):
        self.keys.update({"helper": {"provider": "openai", "model": "gpt-4.1-mini", "key": HELPER}})
        self.keys.update({"helper": {"provider": "openai", "model": "gpt-4.1"}})
        self.assertEqual(os.environ["TEXT_MODEL_API_KEY"], HELPER)
        with self.assertRaises(ValueError):
            self.keys.update({"helper": {"provider": "groq", "model": "llama-3.3-70b-versatile"}})

    def test_vertex_and_custom_endpoints(self):
        state = self.keys.update({"helper": {"provider": "vertex", "model": "gemini-3.5-flash-lite", "project": "my-project-1"}})
        self.assertEqual((state["helper"]["provider"], state["helper"]["location"], state["helper"]["keyHint"]), ("vertex", "global", None))
        self.assertEqual(os.environ["TEXT_MODEL_PROVIDER"], "vertex")
        state = self.keys.update({"helper": {"provider": "custom", "model": "qwen3", "key": HELPER, "baseUrl": "http://localhost:11434/v1/"}})
        self.assertEqual(state["helper"]["baseUrl"], "http://localhost:11434/v1")
        self.assertNotIn("TEXT_MODEL_PROVIDER", os.environ)
        for url in ("http://example.com/v1", "ftp://x", "https://user:pw@example.com", "https://x.com/v1?key=1"):
            with self.assertRaises(ValueError, msg=url):
                self.keys.update({"helper": {"provider": "custom", "model": "m", "key": HELPER, "baseUrl": url}})

    def test_a_server_on_this_mac_needs_no_key(self):
        state = self.keys.update({"helper": {"provider": "custom", "model": "qwen3", "baseUrl": "http://localhost:11434/v1"}})
        self.assertEqual((state["helper"]["configured"], state["helper"]["keyHint"]), (True, "not needed"))
        with self.assertRaises(ValueError):
            self.keys.update({"helper": {"provider": "custom", "model": "m", "baseUrl": "https://api.example.com/v1"}})

    def test_a_saved_key_never_follows_a_new_custom_endpoint(self):
        self.keys.update({"helper": {"provider": "custom", "model": "m", "key": HELPER, "baseUrl": "https://api.example.com/v1"}})
        self.keys.update({"helper": {"provider": "custom", "model": "m2", "baseUrl": "https://api.example.com/v1"}})
        self.assertEqual(os.environ["TEXT_MODEL_API_KEY"], HELPER)
        with self.assertRaises(ValueError):
            self.keys.update({"helper": {"provider": "custom", "model": "m", "baseUrl": "https://attacker.example/v1"}})
        self.assertEqual(os.environ["TEXT_MODEL_BASE_URL"], "https://api.example.com/v1")
        state = self.keys.update({"helper": {"provider": "custom", "model": "m", "baseUrl": "http://localhost:11434/v1"}})
        self.assertEqual(state["helper"]["keyHint"], "not needed")

    def test_removing_the_helper_clears_every_helper_setting(self):
        self.keys.update({"helper": {"provider": "openrouter", "model": "m", "key": HELPER}})
        state = self.keys.update({"helper": None})
        self.assertFalse(state["helper"]["configured"])
        self.assertTrue(all(key not in os.environ for key in HELPER_ENV))
        self.assertNotIn("TEXT_MODEL", self.env_file.read_text())

    def test_bad_input_is_refused(self):
        for body in ({}, {"other": 1}, {"jev": {"key": "short"}}, {"jev": {"key": "has spaces in it ok ok"}},
                     {"helper": {"provider": "nope", "model": "m"}}, {"helper": {"provider": "openai", "model": "bad model"}}):
            with self.assertRaises(ValueError, msg=body):
                self.keys.update(body)


class LiveCheckTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TYPESAFE_API_KEY": JEV, "TEXT_MODEL": "m", "TEXT_MODEL_API_KEY": HELPER,
                                      "MOBSTER_HELPER_PROVIDER": "openai", "TEXT_MODEL_BASE_URL": "https://api.openai.com/v1"})
        env.start()
        self.addCleanup(env.stop)

    def opener(self, code=None):
        calls = []

        def open_(request, timeout):
            calls.append(request)
            if code:
                raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(b""))
            return io.BytesIO(b"{}")
        return open_, calls

    def test_accepted_rejected_and_missing_models_read_plainly(self):
        opener, calls = self.opener()
        self.assertTrue(Keys(None).test("helper", opener)["ok"])
        self.assertEqual(calls[0].full_url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(calls[0].get_header("Authorization"), f"Bearer {HELPER}")
        self.assertIn("rejected", Keys(None).test("jev", self.opener(401)[0])["message"])
        self.assertIn("model", Keys(None).test("helper", self.opener(404)[0])["message"])
        self.assertTrue(Keys(None).test("helper", self.opener(429)[0])["ok"])
        with self.assertRaises(ValueError):
            Keys(None).test("other", opener)


if __name__ == "__main__":
    unittest.main()
