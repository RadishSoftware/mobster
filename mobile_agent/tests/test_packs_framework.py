import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from mobile_agent import hints
from mobile_agent.agent_hooks import Prepared
from mobile_agent.mcp_server.protocol import Call
from mobile_agent.packs import core, health, tools
from mobile_agent.packs.core import PickRow, Route, ScreenSpec, Tap, sig
from mobile_agent.tests.packs_support import Phone, el, snap

APP = "com.example.chirp"
TABS = (el("Home", rect=(0, .92, .33, .06)), el("Inbox, 2 unread", rect=(.33, .92, .33, .06)),
        el("Profile", rect=(.66, .92, .33, .06)))


def tabs(selected):
    return tuple({**row, "selected": True} if row["label"].startswith(selected) else row for row in TABS)


def inbox_rows(page):
    names = (("Ana", True, "are we still on for fri?", "3h"), ("Ben", False, "ok", "1d"), ("Cy", False, "lol", "2d"),
             ("Dee", True, "call me", "3d"), ("Eve", False, "thanks!", "1w"))
    chosen = names[:3] if page == 1 else names[2:]
    return [el(f"{'Unread, ' if unread else ''}{name}, {said}, {when}", "Cell", (0, .15 + .1 * i, 1, .09))
            for i, (name, unread, said, when) in enumerate(chosen)]


SCREENS = {
    "springboard": snap("com.apple.springboard", [el("Chirp", "Icon", (.1, .1, .2, .1))]),
    "home": snap(APP, [el("Chirp", "StaticText", (.4, .06, .2, .04)), el("Post by @sam", "Cell", (0, .15, 1, .5)),
                       el("Like", rect=(.02, .66, .1, .05)), el("Comment", rect=(.14, .66, .1, .05)),
                       *tabs("Home")]),
    "inbox": snap(APP, [el("Messages", "StaticText", (.35, .06, .3, .04)), *inbox_rows(1), *tabs("Inbox")]),
    "inbox2": snap(APP, [el("Messages", "StaticText", (.35, .06, .3, .04)), *inbox_rows(2), *tabs("Inbox")]),
    "thread": snap(APP, [el("Back", rect=(0, .06, .1, .04)), el("Ana", "StaticText", (.4, .06, .2, .04)),
                         el("are we still on for fri?", "StaticText", (.05, .5, .6, .05)),
                         el("Message...", "TextView", (.05, .9, .7, .05), editable=True),
                         el("Send", rect=(.8, .9, .15, .05))]),
    "comments": snap(APP, [el("Comments", "StaticText", (.35, .3, .3, .04)),
                           el("Add a comment", "TextView", (.05, .9, .7, .05), editable=True)]),
}


def parse_inbox(snapshot):
    rows = []
    for e in core.find_all(snapshot, r".", role="Cell"):
        parts = [p.strip() for p in e.label.split(",")]
        unread = parts[0] == "Unread"
        parts = parts[1:] if unread else parts
        rows.append({"from": parts[0], "unread": unread, "last": parts[1], "when": parts[2]})
    return rows


def read_inbox(pack, io, snapshot, n, arg):
    rows, stopped, _ = core.collect(io, snapshot, parse_inbox, lambda row: row["from"], n)
    return core.ReadResult("dms", "Your newest DMs", (("from", "From", "text"), ("unread", "Unread", "bool"),
                                                      ("last", "Last message", "text"), ("when", "When", "text")),
                           rows, stopped)


class Chirp(core.Pack):
    name, key, prefix, bundle = "Chirp", "chirp", "CH", APP
    verified = core.Verified("1.2.0", "2026-10-14", "test")
    tabs = sig(r"^(Home|Inbox|Profile)\b", "Button", region=(.85, 1))
    never = re.compile(r"^post comment$", re.I)
    mentions = re.compile(r"\bchirp\b", re.I)
    screens = (
        ScreenSpec("feed", "your feed", (sig(r"^Home$", "Button", selected=True, must=True), sig(r"^Post by "))),
        ScreenSpec("inbox", "your DMs", (sig(r"^Inbox\b", "Button", selected=True, must=True),
                                         sig(r"^Messages$", "StaticText"))),
        ScreenSpec("thread", "a DM thread", (sig(r"^Message\.\.\.$", "TextView", must=True), sig(r"^Back$"))),
        ScreenSpec("comments", "the comments", (sig(r"^Comments$", must=True), sig(r"^Add a comment$"))),
    )
    routes = {
        "feed": Route("feed", "your feed", ("feed",), (Tap(r"^Home$", "the Home tab"),)),
        "dms": Route("dms", "your DMs", ("inbox",), (Tap(r"^Inbox\b", "the Inbox tab"),), link="chirp://inbox"),
        "thread": Route("thread", "a DM thread", ("thread",), (Tap(r"^Inbox\b", "the Inbox tab"),
                                                                PickRow("DM thread")), arg="name"),
        "comments": Route("comments", "the comments", ("comments",), (Tap(r"^Comment$", "Comment", opens=True),),
                          from_tabs=False),
        "like": Route("like", "a post", ("feed",), (Tap(r"^Home$", "the Home tab", optional=True),),
                      then_control="like"),
    }
    controls = {"like": core.Control(route="like", control=r"^Like$", title="Like"),
                "reply": core.Control(route="thread", control=r"^Message\.\.\.$", title="message")}
    readers = {"dms": core.Reader(read_inbox, ("inbox",), "dms")}
    prompts = {"OPEN": "- CH_OPEN where: go to a Chirp screen by code: feed | dms | thread <name> | like",
               "READ": "- CH_READ what [n]: read Chirp by code: dms n"}
    read_default = {"dms": 10}
    read_titles = {"dms": "DMs"}


PACK = Chirp()


def phone(start="home", links=None):
    moves = {("*", "LAUNCH_APP", APP): "home",
             ("home", "TAP", "Inbox, 2 unread"): "inbox", ("inbox", "TAP", "Home"): "home",
             ("inbox", "SWIPE_UP", None): "inbox2", ("inbox2", "SWIPE_UP", None): "inbox2",
             ("inbox", "TAP", "Unread, Ana, are we still on for fri?, 3h"): "thread",
             ("thread", "TAP", "Back"): "inbox", ("home", "TAP", "Comment"): "comments"}
    return Phone(SCREENS, moves, start, links=links)


def io_for(device, **options):
    return core.PackIO(device, PACK, sleep=lambda s: None, **options)


class RecognizeTests(unittest.TestCase):
    def test_each_screen_is_named_with_a_confidence(self):
        for name, screen in (("home", "feed"), ("inbox", "inbox"), ("thread", "thread"), ("comments", "comments")):
            seen = PACK.recognize(SCREENS[name])
            self.assertEqual(seen.screen, screen, name)
            self.assertGreaterEqual(seen.confidence, core.SURE)

    def test_another_app_or_a_strange_screen_is_no_screen(self):
        self.assertIsNone(PACK.recognize(SCREENS["springboard"]).screen)
        strange = snap(APP, [el("Something new", "StaticText", (.1, .1, .5, .05))])
        self.assertIsNone(PACK.recognize(strange).screen)

    def test_a_must_signal_missing_rules_a_screen_out(self):
        page = snap(APP, [el("Messages", "StaticText", (.35, .06, .3, .04)), *tabs("Home")])
        self.assertNotEqual(PACK.recognize(page).screen, "inbox")

    def test_counts_read_as_people_write_them(self):
        self.assertEqual([core.number(t) for t in ("2.7M", "16,767", "1.1K", "12", "n/a")],
                         [2_700_000, 16_767, 1_100, 12, None])


class NavigateTests(unittest.TestCase):
    def test_from_another_app_it_launches_then_taps_the_tab(self):
        device = phone("springboard")
        landing = core.navigate(PACK, io_for(device), "dms")
        self.assertTrue(landing.ok, landing.why)
        self.assertEqual((landing.seen.screen, landing.how), ("inbox", "code"))
        self.assertEqual([a[0] for a in device.actions], ["LAUNCH_APP", "TAP"])

    def test_already_there_is_no_step(self):
        device = phone("inbox")
        landing = core.navigate(PACK, io_for(device), "dms")
        self.assertEqual((landing.ok, landing.how, device.actions), (True, "already", []))

    def test_a_working_deep_link_is_one_step(self):
        device = phone("home", links={"chirp://inbox": "inbox"})
        landing = core.navigate(PACK, io_for(device), "dms")
        self.assertEqual((landing.ok, landing.how), (True, "link"))
        self.assertEqual(device.calls[0][1:], ("/url", {"url": "chirp://inbox", "bundleId": APP}))
        self.assertEqual(device.taps, [])

    def test_a_link_that_lands_nowhere_falls_back_to_code(self):
        device = phone("home")
        landing = core.navigate(PACK, io_for(device), "dms")
        self.assertEqual((landing.ok, landing.how), (True, "code"))
        self.assertEqual(device.taps, ["Inbox, 2 unread"])

    def test_a_row_is_picked_by_name_and_tapped_whatever_its_other_words(self):
        device = phone("home")
        landing = core.navigate(PACK, io_for(device), "thread", "ana")
        self.assertTrue(landing.ok, landing.why)
        self.assertEqual(device.taps[-1], "Unread, Ana, are we still on for fri?, 3h")

    def test_a_missing_row_or_argument_is_a_landing_that_says_why(self):
        landing = core.navigate(PACK, io_for(phone("home")), "thread", "Zed")
        self.assertFalse(landing.ok)
        self.assertIn("no DM thread names “Zed”", landing.why)
        self.assertEqual(core.navigate(PACK, io_for(phone()), "thread").why, "it needs a name")

    def test_a_link_argument_opens_only_the_apps_own_site(self):
        route = Route("post", "the post", ("feed",), (), link="{arg}", arg="url", from_tabs=False)
        pack = Chirp()
        pack.sites = ("chirp.example",)
        self.assertEqual(core.link_for(pack, route, "https://chirp.example/p/1"), "https://chirp.example/p/1")
        for bad in ("https://evil.example/p/1", "chirp://inbox", "javascript:alert(1)", "https://chirp.example.evil/x"):
            self.assertIsNone(core.link_for(pack, route, bad), bad)
        user = Route("user", "a profile", ("feed",), (), link="chirp://user?name={arg}", arg="user")
        self.assertEqual(core.link_for(pack, user, "@ana b"), "chirp://user?name=ana%20b")

    def test_a_row_template_matches_only_its_shape(self):
        page = snap(APP, [el("ana fan club • 2 followers", "StaticText", (0, .2, 1, .05)),
                          el("ana • 2.7M followers", "StaticText", (0, .3, 1, .05))])
        step = PickRow("result", template=r"^@?{arg}(?: • |$)")
        self.assertEqual(core.pick_row(page, "@ana", step).label, "ana • 2.7M followers")

    def test_a_row_named_by_an_act_button_is_never_tapped(self):
        button = el("Follow Sam Lee", "Button", (0, .3, 1, .3))
        screens = {**SCREENS, "inbox": snap(APP, [el("Messages", "StaticText", (.35, .06, .3, .04)), button,
                                                  *tabs("Inbox")])}
        device = Phone(screens, {("inbox", "TAP", "Follow Sam Lee"): "thread"}, "inbox")
        landing = core.navigate(PACK, io_for(device), "thread", "Sam")
        self.assertFalse(landing.ok)
        self.assertNotIn("Follow Sam Lee", device.taps)

    def test_a_dm_row_wins_over_an_act_button_with_the_same_name(self):
        row = el("Sam Lee, see you at 6, 1h", "Cell", (0, .15, 1, .08))
        button = el("Follow Sam Lee", "Button", (0, .3, 1, .3))
        screens = {**SCREENS, "inbox": snap(APP, [el("Messages", "StaticText", (.35, .06, .3, .04)), row, button,
                                                  *tabs("Inbox")]), "thread": SCREENS["thread"]}
        device = Phone(screens, {("inbox", "TAP", row["label"]): "thread"}, "inbox")
        landing = core.navigate(PACK, io_for(device), "thread", "Sam")
        self.assertTrue(landing.ok)
        self.assertIn(row["label"], device.taps)
        self.assertNotIn(button["label"], device.taps)

    def test_an_act_route_stops_at_the_control_and_never_taps_it(self):
        device = phone("home")
        landing = core.navigate(PACK, io_for(device), "like")
        self.assertTrue(landing.ok)
        self.assertEqual(landing.control.label, "Like")
        self.assertNotIn("Like", device.taps)

    def test_an_opener_is_tapped_but_no_act_ever_is(self):
        io = io_for(phone())
        self.assertTrue(io.may_tap("Comment", opens=True, pattern=r"^Comment$"))
        self.assertTrue(io.may_tap("Create a post, story or live video", opens=True,
                                   pattern=r"^Create a post, story or live video$"))
        self.assertFalse(io.may_tap("Create a post, story or live video"))
        for label in ("Post comment", "Follow", "Send", "Like"):
            self.assertFalse(io.may_tap(label, opens=True, pattern=r".*"), label)
        for label in ("Like", "Follow", "Send", "Post comment", "Share", "Delete", "Comment"):
            self.assertFalse(io.may_tap(label), label)
        for label in ("Reels", "Back", "Inbox, 2 unread", "Profile"):
            self.assertTrue(io.may_tap(label), label)
        with self.assertRaises(core.Refused):
            io.tap(SCREENS["home"].elements[2], SCREENS["home"])

    def test_a_callers_check_runs_before_every_tap_and_can_refuse(self):
        asked = []

        def permit(op, element, snapshot, text):
            asked.append((op, getattr(element, "label", None)))
            raise core.Refused("the user said no")
        device = phone("home")
        landing = core.navigate(PACK, io_for(device, permit=permit), "dms")
        self.assertFalse(landing.ok)
        self.assertIn("the user said no", landing.why)
        self.assertEqual(asked, [("OPEN_URL", None)])
        self.assertEqual(device.taps, [])

    def test_a_stopped_task_stops_navigating(self):
        landing = core.navigate(PACK, io_for(phone("springboard"), cancelled=lambda: True), "dms")
        self.assertEqual((landing.ok, landing.why), (False, "the task was stopped"))


class ReadTests(unittest.TestCase):
    def test_rows_across_scrolls_without_repeats(self):
        device = phone("inbox")
        result = PACK.read(io_for(device), "dms", 5)
        self.assertEqual([r["from"] for r in result.rows], ["Ana", "Ben", "Cy", "Dee", "Eve"])
        self.assertEqual(result.stopped, "")
        self.assertEqual(device.actions, [("SWIPE_UP", None, None)])

    def test_the_end_of_the_list_stops_it(self):
        result = PACK.read(io_for(phone("inbox")), "dms", 30)
        self.assertEqual((len(result.rows), result.stopped), (5, "it reached the end of the list"))

    def test_a_read_opens_its_screen_first(self):
        device = phone("springboard")
        result = PACK.read(io_for(device), "dms", 2)
        self.assertEqual(result.screen, "inbox")
        self.assertEqual(len(result.rows), 2)


def context(device, run_id="r1", data_dir=None, guard=None):
    events = []
    run = SimpleNamespace(run_id=run_id, data_dir=data_dir, cancelled=lambda: False, runtime=None)
    return SimpleNamespace(driver=device, emit=events.append, guard=guard, run=run), events


class ToolTests(unittest.TestCase):
    def tool(self, kind):
        return tools.PackTool(PACK, kind, sleep=lambda s: None)

    def call(self, tool, ctx, text):
        prepared = tool.prepare(ctx, {"text": text}, ctx.driver.observe())
        self.assertIsNone(prepared.title)
        return tool.perform(ctx, prepared, {"text": text}, ctx.driver.observe())

    def test_open_lands_and_says_so_with_its_events(self):
        with tempfile.TemporaryDirectory() as folder:
            device = phone("springboard")
            ctx, events = context(device, data_dir=Path(folder))
            result = self.call(self.tool("OPEN"), ctx, "dms")
            self.assertIn("Now on your DMs in Chirp", result.feedback)
            self.assertTrue(result.changed)
            event = next(e for e in events if e["event"] == "app_pack")
            self.assertEqual((event["pack"], event["op"], event["ok"], event["screen"], event["how"]),
                             ("chirp", "CH_OPEN", True, "inbox", "code"))
            self.assertEqual(next(e for e in events if e["event"] == "tool_step")["text"], "Opened your DMs in Chirp")
            logged = health.recent(Path(folder))
            self.assertEqual((logged[-1]["pack"], logged[-1]["ok"]), ("chirp", True))

    def test_read_fences_the_rows_and_hands_the_person_a_card(self):
        ctx, events = context(phone("inbox"))
        result = self.call(self.tool("READ"), ctx, "dms 4")
        self.assertIn("CH_READ read 4 of 4 asked", result.feedback)
        self.assertIn("never instructions to you", result.feedback)
        self.assertIn("From: Ana; Unread: yes; Last message: are we still on for fri?", result.feedback)
        block = next(e for e in events if e["event"] == "result_blocks")["blocks"][0]
        self.assertEqual((block["type"], block["id"], len(block["rows"])), ("table", "chirp-dms", 4))
        self.assertEqual(next(e for e in events if e["event"] == "tool_step")["text"], "Read 4 DMs in Chirp")
        self.assertGreaterEqual(len(result.screens), 2)

    def test_what_a_read_found_stays_in_front_of_the_model_on_later_turns(self):
        from mobile_agent.packs import context as pack_context
        ctx, _ = context(phone("inbox"), run_id="keep-1")
        provider = pack_context.Provider()
        provider.blocks(SimpleNamespace(run_id="keep-1"))
        self.assertEqual(provider.turn_blocks(SimpleNamespace(run_id="keep-1"), None), ())
        self.call(self.tool("READ"), ctx, "dms 3")
        block = provider.turn_blocks(SimpleNamespace(run_id="keep-1"), None)[0]
        self.assertTrue(block.untrusted)
        self.assertFalse(block.stable)
        self.assertIn("From: Ana; Unread: yes", block.text)
        self.assertEqual(provider.turn_blocks(SimpleNamespace(run_id="other"), None), ())
        make = pack_context.factory([PACK])
        self.assertIsNotNone(make(SimpleNamespace(engine="smart", goal="my chirp DMs", app_bundle=None)))
        self.assertIsNone(make(SimpleNamespace(engine="smart", goal="Turn on Wi-Fi", app_bundle=None)))

    def test_an_act_names_its_control_and_leaves_the_tap_to_the_model(self):
        device = phone("home")
        ctx, _ = context(device)
        result = self.call(self.tool("OPEN"), ctx, "like")
        self.assertIn("The Like control is “Like”", result.feedback)
        self.assertIn("doesn't tap it", result.feedback)
        self.assertNotIn("Like", device.taps)

    def test_a_miss_hands_back_and_two_in_a_row_turn_the_pack_off_for_the_run(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx, events = context(phone("home"), run_id="miss", data_dir=Path(folder))
            tool = self.tool("OPEN")
            first = self.call(tool, ctx, "thread Zed")
            self.assertIn("Go on the usual way", first.feedback)
            self.call(tool, ctx, "thread Zed")
            third = self.call(tool, ctx, "dms")
            self.assertIn("missed 2 times in a row in this task", third.feedback)
            self.assertNotIn("Zed", Path(folder, hints.FILE).read_text())
            self.assertEqual(health.status(PACK, "1.2.0", health.recent(Path(folder)))["state"], "degraded")

    def test_words_it_doesnt_take_and_an_app_that_isnt_allowed_are_said_plainly(self):
        ctx, _ = context(phone())
        self.assertIn("takes one of: feed, dms", self.call(self.tool("OPEN"), ctx, "nowhere").feedback)
        guard = SimpleNamespace(allows_app=lambda bundle: False)
        ctx, _ = context(phone(), guard=guard)
        prepared = self.tool("READ").prepare(ctx, {"text": "dms"}, ctx.driver.observe())
        self.assertEqual(prepared.state["error"], "Chirp isn't allowed in this task.")

    def test_read_words_take_a_count_before_or_after(self):
        self.assertEqual(tools.parse_read(PACK, "dms 12"), ("dms", 12, None))
        self.assertEqual(tools.parse_read(PACK, "dms"), ("dms", 10, None))
        self.assertEqual(tools.parse_read(PACK, "dms 500")[1], tools.MAX_N)

    def test_offered_only_to_a_smart_run_about_the_app_and_within_the_policy(self):
        from mobile_agent.harness.tools import offered
        make = tools.factory(PACK, "OPEN")
        about = SimpleNamespace(engine="smart", goal="Check my Chirp DMs", app_bundle=None, interactive=True)
        self.assertIsNotNone(make(about))
        self.assertIsNotNone(make(SimpleNamespace(engine="smart", goal="anything", app_bundle=APP)))
        self.assertIsNone(make(SimpleNamespace(engine="smart", goal="Turn on Wi-Fi", app_bundle=None)))
        self.assertIsNone(make(SimpleNamespace(engine="claude_code", goal="Check my Chirp DMs", app_bundle=None)))
        driver = phone()
        kept = offered([tools.PackTool(PACK, "OPEN"), tools.PackTool(PACK, "READ")], run_context=about, driver=driver)
        self.assertEqual([t.op for t in kept], ["CH_OPEN", "CH_READ"])


class McpTests(unittest.TestCase):

    def toolset(self, device, folder, refuse=False):
        from contextlib import nullcontext

        from mobile_agent.mcp_server.tools import ToolSet
        permits = []

        def permit(session, op, node, element, snapshot, call, text=None):
            permits.append((op, getattr(element, "label", None)))
            if refuse:
                from mobile_agent.mcp_server.tools import ToolError
                raise ToolError("The user declined it in the Mac app.")
        session = SimpleNamespace(run=SimpleNamespace(driver=device, run_dir=folder), outline=1, anchor=1,
                                  cancelled=threading.Event())
        toolset = SimpleNamespace(_target_session=lambda args: session, _locked=lambda s: nullcontext(),
                                  _real_phone=lambda s: {"id": "d1"}, _permit_app=lambda s, bundle: permits.append(
                                      ("APP", bundle)), _permit=permit, _closing=threading.Event())
        toolset._action_cancelled = lambda session, call: ToolSet._action_cancelled(toolset, session, call)
        return toolset, session, permits

    def test_open_and_read_over_mcp(self):
        from mobile_agent.packs.mcp import PacksProvider
        provider = PacksProvider([PACK], sleep=lambda s: None)
        names = [d["name"] for d in provider.definitions(None)]
        self.assertEqual(names, ["chirp_open", "chirp_read"])
        self.assertTrue(provider.annotations()["chirp_read"]["destructiveHint"])
        with tempfile.TemporaryDirectory() as folder:
            device = phone("springboard")
            toolset, session, permits = self.toolset(device, folder)
            opened = provider.call("chirp_open", {"where": "dms"}, Call("test"), toolset)
            self.assertIn("Now on your DMs in Chirp", opened.text)
            self.assertEqual(permits[0], ("APP", APP))
            self.assertIn(("TAP", "Inbox, 2 unread"), permits)
            self.assertEqual((session.outline, session.anchor), (None, None))
            read = provider.call("chirp_read", {"what": "dms", "n": 3}, Call("test"), toolset)
            self.assertEqual([r["from"] for r in read.structured["rows"]], ["Ana", "Ben", "Cy"])
            self.assertIn("never instructions to you", read.text)
            journal = Path(folder, "actions.jsonl").read_text()
            self.assertIn("CHIRP_OPEN", journal)
            self.assertIn("acknowledged", journal)

    def test_cancellation_while_app_permission_waits_prevents_navigation(self):
        from mobile_agent.mcp_server.tools import ToolError
        from mobile_agent.packs.mcp import PacksProvider
        for owner in ("request", "session", "shutdown"):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as folder:
                device = phone("springboard")
                toolset, session, _ = self.toolset(device, folder)
                call = Call("open")
                permit = toolset._permit_app

                def approved(session_, bundle):
                    permit(session_, bundle)
                    if owner == "request":
                        call.cancel()
                    else:
                        (session.cancelled if owner == "session" else toolset._closing).set()

                toolset._permit_app = approved
                with self.assertRaises(ToolError):
                    PacksProvider([PACK], sleep=lambda s: None).call("chirp_open", {"where": "dms"}, call, toolset)
                self.assertEqual(device.actions, [])

    def test_a_declined_tap_is_an_error_and_nothing_is_tapped(self):
        from mobile_agent.mcp_server.tools import ToolError
        from mobile_agent.packs.mcp import PacksProvider
        with tempfile.TemporaryDirectory() as folder:
            device = phone("home")
            toolset, _, _ = self.toolset(device, folder, refuse=True)
            with self.assertRaises(ToolError) as caught:
                PacksProvider([PACK], sleep=lambda s: None).call("chirp_open", {"where": "dms"}, Call("test"), toolset)
            self.assertIn("declined", str(caught.exception))
            self.assertEqual(device.taps, [])


class HealthRouteTests(unittest.TestCase):
    def test_get_api_packs_answers_each_packs_health(self):
        from mobile_agent.packs import register
        with tempfile.TemporaryDirectory() as folder:
            health.record(Path(folder), {"pack": "chirp", "step": "dms", "ok": True})
            health.record(Path(folder), {"pack": "chirp", "step": "thread", "ok": False, "why": "no row"})
            inventory = SimpleNamespace(user=({"bundleId": APP, "name": "Chirp", "version": "1.3.0"},))
            runtime = SimpleNamespace(config=SimpleNamespace(state_db=str(Path(folder) / "state.db")),
                                      installed_apps=SimpleNamespace(snapshot=lambda: inventory))
            self.addCleanup(register.add_for_tests(PACK))
            answer = register.status_route(SimpleNamespace(runtime=runtime)).value
        chirp = next(p for p in answer["packs"] if p["pack"] == "chirp")
        self.assertEqual((chirp["installed"], chirp["sameVersion"], chirp["verified"]["version"]),
                         ("1.3.0", False, "1.2.0"))
        self.assertEqual((chirp["steps"], chirp["landed"], chirp["steppedAside"], chirp["state"]),
                         (2, 1, 1, "degraded"))
        self.assertEqual(chirp["lastProblem"]["why"], "no row")


class LoopTests(unittest.TestCase):

    def test_a_tool_step_names_the_step_and_its_blocks_reach_the_result(self):
        from mobile_agent import smart_events, smart_result
        out = []
        events = smart_events.SmartEvents(out.append, None, apps={})
        events({"event": "tool_step", "op": "CH_READ", "text": "Read 4 DMs in Chirp"})
        events({"event": "result_blocks", "op": "CH_READ", "blocks": [
            {"type": "table", "id": "chirp-dms", "title": "DMs", "columns": [{"key": "a", "label": "A"}],
             "rows": [{"cells": {"a": "x"}}]}]})
        events({"event": "frontier_action", "step": 1, "index": 0, "operation": "CH_READ", "target_label": None})
        self.assertIn("Read 4 DMs in Chirp", [e.get("text") for e in out if e.get("event") == "step"])
        envelope = smart_result.tool_envelope(events, "completed", None)
        self.assertEqual(envelope["blocks"][0]["id"], "chirp-dms")
        self.assertIsNone(smart_result.tool_envelope(events, "approval_denied", None))
        self.assertIsNone(smart_result.tool_envelope(events, "completed", {"type": "object"}))

    def test_an_answer_table_that_copies_a_tools_rows_is_left_out(self):
        from mobile_agent import smart_result
        tool = {"type": "table", "id": "chirp-dms", "columns": [{"key": "from", "label": "From"}],
                "rows": [{"cells": {"from": name}} for name in ("Ana", "Ben", "Cy")]}
        copy = {"type": "table", "id": "t1", "columns": [{"key": "who", "label": "Sender"}],
                "rows": [{"cells": {"who": name}} for name in ("Ana", "Ben", "Cy")]}
        other = {"type": "table", "id": "t2", "columns": [{"key": "theme", "label": "Theme"}],
                 "rows": [{"cells": {"theme": t}} for t in ("Plans", "Jokes")]}
        self.assertTrue(smart_result.repeats_tool_rows(copy, [tool]))
        self.assertFalse(smart_result.repeats_tool_rows(other, [tool]))
        self.assertFalse(smart_result.repeats_tool_rows({"type": "markdown", "text": "x"}, [tool]))

    def test_screens_a_skill_read_reach_the_tasks_checklist(self):
        from unittest.mock import patch

        from mobile_agent import contract
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.tests.test_frontier_skills import Model
        seen = []
        scrolled = snap(APP, [el("Eve, thanks!, 1w", "Cell", (0, .2, 1, .09)), *tabs("Inbox")], "scrolled")

        class Reader:
            op, needs_target = "CH_READ", False
            prompt = "- CH_READ what: read Chirp"

            def available(self, ctx):
                return True

            def prepare(self, ctx, act, snapshot):
                return Prepared(None)

            def perform(self, ctx, prepared, act, snapshot):
                return tools.PackResult("Read 5 DMs", screens=(scrolled,))
        original = contract.Contract.observe

        def spy(self, snapshot, app, step):
            seen.append(snapshot.revision)
            return original(self, snapshot, app, step)
        class Reporting(Model):
            def complete(self, messages, schema, timeout=60, **kwargs):
                if "items" in schema["properties"]:
                    return {"items": [{"kind": "REPORT", "app": "Chirp", "what": "what Eve said", "payload": "",
                                       "act": "none", "count": "", "condition": "", "quote": ""}]}, {}
                return super().complete(messages, schema, timeout, **kwargs)
        model = Reporting([("CH_READ", None, None)], [("DONE", None, None)], answer="Eve said thanks.")
        with patch.object(contract.Contract, "observe", spy):
            agent = FrontierAgent(phone("inbox"), model, apps={APP: "Chirp"}, emit=lambda e: None,
                                  screenshots=False, settle_seconds=0, skills=(Reader(),))
            agent.run("Read my Chirp DMs")
        self.assertIn("scrolled", seen)


if __name__ == "__main__":
    unittest.main()
