import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent.testkit.discover import Entry
from mobile_agent.testkit.suite import Suite
from mobile_agent.testkit.targets import DEFAULT, Refused
from mobile_agent.verify import conditions
from mobile_agent.verify.assertions import CheckError


class SpreadRefusalTests(unittest.TestCase):
    def test_many_refusals_preserve_reports_and_reach_the_next_runnable_check(self):
        refused_count = sys.getrecursionlimit() + 10
        names = [f"refused-{index}" for index in range(refused_count)] + ["accepted"]
        entries = [Entry(Path(f"{name}.yaml"), f"{name}.yaml", SimpleNamespace(name=name)) for name in names]

        def resolve(check, target, **kwargs):
            if check.name != "accepted":
                raise Refused("The target refuses this check.", "usage")
            return check, [], None

        attempt = Mock(return_value={"verdict": "passed"})
        with patch("mobile_agent.testkit.suite.for_target", side_effect=resolve):
            suite = Suite(entries, [DEFAULT], attempt=attempt, spread=True, cap=1)
            doc = suite.run()

        self.assertEqual(attempt.call_count, 1)
        self.assertEqual(attempt.call_args.args[0].name, "accepted")
        cells = [check["results"][0] for check in doc["checks"]]
        self.assertEqual(len(cells), refused_count + 1)
        self.assertTrue(all(cell["status"] == "couldnt_run" and cell["reason"]["class"] == "usage"
                            and cell["attempts"] == 0 for cell in cells[:-1]))
        self.assertEqual(cells[-1]["status"], "passed")
        self.assertEqual(doc["exitCode"], 3)
        self.assertEqual(suite._inflight, 0)


class CoordinateParsingTests(unittest.TestCase):
    def test_locations_and_waypoints_accept_the_same_coordinates(self):
        point = {"latitude": 52.52, "longitude": 13.405}
        self.assertEqual(conditions.parse({"location": "52.52,13.405"})["location"], point)
        route = conditions.parse({"route": {"waypoints": ["52.52,13.405", point]}})["route"]
        self.assertEqual(route["waypoints"], [point, point])

    def test_malformed_coordinates_name_the_original_field(self):
        for value in ("not-a-place", "north,east", "1,2,3"):
            for block, where in (({"location": value}, "conditions.location"),
                                 ({"route": {"waypoints": ["1,2", value]}}, "conditions.route.waypoints[1]")):
                with self.subTest(value=value, where=where), self.assertRaises(CheckError) as caught:
                    conditions.parse(block)
                self.assertEqual(str(caught.exception),
                                 f'{where} must be {{latitude: 52.52, longitude: 13.405}} or "52.52,13.405".')
