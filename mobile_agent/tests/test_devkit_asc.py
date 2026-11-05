
import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import plan_gate
from mobile_agent.devkit import DevError, asc, asc_cli, es256, shots
from mobile_agent.sim.simctl import Result
from mobile_agent.tests.test_verify_fixtures import app as tree_xml
from mobile_agent.tests.test_verify_fixtures import node

RFC_D = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
RFC_UX = 0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6
RFC_SAMPLE = (0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
              0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8)
RFC_TEST = (0xF1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367,
            0x019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083)


def pem(label, body):
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----"


def p8_for(d):
    def tlv(tag, body):
        return bytes([tag, len(body)]) + body
    ec = tlv(0x30, tlv(0x02, b"\x01") + tlv(0x04, d.to_bytes(32, "big")))
    algorithm = tlv(0x30, tlv(0x06, es256.EC_PUBLIC_KEY) + tlv(0x06, es256.PRIME256V1))
    der = tlv(0x30, tlv(0x02, b"\x00") + algorithm + tlv(0x04, ec))
    text = base64.b64encode(der).decode()
    return pem(es256.P8_LABEL, "\n".join(text[i:i + 64] for i in range(0, len(text), 64))) + "\n"


class ES256Tests(unittest.TestCase):
    def test_rfc6979_vectors(self):
        self.assertEqual(es256.public_key(RFC_D)[0], RFC_UX)
        self.assertEqual(es256.sign(RFC_D, b"sample"), RFC_SAMPLE)
        self.assertEqual(es256.sign(RFC_D, b"test"), RFC_TEST)
        self.assertTrue(es256.verify(es256.public_key(RFC_D), b"sample", RFC_SAMPLE))
        self.assertFalse(es256.verify(es256.public_key(RFC_D), b"sample!", RFC_SAMPLE))


class Keychain:
    def __init__(self):
        self.items = {}

    def get(self, account):
        return self.items.get(account)

    def set(self, account, value):
        self.items[account] = value

    def delete(self, account):
        return self.items.pop(account, None) is not None


class KeyTests(unittest.TestCase):
    def test_a_team_key_goes_to_the_keychain_and_signs_apples_claims(self):
        store = Keychain()
        asc.save_key("2X9R4HXF34", "57246542-96fe-1a63-e053-0824d011072a", p8_for(RFC_D), store=store)
        self.assertIn(es256.P8_BEGIN, json.loads(store.items["default"])["p8"])
        key = asc.load_key(store=store, environ={})
        self.assertEqual(key.public(), {"keyId": "2X9R4HXF34", "issuerId": "57246542-96fe-1a63-e053-0824d011072a",
                                        "kind": "team", "source": "Keychain"})
        head, claims, _ = key.token(now=1_000).split(".")
        self.assertEqual(json.loads(base64.urlsafe_b64decode(head + "==")), {"alg": "ES256", "kid": "2X9R4HXF34",
                                                                             "typ": "JWT"})
        self.assertEqual(json.loads(base64.urlsafe_b64decode(claims + "==")),
                         {"iat": 1000, "exp": 2200, "aud": "appstoreconnect-v1",
                          "iss": "57246542-96fe-1a63-e053-0824d011072a"})
        self.assertNotIn("BEGIN", json.dumps(key.public()))

    def test_an_individual_key_from_the_environment(self):
        key = asc.load_key(store=Keychain(), environ={"MOBSTER_ASC_KEY_ID": "ABCDEFGH12", "MOBSTER_ASC_KEY": p8_for(5)})
        claims = json.loads(base64.urlsafe_b64decode(key.token(now=0).split(".")[1] + "=="))
        self.assertEqual(claims["sub"], "user")
        self.assertNotIn("iss", claims)


class Apple:
    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def __call__(self, request, timeout=None):
        self.seen.append((request.get_method(), request.full_url, request.headers.get("Authorization", "")))
        path = request.full_url.split("api.appstoreconnect.apple.com", 1)[1].split("?")[0]
        return contextlib.nullcontext(io.BytesIO(json.dumps(self.routes[path]).encode()))


def client(routes):
    key = asc.Key("ABCDEFGH12", None, p8_for(7), "test")
    apple = Apple(routes)
    return asc.Client(key, opener=apple), apple


APP = {"data": [{"id": "1234567890", "type": "apps",
                 "attributes": {"name": "Habits", "bundleId": "com.example.habits"}}]}


class ClientTests(unittest.TestCase):
    def test_it_only_reads(self):
        api, apple = client({})
        for method in ("POST", "PATCH", "DELETE", "PUT"):
            with self.assertRaises(DevError):
                api.request(method, "/v1/apps")
        self.assertEqual(apple.seen, [])
        self.assertFalse(any(hasattr(api, name) for name in ("post", "patch", "delete", "upload", "submit")))
        with self.assertRaises(DevError):
            api.get("https://example.com/steal")


class FeedbackSaveTests(unittest.TestCase):
    def test_save_writes_https_screenshots_and_crash_logs_and_never_sends_the_token_to_the_cdn(self):
        routes = {
            "/v1/apps": APP,
            "/v1/apps/1234567890/betaFeedbackScreenshotSubmissions": {"data": [{"id": "s1", "attributes": {
                "createdDate": "2026-10-12T10:00:00Z", "screenshots": [{"url": "https://cdn.example.com/s1.jpg"},
                                                                       {"url": "http://cdn.example.com/plain.jpg"}]}}]},
            "/v1/apps/1234567890/betaFeedbackCrashSubmissions": {"data": [{"id": "c1", "attributes": {
                "createdDate": "2026-10-13T10:00:00Z"}}]},
            "/v1/betaFeedbackCrashSubmissions/c1/crashLog": {"data": {"attributes": {"logText": "Thread 0 Crashed"}}}}
        seen = []

        def opener(request, timeout=None):
            seen.append((request.full_url, request.headers.get("Authorization")))
            if "api.appstoreconnect.apple.com" in request.full_url:
                path = request.full_url.split("api.appstoreconnect.apple.com", 1)[1].split("?")[0]
                return contextlib.nullcontext(io.BytesIO(json.dumps(routes[path]).encode()))
            return contextlib.nullcontext(io.BytesIO(b"JPEG"))
        api = asc.Client(asc.Key("ABCDEFGH12", None, p8_for(7), "test"), opener=opener)
        args = SimpleNamespace(asc_command="feedback", app="com.example.habits", crashes=False, screenshots=False,
                               limit=20, save="out", json=True)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asc_cli.run(args, client=api, cwd=tmp), 0)
            folder = Path(tmp) / "out"
            self.assertEqual(sorted(path.name for path in folder.iterdir()), ["c1.crash", "s1-1.jpg"])
            self.assertEqual((folder / "s1-1.jpg").read_bytes(), b"JPEG")
            self.assertEqual((folder / "c1.crash").read_text(), "Thread 0 Crashed")
        downloads = [(url, auth) for url, auth in seen if "api.appstoreconnect.apple.com" not in url]
        self.assertEqual(downloads, [("https://cdn.example.com/s1.jpg", None)])


class Phone:
    def __init__(self, screens):
        self.screens, self.taps, self.opened = list(screens), [], []

    @contextlib.contextmanager
    def __call__(self, record):
        def request(method, path, body=None, timeout=15):
            if path == "/url":
                self.opened.append(body)
            elif path.startswith("/source"):
                return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
            elif path == "/element":
                return {"ELEMENT": body["value"].split("label == '")[1].rstrip("'")}
            elif path.endswith("/click"):
                self.taps.append(path.split("/")[2])
            return None
        yield request


def screen(*buttons):
    return tree_xml(*(node("Button", label) for label in buttons))


class TestFlightTests(unittest.TestCase):
    def install(self, phone, record=None):
        with mock.patch("mobile_agent.wda.system_dialogs.read", return_value=None):
            return asc.testflight_install(record or {"name": "Test iPhone", "kind": "usb"}, phone, "1234567890",
                                          sleep=lambda s: None)

    def test_up_to_date_taps_nothing_and_terms_are_never_accepted(self):
        phone = Phone([screen("Open")])
        self.assertEqual(self.install(phone), "already")
        self.assertEqual(phone.taps, [])
        phone = Phone([screen("Accept", "Decline")])
        with self.assertRaises(DevError) as caught:
            self.install(phone)
        self.assertIn("doesn't accept terms", caught.exception.message)
        self.assertEqual(phone.taps, [])
        with self.assertRaises(DevError):
            self.install(Phone([screen("Open")]), {"name": "iPhone 17 Pro", "kind": "simulator"})


class ShotsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, name="Home greets you"):
        from mobile_agent.verify.checks import check_from_dict
        return check_from_dict({"version": 1, "name": name, "app": {"bundle": "com.example.habits"},
                                "launch": {"url": "habits://home"}, "expect": [{"text": "Welcome back"}]})

    def test_each_shot_runs_the_check_in_its_language_and_keeps_the_9_41_screen(self):
        calls = []

        def run(argv, timeout=60, env=None, done=None):
            calls.append(argv)
            if "screenshot" in argv:
                from PIL import Image
                Image.new("RGB", (1320, 2868)).save(argv[-1], "PNG")
            return Result(0, "", "")

        class Manager:
            def acquire(self, *a, **k):
                return SimpleNamespace(target=SimpleNamespace(udid="U1"), release=lambda: calls.append("released"))

        seen = []

        def verify(check, *, runs_dir, manager):
            seen.append(check)
            lease = manager.acquire(check.device)
            lease.release()
            return {"verdict": "passed", "run_id": "r1"}
        planned, _ = shots.plan_shots([self.check()], sizes=("6.9",), locales=("de-DE",),
                                      available=["iPhone 17 Pro Max"])
        original = shots.StatusBarAndShot
        with mock.patch.object(shots, "StatusBarAndShot", lambda manager, path: original(manager, path, run=run)):
            doc = shots.take(planned, out=self.root / "out", runs_dir=self.root / "runs", manager=Manager(),
                             verify=verify)
        self.assertEqual(seen[0].device, "iPhone 17 Pro Max")
        self.assertEqual(seen[0].launch_args[-4:], ("-AppleLanguages", "(de-DE)", "-AppleLocale", "de_DE"))
        self.assertEqual(calls[0][:4], ["xcrun", "simctl", "status_bar", "U1"])
        self.assertIn("9:41", calls[0])
        self.assertEqual(calls[-2], ["xcrun", "simctl", "status_bar", "U1", "clear"])
        self.assertEqual(calls[-1], "released")
        row = doc["shots"][0]
        self.assertEqual((row["file"], row["pixels"], row["fits"]), ("de-DE/01_69_home-greets-you.png",
                                                                     [1320, 2868], True))
        self.assertTrue(doc["ok"])
        self.assertTrue((self.root / "out" / "screenshots.json").is_file())

    def test_a_run_that_couldnt_run_leaves_no_shot(self):
        def verify(check, *, runs_dir, manager):
            return {"verdict": "couldnt_run"}
        planned, _ = shots.plan_shots([self.check()], sizes=("6.9",), locales=("en-US",),
                                      available=["iPhone 17 Pro Max"])
        doc = shots.take(planned, out=self.root / "out", runs_dir=self.root, manager=object(), verify=verify)
        self.assertEqual((doc["ok"], doc["shots"][0]["file"]), (False, None))


class PlanTests(unittest.TestCase):
    def test_everything_but_the_key_needs_the_plan(self):
        for verb, needs in (("key", False), ("builds", True), ("feedback", True), ("testflight", True),
                            ("screenshots", True), ("apps", True)):
            self.assertEqual(plan_gate.cli_needs_plan("asc", SimpleNamespace(asc_command=verb)), needs, verb)


if __name__ == "__main__":
    unittest.main()
