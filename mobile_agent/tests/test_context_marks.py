"""Row marks under measurement (frontier.context_on): a search button that isn't a field, a row hidden under a bottom
bar (MOBSTER_CTX_MARKS), a status text that came with its page (MOBSTER_CTX_PROVENANCE). Each states an observed fact,
and only with its switch on. Offline: fake drivers and scripted models."""

import os
import unittest
import unittest.mock

from mobile_agent.frontier import PROVENANCE_MARK, UNDER_BAR_MARK, FrontierAgent, Provenance, screen_rows, view_of
from mobile_agent.state import Element, Snapshot
from mobile_agent.tests.test_frontier import Driver, Script


SWITCHES = ("MARKS", "PROVENANCE", "READBACK", "KEPT")


def on(*names):
    """These switches on and the others off, whatever the environment says."""
    return unittest.mock.patch.dict(os.environ, {f"MOBSTER_CTX_{name}": "on" if name in names else "off"
                                                 for name in SWITCHES})


def off():
    return on()


def screen(*elements, bundle="com.example.app"):
    return Snapshot(list(elements), "\n".join(e.label for e in elements), 402, 874, "synthetic_fixture",
                    bundle_id=bundle)


def tab_bar(first_id=90):
    return [Element(str(first_id + i), label, "Button", (.04 + .24 * i, .91, .2, .06))
            for i, label in enumerate(("Movies", "Discover", "Fan Club", "My Lists"))]


def run(request, screens, steps, **kwargs):
    driver, prompts = Driver(screens), []
    script = Script(steps)
    complete = script.complete

    def recording(messages, schema, timeout=60):
        from mobile_agent.frontier import prompt_text
        if "items" not in schema["properties"]:
            prompts.append(prompt_text(messages))
        return complete(messages, schema, timeout)
    script.complete = recording
    result = FrontierAgent(driver, script, screenshots=False, settle_seconds=0, **kwargs).run(request)
    return driver, result, prompts


class MarksTests(unittest.TestCase):
    """MOBSTER_CTX_MARKS: a search button that isn't a field, a row hidden under a bottom bar."""

    SEARCH = screen(Element("1", "Search MegaMart", "Button", (0, .1, 1, .05)),
                    Element("2", "Search", "Button", (.3, .91, .2, .06)),
                    Element("3", "Reply in thread", "Button", (0, .3, 1, .05)),
                    Element("4", "Search products", "SearchField", (0, .2, 1, .05), True))

    def test_a_search_prompt_button_is_marked_not_editable(self):
        with on("MARKS"):
            lines = screen_rows(self.SEARCH)[1]
        self.assertIn('e1 Button "Search MegaMart" (not editable) @0,10,100,5', lines)
        for line in lines[1:]:  # a one-word tab, an action button and a real field are not marked
            self.assertNotIn("(not editable)", line)

    def test_off_the_rows_are_as_before(self):
        with off():
            self.assertEqual(screen_rows(self.SEARCH)[1][0], 'e1 Button "Search MegaMart" @0,10,100,5')

    def test_a_row_listed_before_a_bottom_bar_it_sits_under_is_marked_hidden(self):
        # QuickChat (multi-087): a search result listed before the tab bar, drawn behind it.
        snapshot = screen(Element("1", "Weekend Trip", "Button", (.04, .43, .92, .1)),
                          Element("2", "Sofia Kim", "StaticText", (.08, .92, .2, .02)),
                          *tab_bar(),
                          Element("9", "Got It!", "Button", (.15, .92, .12, .03)))
        with on("MARKS"):
            lines = screen_rows(snapshot)[1]
        self.assertTrue(lines[1].startswith('e2 StaticText "Sofia Kim" ' + UNDER_BAR_MARK))
        # The row above the bar, the bar's own buttons and a tooltip listed after the bar (drawn over it) are not.
        self.assertEqual([line for line in lines if UNDER_BAR_MARK in line], [lines[1]])
        with off():
            self.assertNotIn(UNDER_BAR_MARK, "\n".join(screen_rows(snapshot)[1]))


class ProvenanceTests(unittest.TestCase):
    """MOBSTER_CTX_PROVENANCE: a status text that came with its page, before any act of the agent's."""

    FILM = [Element("1", "The Drama", "NavigationBar", (0, .07, 1, .06)),
            Element("2", "Wishlist", "Button", (.1, .45, .2, .03))]
    RESULTS = screen(Element("1", "Movies", "NavigationBar", (0, .07, 1, .06)),
                     Element("2", "The Drama, 68%", "Button", (0, .3, 1, .1)))
    TOAST = Element("3", "Added successfully", "StaticText", (.4, .86, .2, .02))

    def marks(self, screens_and_acted):
        provenance = Provenance()
        for snapshot, acted in screens_and_acted:
            provenance.observe(view_of("Cinephile", snapshot), snapshot.elements, acted)
        return provenance.marks(snapshot.elements)

    def test_a_status_that_came_with_a_page_opened_by_a_plain_tap_is_marked(self):
        film = screen(*self.FILM, self.TOAST)
        self.assertEqual(self.marks([(self.RESULTS, False), (film, False)]), {"3": PROVENANCE_MARK})

    def test_a_status_after_an_act_or_on_screen_before_is_not(self):
        film = screen(*self.FILM, self.TOAST)
        self.assertEqual(self.marks([(self.RESULTS, False), (film, True)]), {})  # "Add" opened the page
        before = screen(*self.FILM)
        self.assertEqual(self.marks([(self.RESULTS, False), (before, False), (film, True)]), {})  # came after a tap here
        with_toast = screen(*self.RESULTS.elements, self.TOAST)
        self.assertEqual(self.marks([(with_toast, False), (film, False)]), {})  # was on the screen before

    def test_texts_that_only_mention_a_state_are_not_statuses(self):
        film = screen(*self.FILM, *(Element(str(10 + i), label, "StaticText", (0, .5 + .04 * i, 1, .03))
                                    for i, label in enumerate(("Saved posts", "Updated 8:35 PM", "Sent", "Move to saved"))))
        self.assertEqual(self.marks([(self.RESULTS, False), (film, False)]), {})

    def test_the_mark_reaches_the_prompt_only_when_on(self):
        film = screen(*self.FILM, self.TOAST)
        steps = [("TAP", "The Drama, 68%", None), ("DONE", None, None), ("DONE", None, None)]
        request = "Add a drama film to my Wishlist in Cinephile."
        with on("PROVENANCE"):
            _, _, prompts = run(request, [self.RESULTS, film, film, film], list(steps))
        self.assertIn('"Added successfully" ' + PROVENANCE_MARK, prompts[1])
        with off():
            _, _, prompts = run(request, [self.RESULTS, film, film, film], list(steps))
        self.assertNotIn(PROVENANCE_MARK, prompts[1])


if __name__ == "__main__":
    unittest.main()
