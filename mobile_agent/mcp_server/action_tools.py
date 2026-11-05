"""The tools that act on the screen: tap, type, swipe, gestures, alerts, links, launch and wait_for."""

import os
import threading
import time
import traceback
from types import SimpleNamespace

from ..verify.assertions import normalize
from .action_journal import record_action
from .codes import SECURE_MASK
from .protocol import ToolResult
from .session import Outline, collapse, node_name, node_summary, node_target, role_name
from .support import (
    Phases,
    ToolError,
    cap_text,
    changed_words,
    describe_assertion,
    describe_selector,
    element_at,
    hit_point,
    is_check_error,
    jsonable,
    navbar_hint,
    quote,
    screen_changed,
    screen_fingerprint,
    sentence,
)

# Swipes with `until` stop before this, so the call stays inside CALL_BUDGET.
SWIPE_SECONDS = 30.0

LOGGED_TEXT = 40


class ActionTools:
    def tool_launch_app(self, args, call):
        bundle = args.get("bundle_id")
        if bool(bundle) == bool(args.get("restart")):
            raise ToolError("Pass bundle_id to open an app, or restart to quit the check's app and launch it again, "
                            "not both.")
        if args.get("restart"):
            return self._relaunch(args, call)

        def work(session, phases):
            run, driver = session.run, session.run.driver
            snapshot = driver.observe(timeout=10)
            self._permit_app(session, bundle)
            self._check_action_cancelled(session, call)
            if session.mode == "direct":
                run.launch_app(bundle)
            else:
                driver.call("POST", "/wda/apps/activate", {"bundleId": bundle}, timeout=30)
            after = self._settle(driver, snapshot, timeout=4, wait_seconds=1.0)
            changed = screen_changed(snapshot, after)
            text = f"Opened {bundle}"
            return {"op": "LAUNCH", "text": text, "changed": changed, "line": text + changed_words(changed),
                    "after": screen_fingerprint(after)}
        return self._act(args, call, work, "LAUNCH")

    def tool_home(self, args, call):
        def work(session, phases):
            driver = session.run.driver
            snapshot = driver.observe(timeout=10)
            driver.call("POST", "/wda/pressButton", {"name": "home"}, timeout=10)
            after = self._settle(driver, snapshot)
            changed = screen_changed(snapshot, after)
            return {"op": "HOME", "text": "Pressed Home", "changed": changed,
                    "line": "Pressed Home" + changed_words(changed), "after": screen_fingerprint(after)}
        return self._act(args, call, work, "HOME", check=False)

    def _act(self, args, call, work, operation, check=True):
        session = self._target_session(args)
        run = session.run
        started = time.monotonic()
        with self._locked(session), record_action(run.run_dir, operation):
            phases = Phases()
            if check:
                self._front(session)
            self._check_action_cancelled(session, call)
            # The code fields blacked out when the step began: the after frame is taken while the outline is read,
            # so it can still show a code the outline (read a moment later) no longer has.
            rects_before = list(session.secret_rects)
            try:
                done = work(session, phases)
            except ToolError:
                raise
            except Exception as error:
                self.log(f"Run {session.run_id}: the action failed after {phases.spent}: "
                         + "".join(traceback.format_exception(type(error), error, error.__traceback__, limit=6)))
                raise ToolError(self._action_error(error)) from None
            phases("act")
            step, shot = None, {}
            shooter = None
            try:
                if check:
                    # A link or tap may have switched apps: check before any frame of it is captured.
                    try:
                        self._front(session)
                    except ToolError as error:
                        raise ToolError(done["text"] + ". " + str(error)) from None
                if done.get("record", True):
                    release_frame = (run.reserve_step_frame() if session.mode != "direct" else lambda: None)
                    if release_frame is None:
                        raise ToolError("This run is finishing, so it cannot capture the action's screen.")

                    def take():
                        try:
                            shot["frame"] = run.frame("step")
                        except Exception as error:
                            self.log(f"Run {session.run_id}: the step frame failed ({type(error).__name__}).")
                        finally:
                            release_frame()
                    shooter = threading.Thread(target=take, daemon=True, name=f"mobster-mcp-frame-{session.run_id}")
                    try:
                        shooter.start()
                    except BaseException:
                        release_frame()
                        raise
                outline, alert = self._outline(session, phases, before=done.get("after"))
            except ToolError as error:
                # The action happened; what comes back says why the screen isn't shown (an app that isn't allowed
                # came up), in the tool's own words.
                raise ToolError(done["text"] + ". " + str(error)) from None
            except Exception as error:
                raise ToolError(done["text"] + ". " + self._action_error(error)) from None
            finally:
                if shooter is not None:
                    shooter.join(20)
                    phases("frame")
            frame = shot.get("frame")
            rects = rects_before + [rect for rect in session.secret_rects if rect not in rects_before]
            if frame and rects:
                if not self.codes.redact_file(session, str(frame), rects) or not os.path.isfile(str(frame)):
                    frame = None  # it couldn't be redacted, so it was deleted
            if shooter is not None:
                step = run.record_step(op=done["op"], text=done["text"], target=done.get("target"),
                                       typed=done.get("typed"), changed=done.get("changed"), frame=frame)
        seconds = round(time.monotonic() - started, 2)
        timing = dict(phases.spent)
        self.log(f"Run {session.run_id}: {done.get('log', done['text'])} ({seconds:.2f} s: "
                 + ", ".join(f"{name} {value:.2f}" for name, value in timing.items()) + ")")
        picture = self.imager(path=str(frame)) if args.get("image") and frame else None
        structured = {"ok": bool(done.get("ok", True)), "changed": done.get("changed"),
                      "step": jsonable(step) if step is not None else None, "screen": outline.structured(alert),
                      "seconds": seconds}
        structured.update(done.get("extra") or {})
        return ToolResult(cap_text(done["line"] + "\n" + outline.render(alert)), structured,
                          [picture] if picture else [])

    @staticmethod
    def _action_error(error):
        name = type(error).__name__
        if is_check_error(error):
            return sentence(error)
        if hasattr(error, "klass") or hasattr(error, "kind"):
            return f"The simulator couldn't do that: {error}. Call screen to see the current screen."
        if name == "DriverRejection":
            if getattr(error, "code", None) in getattr(error, "PRE_DISPATCH", ()):
                return "The screen changed before the action. Call screen for current refs."
            return "The action may have reached the device. Call screen to inspect it before another action."
        if name in ("TransportError", "TimeoutError", "URLError", "ConnectionError"):
            return "WebDriverAgent didn't answer in time. Call screen to inspect the device, or stop the run."
        return f"The action failed ({name}: {str(error)[:160]}). Call screen to see the current screen."

    def _resolve(self, session, tree, args, required=True):
        ref, target = args.get("ref"), args.get("target")
        if ref and target:
            raise ToolError("Pass ref or target, not both.")
        if not ref and not target:
            if required:
                raise ToolError("Pass ref (from the latest outline) or target (a selector) to say which element.")
            return None
        shown = {node.path: node for node in tree.shown()}
        if ref:
            # Refs are numbered across the run, so a ref from an earlier outline never names a node of this one.
            outline = session.outline
            if outline is None or ref not in outline.refs:
                if ref in session.book.issued:
                    raise ToolError(f"{ref} is from an earlier screen. Call screen for current refs.")
                raise ToolError(f"{ref} isn't on the latest outline. Call screen for current refs.")
            if tree.fingerprint() != outline.fingerprint or outline.refs[ref] not in shown:
                raise ToolError(f"{ref} is from an earlier screen. Call screen for current refs.")
            return shown[outline.refs[ref]]
        try:
            selector = self.api.parse_selector(target)
        except Exception as error:
            if is_check_error(error):
                raise ToolError(sentence(error)) from None
            raise
        matches = collapse([node for node in tree.select(selector) if node.path in shown])
        if len(matches) == 1:
            return matches[0]
        same_tree = session.outline is not None and tree is session.outline.tree
        session.outline = Outline(tree, session.book)
        if not same_tree:
            session.anchor = None
        outline = session.outline
        described = describe_selector(target)
        if matches:
            lines, named = outline.match_lines(matches)
            if not lines:
                raise ToolError(f"{len(matches)} elements match {described}, and none has a line in the outline. "
                                "Add id, label or role to the target to pick one.")
            more = len(matches) - named
            raise ToolError(f"{len(matches)} elements match {described}: " + "; ".join(lines)
                            + (f"; and {more} more" if more else "")
                            + ". Pass one of these refs, or add id or role to the target.")
        lines = outline.candidates(near=[target.get("label"), target.get("id"), target.get("value")])
        if not lines and target.get("role"):
            lines = outline.candidates([node for node in outline.nodes
                                        if role_name(node) == str(target["role"]).lower()])
        raise ToolError(f"No shown element matches {described}."
                        + (" Closest: " + "; ".join(lines) + "." if lines else "")
                        + " Call screen to see what is shown.")

    def tool_tap(self, args, call):
        def work(session, phases):
            driver = session.run.driver
            tree, snapshot = self._current(session, phases)
            node = self._resolve(session, tree, args)
            element = element_at(snapshot, node.path)
            if self._permit(session, "TAP", node, element, snapshot, call):
                element, snapshot = self._rebind(session, element, snapshot, call)
            if element is not None:
                driver.execute("TAP", element, snapshot, timeout=10)
            else:
                x, y = hit_point(node, tree)
                driver.tap_point(x, y, snapshot, timeout=10)
            phases("dispatch")
            after = self._settle(driver, snapshot)
            phases("settle")
            changed = screen_changed(snapshot, after)
            text = f"Tapped {quote(node_name(node))}"
            return {"op": "TAP", "text": text, "target": node_target(node), "changed": changed,
                    "line": text + changed_words(changed), "after": screen_fingerprint(after),
                    "extra": {"target": node_summary(node)}}
        return self._act(args, call, work, "TAP")

    @staticmethod
    def _check_gesture_cancelled(session, call):
        if session.cancelled.is_set() or call.cancelled.is_set() or call.stop.is_set():
            raise ToolError("This gesture was cancelled before dispatch. Nothing was sent.")

    def _gesture_screen(self, session, grounded, gesture, call):
        from ..wda.point_gestures import same_point_image
        stale = "The gesture's screen changed. Call screen with image=true and choose its points again."
        driver = session.run.driver

        def read():
            self._check_gesture_cancelled(session, call)
            snapshot = driver.observe(timeout=5)
            if snapshot.bundle_id != grounded["bundle"] or screen_fingerprint(snapshot) != grounded["fingerprint"]:
                raise ToolError(stale)
            return snapshot

        read()
        self._check_gesture_cancelled(session, call)  # a cancel during the first read stops before the capture
        picture = self._screenshot(session)
        after = read()
        self._check_gesture_cancelled(session, call)
        parts = [{"image_url": {"url": "data:image/jpeg;base64," + value}}
                 for value in (grounded["picture"], picture or "")]
        if not picture or not all(same_point_image(*parts, x * 100, y * 100) for x, y in gesture.points):
            raise ToolError(stale)
        return after

    def tool_drag_point(self, args, call):
        return self._point_gesture(args, call, "drag")

    def tool_hold_point(self, args, call):
        return self._point_gesture(args, call, "hold")

    def tool_flick_point(self, args, call):
        return self._point_gesture(args, call, "flick")

    def _point_gesture(self, args, call, kind):
        # Sent once: a failure after dispatch is never repeated.
        from ..contract_asks import point_spot
        from ..wda.point_gestures import GESTURE_SCHEMAS, check_gesture_path, validate_gesture_request
        schema = GESTURE_SCHEMAS[kind]
        try:
            gesture = validate_gesture_request(kind, {key: args[key] for key in schema["properties"] if key in args})
        except ValueError as error:
            raise ToolError(str(error)) from None
        operation = kind.upper() + "_POINT"

        def work(session, phases):
            if session.mode != "direct":
                raise ToolError(f"{kind}_point needs a direct device session. Finish this app check, then pass device.")
            grounded, session.point_screen = session.point_screen, None
            if grounded is None:
                raise ToolError(f"Call screen with image=true before each {kind}, then name its visible points.")
            snapshot = self._gesture_screen(session, grounded, gesture, call)
            check_gesture_path(snapshot, gesture)
            endpoints = [(gesture.start, gesture.start_label)]
            if gesture.end is not None:
                endpoints.append((gesture.end, gesture.end_label))
            node = None
            for (x, y), label in endpoints:
                _, element = point_spot(snapshot, x, y)
                node = SimpleNamespace(label=label, role="Point", placeholder="", identifier="", value="")
                self._permit(session, "TAP_XY", node, element, snapshot, call)
            node.label = gesture.label
            self._permit(session, operation, node, None, snapshot, call, forced=True)
            self._front(session)
            snapshot = self._gesture_screen(session, grounded, gesture, call)
            check_gesture_path(snapshot, gesture)
            self._check_gesture_cancelled(session, call)
            positions = (*gesture.start, *(gesture.end or ()))
            options = {"duration": gesture.duration, "timeout": 15 if kind == "hold" else 10}
            if kind == "drag":
                options["hold_seconds"] = gesture.hold_seconds
            send = getattr(session.run.driver, kind + "_point", None)
            if not callable(send):
                raise ToolError(f"This device can't {kind} by point.")
            try:
                send(*positions, snapshot, **options)
            except Exception as error:
                if type(error).__name__ == "DriverRejection" and getattr(error, "code", None) in error.PRE_DISPATCH:
                    raise
                raise ToolError(f"The {kind} may have reached the device. It was not repeated. "
                                "Call screen to inspect the result before another gesture.") from None
            phases("dispatch")
            verb = {"drag": "Dragged", "hold": "Held", "flick": "Flicked"}[kind]
            text = f"{verb} {quote(gesture.start_label)}"
            if gesture.end is not None:
                text += f" to {quote(gesture.end_label)}"
            return {"op": operation, "text": text, "line": text + ".", "changed": None}
        return self._act(args, call, work, operation)

    def tool_type_text(self, args, call):
        text, append, submit = args["text"], bool(args.get("append")), bool(args.get("submit"))

        def work(session, phases):
            driver = session.run.driver
            tree, snapshot = self._current(session, phases)
            node = self._resolve(session, tree, args)
            role = (node.role or "").removeprefix("XCUIElementType")
            element = element_at(snapshot, node.path)
            if role == "SecureTextField":
                from ..state import validate_input_text
                validate_input_text(text)
            # Typing a one-time code asks in the Mac app, with or without Return (permissions.rules.code_entry);
            # Return that commits asks too.
            if self._permit(session, "TYPE_SUBMIT" if submit else "TYPE", node, element, snapshot, call, text=text):
                element, snapshot = self._rebind(session, element, snapshot, call)
            receipt = None
            if role == "SecureTextField":
                # The fast read leaves secure fields out, so there is no Element: tap, then type through WDA.
                x, y = hit_point(node, tree)
                driver.tap_point(x, y, snapshot, timeout=10)
                self._await_focus(driver)
                self._check_action_cancelled(session, call)
                try:
                    driver.call("POST", "/wda/keys", {"value": [text]}, timeout=30)
                    if submit:
                        self._check_action_cancelled(session, call)
                        driver.call("POST", "/wda/keys", {"value": ["\n"]}, timeout=5)
                except Exception as error:
                    # A driver's error can echo the password it tried to send.
                    raise ToolError(f"Mobster couldn't finish typing into the secure field ({type(error).__name__}). "
                                    "Call screen before another action.") from None
                typed, shown = SECURE_MASK, SECURE_MASK
            elif element is not None and element.editable:
                receipt = driver.write_text(element, snapshot, text, append=append, submit=submit,
                                            timeout=min(36.0, 16 + len(text) / 20))
                typed, shown = text, quote(text)
            else:
                raise ToolError(f"{args.get('ref') or 'The target'} is a {role_name(node)}, not a text field. "
                                "Pass a ref or target for a field.")
            phases("type")
            after = None
            if submit:
                try:
                    after = driver.observe_ready(timeout=5)
                except Exception:
                    pass
                phases("settle")
            ok = receipt is None or receipt.get("value") is None or bool(receipt.get("matches"))
            into = quote(node_name(node))
            line = f"Typed {shown} into {into}" + (" and pressed Return" if submit else "")
            if not ok:
                line += f"; the field reads {quote(receipt.get('value'), 80)}, not the text typed"
            logged = SECURE_MASK if typed == SECURE_MASK else quote(text, LOGGED_TEXT)
            return {"op": "TYPE_SUBMIT" if submit else "TYPE", "text": f"Typed {shown} into {into}",
                    "target": node_target(node), "typed": typed, "changed": True, "ok": ok, "line": line + ".",
                    "after": screen_fingerprint(after),
                    "log": f"Typed {logged} into {into}",
                    "extra": {"field_reads": None if not receipt or receipt.get("value") is None
                              else str(receipt["value"])[:200]}}
        return self._act(args, call, work, "TYPE_SUBMIT" if submit else "TYPE")

    @staticmethod
    def _await_focus(driver, seconds=2.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                if getattr(driver.observe(timeout=5), "keyboard", "") in ("visible", "parked"):
                    return
            except Exception:
                break
            time.sleep(.2)
        raise ToolError("Mobster couldn't confirm the secure field's keyboard, so it didn't type. Call screen "
                        "before another action.")

    def tool_swipe(self, args, call):
        direction = args["direction"]
        until = self._parse_assertion(args["until"]) if args.get("until") is not None else None
        limit = int(args.get("max_swipes") or (8 if until is not None else 1))

        def holds(tree):
            return bool(self.api.evaluate_on([until], tree)[0].ok)

        def work(session, phases):
            driver = session.run.driver
            tree, _snapshot = self._current(session, phases)
            node = self._resolve(session, tree, args, required=False)
            # A row's swipe archives or deletes in some apps (Gmail, Outlook): the same rules as a tap, for every
            # swipe. After the first, the next row may have slid under the same spot, and nobody approved that one.
            row_swipe = node is not None and direction in ("left", "right")
            if row_swipe:
                if self._permit(session, "SWIPE_" + direction.upper(), node, element_at(_snapshot, node.path),
                                _snapshot, call):
                    self._rebind(session, None, _snapshot, call)
            held = holds(tree) if until is not None else None
            swipes, moved, stuck, after, refused = 0, False, False, None, None
            aim = node
            deadline = time.monotonic() + SWIPE_SECONDS
            while not held and swipes < limit and time.monotonic() < deadline:
                snapshot = driver.observe(timeout=10)
                self._check_action_cancelled(session, call)
                if swipes and row_swipe and self._real_phone(session) is not None:
                    aim = {n.path: n for n in self.codes.read_tree(session).shown()}.get(aim.path)
                    if aim is None:
                        break  # the element is gone: nothing approved is left to swipe
                    try:
                        if self._permit(session, "SWIPE_" + direction.upper(), aim, element_at(snapshot, aim.path),
                                        snapshot, call):
                            self._rebind(session, None, snapshot, call)
                    except ToolError as error:
                        refused = str(error)
                        break
                if aim is None:
                    driver.execute("SWIPE_" + direction.upper(), None, snapshot, timeout=10)
                else:
                    self._swipe_in(driver, aim, tree, direction)
                swipes += 1
                after = self._settle(driver, snapshot)
                if screen_changed(snapshot, after) is False:
                    stuck = True
                    break
                moved = True
                if until is not None:
                    tree = self.codes.read_tree(session)
                    held = holds(tree)
            where = f" on {quote(node_name(node))}" if node is not None else ""
            count = "once" if swipes == 1 else f"{swipes} times"
            text = f"Swiped {direction}{where} {count}" if swipes else f"No swipe{where}"
            if until is not None:
                line = (f"{describe_assertion(args['until'])} already held; no swipe needed." if held and not swipes
                        else f"{text}; the expectation holds now." if held
                        else f"{text}; the expectation still doesn't hold"
                        + (" (the content stopped moving)." if stuck else "."))
            else:
                line = text + ("; the screen didn't move." if stuck and not moved else ".")
            if refused:
                line += f" It stopped before the next swipe: {refused}"
            return {"op": "SWIPE_" + direction.upper(), "text": text,
                    "target": node_target(node) if node is not None else None, "changed": moved,
                    "ok": held if until is not None else moved, "line": line, "record": swipes > 0,
                    "after": screen_fingerprint(after),
                    "extra": {"swipes": swipes, "held": held}}
        return self._act(args, call, work, "SWIPE_" + direction.upper())

    @staticmethod
    def _swipe_in(driver, node, tree, direction):
        width, height = tree.size
        x, y, w, h = node.rect
        cx, cy = x + w / 2, y + h / 2
        dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}[direction]
        x1, y1 = cx - dx * w * .3, cy - dy * h * .3
        x2, y2 = cx + dx * w * .3, cy + dy * h * .3
        clamp = lambda value, top: round(min(top - 1, max(1, value)))  # noqa: E731
        driver.call("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
            "actions": [{"type": "pointerMove", "duration": 0, "x": clamp(x1, width), "y": clamp(y1, height)},
                        {"type": "pointerDown"},
                        {"type": "pointerMove", "duration": 250, "x": clamp(x2, width), "y": clamp(y2, height)},
                        {"type": "pointerUp"}]}]}, timeout=10)

    @staticmethod
    def _alert_button(requested, buttons):
        # iOS 26.4 names the notification prompt's button "Don’t Allow", with U+2019.
        wanted = normalize(requested)
        exact = [b for b in buttons if normalize(b) == wanted]
        if exact:
            return exact[0]
        loose = [b for b in buttons if normalize(b).casefold() == wanted.casefold()]
        return loose[0] if len(loose) == 1 else None

    def tool_alert(self, args, call):
        action, requested = args["action"], args.get("button")

        def work(session, phases):
            driver = session.run.driver
            alert = self._alert(driver)
            if alert is None:
                raise ToolError("No alert is showing. Call screen to see the current screen.")
            button = requested
            if requested and alert["buttons"]:
                button = self._alert_button(requested, alert["buttons"])
                if button is None:
                    raise ToolError(f"The alert has no button {quote(requested)}. Its buttons: "
                                    + ", ".join(quote(b) for b in alert["buttons"]) + ".")
            self._refuse_grant(session, alert, action, button)
            self._permit_alert(session, alert, action, button, call)
            snapshot = driver.observe(timeout=10)
            self._check_action_cancelled(session, call)
            # WebDriverAgent finds the button by its real name, curly apostrophe and all.
            driver.call("POST", f"/alert/{action}", {"name": button} if button else {}, timeout=10)
            after = self._settle(driver, snapshot)
            changed = screen_changed(snapshot, after)
            verb = "Accepted" if action == "accept" else "Dismissed"
            text = f"{verb} the alert {quote(alert['text'], 80)}" + (f" with {quote(button)}" if button else "")
            return {"op": "ALERT", "text": text, "changed": changed, "line": text + ".",
                    "after": screen_fingerprint(after),
                    "target": {"id": "", "label": button or alert["text"][:120], "role": "Alert"}}
        return self._act(args, call, work, "ALERT")

    def tool_open_url(self, args, call):
        from ..deeplinks import LinkError, decide
        from .direct import url_app
        url = args.get("url")
        if not url:
            return self._deep_links(args)
        try:
            # The one rule for links (deeplinks.decide). An outside agent never uses "Don't ask again": a link that
            # asks asks every time, on the card in the Mac app.
            decision = decide(url, None)
        except LinkError as error:
            raise ToolError(str(error)) from None

        def work(session, phases):
            run, driver = session.run, session.run.driver
            record = self._real_phone(session) if self._on_phone(session) else None
            if record is not None:
                target = url_app(url)
                if target:
                    self._permit_app(session, target)
                if decision.action == "ask":
                    from ..permissions import outside
                    try:
                        outside.check_link(url, decision.title, decision.app, decision.app_name,
                                           device=record.get("id"), client_name=self.client_name())
                    except outside.Refused as refused:
                        raise ToolError(str(refused).replace("Nothing was done.", "Nothing was opened.")) from None
                    self._check_action_cancelled(session, call)
                    if target:
                        self._permit_app(session, target)
            elif decision.action == "ask" and self._on_phone(session):
                raise ToolError(f"Mobster asks before opening {quote(url, 80)}, and nobody here can answer, so it "
                                "didn't open it on the iPhone. Ask the user to open it.")
            snapshot = driver.observe(timeout=10)
            self._check_action_cancelled(session, call)
            opened = run.open_url(url)
            after = self._settle(driver, snapshot, timeout=4, wait_seconds=1.5)
            changed = screen_changed(snapshot, after)
            text = f"Opened {quote(url, 120)}"
            app_link = opened.get("url") if isinstance(opened, dict) else None
            if isinstance(app_link, str) and app_link != url:
                text += (f" as {quote(app_link, 120)}, the app link for that screen (the web address opens the "
                         "home feed)")
            return {"op": "OPEN_URL", "text": text, "changed": changed, "line": text + changed_words(changed),
                    "after": screen_fingerprint(after)}
        return self._act(args, call, work, "OPEN_URL")

    def tool_expo_preview(self, args, call):
        from . import expo_tools
        return expo_tools.preview(self, args, call)

    def tool_expo_reload(self, args, call):
        from . import expo_tools
        return expo_tools.reload(self, args, call)

    def _relaunch(self, args, call):
        if not args.get("run_id") and args.get("device"):
            raise ToolError("restart applies to a check's app. On a device you drive directly, pass bundle_id.")

        def work(session, phases):
            run, driver = session.run, session.run.driver
            run.relaunch()
            try:
                after = driver.observe_ready(timeout=10)
            except Exception:
                after = None
            name = getattr(getattr(run, "app", None), "name", "") or "the app"
            text = f"Relaunched {name}, keeping its data"
            return {"op": "RELAUNCH", "text": text, "changed": True, "line": text + ".",
                    "after": screen_fingerprint(after)}
        return self._act(args, call, work, "RELAUNCH")

    def tool_wait_for(self, args, call):
        assertions = [self._parse_assertion(item) for item in args["expect"]]
        asked = float(args.get("timeout_s", 10))
        session = self._target_session(args)
        started = time.monotonic()
        with self._locked(session):
            self._front(session)
            # evaluate reads until its deadline, then its last read and the outline take their own time (3 to 4 s
            # per read under load): the wait keeps EVALUATE_RESERVE of the call's budget for them.
            timeout = min(asked, self._left(call, self.call_budget - self.evaluate_reserve))
            try:
                results = session.run.evaluate(assertions, timeout=timeout)
                outline, alert = self._outline(session)
            except ToolError:
                raise
            except Exception as error:
                raise ToolError(self._action_error(error)) from None
        seconds = time.monotonic() - started
        held = all(result.ok for result in results)
        head = (f"All {len(results)} hold ({seconds:.1f} s)." if held else
                f"{sum(r.ok for r in results)} of {len(results)} hold after {seconds:.1f} s.")
        shortened = timeout < asked
        if shortened and not held:  # when all hold, the cut changed nothing
            head += (f" The wait was cut to {timeout:.1f} s of the {asked:g} s asked, so this call answers within "
                     f"{self.call_budget:.0f} s.")
        lines = [head] + [f"{'✓' if r.ok else '✗'} {r.text}: {str(r.observed)[:200]}" for r in results]
        lines += list(dict.fromkeys(hint for hint in (navbar_hint(item) for item, r in zip(args["expect"], results)
                                                      if not r.ok) if hint))
        lines.append("This never changes the verdict: verify_finish judges the expectations from verify_start.")
        structured = {"held": held, "seconds": round(seconds, 2), "timeout_s": round(timeout, 2),
                      "shortened": shortened,
                      "results": [{"ok": bool(r.ok), "text": r.text, "observed": r.observed} for r in results],
                      "screen": outline.structured(alert)}
        return ToolResult(cap_text("\n".join(lines) + "\n" + outline.render(alert)), jsonable(structured))
