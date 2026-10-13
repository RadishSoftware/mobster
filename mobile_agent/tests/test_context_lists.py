"""List memory under measurement (frontier.context_on): after an act-naming tap, the rows it changed in a page seen
before, on screen or behind a sheet (MOBSTER_CTX_READBACK); a list the agent looked at and left, kept compact in the
turn log (MOBSTER_CTX_KEPT). Each states an observed fact, and only with its switch on. Offline: fake drivers and
scripted models."""

import unittest

from mobile_agent.frontier import content_rows, kept_list, list_rows, readback
from mobile_agent.state import Element
from mobile_agent.tests.test_context_marks import off, on, run, screen, tab_bar


class ReadBackTests(unittest.TestCase):
    """MOBSTER_CTX_READBACK: after an act-naming tap, the rows it changed in a page seen before, on screen or behind
    a sheet."""

    def test_the_rows_that_changed_and_nothing_when_the_page_moved(self):
        before = {"Banana, 105": 1, "Oatmeal, 150": 1}
        self.assertEqual(readback("Today", before, {"Banana, 105": 1, "Oatmeal, 150": 2}),
                         'read back from Today: "Oatmeal, 150" 2 rows (1 before)')
        self.assertEqual(readback("Today", before, before), "")
        moved = {f"Row {i}": 1 for i in range(8)}
        self.assertEqual(readback("Today", before, moved), "")  # a scroll changes every row: no write to read back

    def today(self, oatmeal):
        return [Element("1", "Today", "NavigationBar", (0, .07, 1, .12)),
                Element("2", "Banana, 1 medium banana, 105", "Button", (0, .63, 1, .06)),
                *(Element(str(3 + i), "Oatmeal, 1 cup cooked, 150", "Button", (0, .7 + .07 * i, 1, .06))
                  for i in range(oatmeal)),
                Element("8", "Add", "Button", (.88, .58, .08, .04))]

    def sheet(self, behind, title="Oatmeal"):
        snapshot = screen(Element("20", title, "NavigationBar", (0, .09, 1, .12)),
                          Element("21", "Add Food" if title == "Oatmeal" else "Close", "Button", (.04, .9, .92, .06)))
        snapshot.behind = tuple(behind)
        return snapshot

    def run_add(self):
        # CalTrack goes back to its search sheet after Add Food; the Today list it wrote to is behind it.
        today = screen(*self.today(1))
        after = self.sheet(self.today(2), "Search Food")
        steps = [("TAP", "Add", None), ("TAP", "Add Food", None), ("DONE", None, None), ("DONE", None, None)]
        return run("Log one oatmeal for breakfast in CalTrack.", [today, self.sheet(self.today(1)), after, after,
                                                                  after], steps)

    def test_an_add_reads_back_the_list_behind_its_sheet(self):
        with on("READBACK"):
            _, _, prompts = self.run_add()
        self.assertIn("TAP 'Add Food' -> screen changed; read back from Today: "
                      '"Oatmeal, 1 cup cooked, 150" 2 rows (1 before)', prompts[2])

    def test_off_no_read_back(self):
        with off():
            _, _, prompts = self.run_add()
        self.assertNotIn("read back", prompts[2])

    def test_the_driver_keeps_the_screen_a_sheet_covers_from_the_same_read(self):
        from mobile_agent.drivers import WDA
        from mobile_agent.tests.test_stacked_panes import SHEET
        driver = WDA("http://localhost:8100", "test")
        calls = []

        def call(method, path, body=None, timeout=10):
            calls.append(path)
            if path.startswith("/source"):
                return SHEET
            if path == "/elements":
                return [{"ELEMENT": "layer-1"}]
            if path.endswith("/attribute/visible"):
                return False
            return None
        driver.call = call
        snapshot = driver.observe(timeout=5)
        self.assertEqual([e.label for e in snapshot.elements], ["Search Food", "Close"])
        self.assertEqual([e.label for e in snapshot.behind], ["Today", "Add"])  # evidence only, never a target
        self.assertEqual(sum(path.startswith("/source") for path in calls), 1)  # no read beyond the one source


class KeptListTests(unittest.TestCase):
    """MOBSTER_CTX_KEPT: a list the agent looked at and left, compact in the turn log."""

    LISTS = [Element("1", "My Lists", "NavigationBar", (0, .07, 1, .12)),
             Element("2", "Wishlist", "Button", (.04, .36, .46, .04), selected=True),
             Element("3", "Create custom list", "Button", (0, .24, 1, .06)),
             Element("4", "2 movies in wishlist (by release date)", "StaticText", (0, .43, 1, .05)),
             Element("5", "Dune, 78%, Sep 15, 2021", "Button", (0, .48, 1, .2)),
             Element("6", "Parasite, 85%, May 30, 2019", "Button", (0, .7, 1, .2)),
             *tab_bar()]
    FILM = [Element("1", "The Drama", "NavigationBar", (0, .07, 1, .06)),
            Element("2", "Wishlist", "Button", (.1, .45, .2, .03)), *tab_bar()]

    def test_a_list_is_rows_stacked_alike_and_kept_compact(self):
        snapshot = screen(*self.LISTS)
        rows = list_rows(snapshot.elements, 402, 874)
        self.assertEqual(rows, ["Create custom list", "Wishlist", "2 movies in wishlist (by release date)",
                                "Dune, 78%, Sep 15, 2021", "Parasite, 85%, May 30, 2019"])
        self.assertNotIn("Movies", content_rows(snapshot.elements, 402, 874))  # the tab bar is chrome
        self.assertEqual(kept_list("My Lists › Wishlist", rows),
                         "My Lists › Wishlist: Create custom list; 2 movies in wishlist (by relea…; "
                         "Dune, 78%, Sep 15, 2021; Parasite, 85%, May 30, 2019")
        self.assertEqual(list_rows(screen(*self.FILM).elements, 402, 874), [])  # a detail page holds no list

    def steps(self, leave):
        return [("TAP", "My Lists", None), leave, ("DONE", None, None), ("DONE", None, None)]

    def test_a_list_looked_at_and_left_stays_in_the_turn_log(self):
        film, lists = screen(*self.FILM), screen(*self.LISTS)
        with on("KEPT"):
            _, _, prompts = run("Is The Drama on my Wishlist in Cinephile?", [film, lists, film, film, film],
                                self.steps(("TAP", "Movies", None)))
        self.assertIn("\n  read: My Lists › Wishlist: Create custom list; 2 movies in wishlist (by relea…; Dune, 78%",
                      prompts[2])
        with off():
            _, _, prompts = run("Is The Drama on my Wishlist in Cinephile?", [film, lists, film, film, film],
                                self.steps(("TAP", "Movies", None)))
        self.assertNotIn("read: ", prompts[2])

    def test_a_run_keeps_at_most_three_lists(self):
        from mobile_agent.frontier import KEPT_LISTS, FrontierAgent
        agent = FrontierAgent.__new__(FrontierAgent)
        agent._turn_log, agent._turn_reads = ["T0"], {}
        agent._app_name = lambda snapshot: "Cinephile"
        lists, film = screen(*self.LISTS), screen(*self.FILM)
        self.assertTrue(agent._kept_read(lists, film, [], None))
        agent._turn_reads = {step: f"list {step}" for step in range(KEPT_LISTS)}
        self.assertEqual(agent._kept_read(lists, film, [], None), "")  # uncapped, mem-030 grew 6.4% (up to 32%)

    def test_a_list_acted_on_is_not_kept(self):
        film, lists = screen(*self.FILM), screen(*self.LISTS)
        with on("KEPT"):
            _, _, prompts = run("Open Dune in my Cinephile Wishlist.", [film, lists, film, film, film],
                                self.steps(("TAP", "Dune, 78%, Sep 15, 2021", None)))
        self.assertNotIn("read: ", prompts[2])


if __name__ == "__main__":
    unittest.main()
