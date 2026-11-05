import socket
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from mobile_agent.drivers import WDA
from mobile_agent.state import WDA_NODE_BUDGET, Element, Snapshot, from_wda, validate_input_text
from mobile_agent.tests.timing import bound
from mobile_agent.transport import HTTP, Deadline, TransportError, decode_json
from mobile_agent.wda.guards import DriverRejection
from mobile_agent.wda.observe import WDA_SOURCE_PATH

XML = ('<XCUIElementTypeApplication width="400" height="800">'
       '<XCUIElementTypeButton label="Search" x="1" y="1" width="100" height="40"/>'
       '</XCUIElementTypeApplication>')


def screen(name="home"):
    elements, text = {
        "home": ([Element("0", "Search", "Button", (.8, .06, .1, .05)),
                  Element("1", "Profile", "Button", (.8, .92, .15, .05))], "For You\nSearch\nProfile"),
        "search": ([Element("0", "Search", "SearchField", (.1, .06, .7, .05), True),
                    Element("1", "Search", "Button", (.82, .06, .15, .05))], "Search TikTok"),
        "results": ([Element("0", "Coffee brewing guide", "Button", (.05, .2, .9, .3)),
                     Element("1", "@coffee.studio", "Button", (.1, .55, .5, .05))],
                    "Top results\nCoffee brewing guide\n@coffee.studio"),
    }[name]
    return Snapshot(elements, text, 402, 874, "synthetic_fixture")


class StateHardeningTests(unittest.TestCase):
    def test_content_fingerprint_preserves_evidence_geometry_and_routing(self):
        original = screen()
        for candidate in [replace(original, width=800), replace(original, bundle_id="other.app"),
                          replace(original, text=original.text + "\nPlease sign in"),
                          replace(original, elements=[replace(e, value="changed") for e in original.elements]),
                          replace(original, elements=[replace(e, actions=()) for e in original.elements]),
                          replace(original, elements=[replace(e, locator="/other") for e in original.elements])]:
            with self.subTest(candidate=candidate):
                self.assertNotEqual(original.content_fingerprint, candidate.content_fingerprint)

    def test_fingerprint_includes_dimensions_revision_and_routing(self):
        original = screen()
        candidates = [replace(original, width=800), replace(original, height=400),
            replace(original, revision="another-process:1"),
            replace(original, elements=[replace(e, locator="/different") for e in original.elements]),
            replace(original, text="a" * 12000 + "secretly changed")]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertNotEqual(original.fingerprint, candidate.fingerprint)
        self.assertNotEqual(replace(original, text="a" * 12000 + "1").fingerprint,
                            replace(original, text="a" * 12000 + "2").fingerprint)

    def test_wda_node_budget_includes_unlabeled_structure(self):
        xml = ('<XCUIElementTypeApplication width="400" height="800">'
               + '<XCUIElementTypeOther/>' * (WDA_NODE_BUDGET + 1) + '</XCUIElementTypeApplication>')
        with self.assertRaises(ValueError):
            from_wda(xml)

    def test_a_view_wider_than_a_double_has_no_size_and_the_screen_still_reads(self):
        for rich in (False, True):
            xml = ('<XCUIElementTypeApplication width="400" height="800"><XCUIElementTypeOther>'
                   '<XCUIElementTypeOther x="0" y="0" width="1.797693134862316e+308" height="1"/>'
                   '<XCUIElementTypeOther x="0" y="0" width="1.797693134862316e+308" '
                   'height="1"/></XCUIElementTypeOther>'
                   '<XCUIElementTypeButton label="New Reminder" x="10" y="700" width="140" height="44"/>'
                   '<XCUIElementTypeStaticText label="nan" x="nan" y="20" width="40" height="20"/>'
                   '</XCUIElementTypeApplication>')
            with self.subTest(rich=rich):
                state = from_wda(xml, rich=rich)
                self.assertIn("New Reminder", [e.label for e in state.elements])
                self.assertTrue(all(all(abs(v) < 1e6 for v in e.rect) for e in state.elements))

    def test_text_blocks_control_and_unicode_submission_characters(self):
        for text in [None, "", "\t", "hello\n", "hello\r", "x\x7f", "x\x85", "x\u2028", "x\ud800"]:
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                validate_input_text(text)
        self.assertEqual(validate_input_text("café ☕"), "café ☕")


class AdapterHardeningTests(unittest.TestCase):
    def test_wda_tap_is_one_coordinate_action_without_resolution(self):
        state = from_wda(XML)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        driver.execute("TAP", state.elements[0], state)
        driver.call.assert_called_once()
        method, path, body = driver.call.call_args[0][:3]
        self.assertEqual((method, path), ("POST", "/actions"))
        move = body["actions"][0]["actions"][0]
        self.assertEqual((move["x"], move["y"]), (50, 20))

    def test_wda_regrounds_an_aging_snapshot_before_tapping(self):
        state = from_wda(XML)
        state.captured_at -= 5
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock(return_value=XML.replace('label="Search"', 'label="Purchase"'))
        with self.assertRaises(DriverRejection) as refused:
            driver.execute("TAP", state.elements[0], state)
        self.assertIn(refused.exception.code, DriverRejection.PRE_DISPATCH)
        self.assertEqual([c[0][1] for c in driver.call.call_args_list], [WDA_SOURCE_PATH])

    def test_wda_aging_but_unchanged_snapshot_still_taps(self):
        state = from_wda(XML)
        state.captured_at -= 5
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock(side_effect=[XML, None])
        driver.execute("TAP", state.elements[0], state)
        self.assertEqual([c[0][1] for c in driver.call.call_args_list], [WDA_SOURCE_PATH, "/actions"])

    def test_wda_type_into_a_focused_field_is_one_keystroke_call(self):
        xml = XML.replace("</XCUIElementTypeApplication>",
            '<XCUIElementTypeKeyboard x="0" y="500" width="400" height="300"/></XCUIElementTypeApplication>')
        xml = xml.replace("XCUIElementTypeButton", "XCUIElementTypeTextField")
        state = from_wda(xml)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        driver.execute("TYPE", state.elements[0], state, "hi")
        driver.call.assert_called_once_with("POST", "/wda/keys", {"value": ["hi"]}, ANY)

    def test_wda_type_focuses_first_and_types_only_once_a_keyboard_is_up(self):
        field = XML.replace("XCUIElementTypeButton", "XCUIElementTypeTextField")
        focused = field.replace("</XCUIElementTypeApplication>",
            '<XCUIElementTypeKeyboard x="0" y="500" width="400" height="300"/></XCUIElementTypeApplication>')
        state = from_wda(field)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock(side_effect=lambda method, path, *a, **k: focused if path.startswith("/source") else None)
        driver.execute("TYPE", state.elements[0], state, "hi")
        paths = [c[0][1] for c in driver.call.call_args_list if c[0][1] != "/elements"]
        self.assertEqual(paths[0], "/actions")
        self.assertEqual(driver.call.call_args_list[-1][0][1:3], ("/wda/keys", {"value": ["hi"]}))

    def test_wda_type_sends_nothing_when_focus_never_arrives(self):
        field = XML.replace("XCUIElementTypeButton", "XCUIElementTypeTextField")
        state = from_wda(field)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock(side_effect=lambda method, path, *a, **k: field if path.startswith("/source") else None)
        with patch("mobile_agent.wda.text.WDA_KEYBOARD_WAIT_SECONDS", .05), self.assertRaises(TransportError):
            driver.execute("TYPE", state.elements[0], state, "hi")
        self.assertNotIn("/wda/keys", [c[0][1] for c in driver.call.call_args_list])

    def test_wda_invalid_text_never_even_resolves_a_target(self):
        xml = XML.replace("XCUIElementTypeButton", "XCUIElementTypeTextField")
        state = from_wda(xml)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        with self.assertRaises(ValueError):
            driver.execute("TYPE", state.elements[0], state, "hi\r")
        driver.call.assert_not_called()

    def test_wda_supports_all_four_swipe_directions(self):
        state = from_wda(XML)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        for operation, (x1, y1, x2, y2) in {
                "SWIPE_UP": (200, 600, 200, 200), "SWIPE_DOWN": (200, 200, 200, 600),
                "SWIPE_LEFT": (300, 400, 100, 400), "SWIPE_RIGHT": (100, 400, 300, 400)}.items():
            with self.subTest(operation=operation):
                driver.execute(operation, None, state)
                method, path, body = driver.call.call_args[0][:3]
                if operation in ("SWIPE_LEFT", "SWIPE_RIGHT"):
                    self.assertEqual((method, path), ("POST", "/actions"))
                    moves = [a for a in body["actions"][0]["actions"] if a["type"] == "pointerMove"]
                    self.assertEqual((moves[0]["x"], moves[0]["y"], moves[-1]["x"], moves[-1]["y"]), (x1, y1, x2, y2))
                    continue
                self.assertEqual((method, path), ("POST", "/wda/dragfromtoforduration"))
                self.assertEqual((body["fromX"], body["fromY"], body["toX"], body["toY"]),
                                 (x1, y1, x2, y2))

    def test_wda_configure_applies_measured_snapshot_depth(self):
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock(side_effect=lambda method, path, *a, **k:
                           {"width": 393, "height": 852} if path == "/window/size" else None)
        driver.http.request = Mock(side_effect=TransportError("no WDA in unit tests"))
        driver.configure()
        driver.call.assert_called_with("POST", "/appium/settings", {"settings": {
            "snapshotMaxDepth": 60, "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0,
            "snapshotTimeout": 6, "customSnapshotTimeout": 6, "activeAppDetectionPoint": "196,426"}}, ANY)

    def test_wda_home_and_volume_keys_use_pressButton(self):
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        result = driver.execute("HOME", None, from_wda(XML))
        driver.call.assert_called_once_with("POST", "/wda/pressButton", {"name": "home"}, ANY)
        self.assertEqual(result, {"dispatch_attempted": True})
        for operation, name in (("VOLUME_UP", "volumeUp"), ("VOLUME_DOWN", "volumeDown")):
            driver.execute(operation, None, from_wda(XML))
            self.assertEqual(driver.call.call_args[0][:3], ("POST", "/wda/pressButton", {"name": name}))


class TransportHardeningTests(unittest.TestCase):
    def test_deadlines_reject_nonfinite_and_boolean_timeouts(self):
        for timeout in [0, -1, True, float("nan"), float("inf"), "1"]:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                Deadline(timeout)

    def test_strict_json_rejects_duplicate_fields_and_nonfinite(self):
        for raw in ['{"ok":false,"ok":true}', '{"n":NaN}', '{"n":Infinity}', '{"n":1e999}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode_json(raw)

    def test_http_oversized_or_unreadable_response_closes_connection(self):
        for status, raw in [(200, b'x' * 16000001), (200, b'[]'), (200, b'{"ok":false,"ok":true}')]:
            client = HTTP("http://localhost")
            connection = Mock(sock=None)
            connection.getresponse.return_value = SimpleNamespace(status=status, read=lambda limit: raw)
            client.connection = connection
            with self.subTest(status=status, size=len(raw)), self.assertRaises(TransportError):
                client.request("GET", "/test")
            connection.close.assert_called()
            self.assertEqual(connection.request.call_count, 1)

    def test_http_truncated_content_length_cannot_become_valid_json(self):
        client = HTTP("http://localhost")
        connection = Mock(sock=None)
        connection.getresponse.return_value = SimpleNamespace(status=200, length=100, read=lambda limit: b'{}')
        client.connection = connection
        with self.assertRaises(TransportError):
            client.request("GET", "/test")
        connection.close.assert_called()

    def test_http_slow_drip_is_bounded_by_absolute_deadline(self):
        local, remote = socket.socketpair()
        client = HTTP("http://localhost")
        client.connection.sock = local
        def peer():
            try:
                remote.recv(4096)
                remote.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n")
                for _ in range(100):
                    remote.sendall(b" ")
                    time.sleep(.01)
            except OSError:
                pass
            finally:
                remote.close()
        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaises(TransportError):
                client.request("GET", "/test", timeout=.08)
            self.assertLess(time.monotonic() - started, bound(.5))
        finally:
            client.close()
            thread.join(.5)
        self.assertFalse(thread.is_alive())

    def test_concurrent_http_request_is_not_replayed_or_interleaved(self):
        client = HTTP("http://localhost")
        client.connection = Mock(sock=None)
        client._inflight.acquire()
        try:
            with self.assertRaises(TransportError):
                client.request("GET", "/test")
            client.connection.request.assert_not_called()
        finally:
            client._inflight.release()


if __name__ == "__main__":
    unittest.main()
