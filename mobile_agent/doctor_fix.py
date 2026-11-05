"""`mobster doctor --fix`: do the fix each finding's row carries (``states.ROWS``), and say what it did and what it
could not.

The row decides how far Mobster goes, not this module:

- ``Auto`` runs unasked: start Mobster's helper, refresh one that has expired. It touches only Mobster's own state, and
  only on a phone no other Mobster is running a task on.
- ``Ask`` names the process or command first and runs only on a yes: free a port, stop a service that doesn't answer.
  Without a terminal it names it and does nothing. It never signals a process whose command line changed since it asked,
  never uses SIGKILL without a second question, and never kills by name.
- ``Guided`` hands over to the flow that asks its own questions (``mobster login``, ``mobster setup``), in a terminal.
- ``Yours``, ``Run`` and ``Wait`` are reported, not run: the iPhone's own taps, a command Mobster doesn't run for you, a
  state that clears by itself.

Every action converges: run twice, or after a crash, it reads the state and does only what is left. A phone action whose
outcome is unknown is never retried here; the helper start reads the phone's answer and stops.
"""

import functools
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from typing import NamedTuple

from . import states
from .states import Ask, Auto, Guided

FIXED, FAILED, REFUSED, DECLINED, NEEDS_TERMINAL, YOURS = ("fixed", "failed", "refused", "declined",
                                                           "needs_terminal", "yours")

# How long a started helper has to answer (Mobster's first start on a phone took 2.4 minutes on 17 Oct 2026, of which 60
# s was iOS's own timeout; a restarted helper answers in about 20 s), how long a refresh may build, and how long a
# stopped process has to exit after SIGTERM.
HELPER_WAIT_S = 90.0
BUILD_WAIT_S = 20 * 60.0
STOP_WAIT_S = 5.0
PROGRESS_EVERY_S = 15.0


class Holder(NamedTuple):
    port: int
    pid: int
    command: str


class Result(NamedTuple):
    outcome: str
    detail: str


class Refused(Exception):
    """An action that leaves things alone, with the sentence that says why (``refusing`` turns it into a Result)."""


@dataclass
class Step:
    """One finding's fix: what it was, what happened, and why."""
    cause: str
    check: str
    kind: str
    outcome: str
    detail: str

    def public(self):
        return {"cause": self.cause, "check": self.check, "kind": self.kind, "outcome": self.outcome,
                "detail": self.detail}


# -- the world, replaceable in tests ---------------------------------------------------------------------------------

# Which program holds a port is in the kernel's socket list: `netstat -anv` gives it as process:pid in about half a
# second (scripts/measure_doctor_time.py). `lsof`, which walks every process's files, took 8 s at a load average of 70
# on 18 Oct 2026. A connect to the port answers in milliseconds, so the list is read only for a port something holds.
NETSTAT_S = 6.0


def _netstat():
    import subprocess
    try:
        done = subprocess.run(["/usr/sbin/netstat", "-anv", "-p", "tcp"], capture_output=True, text=True,
                              timeout=NETSTAT_S)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout


def parse_netstat(text):
    """[(port, pid)] of the listening sockets in `netstat -anv -p tcp`: a row is the protocol, two queues, the local
    address (host.port), the foreign one, the state, two byte counts, two buffer sizes and process:pid. A row that
    doesn't read that way is skipped, so a changed format names nobody and nothing is stopped."""
    found = []
    for line in (text or "").splitlines():
        tokens = line.split()
        if len(tokens) < 11 or not tokens[0].startswith("tcp") or tokens[5] != "LISTEN":
            continue
        owner = re.fullmatch(r"(.+):(\d+)", tokens[10])
        port = tokens[3].rpartition(".")[2]
        if owner and port.isdigit():
            found.append((int(port), int(owner[2])))
    return found


def taken(ports, listening=None):
    """The ports something accepts connections on, read in milliseconds with no process table."""
    from .devices import listening as probe
    return [port for port in ports if (listening or probe)(port)]


def holders(ports, netstat=None):
    """The programs listening on ``ports`` (other than this process), each with its whole command line. A port that
    something holds but the list couldn't name has no holder here: nothing is stopped for a program Mobster can't
    name."""
    from .supervised import command_of
    wanted = {int(port) for port in ports}
    found, seen = [], set()
    for port, pid in parse_netstat((netstat or _netstat)()):
        if port in wanted and pid != os.getpid() and (port, pid) not in seen:
            seen.add((port, pid))
            found.append(Holder(port, pid, command_of(pid) or str(pid)))
    return found


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Context:
    """What the actions work with. ``ask(question)`` is True or False for the person's answer, None without a terminal;
    ``say`` prints a progress line (nothing in a JSON run)."""

    def __init__(self, args=None, *, ask=None, say=None, open_runtime=None, clock=time.monotonic, sleep=time.sleep,
                 netstat=None, listening=None, urls=None, read=None, kill=os.kill, alive=_alive):
        self.args = args
        self.ask = ask or (lambda question: None)
        self.say = say or (lambda line: None)
        self._open_runtime = open_runtime
        self.clock, self.sleep, self.netstat, self.listening, self.kill, self.alive = (clock, sleep, netstat, listening,
                                                                                       kill, alive)
        self._urls, self._read = urls, read
        self.runtime = None

    def manager(self):
        """The chosen phone's manager, from the runtime ``mobster setup`` uses (its helper outlives this process). Never
        opened while a Mobster service answers: it manages the phone's helper itself, and a second manager would race it
        for the phone's ports."""
        if self.runtime is None:
            if self.service_answers():
                raise Refused("Mobster for Mac or mobster serve is running and manages the iPhone's helper itself. "
                              "Fix it from its Devices page, or quit it and run mobster doctor --fix again.")
            from . import setup_cli
            self.runtime = (self._open_runtime or setup_cli.open_runtime)()
            device = getattr(self.args, "device", None)
            if device:
                setup_cli.choose(self.runtime, device)
        return self.runtime.manager

    def close(self):
        if self.runtime is not None:
            self.runtime.close(timeout=5)
            self.runtime = None

    def service_urls(self):
        from .threads.client import service_urls
        return service_urls() if self._urls is None else self._urls

    def service_answers(self):
        """Whether a Mobster service (Mobster for Mac, `mobster serve`) answers on this Mac."""
        from .engine_health import read_from_app
        urls = self.service_urls()
        if not urls:
            return False
        engine = (self._read or read_from_app)(urls[0])
        return not (engine and engine.get("cause") in ("no_answer", "not_running"))


def interactive_ask(question):
    """The question on the terminal: True for yes, False for anything else, None when stdin isn't a terminal."""
    if not sys.stdin.isatty():
        return None
    try:
        answer = input(f"? {question} [y/N] ").strip().lower()
    except EOFError:
        return None
    return answer in ("y", "yes")


# -- the actions ---------------------------------------------------------------------------------------------------

def _phone_name(manager):
    name = manager.settings().get("device_name")
    return name if isinstance(name, str) and name.strip() else "your iPhone"


def refusing(action):
    """``action`` that answers ``Refused`` with a refused Result."""
    @functools.wraps(action)
    def wrapped(ctx, check):
        try:
            return action(ctx, check)
        except Refused as why:
            return Result(REFUSED, str(why))
    return wrapped


def _busy(manager):
    """The reason to leave this phone alone, or None: another Mobster is running a task on it, or it isn't plugged
    in."""
    from .journal import JournalError, Lease
    try:
        Lease.device(manager.wda_url).close()
    except JournalError as error:
        owner = getattr(error, "owner", None)
        who = f"{owner} is" if owner else "Another Mobster is"
        return f"{who} running a task on {_phone_name(manager)}, so Mobster left it alone."
    if manager.attached() is not True:
        message, step, _ = states.words(states.WAITING_FOR_PHONE, "unplugged", _phone_name(manager))
        return f"{message} {step}"
    return None


@refusing
def start_helper(ctx, check):
    """Start Mobster's helper on the chosen phone and wait for it to answer. Nothing is started on a phone another
    Mobster holds, on one that isn't plugged in, or for a helper that isn't built or has expired (those are other
    rows)."""
    manager = ctx.manager()
    name = _phone_name(manager)
    if (why := _busy(manager)) is not None:
        return Result(REFUSED, why)
    if manager.expired():
        return Result(REFUSED, "Its signature has expired, so it needs a refresh first.")
    if not manager.built():
        return Result(REFUSED, f"Mobster's helper isn't built for {name}. Run mobster setup.")
    if manager.wda_ready():
        return Result(FIXED, f"Mobster's helper already answers on {name}.")
    try:
        manager.start_runner()
    except LookupError as error:
        return Result(REFUSED, str(error))
    return _await_helper(ctx, manager, name)


def _await_helper(ctx, manager, name):
    end = ctx.clock() + HELPER_WAIT_S
    told = ctx.clock()
    while ctx.clock() < end:
        if manager.wda_ready():
            return Result(FIXED, f"Started Mobster's helper on {name}. It answers.")
        if ctx.clock() - told >= PROGRESS_EVERY_S:
            ctx.say(f"Waiting for Mobster's helper on {name}...")
            told = ctx.clock()
        ctx.sleep(1.0)
    manager.runner_hint()
    cause = manager.runner_cause if manager.runner_cause in states.BY_CAUSE else "failing"
    message, step, _ = states.words(states.row(cause).state, cause, name)
    return Result(FAILED, f"Mobster's helper didn't answer in {int(HELPER_WAIT_S)} s. {message} {step}")


@refusing
def rebuild_helper(ctx, check):
    """Refresh the helper whose 7-day signature ran out, with the Apple Account it was built with, as Mobster for Mac
    does while the phone is plugged in. It never picks an account: with none saved it points at ``mobster setup``."""
    manager = ctx.manager()
    name = _phone_name(manager)
    if (why := _busy(manager)) is not None:
        return Result(REFUSED, why)
    if not manager.expired():
        return start_helper(ctx, check)
    try:
        manager.start_build(renewal=True)
    except (ValueError, LookupError) as error:
        return Result(REFUSED, f"{error}. Run mobster setup.")
    end = ctx.clock() + BUILD_WAIT_S
    told = started = ctx.clock()
    while manager.build["state"] == "running":
        if ctx.clock() >= end:
            return Result(FAILED, f"Refreshing the helper on {name} was still running after "
                                  f"{int(BUILD_WAIT_S // 60)} minutes. It keeps going; run mobster doctor again.")
        if ctx.clock() - told >= PROGRESS_EVERY_S:
            ctx.say(f"Refreshing Mobster's helper on {name}... {int(ctx.clock() - started)} s")
            told = ctx.clock()
        ctx.sleep(2.0)
    if manager.build["state"] != "succeeded":
        return Result(FAILED, manager.build.get("error") or f"Refreshing the helper on {name} failed.")
    try:
        manager.start_runner()
    except LookupError as error:
        return Result(FAILED, f"Refreshed the helper, but it didn't start: {error}")
    return _await_helper(ctx, manager, name)


def _confirm(ctx, question):
    """The answer to ``question``, or the Result that ends the action: no terminal, or a no."""
    answer = ctx.ask(question)
    if answer is None:
        return None, Result(NEEDS_TERMINAL, f"{question} Run mobster doctor --fix in a terminal to answer.")
    if not answer:
        return None, Result(DECLINED, "You said no, so Mobster left it running.")
    return True, None


def _stop(ctx, holder):
    """SIGTERM ``holder``, wait for it to exit, and only after a second yes SIGKILL it. Only the process Mobster named:
    if its command line changed since, it is a different process and is left alone."""
    from .supervised import command_of
    if command_of(holder.pid) != holder.command:
        return Result(REFUSED, f"Process {holder.pid} is no longer {_short(holder.command)}, so Mobster left it alone.")
    try:
        ctx.kill(holder.pid, signal.SIGTERM)
    except ProcessLookupError:
        return Result(FIXED, f"{_short(holder.command)} had already stopped.")
    except PermissionError:
        return Result(FAILED, f"{_short(holder.command)} (pid {holder.pid}) belongs to another user, so Mobster can't "
                              "stop it.")
    end = ctx.clock() + STOP_WAIT_S
    while ctx.clock() < end:
        if not ctx.alive(holder.pid):
            return Result(FIXED, f"Stopped {_short(holder.command)} (pid {holder.pid}).")
        ctx.sleep(0.1)
    question = (f"{_short(holder.command)} (pid {holder.pid}) is still running {int(STOP_WAIT_S)} s after it was "
                "asked to stop. Force it to stop?")
    answer = ctx.ask(question)
    if not answer:
        return Result(FAILED if answer is None else DECLINED,
                      f"{_short(holder.command)} (pid {holder.pid}) is still running. Mobster didn't force it.")
    if command_of(holder.pid) != holder.command:
        return Result(REFUSED, f"Process {holder.pid} changed, so Mobster left it alone.")
    try:
        ctx.kill(holder.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return Result(FIXED, f"Stopped {_short(holder.command)} (pid {holder.pid}).")


def _short(command):
    return command if len(command) <= 80 else command[:79] + "…"


@refusing
def free_port(ctx, check):
    """Free the ports Mobster's helper relays on, after naming the program that holds each and being told yes."""
    manager = ctx.manager()
    if (why := _busy(manager)) is not None:
        return Result(REFUSED, why)
    held = taken((manager.wda_port, manager.video_port), ctx.listening)
    if not held:
        return Result(FIXED, f"Ports {manager.wda_port} and {manager.video_port} are free.")
    found = holders(held, ctx.netstat)
    if not found:
        return Result(REFUSED, f"Something holds port {held[0]}, and Mobster couldn't read which program, so it "
                               "left it alone.")
    details = []
    for holder in found:
        question = (f"Port {holder.port} is held by {_short(holder.command)} (pid {holder.pid}). It may be another "
                    "Mobster's connection to a phone. Stop it?")
        yes, ended = _confirm(ctx, question)
        if ended is not None:
            return ended
        result = _stop(ctx, holder)
        details.append(result.detail)
        if result.outcome != FIXED:
            return Result(result.outcome, " ".join(details))
    return Result(FIXED, " ".join(details))


@refusing
def restart_service(ctx, check):
    """Stop Mobster for Mac's service when it accepts connections and never answers, so the Mac app starts it again. It
    stops only a program that is listening where the service wrote its address and has Mobster in its command line."""
    from urllib.parse import urlsplit
    urls = ctx.service_urls()
    if not urls:
        return Result(REFUSED, "Mobster for Mac left no address, so there is no service to stop.")
    port = urlsplit(urls[0]).port
    if not taken((port,), ctx.listening):
        message, step, _ = states.words(states.DOWN, "not_running")
        return Result(REFUSED, f"{message} {step}")
    found = holders((port,), ctx.netstat)
    if not found:
        return Result(REFUSED, f"Mobster couldn't read which program holds port {port}, so it left it alone.")
    holder = found[0]
    if "mobster" not in holder.command.lower():
        return Result(REFUSED, f"The program on port {port} is {_short(holder.command)}, not Mobster, so Mobster "
                               "left it alone.")
    question = (f"Mobster's service (pid {holder.pid}) isn't answering. Stop it so Mobster for Mac starts it "
                "again? A task in flight is kept in History and is not resent.")
    yes, ended = _confirm(ctx, question)
    if ended is not None:
        return ended
    return _stop(ctx, holder)


ACTIONS = {"start_helper": start_helper, "rebuild_helper": rebuild_helper, "free_port": free_port,
           "restart_service": restart_service}
# Fixes that depend on each other run in this order: the port first, then the helper, then the service.
ORDER = ("free_port", "rebuild_helper", "start_helper", "restart_service")


# -- the plan and the run -------------------------------------------------------------------------------------------

def _rank(row):
    match row.fix:
        case Auto(action) | Ask(action):
            return (0, ORDER.index(action))
        case Guided():
            return (1, 0)
    return (2, 0)


def plan(checks):
    """[(check, row)] to act on: each WARN or FAIL finding that is a row, once per distinct fix, in the order the fixes
    depend on each other. A finding that is not a row has nothing to run."""
    from .doctor import FAIL, WARN
    chosen, seen = [], set()
    for check in checks:
        if check.state not in (WARN, FAIL) or not check.cause:
            continue
        row = states.row(check.cause)
        identity = (type(row.fix).__name__, getattr(row.fix, "action", None) or getattr(row.fix, "command", None)
                    or row.cause)
        if identity not in seen:
            seen.add(identity)
            chosen.append((check, row))
    return sorted(chosen, key=lambda item: _rank(item[1]))


def guided(command, ctx):
    """Run the guided flow a ``Guided`` fix names. Each asks its own questions, so each needs a terminal."""
    from argparse import Namespace
    if command == "mobster login":
        from .login import login
        return login(Namespace(provider=None, with_key=False, skip_check=False, json=False))
    if command == "mobster setup":
        from .setup_cli import run
        return run(Namespace(device=getattr(ctx.args, "device", None), team=None, json=False))
    raise KeyError(command)


def run(checks, ctx, terminal=True):
    """Act on ``checks`` and return the ``Step`` for each. Order: the unasked and asked fixes, then the guided flows,
    then the rest are reported as the person's."""
    steps = []
    for check, row in plan(checks):
        kind = type(row.fix).__name__.lower()
        match row.fix:
            case Auto(action) | Ask(action):
                result = ACTIONS[action](ctx, check)
            case Guided(command):
                if not terminal:
                    result = Result(NEEDS_TERMINAL, f"{command} asks you questions. Run it in a terminal.")
                else:
                    code = guided(command, ctx)
                    result = Result(FIXED if code == 0 else FAILED,
                                    f"{command} finished." if code == 0 else f"{command} stopped (exit {code}).")
            case _:
                result = Result(YOURS, states.how(row.fix))
        steps.append(Step(row.cause, check.key, kind, result.outcome, result.detail))
    return steps
