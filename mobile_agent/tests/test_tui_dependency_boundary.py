"""Broken core dependencies must not select a different terminal task route."""

import builtins
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent.tui.session import Session
from mobile_agent.tui.tasks import TaskMixin


class DependencyBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.session = object.__new__(Session)
        self.session.runtime = mock.Mock(config=None, journal=None, closing=False)

    def test_broken_core_imports_propagate_instead_of_selecting_fallbacks(self):
        original_import = builtins.__import__
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "note.txt"
            attachment.write_text("A synthetic attachment.")
            cases = [
                ("threads.service", self.session.threads),
                ("attachments.service", lambda: self.session.attach(attachment)),
                ("attachments.store", lambda: self.session.attach(attachment)),
                ("memory", lambda: TaskMixin.remember_here(SimpleNamespace(demo=True), "Remember my gym")),
                ("memory.store", lambda: TaskMixin.remember_here(SimpleNamespace(demo=True), "Remember my gym")),
            ]
            for module, call in cases:
                def broken_import(name, *args, **kwargs):
                    if name == module:
                        raise ImportError("broken core dependency")
                    return original_import(name, *args, **kwargs)

                with self.subTest(module=module), mock.patch("builtins.__import__", side_effect=broken_import):
                    with self.assertRaisesRegex(ImportError, "broken core dependency"):
                        call()

    def test_no_journal_remains_a_supported_runtime_state(self):
        self.assertIsNone(self.session.threads())
        self.assertEqual(self.session.recent_threads(), [])
        self.assertIsNone(self.session.new_thread())
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "note.txt"
            attachment.write_text("A synthetic attachment.")
            with self.assertRaisesRegex(ValueError, "needs Mobster's task history"):
                self.session.attach(attachment)

    def test_demo_memory_proposals_do_not_open_a_store(self):
        with mock.patch("mobile_agent.memory.store.default_store") as store:
            proposals = TaskMixin.remember_here(SimpleNamespace(demo=True), "Remember that my gym is on Fifth Street")
        self.assertTrue(proposals)
        self.assertTrue(all(proposal["id"] is None for proposal in proposals))
        store.assert_not_called()
