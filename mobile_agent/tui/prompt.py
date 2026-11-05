"""The terminal UI's prompt: @mentions, the suggestion popup and history recall."""

import os
import re

from rich.text import Text
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ..app_choice import MENTION, find_app, infer_app, mention_rank
from .chrome import EXAMPLES
from .keys import COMMANDS, SECTIONS
from .render import ACCENT, FAINT, GREEN, TEXT

PATH_MENTION = re.compile(r"(?:^|\s)@((?:\./|~/|/)[^\s]*)$")


class PromptMixin:
    """The prompt box: which app a task names, the popup of commands, apps and paths, and recalling past tasks."""

    def target_app(self, value):
        """The app the next task starts in: the one chosen, the one it names, else None (Mobster picks)."""
        if self.app_choice:
            return self.app_choice
        goal, mentioned = self.split_mentions(value)
        return mentioned or (infer_app(goal, self.apps) if goal.strip() else None)

    def split_mentions(self, value):
        """(the task without the @words that name an app, the first app they name)."""
        named = []

        def drop(match):
            app = find_app(match.group(1), self.apps)
            if app is None:
                return match.group(0)
            named.append(app)
            return ""
        return MENTION.sub(drop, value).strip(), (named[0] if named else None)

    def suggest(self, value):
        box = self.query_one("#suggest", OptionList)
        hint = self.query_one("#suggest-hint", Static)
        options = []
        footer = ""
        if value.startswith("/") and " " not in value:
            query = value[1:].casefold()
            matches = [c for c in COMMANDS if c.name.startswith(query) or (query and query in c.name)]
            name_width = max((len(c.name) + 1 for c in matches), default=0) + 2
            help_width = max((len(c.help) for c in matches), default=0) + 2
            for section in SECTIONS:
                rows = [c for c in matches if c.section == section]
                if not rows:
                    continue
                if not query:
                    options.append(Option(Text(section, style=f"bold {FAINT}"), id=f"section:{section}",
                                          disabled=True))
                for command in rows:
                    prompt = Text()
                    prompt.append(f"/{command.name}".ljust(name_width), style=f"bold {ACCENT}")
                    prompt.append(command.help.ljust(help_width), style=TEXT)
                    prompt.append(command.key or (command.args if command.args.startswith("<") else ""),
                                  style=FAINT)
                    options.append(Option(prompt, id=f"cmd:{command.name}"))
            footer = "↑↓ move   enter choose   esc close"
        else:
            path = PATH_MENTION.search(value)
            match = re.search(r"(?:^|\s)@([\w.-]*)$", value)
            if path:
                for entry in self.path_matches(path.group(1))[:10]:
                    prompt = Text()
                    prompt.append(entry["shown"], style=f"bold {TEXT}")
                    prompt.append("  folder" if entry["dir"] else "  attach", style=FAINT)
                    options.append(Option(prompt, id=f"path:{entry['path']}"))
                footer = "enter attach   tab open a folder   esc close"
            elif match:
                query = match.group(1).casefold()
                # Best match first (tab and enter take the first row): "@fa" is FaceTime, not safari.
                ranked = sorted(((rank, index, app) for index, app in enumerate(self.apps)
                                 if (rank := mention_rank(app, query)) is not None), key=lambda item: item[:2])
                for _, _, app in ranked[:10]:
                    prompt = Text()
                    prompt.append(app["name"], style=f"bold {TEXT}")
                    prompt.append(f"  {app['bundleId']}", style=FAINT)
                    if app.get("installed"):
                        prompt.append("  installed", style=GREEN)
                    options.append(Option(prompt, id=f"app:{app['id']}"))
                footer = "enter choose   esc close"
        box.clear_options()
        if options:
            box.add_options(options)
            first = next((i for i, option in enumerate(options) if not option.disabled), 0)
            box.highlighted = first
        box.set_class(bool(options), "open")
        hint.update(Text(footer, style=FAINT))
        hint.set_class(bool(options), "open")

    def path_matches(self, typed):
        """Files and folders for an @./, @~/ or @/ path: [{path, shown, dir}]."""
        from pathlib import Path
        expanded = os.path.expanduser(typed)
        folder, _, stem = expanded.rpartition("/")
        folder = folder + "/" if folder or typed.startswith("/") else "./"
        try:
            entries = sorted(Path(folder or "/").iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except OSError:
            return []
        out = []
        for entry in entries:
            if entry.name.startswith(".") and not stem.startswith("."):
                continue
            if not entry.name.casefold().startswith(stem.casefold()):
                continue
            shown = typed[:len(typed) - len(stem)] + entry.name + ("/" if entry.is_dir() else "")
            out.append({"path": str(entry), "shown": shown, "dir": entry.is_dir()})
        return out

    def suggestions_open(self):
        return self.query_one("#suggest", OptionList).has_class("open")

    def close_suggestions(self):
        self.query_one("#suggest", OptionList).set_class(False, "open")
        self.query_one("#suggest-hint", Static).set_class(False, "open")

    def accept_suggestion(self, key_name="enter"):
        box = self.query_one("#suggest", OptionList)
        if box.highlighted is None:
            return False
        option = box.get_option_at_index(box.highlighted)
        if option.disabled:
            return False
        prompt = self.query_one("#prompt", Input)
        kind, _, key = option.id.partition(":")
        if kind == "cmd":
            command = next(c for c in COMMANDS if c.name == key)
            if command.args and not command.args.startswith("["):
                prompt.value = f"/{key} "
            else:
                prompt.value = ""
                self.run_command(key, "")
        elif kind == "app":
            self.app_choice = next((a for a in self.apps if a["id"] == key), None)
            prompt.value = re.sub(r"(?:^|\s)@[\w.-]*$", "", prompt.value).lstrip()
        elif kind == "path":
            if os.path.isdir(key) and key_name == "tab":
                typed = PATH_MENTION.search(prompt.value).group(1)
                stem = typed.rpartition("/")[2]
                prompt.value = prompt.value[:len(prompt.value) - len(stem)] + os.path.basename(key) + "/"
                prompt.cursor_position = len(prompt.value)
                self.suggest(prompt.value)
                return True
            prompt.value = PATH_MENTION.sub("", prompt.value).rstrip()
            if not os.path.isdir(key):
                self.attach(key)
        prompt.cursor_position = len(prompt.value)
        self.close_suggestions()
        self.refresh_chip()
        return True

    def prompt_key(self, key, character):
        """Keys the prompt hands over before editing: the popup's, history recall and ``?``. True if used."""
        if self.suggestions_open():
            box = self.query_one("#suggest", OptionList)
            if key in {"down", "up"}:
                box.action_cursor_down() if key == "down" else box.action_cursor_up()
                option = box.get_option_at_index(box.highlighted) if box.highlighted is not None else None
                if option is not None and option.disabled:
                    box.action_cursor_down() if key == "down" else box.action_cursor_up()
                return True
            if key in {"tab", "enter"}:
                return self.accept_suggestion(key)
            if key == "escape":
                self.close_suggestions()
                return True
            return False
        if key in {"up", "down"}:
            self.action_recall(-1 if key == "up" else 1)
            return True
        if character == "?" and not self.query_one("#prompt", Input).value:
            self.run_command("help", "")
            return True
        return False

    def action_recall(self, delta):
        prompt = self.query_one("#prompt", Input)
        # With no past tasks yet, ↑ steps through the Try rows on the welcome screen, the first one first.
        recalls = self.recalls or [goal for _, goal in reversed(EXAMPLES)]
        if self.focused is not prompt or self.suggestions_open() or self.running is not None:
            return
        if self.recall_index is None:
            if delta > 0:
                return
            self.recall_index = len(recalls)
        self.recall_index = max(0, min(len(recalls), self.recall_index + delta))
        prompt.value = recalls[self.recall_index] if self.recall_index < len(recalls) else ""
        prompt.cursor_position = len(prompt.value)
