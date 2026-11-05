"""The terminal UI's engine: the same ``server.Runtime`` that `mobster serve` and the Mac app run."""

import time
from pathlib import Path

from .. import serve, server
from ..api_errors import APIError
from ..cli_constants import DEFAULT_WDA_URL
from ..journal import JournalError, LeaseHeld
from ..paths import default_history_db, user_data_dir
from ..transport import wda_screenshot


def serve_config(**overrides):
    """A config with exactly the attributes `mobster serve` would parse, then ``overrides``."""
    from ..cli_parser import build_parser
    config = build_parser().parse_args(["serve"])
    for name, value in overrides.items():
        setattr(config, name, value)
    return config


class TerminalRuntime(server.Runtime):
    """``server.Runtime`` plus the USB iPhone's installed apps when no device manager runs."""

    one_target = True  # its phone is --wda-url / --device; it never reads the device list to choose one

    def __init__(self, config):
        super().__init__(config)
        if self.installed_apps is None and config.wda_url and _is_usb_relay(config.wda_url):
            from ..device_manager import DeviceManager
            from ..installed_apps import InstalledApps
            data_dir = getattr(config, "data_dir", None) or user_data_dir()
            manager = _phone_manager(data_dir, config.wda_url) or DeviceManager(data_dir, config.wda_url)
            self.installed_apps = InstalledApps(manager.device)


def _is_usb_relay(url):
    """True for the USB relay's own address (8100), or another iPhone's (8101-8199, devices.py)."""
    from ..devices import FIRST_SLOT, LAST_SLOT, WDA_BASE, port_of
    url = url.rstrip("/")
    port = port_of(url)
    return (url.startswith(("http://127.0.0.1:", "http://localhost:")) and port is not None
            and WDA_BASE <= port <= WDA_BASE + LAST_SLOT and (port == WDA_BASE or port - WDA_BASE >= FIRST_SLOT))


def _phone_manager(data_dir, url):
    """The device manager of the iPhone set up besides the first one whose relay is ``url`` (8101-8199)."""
    from ..devices import DeviceStore, manager_for, port_of
    port = port_of(url)
    entry = next((item for item in DeviceStore(data_dir).phones() if item["wdaPort"] == port), None)
    return manager_for(entry["udid"], data_dir) if entry else None


class DemoRuntime(server.Runtime):
    """The real runtime and Mobster's agent over a scripted phone and model (``demo_phone``). No network."""

    one_target = True
    needs_plan = False  # it never acts on a phone (plan_gate)
    pace = 1.0

    def target_status(self):
        return {"device": True, "ready": True, "health": None, "can_act": True, "driver": "demo",
                "screen_reading": False}

    def status(self):
        value = super().status()
        value.update(live_enabled=not self.closing, device="Scripted demo iPhone", limitations=[], demo=True)
        return value

    def check_app(self, app):
        pass

    def smart_client(self):
        from .demo_phone import DemoChat
        return DemoChat(self.pace)

    def open_driver(self, run, trace, setup):
        from .demo_phone import DemoPhone
        driver = setup.hold(DemoPhone(run.app["bundleId"], run.goal, self.pace))
        if run.stop.is_set():
            return None
        run.emit({"event": "app_launch_started", "app": run.app["name"]})
        setup.phase("launching")
        driver._wait(.3)
        run.emit({"event": "app_launch_acknowledged", "app": run.app["name"]})
        setup.phase("reading_app")
        return driver


class Session:
    """One terminal session: a runtime, its running task and settings. Methods may block,
    so the UI calls them off its event loop."""

    def __init__(self, *, wda_url=None, env_file=None, demo=False, history_db=None, spend_cap_usd=None, pace=1.0,
                 origin="tui"):
        """``history_db``: its journal (default: the terminal's; a demo keeps tasks in memory). ``origin``:
        "tui" or "cli"."""
        self.demo = demo
        self.origin = origin
        if env_file:
            from ..config import load_env_file
            # "~/keys.env" as typed (`--env-file=~/…` reaches us unexpanded): read and save in the home folder.
            env_file = Path(env_file).expanduser()
            load_env_file(env_file)
        if demo:
            # Its own lease key and no journal: a demo never waits for, or blocks, another Mobster.
            import uuid
            config = serve_config(wda_url=f"demo://phone/{uuid.uuid4().hex[:8]}", enable_live=True,
                                  state_db=None, env_file=None, spend_cap_usd=spend_cap_usd)
            runtime_class = type("DemoRuntime", (DemoRuntime,), {"pace": pace})
        else:
            import os
            config = serve_config(wda_url=wda_url or os.environ.get("MOBSTER_WDA_URL") or DEFAULT_WDA_URL,
                                  enable_live=os.environ.get("MOBSTER_ENABLE_LIVE", "1") != "0",
                                  state_db=history_db if history_db is not None else default_history_db(),
                                  env_file=env_file, spend_cap_usd=spend_cap_usd)
            runtime_class = serve.runtime_class_for(config, TerminalRuntime)
        self.config = config
        try:
            self.runtime = runtime_class(config)
        except LeaseHeld:
            # The terminal UI and `mobster run` share one task history, and one process at a time may hold it.
            raise LeaseHeld("Another Mobster has this task history open, such as the terminal UI or a `mobster run` "
                            "in another terminal. Use that one or wait for it to finish, then try again.") from None
        self.run = None

    @property
    def wda_url(self):
        return None if self.demo else self.config.wda_url

    def status(self):
        return self.runtime.status()

    def apps(self, fresh=False):
        """The apps a task can open: the catalog, plus the phone's own when they can be read."""
        installed = getattr(self.runtime, "installed_apps", None)
        if fresh and installed is not None:
            installed.refresh()
        return self.runtime.apps()

    def settings(self):
        return self.runtime.settings()

    def set_setting(self, name, value):
        """askBeforeActing. Saved to the env file when there is one, like the Mac app."""
        return self.runtime.update_settings({name: bool(value)})

    @property
    def spend_cap_usd(self):
        return getattr(self.config, "spend_cap_usd", None)

    def set_spend_cap(self, usd):
        """A cap in dollars for the next task, or None for no cap."""
        if usd is not None:
            from ..costs import usd_to_nanodollars
            usd_to_nanodollars(usd)  # validates
        self.config.spend_cap_usd = usd

    def _await_phone_release(self):
        """The last task has published its result; its worker releases the phone a moment later. Wait up to 3 s."""
        previous = self.run
        if previous is None or previous.finished_at is None:
            return
        deadline = time.monotonic() + 3
        while self.runtime.capacity() == 0 and time.monotonic() < deadline:
            with self.runtime.lock:
                if previous.id not in self.runtime.active_runs:
                    break
            time.sleep(.02)

    def start(self, app_id, goal, *, output_format="auto"):
        """Start a task on the phone. Raises ValueError or APIError with a message for the user."""
        self._await_phone_release()
        run = self.runtime.create(app_id, goal, "live", output_format=output_format, origin=self.origin)
        self.run = run
        return run

    def threads(self):
        """The runtime's ThreadService, or None (no journal, or a build without conversations)."""
        try:
            from ..threads.service import service_for
        except ImportError:
            return None
        return service_for(self.runtime)

    def recent_threads(self, limit=30):
        live = self.threads()
        if live is None:
            return []
        return live.store.list(limit=limit)[0]

    def new_thread(self):
        live = self.threads()
        return live.create_thread() if live is not None else None

    def send(self, thread_id, text, *, app_id=None, attachments=None, output_format="auto"):
        """A message to a conversation."""
        live = self.threads()
        body = {"text": text, "outputFormat": output_format, "mode": "auto"}
        if app_id:
            body["appId"] = app_id
        if attachments:
            body["attachmentIds"] = list(attachments)
        self._await_phone_release()
        status, response = live.message(thread_id, body, origin=self.origin)
        routed = response.get("routed") or ("new_run" if response.get("run") else "steer")
        run = None
        if response.get("run"):
            run = self.runtime.runs.get(response["run"]["id"])
            self.run = run
        return routed, run, response

    def attach(self, path, thread_id=None):
        """Attach a file for the next task: (attachment id, its name and size in words). Raises ValueError."""
        path = Path(path).expanduser()
        if not path.is_file():
            raise ValueError(f"There's no file at {path}.")
        try:
            from ..attachments.service import store_for
            from ..attachments.store import FILE_MAX
        except ImportError:
            raise ValueError("Attaching files isn't in this build.") from None
        store = store_for(self.runtime)
        if store is None:
            raise ValueError("Attaching files needs Mobster's task history, which this window doesn't keep.")
        if path.stat().st_size > FILE_MAX:
            raise ValueError("Attach files of 25 MB or less.")
        attachment = store.create(path.read_bytes(), path.name, thread_id=thread_id)
        return attachment.public()

    def stop(self, run=None):
        run = run or self.run
        if run is not None and run.finished_at is None:
            self.runtime.stop_run(run.id)

    def answer(self, approval_id, approve, choice=None, run=None, remember=False):
        """Answer the run's pending approval; ``remember``: its "Don't ask again" (Always allow on an app card)."""
        run = run or self.run
        return bool(run and run.answer_approval(approval_id, approve, choice, remember=remember))

    def pending_approval(self, run=None):
        """The live approval request (with the exact text to be typed), or None."""
        run = run or self.run
        if run is None:
            return None
        with run.condition:
            return dict(run.approval) if run.approval else None

    def follow(self, run, on_event, *, stop=None):
        """Call ``on_event(event)`` for each of the run's events, in order, until it finishes."""
        index = 0
        while stop is None or not stop.is_set():
            with run.condition:
                while index >= len(run.events) and run.finished_at is None and not (stop and stop.is_set()):
                    run.condition.wait(timeout=.5)
                batch = run.events[index:]
                finished = run.finished_at is not None
            for event in batch:
                on_event(event)
            index += len(batch)
            if finished and index >= len(run.events):
                return

    def preview_image(self, run=None):
        """The phone's latest screen as PNG bytes, or None. Captured the way the Mac app's live view is."""
        import base64
        run = run or self.run
        image = None
        if run is not None and run.finished_at is None:
            image = self.runtime.preview(run).get("image")
        elif run is not None and run.image:
            image = run.image
        if image is None and not self.demo and self.config.wda_url:
            image = wda_screenshot(self.config.wda_url)
        if image is None and self.demo and run is None:
            from .demo_phone import SETTINGS, render_screen
            return render_screen(SETTINGS, "top")
        if isinstance(image, str) and image.startswith("data:image"):
            try:
                return base64.b64decode(image.split(",", 1)[1])
            except (ValueError, IndexError):
                return None
        return None

    def history(self):
        """Runs in the journal, newest first."""
        with self.runtime.lock:
            runs = list(self.runtime.runs.values())
        return sorted(runs, key=lambda run: run.created_at, reverse=True)

    def find(self, prefix):
        prefix = (prefix or "").strip().lower()
        matches = [run for run in self.history() if run.id.startswith(prefix)] if prefix else []
        return matches[0] if len(matches) == 1 else None

    def close(self, timeout=10):
        try:
            if self.run is not None and self.run.finished_at is None:
                self.runtime.stop_run(self.run.id)
        except Exception:
            pass
        return self.runtime.close(timeout)


def error_text(error):
    """A one-line message for an exception from the runtime, for the user."""
    if isinstance(error, (APIError, ValueError, JournalError)):
        text = str(error)
        if getattr(error, "code", None) == "engine_unavailable" and "_API_KEY" in text:
            # The runtime's reason names the env file the Mac app's sidecar reads; here `mobster login` adds a key.
            return "Mobster's agent needs your Claude or OpenAI key first: mobster login adds it."
        return text
    return f"{type(error).__name__}: {str(error)[:200]}"
