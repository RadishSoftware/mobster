import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent.setup_service import SetupService
from mobile_agent.tests.fakes import Clock
from mobile_agent.tests.test_fleet import PRO
from mobile_agent.tests.wireless_fixtures import (
    FRESH,
    HELPER_UP,
    OFF_CABLE,
    ON,
    TRUSTED,
)
from mobile_agent.wireless import (
    GATE,
    onboarding,
    pair,
    store,
)
from mobile_agent.wireless.devicectl import Peer
from mobile_agent.wireless.onboarding import Flow


def chosen():
    """A data folder where Setup chose the phone, so Mobster's list can note its switch."""
    folder = Path(tempfile.mkdtemp())
    (folder / "device.json").write_text(json.dumps({"udid": PRO}))
    return folder


class Devicectl:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return {PRO: answer} if answer is not None else {}


def over_wifi(tunnel="up", os="26.0"):
    return Peer(udid=PRO, transport="localNetwork", tunnel=tunnel, os_version=os)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.clock = Clock()
        self.pairs = []
        self.stored = []
        put = patch.object(
            store,
            "put",
            side_effect=lambda data, udid, on, via=None: self.stored.append(
                (udid, on, via)
            ),
        )
        put.start()
        self.addCleanup(put.stop)

    def flow(self, answer=True, lister=None):
        def pair_run():
            self.pairs.append(PRO)
            if isinstance(answer, Exception):
                raise answer
            return answer

        return Flow(
            PRO,
            self.data,
            pair_run=pair_run,
            lister=lister or Devicectl(),
            clock=self.clock,
        )

    def through_pairing(self, flow):
        flow.observe(FRESH)
        self.assertFalse(flow.observe(TRUSTED))
        self.assertEqual(flow.state, "asking")
        flow.answer(True)
        self.assertTrue(flow.observe(TRUSTED))
        self.assertEqual(flow.state, "pairing")
        self.assertFalse(flow.work())
        return flow

    def test_it_asks_and_sends_nothing_to_the_phone_without_a_yes(self):
        flow = self.flow()
        flow.observe(FRESH)
        for facts in (TRUSTED, TRUSTED, OFF_CABLE, TRUSTED, HELPER_UP):
            self.assertFalse(flow.observe(facts))
            self.assertEqual(flow.public()["state"], "asking")
        self.assertFalse(flow.work())
        self.assertEqual((self.pairs, self.stored), ([], []))
        flow.answer(False)
        self.assertEqual(flow.public()["state"], "skipped")
        for facts in (TRUSTED, HELPER_UP):
            self.assertFalse(flow.observe(facts))
        self.assertEqual((self.pairs, self.stored), ([], []))
        with self.assertRaises(LookupError):
            flow.answer(True)

    def test_the_pairing_is_noted_before_it_is_sent_and_kept_when_it_succeeds(self):
        seen = []

        def pair_run():
            seen.append(list(self.stored))
            return True

        flow = Flow(PRO, self.data, pair_run=pair_run, lister=Devicectl(), clock=self.clock)
        flow.observe(FRESH)
        flow.observe(TRUSTED)
        flow.answer(True)
        self.assertTrue(flow.observe(TRUSTED))
        self.assertFalse(flow.work())
        self.assertEqual(seen, [[(PRO, False, "mobster")]])
        self.assertEqual(self.stored, [(PRO, False, "mobster"), (PRO, True, "mobster")])
        self.assertEqual(flow.state, "paired")

    def test_a_phone_not_set_up_here_yet_is_not_paired_until_it_is(self):
        flow = self.flow()
        flow.observe(FRESH)
        flow.observe(TRUSTED)
        flow.answer(True)
        with patch.object(store, "put", side_effect=LookupError("not_set_up")):
            self.assertFalse(flow.observe(TRUSTED))
            self.assertEqual(flow.state, "after_trust")
        self.assertEqual(self.pairs, [])
        self.assertTrue(flow.observe(TRUSTED))
        self.assertEqual(flow.state, "pairing")

    def test_a_yes_asked_for_nothing_is_refused(self):
        flow = self.flow()
        with self.assertRaises(LookupError):
            flow.answer(True)
        flow.observe(FRESH)
        with self.assertRaises(LookupError):
            flow.answer(True)
        self.assertEqual(flow.state, "after_trust")

    def test_the_phones_own_wifi_switch_is_a_yes_and_it_isnt_paired_twice(self):
        flow = self.flow(lister=Devicectl(over_wifi()))
        flow.observe(FRESH)
        flow.observe(TRUSTED)
        with patch.object(store, "enabled", return_value=True):
            self.assertFalse(flow.observe(TRUSTED))
            self.assertEqual(flow.state, "paired")
            self.assertTrue(flow.observe(HELPER_UP))
        self.assertEqual(flow.state, "waiting_tunnel")
        self.assertFalse(flow.work())
        self.assertEqual(flow.state, "ready")
        self.assertEqual((self.pairs, self.stored), ([], []))

    def test_turned_on_off_the_cable_ends_the_step_for_the_wifi_monitor(self):
        flow = self.flow()
        flow.observe(FRESH)
        flow.observe(TRUSTED)
        with patch.object(store, "enabled", return_value=True):
            self.assertFalse(flow.observe(OFF_CABLE))
        self.assertEqual(flow.state, "skipped")
        with patch.object(store, "enabled", side_effect=OSError("unreadable")):
            other = self.flow()
            other.observe(FRESH)
            other.observe(TRUSTED)
            self.assertEqual(other.state, "asking")
        self.assertEqual(self.pairs, [])

    def test_the_tunnel_appears_and_the_phone_may_be_unplugged(self):
        lister = Devicectl(
            Peer(udid=PRO, transport="wired", tunnel="up", os_version="26.0.1"),
            over_wifi(os="26.0.1"),
        )
        flow = self.through_pairing(self.flow(lister=lister))
        self.assertEqual((flow.state, self.pairs), ("paired", [PRO]))
        self.assertEqual(
            flow.public(),
            {
                "state": "paired",
                "reason": None,
                "os": "26.0",
                "hub": False,
                "cable": True,
            },
        )
        self.assertEqual(self.stored, [(PRO, False, "mobster"), (PRO, True, "mobster")])
        self.assertTrue(flow.observe(HELPER_UP))
        self.assertEqual(flow.state, "waiting_tunnel")
        self.assertTrue(flow.work())
        self.clock.now += onboarding.POLL
        self.assertFalse(flow.work())
        self.assertEqual(
            flow.public(),
            {
                "state": "ready",
                "reason": None,
                "os": "26.0.1",
                "hub": False,
                "cable": True,
            },
        )
        self.assertEqual(self.pairs, [PRO])

    def test_a_restart_mid_setup_is_waited_for_and_setup_resumes(self):
        flow = self.through_pairing(self.flow(lister=Devicectl(over_wifi())))
        self.assertFalse(flow.observe(OFF_CABLE))
        self.assertEqual(
            flow.public(),
            {
                "state": "waiting_reboot",
                "reason": None,
                "os": "26.0",
                "hub": False,
                "cable": False,
            },
        )
        self.clock.now += 60
        flow.observe(OFF_CABLE)
        self.assertEqual(flow.state, "waiting_reboot")
        flow.observe(TRUSTED)
        self.assertEqual(flow.state, "paired")
        self.assertTrue(flow.observe(HELPER_UP))
        self.assertFalse(flow.work())
        self.assertEqual(flow.state, "ready")
        self.assertEqual(self.pairs, [PRO])

    def test_a_refused_pairing_stays_on_the_cable_and_is_never_tried_again(self):
        flow = self.through_pairing(
            self.flow(answer=pair.PairError("refused", "the phone said no"))
        )
        self.assertEqual((flow.state, flow.reason), ("cable_only", "refused"))
        for facts in (OFF_CABLE, TRUSTED, HELPER_UP):
            self.assertFalse(flow.observe(facts))
        self.assertEqual(self.pairs, [PRO])
        self.assertEqual(self.stored, [(PRO, False, "mobster"), (PRO, False, None)])   # noted, then put back

    def test_an_answer_it_cant_read_is_never_tried_again(self):
        noted, back = (PRO, False, "mobster"), (PRO, False, None)
        for error, reason, stored in (
            (
                pair.PairError("failed", "mobster-wifi-pair didn't answer in 20 s"),
                "unknown",
                [noted],   # its outcome is unknown: the note stays, so an off clears the switch
            ),
            (OSError("pipe"), "unknown", [noted]),
            (LookupError("no tool"), "needs_xcode", [noted, back]),
        ):
            with self.subTest(error=error):
                self.pairs.clear()
                self.stored.clear()
                flow = self.through_pairing(self.flow(answer=error))
                self.assertEqual((flow.state, flow.reason), ("cable_only", reason))
                flow.observe(OFF_CABLE)
                flow.observe(TRUSTED)
                self.assertEqual(self.pairs, [PRO])
                self.assertEqual(self.stored, stored)

    def test_not_on_the_cable_waits_for_a_new_plug_in_before_it_tries_again(self):
        flow = self.through_pairing(self.flow(answer=pair.PairError("needs_cable")))
        self.assertEqual(flow.state, "after_trust")
        self.assertFalse(flow.observe(TRUSTED))
        self.assertEqual(self.pairs, [PRO])
        flow.observe(OFF_CABLE)
        self.assertTrue(flow.observe(TRUSTED))
        self.assertEqual(flow.state, "pairing")   # the yes holds: it isn't asked again

    def test_a_phone_set_up_before_gets_no_wifi_step(self):
        flow = self.flow()
        self.assertFalse(flow.observe(HELPER_UP))
        self.assertEqual(flow.public()["state"], "skipped")
        self.assertEqual(self.pairs, [])


class StepTests(unittest.TestCase):
    def setUp(self):
        onboarding.forget()
        self.addCleanup(onboarding.forget)
        self.data = tempfile.mkdtemp()
        Path(self.data, "device.json").write_text(json.dumps({"udid": PRO}))   # chosen in Setup

    def test_wifi_for_iphones_off_runs_nothing(self):
        spawned = []
        with (
            patch.dict(os.environ, {GATE: "0"}, clear=True),
            patch.object(pair, "run") as run,
        ):
            answer = onboarding.step(PRO, self.data, TRUSTED, spawn=spawned.append)
        self.assertEqual(
            answer,
            {"state": "off", "reason": None, "os": None, "hub": False, "cable": False},
        )
        self.assertEqual((spawned, run.call_count, onboarding._flows), ([], 0, {}))

    def test_wifi_on_asks_then_starts_one_worker_for_the_pairing(self):
        spawned = []
        with patch.dict(os.environ, ON, clear=True):
            onboarding.step(PRO, self.data, FRESH, spawn=spawned.append)
            asked = onboarding.step(PRO, self.data, TRUSTED, spawn=spawned.append)
            self.assertEqual((asked["state"], spawned), ("asking", []))
            onboarding.answer(PRO.lower(), True)
            answer = onboarding.step(PRO, self.data, TRUSTED, spawn=spawned.append)
            onboarding.step(PRO, self.data, TRUSTED, spawn=spawned.append)
        self.assertEqual(answer["state"], "pairing")
        self.assertEqual(len(spawned), 1)
        with self.assertRaises(LookupError):
            onboarding.answer("00008130-00000000ABCD0009", True)


class SetupBodyTests(unittest.TestCase):
    def service(self, device):
        manager = SimpleNamespace(
            tools=lambda: {
                "iproxy": "/bin/x",
                "idevice_id": "/bin/x",
                "ideviceinfo": "/bin/x",
                "xcodebuild": "/bin/x",
                "git": "/bin/x",
                "xcode": {
                    "state": "ok",
                    "version": "Xcode 26.4",
                    "app": None,
                    "selected": None,
                    "fixes": [],
                },
            },
            devices=lambda: [device],
            device=lambda listed=None: device,
            settings=lambda: {},
            built=lambda: False,
            teams=lambda: [],
            build={"state": "idle", "error": None, "log_tail": []},
            runner_state=lambda: {"state": "stopped", "error": None},
            expired=lambda: False,
            signature_expiry=lambda: None,
            data_dir=chosen(),
        )
        runtime = SimpleNamespace(
            config=SimpleNamespace(enable_live=False),
            video=SimpleNamespace(status=lambda: {}),
        )
        service = SetupService(runtime, manager, Path(tempfile.mkdtemp()) / "agent.env")
        service.developer_mode = lambda phone: None
        return service

    def setUp(self):
        onboarding.forget()
        self.addCleanup(onboarding.forget)

    def test_on_a_trusted_new_phone_it_asks_and_pairs_only_after_a_yes(self):
        device = {"udid": PRO, "name": "Studio iPhone", "trusted": True, "ios": "26.0"}
        with (
            patch.dict(os.environ, ON, clear=True),
            patch.object(onboarding, "_thread") as thread,
        ):
            service = self.service(device)
            self.assertEqual(
                service.state()["wifi"],
                {
                    "state": "asking",
                    "reason": None,
                    "os": "26.0",
                    "hub": False,
                    "cable": True,
                },
            )
            thread.assert_not_called()
            service.answer_wifi(device, True)
            self.assertEqual(service.state()["wifi"]["state"], "pairing")
            with self.assertRaises(LookupError):
                service.answer_wifi(device, True)
        thread.assert_called_once()
