"""Remember-only conversation proposals keep their saved-profile scope."""

import io
import shlex
import unittest

from mobile_agent.accounts.store import store_for
from mobile_agent.tests.test_conversations import FakeClient
from mobile_agent.tests.test_conversations_support import ThreadsBase
from mobile_agent.tests.test_memory import GYM, REMEMBER, scratch_memory
from mobile_agent.threads import chat_once
from mobile_agent.threads import cli as chat
from mobile_agent.threads.term import Term


class ScopedOfferCliTests(unittest.TestCase):
    def test_a_scoped_offer_names_its_profile_in_cli_copy_and_save_command(self):
        proposal = {"id": "p" * 12, "text": GYM, "scope": "profile:aaaaaaaaaaaa", "pin": False}
        out = io.StringIO()
        talk = chat.Chat(FakeClient(), Term(io.StringIO(""), out), color=False, interactive=False)
        talk.offer([proposal])
        self.assertIn("tasks for this saved profile", out.getvalue())
        self.assertIn(f"mobster memory add '{GYM}' --scope profile:aaaaaaaaaaaa", out.getvalue())
        for scope, phrase in (("workflow:111111111111", "tasks in this workflow"),
                              ("profile:aaaaaaaaaaaa/workflow:111111111111",
                               "tasks for this saved profile and workflow")):
            scoped = {**proposal, "scope": scope, "pin": True}
            output = io.StringIO()
            chat.Chat(FakeClient(), Term(io.StringIO(""), output), color=False,
                      interactive=False).offer([scoped])
            self.assertIn(phrase, output.getvalue())
            self.assertNotIn("every task", output.getvalue())
        literal = 'Remember "$HOME" and `echo nope`'
        command = chat_once.where_to_remember({"text": literal, "scope": proposal["scope"]}).split(
            "save it with: ", 1)[1]
        self.assertEqual(shlex.split(command), ["mobster", "memory", "add", literal,
                                                 "--scope", proposal["scope"]])


class ScopedRememberTests(ThreadsBase):
    def test_remember_only_uses_the_saved_profile_and_deduplicates_within_that_scope(self):
        memory = scratch_memory(self)
        runtime = self.runtime()
        account = store_for(runtime).add({"handle": "getmobster", "app": {"bundleId": "com.apple.MobileSMS",
                                                                     "name": "Messages"}})
        memory.add_fact(GYM)  # A global fact does not own a profile's words.
        status, answer, _ = self.call(runtime, "POST", "/api/threads",
                                      {"message": {"text": REMEMBER, "account": account["id"]}},
                                      headers={"X-Mobster-Origin": "cli"})
        self.assertEqual(status, 201, answer)
        thread_id = answer["thread"]["id"]
        self.assertEqual(answer["proposals"][0]["scope"], f"profile:{account['id']}")
        self.assertEqual(runtime.runs, {})
        proposal = answer["proposals"][0]
        saved = memory.resolve_proposal(proposal["id"], True)[1]
        self.assertEqual(saved["scope"], f"profile:{account['id']}")
        before = self.items(runtime, thread_id)
        status, refused, _ = self.call(runtime, "POST", f"/api/threads/{thread_id}/messages",
                                       {"text": REMEMBER, "account": account["id"]},
                                       headers={"X-Mobster-Origin": "cli"})
        self.assertEqual((status, refused["code"]), (409, "memory_duplicate"))
        self.assertEqual(self.items(runtime, thread_id), before)
        status, global_offer, _ = self.call(runtime, "POST", f"/api/threads/{thread_id}/messages",
                                            {"text": REMEMBER, "account": None},
                                            headers={"X-Mobster-Origin": "cli"})
        self.assertEqual((status, global_offer["code"]), (409, "memory_duplicate"))
        other = store_for(runtime).add({"handle": "other", "app": {"bundleId": "com.apple.MobileSMS",
                                                                    "name": "Messages"}})
        status, offered, _ = self.call(runtime, "POST", f"/api/threads/{thread_id}/messages",
                                       {"text": REMEMBER, "account": other["id"]},
                                       headers={"X-Mobster-Origin": "cli"})
        self.assertEqual(status, 201, offered)
        self.assertEqual(offered["proposals"][0]["scope"], f"profile:{other['id']}")

    def test_remember_only_inherits_a_valid_profile_and_refuses_missing_or_group_scope_without_writes(self):
        memory = scratch_memory(self)
        runtime = self.runtime()
        account = store_for(runtime).add({"handle": "getmobster", "app": {"bundleId": "com.apple.MobileSMS",
                                                                     "name": "Messages"}})
        status, started, _ = self.call(runtime, "POST", "/api/threads",
                                       {"message": {"text": "Find my last message from Kate Bell",
                                                    "account": account["id"]}},
                                       headers={"X-Mobster-Origin": "cli"})
        self.assertEqual(status, 201, started)
        thread_id = started["thread"]["id"]
        self.finish(runtime, runtime.runs[started["run"]["id"]])
        self.settle(runtime)
        status, inherited, _ = self.call(runtime, "POST", f"/api/threads/{thread_id}/messages",
                                          {"text": REMEMBER}, headers={"X-Mobster-Origin": "cli"})
        self.assertEqual(status, 201, inherited)
        self.assertEqual(inherited["proposals"][0]["scope"], f"profile:{account['id']}")
        before = self.items(runtime, thread_id)
        for body, code in (({"text": REMEMBER, "account": "not-a-profile"}, None),
                           ({"text": REMEMBER, "profiles": [account["id"]]}, "memory_profile_ambiguous"),
                           ({"text": "Remember that my card is 4111 1111 1111 1111",
                             "account": account["id"]}, "memory_secret")):
            status, refused, _ = self.call(runtime, "POST", f"/api/threads/{thread_id}/messages", body,
                                           headers={"X-Mobster-Origin": "cli"})
            self.assertNotEqual(status, 201)
            if code:
                self.assertEqual(refused["code"], code)
            self.assertEqual(self.items(runtime, thread_id), before)
        store_for(runtime).remove(account["id"])
        status, removed, _ = self.call(runtime, "POST", f"/api/threads/{thread_id}/messages",
                                       {"text": REMEMBER}, headers={"X-Mobster-Origin": "cli"})
        self.assertNotEqual(status, 201)
        self.assertEqual(self.items(runtime, thread_id), before)
        self.assertEqual(memory.all_facts(), [])
        self.assertEqual(len(memory.list_proposals()), 1)
