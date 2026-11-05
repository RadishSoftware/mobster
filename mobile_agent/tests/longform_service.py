"""The real service on longform_world's phone and models, and a stand-in for core's LongSession."""

import json
import sys
import threading
import time
import types
from dataclasses import dataclass, field

from mobile_agent.tests import sota_world as world
from mobile_agent.tests.longform_models import ScriptedLongModel
from mobile_agent.tests.longform_world import EXTRA_APPS, SPEED, new_long_state, pace


@dataclass
class StubBudget:
    kind: str = "items"
    minutes: float | None = None
    items: int | None = 50
    strict: bool = False
    phone_seconds_cap: float = 600.0
    item_noun: str = "Reels"
    label: str = "About 50 Reels"
    item_kind: str = "reel"


@dataclass
class StubNotebook:
    run_id: str = ""
    request: str = ""
    kind: str = "feed"
    focus: str | None = None
    preferences: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    items: list = field(default_factory=list)
    facts: list = field(default_factory=list)
    counters: dict = field(default_factory=lambda: {"seen": 0, "kept": 0, "ads": 0, "repeats": 0, "off_topic": 0,
                                                   "unreadable": 0, "extract_failed": 0})
    phone: dict = field(default_factory=lambda: {"first_action_at": None, "released_at": None, "waited_s": 0.0})


class StubSession:
    """Core's LongSession (DESIGN.md § 13.1) as far as the feed track's macros use it: a notebook, a budget, events,
    yielding, and the extraction pool on ``extract_factory``'s clients."""

    def __init__(self, run_id="r1", request="", *, kind="feed", focus="basketball", items=50, phone_seconds_cap=600.0,
                 cap_usd=1.0, emit=None, extract_factory=None, steering=None, cancelled=None,
                 preferences=(), item_noun="Reels", interactive=True):
        from mobile_agent.longform.budget import NOUNS
        self.run_id, self.request, self.focus = run_id, request, focus
        self.interactive = interactive
        self.plan = {"id": "p_stub", "kind": kind, "focus": focus}
        self.budget = StubBudget(items=items, phone_seconds_cap=phone_seconds_cap, item_noun=item_noun,
                                 label=f"About {items} {item_noun}" if items
                                 else f"{phone_seconds_cap / 60:.0f} minutes",
                                 item_kind=NOUNS.get(item_noun.casefold(), ("row",))[0])
        self.notebook = StubNotebook(run_id=run_id, request=request, kind=kind, focus=focus,
                                     preferences=list(preferences))
        self.preferences = list(preferences)
        self.events = []
        self.frames = {}                       # item id -> key frame bytes
        self.cap_usd, self.spent = cap_usd, 0.0
        self._emit = emit
        self.steering, self.cancelled = steering, cancelled
        self.yield_reason = None               # a test sets "steer", "stopped" ...
        self.started = time.monotonic()
        self.models = types.SimpleNamespace(extract=extract_factory or (lambda: ScriptedLongModel("claude-haiku-5-5")),
                                            digest=None, synth=None, synth_fallback=None, nav=None)
        self._pool = None
        self._lock = threading.RLock()
        self._last_progress = 0.0

    # events
    def emit(self, event):
        self.events.append(dict(event))
        if self._emit is not None:
            try:
                self._emit(event)
            except Exception:  # noqa: BLE001 -- an extraction that lands after the run finished (core drains first)
                pass

    def mask(self, text):
        return text

    def elapsed(self):
        return time.monotonic() - self.started

    def should_yield(self):
        if self.cancelled is not None and self.cancelled():
            return "stopped"
        if self.yield_reason:
            return self.yield_reason
        if self.steering is not None and getattr(self.steering, "pending", lambda: 0)():
            return "steer"
        left = self.budget_left()
        if (left["items"] is not None and left["items"] <= 0) or left["phone_seconds"] <= 0 or left["usd"] <= 0:
            return "budget"
        return None

    def budget_left(self):
        kept = sum(1 for item in self.notebook.items if item.skip in (None, "off_topic"))
        items = None if self.budget.strict or self.budget.items is None else max(0, self.budget.items - kept)
        return {"items": items, "phone_seconds": self.budget.phone_seconds_cap - self.phone_seconds(),
                "usd": self.cap_usd - self.spent_usd()}

    def add_item(self, item, *, frame=None):
        with self._lock:
            nb = self.notebook
            item.n = len(nb.items) + 1
            item.id = f"i{item.n}"
            if item.skip is None and item.identity and any(
                    other.identity == item.identity and other.skip != "repeat" for other in nb.items):
                item.skip = "repeat"
            nb.items.append(item)
            nb.counters["seen"] += 1
            if item.skip is None:
                nb.counters["kept"] += 1
            elif item.skip == "ad":
                nb.counters["ads"] += 1
            elif item.skip == "repeat":
                nb.counters["repeats"] += 1
            elif item.skip == "unreadable":
                nb.counters["unreadable"] += 1
            if frame:
                self.frames[item.id] = frame
        self.emit({"event": "longform_item", "itemId": item.id, "n": item.n, "kind": item.kind,
                   "status": "skipped" if item.skip else "captured", "skip": item.skip, "source": item.source})
        return item

    def update_item(self, item_id, *, extract=None, skip=None, frame=None):
        with self._lock:
            item = next((i for i in self.notebook.items if i.id == item_id), None)
            if item is None:
                return
            if extract is not None:
                item.extract = extract
            if skip and item.skip != skip:
                counters = self.notebook.counters
                if item.skip is None:
                    counters["kept"] -= 1
                key = {"ad": "ads", "repeat": "repeats", "off_topic": "off_topic", "unreadable": "unreadable"}.get(skip)
                if key:
                    counters[key] += 1
                item.skip = skip
            if frame:
                self.frames[item_id] = frame
                item.frame_id = f"f{item_id}"
        event = {"event": "longform_item", "itemId": item_id, "n": item.n, "kind": item.kind,
                 "status": "skipped" if item.skip and item.skip != "off_topic" else "extracted", "skip": item.skip}
        if extract:
            event.update(summary=extract.get("summary"), tags=extract.get("tags"), relevant=extract.get("relevant"))
        if frame:
            event["_frame"] = frame
        self.emit(event)

    def add_fact(self, text, item_ids=(), kind="fact", source="model"):
        fact = types.SimpleNamespace(id=f"f{len(self.notebook.facts) + 1}", text=text, item_ids=tuple(item_ids),
                                     kind=kind, source=source, turn=0)
        self.notebook.facts.append(fact)
        return fact

    def progress(self, phase, *, label=None):
        now = time.monotonic()
        if now - self._last_progress < .5:
            return
        self._last_progress = now
        counters = self.notebook.counters
        self.emit({"event": "longform_progress", "phase": phase, "label": label,
                   "items": {"seen": counters["seen"], "kept": counters["kept"], "ads": counters["ads"],
                             "repeats": counters["repeats"], "offTopic": counters["off_topic"],
                             "target": self.budget.items}})

    def extract_pool(self):
        with self._lock:
            if self._pool is None:
                from mobile_agent.longform.extract import ExtractPool
                self._pool = ExtractPool(self, self.models.extract)
            return self._pool

    def spent_usd(self):
        return self.spent

    def phone_seconds(self):
        return time.monotonic() - self.started

    def close(self):
        if self._pool is not None:
            self._pool.close()


def stub_session_module(registry):
    module = types.ModuleType("mobile_agent.longform.session")
    module.for_run = lambda run_id: registry.get(run_id)
    module.LongSession = StubSession
    return module


class _patch_modules:
    """patch.dict(sys.modules, modules), also setting each as an attribute of its package (``from . import session``
    looks there first)."""

    def __init__(self, modules):
        self.modules = modules
        self.saved = {}

    def __enter__(self):
        import importlib
        for name, module in self.modules.items():
            parent, _, child = name.rpartition(".")
            package = importlib.import_module(parent)
            self.saved[name] = (sys.modules.get(name), getattr(package, child, None), package, child)
            sys.modules[name] = module
            setattr(package, child, module)
        return self

    def __exit__(self, *exc):
        for name, (module, attribute, package, child) in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
            if attribute is None:
                try:
                    delattr(package, child)
                except AttributeError:
                    pass
            else:
                setattr(package, child, attribute)
        return False


def fast():
    """The feed's real waits off (unit tests and the e2e at SPEED 0): the second frame's delay, the stuck pause and
    the extraction retry's wait."""
    from contextlib import ExitStack
    from unittest.mock import patch

    from mobile_agent.longform import extract, feed, items
    stack = ExitStack()
    for target, name in ((feed, "F1_AFTER"), (feed, "STUCK_PAUSE"), (feed, "FRAME_WAIT"), (extract, "RETRY_WAIT"),
                         (items, "TAP_PAUSE"), (items, "SCROLL_PAUSE")):
        stack.enter_context(patch.object(target, name, 0 if name != "FRAME_WAIT" else .05))
    return stack


TOKEN = "f" * 43


class Service:
    """The real HTTP service, Runtime, frontier loop and tracks on the scripted long-form phone and scripted models
    (the test_sota_e2e pattern), for the long-form tests and the bench. Offline: no phone, no keys, a scratch HOME, a
    server on port 0. ``variant``: "after" (core's long-form track) or "before" (today's loop: no long-form track at
    all). ``paced``: the latency model on. ``nav`` / ``extract``:
    client factories for real models (the bench's --real); scripted by default."""

    def __init__(self, *, variant="after", state=None, paced=False, nav=None, extract=None, task_limit=None):
        self.variant, self.paced = variant, paced
        self.state = state if state is not None else new_long_state()
        self.nav, self.extract = nav, extract
        self.task_limit = task_limit
        self.calls = []
        self.models = []

    def addCleanup(self, fn, *args):        # seam_support.isolate's interface
        self.stack.callback(fn, *args)

    def __enter__(self):
        import os
        import shutil
        import tempfile
        from contextlib import ExitStack
        from pathlib import Path
        from unittest.mock import patch

        from mobile_agent import engines, server
        from mobile_agent.journal import Lease
        from mobile_agent.tests.seam_support import isolate
        from mobile_agent.tests.test_engines import CLEAN
        from mobile_agent.tui.session import DemoRuntime, serve_config
        self.stack = stack = ExitStack()
        try:
            root = Path(tempfile.mkdtemp(prefix="mobster-longform-"))
            self.root = root
            stack.callback(shutil.rmtree, root, True)
            stack.enter_context(patch.dict(os.environ, {**CLEAN, "HOME": str(root), "MOBSTER_NO_DEVICES": "1",
                                                        "MOBSTER_MEMORY_DIR": str(root / "memory")}, clear=True))
            for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
                os.environ.pop(name, None)
            isolate(self)
            saved = (dict(world.LATENCY), world.PACE["value"], SPEED["value"])
            stack.callback(self._restore_pace, saved)
            pace(self.paced)
            if not self.paced:
                stack.enter_context(fast())
            kwargs = engines.frontier_kwargs
            settle = None if self.paced else 0
            nav = self.nav or (lambda: ScriptedLongModel("claude-sonnet-5-5", variant=self.variant, log=self.calls))

            def acquire(key):
                client = nav()
                self.models.append(client)
                return client
            for target, value in (("smart_key", lambda env=None: "scripted"), ("acquire_client", acquire),
                                  ("release_client", lambda client: None),
                                  ("frontier_kwargs", lambda *a, **kw: {**kwargs(*a, **kw), **(
                                      {"settle_seconds": settle} if settle is not None else {})})):
                stack.enter_context(patch.object(engines, target, value))
            stack.enter_context(patch("mobile_agent.journal.Lease.device",
                                      side_effect=lambda _: Lease(root / "device.lock")))
            if self.variant == "after":
                self._script_core_clients(stack)
            if self.variant == "before":
                # Today's main: no long-form track at all, even where core's is on the branch.
                from mobile_agent import tracks
                importers = tracks._importers
                stack.enter_context(patch.object(tracks, "_importers", lambda: tuple(
                    pair for pair in importers() if pair[0] != "longform")))
            phone_state = self.state
            service = self

            class Scripted(DemoRuntime):
                pace = 0

                def smart_client(self):
                    return engines.acquire_client(engines.smart_key())  # the world's scripted model

                def scripted_models(self):
                    return False

                def apps(self):
                    known = {app["bundleId"] for app in super().apps()}
                    return super().apps() + [dict(app) for app in EXTRA_APPS if app["bundleId"] not in known]

                def open_driver(self, run, trace, setup):
                    driver = setup.hold(world.ScriptedPhone(phone_state, frames=True))
                    service.driver = driver
                    if run.stop.is_set():
                        return None
                    world.preflight(self, run, driver)
                    if run.stop.is_set():
                        return None
                    if (run.app or {}).get("bundleId"):
                        driver.execute("LAUNCH_APP", run.app["bundleId"], None)
                    return driver

            state_db = root / "Library" / "Application Support" / "app.mobster.desktop" / "state" / "mobster.sqlite3"
            state_db.parent.mkdir(parents=True)
            config = serve_config(wda_url="demo://phone", enable_live=True, state_db=str(state_db), env_file=None,
                                  data_dir=None, port=0)
            self.runtime = Scripted(config)
            stack.callback(self.runtime.close)
            if self.task_limit is not None:
                stack.enter_context(patch.object(type(self.runtime), "task_cap", lambda runtime, *a, **k:
                                                 self.task_limit))
            httpd = server.BoundedServer(("127.0.0.1", 0), server.make_handler(self.runtime, TOKEN, app_session=None))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            stack.callback(httpd.server_close)
            stack.callback(httpd.shutdown)
            self.url = f"http://127.0.0.1:{httpd.server_address[1]}"
        except BaseException:
            stack.close()
            raise
        return self

    def __exit__(self, *exc):
        self.stack.close()
        return False

    @staticmethod
    def _restore_pace(saved):
        world.LATENCY.clear()
        world.LATENCY.update(saved[0])
        world.PACE["value"], SPEED["value"] = saved[1], saved[2]

    def _script_core_clients(self, stack):
        """With core's track on the branch, its extraction, digest and synthesis clients are scripted too (core builds
        them with model_clients.chat_client)."""
        from unittest.mock import patch

        from mobile_agent import model_clients
        extract = self.extract

        def chat_client(model, reasoning="low", *, key=None):
            if extract is not None and model.startswith("claude-haiku"):
                return extract()
            return ScriptedLongModel(model, variant=self.variant, log=self.calls)
        stack.enter_context(patch.object(model_clients, "chat_client", chat_client))

    def call(self, method, path, body=None, *, origin="app"):
        import urllib.error
        import urllib.request
        import uuid
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if origin:
            headers["X-Mobster-Origin"] = origin
        if method == "POST":
            headers["Idempotency-Key"] = uuid.uuid4().hex
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    def start(self, text, thread_id=None, origin="app", **body):
        if self.variant == "after":
            body.setdefault("longform", {})
        if thread_id is None:
            status, data = self.call("POST", "/api/threads", {"message": {"text": text, **body}}, origin=origin)
        else:
            status, data = self.call("POST", f"/api/threads/{thread_id}/messages", {"text": text, **body},
                                     origin=origin)
        if status != 201:
            raise RuntimeError(f"the task didn't start: {status} {data}")
        return (data.get("thread") or {}).get("id") or thread_id, (data.get("run") or {}).get("id")

    def run_of(self, run_id):
        status, data = self.call("GET", f"/api/runs/{run_id}")
        if status != 200:
            raise RuntimeError(f"no run {run_id}: {status}")
        return data["run"]

    def until(self, check, what, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(.02)
        raise TimeoutError(f"timed out waiting for {what}")

    def finished(self, run_id, timeout=60, approve=False):
        """The run once it finished; ``approve``: say yes to every approval it asks for meanwhile (the bench)."""
        def check():
            run = self.run_of(run_id)
            approval = run.get("approval")
            if approve and approval and approval.get("kind") != "clarify":
                self.call("POST", f"/api/runs/{run_id}/approval", {"id": approval["id"], "approve": True},
                          origin=None)
            return run if run.get("finishedAt") else None
        return self.until(check, f"task {run_id} to finish", timeout)

    def asked(self, run_id, kind=None, timeout=60):
        def pending():
            approval = self.run_of(run_id).get("approval")
            return approval if approval and (kind is None or approval.get("kind") == kind) else None
        return self.until(pending, f"task {run_id} to ask ({kind or 'anything'})", timeout)

    def report(self, run_id, run=None):
        run = run or self.run_of(run_id)
        events = run.get("events") or ()
        found = next((e.get("report") for e in reversed(events) if e.get("event") == "longform_report"), None)
        if found is None:
            result = next((e for e in reversed(events) if e.get("event") in ("result", "run_result")), {})
            found = (result.get("summary") or {}).get("report") or result.get("report")
        return found
