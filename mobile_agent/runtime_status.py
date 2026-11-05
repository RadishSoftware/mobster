"""Runtime's settings, keys and the status every surface shows."""

import os

from . import __version__, engines, listen, permissions, tracks
from .api_errors import APIError
from .devices import AmbiguousDevice, DeviceNotFound
from .env_secrets import update_env_file
from .gemini import configured as helper_configured
from .keys import Keys
from .power_watch import PAUSE_RANGE as BATTERY_PAUSE_RANGE
from .power_watch import phone_pause_threshold
from .runtime_limits import MONTH_LIMIT_RANGE, TASK_LIMIT_RANGE
from .wda import frame_source
from .wda.video import mjpeg_settings


class StatusMixin:
    def settings(self):
        value = {"askBeforeActing": self.ask_before_acting(), "taskCostLimitUsd": self.task_cost_limit(),
                 "monthlyLimitUsd": self.monthly_limit(), "permissionMode": permissions.from_env().mode,
                 "phoneBatteryPause": phone_pause_threshold(),
                 "hearPhone": listen.enabled()}
        if frame_source.streams(getattr(self, "video", None)):
            value["videoFps"], value["videoDetail"] = self.video_quality()
        return value

    @property
    def env_file(self):
        return self.setup.env_file if getattr(self, "setup", None) else getattr(self.config, "env_file", None)

    @property
    def keys(self):
        return Keys(self.env_file)

    def save_keys(self, body):
        # Ask Smart's provider afresh: a project the user just fixed must not wait out ModelReach's cached refusal.
        state = self.keys.update(body)
        reach = getattr(self, "model_reach", None)
        if reach is not None and {"openai", "anthropic", "helper"} & set(body):
            reach.forget(engines.smart_key())
        return state

    def check_key(self, target):
        model = engines.smart_model()
        # The key the test is about to read, when it is the one Smart runs on.
        key = engines.smart_key() if target == engines.model_provider(model) else None
        result = self.keys.test(target)
        reach = getattr(self, "model_reach", None)
        # The test takes up to a minute: a key saved meanwhile was never tested, and the next status checks it.
        if key and reach is not None and engines.smart_key() == key:
            if result.get("ok"):
                reach.record(key, True)
            elif result.get("problem") == "no_credit":
                reach.record(key, False, engines.no_credit(model))
        return result

    def update_settings(self, changes):
        allowed = {"askBeforeActing", "videoFps", "videoDetail", "taskCostLimitUsd", "monthlyLimitUsd",
                   "phoneBatteryPause", "hearPhone"}
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise ValueError("Unsupported setting")
        saved = {}
        if "askBeforeActing" in changes:
            if type(changes["askBeforeActing"]) is not bool:
                raise ValueError("askBeforeActing must be true or false")
            saved["MOBSTER_ASK_BEFORE_ACTING"] = "1" if changes["askBeforeActing"] else "0"
            # The older switch sets the mode: off is Don't ask; on keeps a mode that asks, else the default.
            mode = permissions.from_env().mode
            saved["MOBSTER_PERMISSION_MODE"] = permissions.policy.mode_for_ask(changes["askBeforeActing"], mode)
        if "hearPhone" in changes:
            if type(changes["hearPhone"]) is not bool:
                raise ValueError("hearPhone must be true or false")
            saved[listen.SETTING] = "on" if changes["hearPhone"] else "off"
        if "phoneBatteryPause" in changes:
            low, high = BATTERY_PAUSE_RANGE
            percent = changes["phoneBatteryPause"]
            if percent is not None and (type(percent) is not int or not low <= percent <= high):
                raise ValueError(f"phoneBatteryPause must be null or a whole percent from {low} to {high}")
            saved["MOBSTER_PHONE_BATTERY_PAUSE"] = "off" if percent is None else str(percent)
        for name, env, (low, high) in (("taskCostLimitUsd", "MOBSTER_TASK_COST_LIMIT", TASK_LIMIT_RANGE),
                                       ("monthlyLimitUsd", "MOBSTER_MONTHLY_LIMIT", MONTH_LIMIT_RANGE)):
            if name not in changes:
                continue
            amount = changes[name]
            if amount is None:
                saved[env] = "off" if name == "taskCostLimitUsd" else ""
                continue
            if type(amount) not in (int, float) or not low <= amount <= high:
                raise ValueError(f"{name} must be null or between ${low:g} and ${high:,g}")
            saved[env] = f"{round(float(amount), 2):.2f}"
        if {"videoFps", "videoDetail"} & set(changes):
            if not frame_source.streams(getattr(self, "video", None)):
                raise ValueError("Live video settings need a USB iPhone")
            fps, detail = self.video_quality()
            fps, detail = changes.get("videoFps", fps), changes.get("videoDetail", detail)
            if type(fps) is not int:
                raise ValueError("videoFps must be 15, 30 or 60")
            mjpeg_settings(fps, detail)  # validates both
            saved.update(MOBSTER_VIDEO_FPS=str(fps), MOBSTER_VIDEO_DETAIL=detail)
        if self.env_file:
            update_env_file(self.env_file, saved)
        os.environ.update(saved)
        if "MOBSTER_VIDEO_FPS" in saved:
            self.video.set_quality(*self.video_quality())
            for video in (self.fleet.videos() if getattr(self, "fleet", None) is not None else ()):
                video.set_quality(*self.video_quality())
        return self.settings()

    def device_status(self, phone=None):
        if phone is None:
            fleet = getattr(self, "fleet", None)
            phone = fleet.primary if fleet is not None else None
        return {"phone": self.power.phone(phone) if phone is not None else {},
                "mac": {**self.power.mac(), **self.power.awake.status()},
                "warnings": self.power.warnings(),
                "phoneBatteryPause": phone_pause_threshold()}

    def engines_status(self):
        managed = getattr(self, "setup", None) is not None
        key, model = engines.smart_key(), engines.smart_model()
        reach = getattr(self, "model_reach", None)
        if self.scripted_models():
            smart = (True, None)
        elif not key:
            smart = (False, engines.no_key_reason(managed, model))
        else:
            smart = reach.state(key, model) if reach is not None else (True, None)
        return {engines.SMART: {"available": smart[0], "reason": smart[1], "model": model,
                                "provider": engines.provider_name(model),
                                # Whose bill: a plan (Kimi Code, Grok ...) or a key (ai_plans/providers.py).
                                "billing": "plan" if engines.on_plan(model) else "key"}}

    def engine(self):
        return self.engine_sampler.snapshot()

    def status(self):
        target = self.target_status()
        device, ready, health = target["device"], target["ready"], target["health"]
        wda = bool(self.config.wda_url)
        available = self.engines_status()
        key = available[engines.SMART]["available"]
        return {"api_version": "v1", "version": __version__, "helper_configured": helper_configured(),
                "engines": available, "agents": self.agents_status(), "engine": self.engine(),
                "live_enabled": bool(not self.closing and self.config.enable_live and key and ready
                                     and target["can_act"]),
                "device_ready": ready, "durable_history": getattr(self.config, "state_db", None) is not None,
                "screen_reading": target["screen_reading"], "ask_before_acting": self.ask_before_acting(),
                "spend_cap_usd": getattr(self.config, "spend_cap_usd", None),
                "devices": self.pool.public() if self.pool else [],
                "concurrency": {"capacity": self.pool.size if self.pool else self.device_count(),
                                "running": len(self.active_runs),
                                "available": self.capacity()},
                "device": "Your iPhone" if device else None,
                "driver": target["driver"],
                "extensions": dict(tracks.STATUS),
                "limitations": self.limitations(key=key, device=device, ready=ready, health=health, wda=wda,
                                                can_act=target["can_act"],
                                                engine_reason=available[engines.SMART]["reason"])}

    def device_count(self):
        fleet = getattr(self, "fleet", None)
        if fleet is None:
            return 1
        with fleet.lock:
            others = sum(1 for phone in fleet.phones.values() if phone.wda_url)
        return 1 + others

    def resolve_default(self, sticky=True):
        try:
            return self.fleet.default(sticky=sticky)
        except LookupError as error:
            raise APIError(str(error), 404, "device_not_found") from None

    def resolve_device(self, device):
        try:
            return self.fleet.for_run(device)
        except DeviceNotFound as error:
            raise APIError(str(error), 404, "device_not_found") from None
        except AmbiguousDevice as error:
            raise APIError(str(error), 400, "device_ambiguous") from None
