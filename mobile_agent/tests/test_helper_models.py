"""Per-run model choices and exact generation configs, entirely offline."""

import os
import unittest
from unittest.mock import Mock, patch

from mobile_agent.costs import estimate_cost, planning_estimate
from mobile_agent.gemini import GCloudToken, VertexHTTP
from mobile_agent.helper_models import model_options, resolve_helper_model, thinking_config
from mobile_agent.models import Helper


class HelperModelTests(unittest.TestCase):
    def test_null_uses_configured_default_and_known_options_are_deduplicated(self):
        with patch.dict(os.environ, {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.5-flash-lite"}):
            self.assertEqual(resolve_helper_model(), "gemini-3.5-flash-lite")
            self.assertEqual(model_options().count("gemini-3.5-flash-lite"), 1)
            self.assertEqual(resolve_helper_model("gemini-3.7-flash"), "gemini-3.7-flash")

    def test_non_vertex_provider_offers_only_its_configured_model(self):
        with patch.dict(os.environ, {"TEXT_MODEL_PROVIDER": "openrouter", "TEXT_MODEL": "provider/model"}):
            self.assertEqual(model_options(), ["provider/model"])
            self.assertEqual(resolve_helper_model(), "provider/model")
            with self.assertRaises(ValueError):
                resolve_helper_model("gemini-3.7-flash")

    def test_explicit_constructor_override_does_not_mutate_environment_or_another_helper(self):
        with patch.dict(os.environ, {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.5-flash-lite", "GOOGLE_CLOUD_PROJECT": "project-test"}):
            first, second = Helper(model="gemini-3.7-flash"), Helper()
            self.addCleanup(first.http.close)
            self.addCleanup(second.http.close)
            self.assertEqual(first.model, "gemini-3.7-flash")
            self.assertEqual(second.model, "gemini-3.5-flash-lite")
            self.assertEqual(os.environ["TEXT_MODEL"], "gemini-3.5-flash-lite")
            self.assertEqual(first.completion_body([], 300)["model"], "gemini-3.7-flash")

    def test_constructor_rejects_empty_or_nonstring_override_not_silent_default(self):
        for model in ("", True, [], "x" * 129, "gemini-private\nsecret"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                Helper(model=model)

    def test_exact_documented_thinking_configuration_per_model(self):
        expected = {"gemini-3.5-flash-lite": {"thinkingLevel": "MINIMAL"},
                    "gemini-3.7-flash": {"thinkingLevel": "LOW"},
                    "gemini-3.1-pro-preview": {"thinkingLevel": "LOW"},
                    "gemini-2.5-flash-lite": {"thinkingBudget": 0},
                    "gemini-2.5-flash": {"thinkingBudget": 0}}
        for model, config in expected.items():
            self.assertEqual(thinking_config(model), {"thinkingConfig": config})
        for model in ("gemini-2.5-pro", "gemini-3-future", "gemini-2.0-flash"):
            self.assertEqual(thinking_config(model), {})

    def test_generation_payload_uses_selected_model_and_its_supported_thinking_level(self):
        for model in ("gemini-3.5-flash-lite", "gemini-3.7-flash", "gemini-3.1-pro-preview", "gemini-2.5-pro"):
            adapter = VertexHTTP("project-test", "global")
            self.addCleanup(adapter.close)
            adapter.http.request = Mock(return_value={"modelVersion": model, "usageMetadata": {},
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}]})
            with patch.object(GCloudToken, "get", return_value="offline-token"):
                adapter.request("POST", "/chat/completions", {"model": model,
                    "messages": [{"role": "user", "content": "Test"}], "max_tokens": 300})
            route, payload = adapter.http.request.call_args.args[1:3]
            self.assertTrue(route.endswith(model + ":generateContent"))
            generation = payload["generationConfig"]
            self.assertEqual(generation.get("thinkingConfig"), thinking_config(model).get("thinkingConfig"))
            self.assertNotIn("temperature", generation)

    def test_each_supported_selection_uses_its_own_published_rate(self):
        usage = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
        for model, expected in (("gemini-3.7-flash", .00225), ("gemini-3.1-pro-preview", .0032)):
            self.assertEqual(estimate_cost("google", model, usage, "global")["estimated_usd"], expected)
            estimate = planning_estimate("jev-latest", model, "global")
            self.assertEqual(estimate["helperModel"], model)
            self.assertIsNotNone(estimate["estimated_min_usd"])
            self.assertIsNotNone(estimate["estimated_max_usd"])
            self.assertEqual(estimate["rates"]["google"]["model"], model)


if __name__ == "__main__":
    unittest.main()
