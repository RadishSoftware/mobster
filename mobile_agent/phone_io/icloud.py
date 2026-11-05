"""Photos both ways through iCloud Photos, when the person has it on, on this Mac and the iPhone.

Mac to iPhone: ``import_media`` adds the files to this Mac's Photos library (into the album ``album``) with Photos'
own AppleScript import; iCloud Photos then carries them to every device signed in to the same Apple Account, usually
within a minute or two on Wi-Fi. iPhone to Mac: ``export_recent`` copies the newest items in this Mac's library (which
iCloud fills from the iPhone) to a folder, originals as they are.

It is the slow route: Mobster uses it only when you choose it (``--via icloud``, or the queue's route), never as a
silent fallback. It needs Automation permission for Photos the first time (macOS asks). Photos opens in the
background to do it.
"""

import os
import subprocess
from pathlib import Path

from . import PhoneIOError

OSASCRIPT = "/usr/bin/osascript"
TIMEOUT_S = 300

# Each script takes its inputs as arguments (argv), never pasted into its source.
IMPORT_SCRIPT = """
on run argv
  set albumName to item 1 of argv
  set theFiles to {}
  repeat with i from 2 to count of argv
    set end of theFiles to (POSIX file (item i of argv)) as alias
  end repeat
  tell application "Photos"
    if albumName is "" then
      set imported to import theFiles skip check duplicates true
    else
      if not (exists album albumName) then make new album named albumName
      set imported to import theFiles into album albumName skip check duplicates true
    end if
    return count of imported
  end tell
end run
"""

EXPORT_SCRIPT = """
on run argv
  set target to POSIX file (item 1 of argv) as alias
  set wanted to (item 2 of argv) as integer
  tell application "Photos"
    set total to count of media items
    if total is 0 then return "[]"
    set first_index to total - wanted + 1
    if first_index < 1 then set first_index to 1
    set picked to items first_index thru total of (get media items)
    export picked to target with using originals
    set names to {}
    repeat with one in picked
      set end of names to filename of one
    end repeat
  end tell
  set AppleScript's text item delimiters to linefeed
  return names as text
end run
"""


def _osascript(script, args, timeout=TIMEOUT_S, runner=None):
    from ..native_helpers import require_devices
    require_devices()
    argv = [OSASCRIPT, "-", *map(str, args)]
    if runner is not None:
        return runner(argv, script, timeout)
    try:
        done = subprocess.run(argv, input=script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PhoneIOError("Photos on this Mac didn't answer in time.", "unknown",
                           "Open Photos and look before you try again.") from None
    except OSError as error:
        raise PhoneIOError(f"Photos couldn't be reached ({type(error).__name__}).", "tools") from None
    return done.returncode, done.stdout, done.stderr


def _explain(code, stderr):
    text = " ".join((stderr or "").split())
    if "-1743" in text or "Not authorized" in text:
        return PhoneIOError("Mobster isn't allowed to use Photos on this Mac.", "permission",
                            "Allow it in System Settings › Privacy & Security › Automation › Photos.")
    return PhoneIOError(f"Photos on this Mac refused: {text[:160] or f'exit {code}'}.", "failed")


def import_media(paths, album="Mobster", runner=None):
    """Add ``paths`` to this Mac's Photos library (and ``album``)."""
    files = [str(Path(p).expanduser().resolve()) for p in paths]
    for file in files:
        if not os.path.isfile(file):
            raise PhoneIOError(f"There is no file at {file}.", "usage")
    code, out, err = _osascript(IMPORT_SCRIPT, [album or "", *files], runner=runner)
    if code != 0:
        raise _explain(code, err)
    try:
        count = int((out or "0").strip() or 0)
    except ValueError:
        count = 0
    if count != len(files):
        raise PhoneIOError(f"Photos on this Mac took {count} of {len(files)}.", "failed",
                           "Check that each one opens in Photos, then try again.")
    return {"imported": count}


def export_recent(folder, count=1, runner=None):
    """Copy the newest ``count`` items of this Mac's Photos library (originals) into ``folder``. Returns [paths]."""
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in folder.iterdir()}
    code, out, err = _osascript(EXPORT_SCRIPT, [str(folder.resolve()), max(1, min(int(count), 20))], runner=runner)
    if code != 0:
        raise _explain(code, err)
    names = [line.strip() for line in (out or "").splitlines() if line.strip()]
    arrived = sorted(p for p in folder.iterdir() if p.name not in before and p.is_file())
    return {"names": names, "paths": [str(p) for p in arrived]}

