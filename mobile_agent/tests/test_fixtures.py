"""Fixtures (verify/fixtures.py, devkit/fixtures_cli.py, devkit/fixtures_routes.py): `.mobster/fixtures/` flattened
into a check, seeded onto a simulator, refused on a real iPhone, listed by the CLI and the API. Recorded project files
and a fake simctl; offline: no simulator, phone or network."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import api_routes
from mobile_agent.api_errors import APIError
from mobile_agent.devkit import fixtures_cli, fixtures_routes
from mobile_agent.sim import apps
from mobile_agent.tests.test_a11y_levels import CLEAN
from mobile_agent.tests.test_verify_runner import BUNDLE, TARGET, Driver, Manager, RunnerCase
from mobile_agent.verify import fixtures, runner
from mobile_agent.verify.checks import CheckError, check_from_dict, check_to_dict, dump_check
from mobile_agent.verify.runner import VerifyRun


class Project(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-fixtures-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.folder = self.root / ".mobster" / "fixtures"
        self.folder.mkdir(parents=True)

    def fixture(self, name, text):
        (self.folder / f"{name}.yaml").write_text(text, encoding="utf-8")

    def sample(self):
        (self.folder / "files").mkdir(exist_ok=True)
        (self.folder / "files" / "cart.json").write_text('{"items": 2}')
        (self.folder / "files" / "media").mkdir(exist_ok=True)
        (self.folder / "files" / "media" / "a.txt").write_text("a")
        self.fixture("clean", "reset: data\nkeychain: reset\n")
        self.fixture("signed-in", "extends: clean\nlaunch:\n  args: [-UITests]\n  env: {API: staging}\n"
                                   "steps:\n  - type ${TEST_EMAIL} into Email\n  - tap Sign in\n")
        self.fixture("logged-in-cart", "extends: [signed-in]\nlaunch:\n  args: [-Cart]\n  env: {API: local, MODE: x}\n"
                                        "files:\n  - from: files/cart.json\n    to: Documents/cart.json\n"
                                        "  - from: files/media\n    to: Library/media\n"
                                        "pasteboard: SAVE10\nsteps: [open the cart]\n")


class ResolveTests(Project):
    def test_a_fixture_name_cannot_read_outside_the_fixture_folder(self):
        (self.root / "outside.yaml").write_text("reset: data\n")
        with self.assertRaises(fixtures.FixtureError):
            fixtures.resolve(self.root, ["../../outside"])

    def test_fixtures_compose_in_order(self):
        self.sample()
        got = fixtures.resolve(self.root, ["logged-in-cart"])
        self.assertEqual((got.names, got.reset, got.keychain, got.pasteboard),
                         (("clean", "signed-in", "logged-in-cart"), "data", True, "SAVE10"))
        self.assertEqual((got.args, got.env), (("-UITests", "-Cart"), {"API": "local", "MODE": "x"}))
        self.assertEqual(got.steps, ("type ${TEST_EMAIL} into Email", "tap Sign in", "open the cart"))
        self.assertEqual([dest for _, dest in got.files], ["Documents/cart.json", "Library/media"])
        self.assertEqual(got.changes_state, ["a reset", "seed files", "a keychain reset", "a pasteboard"])

    def test_a_later_fixture_overrides_a_file_and_a_fixture_is_applied_once(self):
        self.sample()
        self.fixture("other", "files:\n  - from: files/media/a.txt\n    to: Documents/cart.json\nreset: none\n")
        got = fixtures.resolve(self.root, ["logged-in-cart", "other", "clean"])
        self.assertEqual(got.names, ("clean", "signed-in", "logged-in-cart", "other"))
        self.assertEqual(dict((dest, src.name) for src, dest in got.files)["Documents/cart.json"], "a.txt")
        self.assertEqual(got.reset, "none")

    def test_a_circle_a_missing_fixture_a_deep_chain_and_bad_keys_are_refused_with_a_sentence(self):
        self.fixture("a", "extends: b\n")
        self.fixture("b", "extends: a\n")
        for wanted, fragment in ((["a"], "in a circle: a → b → a"), (["nope"], "There's no fixture called nope")):
            with self.subTest(wanted=wanted), self.assertRaises(fixtures.FixtureError) as caught:
                fixtures.resolve(self.root, wanted)
            self.assertIn(fragment, str(caught.exception))
        for number in range(fixtures.DEPTH + 2):
            self.fixture(f"d{number}", f"extends: d{number + 1}\n" if number < fixtures.DEPTH + 1 else "")
        with self.assertRaises(fixtures.FixtureError) as caught:
            fixtures.resolve(self.root, ["d0"])
        self.assertIn("deep", str(caught.exception))
        for text, fragment in (("colour: red\n", "isn't a fixture key"), ("reset: wipe\n", "reset is one of"),
                               ("keychain: yes\n", "keychain is reset"),
                               ("launch: {x: 1}\n", "launch holds args and env"),
                               ("files:\n  - from: ../x\n    to: Documents/x\n", "inside .mobster/fixtures/"),
                               ("files:\n  - from: files\n    to: /etc/x\n", "inside the app's container"),
                               ("files:\n  - from: files\n    to: ../x\n", "inside the app's container"),
                               ("steps: 3\n", "steps"), ("a: [\n", "isn't valid YAML")):
            with self.subTest(text=text):
                (self.folder / "files").mkdir(exist_ok=True)
                self.fixture("bad", text)
                with self.assertRaises(fixtures.FixtureError) as caught:
                    fixtures.resolve(self.root, ["bad"])
                self.assertIn(fragment, str(caught.exception))

    def test_a_file_source_that_leaves_the_fixtures_folder_through_a_link_is_refused(self):
        outside = self.root / "secret.txt"
        outside.write_text("x")
        (self.folder / "link.txt").symlink_to(outside)
        self.fixture("bad", "files:\n  - from: link.txt\n    to: Documents/x\n")
        with self.assertRaises(fixtures.FixtureError):
            fixtures.resolve(self.root, ["bad"])


class CheckTests(Project):
    def make(self, **extra):
        return check_from_dict({"app": {"bundle": "com.example.shop"}, "expect": [{"text": "Cart"}], **extra},
                               base_dir=self.root)

    def test_the_fixture_key_names_fixtures_that_exist(self):
        self.sample()
        self.assertEqual(self.make(fixture="signed-in").fixture, ("signed-in",))
        self.assertEqual(self.make(fixture=["clean", "signed-in", "clean"]).fixture, ("clean", "signed-in"))
        for value, fragment in (("nope", "isn't in .mobster/fixtures/"), (3, "name of a fixture"),
                                ([], "name of a fixture"),
                                ("Bad Name", "name of a fixture")):
            with self.subTest(value=value), self.assertRaises(CheckError) as caught:
                self.make(fixture=value)
            self.assertIn(fragment, str(caught.exception))

    def test_flattening_puts_the_fixture_first_and_the_checks_own_choices_over_it(self):
        self.sample()
        check = self.make(fixture="logged-in-cart", steps=["tap Checkout"],
                          launch={"args": ["-Mine"], "env": {"API": "mine"}})
        merged = fixtures.with_fixtures(check, self.root)
        self.assertEqual((merged.reset, merged.launch_args, merged.launch_env["API"], merged.launch_env["MODE"]),
                         ("data", ("-UITests", "-Cart", "-Mine"), "mine", "x"))
        self.assertEqual(merged.steps[-1], "tap Checkout")
        self.assertEqual(merged.steps[0], "type ${TEST_EMAIL} into Email")
        self.assertTrue(merged.seed.keychain and merged.seed.pasteboard == "SAVE10" and len(merged.seed.files) == 2)
        own = fixtures.with_fixtures(self.make(fixture="logged-in-cart", reset="reinstall"), self.root)
        self.assertEqual(own.reset, "reinstall")
        self.assertIs(fixtures.with_fixtures(merged, self.root), merged)

    def test_a_saved_copy_names_the_fixture_and_does_not_repeat_it(self):
        self.sample()
        merged = fixtures.with_fixtures(self.make(fixture="signed-in", steps=["tap Checkout"]), self.root)
        text = dump_check(merged)
        self.assertIn("fixture:\n- signed-in", text)
        self.assertNotIn("TEST_EMAIL", text)
        self.assertNotIn("-UITests", text)

    def test_refresh_is_private_run_state_not_a_saved_check_field(self):
        check = self.make()
        self.assertFalse(check.a11y_refresh)
        self.assertNotIn("a11y_refresh", check_to_dict(check))
        with self.assertRaises(CheckError):
            self.make(a11y_refresh=True)


class SeedTests(Project):
    def setUp(self):
        super().setUp()
        self.sample()
        self.sim = self.root / "sim"
        self.container = self.sim / "data" / "Containers" / "Data" / "Application" / "X"
        for name in ("Documents", "Library", "tmp"):
            (self.container / name).mkdir(parents=True)
        patcher = mock.patch.object(apps, "device_folder", lambda udid: self.sim)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def run_simctl(self, args):
        self.calls.append(list(args))
        if args[0] == "get_app_container":
            return 0, str(self.container)
        return 0, ""

    def seed(self, **kwargs):
        got = fixtures.resolve(self.root, ["logged-in-cart"])
        return fixtures.Seed(**{"files": got.files, "keychain": got.keychain, "pasteboard": got.pasteboard, **kwargs})

    def test_it_resets_the_keychain_copies_files_and_folders_and_sets_the_pasteboard(self):
        board = []
        did = fixtures.seed_simulator(self.seed(), udid="U1", bundle="com.example.shop", run=self.run_simctl,
                                      pbcopy=lambda udid, text: board.append((udid, text)))
        self.assertEqual(did, ["keychain reset", "2 files copied", "pasteboard set"])
        self.assertEqual((self.container / "Documents" / "cart.json").read_text(), '{"items": 2}')
        self.assertEqual((self.container / "Library" / "media" / "a.txt").read_text(), "a")
        self.assertEqual((board, self.calls[0]), ([("U1", "SAVE10")], ["keychain", "U1", "reset"]))

    def test_nothing_is_copied_outside_the_container_or_through_a_link(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.container / "Documents" / "trap").symlink_to(outside)
        for dest, fragment in (("Documents/trap/x.json", "symbolic link"),):
            with self.subTest(dest=dest), self.assertRaises(fixtures.FixtureError) as caught:
                fixtures.seed_simulator(fixtures.Seed(files=((self.folder / "files" / "cart.json", dest),)),
                                        udid="U1", bundle="b", run=self.run_simctl)
            self.assertIn(fragment, str(caught.exception))
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_symlink_in_a_seed_folder_is_refused_before_copying_any_file(self):
        got = fixtures.resolve(self.root, ["logged-in-cart"])
        folder = next(source for source, _ in got.files if source.is_dir())
        (folder / "link.txt").symlink_to(folder / "a.txt")
        with self.assertRaisesRegex(fixtures.FixtureError, "symbolic link"):
            fixtures.seed_simulator(self.seed(keychain=False, pasteboard=""), udid="U1", bundle="b",
                                    run=self.run_simctl)
        self.assertFalse((self.container / "Documents" / "cart.json").exists())
        self.assertFalse((self.container / "Library" / "media").exists())

    def test_a_folder_seed_refuses_nested_destination_links_before_copying_any_file(self):
        outside = self.root / "outside.txt"
        outside.write_text("keep me")
        (self.container / "Library" / "media").mkdir()
        (self.container / "Library" / "media" / "a.txt").symlink_to(outside)
        with self.assertRaises(fixtures.FixtureError):
            fixtures.seed_simulator(self.seed(keychain=False, pasteboard=""), udid="U1", bundle="b",
                                    run=self.run_simctl)
        self.assertEqual(outside.read_text(), "keep me")
        self.assertFalse((self.container / "Documents" / "cart.json").exists())

    def test_a_container_outside_the_simulator_folder_is_refused(self):
        elsewhere = self.root / "elsewhere"
        (elsewhere / "Documents").mkdir(parents=True)
        run = lambda args: (0, str(elsewhere))
        with self.assertRaises(fixtures.FixtureError) as caught:
            fixtures.seed_simulator(self.seed(keychain=False, pasteboard=""), udid="U1", bundle="b", run=run)
        self.assertIn("resolves outside this simulator's folder", str(caught.exception))
        self.assertFalse((elsewhere / "Documents" / "cart.json").exists())

    def test_an_app_that_is_not_installed_and_a_keychain_that_will_not_reset_are_refused(self):
        with self.assertRaises(fixtures.FixtureError) as caught:
            fixtures.seed_simulator(self.seed(keychain=False, pasteboard=""), udid="U1", bundle="b",
                                    run=lambda args: (1, "No such app"))
        self.assertIn("isn't installed", str(caught.exception))
        with self.assertRaises(fixtures.FixtureError):
            fixtures.seed_simulator(fixtures.Seed(keychain=True), udid="U1", bundle="b", run=lambda args: (1, "boom"))

    def test_too_many_bytes_are_refused_before_anything_is_copied(self):
        big = self.folder / "big.bin"
        big.write_bytes(b"0" * 1024)
        with mock.patch.object(fixtures, "BYTES_LIMIT", 100), self.assertRaises(fixtures.FixtureError) as caught:
            fixtures.seed_simulator(fixtures.Seed(files=((big, "Documents/big.bin"),)), udid="U1", bundle="b",
                                    run=self.run_simctl)
        self.assertIn("limit", str(caught.exception))
        self.assertFalse((self.container / "Documents" / "big.bin").exists())


class RunTests(RunnerCase):
    def setUp(self):
        super().setUp()
        self.folder = self.root / ".mobster" / "fixtures"
        self.folder.mkdir(parents=True)
        (self.folder / "files").mkdir()
        (self.folder / "files" / "cart.json").write_text("{}")
        (self.folder / "cart.yaml").write_text("reset: data\nlaunch: {args: [-Cart]}\nkeychain: reset\n"
                                               "files:\n  - from: files/cart.json\n    to: Documents/cart.json\n")
        (self.folder / "args-only.yaml").write_text("launch: {args: [-Cart]}\nsteps: [tap Sign in]\n")
        self.sim = self.root / "sim"
        self.container = self.sim / "container"
        for name in ("Documents", "Library", "tmp"):
            (self.container / name).mkdir(parents=True)
        patcher = mock.patch.object(apps, "device_folder", lambda udid: self.sim)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.simctl_calls = []

    def simctl(self, args):
        self.simctl_calls.append(list(args))
        return (0, str(self.container)) if args[0] == "get_app_container" else (0, "large" if len(args) == 3 else "")

    def run_check(self, manager, **extra):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Account"}], **extra},
                                base_dir=self.root)
        driver = Driver([CLEAN])
        self.watch(driver, manager)
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager, simctl=self.simctl)
        with mock.patch.object(runner, "build_driver", lambda *a: driver):
            try:
                run.prepare()
            except runner.CouldntRun:
                pass
        return run.finish()

    def test_a_simulator_run_resets_seeds_and_launches_with_the_fixtures_args(self):
        manager = Manager()
        outcome = self.run_check(manager, fixture="cart", launch={"args": ["-Mine"]})
        self.assertEqual(outcome["verdict"], "passed")
        self.assertIn(("reset", BUNDLE, "data", None), manager.calls)
        launch = next(call for call in manager.calls if call[0] == "launch")
        self.assertEqual(launch[2], ("-Cart", "-Mine"))
        self.assertEqual((self.container / "Documents" / "cart.json").read_text(), "{}")
        self.assertIn(["keychain", TARGET.udid, "reset"], self.simctl_calls)
        self.assertEqual(manager.names().index("reset") < manager.names().index("launch"), True)

    def test_a_real_iphone_refuses_a_fixture_that_changes_it_before_touching_anything(self):
        phone = Manager()
        phone.real_device = True
        outcome = self.run_check(phone, fixture="cart")
        self.assertEqual((outcome["verdict"], outcome["reason"]["class"]), ("couldnt_run", "environment"))
        self.assertIn("fixtures do that on simulators only", outcome["reason"]["message"])
        self.assertEqual(phone.names(), [])
        self.assertFalse((self.container / "Documents" / "cart.json").exists())
        self.assertEqual(self.simctl_calls, [])

    def test_a_real_iphone_takes_a_fixture_of_args_and_steps(self):
        phone = Manager()
        phone.real_device = True
        outcome = self.run_check(phone, fixture="args-only")
        self.assertNotEqual(outcome["reason"]["class"] if outcome["verdict"] == "couldnt_run" else "", "environment")
        self.assertEqual(self.simctl_calls, [])

    def test_a_missing_fixture_file_is_couldnt_run_with_the_list(self):
        (self.folder / "cart.yaml").unlink()
        manager = Manager()
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Account"}], "fixture": "args-only"},
                                base_dir=self.root)
        (self.folder / "args-only.yaml").unlink()
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager, simctl=self.simctl)
        with self.assertRaises(runner.CouldntRun) as caught:
            run.prepare()
        self.assertIn("There's no fixture called args-only", caught.exception.message)
        self.assertEqual(manager.names(), [])

    def test_direct_verify_uses_fixture_steps_for_mode_and_key_admission(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "fixture": "args-only"}, base_dir=self.root)
        manager = Manager()
        with mock.patch("mobile_agent.engines.smart_key", return_value=None):
            outcome = runner.verify(check, runs_dir=self.runs, manager=manager)
        self.assertEqual((outcome["verdict"], outcome["reason"]["class"]), ("couldnt_run", "usage"))
        self.assertIn("These steps need Smart", outcome["reason"]["message"])
        self.assertEqual(manager.names(), [])

        outcome = runner.verify(check, mode="keyless", runs_dir=self.runs, manager=manager)
        self.assertEqual((outcome["verdict"], outcome["reason"]["class"]), ("couldnt_run", "usage"))
        self.assertIn("key-less run with steps", outcome["reason"]["message"])
        self.assertEqual(manager.names(), [])

    def test_direct_verify_refuses_a_changed_fixture_before_acquisition(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "fixture": "args-only"}, base_dir=self.root)
        (self.folder / "args-only.yaml").write_text("steps: [\n", encoding="utf-8")
        manager = Manager()
        outcome = runner.verify(check, runs_dir=self.runs, manager=manager)
        self.assertEqual((outcome["verdict"], outcome["reason"]["class"]), ("couldnt_run", "usage"))
        self.assertEqual(manager.names(), [])


class SurfaceTests(Project):
    def run_cli(self, *argv):
        import argparse
        parser = argparse.ArgumentParser(prog="mobster fixtures")
        fixtures_cli.add_arguments(parser, None)
        args = parser.parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fixtures_cli.run(args, cwd=self.root)
        return code, out.getvalue(), err.getvalue()

    def test_list_says_what_each_fixture_sets_up(self):
        self.sample()
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("clean: reset data, keychain reset  [simulators only]", out)
        self.assertRegex(out,
                         r"logged-in-cart: reset data, 2 files, keychain reset, pasteboard, 2 args, 2 env, 3 steps  "
                         r"\(builds on clean, signed-in\)")
        _, raw, _ = self.run_cli("list", "--json")
        found = {item["name"]: item for item in json.loads(raw)["fixtures"]}
        self.assertEqual((found["signed-in"]["destructive"], found["logged-in-cart"]["pasteboard"]), (True, True))
        self.assertNotIn("SAVE10", raw)
        self.assertEqual(self.run_cli("list", "--project", str(self.root / "elsewhere"))[0], 0)

    def test_list_in_a_project_without_fixtures_says_how_to_start(self):
        shutil.rmtree(self.folder)
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("Add .mobster/fixtures/NAME.yaml", out)

    def test_apply_to_a_phone_refuses_what_changes_it_and_does_nothing_for_launch_only_fixtures(self):
        self.sample()
        code, _, err = self.run_cli("apply", "clean", "--bundle", "com.example.shop", "--device", "Test iPhone")
        self.assertEqual(code, 3)
        self.assertIn("fixtures do that on simulators only", err)
        self.fixture("args-only", "launch: {args: [-X]}\n")
        code, out, _ = self.run_cli("apply", "args-only", "--bundle", "com.example.shop", "--device", "Test iPhone")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to put on the phone now", out)

    def test_apply_to_a_simulator_resets_and_seeds_it(self):
        self.sample()
        sim = self.root / "sim"
        container = sim / "container"
        for name in ("Documents", "Library", "tmp"):
            (container / name).mkdir(parents=True)
        calls = []

        class Manager:
            def __init__(self, progress=None):
                pass

            def acquire(self, device=None, timeout=600):
                return Lease()

            def reset(self, target, bundle, level, app_path=None):
                calls.append(("reset", bundle, level))

        class Lease:
            def __enter__(self):
                return SimpleNamespace(udid="U1", name="iPhone 17 Pro")

            def __exit__(self, *exc):
                return False

        from mobile_agent.verify import conditions
        with mock.patch("mobile_agent.sim.api.SimulatorManager", Manager), \
                mock.patch.object(apps, "device_folder", lambda udid: sim), \
                mock.patch.object(conditions, "simctl", lambda args: (0, str(container))), \
                mock.patch.object(fixtures, "_pbcopy", lambda udid, text: calls.append(("pbcopy", text))):
            code, out, _ = self.run_cli("apply", "logged-in-cart", "--bundle", "com.example.shop")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("reset", "com.example.shop", "data"), ("pbcopy", "SAVE10")])
        self.assertIn("Applied clean, signed-in, logged-in-cart to com.example.shop on iPhone 17 Pro: reset data, "
                      "keychain reset, 2 files copied, pasteboard set.", out)
        self.assertTrue((container / "Documents" / "cart.json").exists())

    def test_the_api_lists_fixtures_and_refuses_a_bad_project_or_fixture(self):
        self.sample()
        if api_routes.match("GET", "/api/tests/fixtures") is None:
            fixtures_routes.register_routes()

        def call(query):
            route, params = api_routes.match("GET", "/api/tests/fixtures")
            return route.handler(api_routes.Request(runtime=None, method="GET", path="/api/tests/fixtures",
                                                    params=params,
                                                    query=query, headers={}, body=None, content_type=None, origin="app",
                                                    app_session=True))
        names = [item["name"] for item in call({"project": [str(self.root)]}).value["fixtures"]]
        self.assertEqual(names, ["clean", "logged-in-cart", "signed-in"])
        with self.assertRaises(APIError) as caught:
            call({"project": ["relative"]})
        self.assertEqual(caught.exception.code, "bad_project")
        self.fixture("bad", "colour: red\n")
        with self.assertRaises(APIError) as caught:
            call({"project": [str(self.root)]})
        self.assertEqual((caught.exception.code, caught.exception.status), ("bad_fixture", 400))


if __name__ == "__main__":
    unittest.main()
