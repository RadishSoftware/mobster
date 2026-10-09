"""`mobster completion zsh|bash|fish`: tab completion, generated from the command line's own parser.

The script is written from ``build_parser()`` when you ask for it, so it always matches this build's commands and
options, and nothing here costs another command any start-up time. Device names, past task IDs and conversation
IDs complete from `mobster completion --values devices|tasks|threads`, which reads the same lists `mobster devices`
and `mobster history` do.
"""

import argparse
import re
import sys

VALUE_SOURCES = {"device": "devices", "resume": "tasks", "thread": "threads"}
PATH_DESTS = {"env_file", "out", "output", "junit", "html", "app", "check", "state_db", "data_dir"}


def run(args):
    if getattr(args, "values", None):
        return print_values(args.values)
    if not args.shell:
        print("mobster completion: name a shell: zsh, bash or fish. Example: mobster completion zsh",
              file=sys.stderr)
        return 2
    from .__main__ import build_parser
    tree = describe(build_parser())
    text = {"zsh": zsh, "bash": bash, "fish": fish}[args.shell](tree)
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        pass
    return 0


def print_values(kind):
    """One value per line, for the scripts' dynamic completion. Never fails loudly: a shell is waiting."""
    try:
        if kind == "devices":
            from . import devices
            for item in devices.discover(probe=False):
                print(item["name"])
        elif kind == "tasks":
            from .history import read_history
            from .tui.session import default_history_db
            for record in read_history(default_history_db(), 30):
                if record.get("id"):
                    print(record["id"])
        elif kind == "threads":
            import json
            from .threads.cli import _last_file
            try:
                for value in json.loads(_last_file().read_text()).values():
                    print(value)
            except (OSError, ValueError):
                pass
    except Exception:  # noqa: BLE001
        pass
    return 0


def _visible(action):
    return action.help != argparse.SUPPRESS


def describe(parser):
    """{"options": [...], "commands": {name: {"help", "options", "commands", "positionals"}}} from a parser."""
    from .help_topics import visible

    def options(target):
        out = []
        for action in target._actions:
            if not action.option_strings or not _visible(action):
                continue
            takes = action.nargs != 0 and not isinstance(action, (argparse._StoreTrueAction,
                                                                   argparse._StoreFalseAction,
                                                                   argparse._HelpAction, argparse._VersionAction,
                                                                   argparse._CountAction))
            source = VALUE_SOURCES.get(action.dest)
            if takes and action.choices:
                source = "choices:" + " ".join(str(c) for c in action.choices)
            elif takes and source is None and (action.dest in PATH_DESTS or action.type is not None and
                                               getattr(action.type, "__name__", "") == "path_type"):
                source = "files"
            out.append({"flags": list(action.option_strings), "help": action.help or "", "takes": takes,
                        "metavar": (action.metavar if isinstance(action.metavar, str) else None)
                        or action.dest.upper(), "source": source})
        return out

    def node(target, top=False):
        subs = next((a for a in target._actions if isinstance(a, argparse._SubParsersAction)), None)
        commands = {}
        if subs is not None:
            helps = {a.dest: a.help for a in subs._choices_actions}
            for name, child in subs.choices.items():
                if top and not visible(name):
                    continue
                if name not in helps and not top:
                    continue
                entry = node(child)
                entry["help"] = helps.get(name) or ""
                commands[name] = entry
        positionals = [a for a in target._actions if not a.option_strings and _visible(a)
                       and not isinstance(a, argparse._SubParsersAction)]
        choices = []
        for action in positionals:
            if action.choices:
                choices += [str(c) for c in action.choices]
        return {"options": options(target), "commands": commands, "choices": choices}
    tree = node(parser, top=True)
    # The commands in the order `mobster --help` lists them, then any a private build adds.
    from .help_topics import GROUPS, MORE
    order = [name for _, names in GROUPS for name in names] + list(MORE)
    tree["commands"] = dict(sorted(tree["commands"].items(),
                                   key=lambda item: order.index(item[0]) if item[0] in order else len(order)))
    return tree


def _zsh_text(text):
    # Inside zsh's single quotes nothing escapes a quote, so an apostrophe is written as ’.
    return re.sub(r"([\[\]:\\])", r"\\\1", text.replace("`", "").replace("'", "’")).replace("\n", " ")[:120]


def _zsh_option(option):
    flags = option["flags"]
    help_text = _zsh_text(option["help"])
    action = ""
    if option["takes"]:
        source = option["source"]
        if source == "files":
            completer = "_files"
        elif source and source.startswith("choices:"):
            completer = "(" + source.split(":", 1)[1] + ")"
        elif source in ("devices", "tasks", "threads"):
            completer = f"_mobster_values {source}"
        else:
            completer = " "
        action = f":{_zsh_text(option['metavar'].lower())}:{completer}"
    if len(flags) > 1:
        exclusive = " ".join(flags)
        return f"'({exclusive})'{{{','.join(flags)}}}'[{help_text}]{action}'"
    return f"'{flags[0]}[{help_text}]{action}'"


def zsh(tree):
    lines = ["#compdef mobster", "# mobster completion zsh: generated from this build's parser.", "",
             "_mobster_values() {", "  local -a values",
             '  values=("${(@f)$(mobster completion --values $1 2>/dev/null)}")',
             "  compadd -a values", "}", ""]

    def function(name, entry):
        commands = entry["commands"]
        body = [f"{name}() {{", "  local curcontext=\"$curcontext\" state line", "  typeset -A opt_args"]
        specs = [_zsh_option(option) for option in entry["options"]]
        if commands:
            body.append("  local -a commands")
            body.append("  commands=(")
            for sub, child in commands.items():
                body.append(f"    '{sub}:{_zsh_text(child.get('help') or '')}'")
            body.append("  )")
            specs += ["'1: :->command'", "'*:: :->args'"]
        elif entry["choices"]:
            specs.append(f"'1: :({' '.join(entry['choices'])})'")
            specs.append("'*:argument:_default'")
        else:
            specs.append("'*:argument:_default'")
        body.append("  _arguments -C -s \\")
        body += [f"    {spec} \\" for spec in specs[:-1]] + [f"    {specs[-1]}"]
        if commands:
            body.append("  case $state in")
            body.append("    command) _describe -t commands 'mobster command' commands ;;")
            body.append("    args)")
            body.append("      case $line[1] in")
            for sub in commands:
                body.append(f"        {sub}) {name}_{sub.replace('-', '_')} ;;")
            body.append("      esac ;;")
            body.append("  esac")
        body.append("}")
        body.append("")
        out = body
        for sub, child in commands.items():
            out += function(f"{name}_{sub.replace('-', '_')}", child)
        return out
    lines += function("_mobster", tree)
    lines += ['if [ "$funcstack[1]" = "_mobster" ]; then', '  _mobster "$@"', "else",
              "  compdef _mobster mobster", "fi", ""]
    return "\n".join(lines)


def bash(tree):
    lines = ["# mobster completion bash: generated from this build's parser.", "_mobster() {",
             "  local cur prev words cword", "  cur=\"${COMP_WORDS[COMP_CWORD]}\"",
             "  prev=\"${COMP_WORDS[COMP_CWORD-1]}\"",
             "  local command=\"\" sub=\"\"", "  local i",
             "  for ((i = 1; i < COMP_CWORD; i++)); do",
             "    case \"${COMP_WORDS[i]}\" in -*) ;; *) if [ -z \"$command\" ]; then command=\"${COMP_WORDS[i]}\"; "
             "elif [ -z \"$sub\" ]; then sub=\"${COMP_WORDS[i]}\"; fi ;; esac", "  done",
             "  case \"$prev\" in",
             "    --device) COMPREPLY=($(compgen -W \"$(mobster completion --values devices 2>/dev/null)\" -- \"$cur\"));"
             " return ;;",
             "    --resume) COMPREPLY=($(compgen -W \"$(mobster completion --values tasks 2>/dev/null)\" -- \"$cur\"));"
             " return ;;",
             "    --thread) COMPREPLY=($(compgen -W \"$(mobster completion --values threads 2>/dev/null)\" -- \"$cur\"));"
             " return ;;",
             "    --env-file|--out|--output|--junit|--html) COMPREPLY=($(compgen -f -- \"$cur\")); return ;;",
             "  esac", "  local words_for=\"\""]
    top = " ".join(list(tree["commands"]) + [f for o in tree["options"] for f in o["flags"]])
    lines.append("  if [ -z \"$command\" ]; then words_for=\"" + top + "\"")
    lines.append("  else")
    lines.append("    case \"$command\" in")
    for name, entry in tree["commands"].items():
        flags = " ".join(f for o in entry["options"] for f in o["flags"])
        subs = " ".join(list(entry["commands"]) + entry["choices"])
        if entry["commands"]:
            lines.append(f"      {name}) if [ -z \"$sub\" ]; then words_for=\"{subs} {flags}\"; else case \"$sub\" in")
            for sub, child in entry["commands"].items():
                child_flags = " ".join(f for o in child["options"] for f in o["flags"])
                lines.append(f"        {sub}) words_for=\"{' '.join(child['choices'])} {child_flags}\" ;;")
            lines.append("        esac; fi ;;")
        else:
            lines.append(f"      {name}) words_for=\"{subs} {flags}\" ;;")
    lines += ["    esac", "  fi", "  COMPREPLY=($(compgen -W \"$words_for\" -- \"$cur\"))", "}",
              "complete -o default -F _mobster mobster", ""]
    return "\n".join(lines)


def _fish_text(text):
    return text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")[:120]


def fish(tree):
    lines = ["# mobster completion fish: generated from this build's parser.", "complete -c mobster -f"]
    names = " ".join(tree["commands"])
    for name, entry in tree["commands"].items():
        lines.append(f"complete -c mobster -n 'not __fish_seen_subcommand_from {names}' -a {name} "
                     f"-d '{_fish_text(entry.get('help') or '')}'")

    def option_lines(condition, options):
        out = []
        for option in options:
            parts = []
            for flag in option["flags"]:
                parts.append(f"-l {flag[2:]}" if flag.startswith("--") else f"-s {flag[1:]}")
            extra = ""
            if option["takes"]:
                source = option["source"]
                if source == "files":
                    extra = " -r -F"
                elif source and source.startswith("choices:"):
                    extra = f" -x -a '{source.split(':', 1)[1]}'"
                elif source in ("devices", "tasks", "threads"):
                    extra = f" -x -a '(mobster completion --values {source} 2>/dev/null)'"
                else:
                    extra = " -x"
            out.append(f"complete -c mobster -n '{condition}' {' '.join(parts)}{extra} "
                       f"-d '{_fish_text(option['help'])}'")
        return out
    lines += option_lines(f"not __fish_seen_subcommand_from {names}", tree["options"])
    for name, entry in tree["commands"].items():
        condition = f"__fish_seen_subcommand_from {name}"
        lines += option_lines(condition, entry["options"])
        for sub, child in entry["commands"].items():
            lines.append(f"complete -c mobster -n '{condition}' -a {sub} -d '{_fish_text(child.get('help') or '')}'")
            lines += option_lines(f"{condition}; and __fish_seen_subcommand_from {sub}", child["options"])
        if entry["choices"]:
            lines.append(f"complete -c mobster -n '{condition}' -a '{' '.join(entry['choices'])}'")
    return "\n".join(lines) + "\n"
