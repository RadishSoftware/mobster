"""What every part of the WDA driver refuses with and checks before it acts: the typed refusal a driver raises, and
the checks that a target belongs to the observation it was given and that a number is a usable size."""

import math
from typing import Literal, TypedDict

from .transport import TransportError


class ActivationDiagnostics(TypedDict, total=False):
    activation_route: Literal["table_selection", "control_primary_action", "control_primary_event",
                              "control_touch_up_inside", "accessibility", "physical_tap",
                              "navigation_pop"]
    dispatch_attempted: bool


def activation_diagnostics(payload: object) -> ActivationDiagnostics:
    """Bounded telemetry only. Missing diagnostics never mean no dispatch or safe retry."""
    result: ActivationDiagnostics = {}
    if not isinstance(payload, dict):
        return result
    route = payload.get("activation_route")
    if isinstance(route, str) and route in {"table_selection", "control_primary_action", "control_primary_event",
                                          "control_touch_up_inside", "accessibility", "physical_tap",
                                          "navigation_pop"}:
        result["activation_route"] = route
    attempted = payload.get("dispatch_attempted")
    if type(attempted) is bool:
        result["dispatch_attempted"] = attempted
    return result


class DriverRejection(TransportError):
    """A driver's typed refusal. Known codes only; never arbitrary app text or relay output."""
    CODES = frozenset({"app_not_active", "app_preview_unavailable", "app_preview_uniform",
        "app_preview_oversized", "incomplete_accessibility_tree", "stale_revision", "target_occluded",
        "unsupported_or_stale_target", "selection_changed", "invalid_text", "text_target_changed",
        "main_thread_busy", "native_exception_outcome_unknown", "main_thread_deadline_expired_outcome_unknown",
        "request_deadline_expired", "request_cancelled", "activation_declined", "response_serialization_failed",
        "bridge_update_required", "physical_tap_not_authorized", "physical_tap_outcome_unknown",
        "home_outcome_unknown", "volup_outcome_unknown", "voldown_outcome_unknown",
        "app_launch_outcome_unknown"})

    # Codes the bridge returns BEFORE its documented dispatch boundary, where no
    # app code has run. Recovering from these is not a replay: the agent observes
    # again and decides again. Every other code -- especially the *_outcome_unknown
    # family -- stays ambiguous and terminal, because an action that may have
    # executed must never be repeated automatically.
    PRE_DISPATCH = frozenset({"stale_revision", "unsupported_or_stale_target",
                              "target_occluded", "incomplete_accessibility_tree"})

    def __init__(self, code, diagnostics=None):
        self.code = code if isinstance(code, str) and code in self.CODES else "native_request_rejected"
        self.diagnostics = activation_diagnostics(diagnostics)
        super().__init__("Driver refused: " + self.code + "; no automatic retry")


def finite_positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def grounded_target(operation, target, snapshot):
    """Adapters are safety boundaries too, even when invoked without the agent loop."""
    if target is None or not any(target == element for element in snapshot.elements):
        raise ValueError("Action target is not part of the supplied observation")
    if operation in {"TYPE", "TYPE_SUBMIT"} and not target.editable:
        raise ValueError("TYPE requires an editable target")
