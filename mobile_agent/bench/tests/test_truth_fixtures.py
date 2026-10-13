"""Ground-truth reading helpers and the fixture manifest."""

import json
import tempfile
import unittest
from pathlib import Path

from mobile_agent.bench.fixtures import IMAGE_SETS, Fixtures
from mobile_agent.bench.tests.fakes import element, snapshot
from mobile_agent.bench.truth import (Navigator, TruthStore, capture_from_reset, read, row_value, switch_value,
                                      wifi_state)
from mobile_agent.evals.oracles import ProbeUnavailable


class ReadTests(unittest.TestCase):
    def test_row_value_from_the_cell_or_the_text_beside_it(self):
        snap = snapshot([element("Bluetooth", value="On", rect=(0, .3, 1, .05)),
                         element("Auto-Lock", role="StaticText", rect=(.05, .4, .3, .05)),
                         element("30 seconds", role="StaticText", rect=(.6, .4, .3, .05))])
        self.assertEqual(row_value(snap, "Bluetooth"), "On")
        self.assertEqual(row_value(snap, "Auto-Lock"), "30 seconds")
        self.assertIsNone(row_value(snap, "Nope"))

    def test_a_value_that_repeats_the_label_is_not_the_value(self):
        # iOS 26 About: the cell's own value echoes its label; the value is the text beside it.
        snap = snapshot([element("iOS Version", value="iOS Version", rect=(.05, .3, .4, .05)),
                         element("26.0.1", role="StaticText", rect=(.7, .3, .2, .05))])
        self.assertEqual(row_value(snap, "iOS Version"), "26.0.1")

    def test_switch_and_wifi(self):
        snap = snapshot([element("Airplane Mode", role="Switch", value="0"),
                         element("Wi-Fi", value="HomeNet", rect=(0, .5, 1, .05))])
        self.assertEqual(switch_value(snap, "Airplane Mode"), "off")
        self.assertEqual(wifi_state(snap), "on")
        self.assertEqual(wifi_state(snapshot([element("Wi-Fi", value="Off")])), "off")
        self.assertEqual(read("title", snapshot(title="About")), "About")

    def test_capture_from_reset_only_on_settings_root(self):
        snap = snapshot([element("Bluetooth", value="Off"), element("Airplane Mode", role="Switch", value="1")])
        self.assertEqual(capture_from_reset(snap, "com.apple.Preferences"),
                         {"settings.bluetooth": "Off", "settings.airplane": "on"})
        self.assertEqual(capture_from_reset(snap, "com.apple.mobilesafari"), {})


class StoreTests(unittest.TestCase):
    def test_seeds_unconfirmed_save_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TruthStore("iphone15pro", Path(tmp) / "t.json")
            self.assertEqual(store.get("device.ios_version"), "26.0.1")
            self.assertIn("title.about", store.unconfirmed())
            before = store.digest()
            store.set("title.about", "About", "capture: test")
            self.assertNotIn("title.about", store.unconfirmed())
            store.set("settings.region", "United States", "capture: test")
            self.assertNotEqual(before, store.digest())
            store.save()
            again = TruthStore("iphone15pro", Path(tmp) / "t.json")
            self.assertEqual(again.get("settings.region"), "United States")


class NavigatorTests(unittest.TestCase):
    def test_walk_taps_rows_by_label_and_never_switches(self):
        root = snapshot([element("Bluetooth", role="Switch", value="1"), element("General", rect=(0, .5, 1, .05))])
        general = snapshot([element("About", rect=(0, .3, 1, .05))], title="General")
        about = snapshot([element("iOS Version", value="26.0.1")], title="About")

        class Driver:
            def __init__(self):
                self.screens, self.taps = [root, general, about], []

            def observe_ready(self, timeout=10):
                return self.screens[0]

            def execute(self, op, target, snap, timeout=10):
                self.taps.append((op, getattr(target, "label", None)))
                if op == "TAP":
                    self.screens.pop(0)

            def wait_for_change(self, snap, timeout=3):
                return self.screens[0]

            def close(self):
                pass

        driver = Driver()

        class Probe:
            def reset(self, bundle, url=None):
                return root

            def driver(self):
                return driver

        final = Navigator(Probe()).walk("com.apple.Preferences", ("General", "About"))
        self.assertEqual(read("row_value", final, "iOS Version"), "26.0.1")
        self.assertEqual(driver.taps, [("TAP", "General"), ("TAP", "About")])
        with self.assertRaises(ProbeUnavailable):
            Navigator(Probe(), max_swipes=1).walk("com.apple.Preferences", ("Bluetooth",))


class FixtureTests(unittest.TestCase):
    def test_selection_is_deterministic_and_names_are_neutral(self):
        a, b = Fixtures(), Fixtures()
        self.assertEqual(a.frozen(), b.frozen())
        for spec, rows in a.sets.values():
            for row in rows:
                self.assertRegex(row["file"], rf"^{spec.folder}-\d\d\.jpg$")
        self.assertEqual(a.value("files.dogs12", "positive_count"), 5)
        self.assertEqual(len(a.value("files.eyes8", "files")), 8)
        self.assertEqual(sum(1 for v in a.value("files.eyes8", "labels").values() if v == "unsure"), 4)

    def test_build_keeps_the_answer_key_off_the_phone(self):
        with tempfile.TemporaryDirectory() as tmp:
            Fixtures().build(tmp)
            phone = Path(tmp) / "copy-to-phone" / "MobsterBench"
            self.assertEqual(sorted(p.name for p in phone.iterdir()), sorted(s.folder for s in IMAGE_SETS))
            self.assertFalse(list(phone.rglob("*.json")))
            labels = json.loads((Path(tmp) / "labels-do-not-copy.json").read_text())
            self.assertIn("files.dogs12", labels["image_sets"])
            self.assertIn("MobsterBench Dogs", (Path(tmp) / "SETUP.txt").read_text())


if __name__ == "__main__":
    unittest.main()
