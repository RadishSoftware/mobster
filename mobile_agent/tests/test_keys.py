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
OPENAI = "sk-proj-" + "c" * 40 + "e3d1"
HELPER = "sk-or-v1-" + "b" * 40 + "9f1c"


class KeyTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("TYPESAFE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MOBSTER_SMART_PROVIDER", "MOBSTER_SMART_MODEL",
                    *HELPER_ENV):
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

    def test_the_smart_key_is_saved_shown_by_its_tail_and_removable(self):
        state = self.keys.update({"openai": {"key": f"  {OPENAI} "}})
        self.assertEqual(state["openai"], {"configured": True, "keyHint": "…e3d1", "fromHelper": False,
                                           "model": "gpt-5.6-sol"})
        self.assertNotIn(OPENAI, json.dumps(state))
        self.assertIn(f"OPENAI_API_KEY={OPENAI}", self.env_file.read_text())
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        self.assertEqual(os.environ["OPENAI_API_KEY"], OPENAI)
        state = self.keys.update({"openai": None})
        self.assertFalse(state["openai"]["configured"])
        self.assertNotIn("OPENAI_API_KEY", self.env_file.read_text())
        self.assertNotIn("OPENAI_API_KEY", os.environ)

    def test_an_openai_helper_key_counts_for_smart_and_other_providers_do_not(self):
        self.assertFalse(self.keys.state()["openai"]["configured"])
        state = self.keys.update({"helper": {"provider": "openai", "model": "gpt-4.1-mini", "key": HELPER}})
        self.assertEqual((state["openai"]["configured"], state["openai"]["fromHelper"], state["openai"]["keyHint"]),
                         (True, True, "…9f1c"))
        state = self.keys.update({"helper": {"provider": "openrouter", "model": "m", "key": HELPER}})
        self.assertFalse(state["openai"]["configured"])
        # Its own key wins over the helper's.
        self.keys.update({"helper": {"provider": "openai", "model": "gpt-4.1-mini", "key": HELPER}})
        state = self.keys.update({"openai": {"key": OPENAI}})
        self.assertEqual((state["openai"]["fromHelper"], state["openai"]["keyHint"]), (False, "…e3d1"))

    def test_with_both_keys_the_chosen_provider_runs_smart_and_the_default_stays_openai(self):
        anthropic = "sk-ant-api03-" + "d" * 40 + "a7f2"
        self.assertEqual(self.keys.state()["smart"], {"provider": None, "preference": None})
        state = self.keys.update({"anthropic": {"key": anthropic}})
        self.assertEqual(state["smart"], {"provider": "anthropic", "preference": None})
        # Both keys and no choice: OpenAI's, as before Setup could choose.
        state = self.keys.update({"openai": {"key": OPENAI}})
        self.assertEqual(state["smart"], {"provider": "openai", "preference": None})
        state = self.keys.update({"smartProvider": "anthropic"})
        self.assertEqual(state["smart"], {"provider": "anthropic", "preference": "anthropic"})
        self.assertIn("MOBSTER_SMART_PROVIDER=anthropic", self.env_file.read_text())
        self.assertEqual(state["anthropic"]["model"], "claude-sonnet-5-5")
        # A choice whose key is gone falls back to the key that is there.
        state = self.keys.update({"anthropic": None})
        self.assertEqual(state["smart"], {"provider": "openai", "preference": "anthropic"})
        state = self.keys.update({"smartProvider": None})
        self.assertNotIn("MOBSTER_SMART_PROVIDER", self.env_file.read_text())
        self.assertIsNone(state["smart"]["preference"])
        for body in ({"smartProvider": "gemini"}, {"smartProvider": 1}, {"smartProvider": ""}):
            with self.assertRaises(ValueError, msg=body):
                self.keys.update(body)

    def test_bad_input_is_refused(self):
        for body in ({}, {"other": 1}, {"jev": {"key": "short"}}, {"openai": {"key": "short"}}, {"openai": "sk-x"},
                     {"openai": {"key": OPENAI, "extra": 1}}, {"jev": {"key": "has spaces in it ok ok"}},
                     {"helper": {"provider": "nope", "model": "m"}}, {"helper": {"provider": "openai", "model": "bad model"}}):
            with self.assertRaises(ValueError, msg=body):
                self.keys.update(body)


class LiveCheckTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TYPESAFE_API_KEY": JEV, "TEXT_MODEL": "m", "TEXT_MODEL_API_KEY": HELPER,
                                      "MOBSTER_HELPER_PROVIDER": "openai", "TEXT_MODEL_BASE_URL": "https://api.openai.com/v1"})
        env.start()
        self.addCleanup(env.stop)

    def opener(self, code=None, error_code=None):
        calls = []

        def open_(request, timeout):
            calls.append(request)
            if code:
                body = json.dumps({"error": {"code": error_code}}).encode() if error_code else b""
                raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(body))
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

    def test_the_openai_check_reads_smarts_model_then_generates_a_few_tokens(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": OPENAI}):
            opener, calls = self.opener()
            check = Keys(None).test("openai", opener)
            self.assertEqual(check, {"ok": True, "message": "OpenAI accepted the key, and it can run gpt-5.6-sol."})
            self.assertEqual([(call.get_method(), call.full_url) for call in calls],
                             [("GET", "https://api.openai.com/v1/models/gpt-5.6-sol"),
                              ("POST", "https://api.openai.com/v1/responses")])
            self.assertEqual({call.get_header("Authorization") for call in calls}, {f"Bearer {OPENAI}"})
            self.assertIsNone(calls[0].data)
            generation = json.loads(calls[1].data)
            # Quota is only enforced when OpenAI generates; this is the smallest generation it accepts.
            self.assertEqual((generation["model"], generation["max_output_tokens"], generation["reasoning"]),
                             ("gpt-5.6-sol", 16, {"effort": "low"}))
            rejected = Keys(None).test("openai", self.opener(401)[0])
            self.assertEqual((rejected["ok"], rejected["problem"]), (False, "rejected"))
            self.assertIn("didn't accept this key", rejected["message"])
            no_model = Keys(None).test("openai", self.opener(404)[0])
            self.assertFalse(no_model["ok"])
            self.assertEqual(no_model["message"], "This key's project can't use gpt-5.6-sol, the model Mobster's agent "
                                                  "runs on. Allow it in OpenAI › Settings › Project › Limits, then test again.")
            self.assertEqual(no_model["problem"], "no_model")
            self.assertTrue(Keys(None).test("openai", self.opener(429, "rate_limit_exceeded")[0])["ok"])

            def offline(request, timeout):
                raise urllib.error.URLError("offline")
            self.assertIn("couldn't reach OpenAI", Keys(None).test("openai", offline)["message"])
            self.assertEqual(Keys(None).test("openai", offline)["problem"], "offline")
        # The helper here is OpenAI, so its key is what the check sends without a key of its own.
        opener, calls = self.opener()
        self.assertTrue(Keys(None).test("openai", opener)["ok"])
        self.assertEqual(calls[0].get_header("Authorization"), f"Bearer {HELPER}")
        with patch.dict(os.environ, {"MOBSTER_HELPER_PROVIDER": "groq"}):
            self.assertEqual(Keys(None).test("openai", opener), {"ok": False, "message": "Add your OpenAI key first."})

    def test_a_key_with_no_credit_fails_with_the_billing_link(self):
        """A new account's key reads the model fine; only the generation says insufficient_quota (as a 429)."""
        calls = []

        def open_(request, timeout):
            calls.append(request)
            if request.get_method() == "POST":
                body = json.dumps({"error": {"message": "You exceeded your current quota", "type": "insufficient_quota",
                                             "code": "insufficient_quota"}}).encode()
                raise urllib.error.HTTPError(request.full_url, 429, "x", {}, io.BytesIO(body))
            return io.BytesIO(b"{}")
        with patch.dict(os.environ, {"OPENAI_API_KEY": OPENAI}):
            check = Keys(None).test("openai", open_)
            self.assertEqual(check, {"ok": False, "message": "Your OpenAI account has no credit yet. Add $5 in Billing, then test again.",
                                     "link": {"label": "Add credit",
                                              "url": "https://platform.openai.com/settings/organization/billing/overview"},
                                     "problem": "no_credit"})
            self.assertEqual(len(calls), 2)
            # The same body on the model read (some accounts answer that way) reads the same.
            self.assertFalse(Keys(None).test("openai", self.opener(429, "insufficient_quota")[0])["ok"])
            # An exhausted credit balance and a 402 are no credit too, never a rate limit that passes.
            for status, code in ((429, "credit_balance_exhausted"), (402, "billing_hard_limit_reached")):
                check = Keys(None).test("openai", self.opener(status, code)[0])
                self.assertEqual((check["ok"], check.get("problem")), (False, "no_credit"), code)
            self.assertNotIn("problem", Keys(None).test("openai", self.opener(429, "rate_limit_exceeded")[0]))
        # A helper with no credit is not "accepted" either.
        self.assertFalse(Keys(None).test("helper", self.opener(429, "insufficient_quota")[0])["ok"])


if __name__ == "__main__":
    unittest.main()
