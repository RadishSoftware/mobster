import unittest
from unittest import mock

from mobile_agent import engines
from mobile_agent.ai_plans import agents, draft_client
from mobile_agent.graph import draft
from mobile_agent.graph.schema import Context
from mobile_agent.workflows import starters

PLAN = {"name": "Morning round", "schedule": "0 9 * * *", "steps": [
    {"id": "a", "kind": "task", "text": "Open Settings", "after": [], "app": "settings", "reader": None,
     "seconds": None,
     "to": None, "cases": None, "over": None, "times": None, "inside": None}]}


class Agent:
    name, env, drop_env = "Claude Code", {}, ()

    def locate(self):
        return "/bin/claude"


def drafter_with(output):
    seen = []
    client = draft_client.AgentDrafter("claude_code", run=lambda argv, stdin, env, timeout: seen.append(
        (argv, stdin)) or output)
    return client, seen


class ClientChoiceTests(unittest.TestCase):
    def choose(self, key=None, plan=False, ready=None):
        with mock.patch.object(engines, "anthropic_key", lambda env=None: key), \
                mock.patch.object(engines, "on_plan", lambda model: plan), \
                mock.patch.object(engines, "smart_key", lambda env=None: "plan-key" if plan else None), \
                mock.patch.object(engines, "build_client", lambda key=None: mock.Mock(model="kimi")), \
                mock.patch.object(agents, "ready", lambda env=None: ready):
            return starters.draft_client()

    def test_a_key_then_a_plan_then_a_ready_agent_then_nothing(self):
        self.assertEqual(self.choose(key="sk-ant-x", ready="codex").model, starters.DRAFT_MODEL)
        self.assertEqual(self.choose(plan=True, ready="codex").model, "kimi")
        agent = self.choose(ready="claude_code")
        self.assertEqual((type(agent).__name__, agent.agent_id), ("AgentDrafter", "claude_code"))
        self.assertIsNone(self.choose())
        self.assertIsNone(self.choose(ready="gemini"))        # no one-shot for it: the rules draft, and say so


class AgentDrafterTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(agents, "get", lambda agent_id: Agent())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_asks_with_no_tools_and_the_prompt_on_stdin_and_reads_the_first_json_object(self):
        client, seen = drafter_with('Sure.\n{"name":"x","schedule":null,"steps":[]}\n{"other":1}')
        out, _ = client.complete([{"role": "system", "content": "SYS"},
                                  {"role": "user", "content": [{"type": "text", "text": "every day, ping Sam"}]}],
                                 {"type": "object"}, timeout=5)
        self.assertEqual(out, {"name": "x", "schedule": None, "steps": []})
        argv, stdin = seen[0]
        self.assertIn("every day, ping Sam", stdin)
        self.assertNotIn("every day, ping Sam", " ".join(argv))
        self.assertEqual(argv[argv.index("--tools") + 1], "")

class DraftedByTests(unittest.TestCase):
    CONTEXT = Context(packs={})

    def test_a_ready_agent_drafts_and_the_answer_says_a_model_did(self):
        with mock.patch.object(agents, "get", lambda agent_id: Agent()):
            client, _ = drafter_with(__import__("json").dumps(PLAN))
            made = draft.draft("Every day at 9 open Settings", self.CONTEXT, client=client)
        self.assertEqual((made["drafted_by"], made["drafter"]), ("model", "claude_code"))
        self.assertNotIn("notes", made)

    def test_with_no_drafter_the_rules_draft_and_say_so_in_words(self):
        made = draft.draft("Every weekday at 9am, open Settings, read the iOS version", self.CONTEXT)
        self.assertEqual((made["drafted_by"], made["drafter"]), ("rules", "local"))
        self.assertIn("Drafted by simple rules", made["notes"][0])
        self.assertIn("Connect an AI account", made["notes"][0])

    def test_a_drafter_that_fails_falls_back_and_says_it_didnt_answer(self):
        client, _ = drafter_with("nope")
        with mock.patch.object(agents, "get", lambda agent_id: Agent()):
            made = draft.draft("Open Settings", self.CONTEXT, client=client)
        self.assertEqual(made["drafted_by"], "rules")
        self.assertIn("didn't answer", made["notes"][0])

    def test_a_long_request_cut_to_twelve_steps_and_a_schedule_the_rules_cant_read_are_told(self):
        words = ". ".join(f"Open app number {n}" for n in range(20)) + "."
        made = draft.draft(words, self.CONTEXT)
        self.assertEqual(sum(1 for n in made["draft"]["graph"]["nodes"] if n["type"] == "phone.task"), 12)
        self.assertTrue(any("first 12 steps" in note for note in made["notes"]))
        arabic = draft.draft("كل يوم الساعة 9 افتح الإعدادات", self.CONTEXT)
        chinese = draft.draft("每天早上9点打开设置", self.CONTEXT)
        for made in (arabic, chinese):
            self.assertEqual(made["drafted_by"], "rules")
            self.assertFalse(any(n["type"] == "trigger.schedule" for n in made["draft"]["graph"]["nodes"]))
            self.assertIn("schedules that aren't in English are not read", made["notes"][0])


if __name__ == "__main__":
    unittest.main()
