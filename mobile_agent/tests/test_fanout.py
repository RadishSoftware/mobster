import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import fanout
from mobile_agent.server import make_handler
from mobile_agent.tests import fleet_world
from mobile_agent.tests import sota_world as world
from mobile_agent.tests.seam_support import request

CLEAN = {"PATH": os.environ.get("PATH", ""), "MOBSTER_ASK_BEFORE_ACTING": "1"}
SESSION = "app-session-secret-0000000000"


class FakeLoad:
    def __init__(self, value=20.0):
        self.value = value

    def percent(self):
        return self.value


class WorldTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-fanout-"))
        env = patch.dict(os.environ, {**CLEAN, "HOME": str(self.root / "home")}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        apply, restore = fleet_world.scripted_engines()
        apply()
        self.addCleanup(restore)
        self.world = fleet_world.World()
        self.runtime, _ = fleet_world.build_runtime(self.root / "state", self.world)
        self.load = FakeLoad()
        self.runtime.fanout.load = self.load
        self.addCleanup(self.runtime.close, 10)
        self.sam, self.work, self.test = (spec.udid for spec in self.world.specs)

    def request(self, method, path, body=None, *, app=True, key=None, origin="app"):
        headers = {"X-Mobster-Origin": origin, **({"X-Mobster-App-Session": SESSION} if app else {}),
                   **({"Idempotency-Key": key} if key else {})}
        status, data, _ = request(make_handler(self.runtime, app_session=SESSION), method, path, body, headers=headers)
        return status, data

    def devices(self):
        status, body = self.request("GET", "/api/devices")
        self.assertEqual(status, 200, body)
        return {item["id"]: item for item in body["devices"]}

    def until(self, check, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(.02)
        self.fail("timed out")

    def finished(self, run_id):
        return self.until(lambda: self.runtime.runs[run_id].finished_at is not None and self.runtime.runs[run_id])

    def group(self, group_id):
        status, body = self.request("GET", f"/api/groups/{group_id}")
        self.assertEqual(status, 200, body)
        return body["group"]

    def group_done(self, group_id):
        return self.until(lambda: (group := self.group(group_id))["state"] == "finished" and group["summary"] and group)


class ManageTests(WorldTest):
    def test_rename_is_a_local_nickname_shown_everywhere(self):
        status, body = self.request("POST", f"/api/devices/{self.work}", {"name": "  Office   iPhone "})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["device"]["name"], body["device"]["nickname"]), ("Office iPhone", "Office iPhone"))
        run = self.runtime.create(None, "What iOS version is this iPhone on?", "live", device="Office iPhone")
        self.assertEqual(run.device_name, "Office iPhone")
        self.finished(run.id)
        status, body = self.request("POST", f"/api/devices/{self.work}", {"name": None})
        self.assertEqual((status, body["device"]["name"], body["device"]["nickname"]), (200, "Work iPhone", None))
        status, body = self.request("POST", f"/api/devices/{self.work}", {"name": "x" * 65})
        self.assertEqual(status, 400)

    def test_set_as_default_is_where_a_task_that_names_none_runs(self):
        self.assertTrue(self.devices()[self.sam]["default"])
        status, body = self.request("POST", f"/api/devices/{self.test}", {"default": True})
        self.assertEqual((status, body["device"]["default"]), (200, True))
        listed = self.devices()
        self.assertEqual([key for key, item in listed.items() if item["default"]], [self.test])
        run = self.runtime.create(None, "What iOS version is this iPhone on?", "live")
        self.assertEqual(run.device_id, self.test)
        self.finished(run.id)
        status, body = self.request("POST", f"/api/devices/{self.test}", {"default": False})
        self.assertEqual(status, 200, body)
        self.assertIsNone(self.runtime.fleet.stored()["defaultDevice"])

    def test_disconnect_stops_using_a_phone_until_connect(self):
        status, body = self.request("POST", f"/api/devices/{self.work}/disconnect", {})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["device"]["state"], body["device"]["disconnected"]), ("disconnected", True))
        self.assertIn("Connect it", body["device"]["reason"])
        self.assertEqual(self.world.spec(self.work).runner, "stopped")
        status, body = self.request("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                          "device": self.work})
        self.assertEqual((status, body["code"]), (409, "device_disconnected"))
        status, body = self.request("POST", f"/api/devices/{self.work}/connect", {})
        self.assertEqual((status, body["device"]["state"], body["device"]["disconnected"]), (200, "ready", False))
        status, body = self.request("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                          "device": self.work})
        self.assertEqual(status, 201, body)
        self.finished(body["run"]["id"])

    def test_disconnect_and_forget_wait_for_a_running_task(self):
        gate = world.hold_at(self.world.spec(self.work).state, "TAP", "General")
        self.addCleanup(gate["release"].set)
        run = self.runtime.create(None, "What iOS version is this iPhone on?", "live", device=self.work)
        self.until(gate["reached"].is_set)
        for path, method in ((f"/api/devices/{self.work}/disconnect", "POST"), (f"/api/devices/{self.work}", "DELETE")):
            status, body = self.request(method, path, {} if method == "POST" else None)
            self.assertEqual((status, body["code"]), (409, "device_refused"), path)
            self.assertEqual(body["error"], "A task is running on it. Stop it first.")
        gate["release"].set()
        self.finished(run.id)

    def test_forget_removes_it_from_mobster_and_its_settings(self):
        self.request("POST", f"/api/devices/{self.work}", {"name": "Office iPhone", "default": True})
        status, body = self.request("DELETE", f"/api/devices/{self.work}")
        self.assertEqual((status, body), (200, {"forgotten": self.work}))
        listed = self.devices()
        self.assertEqual((listed[self.work]["state"], listed[self.work]["nickname"], listed[self.work]["default"]),
                         ("needs_setup", None, False))
        self.assertTrue(listed[self.sam]["default"])

    def test_a_phones_own_rules_need_the_mac_app_and_apply_to_its_tasks(self):
        status, body = self.request("POST", f"/api/devices/{self.work}", {"askBeforeActing": False}, app=False)
        self.assertEqual((status, body["code"]), (403, "app_only"))
        status, body = self.request("POST", f"/api/devices/{self.work}",
                                    {"askBeforeActing": False, "apps": ["com.apple.Preferences"]})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["device"]["askBeforeActing"], body["device"]["apps"]),
                         (False, ["com.apple.Preferences"]))
        status, body = self.request("POST", "/api/runs", {"appId": "notes", "goal": "Save that address in a new note",
                                                          "device": self.work})
        self.assertEqual((status, body["code"]), (400, "app_not_allowed"))
        self.assertIn("Notes isn't one of the apps Mobster may use on Work iPhone", body["error"])
        run = self.runtime.create(None, "What iOS version is this iPhone on?", "live", device=self.work)
        self.assertEqual((run.phone_apps, run.ask_override), (frozenset({"com.apple.Preferences"}), False))
        self.assertFalse(self.runtime.asks_before_acting(run))
        self.assertTrue(self.runtime.make_guard(run, run.phone).allows_app("com.apple.Preferences"))
        self.assertFalse(self.runtime.make_guard(run, run.phone).allows_app("com.apple.MobileSMS"))
        self.assertEqual(set(self.runtime.smart_apps(run)), {"com.apple.Preferences"})
        self.finished(run.id)
        other = self.runtime.create(None, "What iOS version is this iPhone on?", "live", device=self.test)
        self.assertEqual((other.phone_apps, other.ask_override), (None, None))
        self.assertTrue(self.runtime.asks_before_acting(other))
        self.finished(other.id)

    def test_a_remote_task_asks_first_whatever_the_phone_says(self):
        self.request("POST", f"/api/devices/{self.work}", {"askBeforeActing": False})
        run = self.runtime.create(None, "What iOS version is this iPhone on?", "live", device=self.work)
        run.origin = "remote"
        self.assertTrue(self.runtime.asks_before_acting(run))
        self.finished(run.id)


class FanoutTests(WorldTest):
    def test_one_task_on_three_phones_is_one_group(self):
        status, body = self.request("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                          "phones": [self.sam, "Work iPhone", self.test]}, key="g-1")
        self.assertEqual(status, 201, body)
        group = body["group"]
        self.assertEqual(body["groupId"], group["id"])
        self.assertEqual([member["device"] for member in group["members"]], [self.sam, self.work, self.test])
        self.assertEqual({run["device"] for run in body["runs"]}, {self.sam, self.work, self.test})
        self.assertEqual({run["groupId"] for run in body["runs"]}, {group["id"]})
        self.assertEqual(body["refused"], [])
        self.assertAlmostEqual(group["estimateTotalUsd"], group["estimateUsd"] * 3)
        done = self.group_done(group["id"])
        self.assertEqual(done["counts"]["completed"], 3)
        summary = done["summary"]
        self.assertEqual((summary["phones"], summary["completed"]), (3, 3))
        self.assertEqual([line["text"] for line in summary["lines"]], ["This iPhone is on iOS 26.4."])
        self.assertEqual(len(summary["lines"][0]["phones"]), 3)
        status, again = self.request("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                           "phones": [self.sam, "Work iPhone", self.test]}, key="g-1")
        self.assertEqual((status, again["group"]["id"], again["replayed"]), (200, group["id"], True))
        status, listed = self.request("GET", "/api/groups")
        self.assertEqual([item["id"] for item in listed["groups"]], [group["id"]])
        self.assertEqual(listed["limits"]["softLimit"], 10)

    def test_a_phone_that_cant_take_it_is_refused_and_the_others_start(self):
        self.world.spec(self.test).locked = True
        status, body = self.request("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                          "phones": [self.sam, self.work, self.test]})
        self.assertEqual(status, 201, body)
        self.assertEqual([(item["device"], item["code"]) for item in body["refused"]],
                         [(self.test, "device_unavailable")])
        self.assertIn("Test iPhone is not ready for live tasks. Unlock your iPhone.", body["refused"][0]["error"])
        self.assertEqual(len(body["runs"]), 2)
        done = self.group_done(body["group"]["id"])
        self.assertEqual((done["counts"]["completed"], done["counts"]["refused"]), (2, 1))
        refused = next(item for item in done["summary"]["answers"] if item["device"] == self.test)
        self.assertEqual(refused["status"], "refused")

    def test_none_can_start_is_a_refusal_with_every_reason(self):
        for spec in self.world.specs:
            spec.locked = True
        status, body = self.request("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                          "phones": [self.sam, self.work]})
        self.assertEqual((status, body["code"]), (409, "device_unavailable"))
        self.assertEqual([item["device"] for item in body["refused"]], [self.sam, self.work])
        self.assertEqual(self.runtime.fanout.groups, {})

    def test_phones_and_device_are_one_or_the_other(self):
        status, body = self.request("POST", "/api/runs", {"goal": "Open About", "phones": [self.sam],
                                                          "device": self.work})
        self.assertEqual(status, 400)
        status, body = self.request("POST", "/api/runs", {"goal": "Open About", "phones": "all"})
        self.assertEqual(status, 400)
        status, body = self.request("POST", "/api/runs", {"goal": "Open About", "phones": ["Nope"]})
        self.assertEqual((status, body["code"]), (404, "device_not_found"))

    def test_each_phone_keeps_its_own_rules_in_a_group(self):
        self.request("POST", f"/api/devices/{self.test}", {"apps": ["com.apple.Preferences"]})
        status, body = self.request("POST", "/api/runs", {"appId": "notes", "goal": "Save that address in a new note",
                                                          "phones": [self.sam, self.test]})
        self.assertEqual(status, 201, body)
        self.assertEqual([(item["device"], item["code"]) for item in body["refused"]], [(self.test, "app_not_allowed")])
        self.group_done(body["group"]["id"])

    def test_approvals_are_per_phone_and_approve_all_answers_only_what_was_shown(self):
        status, body = self.request("POST", "/api/runs", {"goal": "Text Alex that I'm running late",
                                                          "phones": [self.sam, self.work, self.test]})
        self.assertEqual(status, 201, body)
        group_id = body["groupId"]
        group = self.until(lambda: (item := self.group(group_id))["counts"]["waiting"] == 3 and item)
        self.assertEqual(group["state"], "waiting")
        shown = {member["runId"]: member["approval"]["id"] for member in group["members"]}
        status, refused = self.request("POST", f"/api/groups/{group_id}/approvals",
                                       {"approve": True, "approvals": shown}, app=False)
        self.assertEqual((status, refused["code"]), (403, "app_only"))
        first = next(iter(shown))
        stale = {**shown, first: "aaaaaaaaaaaa"}
        status, answered = self.request("POST", f"/api/groups/{group_id}/approvals", {"approve": True,
                                                                                     "approvals": stale})
        self.assertEqual(status, 200, answered)
        self.assertEqual([(item["runId"], item["answered"]) for item in answered["answers"]],
                         [(run_id, run_id != first) for run_id in stale])
        status, answered = self.request("POST", f"/api/groups/{group_id}/approvals",
                                        {"approve": False, "approvals": {first: shown[first]}}, app=False)
        self.assertEqual((status, answered["answers"][0]["answered"]), (200, True))
        done = self.group_done(group_id)
        self.assertTrue(all(member["finishedAt"] for member in done["members"]))
        sent = [spec.state["sent"].get("Alex Rivera") for spec in self.world.specs]
        self.assertEqual(sum(1 for item in sent if item), 2)

    def test_stop_ends_every_phone(self):
        gates = [world.hold_at(spec.state, "LAUNCH_APP", "com.apple.Preferences") for spec in self.world.specs[:2]]
        for gate in gates:
            self.addCleanup(gate["release"].set)
        status, body = self.request("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                          "phones": [self.sam, self.work]})
        self.assertEqual(status, 201, body)
        status, stopped = self.request("POST", f"/api/groups/{body['groupId']}/stop", {})
        self.assertEqual(status, 200, stopped)
        for gate in gates:
            gate["release"].set()
        done = self.group_done(body["groupId"])
        self.assertEqual(done["counts"]["completed"], 0)

    def test_a_busy_mac_holds_a_new_group_until_its_cpu_drops(self):
        self.load.value = 93.0
        status, body = self.request("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                          "phones": [self.sam, self.work]})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["runs"], [])
        group = body["group"]
        self.assertEqual((group["state"], group["counts"]["held"]), ("held", 2))
        self.assertIn("Your Mac is busy (CPU 93%)", group["held"]["reason"])
        time.sleep(.2)
        self.assertEqual(self.group(group["id"])["counts"]["held"], 2)
        self.load.value = 40.0
        with patch.object(fanout, "HOLD_STAGGER_S", 0.01):
            self.runtime.fanout.wake.set()
            done = self.group_done(group["id"])
        self.assertEqual(done["counts"]["completed"], 2)
        self.assertIsNone(done["held"])

    def test_a_phone_held_too_long_is_given_up(self):
        self.load.value = 99.0
        with patch.object(fanout, "HOLD_MAX_S", 0.05), patch.object(fanout, "HOLD_POLL_S", 0.01):
            status, body = self.request("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                              "phones": [self.sam, self.work]})
            self.assertEqual(status, 202, body)
            done = self.group_done(body["groupId"])
        self.assertEqual([member["code"] for member in done["members"]], ["mac_busy", "mac_busy"])
        self.assertIn("Nothing ran on it", done["members"][0]["error"])

    def test_more_than_ten_at_once_warns_and_still_starts(self):
        with patch.object(fanout, "SOFT_LIMIT", 2):
            status, body = self.request("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                              "phones": [self.sam, self.work, self.test]})
        self.assertEqual(status, 201, body)
        self.assertIn("3 tasks at once is more than 2", body["group"]["warning"])
        self.assertEqual(len(body["runs"]), 3)
        self.group_done(body["groupId"])

    def test_groups_survive_a_restart(self):
        status, body = self.request("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                          "phones": [self.sam, self.work]})
        self.group_done(body["groupId"])
        saved = json.loads((self.root / "state" / "groups.json").read_text())
        self.assertEqual([group["id"] for group in saved["groups"]], [body["groupId"]])
        self.assertNotIn("create", saved["groups"][0])
        again = fanout.Fanout(self.runtime, load=FakeLoad())
        self.assertEqual(again.public(again.get(body["groupId"]))["state"], "finished")


class ConversationTests(WorldTest):
    def thread(self, thread_id):
        status, body = self.request("GET", f"/api/threads/{thread_id}")
        self.assertEqual(status, 200, body)
        return body

    def test_one_message_three_phones_one_conversation(self):
        status, body = self.request("POST", "/api/threads", {"message": {
            "text": "What iOS version is this iPhone on?", "phones": [self.sam, self.work, self.test]}})
        self.assertEqual(status, 201, body)
        thread_id = body["thread"]["id"]
        group_id = body["item"]["groupId"]
        self.group_done(group_id)
        data = self.until(lambda: (item := self.thread(thread_id))["thread"]["status"] == "idle" and item)
        kinds = [item["kind"] for item in data["items"]]
        self.assertEqual(kinds, ["user", "group", "run", "run", "run"])
        group_item = data["items"][1]
        self.assertEqual((group_item["groupId"], group_item["state"]), (group_id, "finished"))
        self.assertEqual(group_item["summary"]["lines"][0]["text"], "This iPhone is on iOS 26.4.")
        self.assertEqual({item["groupId"] for item in data["items"][2:]}, {group_id})
        self.assertEqual(data["thread"]["group"]["phones"], [self.sam, self.work, self.test])
        self.assertEqual(len(data["runs"]), 3)
        status, body = self.request("POST", f"/api/threads/{thread_id}/messages",
                                    {"text": "What iOS version is this iPhone on?"})
        self.assertEqual(status, 201, body)
        self.assertEqual([member["device"] for member in body["group"]["members"]], [self.sam, self.work, self.test])
        self.group_done(body["group"]["id"])

    def test_a_message_while_the_group_runs_is_refused_plainly(self):
        gate = world.hold_at(self.world.spec(self.work).state, "LAUNCH_APP", "com.apple.Preferences")
        self.addCleanup(gate["release"].set)
        status, body = self.request("POST", "/api/threads", {"message": {
            "text": "What iOS version is this iPhone on?", "phones": [self.sam, self.work]}})
        self.assertEqual(status, 201, body)
        thread_id = body["thread"]["id"]
        status, refused = self.request("POST", f"/api/threads/{thread_id}/messages", {"text": "Also check Wi-Fi"})
        self.assertEqual((status, refused["code"]), (409, "group_running"))
        gate["release"].set()
        self.group_done(body["item"]["groupId"])

    def test_one_phone_after_a_group_is_a_one_phone_task(self):
        status, body = self.request("POST", "/api/threads", {"message": {
            "text": "What iOS version is this iPhone on?", "phones": [self.sam, self.work]}})
        thread_id = body["thread"]["id"]
        self.group_done(body["item"]["groupId"])
        self.until(lambda: self.thread(thread_id)["thread"]["status"] == "idle")
        status, body = self.request("POST", f"/api/threads/{thread_id}/messages",
                                    {"text": "What iOS version is this iPhone on?", "device": self.test})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["run"]["device"], self.test)
        self.finished(body["run"]["id"])
        self.assertIsNone(self.thread(thread_id)["thread"].get("group"))


class McpTests(WorldTest):
    TOKEN = "fanout-test-token-000000000000"

    def setUp(self):
        super().setUp()
        from mobile_agent.mcp_server import registry
        from mobile_agent.server import BoundedServer
        from mobile_agent.threads.client import Client
        from mobile_agent.threads.mcp import ThreadsProvider
        server = BoundedServer(("127.0.0.1", 0), make_handler(self.runtime, self.TOKEN))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        saved = list(registry._providers)
        self.addCleanup(lambda: registry._providers.__setitem__(slice(None), saved))
        registry._providers[:] = [ThreadsProvider(connect=lambda: Client.connect(url, origin="mcp",
                                                                                 tokens=[self.TOKEN]))]

    def tools(self, **kwargs):
        from mobile_agent.mcp_server.tools import ToolSet
        tools = ToolSet(runs_dir=tempfile.mkdtemp(prefix="mobster-fanout-mcp-"), keyless=True, devices=lambda: [],
                        **kwargs)
        self.addCleanup(tools.shutdown)
        return tools

    def test_phone_task_runs_on_every_phone_named_and_merges(self):
        tools = self.tools()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def hold_listener():
            entered.set()
            release.wait(30)
        self.runtime.listeners.call(hold_listener)
        self.until(entered.is_set)
        public, observed = self.runtime.fanout.public, []
        def read(group):
            value = public(group)
            if (value["members"] and all(m.get("finishedAt") is not None for m in value["members"])
                    and value["summary"] is None):
                observed.append(value["state"])
                release.set()
            return value
        schema = {tool["name"]: tool for tool in tools.list_tools()}["phone_task"]["inputSchema"]
        self.assertEqual(schema["properties"]["phones"]["minItems"], 2)
        with patch.object(self.runtime.fanout, "public", side_effect=read):
            result = tools.call("phone_task", {"message": "What iOS version is this iPhone on?",
                                               "phones": ["Studio iPhone", "Work iPhone", "Test iPhone"],
                                               "wait_seconds": 30}, None)
        self.assertTrue(observed, "members finished before the summary listener")
        self.assertEqual(set(observed), {"running"})
        self.assertFalse(result.is_error, result.text)
        state = result.structured
        self.assertEqual(state["status"], "finished")
        self.assertEqual([phone["status"] for phone in state["phones"]], ["completed"] * 3)
        self.assertTrue(result.text.startswith("Done on 3 of 3 phones. This iPhone is on iOS 26.4. (3 phones)"),
                        result.text)
        group = self.runtime.fanout.get(state["group_id"])
        self.assertEqual(group["origin"], "mcp")
        status = tools.call("phone_task_status", {"thread_id": state["thread_id"]}, None).structured
        self.assertEqual((status["group_id"], status["status"]), (state["group_id"], "finished"))

    def test_phone_task_runs_as_every_profile_each_on_its_phone_and_names_them(self):
        from mobile_agent.accounts.store import store_for
        notes = {"bundleId": "com.apple.mobilenotes", "name": "Notes"}
        store = store_for(self.runtime)
        store.add({"handle": "getmobster", "app": notes}, device=(self.sam, "Studio iPhone"))
        store.add({"handle": "globex", "app": notes}, device=(self.work, "Work iPhone"))
        tools = self.tools()
        schema = {tool["name"]: tool for tool in tools.list_tools()}["phone_task"]["inputSchema"]
        self.assertIn("profiles", schema["properties"])
        result = tools.call("phone_task", {"message": "List my notes", "profiles": "all", "wait_seconds": 30}, None)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(result.structured["status"], "finished")
        self.assertEqual(sorted((p["profile"], p["status"]) for p in result.structured["phones"]),
                         [("getmobster", "completed"), ("globex", "completed")])
        self.assertEqual(self.runtime.fanout.get(result.structured["group_id"])["origin"], "mcp")
        for mixed in ({"profiles": "all", "profile": "@getmobster"}, {"profiles": "all", "device": "Studio iPhone"},
                      {"profiles": ["@getmobster"], "phones": ["Studio iPhone", "Work iPhone"]}):
            refused = tools.call("phone_task", {"message": "List my notes", **mixed}, None)
            self.assertTrue(refused.is_error, mixed)
        limited = self.tools(allow_devices=["Studio iPhone"])
        self.assertTrue(limited.call("phone_task", {"message": "List my notes", "profiles": "all"}, None).is_error)

    def test_phone_task_never_answers_an_approval_and_honours_allow_device(self):
        tools = self.tools(allow_devices=["Studio iPhone", "Work iPhone"])
        refused = tools.call("phone_task", {"message": "What iOS version is this iPhone on?",
                                            "phones": ["Studio iPhone", "Test iPhone"]}, None)
        self.assertTrue(refused.is_error)
        self.assertIn("--allow-device", refused.text)
        started = tools.call("phone_task", {"message": "Text Alex that I'm running late",
                                            "phones": ["Studio iPhone", "Work iPhone"], "wait_seconds": 0}, None)
        group_id = started.structured["group_id"]
        fanout_ = self.runtime.fanout
        self.until(lambda: sum(1 for m in fanout_.public(fanout_.get(group_id))["members"]
                               if m.get("approval")) == 2)
        result = tools.call("phone_task_status", {"group_id": group_id}, None)
        self.assertEqual(result.structured["status"], "waiting", result.text)
        self.assertIn("2 of 2 phones wait for the user in the Mobster app", result.text)
        group = self.runtime.fanout.get(result.structured["group_id"])
        self.runtime.fanout.stop(group["id"])
        self.group_done(group["id"])


class HeldAdmissionTests(unittest.TestCase):
    def test_restart_finishes_unstarted_members_without_notifying_unready_services(self):
        for state in ("held", "pending"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                group = {"id": "a" * 12, "goal": "Read the screen", "createdAt": 1,
                         "finishedAt": None, "summary": None,
                         "members": [{"device": "phone", "state": state, "runId": None}]}
                fanout.GroupStore(directory).write([group])
                runtime = SimpleNamespace(config=SimpleNamespace(state_db=str(Path(directory) / "state.sqlite3")),
                                          runs={})
                with patch.object(fanout.Fanout, "notify") as notify:
                    manager = fanout.Fanout(runtime, load=FakeLoad())
                restored = manager.public(manager.get(group["id"]))
                self.assertEqual(restored["state"], "finished")
                self.assertEqual(restored["members"][0]["code"], "interrupted")
                self.assertEqual(restored["summary"]["completed"], 0)
                self.assertIsNotNone(manager.store.read()[0]["finishedAt"])
                notify.assert_not_called()

    def manager(self, state="held"):
        runtime = SimpleNamespace(config=SimpleNamespace(state_db=None), create=Mock(), runs={})
        manager = fanout.Fanout(runtime, load=FakeLoad())
        manager.save, manager.notify, manager.finish_if_done = Mock(), Mock(), Mock()
        member = {"device": "phone", "state": state, "runId": None}
        group = {"id": "a" * 12, "members": [member], "held": {"since": time.time() * 1000},
                 "create": {"app_id": None, "goal": "Read the screen"}}
        manager.groups[group["id"]] = group
        return manager, group, member

    def test_stop_during_the_cpu_read_never_admits_the_held_phone(self):
        manager, group, member = self.manager()
        manager.load.percent = lambda: (manager.stop(group["id"]), 20)[1]
        with patch.object(fanout, "HOLD_STAGGER_S", 0):
            manager._held_loop()
        manager.runtime.create.assert_not_called()
        self.assertEqual((member["state"], member["code"]), ("refused", "stopped"))

    def test_stop_refuses_pending_members_before_initial_admission(self):
        manager, group, member = self.manager("pending")
        manager.stop(group["id"])
        self.assertIsNone(manager._admit(group, member))
        manager.runtime.create.assert_not_called()

    def test_a_held_member_still_starts_once_when_not_stopped(self):
        manager, group, member = self.manager()
        run = SimpleNamespace(id="run", device_name="Phone", estimate_usd=None)
        manager.runtime.create.return_value = run
        self.assertIs(manager._admit(group, member), run)
        self.assertIsNone(manager._admit(group, member))
        manager.runtime.create.assert_called_once()
        self.assertEqual((member["state"], member["runId"]), ("started", "run"))

    def test_invalid_group_approvals_never_partially_approve_a_run(self):
        manager, group, member = self.manager()
        member["runId"] = "run"
        run = SimpleNamespace(condition=threading.Condition(), approval={"id": "a", "kind": "commit"},
                              answer_approval=Mock())
        manager.runtime.runs["run"] = run
        with self.assertRaises(ValueError):
            manager.answer_approvals(group["id"], True, {"run": "a", "unknown": "b"})
        run.answer_approval.assert_not_called()

        group["members"].append({"device": "other", "state": "started", "runId": "other"})
        manager.runtime.runs["other"] = SimpleNamespace(condition=threading.Condition(),
                                                       approval={"id": "b", "kind": "clarify"})
        with self.assertRaisesRegex(ValueError, "one at a time"):
            manager.answer_approvals(group["id"], True, {"run": "a", "other": "b"})
        run.answer_approval.assert_not_called()

    def test_history_retention_keeps_active_groups_in_memory_and_on_disk(self):
        manager, active, _ = self.manager()
        active.update(createdAt=0, key="active", goal="Read the screen")
        old = {"id": "b" * 12, "createdAt": 1, "finishedAt": 1, "members": [], "key": "old"}
        new = {"id": "c" * 12, "createdAt": 2, "finishedAt": 2, "members": [], "key": "new"}
        manager.groups.update({group["id"]: group for group in (old, new)})
        manager.keys.update({group["key"]: group["id"] for group in (active, old, new)})
        with tempfile.TemporaryDirectory() as directory, patch.object(fanout, "KEEP", 1):
            manager.store = fanout.GroupStore(directory)
            fanout.Fanout.save(manager)
            self.assertEqual(list(manager.groups), [active["id"], new["id"]])
            self.assertEqual(list(manager.keys), ["active", "new"])
            self.assertEqual([group["id"] for group in manager.store.read()], [active["id"], new["id"]])
            self.assertNotIn("create", manager.store.read()[0])
            self.assertEqual(manager.public(manager.get(active["id"]))["state"], "held")
            manager.finish_if_done = lambda group: fanout.Fanout.finish_if_done(manager, group)
            manager.stop(active["id"])
            self.assertEqual(manager.public(manager.get(active["id"]))["state"], "finished")
            self.assertEqual(active["summary"]["completed"], 0)
            manager.runtime.create.assert_not_called()


class MergeTests(unittest.TestCase):
    def test_lines_found_on_several_phones_are_one_line(self):
        merged = fanout.merge_results([
            {"device": "a", "deviceName": "A", "status": "completed", "costUsd": 0.02,
             "summary": {"answer": "- Alex: dinner Friday\n- Sam: at the park\n"}},
            {"device": "b", "deviceName": "B", "status": "completed", "costUsd": 0.03,
             "summary": {"answer": "• alex: dinner friday.\n• Kate: send the recipe"}},
            {"device": "c", "deviceName": "C", "status": "error", "summary": {"reason": "It was unplugged."}}])
        self.assertEqual([(line["text"], line["phones"]) for line in merged["lines"]],
                         [("Alex: dinner Friday", ["a", "b"]), ("Sam: at the park", ["a"]),
                          ("Kate: send the recipe", ["b"])])
        self.assertEqual((merged["completed"], merged["phones"], merged["costUsd"]), (2, 3, 0.05))
        self.assertEqual(merged["answers"][2], {"device": "c", "deviceName": "C", "status": "error", "answer": None,
                                                "reason": "It was unplugged."})
        self.assertIsNone(merged["rows"])

    def test_records_are_kept_once(self):
        merged = fanout.merge_results([
            {"device": "a", "status": "completed", "summary": {"data": [{"name": "Luca"}, {"name": "Noma"}]}},
            {"device": "b", "status": "completed", "summary": {"data": [{"name": "Luca"}]}}])
        self.assertEqual(merged["rows"], [{"name": "Luca"}, {"name": "Noma"}])


class ProfileFanoutTests(WorldTest):
    def setUp(self):
        super().setUp()
        from mobile_agent.accounts.store import store_for
        self.store = store_for(self.runtime)
        notes = {"bundleId": "com.apple.mobilenotes", "name": "Notes"}
        self.on_sam = self.store.add({"handle": "getmobster", "app": notes}, device=(self.sam, "Studio iPhone"))
        self.on_work = self.store.add({"handle": "globex", "app": notes}, device=(self.work, "Work iPhone"))
        self.on_work_too = self.store.add({"handle": "initech", "app": notes}, device=(self.work, "Work iPhone"))
        self.nowhere = self.store.add({"handle": "umbrella", "app": notes})

    def run_every(self, profiles="all", **extra):
        return self.request("POST", "/api/runs", {"goal": "List my notes", "profiles": profiles, **extra}, key="p-1")

    def test_a_request_for_every_profile_is_one_group_with_a_result_for_each(self):
        status, body = self.run_every("all")
        self.assertEqual(status, 201, body)
        group = body["group"]
        by_profile = {member["profileName"]: member for member in group["members"]}
        self.assertEqual(set(by_profile), {"getmobster", "globex", "initech", "umbrella"})
        self.assertEqual(by_profile["getmobster"]["device"], self.sam)
        self.assertEqual((by_profile["umbrella"]["state"], by_profile["umbrella"]["code"]), ("refused", "no_phone"))
        self.assertEqual([item["profileName"] for item in body["refused"]], ["umbrella"])
        done = self.group_done(group["id"])
        answers = {item["profileName"]: item for item in done["summary"]["answers"]}
        self.assertEqual({name: item["status"] for name, item in answers.items()},
                         {"getmobster": "completed", "globex": "completed", "initech": "completed",
                             "umbrella": "refused"})
        self.assertEqual({run["groupId"] for run in body["runs"]}, {group["id"]})

    def test_profiles_that_share_a_phone_run_one_after_the_other_in_order(self):
        status, body = self.run_every(["@globex", "@initech"])
        self.assertEqual(status, 201, body)
        members = {m["profileName"]: m for m in body["group"]["members"]}
        self.assertEqual((members["globex"]["state"], members["initech"]["state"]), ("started", "queued"))
        self.assertEqual(len(body["runs"]), 1)
        done = self.group_done(body["group"]["id"])
        self.assertEqual([item["profileName"] for item in done["summary"]["answers"]], ["globex", "initech"])
        self.assertEqual(done["counts"]["completed"], 2)
        first, second = (self.runtime.runs[m["runId"]] for m in done["members"])
        self.assertLessEqual(first.finished_at, second.created_at + 1)

    def test_one_profile_failing_does_not_stop_the_others(self):
        self.world.spec(self.work).locked = True
        status, body = self.run_every(["@getmobster", "@globex"])
        self.assertEqual(status, 201, body)
        self.assertEqual([(item["profileName"], item["code"]) for item in body["refused"]],
                         [("globex", "device_unavailable")])
        self.assertEqual(self.group_done(body["group"]["id"])["counts"]["completed"], 1)

    def test_it_refuses_unknown_profiles_other_engines_and_mixed_targets(self):
        for body, text in (({"profiles": ["@nobody"]}, "No profile has that name"),
                           ({"profiles": []}, "profiles is"),
                           ({"profiles": ["@getmobster"], "phones": [self.sam]}, "profiles"),
                           ({"profiles": ["@getmobster"], "device": self.sam}, "phones in phones, or one phone")):
            status, got = self.request("POST", "/api/runs", {"goal": "List my notes", **body})
            self.assertEqual(status, 400, got)
        status, got = self.run_every(["@getmobster"], engine="codex")
        self.assertIn(status, (409, 400), got)
        self.assertEqual(self.runtime.fanout.recent(), [])

    def test_the_same_request_is_the_same_group_and_never_starts_a_profile_twice(self):
        status, first = self.run_every(["@getmobster"])
        status, again = self.run_every(["@getmobster"])
        self.assertEqual((status, again["replayed"], again["group"]["id"]), (200, True, first["group"]["id"]))
        self.group_done(first["group"]["id"])
        self.assertEqual(len([r for r in self.runtime.runs.values() if (r.extras or {}).get("account")]), 1)


class TeamPartsTests(unittest.TestCase):
    """merge_parts: one summary of a task the console ran as one group on each of several Macs (Team plan, C5)."""

    def part(self, *answers, rows=None, cost=None):
        return {"answers": [{"device": device, "deviceName": name, "status": status, "answer": answer, "reason": None}
                            for device, name, status, answer in answers], "rows": rows, "costUsd": cost}

    def test_the_lines_every_mac_found_are_one_each_with_their_phones(self):
        merged = fanout.merge_parts([
            self.part(("aaaaaaaaaaa1", "Rack 1 iPhone", "completed", "- 3 new messages\n- Kim replied"), cost=0.02),
            self.part(("bbbbbbbbbbb2", "Rack 2 iPhone", "completed", "- Kim replied\n- A new follower"), cost=0.03),
            self.part(("ccccccccccc3", "Rack 3 iPhone", "failed", None))])
        self.assertEqual((merged["phones"], merged["completed"]), (3, 2))
        self.assertEqual([(line["text"], line["phones"]) for line in merged["lines"]],
                         [("3 new messages", ["aaaaaaaaaaa1"]), ("Kim replied", ["aaaaaaaaaaa1", "bbbbbbbbbbb2"]),
                          ("A new follower", ["bbbbbbbbbbb2"])])
        self.assertEqual(merged["costUsd"], 0.05)
        self.assertEqual([answer["device"] for answer in merged["answers"]],
                         ["aaaaaaaaaaa1", "bbbbbbbbbbb2", "ccccccccccc3"])

    def test_rows_are_every_parts_once_only_when_every_part_has_rows(self):
        one = self.part(("aaaaaaaaaaa1", "A", "completed", "two rows"), rows=[{"name": "Kim"}, {"name": "Sam"}])
        two = self.part(("bbbbbbbbbbb2", "B", "completed", "one row"), rows=[{"name": "Sam"}, {"name": "Lee"}])
        self.assertEqual(fanout.merge_parts([one, two])["rows"], [{"name": "Kim"}, {"name": "Sam"}, {"name": "Lee"}])
        self.assertIsNone(fanout.merge_parts([one, self.part(("ccccccccccc3", "C", "completed", "text"))])["rows"])


class MacLoadTests(unittest.TestCase):
    def test_reads_a_percent(self):
        load = fanout.MacLoad()
        first = load.percent()
        time.sleep(.6)
        second = load.percent()
        for value in (first, second):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 100.0)


if __name__ == "__main__":
    unittest.main()
