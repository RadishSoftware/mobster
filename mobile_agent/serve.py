"""`mobster serve`: the service's secrets, token file, parent watch and the serve loop."""

import io
import json
import os
import re
import secrets
import select
import signal
import sys
import threading
import time
from pathlib import Path

from . import harness_api, keychain_acl
from .api_http import setup_scope_token
from .costs import usd_to_nanodollars
from .paths import write_private
from .server import BoundedServer, Runtime, make_handler
from .workflows.scheduler import KeepAwake, WorkflowScheduler

TOKEN_FILE = "api-token"
# `mobster chat` and MCP's phone_task find the service here when it moved off 8765 (threads/client.py).
URL_FILE = "api-url"


# The Mac app's shell passes the token and app session on stdin: `ps -E` shows the environment to the same user.
SECRETS_ON_STDIN = "MOBSTER_SECRETS_ON_STDIN"


def launch_secrets(stream=None, wait=10.0):
    if os.environ.pop(SECRETS_ON_STDIN, "") != "1":
        return None, None
    # The app's windowless agent: an untrusted Keychain item fails to read instead of prompting (keychain_acl.py).
    keychain_acl.no_prompts()
    stream = stream or sys.stdin
    try:
        ready = select.select([stream], [], [], wait)[0]
    except (OSError, ValueError, io.UnsupportedOperation):
        ready = [stream]
    if not ready:
        raise ValueError("the Mac app did not pass the API token on stdin")
    parts = stream.readline().split()
    if len(parts) != 2:
        raise ValueError("the Mac app passed no API token and app session on stdin")
    return parts[0], parts[1]


def api_token(supplied=None):
    # Popped so the runner and relay processes don't inherit it.
    configured = os.environ.pop("MOBSTER_API_TOKEN", "")
    token = supplied or configured or secrets.token_urlsafe(32)
    if not re.fullmatch(r"[A-Za-z0-9_.~-]{32,256}", token):
        raise ValueError("MOBSTER_API_TOKEN must be 32 to 256 URL-safe characters")
    return token


def app_session_secret(supplied=None):
    configured = os.environ.pop("MOBSTER_APP_SESSION", "")
    value = supplied or configured
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.~-]{32,256}", value):
        raise ValueError("MOBSTER_APP_SESSION must be 32 to 256 URL-safe characters")
    return value


def token_file(config):
    state_db = getattr(config, "state_db", None)
    return Path(state_db).parent / TOKEN_FILE if state_db else None


def watch_parent(parent, interval=1.0):
    # Killing only a frozen sidecar's bootloader used to orphan this process, which kept holding the API port.
    def watch():
        while True:
            time.sleep(interval)
            if os.getppid() != parent:
                os.kill(os.getpid(), signal.SIGTERM)
                return
    threading.Thread(target=watch, name="mobster-parent-watch", daemon=True).start()


def runtime_class_for(config, base=Runtime):
    runtime_for = harness_api.hooks().runtime_for
    runtime_class = (runtime_for(config) if runtime_for else None) or base
    if runtime_class is not base:
        return runtime_class
    # Imported here: both modules import server at load.
    from . import cable_bt_runtime, mirroring_runtime
    for module, experimental in ((mirroring_runtime, mirroring_runtime.MirroringRuntime),
                                 (cable_bt_runtime, cable_bt_runtime.CableBtRuntime)):
        if module.wanted(config):
            config.wda_url = module.CONFIG_URL
            return experimental
    return base


def serve(config):
    usd_to_nanodollars(getattr(config, "spend_cap_usd", None))
    runtime_class = runtime_class_for(config)
    supplied_token, supplied_session = launch_secrets()
    token, path = api_token(supplied_token), token_file(config)
    session = app_session_secret(supplied_session)
    # The Mac app's agent (it has an app session) needs a plan or trial; `mobster serve` alone has no account.
    config.require_plan = bool(session)
    runtime = runtime_class(config)
    runtime.model_reach.enabled = True
    runtime.engine_sampler.start()
    server = None
    try:
        setup_token = setup_scope_token(token) if session else None
        server = BoundedServer(("127.0.0.1", config.port), make_handler(runtime, token, app_session=session,
                                                                        setup_token=setup_token))
        # --port 0 lets the system pick; a test's stand-in server may have no address.
        bound = getattr(server, "server_address", None)
        if bound:
            config.port = bound[1]
        url = f"http://127.0.0.1:{config.port}"
        if path:
            write_private(path, token + "\n")
            write_private(path.with_name(URL_FILE), url + "\n")
        runtime.local_api = {"url": url, "token": token, "app_session": bool(session)}
        runtime.serving.set()
        runtime.power.awake.attach(KeepAwake())
        scheduled_checks = getattr(runtime, "test_schedules", None)
        runtime.scheduler = WorkflowScheduler(runtime.workflows, keep_awake=runtime.power.awake.handle("schedule"),
                                              also=scheduled_checks.tick if scheduled_checks is not None else None)
        runtime.scheduler.start()
    except Exception:
        if server is not None:
            server.server_close()
        runtime.close()
        raise
    # The token is never printed: stdout reaches logs.
    print(json.dumps({"service": "Mobster API", "url": url,
                      "tokenFile": str(path) if path else None}), flush=True)
    if getattr(config, "exit_with_parent", False):
        watch_parent(os.getppid())
    previous_term = None
    if threading.current_thread() is threading.main_thread():
        previous_term = signal.getsignal(signal.SIGTERM)
        def terminate(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.close()
        for leftover, mine in ((path, token), (path and path.with_name(URL_FILE), url)):
            try:
                if leftover and leftover.read_text().strip() == mine:
                    leftover.unlink()
            except OSError:
                pass
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
