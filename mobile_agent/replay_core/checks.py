"""The check front end: `mobster test` replays a check's recorded path, heals it, and keeps the cache.

verify/runner.py's VerifyRun calls three things when a suite runs it with replay on:

- ``CheckReplay.recorder()`` while Smart runs: the frontier's actions become a path;
- ``CheckReplay.run(cancelled)`` instead of Smart when a path is cached: the replay, with Smart healing a step that
  changed (for that step only) unless the run is strict (``--no-heal``);
- ``CheckReplay.after(result)`` once the verdict is in: what results.json says about the replay (``replay`` and
  ``healed``), and what is kept: a passing run's path goes to the cache, its heals to ``<check>.heal.yaml``.

Modes: ``auto`` replays and heals (the default of `mobster test`), ``strict`` replays and fails on a change
(``--no-heal``), ``fresh`` runs Smart and records (``--fresh``), ``off`` neither replays nor records.

The verdict never changes: replayed or not, healed or not, the assertions are read from the tree after the flow.
"""

import hashlib
import time
from pathlib import Path

from .engine import STRONG, Heal, Replayer, match, screen_likeness
from .heal import HealError
from .path import describe, new_path, replayable_count
from .recorder import Recorder, literal_text
from .store import Cache, env_of, revision

MODES = ("auto", "strict", "fresh", "off")
HEAL_TURNS = 6
LOOKAHEAD = 4
# The same recorded step healed this often in one run stops the replay: the screen keeps undoing the heal.
HEALS_PER_STEP = 2
NOTICE = ("Mobster is replaying this check's recorded steps with no model. Recorded step {n} was: {action}. On this "
          "screen {why}. Do what that step was for on the screen you see, then carry on with the check. The replay "
          "takes over again as soon as the screen matches a later recorded step.")
LIVE_NOTICE = ("Mobster is replaying this check's recorded steps with no model. Recorded step {n} ({action}) is one "
               "you do each run: the text it types isn't in the check's steps. Do it now, then carry on with the "
               "check. The replay takes over again as soon as the screen matches the next recorded step.")


def identity(check, binding=None, asset_proof=None):
    """What a check's path depends on: its steps, its app and how it starts; never its expectations."""
    out = {"kind": "check", "steps": list(check.steps), "bundle": check.bundle_id,
            "app": (check.app_path or "").rsplit("/", 1)[-1] or None, "args": list(check.launch_args),
            "env": dict(sorted(check.launch_env.items())), "url": check.open_url, "reset": check.reset}
    business = getattr(check, "business", None)
    if business is not None:
        if binding is None:
            from ..verify.business import load_business_binding
            from ..verify.checks import project_root
            binding = load_business_binding(check, project_root(check.source))
        out["business"] = {"adapter": business.adapter, "version": "mobster.business/1",
                           "fixture": business.fixture, "seedSha256": binding.action_digest}
    if getattr(check, "conditions", None):
        out["conditions"] = check.conditions
    seed = getattr(check, "seed", None)
    if getattr(check, "fixture", ()):
        out["fixture"] = {"names": list(check.fixture), "keychain": getattr(seed, "keychain", False),
                          "pasteboard": getattr(seed, "pasteboard", "")}
    if asset_proof is not None and (getattr(check, "fixture", ()) or getattr(check, "conditions", None)):
        # Assertion baselines and expected business values do not change the action path.
        out["evidence"] = {name: asset_proof[name] for name in ("seedSha256", "mediaSha256", "pushSha256")}
    return out


def _source_hash(source):
    try:
        return hashlib.sha256(Path(source).read_bytes()).hexdigest() if source else None
    except OSError:
        return None


def _proof(check):
    """Freeze the effective check, including fixture-flattened steps and the current check's assertions."""
    from dataclasses import replace

    from ..verify.checks import check_to_dict
    seed = getattr(check, "seed", None)
    return {"check": check_to_dict(replace(check, fixture_origin=None)),
            "seed": {"files": [(str(source), destination) for source, destination in
                               getattr(seed, "files", ())],
                     "keychain": getattr(seed, "keychain", False),
                     "pasteboard": getattr(seed, "pasteboard", ""),
                     "risky": list(getattr(seed, "risky", ()))},
            "l10n": getattr(check, "l10n", False), "a11yScope": getattr(check, "a11y_scope", "")}


def _saved_check(source):
    from dataclasses import replace

    from ..devkit import links
    from ..verify.checks import CheckError, load_check, project_root
    from ..verify.fixtures import FixtureError, with_fixtures
    root = project_root(source)
    try:
        check = load_check(source)
        if any(links.OPEN_STEP.match(step) for step in check.steps):
            check = replace(check, steps=links.expand_steps(check.steps, links.discover(root, deep=False)))
        return with_fixtures(check, root)
    except (CheckError, FixtureError, links.LinksError) as error:
        raise ValueError(str(error)) from None


def _same_assertions(actual, saved):
    """Planning may add audits, but cannot remove an assertion from the saved check."""
    return (actual.expect == saved.expect and actual.throughout == saved.throughout
            and actual.somewhere == saved.somewhere and actual.business == saved.business
            and set(saved.audit).issubset(actual.audit)
            and (saved.a11y is None or actual.a11y == saved.a11y or
                 (saved.a11y == "warn" and actual.a11y == "strict")))


def cache_dir_for(runs_dir):
    """`.mobster/cache/replay` beside `.mobster/runs` (runs_dir's sibling ``cache/replay``)."""
    from pathlib import Path
    return Path(runs_dir).resolve().parent / "cache" / "replay"


def text_sources(check):
    return list(check.steps) + list(check.launch_args) + list(check.launch_env.values()) + [check.open_url or ""]


def history_line(number, step):
    target = step.get("target") or {}
    what = target.get("label") or step.get("app") or ""
    return f"{number}.0: {step.get('op')} {what[:60]!r}" + (" text=…" if step.get("text") or step.get("param")
                                                            else "") + " -> screen changed (replayed)"


class CheckReplay:
    """Replay state for one VerifyRun."""

    def __init__(self, run, mode):
        if mode not in MODES:
            raise ValueError(f"replay must be one of {', '.join(MODES)}")
        self.run, self.mode = run, mode
        self.cache = Cache(cache_dir_for(run.runs_dir))
        self.identity = None
        self.path = None
        self.reason = None         # why this run didn't replay, in a sentence
        self.result = None
        self.guard = None
        self._recorder = None
        self.heal_cost = 0.0
        self.heal_file = None
        self._looked = False
        self.base_revision = ""
        self.pin = None
        self._pin_error = None
        self._pin_refused = False
        self._pending_heals = 0
        self._legacy_pending = False
        self._source_hash = None
        self._proof = None
        self._run_proof = None
        self._asset_proof = None

    def env(self):
        from ..verify.smart import is_real_device
        run = self.run
        real = is_real_device(run.manager)
        model = getattr(run.target, "device_type", None)
        if real and model == "iPhone":
            model = None  # WDA's fallback is not a physical model identity.
        return env_of(getattr(run.app, "version", None), getattr(run.target, "runtime", None),
                      "iphone" if real else "simulator", device_type=model)

    def refuse_pin(self, reason):
        """Report pre-action repeat refusal even when asset admission precedes cache lookup."""
        self._looked = True
        self._pin_refused = True
        self._pin_error = self.reason = reason
        from ..verify.runner import CouldntRun
        raise CouldntRun("replay", reason)

    def lookup(self):
        """The cached path to replay, or None (``reason`` says why not). Looked up once per run."""
        if self._looked:
            if self._pin_error:
                from ..verify.runner import CouldntRun
                raise CouldntRun("replay", self._pin_error)
            return self.path
        if self.mode == "off":
            return None
        binding = getattr(self.run, "business_binding", None)
        if getattr(self.run.check, "business", None) is not None:
            from ..verify.business import load_business_binding
            from ..verify.checks import CheckError, project_root
            from ..verify.runner import CouldntRun
            if not self.run.check.source:
                raise CouldntRun("business", "An app-state check must be a saved project check file.")
            try:
                current = load_business_binding(self.run.check, project_root(self.run.check.source))
            except CheckError as error:
                raise CouldntRun("business", str(error),
                                 "Fix the enrolled app-state fixture, then run again.") from None
            if binding is not None and binding.digest != current.digest:
                raise CouldntRun("business", "The enrolled app-state fixture changed before replay admission.")
            binding = self.run.business_binding = current
        self._source_hash = _source_hash(self.run.check.source)
        self._run_proof = _proof(self.run.check)
        if self.run.check.source:
            try:
                saved = _saved_check(self.run.check.source)
                if not _same_assertions(self.run.check, saved):
                    raise ValueError("the saved check's assertions changed after admission")
                self._proof = _proof(saved)
            except (OSError, ValueError) as error:
                self._looked = True
                self.reason = str(error)
                if self.mode == "strict" or self.pin is not None:
                    self.refuse_pin(self.reason)
                return None
        else:
            self._proof = self._run_proof
        from . import heal as heals
        try:
            self._asset_proof = heals.proof_assets(self.run.check)
        except heals.HealError as error:
            self._looked = True
            self.reason = f"the check's evidence could not be frozen: {error}"
            if self.mode == "strict" or self.pin is not None:
                self.refuse_pin(self.reason)
            return None
        self.identity = identity(self.run.check, binding, self._asset_proof)
        self._looked = True
        env = self.env()
        path = self.cache.load(self.identity, env)
        self._legacy_pending = bool(path and path.get("pending"))
        self.base_revision = revision(path) if path else ""
        if self.pin is not None:
            pin_env = {**env, "runtime": getattr(self.run.target, "runtime", None)}
            self._pin_error = self.pin.bind(self.identity, pin_env,
                                            "" if self._legacy_pending else self.base_revision)
            if self._pin_error:
                self.refuse_pin(self._pin_error)
        source = self.run.check.source
        if source:
            from . import heal as heals
            try:
                document = heals.read(source)
                self._pending_heals = len(document["heals"]) if document is not None else 0
            except heals.HealError:
                self._pending_heals = 1
        if self.mode == "fresh":
            self.reason = "--fresh"
            return None
        if self._legacy_pending:
            self.reason = "an older unreviewed heal is in the cache; record again with --fresh"
            return None
        if env["model"] is None:
            self.reason = "the device model is unknown"
            return None
        if path is None:
            others = self.cache.others(self.identity)
            if others:
                was = others[0]
                if was.get("app_version") != env["app_version"]:
                    self.reason = (f"the app changed (recorded on {was.get('app_version') or 'an unknown version'}, "
                                   f"this is {env['app_version'] or 'an unknown version'})")
                elif was.get("ios") != env["ios"]:
                    self.reason = (f"iOS changed (recorded on iOS {was.get('ios') or '?'}, this is iOS "
                                   f"{env['ios'] or '?'})")
                elif was.get("device") != env["device"]:
                    self.reason = "it was recorded on another kind of device"
                elif was.get("model") != env["model"]:
                    self.reason = (f"the device model changed (recorded on {was.get('model') or 'an unknown model'}, "
                                   f"this is {env['model'] or 'an unknown model'})")
                else:
                    self.reason = "no matching recorded run"
            else:
                self.reason = "no recorded run yet"
            return None
        if self.mode == "strict" or self.pin is not None:
            live = next(((index, step) for index, step in enumerate(path["steps"], 1) if step.get("live")), None)
            if live is not None:
                index, step = live
                self.reason = f"recorded step {index} ({describe(step)}) needs Smart"
                if self.pin is not None:
                    self.refuse_pin(f"A fixed replay cannot run: {self.reason}.")
                return None
        if not replayable_count(path):
            self.reason = "every recorded step needs the model"
            return None
        self.path = path
        return path

    def recorder(self):
        if not self._looked:
            self.lookup()  # capture the approved base before Smart spends time acting
        self._recorder = Recorder(text_policy=literal_text(text_sources(self.run.check)))
        return self._recorder

    def replay(self, cancelled=None):
        """Replay the cached path on the run's driver. Returns engine.Result."""
        run = self.run
        from .. import engines
        from ..verify.smart import check_approver, check_guard
        # Without a key nothing can heal: a changed step fails the check, as with --no-heal.
        self.can_heal = self.mode != "strict" and self.pin is None and not self._pending_heals \
            and bool(run.key or engines.smart_key())
        heal = self._heal if self.can_heal else None
        guard = self.guard = check_guard(run.manager, run.app.bundle_id)
        names = {run.app.bundle_id: run.app.name}
        def emit(event):
            run.event(event)
            if event.get("event") == "replay_step":
                run.record_step(op=event["op"], text=f"Replayed: {event['action']}",
                                target={"id": None, "label": event.get("label"), "role": event.get("role")},
                                typed=event.get("typed"), changed=True)
            elif event.get("event") == "replay_drift":
                run.progress(f"  Step {event['step']} changed: {event['why']}")

        replayer = Replayer(run.driver, approve=check_approver(run.manager), heal=heal,
                            live=None if self.mode == "strict" or self.pin is not None else self._live,
                            emit=emit, cancelled=lambda: run._stop.is_set() or bool(cancelled and cancelled()),
                            frame=lambda label: run.frame(label), on_commit="deny", app_names=names,
                            allows_app=guard.allows_app if guard is not None else None,
                            dialogs=self._responder(emit))
        self._steps = self.path["steps"]
        self._healed_at = {}
        try:
            result = replayer.run(self.path)
        finally:
            if guard is not None and guard.left:
                run._left_app = guard.left
        if result.status == "drift" and self._pending_heals:
            result.reason += " Review the pending heal with mobster test heal before healing another change."
        if guard is not None:
            # The final action can itself open another app. Check that screen too, before VerifyRun reads its
            # evidence or captures a verdict frame (there may be no next replay step to check it).
            bundle = getattr(result.snapshot, "bundle_id", None)
            if bundle and bundle != "com.apple.springboard":
                guard.allows_app(bundle)
            run._left_app = guard.left
        self.result = result
        return result

    def _responder(self, emit):
        """Answers the dialogs a replay meets from the check's ``dialogs``; None when the run has no policy."""
        run = self.run
        if getattr(run, "dialogs", None) is None:
            return None
        from ..verify.dialogs import Responder
        return Responder(run.dialogs, run.driver, emit=emit, log=run.log)

    def _heal(self, index, step, snapshot, why, done_steps, limit=None):
        self._healed_at[index] = self._healed_at.get(index, 0) + 1
        if self._healed_at[index] > HEALS_PER_STEP:
            return Heal(False, reason=f"step {index + 1} changed again after it healed")
        feedback = NOTICE.format(n=index + 1, action=describe(step), why=why)
        return self._smart(index, done_steps, feedback, start=index, limit=limit)

    def _live(self, index, step, snapshot, done_steps):
        feedback = LIVE_NOTICE.format(n=index + 1, action=describe(step))
        return self._smart(index, done_steps, feedback, start=index + 1)

    def _smart(self, index, done_steps, feedback, *, start, limit=None):
        from .. import engines, harness_api, smart_events
        from ..model_clients import cost_usd
        from ..verify.smart import check_approver, smart_request
        run = self.run
        steps = self._steps
        key = run.key or engines.smart_key()
        if not key:
            return Heal(False, reason="Smart has no key, so the step couldn't heal")
        spent = run.cost_usd + self.heal_cost
        budget = float(run.check.max_usd) - spent
        if budget <= 0:
            return Heal(False, reason=f"the check's ${run.check.max_usd:.2f} spend cap was reached", status="budget")
        recorder = Recorder(text_policy=literal_text(text_sources(run.check)))
        rejoin = {}
        candidates = [j for j in range(start, min(len(steps), start + LOOKAHEAD + 1))
                      if not steps[j].get("live") and steps[j].get("op") not in ("LAUNCH_APP", "HOME", "WAIT")]

        def observer(record):
            recorder.observe_step(record)
            screen = recorder.latest
            if screen is None or "at" in rejoin or "breaker" in rejoin:
                return
            for j in candidates:
                ok, _, _ = match(steps[j], screen)
                if ok and screen_likeness(steps[j], screen) >= STRONG:
                    rejoin["at"] = j
                    return
            if limit is not None and len(recorder.steps) >= limit:
                rejoin["breaker"] = True  # the next action would heal more than half the path

        def stopping():
            return "at" in rejoin or "breaker" in rejoin or run._stop.is_set()

        client = engines.build_client(key=key)
        events = []
        unknown = []

        def on_event(event):
            run.event({**event, "heal": index + 1} if isinstance(event, dict) else event)
            if (isinstance(event, dict) and event.get("event") == "frontier_failed"
                    and event.get("outcome") == "unknown"):
                unknown.append(event)  # an action that may have landed (frontier/act.py:_execute)
            if isinstance(event, dict) and event.get("event") == "frontier_action":
                events.append(event)
                op, label = event.get("operation") or "", event.get("target_label") or ""
                run.record_step(op=op,
                                text=f"Healed: {smart_events.step_text(op, label, {run.app.bundle_id: run.app.name},
                                                                       text=event.get('text'))}",
                                target={"id": None, "label": label or None, "role": None},
                                typed=event.get("text") if op in smart_events.TYPING else None,
                                changed=event.get("changed"))

        try:
            agent = engines.build_frontier(recorder.wrap(run.driver), client, apps={run.app.bundle_id: run.app.name},
                                           emit=on_event, max_cost_usd=budget, approve=check_approver(run.manager),
                                           cancelled=stopping, guard=self.guard, dialogs=True,
                                           dialog_policy=getattr(run, "dialogs", None),
                                           step_observer=observer, max_steps=HEAL_TURNS,
                                           max_seconds=max(20.0, float(run.check.max_seconds) - (time.monotonic()
                                                                                                 - run._t0)))
            initial = harness_api.InitialState(
                history=tuple(history_line(n, s) for n, s in enumerate(done_steps, 1))[-20:], feedback=feedback,
                reason="a replayed check step changed on screen")
            outcome = agent.run(smart_request(run.app.name, run.check.steps), initial=initial)
        finally:
            if self.guard is not None and self.guard.left:
                run._left_app = self.guard.left
            engines.release_client(client)
        cost = cost_usd(getattr(client, "model", ""), outcome.get("usage") or getattr(client, "usage", {}) or {}) or 0
        self.heal_cost += cost
        status = outcome.get("status")
        if unknown and status != "done" and "at" not in rejoin:
            # An action's outcome is unknown and the heal never settled it: stop, so no retry sends it again.
            reason = str(outcome.get("reason") or outcome.get("answer") or status or "")[:300]
            return Heal(False, steps=list(recorder.steps), cost_usd=cost, status="unknown_outcome", reason=reason)
        if "breaker" in rejoin:
            return Heal(False, steps=list(recorder.steps), cost_usd=cost, status="breaker",
                        reason="more than half the recorded steps would heal")
        if "at" in rejoin:
            return Heal(True, steps=list(recorder.steps), resume_at=rejoin["at"], cost_usd=cost)
        if status == "done":
            return Heal(True, steps=list(recorder.steps), done=True, cost_usd=cost, status=status)
        reason = str(outcome.get("reason") or outcome.get("answer") or status or "")[:300]
        return Heal(False, steps=list(recorder.steps), cost_usd=cost, status=status,
                    reason=f"Smart couldn't heal step {index + 1} ({status}{': ' + reason if reason else ''})")

    # -- after the verdict -------------------------------------------------------------------------------------

    def summary(self):
        """results.json's ``replay`` for this run."""
        result = self.result
        recorded = self._recorder.steps if self._recorder is not None else []
        approved = self.base_revision if self.path is not None else None
        fixed = bool(self.pin is not None and approved and self.mode != "fresh")
        if result is None:
            if (self.mode == "strict" or self._pin_refused) and self._looked and self.path is None:
                return {"mode": "unavailable", "status": "unavailable", "reason": self.reason, "steps": 0,
                        "replayed": 0, "healed": 0, "live": 0, "costUsd": 0, "saved": False, "cache": None,
                        "pendingHeals": 0, "approvedRevision": None, "fixed": False}
            return {"mode": "live", "reason": self.reason, "steps": len(recorded), "replayed": 0, "healed": 0,
                    "live": len(recorded), "costUsd": None, "saved": False, "cache": None,
                    "pendingHeals": 0, "approvedRevision": None, "fixed": False}
        mode = "healed" if result.heals or result.live else "replayed"
        return {"mode": mode, "status": result.status, "reason": result.reason or None,
                "steps": len(self.path["steps"]), "replayed": result.replayed, "healed": len(result.heals),
                "live": result.live, "seconds": result.seconds, "costUsd": round(result.cost_usd, 5),
                "saved": False, "cache": None, "pendingHeals": self._pending_heals,
                "approvedRevision": approved, "fixed": fixed}

    def after(self, result):
        """Add ``replay`` and ``healed`` to the run's ``result``, and keep what a passing run proved."""
        info = self.summary()
        healed = [dict(heal) for heal in (self.result.heals if self.result is not None else ())]
        passed = result.get("verdict") == "passed"
        refreshing_a11y = bool(getattr(self.run.check, "a11y_refresh", False))
        # A trial with the saved check's steps and app name shares its cache file, so saving replaces the approved path.
        draft_trial = bool(getattr(self.run.check, "draft_trial", False))
        # A new heal cannot become an approved path during baseline replacement, even when the only failed
        # assertion is a new accessibility finding (which baseline refresh otherwise accepts).
        unstaged = bool(refreshing_a11y and self.result is not None and self.result.status == "done" and healed
                        and result.get("verdict") in ("passed", "failed"))
        try:
            if self.result is None:
                if passed and not refreshing_a11y and not draft_trial and self.mode != "off" \
                        and self._recorder is not None and self._recorder.steps:
                    info.update(self._save_new())
            elif self.result.status == "done" and passed:
                if not refreshing_a11y:
                    info.update(self._save_replayed(healed))
                    unstaged = bool(healed and not info.get("pendingHeals"))
            # A failed or uncertain action never revokes the approved path.
        except Exception as error:
            self.run.log(f"replay cache not written: {type(error).__name__}: {error}")
            unstaged = unstaged or bool(passed and healed)
        if unstaged:
            from ..verify.verdict import EXIT_CODES
            message = ("The check healed during accessibility baseline refresh; its approved path and baseline were "
                       "kept." if refreshing_a11y else
                       "The check's assertions held, but its healed path could not be staged for review.")
            fix = ("Run mobster test without --update-a11y-baseline to review the heal, then refresh the baseline "
                   "in a separate run." if refreshing_a11y else
                   "Inspect the check and replay cache, then run mobster test --fresh.")
            info.update(status="proposal_unavailable", reason=message, saved=False, pendingHeals=0)
            result.update(verdict="couldnt_run", exit_code=EXIT_CODES["couldnt_run"], summary=message,
                          reason={"class": "replay", "message": message, "fix": fix})
        result["replay"] = info
        result["healed"] = healed
        if self.heal_file is not None:
            result["healDiff"] = str(self.heal_file)
        return result

    def _save_new(self):
        if self.pin is not None:
            return {}  # Repetitions observe one frozen baseline; they do not publish a new one.
        if not self._looked:
            self.lookup()
        if self.identity is None or self._asset_proof is None:
            self.run.log("the check's evidence was not frozen; the replay path was not saved")
            return {}
        steps = self._recorder.steps
        env = self.env()
        if env["model"] is None:
            return {}  # Without a model this recording cannot safely replay on the next acquired device.
        path = new_path(steps, kind="check", identity=self.identity, env=env, source_run=self.run.run_id)
        with self.cache.locked():
            current = self.cache.load(self.identity, env)
            if (revision(current) if current else "") != self.base_revision:
                self.run.log("replay cache changed during Smart; the new path was not saved")
                return {}
            if self._legacy_pending and self.mode != "fresh":
                self.run.log("an older unreviewed heal remains; use --fresh to replace its path")
                return {}
            file = self.cache.save(path)
            self._drop_superseded_heals(file.name)
        return {"saved": True, "cache": file.name, "steps": len(steps)}

    def _drop_superseded_heals(self, cache_name):
        """A new recording replaces the steps a waiting heal diff described: that diff goes with them."""
        source = self.run.check.source
        if not source:
            return
        from . import heal as heals
        if heals.discard(source, self.cache.directory, cache_name):
            self.run.log(f"{heals.heal_file(source).name} described steps recorded again; removed it")

    def _save_replayed(self, healed):
        if not healed or self.pin is not None:
            return {}
        old = self.path
        path = dict(old)
        path["steps"] = list(self.result.steps)
        path["strict"] = list(self.result.strict)
        source = self.run.check.source
        if not source:
            self.run.log("the healed path has no check file for review; approved steps were kept")
            return {}
        try:
            current_check = _saved_check(source)
        except (OSError, ValueError):
            current_check = None
        if (_source_hash(source) != self._source_hash or current_check is None
                or _proof(self.run.check) != self._run_proof
                or _proof(current_check) != self._proof):
            self.run.log("the check changed during healing; the approved path was kept without staging a proposal")
            return {}
        from . import heal as heals
        proof = self._asset_proof
        if proof is None:
            self.run.log("the check's evidence was not frozen before replay; proposal was not staged")
            return {}
        try:
            current_proof = heals.proof_assets(current_check, include_a11y=proof["a11ySha256"] is not None)
        except HealError:
            current_proof = None
        if current_proof != proof:
            self.run.log("the check's evidence changed during healing; proposal was not staged")
            return {}
        with self.cache.locked():
            current = self.cache.load(self.identity, self.env())
            if current is None or revision(current) != self.base_revision:
                self.run.log("approved replay changed during healing; proposal was not staged")
                return {}
            app = self.run.app
            self.heal_file = heals.record(source, healed, run_id=self.run.run_id,
                                          cache_file=self.cache._file(self.identity, self.env()),
                                          candidate=path, base_revision=self.base_revision,
                                          check_sha256=self._source_hash,
                                          effective_sha256=heals.effective_digest(current_check), proof=proof,
                                          app={"bundle": app.bundle_id, "version": app.version or None},
                                          ios=self.env()["ios"], device=self.env()["device"])
        for item in healed:
            item["diff"] = str(self.heal_file)
        return {"saved": False, "cache": self.cache._file(self.identity, self.env()).name,
                "pendingHeals": len(healed)}


__all__ = ["CheckReplay", "identity", "cache_dir_for", "MODES"]
