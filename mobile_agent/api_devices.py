"""Device routes of the local API: a phone's settings, setup, control, screen and stream."""

import re
from urllib.parse import parse_qs, unquote, urlsplit

from . import devices as devices_module
from .api_http import APP_ONLY, APPROVE_IN_APP, APPROVE_IN_APP_ORIGINS, DEVICE_FIELDS, PRIMARY_VIDEO, TAKE_BACK_IN_APP
from .app_icons import IconUnavailable
from .devices import AmbiguousDevice, DeviceNotFound
from .runtime_create import TaskStartMixin
from .wda import frame_source
from .wda.video import BOUNDARY as WDA_BOUNDARY
from .wda.video import VideoUnavailable as WdaVideoUnavailable


def runtime_teach(runtime, body, phone):
    action = body.get("action")
    if action == "save":
        return runtime.teach_save(body, phone)
    if set(body) != {"action"} or action not in ("start", "stop", "discard", "questions"):
        raise ValueError("action must be start, stop, discard, questions or save")
    return {"start": runtime.teach_start, "stop": runtime.teach_stop, "discard": runtime.teach_discard,
            "questions": runtime.teach_questions}[action](phone)


class DeviceRoutesMixin:
    def device_phone(self, ident):
        fleet = getattr(self.runtime, "fleet", None)
        if fleet is None:
            self.send_json({"error": "This Mobster has no device list", "code": "device_not_found"}, 404)
            return None
        try:
            return fleet.lookup(unquote(ident))
        except DeviceNotFound as error:
            self.send_json({"error": str(error), "code": "device_not_found"}, 404)
        except AmbiguousDevice as error:
            self.send_json({"error": str(error), "code": "device_ambiguous"}, 400)
        return None

    def send_devices(self):
        fleet = getattr(self.runtime, "fleet", None)
        devices = fleet.public() if fleet is not None and self.runtime.pool is None else []
        fanout = getattr(self.runtime, "fanout", None)
        return self.send_json({"devices": devices,
                               "defaultDevice": next((item["id"] for item in devices if item["default"]), None),
                               "chosenDevices": fleet.chosen(devices) if devices else [],
                               "chosenSaved": list(fleet.stored()["chosen"]) if devices else [],
                               **({"limits": fanout.limits()} if fanout is not None and devices else {})})

    def send_device(self, ident, part):
        phone = self.device_phone(ident)
        if phone is None:
            return
        fleet = self.runtime.fleet
        if part is None:
            return self.send_json({"device": fleet.entry(phone.id or ident)[1]})
        video = self.runtime.video if phone.primary else phone.video
        if part in ("stream", "frame") and video is None:
            # A phone plugged in but not set up has no relay yet: never another phone's picture in its place.
            return self.send_json({"error": "This device has no live view yet", "code": "video_unavailable"}, 503)
        if part == "stream":
            return self.send_stream(video, phone)
        if part == "frame":
            return self.send_frame(video)
        if part == "video/status":
            if video is None:
                return self.send_json({"error": "This device has no live view", "code": "video_unavailable"}, 404)
            return self.send_json({**video.status(), "awakeLease": self.runtime.awake.status()})
        if part == "info":
            return self.send_json(phone.info())
        if part == "status":
            return self.send_json(self.runtime.device_status(phone))
        try:
            return self.send_json(fleet.setup_state(phone))
        except LookupError as error:
            return self.send_json({"error": str(error), "code": "setup_unmanaged"}, 404)

    def owner_action(self, ident, part, body):
        # An agent holding the phone (MCP, Remote, the local API) can't take it back from its owner.
        if self.needs_app(TAKE_BACK_IN_APP):
            return
        if body:
            raise ValueError("Send an empty JSON object")
        phone = self.device_phone(ident)
        if phone is None:
            return
        if part == "take-back":
            self.runtime.take_back(phone if self.runtime.pool is None else None)
        else:
            self.runtime.resume_queue(phone if self.runtime.pool is None else None)
        return self.send_json({"device": self.runtime.fleet.entry(phone.id or ident)[1]})

    def device_action(self, ident, part, action, body):
        # Gestures and teaching reach the phone with no approval card: the Mac app's only, never another process.
        if (part in ("control", "teach") or action == "photos") and self.needs_app():
            return
        phone = self.device_phone(ident)
        if phone is None:
            return
        if part == "control":
            return self.send_json({"ok": True, "action": self.runtime.control_device(body, phone)})
        if part == "teach":
            return self.send_json(runtime_teach(self.runtime, body, phone))
        fleet = self.runtime.fleet
        try:
            target = fleet.setup_action(phone, action, body)
            return self.send_json(fleet.setup_state(target))
        except KeyError:
            return self.send_json({"error": "Not found"}, 404)
        except LookupError as error:
            return self.send_json({"error": str(error), "code": "setup_refused"}, 409)

    def start_group(self, body, key, extras, origin):
        if body.get("device") is not None:
            raise ValueError("Name the phones in phones, or one phone in device, not both")
        if body.get("profiles") is not None and (body.get("phones") is not None or extras.get("account")):
            raise ValueError("Name the profiles in profiles, or the phones in phones, and no single profile")
        team_group = body.get("teamGroup")
        if team_group is not None and (origin != "console" or body.get("phones") is None):
            # The team's console labels the same task on each of its Macs' phones; it grants nothing.
            raise ValueError("teamGroup is set by the team's console, for phones only")
        from .threads.service import fingerprint as body_fingerprint
        by_profile = body.get("profiles") is not None
        group, replayed = (self.runtime.start_profiles if by_profile else self.runtime.start_group)(
            body["profiles"] if by_profile else body["phones"], body.get("appId"), body.get("goal"),
            idempotency_key=key, fingerprint=body_fingerprint({**body, "origin": origin}),
            output_schema=body.get("outputSchema"), output_format=body.get("outputFormat", "auto"),
            allowed_bundles=body.get("allowedBundles"),
            engine=body.get("engine"), **({"extras": extras} if extras else {}),
            **({"origin": origin} if origin != "api" else {}), **({"team_group": team_group} if team_group else {}))
        public = self.runtime.fanout.public(group)
        runs = [self.runtime.runs[member["runId"]].public(include_events=False) for member in group["members"]
                if member.get("runId") in self.runtime.runs]
        refused = [{name: member.get(name) for name in ("device", "deviceName", "profile", "profileName", "error",
                                                        "code") if name in member}
                   for member in group["members"] if member["state"] == "refused"]
        status = 200 if replayed else 201 if runs else 202
        return self.send_json({"group": public, "groupId": group["id"], "runs": runs, "refused": refused,
                               "replayed": replayed}, status)

    def group_request(self, method, path, body):
        fanout = getattr(self.runtime, "fanout", None)
        if path == "/api/groups" and method == "GET":
            if fanout is None:
                return self.send_json({"groups": [], "limits": None})
            return self.send_json({"groups": fanout.recent(), "limits": fanout.limits()})
        match = re.fullmatch(r"/api/groups/([a-f0-9]{12})(?:/(stop|approvals))?", path)
        if not match or fanout is None:
            return False
        if method == "GET" and not match[2]:
            return self.send_json({"group": fanout.public(fanout.get(match[1]))})
        if method != "POST" or not match[2]:
            return self.send_json({"error": "Not found"}, 404)
        if match[2] == "stop":
            if body:
                raise ValueError("Send an empty JSON object")
            return self.send_json({"group": fanout.public(fanout.stop(match[1]))})
        if set(body) != {"approve", "approvals"}:
            raise ValueError("Answer a group's approvals with approve (true or false) and approvals: each "
                             "phone's run id and the approval id you were shown")
        group = fanout.get(match[1])
        if body["approve"] is True:
            if group.get("origin") in APPROVE_IN_APP_ORIGINS and not self.has_app_session():
                return self.send_json({"error": APP_ONLY, "code": "app_only"}, 403)
            if self.needs_app(APPROVE_IN_APP):
                return
        answers = fanout.answer_approvals(match[1], body["approve"], body["approvals"])
        return self.send_json({"answers": answers, "group": fanout.public(group)})

    def choose_devices(self, body):
        fleet = getattr(self.runtime, "fleet", None)
        if fleet is None or self.runtime.pool is not None:
            return self.send_json({"error": "This Mobster has no device list", "code": "device_not_found"}, 404)
        if set(body) != {"devices"}:
            raise ValueError("Send {\"devices\": [the ids of the phones you chose]}")
        fleet.choose(body["devices"])
        return self.send_devices()

    def manage_device(self, ident, part, body):
        phone = self.device_phone(ident)
        if phone is None:
            return
        fleet = self.runtime.fleet
        if part is None:
            if not body or set(body) - DEVICE_FIELDS:
                raise ValueError("Change a phone's name, default, askBeforeActing or apps")
            if {"askBeforeActing", "apps"} & set(body) and self.needs_app():
                return
            if "default" in body and type(body["default"]) is not bool:
                raise ValueError("default is true or false")
            changes = {}
            if "name" in body:
                changes["nickname"] = body["name"] if body["name"] is None else devices_module.clean_nickname(
                    body["name"])
            for key in ("askBeforeActing", "apps"):
                if key in body:
                    changes[key] = body[key]
            try:
                if changes:
                    fleet.set_prefs(phone, **changes)
                if body.get("default") is True:
                    fleet.set_default(phone)
                elif body.get("default") is False and fleet.stored()["defaultDevice"] == phone.id:
                    fleet.set_default(None)
            except LookupError as error:
                return self.send_json({"error": str(error), "code": "device_refused"}, 409)
            return self.send_json({"device": fleet.entry(phone.id)[1]})
        if body:
            raise ValueError("Send an empty JSON object")
        note = None
        try:
            if part == "disconnect":
                fleet.disconnect(phone)
            else:
                note = fleet.connect(phone)
        except LookupError as error:
            return self.send_json({"error": str(error), "code": "device_refused"}, 409)
        return self.send_json({"device": fleet.entry(phone.id)[1], **({"note": note} if note else {})})

    def setup_action(self, path, body):
        from .devkit import DevError

        setup = self.runtime.setup
        if setup is None:
            return self.send_json({"error": "Setup is managed outside this app", "code": "setup_unmanaged"}, 404)
        try:
            if path == "/api/setup/build" and set(body) <= {"team"}:
                setup.manager.start_build(body.get("team"))
            elif path == "/api/setup/start" and not body:
                setup.manager.start_runner()
            elif path == "/api/setup/stop" and not body:
                setup.manager.stop_runner(remember=True)
            elif path == "/api/setup/phone" and set(body) == {"udid"}:
                self.runtime.manager.choose(body["udid"])
            elif path == "/api/setup/live" and set(body) == {"enabled"}:
                setup.set_live(body["enabled"])
            elif path == "/api/setup/photos" and not body:
                if self.needs_app():   # iOS's question appears on the person's iPhone: their click in the Mac app
                    return
                setup.allow_photos()
            elif path == "/api/setup/refresh" and not body:
                setup.refresh()
            elif path == "/api/setup/tools" and not body:
                setup.install_tools()
            elif path == "/api/setup/xcode" and not body:
                setup.open_xcode()
            elif path == "/api/setup/signing/card" and not body:
                setup.signing_card()
            elif path == "/api/setup/signing/approve" and set(body) == {"digest"}:
                setup.signing_approve(body["digest"])
            elif path == "/api/setup/signing/resolve" and set(body) == {"digest", "made"}:
                setup.signing_resolve(body["digest"], body["made"])
            elif path == "/api/setup/signing/devmode" and not body:
                setup.signing_reveal_developer_mode()
            else:
                return self.send_json({"error": "Not found"}, 404)
        except LookupError as error:
            return self.send_json({"error": str(error)}, 409)
        except ValueError as error:
            return self.send_json({"error": str(error)}, 400)
        except DevError as error:
            return self.send_json({"error": error.line(), "code": "signing_refused"}, 409)
        return self.send_json(setup.state())

    def send_run_frame(self, run_id, frame_id):
        frames = getattr(self.runtime, "frames", None)
        data = frames.get(run_id, frame_id) if frames is not None and run_id in self.runtime.runs else None
        if data is None and frames is not None and run_id in self.runtime.runs:
            data = TaskStartMixin.frame(self.runtime, run_id, frame_id)
        if data is None:
            return self.send_json({"error": "Not found", "code": "frame_not_found"}, 404)
        self.send_bytes(data, "image/jpeg")

    def send_icon(self):
        query = parse_qs(urlsplit(self.path).query)
        bundle = (query.get("bundle") or [""])[0]
        try:
            icon = self.runtime.icons.get(bundle)
        except ValueError:
            return self.send_json({"error": "Invalid app bundle identifier", "code": "invalid_bundle"}, 400)
        except IconUnavailable:
            return self.send_json({"error": "The app icon is not available right now", "code": "icon_unavailable"}, 503)
        except OSError:
            return self.send_json({"error": "The app icon could not be saved", "code": "icon_unavailable"}, 503)
        if icon is None:
            return self.send_json({"error": "This app has no icon", "code": "no_icon"}, 404)
        self.send_bytes(*icon)

    def send_stream(self, video=PRIMARY_VIDEO, phone=None):
        # ?framing=raw: WebKit parses multipart/x-mixed-replace itself and hands fetch() bodies without part headers.
        query = parse_qs(urlsplit(self.path).query)
        raw = query.get("framing") == ["raw"]
        asked = (query.get("fps") or [None])[0]
        if asked is not None and not (asked.isdigit() and 1 <= int(asked) <= 60):
            return self.send_json({"error": "fps is a whole number from 1 to 60", "code": "invalid_fps"}, 400)
        rate = (lambda: int(asked)) if asked is not None else (lambda: self.runtime.stream_fps(phone))
        video = self.runtime.video if video is PRIMARY_VIDEO else video
        if not frame_source.streams(video):
            return self.send_json({"error": "Live stream needs a USB iPhone", "code": "video_unavailable"}, 503)
        try:
            viewer = video.subscribe()
        except WdaVideoUnavailable as exc:
            return self.send_json({"error": str(exc), "code": "video_unavailable"}, 503)
        self.close_connection = True
        try:
            self.connection.settimeout(10)  # a stuck viewer releases its slot
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream" if raw else
                             f"multipart/x-mixed-replace; boundary={WDA_BOUNDARY}")
            self.send_header("Cache-Control", "no-store, no-transform")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            for frame in video.paced_frames(viewer, rate):
                content_type, data = frame[1], frame[2]
                # A slow link brought the stream down to this rate (WdaVideo's LinkGovernor): the pane says so.
                reduced = video.reduced_fps()
                note = f"X-Mobster-Reduced: {reduced}\r\n" if reduced else ""
                self.wfile.write(f"--{WDA_BOUNDARY}\r\nContent-Type: {content_type}\r\n{note}"
                                 f"Content-Length: {len(data)}\r\n\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
        except (OSError, TimeoutError):
            pass
        finally:
            video.unsubscribe(viewer)

    def send_frame(self, video=PRIMARY_VIDEO):
        video = self.runtime.video if video is PRIMARY_VIDEO else video
        # A peek starts a stopped relay, so a phone nobody watches still gets a current picture.
        frame = video.peek() if frame_source.streams(video) else None
        if frame is None:
            return self.send_json({"error": "No frame yet", "code": "video_unavailable"}, 503)
        self.send_bytes(frame[2], frame[1])
