"""Driver contract, factory selection, and composition wiring. No devices."""

import os
import unittest
from unittest.mock import patch

from mobile_agent.compose import build_models, build_target_driver, build_visual, close_all
from mobile_agent.demo import DemoDriver
from mobile_agent.drivers import DRIVERS, Driver, WDA, build_driver, register_driver


class DriverContractTests(unittest.TestCase):
    def test_every_driver_implements_the_contract(self):
        for cls in (WDA, DemoDriver):
            self.assertTrue(issubclass(cls, Driver), cls.__name__)

    def test_contract_cannot_be_partially_implemented(self):
        class Partial(Driver):
            def observe(self, timeout=10):
                pass

        with self.assertRaises(TypeError):
            Partial()

    def test_text_entry_is_advertised_honestly(self):
        self.assertTrue(WDA.can_type)
        self.assertFalse(Driver.can_type)

    def test_optional_hooks_have_no_abc_default(self):
        # Each optional hook has a load-bearing getattr fallback at its call site;
        # a default here would silently change preview and settle routing.
        for hook in ("observe_ready", "wait_for_change", "capture_preview"):
            self.assertFalse(hasattr(Driver, hook), hook)

    def test_factory_builds_registered_drivers(self):
        self.assertIsInstance(build_driver("wda", url="http://localhost:8100", session="s"), WDA)
        self.assertIn("wda", DRIVERS)

    def test_factory_rejects_unknown_kinds_before_constructing(self):
        for kind in ("adb", "", None, ["wda"]):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                build_driver(kind)

    def test_factory_rejects_a_non_driver_registration(self):
        class NotADriver:
            pass

        with patch.dict(DRIVERS, {"broken": NotADriver}):
            with self.assertRaises(TypeError):
                build_driver("broken")
        with self.assertRaises(TypeError):
            register_driver("broken", NotADriver)
        self.assertNotIn("broken", DRIVERS)


class TargetSelectionTests(unittest.TestCase):
    def test_wda_target(self):
        with patch.object(WDA, "configure") as configure:
            driver = build_target_driver(wda_url="http://localhost:8100", session="s")
        self.assertIsInstance(driver, WDA)
        configure.assert_called_once_with()

    def test_no_target_rejected(self):
        with self.assertRaises(ValueError):
            build_target_driver()


class ModelWiringTests(unittest.TestCase):
    def test_models_require_their_keys(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                build_models()
            with self.assertRaises(ValueError):
                build_models(helper=True)

    def test_helper_is_opt_in(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test"}):
            model, helper = build_models()
            try:
                self.assertIsNotNone(model)
                self.assertIsNone(helper)
            finally:
                close_all(model, helper)

    def test_helper_wires_explicit_model(self):
        env = {"TYPESAFE_API_KEY": "test", "TEXT_MODEL_API_KEY": "key",
               "TEXT_MODEL": "some-model"}
        with patch.dict(os.environ, env, clear=True):
            model, helper = build_models(helper=True)
            try:
                self.assertEqual(helper.model, "some-model")
                self.assertEqual(helper.provider, "helper")
            finally:
                close_all(model, helper)


class VisualWiringTests(unittest.TestCase):
    def test_screen_reading_is_opt_in(self):
        self.assertIsNone(build_visual())
        self.assertIsNone(build_visual(enabled=False, driver=DemoDriver()))


class CloseAllTests(unittest.TestCase):
    def test_none_safe_and_never_raises(self):
        closed = []

        class Flaky:
            @property
            def http(self):
                raise RuntimeError("no http here")

            def close(self):
                closed.append("driver")
                raise RuntimeError("close failed")

        close_all(None, Flaky())
        self.assertEqual(closed, ["driver"])
