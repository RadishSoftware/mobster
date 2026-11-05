"""Several scripted iPhones in one real service for the multi-phone tests, with a scripted Setup."""

import threading
import time
from dataclasses import dataclass, field

from mobile_agent import devices as registry
from mobile_agent import engines
from mobile_agent.devices import REASONS
from mobile_agent.fleet import Fleet, Phone
from mobile_agent.tests import sota_world as world
from mobile_agent.tui.session import DemoRuntime, serve_config

READY = {"device": True, "ready": True, "health": None, "can_act": True, "driver": "wda", "screen_reading": False}
# The build's xcodebuild lines, one per phase the dashboard reads (dashboard/src/lib/setup.ts › BUILD_PHASES).
BUILD_LINES = ("Cloning into 'WebDriverAgent'...", "Command line invocation: xcodebuild build-for-testing",
               "Preparing iPhone for development", "SwiftCompile normal arm64 FBSession.swift",
               "CodeSign WebDriverAgentRunner-Runner.app", "** BUILD SUCCEEDED **")
TEAM = {"id": "ABCDE12345", "name": "Sam Lee (Personal Team)", "personal": True, "label": "Sam Lee, free account"}


@dataclass
class Spec:
    udid: str
    name: str
    model: str            # ProductType, such as iPhone18,1
    ios: str
    plugged: bool = True
    trusted: bool = True
    developer_mode: bool = True
    set_up: bool = True
    locked: bool = False
    primary: bool = False
    build: dict = field(default_factory=lambda: {"state": "idle", "started_at": None, "line": 0})
    runner: str = "running"
    state: dict = field(default_factory=world.new_phone_state)

    @property
    def model_name(self):
        from mobile_agent.device_info import MODEL_NAMES
        return MODEL_NAMES.get(self.model)


def specs():
    """The demo's phones: three set up (Studio iPhone first), and none waiting."""
    return [Spec("00008130-001A2B3C4D5E6F70", "Studio iPhone", "iPhone18,1", "26.4", primary=True),
            Spec("00008020-000A1B2C3D4E5F60", "Work iPhone", "iPhone17,3", "26.0"),
            Spec("00008030-000B2C3D4E5F6071", "Test iPhone", "iPhone16,1", "18.6")]


class WorldManager:
    """A set-up scripted phone's DeviceManager: its helper starts and stops as its Spec says."""

    def __init__(self, spec):
        self.spec = spec
        self.watching = threading.Event()
        self.runner = None

    def start_runner(self):
        if not self.spec.plugged:
            raise LookupError("Connect your iPhone first")
        self.spec.runner = "running"

    def stop_runner(self, remember=False):
        self.spec.runner = "stopped"

    def save_settings(self, **changes):
        return {}

    def settings(self):
        return {}

    def health_error(self):
        return "Unlock your iPhone." if self.spec.locked else None

    def close(self):
        self.watching.set()


class WorldPhone(Phone):
    """A scripted phone in the fleet: ready, locked or away as its Spec says."""

    def __init__(self, fleet, spec):
        super().__init__(fleet, id=spec.udid, kind="usb",
                         wda_url=world.phone_url(f"{fleet.world.token}-{spec.udid[-6:]}"),
                         udid=spec.udid, name=spec.name, model=spec.model, ios=spec.ios)
        self.spec = spec

    @property
    def manager(self):
        return WorldManager(self.spec) if self.spec.set_up else None

    @manager.setter
    def manager(self, value):
        pass

    def target_status(self):
        spec = self.spec
        ready = spec.plugged and spec.trusted and spec.set_up and spec.runner == "running" and not spec.locked
        return {**READY, "ready": ready, "health": "Unlock your iPhone." if spec.locked else None}

    def info(self):
        spec = self.spec
        return {"modelIdentifier": spec.model, "modelName": spec.model_name, "iosVersion": spec.ios,
                "buildVersion": None, "virtual": False, "source": "usb", "verifiedAt": None, "stale": False}

    @property
    def video(self):
        return None

    @property
    def control(self):
        return None


class ScriptedFleet(Fleet):
    """The fleet of a World: its phones instead of USB, simulators and WDA addresses."""

    def __init__(self, runtime):
        self.world = runtime.world
        super().__init__(runtime)
        self.primary.name = next((spec.name for spec in self.world.specs if spec.primary), None)

    def _phone(self, spec):
        if spec.primary:
            return self.primary
        with self.lock:
            phone = self.phones.get(spec.udid)
            if phone is None:
                phone = self.phones[spec.udid] = WorldPhone(self, spec)
            return phone

    def _primary_identity(self):
        spec = next((spec for spec in self.world.specs if spec.primary), None)
        if spec is None:
            return None
        return {"id": spec.udid, "kind": "usb", "udid": spec.udid, "name": spec.name}

    def inventory(self):
        found = []
        for spec in self.world.specs:
            if not spec.plugged and not spec.set_up:
                continue  # an iPhone never set up is listed only while it's plugged in
            phone = self._phone(spec)
            if spec.primary:
                phone.id, phone.kind, phone.udid = spec.udid, "usb", spec.udid
            phone.name, phone.model, phone.ios = spec.name, spec.model, spec.ios
            found.append((phone, {"spec": spec}))
        nicknames = self.stored()["prefs"]
        for phone, _ in found:
            nickname = (nicknames.get(phone.id) or {}).get("nickname")
            if nickname:
                phone.name = nickname
        return found

    def _record(self, phone, context, wifi=None):
        spec = context["spec"]
        if self.busy(phone) is not None:
            state, reason = "busy", REASONS["busy"]
        elif not spec.plugged:
            state, reason = "disconnected", REASONS["unplugged"]
        elif not spec.trusted:
            state, reason = "needs_setup", REASONS["untrusted"]
        elif not spec.set_up:
            state, reason = "needs_setup", REASONS["not_built"]
        elif spec.runner != "running":
            state, reason = "connected", REASONS["starting"] if spec.runner == "starting" else REASONS["stopped"]
        elif spec.locked:
            state, reason = "connected", REASONS["locked"]
        else:
            state, reason = "ready", None
        return registry.record(id=spec.udid, kind="usb", name=phone.name, udid=spec.udid, model=spec.model,
                               model_name=spec.model_name, ios=spec.ios, state=state, reason=reason,
                               wda_url_value=phone.wda_url, primary=spec.primary, set_up=spec.set_up)

    def target(self, phone):
        return self.world.spec(phone.id) if phone is not None and phone.id else None

    def setup_state(self, phone):
        return self.world.setup_state(self.world.spec(phone.id))

    def setup_action(self, phone, action, body):
        spec = self.world.spec(phone.id)
        if action == "build" and set(body) == {"team"}:
            self.world.start_build(spec)
        elif action == "refresh" and not body:
            pass
        elif action == "start" and not body:
            if not spec.set_up:
                raise LookupError("Set this iPhone up first: install Mobster's helper on it.")
            spec.runner = "running"
        elif action == "stop" and not body:
            spec.runner = "stopped"
        else:
            raise KeyError(action)
        return phone

    def forget(self, query):
        phone = self.find(query)
        spec = self.world.spec(phone.id)
        if not spec.set_up:
            raise LookupError("This iPhone isn't set up in Mobster, so there's nothing to forget.")
        with self.runtime.lock:
            if self.busy(phone) is not None:
                raise LookupError("A task is running on it. Stop it first.")
        forgotten = phone.id
        spec.set_up, spec.runner, spec.build = False, "stopped", {"state": "idle", "started_at": None, "line": 0}
        spec.trusted = spec.trusted  # Trust stays on the phone
        self._drop_prefs(forgotten)
        self.release_profiles(forgotten)
        return forgotten

    def disconnect(self, phone):
        spec = self.world.spec(phone.id)
        if not spec.set_up:
            raise LookupError("This iPhone isn't set up yet, so it isn't in use.")
        with self.runtime.lock:
            if self.busy(phone) is not None:
                raise LookupError("A task is running on it. Stop it first.")
            self._set_disconnected(phone.id, True)
        spec.runner = "stopped"
        return phone.id

    def connect(self, phone):
        spec = self.world.spec(phone.id)
        self._set_disconnected(phone.id, False)
        if spec.set_up and spec.plugged:
            spec.runner = "running"
            return None
        return "Connect your iPhone first"


class World:
    """The scripted phones of one service, and the person's hands on them (plug, trust, unlock)."""

    def __init__(self, phones=None, build_seconds=0.0):
        import uuid
        self.specs = list(phones if phones is not None else specs())
        self.build_seconds = build_seconds
        self.lock = threading.RLock()
        # Each world's phones have addresses of their own: a task holds its phone's device lease (sota_world.phone_url).
        self.token = uuid.uuid4().hex[:8]

    def spec(self, udid):
        found = next((spec for spec in self.specs if spec.udid == udid), None)
        if found is None:
            raise LookupError("No such scripted phone")
        return found

    def plug(self, spec):
        with self.lock:
            if spec not in self.specs:
                self.specs.append(spec)
            spec.plugged = True
        return spec

    def unplug(self, spec):
        spec.plugged = False

    def trust(self, spec):
        spec.trusted = True

    def developer_mode(self, spec, on=True):
        spec.developer_mode = on

    def start_build(self, spec):
        if not spec.trusted:
            raise LookupError("Tap Trust on the iPhone first.")
        spec.build = {"state": "running", "started_at": time.time(), "line": 0}
        if self.build_seconds <= 0:
            self._built(spec)
            return

        def run():
            step = self.build_seconds / len(BUILD_LINES)
            for index in range(len(BUILD_LINES)):
                spec.build["line"] = index
                time.sleep(step)
            self._built(spec)
        threading.Thread(target=run, name="fleet-world-build", daemon=True).start()

    def _built(self, spec):
        spec.build = {"state": "succeeded", "started_at": None, "line": len(BUILD_LINES)}
        spec.developer_mode = True
        spec.set_up, spec.runner = True, "running"

    def setup_state(self, spec):
        """GET /api/devices/{id}/setup for a scripted phone: setup_service.SetupService.state's shape."""
        device = {"udid": spec.udid, "name": spec.name, "model": spec.model, "modelName": spec.model_name,
                  "ios": spec.ios, "trusted": spec.trusted,
                  "developerMode": spec.developer_mode if spec.trusted else None}
        building = spec.build["state"] == "running"
        running = spec.set_up and spec.runner == "running"

        def step(id_, title, done, detail, action=None, blocked_by=None, working=False):
            waiting = None if done or working else blocked_by
            return {"id": id_, "title": title, "state": "done" if done else "working" if working else
                    "blocked" if waiting else "todo", "detail": detail, "action": action, "blocked_by": waiting,
                    "commands": []}

        phone_ok = spec.plugged and spec.trusted
        steps = [
            step("tools", "Get your Mac ready", True, "Xcode and the iPhone connection tools are ready."),
            step("api_key", "Connect your AI account", True, "Mobster's agent uses your Claude account."),
            step("phone", "Connect your iPhone", phone_ok,
                 f"“{spec.name}” is connected and trusts this Mac." if phone_ok else
                 "Unlock your iPhone and tap Trust on it." if spec.plugged else
                 "Plug your iPhone into this Mac with a cable and unlock it."),
            step("wda_build", "Install Mobster's helper on your iPhone", spec.set_up,
                 "Mobster's helper is installed on your iPhone." if spec.set_up else
                 "The first install takes about 2 minutes. Keep your iPhone unlocked and plugged in."
                 if building else "Xcode signs Mobster's helper with your Apple Account and installs it on "
                 "your iPhone.",
                 "build", blocked_by=None if phone_ok else "phone", working=building),
            step("wda_running", "Start Mobster's helper", running, "Mobster is connected to your iPhone." if running
                 else "Mobster's helper keeps the connection to your iPhone open while Mobster runs.", "start",
                 blocked_by="phone" if not phone_ok else None if spec.set_up else "wda_build"),
            step("live", "Allow Mobster to tap and type", True, "Tasks can tap, scroll and type on your iPhone.",
                 "enable_live"),
            step("video", "See your iPhone live", running, "Your iPhone's screen is streaming." if running else
                 "A live picture of your screen confirms everything works.",
                 blocked_by=None if running else "wda_running"),
        ]
        lines = list(BUILD_LINES[:spec.build["line"] + 1]) if building else []
        return {"complete": all(item["state"] == "done" for item in steps), "steps": steps,
                "device": device if spec.plugged else None, "devices": [device] if spec.plugged else [],
                "chosen": spec.udid if spec.set_up else None, "teams": [TEAM],
                "tools": {"xcode": {"state": "ok", "version": "Xcode 26.4", "app": "/Applications/Xcode.app"},
                          "libimobiledevice": True, "homebrew": True, "bundled": True,
                          "install": {"state": "idle", "line": None, "error": None}},
                "build": {"state": spec.build["state"], "error": None, "log_tail": lines,
                          "started_at": (round(spec.build["started_at"] * 1000) if spec.build.get("started_at")
                                         else None),
                          "first": not spec.set_up, "expires_at": None, "expired": False, "renewal": False,
                          "problem": None, "team": TEAM["id"] if building or spec.set_up else None},
                "runner": {"state": "running" if running else "starting" if building else "stopped", "error": None},
                "keys": {"openai": False, "anthropic": True, "jev": False}, "preflight": []}


def build_runtime(state_dir, world_=None, *, pace=0.0, phone_apps=None):
    """A Runtime over ``world_`` (a new World by default) with Smart on the scripted model: (runtime, world).
    ``state_dir``: the journal's folder. Each phone runs tasks on its own ScriptedPhone."""
    from mobile_agent import tracks
    tracks.load()
    the_world = world_ or World()
    world.PACE["value"] = pace

    class WorldRuntime(DemoRuntime):
        one_target = False
        fleet_class = ScriptedFleet

        def __init__(self, config):
            self.world = the_world
            super().__init__(config)

        def smart_client(self):
            return engines.acquire_client(engines.smart_key())  # the world's scripted model

        def scripted_models(self):
            return False

        def target_status(self):
            spec = next(spec for spec in self.world.specs if spec.primary)
            ready = spec.plugged and spec.set_up and spec.runner == "running" and not spec.locked
            return {**READY, "ready": ready, "health": "Unlock your iPhone." if spec.locked else None}

        def status(self):
            value = super().status()
            ready = self.target_status()["ready"]
            value.update(live_enabled=bool(ready and not self.closing), device_ready=ready)
            return value

        def open_driver(self, run, trace, setup):
            phone = self.phone_for(run)
            spec = self.world.spec(phone.id if phone is not None and phone.id else
                                   next(item.udid for item in self.world.specs if item.primary))
            driver = setup.hold(world.ScriptedPhone(spec.state, frames=True))
            if run.stop.is_set():
                return None
            world.preflight(self, run, driver)
            if run.stop.is_set():
                return None
            if (run.app or {}).get("bundleId"):
                driver.execute("LAUNCH_APP", run.app["bundleId"], None)
            return driver

    state_dir.mkdir(parents=True, exist_ok=True)
    primary = next(spec for spec in the_world.specs if spec.primary)
    config = serve_config(wda_url=world.phone_url(f"{the_world.token}-{primary.udid[-6:]}"), enable_live=True,
                          state_db=str(state_dir / "mobster.sqlite3"), env_file=None, data_dir=None, port=0)
    runtime = WorldRuntime(config)
    return runtime, the_world


class FleetModel(world.ScriptedModel):
    """sota_world's scripted model, plus one task whose answer differs from phone to phone: "List my notes" reads each
    phone's own notes (a digest whose merge keeps the shared ones once)."""

    def complete(self, messages, schema, timeout=60, reasoning=None, **kwargs):
        from mobile_agent.frontier import prompt_text
        if "list my notes" not in prompt_text(messages).casefold():
            return super().complete(messages, schema, timeout, reasoning, **kwargs)
        if "items" in schema.get("properties", {}):  # the task contract: it only reads
            return {"items": [{"kind": "READ", "app": "Notes", "what": "the notes", "payload": "", "act": "none",
                               "count": "",
                               "condition": "", "quote": ""}]}, {}
        self.usage["calls"] += 1
        turn = world.Turn(prompt_text(messages), ())
        if not turn.in_app(world.NOTES):
            actions, answer = [world.launch(world.NOTES)], None
        else:
            titles = [label for _, role, label in turn.rows if role == "Cell"]
            actions, answer = (
                [world.act("DONE")], "Notes on this iPhone:\n" + "\n".join(f"- {title}" for title in titles))
        found = [{"id": 1, "status": "found", "value": titles[0]}] if answer and titles else []
        out = {"thought": "", "plan": None, "notes_add": [], "checklist_updates": found, "actions": actions,
               "answer": answer}
        if "data_json" in schema.get("properties", {}):
            out["data_json"] = None
        return out, {}


def scripted_engines():
    """(apply, restore): Smart on the scripted model, as dashboard/qa/live_demo.py does."""
    saved = (engines.smart_key, engines.acquire_client, engines.release_client)

    def apply():
        engines.smart_key = lambda env=None: "scripted"
        engines.acquire_client = lambda key: FleetModel()
        engines.release_client = lambda client: None

    def restore():
        engines.smart_key, engines.acquire_client, engines.release_client = saved

    return apply, restore
