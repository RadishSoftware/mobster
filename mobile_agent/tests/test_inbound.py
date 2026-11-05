"""Commands from Slack and Discord (outlets/inbound.py): a linked person starts a workflow turned on for the channel.

Through the real Runtime on the scripted phone and model, with a stand-in for Remote (the relay side is
relay/test/inbound.test.ts) and a recorded reply post. A stranger, a stale code, a workflow that isn't turned on, a
word that isn't a blank and a replayed command start nothing. Offline: no phone, key or network.
"""

import time
import unittest
from unittest import mock

from mobile_agent.outlets import inbound, inbound_reply
from mobile_agent.tests.test_outlet_hooks import HookBase

# Built at runtime so no committed line holds a whole Discord-id-shaped literal.
DISCORD_USER = "".join(str(d) for d in range(1, 10)) * 2
DISCORD_APP = "".join(str(d) for d in range(9, 0, -1)) * 2
USER = {"slack": "T0123ABCD:U0456EFGH", "discord": DISCORD_USER}
REPLY = {"slack": {"url": "https://hooks.slack.com/commands/T0123ABCD/1/abc"},
         "discord": {"app": DISCORD_APP, "token": "a" * 80}}


class FakeRemote:
    def __init__(self):
        self.told = []

    def address(self, prefix):
        return "https://remote.mobster.dev/hooks/x"

    def hooks_changed(self, ids):
        pass

    def inbound_code(self, channel, digest):
        self.told.append(("code", channel, digest))

    def inbound_unlink(self, channel, sender):
        self.told.append(("unlink", channel, sender))


class InboundBase(HookBase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict("os.environ", {inbound.FLAG: "1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.inbound = inbound.service_for(self.runtime)
        self.inbound._remote = FakeRemote()
        self.posts = []
        self.inbound.call = lambda method, url, data, headers: self.posts.append((url, data)) or mock.Mock(status=200)
        self.n = 0

    def publish(self, goal="Text {person: Alex} that I'm running late", channel="slack"):
        workflow = self.save(goal)
        status, data = self.call("POST", f"/api/workflows/{workflow['id']}/publish", {"revision": workflow["revision"]})
        self.assertIn(status, (200, 201), data)
        self.inbound.enable(channel, workflow["id"])
        return workflow

    def link(self, channel="slack"):
        code = self.inbound.new_code(channel)["code"]
        answer = self.say(channel, "link", code.lower())
        self.assertEqual(answer["status"], 200, answer)

    def say(self, channel, verb, text="", sender=None, key=None, reply=True):
        self.n += 1
        return self.inbound.handle({"channel": channel, "sender": sender or USER[channel], "label": "sam", "verb": verb,
                                    "text": text, "key": key or f"key-{self.n}-{time.time()}",
                                    "reply": REPLY[channel] if reply else None})

    def runs(self):
        return self.runtime.runs


class Linking(InboundBase):
    def test_a_code_links_one_person_once(self):
        code = self.inbound.new_code("slack")
        self.assertRegex(code["code"], r"^[0-9A-Z]{4}-[0-9A-Z]{4}$")
        [(kind, channel, digest)] = self.inbound._remote.told
        self.assertEqual((kind, channel, digest), ("code", "slack", inbound.code_hash(inbound.normalize(code["code"]))))
        self.assertEqual(self.say("slack", "link", code["code"].lower())["status"], 200)
        self.assertEqual([s["sender"] for s in self.inbound.store.senders("slack")], [USER["slack"]])
        self.assertEqual(self.say("slack", "link", code["code"], sender="T0123ABCD:UOTHER001")["status"], 403)

    def test_a_wrong_stale_or_other_channels_code_links_nobody(self):
        code = self.inbound.new_code("slack")["code"]
        self.assertEqual(self.say("slack", "link", "ZZZZ-ZZZZ")["status"], 403)
        self.assertEqual(self.say("discord", "link", code)["status"], 403)
        self.inbound.clock = lambda: time.time() + inbound.CODE_SECONDS + 1
        self.assertEqual(self.say("slack", "link", code)["status"], 403)
        self.assertEqual(self.inbound.store.senders(), [])

    def test_a_stranger_starts_nothing(self):
        self.publish()
        answer = self.say("slack", "run", "Running late person=Sam")
        self.assertEqual(answer["status"], 403)
        self.assertEqual(self.runs(), {})

    def test_forgetting_a_person_stops_them_and_tells_the_relay(self):
        self.publish()
        self.link()
        self.inbound.forget("slack", USER["slack"])
        self.assertEqual(self.inbound._remote.told[-1], ("unlink", "slack", USER["slack"]))
        self.assertEqual(self.say("slack", "run", "Running late")["status"], 403)

    def test_off_by_default(self):
        with mock.patch.dict("os.environ", {inbound.FLAG: "0"}):
            self.assertEqual(self.say("slack", "list")["status"], 503)


class Running(InboundBase):
    def test_a_linked_person_starts_a_workflow_and_gets_the_result_in_their_thread(self):
        self.publish()
        self.link()
        answer = self.say("slack", "run", "running LATE person=Sam Lee")
        self.assertEqual((answer["status"], answer["public"]), (202, False), answer)
        self.assertIn("waits for your OK", answer["message"])
        run = self.until(lambda: next(iter(self.runs().values()), None), "the workflow's run")
        self.assertEqual(run.origin, "hook")
        self.assertIn("Sam Lee", run.goal)
        self.until(lambda: run.finished_at or None, "the run to end")
        self.inbound.poll()
        [(url, data)] = self.posts
        self.assertEqual(url, REPLY["slack"]["url"])
        self.assertIn(b"ephemeral", data)
        self.assertIn(b"Running late", data)

    def test_discord_replies_to_its_interaction_without_pinging_anyone(self):
        self.publish(channel="discord")
        self.link("discord")
        self.assertEqual(self.say("discord", "run", "Running late Sam")["status"], 202)
        run = self.until(lambda: next(iter(self.runs().values()), None), "the workflow's run")
        self.until(lambda: run.finished_at or None, "the run to end")
        self.inbound.poll()
        [(url, data)] = self.posts
        self.assertTrue(url.startswith(f"{inbound_reply.DISCORD_API}/{DISCORD_APP}/"))
        self.assertIn(b'"parse": []', data)

    def test_the_words_fill_blanks_and_nothing_else(self):
        self.publish("Text {person} I'm {minutes: 5} min late")
        self.link()
        for text, status in (("Running late colour=blue", 422), ("Running late Sam", 422), ("Nothing like it", 404)):
            self.assertEqual(self.say("slack", "run", text)["status"], status, text)
        self.assertEqual(self.runs(), {})

    def test_only_a_workflow_turned_on_for_the_channel_runs(self):
        workflow = self.publish(channel="discord")
        self.link()
        self.assertEqual(self.say("slack", "run", "Running late")["status"], 404)
        self.inbound.enable("slack", workflow["id"])
        self.inbound.disable("slack", workflow["id"])
        self.assertEqual(self.say("slack", "run", "Running late")["status"], 404)
        self.assertEqual(self.runs(), {})

    def test_the_same_command_starts_one_run(self):
        self.publish()
        self.link()
        self.assertEqual(self.say("slack", "run", "Running late", key="same")["status"], 202)
        self.assertEqual(self.say("slack", "run", "Running late", key="same")["status"], 409)
        self.until(lambda: len(self.runs()) == 1 or None, "one run")
        time.sleep(0.1)
        self.assertEqual(len(self.runs()), 1)

    def test_a_reply_address_that_is_not_the_platforms_is_never_posted_to(self):
        self.assertIsNone(inbound_reply.address("slack", {"url": "https://evil.example/commands/x"}))
        self.assertIsNone(inbound_reply.address("slack", {"url": "https://hooks.slack.com.evil.example/commands/x"}))
        self.assertIsNone(inbound_reply.address("discord", {"app": "1", "token": "a" * 80}))

    def test_list_names_what_is_turned_on(self):
        self.publish()
        self.link()
        answer = self.say("slack", "list")
        self.assertEqual(answer["status"], 200)
        self.assertIn("Running late (person)", answer["message"])


class Reading(unittest.TestCase):
    def workflow(self, goal):
        return {"name": "Running late", "goal": goal}

    def test_values(self):
        w = self.workflow("Text {person} I'm {minutes: 5} min late")
        self.assertEqual(inbound.read_values(w, "person=Sam Lee minutes: 15"),
                         ({"person": "Sam Lee", "minutes": "15"}, None))
        self.assertEqual(inbound.read_values(w, "person=\"Sam\""), ({"person": "Sam"}, None))
        self.assertIsNone(inbound.read_values(w, "Sam")[0])
        self.assertEqual(inbound.read_values(self.workflow("Text {person}"), "Sam"), ({"person": "Sam"}, None))
        self.assertIsNone(inbound.read_values(self.workflow("Open Notes"), "x=1")[0])


if __name__ == "__main__":
    unittest.main()
