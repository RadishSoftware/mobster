"""Observe required intermediate accessibility screens and retain incomplete-coverage evidence."""

import threading
from contextlib import contextmanager

from .assertions import evaluate_on


class ScreenCoverage:
    def _incomplete_screen(self, reason):
        with self._screen_coverage_lock:
            if self._screen_result_sealed:
                return
            if self._screen_coverage_issue is None:
                self._screen_coverage_issue = reason
        self.log(f"screen coverage incomplete: {reason}")

    @contextmanager
    def _screen_frame(self, label):
        tracked = label in ("launch", "step")
        if tracked:
            with self._screen_coverage_lock:
                admitted = not self._screen_frames_sealed
                if admitted:
                    self._screen_frames_pending += 1
        else:
            admitted = True
        try:
            yield admitted
        finally:
            if tracked and admitted:
                with self._screen_coverage_lock:
                    self._screen_frames_pending -= 1

    def _seal_screen_frames(self):
        with self._screen_coverage_lock:
            self._screen_frames_sealed = True
            self._verdict_thread = threading.get_ident()
            if self._screen_frames_pending and self._screen_coverage_issue is None:
                self._screen_coverage_issue = "An intermediate screen observation was in flight at the verdict"

    def _reserve_source_read(self):
        """Admit a source request before it starts, including reads nested inside driver.observe."""
        with self._screen_coverage_lock:
            if self._screen_result_sealed:
                return None
            if self._screen_frames_sealed and threading.get_ident() != self._verdict_thread:
                reason = "A screen read began after the final verdict started"
                if self._screen_coverage_issue is None:
                    self._screen_coverage_issue = reason
                rejected = True
            else:
                self._screen_frames_pending += 1
                rejected = False
        if rejected:
            self.log(f"screen coverage incomplete: {reason}")
            return None
        released = False

        def release():
            nonlocal released
            with self._screen_coverage_lock:
                if not released:
                    released = True
                    self._screen_frames_pending -= 1

        return release

    def _late_screen_frame(self, label, path):
        with self._screen_coverage_lock:
            late = label in ("launch", "step") and self._screen_frames_sealed
        if late:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                self.log(f"late screen frame could not be removed: {type(error).__name__}")
        return late

    def reserve_step_frame(self):
        """Count a keyless step before its capture thread is scheduled; return its once-only release."""
        with self._screen_coverage_lock:
            if self._screen_frames_sealed:
                return None
            self._screen_frames_pending += 1
        released = False

        def release():
            nonlocal released
            with self._screen_coverage_lock:
                if not released:
                    released = True
                    self._screen_frames_pending -= 1

        return release

    def _check_pending_screens(self):
        with self._screen_coverage_lock:
            pending = self._screen_frames_pending
        if pending:
            self._incomplete_screen("An intermediate screen capture was still in flight at the verdict")

    def _observe_screen(self, frame, tree=None):
        """Use one AX read for every requested per-screen check, with or without an image."""
        if not (self.check.throughout or self.check.somewhere or self._a11y or self._l10n):
            return
        try:
            tree = tree or self.read_tree()
        except Exception as error:
            self.log(f"screen observation skipped: {type(error).__name__}")
            self._incomplete_screen("An accessibility screen could not be read or evaluated")
            return
        with self._screen_coverage_lock:
            if self._screen_result_sealed or (self._screen_frames_sealed
                                              and threading.get_ident() != self._verdict_thread):
                return
            try:
                self._sweep(frame, tree)
                self._audit_screen(frame, tree)
            except Exception as error:
                self.log(f"screen observation skipped: {type(error).__name__}")
                self._incomplete_screen("An accessibility screen could not be read or evaluated")

    def _finalize_screen_coverage(self):
        """Freeze the reported marker with collector writes and pending reads under one short lock."""
        with self._screen_coverage_lock:
            if self._screen_coverage_final is not None:
                return dict(self._screen_coverage_final)
            if self._screen_frames_pending and self._screen_coverage_issue is None:
                self._screen_coverage_issue = "A screen observation was still in flight at the verdict"
            self._screen_result_sealed = True
            self._screen_coverage_final = {
                "complete": self._screen_verdict_audited and self._screen_coverage_issue is None
                            and (self._a11y is None or self._a11y.screens > 0),
                "a11yScreens": self._a11y.screens if self._a11y is not None else 0,
            }
            return dict(self._screen_coverage_final)

    def _audit_screen(self, frame, tree):
        """Add the already-read screen to the accessibility audit."""
        collectors = [item for item in (self._a11y, self._l10n, self._ax_device) if item is not None]
        for collector in collectors:
            if collector.add(tree, frame) is False:
                self._incomplete_screen("A screen audit reached its 60-screen limit")

    def _sweep(self, frame, tree):
        check = self.check
        if not (check.throughout or check.somewhere):
            return
        frame = self._relative(frame)
        fingerprint = tree.audit_fingerprint()
        for assertion, result in zip(check.throughout, evaluate_on(check.throughout, tree)):
            text = assertion.name or result.text
            if result.ok:
                continue
            prior = self._unframed_findings.get((fingerprint, text)) if frame else None
            if prior is not None:
                prior["frame"] = prior["evidence"] = frame
                del self._unframed_findings[(fingerprint, text)]
            elif not any(item["id"] == text and item["frame"] == frame for item in self.findings):
                finding = {"id": text, "guideline": None, "level": "fail", "frame": frame,
                           "message": f"{text}: {result.observed}",
                           "evidence": frame or "an observed accessibility screen"}
                self.findings.append(finding)
                if frame is None:
                    self._unframed_findings[(fingerprint, text)] = finding
        for index, result in enumerate(evaluate_on(check.somewhere, tree)):
            if result.ok:
                self._held_somewhere.add(index)

    def _missing_somewhere(self):
        """A finding for each ``somewhere`` assertion no screen held."""
        # The run may not have reached that screen, so a person looks (needs_review) rather than the check failing.
        out = []
        for index, assertion in enumerate(self.check.somewhere):
            if index not in self._held_somewhere:
                text = assertion.name or assertion.render()
                out.append({"id": text, "guideline": None, "level": "review", "frame": None,
                            "message": f"{text}: no screen the run visited showed it. Open the screens in the "
                                       "report; if the app has it, add a step that goes there.",
                            "evidence": f"{len(self.frames)} frames in the run"})
        return out
