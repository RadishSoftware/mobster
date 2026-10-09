"""`mobster mcp`: the command's flags and its run (SPEC §7.7 and §12.3).

``add_arguments(parser, helpers)`` adds the flags; ``run(args)`` serves MCP on stdin and stdout until the
client disconnects and returns the exit code. It handles its own errors and never raises.
"""

import argparse
import os
from pathlib import Path
import signal
import sys
import threading
import time


def add_arguments(parser, helpers):
    path_type = helpers.get("path_type") or (lambda value: Path(value).expanduser())
    # SUPPRESS keeps a value given before the command's name (`mobster --env-file F mcp`).
    parser.add_argument("--env-file", type=path_type, metavar="PATH", default=argparse.SUPPRESS,
                        help="load KEY=VALUE lines; OPENAI_API_KEY or ANTHROPIC_API_KEY turns on verify (Smart); "
                             "variables already set win; default: $MOBSTER_ENV_FILE")
    parser.add_argument("--keyless", action="store_true",
                        help="never offer verify (Smart), even with an OpenAI or Anthropic key: the coding agent "
                             "drives")
    parser.add_argument("--device", metavar="NAME",
                        help="the device for runs and phone tools that don't name one: an id, UDID or name from "
                             "`mobster devices`, or a simulator device type (default: iPhone 17 Pro)")
    parser.add_argument("--allow-device", action="append", metavar="NAME", dest="allow_devices",
                        help="drive only this real iPhone (id, UDID or name), never another; repeatable. Without it "
                             "every set-up device is allowed; simulators always are")
    parser.add_argument("--allow-files", action="store_true",
                        help="offer the file tools (put_file, get_file, list_files) when this build has them; off by "
                             "default")
    parser.add_argument("--runtime", metavar="NAME",
                        help="the iOS runtime for runs that don't name one, such as \"iOS 26.4\" "
                             "(default: the newest installed)")
    parser.add_argument("--out", type=path_type, metavar="DIR",
                        help="where runs go (default: $MOBSTER_RUNS_DIR, else ./.mobster/runs)")
    parser.add_argument("--log", type=path_type, metavar="FILE",
                        help="also append the server's log to FILE (it never holds keys or long typed text)")


def runs_dir(out=None, env=None, cwd=None, home=None):
    """--out, else $MOBSTER_RUNS_DIR, else <cwd>/.mobster/runs when the cwd is neither / nor $HOME, else
    <dev data>/runs (SPEC §7.3)."""
    env = os.environ if env is None else env
    if out:
        return Path(out).expanduser().absolute()
    if env.get("MOBSTER_RUNS_DIR"):
        return Path(env["MOBSTER_RUNS_DIR"]).expanduser().absolute()
    cwd = Path.cwd() if cwd is None else Path(cwd)
    home = Path.home() if home is None else Path(home)
    try:
        at_home = cwd.resolve() == home.resolve()
    except OSError:
        at_home = False
    if str(cwd) != "/" and not at_home:
        return cwd / ".mobster" / "runs"
    return dev_data_dir(env) / "runs"


def device_runs_dir(out=None, env=None):
    """Where a real iPhone driven directly keeps its frames: the runs folder --out or $MOBSTER_RUNS_DIR names, else
    <dev data>/runs, so the phone's screens never land in the repository the client started the server in."""
    env = os.environ if env is None else env
    if out or env.get("MOBSTER_RUNS_DIR"):
        return runs_dir(out, env)
    return dev_data_dir(env) / "runs"


def dev_data_dir(env=None):
    env = os.environ if env is None else env
    try:
        from ..paths import dev_data_dir as shared
    except ImportError:
        shared = None
    if shared is not None:
        return Path(shared())
    if env.get("MOBSTER_DATA_DIR"):
        return Path(env["MOBSTER_DATA_DIR"]).expanduser()
    from ..paths import user_data_dir
    return user_data_dir() / "dev"


def smart_setup(keyless=False, env=None, env_file=None):
    """(key, model, None) when the server offers verify (Smart), else (None, None, why it is off). An ``env_file``
    that doesn't exist leads the reason: status and the log then name the mistyped path, not "pass --env-file".

    The key and the model are the ones the Mac app's Smart uses (engines.smart_key and engines.smart_model):
    SMART_CONFIG's model on an OpenAI key, ANTHROPIC_SMART_MODEL for a user whose only key is Anthropic's, or the
    model MOBSTER_SMART_MODEL names, on its provider's key. A verify run builds its client from the same
    environment (engines.build_client), so what the server says is what the run uses."""
    if keyless:
        return None, None, "--keyless"
    from .. import engines
    env = os.environ if env is None else env
    model = engines.smart_model(env)
    key = engines.smart_key(env)
    if key:
        return key, model, None
    reason = smart_off_reason(model, env)
    if env_file and not Path(env_file).expanduser().exists():
        from ..config import missing_env_file
        reason = f"{missing_env_file(env_file)}; {reason}"
    return None, None, reason


def smart_off_reason(model, env):
    """Why no key fits ``model``: MOBSTER_SMART_MODEL chose a model whose provider has no key here, or there
    is no OpenAI or Anthropic key at all."""
    from .. import engines
    chosen = (env.get("MOBSTER_SMART_MODEL") or "").strip()
    if chosen and chosen == model:
        anthropic = engines.model_provider(model) == "anthropic"
        provider, variable = ("Anthropic", "ANTHROPIC_API_KEY") if anthropic else ("OpenAI", "OPENAI_API_KEY")
        other = engines.openai_smart_key(env) if anthropic else engines.anthropic_key(env)
        reason = f"MOBSTER_SMART_MODEL is {model}, which needs an {provider} key; set {variable}"
        if other:
            reason += f", or unset MOBSTER_SMART_MODEL to use your {'OpenAI' if anthropic else 'Anthropic'} key"
        return reason
    from .tools import NO_SMART_KEY
    return NO_SMART_KEY


class Log:
    """Log lines to stderr and, with --log, to a file. Callers never pass keys or long typed text."""

    def __init__(self, path=None, stream=None):
        self.stream = stream or sys.stderr
        self.path = Path(path) if path else None
        self.lock = threading.Lock()

    def __call__(self, message):
        line = f"mobster mcp {time.strftime('%H:%M:%S')} {message}".rstrip()
        with self.lock:
            try:
                self.stream.write(line + "\n")
                self.stream.flush()
            except (OSError, ValueError):
                pass
            if self.path is not None:
                try:
                    with open(self.path, "a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                except OSError:
                    pass


def run(args):
    log = Log(getattr(args, "log", None))
    try:
        return _serve(args, log)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        log(f"The server stopped on an error ({type(error).__name__}: {error}).")
        return 1


def _serve(args, log):
    from .. import __version__, tls
    from .protocol import Server, claim_stdout
    from .tools import ToolSet

    env_file = getattr(args, "env_file", None) or (
        Path(os.environ["MOBSTER_ENV_FILE"]).expanduser() if os.environ.get("MOBSTER_ENV_FILE") else None)
    if env_file:
        from ..config import load_env_file
        for warning in load_env_file(env_file).warnings:
            log(warning)
    tls.ensure_ca_bundle()
    keyless = bool(getattr(args, "keyless", False))
    key, model, off = smart_setup(keyless, env_file=env_file)
    folder = runs_dir(getattr(args, "out", None))
    from .. import tracks
    tracks.load()  # the tracks' MCP tools (registry.py) before the ToolSet lists any
    tools = ToolSet(runs_dir=folder, key=key, keyless=keyless, smart_model=model, smart_off=off,
                    device=getattr(args, "device", None), runtime=getattr(args, "runtime", None), log=log,
                    version=__version__, allow_devices=getattr(args, "allow_devices", None),
                    device_runs_dir=device_runs_dir(getattr(args, "out", None)),
                    allow_files=bool(getattr(args, "allow_files", False)))
    proto = claim_stdout()
    server = Server(tools, version=__version__, log=log)
    log(f"Serving MCP on stdio. Mobster {__version__}; Smart {tools.smart_state()}; runs go to {folder}.")
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _terminate)
    tools.start()
    code = 0
    try:
        code = server.serve(sys.stdin.buffer, proto)
    except KeyboardInterrupt:
        server.close("ctrl+c stopped the server")
        code = 130
    except SystemExit:
        server.close("the server was terminated")
        code = 0
    log("Stopped.")
    _exit_if_stuck(code)
    return code


def _terminate(signum, frame):
    raise SystemExit(0)


def _exit_if_stuck(code):
    """A thread that is not a daemon (a model call inside a run) would keep the process alive after the
    client left: exit at once instead."""
    if any(t.is_alive() and not t.daemon for t in threading.enumerate() if t is not threading.main_thread()):
        try:
            sys.stderr.flush()
        finally:
            os._exit(code)
