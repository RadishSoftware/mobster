"""The iOSWorld harness refuses a simulator whose apps live in another simulator's containers. Offline."""

import subprocess
import unittest
from unittest import mock

from mobile_agent.bench import iosworld

MANIFEST = {"notes": {"bundle_id": "com.iosworld.benchmark.notes"}, "clock": {"bundle_id": "com.iosworld.benchmark.clock"},
            "meta": {}}


def simctl(paths):
    """A fake ``simctl get_app_container``: paths[(bundle, kind)], or not installed."""
    def run(command, **_):
        path = paths.get((command[4], command[5]))
        return subprocess.CompletedProcess(command, 0 if path else 2, stdout=(path or "") + "\n", stderr="")
    return run


class IsolationTest(unittest.TestCase):
    def device(self, udid, rest):
        return str(iosworld.SIM_DEVICES / udid / "data/Containers" / rest)

    def test_own_containers_pass(self):
        paths = {(b, k): self.device("SIM-B", f"{k}/{b}") for b in ("com.iosworld.benchmark.notes",
                                                                     "com.iosworld.benchmark.clock") for k in ("app", "data")}
        with mock.patch.object(iosworld.subprocess, "run", simctl(paths)):
            self.assertEqual(iosworld.foreign_containers("SIM-B", MANIFEST), [])
            iosworld.check_isolation("SIM-B", MANIFEST)

    def test_clone_resolving_to_its_source_is_refused(self):
        paths = {("com.iosworld.benchmark.notes", "data"): self.device("SIM-A", "Data/notes"),
                 ("com.iosworld.benchmark.clock", "data"): self.device("SIM-A", "Data/clock")}
        with mock.patch.object(iosworld.subprocess, "run", simctl(paths)):
            foreign = iosworld.foreign_containers("SIM-B", MANIFEST)
            self.assertEqual([b for b, _ in foreign], ["com.iosworld.benchmark.notes", "com.iosworld.benchmark.clock"])
            with self.assertRaises(SystemExit) as raised:
                iosworld.check_isolation("SIM-B", MANIFEST)
            self.assertIn("not isolated", str(raised.exception))

    def test_a_udid_prefix_is_not_the_device(self):
        paths = {("com.iosworld.benchmark.notes", "data"): self.device("SIM-B2", "Data/notes")}
        with mock.patch.object(iosworld.subprocess, "run", simctl(paths)):
            self.assertEqual(len(iosworld.foreign_containers("SIM-B", MANIFEST)), 1)


if __name__ == "__main__":
    unittest.main()
