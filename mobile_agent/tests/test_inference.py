import unittest
from unittest.mock import Mock

from ..inference import reported_usage, request_inference


class InferenceTests(unittest.TestCase):
    def test_google_and_openai_counts_are_only_normalized_not_estimated(self):
        self.assertEqual(reported_usage({"promptTokenCount": 17, "candidatesTokenCount": 3,
            "totalTokenCount": 22, "thoughtsTokenCount": 2}),
            {"input_tokens": 17, "output_tokens": 3, "total_tokens": 22, "reasoning_tokens": 2})
        self.assertEqual(reported_usage({"prompt_tokens": 0}), {"input_tokens": 0})
        self.assertEqual(reported_usage({"input_tokens": 5, "output_tokens": 2}),
                         {"input_tokens": 5, "output_tokens": 2})

    def test_invalid_or_absent_usage_is_unknown_not_zero(self):
        for usage in (None, [], {}, {"input_tokens": True}, {"total_tokens": -1},
                      {"total_tokens": 1.5}, {"total_tokens": 2 ** 54}):
            self.assertEqual(reported_usage(usage), {})

    def test_jev_total_has_one_normalization_boundary(self):
        self.assertEqual(reported_usage({"input_tokens": 5000, "output_tokens": 250}, "typesafe"),
                         {"input_tokens": 5000, "output_tokens": 250, "total_tokens": 5250})
        self.assertEqual(reported_usage({"input_tokens": 5000}, "typesafe"), {"input_tokens": 5000})

    def call(self, http, emit):
        return request_inference(http, "/route", {"secret": "never log this"}, 2, emit=emit,
            provider="google", call_id="helper:1", model="gemini-test", purpose="planning")

    def test_actual_provider_response_metadata_excludes_content(self):
        http = Mock()
        response = {"model": "gemini-actual", "usage": {"promptTokenCount": 4},
                    "choices": [{"message": {"content": "private answer"}}]}
        http.request.return_value = response
        events = []
        self.assertIs(self.call(http, events.append), response)
        self.assertEqual([e["event"] for e in events], ["inference_started", "inference_finished"])
        self.assertEqual(events[1]["model"], "gemini-actual")
        self.assertEqual(events[1]["usage"], {"input_tokens": 4})
        self.assertTrue(events[1]["success"])
        self.assertEqual(events[0]["hybrid_path"], "system_two")
        self.assertEqual(events[1]["hybrid_path"], "system_two")
        self.assertNotIn("never log this", str(events))
        self.assertNotIn("private answer", str(events))

    def test_jev_events_tag_system_one_path_without_changing_usage(self):
        http = Mock(location=None)
        http.request.return_value = {"model": "jev-latest", "usage": {"input_tokens": 10}}
        events = []
        request_inference(http, "/systemone", {}, 1, emit=events.append, provider="typesafe",
                          call_id="jev:1", model="jev-latest", purpose="decision")
        self.assertEqual([e["hybrid_path"] for e in events], ["system_one", "system_one"])
        self.assertEqual(events[1]["usage"], {"input_tokens": 10})
        # No observer: the raw provider contract is unchanged (no telemetry fields).
        bare = Mock()
        request_inference(bare, "/systemone", {}, 1, emit=None, provider="typesafe",
                          call_id="jev:1", model="jev-latest", purpose="decision")
        bare.request.assert_called_once_with("POST", "/systemone", {}, 1)

    def test_provider_failure_has_no_exception_body_or_fake_usage(self):
        http = Mock()
        http.request.side_effect = RuntimeError("sensitive response body")
        events = []
        with self.assertRaises(RuntimeError):
            self.call(http, events.append)
        self.assertFalse(events[-1]["success"])
        self.assertEqual(events[-1]["usage"], {})
        self.assertEqual(events[-1]["error_type"], "RuntimeError")
        self.assertNotIn("sensitive", str(events))
        self.assertEqual(http.request.call_count, 1)

    def test_start_journal_failure_prevents_provider_dispatch(self):
        http = Mock()
        with self.assertRaises(OSError):
            self.call(http, Mock(side_effect=OSError))
        http.request.assert_not_called()

    def test_no_observer_preserves_direct_request_contract(self):
        http = Mock()
        self.assertIs(self.call(http, None), http.request.return_value)
        http.request.assert_called_once_with("POST", "/route", {"secret": "never log this"}, 2)
