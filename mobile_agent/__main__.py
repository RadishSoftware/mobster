import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from . import __version__
from .agent import Agent
from .compose import build_models, build_target_driver, build_visual, close_all
from .extensions import load as load_extensions
from .costs import SpendLedger, usd_to_nanodollars
from .demo import DemoDriver, DemoHelper, DemoModel, screen
from .drivers import resolve_wda_session
from .models import Jev
from .journal import Lease
from .paths import build_dir, source_checkout, user_data_dir
from .state import validate_bundle_id


def output(value):
    try:
        print(json.dumps(value, allow_nan=False), flush=True)
    except BrokenPipeError:
        # The reader went away (`demo | head -1`): stop quietly, not with a traceback on exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)


def default_state_db(args):
    """Where `serve` keeps its journal when --state-db is not given.

    A source checkout keeps it beside the package. An installed copy must not
    (site-packages is shared, may be read-only, and an upgrade deletes it), nor
    may a frozen sidecar (its modules live inside the signed app bundle), so
    both use the data directory.
    """
    if not source_checkout():
        return Path(getattr(args, "data_dir", None) or user_data_dir()) / "state" / "mobster.sqlite3"
    return Path(__file__).parent / ".state" / "mobster.sqlite3"


def build_parser():
    parser = argparse.ArgumentParser(prog="mobster", description="Mobster CLI: Jev-powered iPhone automation")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("build-ocr", help="Compile the standalone Apple Vision helper")
    subs.add_parser("demo", help="Offline fixture replay, not a device benchmark")
    fixture = subs.add_parser("decide-fixture", help="One real Jev decision on a synthetic UI")
    fixture.add_argument("--goal", default="Open Search")
    run = subs.add_parser("run", help="Preview one decision, or execute a bounded task")
    run.add_argument("goal")
    target = run.add_mutually_exclusive_group(required=True)
    target.add_argument("--wda-url", help="WebDriverAgent's URL, e.g. http://127.0.0.1:8100")
    run.add_argument("--session", help="WDA session ID; default: the one WDA is serving, else a new one")
    run.add_argument("--env-file", type=Path,
                     help="KEY=VALUE lines (e.g. TYPESAFE_API_KEY) to load; variables already set win")
    run.add_argument("--execute", action="store_true")
    run.add_argument("--helper", action="store_true")
    run.add_argument("--max-steps", type=int, default=30)
    run.add_argument("--max-seconds", type=float, default=120)
    run.add_argument("--spend-cap-usd", type=float, default=None,
                     help="Stop before further model calls once observed inference spend reaches this USD amount")
    run.add_argument("--expected-text")
    run.add_argument("--allow-app", action="append", metavar="BUNDLE_ID", default=None,
                     type=validate_bundle_id,
                     help="Offer LAUNCH_APP to this bundle id; repeat for several. "
                          "Without any --allow-app, app switching is never offered.")
    serve = subs.add_parser("serve", help="Local dashboard API")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--wda-url")
    serve.add_argument("--manage-device", action="store_true",
                       help="Own the USB iPhone setup: build, run and supervise WebDriverAgent (desktop app)")
    serve.add_argument("--data-dir", type=Path, help="Where the managed device keeps its files")
    serve.add_argument("--exit-with-parent", action="store_true",
                       help="Shut down when the launching process exits (desktop sidecar safety net)")
    serve.add_argument("--keep-runner", action="store_true",
                       help="Leave the iPhone runner running on exit, for development reloads; the next server adopts it")
    serve.add_argument("--env-file", type=Path,
                       help="KEY=VALUE lines (e.g. TYPESAFE_API_KEY) to load; variables already set win")
    serve.add_argument("--session", help="Preferred WDA session; the one WDA is serving always wins")
    serve.add_argument("--enable-live", action="store_true")
    serve.add_argument("--spend-cap-usd", type=float, default=None,
                       help="Stop a run before further model calls once its observed inference spend reaches this USD amount")
    serve.add_argument("--state-db", type=Path, default=None,
                       help="Private durable task journal (never automatically replays interrupted tasks); "
                            "default: .state/ beside the source in a checkout, else <data dir>/state/")
    commands = {"run": run, "serve": serve, "run_target": target}
    for extension in load_extensions().cli:
        extension.add_arguments(subs, commands)
    return parser


def run_task(args, *, lease_key, target, visual_options=None):
    """The ``run`` command against one target: preview a decision, or execute a bounded task.

    ``target`` gives ``build_target_driver``'s keyword arguments, or is a callable
    that returns them once the device lease is held; ``lease_key`` names the device
    for the cross-process lease.
    """
    usd_to_nanodollars(args.spend_cap_usd)  # Fail before touching the device.
    lease = Lease.device(lease_key) if args.execute else None
    driver = model = helper = None
    try:
        if callable(target):
            target = target()
        driver = build_target_driver(**target)
        if target.get("wda_url") and args.execute:
            # Pixel settle (MOBSTER_FRAME_CLOCK) over its own MJPEG connection.
            from .frame_clock import attach_frame_clock
            attach_frame_clock(driver, wda_url=target["wda_url"], session=target.get("session"))
        ledger = SpendLedger()
        model, helper = build_models(helper=args.helper,
                                     on_inference=ledger.add_event)
        visual = build_visual(enabled=getattr(args, "read_screen", False), driver=driver,
                              **(visual_options or {}))
        result = Agent(driver, model, helper, args.max_steps, args.max_seconds,
                       emit=output, visual=visual, spend_cap_usd=args.spend_cap_usd,
                       spend_ledger=ledger, allowed_bundles=args.allow_app).run(
            args.goal, execute=args.execute, expected_text=args.expected_text)
        return 0 if result["status"] in {"preview", "expected_text_visible", "completed_unverified"} else 1
    finally:
        close_all(driver, model, helper)
        if lease:
            lease.close()


def main():
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "env_file", None):
        from .config import load_env_file
        load_env_file(args.env_file)
    try:
        for extension in load_extensions().cli:
            code = extension.run(args, output, run_task)
            if code is not None:
                return code
        if args.command == "build-ocr":
            root = Path(__file__).parent
            build = build_dir()
            build.mkdir(parents=True, exist_ok=True)
            subprocess.run(["xcrun", "swiftc", "-O", str(root / "ocr.swift"),
                            "-o", str(build / "ocr")], check=True)
            output({"ok": True, "binary": str(build / "ocr")})
        elif args.command == "decide-fixture":
            from dataclasses import asdict
            model = Jev()
            try:
                output({"input": "synthetic_fixture", **asdict(model.decide(screen(), args.goal, []))})
            finally:
                model.http.close()
        elif args.command == "demo":
            output({"mode": "synthetic replay; no phone or model APIs"})
            Agent(DemoDriver(), DemoModel(), DemoHelper(), emit=output).run(
                "Search for coffee", execute=True, expected_text="Coffee brewing guide")
        elif args.command == "serve":
            from .server import serve
            if args.state_db is None:
                args.state_db = default_state_db(args)
            if getattr(args, "manage_device", False) and not args.wda_url:
                args.wda_url = "http://127.0.0.1:8100"  # The relay the device manager runs.
            saved = os.environ.get("MOBSTER_ENABLE_LIVE")
            if saved in {"0", "1"}:
                args.enable_live = saved == "1"  # The setup flow's saved choice wins.
            serve(args)
        else:
            def target():
                try:
                    session = resolve_wda_session(args.wda_url, preferred=args.session)
                except Exception as exc:
                    if "ConnectionRefusedError" not in str(exc):
                        raise
                    raise ConnectionError(
                        f"WebDriverAgent is not reachable at {args.wda_url}. Start it with "
                        "`python -m mobile_agent serve --manage-device` or scripts/usb-wda.sh, then run again.") from None
                return {"wda_url": args.wda_url, "session": session}
            return run_task(args, lease_key=args.wda_url.rstrip("/"), target=target)
        return 0
    except Exception as exc:
        # Exception messages here are locally authored; transport never includes provider bodies/keys.
        output({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
