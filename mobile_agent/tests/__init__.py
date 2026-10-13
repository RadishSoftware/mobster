"""Offline backend tests. No devices, provider calls or real keys.

Every test module imports this package first, so ``guard()`` runs before any test does. A test never sees a key from
this Mac: not one in the shell's environment, not the Mac app's agent.env, not the key vault's files, not the
Keychain, also when a test clears its environment. A failure message shows a key-shaped value only by its last four
characters. Nor does a test run this Mac's Codex, Claude Code or Gemini CLI, or a login shell.
"""

import atexit
import errno
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import unittest.result

from mobile_agent.secret_filter import API_KEY as _API_KEY

# A key, token or secret by its name: OPENAI_API_KEY, TEXT_MODEL_API_KEY, TYPESAFE_API_KEY, GEMINI_API_KEY, HF_TOKEN,
# ANTHROPIC_AUTH_TOKEN, GOOGLE_APPLICATION_CREDENTIALS, MOBSTER_API_TOKEN_FILE. test_no_real_keys checks that it
# covers every name keys.py and the provider registry read.
SECRET_NAME = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)(?:_FILE|_PATH)?$", re.I)
# A key by its shape, whatever variable holds it: secret_filter's shapes, and the providers' own prefixes it lacks
# (Jev's apikey_, xAI, Groq, Hugging Face).
KEY_SHAPE = re.compile(_API_KEY.pattern + r"|\b(?:apikey_|xai-|gsk_|hf_)[A-Za-z0-9_-]{16,}")
# The helper model keys.py sets up (keys.HELPER_ENV): with these a test would use the helper this Mac chose.
HELPER = ("TEXT_MODEL", "TEXT_MODEL_BASE_URL", "TEXT_MODEL_PROVIDER", "MOBSTER_HELPER_PROVIDER",
          "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")
# Files and folders that hold keys or sign-ins: an env file every command loads, the coding CLIs' and gcloud's
# configuration.
KEY_PLACES = ("MOBSTER_ENV_FILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME", "XDG_CONFIG_HOME",
              "CLOUDSDK_CONFIG")

HOME = os.path.realpath(tempfile.mkdtemp(prefix="mobster-test-home-"))
# Where this run's device leases go (journal._LEASE_ROOT, set below).
LEASES = os.path.realpath(tempfile.mkdtemp(prefix="mobster-test-leases-"))
_OWNER = os.getpid()
atexit.register(lambda: os.getpid() == _OWNER and shutil.rmtree(HOME, True))
atexit.register(lambda: os.getpid() == _OWNER and shutil.rmtree(LEASES, True))


def mask(text):
    """``text`` with every key-shaped value shown as ``<key …e3d1>``."""
    return KEY_SHAPE.sub(lambda match: f"<key …{match.group()[-4:]}>", text)


def guard(environ=os.environ):
    """Remove every key from ``environ`` and point it at this run's empty HOME. Returns the names it removed."""
    removed = sorted(name for name, value in environ.items()
                     if SECRET_NAME.search(name) or KEY_SHAPE.search(value) or name in HELPER or name in KEY_PLACES)
    for name in removed:
        del environ[name]
    # The Mac app's agent.env, the key vault's files and every other file under ~ are this run's empty ones.
    environ["HOME"] = HOME
    # Plan sign-ins and provider keys go to files in that HOME, never the person's Keychain; so do Mobster Remote's
    # device key and the outlets' secrets.
    environ["MOBSTER_VAULT"] = "file"
    environ["MOBSTER_REMOTE_KEYCHAIN"] = "file:" + os.path.join(HOME, "remote-keychain.json")
    environ["MOBSTER_OUTLETS_KEYCHAIN"] = "file:" + os.path.join(HOME, "outlets-keychain.json")
    environ["MOBSTER_CONNECTORS_KEYCHAIN"] = "file:" + os.path.join(HOME, "connectors-keychain.json")
    # Nor does a test find the Codex, Claude Code or Gemini CLI installed on this Mac: a test that wants one points
    # these at a fake (fake_agent_cli.py).
    for name in ("MOBSTER_CODEX_PATH", "MOBSTER_CLAUDE_CODE_PATH", "MOBSTER_GEMINI_PATH"):
        environ.setdefault(name, "/nonexistent/mobster-test-agent")
    return removed


guard()

_expanduser = posixpath.expanduser


def _test_expanduser(path):
    """~ in a test that cleared the environment (patch.dict(os.environ, clear=True)) is this run's HOME. Python
    would otherwise ask the password database and find the account's real home, with its agent.env."""
    if "HOME" in os.environ:
        return _expanduser(path)
    path = os.fspath(path)
    tilde, sep, home = ("~", "/", HOME) if isinstance(path, str) else (b"~", b"/", os.fsencode(HOME))
    if path == tilde or path.startswith(tilde + sep):
        return home + path[1:]
    return _expanduser(path)


posixpath.expanduser = _test_expanduser



def _harness(name):
    """``mobile_agent.<name>``, or None in the open-source tree, which holds the engine and not the harness."""
    import importlib
    try:
        return importlib.import_module("mobile_agent." + name)
    except ModuleNotFoundError as error:
        if error.name and ("mobile_agent." + name).startswith(error.name) and error.name != "mobile_agent":
            return None
        raise


# A test that clears the environment loses MOBSTER_VAULT too, so the Keychain is ruled out here as well.
_vault = _harness("ai_plans.vault")
if _vault is not None:
    _vault.on_mac = lambda: False

# A phone's device lease (journal.Lease.device) is this run's own, whatever HOME or $TMPDIR a test sets. In the data
# folder or the temporary folder it would meet the lease of the Mac app or a benchmark on the same address: a test
# would be refused as "busy in another Mobster process", or would keep the app off the phone while it ran. The
# tests of those folders' rules set it back to None, as every Mobster process has it (test_cli_bugs).
from mobile_agent import journal as _journal  # noqa: E402

_journal._LEASE_ROOT = LEASES

_exc_info_to_string = unittest.result.TestResult._exc_info_to_string


def _masked_exc_info_to_string(self, err, test):
    """A failure or error as unittest prints it, with key-shaped values masked: a failed assertEqual on a header
    would otherwise print the whole key."""
    return mask(_exc_info_to_string(self, err, test))


unittest.result.TestResult._exc_info_to_string = _masked_exc_info_to_string

# Every app counts as approved in a test, also one that clears the environment (permissions.policy.DEFAULT_ACCESS),
# so a task never stops at "Allow Mobster to use Messages?"; the tests of that card set MOBSTER_APP_ACCESS=ask.
from mobile_agent.permissions import policy as _policy  # noqa: E402

_policy.DEFAULT_ACCESS = "all"

# A test never finds the Codex, Claude Code or Gemini CLI installed on this Mac, nor runs a login shell to look for one,
# also a test that clears the environment and so loses guard()'s MOBSTER_CODEX_PATH and the others. A Runtime reads
# the CLIs' status on threads of its own (`codex login status`, `claude auth status`), and each fork shared the flock
# of a device lease or a journal the test had open until the child ran its program: the test's next task was refused
# as "busy in another Mobster process". A test finds only a CLI it put somewhere itself: at such a path, or in a
# SEARCH_DIRS it sets.
_cli = _harness("ai_plans.cli")
if _cli is not None:
    _cli.SEARCH_DIRS = ()  # ~/.local/bin, Homebrew, npm and the rest: where this Mac's CLIs are
    _cli.login_shell_path = lambda env=None, refresh=False: ""
    _find_binary = _cli.find_binary

    def _find_binary_but_not_this_macs(name, env=None, override=None):
        """cli.find_binary without PATH, which is this Mac's."""
        return _find_binary(name, {**(os.environ if env is None else env), "PATH": ""}, override)

    _cli.find_binary = _find_binary_but_not_this_macs

# Nor does a test reach the Keychain when it clears the environment and so loses guard()'s MOBSTER_VAULT,
# MOBSTER_REMOTE_KEYCHAIN and MOBSTER_OUTLETS_KEYCHAIN (test_engines.CLEAN drops every MOBSTER_ name). The AI plans'
# keys stay in files through vault.on_mac, above. Mobster Remote's device key and the outlets' secrets go to the same
# files in this run's HOME as with guard()'s values, unless a test names a file of its own.
_outlets_vault = _harness("outlets.vault")
_remote_keychain = _harness("remote.keychain")
if _remote_keychain is not None:
    _remote_backend = _remote_keychain.backend

    def _remote_backend_in_a_file():
        store = _remote_backend()
        return (store if isinstance(store, _remote_keychain.FileKeychain)
                else _remote_keychain.FileKeychain(os.path.join(HOME, "remote-keychain.json")))

    _remote_keychain.backend = _remote_backend_in_a_file
if _outlets_vault is not None:
    _outlets_backend = _outlets_vault.backend

    def _outlets_backend_in_a_file():
        store = _outlets_backend()
        return (store if isinstance(store, _outlets_vault.FileVault)
                else _outlets_vault.FileVault(os.path.join(HOME, "outlets-keychain.json")))

    _outlets_vault.backend = _outlets_backend_in_a_file

# Connectors' sign-ins too (a tree without the package, the public CLI, has none to guard).
try:
    from mobile_agent.connectors import vault as _connectors_vault  # noqa: E402
except ImportError:  # pragma: no cover
    _connectors_vault = None
if _connectors_vault is not None:
    _connectors_backend = _connectors_vault.backend

    def _connectors_backend_in_a_file():
        store = _connectors_backend()
        return (store if isinstance(store, _connectors_vault.FileVault)
                else _connectors_vault.FileVault(os.path.join(HOME, "connectors-keychain.json")))

    _connectors_vault.backend = _connectors_backend_in_a_file

# Whatever finds one, a test process never starts it: subprocess refuses this Mac's coding agents (any outside the
# temporary folder, where a test's fakes are), every login shell and every Keychain command of /usr/bin/security, before
# it forks. The test running then fails with what was tried, also when the code that tried swallowed the error.
CODING_AGENTS = {"codex": "MOBSTER_CODEX_PATH", "claude": "MOBSTER_CLAUDE_CODE_PATH", "gemini": "MOBSTER_GEMINI_PATH"}
SHELLS = ("sh", "bash", "zsh", "dash", "ksh", "fish", "tcsh", "csh")
_TEMPORARY = os.path.realpath(tempfile.gettempdir())
REFUSED = []  # what this process tried to start since the last test ended


def refusal(args, executable=None, env=None):
    """Why a test may not start ``args`` (subprocess's ``args``, ``executable`` and ``env``), or None."""
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
    program = os.fsdecode(executable if executable is not None else argv[0])
    name = os.path.basename(program)
    flags = [os.fsdecode(arg) for arg in argv[1:3]]
    if name in SHELLS and any(flag == "--login" or (flag[:1] == "-" and flag[1:2] != "-" and "l" in flag)
                              for flag in flags):
        return (f"A test tried to start a login shell ({program} {' '.join(flags)}), whose PATH is this Mac's. "
                "Use a fake CLI")
    if name == "security" and (flags[:1] or [""])[0] != "cms":  # cms -D decodes a profile and reads no item
        return (f"A test tried to reach this Mac's Keychain ({program} {' '.join(flags)}). Give the code a file "
                "store, as tests/__init__.py does")
    if name not in CODING_AGENTS:
        return None
    if os.sep in program:
        path = program
    else:
        path = next((os.path.join(folder, program) for folder in os.get_exec_path(env)
                     if os.path.isfile(os.path.join(folder, program))), None)
    if path is None or os.path.realpath(path).startswith(_TEMPORARY + os.sep):
        return None  # not there (subprocess says so), or a test's fake
    return (f"A test tried to start this Mac's {name} ({path}). "
            f"Point {CODING_AGENTS[name]} at a fake (fake_agent_cli.py)")


_execute_child = subprocess.Popen._execute_child


def _execute_child_but_not_this_macs(self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env, *rest,
                                     **kwargs):
    why = refusal(args, executable, env)
    if why:
        REFUSED.append(why)
        raise PermissionError(errno.EPERM, why)
    return _execute_child(self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env, *rest, **kwargs)


subprocess.Popen._execute_child = _execute_child_but_not_this_macs

_stop_test = unittest.result.TestResult.stopTest


def _stop_test_failing_what_was_refused(self, test):
    """A test during which this process tried to start what REFUSED holds fails with it."""
    tried = []
    while REFUSED:
        tried.append(REFUSED.pop(0))
    if tried:
        self.addError(test, (PermissionError, PermissionError(errno.EPERM, "; ".join(dict.fromkeys(tried))), None))
    _stop_test(self, test)


unittest.result.TestResult.stopTest = _stop_test_failing_what_was_refused
