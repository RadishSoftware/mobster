"""The opt-in corpus: nothing is written while it is off, a done run writes one record per step, and no label text
(an email, a phone number, a handle, a name) is in the record."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import harness_api
from mobile_agent.corpus import ENV_FLAG, consent, install, skeleton, store
from mobile_agent.corpus.listener import CorpusListener
from mobile_agent.state import Element


def screen(*labels, role="Button"):
    return SimpleNamespace(elements=[Element(id=f"e{i}", label=label, role=role, rect=(0.1, 0.01 * i, 0.5, 0.01))
                                     for i, label in enumerate(labels)], bundle_id="com.apple.Preferences")


def step(captured, index=0, op="TAP", app="com.apple.Preferences", approval=None, changed=True):
    target = {"label": "x", "role": "Button", "id": "e0", "frame": [0, 0, 1, 1]}
    return harness_api.StepRecord(run_id="r1", step=index, index=index, op=op, app=app, target=target, text=None,
                                  screen="s", changed=changed, approval=approval, commit=None, corpus=captured)


class CorpusTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        patch = mock.patch.dict(os.environ, {"MOBSTER_APP_DATA_DIR": str(self.root), ENV_FLAG: "1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.run_ = SimpleNamespace(id="run-1")
        self.listener = CorpusListener()

    def finish(self, steps, status="completed", outcome="done"):
        self.listener.on_created(self.run_, SimpleNamespace(installed_apps=None))
        for number, labels in enumerate(steps):
            snapshot = screen(*labels)
            self.listener.on_step(self.run_, step(skeleton.capture(snapshot, snapshot.elements[0]), number))
        self.listener.on_finished(self.run_, {"status": status, "outcome": outcome}, {})

    def stored(self):
        return list(store.read())

    def test_off_by_default_writes_nothing(self):
        self.assertFalse(consent.on())
        self.finish([["General", "About"]])
        self.assertFalse((self.root / "corpus").exists())
        consent.set_on(True)
        with mock.patch.dict(os.environ, {ENV_FLAG: "0"}):
            self.finish([["General"]])
        self.assertEqual(self.stored(), [])

    def test_old_text_version_is_off(self):
        consent.set_on(True)
        self.assertTrue(consent.on())
        with mock.patch.object(consent, "TEXT_VERSION", 2):
            self.assertFalse(consent.on())

    def test_opted_in_done_run_writes_one_record_per_step(self):
        consent.set_on(True)
        self.finish([["General", "About"], ["Name", "Model Name"]])
        records = self.stored()
        self.assertEqual([r["step"] for r in records], [0, 1])
        first = records[0]
        self.assertEqual(first["app"], "com.apple.Preferences")
        self.assertEqual(first["screen"]["nodes"][0], ["Button", hashlib.sha1(b"general").hexdigest()[:10]])
        self.assertEqual(first["action"], {"op": "TAP", "typed": False, "target": {
            "i": 0, "role": "Button", "label": first["screen"]["nodes"][0][1]}})
        self.assertNotEqual(first["run"], "run-1")
        self.assertEqual(len({r["run"] for r in records}), 1)
        self.assertEqual(store.stats()["records"], 2)

    def test_unfinished_redirected_and_denied_runs_write_nothing(self):
        consent.set_on(True)
        self.finish([["General"]], outcome="blocked")
        self.finish([["General"]], status="stopped")
        self.assertEqual(self.stored(), [])
        self.listener.on_created(self.run_, None)
        snapshot = screen("General")
        for record in (step(skeleton.capture(snapshot, None), 0, approval="redirected"),
                       step(skeleton.capture(snapshot, None), 1, app="com.apple.Passwords")):
            self.listener.on_step(self.run_, record)
        self.listener.on_finished(self.run_, {"status": "completed", "outcome": "done"}, {})
        self.assertEqual(self.stored(), [])

    def test_secure_field_screen_is_never_captured(self):
        consent.set_on(True)
        self.assertIsNone(skeleton.capture(screen("Password", role="SecureTextField"), None))

    def test_labels_with_personal_text_never_appear(self):
        consent.set_on(True)
        labels = ["sam.lee@example.com", "+1 (415) 555-0134", "@samlee_99", "https://example.com/u/sam",
                  "Sam Lee", "Order 1234567", "Hi Sam, are we still on for dinner tonight? Bring the dog."]
        self.finish([labels])
        text = (self.root / "corpus" / "records.jsonl").read_text()
        for label in labels:
            self.assertNotIn(label, text)
            self.assertNotIn(hashlib.sha1(label.encode()).hexdigest()[:10], text)
        nodes = [n[1] for n in json.loads(text.splitlines()[0])["screen"]["nodes"]]
        self.assertEqual(nodes[:4], ["~email", "~number", "~handle", "~link"])
        self.assertIn(nodes[5], ("~number", "~secret"))
        self.assertEqual(nodes[6], "~long")
        self.assertNotIn("Sam", text)

    def test_digit_runs_fold(self):
        self.assertEqual(skeleton.label_token("3 new messages"), skeleton.label_token("5 new messages"))

    def test_sanitize_refuses_unshaped_records(self):
        consent.set_on(True)
        self.finish([["General"]])
        good = self.stored()[0]
        bad = json.loads(json.dumps(good))
        bad["screen"]["nodes"][0][1] = "sam.lee@example.com"
        self.assertIsNone(store.sanitize(bad))
        bad = json.loads(json.dumps(good))
        bad["app"] = "password is hunter2!"
        self.assertIsNone(store.sanitize(bad))
        self.assertEqual(store.append([bad]), 0)

    def test_rotation_keeps_the_cap(self):
        consent.set_on(True)
        self.finish([["General"]])
        record = self.stored()[0]
        with mock.patch.object(store, "FILE_BYTES", 600):
            for _ in range(20):
                store.append([record])
        files = list((self.root / "corpus").glob("records*.jsonl"))
        self.assertEqual(sorted(p.name for p in files), ["records.1.jsonl", "records.2.jsonl", "records.jsonl"])
        self.assertTrue(all(p.stat().st_size <= 600 for p in files))
        self.assertEqual((self.root / "corpus" / "records.jsonl").stat().st_mode & 0o777, 0o600)

    def test_export_writes_a_new_masked_file_and_refuses_to_overwrite(self):
        consent.set_on(True)
        self.finish([["General"]])
        out = self.root / "bundle.jsonl"
        self.assertEqual(store.export(out), 1)
        header, record = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual((header["records"], record["v"]), (1, 1))
        with self.assertRaises(store.CorpusError):
            store.export(out)

    def test_command_exists_only_with_the_flag(self):
        hooks = SimpleNamespace(cli=[])
        with mock.patch.dict(os.environ, {ENV_FLAG: ""}):
            install(hooks)
        self.assertEqual(hooks.cli, [])
        install(hooks)
        self.assertEqual(len(hooks.cli), 1)

    def test_cli_on_needs_an_answer(self):
        from mobile_agent.corpus import cli
        args = SimpleNamespace(command="corpus", corpus_command="on", yes=False, json=False)
        with mock.patch("sys.stdin.isatty", return_value=False), mock.patch("builtins.print"):
            self.assertEqual(cli.run(args, print), 1)
        self.assertFalse(consent.on())
        args.yes = True
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cli.run(args, print), 0)
        self.assertIn(consent.TITLE, printed.call_args_list[0].args[0])
        self.assertTrue(consent.on())
        self.assertIsNone(cli.run(SimpleNamespace(command="run"), print))


if __name__ == "__main__":
    unittest.main()
