"""The terminal UI's engine: the same ``server.Runtime`` that `mobster serve` and the Mac app run.

Nothing about a run is reimplemented here. A task is ``Runtime.create``, its
events are ``Run.events``, an approval is ``Run.request_approval`` answered with
``Run.answer_approval``, Stop is ``Runtime.stop_run``, the settings are
``Runtime.update_settings`` and history is the runtime's journal. The terminal UI
only draws them. This module imports no UI library, so it is tested on its own.
"""

from pathlib import Path
import time

from .. import server
from ..api_errors import APIError
from ..extensions import load as load_extensions
from ..journal import JournalError, LeaseHeld
from ..paths import source_checkout, user_data_dir

DEFAULT_WDA_URL = "http://127.0.0.1:8100"


def default_history_db():
    """The terminal's own journal, beside `serve`'s but a separate file.

    A journal has one owner at a time (``journal.Lease``); a separate file lets
    the terminal UI run while `mobster serve` or the Mac app holds theirs.
    """
    if source_checkout():
        return Path(server.__file__).parent / ".state" / "terminal.sqlite3"
    return user_data_dir() / "state" / "terminal.sqlite3"


def serve_config(**overrides):
    """A config with exactly the attributes `mobster serve` would parse, then ``overrides``."""
    from ..__main__ import build_parser
    config = build_parser().parse_args(["serve"])
    for name, value in overrides.items():
        setattr(config, name, value)
    return config


class TerminalRuntime(server.Runtime):
    """``server.Runtime`` plus the USB iPhone's installed apps when no device manager runs.

    `serve --manage-device` lists them through its device manager. The terminal UI
    does not manage the device, so it asks the same ``InstalledApps`` about the
    phone the Mac app chose (or the only one connected), read-only.
    """

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
    """True for the USB relay's own address (8100), or another iPhone's (8101-8199, devices.py); a simulator's WDA
    listens elsewhere (8200+)."""
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
    """The real runtime and agent over a scripted phone and policy (``demo_phone``). No network."""

    one_target = True
    pace = 1.0

    def target_status(self):
        return {"device": True, "ready": True, "health": None, "can_act": True, "driver": "demo",
                "screen_reading": False, "notes": []}

    def status(self):
        value = super().status()
        value.update(jev_configured=True, live_enabled=not self.closing, device="Scripted demo iPhone",
                     helper_configured=True, helper_model="scripted", helper_models=["scripted"],
                     limitations=[], demo=True)
        return value

    def check_app(self, app):
        pass

    def open_models(self, run, on_inference):
        from .demo_phone import DemoHelper, DemoPolicy
        return DemoPolicy(self.pace), DemoHelper(), None

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
    """One terminal session: a runtime, the task running on it, and its settings.

    Every method may block briefly (the runtime checks WDA, the journal writes),
    so the UI calls them from worker threads, never its event loop.
    """

    def __init__(self, *, wda_url=None, env_file=None, demo=False, history_db=None, spend_cap_usd=None,
                 data_dir=None, pace=1.0, journal=True, origin="tui"):
        """``journal=False``: no task history (`mobster run`); ``origin``: where its tasks come from ("tui", or
        "cli" for `mobster run`), so the tracks that read it (memory, conversations) treat them as a person's."""
        from .. import tracks
        tracks.load()  # the SOTA tracks register before the runtime opens its journal (seam S1)
        self.demo = demo
        self.origin = origin
        if env_file:
            from ..config import load_env_file
            # "~/keys.env" as typed (`--env-file=~/…` reaches us unexpanded): read and save in the home folder.
            env_file = Path(env_file).expanduser()
            load_env_file(env_file)
        if demo:
            # Its own lease key and no journal: a demo never waits for, or blocks, another Mobster (P1-10).
            import uuid
            config = serve_config(wda_url=f"demo://phone/{uuid.uuid4().hex[:8]}", enable_live=True,
                                  state_db=None, env_file=None, spend_cap_usd=spend_cap_usd, data_dir=data_dir)
            runtime_class = type("DemoRuntime", (DemoRuntime,), {"pace": pace})
        else:
            import os
            config = serve_config(wda_url=wda_url or os.environ.get("MOBSTER_WDA_URL") or DEFAULT_WDA_URL,
                                  enable_live=os.environ.get("MOBSTER_ENABLE_LIVE", "1") != "0",
                                  state_db=history_db if history_db is not None else
                                  default_history_db() if journal else None,
                                  env_file=env_file, spend_cap_usd=spend_cap_usd, data_dir=data_dir)
            hooks = load_extensions()
            runtime_class = (hooks.runtime_for(config) if hooks.runtime_for else None) or TerminalRuntime
        self.config = config
        try:
            self.runtime = runtime_class(config)
        except LeaseHeld:
            # Every terminal UI shares one task history, and one process at a time may hold it.
            raise LeaseHeld("Mobster is already open in another terminal window. Use that one, or close it "
                            "first. `mobster run` works alongside it.") from None
        self.run = None

    # -- status ----------------------------------------------------------------------------

    @property
    def wda_url(self):
        return None if self.demo else self.config.wda_url

    def status(self):
        return self.runtime.status()

    def apps(self, fresh=False):
        """The apps a task can open: the catalog, plus the phone's own when they can be read. ``fresh`` reads
        the phone now instead of using the cached list (the phone has just become ready)."""
        installed = getattr(self.runtime, "installed_apps", None)
        if fresh and installed is not None:
            installed.refresh()
        return self.runtime.apps()

    def settings(self):
        return self.runtime.settings()

    def set_setting(self, name, value):
        """askBeforeActing or bypassChecks. Saved to the env file when there is one, like the Mac app."""
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

    # -- tasks -----------------------------------------------------------------------------

    def start(self, app_id, goal, *, preview=False, helper_model=None, output_format="auto"):
        """Start a task on the phone. Raises ValueError or APIError with a message for the user."""
        previous = self.run
        if previous is not None and previous.finished_at is not None:
            # The last task has published its result; its worker releases the phone a moment later.
            deadline = time.monotonic() + 3
            while self.runtime.capacity() == 0 and time.monotonic() < deadline:
                with self.runtime.lock:
                    if previous.id not in self.runtime.active_runs:
                        break
                time.sleep(.02)
        run = self.runtime.create(app_id, goal, "live", output_format=output_format,
                                  helper_model=helper_model, dry_run=preview, origin=self.origin)
        self.run = run
        return run

    # -- conversations (the conversations track's service, in this process) ----------------------------------

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

    def send(self, thread_id, text, *, app_id=None, attachments=None, output_format="auto", engine=None,
             mode="auto"):
        """A message to a conversation: a new task in it, or, while its task runs, a message to that task (steering).
        Returns (routed, run or None, response): routed is "new_run", "steer", "answer" or "remember"."""
        live = self.threads()
        body = {"text": text, "outputFormat": output_format, "mode": mode}
        if app_id:
            body["appId"] = app_id
        if attachments:
            body["attachmentIds"] = list(attachments)
        if engine:
            body["engine"] = engine
        previous = self.run
        if previous is not None and previous.finished_at is not None:
            deadline = time.monotonic() + 3
            while self.runtime.capacity() == 0 and time.monotonic() < deadline:
                with self.runtime.lock:
                    if previous.id not in self.runtime.active_runs:
                        break
                time.sleep(.02)
        status, response = live.message(thread_id, body, origin=self.origin)
        routed = response.get("routed") or ("new_run" if response.get("run") else "steer")
        run = None
        if response.get("run"):
            run = self.runtime.runs.get(response["run"]["id"])
            self.run = run
        return routed, run, response

    def attach(self, path, thread_id=None):
        """Attach a file for the next task: (attachment id, its name and size in words). Raises ValueError."""
        from pathlib import Path
        path = Path(path).expanduser()
        if not path.is_file():
            raise ValueError(f"There's no file at {path}.")
        try:
            from ..attachments.service import store_for
        except ImportError:
            raise ValueError("Attaching files isn't in this build.") from None
        store = store_for(self.runtime)
        if store is None:
            raise ValueError("Attaching files needs Mobster's task history, which this window doesn't keep.")
        if path.stat().st_size > 25 * 1024 * 1024:
            raise ValueError("Attach files of 25 MB or less.")
        attachment = store.create(path.read_bytes(), path.name, thread_id=thread_id)
        return attachment.public() if hasattr(attachment, "public") else attachment

    def stop(self, run=None):
        run = run or self.run
        if run is not None and run.finished_at is None:
            self.runtime.stop_run(run.id)

    def answer(self, approval_id, approve, choice=None, run=None):
        run = run or self.run
        return bool(run and run.answer_approval(approval_id, approve, choice))

    def pending_approval(self, run=None):
        """The live approval request (with the exact text to be typed), or None."""
        run = run or self.run
        if run is None:
            return None
        with run.condition:
            return dict(run.approval) if run.approval else None

    def follow(self, run, on_event, *, start=0, stop=None):
        """Call ``on_event(event)`` for each of the run's events from ``start``, in order, until it finishes.

        Runs on the calling thread; ``stop`` (an Event) ends it early.
        """
        index = start
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
                return index
        return index

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


def wda_screenshot(wda_url, timeout=2):
    """WDA's current screen as a data URL (sessionless GET /screenshot), or None."""
    from ..transport import HTTP
    client = None
    try:
        client = HTTP(wda_url)
        value = client.request("GET", "/screenshot", timeout=timeout).get("value")
        return "data:image/png;base64," + value if isinstance(value, str) and value else None
    except Exception:
        return None
    finally:
        if client is not None:
            client.close()


# The runtime's reasons for a missing key name the env file the Mac app's sidecar reads. In a terminal the way to
# add one is `mobster login` (or /login), so a missing key reads as that.
NO_KEY_HERE = "Mobster's agent needs your Claude or OpenAI key first: mobster login adds it."


def error_text(error):
    """A one-line message for an exception from the runtime, for the user."""
    if isinstance(error, (APIError, ValueError, JournalError)):
        text = str(error)
        if getattr(error, "code", None) == "engine_unavailable" and "_API_KEY" in text and "TYPESAFE" not in text:
            return NO_KEY_HERE
        return text
    return f"{type(error).__name__}: {str(error)[:200]}"
