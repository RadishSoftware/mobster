import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from mobile_agent.ai_plans import compat_chat, device_login, providers, routes


class Isolated(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-providers-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        env = patch.dict(os.environ, {"HOME": str(self.root), "MOBSTER_VAULT": "file",
                                      "MOBSTER_DATA_DIR": str(self.root / "data")})
        env.start()
        self.addCleanup(env.stop)
        signins = patch.dict(routes._signins, clear=True)
        signins.start()
        self.addCleanup(signins.stop)
        for name in [n for p in providers.ALL.values() for n in p.env] + ["MOBSTER_SMART_PROVIDER",
                                                                          "MOBSTER_SMART_MODEL"]:
            os.environ.pop(name, None)


class RegistryTests(Isolated):
    def test_the_keychain_gets_the_secret_through_the_framework_never_argv(self):
        import base64

        from mobile_agent.ai_plans import vault
        from mobile_agent.tests.fake_keychain import FakeBridge
        bridge = FakeBridge()
        vault.KeychainVault(account="provider-grok", runner=lambda argv, **kw: bridge.runner(argv, kw.get("input")),
                            bridge=bridge).save({"access_token": "secret-token", "refresh_token": "secret-refresh"})
        self.assertEqual(bridge.cli, [])
        item = bridge.items[(vault.SERVICE, "provider-grok")]
        self.assertTrue(item["marked"])
        self.assertEqual(json.loads(base64.b64decode(item["data"]))["refresh_token"], "secret-refresh")

    def test_a_saved_key_lives_in_the_vault_and_routes_smart(self):
        providers.save_record("xai", {"key": "xai-secret", "model": "grok-4.7"})
        from mobile_agent import engines, model_clients
        env = {**os.environ, "MOBSTER_SMART_PROVIDER": "xai"}
        model = engines.smart_model(env)
        self.assertEqual((model, engines.model_provider(model), engines.provider_name(model)),
                         ("xai:grok-4.7", "xai", "xAI"))
        self.assertEqual(engines.smart_key(env), "xai-secret")
        client = model_clients.chat_client(model)
        self.assertEqual((type(client).__name__, client.host, client.path),
                         ("CompatChat", "api.x.ai", "/v1/chat/completions"))
        self.assertIsNone(model_clients.cost_usd(model, {"prompt_tokens": 5}))  # billed at xAI's own rates
        saved = next(self.root.rglob("provider-xai.json"))  # the test's HOME: never the person's Keychain
        self.assertEqual(oct(saved.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("xai-secret", json.dumps(providers.public()))

    def test_a_chosen_provider_without_its_key_is_never_a_silent_switch(self):
        from mobile_agent import engines
        env = {**os.environ, "MOBSTER_SMART_PROVIDER": "groq", "OPENAI_API_KEY": "sk-openai"}
        self.assertEqual(engines.model_provider(engines.smart_model(env)), "groq")
        self.assertIsNone(engines.smart_key(env))


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body.encode() if isinstance(body, str) else body

    def read(self):
        return self.body

    def getheader(self, name):
        return None


class FakeConnection:
    def __init__(self, replies, log):
        self.replies, self.log, self.timeout = replies, log, None

    def request(self, method, path, body, headers):
        self.log.append((path, json.loads(body), headers))

    def getresponse(self):
        return self.replies.pop(0)

    def close(self):
        pass


SCHEMA = {"type": "object", "properties": {"done": {"type": "boolean"}}, "required": ["done"],
          "additionalProperties": False}
MESSAGES = [{"role": "system", "content": "You operate an iPhone."},
            {"role": "user", "content": [{"type": "text", "text": "Screen: Home"},
                                         {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]}]


def chat(provider_id, *replies, cls=compat_chat.CompatChat, model=None):
    log, queue = [], list(replies)
    provider = providers.get(provider_id)
    client = cls(provider, model or provider.model or "m", key="k")
    client._connect = lambda timeout: FakeConnection(queue, log)
    return client, log


def tool_reply(arguments):
    return FakeResponse(200, json.dumps({"choices": [{"message": {"tool_calls": [
        {"type": "function", "function": {"name": "step", "arguments": json.dumps(arguments)}}]}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 20}}}))


class ChatTests(Isolated):
    def test_the_answer_is_a_forced_step_call_with_the_screenshot(self):
        client, log = chat("openrouter", tool_reply({"done": True}))
        out, usage = client.complete(MESSAGES, SCHEMA)
        self.assertEqual((out, usage["cached_tokens"]), ({"done": True}, 20))
        path, body, headers = log[0]
        self.assertEqual(path, "/api/v1/chat/completions")
        self.assertEqual(body["tool_choice"], {"type": "function", "function": {"name": "step"}})
        self.assertEqual(body["messages"][1]["content"][1]["type"], "image_url")
        self.assertEqual(headers["Authorization"], "Bearer k")
        self.assertEqual(headers["X-Title"], "Mobster")

    def test_a_text_only_model_gets_the_screen_as_text_and_the_loop_turns_screenshots_off(self):
        client, log = chat("zai", tool_reply({"done": False}), model="glm-5.1")
        client.complete(MESSAGES, SCHEMA)
        self.assertEqual(log[0][1]["messages"][1]["content"], "Screen: Home")
        from mobile_agent.frontier import FrontierAgent
        self.assertFalse(FrontierAgent(object(), client).screenshots)

    def test_a_refused_tool_choice_is_dropped_once_then_the_turn_goes_again(self):
        refused = FakeResponse(400, json.dumps({"error": {"message": "tool_choice object is not supported"}}))
        client, log = chat("deepseek", refused, tool_reply({"done": True}))
        out, _ = client.complete(MESSAGES, SCHEMA)
        self.assertEqual(out, {"done": True})
        self.assertEqual(log[1][1]["tool_choice"], "required")

    def test_a_plans_quota_is_never_retried(self):
        limited = FakeResponse(429, json.dumps({"error": {"message": "Usage limit reached for 5 hour. resets at 14:00",
                                                          "code": "1308"}}))
        client, log = chat("zai", limited)
        with self.assertRaises(compat_chat.ProviderError) as caught:
            client.complete(MESSAGES, SCHEMA)
        self.assertTrue(caught.exception.quota)
        self.assertEqual(len(log), 1)

    def test_the_anthropic_wire_forces_the_step_tool_and_sends_the_image_as_base64(self):
        reply = FakeResponse(200,
                             json.dumps({"content": [{"type": "tool_use", "name": "step", "input": {"done": True}}],
                                         "usage": {"input_tokens": 30, "output_tokens": 4}}))
        client, log = chat("minimax", reply, cls=compat_chat.AnthropicCompatChat)
        out, usage = client.complete(MESSAGES, SCHEMA)
        self.assertEqual((out, usage["prompt_tokens"]), ({"done": True}, 30))
        path, body, headers = log[0]
        self.assertEqual(path, "/anthropic/v1/messages")
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "step"})
        self.assertEqual(body["system"], "You operate an iPhone.")
        self.assertEqual(body["messages"][0]["content"][1]["source"]["data"], "AAAA")
        self.assertEqual(headers["x-api-key"], "k")

    def test_structuring_uses_each_providers_own_schema_wire(self):
        from mobile_agent import smart_result
        schema = {"type": "object", "properties": {"version": {"type": "string"}}}
        data = {"data": {"version": "26.4"}}
        replies = [tool_reply(data), FakeResponse(200, json.dumps({"content": [
            {"type": "tool_use", "name": "step", "input": data}]}))]
        for provider_id, cls, response in (("openrouter", compat_chat.CompatChat, replies[0]),
                                          ("minimax", compat_chat.AnthropicCompatChat, replies[1])):
            with self.subTest(provider=provider_id):
                client, log = chat(provider_id, response, cls=cls)
                self.addCleanup(client.close)
                self.assertEqual(smart_result.structure(client, "Read version", "26.4", [], schema),
                                 ({"version": "26.4"}, None))
                tool = log[0][1]["tools"][0]
                self.assertNotIn("strict", tool)
                self.assertNotIn("reasoning", log[0][1])
                if cls is compat_chat.CompatChat:
                    self.assertFalse(tool["function"]["strict"])
                    self.assertTrue(client.body(MESSAGES, SCHEMA)["tools"][0]["function"]["strict"])


class PlainHttpTests(Isolated):
    def test_copilots_api_address_is_https_only(self):
        for api, expected in (("http://api.evil.example", "https://api.individual.githubcopilot.com"),
                              ("https://api.business.githubcopilot.com/", "https://api.business.githubcopilot.com")):
            reply = json.dumps({"token": "t", "expires_at": 9e9, "endpoints": {"api": api}}).encode()
            record = device_login._exchange("gho", lambda request, timeout=None: Reply(reply))
            self.assertEqual(record["baseUrl"], expected)

    def test_a_saved_internet_http_address_never_gets_the_key(self):
        import dataclasses
        custom = providers.get("custom")
        client = compat_chat.CompatChat(dataclasses.replace(custom, base_url="http://llm.example.com/v1"), "m", key="k")
        with self.assertRaises(compat_chat.ProviderError) as caught:
            client._connect(5)
        self.assertIn("unencrypted", str(caught.exception))
        local = compat_chat.CompatChat(dataclasses.replace(custom, base_url="http://127.0.0.1:11434/v1"), "m", key="k")
        self.assertIsInstance(local._connect(5), http.client.HTTPConnection)
        secure = compat_chat.CompatChat(dataclasses.replace(custom, base_url="https://llm.example.com/v1"), "m",
                                        key="k")
        self.assertIsInstance(secure._connect(5), http.client.HTTPSConnection)

class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RouteTests(Isolated):
    class Keys:
        def __init__(self):
            self.saved = {}

        def update(self, body):
            self.saved.update(body)

        def state(self):
            return {"smart": {"preference": self.saved.get("smartProvider")}}

    def test_copilot_is_offered_only_with_mobsters_own_github_app(self):
        with self.assertRaises(routes.RouteError) as caught:
            routes.handle(self.Keys(), "copilot", "sign-in", {})
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertFalse(next(p for p in providers.public() if p["id"] == "copilot")["available"])

    def test_a_sign_in_failure_finishes_as_failed_and_names_no_secret(self):
        workers = []

        class Thread:
            def __init__(self, *, target, **kwargs):
                workers.append(target)

            def start(self):
                pass
        with patch.object(routes.threading, "Thread", Thread), \
                patch.object(device_login, "start", return_value={"user_code": "ABCD", "url": "https://example.com"}), \
                patch.object(device_login, "wait", side_effect=TypeError("secret reply")):
            routes.handle(self.Keys(), "grok", "sign-in", {})
            workers[-1]()
        self.assertEqual(routes._signins["grok"]["state"], "failed")
        self.assertNotIn("secret", routes._signins["grok"]["error"])

    def test_a_key_is_checked_against_the_model_list_then_saved_and_used(self):
        seen = []

        def opener(request, timeout=None):
            seen.append((request.full_url, request.headers.get("Authorization")))
            return Reply(b'{"data": []}')
        keys = self.Keys()
        answer = routes.handle(keys, "groq", "key", {"key": "gsk-1", "model": "qwen/qwen3.8-27b"}, opener)
        self.assertEqual((answer["saved"], answer["checked"]), (True, True))
        self.assertEqual(seen, [("https://api.groq.com/openai/v1/models", "Bearer gsk-1")])
        routes.handle(keys, "groq", "use", {}, opener)
        self.assertEqual(keys.saved["smartProvider"], "groq")
        routes.handle(keys, "groq", "forget", {}, opener)
        self.assertEqual(keys.saved["smartProvider"], None)
        self.assertFalse(providers.configured(providers.get("groq")))

    def test_a_refused_key_is_not_saved(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, io.BytesIO(b""))
        with self.assertRaises(routes.RouteError) as caught:
            routes.handle(self.Keys(), "openrouter", "key", {"key": "bad"}, opener)
        self.assertEqual(caught.exception.code, "key_rejected")
        self.assertEqual(providers.load_record("openrouter"), {})

    def test_a_custom_endpoint_needs_its_base_url(self):
        with self.assertRaises(routes.RouteError):
            routes.handle(self.Keys(), "custom", "key", {"key": "k"}, lambda *a, **k: Reply(b"{}"))
        answer = routes.handle(self.Keys(), "custom", "key", {"key": "k", "baseUrl": "https://llm.example.com/v1",
                                                              "model": "my-vl", "vision": True},
                               lambda *a, **k: Reply(b"{}"))
        self.assertTrue(answer["provider"]["vision"])

    def test_a_key_never_goes_to_an_internet_address_over_plain_http(self):
        tried = []

        def opener(request, timeout=None):
            tried.append(request.full_url)
            return Reply(b"{}")
        for base in ("http://llm.example.com/v1", "http://8.8.8.8:8080/v1", "http://[2001:4860::8888]/v1"):
            with self.assertRaises(routes.RouteError) as caught:
                routes.handle(self.Keys(), "custom", "key", {"key": "k", "baseUrl": base}, opener)
            self.assertIn("unencrypted", str(caught.exception))
        self.assertEqual(tried, [], "the key is refused before it's tried")
        self.assertEqual(providers.load_record("custom"), {})
        # This Mac and a local network still take http://, as Ollama and LM Studio serve it.
        for base in ("http://127.0.0.1:11434/v1", "http://localhost:1234/v1", "http://192.168.1.20:1234/v1",
                     "http://studio.local:1234/v1"):
            routes.handle(self.Keys(), "custom", "key", {"key": "k", "baseUrl": base}, opener)
        self.assertEqual(len(tried), 4)

    def test_sign_in_attempts_keep_their_own_cancellation_and_completion(self):
        workers = []
        class Thread:
            def __init__(self, *, target, **kwargs):
                workers.append(target)
            def start(self):
                pass
        def wait(provider_id, attempt, opener, cancelled, save=None):
            if cancelled():
                raise device_login.DeviceError("cancelled", "cancelled")
        with patch.object(routes.threading, "Thread", Thread), \
                patch.object(device_login, "start", return_value={"user_code": "ABCD", "url": "https://example.com"}), \
                patch.object(device_login, "wait", wait):
            routes.handle(self.Keys(), "grok", "sign-in", {})
            first = routes._signins["grok"]
            routes.handle(self.Keys(), "grok", "sign-in", {})
            current = routes._signins["grok"]
            self.assertTrue(first["cancel"])
            workers[0]()
            self.assertIs(routes._signins["grok"], current)
            self.assertEqual(current["state"], "waiting")
            workers[1]()
            self.assertEqual(current["state"], "signed_in")

    def test_cancel_forget_and_supersede_are_atomic_with_token_persistence(self):
        for action in ("cancel", "forget", "sign-in"):
            with self.subTest(action=action):
                routes._signins.clear()
                providers.save_record("grok", {"access_token": "existing"})
                ready, release, finished, newer = (threading.Event() for _ in range(4))
                starts = iter(({"device_code": "old", "user_code": "ABCD", "url": "https://example.com"},
                               {"device_code": "new", "user_code": "EFGH", "url": "https://example.com"}))
                def wait(provider_id, attempt, opener, cancelled, save):
                    if attempt["device_code"] == "old":
                        ready.set()
                        release.wait(3)
                        try:
                            save(provider_id, {"access_token": "old"})
                        finally:
                            finished.set()
                    else:
                        save(provider_id, {"access_token": "new"})
                        newer.set()
                with patch.object(device_login, "start", side_effect=lambda *args: next(starts)), \
                        patch.object(device_login, "wait", wait):
                    routes.handle(self.Keys(), "grok", "sign-in", {})
                    try:
                        self.assertTrue(ready.wait(2))
                        routes.handle(self.Keys(), "grok", action, {})
                        if action == "sign-in":
                            self.assertTrue(newer.wait(2))
                    finally:
                        release.set()
                    self.assertTrue(finished.wait(2))
                record = providers.load_record("grok")
                self.assertEqual(record.get("access_token"), {"cancel": "existing", "forget": None,
                                                              "sign-in": "new"}[action])

    def test_an_older_start_response_never_replaces_a_new_attempt_or_resurrects_a_forgotten_one(self):
        for action in ("sign-in", "forget"):
            with self.subTest(action=action):
                routes._signins.clear()
                providers.forget("grok")
                ready, release, saved = (threading.Event() for _ in range(3))
                replies, errors = [], []
                def start(*args):
                    if not ready.is_set():
                        ready.set()
                        release.wait(3)
                        return {"device_code": "old", "user_code": "ABCD", "url": "https://example.com"}
                    return {"device_code": "new", "user_code": "EFGH", "url": "https://example.com"}
                def wait(provider_id, attempt, opener, cancelled, save):
                    save(provider_id, {"access_token": attempt["device_code"]})
                    saved.set()
                def request():
                    try:
                        replies.append(routes.handle(self.Keys(), "grok", "sign-in", {}))
                    except routes.RouteError as error:
                        errors.append(error.code)
                worker = threading.Thread(target=request, daemon=True)
                with patch.object(device_login, "start", start), patch.object(device_login, "wait", wait):
                    worker.start()
                    try:
                        self.assertTrue(ready.wait(2))
                        routes.handle(self.Keys(), "grok", action, {})
                        if action == "sign-in":
                            self.assertTrue(saved.wait(2))
                    finally:
                        release.set()
                        worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(replies, [])
                self.assertEqual(errors, ["cancelled"])
                self.assertEqual(providers.load_record("grok").get("access_token"),
                                 "new" if action == "sign-in" else None)

    def test_late_refresh_success_rejection_and_model_edits_cannot_restore_or_replace_credentials(self):
        for operation, accepted in (("refresh", True), ("refresh", False), ("key", True)):
            for action in ("forget", "sign-in"):
                with self.subTest(operation=operation, accepted=accepted, action=action):
                    routes._signins.clear()
                    providers.save_record("grok", {"access_token": "old", "refresh_token": "refresh-old",
                                                    "expires_at": 1})
                    ready, release, saved = (threading.Event() for _ in range(3))
                    errors = []

                    def opener(request, timeout=None):
                        ready.set()
                        if not release.wait(3):
                            raise TimeoutError("test did not release the provider reply")
                        reply = {"access_token": "stale"} if accepted else {"error": "invalid_grant"}
                        return Reply(json.dumps(reply).encode())

                    def wait(provider_id, attempt, opener, cancelled, save):
                        save(provider_id, {"access_token": "new", "expires_at": time.time() + 3600})
                        saved.set()

                    def request():
                        try:
                            if operation == "refresh":
                                device_login.access_token("grok", opener)
                            else:
                                routes.handle(self.Keys(), "grok", "key", {"model": "new-model"}, opener)
                        except (device_login.DeviceError, routes.RouteError) as error:
                            errors.append(error.code)

                    worker = threading.Thread(target=request, daemon=True)
                    with patch.object(device_login, "start", return_value={"user_code": "ABCD",
                                                                           "url": "https://example.com"}), \
                            patch.object(device_login, "wait", wait):
                        worker.start()
                        try:
                            self.assertTrue(ready.wait(2))
                            routes.handle(self.Keys(), "grok", action, {})
                            if action == "sign-in":
                                self.assertTrue(saved.wait(2))
                            self.assertTrue(worker.is_alive(), "credential replacement waited for provider network")
                        finally:
                            release.set()
                            worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, ["sign_in_changed" if operation == "refresh" else "credentials_changed"])
                    self.assertEqual(providers.load_record("grok").get("access_token"),
                                     "new" if action == "sign-in" else None)


class DeviceTests(Isolated):
    def test_a_cancel_while_the_token_reply_arrives_saves_nothing(self):
        cancelled = []
        def opener(request, timeout=None):
            cancelled.append(True)
            return Reply(b'{"access_token":"at","expires_in":3600}')
        attempt = {"interval": 0, "expires_at": time.time() + 30, "device_code": "d"}
        with patch.object(providers, "save_record") as save, self.assertRaises(device_login.DeviceError) as caught:
            device_login.wait("grok", attempt, opener, pause=lambda seconds: None,
                              cancelled=lambda: bool(cancelled))
        self.assertEqual(caught.exception.code, "cancelled")
        save.assert_not_called()

    def test_grok_signs_in_with_a_device_code_and_refreshes(self):
        replies = [{"device_code": "d1", "user_code": "ABCD-EFGH", "verification_uri": "https://accounts.x.ai/device",
                    "interval": 2, "expires_in": 600},
                   {"error": "authorization_pending"},
                   {"access_token": "xai-at", "refresh_token": "xai-rt", "expires_in": 3600},
                   {"access_token": "xai-at2", "refresh_token": "xai-rt2", "expires_in": 3600}]
        forms = []

        def opener(request, timeout=None):
            forms.append(dict(p.split("=", 1) for p in request.data.decode().split("&")))
            return Reply(json.dumps(replies.pop(0)).encode())
        attempt = device_login.start("grok", opener)
        self.assertEqual(attempt["user_code"], "ABCD-EFGH")
        record = device_login.wait("grok", attempt, opener, pause=lambda s: None)
        self.assertEqual(record["access_token"], "xai-at")
        self.assertEqual(forms[0]["client_id"], device_login.FLOWS["grok"]["client_id"])
        self.assertEqual(device_login.access_token("grok", opener, now=time.time() + 4000), "xai-at2")
        self.assertTrue(providers.configured(providers.get("grok")))


class LoginCommandTests(Isolated):
    def test_mobster_login_saves_a_providers_key_from_stdin(self):
        from types import SimpleNamespace

        from mobile_agent import login
        args = SimpleNamespace(command="login", action="openrouter", with_key=True, model=None, base_url=None)
        saved = {}

        class Keys:
            def update(self, body):
                saved.update(body)

            def state(self):
                return {}
        with patch("sys.stdin", io.StringIO("sk-or-1\n")), patch.object(login, "_keys", lambda: Keys()), \
                patch.object(routes, "check_key", lambda *a, **k: (True, None)), \
                patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(login.run(args), 0)
        self.assertEqual(providers.load_record("openrouter")["key"], "sk-or-1")
        self.assertEqual(saved["smartProvider"], "openrouter")
        self.assertIn("anthropic/claude-sonnet-5.5", out.getvalue())


if __name__ == "__main__":
    unittest.main()
