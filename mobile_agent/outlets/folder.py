"""A folder destination: each result is appended to one file in a folder you chose, as a CSV row or a JSON line."""

import csv
import errno
import fcntl
import io
import json
import os
import re
import stat
from pathlib import Path

from .kinds import Kind, cell, flat
from .webhook import Outcome

FILE_NAME = re.compile(r"[^/\\:\x00-\x1f]{1,96}\.(csv|jsonl)", re.IGNORECASE)
DEFAULT_FILE = "Mobster results.csv"
HEADER_BYTES = 65_536


def home():
    """The home folder (tests replace it)."""
    return Path.home()


def shown(path):
    """``path`` with the home folder written as ~."""
    try:
        return "~/" + str(Path(path).relative_to(home().resolve())) if Path(path) != home().resolve() else "~"
    except ValueError:
        return str(path)


def inside_home(value):
    """The folder ``value`` names (~ allowed), resolved, when it exists inside the home folder; ValueError else."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError("Choose a folder.")
    if len(text) > 1024 or "\x00" in text:
        raise ValueError("That isn't a folder's path.")
    base = home().resolve()
    path = Path(text.replace("~", str(base), 1) if text.startswith("~") else text)
    if not path.is_absolute():
        raise ValueError("Choose a folder in your home folder, such as ~/Documents.")
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("That folder doesn't exist. Choose one that does.") from None
    if real != base and base not in real.parents:
        raise ValueError("Choose a folder in your home folder. Mobster doesn't write anywhere else.")
    if not real.is_dir():
        raise ValueError("That's a file, not a folder.")
    return real


def file_name(value):
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if not text:
        return DEFAULT_FILE
    if text.startswith(".") or not FILE_NAME.fullmatch(text):
        raise ValueError("Name the file like Results.csv or Results.jsonl.")
    return text


class Folder(Kind):
    id, label = "folder", "Folder"
    example = "Results file"
    fields = ("folder", "file")
    replay_safe = False
    keychain = False

    def setup(self, body, current=None):
        config = dict(current["row"]["config"]) if current else {}
        if current is None or "folder" in body:
            config["folder"] = shown(inside_home(body.get("folder")))
        if current is None or "file" in body:
            config["file"] = file_name(body.get("file"))
        config["format"] = "jsonl" if config["file"].lower().endswith(".jsonl") else "csv"
        if current is not None and not ({"folder", "file"} & set(body)):
            return {}
        target = config["folder"].rstrip("/") + "/" + config["file"]
        if (inside_home(config["folder"]) / config["file"]).is_symlink():
            raise ValueError("That file is a link. Name another file.")
        return {"hint": target, "config": config, "last_test": None}

    @staticmethod
    def _open(config):
        """The file, opened to append (created 0644 when new): (fd, path). OSError or ValueError when it can't be."""
        folder = inside_home(config["folder"])
        path = folder / config["file"]
        # Only ever appends: never truncates, renames or replaces the file, and refuses a link or a non-regular file.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("That isn't a regular file.")
        except BaseException:
            os.close(fd)
            raise
        return fd, path

    @staticmethod
    def _header(path):
        """The file's first line as CSV column names, and whether it ends with a line break; ([], True) when empty."""
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as handle:
            head = handle.read(HEADER_BYTES)
            size = os.fstat(handle.fileno()).st_size
            ends = True
            if size:
                handle.seek(size - 1)
                ends = handle.read(1) in (b"\n", b"\r")
        if not head:
            return [], True
        first = head.decode("utf-8-sig", errors="replace").splitlines()[0] if head.strip() else ""
        names = next(csv.reader(io.StringIO(first)), []) if first else []
        return [name.strip() for name in names], ends

    def lines(self, config, body, path):
        """The bytes to append for ``body``."""
        if config.get("format") == "jsonl":
            document = {key: value for key, value in body.items() if key != "screenshot"}
            return (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        row = flat(body)
        header, ends = self._header(path)
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        if not ends:
            out.write("\n")
        if not header:
            header = list(row)
            writer.writerow(header)
        lower = {name.casefold(): value for name, value in row.items()}
        writer.writerow([cell(lower.get(name.casefold(), "")) for name in header])
        return out.getvalue().encode("utf-8")

    def _append(self, ctx, body):
        config = ctx.destination["config"]
        where = config.get("folder", "the folder")
        try:
            fd, path = self._open(config)
        except ValueError as error:
            return Outcome(False, False, None, f"{error} Choose another in Settings › Connectors.")
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.EMLINK):
                return Outcome(False, False, None, f"{config.get('file')} is a link, so Mobster won't write to it. "
                                                   "Name another file in Settings › Connectors.")
            if error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                return Outcome(False, False, None, f"macOS didn't let Mobster write in {where}. Allow it in System "
                                                   "Settings › Privacy & Security › Files and Folders, then resend.")
            return Outcome(False, False, None, f"Mobster couldn't open {config.get('file')} in {where}.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self.lines(config, body, path)
            written = os.write(fd, data)
            if written != len(data):
                return Outcome(False, False, None, f"Only part of the result fit in {config.get('file')}. Check the "
                                                   "file and your disk's free space.")
        except OSError as error:
            if error.errno == errno.ENOSPC:
                return Outcome(False, False, None, "The disk is full. Free some space, then resend.")
            return Outcome(False, False, None, f"Mobster couldn't write to {config.get('file')}.")
        finally:
            os.close(fd)
        return Outcome(True, False, None)

    def send(self, ctx, body):
        return self._append(ctx, body)

    def test(self, ctx):
        """Checks the folder and the file can be written, without adding a row."""
        config = ctx.destination["config"]
        try:
            fd, _path = self._open(config)
        except ValueError as error:
            return Outcome(False, False, None, f"{error} Choose another in Settings › Connectors.")
        except OSError:
            return Outcome(False, False, None, f"Mobster can't write to {config.get('file')} in "
                                               f"{config.get('folder')}. Choose another folder or file.")
        os.close(fd)
        return Outcome(True, False, None, f"Mobster can write to {config.get('file')}.")
