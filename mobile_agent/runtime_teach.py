"""Runtime's live view: the person's taps on it, teaching a lesson on it, and its pace."""

import re
import sys
import time
from pathlib import Path

from . import engines
from .api_errors import APIError
from .journal import JournalError, Lease

# 13 Oct 2026, WKWebView at 589 x 1278: 5 fps used 4.7-8.7% of a core, 15 used 12-17%, 30 used 22-28%
# (research/live-view-2026-10-13.md).
IDLE_STREAM_FPS = 15
CONTROL_STREAM_SECONDS = 10.0

class LiveViewMixin:
    def control_device(self, body, phone=None):
        # Never while a task holds that phone; tasks on other phones don't stop it.
        fleet = getattr(self, "fleet", None)
        primary = phone is None or phone.primary
        control = self.control if primary else phone.control
        if control is None:
            if not primary and phone.kind == "usb":
                raise APIError("Set this iPhone up in Setup first, then you can control it here", 409,
                               "control_unavailable")
            raise APIError("Direct control needs a USB iPhone", 404, "control_unavailable")
        if not self.config.enable_live:
            raise APIError("Turn on “Allow actions on your iPhone” in Settings to control it", 409, "live_disabled")
        with self.lock:
            if getattr(self, "closing", False):
                raise APIError("The service is shutting down", 503, "shutting_down")
            busy = (bool(self.active_runs) if fleet is None else
                    self.run_on(fleet.primary if primary else phone) is not None)
            if busy:
                raise APIError("A task is using your iPhone. Stop it to take over.", 409, "device_busy")
            try:
                lease = Lease.device((self.config.wda_url if primary else phone.wda_url).rstrip("/"))
            except JournalError:
                raise APIError("Your iPhone is busy. Wait for it to finish, then try again.", 409,
                               "device_busy") from None
        try:
            getattr(self, "controlled_at", {})[None if primary else phone.key] = time.monotonic()
            lesson = self.lesson_for(phone)
            if lesson is not None and lesson.state == "recording":
                # Teaching: the screen is read before the gesture and the gesture recorded after it is sent.
                return control.perform(body, teach=lesson)
            return control.perform(body)
        except ValueError:
            raise
        except Exception as exc:
            # No gesture content or typed text.
            print(f"mobster: direct control failed: {type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr,
                  flush=True)
            raise APIError("Your iPhone did not accept that. Check it is unlocked and connected.", 503,
                           "control_failed") from None
        finally:
            lease.close()

    def lesson_key(self, phone):
        return None if phone is None or phone.primary else phone.key

    def lesson_for(self, phone):
        if not getattr(self, "lessons", None):
            return None
        with self.lessons_lock:
            lesson = self.lessons.get(self.lesson_key(phone))
            if lesson is not None and lesson.idle():
                self.lessons.pop(self.lesson_key(phone), None)
                return None
            return lesson

    def lesson_device(self, phone):
        if phone is not None:
            return phone.id
        fleet = getattr(self, "fleet", None)
        return getattr(fleet.primary, "id", None) if fleet is not None and self.pool is None else None

    def teach_state(self, phone=None):
        lesson = self.lesson_for(phone)
        return {"lesson": lesson.public() if lesson is not None else None}

    def teach_start(self, phone=None):
        primary = phone is None or phone.primary
        control = self.control if primary else phone.control
        if control is None:
            raise APIError("Teaching needs a USB iPhone you can control from its live view", 404,
                           "control_unavailable")
        if not self.config.enable_live:
            raise APIError("Turn on “Allow actions on your iPhone” in Settings to teach it", 409, "live_disabled")
        fleet = getattr(self, "fleet", None)
        with self.lock:
            busy = (bool(self.active_runs) if fleet is None else
                    self.run_on(fleet.primary if primary else phone) is not None)
        if busy:
            raise APIError("A task is using your iPhone. Stop it, then teach.", 409, "device_busy")
        from .learned.teach import Lesson
        key = self.lesson_key(phone)
        with self.lessons_lock:
            current = self.lessons.get(key)
            if current is not None and not current.idle():
                return {"lesson": current.public()}
            lesson = self.lessons[key] = Lesson(self.lesson_device(phone), names=self.lesson_names(phone))
        return {"lesson": lesson.public()}

    def lesson_names(self, phone):
        try:
            apps = self.apps() if phone is None or phone.primary else phone.apps()
            return {app.get("bundleId"): app.get("name") for app in apps or () if app.get("bundleId")}
        except Exception:  # noqa: BLE001
            return {}

    def teach_stop(self, phone=None):
        lesson = self._lesson(phone)
        if lesson.state == "recording":
            primary = phone is None or phone.primary
            control = self.control if primary else phone.control
            try:
                snapshot = control.observe(timeout=8) if control is not None else None
            except Exception:  # noqa: BLE001 -- the end screen is a nicety: the program ends at its last step
                snapshot = None
            lesson.stop(snapshot)
            from .learned.teach import program_steps
            from .memory import writer
            writer.lesson_staged(lesson, program_steps(lesson)[0])  # held until the lesson is saved
        return {"lesson": lesson.public()}

    def teach_questions(self, phone=None):
        # At most 2, in one text-only call; [] without a model or on a failed call, so the sheet still saves.
        from .learned.teach import ambiguous, ask_questions
        lesson = self._lesson(phone)
        if lesson.state == "recording":
            raise APIError("Stop the lesson first, then Mobster asks about it.", 409, "lesson_recording")
        if lesson.questions is None:
            client = None
            if ambiguous(lesson):
                try:
                    client = self.lesson_client()
                except Exception:  # noqa: BLE001 -- no key, a bad key: no questions
                    client = None
            try:
                ask_questions(lesson, client)
            finally:
                if client is not None:
                    engines.release_client(client)
        return {"lesson": lesson.public()}

    def lesson_client(self):
        key = engines.smart_key()
        if not key:
            return None
        client = engines.build_client(key=key)
        if hasattr(client, "reasoning"):
            client.reasoning = "none"
        return client

    def teach_discard(self, phone=None):
        with self.lessons_lock:
            dropped = self.lessons.pop(self.lesson_key(phone), None)
        if dropped is not None:
            from .memory import writer
            writer.lesson_discarded(dropped.id)
        return {"lesson": None, "discarded": dropped is not None}

    def teach_save(self, body, phone=None):
        from .learned import programs as learned_programs
        from .learned.taught import save_from_demo
        from .learned.teach import LessonError, answered, program_steps
        if not isinstance(body, dict) or set(body) - {"action", "request", "name", "blanks", "workflow", "purposes",
                                                      "rules", "answers"}:
            raise ValueError("A lesson is saved with request, and optionally name, blanks, purposes, rules, answers "
                             "and workflow")
        answers = body.get("answers") or []
        if not isinstance(answers, list) or len(answers) > 2 or not all(
                isinstance(item, dict) and set(item) == {"question", "choice"} for item in answers):
            raise ValueError("answers is a list of {question, choice}: the clarifying questions' answers")
        edits = {}
        for part, key in (("purposes", "purpose"), ("rules", "rule")):
            given = body.get(part) or []
            if not isinstance(given, list) or len(given) > 60 or not all(
                    isinstance(item, dict) and set(item) == {"step", key} and type(item["step"]) is int
                    and isinstance(item[key], str) for item in given):
                raise ValueError(f"{part} is a list of {{step, {key}}}")
            edits[part] = given
        blanks = body.get("blanks") or []
        workflow = body.get("workflow", False)
        if type(workflow) is not bool:
            raise ValueError("workflow must be true or false")
        if not isinstance(blanks, list) or not all(isinstance(item, dict) and type(item.get("step")) is int
                                                   and set(item) <= {"step", "name"} for item in blanks):
            raise ValueError("blanks is a list of {step, name}: the typed values that change each time")
        lesson = self._lesson(phone)
        if lesson.state == "recording":
            self.teach_stop(phone)
        steps, typed, final = program_steps(lesson)
        # The sheet numbers the lesson's steps; a lesson that started in an app has one more first step (its open).
        offset = 1 if steps and lesson.start_app and (not lesson.steps or lesson.steps[0]["op"] != "LAUNCH_APP") else 0
        indexes = [item["step"] - 1 + offset for item in blanks]
        purposes = {item["step"] - 1 + offset: item["purpose"] for item in edits["purposes"]}
        rules, decide = {}, set()
        for item in edits["rules"]:
            rules.setdefault(item["step"] - 1 + offset, []).append(item["rule"])
        try:
            given = answered(lesson, answers)
        except LessonError as error:
            raise ValueError(str(error)) from error
        for number, picked in given.items():
            for rule, each_run in picked:
                rules.setdefault(number - 1 + offset, []).append(rule)
                if each_run:
                    decide.add(number - 1 + offset)
        data_dir = Path(self.config.state_db).parent if getattr(self.config, "state_db", None) else None
        if data_dir is None:
            raise APIError("Mobster has no data folder to keep programs in", 409, "programs_unavailable")
        store = learned_programs.Programs(learned_programs.programs_dir(data_dir))
        report = {}
        path = save_from_demo(store, steps, body.get("request"), indexes, device=lesson.device,
                               name=body.get("name"), final=final, typed=typed,
                               purposes=purposes, rules=rules, decide=decide, report=report)
        saved_workflow = None
        linked = []
        if workflow and report:
            # Taught again: the workflow its words already have stays the one.
            try:
                linked = [item for item in self.workflows.list() if learned_programs.code_fills(
                    (path.get("identity") or {}).get("template") or "", learned_programs.workflow_words(item))]
            except Exception:  # noqa: BLE001
                linked = []
        if workflow and not linked:
            saved_workflow = self.workflows.create(self.lesson_workflow(path, blanks, indexes, lesson))
            path = dict(path, workflowId=saved_workflow["id"])
            store.cache.save(path)
        with self.lessons_lock:
            if self.lessons.get(self.lesson_key(phone)) is lesson:
                self.lessons.pop(self.lesson_key(phone), None)
        from .memory import writer
        writer.lesson_saved(lesson, steps, body.get("request"))  # the path and screens it taught become memory
        merged = None
        if report:
            # In the sheet's numbers: a lesson that started in an app has one more first step (its open).
            number = lambda index: index - offset + 1  # noqa: E731
            merged = {"into": report["into"], "added": [number(i) for i in report["added"] if number(i) >= 1],
                      "changed": [number(i) for i in report["changed"] if number(i) >= 1]}
        return {"program": learned_programs.public(path, [saved_workflow] if saved_workflow else linked),
                "workflow": saved_workflow or (linked[0] if linked else None), "merged": merged}

    @staticmethod
    def lesson_workflow(path, blanks, indexes, lesson):
        from .workflows import starters
        template = (path.get("identity") or {}).get("template") or ""
        names = {}
        for item, index in zip(blanks, indexes):
            param = (path["steps"][index] or {}).get("param")
            if param:
                names[param] = " ".join(str(item.get("name") or "").split())[:starters.NAME_CHARS] or param
        example = path.get("example") or {}
        parts = []
        for piece in re.split(r"(\{p\d+\})", template):
            found = re.fullmatch(r"\{(p\d+)\}", piece)
            if found:
                name = names.get(found.group(1)) or found.group(1)
                value = str(example.get(found.group(1)) or "").replace("{", "").replace("}",
                                                                                        "")[:starters.DEFAULT_CHARS]
                parts.append("{" + name + (f": {value}" if value else "") + "}")
            else:
                parts.append(starters.escape(piece))
        goal = "".join(parts)
        record = {"name": path.get("name") or template[:80], "goal": goal, "appId": None}
        if lesson.device:
            record["device"] = lesson.device
        return record

    def _lesson(self, phone):
        lesson = self.lesson_for(phone)
        if lesson is None:
            raise APIError("No lesson is going on this iPhone. Start one with Teach.", 409, "no_lesson")
        return lesson

    def teaching(self, phone):
        # A lesson's gestures and a task's would mix.
        if self.lesson_for(phone) is not None:
            raise APIError("You're teaching Mobster on this iPhone. Save or discard the lesson first.", 409,
                           "device_busy")

    def stream_fps(self, phone=None):
        primary = phone is None or phone.primary
        controlled = getattr(self, "controlled_at", {}).get(None if primary else phone.key)
        if controlled is not None and time.monotonic() - controlled < CONTROL_STREAM_SECONDS:
            return None
        fleet = getattr(self, "fleet", None)
        if fleet is None or getattr(self, "pool", None) is not None:
            busy = bool(self.active_runs)  # no device list, or a pool's phones: any task counts
        else:
            busy = self.run_on(fleet.primary if primary else phone) is not None
        return None if busy else IDLE_STREAM_FPS
