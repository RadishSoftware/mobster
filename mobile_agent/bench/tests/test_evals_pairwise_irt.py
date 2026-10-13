"""The pairwise step judge and the IRT fit. Offline: the judge is faked; no device, model or network."""

import unittest
from unittest import mock

from mobile_agent.bench.evals import models as M


class PairwiseTests(unittest.TestCase):
    def test_a_verdict_counts_only_when_both_orders_agree(self):
        from mobile_agent.bench.evals import pairwise as P
        item = {"text": 'Screen elements:\ne1 Button "Send" @0,0,1,1', "image": None, "id": "x"}
        ctx = {"judge": "j", "spend": None, "set_dir": None, "image": False}
        def fake(answers):
            it = iter(answers)
            return lambda *a, **k: M.Reply(out={"better": next(it), "reason": ""}, usd=.001, seconds=.1)
        cand, rec = {"op": "TAP", "label": "Send", "target": "e1"}, {"op": "DONE", "label": "", "text": "x"}
        with mock.patch.object(M, "ask", fake(["A", "B"])):
            self.assertEqual(P.judge_pair(item, cand, rec, ctx)["verdict"], "candidate")
        with mock.patch.object(M, "ask", fake(["A", "A"])):  # position bias: both say "first"
            out = P.judge_pair(item, cand, rec, ctx)
            self.assertEqual((out["verdict"], out["consistent"]), ("tie", False))
        self.assertEqual(P.describe(cand, {}), 'TAP e1 "Send"')


class IRTTests(unittest.TestCase):
    def test_the_fit_orders_tasks_by_difficulty_and_engines_by_ability(self):
        import random
        from mobile_agent.bench.evals import irt
        rng = random.Random(3)
        truth_theta = {f"p{j}": -1.5 + j * .5 for j in range(7)}
        truth_b = {"easy": -1.5, "mid": 0.0, "hard": 1.5}
        cells = {}
        for p, th in truth_theta.items():
            for t, b in truth_b.items():
                n = 12
                s = sum(rng.random() < irt.sigmoid(1.5 * (th - b)) for _ in range(n))
                cells[(p, t)] = (s, n)
        params = irt.fit(cells, iterations=800)
        self.assertLess(params["b"]["easy"], params["b"]["mid"])
        self.assertLess(params["b"]["mid"], params["b"]["hard"])
        self.assertLess(params["theta"]["p0"], params["theta"]["p6"])
        best = irt.informative(params, ["easy", "mid", "hard"], params["theta"]["p3"], 1)
        self.assertEqual(best, ["mid"])
        self.assertGreater(irt.ability({"mid": (10, 12)}, params), irt.ability({"mid": (2, 12)}, params))


if __name__ == "__main__":
    unittest.main()


class SyntheticPairTests(unittest.TestCase):
    def test_known_pairs_are_a_random_other_tap_or_an_early_done(self):
        from mobile_agent.bench.evals import pairwise as P
        text = ('Screen elements:\ne1 Button "Send" @0,0,10,10\ne2 Button "Cancel" @0,20,10,10\n'
                'e3 Button "Attach" @0,40,10,10')
        items = [{"id": f"i{n}", "kind": "path", "text": text, "alternatives": [],
                  "gold": {"op": "TAP", "label": "Send", "text": None, "app": None, "target": "e1"}} for n in range(4)]
        pairs = P.synthetic_pairs(items, 4)
        self.assertEqual(len(pairs), 4)
        taps = [bad for _, bad, _ in pairs if bad["op"] == "TAP"]
        self.assertTrue(taps and all(b["target"] in ("e2", "e3") for b in taps))
        self.assertTrue(any(bad["op"] == "DONE" for _, bad, _ in pairs))


class ProxyTests(unittest.TestCase):
    def test_rank_correlation_needs_enough_hypotheses_with_a_paired_ab(self):
        from mobile_agent.bench.evals import proxy
        entries = [{"hypothesis": f"h{i}", "proxies": {"l1:x": x}, "l3": {"pass_delta": y}}
                   for i, (x, y) in enumerate([(.1, .02), (.3, .05), (.2, .04), (.5, .09)])]
        entries.append({"hypothesis": "stopped", "proxies": {"l1:x": .9}})
        report = proxy.validity(entries, minimum=4)
        self.assertEqual((report["with_l3"], report["stopped_early"]), (4, 1))
        self.assertEqual(report["proxies"][0]["spearman"], 1.0)
        self.assertNotIn("spearman", proxy.validity(entries[:3], minimum=4)["proxies"][0])
        self.assertEqual(proxy.ranks([3, 1, 1]), [3.0, 1.5, 1.5])
