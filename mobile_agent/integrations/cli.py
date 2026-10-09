"""`mobster mcp install`, `mobster mcp clients` and `mobster mcp doctor`.

``add_subcommands(parser, helpers)`` adds the three to `mobster mcp`'s parser (devtools.py calls it), and
``run(args)`` runs the one named in ``args.mcp_command``. `mobster mcp` with no subcommand still serves MCP.
"""

import argparse
import json
import os
from pathlib import Path
import sys

from . import clients as registry
from . import skill

USAGE_EXIT, FAILED_EXIT = 2, 1
RESTART = {"claude-desktop", "cursor", "vscode", "windsurf", "zed", "cline"}

INSTALL_DESCRIPTION = (
    "Add Mobster's MCP server to your agents' configs: Claude Code, Codex, Cursor, VS Code, Claude\n"
    "Desktop, opencode, Gemini CLI, Windsurf, Zed, Goose, Amp, Cline and oh-my-pi (omp). It writes\n"
    "the absolute path of this mobster, because apps opened from the Dock don't see your shell's PATH.\n"
    "Claude Code and Codex are set up with their own `mcp add` commands; the rest by editing the file,\n"
    "keeping its comments and formatting, with a copy of the old file beside it (.mobster-backup).\n"
    "Running it again changes nothing. It never writes a key: Smart's key stays in --env-file.")
INSTALL_EPILOG = (
    "examples:\n"
    "  mobster mcp install --all                      every client found on this Mac\n"
    "  mobster mcp install claude-code cursor --with-skill\n"
    "  mobster mcp install codex --env-file ~/.config/mobster/agent.env\n"
    "  mobster mcp install --all --allow-device \"Test iPhone\"\n"
    "  mobster mcp install cursor --project .         this repository's .cursor/mcp.json\n"
    "  mobster mcp install --all --remove --with-skill\n\n"
    "clients: " + ", ".join(client.id for client in registry.CLIENTS))


def add_subcommands(parser, helpers):
    path_type = helpers.get("path_type") or (lambda value: Path(value).expanduser())
    subs = parser.add_subparsers(dest="mcp_command", metavar="<subcommand>", title="subcommands",
                                 description="without one, `mobster mcp` serves MCP on stdio")
    install = subs.add_parser("install", help="add Mobster's server to Claude Code, Codex, Cursor and others",
                              description=INSTALL_DESCRIPTION, epilog=INSTALL_EPILOG,
                              formatter_class=parser.formatter_class)
    install.add_argument("clients", nargs="*", metavar="CLIENT",
                         help="the clients to set up, by name (see `mobster mcp clients`)")
    install.add_argument("--all", action="store_true", help="every client found on this Mac")
    install.add_argument("--project", type=path_type, metavar="DIR",
                         help="write the project's config in DIR (such as .cursor/mcp.json) instead of yours")
    install.add_argument("--allow-device", action="append", metavar="NAME", dest="allow_devices",
                         default=argparse.SUPPRESS, help="the server drives only this real iPhone; repeatable")
    install.add_argument("--env-file", type=path_type, metavar="PATH", default=argparse.SUPPRESS,
                         help="the server loads this file (OPENAI_API_KEY or ANTHROPIC_API_KEY for Smart); it must "
                              "be readable by you only (chmod 600)")
    install.add_argument("--with-skill", action="store_true",
                         help="also copy the mobster skill into each client's skills folder")
    install.add_argument("--dry-run", action="store_true", help="show what would change and write nothing")
    install.add_argument("--remove", action="store_true", help="take Mobster out of the clients' configs instead")
    install.add_argument("--json", action="store_true", help="print the result as JSON")

    listing = subs.add_parser("clients", formatter_class=parser.formatter_class, help="list the agent clients found here and whether Mobster is set up",
                              description="List the agent clients Mobster can set up, whether each is installed "
                                          "on this Mac, and whether its config runs Mobster's server.")
    listing.add_argument("--project", type=path_type, metavar="DIR", help="look at the project's configs in DIR")
    listing.add_argument("--json", action="store_true", help="print the list as JSON")

    doctor = subs.add_parser("doctor", formatter_class=parser.formatter_class, help="start the server as a client would, list its tools and call status",
                             description="Start Mobster's MCP server the way an app opened from the Dock would "
                                         "(its absolute path, launchd's PATH, the home folder), send initialize, "
                                         "list the tools and call status, with the time each step took. Then "
                                         "check that every client's entry runs a mobster that exists.")
    doctor.add_argument("--env-file", type=path_type, metavar="PATH", default=argparse.SUPPRESS,
                        help="start the server with this env file, as install would")
    doctor.add_argument("--json", action="store_true", help="print the report as JSON")
    return subs


def run(args):
    command = getattr(args, "mcp_command", None)
    try:
        if command == "install":
            return install(args)
        if command == "clients":
            return list_clients(args)
        if command == "doctor":
            return doctor(args)
    except KeyboardInterrupt:
        return 130
    print(f"mobster mcp: unknown subcommand {command}", file=sys.stderr)
    return USAGE_EXIT


# -------------------------------------------------------------------------------------------- install

def server_args(args):
    """The flags the clients' entries pass to `mobster mcp`."""
    extra = []
    env_file = getattr(args, "env_file", None)
    if env_file:
        extra += ["--env-file", str(Path(env_file).expanduser().absolute())]
    for name in getattr(args, "allow_devices", None) or []:
        extra += ["--allow-device", name]
    return extra


def check_env_file(path):
    """None when ``path`` is a file only its owner can read, else the sentence that says what to fix."""
    path = Path(path).expanduser()
    try:
        info = path.stat()
    except FileNotFoundError:
        return f"{path} doesn't exist. Create it with one line, OPENAI_API_KEY=… or ANTHROPIC_API_KEY=…, then " \
               f"run `chmod 600 {path}`"
    except OSError as error:
        return f"{path} can't be read ({error.strerror})"
    if not path.is_file():
        return f"{path} isn't a file"
    if info.st_mode & 0o077:
        return f"{path} can be read by other users (mode {info.st_mode & 0o777:o}). Run `chmod 600 {path}`, " \
               "then try again"
    return None


def select(args, env, out):
    """The clients to work on, or an exit code after printing why."""
    if args.clients and args.all:
        out.error("name clients or use --all, not both")
        return USAGE_EXIT
    if args.all:
        found = [client for client in registry.CLIENTS if client.detected(env)[0]]
        if args.project:
            found = [client for client in found if client.paths(env, args.project)]
        if not found:
            out.error("no agent clients were found on this Mac. Name one to set it up anyway: "
                      "mobster mcp install cursor")
            return FAILED_EXIT
        return found
    if not args.clients:
        found = [client.name for client in registry.CLIENTS if client.detected(env)[0]]
        out.error("name the clients to set up, or use --all, such as `mobster mcp install claude-code cursor`."
                  + (f" Found on this Mac: {', '.join(found)}." if found else ""))
        return USAGE_EXIT
    chosen, unknown = [], []
    for name in args.clients:
        client = registry.lookup(name)
        if client is None:
            hint = registry.suggestion(name)
            unknown.append(f"{name!r}" + (f" (did you mean {hint}?)" if hint else ""))
        elif client not in chosen:
            chosen.append(client)
    if unknown:
        out.error(f"unknown client {', '.join(unknown)}. Known clients: "
                  + ", ".join(client.id for client in registry.CLIENTS))
        return USAGE_EXIT
    return chosen


def install(args, env=None, server=None, stream=None):
    env = env or registry.Env.current()
    out = Output(args, stream)
    if getattr(args, "env_file", None) and not args.remove:
        problem = check_env_file(args.env_file)
        if problem:
            out.error(problem)
            return USAGE_EXIT
    for name in getattr(args, "allow_devices", None) or []:
        if not name.strip():
            out.error("--allow-device needs a device's id, UDID or name")
            return USAGE_EXIT
    chosen = select(args, env, out)
    if isinstance(chosen, int):
        return chosen
    project = Path(args.project).expanduser().absolute() if args.project else None
    if project and not project.is_dir():
        out.error(f"{project} isn't a folder")
        return USAGE_EXIT
    server = server or registry.server_for(server_args(args), env)
    changes, skills, failed = [], [], False
    for client in chosen:
        found, why = client.detected(env)
        if not found and not project and not args.remove:
            changes.append(registry.Change(client.id, "skipped", message=f"{client.name} isn't installed here "
                                                                         f"({why}). Install it first"))
            failed = True
            continue
        if args.remove:
            changes += client.remove(env, project, args.dry_run)
        else:
            changes += client.install(env, server, project, args.dry_run)
    if args.with_skill:
        done = set()
        for client in chosen:
            folder = client.skills_dir(env, project)
            if folder is None:
                skills.append({"client": client.id, "action": "skipped", "path": "",
                               "message": f"{client.name} has no skills folder"})
                continue
            if folder in done or not any(c.client == client.id and c.action not in ("skipped", "error")
                                         for c in changes):
                continue
            done.add(folder)
            action, path, message = (skill.remove if args.remove else skill.install)(folder, args.dry_run)
            sharing = [c.name for c in chosen if c.skills_dir(env, project) == folder]
            skills.append({"client": client.id, "clients": sharing, "action": action, "path": path,
                           "message": message})
    failed = failed or any(change.action == "error" for change in changes) or \
        any(item["action"] == "error" for item in skills)
    out.install_report(server, changes, skills, args, chosen)
    return FAILED_EXIT if failed else 0


# -------------------------------------------------------------------------------------------- clients

def list_clients(args, env=None, stream=None):
    env = env or registry.Env.current()
    out = Output(args, stream)
    project = Path(args.project).expanduser().absolute() if getattr(args, "project", None) else None
    rows = []
    for client in registry.CLIENTS:
        found, why = client.detected(env)
        row = {"id": client.id, "name": client.name, "detected": found, "found": why, "docs": client.docs}
        row.update(client.status(env, project))
        folder = client.skills_dir(env, project)
        row["skills_dir"] = str(folder) if folder else None
        row["skill"] = bool(folder) and skill.ours(folder / skill.NAME)
        rows.append(row)
    out.clients_report(rows)
    return 0


# -------------------------------------------------------------------------------------------- doctor

def doctor(args, env=None, server=None, stream=None):
    from . import doctor as probe
    env = env or registry.Env.current()
    out = Output(args, stream)
    if getattr(args, "env_file", None):
        problem = check_env_file(args.env_file)
        if problem:
            out.error(problem)
            return USAGE_EXIT
    server = server or registry.server_for(server_args(args), env)
    report = probe.probe(server, env.environ, cwd=str(env.home))
    problems = []
    for client in registry.CLIENTS:
        if not client.detected(env)[0]:
            continue
        state = client.status(env)
        if state.get("error"):
            problems.append({"client": client.id, "message": state["error"]})
        elif state.get("configured") and not state.get("command_ok"):
            problems.append({"client": client.id, "message":
                             f"{client.name} runs {state.get('command') or 'a command'} that doesn't exist. Run "
                             f"`mobster mcp install {client.id}`"})
        report.setdefault("clients", []).append({"id": client.id, "name": client.name, **state})
    report["problems"] = problems
    out.doctor_report(report)
    return 0 if report["ok"] and not problems else FAILED_EXIT


# -------------------------------------------------------------------------------------------- output

class Output:
    def __init__(self, args, stream=None):
        self.json = bool(getattr(args, "json", False))
        self.stream = stream or sys.stdout

    def print(self, text=""):
        print(text, file=self.stream, flush=True)

    def error(self, message):
        if self.json:
            self.print(json.dumps({"ok": False, "error": message}))
        else:
            print(f"mobster mcp: {message}", file=sys.stderr, flush=True)

    def install_report(self, server, changes, skills, args, chosen):
        ok = not any(c.action == "error" for c in changes)
        if self.json:
            self.print(json.dumps({"ok": ok, "dry_run": bool(args.dry_run), "remove": bool(args.remove),
                                   "server": {"command": server.command, "args": server.args},
                                   "changes": [c.json() for c in changes], "skills": skills}, indent=2))
            return
        names = {client.id: client.name for client in registry.CLIENTS}
        if not args.remove:
            self.print("Mobster's MCP server: " + " ".join(_quote(part) for part in server.argv()))
        for change in changes:
            where = registry._home(change.path) if change.path else ""
            how = f" (with `{Path(change.command[0]).name} mcp {change.command[2]}`)" \
                if change.method == "cli" and change.command else ""
            line = f"  {names[change.client]:<16}{change.action:<10} {where}{how}"
            self.print(line.rstrip())
            if change.message:
                self.print(f"    {change.message}")
            if args.dry_run and change.diff:
                for diff_line in change.diff.rstrip("\n").splitlines():
                    self.print("    " + diff_line)
            if args.dry_run and change.method == "cli" and change.command:
                self.print("    would run: " + " ".join(_quote(part) for part in change.command))
        for item in skills:
            who = _join(item.get("clients") or [names.get(item["client"], item["client"])])
            where = registry._home(item["path"]) + "  " if item["path"] else ""
            self.print(f"  {'Skill':<16}{item['action']:<10} {where}({who})")
            if item.get("message"):
                self.print(f"    {item['message']}")
        if args.dry_run:
            self.print("Nothing was written (--dry-run).")
            return
        touched = [c for c in changes if c.action in ("added", "updated", "removed")]
        restart = sorted({names[c.client] for c in touched if c.client in RESTART})
        if restart:
            self.print(f"Restart {_join(restart)} to load the change.")
        if touched and not args.remove:
            self.print("New sessions of the other clients pick it up. Check it with `mobster mcp doctor`.")

    def clients_report(self, rows):
        if self.json:
            self.print(json.dumps({"clients": rows}, indent=2))
            return
        import shutil
        from ..style import palette
        paint = palette(self.stream)

        def state(row):
            if row.get("error"):
                return "error"
            if row.get("configured"):
                return "added" if row.get("command_ok") else "broken"
            return "no"
        found = sum(1 for row in rows if row["detected"])
        added = sum(1 for row in rows if state(row) == "added")
        # The clients on this Mac first, then the rest, each in the registry's order.
        rows = sorted(rows, key=lambda row: not row["detected"])
        # A column of "no" says nothing: with Mobster in no client, the summary line says it instead.
        show_added = any(state(row) != "no" for row in rows)
        self.print(f"{len(rows)} MCP clients · {found} on this Mac · Mobster added to {added}")
        self.print()
        words = {"yes": ("✓ yes", "green"), "no": ("no", "tertiary"), "added": ("✓ added", "green"),
                 "broken": ("broken", "amber"), "error": ("error", "coral")}
        name_width = max(len("Client"), *(len(row["name"]) for row in rows)) + 3
        columns = [("Client", name_width), ("On this Mac", 14)] + ([("Added", 10)] if show_added else [])
        self.print("  " + "".join(paint(title, "tertiary", "underline") + " " * (width - len(title))
                                  for title, width in columns) + paint("Config", "tertiary", "underline"))
        room = max(24, shutil.get_terminal_size((100, 24)).columns - 2 - sum(width for _, width in columns))
        for row in rows:
            dim = not row["detected"]
            name = paint(row["name"].ljust(name_width), "tertiary" if dim else "text")
            mac, mac_role = words["yes" if row["detected"] else "no"]
            line = "  " + name + paint(mac, mac_role) + " " * (14 - len(mac))
            if show_added:
                text, role = words[state(row)]
                line += paint(text, role) + " " * (10 - len(text))
            self.print((line + paint(_middle(registry._home(row.get("path", "")), room), "tertiary")).rstrip())
        self.print()
        ready = sum(1 for row in rows if row["detected"] and state(row) == "added")
        if found and ready < found:
            missing = found - ready
            self.print(f"Add Mobster to the {missing} client{'s' if missing != 1 else ''} on this Mac: "
                       + paint("mobster mcp install --all", "accent"))
        elif found:
            self.print("Every client found here has Mobster. Check one: " + paint("mobster mcp doctor", "accent"))
        else:
            self.print("No MCP client found here. Install one, then: " + paint("mobster mcp install", "accent"))
        if any(state(row) == "broken" for row in rows):
            self.print(paint("broken: the entry runs a mobster that no longer exists; install again to fix it.",
                             "tertiary"))

    def doctor_report(self, report):
        if self.json:
            self.print(json.dumps(report, indent=2))
            return
        self.print("Server: " + " ".join(_quote(part) for part in report["command"]))
        self.print(f"  started with launchd's PATH ({'/usr/bin:/bin:/usr/sbin:/sbin'}), as an app from the Dock "
                   "would start it")
        for step in report["steps"]:
            self.print(f"  {step['step']:<20}{step['ms']:>6} ms")
        if report.get("tools"):
            self.print(f"  {len(report['tools'])} tools: {', '.join(report['tools'])}")
        if report.get("status"):
            for line in report["status"].splitlines()[:6]:
                self.print(f"  status: {line}")
        if report["ok"]:
            self.print(f"OK in {report['total_ms']} ms.")
        else:
            self.print(f"Failed: {report.get('error')}")
            for line in report.get("stderr") or []:
                self.print(f"  server: {line}")
        for client in report.get("clients") or []:
            state = "set up" if client.get("configured") else "not set up"
            self.print(f"  {client['name']:<16}{state:<11}{registry._home(client.get('path', ''))}".rstrip())
        for problem in report.get("problems") or []:
            self.print(f"Problem: {problem['message']}")


def _quote(part):
    import shlex
    return shlex.quote(str(part))


def _middle(path, width):
    """``path`` shortened in the middle with … when it is wider than ``width``: the file name stays whole."""
    if len(path) <= width:
        return path
    keep = max(1, width - 1)
    tail = keep * 2 // 3
    return path[:keep - tail] + "…" + path[-tail:]


def _join(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
