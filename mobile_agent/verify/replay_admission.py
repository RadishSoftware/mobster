"""Freeze a repeated check's assets, action path and exact device environment before reset."""

from .smart import is_real_device
from .verdict import CouldntRun


class ReplayAdmission:
    def _pin_assets(self):
        pin = getattr(self.replay, "pin", None) if self.replay is not None else getattr(self, "repeat_pin", None)
        if (self.check.throughout or self.check.somewhere) and (
                pin is not None or self.replay is not None and self.replay.mode == "strict"):
            reason = "A fixed replay cannot cover throughout or somewhere assertions on every visited screen."
            if self.replay is not None:
                self.replay.refuse_pin(reason)
            raise CouldntRun("replay", reason)
        if pin is None:
            return
        from ..replay_core.heal import HealError, proof_assets
        reason = pin.bind_check(self.check, self._live)
        if reason is None:
            try:
                reason = pin.bind_assets(proof_assets(self.check))
            except HealError as error:
                reason = str(error)
        if reason:
            if self.replay is not None:
                self.replay.refuse_pin(reason)
            raise CouldntRun("replay", reason)

    def _pin_replay_before_effects(self):
        pin = getattr(self.replay, "pin", None) if self.replay is not None else getattr(self, "repeat_pin", None)
        if self.replay is not None:
            path = self.replay.lookup()
            if path is None and (self.replay.mode == "strict" or pin is not None):
                self.replay.refuse_pin(f"A fixed replay cannot run: {self.replay.reason or 'no approved path'}.")
        elif pin is not None:
            from ..replay_core.checks import identity
            from ..replay_core.store import env_of
            real = is_real_device(self.manager)
            model = getattr(self.target, "device_type", None)
            if real and model == "iPhone":
                model = None
            environment = env_of(getattr(self.app, "version", None), getattr(self.target, "runtime", None),
                                 "iphone" if real else "simulator", device_type=model)
            reason = pin.bind(identity(self.check, self.business_binding),
                              {**environment, "runtime": getattr(self.target, "runtime", None)}, "")
            if reason:
                raise CouldntRun("replay", reason)
