"""`mobster xray app APP`: map an app on your iPhone with no model, through the same runtime as `mobster run`."""

import sys
import threading

from . import job, policy

HELP = "map an app's screens, links and paywalls with no model"
DESCRIPTION = ("Map an app on your iPhone: Mobster taps through every control that opens a screen, within the limits\n"
               "you set, and writes the map (map.json), a report (report.html) and a thumbnail of each screen to a\n"
               "folder on this Mac. It reads paywalls and taps nothing on them. It never taps a control that could act\n"  # noqa: E501
               "(delete, send, buy, a switch, a field), never types, and calls no model, so it needs no key. It runs\n"
               "like any task: your plan, the phone's queue and Stop apply.")
EPILOG = (
    "exit codes: 0 mapped, 1 stopped or mapped nothing, 2 usage, 3 couldn't run (no phone, no plan, app not found,\n"
          "            an app it doesn't map), 130 ctrl+c\n\n"
          "examples:\n"
          "  mobster xray app com.apple.Preferences\n"
          "  mobster xray app Weather --screens 60 --minutes 10 --device \"Test iPhone\"\n"
          "  mobster xray app com.example.yourapp --json")


def add_arguments(parser):
    # dest isn't "app": `mobster --app` is the terminal UI's, and main's check_placement refuses it on other commands.
    parser.add_argument("target_app", metavar="APP", help="the app's bundle ID, or its name on the phone")
    parser.add_argument("--device", metavar="NAME",
                        help="the iPhone to use (an id, UDID or name from `mobster devices`)")
    parser.add_argument("--screens", type=int, default=job.DEFAULT["screens"], metavar="N",
                        help=f"stop after this many screens, 1 to 120 (default {job.DEFAULT['screens']})")
    parser.add_argument("--minutes", type=int, default=job.DEFAULT["minutes"], metavar="N",
                        help=f"stop after this many minutes, 1 to 30 (default {job.DEFAULT['minutes']})")
    parser.add_argument("--json", action="store_true", help="print each event as one JSON line; the last is the result")


def run(args):
    from .. import cli_run as cli
    from ..cli_constants import EXIT_COULDNT, EXIT_FAILED, EXIT_OK, EXIT_USAGE
    from ..tui.session import Session, error_text
    as_json = bool(args.json) or not sys.stdout.isatty()

    def fail(message, code):
        if as_json:
            cli.output({"event": "result", "status": "error", "ok": False, "error": message, "reason": message,
                        "exit_code": code})
        else:
            print(f"mobster xray: {message}", file=sys.stderr)
        return code

    try:
        options = job.clean({"screens": args.screens, "minutes": args.minutes})
    except ValueError as error:
        return fail(str(error), EXIT_USAGE)
    refusal = policy.app_refusal(args.target_app.strip())
    if refusal:
        return fail(refusal, EXIT_COULDNT)
    wda_url = None
    if args.device:
        try:
            wda_url = cli.device_target(args.device)["wdaUrl"]
        except cli.NoDevice as error:
            return fail(str(error), EXIT_COULDNT)
    emit = cli.event_printer(args, preview=False)
    try:
        session = Session(wda_url=wda_url or cli.wda_url_for(args), origin="cli")
    except Exception as error:  # noqa: BLE001
        return fail(error_text(error), EXIT_COULDNT)
    stop = threading.Event()
    try:
        try:
            app = job.find_app(session.runtime, args.target_app)
            if app is None:
                return fail(f"{args.target_app} isn't on this iPhone. Use its bundle ID or the name under its icon.",
                            EXIT_COULDNT)
            refusal = policy.app_refusal(app["bundleId"])
            if refusal:
                return fail(refusal, EXIT_COULDNT)
            run = session.runtime.create(app["id"], job.GOAL.format(name=app["name"]), "live", origin=session.origin,
                                         extras={"xray": options})
            session.run = run
        except Exception as error:  # noqa: BLE001
            return fail(error_text(error), EXIT_COULDNT)
        if emit is not cli.output:
            emit.start(f"Map {app['name']}", "App X-ray: no model, no key")
        with cli.stop_at_safe_point(stop, on_stop=lambda: session.stop(run)) as caught:
            result = {}

            def on_event(event):
                kind = event.get("event")
                if kind == "run_finished":
                    return
                if kind == "result":
                    result.update(event)  # printed last
                    return
                emit(event)

            try:
                session.follow(run, on_event)
            except KeyboardInterrupt:
                session.stop(run)
                raise
        if not result:
            summary = run.summary if isinstance(getattr(run, "summary", None), dict) else {}
            result = {**summary, "event": "result", "status": run.status}
        emit(result)
        where = result.get("xray") or {}
        if where and emit is not cli.output:
            emit.line("")
            emit.line(f"Map: {where['map']}")
            emit.line(f"Report: {where['report']}")
        if caught:
            return 128 + caught[0]
        return EXIT_OK if result.get("status") == "completed" else EXIT_FAILED
    finally:
        if emit is not cli.output:
            emit.close()
        session.close()
