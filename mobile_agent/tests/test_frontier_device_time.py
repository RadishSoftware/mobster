"""Device-time and stale-screen fixes from the frontier trace review (9 Oct): Safari's address opened directly, and no
tap on search results that are still changing.
Offline: scripted model and fake drivers."""

import unittest
import unittest.mock

from mobile_agent import frontier
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, Script, screen

SAFARI = "com.apple.mobilesafari"


class Writing(Driver):
    """A driver with write_text: records whether each write appended, and answers with a receipt."""

    def __init__(self, screens, matches=True):
        super().__init__(screens)
        self.writes, self.matches = [], matches

    def write_text(self, element, snapshot, text, append=False, submit=False, timeout=30):
        self.writes.append((element.label, text, append, submit))
        self.index += 1
        return {"method": "type", "value": text if self.matches else text + text, "expected": text,
                "matches": self.matches, "repaired": False, "flattened": False, "chars": len(text),
                "submitted": bool(submit and self.matches)}


class RecordingScript(Script):
    def complete(self, messages, schema, timeout=60):
        if "actions" in schema["properties"]:
            from mobile_agent.frontier import prompt_text
            self.prompts_seen = getattr(self, "prompts_seen", []) + [prompt_text(messages)]
        return super().complete(messages, schema, timeout)


class SubmitIntoForeignText(unittest.TestCase):
    def run_write(self, field, text, matches=True):
        after = screen(Element("2", "Results", "StaticText", (0, .3, 1, .05)), bundle=field_bundle(field))
        driver = Writing([screen(field, bundle=field_bundle(field)), after, after, after], matches=matches)
        script = RecordingScript([[("TYPE_SUBMIT", field.label, text)], ("DONE", None, None), ("DONE", None, None)])
        FrontierAgent(driver, script, screenshots=False, settle_seconds=0).run("Search the web for " + text)
        return driver, script

    def test_a_submit_into_an_old_query_replaces_it(self):
        driver, _ = self.run_write(Element("1", "Address", "TextField", (0, .9, 1, .05), True, value="Grace Hopper"),
                                   "en.m.wikipedia.org/wiki/IOS_26")
        self.assertEqual(driver.writes[0][2], False)  # replaced, never appended to "Grace Hopper"

    def test_typing_into_a_note_body_still_appends(self):
        body = Element("1", "Body", "TextView", (0, .2, 1, .5), True, value="Shopping list")
        driver = Writing([screen(body), screen(body), screen(body)])
        FrontierAgent(driver, Script([("TYPE", "Body", "eggs"), ("DONE", None, None), ("DONE", None, None)]),
                      screenshots=False, settle_seconds=0).run("Add eggs to my shopping list note")
        self.assertEqual(driver.writes[0][2], True)

    def test_an_unmatched_submit_tells_the_model_return_was_not_pressed(self):
        driver, script = self.run_write(Element("1", "Search", "TextField", (0, .1, 1, .05), True),
                                        "Statue of Liberty", matches=False)
        self.assertIn("Return was not pressed", script.prompts_seen[1])


def field_bundle(field):
    return SAFARI if field.label == "Address" else "com.example.app"


class ReadsDriver(Driver):
    """Each observe returns the next scripted read (the screen keeps changing on its own); actions are recorded."""

    def __init__(self, reads):
        super().__init__(reads)
        self.reads = list(reads)

    def observe(self, timeout=10):
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, getattr(target, "label", target), text))


def contacts(*rows, typed="Bench"):
    return screen(Element("s", "Search", "SearchField", (0, .1, 1, .05), True, value=typed), *rows,
                  bundle="com.apple.MobileAddressBook")


class SubmitSettle(unittest.TestCase):
    REQUEST = "In Contacts, open Bench Tester and report the company."

    def run_chain(self, reads):
        driver = ReadsDriver(reads)
        events = []
        with unittest.mock.patch("time.sleep"), unittest.mock.patch.object(frontier, "SUBMIT_SETTLE_SECONDS", 0.0):
            result = FrontierAgent(driver, Script([[("TYPE_SUBMIT", "Search", "Bench"), ("TAP", "Bench Tester", None)],
                                                   ("DONE", None, None), ("DONE", None, None)]),
                                   emit=events.append, screenshots=False, settle_seconds=0).run(self.REQUEST)
        return driver, result, events

    def test_a_tap_on_settled_results_goes_ahead(self):
        row = Element("r", "Bench Tester", "Cell", (0, .3, 1, .06))
        settled = contacts(row)
        driver, result, events = self.run_chain([contacts(typed=""), settled, settled, settled, settled, settled])
        self.assertIn(("TAP", "Bench Tester", None), driver.actions)

    def test_results_still_moving_are_never_tapped(self):
        reads = [contacts(typed="")] + [contacts(Element("r", "Bench Tester", "Cell", (0, .3 + .05 * i, 1, .06)),
                                                  Element(f"x{i}", f"Suggestion {i}", "Cell", (0, .2, 1, .06)))
                                         for i in range(12)]
        driver, result, events = self.run_chain(reads)
        self.assertNotIn(("TAP", "Bench Tester", None), driver.actions)
        self.assertIn("results_moving", [e.get("reason") for e in events if e["event"] == "frontier_chunk_stop"])


if __name__ == "__main__":
    unittest.main()
