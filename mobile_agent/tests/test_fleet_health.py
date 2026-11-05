import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import alerts as alert_module
from mobile_agent import fleet_health as fh
from mobile_agent.api_errors import APIError
from mobile_agent.devices import REASONS

HOOK = "https://" + "discord.com" + "/api/" + "webhooks/1/x"

INSTAGRAM = {"bundleId": "com.burbn.instagram", "name": "Instagram"}
SAM = "00008130-001A2B3C4D5E6F70"

LOGIN = [("StaticText", "Instagram"), ("TextField", "Phone number, username or email address"),
         ("SecureTextField", "Password"), ("Button", "Log in"), ("Button", "Forgot password?"),
         ("Button", "Create new account")]
SIGNED_OUT_BANNER = [("StaticText", "You\u2019ve been logged out"),
                     ("StaticText", "Log in again to keep using Instagram"), ("Button", "Log in")]
AUTOMATED = [("StaticText", "We suspect automated behavior on your account"),
             ("StaticText", "Please complete this step to use Instagram."), ("Button", "Continue")]
CONFIRM = [("StaticText", "Confirm it\u2019s you"),
           ("StaticText", "Take a video selfie so we can confirm you own this account."), ("Button", "Next")]
CAPTCHA = [("StaticText", "Verify to continue"), ("StaticText", "Select all images with traffic lights"),
           ("Button", "Refresh"), ("Button", "Submit")]
SUSPENDED = [("StaticText", "Your account has been temporarily locked"), ("Button", "Learn more")]
FEED = [("Button", "Home"), ("StaticText", "getmobster"), ("StaticText", "Liked by sam and 12 others"),
        ("StaticText", "Log in to see more from your friends' posts"), ("Button", "Reels"), ("Button", "Search"),
        ("Button", "Profile")]
DM = [("StaticText", "Sam"), ("StaticText", "lol the app said we suspect automated behavior on my account"),
      ("TextField", "Message\u2026"), ("Button", "Send")]
PROFILE = [("StaticText", "getmobster"), ("Button", "Edit profile"), ("Button", "Share profile"),
           ("StaticText", "128 posts"), ("StaticText", "4,310 followers")]


def item(state="ready", reason=None, **extra):
    return {"id": SAM, "kind": "usb", "name": "Studio iPhone", "state": state, "reason": reason, "activeRunId": None,
            "default": True, "disconnected": False, **extra}


def getmobster(**extra):
    return {"id": "a1b2c3d4e5f6", "handle": "getmobster", "app": dict(INSTAGRAM), "device": SAM,
        "deviceName": "Studio iPhone",
            **extra}


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Fleet:
    def __init__(self, folder, runs=None, busy=None):
        self.data_dir = Path(folder)
        self.runtime = SimpleNamespace(runs=runs or {}, publish=lambda topic, event: self.events.append((topic, event)))
        self.events = []
        self._busy = busy
        self._in_use = False
        self._disconnected = False

    def busy(self, phone):
        return self._busy

    def in_use(self, phone):
        return self._in_use or self._busy is not None

    def is_disconnected(self, phone):
        return self._disconnected


def phone(**extra):
    return SimpleNamespace(id=SAM, name="Studio iPhone", primary=True, wda_url="http://127.0.0.1:8100", **extra)


class Case(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="mobster-health-")
        self.clock = Clock()
        self.fleet = Fleet(self.folder)
        self.reads = []
        self.locale = {"region": "US", "language": "en", "timezone": "America/Chicago"}

        def read(url):
            self.reads.append(url)
            return self.locale

        self.posts = []
        self.alerts = alert_module.Alerts(
            env={alert_module.ENV: HOOK}, post=lambda *a: self.posts.append(a) or 204, background=False,
            resolve=lambda host, port, type=None: [(2, 1, 6, "", ("93.184.216.34", port))])
        self.health = fh.FleetHealth(self.fleet, clock=self.clock, monotonic=self.clock, read=read,
                                     alerts=self.alerts, background=False)
        self.profiles = []
        self.health.profiles = lambda: list(self.profiles)


class ScreenTests(unittest.TestCase):
    def test_a_sign_in_page_a_session_banner_and_the_checks_are_recognized(self):
        self.assertEqual(fh.classify_screen(LOGIN), "signed_out")
        self.assertEqual(fh.classify_screen(SIGNED_OUT_BANNER), "signed_out")
        for screen in (AUTOMATED, CONFIRM, CAPTCHA, SUSPENDED, LOGIN + AUTOMATED):
            self.assertEqual(fh.classify_screen(screen), "challenge", screen)

    def test_a_feed_a_profile_and_a_message_that_quotes_the_words_say_nothing(self):
        for screen in (FEED, PROFILE, DM, [], None, [("Button", "Log in")], [("Button", "Forgot password?")],
                       [("StaticText", "We suspect automated behavior on your account")], [("Button", "Continue")]):
            self.assertIsNone(fh.classify_screen(screen), screen)

    def test_the_text_a_person_wrote_is_not_read_as_the_screens_own(self):
        screen = [("Cell", "We suspect automated behavior on your account"), ("TextView", "Confirm it\u2019s you"),
                  ("Button", "Continue")]
        self.assertIsNone(fh.classify_screen(screen))
        for label in ("Did you see the post: we suspect automated behavior on your account?",
                      "Confirm it\u2019s you was the worst screen of my week"):
            self.assertIsNone(fh.classify_screen([("StaticText", label), ("Button", "Send")]), label)
        long_label = "We suspect automated behavior on your account" + " and more" * 20
        self.assertGreater(len(long_label), fh.MAX_LABEL)
        self.assertIsNone(fh.classify_screen([("StaticText", long_label), ("Button", "Continue")]))

    def test_it_reads_a_snapshot_and_survives_odd_input(self):
        shot = SimpleNamespace(elements=[SimpleNamespace(role="StaticText", label="Confirm it's you", value=None),
                                         SimpleNamespace(role="Button", label="Next", value=None),
                                         SimpleNamespace(role="TextField", label=None, value="We suspect automated "
                                                         "behavior on your account")])
        pairs = fh.snapshot_labels(shot)
        self.assertEqual(pairs, [("StaticText", "Confirm it's you"), ("Button", "Next")])
        self.assertEqual(fh.classify_screen(pairs), "challenge")
        self.assertEqual(fh.snapshot_labels(SimpleNamespace(elements=None)), [])
        self.assertIsNone(fh.classify_screen([1, None, {"a": 1}, "x" * 10_000, ("Button",), ("StaticText", 5)]))


class LocaleTests(unittest.TestCase):
    def test_wda_device_info_becomes_region_and_an_iana_time_zone_and_never_a_language(self):
        self.assertEqual(fh.parse_locale({"currentLocale": "en_US", "timeZone": "America/Chicago"}),
                         {"region": "US", "language": None, "timezone": "America/Chicago"})
        self.assertEqual(fh.parse_locale({"currentLocale": "zh_Hans_CN@calendar=gregorian"})["region"], "CN")
        self.assertEqual(fh.parse_locale({"currentLocale": "fr-CA"})["region"], "CA")
        self.assertEqual(fh.parse_locale({"currentLocale": "en"}), {"region": None, "language": None, "timezone": None})
        self.assertEqual(fh.parse_locale(None), {"region": None, "language": None, "timezone": None})
        self.assertIsNone(fh.parse_locale({"timeZone": "x" * 200})["timezone"])

    def test_the_same_zone_under_another_name_matches(self):
        self.assertTrue(fh.zones_match("Asia/Calcutta", "Asia/Kolkata"))
        self.assertTrue(fh.zones_match("America/Chicago", "America/Chicago"))
        self.assertFalse(fh.zones_match("America/Chicago", "America/Denver"))
        self.assertFalse(fh.zones_match("America/Phoenix", "America/Los_Angeles"))
        self.assertFalse(fh.zones_match("Nowhere/Land", "America/Chicago"))

    def test_checks_match_mismatch_and_unknown(self):
        profile = getmobster(region="gb", language="EN", timezone="Europe/London")
        checks = fh.compare(profile, {"region": "GB", "language": "en", "timezone": "Europe/London"})
        self.assertEqual([c["state"] for c in checks], ["match"] * 3)
        checks = fh.compare(profile, {"region": "US", "language": "en", "timezone": None})
        self.assertEqual([(c["field"], c["state"]) for c in checks],
                         [("region", "mismatch"), ("language", "match"), ("timezone", "unknown")])
        self.assertEqual(fh.region_state(checks), "mismatch")
        self.assertEqual(fh.region_state(fh.compare(profile, None)), "unknown")
        self.assertEqual(fh.region_state(fh.compare(getmobster(), {"region": "US"})), "none")
        renamed = fh.compare(getmobster(timezone="Asia/Kolkata"), {"timezone": "Asia/Calcutta"})
        self.assertEqual([c["state"] for c in renamed], ["match"])


class AlertTests(Case):
    def test_unplugged_locked_and_paused_phones(self):
        gone = fh.state_alerts(item("disconnected", REASONS["unplugged"]), "Studio iPhone")
        self.assertEqual([a["code"] for a in gone], ["unplugged"])
        self.assertIn("Plug it into this Mac", gone[0]["action"])
        away = fh.state_alerts(item("disconnected", REASONS["wifi_unreachable"]), "Studio iPhone")
        self.assertIn("same Wi-Fi", away[0]["action"])
        locked = fh.state_alerts(item("connected", REASONS["locked"]), "Studio iPhone")
        self.assertEqual([a["code"] for a in locked], ["locked"])
        self.assertEqual(fh.state_alerts(item("disconnected", REASONS["paused"], disconnected=True), "x"), [])
        self.assertEqual(fh.state_alerts({**item("disconnected", REASONS["sim_off"]), "kind": "simulator"}, "x"), [])
        self.assertEqual(fh.state_alerts(item(), "x"), [])

    def test_the_owner_field_is_read_when_present_and_its_absence_is_fine(self):
        mine = fh.state_alerts(item(owner={"state": "owner_active", "since": 5}), "Studio iPhone")
        self.assertEqual([(a["code"], a["severity"], a["since"]) for a in mine], [("owner_active", "info", 5)])
        for owner in ({"state": "mobster", "since": 1}, {"state": "idle"}, None, "junk"):
            self.assertEqual(fh.state_alerts(item(owner=owner), "x"), [])
        self.assertEqual(fh.state_alerts(item(), "x"), [])


class HealthTests(Case):
    def test_a_phone_with_nothing_to_say(self):
        health = self.health.health_of(phone(), item())
        self.assertEqual(health, {"alerts": [], "lastGoodRun": None, "profiles": [], "currentTask": None,
                                  "region": {"state": "none", "profiles": [], "phone": {
                                      "region": None, "language": None, "timezone": None, "readAt": None}}})

    def test_profiles_on_the_phone_with_what_they_expect(self):
        self.profiles = [getmobster(region="US"), getmobster(id="b" * 12, handle="elsewhere", device="usb-other")]
        health = self.health.health_of(phone(), item())
        self.assertEqual([p["handle"] for p in health["profiles"]], ["getmobster"])
        self.assertEqual((health["profiles"][0]["region"], health["profiles"][0]["signedIn"]), ("US", None))

    def test_a_profile_with_no_phone_belongs_to_the_default_phone(self):
        self.profiles = [getmobster(device=None)]
        self.assertEqual(len(self.health.health_of(phone(), item())["profiles"]), 1)
        self.assertEqual(len(self.health.health_of(phone(), item(default=False))["profiles"]), 0)

    def test_the_last_good_run_is_the_newest_completed_one_on_the_phone(self):
        def run(run_id, status, at, device=SAM, mode="live"):
            return SimpleNamespace(id=run_id, status=status, finished_at=at, device_id=device,
                                   goal="Post the photo " * 20, mode=mode, created_at=at - 5)
        self.fleet.runtime.runs = {r.id: r for r in (run("a", "completed", 100), run("b", "completed", 300),
                                                      run("c", "error", 400),
                                                      run("d", "completed", 500, device="usb-2"),
                                                      run("e", "completed", 600, mode="demo"))}
        good = self.health.health_of(phone(), item())["lastGoodRun"]
        self.assertEqual((good["runId"], good["at"]), ("b", 300_000))
        self.assertLessEqual(len(good["summary"]), fh.GOAL_CHARS)

    def test_the_current_task_comes_from_the_run_and_from_a_holder(self):
        running = SimpleNamespace(id="r1", goal="Reply to comments", created_at=50, status="running", finished_at=None,
                                  device_id=SAM, mode="live")
        self.fleet.runtime.runs = {"r1": running}
        task = self.health.health_of(phone(), item("busy", activeRunId="r1"))["currentTask"]
        self.assertEqual((task["runId"], task["goal"], task["since"]), ("r1", "Reply to comments", 50_000))
        holder = {"kind": "mcp", "label": "Claude Code", "runId": None, "since": 9, "stopping": False}
        task = self.health.health_of(phone(), item("busy", holder=holder))["currentTask"]
        self.assertEqual((task["goal"], task["kind"], task["since"]), ("Claude Code", "mcp", 9))

    def test_a_signed_out_profile_alerts_and_says_so_in_its_chip(self):
        self.profiles = [getmobster()]
        self.health.signals.mark("a1b2c3d4e5f6", "signed_out", SAM)
        health = self.health.health_of(phone(), item())
        self.assertEqual([a["code"] for a in health["alerts"]], ["signed_out"])
        self.assertEqual(health["alerts"][0]["profile"], "a1b2c3d4e5f6")
        self.assertIs(health["profiles"][0]["signedIn"], False)

    def test_a_region_mismatch_alerts_only_with_a_known_value(self):
        self.profiles = [getmobster(region="GB")]
        self.health.locale(phone(), read_now=True)
        health = self.health.health_of(phone(), item())
        self.assertEqual([a["code"] for a in health["alerts"]], ["region_mismatch"])
        self.assertEqual(health["region"]["state"], "mismatch")
        self.assertEqual(health["region"]["phone"]["region"], "US")
        self.health.locales[SAM] = (self.clock(), None)
        health = self.health.health_of(phone(), item())
        self.assertEqual((health["alerts"], health["region"]["state"]), ([], "unknown"))

    def test_a_profile_with_no_app_does_not_break_the_page_or_the_alerts(self):
        broken = {"id": "c" * 12, "handle": "ghost", "device": SAM}
        self.profiles = [broken]
        self.health.signals.mark(broken["id"], "challenge", SAM)
        health = self.health.health_of(phone(), item())
        self.assertEqual([a["code"] for a in health["alerts"]], ["challenge"])
        self.assertEqual((health["profiles"][0]["name"], health["profiles"][0]["app"]), ("@ghost", "the app"))

    def test_the_background_read_runs_for_an_idle_ready_phone_that_has_an_expectation(self):
        self.profiles = [getmobster()]
        self.health.health_of(phone(), item())
        self.assertEqual(self.reads, [])
        self.profiles = [getmobster(region="US")]
        self.health.health_of(phone(), item("busy", activeRunId="r1"))
        self.health.health_of(phone(), item("connected", REASONS["locked"]))
        self.assertEqual(self.reads, [])
        self.fleet._in_use = True
        self.health.health_of(phone(), item())
        self.assertEqual(self.reads, [])
        self.fleet._in_use, self.fleet._disconnected = False, True
        self.health.health_of(phone(), item())
        self.health.health_of(phone(), item(disconnected=True))
        self.assertEqual(self.reads, [])
        self.fleet._disconnected = False
        self.health.health_of(phone(), item())
        self.health.health_of(phone(), item())
        self.assertEqual(len(self.reads), 1)
        self.clock.advance(fh.LOCALE_TTL + 1)
        self.health.health_of(phone(), item())
        self.assertEqual(len(self.reads), 2)


class EdgeTests(Case):
    def hold(self, *codes):
        current = {}
        for code in codes:
            current[(SAM, code, None)] = (item(), fh._alert(code, f"{code}.", "Do it.", None))
        return self.health.observe(current)

    def test_an_alert_comes_once_after_the_debounce_and_not_again_while_it_lasts(self):
        self.hold()
        self.assertEqual(self.hold("locked"), [])
        self.clock.advance(fh.DEBOUNCE_S - 1)
        self.assertEqual(self.hold("locked"), [])
        self.clock.advance(2)
        self.assertEqual(self.hold("locked"), [(SAM, "locked", None)])
        for _ in range(5):
            self.clock.advance(30)
            self.assertEqual(self.hold("locked"), [])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.fleet.events), 1)
        topic, event = self.fleet.events[0]
        self.assertEqual((topic, event["event"], event["code"], event["device"]),
                         ("devices", "phone_alert", "locked", SAM))
        self.assertIn("Studio iPhone", event["title"])
        self.assertIn("locked. Do it.", event["body"])

    def test_it_clears_after_the_debounce_and_an_alert_comes_again_when_it_returns(self):
        self.hold()
        self.hold("unplugged")
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.assertEqual(len(self.hold("unplugged")), 1)
        self.hold()
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.hold()
        self.assertEqual(self.health.active, {})
        self.hold("unplugged")
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.assertEqual(len(self.hold("unplugged")), 1)
        self.assertEqual(len(self.posts), 2)

    def test_a_phone_that_flickers_alerts_once(self):
        self.hold()
        self.hold("unplugged")
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.assertEqual(len(self.hold("unplugged")), 1)
        for _ in range(4):
            self.clock.advance(5)
            self.hold()
            self.clock.advance(5)
            self.assertEqual(self.hold("unplugged"), [])
        self.assertEqual(len(self.posts), 1)

    def test_what_already_holds_at_start_is_the_baseline(self):
        self.assertEqual(self.hold("unplugged"), [])
        self.clock.advance(60)
        self.assertEqual(self.hold("unplugged"), [])
        self.assertEqual(self.posts, [])

    def test_a_condition_that_returns_before_it_was_delivered_starts_its_wait_again(self):
        self.hold()
        self.hold("unplugged")
        self.clock.advance(15)
        self.hold()
        self.clock.advance(3)
        self.hold("unplugged")
        self.clock.advance(3)
        self.assertEqual(self.hold("unplugged"), [])
        self.clock.advance(fh.DEBOUNCE_S - 3)
        self.assertEqual(self.hold("unplugged"), [(SAM, "unplugged", None)])

    def test_a_read_of_one_phone_does_not_age_out_what_holds_on_another(self):
        other = "00008130-00000000ABCD0008"
        both = {(SAM, "locked", None): (item(), fh._alert("locked", "l.", "Unlock.", None)),
                (other, "locked", None): (item(id=other), fh._alert("locked", "l.", "Unlock.", None))}
        self.health.observe({})
        self.health.observe(both)
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.assertEqual(len(self.health.observe(both)), 2)
        for _ in range(3):
            self.clock.advance(fh.DEBOUNCE_S + 1)
            self.health.observe({(other, "locked", None): both[(other, "locked", None)]}, {SAM})
        self.assertNotIn((SAM, "locked", None), self.health.active)
        self.assertIn((other, "locked", None), self.health.active)
        for _ in range(3):
            self.clock.advance(fh.DEBOUNCE_S + 1)
            self.health.observe({}, {SAM})
        self.assertIn((other, "locked", None), self.health.active)
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.health.observe({}, {other})
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.health.observe({}, {other})
        self.assertNotIn((other, "locked", None), self.health.active)

    def test_only_a_read_of_every_phone_sets_the_baseline(self):
        locked = {(SAM, "locked", None): (item(), fh._alert("locked", "l.", "Unlock.", None))}
        self.assertEqual(self.health.observe(locked, {SAM}), [])
        self.assertFalse(self.health.baselined)
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.assertEqual(self.health.observe(locked), [])
        self.assertTrue(self.health.baselined)
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.assertEqual(self.health.observe(locked), [])
        self.assertEqual(self.posts, [])

    def test_apply_with_one_phones_probe_is_not_a_full_read(self):
        rows = [(phone(), item("disconnected", REASONS["unplugged"]))]
        self.health.apply(rows, complete=False)
        self.assertFalse(self.health.baselined)
        self.health.apply(rows)
        self.assertTrue(self.health.baselined)

    def test_you_using_the_phone_is_shown_but_never_sent(self):
        self.hold()
        self.hold("owner_active")
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.assertEqual(self.hold("owner_active"), [])
        self.assertEqual((self.posts, self.fleet.events), ([], []))

    def test_each_profile_alerts_on_its_own(self):
        self.hold()
        one = {(SAM, "signed_out", "p1"): (item(), fh._alert("signed_out", "p1.", "Sign in.", None, "p1")),
               (SAM, "signed_out", "p2"): (item(), fh._alert("signed_out", "p2.", "Sign in.", None, "p2"))}
        self.health.observe(one)
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.assertEqual(len(self.health.observe(one)), 2)

    def test_apply_adds_health_to_every_record_and_fills_since(self):
        rows = [(phone(), item("disconnected", REASONS["unplugged"]))]
        self.health.apply(rows)
        alert = rows[0][1]["health"]["alerts"][0]
        self.assertEqual(alert["since"], 1_000_000_000)
        self.clock.advance(100)
        self.health.apply(rows)
        self.assertEqual(rows[0][1]["health"]["alerts"][0]["since"], 1_000_000_000)

    def test_the_webhook_and_the_mac_get_the_same_condition_and_a_bad_webhook_fails_quietly(self):
        self.hold()
        self.hold("challenge")
        self.clock.advance(fh.DEBOUNCE_S + 1)
        self.alerts.post = lambda *a: (_ for _ in ()).throw(OSError("down"))
        self.alerts.resolve = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        self.assertEqual(len(self.hold("challenge")), 1)
        self.assertEqual(len(self.fleet.events), 1)


class WebhookTests(unittest.TestCase):
    def test_the_payload_names_the_phone_and_never_a_task_answer(self):
        data = alert_module.phone_payload("Studio iPhone", "locked", "Studio iPhone is locked.", "Unlock it.", at=0)
        self.assertEqual((data["status"], data["runId"], data["workflow"]), ("phone_locked", None, "Studio iPhone"))
        body, headers = alert_module.shape(HOOK, data)
        self.assertEqual(json.loads(body)["content"], "Mobster: Studio iPhone is locked. Unlock it.")
        body, headers = alert_module.shape("https://hooks.slack.com/services/x", data)
        self.assertIn("Unlock it.", json.loads(body)["text"])

    def test_phone_alert_posts_once_and_nothing_without_an_address(self):
        posts = []
        quiet = alert_module.Alerts(env={}, post=lambda *a: posts.append(a) or 200, background=False)
        self.assertFalse(quiet.phone_alert("Studio iPhone", "locked", "m", "a"))
        loud = alert_module.Alerts(env={alert_module.ENV: "https://ntfy.sh/mobster"},
                                   post=lambda *a: posts.append(a) or 200, background=False,
                                   resolve=lambda host, port, type=None: [(2, 1, 6, "", ("93.184.216.34", port))])
        self.assertTrue(loud.phone_alert("Studio iPhone", "locked", "Studio iPhone is locked.", "Unlock it."))
        self.assertEqual(len(posts), 1)


class SignalTests(Case):
    def test_signals_are_kept_across_a_restart_and_a_challenge_is_not_downgraded(self):
        self.health.signals.mark("p1", "challenge", SAM)
        self.health.signals.mark("p1", "signed_out", SAM)
        self.assertEqual(fh.Signals(self.folder).get("p1")["signal"], "challenge")
        self.assertEqual(oct(Path(self.folder, fh.STORE).stat().st_mode & 0o777), "0o600")

    def test_a_damaged_file_reads_as_empty(self):
        Path(self.folder, fh.STORE).write_text("{not json")
        self.assertEqual(fh.Signals(self.folder).get("p1"), {})

    def test_marking_as_handled_clears_the_alert(self):
        self.health.signals.mark("p1", "challenge", SAM)
        self.assertFalse(self.health.acknowledge("p1", "signed_out"))
        self.assertTrue(self.health.acknowledge("p1", "challenge"))
        self.assertFalse(self.health.acknowledge("p1"))
        with self.assertRaises(ValueError):
            self.health.acknowledge("p1", "locked")

    def run_of(self, status="completed", events=(), summary=None, account="a1b2c3d4e5f6"):
        return SimpleNamespace(extras={"account": {"id": account}}, device_id=SAM, status=status, summary=summary or {},
                               events=list(events))

    def test_a_run_that_stopped_on_the_profile_check_marks_it_signed_out(self):
        for code in ("account_unconfirmed", "account_mismatch"):
            self.health.signals.clear("a1b2c3d4e5f6")
            self.health.note_run(self.run_of("blocked", summary={"stop_code": code}))
            self.assertEqual(self.health.signals.get("a1b2c3d4e5f6")["signal"], "signed_out")

    def test_a_security_check_seen_on_screen_marks_it_and_only_the_person_clears_it(self):
        sighting = {"event": "account_signal", "account": "a1b2c3d4e5f6", "signal": "challenge"}
        self.health.note_run(self.run_of("blocked", [sighting]))
        self.assertEqual(self.health.signals.get("a1b2c3d4e5f6")["signal"], "challenge")
        confirmed = {"event": "account_checked", "account": "a1b2c3d4e5f6", "state": "confirmed"}
        self.health.note_run(self.run_of("completed", [confirmed]))
        self.health.note_run(self.run_of("completed"))
        self.assertEqual(self.health.signals.get("a1b2c3d4e5f6")["signal"], "challenge")
        self.assertTrue(self.health.acknowledge("a1b2c3d4e5f6", "challenge"))
        self.assertIsNone(self.health.signals.get("a1b2c3d4e5f6").get("signal"))

    def test_a_profile_confirmed_on_screen_clears_signed_out_and_a_run_that_never_looked_does_not(self):
        self.health.signals.mark("a1b2c3d4e5f6", "signed_out", SAM)
        self.health.note_run(self.run_of("completed"))
        self.assertEqual(self.health.signals.get("a1b2c3d4e5f6")["signal"], "signed_out")
        self.health.note_run(self.run_of("completed",
                                         [{"event": "account_checked", "account": "other", "state": "confirmed"}]))
        self.assertEqual(self.health.signals.get("a1b2c3d4e5f6")["signal"], "signed_out")
        self.health.note_run(self.run_of("completed", [{"event": "account_checked", "account": "a1b2c3d4e5f6",
                                                        "state": "confirmed"}]))
        stored = self.health.signals.get("a1b2c3d4e5f6")
        self.assertIsNone(stored["signal"])
        self.assertTrue(stored["confirmedAt"])
        self.profiles = [getmobster()]
        self.assertIs(self.health.health_of(phone(), item())["profiles"][0]["signedIn"], True)

    def test_a_security_check_seen_in_the_same_run_beats_a_later_confirmation(self):
        events = [{"event": "account_signal", "account": "a1b2c3d4e5f6", "signal": "challenge"},
                  {"event": "account_checked", "account": "a1b2c3d4e5f6", "state": "confirmed"}]
        self.health.note_run(self.run_of("completed", events))
        self.assertEqual(self.health.signals.get("a1b2c3d4e5f6")["signal"], "challenge")

    def test_the_screen_marks_the_profile_at_once_not_when_the_run_ends(self):
        self.health.note_screen({"id": "a1b2c3d4e5f6"}, SAM, "challenge")
        self.assertEqual(self.health.signals.get("a1b2c3d4e5f6")["signal"], "challenge")
        self.health.note_screen({"id": "a1b2c3d4e5f6"}, SAM, None)
        self.health.note_screen({"id": ""}, SAM, "signed_out")
        self.health.note_screen(None, SAM, "signed_out")
        self.assertEqual(list(self.health.signals.all()), ["a1b2c3d4e5f6"])
        self.assertEqual(self.health.signals.get("a1b2c3d4e5f6")["signal"], "challenge")

    def test_a_run_for_no_profile_or_with_odd_data_changes_nothing(self):
        self.health.note_run(SimpleNamespace(extras={}, device_id=SAM, status="completed", summary={}, events=[]))
        self.health.note_run(SimpleNamespace())
        self.assertEqual(self.health.signals.all(), {})

    def test_two_processes_on_one_folder_do_not_lose_each_others_marks(self):
        other = fh.FleetHealth(self.fleet, clock=self.clock, monotonic=self.clock, read=lambda url: None,
                               alerts=self.alerts, background=False)
        self.health.signals.mark("p1", "challenge", SAM)
        other.signals.mark("p2", "signed_out", SAM)
        self.health.signals.mark("p3", "signed_out", SAM)
        for health in (self.health, other):
            self.assertEqual(sorted(health.signals.all()), ["p1", "p2", "p3"])
        other.signals.clear("p1")
        self.assertIsNone(self.health.signals.get("p1")["signal"])
        self.assertEqual(self.health.signals.get("p2")["signal"], "signed_out")

    def test_marks_from_many_threads_on_one_folder_all_land(self):
        import threading
        errors = []

        def mark(index):
            try:
                fh.Signals(self.folder, self.clock).mark(f"p{index}", "signed_out", SAM)
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=mark, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(fh.Signals(self.folder).all()), 12)

    def test_a_folder_that_cannot_be_written_raises_instead_of_dropping_the_mark(self):
        with patch.object(fh.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.health.signals.mark("p1", "challenge", SAM)
        self.assertEqual(self.health.signals.all(), {})
        self.assertEqual([p.name for p in Path(self.folder).iterdir() if p.name.startswith(".fleet-health.")], [])

    def test_the_memory_only_store_works_without_a_folder(self):
        memory = fh.Signals(None, self.clock)
        memory.mark("p1", "challenge", None)
        self.assertEqual(memory.get("p1")["signal"], "challenge")
        self.assertTrue(memory.clear("p1", "challenge"))
        self.assertIsNone(memory.get("p1")["signal"])


class PreflightTests(Case):
    def refuse(self, account=None):
        with self.assertRaises(APIError) as caught:
            self.health.preflight(account or getmobster(), phone())
        return caught.exception

    def test_a_matching_phone_runs(self):
        self.profiles = [getmobster(region="US", language="en", timezone="America/Chicago")]
        self.health.preflight(getmobster(), phone())
        self.assertEqual(len(self.reads), 1)

    def test_a_known_difference_refuses_with_both_values_and_the_fix(self):
        self.profiles = [getmobster(region="GB", timezone="Europe/London")]
        error = self.refuse()
        self.assertEqual((error.status, error.code), (409, "profile_region_mismatch"))
        message = str(error)
        for part in ("region US where GB is expected", "time zone America/Chicago where Europe/London is expected",
                     "Language & Region › Region", "Date & Time", "Settings › Profiles", "No action was taken."):
            self.assertIn(part, message)
        self.assertEqual(error.details["device"], SAM)

    def test_a_value_that_cannot_be_read_does_not_block(self):
        self.profiles = [getmobster(region="GB")]
        self.locale = None
        self.health.preflight(getmobster(), phone())
        self.locale = {"region": None, "language": "en", "timezone": None}
        self.clock.advance(fh.PRE_RUN_TTL + 1)
        self.health.preflight(getmobster(), phone())

    def test_a_profile_that_expects_nothing_reads_nothing(self):
        self.profiles = [getmobster()]
        self.health.preflight(getmobster(), phone())
        self.assertEqual(self.reads, [])

    def test_a_phone_running_a_task_is_not_read(self):
        self.fleet._busy = "r1"
        self.profiles = [getmobster(region="GB")]
        self.health.preflight(getmobster(), phone())
        self.assertEqual(self.reads, [])

    def test_a_setting_changed_on_the_phone_is_seen_within_seconds(self):
        self.profiles = [getmobster(region="GB")]
        self.refuse()
        self.locale = {**self.locale, "region": "GB"}
        self.clock.advance(fh.PRE_RUN_TTL + 1)
        self.health.preflight(getmobster(), phone())

    def test_the_phones_language_is_never_compared_so_it_never_blocks_a_task(self):
        self.profiles = [getmobster(language="fr")]
        self.locale = fh.parse_locale({"currentLocale": "en_US", "timeZone": "America/Chicago"})
        self.health.preflight(getmobster(), phone())
        self.assertEqual(len(self.reads), 1)
        checks = fh.compare(self.profiles[0], self.locale)
        self.assertEqual([(c["field"], c["state"]) for c in checks], [("language", "unknown")])

    def test_a_time_zone_the_runner_reports_as_an_abbreviation_is_unknown_and_an_iana_one_still_refuses(self):
        self.profiles = [getmobster(timezone="Europe/London")]
        self.locale = fh.parse_locale({"currentLocale": "en_US", "timeZone": "CST"})
        self.health.preflight(getmobster(), phone())
        self.clock.advance(fh.PRE_RUN_TTL + 1)
        self.locale = fh.parse_locale({"currentLocale": "en_US", "timeZone": "America/Chicago"})
        self.assertEqual(self.refuse().code, "profile_region_mismatch")

    def test_a_phone_switched_off_in_mobster_or_used_by_another_mobster_is_not_read(self):
        self.profiles = [getmobster(region="GB")]
        for attribute in ("_disconnected", "_in_use"):
            setattr(self.fleet, attribute, True)
            self.health.preflight(getmobster(), phone())
            self.assertEqual(self.reads, [], attribute)
            setattr(self.fleet, attribute, False)
        self.assertEqual(self.refuse().code, "profile_region_mismatch")
        self.assertEqual(len(self.reads), 1)

    def test_a_fleet_that_cannot_tell_if_the_phone_is_in_use_reads_nothing(self):
        self.profiles = [getmobster(region="GB")]
        self.fleet.in_use = lambda phone: (_ for _ in ()).throw(RuntimeError("unclear"))
        self.health.preflight(getmobster(), phone())
        self.assertEqual(self.reads, [])

    def test_a_profile_with_no_app_is_still_checked_and_named(self):
        ghost = {"id": "c" * 12, "handle": "ghost", "device": SAM, "region": "GB"}
        self.profiles = [ghost]
        error = self.refuse(ghost)
        self.assertEqual(error.code, "profile_region_mismatch")
        self.health.signals.mark(ghost["id"], "challenge", SAM)
        self.assertEqual(self.refuse(ghost).code, "profile_challenge")

    def test_a_security_check_another_mobster_process_saw_refuses_here(self):
        other = fh.Signals(self.folder)
        other.mark("a1b2c3d4e5f6", "challenge", SAM)
        self.assertEqual(self.refuse().code, "profile_challenge")
        fh.Signals(self.folder).clear("a1b2c3d4e5f6", "challenge")
        self.health.preflight(getmobster(), phone())

    def test_a_security_check_refuses_until_it_is_marked_handled(self):
        self.health.signals.mark("a1b2c3d4e5f6", "challenge", SAM)
        error = self.refuse()
        self.assertEqual((error.status, error.code), (409, "profile_challenge"))
        self.assertIn("No action was taken.", str(error))
        self.assertIn("Instagram", str(error))
        self.health.acknowledge("a1b2c3d4e5f6", "challenge")
        self.health.preflight(getmobster(), phone())

    def test_signed_out_alone_does_not_refuse(self):
        self.health.signals.mark("a1b2c3d4e5f6", "signed_out", SAM)
        self.health.preflight(getmobster(), phone())


if __name__ == "__main__":
    unittest.main()
