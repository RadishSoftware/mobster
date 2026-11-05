"""Apple's own accessibility audit, run on the iPhone (`audit: ax-device`, experimental).

The phone's accessibility audit daemon (``com.apple.accessibility.axAuditDaemon.remoteserver``) checks contrast, hit
regions, element descriptions, clipped text and Dynamic Type on the screen that is up, with no model and no pixels sent
anywhere. Mobster asks for it through ``pymobiledevice3 developer accessibility``, a separate program you install
yourself (``pipx install pymobiledevice3``), over the tunnel macOS keeps to an iPhone paired for development
(``--native``, iOS 17 or later). Mobster never imports pymobiledevice3: it reads the JSON the program prints. A
simulator has no such service, so on a simulator the check reports one note and the tree audit (a11y.py) is what covers
the screen.
"""

import json
import os
import re
import subprocess

from ..phone_io import tunnel

__all__ = ["NAME", "SETTING", "enabled", "findings_from_issues", "Collector"]

NAME = "ax-device"
SETTING = "MOBSTER_EXPERIMENTAL_AUDIT"
TIMEOUT = 45.0  # seconds for one command: each opens macOS's tunnel to the phone again (about 7 s, phone_io/tunnel.py)
LIMIT = 30  # screens per run: an audit takes seconds on the phone
UDID = re.compile(r"[0-9A-Fa-f-]{24,40}")
AUDIT_TYPE = re.compile(r"[A-Za-z0-9_]{1,64}")
NO_TOOL = (
    "The on-device audit runs pymobiledevice3, which isn't on this Mac. Install it with "
    "`pipx install pymobiledevice3`, then run again."
)
# issue_classification (pymobiledevice3 names the classification it knows) -> (rule id, guideline, what to do)
RULES = {
    "testTypeContrast": (
        "device-contrast",
        "WCAG 1.4.3",
        "its text contrast is too low. Darken the text or lighten the background.",
    ),
    "testTypeHitRegion": (
        "device-hit-region",
        "HIG 44 pt",
        "its tap area is too small. Make the control or its tappable area at least 44 pt.",
    ),
    "testTypeSufficientElementDescription": (
        "device-description",
        "WCAG 4.1.2",
        "VoiceOver has no good description for it. Give it an accessibility label.",
    ),
    "testTypeElementDetection": (
        "device-element-detection",
        "WCAG 4.1.2",
        "it looks like a control the app doesn't expose. Make it an accessibility element.",
    ),
    "testTypeTextClipped": (
        "device-text-clipped",
        "WCAG 1.4.4",
        "its text is clipped. Let the text wrap or the container grow.",
    ),
    "testTypeDynamicText": (
        "device-dynamic-text",
        "WCAG 1.4.4",
        "its text doesn't follow the Dynamic Type size. Use a text style or scaled font.",
    ),
}
RECT = re.compile(r"-?\d+(?:\.\d+)?")


def enabled():
    return os.environ.get(SETTING, "").strip().lower() in ("1", "on", "true", "yes")


def _rect(value):
    numbers = [float(n) for n in RECT.findall(value)] if isinstance(value, str) else []
    return numbers if len(numbers) == 4 else [0.0, 0.0, 0.0, 0.0]


def _note(rule, message, evidence, frame):
    return {
        "id": rule,
        "audit": NAME,
        "guideline": None,
        "level": "review",
        "frame": frame,
        "message": message,
        "evidence": evidence,
        "element": None,
    }


def findings_from_issues(issues, frame=None):
    """Run findings from the audit's issues, as ``run-audit`` prints them. An issue of a kind this table doesn't know is
    kept, as a review."""
    out = []
    for issue in issues:
        kind = str(issue.get("issue_classification"))
        rule, guideline, advice = RULES.get(kind, (f"device-{kind}", None, "Apple's audit flagged it. Check it."))
        rect = _rect(issue.get("element_rect_value"))
        where = f"at {rect[0]:g}, {rect[1]:g}, {rect[2]:g} x {rect[3]:g} pt"
        detail = issue.get("ml_generated_description") or issue.get("long_description_extra_info")
        name = f" ({detail})" if isinstance(detail, str) and detail.strip() else ""
        out.append(
            {
                "id": rule,
                "audit": NAME,
                "guideline": guideline,
                "level": "review",
                "frame": frame,
                "message": f"The element{name} {where}: {advice}",
                "evidence": f"{kind} {where}",
                "element": {"role": "", "label": "", "identifier": "", "path": "", "rect": rect},
            }
        )
    return out


class ToolMissing(RuntimeError):
    """pymobiledevice3 isn't on this Mac."""


class Unavailable(RuntimeError):
    """The audit gave no answer Mobster can read. The message names the reason only, never the phone's or the app's
    text."""


def run_tool(argv, timeout):
    """(exit code, standard output) of one pymobiledevice3 command, run as a separate program for at most ``timeout``
    seconds. Its standard error is dropped: a finding never carries the program's own text."""
    from ..native_helpers import require_devices

    require_devices()
    env = dict(os.environ, NO_COLOR="1", TERM="dumb", COLUMNS="200")
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                              text=True, errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise Unavailable(f"pymobiledevice3 didn't finish in {timeout:g} seconds") from None
    except OSError as error:
        raise Unavailable(f"pymobiledevice3 couldn't start ({type(error).__name__})") from None
    return done.returncode, done.stdout


def command(program, udid, *args):
    """``pymobiledevice3 developer accessibility <args>`` for the iPhone ``udid``, over macOS's own tunnel."""
    if not isinstance(udid, str) or not UDID.fullmatch(udid):
        raise Unavailable("the run's device has no iPhone identifier")
    return [program, "developer", "accessibility", *args, "--native", "--udid", udid]


def audit_types(value):
    """The audit types the phone supports, as ``supported-audit-types`` prints them: JSON strings only. None is no
    answer, never "audit nothing": an empty list, or numbers ``run-audit`` would get as text, could pass every screen
    unchecked."""
    types = value if isinstance(value, list) else []
    if not types or not all(isinstance(item, str) and AUDIT_TYPE.fullmatch(item) for item in types):
        raise Unavailable("pymobiledevice3 listed no audit types")
    return types


def audit_issues(value):
    """The issues ``run-audit`` printed: a list of objects, or no answer."""
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise Unavailable("pymobiledevice3 printed no list of issues")
    return value


class Collector:
    """The audit's findings for a run; ``add`` audits the screen that is up on the phone, once per distinct screen."""

    def __init__(self, udid_of, *, program=tunnel.tool, run=run_tool):
        self._udid_of = udid_of  # () -> the real iPhone's UDID, or None on a simulator
        self._program, self._run = program, run  # () -> pymobiledevice3's path or None; (argv, timeout) -> (code, out)
        self._types = None  # the phone's audit types, read once a run
        self._seen, self._found, self._noted = set(), [], False

    def _note_once(self, rule, message, evidence):
        if not self._noted:
            self._noted = True
            self._found.append(_note(rule, message, evidence, None))

    def _read(self, argv):
        code, output = self._run(argv, TIMEOUT)
        if code != 0:
            raise Unavailable(f"pymobiledevice3 exited {code}")
        try:
            return json.loads(output)
        except ValueError:
            raise Unavailable("pymobiledevice3 printed no JSON") from None

    def _audit(self, udid):
        program = self._program()
        if not program:
            raise ToolMissing()
        if self._types is None:
            self._types = audit_types(self._read(command(program, udid, "supported-audit-types")))
        return audit_issues(self._read(command(program, udid, "run-audit", *self._types)))

    def add(self, tree, frame=None):
        if not enabled():
            return self._note_once(
                "device-audit-off",
                f"The on-device audit is experimental. Set {SETTING}=1 to run it.",
                f"{SETTING} is not set",
            )
        udid = self._udid_of()
        if udid is None:
            return self._note_once(
                "device-audit-simulator",
                "The on-device audit needs a real iPhone: a simulator "
                "has no audit service. The accessibility tree audit (`audit: a11y`) covers "
                "this screen.",
                "the run's device is a simulator",
            )
        fingerprint = tree.fingerprint()
        if fingerprint in self._seen or len(self._seen) >= LIMIT:
            return None
        self._seen.add(fingerprint)
        try:
            issues = self._audit(udid)
        except ToolMissing:
            return self._note_once("device-audit-no-tool", NO_TOOL, "pymobiledevice3 is not installed")
        except Exception as error:
            reason = str(error) if isinstance(error, Unavailable) else type(error).__name__
            return self._note_once(
                "device-audit-unavailable",
                f"The iPhone's audit service didn't answer ({reason}). It needs iOS 17 or later, paired with this Mac "
                "for development, unlocked, in Developer Mode and with the developer disk image mounted. Fix what's "
                "missing, then run again.",
                f"{reason} from the audit",
            )
        self._found.extend(findings_from_issues(issues, frame))
        return None

    @property
    def screens(self):
        return len(self._seen)

    def findings(self):
        return list(self._found)


def for_run(run, audit):
    """The collector for ``run`` when its check audits ``ax_device``, else None. It asks for the iPhone's UDID when it
    reads: None on a simulator or before a device is held."""
    from .smart import is_real_device
    if NAME not in audit:
        return None
    return Collector(lambda: getattr(run.target, "udid", None) if is_real_device(run._manager()) else None)
