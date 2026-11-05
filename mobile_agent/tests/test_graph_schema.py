import copy
import json
import unittest

from mobile_agent.graph import chain, schema
from mobile_agent.graph.schema import Context, GraphError, validate
from mobile_agent.workflows import records as batch

PACKS = {"instagram": {"name": "Instagram", "readers": {"dms": [
    {"key": "handle", "label": "From", "type": "text"}, {"key": "unread", "label": "Unread", "type": "boolean"}]}}}


def node(ident, kind, config=None, **extra):
    return {"id": ident, "type": kind, "config": config or {}, "position": {"x": 0, "y": 0}, **extra}


def edge(ident, source, target, source_port="out", target_port="in", **extra):
    return {"id": ident, "source": source, "sourcePort": source_port, "target": target, "targetPort": target_port,
            **extra}


def graph(nodes, edges, **extra):
    return {"version": 2, "nodes": nodes, "edges": edges, **extra}


TASK = {"text": "Read the newest message from {who: Sam}", "appId": None}


def errors(value, context=None):
    with unittest.TestCase().assertRaises(GraphError) as caught:
        validate(value, context)
    return caught.exception.errors


class CatalogTests(unittest.TestCase):
    def test_every_type_is_described_for_the_canvas(self):
        listed = schema.catalog()
        json.dumps(listed)  # the canvas reads it as JSON
        kinds = {item["type"]: item for item in listed}
        self.assertTrue({"trigger.manual", "trigger.schedule", "trigger.webhook", "trigger.notification",
                         "trigger.shortcut", "trigger.app", "phone.task", "phone.taught", "phone.check", "app.read",
                         "app.action", "connector.call", "send", "brief", "transform", "ai.ask", "output", "branch",
                         "loop", "merge", "wait", "approval", "workflow.call"} <= set(kinds))
        self.assertEqual(kinds["phone.task"]["fields"]["text"]["instructs"], True)
        self.assertEqual(kinds["phone.task"]["fields"]["mode"]["choices"], ["auto", "replay", "agent"])
        self.assertIn({"name": "error", "schema": schema.ERROR_OUT}, kinds["phone.task"]["outputs"])
        self.assertEqual(kinds["trigger.manual"]["inputs"], [])
        self.assertEqual(kinds["loop"]["inputs"], [{"name": "in", "single": False}, {"name": "items", "single": True}])
        self.assertTrue(kinds["send"]["outward"] and kinds["phone.task"]["phone"])
        json.dumps(schema.public_limits())

class ValidGraphTests(unittest.TestCase):
    def test_a_free_graph_with_every_flow_kind(self):
        made = validate(graph([
            node("start", "trigger.manual"),
            node("hook", "trigger.webhook"),
            node("read", "app.read", {"app": "instagram", "reader": "dms", "n": 5}),
            node("fork", "branch", {"cases": [{"id": "many", "label": "Many", "when": "count($input.rows) > 3"}]}),
            node("each", "loop", {"over": "items", "concurrency": 2, "collect": "reply"}),
            node("reply", "phone.task", {"text": "Answer the DM in the input, kindly", "appId": "instagram",
                                         "device": "any", "input": "{= $item }"}, parent="each"),
            node("note", "transform", {"fields": {"count": "{= count($node.read.rows) }",
                                                  "line": "{= $inputs.who } has DMs"}}),
            node("join", "merge", {"mode": "all"}),
            node("ok", "approval", {"message": "Post {= count($input.items) } results?"}),
            node("tell", "send", {"to": "notification", "text": "{= json($input) }"}),
            node("oops", "send", {"to": "clipboard"}),
            node("done", "output", {"value": "{= $node.note }"}),
        ], [
            edge("e1", "start", "read"), edge("e2", "hook", "read"), edge("e3", "read", "fork"),
            edge("e4", "fork", "each", "many", "items", map="$input.rows"),
            edge("e5", "each", "reply", "item", "in"), edge("e6", "fork", "note", "else"),
            edge("e7", "each", "join"), edge("e8", "note", "join"), edge("e9", "join", "ok"),
            edge("e10", "ok", "tell", "approved"), edge("e11", "tell", "oops", "error"),
            edge("e12", "ok", "done", "approved"),
        ], inputs=[{"name": "who", "type": "string", "default": "Sam"}], vars={"channel": "C024"}),
            Context(packs=PACKS))
        configs = {n["id"]: n["config"] for n in made["nodes"]}
        self.assertEqual(configs["read"]["n"], 5)
        self.assertEqual(configs["each"]["max"], schema.MAX_ITERATIONS)        # defaults are filled in
        self.assertEqual(configs["reply"]["mode"], "auto")
        self.assertEqual(configs["fork"]["cases"][0]["when"], "count($input.rows) > 3")
        self.assertEqual(made["limits"], schema.DEFAULT_LIMITS)
        self.assertEqual(made["edges"][3]["map"], "$input.rows")

    def test_loops_over_rows_times_until_and_phones(self):
        validate(graph([
            node("start", "trigger.manual"),
            node("rows", "loop", {"over": "rows", "rows": [{"who": "Kate"}, {"who": "Sam"}]}),
            node("text", "phone.task", {"text": "Text {who} hello, as {= $item.who }"}, parent="rows"),
            node("phones", "loop", {"over": "phones", "phones": {"count": 3}}),
            node("check", "phone.check", {"question": "Is Instagram open?", "device": "loop"}, parent="phones"),
            node("until", "loop", {"over": "until", "until": "seen", "max": 5}),
            node("look", "phone.task", {"text": "Refresh the order page", "input": None}, parent="until"),
            node("seen", "phone.check", {"question": "Does it say Delivered?"}, parent="until"),
            node("wait", "wait", {"seconds": 60}),
        ], [edge("e1", "start", "rows"), edge("e2", "rows", "phones"), edge("e3", "phones", "until"),
            edge("e4", "look", "seen"), edge("e5", "until", "wait")]))

    def test_body_nodes_may_read_ancestors_of_their_loop(self):
        validate(graph([
            node("start", "trigger.manual"), node("read", "phone.task", {"text": "Read the list"}),
            node("each", "loop", {"over": "times", "times": 3}),
            node("use", "transform", {"fields": {"a": "{= $node.read.answer }", "i": "{= $index }"}}, parent="each"),
        ], [edge("e1", "start", "read"), edge("e2", "read", "each"), edge("e3", "read", "use")]))


class RefusalTests(unittest.TestCase):
    def test_every_problem_is_listed_with_where_it_is(self):
        found = errors(graph([
            node("start", "trigger.manual"), node("start2", "trigger.manual"),
            node("a", "phone.task", {"text": "Do it", "colour": "red"}),
            node("b", "nonsense"),
            node("c", "wait", {"seconds": 1}),
        ], [edge("e1", "start", "a", "nope"), edge("e2", "start", "start2")]))
        where = {(item.get("node"), item.get("edge"), item.get("field")) for item in found}
        self.assertIn(("a", None, "colour"), where)
        self.assertIn(("b", None, None), where)
        self.assertIn(("c", None, "seconds"), where)
        messages = " ".join(item["message"] for item in found)
        self.assertIn("one Manual trigger at most", messages)
        self.assertIn("no output called nope", messages)
        self.assertIn("Nothing runs into a trigger", messages)

    def test_structure_refusals(self):
        start = node("start", "trigger.manual")
        cases = {
            "no trigger": (graph([node("a", "phone.task", TASK)], []), "Add a trigger"),
            "cycle": (graph([start, node("a", "phone.task", TASK), node("b", "phone.task", TASK)],
                            [edge("e1", "start", "a"), edge("e2", "a", "b"), edge("e3", "b", "a")]), "in a circle"),
            "unreached": (graph([start, node("a", "phone.task", TASK)], []), "never runs"),
            "merge of one": (graph([start, node("m", "merge")], [edge("e1", "start", "m")]), "two or more"),
            "branch of two": (graph([start, node("t", "phone.task", TASK),
                                     node("b", "branch", {"cases": [{"id": "x", "label": "X", "when": "true"}]})],
                                    [edge("e1", "start", "t"), edge("e2", "start", "b"), edge("e3", "t", "b")]),
                              "one edge"),
            "two results": (graph([start, node("o1", "output"), node("o2", "output")],
                                  [edge("e1", "start", "o1"), edge("e2", "start", "o2")]), "one Result"),
            "fan-out": (graph([start] + [node(f"t{i}", "phone.task", TASK) for i in range(11)],
                              [edge(f"e{i}", "start", f"t{i}") for i in range(11)]), "at most 10"),
            "two into items": (graph([start, node("l", "loop", {"over": "items"}), node("t", "phone.task", TASK)],
                                     [edge("e1", "start", "l", "out", "items"), edge("e2", "start", "t"),
                                      edge("e3", "t", "l", "out", "items")]), "takes one edge into items"),
            "items not a list": (graph([start, node("t", "phone.check", {"question": "Open?"}),
                                        node("l", "loop", {"over": "items"})],
                                       [edge("e1", "start", "t"), edge("e2", "t", "l", "out", "items",
                                                                       map="$input.observed")]), "items is a list"),
            "too many nodes": (graph([start] + [node(f"w{i}", "wait", {"seconds": 5}) for i in range(100)], []),
                               "at most 100 nodes"),
        }
        for name, (value, message) in cases.items():
            with self.subTest(name):
                self.assertIn(message, " ".join(item["message"] for item in errors(value)))

    def test_loop_scope_refusals(self):
        start = node("start", "trigger.manual")
        loop = node("l", "loop", {"over": "times", "times": 2})
        inner = node("i", "phone.task", TASK, parent="l")
        outer = node("o", "phone.task", TASK)
        cases = {
            "item outside": ([edge("e1", "start", "l"), edge("e2", "l", "o", "item")], "goes only to steps inside"),
            "out inside": ([edge("e1", "start", "l"), edge("e2", "l", "i", "out")], "leave through its out port"),
            "body out": ([edge("e1", "start", "l"), edge("e2", "i", "o")], "through the loop's collect"),
        }
        for name, (edges, message) in cases.items():
            with self.subTest(name):
                self.assertIn(message,
                              " ".join(e["message"] for e in errors(graph([start, loop, inner, outer], edges))))
        deep = [start, node("l1", "loop", {"over": "times", "times": 2}),
                node("l2", "loop", {"over": "times", "times": 2}, parent="l1"),
                node("l3", "loop", {"over": "times", "times": 2}, parent="l2")]
        self.assertIn("at most 2 deep",
                      " ".join(e["message"] for e in errors(graph(deep, [edge("e1", "start", "l1")]))))
        collect = graph([start, node("l", "loop", {"over": "times", "times": 2, "collect": "o"}), outer],
                        [edge("e1", "start", "l"), edge("e2", "l", "o")])
        self.assertIn("collect is a step inside it", " ".join(e["message"] for e in errors(collect)))
        phones = graph([start, node("t", "phone.task", {**TASK, "device": "loop"})], [edge("e1", "start", "t")])
        self.assertIn("inside a loop over phones", " ".join(e["message"] for e in errors(phones)))

    def test_expressions_are_checked_where_they_sit(self):
        start = node("start", "trigger.manual")
        cases = {
            "parse": ("{= 1 + }", "ends too soon"),
            "unknown node": ("{= $node.later.answer }", "isn't a step that runs before"),
            "type": ("{= $node.a.status - 1 }", "needs two numbers"),
            "item outside a loop": ("{= $item }", "only inside a loop"),
        }
        for name, (value, message) in cases.items():
            with self.subTest(name):
                found = errors(graph([start, node("a", "phone.task", TASK), node("t", "transform", {"fields": {
                    "x": value}})], [edge("e1", "start", "a"), edge("e2", "a", "t")]))
                self.assertEqual(found[0]["node"], "t")
                self.assertIn(message, found[0]["message"])
        branch = graph([start, node("b", "branch", {"cases": [{"id": "x", "label": "X", "when": "'yes'"}]})],
                       [edge("e1", "start", "b")])
        self.assertIn("needs true or false", errors(branch)[0]["message"])

    def test_screen_text_never_becomes_instructions(self):
        start = node("start", "trigger.manual")
        read = node("a", "phone.task", TASK)
        for text in ("Reply to {= $node.a.answer }", "Reply to {= $input.answer }"):
            with self.subTest(text):
                found = errors(graph([start, read, node("b", "phone.task", {"text": text})],
                                     [edge("e1", "start", "a"), edge("e2", "a", "b")]))
                self.assertIn("Screen text can't become a step's instructions", found[0]["message"])
                self.assertEqual((found[0]["node"], found[0]["field"]), ("b", "text"))
        validate(graph([start, read, node("b", "phone.task", {"text": "Reply to the person in the input",
                                                              "input": "{= $node.a.answer }"})],
                       [edge("e1", "start", "a"), edge("e2", "a", "b")]))
        items = graph([start, read, node("l", "loop", {"over": "items"}),
                       node("t", "phone.task", {"text": "Message {= $item.handle }"}, parent="l")],
                      [edge("e1", "start", "a"), edge("e2", "a", "l", "out", "items", map="[$input]")])
        self.assertIn("Screen text can't", errors(items)[0]["message"])
        validate(graph([start, node("l", "loop", {"over": "rows", "rows": [{"who": "Kate"}]}),
                        node("t", "phone.task", {"text": "Message {= $item.who } at {= $vars.time }"}, parent="l")],
                       [edge("e1", "start", "l")], vars={"time": "9"}))
        call = graph([start, read, node("c", "workflow.call", {"workflowId": "0123456789ab",
                                                               "values": {"who": "{= $node.a.answer }"}})],
                     [edge("e1", "start", "a"), edge("e2", "a", "c")])
        self.assertIn("Screen text can't", errors(call)[0]["message"])

    def test_retry_only_where_nothing_may_act_outward_by_its_nature(self):
        start = node("start", "trigger.manual")
        validate(graph([start, node("r", "phone.check", {"question": "Open?"}, retry={"tries": 3})],
                       [edge("e1", "start", "r")]))
        for kind, config in (("send", {"to": "clipboard"}), ("approval", {"message": "OK?"}),
                             ("workflow.call", {"workflowId": "0123456789ab"})):
            with self.subTest(kind):
                found = errors(graph([start, node("x", kind, config, retry={"tries": 2})], [edge("e1", "start", "x")]))
                self.assertIn("never retries it", found[0]["message"])
        self.assertIn("retry is", errors(graph([start, node("r", "wait", {"seconds": 5}, retry={"tries": 9})],
                                               [edge("e1", "start", "r")]))[0]["message"])

    def test_context_checks(self):
        start = node("start", "trigger.manual")
        records = {"aaaaaaaaaaaa": {"id": "aaaaaaaaaaaa", "name": "Two steps", "graph": chain.from_chain(
            [{"id": "a", "kind": "task", "text": "One", "appId": None}, {"id": "b", "kind": "wait", "seconds": 5}])},
                   "bbbbbbbbbbbb": {"id": "bbbbbbbbbbbb", "name": "Calls me", "graph": graph([start, node(
                       "c", "workflow.call", {"workflowId": "cccccccccccc"})], [])}}
        context = Context(workflow_id="cccccccccccc", workflows=records, destination=lambda ident: False,
                          packs=PACKS, plain=chain.record_is_plain)
        cases = {
            "taught many steps": (node("t", "phone.taught", {"workflowId": "aaaaaaaaaaaa"}), "has several steps"),
            "taught deleted": (node("t", "phone.taught", {"workflowId": "dddddddddddd"}), "was deleted"),
            "taught self": (node("t", "phone.taught", {"workflowId": "cccccccccccc"}), "can't repeat itself"),
            "call cycle": (node("t", "workflow.call", {"workflowId": "bbbbbbbbbbbb"}), "in a circle"),
            "call self": (node("t", "workflow.call", {"workflowId": "cccccccccccc"}), "can't run itself"),
            "destination gone": (node("t", "send", {"to": "destination", "destination": "eeeeeeeeeeee"}), "removed"),
            "reader": (node("t", "app.read", {"app": "instagram", "reader": "reels"}), "can't read reels"),
            "pack": (node("t", "app.read", {"app": "tiktok", "reader": "dms"}), "No app pack"),
            "action": (node("t", "app.action", {"app": "instagram", "action": "like"}), "No app lists an action"),
        }
        for name, (made, message) in cases.items():
            with self.subTest(name):
                found = errors(graph([start, made], [edge("e1", "start", "t")]), context)
                self.assertIn(message, " ".join(e["message"] for e in found))

    def test_pins_fit_their_ports(self):
        start = node("start", "trigger.manual")
        good = node("c", "phone.check", {"question": "Open?"}, pin={"out": {"observed": True, "runId": "x"}})
        validate(graph([start, good], [edge("e1", "start", "c")]))
        for pin, message in (({"out": "yes"}, "should be an object"), ({"nope": 1}, "no output called nope"),
                             ({"out": {"x": "y" * 70_000}}, "at most 64 KB")):
            with self.subTest(pin=str(pin)[:30]):
                found = errors(graph([start, {**good, "pin": pin}], [edge("e1", "start", "c")]))
                self.assertIn(message, " ".join(e["message"] for e in found))


CHAINS = [
    ([{"id": "s1", "kind": "task", "text": "Read iOS version", "appId": "settings"}], None),
    ([{"id": "a", "kind": "task", "text": "Open Messages and read {who: Sam}", "appId": None},
      {"id": "b", "kind": "taught", "workflowId": "0123456789ab"},
      {"id": "c", "kind": "wait", "seconds": 600},
      {"id": "d", "kind": "send", "to": "destination", "destination": "abcdefabcdef", "what": "last"},
      {"id": "e", "kind": "brief", "title": "Morning"},
      {"id": "f", "kind": "brief"},
      {"id": "start", "kind": "send", "to": "clipboard", "what": "all"}], None),
    ([{"id": "a", "kind": "task", "text": "Post it", "appId": "instagram"}], {"kind": "times", "times": 3}),
    ([{"id": "a", "kind": "task", "text": "Text {name} hi", "appId": None}],
     {"kind": "each", "rows": [{"name": "Kate"}, {"name": "Sam"}], "max": 2}),
    ([{"id": "a", "kind": "task", "text": "Text {name} hi", "appId": None}],
     {"kind": "each", "rows": None, "max": 200}),
    ([{"id": "a", "kind": "task", "text": "Refresh the page", "appId": None},
      {"id": "until", "kind": "wait", "seconds": 30}], {"kind": "until", "check": "It says Delivered", "max": 10}),
]


class ChainTests(unittest.TestCase):
    def test_every_chain_round_trips_with_no_loss(self):
        for limits, cron, zone in ((schema.DEFAULT_LIMITS, None, None),
                                   ({"maxSpendUsd": 2.5, "maxMinutes": 30}, "0 9 * * *", "Europe/London")):
            for steps, repeat in CHAINS:
                with self.subTest(steps=[s["kind"] for s in steps], repeat=repeat and repeat["kind"], cron=cron):
                    made = chain.from_chain(copy.deepcopy(steps), copy.deepcopy(repeat), limits, cron, zone)
                    self.assertEqual(chain.as_chain(made), (steps, repeat, limits, cron, zone))
                    saved = validate(made, Context(destination=lambda ident: True))
                    self.assertEqual(chain.as_chain(saved), (steps, repeat, limits, cron, zone))
                    self.assertEqual(chain.words(saved), batch.summary_of(steps, repeat))
                    self.assertEqual(chain.is_plain(saved), repeat is None and len(steps) == 1)
                    # The triggers side by side in the first row, then the chain down the column under them.
                    triggers = [n["position"] for n in saved["nodes"] if n["type"].startswith("trigger.")]
                    self.assertEqual(triggers, [{"x": chain.SIDE * i, "y": 0} for i in range(len(triggers))])
                    steps = [n for n in saved["nodes"] if "parent" not in n and not n["type"].startswith("trigger.")]
                    top = [n["position"]["y"] for n in steps]
                    self.assertEqual(top, [chain.PITCH * (i + 1) for i in range(len(top))])

    def test_what_a_chain_cant_say(self):
        base = validate(chain.from_chain([{"id": "a", "kind": "task", "text": "Do it", "appId": None}]))
        self.assertIsNotNone(chain.as_chain(base))
        task = base["nodes"][1]
        variants = {
            "an input": {**base,
                         "nodes": [base["nodes"][0], {**task, "config": {**task["config"], "input": "{= $input }"}}]},
            "expect": {**base, "nodes": [base["nodes"][0], {**task, "config": {**task["config"], "expect": "Sent"}}]},
            "a pin": {**base, "nodes": [base["nodes"][0], {**task, "pin": {"out": {}}}]},
            "a retry": {**base, "nodes": [base["nodes"][0], {**task, "retry": {"tries": 2, "backoffSeconds": 5}}]},
            "inputs": {**base, "inputs": [{"name": "who", "type": "string"}]},
            "a webhook": {**base, "nodes": base["nodes"] + [node("hook", "trigger.webhook")],
                          "edges": base["edges"] + [edge("e9", "hook", "a")]},
            "a map": {**base, "edges": [{**base["edges"][0], "map": "$input"}]},
        }
        for name, value in variants.items():
            with self.subTest(name):
                self.assertIsNone(chain.as_chain(value))
                self.assertFalse(chain.is_plain(value))
        schedule = validate(chain.from_chain([{"id": "a", "kind": "task", "text": "Do it", "appId": None}], None, None,
                                             "0 9 * * *", "UTC"))
        late = copy.deepcopy(schedule)
        late["nodes"][1]["config"]["missed"] = "run_once"
        self.assertIsNotNone(chain.as_chain(schedule))
        self.assertIsNone(chain.as_chain(late))

    def test_record_is_plain_reads_the_graph_first(self):
        plain = {"graph": chain.from_chain([{"id": "a", "kind": "task", "text": "One", "appId": None}])}
        self.assertTrue(chain.record_is_plain(plain))
        free = {"graph": graph([node("start", "trigger.manual"), node("a", "phone.task", TASK),
                                node("b", "phone.task", TASK)], [edge("e1", "start", "a"), edge("e2", "start", "b")]),
                "steps": None, "repeat": None}
        self.assertFalse(chain.record_is_plain(free))
        self.assertTrue(chain.record_is_plain({"steps": [{"id": "a", "kind": "task", "text": "x"}], "repeat": None}))

    def test_free_graph_words_follow_run_order(self):
        made = graph([node("start", "trigger.manual"), node("b", "phone.task", {"text": "Second"}),
                      node("a", "phone.task", {"text": "First"}), node("c", "phone.check", {"question": "Done?"})],
                     [edge("e1", "start", "a"), edge("e2", "a", "b"), edge("e3", "b", "c")])
        self.assertEqual(chain.words(validate(made)), "First\nSecond\nDone?")


if __name__ == "__main__":
    unittest.main()
