import ast
import functools
import unittest
from pathlib import Path

from mobile_agent import cable_bt, drivers, mirroring
from mobile_agent.frontier import OPERATIONS
from mobile_agent.tui import demo_phone

ROOT = Path(__file__).resolve().parents[1]
BASE = frozenset({"observe", "execute", "close", "can_type", "last_image"})
LEAKS = frozenset({
    ("lockscreen.py", "_hinted"),
    ("bench/actuation.py", "_write_rank"),
    ("bench/agents/smart.py", "_last_bundle"),
})


def _names_driver(node):
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
    return name.lstrip("_") == "driver"


@functools.lru_cache(maxsize=1)
def probes():
    found = set()
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(("tests/", "bench/tests/", ".venv/")) or "/.venv/" in relative:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("getattr", "hasattr") and len(node.args) >= 2
                    and _names_driver(node.args[0]) and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                found.add((relative, node.args[1].value))
    return frozenset(found)


class DriverContractTests(unittest.TestCase):
    def test_every_probe_is_declared(self):
        undeclared = sorted((path, name) for path, name in probes()
                            if name not in drivers.Driver.OPTIONAL and name not in BASE and (path, name) not in LEAKS)
        self.assertEqual([], undeclared, "declare these in drivers.Driver.OPTIONAL, with what their absence means")

    def test_leaks_only_shrink(self):
        self.assertEqual(sorted(LEAKS), sorted(LEAKS & probes()))

    def test_probes_are_found(self):
        found = {name for _, name in probes()}
        self.assertTrue({"capture_preview", "frame_clock", "write_text", "observe_ready"} <= found)

    def test_product_drivers_implement_the_core(self):
        for cls in (drivers.WDA, mirroring.MirroringDriver, demo_phone.DemoPhone):
            self.assertFalse(getattr(cls, "__abstractmethods__", None), cls.__name__)
        for name in ("tap_point", "long_press", "write_text", "set_value", "clear_text", "replace_text",
                     "observe_ready", "wait_for_change", "capture_preview", "active_app", "relaunch_app", "tune"):
            self.assertTrue(callable(getattr(drivers.WDA, name, None)), f"WDA.{name}")

    def test_image_only_drivers_say_what_they_cannot_do(self):
        for cls in (mirroring.MirroringDriver, cable_bt.CableBtDriver):
            for name in ("set_value", "write_text", "replace_text", "clear_text", "call"):
                self.assertNotIn(name, cls.capabilities, name)
            for name in ("tap_point", "long_press", "dismiss_at", "observe_ready", "wait_for_change", "active_app"):
                self.assertIn(name, cls.capabilities, name)


def minimal(**members):
    members = {"observe": lambda self, timeout=10: None, "execute": lambda self, *args, **kwargs: None,
               "close": lambda self: None, **members}
    return type("Minimal", (drivers.Driver,), members)


class DeclaredCapabilityTests(unittest.TestCase):
    def test_a_driver_that_declares_nothing_and_implements_nothing_builds(self):
        self.assertEqual(frozenset(), minimal()().capabilities)

    def test_a_driver_that_declares_what_it_implements_builds(self):
        driver = minimal(capabilities=frozenset({"long_press"}), long_press=lambda self, element, snapshot: None)()
        self.assertEqual(frozenset({"long_press"}), driver.capabilities)

    def test_declaring_a_name_the_driver_lacks_fails_to_build(self):
        with self.assertRaisesRegex(TypeError, r"declares \['long_press'\] it doesn't have"):
            minimal(capabilities=frozenset({"long_press"}))()

    def test_implementing_an_optional_name_without_declaring_it_fails_to_build(self):
        with self.assertRaisesRegex(TypeError, r"leaves out \['long_press'\] it has"):
            minimal(long_press=lambda self, element, snapshot: None)()

    def test_an_attribute_set_in_init_is_implemented_too(self):
        def init(self):
            self.http = object()

        with self.assertRaisesRegex(TypeError, r"leaves out \['http'\]"):
            minimal(__init__=init)()
        self.assertEqual(frozenset({"http"}), minimal(__init__=init, capabilities=frozenset({"http"}))().capabilities)

    def test_a_declared_name_must_be_one_of_driver_optional(self):
        with self.assertRaisesRegex(TypeError, r"declares \['swipe_hard'\]"):
            minimal(capabilities=frozenset({"swipe_hard"}), swipe_hard=lambda self: None)()

    def test_the_product_drivers_declare_the_optional_names_they_implement(self):
        for cls in (drivers.WDA, mirroring.MirroringDriver, cable_bt.CableBtDriver, demo_phone.DemoPhone):
            implemented = {name for name in drivers.Driver.OPTIONAL if hasattr(cls, name)}
            self.assertLessEqual(cls.capabilities, set(drivers.Driver.OPTIONAL), cls.__name__)
            self.assertLessEqual(implemented, cls.capabilities, cls.__name__)

    def test_a_wda_builds_with_what_it_declares(self):
        wda = drivers.WDA("http://127.0.0.1:1", "session")
        self.assertTrue({"url", "prefix", "http", "tap_point", "set_value"} <= wda.capabilities)
        wda.close()


class OperationsForTests(unittest.TestCase):
    """drivers.operations_for: one test for each rule, naming the capability that gates the operation."""

    def offered(self, *capabilities, role=None):
        return drivers.operations_for(frozenset(capabilities), OPERATIONS, role)

    def test_long_press_needs_long_press(self):
        self.assertNotIn("LONG_PRESS", self.offered())
        self.assertNotIn("LONG_PRESS", self.offered("tap_point", "dismiss_at", "write_text"))
        self.assertIn("LONG_PRESS", self.offered("long_press"))

    def test_tap_xy_needs_tap_point(self):
        self.assertNotIn("TAP_XY", self.offered())
        self.assertNotIn("TAP_XY", self.offered("long_press", "dismiss_at"))
        self.assertIn("TAP_XY", self.offered("tap_point"))

    def test_dismiss_needs_the_tap_it_goes_through(self):
        # FrontierAgent taps a DISMISS point through dismiss_at, else tap_point.
        self.assertNotIn("DISMISS", self.offered())
        self.assertNotIn("DISMISS", self.offered("long_press", "write_text"))
        self.assertIn("DISMISS", self.offered("tap_point"))
        self.assertIn("DISMISS", self.offered("dismiss_at"))

    def test_set_text_on_a_wheel_needs_set_value(self):
        self.assertNotIn("SET_TEXT", self.offered("clear_text", "replace_text", "write_text", role="PickerWheel"))
        self.assertIn("SET_TEXT", self.offered("set_value", role="PickerWheel"))

    def test_set_text_on_a_field_needs_nothing_beyond_execute(self):
        # Clearing and typing fall back to execute: no set_value, clear_text or replace_text needed.
        for role in (None, "TextField", "SearchField"):
            self.assertIn("SET_TEXT", self.offered(role=role))

    def test_every_other_operation_is_offered_to_a_driver_with_no_capabilities(self):
        # READ_LIST and SCROLL_TO swipe and read through execute and observe; TYPE, TYPE_SUBMIT and SUBMIT use
        # execute when write_text is absent.
        self.assertEqual([op for op in OPERATIONS if op not in {"LONG_PRESS", "TAP_XY", "DISMISS"}],
                         list(self.offered()))

    def test_a_driver_with_every_capability_keeps_the_whole_list_in_order(self):
        every = frozenset(drivers.Driver.OPTIONAL)
        self.assertEqual(OPERATIONS, drivers.operations_for(every, OPERATIONS))
        self.assertEqual(OPERATIONS, drivers.operations_for(every, OPERATIONS, "PickerWheel"))

    def test_an_operation_with_no_rule_passes_through(self):
        # A skill's operation (USE_CODE) is appended by the loop, not derived from a capability.
        self.assertEqual(("TAP", "USE_CODE"), drivers.operations_for(frozenset(), ("TAP", "USE_CODE")))

    def test_each_product_driver_is_offered_what_it_declares(self):
        self.assertEqual(OPERATIONS, drivers.operations_for(drivers.WDA.capabilities, OPERATIONS, "PickerWheel"))
        for cls in (mirroring.MirroringDriver, cable_bt.CableBtDriver):
            self.assertEqual(OPERATIONS, drivers.operations_for(cls.capabilities, OPERATIONS), cls.__name__)
            self.assertNotIn("SET_TEXT", drivers.operations_for(cls.capabilities, OPERATIONS, "PickerWheel"))
        self.assertEqual({"LONG_PRESS", "TAP_XY", "DISMISS"},
                         set(OPERATIONS) - set(drivers.operations_for(demo_phone.DemoPhone.capabilities, OPERATIONS)))

    def test_every_operation_offered_to_a_product_driver_is_backed(self):
        for cls in (drivers.WDA, mirroring.MirroringDriver, cable_bt.CableBtDriver, demo_phone.DemoPhone):
            for role in (None, "PickerWheel"):
                for op in drivers.operations_for(cls.capabilities, OPERATIONS, role):
                    needs = {"set_value"} if op == "SET_TEXT" and role else drivers.OPERATION_NEEDS.get(op, ())
                    self.assertTrue(not needs or any(callable(getattr(cls, name, None)) for name in needs),
                                    f"{cls.__name__} is offered {op} on {role} without {sorted(needs)}")


if __name__ == "__main__":
    unittest.main()
