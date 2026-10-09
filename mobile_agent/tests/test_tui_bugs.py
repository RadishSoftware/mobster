"""The terminal UI's bug sweep (CLI-n): each test failed before its fix.

Driven headlessly with Textual's pilot, over the scripted demo phone or a loopback
fake WebDriverAgent. No device, no network and no model calls: no test submits a task
to a real engine.
"""

import asyncio
import os
import unittest
from unittest import mock

from mobile_agent.tests.test_cli_bugs import clean_env, fake_wda
from mobile_agent.tests.test_tui import ENV, demo_app, screen_text, submit, textual, until


@unittest.skipIf(textual is None, "Textual is not installed")
@mock.patch.dict(os.environ, ENV)
class Mentions(unittest.IsolatedAsyncioTestCase):
    """CLI-5: every @word was deleted from the task, so "Follow @nasa" reached the agent as "Follow"."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_a_handle_stays_in_the_task(self):
        app = demo_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Follow @nasa in TikTok")
            await until(pilot, lambda: app.run_view is not None)
            self.assertEqual(app.run_view.run.goal, "Follow @nasa in TikTok")
            self.assertEqual(app.run_view.run.app["name"], "TikTok")
            # An @word that names an app picks it and leaves the task; a handle stays.
            goal, named = app.split_mentions("@messages text Alex, cc @sam.")
            self.assertEqual((goal, named["name"]), ("text Alex, cc @sam.", "Messages"))


@unittest.skipIf(textual is None, "Textual is not installed")
@mock.patch.dict(os.environ, ENV)
class MentionRanking(unittest.IsolatedAsyncioTestCase):
    """CLI-12: the @ popup listed bundle-ID matches first: "@fa" + tab picked Safari, "@me" Files."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

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


@unittest.skipIf(textual is None, "Textual is not installed")
@mock.patch.dict(os.environ, ENV)
class AppFlag(unittest.IsolatedAsyncioTestCase):
    """CLI-9: `--app` was dropped without a word for an installed app outside the catalog, or a typo."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_an_installed_apps_bundle_id_is_chosen(self):
        from mobile_agent.catalog import APPS
        from mobile_agent.tui import _app_id
        from mobile_agent.tui.app import MobsterApp
        from mobile_agent.tui.session import Session
        instagram = {"id": "com.burbn.instagram", "name": "Instagram", "bundleId": "com.burbn.instagram",
                     "installed": True, "foundOnPhone": True}

        def factory():
            session = Session(demo=True, pace=0)
            session.apps = lambda: [dict(app) for app in APPS] + [instagram]  # the phone's installed apps
            return session
        for flag in ("com.burbn.instagram", "Instagram"):
            app = MobsterApp(factory, demo=True, bell=False, app_id=_app_id(flag))
            async with app.run_test(size=(120, 36)) as pilot:
                await until(pilot, lambda: app.session is not None and any(a["id"] == instagram["id"] for a in app.apps))
                await pilot.pause(.1)
                self.assertEqual((app.app_choice or {}).get("bundleId"), "com.burbn.instagram", flag)
                self.assertIn("Instagram", app.query_one("#chip").render().plain)
            app.session.close(5)

    async def test_an_unknown_name_says_so(self):
        from mobile_agent.tui import _app_id
        app = demo_app(app_id=_app_id("Instagrm"))
        notes = []
        async with app.run_test(size=(120, 36)) as pilot:
            original = app.notify
            app.notify = lambda message, *a, **k: (notes.append(message), original(message, *a, **k))
            await until(pilot, lambda: app.session is not None)
            await until(pilot, lambda: notes)
            self.assertIsNone(app.app_choice)
        self.assertIn("No app called Instagrm", notes[0])

    async def test_a_phone_not_ready_yet_is_asked_again_when_it_is(self):
        """With WDA not answering yet the list was the catalog alone, so --app was reported missing and dropped
        for good, and the first task ran in an inferred app."""
        import socket
        import tempfile
        from mobile_agent.tui import _app_id
        from mobile_agent.tui.app import MobsterApp
        from mobile_agent.tui.session import Session
        with socket.socket() as probe:  # a loopback port nothing listens on: WDA still coming up
            probe.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{probe.getsockname()[1]}"
        history = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
        instagram = {"id": "com.burbn.instagram", "name": "Instagram", "bundleId": "com.burbn.instagram",
                     "installed": True, "foundOnPhone": True}
        with mock.patch.dict(os.environ, clean_env(**ENV), clear=True):
            app = MobsterApp(lambda: Session(wda_url=url, history_db=history), bell=False,
                             app_id=_app_id("com.burbn.instagram"))
            notes = []
            original = app.notify
            app.notify = lambda message, *a, **k: (notes.append(message), original(message, *a, **k))
            async with app.run_test(size=(120, 36)) as pilot:
                await until(pilot, lambda: any("Couldn't read the phone's apps yet" in note for note in notes))
                self.assertFalse(any("No app called" in note for note in notes), notes)
                self.assertEqual(app.requested_app, "com.burbn.instagram")
                self.assertIsNone(app.app_choice)
                self.assertIn("com.burbn.instagram", app.query_one("#chip").render().plain)
                # A task typed meanwhile waits for that app instead of running in an inferred one.
                await submit(pilot, "Like the latest post")
                await pilot.pause(.1)
                self.assertIsNone(app.run_view)
                self.assertIn("Still looking for com.burbn.instagram", notes[-1])
                # The phone answers: its apps are read again, fresh, and the one --app named is chosen.
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
        """Ready, but its list is still the catalog alone (a simulator): --app is given up with that reason."""
        import tempfile
        from mobile_agent.tui import _app_id
        from mobile_agent.tui.app import MobsterApp
        from mobile_agent.tui.session import Session
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        history = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
        with mock.patch.dict(os.environ, clean_env(**ENV), clear=True):
            app = MobsterApp(lambda: Session(wda_url=url, history_db=history), bell=False,
                             app_id=_app_id("com.burbn.instagram"))
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
@mock.patch.dict(os.environ, ENV)
class QuitSummary(unittest.IsolatedAsyncioTestCase):
    """CLI-17: quitting during a task printed "0 ms" and no line saying it stopped."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_quitting_mid_task_prints_how_long_it_ran(self):
        from mobile_agent.tui.app import MobsterApp
        from mobile_agent.tui.session import Session
        app = MobsterApp(lambda: Session(demo=True, pace=.5), demo=True, bell=False)
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.session is not None)
            await submit(pilot, "Turn on Dark Mode")
            await until(pilot, lambda: app.run_view is not None and app.run_view.narrator.actions >= 1, timeout=30)
            await pilot.pause(.3)
            await pilot.press("ctrl+d")
        summary = app.transcript_summary()
        app.session.close(5)
        self.assertNotIn(" · 0 ms", summary)
        self.assertIn("Stopped when you quit", summary)


@unittest.skipIf(textual is None, "Textual is not installed")
class SmartOnly(unittest.IsolatedAsyncioTestCase):
    """CLI-4: with only an OpenAI key (Smart), the welcome asked for a Jev key, the status line named
    jev-latest, and /preview let a task be refused later for a missing Jev key."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_the_ui_describes_smart_and_says_what_a_preview_needs(self):
        import tempfile
        from mobile_agent.tui.app import MobsterApp
        from mobile_agent.tui.session import Session
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
                self.assertEqual(app.status.get("defaultEngine"), "smart")
                text = screen_text(app)
                self.assertNotIn("No Jev key", text)
                self.assertNotIn("jev-latest", app.model_words())
                self.assertIn("gpt-", app.model_words())
                await submit(pilot, "/preview")
                await pilot.pause(.2)
                self.assertFalse(app.preview_next)
                self.assertTrue(any("A preview needs Quick mode" in note for note in notes), notes)
            app.session.close(2)

    async def test_no_key_at_all_shows_the_key_row_of_the_checklist(self):
        import tempfile
        from mobile_agent.tui.app import MobsterApp
        from mobile_agent.tui.session import Session
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


class InferApp(unittest.TestCase):
    """CLI-10: "Find my …" went to Find My and "Message Sam about the camera settings" to Settings."""

    INSTALLED = [{"id": bundle, "name": name, "bundleId": bundle} for bundle, name in (
        ("com.ubercab.UberClient", "Uber"), ("com.zimride.instant", "Lyft"), ("com.airbnb.app", "Airbnb"),
        ("com.dd.doordash", "DoorDash"), ("net.whatsapp.WhatsApp", "WhatsApp"))]

    def test_a_messaging_verb_whose_object_is_an_app_runs_in_it(self):
        """The CLI-10 fix sent "Call an Uber…" to Phone: a call verb beat any app not named as the channel."""
        from mobile_agent.catalog import APPS
        from mobile_agent.tui.app import infer_app
        cases = {"Call an Uber to the airport": "Uber",
                 "Call a Lyft home": "Lyft",
                 "Call me an Uber": "Uber",
                 # Deliberately: the host and the driver are reached in their app.
                 "Message my Airbnb host about checkout": "Airbnb",
                 "Text the DoorDash driver": "DoorDash",
                 "Message my Airbnb host on WhatsApp": "WhatsApp",
                 # An app named in what is said is still not where it runs.
                 "Text Sam my Uber ETA": "Messages",
                 "Call Mom": "Phone",
                 # The verbs' own apps and the common words are what is sent.
                 "Email the notes to Sam": "Mail",
                 "Text my photos to Mom": "Messages"}
        self.assertEqual({goal: (infer_app(goal, [*APPS, *self.INSTALLED]) or {}).get("name") for goal in cases},
                         cases)

    def test_find_my_counts_only_for_the_app_or_what_it_finds(self):
        from mobile_agent.catalog import APPS
        from mobile_agent.tui.app import infer_app
        cases = {"Find my boarding pass": None,
                 "Find my latest screenshot": None,
                 "Find my phone number": None,
                 "Find my AirPods": "Find My",
                 "Find my son's iPad": "Find My",
                 "Open Find My": "Find My",
                 "Share my location in Find My": "Find My",
                 "Check Find My for my keys": "Find My"}
        self.assertEqual({goal: (infer_app(goal, APPS) or {}).get("name") for goal in cases}, cases)

    def test_the_app_a_task_works_in_wins_over_everyday_phrases(self):
        from mobile_agent.catalog import APPS
        from mobile_agent.tui.app import infer_app
        whatsapp = {"id": "net.whatsapp.WhatsApp", "name": "WhatsApp", "bundleId": "net.whatsapp.WhatsApp"}
        cases = {"Find my boarding pass in Wallet": "Wallet",
                 "Find my latest screenshot in Photos": "Photos",
                 "Open Photos and find my dog pictures": "Photos",
                 "Message Sam about the camera settings": "Messages",
                 "Message Sam on WhatsApp": "WhatsApp",
                 "Call Mom on FaceTime": "FaceTime",
                 "Search TikTok for cat videos": "TikTok",
                 "Find My iPhone": "Find My",
                 "Find my phone": "Find My"}
        self.assertEqual({goal: (infer_app(goal, [*APPS, whatsapp]) or {}).get("name") for goal in cases}, cases)


if __name__ == "__main__":
    unittest.main()
