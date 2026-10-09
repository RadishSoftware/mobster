"""The registry of Mobster's simulators (lock, atomic writes) and their port slots."""

import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

from mobile_agent import paths
from mobile_agent.sim import SimError, registry
from mobile_agent.sim.registry import Registry

ENTRY = {"udid": "0A1B2C3D-0000-4000-8000-000000000001", "name": "Mobster · iPhone 17 Pro · iOS 26.4",
         "device_type": "iPhone 17 Pro", "runtime": "iOS 26.4", "wda_port": 8310, "mjpeg_port": 9310,
         "created_at": "2026-09-28T12:00:00Z", "last_used_at": "2026-09-28T12:00:00Z"}


class DataDirTests(unittest.TestCase):
    def test_environment_wins(self):
        with mock.patch.dict(os.environ, {"MOBSTER_DATA_DIR": "~/somewhere/dev-data"}):
            self.assertEqual(paths.dev_data_dir(), Path("~/somewhere/dev-data").expanduser())

    def test_default_is_the_dev_folder_of_the_data_folder(self):
        with mock.patch.dict(os.environ, {"MOBSTER_DATA_DIR": ""}):
            self.assertEqual(paths.dev_data_dir(), paths.user_data_dir() / "dev")
        environ = {key: value for key, value in os.environ.items() if key != "MOBSTER_DATA_DIR"}
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual(paths.dev_data_dir().name, "dev")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.registry = Registry(Path(self.folder.name) / "dev")

    def test_missing_file_is_empty(self):
        self.assertEqual(self.registry.read(), [])

    def test_write_then_read_keeps_exactly_the_fields(self):
        with self.registry.edit() as entries:
            entries.append({**ENTRY, "extra": "dropped"})
        self.assertEqual(self.registry.read(), [ENTRY])
        data = json.loads(self.registry.path.read_text())
        self.assertEqual(list(data["simulators"][0]), list(registry.FIELDS))
        self.assertEqual(oct(self.registry.path.stat().st_mode & 0o777), "0o600")

    def test_an_unchanged_edit_does_not_write(self):
        with self.registry.edit() as entries:
            entries.append(dict(ENTRY))
        before = self.registry.path.stat().st_mtime_ns
        with mock.patch.object(Registry, "write") as write, self.registry.edit():
            pass
        write.assert_not_called()
        self.assertEqual(self.registry.path.stat().st_mtime_ns, before)

    def test_a_failed_write_keeps_the_old_file_and_leaves_no_temporary(self):
        with self.registry.edit() as entries:
            entries.append(dict(ENTRY))
        with mock.patch("mobile_agent.sim.registry.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError), self.registry.edit() as entries:
                entries.append({**ENTRY, "udid": "0A1B2C3D-0000-4000-8000-000000000002"})
        self.assertEqual(self.registry.read(), [ENTRY])
        self.assertEqual(sorted(path.name for path in self.registry.data_dir.iterdir()),
                         ["simulators.json", "simulators.lock"])

    def test_a_damaged_file_is_an_error_not_an_empty_registry(self):
        self.registry.data_dir.mkdir(parents=True)
        for text in ("{not json", '{"simulators": {}}', '{"simulators": [{"name": "no udid"}]}', "[]"):
            with self.subTest(text=text):
                self.registry.path.write_text(text)
                with self.assertRaises(SimError) as caught:
                    self.registry.read()
                self.assertEqual(caught.exception.kind, "simulator")
                self.assertIn("Mobster · ", caught.exception.fix)

    def test_the_lock_excludes_a_second_holder(self):
        with self.registry.locked():
            with self.assertRaises(SimError) as caught:
                with self.registry.locked(timeout=0.05):
                    pass
        self.assertEqual(caught.exception.kind, "busy")
        with self.registry.locked(timeout=0.05):  # free again once released
            pass

    def test_concurrent_edits_lose_nothing(self):
        def add(number):
            with self.registry.edit() as entries:
                entries.append({**ENTRY, "udid": f"0A1B2C3D-0000-4000-8000-{number:012d}"})

        threads = [threading.Thread(target=add, args=(number,)) for number in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.registry.read()), 24)


class PortTests(unittest.TestCase):
    def test_base_default_and_environment(self):
        self.assertEqual(registry.port_base({}), 8310)
        self.assertEqual(registry.port_base({"MOBSTER_SIM_PORT_BASE": "8320"}), 8320)
        for bad in ("abc", "80", "65000"):
            with self.subTest(bad=bad), self.assertRaises(SimError) as caught:
                registry.port_base({"MOBSTER_SIM_PORT_BASE": bad})
            self.assertEqual(caught.exception.kind, "environment")

    def test_slots_pair_mjpeg_at_plus_1000(self):
        self.assertEqual(registry.slot_ports(8310, 0), (8310, 9310))
        self.assertEqual(registry.slot_ports(8310, 39), (8349, 9349))

    def test_reserved_ports_are_never_given_out(self):
        for wda, mjpeg in ((8100, 9100), (8201, 9201), (8299, 9299), (8765, 9765), (7200, 8200), (7765, 8765)):
            with self.subTest(wda=wda):
                self.assertFalse(registry.allowed(wda, mjpeg))
        self.assertTrue(registry.allowed(8310, 9310))
        # A base inside the benchmark range skips past 8299 instead of landing in it.
        self.assertEqual(registry.free_slot(8280, busy=lambda port: False), (8300, 9300))

    def test_free_slot_skips_taken_and_busy_ports(self):
        self.assertEqual(registry.free_slot(8310, busy=lambda port: False), (8310, 9310))
        self.assertEqual(registry.free_slot(8310, taken={8310}, busy=lambda port: False), (8311, 9311))
        self.assertEqual(registry.free_slot(8310, taken={9311}, busy=lambda port: False), (8310, 9310))
        self.assertEqual(registry.free_slot(8310, taken={9310}, busy=lambda port: False), (8311, 9311))
        busy = {8310, 8311, 9312}
        self.assertEqual(registry.free_slot(8310, busy=busy.__contains__), (8313, 9313))

    def test_no_free_slot(self):
        with self.assertRaises(SimError) as caught:
            registry.free_slot(8310, busy=lambda port: True)
        self.assertEqual(caught.exception.kind, "simulator")
        self.assertIn("8310 to 8349", str(caught.exception))

    def test_taken_ports_from_entries(self):
        entries = [ENTRY, {**ENTRY, "udid": "B", "wda_port": 8311, "mjpeg_port": 9311}]
        self.assertEqual(registry.taken_ports(entries), {8310, 9310, 8311, 9311})
        self.assertEqual(registry.taken_ports(entries, skip_udid="B"), {8310, 9310})

    def test_listening_probe_on_loopback(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            self.assertTrue(registry.listening(port))
        finally:
            server.close()
        self.assertFalse(registry.listening(port))


if __name__ == "__main__":
    unittest.main()
