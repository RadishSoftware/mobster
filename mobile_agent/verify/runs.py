"""The run folder: ``.mobster/runs/<run id>/`` and the ``.gitignore`` that keeps it out of git."""

import os
import secrets
import time
from pathlib import Path


def new_run_id():
    """YYYYMMDD-HHMMSS-xxxx: the local time plus 4 random hex characters."""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


# Runs and test results show whatever was on the screen, and the cache is per machine.
GITIGNORE = "runs/\nbuild/\ncache/\ntest-results/\n"
OLD_GITIGNORES = ("runs/\nbuild/\n",)


def ensure_gitignore(runs_dir):
    # Never over the user's own file; one an earlier Mobster wrote, word for word, is brought up to date.
    runs_dir = Path(runs_dir)
    if runs_dir.parent.name != ".mobster":
        return
    folder = runs_dir.parent
    ignore = folder / ".gitignore"
    if ignore.exists():
        try:
            if ignore.is_file() and not ignore.is_symlink() and ignore.read_text(encoding="utf-8") in OLD_GITIGNORES:
                temporary = folder / ".gitignore.tmp"
                temporary.write_text(GITIGNORE, encoding="utf-8")
                os.replace(temporary, ignore)
        except (OSError, UnicodeDecodeError):
            pass  # the user's file, or one we can't read: left as it is
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(ignore, "x", encoding="utf-8") as handle:
            handle.write(GITIGNORE)
    except FileExistsError:
        pass  # another run wrote it first


def make_run_dir(runs_dir):
    # 0700, as the task journal's are: a run's frames and steps hold what was on the screen.
    runs_dir = Path(runs_dir)
    ensure_gitignore(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    while True:
        run_id = new_run_id()
        run_dir = runs_dir / run_id
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(run_dir, 0o700)  # whatever the umask
        (run_dir / "frames").mkdir(mode=0o700)
        return run_id, run_dir
