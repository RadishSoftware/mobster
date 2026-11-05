import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import journal
from mobile_agent.journal import Lease, LeaseHeld

UDID = "00008130-001A2B3C4D5E6F70"
DIRECT, RELAY = "http://127.0.0.1:8100", "http://127.0.0.1:19100"


class PhoneLeaseTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mobster-phone-lease-")
        previous, journal._LEASE_ROOT = journal._LEASE_ROOT, self.root
        self.addCleanup(setattr, journal, "_LEASE_ROOT", previous)
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()

    def holder(self, address=DIRECT, udid=UDID, *, command=None):
        code = textwrap.dedent(f"""
            import sys, time
            from mobile_agent import journal
            journal._LEASE_ROOT = {self.root!r}
            journal._command_of = lambda pid: "mobster-test-holder"      # no `ps` under load: the record is fixed
            lease = journal.Lease.device({address!r}, udid={udid!r})
            print("held", flush=True)
            time.sleep(60)
        """)
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                                 cwd=str(Path(journal.__file__).resolve().parents[1]))
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    def test_a_free_phone_is_taken_and_the_holder_is_recorded(self):
        command = "mobster-test-worker --device test"
        with patch.object(journal, "_command_of", return_value=command):
            lease = Lease.device(DIRECT, udid=UDID)
        self.addCleanup(lease.close)
        record = json.loads(next(Path(self.root, "device-leases").glob("phone-*.lock")).read_text())
        self.assertEqual(record["pid"], os.getpid())
        self.assertEqual(record["command"], command)

    def test_a_second_mobster_is_refused_by_name_even_by_another_address(self):
        child = self.holder(DIRECT)
        command = json.loads(next(Path(self.root, "device-leases").glob("phone-*.lock")).read_text())["command"]
        with patch.object(journal, "_command_of", side_effect=lambda pid: command if pid == child.pid else None):
            for address in (DIRECT, RELAY):
                with self.subTest(address), self.assertRaises(LeaseHeld) as refused:
                    Lease.device(address, wait=0, udid=UDID)
                self.assertIn(f"pid {child.pid}", refused.exception.owner)
                self.assertIn("is driving this iPhone", str(refused.exception))
        other = Lease.device(RELAY, wait=0)
        other.close()

    def test_another_phone_is_not_held(self):
        self.holder(DIRECT, UDID)
        lease = Lease.device(RELAY, wait=0, udid="00008020-000A1B2C3D4E5F60")
        lease.close()

    def test_a_dead_holder_is_taken_over_and_its_record_replaced(self):
        child = self.holder()
        child.send_signal(signal.SIGKILL)
        child.wait()
        # The kernel dropped the dead holder's flock when it exited, before wait() returned: no waiting, no retry.
        lease = Lease.device(RELAY, wait=0, udid=UDID)
        self.addCleanup(lease.close)
        record = json.loads(next(Path(self.root, "device-leases").glob("phone-*.lock")).read_text())
        self.assertEqual(record["pid"], os.getpid())

    def test_a_record_that_no_longer_matches_its_process_is_not_named_but_still_refuses(self):
        child = self.holder()
        path = next(Path(self.root, "device-leases").glob("phone-*.lock"))
        for record in ({"pid": child.pid, "command": "something else entirely"},
                       {"pid": 2 ** 22 + 7, "command": "mobster serve"},
                       "not json"):
            with self.subTest(record=record):
                path.write_text(record if isinstance(record, str) else json.dumps(record))
                with self.assertRaises(LeaseHeld) as refused:
                    Lease.device(DIRECT, wait=0, udid=UDID)
                self.assertIsNone(refused.exception.owner)
                self.assertIn("Another Mobster process is driving this iPhone", str(refused.exception))

    def test_unknown_process_metadata_still_refuses_the_held_phone(self):
        self.holder()
        with patch.object(journal, "_command_of", return_value=None), self.assertRaises(LeaseHeld) as refused:
            Lease.device(RELAY, wait=0, udid=UDID)
        self.assertIsNone(refused.exception.owner)
        self.assertIn("Another Mobster process is driving this iPhone", str(refused.exception))

    def test_commands_are_described_for_a_person(self):
        self.assertEqual(journal.describe("/Applications/Mobster.app/Contents/MacOS/mobster-agent serve"),
                         "The Mobster app")
        self.assertEqual(journal.describe("python -m mobile_agent serve --port 8765"), "`mobster serve`")
        self.assertTrue(journal.describe("/usr/bin/other thing").startswith("Another Mobster ("))


if __name__ == "__main__":
    unittest.main()
