"""Smart on Claude: frontier.AnthropicChat over a scripted HTTP layer, and the keys, availability and errors
around it. Offline: no request leaves the process."""

import io
import json
import os
import threading
import unittest
import unittest.mock
import urllib.error

from mobile_agent import doctor, engines, frontier, hedge, usage
from mobile_agent.frontier import (AnthropicChat, AnthropicError, FrontierAgent, OpenAIChat, anthropic_schema,
                                   anthropic_thinking, chat_client, cost_usd, prompt_messages)
from mobile_agent.keys import Keys
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, screen

KEY = "sk-ant-" + "k" * 40
DECISION = {"thought": "t", "plan": None, "notes_add": [], "checklist_updates": [],
            "actions": [{"op": "DONE", "target": None, "text": None}], "answer": "Done."}


def reply(out, *, usage=None, stop="end_turn", thinking=True):
    content = ([{"type": "thinking", "thinking": "", "signature": "s"}] if thinking else []) + [
        {"type": "text", "text": json.dumps(out)}]
    return {"id": "msg_1", "type": "message", "role": "assistant", "content": content, "stop_reason": stop,
            "usage": usage or {"input_tokens": 1000, "output_tokens": 50, "cache_read_input_tokens": 3000,
                               "cache_creation_input_tokens": 0}}


class Response:
    def __init__(self, status, body, headers=None):
        self.status, self.raw, self.headers = status, body if isinstance(body, bytes) else json.dumps(body).encode(), headers or {}

    def read(self):
        return self.raw

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)


class Server:
    """Scripted Messages API: each request pops the next answer (a Response, an exception, or a callable of the
    request body that returns one). Records every request and each connection it came on."""

    def __init__(self, *answers, default=None):
        self.answers, self.requests, self.connections, self.default = list(answers), [], [], default
        self.lock = threading.Lock()

    def connect(self, timeout):
        connection = Connection(self)
        self.connections.append(connection)
        return connection


class Connection:
    def __init__(self, server):
        self.server, self.sock, self.timeout, self.closed, self.pending = server, None, None, False, None

    def request(self, method, path, body, headers):
        with self.server.lock:
            self.server.requests.append({"method": method, "path": path, "body": json.loads(body), "headers": headers,
                                         "connection": self})
            answer = self.server.answers.pop(0) if self.server.answers else self.server.default
        if isinstance(answer, BaseException):
            raise answer
        self.pending = answer

    def getresponse(self):
        answer, self.pending = self.pending, None
        if callable(answer):
            answer = answer(self.server.requests[-1]["body"])
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        self.closed = True


def client(server, model="claude-sonnet-5-5", reasoning="low", hedger=None):
    chat = AnthropicChat(model, key=KEY, reasoning=reasoning, hedger=hedger)
    chat._connect = server.connect
    return chat


MESSAGES = prompt_messages("Archive it.", "- Mail: com.example.mail", "0 of 50", "Mail", "", [], [], None,
                           ['e1 Button "Archive" @1,1,1,1'],
                           {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD", "detail": "low"}})
SCHEMA = frontier._schema({"com.example.mail": "Mail"}, frontier.OPERATIONS)


class RequestTests(unittest.TestCase):
    def test_a_turn_becomes_a_messages_request_with_two_cache_breakpoints_and_the_screenshot(self):
        body = AnthropicChat("claude-sonnet-5-5", key=KEY).body(MESSAGES, SCHEMA)
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        self.assertEqual(body["system"], [{"type": "text", "text": frontier.SYSTEM, "cache_control": {"type": "ephemeral"}}])
        (turn,) = body["messages"]
        stable, rest, image = turn["content"]
        self.assertEqual(turn["role"], "user")
        self.assertEqual(stable["cache_control"], {"type": "ephemeral"})
        self.assertTrue(stable["text"].startswith("Request: Archive it."))
        self.assertNotIn("cache_control", rest)
        self.assertEqual(image, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}})
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(body["output_config"]["effort"], "low")
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)  # Sonnet 5.5 and Opus 5.5 refuse forced tool use
        self.assertNotIn("temperature", body)

    def test_the_answer_schema_fits_the_grammar(self):
        schema = AnthropicChat("claude-sonnet-5-5", key=KEY).body(MESSAGES, SCHEMA)["output_config"]["format"]["schema"]
        text = json.dumps(schema)
        self.assertNotIn("maxItems", text)
        self.assertNotIn('"pattern"', text)
        self.assertEqual(schema["properties"]["actions"]["minItems"], 1)
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["properties"]["actions"]["items"]["additionalProperties"])

    def test_the_schema_is_closed_and_cut_to_what_structured_outputs_take(self):
        user = {"type": "object", "properties": {
            "pattern": {"type": "string", "maxLength": 5, "pattern": "^a+$", "format": "email"},
            "count": {"type": "integer", "minimum": 0},
            "tags": {"type": "array", "minItems": 3, "maxItems": 9, "uniqueItems": True, "items": {"type": "string"}},
            "when": {"type": "string", "format": "date"},
            "odd": {"type": "string", "format": "phone"},
            "either": {"oneOf": [{"type": "object", "properties": {"a": {"type": "string"}}}, {"type": "null"}]}},
            "required": ["count"], "additionalProperties": True}
        self.assertEqual(anthropic_schema(user), {"type": "object", "properties": {
            "pattern": {"type": "string", "format": "email"},
            "count": {"type": "integer"},
            "tags": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "when": {"type": "string", "format": "date"},
            "odd": {"type": "string"},
            "either": {"anyOf": [{"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False},
                                 {"type": "null"}]}},
            "required": ["count"], "additionalProperties": False})
        self.assertEqual(anthropic_schema({"type": ["object", "null"]}), {"type": ["object", "null"],
                                                                          "additionalProperties": False})

    def test_reasoning_maps_to_adaptive_thinking_and_none_to_the_least_each_model_takes(self):
        self.assertEqual(anthropic_thinking("claude-sonnet-5-5", "low"), ({"type": "adaptive"}, "low"))
        self.assertEqual(anthropic_thinking("claude-opus-5-5", "medium"), ({"type": "adaptive"}, "medium"))
        self.assertEqual(anthropic_thinking("claude-sonnet-5-5", "minimal"), ({"type": "adaptive"}, "low"))
        # Sonnet 5.5 refuses "disabled": between_tools turns off up-front thinking.
        self.assertEqual(anthropic_thinking("claude-sonnet-5-5", "none"), ({"type": "between_tools"}, "low"))
        # Opus 5.5 and Fable think always: the lowest effort instead.
        self.assertEqual(anthropic_thinking("claude-opus-5-5", "none"), (None, "low"))
        self.assertEqual(anthropic_thinking("claude-fable-5-1", "none"), (None, "low"))
        self.assertEqual(anthropic_thinking("claude-sonnet-5", "none"), ({"type": "disabled"}, None))
        self.assertEqual(anthropic_thinking("claude-haiku-4-5", "low"), (None, None))
        body = AnthropicChat("claude-opus-5-5", key=KEY, reasoning="none").body(MESSAGES, SCHEMA)
        self.assertNotIn("thinking", body)
        self.assertEqual(body["output_config"]["effort"], "low")

    def test_claude_models_route_to_anthropic_on_their_own_key(self):
        with unittest.mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY}, clear=True):
            self.assertIsInstance(chat_client("claude-sonnet-5-5"), AnthropicChat)
            self.assertEqual(chat_client("claude-sonnet-5-5").key, KEY)
            with self.assertRaises(ValueError):
                chat_client("gpt-5.6-sol")
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(chat_client("claude-opus-5-5", key="other-key-0000000000").key, "other-key-0000000000")
            with self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
                chat_client("claude-sonnet-5-5")
            self.assertIsInstance(chat_client("gpt-5.6-sol", key="sk-" + "o" * 30), OpenAIChat)

    def test_cache_hits_and_writes_are_priced_at_claudes_rates(self):
        usage = {"prompt_tokens": 5000, "cached_tokens": 3000, "cache_write_tokens": 1000, "completion_tokens": 200}
        self.assertAlmostEqual(cost_usd("claude-sonnet-5-5", usage), (1000 * 2 + 3000 * .2 + 1000 * 2.5 + 200 * 10) / 1e6)
        self.assertAlmostEqual(cost_usd("claude-opus-5-5", usage), (1000 * 4 + 3000 * .2 + 1000 * 5 + 200 * 20) / 1e6)


@unittest.mock.patch.dict(os.environ, {"MOBSTER_HEDGE": "0"})
class ExchangeTests(unittest.TestCase):
    def test_a_reply_is_its_json_and_its_usage_in_openai_terms(self):
        server = Server(Response(200, reply(DECISION)), Response(200, reply(DECISION, thinking=False)))
        chat = client(server)
        out, used = chat.complete(MESSAGES, SCHEMA, timeout=5)
        self.assertEqual(out, DECISION)
        self.assertEqual(used, {"prompt_tokens": 4000, "completion_tokens": 50, "cached_tokens": 3000,
                                "cache_write_tokens": 0})
        chat.complete(MESSAGES, SCHEMA, timeout=5)
        self.assertEqual(chat.usage["calls"], 2)
        self.assertEqual(chat.usage["prompt_tokens"], 8000)
        first, second = server.requests
        self.assertEqual((first["method"], first["path"]), ("POST", "/v1/messages"))
        self.assertEqual(first["headers"]["x-api-key"], KEY)
        self.assertEqual(first["headers"]["anthropic-version"], frontier.ANTHROPIC_VERSION)
        self.assertNotIn("Authorization", first["headers"])
        self.assertIs(first["connection"], second["connection"])  # kept alive
        self.assertIs(chat.connection, first["connection"])

    def test_a_dropped_kept_alive_connection_is_reopened_once(self):
        server = Server(ConnectionResetError("peer closed"), Response(200, reply(DECISION)))
        out, _ = client(server).complete(MESSAGES, SCHEMA, timeout=5)
        self.assertEqual(out, DECISION)
        self.assertEqual(len(server.connections), 2)
        self.assertTrue(server.connections[0].closed)

    def test_an_error_keeps_its_status_and_body_for_the_app(self):
        body = {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}}
        with self.assertRaises(AnthropicError) as raised:
            client(Server(Response(400, body))).complete(MESSAGES, SCHEMA, timeout=5)
        text = str(raised.exception)
        self.assertTrue(text.startswith("HTTP 400; Anthropic HTTP 400:"))
        self.assertFalse(hedge.transient(raised.exception))
        self.assertTrue(engines.out_of_credit(text))
        self.assertEqual(engines.plain_error(text), engines.NO_ANTHROPIC_CREDIT)

    def test_overload_and_named_waits_are_waited_out_and_a_spend_cap_is_not(self):
        with unittest.mock.patch.object(frontier.time, "sleep") as sleep:
            server = Server(Response(529, {"type": "error"}), Response(429, {}, {"retry-after": "2"}),
                            Response(200, reply(DECISION)))
            self.assertEqual(client(server).complete(MESSAGES, SCHEMA, timeout=30)[0], DECISION)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2.0])
            with self.assertRaisesRegex(AnthropicError, "HTTP 429"):
                client(Server(Response(429, {"error": {"type": "rate_limit_error"}}))).complete(MESSAGES, SCHEMA, timeout=30)

    def test_a_refusal_a_cut_reply_and_prose_raise(self):
        for data, words in ((reply(DECISION, stop="refusal") | {"stop_details": {"category": "cyber"}}, "refusal: cyber"),
                            (reply(DECISION, stop="max_tokens"), "max_tokens"),
                            ({"content": [{"type": "text", "text": "Sure!"}], "stop_reason": "end_turn"}, "not JSON")):
            with self.assertRaisesRegex(RuntimeError, words):
                AnthropicChat.answer(data)
        self.assertEqual(engines.plain_error("Anthropic declined the request (refusal: cyber)"),
                         "Claude declined this request. Try rewording it, or run it on another model.")


class HedgeTests(unittest.TestCase):
    def test_a_transient_failure_is_resent_on_a_fresh_connection(self):
        server = Server(Response(500, {"type": "error"}), Response(200, reply(DECISION)))
        chat = client(server, hedger=hedge.Hedger(any_floor=5, any_default=5))
        with unittest.mock.patch.dict(os.environ, {"MOBSTER_HEDGE": "1"}):
            self.assertEqual(chat.complete(MESSAGES, SCHEMA, timeout=10)[0], DECISION)
        first, second = server.requests
        self.assertIsNot(first["connection"], second["connection"])
        self.assertEqual(chat.usage["calls"], 1)

    def test_a_slow_call_gets_a_twin_and_the_loser_still_bills(self):
        release, done = threading.Event(), threading.Event()

        def slow(body):
            release.wait(5)
            return Response(200, reply(DECISION, usage={"input_tokens": 700, "output_tokens": 70}))
        server = Server(slow, Response(200, reply(DECISION)))
        chat = client(server, hedger=hedge.Hedger(any_floor=.05, any_default=.05, budget=hedge.HedgeBudget(ratio=1)))
        original = chat._count

        def count(used, hedge=False):
            original(used, hedge=hedge)
            if hedge:
                done.set()
        chat._count = count
        with unittest.mock.patch.dict(os.environ, {"MOBSTER_HEDGE": "1"}):
            out, used = chat.complete(MESSAGES, SCHEMA, timeout=10)
        self.assertEqual(out, DECISION)
        self.assertEqual(used["prompt_tokens"], 4000)  # the twin's answer
        self.assertEqual(server.requests[0]["body"], server.requests[1]["body"])  # the same question, twice
        release.set()
        self.assertTrue(done.wait(5))
        self.assertEqual((chat.usage["calls"], chat.usage["hedges"]), (1, 1))
        self.assertEqual(chat.usage["prompt_tokens"], 4700)
        self.assertEqual(chat.usage["completion_tokens"], 120)


class LoopTests(unittest.TestCase):
    def test_the_frontier_loop_runs_on_claude(self):
        def answer(body):
            properties = body["output_config"]["format"]["schema"]["properties"]
            if "items" in properties:  # the task contract, at the least thinking Sonnet 5.5 takes
                self.assertEqual(body["thinking"], {"type": "between_tools"})
                return Response(200, reply({"items": []}))
            self.assertEqual(body["thinking"], {"type": "adaptive"})
            tapped = len(driver.actions) > 0
            return Response(200, reply({"thought": "", "plan": None, "notes_add": [], "checklist_updates": [],
                                        "actions": [{"op": "DONE" if tapped else "TAP", "target": None if tapped else "e1",
                                                     "text": None}], "answer": "Archived." if tapped else None}))
        server = Server(default=answer)
        driver = Driver([screen(Element("1", "Archive", "Button", (.1, .1, .2, .05))),
                         screen(Element("1", "Archived", "StaticText", (.1, .1, .2, .05)))])
        with unittest.mock.patch.dict(os.environ, {"MOBSTER_HEDGE": "0"}):
            result = FrontierAgent(driver, client(server), screenshots=False, settle_seconds=0).run("Archive the receipt.")
        self.assertEqual(driver.actions, [("TAP", "Archive", None)])
        self.assertEqual((result["status"], result["answer"]), ("completed", "Archived."))
        self.assertEqual(result["usage"]["calls"], len(server.requests))
        self.assertAlmostEqual(result["cost_usd"], cost_usd("claude-sonnet-5-5", result["usage"]))


class StructureTests(unittest.TestCase):
    def test_a_users_schema_is_filled_on_claude_without_reasoning(self):
        seen = []

        def answer(body):
            seen.append(body)
            return Response(200, reply({"data": {"total": "$28.00"}}))
        chat = client(Server(answer), reasoning="low")
        schema = {"type": "object", "required": ["total"], "properties": {"total": {"type": "string", "maxLength": 20}}}
        with unittest.mock.patch.dict(os.environ, {"MOBSTER_HEDGE": "0"}):
            data, problem = engines.structure(chat, "What was the total?", "It was $28.00.", [], schema)
        self.assertEqual((data, problem), ({"total": "$28.00"}, None))
        self.assertEqual(seen[0]["thinking"], {"type": "between_tools"})
        self.assertNotIn("maxLength", json.dumps(seen[0]["output_config"]))
        self.assertEqual(chat.reasoning, "low")

    def test_calls_are_metered_as_anthropic_and_the_usage_ledger_keeps_them(self):
        events = []
        chat = engines.meter(client(Server(Response(200, reply(DECISION)))), events.append)
        with unittest.mock.patch.dict(os.environ, {"MOBSTER_HEDGE": "0"}):
            chat.complete(MESSAGES, SCHEMA, timeout=5)
        finished = events[-1]
        self.assertEqual((finished["provider"], finished["model"]), ("anthropic", "claude-sonnet-5-5"))
        self.assertAlmostEqual(finished["estimated_usd"], cost_usd("claude-sonnet-5-5", {
            "prompt_tokens": 4000, "cached_tokens": 3000, "completion_tokens": 50}))
        self.assertIn("anthropic", usage.PROVIDERS)

    def test_a_losing_twin_is_metered_as_its_own_call(self):
        events = []
        chat = engines.meter(client(Server()), events.append)
        chat._count({"prompt_tokens": 700, "completion_tokens": 70}, hedge=True)
        started, finished = events
        self.assertEqual((started["event"], finished["event"]), ("inference_started", "inference_finished"))
        self.assertEqual((finished["purpose"], finished["provider"], finished["call_id"]),
                         ("hedge", "anthropic", started["call_id"]))
        self.assertAlmostEqual(finished["estimated_usd"], cost_usd("claude-sonnet-5-5", {"prompt_tokens": 700,
                                                                                         "completion_tokens": 70}))
        self.assertEqual(chat.usage["hedges"], 1)
        chat.on_hedge_usage = lambda used: 1 / 0  # a late event the run refuses changes nothing
        chat._count({"prompt_tokens": 1}, hedge=True)
        self.assertEqual(chat.usage["hedges"], 2)


class SelectionTests(unittest.TestCase):
    OPENAI = "sk-" + "o" * 40

    def test_the_model_follows_the_setting_else_the_key_the_user_has(self):
        for env, model in (({}, "gpt-5.6-sol"),
                           ({"OPENAI_API_KEY": self.OPENAI}, "gpt-5.6-sol"),
                           ({"ANTHROPIC_API_KEY": KEY}, engines.ANTHROPIC_SMART_MODEL),
                           ({"OPENAI_API_KEY": self.OPENAI, "ANTHROPIC_API_KEY": KEY}, "gpt-5.6-sol"),
                           ({"OPENAI_API_KEY": self.OPENAI, "MOBSTER_SMART_MODEL": "claude-opus-5-5"}, "claude-opus-5-5"),
                           ({"ANTHROPIC_API_KEY": KEY, "MOBSTER_SMART_MODEL": "gpt-5.6-terra"}, "gpt-5.6-terra"),
                           ({"ANTHROPIC_API_KEY": KEY, "MOBSTER_SMART_MODEL": "bad model!"}, engines.ANTHROPIC_SMART_MODEL)):
            self.assertEqual(engines.smart_model(env), model, env)
        self.assertEqual(engines.smart_key({"OPENAI_API_KEY": self.OPENAI, "ANTHROPIC_API_KEY": KEY,
                                            "MOBSTER_SMART_MODEL": "claude-sonnet-5-5"}), KEY)
        self.assertIsNone(engines.smart_key({"OPENAI_API_KEY": self.OPENAI, "MOBSTER_SMART_MODEL": "claude-sonnet-5-5"}))
        # An Anthropic helper's key powers Smart on Claude, as an OpenAI helper's does on OpenAI.
        helper = {"MOBSTER_HELPER_PROVIDER": "anthropic", "TEXT_MODEL": "claude-haiku-4-5", "TEXT_MODEL_API_KEY": KEY}
        self.assertEqual((engines.smart_model(helper), engines.smart_key(helper)), (engines.ANTHROPIC_SMART_MODEL, KEY))
        config = engines.smart_config({"ANTHROPIC_API_KEY": KEY})
        self.assertEqual((config.model, config.max_steps, config.switches),
                         (engines.ANTHROPIC_SMART_MODEL, engines.SMART_CONFIG.max_steps, engines.SMART_CONFIG.switches))
        self.assertIs(engines.smart_config({}), engines.SMART_CONFIG)

    def test_an_anthropic_key_alone_builds_smart_on_claude_and_the_default_is_untouched(self):
        with unittest.mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY}, clear=True):
            built = engines.build_client(key=engines.smart_key())
            self.assertIsInstance(built, AnthropicChat)
            self.assertEqual((built.model, built.reasoning), (engines.ANTHROPIC_SMART_MODEL, "low"))
            self.assertEqual(engines.default_engine(), engines.SMART)
            estimate = engines.smart_estimate("Text Sam I'm late")
            self.assertEqual((estimate["model"], estimate["provider"]), (engines.ANTHROPIC_SMART_MODEL, "Anthropic"))
            sol = engines.smart_estimate("Text Sam I'm late", model="gpt-5.6-sol")
            self.assertAlmostEqual(estimate["highUsd"], round(sol["highUsd"] * engines.SMART_COST_RATIO[
                engines.ANTHROPIC_SMART_MODEL], 4), places=3)
        with unittest.mock.patch.dict(os.environ, {"OPENAI_API_KEY": self.OPENAI}, clear=True):
            self.assertIsInstance(engines.build_client(key=engines.smart_key()), OpenAIChat)
            self.assertEqual(engines.smart_estimate("Text Sam")["provider"], "OpenAI")

    def test_no_key_says_which_key_the_model_needs(self):
        self.assertEqual(engines.no_key_reason(True, "gpt-5.6-sol"), engines.NO_OPENAI_KEY)
        self.assertEqual(engines.no_key_reason(False, "gpt-5.6-sol"),
                         "Add OPENAI_API_KEY or ANTHROPIC_API_KEY to the agent's env file")
        self.assertIn("ANTHROPIC_API_KEY", engines.no_key_reason(True, "claude-opus-5-5"))

    def test_anthropic_errors_read_as_what_to_do(self):
        model = "claude-sonnet-5-5"
        self.assertEqual(engines.model_failure("HTTP 401; Anthropic HTTP 401: invalid x-api-key", model),
                         (False, "Claude didn't accept your key. Check it in Settings › AI account."))
        self.assertEqual(engines.model_failure('HTTP 404; Anthropic HTTP 404: {"message": "model: claude-x"}', model),
                         (False, f"Your Claude key can't use {model}."))
        self.assertEqual(engines.model_failure("HTTP 402; Anthropic HTTP 402: billing_error", model),
                         (False, engines.NO_ANTHROPIC_CREDIT))
        self.assertEqual(engines.model_failure("HTTP 429; Anthropic HTTP 429: rate", model), (None, None))
        self.assertEqual(engines.plain_error("HTTP 529; Anthropic HTTP 529: overloaded", model=model),
                         "Claude had a problem answering. Try again in a moment.")
        self.assertEqual(engines.plain_error("HTTP 429; Anthropic HTTP 429: rate", model=model),
                         "Claude is rate limiting your key. Wait a minute, then try again.")
        self.assertEqual(engines.plain_error("HTTP ConnectionResetError; Anthropic request outcome unknown", True, model),
                         engines.NO_ANTHROPIC_REACH)
        # OpenAI's wording is unchanged.
        self.assertEqual(engines.plain_error("OpenAI HTTP 429: slow down", model="gpt-5.6-sol"),
                         "OpenAI is rate limiting your key. Wait a minute, then try again.")
        self.assertEqual(engines.structure_note("csv", "RuntimeError: Anthropic HTTP 400: credit balance is too low"),
                         "Your Claude account ran out of credit before Mobster could turn this answer into CSV, "
                         "so it’s shown as text.")

    def test_reach_asks_anthropic_about_a_claude_model_and_remembers_an_empty_account(self):
        seen = []

        def opener(request, timeout=15):
            seen.append(request)
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        reach = engines.ModelReach(opener=opener)
        self.assertEqual(reach.probe(KEY, "claude-sonnet-5-5"),
                         (False, "Your Claude key can't use claude-sonnet-5-5."))
        self.assertEqual(seen[0].full_url, "https://api.anthropic.com/v1/models/claude-sonnet-5-5")
        self.assertEqual(seen[0].get_header("X-api-key"), KEY)
        self.assertIsNone(seen[0].get_header("Authorization"))
        reach.record(KEY, False, engines.NO_ANTHROPIC_CREDIT, model="claude-sonnet-5-5")
        self.assertEqual(reach.state(KEY, "claude-sonnet-5-5"), (False, engines.NO_ANTHROPIC_CREDIT))
        self.assertEqual(reach.state(KEY, "claude-opus-5-5"), (True, None))  # another model, not yet known


class KeyTests(unittest.TestCase):
    def test_an_anthropic_key_is_saved_shown_by_its_hint_and_removed(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            keys = Keys(None)
            state = keys.update({"anthropic": {"key": " " + KEY + " "}})
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], KEY)
            self.assertEqual(state["anthropic"], {"configured": True, "keyHint": "…kkkk", "fromHelper": False,
                                                  "model": engines.ANTHROPIC_SMART_MODEL})
            self.assertNotIn(KEY, json.dumps(state))
            with self.assertRaisesRegex(ValueError, "Claude key"):
                keys.update({"anthropic": {"key": "short"}})
            self.assertFalse(keys.update({"anthropic": None})["anthropic"]["configured"])

    def test_the_key_test_reads_the_model_then_generates_a_few_tokens(self):
        seen = []

        def opener(request, timeout=30):
            seen.append(request)
            return io.BytesIO(b"{}")
        with unittest.mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY}, clear=True):
            result = Keys(None).test("anthropic", opener=opener)
        self.assertTrue(result["ok"])
        self.assertIn("claude-sonnet-5-5", result["message"])
        read, generate = seen
        self.assertEqual(read.full_url, "https://api.anthropic.com/v1/models/claude-sonnet-5-5")
        body = json.loads(generate.data)
        self.assertEqual((body["max_tokens"], body["thinking"]), (16, {"type": "between_tools"}))

    def test_the_key_test_names_an_empty_account_and_a_rejected_key(self):
        def failing(status, body):
            def opener(request, timeout=30):
                raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(json.dumps(body).encode()))
            return opener
        credit = {"type": "error", "error": {"type": "invalid_request_error",
                                             "message": "Your credit balance is too low to access the Anthropic API."}}
        with unittest.mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY}, clear=True):
            empty = Keys(None).test("anthropic", opener=failing(400, credit))
            rejected = Keys(None).test("anthropic", opener=failing(401, {}))
        self.assertEqual((empty["ok"], empty["problem"]), (False, "no_credit"))
        self.assertFalse(rejected["ok"])
        self.assertIn("didn't accept this key", rejected["message"])
        self.assertEqual(rejected["problem"], "rejected")

    def test_doctor_counts_an_anthropic_key_for_smart(self):
        with unittest.mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY, "TYPESAFE_API_KEY": "j" * 20}, clear=True):
            check = doctor.check_key()
        self.assertEqual(check.state, doctor.OK)
        # Masked, with where it came from (P0-2 of the CLI polish brief): never the whole key.
        self.assertIn("Claude (sk-ant-…", check.detail)
        self.assertNotIn(KEY, check.detail)
        with unittest.mock.patch.dict(os.environ, {"MOBSTER_SMART_MODEL": "claude-opus-5-5", "OPENAI_API_KEY": "sk-" + "o" * 30},
                                      clear=True):
            check = doctor.check_key()
        self.assertEqual(check.state, doctor.FAIL)
        self.assertIn("no Claude key is saved", check.detail)
        self.assertEqual(check.fix, "mobster login")


if __name__ == "__main__":
    unittest.main()
