"""The agent harnesses `mobster mcp install` knows: where each keeps its MCP servers, and the entry it needs.

Every format below was checked against the client's own documentation on 7 Oct 2026; each class names the page.
Paths are built from ``Env`` (HOME and the variables that move each config), never from the real home folder
directly, so tests and dry runs can point every client at a temporary folder.

The entry always names ``mobster`` by its absolute path: apps opened from the Dock or Spotlight (Claude Desktop,
Cursor, VS Code, Zed) start servers with launchd's PATH, not your shell's, so a bare ``mobster`` that works in a
terminal fails there. No entry ever holds a key: Smart's key reaches the server through ``--env-file``.
"""

from dataclasses import dataclass, field
import difflib
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import jsonc, textedit

SERVER = "mobster"
GOOSE_TIMEOUT_S = 300
CLI_TIMEOUT_S = 60


@dataclass
class Env:
    """Where to look: the home folder, the environment and how to find a command. ``Env.current()`` is this
    process's; tests build their own."""

    home: Path
    environ: dict
    which: object = None
    applications: tuple = ()

    @classmethod
    def current(cls):
        home = Path(os.environ.get("HOME") or Path.home())
        return cls(home=home, environ=dict(os.environ), which=shutil.which,
                   applications=(Path("/Applications"), home / "Applications"))

    def find(self, name):
        return self.which(name, path=self.environ.get("PATH")) if self.which else None

    def var(self, name):
        value = self.environ.get(name, "")
        return Path(value).expanduser() if value else None

    def xdg_config(self):
        return self.var("XDG_CONFIG_HOME") or self.home / ".config"

    def support(self, *parts):
        """~/Library/Application Support/..."""
        return self.home.joinpath("Library", "Application Support", *parts)

    def app(self, *names):
        return any((folder / f"{name}.app").exists() for folder in self.applications for name in names)


@dataclass
class Server:
    """What the client runs: ``command`` (absolute) with ``args``, and ``env`` (never a key)."""

    command: str
    args: list
    env: dict = field(default_factory=dict)

    def argv(self):
        return [self.command, *self.args]


@dataclass
class Change:
    """What install or remove did (or, with --dry-run, would do) to one config."""

    client: str
    action: str  # added, updated, unchanged, removed, absent, skipped, error
    path: str = ""
    method: str = "file"  # file or cli
    command: list = None
    diff: str = ""
    message: str = ""

    def json(self):
        out = {"client": self.client, "action": self.action, "path": self.path, "method": self.method}
        if self.command:
            out["command"] = self.command
        if self.message:
            out["message"] = self.message
        return out


# ----------------------------------------------------------------------------------------------- the server

def server_for(args=(), env=None):
    """The server entry for this Mobster: its absolute path, and ``mcp`` plus ``args``.

    A frozen build (the curl install, Homebrew, the Mac app) names itself, by the link on PATH when that link
    points at this very binary, so an upgrade that moves the versioned folder keeps working. A pip install
    names the console script beside its Python. A source checkout without one runs ``python -m mobile_agent``
    with PYTHONPATH set to the checkout."""
    env = env or Env.current()
    mcp_args = ["mcp", *args]
    if getattr(sys, "frozen", False):
        exe = os.path.realpath(sys.executable)
        candidates = [env.find("mobster"), env.home / ".local/bin/mobster", "/opt/homebrew/bin/mobster",
                      "/usr/local/bin/mobster"]
        for candidate in candidates:
            if candidate and Path(candidate).exists() and os.path.realpath(candidate) == exe:
                return Server(str(Path(candidate).absolute()), mcp_args)
        return Server(exe, mcp_args)
    script = Path(sys.executable).parent / "mobster"
    if script.is_file() and os.access(script, os.X_OK):
        return Server(str(script), mcp_args)
    found = env.find("mobster")
    if found:
        return Server(str(Path(found).absolute()), mcp_args)
    root = Path(__file__).resolve().parents[2]
    return Server(sys.executable, ["-m", "mobile_agent", *mcp_args], {"PYTHONPATH": str(root)})


def same(existing, wanted):
    """Whether a client's entry already runs ``wanted``: the same command, arguments and environment."""
    if not isinstance(existing, dict):
        return False
    keys = set(wanted) | {k for k in existing if k in ("command", "args", "env", "cmd", "envs", "environment")}
    return all(_norm(existing.get(k)) == _norm(wanted.get(k)) for k in keys)


def _norm(value):
    return None if value in (None, {}, []) else value


# -------------------------------------------------------------------------------------------- client base

class Client:
    """One harness. Subclasses give the paths and the entry; the base edits JSON or JSONC."""

    id = ""
    name = ""
    aliases = ()
    docs = ""
    container = ("mcpServers",)
    cli_replaces = False  # whether the client's own add command replaces an existing entry

    def detected(self, env):
        """(True, what was found) or (False, what was looked for)."""
        raise NotImplementedError

    def paths(self, env, project=None):
        """The config files to write, user-level or for ``project``. [] when the client has no such file."""
        raise NotImplementedError

    def entry(self, server):
        entry = {"command": server.command, "args": list(server.args)}
        if server.env:
            entry["env"] = dict(server.env)
        return entry

    def skills_dir(self, env, project=None):
        """Where the client reads Agent Skills (SKILL.md folders), or None."""
        if project:
            return Path(project) / ".agents" / "skills"
        return env.home / ".agents" / "skills"

    # The JSON edit. Codex (TOML) and Goose (YAML) override these three.
    def read(self, path):
        text = _read(path)
        return None if text is None else jsonc.get_member(text, list(self.container), SERVER)

    def edit(self, text, server):
        return jsonc.set_member(text or "", list(self.container), SERVER, self.entry(server))

    def unedit(self, text):
        return jsonc.remove_member(text, list(self.container), SERVER)

    # Install and remove, through the client's command where it has one (``via_cli``), else the file.
    def via_cli(self, env, project):
        return None

    def install(self, env, server, project=None, dry_run=False):
        paths = self.paths(env, project)
        if not paths:
            return [Change(self.id, "skipped", message=f"{self.name} has no project-level config")]
        changes = []
        for path in paths:
            current = self._safe_read(path)
            if isinstance(current, Change):
                changes.append(current)
                continue
            if current is not None and same(current, self.entry(server)):
                changes.append(Change(self.id, "unchanged", str(path)))
                continue
            action = "updated" if current is not None else "added"
            argv = self.via_cli(env, project)
            if argv:
                replace = current is not None and not self.cli_replaces
                changes.append(self._run_cli(env, path, action, argv("add", server), dry_run,
                                             remove_first=argv("remove", server) if replace else None))
            else:
                changes.append(self._write(path, action, lambda text: self.edit(text, server), dry_run))
        return changes

    def remove(self, env, project=None, dry_run=False):
        paths = self.paths(env, project)
        if not paths:
            return [Change(self.id, "skipped", message=f"{self.name} has no project-level config")]
        changes = []
        for path in paths:
            current = self._safe_read(path)
            if isinstance(current, Change):
                changes.append(current)
                continue
            if current is None:
                changes.append(Change(self.id, "absent", str(path)))
                continue
            argv = self.via_cli(env, project)
            if argv:
                changes.append(self._run_cli(env, path, "removed", argv("remove", None), dry_run))
            else:
                changes.append(self._write(path, "removed", self.unedit, dry_run))
        return changes

    def status(self, env, project=None):
        """{configured, path, command, command_ok} for `mobster mcp clients`."""
        for path in self.paths(env, project):
            current = self._safe_read(path)
            if isinstance(current, dict):
                command = self.command_of(current)
                return {"configured": True, "path": str(path), "command": command,
                        "command_ok": bool(command) and Path(command).is_file() and os.access(command, os.X_OK)}
            if isinstance(current, Change):
                return {"configured": False, "path": str(path), "error": current.message}
        paths = self.paths(env, project)
        return {"configured": False, "path": str(paths[0]) if paths else ""}

    def command_of(self, entry):
        return entry.get("command") if isinstance(entry.get("command"), str) else None

    # Helpers.
    def _safe_read(self, path):
        try:
            return self.read(path)
        except (jsonc.JsoncError, textedit.EditError) as error:
            return Change(self.id, "error", str(path), message=f"{_home(path)} can't be read: {error}. Fix it, "
                                                                "or add Mobster by hand")
        except OSError as error:
            return Change(self.id, "error", str(path), message=f"{_home(path)} can't be read ({error.strerror})")

    def _write(self, path, action, transform, dry_run):
        try:
            before = _read(path)
            after = transform(before)
        except (jsonc.JsoncError, textedit.EditError) as error:
            return Change(self.id, "error", str(path), message=f"{_home(path)}: {error}. Nothing was written")
        except OSError as error:
            return Change(self.id, "error", str(path), message=f"{_home(path)} can't be read ({error.strerror})")
        if after == (before or ""):
            return Change(self.id, "unchanged" if action != "removed" else "absent", str(path))
        diff = "".join(difflib.unified_diff((before or "").splitlines(True), after.splitlines(True),
                                            str(path), str(path)))
        if not dry_run:
            try:
                write_atomic(path, after)
            except OSError as error:
                return Change(self.id, "error", str(path), message=f"{_home(path)} can't be written "
                                                                    f"({error.strerror}). Nothing was changed")
        return Change(self.id, action, str(path), diff=diff)

    def _run_cli(self, env, path, action, argv, dry_run, remove_first=None):
        commands = [remove_first, argv] if remove_first else [argv]
        if dry_run:
            first = f"`{' '.join(remove_first[1:])}` first, then the command below" if remove_first else ""
            return Change(self.id, action, str(path), "cli", command=argv, message=first and f"would run {first}")
        backup(path)
        for command in commands:
            try:
                done = subprocess.run(command, env=env.environ, capture_output=True, text=True,
                                      timeout=CLI_TIMEOUT_S, stdin=subprocess.DEVNULL, cwd=str(env.home))
            except (OSError, subprocess.TimeoutExpired) as error:
                return Change(self.id, "error", str(path), "cli", command, message=f"`{command[0]}` didn't run "
                                                                                   f"({error})")
            if done.returncode != 0:
                said = (done.stderr or done.stdout).strip().splitlines()
                return Change(self.id, "error", str(path), "cli", command,
                              message=f"`{' '.join(command[:4])} …` exited {done.returncode}"
                                      + (f": {said[-1]}" if said else ""))
        return Change(self.id, action, str(path), "cli", command=argv)


# -------------------------------------------------------------------------------------------- the clients

class ClaudeCode(Client):
    # https://code.claude.com/docs/en/mcp (read 7 Oct 2026): `claude mcp add --scope user <name> -- <command>
    # [args]` stores the server in ~/.claude.json's top-level "mcpServers" ($CLAUDE_CONFIG_DIR/.claude.json when
    # that is set, from Claude Code 2.1.290's own code); project scope is .mcp.json at the project's root.
    # Claude Code rewrites ~/.claude.json all the time, so its own command edits it whenever it's installed.
    id, name, aliases = "claude-code", "Claude Code", ("claude", "claudecode", "claude_code")
    docs = "https://code.claude.com/docs/en/mcp"

    def config_dir(self, env):
        return env.var("CLAUDE_CONFIG_DIR")

    def detected(self, env):
        if env.find("claude"):
            return True, "the claude command"
        folder = self.config_dir(env) or env.home / ".claude"
        if folder.is_dir() or (env.home / ".claude.json").is_file():
            return True, _home(folder)
        return False, "no claude command or ~/.claude"

    def paths(self, env, project=None):
        if project:
            return [Path(project) / ".mcp.json"]
        return [(self.config_dir(env) or env.home) / ".claude.json"]

    def entry(self, server):
        # What `claude mcp add` writes, so a file edit and the command agree.
        return {"type": "stdio", **super().entry(server), "env": dict(server.env)}

    def via_cli(self, env, project):
        command = env.find("claude")
        if project or not command:
            return None

        def argv(verb, server):
            if verb == "remove":
                return [command, "mcp", "remove", "--scope", "user", SERVER]
            extra = [item for key, value in server.env.items() for item in ("-e", f"{key}={value}")]
            return [command, "mcp", "add", "--scope", "user", *extra, SERVER, "--", server.command, *server.args]
        return argv

    def skills_dir(self, env, project=None):
        # https://code.claude.com/docs/en/skills: ~/.claude/skills/<name>/SKILL.md, .claude/skills in a project.
        if project:
            return Path(project) / ".claude" / "skills"
        return (self.config_dir(env) or env.home / ".claude") / "skills"


class Codex(Client):
    # https://learn.chatgpt.com/docs/extend/mcp?surface=cli (read 7 Oct 2026): [mcp_servers.<name>] with command
    # and args in $CODEX_HOME/config.toml (default ~/.codex), or .codex/config.toml in a trusted project;
    # `codex mcp add <name> -- <command> [args]` writes the same table. Skills: $HOME/.agents/skills
    # (https://learn.chatgpt.com/docs/build-skills).
    id, name, aliases = "codex", "Codex", ("codex-cli", "openai-codex")
    docs = "https://learn.chatgpt.com/docs/extend/mcp?surface=cli"
    cli_replaces = True

    def home(self, env):
        return env.var("CODEX_HOME") or env.home / ".codex"

    def detected(self, env):
        if env.find("codex"):
            return True, "the codex command"
        if self.home(env).is_dir():
            return True, _home(self.home(env))
        return False, "no codex command or ~/.codex"

    def paths(self, env, project=None):
        if project:
            return [Path(project) / ".codex" / "config.toml"]
        return [self.home(env) / "config.toml"]

    def via_cli(self, env, project):
        command = env.find("codex")
        if project or not command:
            return None

        def argv(verb, server):
            if verb == "remove":
                return [command, "mcp", "remove", SERVER]
            extra = [item for key, value in server.env.items() for item in ("--env", f"{key}={value}")]
            # `codex mcp add` replaces an existing entry itself.
            return [command, "mcp", "add", SERVER, *extra, "--", server.command, *server.args]
        return argv

    def read(self, path):
        text = _read(path)
        if text is None:
            return None
        servers = textedit.toml_loads(text).get("mcp_servers")
        return servers.get(SERVER) if isinstance(servers, dict) else None

    def edit(self, text, server):
        return textedit.toml_set_table(text or "", ("mcp_servers", SERVER), self.entry(server))

    def unedit(self, text):
        return textedit.toml_remove_table(text, ("mcp_servers", SERVER))


class Cursor(Client):
    # https://cursor.com/docs/context/mcp (read 7 Oct 2026): ~/.cursor/mcp.json for every project,
    # .cursor/mcp.json for one, {"mcpServers": {name: {command, args, env}}}. Its field table marks
    # "type": "stdio" required while its examples leave it out, so the entry has it.
    id, name, aliases = "cursor", "Cursor", ()
    docs = "https://cursor.com/docs/context/mcp"

    def detected(self, env):
        if env.app("Cursor"):
            return True, "Cursor.app"
        if (env.home / ".cursor").is_dir():
            return True, "~/.cursor"
        return False, "no Cursor.app or ~/.cursor"

    def paths(self, env, project=None):
        return [(Path(project) if project else env.home) / ".cursor" / "mcp.json"]

    def entry(self, server):
        return {"type": "stdio", **super().entry(server)}


class VSCode(Client):
    # https://code.visualstudio.com/docs/copilot/customization/mcp-servers (read 7 Oct 2026): the user profile's
    # mcp.json (~/Library/Application Support/Code/User/mcp.json for the default profile) or .vscode/mcp.json,
    # {"servers": {name: {"type": "stdio", command, args, env}}}. The file is JSONC: comments are kept.
    id, name, aliases = "vscode", "VS Code", ("code", "vs-code", "visual-studio-code", "copilot")
    docs = "https://code.visualstudio.com/docs/copilot/customization/mcp-servers"
    container = ("servers",)

    def detected(self, env):
        if env.app("Visual Studio Code"):
            return True, "Visual Studio Code.app"
        if env.support("Code", "User").is_dir():
            return True, "~/Library/Application Support/Code"
        return False, "no Visual Studio Code.app"

    def paths(self, env, project=None):
        if project:
            return [Path(project) / ".vscode" / "mcp.json"]
        return [env.support("Code", "User", "mcp.json")]

    def entry(self, server):
        return {"type": "stdio", **super().entry(server)}

    def skills_dir(self, env, project=None):
        # VS Code reads ~/.agents/skills, ~/.copilot/skills and ~/.claude/skills (Agent Skills docs, 7 Oct 2026).
        return super().skills_dir(env, project)


class ClaudeDesktop(Client):
    # https://modelcontextprotocol.io/docs/develop/connect-local-servers (read 7 Oct 2026):
    # ~/Library/Application Support/Claude/claude_desktop_config.json, {"mcpServers": {name: {command, args,
    # env}}}, with absolute paths; restart the app after editing. It has no skills folder.
    id, name, aliases = "claude-desktop", "Claude Desktop", ("claude-app", "claudedesktop", "desktop")
    docs = "https://modelcontextprotocol.io/docs/develop/connect-local-servers"

    def detected(self, env):
        if env.app("Claude"):
            return True, "Claude.app"
        if env.support("Claude").is_dir():
            return True, "~/Library/Application Support/Claude"
        return False, "no Claude.app"

    def paths(self, env, project=None):
        return [] if project else [env.support("Claude", "claude_desktop_config.json")]

    def skills_dir(self, env, project=None):
        return None


class OpenCode(Client):
    # https://opencode.ai/docs/mcp-servers/ and https://opencode.ai/docs/config/ (read 7 Oct 2026): "mcp":
    # {name: {"type": "local", "command": [command, ...args], "enabled": true, "environment": {}}} in
    # $XDG_CONFIG_HOME/opencode/opencode.json or opencode.jsonc (JSONC), or opencode.json in a project.
    # Skills: ~/.config/opencode/skills, ~/.claude/skills and ~/.agents/skills.
    id, name, aliases = "opencode", "opencode", ("open-code",)
    docs = "https://opencode.ai/docs/mcp-servers/"
    container = ("mcp",)

    def folder(self, env):
        return env.xdg_config() / "opencode"

    def detected(self, env):
        if env.find("opencode"):
            return True, "the opencode command"
        if self.folder(env).is_dir():
            return True, _home(self.folder(env))
        return False, "no opencode command or ~/.config/opencode"

    def paths(self, env, project=None):
        folder = Path(project) if project else self.folder(env)
        for name in ("opencode.jsonc", "opencode.json"):
            if (folder / name).is_file():
                return [folder / name]
        return [folder / "opencode.json"]

    def entry(self, server):
        entry = {"type": "local", "command": [server.command, *server.args], "enabled": True}
        if server.env:
            entry["environment"] = dict(server.env)
        return entry

    def command_of(self, entry):
        command = entry.get("command")
        return command[0] if isinstance(command, list) and command and isinstance(command[0], str) else None


class Gemini(Client):
    # https://geminicli.com/docs/tools/mcp-server/ (read 7 Oct 2026): "mcpServers": {name: {command, args, env}}
    # in ~/.gemini/settings.json ($GEMINI_CLI_HOME/.gemini when set) or .gemini/settings.json in a project.
    # Skills: ~/.gemini/skills or ~/.agents/skills.
    id, name, aliases = "gemini", "Gemini CLI", ("gemini-cli", "google-gemini")
    docs = "https://geminicli.com/docs/tools/mcp-server/"

    def folder(self, env):
        return (env.var("GEMINI_CLI_HOME") or env.home) / ".gemini"

    def detected(self, env):
        if env.find("gemini"):
            return True, "the gemini command"
        if self.folder(env).is_dir():
            return True, _home(self.folder(env))
        return False, "no gemini command or ~/.gemini"

    def paths(self, env, project=None):
        return [(Path(project) / ".gemini" if project else self.folder(env)) / "settings.json"]


class Windsurf(Client):
    # https://docs.devin.ai/desktop/cascade/mcp (read 7 Oct 2026; Windsurf became Devin Desktop on 2 Jun 2026):
    # {"mcpServers": {name: {command, args, env}}}. The docs name ~/.config/devin/mcp_config.json, the FAQ
    # keeps ~/.codeium/windsurf/mcp_config.json, so both are written when their folders exist.
    # Skills: ~/.codeium/windsurf/skills.
    id, name, aliases = "windsurf", "Windsurf", ("devin", "devin-desktop", "codeium")
    docs = "https://docs.devin.ai/desktop/cascade/mcp"

    def detected(self, env):
        if env.app("Windsurf", "Devin"):
            return True, "Windsurf.app"
        for folder in (env.home / ".codeium" / "windsurf", env.xdg_config() / "devin"):
            if folder.is_dir():
                return True, _home(folder)
        return False, "no Windsurf.app or ~/.codeium/windsurf"

    def paths(self, env, project=None):
        if project:
            return []
        paths = [env.home / ".codeium" / "windsurf" / "mcp_config.json"]
        if (env.xdg_config() / "devin").is_dir():
            paths.append(env.xdg_config() / "devin" / "mcp_config.json")
        return paths

    def skills_dir(self, env, project=None):
        if project:
            return Path(project) / ".windsurf" / "skills"
        return env.home / ".codeium" / "windsurf" / "skills"


class Zed(Client):
    # https://zed.dev/docs/ai/mcp (read 7 Oct 2026): "context_servers": {name: {command, args, env}} in
    # ~/.config/zed/settings.json, JSON with comments, or .zed/settings.json in a project. Skills:
    # ~/.agents/skills. (/usr/local/zfs/bin/zed is the ZFS event daemon, so the zed command proves nothing.)
    id, name, aliases = "zed", "Zed", ("zed-editor",)
    docs = "https://zed.dev/docs/ai/mcp"
    container = ("context_servers",)

    def detected(self, env):
        if env.app("Zed", "Zed Preview"):
            return True, "Zed.app"
        if (env.home / ".config" / "zed").is_dir():
            return True, "~/.config/zed"
        return False, "no Zed.app or ~/.config/zed"

    def paths(self, env, project=None):
        if project:
            return [Path(project) / ".zed" / "settings.json"]
        return [env.home / ".config" / "zed" / "settings.json"]

    def entry(self, server):
        return {"command": server.command, "args": list(server.args), "env": dict(server.env)}


class Goose(Client):
    # https://goose-docs.ai/docs/guides/config-files (read 7 Oct 2026): ~/.config/goose/config.yaml
    # ($GOOSE_PATH_ROOT/config when set), "extensions": {name: {enabled, type: stdio, name, cmd, args, envs,
    # env_keys, timeout}}. Skills: ~/.agents/skills.
    id, name, aliases = "goose", "Goose", ("block-goose",)
    docs = "https://goose-docs.ai/docs/guides/config-files"

    def folder(self, env):
        root = env.var("GOOSE_PATH_ROOT")
        return root / "config" if root else env.xdg_config() / "goose"

    def detected(self, env):
        if env.find("goose"):
            return True, "the goose command"
        if env.app("Goose"):
            return True, "Goose.app"
        if self.folder(env).is_dir():
            return True, _home(self.folder(env))
        return False, "no goose command or ~/.config/goose"

    def paths(self, env, project=None):
        return [] if project else [self.folder(env) / "config.yaml"]

    def entry(self, server):
        return {"enabled": True, "type": "stdio", "name": SERVER,
                "description": "Check iOS screens and drive an iPhone with Mobster",
                "cmd": server.command, "args": list(server.args), "envs": dict(server.env), "env_keys": [],
                "timeout": GOOSE_TIMEOUT_S}

    def command_of(self, entry):
        return entry.get("cmd") if isinstance(entry.get("cmd"), str) else None

    def read(self, path):
        text = _read(path)
        if text is None:
            return None
        extensions = textedit.yaml_loads(text).get("extensions")
        return extensions.get(SERVER) if isinstance(extensions, dict) else None

    def edit(self, text, server):
        return textedit.yaml_set_entry(text or "", "extensions", SERVER, self.entry(server))

    def unedit(self, text):
        return textedit.yaml_remove_entry(text, "extensions", SERVER)


class Amp(Client):
    # https://ampcode.com/docs/markdown/cli/settings and /customize/mcp (read 7 Oct 2026): "amp.mcpServers":
    # {name: {command, args, env}} in ~/.config/amp/settings.json (or settings.jsonc), or .amp/settings.json in a
    # workspace. "amp.mcpServers" is one key with a dot in it. Skills: ~/.config/agents/skills, ~/.agents/skills.
    id, name, aliases = "amp", "Amp", ("ampcode", "sourcegraph-amp")
    docs = "https://ampcode.com/docs/markdown/cli/settings"
    container = ("amp.mcpServers",)

    def folder(self, env):
        return env.xdg_config() / "amp"

    def detected(self, env):
        if env.find("amp"):
            return True, "the amp command"
        if self.folder(env).is_dir():
            return True, _home(self.folder(env))
        return False, "no amp command or ~/.config/amp"

    def paths(self, env, project=None):
        folder = Path(project) / ".amp" if project else self.folder(env)
        for name in ("settings.jsonc", "settings.json"):
            if (folder / name).is_file():
                return [folder / name]
        return [folder / "settings.json"]


class Cline(Client):
    # https://docs.cline.bot/mcp/configuring-mcp-servers (read 7 Oct 2026): ~/.cline/data/settings/
    # cline_mcp_settings.json, which the VS Code extension, the CLI and JetBrains share; {"mcpServers": {name:
    # {command, args, env, disabled, autoApprove}}}. Older extensions read the file in VS Code's globalStorage,
    # which is written too when it exists. Skills: ~/.cline/skills and ~/.agents/skills.
    id, name, aliases = "cline", "Cline", ("cline-cli",)
    docs = "https://docs.cline.bot/mcp/configuring-mcp-servers"
    LEGACY = ("Code", "User", "globalStorage", "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json")

    def folder(self, env):
        return env.var("CLINE_DIR") or env.home / ".cline"

    def detected(self, env):
        if env.find("cline"):
            return True, "the cline command"
        if self.folder(env).is_dir():
            return True, _home(self.folder(env))
        if env.support(*self.LEGACY[:4]).is_dir():
            return True, "Cline in VS Code"
        return False, "no cline command, ~/.cline or Cline extension"

    def paths(self, env, project=None):
        if project:
            return []
        override = env.var("CLINE_MCP_SETTINGS_PATH")
        if override:
            return [override]
        data = env.var("CLINE_DATA_DIR") or self.folder(env) / "data"
        paths = [data / "settings" / "cline_mcp_settings.json"]
        if env.support(*self.LEGACY[:4]).is_dir():
            paths.append(env.support(*self.LEGACY))
        return paths

    def entry(self, server):
        # Never auto-approve: a phone tool acts on someone's real iPhone.
        return {**super().entry(server), "disabled": False, "autoApprove": []}


class Omp(Client):
    # oh-my-pi's docs/mcp-config.md (github.com/can1357/oh-my-pi, read 7 Oct 2026) and omp 18.2.8's own code:
    # {"mcpServers": {name: {"type": "stdio", command, args, env}}} in ~/.omp/agent/mcp.json
    # ($PI_CODING_AGENT_DIR, or $PI_CONFIG_DIR/agent), or .omp/mcp.json in a project. omp reads other clients'
    # user configs only when told to, so it gets its own entry. Skills: ~/.agents/skills (on by default).
    id, name, aliases = "omp", "oh-my-pi (omp)", ("oh-my-pi", "pi")
    docs = "https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-config.md"

    def folder(self, env):
        agent = env.var("PI_CODING_AGENT_DIR")
        if agent:
            return agent
        return (env.var("PI_CONFIG_DIR") or env.home / ".omp") / "agent"

    def detected(self, env):
        if env.find("omp"):
            return True, "the omp command"
        if self.folder(env).is_dir():
            return True, _home(self.folder(env))
        return False, "no omp command or ~/.omp"

    def paths(self, env, project=None):
        return [(Path(project) / ".omp" if project else self.folder(env)) / "mcp.json"]

    def entry(self, server):
        return {"type": "stdio", **super().entry(server)}


CLIENTS = [ClaudeCode(), Codex(), Cursor(), VSCode(), ClaudeDesktop(), OpenCode(), Gemini(), Windsurf(), Zed(),
           Goose(), Amp(), Cline(), Omp()]
BY_ID = {client.id: client for client in CLIENTS}


def lookup(name):
    """The client called ``name`` (its id, an alias or its display name, any case), or None."""
    key = name.strip().lower().replace(" ", "-")
    for client in CLIENTS:
        if key in (client.id, client.name.lower().replace(" ", "-"), *client.aliases):
            return client
    return None


def suggestion(name):
    names = [client.id for client in CLIENTS] + [alias for client in CLIENTS for alias in client.aliases]
    close = difflib.get_close_matches(name.lower(), names, n=1, cutoff=.6)
    return lookup(close[0]).id if close else None


# ------------------------------------------------------------------------------------------- file helpers

def _read(path):
    """The file's text, or None when there is no file."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _home(path):
    """``path`` with the home folder written as ~."""
    text, home = str(path), os.environ.get("HOME", "")
    return "~" + text[len(home):] if home and text.startswith(home + "/") else text


BACKUP_SUFFIX = ".mobster-backup"


def backup(path):
    """Copy ``path`` to ``path`` + ".mobster-backup" (the copy before Mobster's latest change). No file, no copy."""
    path = Path(os.path.realpath(path))
    if path.is_file():
        target = path.with_name(path.name + BACKUP_SUFFIX)
        shutil.copy2(path, target)
        os.chmod(target, path.stat().st_mode & 0o777)


def write_atomic(path, text, keep_backup=True):
    """Write ``text`` to ``path`` in one step: a temporary file beside it, flushed, then renamed over it. A link
    is followed, so a config kept in a dotfiles folder stays a link. The old file is copied to
    ``.mobster-backup`` first (unless ``keep_backup`` is false). The file keeps its mode; a new one is 0600 when
    its folder is private, else 0644."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if keep_backup:
        backup(path)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600 if path.parent.stat().st_mode & 0o077 == 0 else 0o644
    temp = path.with_name(f".{path.name}.mobster-{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
