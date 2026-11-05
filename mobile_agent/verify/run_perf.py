"""What a run measures of the app under test: cold launch to the app in front and to its first stable screen, and,
on a simulator, its memory and CPU while the steps run (perf.py says how)."""

from . import perf
from .smart import is_real_device
from .verdict import CouldntRun

LAUNCH_SETTLE_SECONDS, LAUNCH_READ_PAUSE = 5, .1


class PerfMeasure:
    def _measure_launch(self, started, in_front):
        """Cold launch to the app in front, and to its first screen that two reads in a row agree on (perf.py). Fails
        soft: a number that could not be taken is left out, and the reads after it say if WebDriverAgent is gone."""
        if not in_front:
            return
        from .runner import _now as now  # the run's own clock, so a test that sets it sets this one too
        self._launch["foregroundMs"] = round((now() - started) * 1000)
        if not is_real_device(self.manager):   # the pid is read at each sample: WDA may name it after the launch
            self._sampler = perf.Sampler(lambda: self._app_pid)
            self._sampler.start()
        deadline = now() + LAUNCH_SETTLE_SECONDS
        try:
            seen, seen_at = None, None
            while now() < deadline:
                tree = self.read_tree(glance=True)
                at = now()
                layout = tree.layout() if tree.nodes else None
                if layout is not None and layout == seen:
                    self._launch["interactiveMs"] = round((seen_at - started) * 1000)
                    break
                seen, seen_at = layout, at
                self._sleep(LAUNCH_READ_PAUSE)
        except CouldntRun as error:
            self.log(f"launch time: not measured ({error.message})")

    def _perf(self):
        """The run's ``perf`` (perf.build); the sampler stops here, with the steps."""
        summary = self._sampler.stop() if self._sampler is not None else None
        self._sampler = None
        simulator = not is_real_device(self.manager)
        capture = getattr(self.evidence, "capture", None)
        hangs = perf.hang_summary(line.text for line in capture.all()) if capture is not None else None
        return perf.build(launch=dict(self._launch), sampler_summary=summary, hangs=hangs, simulator=simulator)
