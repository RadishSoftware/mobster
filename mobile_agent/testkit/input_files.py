"""Bounded, symlink-free reads of saved test inputs from held directory descriptors."""

import hashlib
import os
import stat
from pathlib import Path

MAX_APP_BYTES = 2_000_000_000
MAX_CACHE_BYTES = 128_000_000
MAX_FILES = 50_000
MAX_DEPTH = 64


class InputError(ValueError):
    """A saved test input cannot be trusted at this admission boundary."""


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _node_identity(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def inside(root, given, *, prefix=None):
    """A physical project child, with no symlink in any relative component."""
    path = Path(given)
    path = path if path.is_absolute() else root / path
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise InputError("The approved file must be inside this project") from None
    if not parts or any(part in (".", "..") for part in parts):
        raise InputError("The approved file must be inside this project")
    if prefix is not None and parts[:len(prefix)] != tuple(prefix):
        raise InputError("Choose a check in this project's .mobster/checks folder")
    current = root
    for part in parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            raise InputError("An approved project file is missing") from None
        if stat.S_ISLNK(mode):
            raise InputError("Symlinks are not allowed in approved project inputs")
    return current


def digest(path, limit):
    """Hash a bounded regular file or directory tree, including names and file boundaries."""
    digest = hashlib.sha256()
    total = seen = 0

    def visit(parent, name, relative, depth):
        nonlocal total, seen
        if depth > MAX_DEPTH:
            raise InputError("An approved project input is nested too deeply")
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        seen += 1
        if seen > MAX_FILES:
            raise InputError("An approved project input has too many files")
        encoded = relative.encode()
        label = len(encoded).to_bytes(4, "big") + encoded
        if stat.S_ISDIR(before.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if _identity(before) != _identity(os.fstat(child)):
                    raise InputError("An approved project input changed while being read")
                digest.update(b"D" + label)
                names = []
                with os.scandir(child) as entries:
                    for entry in entries:
                        names.append(entry.name)
                        if len(names) + seen > MAX_FILES:
                            raise InputError("An approved project input has too many files")
                names.sort()
                for part in names:
                    visit(child, part, f"{relative}/{part}" if relative else part, depth + 1)
                if (_identity(before) != _identity(os.fstat(child)) or
                        _identity(before) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))):
                    raise InputError("An approved project input changed while being read")
            finally:
                os.close(child)
            return
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit - total:
            raise InputError("An approved project input is not a bounded regular file")
        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if _identity(before) != _identity(os.fstat(child)):
                raise InputError("An approved project input changed while being read")
            digest.update(b"F" + label + before.st_size.to_bytes(8, "big"))
            count = 0
            while chunk := os.read(child, 1024 * 1024):
                count += len(chunk)
                if count > limit - total:
                    raise InputError("An approved project input exceeds its byte limit")
                digest.update(chunk)
            if (count != before.st_size or _identity(before) != _identity(os.fstat(child)) or
                    _identity(before) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))):
                raise InputError("An approved project input changed while being read")
            total += count
        finally:
            os.close(child)

    path = Path(path).absolute()
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    opened = [parent]
    ancestors = []
    try:
        for part in path.parts[1:-1]:
            before = os.stat(part, dir_fd=parent, follow_symlinks=False)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            ancestors.append((parent, part, child, before))
            opened.append(child)
            if _node_identity(before) != _node_identity(os.fstat(child)):
                raise InputError("An approved project input changed while being read")
            parent = child
        visit(parent, path.name, "", 0)
        for directory, part, child, before in reversed(ancestors):
            if (_node_identity(before) != _node_identity(os.fstat(child)) or
                    _node_identity(before) != _node_identity(os.stat(part, dir_fd=directory, follow_symlinks=False))):
                raise InputError("An approved project input changed while being read")
    except OSError:
        raise InputError("An approved project input cannot be read") from None
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
    return digest.hexdigest()


def read_bytes(path, limit):
    """Read one bounded regular file and prove the bytes came from its unchanged physical path."""
    path = Path(path).absolute()
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    opened = [parent]
    ancestors = []
    try:
        for part in path.parts[1:-1]:
            before = os.stat(part, dir_fd=parent, follow_symlinks=False)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            ancestors.append((parent, part, child, before))
            opened.append(child)
            if _node_identity(before) != _node_identity(os.fstat(child)):
                raise InputError("A saved test input changed while being read")
            parent = child
        before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise InputError("A saved test input is not a bounded regular file")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if _identity(before) != _identity(os.fstat(fd)):
                raise InputError("A saved test input changed while being read")
            data = bytearray()
            while chunk := os.read(fd, min(65_536, limit + 1 - len(data))):
                data.extend(chunk)
                if len(data) > limit:
                    raise InputError("A saved test input exceeds its byte limit")
            if (len(data) != before.st_size or _identity(before) != _identity(os.fstat(fd)) or
                    _identity(before) != _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False))):
                raise InputError("A saved test input changed while being read")
        finally:
            os.close(fd)
        for directory, part, child, before in reversed(ancestors):
            if (_node_identity(before) != _node_identity(os.fstat(child)) or
                    _node_identity(before) != _node_identity(os.stat(part, dir_fd=directory, follow_symlinks=False))):
                raise InputError("A saved test input changed while being read")
        return bytes(data)
    except OSError:
        raise InputError("A saved test input cannot be read") from None
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
