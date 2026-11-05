"""verify/check_schema.py: the check file's JSON Schema covers every key the loader reads, accepts every check the
loader reads (the docs' examples, the fixtures and tricky values), refuses the mistakes it can see, and the copy on
mobster.dev (site/public/schema/check.json) is current. Offline."""

import copy
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

from mobile_agent import links
from mobile_agent.tests.test_verify import PAYWALL
from mobile_agent.verify import assertions, check_schema, checks, conditions
from mobile_agent.verify.checks import CheckError, check_from_dict, dump_check, load_check

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen_check_schema.py"
DOCS = (ROOT / "docs" / "checks.mdx", ROOT / "docs" / "testing.mdx")
SCHEMA = check_schema.schema()
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
BASE = yaml.safe_load(PAYWALL)


def errors(data):
    return [error.message for error in VALIDATOR.iter_errors(data)]


def loads(data, base_dir=None):
    try:
        check_from_dict(copy.deepcopy(data), base_dir=base_dir)
    except CheckError:
        return False
    return True


def variant(**changes):
    """The paywall check with ``changes`` applied: a dotted key to a value, or to DROP to leave it out."""
    data = copy.deepcopy(BASE)
    for dotted, value in changes.items():
        *path, last = dotted.split("__")
        node = data
        for key in path:
            node = node.setdefault(key, {})
        if value is DROP:
            node.pop(last, None)
        else:
            node[last] = value
    return data


DROP = object()


class CoverageTests(unittest.TestCase):
    def test_it_is_a_valid_2020_12_schema_with_its_url(self):
        jsonschema.Draft202012Validator.check_schema(SCHEMA)
        self.assertEqual(SCHEMA["$id"], links.CHECK_SCHEMA)
        self.assertEqual(SCHEMA["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_every_key_the_loader_reads_has_a_schema(self):
        self.assertEqual(tuple(SCHEMA["else"]["properties"]), checks.FILE_KEYS)
        self.assertEqual(tuple(SCHEMA["then"]["properties"]), checks.TOOL_KEYS + checks.TOOL_ONLY)
        self.assertEqual([branch["required"] for branch in SCHEMA["if"]["anyOf"]],
                         [[key] for key in checks.TOOL_SHAPE_KEYS])
        defs = SCHEMA["$defs"]
        self.assertEqual(tuple(defs["selector"]["properties"]), assertions.SELECTOR_KEYS)
        self.assertEqual([item["$ref"] for item in defs["assertion"]["anyOf"]],
                         [f"#/$defs/assertion_{kind}" for kind in assertions.KINDS])
        condition = defs["conditions"]["anyOf"][0]["properties"]
        self.assertEqual(set(condition), set(conditions.KEYS))
        self.assertEqual(set(condition["status_bar"]["properties"]), set(conditions.STATUS_BAR_KEYS))

    def test_every_description_is_one_sentence_with_no_middle_dot(self):
        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("description"), str):
                    yield node["description"]
                for value in node.values():
                    yield from walk(value)
            elif isinstance(node, list):
                for value in node:
                    yield from walk(value)
        for text in walk(SCHEMA):
            self.assertNotIn("·", text)
            self.assertTrue(text.endswith("."), text)


class AcceptsWhatTheLoaderReadsTests(unittest.TestCase):
    def assert_accepted(self, data, base_dir=None):
        self.assertTrue(loads(data, base_dir), f"the loader refuses the example itself: {data}")
        self.assertEqual(errors(data), [], data)

    def test_the_fixture_and_the_file_mobster_saves(self):
        self.assert_accepted(BASE)
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / ".mobster").mkdir()
            path = Path(folder) / ".mobster" / "paywall.yaml"
            path.write_text(PAYWALL)
            text = dump_check(load_check(path), project_root=folder)
        self.assertTrue(text.startswith(f"# yaml-language-server: $schema={links.CHECK_SCHEMA}\n"), text[:120])
        self.assert_accepted(yaml.safe_load(text))

    def test_every_check_in_the_docs(self):
        """Each YAML block in docs/checks.mdx and docs/testing.mdx that the loader reads, with an app added to a
        fragment that has none, and the files a conditions block names made in a project folder."""
        read = 0
        for doc in DOCS:
            if not doc.is_file():
                self.skipTest("the docs are not part of this copy")
            for block in re.findall(r"```yaml[^\n]*\n(.*?)```", doc.read_text(), re.S):
                data = yaml.safe_load(block)
                if not isinstance(data, dict) or not set(data) <= set(checks.FILE_KEYS):
                    continue
                data.setdefault("app", {"bundle": "org.mozilla.ios.Firefox"})
                with tempfile.TemporaryDirectory() as folder:
                    named = (data.get("conditions") or {})
                    for name in [named.get("push"), *(named.get("media") or [])]:
                        if isinstance(name, str):
                            (Path(folder) / name).parent.mkdir(parents=True, exist_ok=True)
                            (Path(folder) / name).write_text(json.dumps({"aps": {"alert": "x"}}))
                    if loads(data, folder):
                        read += 1
                        with self.subTest(doc=doc.name, block=block[:60]):
                            self.assertEqual(errors(data), [])
        self.assertGreaterEqual(read, 3, "fewer docs examples than expected were checks")

    def test_values_the_loader_reads_that_a_schema_could_easily_refuse(self):
        for data in (
            variant(name=None), variant(name="  padded  "), variant(device=None), variant(reset=None),
            variant(version=1.0), variant(version=DROP), variant(launch=None), variant(budget=None),
            variant(budget__max_usd=None), variant(budget__max_seconds=600), variant(expect=None),
            variant(steps="One step as a string"), variant(steps=None),
            variant(fixture="signed-in"), variant(fixture=["signed-in", "feed"]),
            variant(schedule="every 1h"), variant(schedule=None),
            variant(conditions={"increase_contrast": "enabled", "route": {"waypoints": ["52,13", "53,14"]},
                                "status_bar": {"wifi_mode": "active", "cellular_mode": "notSupported"}}),
            variant(tags="smoke"), variant(tags=[" smoke ", "smoke", "paywall_2"]),
            variant(app__bundle="  org.mozilla.ios.Firefox  "), variant(app__bundle=""), variant(app__path=None),
            variant(launch__url="  firefox://paywall  "), variant(launch__url=""), variant(launch__env={}),
            variant(expect=[{"text": ["Choose your plan", "Restore"], "name": "Plans"}]),
            variant(expect=[{"visible": {"role": "XCUIElementTypeButton"}}, {"visible": {"role": "button"}},
                            {"visible": {"role": "statictext"}}, {"absent": {"enabled": False}}]),
            variant(expect=[{"value": {"id": "toggle"}, "equals": True}, {"value": {"id": "n"}, "equals": 3.5}]),
            variant(expect=[{"count": {"label": "/Plan/i"}, "at_least": 0, "name": None}]),
            variant(throughout=[{"no_text": ["Lorem", "/ipsum/i"]}, {"absent": {"role": "alert"}}]),
            variant(somewhere=[{"text": "Restore Purchases"}]),
            variant(audit="a11y"), variant(audit=["usage-descriptions", "a11y"]), variant(audit=[]),
            variant(a11y=True), variant(a11y=False), variant(a11y="strict"), variant(a11y=None),
            variant(conditions={"appearance": "dark", "locale": "zh-Hans-CN", "location": "52.52,13.405",
                                "content_size": "accessibility-medium", "permissions": {"photos": "grant"},
                                "status_bar": {"time": "9:41", "battery_level": 100, "data_network": "5g"}}),
            variant(conditions={"location": {"latitude": -90, "longitude": 180}}), variant(conditions=None),
            {"bundle_id": "org.mozilla.ios.Firefox", "steps": ["Open it"], "expect": [{"text": "Hi"}], "image": "x"},
            {"app_path": "/tmp/Firefox.app", "bundle_id": None, "launch_args": ["-A"], "max_usd": 1},
        ):
            with self.subTest(data=data):
                self.assert_accepted(data)


class RefusesWhatItCanSeeTests(unittest.TestCase):
    def test_mistakes_the_loader_also_refuses(self):
        for label, data in (
            ("an unknown top-level key", variant(expectations=[])),
            ("a typo in app", variant(app__bundel="x.y")),
            ("no app", variant(app=DROP)),
            ("an app with neither bundle nor path", variant(app={"bundle": "  "})),
            ("version 2", variant(version=2)),
            ("a bad reset", variant(reset="wipe")),
            ("21 steps", variant(steps=["Tap"] * 21)),
            ("a blank step", variant(steps=["Tap", "  "])),
            ("an unknown assertion kind", variant(expect=[{"exists": {"id": "x"}}])),
            ("two kinds in one assertion", variant(expect=[{"text": "A", "visible": {"id": "x"}}])),
            ("a bound on text", variant(expect=[{"text": "A", "equals": 1}])),
            ("value without equals", variant(expect=[{"value": {"id": "x"}}])),
            ("count with two bounds", variant(expect=[{"count": {"id": "x"}, "at_least": 1, "at_most": 3}])),
            ("a negative count", variant(expect=[{"count": {"id": "x"}, "equals": -1}])),
            ("an empty selector", variant(expect=[{"visible": {}}])),
            ("an unknown selector field", variant(expect=[{"visible": {"name": "x"}}])),
            ("text in throughout", variant(throughout=[{"text": "A"}])),
            ("an unknown audit", variant(audit=["contrast"])),
            ("a bad tag", variant(tags=["Smoke Test"])),
            ("11 tags", variant(tags=[f"t{n}" for n in range(11)])),
            ("a bad env key", variant(launch__env={"lower": "x"})),
            ("a url with no scheme", variant(launch__url="paywall")),
            ("a budget over the cap", variant(budget__max_usd=2)),
            ("network conditions", variant(conditions={"network": "3g"})),
            ("an unknown appearance", variant(conditions={"appearance": "sepia"})),
            ("a bad locale", variant(conditions={"locale": "German"})),
            ("an empty conditions block", variant(conditions={})),
            ("tags in the tool shape", {"bundle_id": "a.b", "tags": ["smoke"]}),
            ("the two shapes mixed", variant(bundle_id="a.b")),
        ):
            with self.subTest(label):
                self.assertFalse(loads(data), f"the loader reads {label}, so the schema must not refuse it")
                self.assertNotEqual(errors(data), [], f"the schema doesn't refuse {label}")


@unittest.skipUnless(SCRIPT.is_file() and (ROOT / "site").is_dir(), "the site is not part of this copy")
class PublishedCopyTests(unittest.TestCase):
    def test_the_committed_schema_is_current(self):
        spec = importlib.util.spec_from_file_location("gen_check_schema", SCRIPT)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        self.assertTrue(script.OUT.exists(), "run python scripts/gen_check_schema.py")
        self.assertEqual(script.OUT.read_text(), script.build(), "run python scripts/gen_check_schema.py")
        self.assertEqual(json.loads(script.OUT.read_text()), SCHEMA)


if __name__ == "__main__":
    unittest.main()
