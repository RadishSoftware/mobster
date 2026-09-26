"""Throttling is retried and accounted as infrastructure; answers are never re-requested."""

import unittest
from unittest import mock

from mobile_agent.bench.vertex import Vertex, cost_usd, pick_model
from mobile_agent.transport import TransportError


class FakeHTTP:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.key = ""
        self.calls = 0

    def request(self, method, path, body, timeout):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def vertex(outcomes):
    v = Vertex(project="demo-project-123", location="global", sleep=lambda s: None)
    http = FakeHTTP(outcomes)
    v._http = lambda: http
    return v, http


OK = {"candidates": [{"content": {"parts": [{"text": "OK"}]}}],
      "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 10, "thoughtsTokenCount": 90},
      "modelVersion": "gemini-3.7-flash"}


@mock.patch("mobile_agent.bench.vertex.GCloudToken.get", return_value="token")
class VertexTests(unittest.TestCase):
    def test_throttling_is_retried_and_reported(self, _):
        v, http = vertex([TransportError("HTTP 429; request not retried"), OK])
        response, info = v.generate("gemini-3.7-flash", {})
        self.assertEqual(http.calls, 2)
        self.assertEqual(info["attempts"], 2)
        self.assertGreaterEqual(info["throttle_ms"], 2000)
        self.assertAlmostEqual(info["cost_usd"], (1000 * 1.5 + 100 * 7.5) / 1e6)

    def test_other_errors_are_not_retried(self, _):
        v, http = vertex([TransportError("HTTP 400; request not retried"), OK])
        with self.assertRaises(TransportError):
            v.generate("gemini-3.7-flash", {})
        self.assertEqual(http.calls, 1)

    def test_retries_are_bounded(self, _):
        v, http = vertex([TransportError("HTTP 429; x")] * 10)
        with self.assertRaises(TransportError):
            v.generate("gemini-3.7-flash", {}, retries=2)
        self.assertEqual(http.calls, 3)

    def test_pick_model_skips_unavailable(self, _):
        class Probe:
            def probe(self, model):
                return (model == "b", "ok" if model == "b" else "429")
        self.assertEqual(pick_model(Probe(), ("a", "b", "c")), "b")
        self.assertIsNone(pick_model(Probe(), ("a", "c")))


class CostTests(unittest.TestCase):
    def test_unknown_model_or_usage_is_unpriced(self):
        self.assertIsNone(cost_usd("gemini-9", {"promptTokenCount": 1}))
        self.assertIsNone(cost_usd("gemini-3.7-flash", {}))
        self.assertAlmostEqual(cost_usd("gemini-3.1-pro-preview", {"promptTokenCount": 1_000_000}), 2.0)


if __name__ == "__main__":
    unittest.main()
