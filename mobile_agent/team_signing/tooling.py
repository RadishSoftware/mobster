"""Install and launch the signed runner without Xcode: pymobiledevice3 first, go-ios as the fallback.

Both are separate programs you install yourself: ``pipx install pymobiledevice3`` (GPL-3.0-or-later), or go-ios (MIT).
Mobster never imports or bundles either. It builds their command lines and runs them as it runs ``xcodebuild`` today:
the install is a short command, the launch is the long-running runner that Mobster's supervisor keeps alive and whose
readiness it checks with the same ``/status`` probe. ``xcodebuild`` and ``xcrun`` are never among them.

Unverified on a real iPhone as of 19 Oct 2026: the commands follow pymobiledevice3 11.18.0's and go-ios's own help. The
install-without-Xcode proof is pending a phone (team_signing/README in docs/device-setup.mdx says so).
"""

import json
import os
import re
import shutil
import zipfile
from pathlib import Path

from ..devkit import DevError
from ..phone_io import tunnel

PMD3 = "pymobiledevice3"
GO_IOS = "ios"
DRIVERS = ("xcodebuild", "pymobiledevice3", "go-ios")
LOOPBACK = "127.0.0.1"
UDID = re.compile(r"[0-9A-Fa-f-]{24,40}")
NEVER = frozenset({"xcodebuild", "xcrun", "xcode-select", "devicectl"})
GO_IOS_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    os.path.expanduser("~/go/bin"),
    os.path.expanduser("~/.local/bin"),
)


def pmd3():
    """pymobiledevice3 on PATH or in the usual pipx and Homebrew folders; None when it isn't installed."""
    return tunnel.tool()


def go_ios():
    """go-ios's ``ios`` on PATH or in the usual folders; None when it isn't installed."""
    for candidate in (shutil.which(GO_IOS), *(os.path.join(directory, GO_IOS) for directory in GO_IOS_DIRS)):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def choose_driver(*, flag, pmd3_path=None, go_ios_path=None):
    """Which program installs and launches the runner. "xcodebuild" unless Without Xcode is on (__init__.FLAG); then
    pymobiledevice3 if it is installed, else go-ios. DevError, with how to install, when neither is."""
    if not flag:
        return "xcodebuild"
    if pmd3_path:
        return "pymobiledevice3"
    if go_ios_path:
        return "go-ios"
    raise DevError(
        "Mobster needs pymobiledevice3 or go-ios to put its helper on your iPhone without Xcode, and neither "
        "is on this Mac.",
        "Install pymobiledevice3 with `pipx install pymobiledevice3`, then try again.",
    )


def _udid(udid):
    if not isinstance(udid, str) or not UDID.fullmatch(udid):
        raise DevError("That isn't an iPhone's identifier.")
    return udid


def _check(argv):
    if Path(argv[0]).name in NEVER:
        raise DevError(f"Mobster didn't run {Path(argv[0]).name}: this path never starts Xcode's tools.")
    return argv


# -- pymobiledevice3 -----------------------------------------------------------------------------------------------


def developer_mode_command(program, udid):
    return _check([program, "amfi", "developer-mode-status", "--native", "--udid", _udid(udid)])


def reveal_developer_mode_command(program, udid):
    """Makes the Developer Mode switch appear in Settings › Privacy & Security. The person still turns it on."""
    return _check([program, "amfi", "reveal-developer-mode", "--native", "--udid", _udid(udid)])


def mounted_command(program, udid):
    return _check([program, "mounter", "list", "--native", "--udid", _udid(udid)])


def mount_command(program, udid):
    """Mounts Apple's developer disk image for the phone (it downloads the image and asks Apple's signing service to
    personalize it, as Xcode does the first time a phone is prepared)."""
    return _check([program, "mounter", "auto-mount", "--native", "--udid", _udid(udid)])


def install_command(program, udid, app):
    return _check([program, "apps", "install", str(app), "--native", "--udid", _udid(udid)])


def launch_command(program, udid, runner_bundle_id, env):
    """The XCTest session that is Mobster's helper. It runs until stopped. ``env`` is what the runner reads
    (USE_IP, the capture gate)."""
    command = [program, "developer", "dvt", "xcuitest", runner_bundle_id, "--native", "--udid", _udid(udid)]
    for key, value in sorted(env.items()):
        command += ["--env", f"{key}={value}"]
    return _check(command)


def runner_env(kind="phone", environ=None):
    """The environment the runner starts with, as KEY=VALUE for ``--env``: USE_IP on the loopback (the USB relay reaches
    it there) and the capture gate for an iPhone (wda_gate)."""
    from ..wda import gate as wda_gate

    gate = wda_gate.gate_for(kind, environ)
    return {"USE_IP": LOOPBACK, wda_gate.OVERRIDE: gate}


# -- go-ios --------------------------------------------------------------------------------------------------------


def ipa_of(app, folder):
    """A .ipa (Payload/<name>.app) of the signed runner, for go-ios, which installs packages and not bundles."""
    app = Path(app)
    target = Path(folder) / f"{app.stem}.ipa"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(app.rglob("*")):
            relative = f"Payload/{app.name}/{path.relative_to(app).as_posix()}"
            info = zipfile.ZipInfo.from_file(path, relative)
            if path.is_symlink():
                info.external_attr = 0o120755 << 16
                archive.writestr(info, os.readlink(path))
            elif path.is_dir():
                continue
            else:
                with open(path, "rb") as stream:
                    archive.writestr(info, stream.read(), zipfile.ZIP_DEFLATED)
    return target


def go_ios_install_command(program, udid, ipa):
    return _check([program, "install", f"--path={ipa}", f"--udid={_udid(udid)}"])


def go_ios_launch_command(program, udid, runner_bundle_id, env):
    """go-ios's ``runwda``: the runner's bundle id, its test bundle and the xctest configuration it runs."""
    bundle = runner_bundle_id.removesuffix(".xctrunner")
    command = [
        program,
        "runwda",
        f"--bundleid={bundle}",
        f"--testrunnerbundleid={runner_bundle_id}",
        "--xctestconfig=WebDriverAgentRunner.xctest",
        f"--udid={_udid(udid)}",
    ]
    command += [f"--env={key}={value}" for key, value in sorted(env.items())]
    return _check(command)


# -- one place to run the short commands ---------------------------------------------------------------------------


def run_short(command, *, run=None, timeout=300):
    """Run an install-time command (not the launch). ``run(argv, timeout) -> (code, output)`` replaces the program in
    tests. DevError with the program's last line when it fails."""
    import subprocess

    if run is None:

        def run(argv, timeout):
            from ..native_helpers import require_devices

            require_devices()
            env = dict(os.environ, NO_COLOR="1", TERM="dumb", COLUMNS="200")
            try:
                done = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=timeout,
                    stdin=subprocess.DEVNULL,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                return 124, f"{Path(argv[0]).name} did not finish in {timeout} seconds"
            except OSError as error:
                return 127, type(error).__name__
            return done.returncode, (done.stdout or "") + (done.stderr or "")

    code, output = run(command, timeout)
    if code != 0:
        line = next((line for line in reversed(str(output).splitlines()) if line.strip()), "")
        raise DevError(
            f"{Path(command[0]).name} couldn't {command[1]}. {line[:200]}".strip(),
            "Keep the iPhone unlocked and plugged in, then try again.",
        )
    return output


def developer_mode(program, udid, *, run=None):
    """True or False from pymobiledevice3, None when it can't say (the phone doesn't trust this Mac, or the tool is
    missing). Reads the phone's Developer Mode state over USB; changes nothing."""
    try:
        output = run_short(developer_mode_command(program, udid), run=run, timeout=30)
    except DevError:
        return None
    words = output.strip().lower().splitlines()
    last = words[-1] if words else ""
    if re.search(r"\btrue\b|\benabled\b", last):
        return True
    if re.search(r"\bfalse\b|\bdisabled\b", last):
        return False
    return None


def mounted(program, udid, *, run=None):
    """Whether a developer disk image is mounted on the phone: ``mounter list`` prints a JSON list of what is."""
    try:
        output = run_short(mounted_command(program, udid), run=run, timeout=60)
    except DevError:
        return False
    try:
        listed = json.loads(output)
    except ValueError:
        return False
    return bool(listed)


def ensure_mounted(program, udid, *, run=None):
    """Mount the developer disk image unless one is. True when this call mounted it. A phone that Xcode (on any Mac)
    prepared already has it, and nothing is downloaded or asked of Apple's signing service."""
    if mounted(program, udid, run=run):
        return False
    try:
        run_short(mount_command(program, udid), run=run, timeout=900)
    except DevError as error:
        if "already" not in error.message.lower():
            raise
        return False
    return True
