import unittest

from mobile_agent.bench.metrics import bootstrap_ci, flaky_tasks, pass_hat_k, percentile, sign_test, wilson


class MetricsTests(unittest.TestCase):
    def test_wilson_known_values(self):
        low, high = wilson(8, 10)
        self.assertAlmostEqual(low, .4902, places=3)
        self.assertAlmostEqual(high, .9433, places=3)
        low, high = wilson(0, 10)
        self.assertEqual(low, 0)
        self.assertAlmostEqual(high, .2775, places=3)
        self.assertTrue(all(v != v for v in wilson(0, 0)))

    def test_percentile(self):
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(percentile([5], 90), 5)
        self.assertIsNone(percentile([None], 50))

    def test_pass_hat_k_and_flaky(self):
        outcomes = {"a": [True, True, True], "b": [True, False, False], "c": [False, False, False]}
        self.assertAlmostEqual(pass_hat_k(outcomes, 1), (1 + 1 / 3 + 0) / 3)
        self.assertAlmostEqual(pass_hat_k(outcomes, 3), 1 / 3)
        self.assertEqual(flaky_tasks(outcomes), ["b"])

    def test_sign_test(self):
        self.assertEqual(sign_test(0, 0), 1.0)
        self.assertAlmostEqual(sign_test(9, 1), 0.02148, places=4)

    def test_bootstrap_is_deterministic(self):
        values = [0, 0, 1, 1, 1, .5]
        self.assertEqual(bootstrap_ci(values, resamples=500), bootstrap_ci(values, resamples=500))
        mean, low, high = bootstrap_ci(values, resamples=2000)
        self.assertLessEqual(low, mean)
        self.assertLessEqual(mean, high)


if __name__ == "__main__":
    unittest.main()
