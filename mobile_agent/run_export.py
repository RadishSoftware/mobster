"""`mobster export ID --gif [--mp4] [--redact] [-o PATH]`: save a finished task as a captioned GIF or MP4.

It reads the task history the way `mobster history` does (SQLite, read-only, so the terminal UI and the Mac app can
keep theirs open): the terminal UI's journal and the Mac app's (`mobster serve`'s), with each step's screen from the
`frames` folder beside it. Nothing is uploaded. Without -o the file goes to ~/Downloads and never replaces one there.
"""

import json
from pathlib import Path
import re
import sqlite3
import sys

from .paths import source_checkout, user_data_dir

FRAME_ID = re.compile(r"f[0-9a-f]{10}")
RUN_ID = re.compile(r"[0-9a-f]{4,12}")
ACTIVE = ("queued", "running")
REDACT_HINT = "It shows every screen of the task. Check it before you post it, or save it again with --redact."


class ExportError(Exception):
    pass


def journals():
    """Every task journal on this Mac, newest-written first: the terminal UI's and the Mac app's, in a source
    checkout's .state and in the data folder."""
    from . import server
    folders = []
    if source_checkout():
        folders.append(Path(server.__file__).parent / ".state")
    folders.append(user_data_dir() / "state")
    paths = [folder / name for folder in folders for name in ("terminal.sqlite3", "mobster.sqlite3")]
    found = [path for path in dict.fromkeys(paths) if path.is_file()]
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def _connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)


def _events(connection, run_id):
    events = []
    for (data,) in connection.execute("SELECT data FROM events WHERE run_id=? ORDER BY seq", (run_id,)):
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def find_run(identifier, paths=None):
    """(run record with its events, the journal's path). ``identifier``: a task's id, a unique start of one, or
    "latest" (the newest finished task in any journal). ExportError when none or several match."""
    paths = journals() if paths is None else paths
    identifier = (identifier or "").strip().lower()
    latest = identifier == "latest"
    if not latest and not RUN_ID.fullmatch(identifier):
        raise ExportError(f"“{identifier}” isn’t a task ID. List your tasks with `mobster history`.")
    matches = []
    for path in paths:
        try:
            connection = _connect(path)
        except sqlite3.Error:
            continue
        try:
            if latest:
                rows = connection.execute("SELECT id, data FROM runs ORDER BY rowid DESC LIMIT 50").fetchall()
            else:
                rows = connection.execute("SELECT id, data FROM runs WHERE substr(id, 1, ?) = ?",
                                          (len(identifier), identifier)).fetchall()
            for run_id, data in rows:
                try:
                    record = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(record, dict) or (latest and record.get("status") in ACTIVE):
                    continue
                matches.append((record, path, run_id))
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    if latest:
        matches.sort(key=lambda item: item[0].get("createdAt") or 0, reverse=True)
        matches = matches[:1]
    if not matches:
        what = "No finished task" if latest else f"No task {identifier}"
        raise ExportError(f"{what} in your history. List your tasks with `mobster history`.")
    if len({run_id for _, _, run_id in matches}) > 1:
        raise ExportError(f"Several tasks start with {identifier}. Give more of the ID.")
    record, path, run_id = matches[0]
    connection = _connect(path)
    try:
        record = {**record, "events": _events(connection, run_id)}
    finally:
        connection.close()
    return record, path


def frame_reader(journal, run_id):
    folder = Path(journal).parent / "frames" / run_id

    def read(frame_id):
        if not FRAME_ID.fullmatch(frame_id or ""):
            return None
        try:
            return (folder / f"{frame_id}.jpg").read_bytes()
        except OSError:
            return None
    return read


def targets(output, stem, want_gif, want_mp4):
    """{extension: (folder, stem, overwrite)}: -o a folder (or none) saves "stem.gif" there, never replacing a
    file; -o a file saves to that name (its .mp4 beside it), replacing it."""
    if output is None or output.is_dir() or str(output).endswith("/"):
        folder = output if output is not None else Path.home() / "Downloads"
        return {ext: (folder, stem, False) for ext, wanted in (("gif", want_gif), ("mp4", want_mp4)) if wanted}
    base = output.with_suffix("") if output.suffix.lower() in (".gif", ".mp4") else output
    return {ext: (base.parent, base.name, True) for ext, wanted in (("gif", want_gif), ("mp4", want_mp4)) if wanted}


def _write(folder, stem, extension, data, overwrite):
    from .server import save_export_bytes
    from .api_errors import APIError
    if overwrite:
        target = Path(folder) / f"{stem}.{extension}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as error:
            raise ExportError(f"Couldn’t write {target}: {error.strerror or error}.") from None
        return {"name": target.name, "path": str(target)}
    try:
        return save_export_bytes(stem, extension, data, folder)
    except APIError as error:
        raise ExportError(str(error)) from None
    except OSError as error:
        raise ExportError(f"Couldn’t write to {folder}: {error.strerror or error}.") from None


def _size(count):
    return f"{count / 1_000_000:.1f} MB" if count >= 100_000 else f"{max(1, round(count / 1000))} KB"


def _home(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def export(args, stream=None, errors=None):
    """Exit 0 saved, 1 couldn't (no such task, no screens, too long, no ffmpeg for --mp4), 2 usage."""
    from . import run_gif
    stream, errors = stream or sys.stdout, errors or sys.stderr
    if not args.gif and not args.mp4:
        errors.write("mobster export: say what to save: --gif, --mp4, or both. Example: "
                     "mobster export latest --gif\n")
        return 2
    try:
        record, journal = find_run(args.run_id)
        if record.get("status") in ACTIVE:
            raise ExportError("This task is still running. Save it once it ends.")
        read = frame_reader(journal, record.get("id") or "")
        ffmpeg = run_gif.ffmpeg_path() if args.mp4 else None
        if args.mp4 and ffmpeg is None:
            raise ExportError("--mp4 needs ffmpeg. Install it with `brew install ffmpeg`, or save a GIF with --gif.")
        stem = run_gif.file_stem(record.get("goal"))
        places = targets(args.output, stem, args.gif, args.mp4)
        result = {"ok": True, "id": record.get("id"), "gif": None, "mp4": None}
        if args.gif:
            try:
                clip = run_gif.build_clip(record, read, theme=args.theme, redact=args.redact)
            except ValueError as error:
                raise ExportError(str(error)) from None
            folder, name, overwrite = places["gif"]
            saved = _write(folder, name, "gif", clip.gif, overwrite)
            result["gif"] = {**saved, "bytes": len(clip.gif), "frames": clip.frames, "seconds": round(clip.seconds, 1)}
            stem = Path(saved["name"]).stem
        if args.mp4:
            try:
                data = run_gif.build_mp4(record, read, theme=args.theme, redact=args.redact, ffmpeg=ffmpeg)
            except ValueError as error:
                raise ExportError(str(error)) from None
            except (RuntimeError, OSError) as error:
                raise ExportError(str(error)) from None
            folder, name, overwrite = places["mp4"]
            saved = _write(folder, stem if args.gif and not overwrite else name, "mp4", data, overwrite)
            result["mp4"] = {**saved, "bytes": len(data)}
    except ExportError as error:
        if args.json:
            stream.write(json.dumps({"ok": False, "error": str(error)}) + "\n")
        else:
            errors.write(f"mobster export: {error}\n")
        return 1
    if args.json:
        stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    for kind in ("gif", "mp4"):
        saved = result[kind]
        if saved:
            facts = _size(saved["bytes"]) + (f", {saved['seconds']:g} s" if kind == "gif" else "")
            stream.write(f"Saved {saved['name']} ({facts}) to {_home(str(Path(saved['path']).parent))}\n")
    if not args.redact:
        stream.write(REDACT_HINT + "\n")
    return 0
