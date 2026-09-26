"""A search field holding an earlier query is emptied before the new one is typed. Offline."""

import unittest

from mobile_agent.drivers import WDA
from mobile_agent.state import Element, Snapshot


class Recorder(WDA):
    def __init__(self):
        super().__init__("http://127.0.0.1:8100", "s")
        self.calls = []

    def call(self, method, path, body=None, timeout=10):
        self.calls.append((method, path, body))
        if path == "/elements":
            return [{"ELEMENT": "E1"}]
        return None

    def _check_current(self, snapshot, deadline):
        pass


def screen(value, role="SearchField"):
    field = Element("f", "Search", role, (.08, .92, .68, .05), value=value, locator="/field",
                    editable=True, actions=("TAP", "TYPE", "SUBMIT"))
    return field, Snapshot([field], "Search", 393, 852, "wda", bundle_id="com.apple.mobilenotes")


class SearchClearTests(unittest.TestCase):
    def paths(self, value, role="SearchField"):
        driver = Recorder()
        field, snapshot = screen(value, role)
        driver.execute("TYPE_SUBMIT", field, snapshot, text="Locker code", timeout=10)
        return [path for _, path, _ in driver.calls], driver.calls

    def test_an_earlier_query_is_cleared_before_typing(self):
        paths, calls = self.paths("MobsterBench Note")
        self.assertEqual(paths[:2], ["/elements", "/element/E1/clear"])
        # Found by name ("name": WDA answers HTTP 500 to "identifier"), and re-found by the same query
        # after the clear, when its value is gone.
        self.assertIn("name == 'Search' OR label == 'Search'", calls[0][2]["value"])
        self.assertEqual(paths[-1], "/wda/keys")

    def test_a_cleared_query_is_typed_without_a_focusing_tap(self):
        # The clear focused the field: no tap and no keyboard wait (mem-015 paid both per Mail search).
        driver = Recorder()
        field = Element("f", "Search Mail", "SearchField", (.08, .92, .68, .05), value="CityRide", locator="/field",
                        editable=True, actions=("TAP", "TYPE"))
        snapshot = Snapshot([field], "Search", 393, 852, "wda", bundle_id="com.example.mail")
        driver.execute("TYPE_SUBMIT", field, snapshot, text="DL1358", timeout=10)
        self.assertEqual([path for _, path, _ in driver.calls], ["/elements", "/element/E1/clear", "/wda/keys"])
        self.assertEqual(driver.calls[-1][2], {"value": ["DL1358", "\n"]})

    def test_the_value_finds_the_field_only_when_name_and_kind_are_not_unique(self):
        driver = Recorder()
        answers = [[{"ELEMENT": "A"}, {"ELEMENT": "B"}], [], [{"ELEMENT": "C"}, {"ELEMENT": "D"}]]
        driver.call = lambda method, path, body=None, timeout=10: (
            driver.calls.append((method, path, body)) or (answers.pop(0) if path == "/elements" else None))
        field, snapshot = screen("MobsterBench Note")
        driver.clear_text(field)
        predicates = [body["value"] for _, path, body in driver.calls if path == "/elements"]
        self.assertEqual(len(predicates), 3)
        self.assertIn("value == 'MobsterBench Note'", predicates[-1])
        self.assertEqual(driver.calls[-1][1], "/element/C/clear")

    def test_a_truncated_long_value_is_found_by_its_head(self):
        driver = Recorder()
        answers = [[{"ELEMENT": "A"}, {"ELEMENT": "B"}], [{"ELEMENT": "E1"}]]
        driver.call = lambda method, path, body=None, timeout=10: (
            driver.calls.append((method, path, body)) or (answers.pop(0) if path == "/elements" else None))
        body = Element("b", "", "TextView", (.05, .3, .9, .5), value="Fiction:\n- Dune … Currently reading: Dune",
                       editable=True, actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        driver.clear_text(body)
        predicates = [body["value"] for _, path, body in driver.calls if path == "/elements"]
        self.assertIn("value BEGINSWITH 'Fiction:\n- Dune'", predicates[-1])
        self.assertEqual(driver.calls[-1][1], "/element/E1/clear")

    def test_an_empty_field_or_its_placeholder_is_typed_into_directly(self):
        for value in ("", "Search"):
            self.assertEqual(self.paths(value)[0], ["/wda/keys"])

    def test_other_fields_keep_their_text(self):
        self.assertEqual(self.paths("Dear Sam,", role="TextField")[0], ["/wda/keys"])


if __name__ == "__main__":
    unittest.main()


class ElementTypingTests(unittest.TestCase):
    def test_an_unfocused_text_view_is_typed_through_the_element(self):
        # mem-041: a coordinate tap left focus in the title and the body went there.
        driver = Recorder()
        body = Element("b", "notes_body_editor", "TextView", (.05, .3, .9, .5), editable=True,
                       actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        snapshot = Snapshot([body], "", 393, 852, "wda", bundle_id="com.example.notes")
        driver.execute("TYPE", body, snapshot, text="line one\nline two", timeout=10)
        paths = [path for _, path, _ in driver.calls]
        self.assertEqual(paths, ["/elements", "/element/E1/value"])
        # WDA 16.12 rejects an "identifier" predicate: the field is found by name.
        self.assertIn("name == 'notes_body_editor'", driver.calls[0][2]["value"])
        self.assertEqual(driver.calls[-1][2], {"value": "line one\nline two"})

    def test_type_submit_sends_return_in_the_typing_request(self):
        driver = Recorder()
        field = Element("s", "search_products_field", "TextField", (.05, .1, .9, .05), editable=True,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        snapshot = Snapshot([field], "", 393, 852, "wda", bundle_id="com.example.shop")
        driver.execute("TYPE_SUBMIT", field, snapshot, text="PS5 Controller", timeout=10)
        self.assertEqual([path for _, path, _ in driver.calls], ["/elements", "/element/E1/value"])
        self.assertEqual(driver.calls[-1][2], {"value": "PS5 Controller\n"})

    def notes_screen(self):
        title = Element("t", "notes_title_field", "TextField", (.05, .15, .9, .05), value="Map", editable=True,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"))
        body = Element("b", "notes_body_editor", "TextView", (.05, .3, .9, .5), editable=True,
                       actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"))
        return body, Snapshot([title, body], "", 393, 852, "wda", bundle_id="com.example.notes", keyboard="visible")

    def focused(self, rect):
        driver = Recorder()
        answers = {"/element/active": {"ELEMENT": "A1"}, "/element/A1/rect": rect}
        recorded = driver.call
        driver.call = lambda method, path, body=None, timeout=10: answers.get(path) or recorded(method, path, body, timeout)
        return driver

    def test_with_the_keyboard_on_another_field_focus_moves_before_typing(self):
        # mem-048: the keyboard was up for the title, and the note body was typed into it.
        driver = self.focused({"x": 20, "y": 128, "width": 353, "height": 40})
        body, snapshot = self.notes_screen()
        driver.execute("TYPE", body, snapshot, text="Top colleagues", timeout=10)
        self.assertEqual([path for _, path, _ in driver.calls], ["/elements", "/element/E1/value"])

    def test_with_the_keyboard_on_the_target_keys_are_sent_directly(self):
        driver = self.focused({"x": 20, "y": 256, "width": 353, "height": 426})
        body, snapshot = self.notes_screen()
        driver.execute("TYPE", body, snapshot, text="Top colleagues", timeout=10)
        self.assertEqual([path for _, path, _ in driver.calls], ["/wda/keys"])


class ReplaceTextTests(unittest.TestCase):
    def field(self, value, role="TextField", label="notes_title_field"):
        return Element("t", label, role, (.05, .15, .9, .05), value=value, editable=True,
                       actions=("TAP", "TYPE", "TYPE_SUBMIT"))

    def test_one_lookup_then_clear_and_type_on_the_same_element(self):
        # SET_TEXT used to clear (a lookup), read the screen, and type as a TYPE (another lookup).
        driver = Recorder()
        self.assertTrue(driver.replace_text(self.field("Map"), "Books to Read"))
        self.assertEqual([path for _, path, _ in driver.calls], ["/elements", "/element/E1/clear", "/element/E1/value"])
        self.assertIn("name == 'notes_title_field'", driver.calls[0][2]["value"])
        self.assertEqual(driver.calls[-1][2], {"value": "Books to Read"})

    def test_an_empty_field_or_its_placeholder_is_not_cleared(self):
        for value in ("", "notes_title_field"):
            driver = Recorder()
            self.assertTrue(driver.replace_text(self.field(value), "Books"))
            self.assertEqual([path for _, path, _ in driver.calls], ["/elements", "/element/E1/value"])

    def test_a_filled_search_field_takes_keystrokes_where_its_clear_left_focus(self):
        # Mail's search in mem-015: each new query paid three lookups, two reads and a focusing tap.
        driver = Recorder()
        self.assertTrue(driver.replace_text(self.field("CityRide", role="SearchField", label="Search Mail"), "DL1358"))
        self.assertEqual([path for _, path, _ in driver.calls], ["/elements", "/element/E1/clear", "/wda/keys"])
        self.assertEqual(driver.calls[-1][2], {"value": ["DL1358"]})

    def test_a_failed_clear_is_typed_over_but_a_search_field_goes_back_to_the_caller(self):
        # MegaMart's search field timed out clearing at 5 s; a second clear by the caller cost 5 s more.
        from mobile_agent.transport import TransportError
        for role, typed in (("TextField", True), ("SearchField", False)):
            driver = Recorder()
            recorded = driver.call

            def call(method, path, body=None, timeout=10, recorded=recorded, driver=driver):
                if path.endswith("/clear"):
                    driver.calls.append((method, path, body))
                    raise TransportError("HTTP TimeoutError; request outcome unknown; not retried")
                return recorded(method, path, body, timeout)
            driver.call = call
            self.assertEqual(driver.replace_text(self.field("PS5", role=role, label="search_products_field"), "x"),
                             typed)
            self.assertEqual(driver.calls[-1][1] != "/element/E1/clear", typed)

    def test_empty_search_fields_unnamed_or_ambiguous_fields_are_left_to_the_caller(self):
        driver = Recorder()
        for value in ("", "Search"):  # nothing to clear: focus needs the caller's tap
            self.assertFalse(driver.replace_text(self.field(value, role="SearchField", label="Search"), "x"))
        self.assertFalse(driver.replace_text(self.field("q", label=""), "x"))
        self.assertEqual(driver.calls, [])
        driver.call = lambda method, path, body=None, timeout=10: (
            driver.calls.append((method, path, body)) or [{"ELEMENT": "A"}, {"ELEMENT": "B"}])
        self.assertFalse(driver.replace_text(self.field("Map"), "x"))
        self.assertEqual([path for _, path, _ in driver.calls], ["/elements"])


class ParkedKeyboardWaitTests(unittest.TestCase):
    def setUp(self):
        WDA._parked_keyboards.discard("http://127.0.0.1:8100")
        self.addCleanup(WDA._parked_keyboards.discard, "http://127.0.0.1:8100")

    def reads(self, keyboard):
        from unittest.mock import patch
        from mobile_agent.transport import Deadline
        driver = Recorder()
        field = Element("f", "Search", "SearchField", (.08, .1, .68, .05), editable=True, actions=("TAP", "TYPE"))
        seen = []
        driver.observe = lambda timeout=10: seen.append(1) or Snapshot(
            [field], "", 393, 852, "wda", keyboard=keyboard)
        with patch("mobile_agent.drivers.WDA_KEYBOARD_WAIT_SECONDS", .3):
            self.assertTrue(driver._await_keyboard(Deadline(5)))
        return len(seen)

    def test_a_hardware_keyboard_is_learned_and_then_read_once(self):
        # Simulators in hardware-keyboard mode never raise one: every focusing tap paid half the
        # keyboard wait in reads (1-2 reads of ~1 s on a long list).
        self.assertGreater(self.reads("parked"), 1)
        self.assertEqual(self.reads("parked"), 1)

    def test_a_software_keyboard_forgets_it(self):
        WDA._parked_keyboards.add("http://127.0.0.1:8100")
        self.assertEqual(self.reads("visible"), 1)
        self.assertNotIn("http://127.0.0.1:8100", WDA._parked_keyboards)
