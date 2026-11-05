"""The phone tools that are not taps: codes, notifications, the clipboard, Photos, buttons, display, installs, the lock
and the sound."""

import contextlib
import os
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

from .action_journal import record_action
from .codes import SECURE_MASK
from .protocol import ToolResult
from .session import node_name, node_target
from .support import (
    SHUTTING_DOWN,
    Phases,
    ToolError,
    cap_text,
    changed_words,
    device_request,
    jsonable,
    screen_changed,
    screen_fingerprint,
    sentence,
)

# Dormant: only a build with lockscreen.CAN_UNLOCK on reaches these (this one doesn't, and the Mac app has no such
# setting). What unlock says when the phone's owner hasn't let scripts and MCP unlock it.
UNLOCK_OFF = ("Mobster may not unlock “{name}” from scripts or MCP. Its owner can allow it in the Mobster app: "
              "Settings › iPhones › {name} › Unlock with passcode › Scripts and MCP may unlock.")
CANT_UNLOCK = "Mobster can't unlock an iPhone and never enters a passcode. Ask the user to unlock “{name}” themselves."


class PhoneTools:
    def tool_use_code(self, args, call):
        def work(session, phases):
            from ..skills.codes import CodeRefused, Field, enter_code, find_code
            driver = session.run.driver
            tree, snapshot = self._current(session, phases)
            node = self._resolve(session, tree, args)
            width, height = tree.size
            x, y, w, h = node.rect
            field = Field(label=node.label or "", role=(node.role or "").removeprefix("XCUIElementType"),
                          rect=(x / width, y / height, w / width, h / height), identifier=node.identifier or "",
                          placeholder=getattr(node, "placeholder", "") or "", locator=node.path)
            bundle = getattr(tree, "bundle_id", "") or getattr(snapshot, "bundle_id", "") or ""
            snapshot = self._permit_code(session, bundle, node_name(node) or "the code field", snapshot, call)
            found = None
            try:
                found = find_code(driver, field=field, bundle=bundle, snapshot=snapshot, hint=args.get("sender"))
                phases("find")
                rect = enter_code(driver, found)
            except CodeRefused as refusal:  # always before a key is sent
                session.forget_outline()
                raise ToolError(str(refusal)) from None
            except Exception as error:
                # Only the error's kind, in the result and the log: a driver's message could echo what it sent.
                # A failure after the code was found may leave it in the field (a timeout after the keys landed),
                # so it is masked and blacked out from now on as if it had been typed.
                session.forget_outline()
                name = type(error).__name__
                self.log(f"Run {session.run_id}: use_code failed ({name}).")
                if found is None:
                    raise ToolError(f"Mobster couldn't look for the code ({name}). Call screen to see where the "
                                    "device is.") from None
                self.codes.remember(session, found.code, found.frame)
                raise ToolError(f"Mobster couldn't finish entering the code ({name}). Call screen to see the field "
                                "before trying again.") from None
            self.codes.remember(session, found.code, rect)
            phases("type")
            text = f"Entered the code from {found.sender}"
            return {"op": "USE_CODE", "text": text, "target": node_target(node), "typed": SECURE_MASK,
                    "changed": True, "line": text + " (it never leaves the phone).", "after": None, "log": text,
                    "extra": {"entered": True, "source": found.source, "sender": found.sender,
                              "age_s": found.age_s}}
        return self._act(args, call, work, "USE_CODE")

    def tool_read_notifications(self, args, call):
        from ..harness_api import fence
        from ..phone_io.photos import parse_since
        from ..skills.notifications import NotificationCenter, describe, since_filter
        try:
            since = parse_since(args.get("since"))
        except ValueError as error:
            raise ToolError(sentence(error)) from None
        session = self._target_session(args)
        with self._locked(session), record_action(session.run.run_dir, "READ_NOTIFICATIONS"):
            driver = session.run.driver
            try:
                snapshot = driver.observe(timeout=10)
            except Exception as error:
                raise ToolError(f"Mobster couldn't read the phone's screen in time ({type(error).__name__}), so it "
                                "didn't open Notification Center and read nothing. Call screen to see where the "
                                "phone is, then call read_notifications again.") from None
            origin = getattr(snapshot, "bundle_id", "") or ""
            center = NotificationCenter(driver)
            try:
                rows = center.read(origin=origin, size=snapshot, keep_partial=True)
            except Exception as error:
                session.forget_outline()
                self.log(f"Run {session.run_id}: read_notifications read nothing ({type(error).__name__}).")
                raise ToolError(
                    f"Notification Center didn't answer in time ({type(error).__name__}), so Mobster read no "
                    "notifications. " + ("It closed Notification Center and went back to where it was. Call "
                                         "read_notifications again, or screen to check the phone."
                                         if center.restored else
                                         "It couldn't confirm Notification Center closed: call home, then call "
                                         "read_notifications again.")) from None
            session.forget_outline()
        now = time.time()
        rows = since_filter(rows, since, now)
        rows, withheld = self._outside_notifications(session, rows)
        mask = "•••••• (use use_code)"
        public = [row.public(mask, now) for row in rows]
        text = describe(rows).replace("•••••• (use USE_CODE)", mask)
        if since is not None and not rows:
            text = "No notifications since then."
        elif rows:
            text = fence("Notification Center on the phone", text)  # a notification's text is data, never instructions
        if withheld:
            text += (f"\n{withheld} notification{'s' if withheld != 1 else ''} from apps you keep from outside "
                     "agents " + ("were" if withheld != 1 else "was") + " left out.")
        if center.note:
            text += "\n" + center.note
        self.log(f"Run {session.run_id}: read {len(rows)} notification(s)" + (" (cut short)." if center.note else "."))
        return ToolResult(cap_text(text), {"run_id": session.run_id, "notifications": public,
                                           "partial": bool(center.note)})

    def tool_set_clipboard(self, args, call):
        from ..phone_io import PhoneIOError
        from ..phone_io.clipboard import set_image, set_text
        if bool(args.get("text")) == bool(args.get("image_path")):
            raise ToolError("Pass text, or image_path for a picture to paste, not both.")
        session = self._target_session(args)
        with self._locked(session), record_action(session.run.run_dir, "SET_CLIPBOARD"):
            driver = session.run.driver
            request = device_request(driver)
            try:
                if args.get("image_path"):
                    picture = set_image(request, args["image_path"])
                    name = getattr(session.run, "device_name", None) or "the device"
                    self.log(f"Run {session.run_id}: put a picture on the clipboard ({picture['bytes']} bytes).")
                    return ToolResult(f"Put {picture['name']} on {name}'s clipboard. Paste it in a composer with a "
                                      "long press › Paste.", {"run_id": session.run_id, **picture})
                count = set_text(request, args["text"], driver=driver)
            except PhoneIOError as error:
                raise ToolError(sentence(error)) from None
        name = getattr(session.run, "device_name", None) or "the device"
        self.log(f"Run {session.run_id}: set the clipboard ({count} characters).")
        return ToolResult(f"Put {count:,} character{'s' if count != 1 else ''} on {name}'s clipboard. Paste it with "
                          "a long press in a field, or type_text.", {"run_id": session.run_id, "characters": count})

    def _deep_links(self, args):
        from ..deeplinks import search
        links = search(args.get("app"), args.get("query"))
        from .. import intents
        from ..intents import atlas
        found = atlas.discovered(args) if intents.enabled() and not links else []
        if found:
            return ToolResult(cap_text(atlas.describe(found)), {"links": [], "discovered": found})
        if not links:
            return ToolResult("No known deep link fits. Navigate there with tap, or open the app's own https link with "
                              "open_url and check the screen: some web addresses open an app's home feed, not the "
                              "screen they name.", {"links": []})
        lines = [f"{link.app}: {link.does}: {link.template}" + ("" if link.risk == "opens" else f" ({link.risk})")
                 + (" (best effort)" if link.best_effort else "") for link in links]
        return ToolResult(cap_text("Fill in each {placeholder} (URL-encoded) and call open_url:\n" + "\n".join(lines)),
                          {"links": [link.public() for link in links]})

    def tool_send_media(self, args, call):
        from ..phone_io import PhoneIOError
        from ..phone_io.photos import MAX_SEND_BYTES, sniff
        path = os.path.expanduser(args["path"])
        if not os.path.isabs(path):
            raise ToolError("Pass the absolute path of a photo or video on this Mac.")
        session = self._target_session(args)
        record = self._phone_record(session) or {}
        name = getattr(session.run, "device_name", None) or record.get("name") or "the device"
        if record.get("kind") == "simulator":
            try:
                sniff(path)
                if os.path.getsize(path) > MAX_SEND_BYTES:
                    raise PhoneIOError(f"{os.path.basename(path)} is over 100 MB; Mobster moves files up to 100 MB.",
                                       "usage")
            except (PhoneIOError, OSError) as error:
                raise ToolError(sentence(error)) from None
            udid = record.get("udid") or record.get("id")
            argv = ["xcrun", "simctl", "addmedia", udid, path]
            with self._locked(session), record_action(session.run.run_dir, "SEND_MEDIA"):
                done = (self._simctl or (lambda a: subprocess.run(a, capture_output=True, text=True, timeout=60)))(argv)
                if done.returncode != 0:
                    raise ToolError(f"The simulator didn't take the file: {(done.stderr or done.stdout).strip()[:160]}")
            return ToolResult(f"Added {Path(path).name} to Photos on {name}.", {"device": record.get("id"),
                                                                                  "name": Path(path).name})
        from ..phone_io.transfer import Phone, Transfer, summary
        from ..phone_io.transfer import run as run_transfer
        paths = [path] + [os.path.expanduser(p) for p in args.get("paths") or ()]
        if not all(os.path.isabs(p) for p in paths):
            raise ToolError("Pass absolute paths of photos or videos on this Mac.")
        with self._locked(session), record_action(session.run.run_dir, "SEND_MEDIA"):
            driver = session.run.driver

            @contextlib.contextmanager
            def runner():
                yield lambda method, p, body=None, timeout=15: driver.call(method, p, body, timeout=timeout)
            phone = Phone({**record, "name": name}, runner=runner)
            if self._phone_files:
                phone.files = lambda rec, container=False: self._phone_files(rec)
            transfer = Transfer("to_phone", "photos", phone, via=args.get("via") or "auto", paths=paths,
                                album=args.get("album", "Mobster") or None)
            try:
                run_transfer(transfer)
            except PhoneIOError as error:
                raise ToolError(sentence(error) + (" " + sentence(error.fix) if error.fix else "")) from None
            session.forget_outline()
            if transfer.state != "done":
                error = transfer.error or {}
                raise ToolError(summary(transfer) + (" " + sentence(error["fix"]) if error.get("fix") else ""))
        self.log(f"Run {session.run_id}: added {len(transfer.items)} to Photos by {transfer.route}.")
        return ToolResult(summary(transfer) + " Apps can attach them from the photo picker now.",
                          jsonable({"run_id": session.run_id, **transfer.public()}))

    def tool_pull_media(self, args, call):
        from ..phone_io.photos import CameraRoll, parse_since
        record = self._record_for(args)
        try:
            since = parse_since(args.get("since"))
        except ValueError as error:
            raise ToolError(sentence(error)) from None
        folder = Path(os.getcwd()) / "iphone-media"
        from ..phone_io.transfer import Transfer, phone_from_record
        from ..phone_io.transfer import run as run_transfer
        phone = phone_from_record(record)
        if self._phone_files:
            phone.roll = lambda rec: CameraRoll(self._phone_files(rec))
        transfer = Transfer("to_mac", "photos", phone, via=args.get("via") or "auto", folder=str(folder),
                            count=int(args.get("count") or 1), since=since,
                            convert_heic=args.get("jpeg", True) is not False)
        run_transfer(transfer)
        if transfer.state != "done" and not transfer.items:
            error = transfer.error or {}
            raise ToolError(sentence(error.get("message") or "Nothing moved.")
                            + (" " + sentence(error["fix"]) if error.get("fix") else ""))
        items = [{**i.public(), "path": i.path} for i in transfer.items if i.path]
        if not items:
            return ToolResult(f"Nothing in {record['name']}'s camera roll" + (" since then." if since else "."),
                              {"device": record["id"], "files": []})
        pictures = [self.imager(path=item["path"]) for item in items[:3]
                    if item["kind"] == "photo" and item["path"].lower().endswith((".jpg", ".jpeg", ".png"))]
        lines = [f"{item['path']} ({item['kind']}, {item['bytes']:,} bytes)" for item in items]
        return ToolResult(f"Saved {len(items)} from {record['name']}'s camera roll, newest first:\n" + "\n".join(lines),
                          jsonable({"device": record["id"], "files": items}), [p for p in pictures if p])

    def tool_press_button(self, args, call):
        from ..phone_io import PhoneIOError
        from ..phone_io.controls import LOCKED_AFTER, lock, press
        button = args["button"]

        def work(session, phases):
            driver = session.run.driver
            request = device_request(driver)
            simulator = not self._on_phone(session)
            snapshot = driver.observe(timeout=10)
            try:
                if button == "lock":
                    lock(request)
                else:
                    press(request, button, simulator=simulator)
            except PhoneIOError as error:
                raise ToolError(sentence(error)) from None
            if button == "lock":
                text = "Locked the screen" + ("" if simulator else ". " + LOCKED_AFTER)
                return {"op": "LOCK", "text": "Locked the screen", "changed": True, "line": text, "after": None,
                        "record": False}
            after = self._settle(driver, snapshot)
            changed = screen_changed(snapshot, after)
            text = {"home": "Pressed Home", "volume_up": "Pressed Volume Up",
                    "volume_down": "Pressed Volume Down"}[button]
            return {"op": "BUTTON", "text": text, "changed": changed, "line": text + changed_words(changed),
                    "after": screen_fingerprint(after)}
        if button == "lock":
            session = self._target_session(args)
            with self._locked(session), record_action(session.run.run_dir, "LOCK"):
                done = work(session, Phases())
            return ToolResult(done["line"], {"run_id": session.run_id, "locked": True})
        return self._act(args, call, work, "BUTTON")

    def tool_set_display(self, args, call):
        from ..phone_io import PhoneIOError
        from ..phone_io.controls import set_appearance, set_orientation
        if not args.get("appearance") and not args.get("orientation"):
            raise ToolError("Pass appearance (light or dark), orientation, or both.")

        def work(session, phases):
            driver = session.run.driver
            request = device_request(driver)
            snapshot = driver.observe(timeout=10)
            said, extra = [], {}
            try:
                if args.get("appearance"):
                    done = set_appearance(request, args["appearance"])
                    said.append(f"appearance {done['appearance']}" + (f" (was {done['before']})" if done["before"]
                                                                      else ""))
                    extra["appearance_before"] = done["before"]
                if args.get("orientation"):
                    done = set_orientation(request, args["orientation"])
                    said.append(f"orientation {done['orientation'].replace('_', ' ')}"
                                + (f" (was {done['before'].replace('_', ' ')})" if done["before"] else ""))
                    extra["orientation_before"] = done["before"]
            except PhoneIOError as error:
                raise ToolError(sentence(error)) from None
            after = self._settle(driver, snapshot, timeout=4, wait_seconds=1.0)
            changed = screen_changed(snapshot, after)
            text = "Set " + " and ".join(said)
            return {"op": "DISPLAY", "text": text, "changed": changed, "line": text + changed_words(changed),
                    "after": screen_fingerprint(after), "extra": extra}
        return self._act(args, call, work, "DISPLAY")

    def tool_install_app(self, args, call):
        from ..journal import Lease, LeaseHeld
        from ..phone_io import PhoneIOError
        from ..verify.runs import make_run_dir
        path = args["path"]
        if not os.path.isabs(os.path.expanduser(path)):
            raise ToolError("Pass the absolute path of a local .app or .ipa.")
        record = self._record_for(args)
        if self._installer is not None:
            installer = self._installer
        else:
            from ..phone_io.install import install as installer
        with ExitStack() as held:
            with self._lock:
                if self._closing.is_set():
                    raise ToolError(SHUTTING_DOWN)
                session = self._open_direct(record["id"])
                if session is None:
                    if self._device_busy_with_check(record) is not None:
                        raise ToolError("A check is using this device. Stop it before installing an app.")
                    if not record.get("wdaUrl"):
                        raise ToolError("The device has no WebDriverAgent address to lock. "
                                        "Set it up before installing.")
                    try:
                        lease = Lease.device(record["wdaUrl"])
                    except LeaseHeld:
                        raise ToolError("Another Mobster process is using this device. Stop its run before installing "
                                        "an app.") from None
                    held.callback(lease.close)
                    folder = self.runs_dir if record.get("kind") == "simulator" else self.device_runs_dir
                    run_id, run_dir = make_run_dir(folder)
            if session is not None:
                held.enter_context(self._locked(session))
                run_id, run_dir = session.run_id, session.run.run_dir
            try:
                with record_action(run_dir, "INSTALL_APP"):
                    result = installer(record, path, progress=self.log)
            except PhoneIOError as error:
                raise ToolError(sentence(error) + (" " + sentence(error.fix) if error.fix else "")) from None
        return ToolResult(f"Installed {result.get('bundle_id') or Path(path).name} on {record['name']} in "
                          f"{result.get('seconds', 0):g} s. Open it with launch_app.",
                          jsonable({**result, "run_id": run_id, "run_dir": str(run_dir)}))

    def tool_unlock_status(self, args, call):
        from .direct import lockscreen_available, passcode_saved, unlock_settings
        record = self._record_for(args)
        phone = record.get("kind") != "simulator"
        if not (phone and lockscreen_available()):
            # This build never unlocks: the answer is whether the phone is locked and that the user has to unlock it.
            locked = self._locked_now(record)
            status = {"device": record["id"], "supported": False, "locked": locked}
            if not phone:
                return ToolResult(f"“{record['name']}” is a simulator: it has no passcode.", status)
            from .. import owner_presence
            if locked is True and owner_presence.is_owner_active(record.get("wdaUrl")):
                # The owner is on the phone: nothing to ask of them, and nothing for the agent to wait on.
                state, ask = "is in use by its owner", " Leave it alone and try again later."
                status["owner"] = "active"
            elif locked is True:
                state, ask = "is locked", " Ask the user to unlock it."
            elif locked is False:
                state, ask = "is unlocked", ""
            else:
                state, ask = "may be locked: its runner didn't answer", " If it is, ask the user to unlock it."
            return ToolResult(f"“{record['name']}” {state}. Mobster can't unlock an iPhone and never enters a "
                              f"passcode.{ask}", status)
        settings = unlock_settings(record)
        status = {"device": record["id"], "supported": True,
                  "enabled": bool(settings.get("enabled")), "passcode_saved": passcode_saved(record),
                  "scripts_may_unlock": bool(settings.get("scripts")),
                  "ask_before_unlocking": bool(settings.get("askBeforeUnlocking")),
                  "relock_after": bool(settings.get("relockAfter")), "needs_check": bool(settings.get("needsCheck")),
                  "locked": self._locked_now(record)}
        words = [f"“{record['name']}”:", "unlocking with a passcode is " + ("on" if status["enabled"] else "off") + ";",
                 "a passcode is " + ("saved" if status["passcode_saved"] else "not saved") + ";",
                 "scripts and MCP " + ("may" if status["scripts_may_unlock"] else "may not") + " unlock it"]
        if status["needs_check"]:
            words.append("; the saved passcode failed once, so it must be entered again in the Mobster app")
        if status["locked"] is not None:
            words.append("; it is " + ("locked" if status["locked"] else "unlocked") + " now")
        return ToolResult(" ".join(words).replace(" ;", ";") + ".", status)

    def tool_device_status(self, args, call):
        from .. import device_status
        record = self._record_for(args)
        phone = device_status.read_phone(record.get("kind"), udid=record.get("udid"), wda_url=record.get("wdaUrl"))
        mac = device_status.read_mac()
        holder, queue = self._holder_of(record)
        status = {"device": record["id"], "phone": phone, "mac": mac, "holder": holder, "queue": queue}
        text = " ".join(device_status.describe(record.get("name") or record["id"], phone, mac))
        if holder:
            text += f" {self._holder_words(holder, queue)}."
        return ToolResult(text, status)

    def tool_listen(self, args, call):
        from .. import listen
        from ..harness_api import fence
        record = self._record_for(args)
        if record.get("kind") != "usb":
            raise ToolError(f"Mobster can hear only an iPhone on this Mac's USB; “{record['name']}” isn't one. A "
                            "simulator plays through this Mac's own speakers.")
        seconds = args.get("seconds") or listen.DEFAULT_SECONDS
        try:
            result = listen.listen(seconds, udid=record.get("udid"), locale=args.get("locale"))
        except listen.ListenError as error:
            if error.code in listen.AFTER_CAPTURE:
                self._after_capture(record)
            raise ToolError(sentence(error)) from None
        self._after_capture(record)
        name = record["name"]
        if result["text"]:
            text = fence(f"What “{name}” played in {result['seconds']} s", result["text"])
        elif result["silent"]:
            text = f"Nothing was heard on “{name}” in {result['seconds']} s. {listen.silence_note(result)}"
        else:
            text = f"“{name}” played sound for {result['seconds']} s, but no words were recognized in it."
        from ..watch import clock_text
        sounds = [sound for sound in result.get("sounds") or () if sound["label"] != "speech" or not result["text"]]
        if sounds:
            text += "\n" + fence(f"The sounds “{name}” played, told apart on this Mac",
                                 "\n".join(listen.sound_lines(sounds, clock=clock_text)))
        return ToolResult(cap_text(text), {"device": record["id"], **result})

    def _after_capture(self, record):
        # listen's capture resets the phone's USB network link and ends its runner (drivers.WDA_LINK_BLIP_SECONDS).
        from .. import drivers, native_helpers
        url = (record.get("wdaUrl") or "").rstrip("/")
        if not url or not native_helpers.devices_allowed():
            return
        session = self._open_direct(record["id"])
        driver = getattr(getattr(session, "run", None), "driver", None) if session is not None else None
        try:
            if callable(getattr(driver, "after_capture", None)):
                driver.after_capture()
                return
            probe = drivers.HTTP(url)

            def status_ok(timeout):
                try:
                    value = probe.request("GET", "/status", timeout=timeout).get("value")
                    return isinstance(value, dict) and value.get("ready", True) is not False
                except Exception:  # noqa: BLE001
                    probe.close()
                    return False
            started = time.monotonic()
            drivers.wait_out_capture(status_ok)
            while time.monotonic() - started < drivers.WDA_RECOVER_SECONDS and not status_ok(3):
                time.sleep(.5)
            probe.close()
        except Exception:  # noqa: BLE001
            pass

    def _locked_now(self, record):
        url = (record.get("wdaUrl") or "").rstrip("/")
        if not url:
            return None
        try:
            value = self._wda_get(url + "/wda/locked", 2.0)
        except Exception:
            return None
        return value if isinstance(value, bool) else None

    def tool_unlock(self, args, call):
        from .direct import PhoneStop, lockscreen_available, unlock_settings
        record = self._record_for(args)
        name = record["name"]
        if record.get("kind") == "simulator":
            raise ToolError(f"“{name}” is a simulator: it has no passcode to enter.")
        if not lockscreen_available():
            raise ToolError(CANT_UNLOCK.format(name=name))
        settings = unlock_settings(record)
        if not (settings.get("enabled") and settings.get("scripts")):
            raise ToolError(UNLOCK_OFF.format(name=name))
        if settings.get("needsCheck"):
            raise ToolError(f"The saved passcode didn't unlock “{name}” last time, so Mobster won't try it again until "
                            "its owner re-enters it in the Mobster app: Settings › iPhones.")
        session = self._open_direct(record["id"])
        if session is not None and session.open:
            with self._locked(session, check_phone=False):
                try:
                    verdict = session.run.check_phone("resume")
                except PhoneStop as stop:
                    raise ToolError(sentence(stop)) from None
        else:
            session = self._device_session(record["id"])
            verdict = getattr(session.run, "preflight", None)
        state = getattr(verdict, "state", None) or "ready"
        unlocked = state == "unlocked"
        session.forget_outline()
        return ToolResult(f"Mobster unlocked “{name}”." if unlocked else f"“{name}” is unlocked.",
                          {"run_id": session.run_id, "device": record["id"], "unlocked": unlocked, "state": state})
