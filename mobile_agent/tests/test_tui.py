"""The terminal UI, driven headlessly with Textual's pilot over the scripted demo phone.

The demo runs the real runtime and agent (``tui.session.DemoRuntime``); only the
phone and the decision policy are scripted, so these tests cover the whole path
from the prompt to an approval and a finished task. No device, no network.
"""

import asyncio
import os
import time
import unittest
from unittest import mock

try:
    import textual  # noqa: F401
except ImportError:  # pragma: no cover - the UI is an install-time dependency
    textual = None

# MOBSTER_NO_DEVICES: the checklist never lists a phone plugged into the Mac running the tests.
ENV = {"MOBSTER_ASK_BEFORE_ACTING": "1", "MOBSTER_BYPASS_CHECKS": "0", "MOBSTER_NO_DEVICES": "1"}


def demo_app(**kwargs):
    from mobile_agent.tui.app import MobsterApp
    from mobile_agent.tui.session import Session
    return MobsterApp(lambda: Session(demo=True, pace=0), demo=True, bell=False, **kwargs)


async def until(pilot, predicate, timeout=15):
    deadline = time.monotonic() + timeout * float(os.environ.get("MOBSTER_TEST_TIME_SCALE", "1"))
    while time.monotonic() < deadline:
        await pilot.pause(.02)
        if predicate():
            return
    raise AssertionError("the UI never reached the expected state")


def screen_text(app):
    """The screen as plain text (the SVG export without markup)."""
    import html
    import re
    svg = app.export_screenshot()
    return html.unescape(re.sub(r"<[^>]+>", "", svg)).replace("\xa0", " ")


async def submit(pilot, text):
    pilot.app.query_one("#prompt").value = text
    await pilot.pause(.02)
    await pilot.press("enter")


@unittest.skipIf(textual is None, "Textual is not installed")
@mock.patch.dict(os.environ, ENV)
class TerminalUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)  # the UI's first frame is slower than debug mode likes

    async def test_idle_screen_shows_the_demo_phone_and_examples(self):
        app = demo_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            svg = app.export_screenshot()
            self.assertIn("DEMO", svg)
            self.assertIn("Turn", svg)
            self.assertIn("ask", app.query_one("#status").render().plain)

    async def test_the_header_names_mobster_in_the_text_color(self):
        from mobile_agent.tui.render import TEXT
        app = demo_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await until(pilot, lambda: app.session is not None)
            header = app.query_one("#header").render()
            self.assertTrue(header.plain.startswith("Mobster 0."), header.plain)
            self.assertLessEqual(len(header.plain), 78)  # fits 80 columns beside the padding
            name = next(span for span in header.spans if span.start == 0)
            self.assertEqual((name.end, name.style.bold, name.style.foreground.hex.lower()), (7, True, TEXT.lower()))
            self.assertIn("Do anything on your iPhone with agents.", screen_text(app))

    async def test_a_task_streams_its_steps_and_finishes(self):
        app = demo_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Turn on Dark Mode")
            await until(pilot, lambda: app.run_view is not None and app.running is None
                        and app.run_view.narrator.status != "running")
            view = app.run_view
            self.assertEqual(view.run.app["name"], "Settings")
            self.assertEqual(view.narrator.status, "completed_unverified")
            titles = [item.title for item in view.narrator.items if hasattr(item, "phases")]
            self.assertIn("Tapped “Dark”", titles)
            self.assertGreaterEqual(view.steps, 3)

    async def test_an_approval_waits_for_yes(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Text Alex 'on my way'")
            await until(pilot, lambda: app.query_one("#approval").has_class("open"))
            card = app.query_one("#approval")
            self.assertIs(app.focused, card)
            self.assertIn("Send", card.render().plain)
            await pilot.press("y")
            await until(pilot, lambda: app.running is None and app.run_view.narrator.status != "running")
            self.assertEqual(app.run_view.run.status, "completed_unverified")
            self.assertFalse(card.has_class("open"))
            self.assertEqual(app.focused.id, "prompt")

    async def test_declining_ends_the_task_without_acting(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Text Alex 'on my way'")
            await until(pilot, lambda: app.query_one("#approval").has_class("open"))
            await pilot.press("escape")
            await until(pilot, lambda: app.running is None and app.run_view.narrator.status != "running")
            self.assertEqual(app.run_view.run.status, "approval_denied")
            self.assertIn("instead", app.query_one("#prompt").placeholder)

    async def test_escape_stops_a_running_task(self):
        from mobile_agent.tui.app import MobsterApp
        from mobile_agent.tui.session import Session
        app = MobsterApp(lambda: Session(demo=True, pace=.5), demo=True, bell=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Turn on Dark Mode")
            await until(pilot, lambda: app.running is not None)
            await pilot.press("escape")
            await until(pilot, lambda: app.running is None, timeout=20)
            self.assertEqual(app.run_view.run.status, "stopped")

    async def test_slash_opens_the_command_popup_and_enter_runs_it(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await pilot.press("slash", "h", "i", "s")
            self.assertTrue(app.suggestions_open())
            await pilot.press("enter")
            await pilot.pause(.1)
            self.assertEqual(type(app.screen).__name__, "Picker")
            await pilot.press("escape")

    async def test_at_mention_picks_the_app(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await pilot.press("at", "m", "e", "s", "s")
            self.assertTrue(app.suggestions_open())
            await pilot.press("tab")
            self.assertEqual(app.app_choice["name"], "Messages")
            self.assertEqual(app.query_one("#prompt").value, "")
            self.assertIn("Messages", app.query_one("#chip").render().plain)

    async def test_up_recalls_the_last_task_and_question_mark_opens_help(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Turn on Dark Mode")
            await until(pilot, lambda: app.run_view is not None and app.running is None
                        and app.run_view.narrator.status != "running")
            await pilot.press("up")
            self.assertEqual(app.query_one("#prompt").value, "Turn on Dark Mode")
            app.query_one("#prompt").value = ""
            await pilot.press("question_mark")
            await pilot.pause(.1)
            self.assertEqual(type(app.screen).__name__, "InfoScreen")

    async def test_shift_tab_turns_ask_before_acting_off_for_the_session(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await pilot.press("shift+tab")
            await until(pilot, lambda: app.session.settings()["askBeforeActing"] is False)
            self.assertIn("without asking", app.query_one("#status").render().plain)

    async def test_history_resume_puts_the_task_back_in_the_prompt(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Turn on Dark Mode")
            await until(pilot, lambda: app.running is None and app.run_view is not None
                        and app.run_view.narrator.status != "running")
            run_id = app.run_view.run.id
            app.action_clear()
            await pilot.press("ctrl+r")
            await pilot.pause(.1)
            await pilot.press("enter")
            await pilot.pause(.1)
            self.assertEqual(app.run_view.run.id, run_id)
            self.assertTrue(app.run_view.past)
            self.assertEqual(app.query_one("#prompt").value, "Turn on Dark Mode")

    async def test_ctrl_c_twice_quits_when_idle(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await pilot.press("ctrl+c")
            self.assertTrue(app.is_running)
            await pilot.press("ctrl+c")
            await pilot.pause(.1)
        self.assertFalse(app.is_running)
        app.session.close()

    async def test_no_phone_shows_setup_hints_and_refuses_the_task(self):
        import tempfile
        from mobile_agent.tui.app import MobsterApp
        from mobile_agent.tui.session import Session
        db = os.path.join(tempfile.mkdtemp(), "terminal.sqlite3")
        app = MobsterApp(lambda: Session(wda_url="http://127.0.0.1:9", history_db=db), bell=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: bool(app.status))
            text = screen_text(app)
            self.assertIn("Get set up", text)
            self.assertIn("plug it in with a cable and unlock it", text)
            self.assertIn("Building an iOS app?", text)  # the simulator path, for developers, last
            for jargon in ("WDA", "WebDriverAgent", "env file", "--env-file", "scripts/", "docs/", "8200"):
                self.assertNotIn(jargon, text)
            self.assertIn("No iPhone yet", app.query_one("#header").render().plain)
            await submit(pilot, "Turn on Dark Mode")
            await pilot.pause(.5)
            self.assertIn("Couldn't start", screen_text(app))


@unittest.skipIf(textual is None, "Textual is not installed")
@mock.patch.dict(os.environ, ENV)
class PhonePaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_the_text_pane_outlines_the_screen_and_marks_the_target(self):
        app = demo_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Text Alex 'on my way'")
            await until(pilot, lambda: app.query_one("#approval").has_class("open"))
            await pilot.pause(.2)
            self.assertTrue(app.query_one("#phone").has_class("text"))
            outline = app.query_one("#phone-outline").render().plain
            self.assertIn("‹ Messages", outline)
            self.assertIn("❯ ", outline)
            marked = next(line for line in outline.splitlines() if line.startswith("❯"))
            self.assertIn("Send", marked)
            await pilot.press("n")

    async def test_the_image_pane_takes_the_screenshot(self):
        from textual_image.widget import HalfcellImage  # stands in for the kitty and sixel widgets here
        from mobile_agent.tui.app import MobsterApp
        from mobile_agent.tui.session import Session
        app = MobsterApp(lambda: Session(demo=True, pace=0), demo=True, bell=False, image_class=HalfcellImage)
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Turn on Dark Mode")
            await until(pilot, lambda: app.running is None and app.run_view is not None
                        and app.run_view.narrator.status != "running")
            await until(pilot, lambda: app.query_one("#phone-image").image is not None)
            self.assertTrue(app.query_one("#phone").has_class("image"))

    def test_image_support_is_asked_only_where_it_is_likely(self):
        from mobile_agent.tui.images import image_widget, likely
        self.assertFalse(likely({"TERM": "xterm-256color", "TERM_PROGRAM": "Apple_Terminal"}))
        self.assertTrue(likely({"TERM": "xterm-kitty"}))
        self.assertTrue(likely({"TERM_PROGRAM": "iTerm.app"}))
        self.assertIsNone(image_widget({"TERM": "xterm-kitty", "MOBSTER_PHONE_VIEW": "text"}))
        self.assertIsNone(image_widget({"TERM": "xterm-256color"}))


class OutlineTests(unittest.TestCase):
    def test_a_settings_row_reads_once_with_its_value(self):
        from mobile_agent.tui.render import screen_outline
        elements = [
            {"id": "n", "label": "About", "role": "NavigationBar", "rect": [0, .07, 1, .06]},
            {"id": "b", "label": "General", "role": "Button", "rect": [.04, .07, .11, .05]},
            {"id": "c", "label": "iOS Version, 26.4", "role": "Cell", "rect": [.05, .21, .91, .06]},
            {"id": "t", "label": "iOS Version", "role": "StaticText", "rect": [.09, .23, .22, .03]},
            {"id": "s", "label": "Wi-Fi", "role": "Switch", "rect": [0, .3, 1, .06], "value": "1"},
        ]
        text = screen_outline({"app": "Settings", "elements": elements}, 30, 20, target_id="s").plain
        self.assertIn("‹ General", text)
        self.assertEqual(text.count("iOS Version"), 1)
        self.assertRegex(text, r"iOS Version +26\.4")
        self.assertRegex(text, r"❯ Wi-Fi +on")


class InferAppTests(unittest.TestCase):
    def test_names_and_hints(self):
        from mobile_agent.catalog import APPS
        from mobile_agent.tui.app import infer_app
        self.assertEqual(infer_app("Open Settings and turn on Wi-Fi", APPS)["name"], "Settings")
        self.assertEqual(infer_app("Text Alex that I'm late", APPS)["name"], "Messages")
        self.assertEqual(infer_app("Search for the best espresso in Rome", APPS)["name"], "Safari")
        self.assertEqual(infer_app("Find My iPhone", APPS)["name"], "Find My")
        self.assertIsNone(infer_app("do the thing", APPS))
        # Verbs count only at the start: this is a Settings task, not a message (a live run, 26 Sep).
        self.assertEqual(infer_app("Open Accessibility, then Display & Text Size", APPS)["name"], "Settings")
        self.assertEqual(infer_app("Please text Mom that dinner is ready", APPS)["name"], "Messages")
        self.assertEqual(infer_app("Turn on Dark Mode", APPS)["name"], "Settings")
        self.assertEqual(infer_app("Set an alarm for 7", APPS)["name"], "Clock")

    def test_the_shared_cases_the_dashboard_port_runs(self):
        import json
        from pathlib import Path
        from mobile_agent.tui.app import infer_app
        cases = Path(__file__).resolve().parents[2] / "dashboard" / "src" / "test" / "infer-app-cases.json"
        if not cases.exists():
            self.skipTest("the dashboard is not in this checkout")
        fixture = json.loads(cases.read_text())
        for goal, expected in fixture["cases"]:
            with self.subTest(goal=goal):
                found = infer_app(goal, fixture["apps"])
                self.assertEqual(found and found["name"], expected)
        # Apps from the phone that the catalog lacks: "Call an Uber" runs in Uber, not Phone.
        for goal, expected in fixture["installedCases"]:
            with self.subTest(goal=goal, installed=True):
                found = infer_app(goal, fixture["apps"] + fixture["installed"])
                self.assertEqual(found and found["name"], expected)

    def test_an_opening_verb_beats_an_app_name_that_is_an_everyday_word(self):
        from mobile_agent.catalog import APPS
        from mobile_agent.tui.app import infer_app
        self.assertEqual(infer_app("Text Sam I'm heading home", APPS)["name"], "Messages")
        self.assertIsNone(infer_app("What's the news today?", [*APPS, {"id": "news", "name": "News"}]))
        self.assertEqual(infer_app("Open Photos", APPS)["name"], "Photos")


class EntryTests(unittest.TestCase):
    def test_the_ui_refuses_a_pipe(self):
        import argparse
        import io
        from contextlib import redirect_stderr
        from mobile_agent.tui import main
        errors = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stderr(errors):
            self.assertEqual(main(argparse.Namespace()), 2)
        self.assertIn("mobster run", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
