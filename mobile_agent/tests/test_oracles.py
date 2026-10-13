"""The bench's WDA probe and its navigation reads, with a fake WDA. No device."""

import unittest

from mobile_agent.errors import MobsterError
from mobile_agent.evals.oracles import ProbeUnavailable, navigation_title
from mobile_agent.state import Element, Snapshot

BUNDLE = "com.apple.Preferences"


def snapshot(text, elements=(), bundle_id=BUNDLE):
    return Snapshot(list(elements), text, 402, 874, "wda", bundle_id=bundle_id)


def title_element(label):
    return Element("t", label, "StaticText", (.3, .05, .4, .04), actions=())


class NavigationTitleTests(unittest.TestCase):
    def test_nav_title_reads_the_title_band(self):
        elements = [title_element("About"),
                    Element("r", "About", "Button", (.1, .5, .8, .06))]
        self.assertEqual(navigation_title(snapshot("rows", elements)), "About")

    def test_probe_failure_is_a_domain_error(self):
        self.assertTrue(issubclass(ProbeUnavailable, MobsterError))
        self.assertIs(ProbeUnavailable.retryable, False)


WDA_ABOUT = ('<XCUIElementTypeApplication width="400" height="800">'
             '<XCUIElementTypeNavigationBar label="About" x="0" y="50" width="400" height="50">'
             '<XCUIElementTypeButton label="General" x="0" y="55" width="90" height="40"/>'
             '</XCUIElementTypeNavigationBar>'
             '<XCUIElementTypeButton label="iOS Version, 26.0.1" x="0" y="200" width="400" height="44"/>'
             '</XCUIElementTypeApplication>')


class WDAProbeTests(unittest.TestCase):
    def probe(self, *foregrounds):
        from mobile_agent.evals.oracles import WDAProbe
        probe = WDAProbe("http://127.0.0.1:8100", session="s")
        self.addCleanup(probe.close)
        apps = iter(foregrounds)
        def request(method, path, body=None, timeout=10):
            if path == "/wda/activeAppInfo":
                return {"value": {"bundleId": next(apps)}}
            return {"value": WDA_ABOUT}
        probe.http.request = request
        return probe

    def test_reads_are_sessionless_and_stamped_with_the_foreground_app(self):
        snapshot = self.probe("com.apple.Preferences", "com.apple.Preferences").observe("com.apple.Preferences")
        self.assertEqual(snapshot.bundle_id, "com.apple.Preferences")
        self.assertEqual(navigation_title(snapshot), "About")

    def test_a_read_that_straddles_an_app_switch_is_not_graded(self):
        with self.assertRaises(ProbeUnavailable):
            self.probe("com.apple.Preferences", "com.apple.springboard").observe("com.apple.Preferences")

    def test_on_a_simulator_springboard_from_active_app_info_defers_to_the_session_tree(self):
        # iOS 26.4 simulators name SpringBoard in activeAppInfo whatever is in front (10 Oct).
        from mobile_agent.evals.oracles import WDAProbe
        tree = WDA_ABOUT.replace('<XCUIElementTypeApplication ',
                                 '<XCUIElementTypeApplication bundleId="com.apple.Preferences" ', 1)
        for port, simulator in ((8291, True), (8292, False)):
            url = f"http://127.0.0.1:{port}"
            self.addCleanup(WDAProbe._overlay_apps.pop, url, None)
            probe = WDAProbe(url, session="s")
            self.addCleanup(probe.close)

            def request(method, path, body=None, timeout=10, simulator=simulator):
                if path == "/wda/activeAppInfo":
                    return {"value": {"bundleId": "com.apple.springboard"}}
                if path == "/status":
                    return {"value": {"ios": {"simulatorVersion": "26.4"} if simulator else {}}}
                return {"value": tree if "source" in path else None}
            probe.http.request = request
            got = probe.foreground(expected="com.apple.Preferences")
            self.assertEqual(got, "com.apple.Preferences" if simulator else "com.apple.springboard", simulator)

    def test_on_a_phone_springboard_from_active_app_info_is_checked_with_the_session(self):
        # The bench phone, iOS 26.4, 11 Oct: after activating a deleted app, the sessionless read named SpringBoard with
        # Settings in front; the session's own activeAppInfo named Settings.
        from mobile_agent.evals.oracles import WDAProbe
        url = "http://127.0.0.1:8293"
        self.addCleanup(WDAProbe._overlay_apps.pop, url, None)
        probe = WDAProbe(url, session="s")
        self.addCleanup(probe.close)
        asked = []

        def request(method, path, body=None, timeout=10):
            asked.append(path)
            if path == "/wda/activeAppInfo":
                return {"value": {"bundleId": "com.apple.springboard"}}
            if path == "/session/s/wda/activeAppInfo":
                return {"value": {"bundleId": "com.apple.Preferences"}}
            if path == "/status":
                return {"value": {"ios": {}}}
            raise AssertionError(f"no tree read expected: {path}")
        probe.http.request = request
        self.assertEqual(probe.foreground(expected="com.apple.Preferences"), "com.apple.Preferences")
        self.assertIn("/session/s/wda/activeAppInfo", asked)

    def test_back_button_is_the_leading_navigation_bar_button(self):
        from mobile_agent.evals.oracles import navigation_back
        from mobile_agent.state import from_wda
        back = navigation_back(from_wda(WDA_ABOUT))
        self.assertEqual(back.label, "General")


if __name__ == "__main__":
    unittest.main()
