import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent.permissions.exceptions import Exceptions, key_of
from mobile_agent.permissions.policy import APP_LABEL, APP_OP, AppPolicy


class ExceptionStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "exceptions.json"
        self.store = Exceptions(self.path)
        self.apps = AppPolicy()
        self.existing = "com.example.existing"
        self.proposed = "com.example.proposed"

    def test_failed_add_does_not_grant_permission_or_change_saved_rules(self):
        self.store.add(self.existing, APP_LABEL, APP_OP)
        before = self.path.read_bytes()
        with patch("mobile_agent.permissions.exceptions.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.add(self.proposed, APP_LABEL, APP_OP)
        self.assertTrue(self.apps.approved(self.existing, self.store))
        self.assertFalse(self.apps.approved(self.proposed, self.store))
        self.assertFalse(self.apps.approved(self.proposed, Exceptions(self.path)))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".exceptions-*")), [])

    def test_failed_first_save_does_not_grant_permission(self):
        with patch("mobile_agent.permissions.exceptions.tempfile.mkstemp", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                self.store.add(self.proposed, APP_LABEL, APP_OP)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.apps.approved(self.proposed, self.store))

    def test_failed_remove_leaves_cache_consistent_with_saved_rules(self):
        self.store.add(self.existing, APP_LABEL, APP_OP)
        before = self.path.read_bytes()
        with patch("mobile_agent.permissions.exceptions.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.remove(key_of(self.existing, APP_LABEL, APP_OP))
        self.assertTrue(self.apps.approved(self.existing, self.store))
        self.assertTrue(self.apps.approved(self.existing, Exceptions(self.path)))
        self.assertEqual(self.path.read_bytes(), before)

    def test_saved_add_and_remove_are_visible_to_existing_and_fresh_readers(self):
        other = Exceptions(self.path)
        self.assertFalse(self.apps.approved(self.existing, other))
        self.store.add(self.existing, APP_LABEL, APP_OP)
        self.assertTrue(self.apps.approved(self.existing, self.store))
        self.assertTrue(self.apps.approved(self.existing, other))
        self.assertTrue(self.store.remove(key_of(self.existing, APP_LABEL, APP_OP)))
        self.assertFalse(self.apps.approved(self.existing, self.store))
        self.assertFalse(self.apps.approved(self.existing, other))
        self.assertFalse(self.apps.approved(self.existing, Exceptions(self.path)))

    def test_returned_records_cannot_mutate_saved_or_cached_rules(self):
        added = self.store.add(self.existing, APP_LABEL, APP_OP, "Existing")
        added["appName"] = "Changed outside the store"
        listed = self.store.public()
        self.assertEqual(listed[0]["appName"], "Existing")
        listed[0]["appName"] = "Changed through public state"
        self.assertEqual(self.store.public()[0]["appName"], "Existing")
        self.assertEqual(Exceptions(self.path).public()[0]["appName"], "Existing")
