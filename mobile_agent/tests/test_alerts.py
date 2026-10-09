"""Alerts (alerts.py) and keeping the Mac awake (workflow_scheduler.KeepAwake): https only, never the local network,
no answer in the payload, a slow or failing webhook never fails a run, only scheduled runs alert, and caffeinate
held around a due schedule. Fakes only: no network, no caffeinate."""

from datetime import datetime, timezone
import io
import json
import socket
import contextlib
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from mobile_agent import alerts
from mobile_agent.alerts import AlertError, Alerts, deliver, payload, shape, validate_url
from mobile_agent.journal import Journal
from mobile_agent.tests.test_workflows import FakeRuntime
from mobile_agent.workflow_scheduler import KeepAwake, WorkflowScheduler, awake_needed
from mobile_agent.workflows import Workflows

PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def resolver(*addresses):
    return lambda host, port, type=None: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]


class Poster:
    def __init__(self, *answers):
        self.answers, self.posts = list(answers) or [200], []

    def __call__(self, host, address, port, path, body, headers, timeout):
        self.posts.append({"host": host, "address": address, "path": path, "body": body, "headers": headers})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class AddressTests(unittest.TestCase):
    def test_only_https_and_never_the_local_network(self):
        self.assertEqual(validate_url(" https://hooks.slack.com/services/T/B/x "), "https://hooks.slack.com/services/T/B/x")
        for url in ("http://hooks.slack.com/x", "https://localhost/x", "https://127.0.0.1/x", "https://10.0.0.8/x",
                    "https://192.168.1.4/hook", "https://169.254.169.254/latest", "https://[::1]/x",
                    "https://printer.local/x", "https://router/x", "https://user:pw@example.com/x", "ftp://x.com",
                    "", "https://example.com:99999/x", "https://100.101.102.103/x", "https://198.18.0.1/x",
                    "https://[fd7a:115c:a1e0::1]/x"):
            with self.subTest(url=url), self.assertRaises(AlertError):
                validate_url(url)

    def test_a_name_that_resolves_to_a_private_address_is_never_posted_to(self):
        post = Poster()
        self.assertFalse(deliver("https://hooks.example.com/x", payload("Morning", "blocked"), post=post,
                                 resolve=resolver("10.1.2.3"), sleep=lambda s: None))
        self.assertFalse(deliver("https://hooks.example.com/x", payload("Morning", "blocked"), post=post,
                                 resolve=resolver("93.184.216.34", "127.0.0.1"), sleep=lambda s: None))
        # Shared address space (Tailscale, carrier NAT) is not the internet either.
        self.assertFalse(deliver("https://hooks.example.com/x", payload("Morning", "blocked"), post=post,
                                 resolve=resolver("100.64.0.7"), sleep=lambda s: None))
        self.assertEqual(post.posts, [])

    def test_it_connects_to_the_address_it_checked(self):
        post = Poster()
        self.assertTrue(deliver("https://hooks.example.com/a/b?c=1", payload("Morning", "blocked"), post=post,
                                resolve=resolver("93.184.216.34")))
        self.assertEqual((post.posts[0]["host"], post.posts[0]["address"], post.posts[0]["path"]),
                         ("hooks.example.com", "93.184.216.34", "/a/b?c=1"))


class PayloadTests(unittest.TestCase):
    def test_the_payload_never_carries_an_answer(self):
        data = payload("Morning summary", "max_steps", "run123", at=0)
        self.assertEqual(set(data), {"workflow", "status", "reason", "runId", "at"})
        self.assertEqual(data["reason"], "It ran out of steps.")
        self.assertEqual(data["at"], "1970-01-01T00:00:00+00:00")
        self.assertEqual(payload("x", "blocked", code="phone_locked")["reason"], "Your iPhone was locked.")
        self.assertEqual(payload("x", "blocked", code="anything else")["reason"], "It couldn't finish the task.")

    def test_each_service_gets_its_own_shape(self):
        data = payload("Morning summary", "blocked", "run123")
        slack, _ = shape("https://hooks.slack.com/services/x", data)
        self.assertEqual(set(json.loads(slack)), {"text"})
        discord, _ = shape("https://discord.com/api/webhooks/1/x", data)
        self.assertEqual(set(json.loads(discord)), {"content"})
        ntfy, headers = shape("https://ntfy.sh/my-topic", data)
        self.assertTrue(ntfy.decode().startswith("Mobster: “Morning summary” didn't finish (blocked)."))
        self.assertEqual(headers["Title"], "Mobster: Morning summary")
        generic, headers = shape("https://example.com/hook", data)
        self.assertEqual(set(json.loads(generic)), {"workflow", "status", "reason", "runId", "at", "text"})
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_a_timeout_retries_once_and_never_raises(self):
        post = Poster(socket.timeout("slow"), socket.timeout("slow"))
        self.assertFalse(deliver("https://example.com/hook", payload("x", "blocked"), post=post,
                                 resolve=resolver("93.184.216.34"), sleep=lambda s: None))
        self.assertEqual(len(post.posts), 2)
        post = Poster(500, 200)
        self.assertTrue(deliver("https://example.com/hook", payload("x", "blocked"), post=post,
                                resolve=resolver("93.184.216.34"), sleep=lambda s: None))
        post = Poster(404)
        self.assertFalse(deliver("https://example.com/hook", payload("x", "blocked"), post=post,
                                 resolve=resolver("93.184.216.34"), sleep=lambda s: None))
        self.assertEqual(len(post.posts), 1)

    def test_alerts_are_off_without_an_address_and_quiet_for_good_endings(self):
        post = Poster()
        quiet = Alerts({}, post=post, resolve=resolver("93.184.216.34"), background=False)
        self.assertFalse(quiet.run_finished("x", "blocked", "r1"))
        on = Alerts({alerts.ENV: "https://example.com/hook"}, post=post, resolve=resolver("93.184.216.34"),
                    background=False)
        for status in ("completed", "stopped", "approval_denied"):
            self.assertFalse(on.run_finished("x", status, "r1"))
        self.assertTrue(on.run_finished("x", "approval_timeout", "r1"))
        self.assertEqual(len(post.posts), 1)
        bad = Alerts({alerts.ENV: "http://example.com/hook"}, post=post, background=False)
        self.assertFalse(bad.run_finished("x", "blocked", "r1"))

    def test_a_hanging_webhook_never_holds_the_caller(self):
        release = threading.Event()

        def hang(*args):
            release.wait(5)
            return 200
        on = Alerts({alerts.ENV: "https://example.com/hook"}, post=hang, resolve=resolver("93.184.216.34"))
        started = time.monotonic()
        self.assertTrue(on.run_finished("x", "blocked", "r1"))
        self.assertLess(time.monotonic() - started, .5)
        release.set()

    def test_the_test_command(self):
        out = io.StringIO()
        args = SimpleNamespace(url="https://example.com/hook", json=True)
        with contextlib.redirect_stdout(out):
            code = alerts.run(args, post=Poster(), resolve=resolver("93.184.216.34"))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(alerts.run(SimpleNamespace(url="https://example.com/hook", json=False),
                                        post=Poster(500), resolve=resolver("93.184.216.34")), 1)
            self.assertEqual(alerts.run(SimpleNamespace(url="http://example.com/hook", json=False)), 2)


class RecordingAlerts:
    def __init__(self):
        self.finished, self.waiting = [], []

    def run_finished(self, workflow, status, run_id, code=None):
        self.finished.append((workflow, status, run_id, code))
        return True

    def approval_waiting(self, workflow, run_id):
        self.waiting.append((workflow, run_id))
        return True


class WorkflowAlertTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mobster-alerts-test-")
        self.journal = Journal(Path(self.directory.name) / "runs.sqlite3")
        self.runtime = FakeRuntime(self.journal)
        self.time = datetime(2026, 9, 19, tzinfo=timezone.utc).timestamp()
        self.alerts = RecordingAlerts()
        self.workflows = Workflows(self.runtime, lambda: self.time, alerts=self.alerts)

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def scheduled(self, name="Morning summary"):
        workflow = self.workflows.create({"name": name, "appId": "settings", "goal": "Read iOS version"})
        return self.workflows.update(workflow["id"], {"revision": 1, "cron": "*/30 * * * *", "timezone": "UTC",
                                                      "enabled": True})

    def due(self, workflow):
        self.time = workflow["nextAt"] / 1000
        self.workflows.tick()
        return self.runtime.runs[sorted(self.runtime.runs)[-1]]

    def test_a_scheduled_run_that_fails_alerts_once_and_one_that_completes_does_not(self):
        workflow = self.scheduled()
        run = self.due(workflow)
        run.status, run.finished_at, run.summary = "max_steps", self.time, {"answer": "secret answer"}
        self.workflows.list()
        self.workflows.list()
        self.assertEqual(self.alerts.finished, [("Morning summary", "max_steps", run.id, None)])
        workflow = self.workflows.get(workflow["id"])
        run = self.due(workflow)
        run.status, run.finished_at = "completed", self.time
        self.workflows.list()
        self.assertEqual(len(self.alerts.finished), 2)
        self.assertEqual(self.alerts.finished[-1][1], "completed")  # Alerts.run_finished keeps it quiet

    def test_a_manual_run_never_alerts(self):
        workflow = self.workflows.create({"name": "By hand", "appId": "settings", "goal": "Read iOS version"})
        run, _ = self.workflows.run(workflow["id"], "key-1")
        run.status, run.finished_at = "blocked", self.time
        self.workflows.list()
        self.assertEqual(self.alerts.finished, [])

    def test_an_approval_waiting_a_minute_alerts_once(self):
        workflow = self.scheduled()
        run = self.due(workflow)
        run.approval = {"id": "a1", "requestedAt": self.time * 1000}
        self.time += 30
        self.workflows.tick()
        self.assertEqual(self.alerts.waiting, [])
        self.time += 31
        self.workflows.tick()
        self.workflows.tick()
        self.assertEqual(self.alerts.waiting, [("Morning summary", run.id)])

    def test_an_alert_that_raises_never_breaks_scheduling(self):
        broken = mock.Mock()
        broken.run_finished.side_effect = RuntimeError("webhook exploded")
        self.workflows.alerts = broken
        workflow = self.scheduled()
        run = self.due(workflow)
        run.status, run.finished_at = "blocked", self.time
        self.assertEqual(self.workflows.list()[0]["lastOutcome"], "blocked")

    def test_the_default_alerts_read_only_the_environment(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(Workflows(self.runtime).alerts.url())


class ExampleWorkflowTests(unittest.TestCase):
    setUp, tearDown = WorkflowAlertTests.setUp, WorkflowAlertTests.tearDown

    def test_the_example_recipes_are_valid_saved_tasks_and_start_paused(self):
        folder = Path(__file__).resolve().parents[2] / "examples" / "workflows"
        if not folder.is_dir():
            self.skipTest("the examples are not part of this copy")
        recipes = sorted(folder.glob("*.json"))
        self.assertEqual(len(recipes), 5)
        for recipe in recipes:
            with self.subTest(recipe=recipe.name):
                body = json.loads(recipe.read_text())
                saved = self.workflows.create(body)
                self.assertFalse(saved["enabled"])
                self.assertIsNotNone(saved["cron"])
                self.assertRegex(body["goal"], r"(?i)don't|do not|change nothing|only read")


class Process:
    def __init__(self, argv):
        self.argv, self.ended = argv, False

    def poll(self):
        return 0 if self.ended else None

    def terminate(self):
        self.ended = True

    def wait(self, timeout=None):
        return 0


class KeepAwakeTests(unittest.TestCase):
    def test_caffeinate_is_held_around_a_due_schedule_and_released_after(self):
        started = []

        def spawn(argv):
            started.append(Process(argv))
            return started[-1]
        awake = KeepAwake(spawn=spawn, enabled=True, pid=4242, platform="darwin", exists=lambda path: True)
        now = 1_000_000_000_000
        listed = [{"enabled": True, "nextAt": now + 11 * 60_000, "lastOutcome": None}]
        workflows = SimpleNamespace(list=lambda: listed, now=lambda: now)
        clock = [0.0]
        scheduler = WorkflowScheduler(workflows, keep_awake=awake, clock=lambda: clock[0])
        scheduler._stay_awake()
        self.assertEqual(started, [])  # 11 minutes away: not yet
        listed[0]["nextAt"] = now + 9 * 60_000
        clock[0] += 10
        scheduler._stay_awake()
        self.assertEqual(started[0].argv, ["/usr/bin/caffeinate", "-i", "-w", "4242"])
        clock[0] += 10
        listed[0].update(nextAt=now + 30 * 60_000, lastOutcome="started")  # the run is going
        scheduler._stay_awake()
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0].ended)
        listed[0]["lastOutcome"] = "completed"
        clock[0] += 10
        scheduler._stay_awake()
        self.assertTrue(started[0].ended)
        listed[0]["nextAt"] = now
        clock[0] += 10
        scheduler._stay_awake()
        self.assertEqual(len(started), 2)
        self.assertTrue(scheduler.close())
        self.assertTrue(started[1].ended)

    def test_it_is_off_when_asked_or_off_a_mac(self):
        spawn = mock.Mock()
        for options in ({"enabled": False, "platform": "darwin"}, {"enabled": True, "platform": "linux"}):
            awake = KeepAwake(spawn=spawn, exists=lambda path: True, **options)
            awake.hold()
        spawn.assert_not_called()
        self.assertFalse(awake_needed([{"enabled": False, "nextAt": 0}], 10))
        self.assertTrue(awake_needed([{"enabled": False, "nextAt": None, "lastOutcome": "claimed"}], 10))


if __name__ == "__main__":
    unittest.main()
