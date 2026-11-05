import asyncio
import os
import threading
import time
import unittest
from unittest import mock

from mobile_agent.app_choice import infer_app
from mobile_agent.catalog import APPS
from mobile_agent.tests.test_cli_bugs import clean_env, fake_wda
from mobile_agent.tui.session import Session

try:
    import textual  # noqa: F401
except ImportError:  # pragma: no cover - the UI is an install-time dependency
    textual = None


ENV = {"MOBSTER_ASK_BEFORE_ACTING": "1", "MOBSTER_BYPASS_CHECKS": "0", "MOBSTER_NO_DEVICES": "1"}


def demo_app(**kwargs):
    from mobile_agent.tui.app import MobsterApp
    return MobsterApp(lambda: Session(demo=True, pace=0), demo=True, bell=False, **kwargs)


async def until(pilot, predicate, timeout=15):
    deadline = time.monotonic() + timeout * float(os.environ.get("MOBSTER_TEST_TIME_SCALE", "1"))
    while time.monotonic() < deadline:
        await pilot.pause(.02)
        if predicate():
            return
    raise AssertionError("the UI never reached the expected state")


def screen_text(app):
    import html
    import re
    svg = app.export_screenshot()
    return html.unescape(re.sub(r"<[^>]+>", "", svg)).replace("\xa0", " ")


async def submit(pilot, text):
    pilot.app.query_one("#prompt").value = text
    await pilot.pause(.02)
    await pilot.press("enter")


@unittest.skipIf(textual is None, "Textual is not installed")
class AppCardTests(unittest.TestCase):
    APP_CARD = {"id": "c1", "kind": "commit", "act": "use app", "title": "Allow Mobster to use Messages and Notes?",
                "apps": ["Messages", "Notes"], "rememberable": True, "rememberLabel": "Always allow"}

    def test_a_tasks_app_card_offers_always_allow(self):
        from mobile_agent.tui.widgets import ApprovalCard
        card = ApprovalCard()
        card.request = dict(self.APP_CARD)
        self.assertEqual(card.options(), [("__yes", "Allow"), ("__always", "Always allow"), ("__no", "Not now")])
        card.request = {**self.APP_CARD, "reason": "Allow is for this task. Always allow keeps them for later tasks."}
        drawn = []
        with mock.patch.object(card.body, "update", lambda text: drawn.append(text.plain)):
            card.redraw()
        self.assertEqual([line.strip() for line in drawn[-1].splitlines()[:6]],
                         ["Allow Mobster to use Messages and Notes?",
                          "Allow is for this task. Always allow keeps them for later tasks.", "",
                          "❯ 1. Allow   y", "2. Always allow   a", "3. Not now   n or esc"])
        card.request = {"id": "c2", "kind": "commit", "act": "send_message", "title": "Send this?",
                        "rememberable": True, "rememberLabel": "Don't ask again for sending messages in Messages"}
        self.assertEqual([key for key, _ in card.options()], ["__yes", "__no"])
        card.request = {**self.APP_CARD, "rememberable": False}  # a store that can't keep it: Allow and Not now
        self.assertEqual([key for key, _ in card.options()], ["__yes", "__no"])

    def test_always_allow_reaches_the_run_and_allow_doesnt(self):
        from types import SimpleNamespace

        from mobile_agent.tui.app import MobsterApp
        sent = []
        fake = SimpleNamespace(session=object(), running="run", hide_approval=lambda: None,
                               send_answer=lambda *args, **kwargs: sent.append((args, kwargs)))
        MobsterApp.answer_approval(fake, self.APP_CARD, "__always")
        MobsterApp.answer_approval(fake, self.APP_CARD, "__yes")
        MobsterApp.answer_approval(fake, self.APP_CARD, "__no")
        self.assertEqual(sent, [(("run", "c1", True, None), {"remember": True}),
                                (("run", "c1", True, None), {"remember": False}),
                                (("run", "c1", False, None), {"remember": False})])
        answered = []
        run = SimpleNamespace(answer_approval=lambda *args, **kwargs: answered.append((args, kwargs)) or True)
        session = object.__new__(Session)
        session.run = run
        self.assertTrue(session.answer("c1", True, run=run, remember=True))
        self.assertTrue(session.answer("c1", True, run=run))
        self.assertEqual(answered, [(("c1", True, None), {"remember": True}),
                                    (("c1", True, None), {"remember": False})])


@unittest.skipIf(textual is None, "Textual is not installed")
@mock.patch.dict(os.environ, ENV)
class TerminalUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)  # the UI's first frame is slower than debug mode likes

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
            self.assertIn("Test and automate apps on real iPhones.", screen_text(app))

    async def test_a_narrow_header_drops_the_demo_note_rather_than_cut_it(self):
        app = demo_app()
        async with app.run_test(size=(60, 20)) as pilot:
            await until(pilot, lambda: app.session is not None)
            header = app.query_one("#header").render().plain
            self.assertLessEqual(len(header), 58, header)
            self.assertTrue(header.endswith("DEMO"), header)
            await pilot.resize_terminal(100, 30)
            await pilot.pause(.1)
            self.assertTrue(app.query_one("#header").render().plain.endswith("no iPhone, no model calls"))

    async def test_the_idle_screen_names_the_version_once_and_reads_from_the_top(self):
        from mobile_agent import __version__
        app = demo_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            self.assertEqual(screen_text(app).count(f"Mobster {__version__}"), 1)
            self.assertEqual(app.query_one("#header").region.y, 1)  # one row under the top edge
            self.assertLess(app.query_one("#welcome").region.y, 6)  # not pushed to the foot
            self.assertIn("ask before acting", app.query_one("#status").render().plain)
            await submit(pilot, "What's on my calendar tomorrow?")
            await until(pilot, lambda: app.run_view is not None)
            self.assertFalse(app.query_one("#transcript").has_class("idle"))
        async with demo_app().run_test(size=(80, 24)) as pilot:
            self.assertEqual(pilot.app.query_one("#header").region.y, 0)  # a short terminal keeps its rows

    async def test_a_short_terminal_drops_the_mark_and_keeps_the_examples(self):
        from mobile_agent.tui.theme import mark_text
        mark = mark_text().plain.splitlines()[0].strip()
        app = demo_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await until(pilot, lambda: app.session is not None)
            text = screen_text(app)
            self.assertNotIn(mark, text)
            self.assertIn("Test and automate apps on real iPhones.", text)
            self.assertIn("What's on my calendar tomorrow?", text)
            await pilot.resize_terminal(100, 40)
            await pilot.pause(.1)
            self.assertIn(mark, screen_text(app))

    async def test_a_task_streams_its_steps_and_finishes(self):
        app = demo_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Turn on Dark Mode")
            await until(pilot, lambda: app.run_view is not None and app.running is None
                        and app.run_view.narrator.status != "running")
            view = app.run_view
            self.assertEqual(view.run.app["name"], "Settings")
            self.assertEqual(view.narrator.status, "completed")
            titles = [item.title for item in view.narrator.items if hasattr(item, "phases")]
            self.assertIn("Tapped “Dark”", titles)
            self.assertGreaterEqual(view.steps, 3)

    async def test_an_approval_waits_for_yes(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Text Alex 'on my way'")
            card = app.query_one("#approval")
            # The card opens, then takes the focus a moment later: wait for both, not for the first and a guess.
            await until(pilot, lambda: card.has_class("open") and app.focused is card)
            self.assertIs(app.focused, card)
            self.assertIn("Send", card.body.render().plain)
            status = app.query_one("#status").render().plain
            self.assertIn("ctrl+c to stop", status)  # esc answers the card, so the status line names ctrl+c
            self.assertNotIn("esc to stop", status)
            await pilot.press("y")
            await until(pilot, lambda: app.running is None and app.run_view.narrator.status != "running")
            self.assertEqual(app.run_view.run.status, "completed")
            self.assertFalse(card.has_class("open"))
            self.assertEqual(app.focused.id, "prompt")

    async def answers_to(self, request, *keys):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            sent = []
            app.session.answer = lambda *args, **kwargs: sent.append(args)
            app.show_approval(request)
            await pilot.pause(.05)
            await pilot.press(*keys)
            await until(pilot, lambda: sent)
            self.assertFalse(app.query_one("#approval").has_class("open"))
        return sent[0][:3]

    CHOICES = {"id": "q1", "kind": "question", "question": "Which one?", "defaultChoice": "b",
               "choices": [{"id": "a", "label": "First"}, {"id": "b", "label": "Second"},
                           {"id": "c", "label": "Third"}]}

    async def test_a_long_app_card_can_be_read_and_the_next_card_starts_at_the_top(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            names = [f"App{i}: " + "long app name " * 10 for i in range(9)]
            app.show_approval({**AppCardTests.APP_CARD, "apps": names})
            await pilot.pause(.05)
            card = app.query_one("#approval")
            self.assertGreater(card.max_scroll_y, 0)
            self.assertIn("App0:", screen_text(app))
            await pilot.press("pagedown", "pagedown")
            await pilot.pause(.05)
            self.assertGreater(card.scroll_y, 0)
            self.assertIn("Always allow", screen_text(app))
            self.assertIn("App8:", screen_text(app))
            app.show_approval(AppCardTests.APP_CARD)
            await pilot.pause(.05)
            self.assertEqual(card.scroll_y, 0)
            self.assertIn("Messages and Notes?", screen_text(app))

    async def test_a_number_picks_that_choice(self):
        self.assertEqual(await self.answers_to(self.CHOICES, "3"), ("q1", True, "c"))

    async def test_enter_picks_the_choice_the_cursor_moved_to(self):
        self.assertEqual(await self.answers_to(self.CHOICES, "down", "enter"), ("q1", True, "c"))

    async def test_y_on_a_choice_card_picks_the_highlighted_choice_never_a_yes(self):
        self.assertEqual(await self.answers_to(self.CHOICES, "y"), ("q1", True, "b"))

    async def test_stopping_a_choice_card_refuses_and_picks_nothing(self):
        for key in ("4", "n", "escape"):
            with self.subTest(key=key):
                self.assertEqual(await self.answers_to(self.CHOICES, key), ("q1", False, None))

    async def test_n_declines_an_action_card(self):
        action = {"id": "a1", "kind": "action", "type": "send", "app": "Messages", "text": "on my way"}
        self.assertEqual(await self.answers_to(action, "n"), ("a1", False, None))

    async def test_a_status_that_lands_after_the_ui_closed_changes_nothing(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
        app.status_ready({"device_ready": True})
        self.assertTrue(app.status["device_ready"])

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
            self.assertIn("full access", app.query_one("#status").render().plain)

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
        db = os.path.join(tempfile.mkdtemp(), "terminal.sqlite3")
        app = MobsterApp(lambda: Session(wda_url="http://127.0.0.1:9", history_db=db), bell=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: bool(app.status))
            await until(pilot, lambda: "Building an iOS app?" in screen_text(app))
            text = screen_text(app)
            self.assertIn("Get set up", text)
            self.assertIn("plug it in with a cable and unlock it", text)
            self.assertIn("Building an iOS app?", text)  # the simulator path, for developers, last
            for jargon in ("WDA", "WebDriverAgent", "env file", "--env-file", "scripts/", "docs/", "8200"):
                self.assertNotIn(jargon, text)
            self.assertIn("No iPhone yet", app.query_one("#header").render().plain)
            await submit(pilot, "Turn on Dark Mode")
            await until(pilot, lambda: "Couldn't start" in screen_text(app))

    async def test_a_handle_stays_in_the_task(self):
        app = demo_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Follow @nasa in TikTok")
            await until(pilot, lambda: app.run_view is not None)
            self.assertEqual(app.run_view.run.goal, "Follow @nasa in TikTok")
            self.assertEqual(app.run_view.run.app["name"], "TikTok")
            goal, named = app.split_mentions("@messages text Alex, cc @getmobster.")
            self.assertEqual((goal, named["name"]), ("text Alex, cc @getmobster.", "Messages"))

    async def test_tab_takes_the_app_whose_name_starts_with_the_letters(self):
        app = demo_app()
        picked = {}
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            for typed in ("@fa", "@me", "@voi"):
                app.app_choice = None
                app.query_one("#prompt").value = typed
                await pilot.pause(.1)
                await pilot.press("tab")
                await pilot.pause(.05)
                picked[typed] = (app.app_choice or {}).get("name")
        self.assertEqual(picked, {"@fa": "FaceTime", "@me": "Messages", "@voi": "Voice Memos"})

    async def test_quitting_mid_task_prints_how_long_it_ran(self):
        from mobile_agent.tui.app import MobsterApp
        app = MobsterApp(lambda: Session(demo=True, pace=.5), demo=True, bell=False)
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Turn on Dark Mode")
            await until(pilot, lambda: app.run_view is not None and app.run_view.narrator.actions >= 1, timeout=30)
            await pilot.pause(.3)
            await pilot.press("ctrl+d")
        summary = app.transcript_summary()
        app.session.close(5)
        self.assertNotIn(", 0 ms", summary)
        self.assertIn("Stopped when you quit", summary)


@unittest.skipIf(textual is None, "Textual is not installed")
@mock.patch.dict(os.environ, ENV)
class PhonePaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_the_text_pane_outlines_the_screen_and_marks_the_target(self):
        app = demo_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            self.assertTrue(app.query_one("#phone").has_class("text"))
            screen = {"app": "Messages", "bundle": "com.apple.MobileSMS", "keyboard": "", "elements": [
                {"id": "0", "label": "Messages", "role": "Button", "rect": [.02, .065, .24, .035]},
                {"id": "4", "label": "Send", "role": "Button", "rect": [.85, .9, .1, .045]}]}
            from mobile_agent.tui.widgets import PhonePanel
            panel = app.query_one(PhonePanel)
            app.show_outline = lambda: panel.show_outline(screen, "4", "Messages")
            app.show_outline()
            await until(pilot, lambda: "Send" in app.query_one("#phone-outline").render().plain)
            outline = app.query_one("#phone-outline").render().plain
            self.assertIn("Messages", outline)
            marked = next(line for line in outline.splitlines() if line.startswith("❯"))
            self.assertIn("Send", marked)

    async def test_the_image_pane_takes_the_screenshot(self):
        from textual_image.widget import HalfcellImage  # stands in for the kitty and sixel widgets here

        from mobile_agent.tui.app import MobsterApp
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


@unittest.skipIf(textual is None, "Textual is not installed")
class CommandRegistryTests(unittest.TestCase):
    ALIASES = {"device": "devices"}  # the old name of /devices, kept so it still works

    def test_every_command_has_a_handler_and_every_handler_a_command(self):
        from mobile_agent.tui.app import MobsterApp
        from mobile_agent.tui.keys import COMMANDS
        names = {command.name for command in COMMANDS}
        handlers = {name[len("command_"):] for name in dir(MobsterApp) if name.startswith("command_")}
        self.assertEqual(names - handlers, set(), "a command with no handler")
        self.assertEqual(handlers - names - set(self.ALIASES), set(), "a handler with no command")

    def test_the_docs_list_every_command(self):
        from pathlib import Path

        from mobile_agent.tui.keys import COMMANDS
        docs = (Path(__file__).resolve().parents[2] / "docs" / "tui.mdx").read_text()
        for command in COMMANDS:
            self.assertIn(f"`/{command.name}", docs, f"docs/tui.mdx misses /{command.name}")


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
    INSTALLED = [{"id": bundle, "name": name, "bundleId": bundle} for bundle, name in (
        ("com.ubercab.UberClient", "Uber"), ("com.zimride.instant", "Lyft"), ("com.airbnb.app", "Airbnb"),
        ("com.dd.doordash", "DoorDash"), ("net.whatsapp.WhatsApp", "WhatsApp"))]

    def test_the_shared_cases_the_dashboard_port_runs(self):
        import json
        from pathlib import Path
        cases = Path(__file__).resolve().parents[2] / "dashboard" / "src" / "test" / "infer-app-cases.json"
        if not cases.exists():
            self.skipTest("the dashboard is not in this checkout")
        fixture = json.loads(cases.read_text())
        for goal, expected in fixture["cases"]:
            with self.subTest(goal=goal):
                found = infer_app(goal, fixture["apps"])
                self.assertEqual(found and found["name"], expected)
        for goal, expected in fixture["installedCases"]:
            with self.subTest(goal=goal, installed=True):
                found = infer_app(goal, fixture["apps"] + fixture["installed"])
                self.assertEqual(found and found["name"], expected)

    def test_the_cases_the_shared_file_lacks(self):
        cases = {"Text my photos to Mom": "Messages",
                 "Find my phone": "Find My",
                 "Find my phone number": None,
                 "Open Find My": "Find My",
                 "Share my location in Find My": "Find My",
                 "Check Find My for my keys": "Find My",
                 "Open Photos": "Photos"}
        self.assertEqual({goal: (infer_app(goal, [*APPS, *self.INSTALLED]) or {}).get("name") for goal in cases},
                         cases)


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


class DoctorReportTests(unittest.TestCase):
    def test_a_failed_check_shows_its_glyph_label_fix_and_the_count(self):
        from mobile_agent import doctor
        from mobile_agent.tui import render
        checks = [doctor.Check("python", "Python", doctor.OK, "3.12.1"),
                  doctor.Check("key", "Model key", doctor.FAIL, "none found", "mobster login")]
        text = render.doctor_report(checks)
        self.assertEqual(text.plain.splitlines()[0], f"✓ {'Python':<18}3.12.1")
        self.assertIn(f"✗ {'Model key':<18}none found", text.plain)
        self.assertIn(f"  {'':<18}→ mobster login", text.plain)
        self.assertTrue(text.plain.endswith("1 thing to fix before your first task."))
        self.assertEqual(render.doctor_report(checks[:1]).plain.splitlines()[-1], "Ready for tasks.")


@unittest.skipIf(textual is None, "Textual is not installed")
class WorkerShutdown(unittest.TestCase):
    def test_a_queued_timer_does_not_redraw_after_shutdown(self):
        app = demo_app()
        app.running = object()  # the task can outlive the UI's widgets
        app.frame = 4  # the next timer callback would refresh the missing status widget
        app.tick()
        self.assertEqual(app.frame, 4)

    def test_late_and_already_queued_worker_updates_are_discarded_after_shutdown(self):
        from textual.app import App
        app = demo_app()
        update = mock.Mock()
        app.call_from_thread(update)
        update.assert_not_called()

        def dispatch(callback):
            app._running = False  # shutdown starts after the worker queues its update, before it executes
            return callback()

        app._running = True
        with mock.patch.object(App, "call_from_thread", side_effect=dispatch):
            app.call_from_thread(update)
        update.assert_not_called()

    def test_running_worker_updates_still_execute_and_report_errors(self):
        from textual.app import App
        app = demo_app()
        app._running = True
        try:
            with mock.patch.object(App, "call_from_thread", side_effect=lambda callback: callback()):
                self.assertEqual(app.call_from_thread(lambda value: value + 1, 2), 3)
                with self.assertRaisesRegex(RuntimeError, "real error"):
                    app.call_from_thread(mock.Mock(side_effect=RuntimeError("real error")))
        finally:
            app._running = False


@unittest.skipIf(textual is None, "Textual is not installed")
@mock.patch.dict(os.environ, ENV)
class SettingsWrites(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_a_failed_old_save_cannot_overwrite_a_new_choice_with_the_same_value(self):
        app = demo_app()
        started, release = threading.Event(), threading.Event()
        workers, attempts = [], []
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            persist, schedule = app.session.set_setting, app.save_setting

            def save(name, value):
                attempts.append(value)
                if len(attempts) == 1:
                    started.set()
                    release.wait()
                    raise OSError("save failed")
                return persist(name, value)

            def scheduled(*args):
                workers.append(schedule(*args))

            with mock.patch.object(app.session, "set_setting", save), mock.patch.object(app, "save_setting", scheduled):
                try:
                    app.change_setting("askBeforeActing", False)
                    await until(pilot, started.is_set)
                    app.change_setting("askBeforeActing", True)
                    app.change_setting("askBeforeActing", False)
                finally:
                    release.set()
                await until(pilot, lambda: all(worker.is_finished for worker in workers))
            self.assertFalse(app.settings["askBeforeActing"])
            self.assertFalse(app.session.settings()["askBeforeActing"])

    async def test_a_failed_readback_does_not_end_the_app(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            with mock.patch.object(app.session, "set_setting", side_effect=OSError("save failed")), \
                    mock.patch.object(app.session, "settings", side_effect=OSError("service closed")):
                schedule, workers = app.save_setting, []
                with mock.patch.object(app, "save_setting", side_effect=lambda *args: workers.append(schedule(*args))):
                    app.change_setting("askBeforeActing", False)
                    await until(pilot, lambda: all(worker.is_finished for worker in workers))
                self.assertTrue(app.is_running)

    async def test_a_refused_save_restores_the_runtime_setting(self):
        app = demo_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            before = app.session.settings()
            desired = not before["askBeforeActing"]
            with mock.patch.object(app.session, "set_setting", side_effect=OSError("save failed")):
                schedule, workers = app.save_setting, []
                with mock.patch.object(app, "save_setting", side_effect=lambda *args: workers.append(schedule(*args))):
                    app.change_setting("askBeforeActing", desired)
                    await until(pilot, lambda: all(worker.is_finished for worker in workers))
            self.assertEqual(app.settings["askBeforeActing"], before["askBeforeActing"])
            self.assertEqual(app.settings["permissionMode"], before["permissionMode"])

    async def test_an_older_save_cannot_restore_another_setting(self):
        app = demo_app()
        first_saved, second_saved, release = threading.Event(), threading.Event(), threading.Event()
        workers = []
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.session is not None)
            persist, schedule = app.session.set_setting, app.save_setting

            def save(name, value):
                result = persist(name, value)
                if name == "askBeforeActing":
                    first_saved.set()
                    release.wait()
                else:
                    second_saved.set()
                return result

            def scheduled(*args):
                workers.append(schedule(*args))

            with mock.patch.object(app.session, "set_setting", save), mock.patch.object(app, "save_setting", scheduled):
                try:
                    app.change_setting("askBeforeActing", False)
                    await until(pilot, first_saved.is_set)
                    app.change_setting("hearPhone", True)
                    await asyncio.to_thread(second_saved.wait, .2)
                finally:
                    release.set()
                await until(pilot, lambda: all(worker.is_finished for worker in workers))
            self.assertFalse(app.settings["askBeforeActing"])
            self.assertTrue(app.settings["hearPhone"])
            self.assertEqual(app.settings["permissionMode"], "dont_ask")
            self.assertTrue(app.session.settings()["hearPhone"])


@unittest.skipIf(textual is None, "Textual is not installed")
@mock.patch.dict(os.environ, ENV)
class AppFlag(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_an_installed_apps_bundle_id_is_chosen(self):
        from mobile_agent.tui.app import MobsterApp
        instagram = {"id": "com.burbn.instagram", "name": "Instagram", "bundleId": "com.burbn.instagram",
                     "installed": True, "foundOnPhone": True}

        def factory():
            session = Session(demo=True, pace=0)
            session.apps = lambda: [dict(app) for app in APPS] + [instagram]  # the phone's installed apps
            return session
        for flag in ("com.burbn.instagram", "Instagram"):
            app = MobsterApp(factory, demo=True, bell=False, app_id=flag)
            async with app.run_test(size=(120, 36)) as pilot:
                await until(pilot, lambda: app.session is not None
                            and any(a["id"] == instagram["id"] for a in app.apps))
                await pilot.pause(.1)
                self.assertEqual((app.app_choice or {}).get("bundleId"), "com.burbn.instagram", flag)
                self.assertIn("Instagram", app.query_one("#chip").render().plain)
            app.session.close(5)

    async def test_an_unknown_name_says_so(self):
        app = demo_app(app_id="Instagrm")
        notes = []
        async with app.run_test(size=(120, 36)) as pilot:
            original = app.notify
            app.notify = lambda message, *a, **k: (notes.append(message), original(message, *a, **k))
            await until(pilot, lambda: app.session is not None)
            await until(pilot, lambda: notes)
            self.assertIsNone(app.app_choice)
        self.assertIn("No app called Instagrm", notes[0])

    async def test_a_phone_not_ready_yet_is_asked_again_when_it_is(self):
        import socket
        import tempfile

        from mobile_agent.tui.app import MobsterApp
        with socket.socket() as probe:  # a loopback port nothing listens on: WDA still coming up
            probe.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{probe.getsockname()[1]}"
        history = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
        instagram = {"id": "com.burbn.instagram", "name": "Instagram", "bundleId": "com.burbn.instagram",
                     "installed": True, "foundOnPhone": True}
        with mock.patch.dict(os.environ, clean_env(**ENV), clear=True):
            app = MobsterApp(lambda: Session(wda_url=url, history_db=history), bell=False,
                             app_id="com.burbn.instagram")
            notes = []
            original = app.notify
            app.notify = lambda message, *a, **k: (notes.append(message), original(message, *a, **k))
            async with app.run_test(size=(120, 36)) as pilot:
                await until(pilot, lambda: any("Couldn't read the phone's apps yet" in note for note in notes))
                self.assertFalse(any("No app called" in note for note in notes), notes)
                self.assertEqual(app.requested_app, "com.burbn.instagram")
                self.assertIsNone(app.app_choice)
                self.assertIn("com.burbn.instagram", app.query_one("#chip").render().plain)
                await submit(pilot, "Like the latest post")
                await pilot.pause(.1)
                self.assertIsNone(app.run_view)
                self.assertIn("Still looking for com.burbn.instagram", notes[-1])
                reads = []
                catalog = [dict(item) for item in app.apps]
                app.session.apps = lambda fresh=False: (reads.append(fresh), [*catalog, instagram])[1]
                app.status_ready({**app.status, "device_ready": True})
                await until(pilot, lambda: app.app_choice is not None)
                self.assertEqual(reads, [True])
                self.assertEqual(app.app_choice["bundleId"], "com.burbn.instagram")
                self.assertIsNone(app.requested_app)
                self.assertIn("Instagram", app.query_one("#chip").render().plain)
            app.session.close(2)

    async def test_a_phone_whose_apps_cannot_be_read_says_so(self):
        import tempfile

        from mobile_agent.tui.app import MobsterApp
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        history = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
        with mock.patch.dict(os.environ, clean_env(**ENV), clear=True):
            app = MobsterApp(lambda: Session(wda_url=url, history_db=history), bell=False,
                             app_id="com.burbn.instagram")
            notes = []
            original = app.notify
            app.notify = lambda message, *a, **k: (notes.append(message), original(message, *a, **k))
            async with app.run_test(size=(120, 36)) as pilot:
                await until(pilot, lambda: any("Couldn't read the phone's apps to find" in note for note in notes))
                self.assertIsNone(app.requested_app)
                self.assertIsNone(app.app_choice)
                self.assertFalse(any("No app called" in note for note in notes), notes)
            app.session.close(2)


@unittest.skipIf(textual is None, "Textual is not installed")
class SmartOnly(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_a_task_without_a_start_app_leaves_the_choice_to_runtime(self):
        import tempfile

        from mobile_agent.tui.app import MobsterApp
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        history = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
        with mock.patch.dict(os.environ, clean_env(**ENV, OPENAI_API_KEY="sk-test-0000000000000000000000"),
                             clear=True):
            app = MobsterApp(lambda: Session(wda_url=url, history_db=history), bell=False)
            async with app.run_test(size=(100, 40)) as pilot:
                await until(pilot, lambda: app.session is not None and app.status.get("device_ready"))
                app.session.threads = lambda: None
                app.session.start = mock.Mock(side_effect=ValueError("captured before dispatch"))
                app.task_refused = mock.Mock()
                app.submit_task(None, "Find the requested information", "auto")
                await until(pilot, lambda: app.task_refused.called)
                self.assertIsNone(app.session.start.call_args.args[0])
            app.session.close(2)

    async def test_the_ui_names_the_model_and_refuses_an_unknown_command(self):
        import tempfile

        from mobile_agent.tui.app import MobsterApp
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        history = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
        env = clean_env(**ENV, OPENAI_API_KEY="sk-test-0000000000000000000000")
        with mock.patch.dict(os.environ, env, clear=True):
            app = MobsterApp(lambda: Session(wda_url=url, history_db=history), bell=False)
            notes = []
            async with app.run_test(size=(100, 40)) as pilot:
                original = app.notify
                app.notify = lambda message, *a, **k: (notes.append(message), original(message, *a, **k))
                await until(pilot, lambda: app.session is not None and app.status.get("device_ready"))
                await pilot.pause(.2)
                self.assertEqual(list(app.status.get("engines")), ["smart"])
                self.assertIn("gpt-", app.model_words())
                await submit(pilot, "/model")
                await pilot.pause(.2)
                self.assertTrue(any("There's no /model" in note for note in notes), notes)
            app.session.close(2)

    async def test_no_key_at_all_shows_the_key_row_of_the_checklist(self):
        import tempfile

        from mobile_agent.tui.app import MobsterApp
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        history = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
        with mock.patch.dict(os.environ, clean_env(**ENV), clear=True):
            app = MobsterApp(lambda: Session(wda_url=url, history_db=history), bell=False)
            async with app.run_test(size=(100, 40)) as pilot:
                await until(pilot, lambda: app.session is not None and app.status.get("device_ready"))
                await pilot.pause(.2)
                text = screen_text(app)
                self.assertIn("Get set up", text)
                self.assertIn("Model key", text)
                self.assertIn("/login", text)
                self.assertNotIn("No OpenAI key", text)
                self.assertNotIn("OPENAI_API_KEY", text)
                self.assertIn("no key yet", app.model_words())
            app.session.close(2)


if __name__ == "__main__":
    unittest.main()
