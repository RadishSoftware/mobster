import unittest

from mobile_agent.graph import functions
from mobile_agent.graph.expr import ExprError, compile, evaluate, render, template
from mobile_agent.graph.infer import RUN_SCHEMA, infer
from mobile_agent.workflows import starters

SCOPE = {
    "input": {"answer": "Sam said hi", "data": {"count": 3, "items": [{"name": "Kate", "age": 31},
                                                                       {"name": "Sam", "age": 28}]}},
    "inputs": {"who": "Sam", "n": 2},
    "vars": {"channel": "C024"},
    "node": {"read": {"rows": [{"handle": "@a", "unread": True}, {"handle": "@b", "unread": False}], "title": "DMs"}},
    "run": {"id": "abc", "trigger": "manual", "startedAt": 1_760_000_000_000, "test": False},
}


def ev(source, scope=SCOPE, **options):
    return evaluate(source, scope, **options)


class GrammarTests(unittest.TestCase):
    def test_literals_and_operators(self):
        cases = {
            "1 + 2 * 3": 7, "(1 + 2) * 3": 9, "7 / 2": 3.5, "6 / 3": 2, "7 % 4": 3, "-2 - -3": 1,
            "'a' + \"b\"": "ab", "[1, 2] + [3]": [1, 2, 3], '{"a": 1, "b": [true, null]}': {"a": 1, "b": [True, None]},
            "1 == 1.0": True, "true == 1": False, "[1, {\"a\": 2}] == [1, {\"a\": 2}]": True, "'b' > 'a'": True,
            "2 in [1, 2]": True, "'x' in {\"x\": 1}": True, "'ell' in 'hello'": True,
            "null ?? 'fallback'": "fallback", "0 ?? 5": 0, "not (1 < 2) or true and false": False,
            "'tab\\there'": "tab\there", "'\\u00e9'": "é",
        }
        for source, wanted in cases.items():
            with self.subTest(source=source):
                self.assertEqual(ev(source), wanted)

    def test_roots_and_reading(self):
        self.assertEqual(ev("$input.data.count"), 3)
        self.assertEqual(ev("$input.data.items[1].name"), "Sam")
        self.assertEqual(ev("$input.data.items[-1].age"), 28)
        self.assertEqual(ev("$input['data']['count']"), 3)
        self.assertEqual(ev("$inputs.who + ' / ' + $vars.channel"), "Sam / C024")
        self.assertEqual(ev("$node.read.title"), "DMs")
        self.assertEqual(ev('$node["read"].rows[0].handle'), "@a")
        self.assertEqual(ev("$run.trigger"), "manual")
        self.assertIsNone(ev("$input.nothing.deeper[3]"))
        self.assertIsNone(ev("$input.data.items[9]"))

    def test_what_is_refused_while_parsing(self):
        bad = {
            "": "Write an expression", "1 +": "ends too soon", "$foo": "isn't a root", "open()": "isn't a function",
            "len(1, 2)": "takes 1", "1 < 2 < 3": "Chain comparisons", "$node": "Name the node",
            "$node[1]": "in quotes", "{a: 1}": "Expected lit", "'\\q'": "Unknown escape", "a.b": "isn't a function",
            "1 ; 2": "don't use", "__import__('os')": "isn't a function", "$input.__class__": None,
        }
        for source, message in bad.items():
            with self.subTest(source=source):
                if message is None:
                    self.assertIsNone(ev(source))   # a key like any other on JSON: no attribute access exists
                    continue
                with self.assertRaisesRegex(ExprError, message):
                    compile(source)

    def test_errors_fail_closed(self):
        bad = {
            "$input.answer > 3": "compares two numbers or two texts", "null > 3": "compares", "1 / 0": "divide by zero",
            "'a' and true": "needs true or false", "not 'x'": "needs true or false", "'a' - 1": "needs a number",
            "1 + 'a'": "adds two numbers", "$input.data.items.name": "pluck", "$input.data.count.x": "Can't read",
            "$item": "only inside a loop", "$node.later": "hasn't run", "if('yes', 1, 2)": "needs true or false",
            "5 in 5": "looks inside", "[1][true]": "whole number",
        }
        for source, message in bad.items():
            with self.subTest(source=source):
                with self.assertRaisesRegex(ExprError, message):
                    ev(source)

    def test_and_or_and_if_only_evaluate_what_they_need(self):
        self.assertFalse(ev("false and 1 / 0 > 1"))
        self.assertTrue(ev("true or 1 / 0 > 1"))
        self.assertEqual(ev("if(true, 'yes', 1 / 0)"), "yes")
        self.assertEqual(ev("$input.missing ?? 'none'"), "none")


class FunctionTests(unittest.TestCase):
    def test_every_function(self):
        items = "$input.data.items"
        cases = {
            "lower('AbC')": "abc", "upper('abc')": "ABC", "trim('  x ')": "x", "len('abc')": 3, "len([1, 2])": 2,
            "len({\"a\": 1})": 1, "contains('Hello', 'ELL')": True, "contains([1, 2], 2)": True,
            "starts_with('Hello', 'he')": True, "ends_with('Hello', 'LO')": True,
            "replace('a-b-c', '-', '+')": "a+b+c", "split('a,b', ',')": ["a", "b"],
            "join(['a', 1, true], '/')": "a/1/true",
            "slice('hello', 1, 3)": "el", "slice([1, 2, 3], -2)": [2, 3], "like('Report 7.pdf', 'report*.PDF')": True,
            "string(3.0)": "3", "string(2.5)": "2.5", "string(null)": "", "string([1])": "[1]",
            "json({\"a\": [1]})": '{"a":[1]}', "parse_json('{\"a\": 2}')": {"a": 2}, "number(' 42 ')": 42,
            "number('2.5')": 2.5, "round(2.5)": 3, "round(-2.5)": -3, "round(1.2345, 2)": 1.23, "floor(2.7)": 2,
            "ceil(2.1)": 3, "abs(-4)": 4, "min([3, 1, 2])": 1, "max(3, 9)": 9, "min([])": None, "sum([1, 2.5])": 3.5,
            "avg([1, 2])": 1.5, "avg([])": None, "count([1, 2])": 2, "first([7, 8])": 7, "last([7, 8])": 8,
            "first([])": None, f"pluck({items}, 'name')": ["Kate", "Sam"],
            f"pluck(where({items}, 'age', '>', 30), 'name')": ["Kate"],
            f"pluck(sort({items}, 'age'), 'name')": ["Sam", "Kate"],
            "sort([3, null, 1])": [1, 3, None], "reverse([1, 2])": [2, 1], "reverse('ab')": "ba",
            "unique([1, 1.0, 2, [1], [1]])": [1, 2, [1]], "flatten([[1], 2, [3, [4]]])": [1, 2, 3, [4]],
            f"where({items}, 'name', 'contains', 'AT')": [{"name": "Kate", "age": 31}], "range(3)": [0, 1, 2],
            "range(2, 4)": [2, 3], 'keys({"a": 1})': ["a"], 'values({"a": 1})': [1],
            f"get({items}, '[0].name')": "Kate", "get($input, 'nope', 'x')": "x", "default('', 'x')": "x",
            "default('y', 'x')": "y", "empty([])": True, "empty(0)": False, "exists(null)": False, "bool('Yes')": True,
            "days(1)": 86_400_000, "hours(2)": 7_200_000, "minutes(1)": 60_000,
            "date(0, 'YYYY-MM-DD HH:mm')": "1970-01-01 00:00", "date(0, 'D MMM [at] H')": "1 Jan at 0",
            "date(0, 'HH:mm', 'Asia/Tokyo')": "09:00",
        }
        self.assertEqual(set(functions.FUNCTIONS) - {"if"}, {source.split("(")[0] for source in cases} | set())
        for source, wanted in cases.items():
            with self.subTest(source=source):
                self.assertEqual(ev(source), wanted)

    def test_date_uses_the_workflows_zone(self):
        self.assertEqual(ev("date(0, 'HH')", timezone="America/New_York"), "19")
        with self.assertRaisesRegex(ExprError, "time zone"):
            ev("date(0, 'HH', 'Mars/Base')")

    def test_function_refusals(self):
        bad = {
            "lower(1)": "needs text", "split('a', '')": "separator", "number('x')": "can't read",
            "parse_json('NaN')": "needs JSON", "sum(['a'])": "items needs a number", "sort([1, 'a'])": "not a mix",
            "range(20000)": "at most 10,000", "round(1, 11)": "0 to 10", "bool('maybe')": "can't read",
            "where([1], 'x', 'like', 1)": "test is one of", "like('a', '" + "*" * 201 + "')": "at most 200",
        }
        for source, message in bad.items():
            with self.subTest(source=source):
                with self.assertRaisesRegex(ExprError, message):
                    ev(source)


class BoundTests(unittest.TestCase):
    def test_source_and_size_bounds(self):
        with self.assertRaisesRegex(ExprError, "at most 2,000 characters"):
            compile("1" + " + 1" * 600)
        with self.assertRaisesRegex(ExprError, "at most 500 parts"):
            compile("+".join(["1"] * 400))
        with self.assertRaisesRegex(ExprError, "nests at most 32"):
            compile("(" * 40 + "1" + ")" * 40)

    def test_step_and_result_bounds(self):
        with self.assertRaisesRegex(ExprError, "more than 20,000 steps"):
            ev("count(sort(range(10000))) + count(range(10))")
        self.assertEqual(ev("count(sort(range(5000)))"), 5000)
        with self.assertRaisesRegex(ExprError, "more than 1 MB|too long"):
            ev("join(split(slice(json(range(2000)), 1, -1), ','), '" + "x" * 600 + "')")
        with self.assertRaisesRegex(ExprError, "at most 10,000 items"):
            ev("range(9000) + range(2000)")


class TemplateTests(unittest.TestCase):
    def test_whole_value_keeps_its_type(self):
        self.assertEqual(render("{= $input.data.count }", SCOPE), 3)
        self.assertEqual(render("  {= $node.read.rows }  ", SCOPE), SCOPE["node"]["read"]["rows"])
        self.assertEqual(render("{= 1 } items", SCOPE), "1 items")

    def test_expressions_and_blanks_together(self):
        text = "Text {who: Kate} that {= count($node.read.rows) } DMs wait, about {topic}"
        self.assertEqual(render(text, SCOPE, values={"Topic": "tea"}), "Text Kate that 2 DMs wait, about tea")
        self.assertEqual(render(text, SCOPE, values={"who": "Sam", "topic": "x"}), "Text Sam that 2 DMs wait, about x")
        with self.assertRaises(starters.MissingBlanks):
            render(text, SCOPE)
        self.assertEqual([b[1] for b in template('{= "hi {name}" } {who}').blanks], ["who"])

    def test_old_text_means_what_it_meant(self):
        self.assertEqual(render(starters.escape("Use {curly} braces"), SCOPE), "Use {curly} braces")
        self.assertEqual(render("Say {{= hi }}", SCOPE), "Say {= hi }")
        self.assertEqual(starters.fill("Plain {= 1 + 1 }"), "Plain {= 1 + 1 }")

    def test_objects_in_expressions_and_nested_config(self):
        self.assertEqual(render('{= {"a": {"b": 1}} }', SCOPE), {"a": {"b": 1}})
        config = {"channel": "{= $vars.channel }", "blocks": [{"text": "n={= $inputs.n }"}], "limit": 5}
        self.assertEqual(render(config, SCOPE), {"channel": "C024", "blocks": [{"text": "n=2"}], "limit": 5})

    def test_template_errors_point_at_the_text(self):
        with self.assertRaisesRegex(ExprError, "no closing"):
            template("Hello {= $inputs.who")
        with self.assertRaises(ExprError) as caught:
            template("Hello {= $nope }")
        self.assertEqual(caught.exception.at, 9)


class TypingTests(unittest.TestCase):
    ENV = {
        "input": {"type": "object", "additionalProperties": False, "properties": {
            "answer": {"type": ["string", "null"]}, "title": {"type": "string"},
            "data": {"type": "object", "properties": {"count": {"type": "integer"}}},
            "rows": {"type": "array", "items": {"type": "object"}}}},
        "inputs": {"type": "object", "properties": {"n": {"type": "number"}, "who": {"type": "string"}}},
        "vars": {"type": "object"}, "node": {"read": {"type": "object", "properties": {"rows": {"type": "array"}}}},
        "run": RUN_SCHEMA,
    }

    def test_operations_that_can_only_fail_are_refused(self):
        bad = {
            "$input.title > 3": "compares", "$input.data.count + 'x'": "adds",
            "not $input.title": "takes true or false",
            "$input.rows - 1": "needs two numbers", "$input.data.count.x": "from a number",
            "$item": "only inside a loop",
            "$node.later.x": "isn't a step that runs before", "$input.nothing > 1": "compares",
            "-$input.title": "needs a number",
        }
        for source, message in bad.items():
            with self.subTest(source=source):
                with self.assertRaisesRegex(ExprError, message):
                    infer(source, self.ENV)

    def test_unknown_types_pass(self):
        for source in ("$vars.x > 3", "$input.answer + 'x'", "$node.read.rows[0].y == 2", "first($input.rows).z"):
            with self.subTest(source=source):
                infer(source, self.ENV)


if __name__ == "__main__":
    unittest.main()
