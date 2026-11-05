
import unittest
from unittest import mock

from mobile_agent.run import Run, phone_wait
from mobile_agent.threads import mcp
from mobile_agent.threads.client import Client

APP = {"id": "any", "name": "Any app"}


def waiting(kind="lock_screen", **fields):
    return {"event": "handoff_waiting", "kind": kind, "seconds": 120, "timestamp": 1_000_000.0, **fields}


class PhoneWaitTests(unittest.TestCase):
    def test_the_newest_wait_with_no_end_is_the_one(self):
        self.assertEqual(phone_wait([{"event": "run_started"}, waiting()]),
                         {"kind": "lock_screen", "seconds": 120, "since": 1_000_000.0, "midrun": False})
        self.assertEqual(phone_wait([waiting("unplugged", midrun=True)])["midrun"], True)
        self.assertIsNone(phone_wait([waiting(), {"event": "handoff_finished", "kind": "lock_screen", "ok": True}]))
        self.assertIsNone(phone_wait([waiting("face_id")]))
        self.assertIsNone(phone_wait([]))

    def test_a_running_task_shows_it_and_a_finished_one_never_does(self):
        run = Run(dict(APP), "What iOS version is this iPhone on?", "live", engine="smart")
        self.assertNotIn("phoneWait", run.public())
        run.emit({"event": "handoff_waiting", "kind": "lock_screen", "seconds": 120})
        self.assertEqual(run.public(include_events=False)["phoneWait"]["kind"], "lock_screen")
        run.emit({"event": "handoff_finished", "kind": "lock_screen", "ok": True})
        self.assertNotIn("phoneWait", run.public())


class McpTests(unittest.TestCase):
    def test_waiting_for_the_phone_names_what_to_do(self):
        run = {"finishedAt": None, "approval": None, "deviceName": "Work iPhone",
               "phoneWait": {"kind": "lock_screen", "seconds": 120, "since": 1_000_000.0, "midrun": True}}
        state = mcp.describe(run)
        self.assertEqual(state, {"status": "waiting_for_phone", "need": "unlock",
                                 "message": "Unlock Work iPhone: the task goes on as soon as it's unlocked.",
                                 "expires_at": 1_120_000.0})
        for kind, need in (("unplugged", "plug_in"), ("wifi_lost", "bring_back"), ("passcode_keypad", "unlock")):
            self.assertEqual(mcp.describe({**run, "phoneWait": {**run["phoneWait"], "kind": kind}})["need"], need)
        approval = {**run, "approval": {"title": "Send", "kind": "approval"}}
        self.assertEqual(mcp.describe(approval)["status"], "waiting_for_approval")

    def test_a_running_task_is_still_running(self):
        self.assertEqual(mcp.describe({"finishedAt": None, "approval": None}), {"status": "running"})


class WaitRunTests(unittest.TestCase):
    def client(self, *runs):
        client = Client.__new__(Client)
        client.run = mock.Mock(side_effect=list(runs))
        return client

    def test_a_wait_on_the_phone_ends_the_wait_so_the_caller_can_tell_the_person(self):
        busy = {"finishedAt": None, "approval": None}
        locked = {**busy, "phoneWait": {"kind": "lock_screen", "seconds": 120, "since": 1.0}}
        client = self.client(busy, locked)
        with mock.patch("mobile_agent.threads.client.time.sleep"):
            self.assertIs(client.wait_run("r1", 30, poll=0.01, through_approval=True), locked)

    def test_through_keeps_waiting(self):
        locked = {"finishedAt": None, "approval": None, "phoneWait": {"kind": "lock_screen"}}
        done = {"finishedAt": 2.0, "approval": None}
        client = self.client(locked, done)
        with mock.patch("mobile_agent.threads.client.time.sleep"):
            self.assertIs(client.wait_run("r1", 30, poll=0.01, through=True), done)


if __name__ == "__main__":
    unittest.main()
