import json
import os
import re
import time
import traceback
from pathlib import Path

from .. import project_env
from . import perf, proof, snapshot_run
from .checks import dump_check, project_root
from .smart import VIEW_SERVICE, is_real_device
from .tree import read_tree as read_tree_from
from .tree import screen_title

SCHEMA = "mobster.verify/1"
EXIT_CODES = {"passed": 0, "failed": 1, "needs_review": 2, "couldnt_run": 3}
# Smart statuses that end the flow early without an error: an assertion failing after one is class "blocked".
BLOCKING = frozenset({"blocked", "timeout", "max_steps", "approval_denied", "approval_timeout"})
STEP_FRAME_CAP, STEP_FRAMES_HEAD, STEP_FRAMES_TAIL = 60, 10, 10
# couldnt_run classes a crash of the app explains: the run then failed, class app_crashed (the app's failure, not
# the machine's). A stop, a usage error, a budget or an unknown outcome stays what it is.
CRASH_EXPLAINS = frozenset({"wda", "launch", "replay"})
HELD_BEFORE_FLOW = ("Every expectation already held before the flow ran, so this run doesn't show the flow works. "
                    "Add an expectation that only holds after it.")


class CouldntRun(Exception):
    def __init__(self, klass, message, fix=""):
        super().__init__(message)
        self.klass, self.message, self.fix = klass, message, fix or ""


def mobster_version():
    from .. import __version__
    return __version__


def _shell_quote(text):
    return text if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", text) else "'" + text.replace("'", "'\\''") + "'"


class Verdict:
    def _decide(self):
        """{verdict, klass, message, fix, results, tree, stable, alert, foreground} by the first rule that applies."""
        if self._ready_at is not None and "flow" not in self.timing and self.mode == "keyless":
            self.timing["flow"] = round(time.monotonic() - self._ready_at, 2)
        self._verdict_ms = self._ms()
        self.perf = self._perf()
        if self.error is not None and self.error["klass"] == "unknown_outcome":
            if not self._left_app:
                self._verdict_frames(None, ())
            return None
        action_failure = self._action_failure()
        if action_failure is not None:
            raise action_failure
        if self.error is not None:
            if not self._left_app:
                self._verdict_frames(None, ())
            crash = self._crash(suspect=True) if self.error["klass"] in CRASH_EXPLAINS else None
            return self._crashed(crash) if crash else None
        failure = self._smart_failure()
        if failure is not None:
            if not self._left_app:  # another app is in front on someone's phone: no frame of it
                self._verdict_frames(None, ())
            crash = self._crash(suspect=True) if failure.klass in CRASH_EXPLAINS else None
            if crash:
                return self._crashed(crash)
            raise failure
        from .system_surfaces.play import note as surfaces_note
        started = time.monotonic()
        expect = self.check.expect
        results, tree, stable = self._settle(expect, self.assert_timeout)
        if self._wire is not None:
            results = [*results, *self._wire.judge(self._conditions.net if self._conditions else None)]
        self.timing["assert"] = round(time.monotonic() - started, 2)
        frame = self._verdict_frames(tree, results)
        self._observe_screen(self._relative(frame), tree)
        self._check_pending_screens()
        if self._a11y is not None and self._a11y.screens == 0:
            self._incomplete_screen("No accessibility screen was audited")
        static_findings = self._audit_findings()
        snapshot_found = []
        if self.check.snapshot is not None:   # the screen against its baseline, outside the coverage lock
            self.snapshot, snapshot_found = snapshot_run.take(self, tree, self._relative(frame))
        with self._screen_coverage_lock:
            self.findings.extend(self._missing_somewhere())
            if self._a11y is not None:
                self.findings.extend(self._a11y.findings())
            if self._l10n is not None:
                self.findings.extend(self._l10n.findings())
            self.findings.extend(static_findings)
            self.findings.extend(snapshot_found)
            self._screen_verdict_audited = True
        coverage = self._finalize_screen_coverage()
        decided = {"results": results, "tree": tree, "stable": stable, "frame": frame, "alert": self._alert()}
        foreground = self._foreground()
        # A crash anywhere in the run fails it, even when the app came back and the expectations held.
        crash = self._crash(suspect=foreground != self.app.bundle_id)
        if crash:
            return self._crashed(crash, decided)
        failed = [result for result in results if not result.ok]
        status = self._smart_status
        still = "; the screen was still changing" if not stable else ""
        if status == "budget" and failed:
            return {**decided, "verdict": "couldnt_run", "klass": "budget",
                    "message": f"Smart reached its ${self.check.max_usd:.2f} spend cap and {len(failed)} of "
                               f"{len(results)} expectations failed{still}.",
                    "fix": "Raise --max-usd (at most 1.00), then run again."}
        if self._replay_drift:
            return {**decided, "verdict": "failed", "klass": "drift", "message": self._replay_drift,
                    "fix": "If the app changed on purpose, run `mobster test --fresh` to record the flow again; "
                           "without --no-heal, Smart heals the step and you review the diff."}
        if not foreground:
            return {**decided, "verdict": "couldnt_run", "klass": "wda",
                    "message": "WebDriverAgent could not confirm which app was in front at the end.",
                    "fix": "Check WebDriverAgent, then run the check again."}
        if foreground and foreground != self.app.bundle_id:
            alert = f"; an alert says \"{decided['alert']}\"" if decided["alert"] else ""
            return {**decided, "verdict": "failed", "klass": "app_not_running",
                    "message": f"{self.app.name} wasn't in front at the end: {foreground} was{alert}.",
                    "fix": None, "foreground": foreground}
        unavailable = self._observe_business(decided)
        if unavailable is not None:
            return unavailable
        if failed:
            message = f"{len(failed)} of {len(results)} expectations failed{still}"
            if status in BLOCKING:
                note = (self.agent or {}).get("note")
                return {**decided, "verdict": "failed", "klass": "blocked",
                        "message": f"Smart ended {status}: " + (note or "no note") + f". {message}", "fix": None}
            return {**decided, "verdict": "failed", "klass": "assertion", "message": message, "fix": None}
        over, unmeasured = perf.judge(self.check.perf_budget, self.perf)
        if over:
            return {**decided, "verdict": "failed", "klass": "budget_exceeded",
                    "message": " ".join(item.message for item in over),
                    "fix": "Find what the app does while it launches or runs, or raise the budget if the change "
                           "is on purpose. The report shows the numbers over the last runs."}
        broken = [item for item in self.findings if item["level"] == "fail"]
        if broken:
            return {**decided, "verdict": "failed", "klass": "finding",
                    "message": f"{len(broken)} finding{'' if len(broken) == 1 else 's'}: {broken[0]['message']}",
                    "fix": None}
        business_count = len(self.business_binding.spec.expect) if self.business_binding is not None else 0
        business_failed = [item for item in (self.business_result or {}).get("assertions", ()) if not item["ok"]]
        if business_failed:
            return {**decided, "verdict": "failed", "klass": "business",
                    "message": f"{len(business_failed)} of {business_count} app-state expectations failed.",
                    "fix": None}
        if not coverage["complete"] and (self.check.throughout or self._a11y is not None
                                         and self._a11y.level == "strict"):
            return {**decided, "verdict": "needs_review", "klass": "screen_coverage",
                    "message": f"The run did not observe every required screen: {self._screen_coverage_issue}.",
                    "fix": "Check WebDriverAgent and run the check again."}
        unsure = [item for item in self.findings if item["level"] == "review"]
        if unsure:
            return {**decided, "verdict": "needs_review", "klass": "finding",
                    "message": f"{len(unsure)} finding{'' if len(unsure) == 1 else 's'} for a person to judge: "
                               f"{unsure[0]['message']}", "fix": None}
        if unmeasured:
            return {**decided, "verdict": "needs_review", "klass": "budget_unmeasured",
                    "message": " ".join(item.message for item in unmeasured),
                    "fix": "Run the check on a simulator, or drop the budget key this run cannot measure."
                    if is_real_device(self.manager) else "Run the check again: the simulator gave no reading this "
                    "time, which says nothing about the budget."}
        if not (expect or self.check.wire or self.check.throughout or self.check.somewhere or self.check.audit
                or self.check.a11y or self.check.surfaces or business_count
                or self.check.snapshot):
            return {**decided, "verdict": "needs_review", "klass": "no_assertions",
                    "message": "The check has no expectations, so nothing decided the run. The frames show what "
                               "happened.", "fix": "Add expect: what must be true at the end."}
        held_ax = all(self.baseline.get("held") or [False]) if expect else True
        held_business = (all(self.baseline.get("businessHeld") or [False])
                         if business_count else True)
        if (self.check.steps and not self.check.wire and self.baseline.get("taken") and (expect or business_count)
                and held_ax and held_business):
            return {**decided, "verdict": "needs_review", "klass": "held_before_flow", "message": HELD_BEFORE_FLOW,
                    "fix": None}
        return {**decided, "verdict": "passed", "klass": None,
                "message": f"All {len(results) + business_count} expectations held{still}{surfaces_note(self)}",
                "fix": None}

    def _alert(self):
        try:
            text = self.driver.call("GET", "/alert/text", timeout=3)
            return text[:300] if isinstance(text, str) and text.strip() else None
        except Exception:
            return None

    def _verdict_frames(self, tree, results):
        """NN-verdict (the screen) and NN-verdict-ax (the same with the accessibility overlay)."""
        if self.driver is None or self.target is None:
            return None
        if is_real_device(self.manager):
            foreground = self._foreground()
            if not foreground or (foreground != self.app.bundle_id and not VIEW_SERVICE.fullmatch(foreground)):
                self.log("no verdict frame: the check's app is not confirmed in front")
                return None
        plain = self.frame("verdict")
        if plain is None:
            return None
        self._give_after_frame(plain)
        try:
            if tree is None:
                tree = read_tree_from(self.driver, pause=self._sleep)
            from .overlay import draw_overlay
            asserted = [(result, list(result.paths)) for result in results]
            path = self._frame_path("verdict-ax")
            draw_overlay(plain, tree, asserted, path)
            self._register_frame(path, "verdict-ax")
        except Exception as error:
            self.log(f"no overlay frame: {type(error).__name__}: {error}")
        return plain

    def _cap_frames(self):
        """At most 60 step frames: the first 10, the last 10, and an even spread of the rest."""
        frames = self.step_frames
        if len(frames) <= STEP_FRAME_CAP:
            return
        head, tail, middle = frames[:STEP_FRAMES_HEAD], frames[-STEP_FRAMES_TAIL:], \
            frames[STEP_FRAMES_HEAD:-STEP_FRAMES_TAIL]
        room = STEP_FRAME_CAP - len(head) - len(tail)
        stride = 2
        while -(-len(middle) // stride) > room:
            stride += 1
        keep = set(head) | set(tail) | set(middle[::stride])
        dropped = set(frames) - keep
        for relative in dropped:
            try:
                (self.run_dir / relative).unlink()
            except OSError:
                pass
        self.frames = [frame for frame in self.frames if frame not in dropped]
        self.step_frames = [frame for frame in frames if frame in keep]
        for step in self.steps:
            if step.get("frame") in dropped:
                step["frame"] = None

    def _compose(self, decided):
        coverage = self._finalize_screen_coverage()
        check, app, target = self.check, self.app, self.target
        self._cap_frames()
        if decided is None:
            error = self.error or {"klass": "internal", "message": "The run ended without a verdict.", "fix": ""}
            verdict, klass, message, fix = "couldnt_run", error["klass"], error["message"], error.get("fix") or None
            results, stable, alert, frame = [], None, None, None
            summary = message
        else:
            verdict, klass, message, fix = decided["verdict"], decided["klass"], decided["message"], decided["fix"]
            results, stable, alert, frame = decided["results"], decided["stable"], decided["alert"], decided["frame"]
            failed = [result for result in results if not result.ok]
            if verdict == "passed":
                total = len(results) + len(check.business.expect if check.business is not None else ())
                summary = f"{total} of {total} expectations held"
            elif klass == "finding":
                summary = message
            elif klass in ("assertion", "blocked", "budget"):
                summary = f"{failed[0].text} failed: {failed[0].observed}"
            else:
                summary = message
        frame_rel = Path(frame).relative_to(self.run_dir).as_posix() if frame else None
        assertions = [{"index": index, "assertion": assertion.to_dict(), "text": result.text, "ok": result.ok,
                       "observed": result.observed, "matches": [dict(match) for match in result.matches],
                       "frame": frame_rel}
                      for index, (assertion, result) in enumerate(zip((*check.expect, *check.wire), results))]
        draft = self.run_dir / "check.yaml"
        ax = next((f for f in reversed(self.frames) if f.endswith("-verdict-ax.jpg")), None)
        plain = frame_rel or next((f for f in reversed(self.frames) if f.endswith("-verdict.jpg")), None)
        last_step = self.step_frames[-1] if self.step_frames else None
        proof = [str(self.run_dir / relative) for relative in (ax, plain, last_step) if relative]
        result = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "verdict": verdict,
            "exit_code": EXIT_CODES[verdict],
            "summary": self._scrub(summary),
            "reason": {"class": klass, "message": self._scrub(message), "fix": fix},
            "mode": self.mode,
            "check": {"name": check.name, "steps": list(check.steps),
                      "expect": [assertion.to_dict() for assertion in (*check.expect, *check.wire)]
                      + ([check.snapshot.to_dict()] if check.snapshot else []),
                      **({"throughout": [assertion.to_dict() for assertion in check.throughout]}
                         if check.throughout else {}),
                      **({"somewhere": [assertion.to_dict() for assertion in check.somewhere]}
                         if check.somewhere else {}),
                      **({"audit": list(check.audit)} if check.audit else {}),
                      **({"a11y": check.a11y} if check.a11y else {}), "source": check.source},
            "app": ({"bundle_id": app.bundle_id, "name": app.name, "version": app.version,
                     "path": str(Path(app.path).resolve()) if app.path else check.app_path}
                    if app is not None else {"bundle_id": check.bundle_id, "name": None, "version": None,
                                             "path": check.app_path}),
            "device": ({"name": target.name, "udid": target.udid, "type": target.device_type,
                        "runtime": target.runtime} if target is not None
                       else {"name": None, "udid": None, "type": check.device, "runtime": check.runtime}),
            "assertions": assertions,
            **({"screen": {"title": screen_title(decided["tree"])}} if decided and decided.get("tree") else {}),
            "findings": [dict(item) for item in self.findings],
            **({"snapshot": dict(self.snapshot)} if self.snapshot else {}),
            "screenCoverage": coverage,
            **({"conditions": {**self._conditions.report(), "cleanup": self.conditions_cleanup}}
               if self._conditions is not None else {}),
            "perf": self.perf,
            "baseline": dict(self.baseline),
            "stable": stable,
            "steps": [dict(step) for step in self.steps],
            "agent": dict(self.agent) if self.mode == "smart" and self.agent is not None else None,
            "frames": list(self.frames),
            "proof": proof,
            "alert": alert,
            "report": str(self.run_dir / "report.html"),
            "draft_check": str(draft),
            "run_dir": str(self.run_dir),
            "repro": f"mobster verify --check {_shell_quote(str(draft))}",
            "seconds": round(time.monotonic() - self._t0, 1),
            "timing": {key: self.timing[key] for key in ("simulator", "install", "launch", "flow", "assert", "report")
                       if key in self.timing},
            "cost_usd": self.cost_usd if self.mode == "smart" else 0,
            "mobster": {"version": mobster_version()},
        }
        evidence = self._evidence_summary(verdict)
        if check.business is not None:
            result["check"]["business"] = check.business.to_dict()
            result["business"] = dict(self.business_result or {})
        if evidence is not None:
            result["evidence"] = evidence
        step_proof = self._step_proof(verdict, klass, stable, decided, plain, ax)
        if step_proof is not None:
            result["step_proof"] = step_proof
        return project_env.mask_deep(result, self.secrets)

    def _step_proof(self, verdict, klass, stable, decided, plain, ax):
        """Each step's log lines and network rows, the frame and tree the verdict used, and the cause line."""
        end = self._failed_ms if verdict == "couldnt_run" and self._failed_ms is not None else self._verdict_ms
        end = self._ms() if end is None else end
        try:
            lines = self.evidence.lines() if self.evidence is not None else []
            return proof.build(verdict=verdict, klass=klass, stable=stable, steps=self.steps, lines=lines, end_ms=end,
                               frame=plain, ax_frame=ax, tree=decided.get("tree") if decided else None,
                               verdict_ms=self._verdict_ms)
        except Exception as error:
            self.log(f"step proof: none ({type(error).__name__}: {error})")
            return None

    def _evidence_summary(self, verdict):
        if self.evidence is None:
            return None
        center = self._failed_ms if verdict == "couldnt_run" and self._failed_ms is not None else self._verdict_ms
        try:
            return self.evidence.summary(center_ms=center if center is not None else self._ms(), steps=self.steps,
                                         failed=verdict != "passed")
        except Exception as error:
            self.log(f"evidence: no summary ({type(error).__name__}: {error})")
            return None

    def _write(self, result):
        """check.yaml, result.json and report.html."""
        started = time.monotonic()
        root = project_root(self.run_dir) if self.runs_dir.parent.name == ".mobster" else None
        try:
            (self.run_dir / "check.yaml").write_text(dump_check(self.check, project_root=root), encoding="utf-8")
        except Exception as error:
            self.log(f"check.yaml not written: {type(error).__name__}: {error}")
        from .report import write_report
        try:
            write_report(self.run_dir, result, scrub=self._scrub)
        except Exception as error:
            self.log("".join(traceback.format_exception(type(error), error, error.__traceback__)))
        result["timing"]["report"] = round(time.monotonic() - started, 2)
        result["seconds"] = round(time.monotonic() - self._t0, 1)
        self._write_json(result)
        if self.secrets:
            project_env.sweep_files(self.run_dir, self.secrets)

    def _write_json(self, result):
        text = self._scrub(json.dumps(result, indent=1, ensure_ascii=False, allow_nan=False))
        temporary = self.run_dir / "result.json.tmp"
        temporary.write_text(text + "\n", encoding="utf-8")
        os.replace(temporary, self.run_dir / "result.json")
