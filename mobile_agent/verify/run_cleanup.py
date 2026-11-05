"""Resolve run-owned cleanup under the lease before composing the terminal verdict."""

from .actions import ActionError
from .verdict import CouldntRun


class RunCleanup:
    def _action_failure(self):
        klass = self.actions.klass
        return CouldntRun(klass, str(ActionError(klass))) if klass is not None else None

    def _restore_conditions(self):
        if self._conditions is None or self._conditions_restored:
            return None
        self._conditions_restored = True
        if self.actions.blocked or self.error is not None and self.error["klass"] == "unknown_outcome":
            self.conditions_cleanup = "needs_reconciliation"
            return None
        try:
            self._conditions.restore(self.actions)
        except BaseException as error:
            self.conditions_cleanup = "needs_reconciliation"
            return self._action_failure() or CouldntRun("environment",
                                                       f"Device conditions were not confirmed restored "
                                                       f"({type(error).__name__}).")
        self.conditions_cleanup = "restored"
        return None

    def _cleanup_after_run(self):
        business_error = self._cleanup_business()
        condition_error = self._restore_conditions()
        return self._action_failure() or condition_error or business_error

    def _release(self):
        """Stop evidence, close the driver and release the lease once, even across abort and finish threads."""
        if self._sampler is not None:   # the perf sampler's thread (perf.py), on every way out of a run
            self._sampler.stop()
        self._stop_evidence()
        with self._release_lock:
            driver = None if self._driver_closed else self.driver
            if driver is not None:
                self._driver_closed = True
            lease, self.lease = self.lease, None
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass
            try:
                if self.source_observation is not None:
                    self.source_observation.detach(driver)
            except Exception as error:
                self.log(f"source observer detach failed: {type(error).__name__}")
            finally:
                self.actions.unwrap_http(driver)
        if lease is not None:
            if self._conditions is not None and self._conditions.net is not None:
                try:
                    self._conditions.net.write(self.run_dir)
                    if self._wire is not None:
                        self._wire.write(self.run_dir)
                except OSError:
                    pass
            try:
                lease.release()
            except Exception as error:
                self.log(f"lease release failed: {type(error).__name__}")
