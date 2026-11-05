#!/usr/bin/env python3
"""Stop a build that could hold pymobiledevice3.

    python scripts/check_no_pymobiledevice3.py            # the Python running this can't import it
    python scripts/check_no_pymobiledevice3.py PATH...    # these frozen builds (folders or files) don't hold it

pymobiledevice3 is a separate program you install yourself (`pipx install pymobiledevice3`). Mobster runs it as a
child process and never imports or bundles it; mobile_agent/tests/test_no_pymobiledevice3.py checks the source.
PyInstaller freezes every import it can resolve, so a build venv that has pymobiledevice3 could carry it into a build.
desktop/scripts/build-sidecar.sh and packaging/cli/build.sh run this with the build venv's Python, before freezing and
again on what was frozen.

A frozen build is checked by its file and folder names, the members of each zip in it, and the table of contents of
each PyInstaller archive in it (the modules inside the bootloader), read with the build venv's PyInstaller.

Exit status 0 when nothing is found, 1 when anything is, 2 on bad input.
"""

import importlib.util
import os
import sys
import zipfile
from pathlib import Path

NAMES = ("pymobiledevice3",)
MACH_O = {bytes.fromhex(magic) for magic in ("cffaedfe", "cefaedfe", "feedfacf", "feedface", "cafebabe")}
SHOWN = 20


def names_it(name):
    """Whether a path or a module name points into one of NAMES: the package, a module in it, or its dist-info."""
    for part in str(name).replace("\\", "/").split("/"):
        if any(part == package or part.startswith((package + ".", package + "-")) for package in NAMES):
            return True
    return False


def importable(find_spec=importlib.util.find_spec):
    """The NAMES the running Python could import, found without importing them."""
    return [name for name in NAMES if find_spec(name) is not None]


def _mach_o(path):
    with open(path, "rb") as stream:
        return stream.read(4) in MACH_O


def contents(root, read_archive):
    """(where, name) for every name a build holds: each file and folder, each zip member, and each entry of a
    PyInstaller archive. ``read_archive(path)`` lists a Mach-O file's archive, or None when it holds none."""
    root = Path(root)
    if root.is_file():
        found = [(root, root.name)]
    else:
        found = []
        for directory, folders, files in os.walk(root):
            for name in sorted(folders) + sorted(files):
                path = Path(directory) / name
                found.append((path, path.relative_to(root).as_posix()))
    for path, where in found:
        yield where, where
        if path.is_symlink() or not path.is_file():
            continue
        if _mach_o(path):
            for name in read_archive(path) or ():
                yield where, name
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    yield where, name


def pyinstaller_reader():
    """PyInstaller's own reader of a bootloader's archive, nested PYZ included. ImportError without PyInstaller."""
    from PyInstaller.archive.readers import ArchiveReadError, pkg_archive_contents

    def read(path):
        try:
            return pkg_archive_contents(str(path))
        except ArchiveReadError:  # a library or program with no PyInstaller archive
            return None

    return read


def main(argv, find_spec=importlib.util.find_spec, reader=pyinstaller_reader):
    if not argv:
        found = importable(find_spec)
        if found:
            print(f"check_no_pymobiledevice3: {sys.executable} can import {', '.join(found)}", file=sys.stderr)
            return 1
        return 0
    try:
        read = reader()
    except ImportError:
        print("check_no_pymobiledevice3: reading a frozen build needs PyInstaller. Run this with the build venv's "
              "Python.", file=sys.stderr)
        return 2
    hits = []
    for root in argv:
        if not os.path.exists(root):
            print(f"check_no_pymobiledevice3: {root} doesn't exist", file=sys.stderr)
            return 2
        try:
            hits += [f"{root}: {where}: {name}" for where, name in contents(root, read) if names_it(name)]
        except Exception as error:  # an archive PyInstaller can't read: no answer is not a pass
            print(f"check_no_pymobiledevice3: couldn't read {root} ({type(error).__name__})", file=sys.stderr)
            return 2
    for line in hits[:SHOWN]:
        print(f"check_no_pymobiledevice3: {line}", file=sys.stderr)
    if len(hits) > SHOWN:
        print(f"check_no_pymobiledevice3: and {len(hits) - SHOWN} more", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
