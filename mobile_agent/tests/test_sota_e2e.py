"""The SOTA tracks together, end to end: the real HTTP service, Runtime, frontier loop and every track (threads, memory,
attachments, harness, testkit) over a scripted phone and a scripted model (sota_world), driven the way the Mac app and
`mobster chat` drive them. A four-task conversation whose follow-ups use earlier answers, a message that steers a
task mid-run, a send that still asks (and a typed "yes" that doesn't approve it), a clarifying question answered from
the thread, a memory suggested, accepted and used in a later conversation, an attached file the agent reads, and a
finished task recorded as a check that `mobster test` runs. Offline: no phone, no keys, no model calls, a scratch HOME,
and a server on port 0 (never the Mac app's 8765).

This is the integration check SPEC §5.2 asks for after the tracks merge; each piece also has its own unit tests."""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
import uuid

from mobile_agent.tests import sota_world as world
from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.test_engines import CLEAN

TOKEN = "e" * 43
MENU = ("Luca, dinner menu\n\nStarters\n- Burrata with peaches (vegetarian)\n- Fried calamari\n\nMains\n"
        "- Mushroom risotto (vegetarian)\n- Eggplant parmigiana (vegetarian)\n- Steak frites\n")


class Service(unittest.TestCase):
    """One scripted Mobster per test: ``self.url``, ``self.runtime``, ``self.phone`` (the scripted phone's state)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-sota-e2e-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        env = patch.dict(os.environ, {**CLEAN, "HOME": str(self.root), "MOBSTER_NO_DEVICES": "1",
                                      "MOBSTER_MEMORY_DIR": str(self.root / "memory")}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
            os.environ.pop(name, None)
        isolate(self)
        from mobile_agent import engines, server
        from mobile_agent.journal import Lease
        from mobile_agent.tui.session import DemoRuntime, serve_config
        self.calls = []
        self.phone = world.new_phone_state()
        kwargs = engines.frontier_kwargs
        for target, value in (("smart_key", lambda env=None: "scripted"),
                              ("acquire_client", lambda key: world.ScriptedModel(self.calls)),
                              ("release_client", lambda client, ok: None),
                              # The scripted phone changes at once: no 0.6 s settle after each action.
                              ("frontier_kwargs", lambda *a, **kw: {**kwargs(*a, **kw), "settle_seconds": 0})):
            patcher = patch.object(engines, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        lease = patch("mobile_agent.server.Lease.device", side_effect=lambda _: Lease(self.root / "device.lock"))
        lease.start()
        self.addCleanup(lease.stop)
        phone = self.phone

        class Scripted(DemoRuntime):
            pace = 0

            def default_engine(self):
                return engines.SMART

            def scripted_models(self):
                return False

            def open_driver(self, run, trace, setup):
                driver = setup.hold(world.ScriptedPhone(phone))
                if run.stop.is_set():
                    return None
                world.preflight(self, run, driver)  # the real guard: a locked scripted phone stops or waits
                if run.stop.is_set():
                    return None
                if (run.app or {}).get("bundleId"):
                    driver.execute("LAUNCH_APP", run.app["bundleId"], None)
                return driver

        self.state_db = self.root / "Library" / "Application Support" / "app.mobster.desktop" / "state" / "mobster.sqlite3"
        self.state_db.parent.mkdir(parents=True)
        config = serve_config(wda_url="demo://phone", enable_live=True, state_db=str(self.state_db), env_file=None,
                              data_dir=None, port=0)
        self.runtime = Scripted(config)
        self.addCleanup(self.runtime.close)
        httpd = server.BoundedServer(("127.0.0.1", 0), server.make_handler(self.runtime, TOKEN, app_session=None))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.url = f"http://127.0.0.1:{httpd.server_address[1]}"

    # -- HTTP, as the Mac app sends it -------------------------------------------------------------------------------

    def call(self, method, path, body=None, *, raw=None, origin="app", key=True):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if origin:
            headers["X-Mobster-Origin"] = origin
        if key and method == "POST" and raw is None:
            headers["Idempotency-Key"] = uuid.uuid4().hex
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = "application/octet-stream" if raw is not None else "application/json"
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    def run_of(self, run_id):
        status, data = self.call("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 200, data)
        return data["run"]

    def until(self, check, what, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(.02)
        self.fail(f"timed out waiting for {what}")

    def finished(self, run_id):
        return self.until(lambda: (lambda run: run if run.get("finishedAt") else None)(self.run_of(run_id)),
                          f"task {run_id} to finish")

    def asked(self, run_id, kind=None):
        def pending():
            approval = self.run_of(run_id).get("approval")
            return approval if approval and (kind is None or approval.get("kind") == kind) else None
        return self.until(pending, f"task {run_id} to ask ({kind or 'anything'})")

    def items(self, thread_id):
        status, data = self.call("GET", f"/api/threads/{thread_id}")
        self.assertEqual(status, 200, data)
        return data["items"]

    def result(self, thread_id, run_id):
        item = next(i for i in self.items(thread_id) if i.get("kind") == "run" and i.get("runId") == run_id)
        return item.get("result") or {}

    def start(self, text, thread_id=None, origin="app", **body):
        if thread_id is None:
            status, data = self.call("POST", "/api/threads", {"message": {"text": text, **body}}, origin=origin)
        else:
            status, data = self.call("POST", f"/api/threads/{thread_id}/messages", {"text": text, **body}, origin=origin)
        self.assertEqual(status, 201, data)
        return (data.get("thread") or {}).get("id") or thread_id, (data.get("run") or {}).get("id")

    def prompts(self, request_part):
        return [c["prompt"] for c in self.calls if request_part in c["request"]]


class ConversationTests(Service):
    def test_four_tasks_in_one_conversation_with_a_steer_and_an_approval(self):
        thread, first = self.start("What's the address of the dinner place Alex sent me in Messages?")
        self.finished(first)
        answer = self.result(thread, first)
        self.assertIn("214 Pine St", answer["answer"])
        self.assertEqual(answer["outcome"], "done")

        # A follow-up that only works with the first answer in its context, which is fenced as data.
        _, second = self.start("When does that place close tonight?", thread)
        self.finished(second)
        self.assertIn("10 pm", self.result(thread, second)["answer"].casefold())
        prompt = self.prompts("When does that place close")[0]
        earlier = prompt[prompt.index("What Mobster's earlier tasks"):]
        self.assertIn("<<<", earlier.split("214 Pine St")[0])

        # A send, steered mid-task: held inside its tap on the contact while the message arrives.
        gate = world.hold_at(self.phone, "TAP", "Alex Rivera")
        _, third = self.start("Text Alex that I'm running late", thread)
        self.until(gate["reached"].is_set, "the task to reach the contact")
        status, routed = self.call("POST", f"/api/threads/{thread}/messages", {"text": "Actually make it 15 minutes"})
        self.assertEqual((status, routed.get("routed")), (202, "steer"), routed)
        gate["release"].set()
        approval = self.asked(third, "commit")
        self.assertIn("15 minutes", approval["text"])
        # Words typed while it waits are refused and approve nothing.
        status, refused = self.call("POST", f"/api/threads/{thread}/messages", {"text": "yes send it"})
        self.assertEqual((status, refused.get("code")), (409, "approval_pending"), refused)
        self.assertEqual(self.run_of(third)["approval"]["id"], approval["id"])
        self.assertEqual(self.phone["sent"], {})
        status, _ = self.call("POST", f"/api/runs/{third}/approval", {"id": approval["id"], "approve": True}, origin=None)
        self.assertEqual(status, 200)
        run = self.finished(third)
        self.assertIn("steer_applied", [e.get("event") for e in run["events"]])
        self.assertEqual(self.phone["sent"], {"Alex Rivera": ["Running 15 minutes late, sorry!"]})
        self.assertEqual(self.result(thread, third)["outcome"], "done")
        steers = [i for i in self.items(thread) if i.get("routed") == "steer"]
        self.assertEqual([i["text"] for i in steers], ["Actually make it 15 minutes"])

        # A fourth task that uses the first one's answer again, in another app.
        _, fourth = self.start("Save that address in a new note", thread)
        self.finished(fourth)
        self.assertTrue(any("214 Pine St" in note for note in self.phone["notes"]), self.phone["notes"])
        runs = [i for i in self.items(thread) if i.get("kind") == "run"]
        self.assertEqual([i["result"]["outcome"] for i in runs], ["done"] * 4)


class QuestionTests(Service):
    def test_a_task_started_in_a_conversation_asks_and_the_next_message_answers(self):
        for origin in ("app", "cli"):
            with self.subTest(origin=origin):
                self.phone["sent"].clear()
                thread, run_id = self.start("Text Sam that I'm here", origin=origin)
                question = self.asked(run_id, "clarify")
                self.assertEqual([c["label"] for c in question["choices"]], ["Sam Lee", "Sam Ortiz"])
                status, routed = self.call("POST", f"/api/threads/{thread}/messages", {"text": "Sam Ortiz"},
                                           origin=origin)
                self.assertEqual((status, routed.get("routed")), (202, "answer"), routed)
                approval = self.asked(run_id, "commit")          # the answer approved nothing: the send still asks
                self.assertIn("Sam Ortiz", approval["title"])
                self.call("POST", f"/api/runs/{run_id}/approval", {"id": approval["id"], "approve": True}, origin=None)
                self.finished(run_id)
                self.assertEqual(self.phone["sent"], {"Sam Ortiz": ["I'm here, by the entrance"]})
                self.assertIn("clarify", [i["kind"] for i in self.items(thread)])

    def test_stopping_while_a_question_waits_frees_the_phone(self):
        """App track ask (7 Oct): stopping a task while its question waited left every next task refused as
        "busy in another Mobster process"."""
        thread, run_id = self.start("Text Sam that I'm here")
        self.asked(run_id, "clarify")
        status, _ = self.call("POST", f"/api/runs/{run_id}/stop", {}, origin=None)
        self.assertIn(status, (200, 202))
        self.assertEqual(self.finished(run_id)["status"], "stopped")
        _, next_run = self.start("What's on my calendar tomorrow?", thread)   # 201, not 503 device_busy
        self.finished(next_run)

    def test_nobody_is_asked_over_the_plain_api(self):
        thread, run_id = self.start("Text Sam that I'm here", origin=None)
        run = self.finished(run_id)
        self.assertNotIn("clarify", [e.get("approval", {}).get("kind") for e in run["events"]
                                     if e.get("event") == "approval_requested"])
        self.assertIn("Sam Lee", self.result(thread, run_id)["answer"])
        self.assertEqual(self.phone["sent"], {})


class MemoryAndFilesTests(Service):
    def test_a_memory_is_suggested_accepted_and_used_in_a_later_conversation(self):
        # A message that only asks Mobster to remember something runs nothing on the phone: it gets the suggestion.
        thread, run_id = self.start("Remember that my gym is Equinox on 5th Street")
        self.assertIsNone(run_id)
        proposal = self.until(lambda: next((i for i in self.items(thread) if i["kind"] == "memory_proposal"), None),
                              "the suggestion in the conversation")
        self.assertEqual(self.call("GET", "/api/memory/facts")[1]["facts"], [])     # nothing stored before Remember
        status, _ = self.call("POST", f"/api/memory/proposals/{proposal['proposalId']}", {"accept": True})
        self.assertEqual(status, 200)
        later, used = self.start("When does my gym close today?")
        run = self.finished(used)
        self.assertIn("11 pm", self.result(later, used)["answer"].casefold())
        self.assertIn("memory_used", [e.get("event") for e in run["events"]])
        self.assertEqual(self.call("POST", "/api/memory/facts", {"text": "My bank PIN is 4821"})[0], 400)

    def test_the_agent_reads_an_attached_file_as_data(self):
        status, data = self.call("POST", "/api/attachments?name=dinner-menu.txt", raw=MENU.encode())
        self.assertEqual(status, 201, data)
        self.assertEqual(self.call("POST", "/api/attachments?name=tool.exe", raw=b"MZ\x90\x00" + b"\x00" * 64)[0], 415)
        thread, run_id = self.start("Which dishes on this menu are vegetarian?", attachmentIds=[data["attachment"]["id"]])
        run = self.finished(run_id)
        self.assertIn("mushroom risotto", self.result(thread, run_id)["answer"].casefold())
        self.assertIn("attachment_read", [e.get("event") for e in run["events"]])
        prompt = self.prompts("Which dishes on this menu")[0]
        self.assertIn("<<<", prompt.split("Mushroom risotto")[0])
        user = next(i for i in self.items(thread) if i["kind"] == "user")
        self.assertEqual([a["name"] for a in user["attachments"]], ["dinner-menu.txt"])


class CheckTests(Service):
    def test_a_finished_task_becomes_a_check_that_mobster_test_runs(self):
        from mobile_agent import __main__ as cli
        from mobile_agent.extensions import Hooks
        from mobile_agent.testkit import api as testkit
        from mobile_agent.testkit.record import check_yaml, find_run
        _, run_id = self.start("What iOS version is this iPhone on?")
        self.finished(run_id)
        project = self.root / "Daybreak"
        checks = project / ".mobster" / "checks"
        checks.mkdir(parents=True)
        path = checks / "ios-version.yaml"
        path.write_text(check_yaml(find_run(run_id, paths=[self.state_db]), name="Shows iOS 26.4 in About",
                                   path=".mobster/checks/ios-version.yaml"))
        text = path.read_text()
        self.assertIn("- Tap General", text)
        self.assertIn("- text: '26.4'", text)

        class Replay:
            """Each attempt does the check's steps on a fresh scripted phone, then reads its expectations off the last
            screen (verify's own runner needs a simulator; this proves the recorded check is one that passes)."""

            def __call__(self, check, record, *, label, video_name=None):
                phone = world.ScriptedPhone(world.new_phone_state())
                phone.execute("LAUNCH_APP", check.bundle_id, None)
                for step in check.steps:
                    tap = re.fullmatch(r"Tap (.+)", step.strip())
                    target = tap and next((e for e in phone.observe().elements if e.label == tap.group(1)), None)
                    if target is not None:
                        phone.execute("TAP", target, None)
                screen = phone.observe().text
                missing = [a.needle.text for a in check.expect if a.needle.text not in screen]
                return {"verdict": "failed" if missing else "passed", "run_id": "20261007-120000-e2e0",
                        "reason": {"class": "assertion", "message": f"{missing} not on screen"} if missing else None,
                        "seconds": 0.1, "device": {"name": "Scripted iPhone"}}

            def abort_all(self, message=""):
                pass

        real = testkit.execute
        out, err = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)
        with patch.object(cli, "load_extensions", return_value=Hooks()), \
                patch.object(testkit, "execute", lambda planned, **kw: real(planned, attempts=Replay(), **kw)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["test", str(path)])
            except SystemExit as stop:
                code = stop.code
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertIn("1 passed", out.getvalue())
        self.assertTrue(list((project / ".mobster" / "test-results").glob("*/junit.xml")))


if __name__ == "__main__":
    unittest.main()
