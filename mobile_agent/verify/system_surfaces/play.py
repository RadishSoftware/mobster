"""Run a check's surfaces on the simulator the run holds, after its flow."""

from ..smart import is_real_device
from ..verdict import CouldntRun
from . import spec
from .stage import Stage, SurfaceMiss
from .surfaces import DRIVERS

NOT_ON_SIMULATOR = "Not on a simulator: "


class Context:
    """What a driver needs from the simulator manager, with its errors as a check failing rather than the run."""

    def __init__(self, run):
        self.run = run

    def _try(self, what, *args):
        try:
            return what(self.run.target, *args)
        except Exception as error:
            raise SurfaceMiss(str(error) or type(error).__name__) from None

    def open_url(self, url):
        self._try(self.run.manager.open_url, url)

    def launch(self, bundle, env):
        self._try(self.run.manager.launch, bundle, (), env)

    def install(self, path):
        self._try(self.run.manager.install, path)


def refuse_unsupported(run):
    """The run can't drive these, so it says so before it touches the simulator."""
    if is_real_device(run.manager):
        raise CouldntRun("environment", NOT_ON_SIMULATOR + "system surfaces are driven on a simulator. They press, "
                         "swipe and lock the screen, which Mobster never does on your iPhone.",
                         "Run the check with --sim.")
    for surface in run.check.surfaces:
        via = surface.params.get("via", "url")
        if surface.kind == "app_clip" and via != "url":
            raise CouldntRun("environment", NOT_ON_SIMULATOR + f"an App Clip card opened by {via} has no simulator "
                             "path. Only its invocation URL runs.",
                             "Use via: url, or test the card on a phone by hand.")


def play(run, stage, ctx, surface):
    """One surface: drive it, tap its ``pick``, and read: (assertion results, the sentence that failed, its frame)."""
    results, miss, frames = [], None, []
    stage.shot = lambda: frames.append(run.frame(f"surface-{surface.kind}-{len(frames) + 1}"))
    try:
        stage.follow_the_front()
        results, tree = DRIVERS[surface.kind](stage, surface, ctx)
        if surface.pick is not None:
            stage.tap_node(stage.wait_for(None, f"{surface.pick.render()} to pick", selector=surface.pick), "the pick")
            after, tree = stage.settle(surface.then)
            results = [*results, *after]
        elif surface.kind == "share_sheet":
            stage.tap(tree.size[0] / 2, tree.size[1] * .08, "outside the sheet to close it")
    except SurfaceMiss as error:
        miss = str(error)
    # A CouldntRun (an action with no answer) skips this: nothing more is sent to a simulator in an unknown state.
    stage.activate(run.app.bundle_id)
    return results, miss, next((frame for frame in reversed(frames) if frame), None)


def finding(surface, text, frame, run):
    frame = run._relative(frame)
    return {"id": f"{surface.label()}", "guideline": None, "level": "fail", "frame": frame,
            "message": f"{surface.label()}: {text}", "evidence": frame or "the surface"}


def drive(run):
    """Every surface in order. A surface that fails adds a fail finding and the next one still runs; a refused
    environment or an action with no answer ends the run."""
    surfaces = run.check.surfaces
    if not surfaces:
        return
    if not spec.enabled():
        raise CouldntRun("usage", f"The check has surfaces, which are off. Set {spec.FLAG}=1.")
    refuse_unsupported(run)
    from ..runner import _now  # the run's own clock, so a test that sets it sets this one too
    stage = Stage(run.driver, log=run.log, sleep=run._sleep, clock=_now)
    ctx = Context(run)
    run.surface_reports = []
    with run.lock:
        for surface in surfaces:
            run._check_stop()
            results, miss, frame = play(run, stage, ctx, surface)
            failed = [item for item in results if not item.ok]
            run.event({"event": "surface", "surface": surface.kind, "name": surface.label(),
                       "held": len(results) - len(failed), "failed": len(failed), "miss": miss})
            run.log(f"surface {surface.label()}: {len(results) - len(failed)} of {len(results)} held"
                    + (f"; {miss}" if miss else ""))
            if miss:
                run.findings.append(finding(surface, miss, frame, run))
            for item in failed:
                run.findings.append(finding(surface, f"{item.text}: {item.observed}", frame, run))
            run.surface_reports.append({"surface": surface.kind, "name": surface.label(), "ok": not (miss or failed)})


def note(run):
    """The verdict's tail for a run that drove surfaces: how many held."""
    reports = getattr(run, "surface_reports", ())
    return f"; {sum(item['ok'] for item in reports)} of {len(reports)} system surfaces held" if reports else ""
