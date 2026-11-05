"""Writing through a field WDA can lose, and an answer that proves nothing: the real WDA class and the real writer.
WDA's /value is faked behind them as v16.12.10 runs it: find the element through its query, tap, find it again, type."""

import json
import re
import unittest

from mobile_agent.bench.tape import decode_error, encode_error
from mobile_agent.drivers import WDA
from mobile_agent.frontier import FrontierAgent
from mobile_agent.frontier.safety import outcome_unknown
from mobile_agent.state import Element, Snapshot
from mobile_agent.tests import test_frontier as F
from mobile_agent.tests.test_wda_stale_session import serve
from mobile_agent.transport import HTTP, NotSent, StaleSession, TransportError, answer_error
from mobile_agent.wda.text import FieldNotCleared, NoKeyboardFocus, WriteInterrupted

W, H = 400, 800
ROOT = "/XCUIElementTypeApplication/XCUIElementTypeWindow"
QUERY = re.compile(r"type == 'XCUIElementType(\w+)' AND \(name == '(.*?)' OR label == '(.*?)'\)\Z", re.S)
STALE = {"status": 404, "wda_error": "stale element reference"}
EDITING = ("TAP", "TYPE", "TYPE_SUBMIT")


def field(label, hint=None, role="TextField", value="", y=None):
    return {"label": label, "hint": hint, "role": role, "value": value, "y": y}


class FormWDA(WDA):
    """Fields of any role. While a field holds focus and is empty, its label gains its hint on a new line."""

    def __init__(self, *fields, focus=None, query_breaks=False, tap_moves_focus=True, before=None, after=None):
        super().__init__("http://localhost:8100", "s1")
        WDA._simulators[self.url] = True
        self.fields = list(fields)
        for i, f in enumerate(self.fields):
            f["y"] = f["y"] if f["y"] is not None else 0.1 + 0.15 * i
        self.focus, self.query_breaks, self.tap_moves_focus, self.before, self.after = (
            focus,
            query_breaks,
            tap_moves_focus,
            before,
            after,
        )
        self.posted, self.keys, self.queries, self.tapped = [], [], {}, False

    def label_of(self, f):
        return f"{f['label']}\n{f['hint']}" if f["hint"] and self.focus == f["label"] and not f["value"] else f["label"]

    def observe(self, timeout=10):
        els = [
            Element(
                str(i),
                self.label_of(f),
                f["role"],
                (0.05, f["y"], 0.9, 0.1),
                True,
                f"{ROOT}/XCUIElementType{f['role']}[{i + 1}]",
                f["value"],
                EDITING,
            )
            for i, f in enumerate(self.fields)
        ]
        return Snapshot(
            els,
            " ".join(e.label for e in els),
            W,
            H,
            "wda",
            bundle_id="com.example.app",
            keyboard="visible" if self.focus else "",
        )

    def matches(self, role, name):
        name = name.replace("\\'", "'").replace("\\\\", "\\")
        return [f for f in self.fields if f["role"] == role and self.label_of(f) == name]

    def find_again(self, uuid):
        role, name, position = self.queries[uuid]
        hits = self.matches(role, name)
        if self.query_breaks and self.tapped or len(hits) <= position:
            raise TransportError("HTTP 404; request not retried", **STALE)
        return hits[position]

    def into(self, f, text):
        f["value"] += text
        self.keys.append((f["label"], text))

    def route(self, path, body):
        if path == "/elements":
            found = QUERY.fullmatch(body["value"])
            assert found and found.group(2) == found.group(3), body["value"]
            ids = []
            for position, _ in enumerate(self.matches(found.group(1), found.group(2))):
                self.queries[f"q{len(self.queries)}"] = (found.group(1), found.group(2), position)
                ids.append({"ELEMENT": f"q{len(self.queries) - 1}"})
            return ids
        if path == "/element/active":
            if not self.focus:
                raise TransportError("HTTP 404; request not retried", status=404, wda_error="no such element")
            return {"ELEMENT": "active"}
        if path == "/element/active/rect":
            f = next(x for x in self.fields if x["label"] == self.focus)
            return {"x": 0.05 * W, "y": f["y"] * H, "width": 0.9 * W, "height": 0.1 * H}
        if path == "/element/active/attribute/value":
            return next(x for x in self.fields if x["label"] == self.focus)["value"]
        if path == "/wda/keys":
            f = next((x for x in self.fields if x["label"] == self.focus), None)
            if f is not None:
                self.into(f, "".join(body["value"]))
            return None
        if path == "/actions":
            move = next(a for a in body["actions"][0]["actions"] if a.get("type") == "pointerMove")
            hit = next((f for f in self.fields if f["y"] * H <= move["y"] <= (f["y"] + 0.1) * H), None)
            if hit is not None and self.tap_moves_focus:
                self.focus = hit["label"]
            return None
        if path == "/appium/settings":
            return None
        value = re.fullmatch(r"/element/(q\d+)/(value|clear)", path)
        if value and value.group(2) == "clear":
            self.find_again(value.group(1))["value"] = ""
            return None
        if value:
            f = self.find_again(value.group(1))
            if self.focus != f["label"]:
                self.tapped = True
                if self.tap_moves_focus:
                    self.focus = f["label"]
            f = self.find_again(value.group(1))
            self.into(f, body["value"])
            return None
        raise AssertionError(f"this fake doesn't know {path}")

    def call(self, method, path, body=None, timeout=None):
        self.posted.append(path)
        if self.before:
            self.before(path, self.posted.count(path))
        answer = self.route(path, body)
        if self.after:
            self.after(path, self.posted.count(path))
        return answer

    def named(self, label):
        snapshot = self.observe()
        return next(e for e in snapshot.elements if e.label == label), snapshot

    def posts(self, kind):
        return sum(p.endswith(kind) for p in self.posted)


def raising(at, error):
    """Once, on the first request to a path ending ``at``, whichever element it names."""
    spent = []

    def hook(path, count):
        if path.endswith(at) and not spent:
            spent.append(1)
            raise error

    return hook


class ExactBinding(unittest.TestCase):
    """Astra acf21ae2 R1: a write through an element needs one element with exactly the observed name."""

    def left_behind(self):
        # The observed field goes between the look and the lookup, and one that starts with its name stays.
        wda = FormWDA(field("Address"), field("Address\nBilling", value="kept"))
        self.addCleanup(wda.close)
        element, snapshot = wda.named("Address")

        def goes(path, count):
            if path == "/elements":
                wda.fields[:] = wda.fields[-1:]

        wda.before = goes
        wda._await_keyboard = lambda deadline: False
        return wda, element, snapshot

    def test_the_exact_name_beside_one_sharing_its_first_line_is_the_one_written(self):
        wda = FormWDA(field("Address"), field("Address\nBilling"), focus="Address\nBilling", tap_moves_focus=False)
        self.addCleanup(wda.close)
        element, snapshot = wda.named("Address")
        wda.write_text(element, snapshot, "pilot 1", append=True)
        self.assertEqual(wda.keys, [("Address", "pilot 1")])
        self.assertEqual((wda.posts("/value"), wda.posts("/actions"), wda.posts("/wda/keys")), (1, 0, 0))

    def test_a_full_label_with_a_second_line_finds_only_itself(self):
        wda = FormWDA(field("Address\nBilling"), field("Address\nShipping"))
        self.addCleanup(wda.close)
        element, snapshot = wda.named("Address\nBilling")
        wda.write_text(element, snapshot, "pilot 1", append=True)
        self.assertEqual([f["value"] for f in wda.fields], ["pilot 1", ""])
        self.assertEqual(wda.posts("/value"), 1)

    def test_a_write_gives_what_starts_with_the_name_nothing(self):
        wda, element, snapshot = self.left_behind()
        with self.assertRaises(WriteInterrupted) as raised:
            wda.write_text(element, snapshot, "pilot 1", append=True)
        self.assertIsInstance(raised.exception.__cause__, NoKeyboardFocus)
        self.assertEqual(wda.fields[0]["value"], "kept")
        self.assertEqual((wda.keys, wda.posts("/value"), wda.posts("/wda/keys")), ([], 0, 0))

    def test_a_replacement_neither_clears_nor_writes_it(self):
        wda, element, _ = self.left_behind()
        self.assertFalse(wda.replace_text(element, "pilot 1"))
        self.assertEqual(wda.fields[0]["value"], "kept")
        self.assertEqual((wda.posts("/clear"), wda.posts("/value"), wda.keys), (0, 0, []))


class LabelThatChangesOnFocus(unittest.TestCase):
    """v2-pilot-2, 3 Nov: WDA's query for the field stops matching after its focusing tap. Still a failure, read as
    unknown: nothing that only starts with the name stands in for the field, and a fresh look names it again."""

    def test_it_fails_stale_gives_no_field_anything_and_a_fresh_look_can_write(self):
        wda = FormWDA(field("Title", "Enter a title..."), field("Description", "Enter a description..."))
        self.addCleanup(wda.close)
        element, snapshot = wda.named("Description")
        with self.assertRaises(WriteInterrupted) as raised:
            wda.write_text(element, snapshot, "pilot 1", append=True)
        self.assertEqual(raised.exception.__cause__.wda_error, "stale element reference")
        self.assertTrue(outcome_unknown(raised.exception, "TYPE"))
        self.assertEqual((wda.keys, wda.posts("/wda/keys"), wda.posts("/actions")), ([], 0, 0))
        element, snapshot = wda.named("Description\nEnter a description...")
        wda.write_text(element, snapshot, "pilot 1", append=True)
        self.assertEqual(wda.keys, [("Description", "pilot 1")])

    def test_a_stale_field_gets_no_keystrokes_even_when_it_holds_focus(self):
        wda = FormWDA(field("Title"), field("Description"), query_breaks=True)
        self.addCleanup(wda.close)
        element, snapshot = wda.named("Description")
        with self.assertRaises(WriteInterrupted) as raised:
            wda.write_text(element, snapshot, "pilot 1", append=True)
        self.assertEqual(raised.exception.__cause__.wda_error, "stale element reference")
        self.assertEqual((wda.posts("/value"), wda.posts("/wda/keys"), wda.keys), (1, 0, []))
        self.assertTrue(outcome_unknown(raised.exception, "TYPE"))

    def test_the_type_operation_keeps_the_error_and_it_reads_as_unknown(self):
        wda = FormWDA(field("Title"), field("Description"), query_breaks=True)
        self.addCleanup(wda.close)
        element, snapshot = wda.named("Description")
        with self.assertRaises(TransportError) as raised:
            wda.execute("TYPE", element, snapshot, text="pilot 1", timeout=5)
        self.assertEqual(raised.exception.wda_error, "stale element reference")
        self.assertTrue(outcome_unknown(raised.exception, "TYPE"))
        self.assertEqual((wda.posts("/value"), wda.keys), (1, []))


class NoRefocusOverAnOverlap(unittest.TestCase):
    """Astra acf21ae2 R2: focus held over another field takes no tap, whatever a later read of focus says. The tap would
    land on ``Caption``, which comes first."""

    def overlapping(self):
        wda = FormWDA(field("Caption", y=0.25), field("Body", role="TextView", value="old", y=0.25), focus="Body")
        self.addCleanup(wda.close)
        return (wda, *wda.named("Body"))

    def test_held_focus_takes_keys_with_no_tap(self):
        wda, element, snapshot = self.overlapping()
        wda.write_text(element, snapshot, " more", append=True)
        self.assertEqual(wda.posts("/actions"), 0)
        self.assertEqual([f["value"] for f in wda.fields], ["", "old more"])

    def test_a_focus_read_failing_at_any_point_leads_to_no_tap(self):
        for first_failed in (1, 2, 3):
            with self.subTest(first_failed=first_failed):
                wda, element, snapshot = self.overlapping()

                def unreadable(path, count):
                    if path == "/element/active" and count >= first_failed:
                        raise TransportError("timed out")

                wda.before = unreadable
                wda.write_text(element, snapshot, " more", append=True)
                self.assertEqual(wda.posts("/actions"), 0)
                self.assertEqual(wda.fields[0]["value"], "")


class CompoundWrites(unittest.TestCase):
    """Astra ed840f85 B1: the last request's answer says nothing about the requests before it."""

    def interrupted(self, wda, *args, **kwargs):
        self.addCleanup(wda.close)
        element, snapshot = wda.named(args[0])
        with self.assertRaises(WriteInterrupted) as raised:
            wda.write_text(element, snapshot, *args[1:], **kwargs)
        return raised.exception

    def test_a_read_refused_after_the_text_went_in_is_unknown_for_every_op(self):
        wda = FormWDA(field("Title"))
        real = wda.observe
        reads = []

        def observe(timeout=10):
            if wda.keys and not reads:
                reads.append(1)
                raise StaleSession("HTTP 404 StaleSession: invalid session id; nothing sent; not retried")
            return real(timeout)

        wda.observe = observe
        error = self.interrupted(wda, "Title", "pilot 1", append=True)
        self.assertNotIn("StaleSession", str(error))
        for op in ("TYPE", "TYPE_SUBMIT", "SET_TEXT"):
            self.assertTrue(outcome_unknown(error, op), op)

    def test_a_named_refusal_after_a_clear_proves_nothing_about_the_clear(self):
        filled = field("Title", value="old text")
        wda = FormWDA(
            filled,
            before=raising(
                "/value", TransportError("HTTP 400; request not retried", status=400, wda_error="invalid argument")
            ),
        )
        error = self.interrupted(wda, "Title", "pilot 1")
        self.assertEqual(filled["value"], "")
        self.assertEqual(error.__cause__.wda_error, "invalid argument")
        self.assertTrue(outcome_unknown(error, "SET_TEXT"))

    def test_a_failure_on_return_after_the_text_is_unknown_and_return_isnt_sent_again(self):
        wda = FormWDA(
            field("Title"), before=raising("/wda/keys", TransportError("HTTP 500; request not retried", status=500))
        )
        self.interrupted(wda, "Title", "pilot 1", append=True, submit=True)
        self.assertEqual(wda.keys, [("Title", "pilot 1")])
        self.assertEqual(wda.posts("/wda/keys"), 1)

    def test_a_write_refused_before_anything_went_out_keeps_its_own_error(self):
        wda = FormWDA(
            field("Title"), before=raising("/value", NotSent("HTTP ConnectionRefusedError; nothing sent; not retried"))
        )
        self.addCleanup(wda.close)
        element, snapshot = wda.named("Title")
        with self.assertRaises(NotSent):
            wda.write_text(element, snapshot, "pilot 1", append=True)
        with self.assertRaises(ValueError):
            wda.write_text(element, snapshot, "bad\x07text", append=True)
        self.assertEqual(wda.keys, [])


class UnprovenClear(unittest.TestCase):
    """Astra 74130fb6: a field known to hold text gets nothing more until its clear is acknowledged."""

    def refused(self, wda, label, text):
        self.addCleanup(wda.close)
        element, snapshot = wda.named(label)
        with self.assertRaises(WriteInterrupted) as raised:
            wda.write_text(element, snapshot, text)
        self.assertIsInstance(raised.exception.__cause__, FieldNotCleared)
        return raised.exception

    def test_a_filled_unnamed_field_with_no_focus_proof_is_refused_before_any_typing(self):
        wda = FormWDA(field("", role="TextView", value="old text"))
        error = self.refused(wda, "", "pilot 1")
        self.assertEqual((wda.fields[0]["value"], wda.keys), ("old text", []))
        self.assertEqual((wda.posts("/value"), wda.posts("/wda/keys")), (0, 0))
        self.assertTrue(outcome_unknown(error, "SET_TEXT"))

    def test_a_repair_with_no_clear_doesnt_type_a_second_time(self):
        wda = FormWDA(field("Notes"))
        self.addCleanup(wda.close)
        real, sent = wda.into, []

        def garbled(f, text):
            sent.append(text)
            real(f, text + "x")

        wda.into = garbled
        wda._clear_field = lambda *a: False
        wda._active_id = lambda *a, **k: None
        element, snapshot = wda.named("Notes")
        with self.assertRaises(WriteInterrupted) as raised:
            wda.write_text(element, snapshot, "pilot 1")
        self.assertIsInstance(raised.exception.__cause__, FieldNotCleared)
        self.assertEqual(sent, ["pilot 1"])

    def test_an_acknowledged_clear_still_replaces(self):
        filled = field("Title", value="old text")
        wda = FormWDA(filled)
        self.addCleanup(wda.close)
        element, snapshot = wda.named("Title")
        receipt = wda.write_text(element, snapshot, "pilot 1")
        self.assertEqual(filled["value"], "pilot 1")
        self.assertTrue(receipt["matches"])
        self.assertEqual(wda.posts("/clear"), 1)


class Envelope(unittest.TestCase):
    """Astra 74130fb6: WDA answers every command with a value, null included."""

    def test_an_answer_with_no_value_is_unknown_and_never_success(self):
        for result in ({}, {"sessionId": "s1"}):
            with self.assertRaises(TransportError) as raised:
                WDA.response_value(result)
            self.assertIn("outcome unknown", str(raised.exception))
            for op in ("TAP", "TYPE", "SET_TEXT", "SWIPE_UP"):
                self.assertTrue(outcome_unknown(raised.exception, op), op)

    def test_a_null_value_is_a_good_answer(self):
        self.assertIsNone(WDA.response_value({"value": None, "sessionId": "s1"}))
        self.assertEqual(WDA.response_value({"value": [1]}), [1])

    def test_an_error_in_the_value_is_still_a_named_rejection(self):
        with self.assertRaises(TransportError) as raised:
            WDA.response_value({"value": {"error": "invalid argument", "message": "private"}})
        self.assertEqual((raised.exception.status, raised.exception.wda_error), (200, "invalid argument"))


class LoopWDA(FormWDA):
    """Enough of a driver for the agent's loop, on the real writer."""

    recovered = 0

    def observe_ready(self, timeout=10):
        return self.observe(timeout)

    def wait_for_change(self, snapshot, timeout=2, wait_seconds=0.6, **kwargs):
        return self.observe(timeout)

    def tune(self, **kwargs):
        return {}

    def recover(self, timeout=None):
        self.recovered += 1


def loop(wda, steps):
    events = []
    FrontierAgent(
        wda, F.Script(list(steps) + [("DONE", None, None)] * 3), emit=events.append, screenshots=False, settle_seconds=0
    ).run("Write it.")
    return events


class RecoveryAndHoldInTheLoop(unittest.TestCase):
    def stale_read_after(self, wda):
        real, spent = wda.observe, []

        def observe(timeout=10):
            if wda.keys and not spent:
                spent.append(1)
                raise StaleSession("HTTP 404 StaleSession: invalid session id; nothing sent; not retried")
            return real(timeout)

        wda.observe = observe

    def test_a_dropped_session_after_delivered_text_doesnt_write_it_again(self):
        for op in ("TYPE", "SET_TEXT"):
            wda = LoopWDA(field("Title"))
            self.addCleanup(wda.close)
            self.stale_read_after(wda)
            events = loop(wda, [(op, "Title", "pilot 1")])
            self.assertEqual(wda.fields[0]["value"], "pilot 1", op)
            self.assertEqual(wda.posts("/value"), 1, op)
            self.assertEqual([e["outcome"] for e in events if e["event"] == "frontier_failed"], ["unknown"], op)

    def test_a_second_set_text_after_an_interrupted_one_that_landed_is_refused(self):
        wda = LoopWDA(field("Title"))
        self.addCleanup(wda.close)
        self.stale_read_after(wda)
        events = loop(wda, [("SET_TEXT", "Title", "pilot 1")] * 2)
        self.assertEqual(wda.posts("/value"), 1)
        self.assertEqual(wda.keys, [("Title", "pilot 1")])
        self.assertIn("outcome_unknown", [e.get("label") for e in events if e["event"] == "frontier_refused"])

    def test_a_second_set_text_goes_only_after_a_fresh_look_settles_the_first(self):
        wda = LoopWDA(
            field("Title"), before=raising("/value", TransportError("HTTP 500; request not retried", status=500))
        )
        self.addCleanup(wda.close)
        events = loop(wda, [("SET_TEXT", "Title", "pilot 1")] * 2)
        self.assertEqual([e["reason"] for e in events if e["event"] == "frontier_retry"], ["outcome_unknown_unchanged"])
        self.assertEqual(wda.keys, [("Title", "pilot 1")])
        self.assertEqual(wda.posts("/value"), 2)

    def test_replacing_with_new_text_after_a_good_one_isnt_held(self):
        wda = LoopWDA(field("Title"))
        self.addCleanup(wda.close)
        events = loop(wda, [("SET_TEXT", "Title", "pilot 1"), ("SET_TEXT", "Title", "pilot 2")])
        self.assertEqual(wda.fields[0]["value"], "pilot 2")
        self.assertNotIn("outcome_unknown", [e.get("label") for e in events if e["event"] == "frontier_refused"])

    def test_a_session_dropped_before_anything_went_out_still_recovers_and_sends_once(self):
        wda = LoopWDA(
            field("Title"),
            before=raising(
                "/value", StaleSession("HTTP 404 StaleSession: invalid session id; nothing sent; not retried")
            ),
        )
        self.addCleanup(wda.close)
        loop(wda, [("TYPE", "Title", "pilot 1")])
        self.assertEqual(wda.recovered, 1)
        self.assertEqual(wda.posts("/value"), 2)
        self.assertEqual(wda.keys, [("Title", "pilot 1")])


class UnreadableAnswers(unittest.TestCase):
    """Astra ed840f85 B3: what can't be read, after sending, proves nothing."""

    def ask(self, status, body):
        server = serve(status, body)
        client = HTTP(f"http://127.0.0.1:{server.server_address[1]}", timeout=5)
        try:
            return client.request("GET", "/session/x/source")
        finally:
            client.close()
            server.shutdown()
            server.server_close()

    def test_a_good_answer_that_isnt_an_object_is_unknown(self):
        with self.assertRaises(TransportError) as raised:
            self.ask(200, [1, 2])
        self.assertIn("outcome unknown", str(raised.exception))
        for op in ("TAP", "TYPE", "SET_TEXT"):
            self.assertTrue(outcome_unknown(raised.exception, op), op)

    def test_a_failed_answer_keeps_its_status_and_name_and_nothing_else(self):
        with self.assertRaises(TransportError) as raised:
            self.ask(404, {"value": {"error": "stale element reference", "message": "private text"}})
        self.assertEqual((raised.exception.status, raised.exception.wda_error), (404, "stale element reference"))
        self.assertEqual(str(raised.exception), "HTTP 404; request not retried")

    def test_a_body_with_two_error_fields_names_nothing(self):
        self.assertIsNone(answer_error(b'{"value": {"error": "invalid argument", "error": "unknown error"}}'))
        self.assertIsNone(answer_error(b'{"value": {"error": "invalid argument"}, "value": {"error": "x"}}'))
        self.assertEqual(answer_error(b'{"value": {"error": "invalid argument"}}'), "invalid argument")

    def test_a_tap_answered_with_nothing_readable_is_unknown_and_a_named_one_stays_known(self):
        self.assertTrue(outcome_unknown(TransportError("HTTP 400; request not retried", status=400), "TAP"))
        self.assertTrue(
            outcome_unknown(TransportError("HTTP response exceeds limit; outcome unknown; not retried"), "TAP")
        )
        self.assertTrue(outcome_unknown(TransportError("anything at all"), "TAP"))
        self.assertFalse(
            outcome_unknown(
                TransportError("HTTP 400; request not retried", status=400, wda_error="invalid argument"), "TAP"
            )
        )
        self.assertTrue(
            outcome_unknown(
                TransportError("HTTP 400; request not retried", status=400, wda_error="invalid argument"), "TYPE"
            )
        )


class Recorded(unittest.TestCase):
    """Astra ed840f85 S2: a replay reads the same safety facts as the run did."""

    def test_status_and_name_survive_the_tape(self):
        error = decode_error(
            json.loads(
                json.dumps(
                    encode_error(
                        TransportError("HTTP 400; request not retried", status=400, wda_error="invalid argument")
                    )
                )
            )
        )
        self.assertEqual(
            (error.status, error.wda_error, str(error)), (400, "invalid argument", "HTTP 400; request not retried")
        )
        self.assertFalse(outcome_unknown(error, "TAP"))

    def test_an_interrupted_write_stays_one_on_the_tape(self):
        error = decode_error(
            json.loads(
                json.dumps(
                    encode_error(WriteInterrupted("The write stopped partway; request outcome unknown; not retried"))
                )
            )
        )
        self.assertIsInstance(error, WriteInterrupted)
        self.assertTrue(outcome_unknown(error, "TYPE"))

    def test_a_legacy_record_or_a_bad_name_proves_nothing(self):
        legacy = decode_error(
            {"type": "TransportError", "module": "mobile_agent.transport", "message": "HTTP 400; request not retried"}
        )
        self.assertEqual((legacy.status, legacy.wda_error), (None, None))
        self.assertTrue(outcome_unknown(legacy, "TAP"))
        bad = decode_error(
            {
                "type": "TransportError",
                "module": "mobile_agent.transport",
                "message": "x",
                "status": "400",
                "wda_error": "<invalid argument>",
            }
        )
        self.assertEqual((bad.status, bad.wda_error), (None, None))
        self.assertTrue(outcome_unknown(bad, "TAP"))


if __name__ == "__main__":
    unittest.main()
