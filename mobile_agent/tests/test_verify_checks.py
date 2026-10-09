"""verify/checks.py: the check file and tool shapes, every error, limits, paths and the draft round trip. Offline."""

import json
from pathlib import Path
import re
import tempfile
import unittest

from mobile_agent.verify.checks import (Check, CheckError, check_from_dict, dump_check, load_check,
                                        project_root)

PAYWALL = """\
# .mobster/checks/paywall.yaml
version: 1
name: A new user sees three plans on the paywall
app:
  bundle: dev.mobster.daybreak
  path: .mobster/build/Build/Products/Debug-iphonesimulator/Daybreak.app
device: iPhone 17 Pro
runtime: iOS 26.4
reset: reinstall
launch:
  args: ["-DaybreakSkipOnboarding", "YES"]
  env: { DAYBREAK_SEED: "3" }
  url: daybreak://paywall
steps:
  - Complete onboarding as a new user. Don't allow notifications.
  - Reach the paywall.
expect:
  - text: Choose your plan
  - count: { id: /^plan_/ }
    equals: 3
  - value: { id: plan_annual }
    equals: $39.99 / year
  - visible: { label: Restore Purchases, role: button }
  - no_text: [Loading, Error]
budget: { max_seconds: 180, max_usd: 0.25 }
"""


def minimal(**extra):
    return {"app": {"bundle": "dev.mobster.daybreak"}, **extra}


class FileShapeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name).resolve()
        (self.root / ".mobster" / "checks").mkdir(parents=True)

    def tearDown(self):
        self.folder.cleanup()

    def write(self, text, name="paywall.yaml", where=".mobster/checks"):
        path = self.root / where / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_the_spec_example(self):
        path = self.write(PAYWALL)
        check = load_check(path)
        self.assertEqual(check.name, "A new user sees three plans on the paywall")
        self.assertEqual(check.bundle_id, "dev.mobster.daybreak")
        self.assertEqual(check.app_path, str(self.root / ".mobster/build/Build/Products/Debug-iphonesimulator/"
                                                         "Daybreak.app"))
        self.assertEqual((check.device, check.runtime, check.reset), ("iPhone 17 Pro", "iOS 26.4", "reinstall"))
        self.assertEqual(check.launch_args, ("-DaybreakSkipOnboarding", "YES"))
        self.assertEqual(check.launch_env, {"DAYBREAK_SEED": "3"})
        self.assertEqual(check.open_url, "daybreak://paywall")
        self.assertEqual(len(check.steps), 2)
        self.assertEqual([a.kind for a in check.expect], ["text", "count", "value", "visible", "no_text", "no_text"])
        self.assertEqual((check.max_seconds, check.max_usd), (180, 0.25))
        self.assertEqual(check.source, str(path.resolve()))

    def test_the_project_root_is_the_folder_holding_dot_mobster(self):
        self.assertEqual(project_root(self.root / ".mobster" / "checks" / "x.yaml"), self.root)
        deep = self.write("app: {bundle: a.b, path: App.app}\n", "x.yaml", "sub/dir")
        self.assertEqual(load_check(deep).app_path, str(self.root / "App.app"))
        with tempfile.TemporaryDirectory() as other:
            loose = Path(other).resolve() / "check.yaml"
            loose.write_text("app: {path: Build/App.app}\n")
            self.assertEqual(load_check(loose).app_path, str(Path(other).resolve() / "Build" / "App.app"))

    def test_json_files(self):
        path = self.write(json.dumps({"app": {"bundle": "com.apple.Preferences"}, "expect": [{"text": "General"}]}),
                          "settings.json")
        self.assertEqual(load_check(path).expect[0].to_dict(), {"text": "General"})

    def test_file_errors(self):
        cases = [("app: {bundle: a.b\n", "line 2"), ("", "is empty"), ("- a\n- b\n", "must be an object"),
                 ("app: {bundle: a.b}\nexpect: [{txt: Hi}]\n", "did you mean text"),
                 ("app: {bundle: a.b}\nlaunch: {args: [-Flag, YES]}\n", r"launch\.args\[1\] must be text; quote it")]
        for text, message in cases:
            with self.subTest(text=text):
                with self.assertRaisesRegex(CheckError, message):
                    load_check(self.write(text, "bad.yaml"))
        with self.assertRaisesRegex(CheckError, "does not exist"):
            load_check(self.root / "missing.yaml")
        big = self.write("# " + "x" * 1_000_001, "big.yaml")
        with self.assertRaisesRegex(CheckError, "larger than 1 MB"):
            load_check(big)

    def test_the_draft_round_trip(self):
        check = load_check(self.write(PAYWALL))
        text = dump_check(check, project_root=self.root)
        self.assertIn("path: .mobster/build/Build/Products/Debug-iphonesimulator/Daybreak.app", text)
        self.assertTrue(text.startswith("# A Mobster check."))
        again = load_check(self.write(text, "draft.yaml", ".mobster/runs/20261001-101500-3f9a"))
        self.assertEqual(again.__dict__ | {"source": None}, check.__dict__ | {"source": None})

    def test_round_trips_of_tricky_values(self):
        check = check_from_dict({
            "app_path": str(self.root / "Build" / "Weird App.app"), "bundle_id": "dev.mobster.daybreak",
            "steps": ["Open: the \"paywall\"", "yes"], "launch_args": ["YES", "-1", "on"],
            "launch_env": {"FLAG": "NO", "COUNT": "007"}, "open_url": "daybreak://x?a=1&b=2", "max_usd": 0.5,
            "max_seconds": 90, "expect": [{"value": {"id": "t"}, "equals": True},
                                          {"value": {"id": "n"}, "equals": 39.99},
                                          {"value": {"id": "s"}, "equals": "1"},
                                          {"text": "/^(Yes|No)$/i"}, {"absent": {"enabled": False, "role": "button"}},
                                          {"count": {"label": "null"}, "at_most": 0}]})
        for root in (self.root, None):
            with self.subTest(root=root):
                path = self.write(dump_check(check, project_root=root), "tricky.yaml")
                self.assertEqual(load_check(path).__dict__ | {"source": None}, check.__dict__)


class ToolShapeTests(unittest.TestCase):
    def test_mcp_arguments(self):
        check = check_from_dict({"app_path": "/abs/Daybreak.app", "steps": ["Reach the paywall."],
                                 "expect": [{"text": "Choose your plan"}], "launch_args": ["-A"],
                                 "launch_env": {"X": "1"}, "open_url": "daybreak://paywall", "max_usd": 0.1,
                                 "image": True, "reset": "data"})
        self.assertEqual(check.app_path, "/abs/Daybreak.app")
        self.assertIsNone(check.bundle_id)
        self.assertEqual(check.name, "Reach the paywall.")
        self.assertEqual((check.max_usd, check.max_seconds, check.reset), (0.1, 180, "data"))

    def test_names_default(self):
        self.assertEqual(check_from_dict({"bundle_id": "a.b"}).name, "Launch check")
        long = check_from_dict({"bundle_id": "a.b", "steps": ["x" * 300]}).name
        self.assertEqual(len(long), 120)
        self.assertTrue(long.endswith("…"))

    def test_shapes_do_not_mix(self):
        with self.assertRaisesRegex(CheckError, "mixes the file format"):
            check_from_dict({"app": {"bundle": "a.b"}, "bundle_id": "a.b"})
        with self.assertRaisesRegex(CheckError, "needs an app"):
            check_from_dict({"steps": ["x"]})
        with self.assertRaisesRegex(CheckError, "did you mean bundle_id"):
            check_from_dict({"bundle_idd": "a.b"})


class ErrorTests(unittest.TestCase):
    def test_a_file_nested_too_deeply(self):
        # Review round 3: 100,000 nested brackets raised RecursionError past load_check.
        with tempfile.TemporaryDirectory() as folder:
            for suffix in (".json", ".yaml"):
                with self.subTest(suffix=suffix):
                    path = Path(folder) / f"deep{suffix}"
                    path.write_text("[" * 100_000 + "]" * 100_000)
                    with self.assertRaisesRegex(CheckError, "nested too deeply"):
                        load_check(path)

    def test_every_rule(self):
        cases = [
            (minimal(nmae="x"), "unknown key 'nmae'; did you mean name"),
            ({"app": {"bundel": "a.b"}}, "app has an unknown key 'bundel'; did you mean bundle"),
            (minimal(launch={"arg": []}), "did you mean args"),
            (minimal(budget={"max_dollars": 1}), "unknown key 'max_dollars'"),
            ({"app": {}}, "needs app.bundle or app.path"),
            ({"app": {"bundle": "not a bundle"}}, "not a bundle ID"),
            (minimal(version=2), "version 2 is not supported"),
            (minimal(version=True), "not supported"),
            (minimal(name="x" * 121), "longer than 120"),
            (minimal(name=3), "name must be text"),
            (minimal(steps=["s"] * 21), "more than 20 steps"),
            (minimal(steps=["x" * 501]), r"steps\[0\] is longer than 500"),
            (minimal(steps=[""]), r"steps\[0\] is empty"),
            (minimal(steps={"a": 1}), "list of plain-English steps"),
            (minimal(expect=[{"text": f"t{i}"} for i in range(51)]), "more than 50 assertions"),
            (minimal(expect={"text": "a"}), "expect must be a list"),
            (minimal(launch={"args": ["a"] * 21}), "more than 20 items"),
            (minimal(launch={"env": {f"K{i}": "v" for i in range(21)}}), "more than 20 entries"),
            (minimal(launch={"env": {"lower": "v"}}), "not a variable name"),
            (minimal(launch={"env": {"1X": "v"}}), "not a variable name"),
            (minimal(launch={"env": {"X": 3}}), "must be text; quote it"),
            (minimal(launch={"url": "x" * 2001}), "longer than 2,000"),
            (minimal(launch={"url": "paywall"}), "needs? a scheme|with a scheme"),
            (minimal(reset="all"), "reset must be one of none, data, reinstall"),
            (minimal(budget={"max_usd": 1.5}), "at most 1"),
            (minimal(budget={"max_usd": 0}), "more than 0"),
            (minimal(budget={"max_usd": True}), "must be a number"),
            (minimal(budget={"max_seconds": 601}), "at most 600"),
            (minimal(app="a.b"), "app must be an object"),
            ("check", "must be an object"),
        ]
        for data, message in cases:
            with self.subTest(data=str(data)[:60]):
                with self.assertRaisesRegex(CheckError, message):
                    check_from_dict(data)

    def test_reset_levels(self):
        def level(**kwargs):
            return Check(name="n", steps=(), expect=(), device=None, runtime=None, launch_args=(), **kwargs)
        self.assertEqual(level(app_path="/a.app", bundle_id=None, reset="auto").reset_level(), "reinstall")
        self.assertEqual(level(app_path=None, bundle_id="a.b", reset="auto").reset_level(), "data")
        self.assertEqual(level(app_path=None, bundle_id="a.b", reset="none").reset_level(), "none")
        self.assertEqual(level(app_path=None, bundle_id="com.apple.Preferences", reset="data").reset_level(), "none")


class NoticesTests(unittest.TestCase):
    """PyYAML came in with check files, and the release tarball ships THIRD_PARTY_NOTICES.md: every pin in
    requirements.txt has a row there at the same version."""

    def test_every_pinned_dependency_has_a_notice(self):
        root = Path(__file__).resolve().parents[2]
        requirements, notices = root / "mobile_agent" / "requirements.txt", root / "THIRD_PARTY_NOTICES.md"
        if not (requirements.is_file() and notices.is_file()):
            self.skipTest("no source checkout")

        def key(name):
            return re.sub(r"[-_.]+", "-", name.strip()).lower()
        pins = dict(line.split("==", 1) for line in requirements.read_text().splitlines()
                    if "==" in line and not line.lstrip().startswith("#"))
        rows = {}
        for line in notices.read_text().splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if line.startswith("| ") and len(cells) >= 3:
                rows[key(cells[0])] = cells[1]
        self.assertIn("PyYAML", pins)
        self.assertEqual({key(name): version.strip() for name, version in pins.items() if rows.get(key(name)) !=
                          version.strip()}, {}, "pins with no row, or another version, in THIRD_PARTY_NOTICES.md")


if __name__ == "__main__":
    unittest.main()
