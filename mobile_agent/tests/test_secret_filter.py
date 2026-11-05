import json
import unittest

from mobile_agent import secret_filter
from mobile_agent.agent_hooks import MASK
from mobile_agent.feedback import bundle
from mobile_agent.journal import Journal
from mobile_agent.run import Run
from mobile_agent.secret_filter import is_secret, redact
from mobile_agent.tests.test_server_hardening import APP

X = "x" * 24
JWT = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20


class FormatTests(unittest.TestCase):
    def assertMasked(self, text, secret):
        self.assertNotIn(secret, redact(text))
        self.assertIn(MASK, redact(text))
        self.assertTrue(is_secret(text))
        self.assertEqual(redact(redact(text)), redact(text))

    def test_stripe_keys_live_test_secret_and_restricted(self):
        for prefix in ("sk_live_", "sk_test_", "rk_live_", "rk_test_"):
            self.assertMasked(f"key {prefix}{X} here", prefix + X)

    def test_jwt(self):
        self.assertMasked(f"token {JWT} end", JWT)

    def test_team_ci_key(self):
        key = "mob_ci_" + "R" * 30 + "_" + "s" * 43
        self.assertMasked(f"key {key} here", key)

    def test_bearer_header_keeps_the_word(self):
        out = redact("Authorization: Bearer " + "Ab3" + "d" * 20)
        self.assertEqual(out, "Authorization: Bearer " + MASK)

    def test_google_oauth_tokens(self):
        self.assertMasked("ya29." + "A1" * 20, "A1" * 20)
        self.assertMasked("refresh 1//0" + "gA1" * 12, "gA1" * 12)

    def test_env_lines_keep_the_name(self):
        for name in ("OPENAI_API_KEY", "GH_TOKEN", "APP_SECRET"):
            self.assertEqual(redact(f"{name}=hunter22"), f"{name}={MASK}")
            self.assertEqual(redact(f"{name}='hunter22'"), f"{name}='{MASK}'")
            self.assertNotIn("hunter22", redact(f'export {name}="hunter22"'))

    def test_feedback_scrub_uses_the_same_formats(self):
        for secret in (JWT, "rk_live_" + X, "ya29." + "A1" * 20):
            self.assertNotIn(secret, bundle.scrub(f"log {secret}"))


class OrdinaryTextTests(unittest.TestCase):
    def test_ids_hashes_and_sentences_are_untouched(self):
        for text in ("20260928-120001-abcd", "123e4567-e89b-12d3-a456-426614174000", "a" * 40, "com.apple.mobilesafari",
                     "W/\"33a64df551425fcc55e4d42a148795d9f25f89d4\"", "frame-0123456789ab",
                     "The token expired, so Bearer auth failed", "Uses Bearer authentication", "Tap the Token row",
                     "MAX_TOKENS=4096 set", "Open the API key page"):
            self.assertEqual(redact(text), text, text)

    def test_a_jwt_needs_its_header_and_two_dots(self):
        for text in ("abc." + "b" * 20 + "." + "c" * 20, "eyJ" + "a" * 20 + "." + "b" * 20):
            self.assertEqual(redact(text), text)

    def test_deep_redaction_passes_image_data_urls_through(self):
        picture = "data:image/png;base64," + "A1b2" * 2000
        self.assertEqual(secret_filter.redact_deep({"png": picture, "n": 7})["png"], picture)

    def test_long_unspaced_text_is_still_redacted(self):
        key = "mob_ci_" + "R" * 30 + "_" + "s" * 43
        text = "x" * 5000 + ":" + key
        result = secret_filter.redact_deep({"text": text})["text"]
        self.assertNotIn(key, result)
        self.assertIn(MASK, result)
        self.assertEqual(secret_filter.redact_deep({"text": "x" * 5000})["text"], "x" * 5000)

    def test_invalid_image_data_url_does_not_exempt_a_secret(self):
        key = "mob_ci_" + "R" * 30 + "_" + "s" * 43
        text = "data:image/png;base64," + "A" * 5000 + ":" + key
        self.assertNotIn(key, secret_filter.redact_deep({"image": text})["image"])

    def test_short_image_looking_value_does_not_exempt_a_secret(self):
        key = "AKIA" + "A" * 16
        text = "data:image/png;base64," + key
        result = secret_filter.redact_deep({"image": text})["image"]
        self.assertNotIn(key, result)
        self.assertIn(MASK, result)

    def test_depth_limit_masks_the_omitted_subtree(self):
        key = "mob_ci_" + "R" * 30 + "_" + "s" * 43
        nested = {"secret": key, "plain": "safe"}
        for _ in range(9):
            nested = {"next": nested}
        result = secret_filter.redact_deep(nested)
        self.assertNotIn(key, str(result))
        for _ in range(8):
            result = result["next"]
        self.assertEqual(result["next"], MASK)


class RunEventTests(unittest.TestCase):
    def test_a_key_on_a_screen_is_masked_in_the_events_a_run_stores_and_serves(self):
        run = Run(dict(APP), "Read the page", "live")
        key = "sk_live_" + X
        run.emit({"event": "screen_read", "text": f"Your key is {key}",
                  "nested": {"rows": [f"Bearer {'Ab3' + 'd' * 20}"]},
                  "bundleId": "com.apple.Preferences", "runId": "20260928-120001-abcd"})
        entry = run.events[-1]
        self.assertNotIn(key, str(entry))
        self.assertNotIn("Ab3" + "d" * 20, str(entry))
        self.assertEqual(entry["bundleId"], "com.apple.Preferences")
        self.assertEqual(entry["runId"], "20260928-120001-abcd")

    def test_a_key_is_masked_in_the_journal(self):
        db = Journal(None)
        self.addCleanup(db.close)
        run = Run(dict(APP), "Read the page", "live", journal=db)
        db.create(run.metadata())
        key = "rk_test_" + X
        run.emit({"event": "note", "text": key})
        self.assertNotIn(key, str(db.load()))


class RunRedactionTests(unittest.TestCase):
    def test_terminal_summary_uses_the_same_deep_secret_filter_as_events(self):
        journal = Journal(None)
        self.addCleanup(journal.close)
        run = Run(dict(APP), "Read the page", "live", journal=journal)
        journal.create(run.metadata())
        secret = "rk_test_" + "x" * 24
        result = {"event": "result", "status": "completed", "answer": f"Use {secret}",
                  "reason": f"Could not verify {secret}",
                  "data": {"report": [f"proof: {secret}", {"count": 2, "valid": True, "empty": None}]}}
        run.emit(result)
        run.finish(result)
        public = run.public()
        stored = next(record for record in journal.load() if record["id"] == run.id)
        for value in (public, stored):
            self.assertNotIn(secret, json.dumps(value))
            self.assertEqual(value["summary"]["data"]["report"][1],
                             {"count": 2, "valid": True, "empty": None})
            self.assertEqual(value["events"][-1]["summary"], value["summary"])
        self.assertIn(secret, result["answer"])  # finish made a redacted copy; the caller's value was not mutated.


if __name__ == "__main__":
    unittest.main()
