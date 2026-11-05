import os
import threading
import unittest

from mobile_agent import answer_shapes
from mobile_agent import result_blocks as rb
from mobile_agent.agent_hooks import MASK
from mobile_agent.run import Run
from mobile_agent.tests.test_server_engine import MESSAGES, Base

QUEUE = rb.review_queue([
    {"id": "c1", "bucket": "reply", "to": "Sam Lee", "handle": "@samlee", "app": "Messages",
     "bundleId": "com.apple.MobileSMS", "context": "Asked about Friday", "lastMessage": "still on for fri?",
     "draft": "Yes, Friday at 8 works for me.", "frameId": "f12"},
    {"id": "c2", "bucket": "reply", "to": "Ana", "app": "Messages", "draft": "Friday at 8 [works / doesn't work]"},
    {"id": "c3", "bucket": "spam", "to": "Prize Desk", "app": "Messages", "context": "You won a cruise"},
], title="Review queue: 3 DMs")


def envelope(*blocks):
    value, problems = rb.validate(list(blocks))
    assert not problems, problems
    return value


class SchemaTests(unittest.TestCase):
    def test_every_type_validates_and_gets_an_id(self):
        value, problems = rb.validate({"v": 1, "blocks": [
            QUEUE,
            rb.table(["Creator", {"label": "Likes", "type": "number"}], [["@nasa", "1,200"], ["@esa", 800]]),
            rb.media_list([{"title": "Typed captions", "frameId": "f20", "sounds": ["Espresso"], "at": 12.5}]),
            rb.evidence("The tab bar overlaps the home indicator.", frame_id="f7",
                        crop={"x": 0, "y": .88, "w": 1, "h": .12}, verdict="fail"),
            rb.checklist([{"label": "Login screen", "status": "failed", "expected": "Sign in", "observed": "Cut off"}]),
            {"type": "markdown", "text": "## Themes\n\n- one"},
            rb.chart(["A", "B"], [{"label": "Reels", "values": [8, 5]}]),
        ]})
        self.assertEqual(problems, [])
        self.assertEqual([b["type"] for b in value["blocks"]], list(rb.TYPES[:7]))
        self.assertEqual(value["v"], 1)
        ids = [b["id"] for b in value["blocks"]]
        self.assertEqual(len(set(ids)), len(ids))
        table = value["blocks"][1]
        self.assertEqual(table["rows"][0]["cells"], {"creator": "@nasa", "likes": 1200})
        self.assertEqual(value["blocks"][4]["items"][0]["status"], "fail")

    def test_a_block_that_does_not_fit_falls_back_to_its_text(self):
        value, problems = rb.validate([{"type": "table", "title": "Prices", "columns": [], "rows": [["Oat milk"]]},
                                       {"type": "hologram", "id": 3}])
        self.assertEqual(len(problems), 2)
        self.assertEqual(len(value["blocks"]), 1)  # the hologram had nothing to say
        fallback = value["blocks"][0]
        self.assertEqual(fallback["type"], "markdown")
        self.assertTrue(fallback["fallback"])
        self.assertIn("Oat milk", fallback["text"])
        self.assertIn("Prices", fallback["text"])

    def test_nothing_usable_is_none_and_other_versions_are_refused(self):
        self.assertEqual(rb.validate(None), (None, []))
        self.assertIsNone(rb.validate("<b>hi</b>")[0])
        self.assertIsNone(rb.validate({"v": 2, "blocks": [{"type": "markdown", "text": "x"}]})[0])

    def test_secrets_are_masked_and_only_https_links_survive(self):
        value = envelope(
            rb.markdown("The wifi password is hunter2!"),
            rb.table([{"label": "Link", "type": "link"}], [["javascript:alert(1)"], ["https://example.com/a"]]),
            rb.media_list([{"title": "x", "link": "http://example.com"}]))
        self.assertNotIn("hunter2", value["blocks"][0]["text"])
        self.assertIn(MASK, value["blocks"][0]["text"])
        cells = [row["cells"]["link"] for row in value["blocks"][1]["rows"]]
        self.assertEqual(cells[1], "https://example.com/a")
        self.assertNotEqual(cells[0], "https://example.com/a")
        self.assertNotIn("link", value["blocks"][2]["items"][0])

    def test_a_brand_cell_is_plain_text_of_at_most_80_characters(self):
        value = envelope(rb.table([{"label": "Company", "type": "brand"}, {"label": "Other", "type": "nonsense"}],
                                  [["Google", 1], ["<b>Meta</b>\nPlatforms " + "x" * 100, 2], [None, 3], [42, 4]]))
        table = value["blocks"][0]
        self.assertEqual(table["columns"][0]["type"], "brand")
        self.assertEqual(table["columns"][1]["type"], "text")
        cells = [row["cells"]["company"] for row in table["rows"]]
        self.assertEqual(cells[0], "Google")
        self.assertLessEqual(len(cells[1]), 80)
        self.assertNotIn("\n", cells[1])
        self.assertIsNone(cells[2])
        self.assertEqual(cells[3], "42")
        masked = envelope(rb.table([{"label": "Company", "type": "brand"}], [["sk-ant-abcdefghijklmnop1234"]]))
        self.assertNotIn("abcdefghijklmnop", masked["blocks"][0]["rows"][0]["cells"]["company"])

    def test_frames_are_ids_never_paths(self):
        value = envelope(rb.evidence("x", frame_id="../../etc/passwd", quote="seen"))
        self.assertNotIn("frameId", value["blocks"][0])

    def test_limits(self):
        value, problems = rb.validate([rb.markdown(str(n)) for n in range(12)])
        self.assertEqual(len(value["blocks"]), rb.MAX_BLOCKS)
        self.assertTrue(problems)
        big = envelope(rb.table(["n"], [[n] for n in range(500)]))["blocks"][0]
        self.assertEqual(len(big["rows"]), rb.MAX_ROWS)
        self.assertEqual(big["truncated"], 500)

    def test_review_queue_needs_a_recipient(self):
        value, problems = rb.validate([rb.review_queue([{"draft": "hi"}])])
        self.assertTrue(problems)
        self.assertEqual(value["blocks"][0]["type"], "markdown")


class ExportTests(unittest.TestCase):
    def test_table_csv_guards_formulas(self):
        block = envelope(rb.table(["Name", {"label": "Total", "type": "money"}],
                                  [["=HYPERLINK(1)", 3.5], ["Oat, milk", None]]))["blocks"][0]
        self.assertEqual(rb.table_csv(block), "Name,Total\n'=HYPERLINK(1),3.5\n\"Oat, milk\",\n")

    def test_markdown_of_every_block(self):
        value = envelope(QUEUE, rb.table(["a", "b"], [["x|y", 1]], title="T"),
                         rb.checklist([{"label": "Home", "status": "pass"}]))
        text = rb.to_markdown(value)
        self.assertIn("**Sam Lee** (Messages)", text)
        self.assertIn("Nothing here was sent.", text)
        self.assertIn("## T", text)
        self.assertIn("x\\|y", text)
        self.assertIn("- Pass: Home", text)


class ReportTests(unittest.TestCase):
    def test_an_inbox_sweeps_drafts_become_a_review_queue(self):
        report = {"title": "Inbox", "drafts": [{"id": "n1", "to": "Sam", "app": "Instagram", "lastMessage": "yo",
                                                 "draft": "Hey Sam!"}, {"id": "n2", "to": None, "draft": "x"}]}
        block = rb.from_report(report)["blocks"][0]
        self.assertEqual(block["type"], "review_queue")
        self.assertEqual([i["to"] for i in block["items"]], ["Sam"])
        self.assertEqual(block["items"][0]["bucket"], "reply")
        self.assertIsNone(rb.from_report({"title": "Nothing"}))


def commit(text="Yes, Friday at 8 works for me.", recipient="Sam Lee", seen=True, app="Messages", act="send_message"):
    return {"kind": "commit", "operation": "TAP", "label": "Send", "text": text, "title": f"Send to {recipient}?",
            "act": act, "app_name": app, "recipient": recipient, "recipient_seen": seen, "step": 3}


class PreapprovalTests(unittest.TestCase):
    def run_with_spec(self):
        block, item, words = rb.approval_item(envelope(QUEUE), "queue", "c1", None)
        run = Run({"id": "messages", "name": "Messages"}, "goal", "live")
        run.preapproved = [rb.preapproval(block, item, words)]
        return run

    def ask(self, run, request):
        answer = {}
        thread = threading.Thread(target=lambda: answer.update(value=run.request_approval(request, timeout=5)))
        thread.start()
        for _ in range(200):
            if run.approval is not None or "value" in answer:
                break
            thread.join(0.01)
        if run.approval is not None:
            run.stop.set()
            thread.join(2)
            return "asked"
        thread.join(2)
        return answer["value"]

    def test_the_approved_send_goes_through_once_and_is_journaled(self):
        run = self.run_with_spec()
        self.assertEqual(self.ask(run, commit()), "approved")
        requested = [e for e in run.events if e["event"] == "approval_requested"]
        resolved = [e for e in run.events if e["event"] == "approval_resolved"]
        self.assertEqual(requested[-1]["preapproved"], "review_queue")
        self.assertEqual(resolved[-1]["decision"], "approved")
        self.assertEqual(resolved[-1]["via"], "review_queue")
        self.assertEqual(resolved[-1]["source"], {"block": "queue", "item": "c1"})
        self.assertEqual(self.ask(run, commit()), "asked")

    def test_anything_else_asks(self):
        for request in (commit(text="Yes, Friday at 9 works for me."), commit(recipient="Sam Leigh"),
                        commit(seen=False), commit(app="WhatsApp"), commit(act="follow"),
                        {**commit(), "kind": "action"}, {**commit(), "recipient": None}):
            with self.subTest(request=request):
                self.assertEqual(self.ask(self.run_with_spec(), request), "asked")

    def test_a_stopped_run_never_uses_it(self):
        run = self.run_with_spec()
        run.stop.set()
        self.assertEqual(run.request_approval(commit(), timeout=1), "stopped")
        self.assertFalse(run.preapproved[0]["used"])


class YourWordsTests(unittest.TestCase):

    @staticmethod
    def stored(**item):
        row = {"id": "w1", "to": "Sam Lee", "app": "Messages", "bundleId": "com.apple.MobileSMS", **item}
        value, problems = rb.validate([rb.review_queue([row], id="queue")])
        assert not problems, problems
        return value

    def test_code_marks_an_item_with_no_draft_and_the_person_s_words_are_taken(self):
        value = self.stored(yourWords=rb.YOUR_WORDS)
        self.assertIs(value["blocks"][0]["items"][0]["yourWords"], True)
        _, _, words = rb.approval_item(value, "queue", "w1", "  Sorry, I got buried today. ")
        self.assertEqual(words, "Sorry, I got buried today.")

    def test_the_mark_goes_only_on_an_item_with_no_draft(self):
        value = self.stored(yourWords=rb.YOUR_WORDS, draft="Let me check.")
        self.assertNotIn("yourWords", value["blocks"][0]["items"][0])

    def test_nothing_that_json_can_say_makes_the_mark(self):
        for claim in (True, "yourWords", "true", 1, {"yourWords": True}, ["yourWords"]):
            with self.subTest(claim=claim):
                value = self.stored(yourWords=claim)
                self.assertNotIn("yourWords", value["blocks"][0]["items"][0])
                with self.assertRaises(rb.BlockError):
                    rb.approval_item(value, "queue", "w1", "Sorry, I got buried today.")
        again, _ = rb.validate(self.stored(yourWords=rb.YOUR_WORDS))
        self.assertNotIn("yourWords", again["blocks"][0]["items"][0])

    def test_a_marked_item_still_refuses_words_it_could_not_send(self):
        value = self.stored(yourWords=rb.YOUR_WORDS)
        for text in (None, "", "   ", "\n\t", "\x07\x08", "free on [day]?", "my password is hunter2!", "x" * 4001):
            with self.subTest(text=text), self.assertRaises(rb.BlockError):
                rb.approval_item(value, "queue", "w1", text)
        with self.assertRaises(rb.BlockError):
            rb.approval_item(value, "queue", "w1", 5)

    def test_an_item_with_no_draft_and_no_mark_refuses_typed_words(self):
        with self.assertRaises(rb.BlockError) as refused:
            rb.approval_item(envelope(QUEUE), "queue", "c3",
                             "Hi there, want to chat?")   # an outreach item code left bare
        self.assertIn("no draft", str(refused.exception))


class ApproveTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-000000000000"  # a fake key: nothing calls a model here

    def finished(self, runtime, blocks=None):
        run = runtime.create(MESSAGES["id"], "Sort my DMs", "live")
        run.finish({"event": "result", "status": "completed", "answer": "3 DMs.",
                    "blocks": blocks if blocks is not None else envelope(QUEUE)})
        if run.lease:
            run.lease.close()
            run.lease = None
        runtime.active_runs.clear()
        return run

    def decide(self, runtime, run, item, body):
        return self.request(runtime, "POST", f"/api/runs/{run.id}/blocks/queue/items/{item}", body)

    def test_finish_validates_and_falls_back(self):
        runtime = self.runtime()
        run = self.finished(runtime, blocks=[{"type": "markdown", "text": "ok"}, {"type": "table", "rows": "x",
                                                                                  "title": "Broken"}])
        blocks = run.summary["blocks"]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["markdown", "markdown"])
        self.assertTrue(blocks[1]["fallback"])
        self.assertTrue(run.summary["blocks_problems"])
        stored = self.request(runtime, "GET", f"/api/runs/{run.id}")[1]["run"]["summary"]
        self.assertEqual(stored["blocks"], run.summary["blocks"])

    def test_approve_starts_a_task_that_sends_exactly_the_item_and_sends_nothing_itself(self):
        runtime = self.runtime()
        run = self.finished(runtime)
        before = set(runtime.runs)
        status, body, _ = self.decide(runtime, run, "c1", {"decision": "approve",
                                                           "text": "Yes, Friday at 8 works for me!"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "approved")
        follow = runtime.runs[body["runId"]]
        self.assertEqual(set(runtime.runs) - before, {follow.id})
        self.assertIn("“Yes, Friday at 8 works for me!”", follow.goal)
        self.assertIn("Sam Lee", follow.goal)
        self.assertEqual(follow.app["id"], MESSAGES["id"])
        self.assertEqual(follow.preapproved[0]["text"], "Yes, Friday at 8 works for me!")
        self.assertEqual(follow.preapproved[0]["to"], "Sam Lee")
        self.assertFalse(any(e["event"] in ("action_acknowledged", "approval_resolved") for e in follow.events))
        self.assertEqual(follow.status, "queued")
        self.assertEqual(run.block_state["queue/c1"]["runId"], follow.id)
        saved = {item["id"]: item for item in runtime.journal.load()}[run.id]
        self.assertEqual(saved["blockState"]["queue/c1"]["state"], "approved")

    def test_approving_twice_never_starts_a_second_send(self):
        runtime = self.runtime()
        run = self.finished(runtime)
        first = self.decide(runtime, run, "c1", {"decision": "approve"})[1]
        count = len(runtime.runs)
        status, again, _ = self.decide(runtime, run, "c1", {"decision": "approve", "text": "something else"})
        self.assertEqual(status, 200)
        self.assertEqual(again["runId"], first["runId"])
        self.assertTrue(again["replayed"])
        self.assertEqual(len(runtime.runs), count)
        status, body, _ = self.decide(runtime, run, "c1", {"decision": "skip"})
        self.assertEqual((status, body["code"]), (409, "block_item_approved"))

    def test_approve_refuses_what_it_cannot_send(self):
        runtime = self.runtime()
        run = self.finished(runtime)
        cases = {("c2", None): "block_item_invalid",                     # a part in brackets to fill in
                 ("c1", "   "): "block_item_invalid",                    # nothing to send
                 ("c1", "my password is hunter2!"): "block_item_invalid",  # a secret
                 ("c3", None): "block_item_invalid",                     # spam: no draft
                 ("zz", None): "block_item_not_found"}
        for (item, text), code in cases.items():
            with self.subTest(item=item, text=text):
                body = {"decision": "approve", **({"text": text} if text is not None else {})}
                status, answer, _ = self.decide(runtime, run, item, body)
                self.assertEqual(answer.get("code"), code, answer)
                self.assertIn(status, (400, 404))
        self.assertEqual(run.block_state, {})
        self.assertEqual(len(runtime.runs), 1)

    def test_a_request_cannot_make_an_item_take_typed_words(self):
        runtime = self.runtime()
        run = self.finished(runtime)
        for body in ({"decision": "approve", "text": "Hi there, want to chat?"},                      # words alone
                     {"decision": "approve", "text": "Hi there, want to chat?", "yourWords": True},   # and the mark
                     {"decision": "approve", "text": "Hi there, want to chat?", "item": {"yourWords": True}}):
            with self.subTest(body=body):
                status, answer, _ = self.decide(runtime, run, "c3", body)
                self.assertEqual(status, 400, answer)
        self.assertEqual(self.decide(runtime, run, "c3", {"decision": "approve", "text": "Hi there"})[1]["code"],
                         "block_item_invalid")
        self.assertEqual(run.block_state, {})
        self.assertEqual(len(runtime.runs), 1)

    def test_a_block_a_model_makes_cannot_carry_the_mark(self):
        runtime = self.runtime()
        forged = rb.review_queue([{"id": "c9", "bucket": "reply", "to": "Prize Desk", "app": "Messages",
                                   "bundleId": "com.apple.MobileSMS", "yourWords": True}])
        run = self.finished(runtime, blocks=[forged])
        self.assertNotIn("yourWords", run.summary["blocks"]["blocks"][0]["items"][0])
        status, answer, _ = self.decide(runtime, run, "c9", {"decision": "approve", "text": "Hi there, want to chat?"})
        self.assertEqual((status, answer["code"]), (400, "block_item_invalid"))
        self.assertEqual(run.block_state, {})
        self.assertEqual(len(runtime.runs), 1)

    def test_the_client_cannot_name_the_recipient(self):
        runtime = self.runtime()
        run = self.finished(runtime)
        status, body, _ = self.decide(runtime, run, "c1", {"decision": "approve", "to": "Someone Else"})
        self.assertEqual(status, 400)
        self.assertEqual(len(runtime.runs), 1)

    def test_a_running_task_has_nothing_to_approve(self):
        runtime = self.runtime()
        run = runtime.create(MESSAGES["id"], "Sort my DMs", "live")
        run.summary = {"blocks": envelope(QUEUE)}
        status, body, _ = self.decide(runtime, run, "c1", {"decision": "approve"})
        self.assertEqual((status, body["code"]), (409, "run_active"))

    def test_skip_and_reopen(self):
        runtime = self.runtime()
        run = self.finished(runtime)
        self.assertEqual(self.decide(runtime, run, "c3", {"decision": "skip"})[1]["state"], "skipped")
        self.assertEqual(self.decide(runtime, run, "c3", {"decision": "reopen"})[1]["state"], "open")
        self.assertEqual(run.block_state, {})
        status, body, _ = self.decide(runtime, run, "c3", {"decision": "reopen"})
        self.assertEqual((status, body["code"]), (409, "block_item_not_skipped"))


class McpTests(unittest.TestCase):
    def test_phone_task_answers_with_small_blocks_and_large_ones_as_markdown(self):
        from mobile_agent.threads.mcp import describe
        small = envelope(QUEUE)
        out = describe({"finishedAt": 1, "status": "completed", "summary": {"answer": "3 DMs.", "blocks": small}})
        self.assertEqual(out["blocks"], small)
        large = envelope(rb.table(["n", "words"], [[n, "word " * 40] for n in range(150)]))
        out = describe({"finishedAt": 1, "status": "completed", "summary": {"answer": "x", "blocks": large}})
        self.assertNotIn("blocks", out)
        self.assertTrue(out["blocks_markdown"].startswith("| n | words |"))


ANSWER = """I checked the 3 main screens of Firefox.

Screens:

| Screen | Price | Issue |
| --- | ---: | --- |
| Home | $4.99 | none |
| **Plans** | $49.99 | Annual price shows $4,999 |

## Checks
- [pass] Home: the tab bar shows 4 tabs
- [fail] Plans: the annual price reads $4,999
- [unsure] Settings: the toggle didn't change

Nothing else looked wrong."""


class AnswerTests(unittest.TestCase):
    def test_tables_and_check_lines_become_blocks_and_leave_the_rest(self):
        rest, blocks = answer_shapes.from_answer(ANSWER)
        self.assertEqual(rest, "I checked the 3 main screens of Firefox.\n\nNothing else looked wrong.")
        value = envelope(*blocks)
        table, checks = value["blocks"]
        self.assertEqual(table["title"], "Screens")
        self.assertEqual([c["type"] for c in table["columns"]], ["text", "money", "text"])
        self.assertEqual(table["rows"][1]["cells"],
                         {"screen": "Plans", "price": 49.99, "issue": "Annual price shows $4,999"})
        self.assertEqual(checks["title"], "Checks")
        self.assertEqual([(i["label"], i["status"]) for i in checks["items"]],
                         [("Home", "pass"), ("Plans", "fail"), ("Settings", "uncertain")])
        self.assertEqual(checks["items"][1]["observed"], "the annual price reads $4,999")

    def test_plain_answers_and_lone_lines_stay_text(self):
        for text in ("Your battery is at 81%.", "- [pass] just one check", "| a | b |\n| c | d |", None):
            self.assertEqual(answer_shapes.from_answer(text), (text, []))

    def test_a_table_too_wide_for_a_block_stays_text_and_the_rest_still_converts(self):
        wide = "\n".join("| " + " | ".join(row) + " |" for row in (
            [f"c{i}" for i in range(17)], ["---"] * 17, [str(i) for i in range(17)]))
        self.assertEqual(answer_shapes.from_answer(wide), (wide, []))
        rest, blocks = answer_shapes.from_answer(wide + "\n\n" + ANSWER)
        self.assertEqual([b["type"] for b in blocks], ["table", "checklist"])
        self.assertIn("| c0 |", rest)
        self.assertNotIn("| screen |", rest)

    def test_summarize_puts_an_answers_table_in_blocks(self):
        from types import SimpleNamespace

        from mobile_agent import smart_result
        agent = SimpleNamespace(client=SimpleNamespace(model="m", model_failed=False), _contract=None)
        summary = smart_result.summarize({"status": "completed", "answer": ANSWER, "steps": 3, "actions": 5,
                                          "elapsed": 9.0}, agent, None, request="Check each screen")
        self.assertEqual([b["type"] for b in summary["blocks"]["blocks"]], ["table", "checklist"])
        self.assertEqual(summary["data"], "I checked the 3 main screens of Firefox.\n\nNothing else looked wrong.")
        self.assertEqual(summary["full_answer"], ANSWER)
        self.assertTrue(summary["answer"].startswith("I checked the 3 main screens"))
        plain = smart_result.summarize({"status": "completed", "answer": "Battery is at 81%.", "steps": 1, "actions": 1,
                                        "elapsed": 2.0}, agent, None, request="Battery?")
        self.assertNotIn("blocks", plain)
        only = smart_result.summarize({"status": "completed", "answer": "| a | b |\n|---|---|\n| 1 | 2 |", "steps": 1,
                                       "actions": 1, "elapsed": 2.0}, agent, None, request="List them")
        self.assertEqual(only["answer"], "Here\u2019s what Mobster found.")
        self.assertIsNone(only["data"])


class ProducesTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path

        from mobile_agent.journal import Journal
        from mobile_agent.tests.test_workflows import FakeRuntime
        from mobile_agent.workflows.service import Workflows
        self.directory = tempfile.TemporaryDirectory(prefix="mobster-produces-")
        self.journal = Journal(Path(self.directory.name) / "runs.sqlite3")
        self.runtime = FakeRuntime(self.journal)
        stamp = datetime(2026, 10, 13, tzinfo=timezone.utc).timestamp()
        self.workflows = Workflows(self.runtime, lambda: stamp)
        self.runtime.workflows = self.workflows

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def test_a_workflow_keeps_what_it_produces(self):
        made = self.workflows.create({"name": "DMs", "appId": "settings", "goal": "Sort my DMs",
                                      "produces": ["review_queue"]})
        self.assertEqual(made["produces"], ["review_queue"])
        self.assertEqual(self.workflows.get(made["id"])["produces"], ["review_queue"])
        changed = self.workflows.update(made["id"], {"revision": 1, "produces": ["table", "chart"]})
        self.assertEqual(changed["produces"], ["table", "chart"])
        cleared = self.workflows.update(made["id"], {"revision": changed["revision"], "produces": None})
        self.assertNotIn("produces", cleared)
        for bad in (["hologram"], [], ["table", "table"], "table", ["a", "b", "c", "d"]):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "produces is 1 to 3"):
                self.workflows.create({"name": "x", "appId": "settings", "goal": "x", "produces": bad})

    def test_a_plugins_workflow_can_say_it(self):
        import json
        import tempfile
        from pathlib import Path

        from mobile_agent.plugins import manifest
        from mobile_agent.tests.test_plugins import write_plugin
        with tempfile.TemporaryDirectory() as root:
            folder = write_plugin(Path(root) / "p", mcp=False, skill=False)
            path = folder / "mobster" / "workflows" / "triage.json"
            path.write_text(json.dumps({**json.loads(path.read_text()), "produces": ["table"]}))
            plugin = manifest.read(folder)
            self.assertEqual(plugin.workflows[0]["produces"], ["table"])
            self.assertEqual(plugin.summary()["workflows"][0]["produces"], ["table"])
            path.write_text(json.dumps({**json.loads(path.read_text()), "produces": ["hologram"]}))
            with self.assertRaises(manifest.PluginError):
                manifest.read(folder)


class VisualBlockTests(unittest.TestCase):
    def test_the_new_types_validate(self):
        value, problems = rb.validate([
            rb.map_block([{"name": "Blue Bottle", "address": "66 Mint St"},
                          {"name": "Pier", "lat": "37.8", "lon": -122.4}],
                         near="San Francisco, CA"),
            rb.timeline([{"at": "9:00 AM", "title": "Leave", "state": "done"}, {"title": "Arrive", "status": "next"}]),
            rb.comparison([{"name": "A", "facts": {"Price": 10, "Seats": "4"}, "pros": ["Cheap"]},
                           {"name": "B", "facts": [{"label": "Price", "value": "$12"}], "cons": ["Loud"]}],
                          recommended="o1", verdict="A is cheaper."),
            rb.gallery([{"frameId": "f0123456789", "caption": "Home"}, {"frameId": "../etc/passwd"}]),
        ])
        self.assertEqual(problems, [])
        kinds = [b["type"] for b in value["blocks"]]
        self.assertEqual(kinds, ["map", "timeline", "comparison", "gallery"])
        places = value["blocks"][0]["places"]
        self.assertEqual(places[1], {"id": "p2", "name": "Pier", "lat": 37.8, "lon": -122.4})
        self.assertEqual(value["blocks"][0]["near"], "San Francisco, CA")
        self.assertEqual(value["blocks"][1]["items"][1]["state"], "next")
        compare = value["blocks"][2]
        self.assertEqual(compare["options"][0]["facts"],
                         [{"label": "Price", "value": "10"}, {"label": "Seats", "value": "4"}])
        self.assertEqual(compare["recommended"], "o1")
        self.assertEqual(len(value["blocks"][3]["items"]), 1)

    def test_a_place_needs_somewhere_and_a_comparison_two_options(self):
        _, problems = rb.validate([rb.map_block([{"name": "Nowhere"}]), rb.comparison([{"name": "Only"}])])
        self.assertEqual(len(problems), 2)
        _, problems = rb.validate([rb.map_block([{"name": "Bad", "lat": 91, "lon": 0}])])
        self.assertEqual(len(problems), 1)

    def test_every_chart_kind(self):
        for kind in ("bar", "line", "area", "pie", "sparkline", "column", "donut"):
            value, problems = rb.validate([rb.chart(["Mon", "Tue"], [{"label": "Steps", "values": [3, 4]},
                                                                     {"label": "Floors", "values": [1, None]}],
                                                    kind=kind, format="number", stacked=True, yLabel="Count")])
            self.assertEqual(problems, [], kind)
            block = value["blocks"][0]
            self.assertEqual(block["kind"], {"column": "bar", "donut": "pie"}.get(kind, kind))
            self.assertEqual(len(block["series"]), 1 if block["kind"] == "pie" else 2)
            self.assertEqual("stacked" in block, block["kind"] in ("bar", "area"))
        value, problems = rb.validate([rb.chart(None, [{"label": "Cafés", "points": [[1, 4.5],
                                                                                     {"x": 2, "y": 4.1, "label": "B"},
                                                                                     {"x": "no", "y": 1}]}],
                                                kind="scatter")])
        self.assertEqual(problems, [])
        self.assertEqual(value["blocks"][0]["series"][0]["points"],
                         [{"x": 1, "y": 4.5}, {"x": 2, "y": 4.1, "label": "B"}])
        _, problems = rb.validate([rb.chart(["A"], [{"values": [1]}], kind="radar")])
        self.assertEqual(problems, ["A chart is bar, line, area, pie, scatter or sparkline"])

    def test_a_table_keeps_its_view_and_chart_only_when_they_fit(self):
        columns = [{"label": "Month"}, {"label": "Revenue", "type": "money"}, {"label": "Notes"}]
        value, _ = rb.validate([rb.table(columns, [["Jan", 1, "x"], ["Feb", 2, "y"]]) | {
            "view": "chart", "chart": {"kind": "line", "x": "month", "y": ["revenue", "notes", "nope"]}}])
        table = value["blocks"][0]
        self.assertEqual(table["view"], "chart")
        self.assertEqual(table["chart"], {"kind": "line", "y": ["revenue"], "x": "month"})
        value, _ = rb.validate([rb.table(columns, [["Jan", 1, "x"]]) | {"view": "hologram", "chart": {"y": ["notes"]}}])
        self.assertNotIn("view", value["blocks"][0])
        self.assertNotIn("chart", value["blocks"][0])

    def test_markdown_of_the_new_types(self):
        value, _ = rb.validate([rb.timeline([{"at": "Oct 12", "title": "Ordered", "detail": "Paid $12"}]),
                                rb.comparison([{"name": "A", "facts": {"Price": "$1"}, "pros": ["Fast"]},
                                               {"name": "B", "facts": {"Price": "$2"}}], recommended="o2",
                                              verdict="B lasts.")])
        text = rb.to_markdown(value)
        self.assertIn("- Oct 12: **Ordered**. Paid $12", text)
        self.assertIn("| Price | $1 | $2 |", text)
        self.assertIn("| Pros | Fast |  |", text)
        self.assertIn("**Pick: B.** B lasts.", text)

    def test_more_series_and_points_than_a_chart_draws_are_cut(self):
        series = [{"label": f"S{i}", "values": [i] * 60} for i in range(8)]
        value, _ = rb.validate([rb.chart([str(i) for i in range(60)], series, kind="line")])
        self.assertEqual(len(value["blocks"][0]["series"]), rb.MAX_SERIES)
        _, problems = rb.validate([rb.chart([str(i) for i in range(61)], series[:1])])
        self.assertTrue(problems)


class AnswerViewTests(unittest.TestCase):

    TABLE = (
        "| Month | Revenue | Costs |\n|---|---|---|\n| 2026-01 | $10 | $4 |\n| 2026-02 | $12 | $5 |\n| 2026-03 | $9 | "
        "$5 |")

    def test_a_chart_request_opens_the_table_as_its_chart(self):
        _, blocks = answer_shapes.from_answer("Revenue by month:\n\n" + self.TABLE, "Chart my revenue by month")
        self.assertEqual(blocks[0]["view"], "chart")
        self.assertEqual(blocks[0]["chart"], {"kind": "line", "y": ["revenue", "costs"], "x": "month"})
        _, blocks = answer_shapes.from_answer(self.TABLE.replace("2026-0", "Store "), "Graph revenue for each store")
        self.assertEqual(blocks[0]["chart"]["kind"], "bar")
        _, blocks = answer_shapes.from_answer(self.TABLE.replace("2026-0", "Store "), "Pie chart of the revenue split")
        self.assertEqual(blocks[0]["chart"], {"kind": "pie", "y": ["revenue"], "x": "month"})
        _, blocks = answer_shapes.from_answer(self.TABLE, "scatter plot of revenue against costs")
        self.assertEqual(blocks[0]["chart"], {"kind": "scatter", "x": "revenue", "y": ["costs"]})

    def test_without_a_request_or_its_words_a_table_stays_a_table(self):
        for request in (None, "List my revenue by month"):
            _, blocks = answer_shapes.from_answer(self.TABLE, request)
            self.assertNotIn("view", blocks[0])
            self.assertNotIn("chart", blocks[0])

    def test_places_open_on_a_map_and_options_side_by_side(self):
        places = "| Name | Address |\n|---|---|\n| Blue Bottle | 66 Mint St |\n| Sightglass | 270 7th St |"
        _, blocks = answer_shapes.from_answer(places, "Find coffee near me")
        self.assertEqual(blocks[0]["view"], "map")
        _, blocks = answer_shapes.from_answer(places, "List my favourite cafés")
        self.assertNotIn("view", blocks[0])
        flights = "| Airline | Price | Stops |\n|---|---|---|\n| United | $320 | 0 |\n| Delta | $298 | 1 |"
        _, blocks = answer_shapes.from_answer(flights, "Compare these two flights")
        self.assertEqual(blocks[0]["view"], "compare")

    def test_three_dated_lines_are_a_timeline(self):
        text = ("Your day is set.\n\nToday:\n- 9:00 AM: Leave home. Take the 38 bus\n- **10:30 AM** — Meeting\n"
                "- 1 PM - Lunch with Sam\n\nHave fun.")
        rest, blocks = answer_shapes.from_answer(text)
        self.assertEqual(rest, "Your day is set.\n\nHave fun.")
        self.assertEqual(blocks[0]["type"], "timeline")
        self.assertEqual(blocks[0]["title"], "Today")
        self.assertEqual([i["at"] for i in blocks[0]["items"]], ["9:00 AM", "10:30 AM", "1 PM"])
        self.assertEqual(blocks[0]["items"][0], {"at": "9:00 AM", "title": "Leave home", "detail": "Take the 38 bus"})
        two = "- 9:00 AM: Leave\n- 10:00 AM: Arrive"
        self.assertEqual(answer_shapes.from_answer(two), (two, []))


if __name__ == "__main__":
    unittest.main()
