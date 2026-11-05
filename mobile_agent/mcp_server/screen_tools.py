"""Reading a session's screen: the outline, the picture, the alert, the run's lock."""

import base64
import os
import tempfile
from pathlib import Path

from .protocol import ToolResult
from .session import Outline
from .support import INLINE_QUALITY, INLINE_WIDTH, Busy, Phases, ToolError, cap_text, screen_fingerprint, sentence


class ScreenTools:
    def _first_frame(self, session):
        frames = Path(session.run.run_dir) / "frames"
        first = sorted(frames.glob("01-*.jpg")) if frames.is_dir() else []
        if first and not session.secret_rects:
            return self.imager(path=str(first[0]))
        return self._screenshot(session)

    def _screenshot(self, session):
        run = session.run
        handle, path = tempfile.mkstemp(suffix=".jpg", prefix="mobster-mcp-")
        os.close(handle)
        try:
            try:
                manager = getattr(run, "manager", None) or self._manager()
                shot = manager.screenshot(run.target, path, max_width=INLINE_WIDTH, quality=INLINE_QUALITY)
                if not self.codes.redact_file(session, str(shot or path)):
                    return None  # never show a picture whose masking could not be confirmed
                return self.imager(path=str(shot or path))
            except Exception:
                preview = run.driver.capture_preview()
                return self.imager(data=self.codes.redact_bytes(session, base64.b64decode(preview.split(",", 1)[-1])))
        except Exception:
            return None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _outline(self, session, phases=None, before=None):
        phases = phases or Phases()
        driver = session.run.driver
        self._front(session)  # nothing of an app the person hasn't allowed is read
        if before is None:
            before = self._quick_fingerprint(driver)
            phases("anchor")
        tree = self.codes.read_tree(session)
        phases("outline_read")
        outline = Outline(tree, session.book)
        session.outline, session.anchor = outline, None
        after = self._quick_fingerprint(driver)
        if before is not None and after == before:
            session.anchor = after
        phases("anchor")
        alert = self._alert(driver)
        phases("alert")
        return outline, alert

    @staticmethod
    def _quick_fingerprint(driver):
        try:
            return screen_fingerprint(driver.observe(timeout=10))
        except Exception:
            return None

    def _current(self, session, phases):
        driver = session.run.driver
        snapshot = driver.observe(timeout=10)
        phases("observe")
        outline = session.outline
        if outline is not None and session.anchor is not None and screen_fingerprint(snapshot) == session.anchor:
            return outline.tree, snapshot
        before = screen_fingerprint(snapshot)
        tree = self.codes.read_tree(session)
        phases("read")
        snapshot = driver.observe(timeout=10)  # fresh for the action after the slower evidence read
        phases("observe")
        if screen_fingerprint(snapshot) != before:
            session.forget_outline()
            raise ToolError("The screen changed while reading its controls. Call screen before another action.")
        return tree, snapshot

    @staticmethod
    def _settle(driver, snapshot, timeout=3.0, wait_seconds=.6):
        # One read once the pixels rest: a read mid-animation waits for the app to go idle (2.2 s against 0.78 s
        # after tapping General in Settings, 28 Sep).
        options = {"single_read": True} if getattr(driver, "settles_on_one_read", False) else {}
        try:
            return driver.wait_for_change(snapshot, timeout=timeout, wait_seconds=wait_seconds, **options)
        except Exception:
            try:
                return driver.observe_ready(timeout=8)
            except Exception:
                return None

    @staticmethod
    def _alert(driver):
        try:
            text = driver.call("GET", "/alert/text", timeout=3)
        except Exception:
            return None
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            buttons = driver.call("GET", "/wda/alert/buttons", timeout=3)
        except Exception:
            buttons = []
        # Real names: the alert tool sends WebDriverAgent back the name it reported.
        buttons = [str(b)[:200] for b in buttons[:8]] if isinstance(buttons, list) else []
        alert = {"text": text.strip()[:300], "buttons": buttons}
        from ..wda.system_dialogs import classify
        kind = classify(text, buttons).kind
        if kind != "unknown":
            alert["kind"] = kind  # iOS's own prompt: location, notifications, paste, tracking... (system_dialogs)
        return alert

    def _locked(self, session, timeout=None, *, check_phone=True):
        timeout = self.lock_wait if timeout is None else timeout

        class Held:
            def __enter__(self):
                if not session.run.lock.acquire(timeout=max(0.0, timeout)):
                    raise Busy(f"Another call on run {session.run_id} is still going. Try again when it returns.")
                try:
                    if not session.open:
                        raise ToolError(f"Run {session.run_id} has ended. Start a new run, or pass device again.")
                    check = getattr(session.run, "check_phone", None)
                    if check_phone and callable(check):
                        from .direct import PhoneStop
                        try:
                            check("resume")
                        except PhoneStop as stop:
                            session.forget_outline()
                            raise ToolError(sentence(stop)) from None
                except BaseException:
                    session.run.lock.release()
                    raise
                session.busy += 1

            def __exit__(self, *exc):
                session.busy -= 1
                session.touch()
                session.run.lock.release()
        return Held()

    def tool_screen(self, args, call):
        session = self._target_session(args)
        with self._locked(session):
            outline, alert = self._outline(session)
            picture = self._screenshot(session) if args.get("image", True) else None
            self._remember_point_screen(session, picture)
        return ToolResult(cap_text(outline.render(alert)),
                          {"run_id": session.run_id, "screen": outline.structured(alert)},
                          [picture] if picture else [])

    def _remember_point_screen(self, session, picture):
        session.point_screen = None
        if session.mode != "direct" or not picture or session.anchor is None:
            return
        try:
            snapshot = session.run.driver.observe(timeout=5)
        except Exception:
            return
        if screen_fingerprint(snapshot) == session.anchor and snapshot.bundle_id == session.outline.tree.bundle_id:
            session.point_screen = {"picture": picture, "fingerprint": session.anchor, "bundle": snapshot.bundle_id}
