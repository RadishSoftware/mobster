"""Where a plugin comes from: a folder on this Mac, a ``.zip``, or a GitHub repository (``owner/repo`` or
``owner/repo@ref``, downloaded as GitHub's tarball over https)."""

import io
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .. import __version__
from .manifest import PluginError

MAX_FILES = 2000
MAX_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE = 20 * 1024 * 1024
REPO = re.compile(r"(?:https://github\.com/)?([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?"
                  r"(?:@([A-Za-z0-9._/-]{1,100}))?/?")
SKIP = {".git", "node_modules", "__pycache__", ".DS_Store"}


def staging():
    """A fresh folder each source is copied into first, so what is read, shown and installed is one fixed copy."""
    return Path(tempfile.mkdtemp(prefix="mobster-plugin-"))


def _safe_name(name):
    """``name`` as a relative path inside the archive, or None for one that would land outside it."""
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or any(part == ".." for part in path.parts) or not path.parts:
        return None
    return Path(*path.parts)


def _budget():
    return {"files": 0, "bytes": 0}


def _count(budget, size):
    budget["files"] += 1
    budget["bytes"] += size
    if budget["files"] > MAX_FILES:
        raise PluginError(f"This plugin has more than {MAX_FILES} files.")
    if budget["bytes"] > MAX_BYTES:
        raise PluginError("This plugin is larger than 50 MB unpacked.")


def from_folder(path, into):
    source = Path(os.path.expanduser(str(path))).resolve()
    if not source.is_dir():
        raise PluginError("There's no folder at that path. Check it and try again.")
    budget = _budget()
    for folder, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d not in SKIP and not os.path.islink(os.path.join(folder, d))]
        for name in files:
            file = Path(folder) / name
            if name in SKIP or file.is_symlink() or not file.is_file():
                continue
            _count(budget, file.stat().st_size)
            target = into / file.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
    return root_of(into)


def from_zip(data, into):
    if len(data) > MAX_ARCHIVE:
        raise PluginError("This archive is larger than 20 MB.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PluginError("That file isn't a zip archive.") from None
    budget = _budget()
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            relative = _safe_name(info.filename)
            if relative is None:
                raise PluginError("This archive has a file that would land outside its folder, so Mobster refused it.")
            mode = (info.external_attr >> 16) & 0o170000
            if mode and mode != 0o100000:
                raise PluginError("This archive has a link or a special file, so Mobster refused it.")
            if relative.parts[0] in SKIP or relative.name in SKIP:
                continue
            _count(budget, info.file_size)
            target = into / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as stream, open(target, "wb") as out:
                out.write(stream.read(MAX_BYTES + 1))
    return root_of(into)


def from_tar(data, into):
    if len(data) > MAX_ARCHIVE:
        raise PluginError("This archive is larger than 20 MB.")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError:
        raise PluginError("GitHub's download isn't an archive Mobster can read.") from None
    budget = _budget()
    with archive:
        for member in archive.getmembers():
            if member.isdir() or member.type == tarfile.XGLTYPE or member.type == tarfile.XHDTYPE:
                continue
            relative = _safe_name(member.name)
            if relative is None:
                raise PluginError("This archive has a file that would land outside its folder, so Mobster refused it.")
            if not member.isfile():
                continue  # links and special files are left out
            if any(part in SKIP for part in relative.parts):
                continue
            _count(budget, member.size)
            target = into / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            with open(target, "wb") as out:
                out.write(stream.read(MAX_BYTES + 1) if stream else b"")
    return root_of(into)


def parse_repo(text):
    """(owner, repo, ref) from ``owner/repo``, ``owner/repo@ref`` or a github.com URL; PluginError otherwise."""
    match = REPO.fullmatch(str(text or "").strip())
    if not match:
        raise PluginError("Write a GitHub repository as owner/repo, or owner/repo@branch.")
    return match.group(1), match.group(2), match.group(3) or "HEAD"


def download(url, opener=None):
    if not url.startswith("https://"):
        raise PluginError("Mobster downloads plugins over https only.")
    request = Request(url, headers={"User-Agent": f"Mobster/{__version__}"})
    try:
        with (opener or urlopen)(request, timeout=30) as response:
            data = response.read(MAX_ARCHIVE + 1)
    except HTTPError as error:
        if error.code == 404:
            raise PluginError("GitHub has no such repository, or it's private.") from None
        raise PluginError(f"GitHub answered HTTP {error.code}. Try again.") from None
    except (URLError, OSError):
        raise PluginError("Mobster couldn't reach GitHub. Check this Mac's connection, then try again.") from None
    return data


def from_github(text, into, opener=None):
    owner, repo, ref = parse_repo(text)
    data = download(f"https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}", opener)
    return from_tar(data, into)


def root_of(folder):
    """The folder holding .claude-plugin/plugin.json: ``folder``, or the one folder inside it."""
    if (folder / ".claude-plugin" / "plugin.json").is_file():
        return folder
    inside = [p for p in folder.iterdir() if p.is_dir()]
    if len(inside) == 1 and (inside[0] / ".claude-plugin" / "plugin.json").is_file():
        return inside[0]
    raise PluginError("This isn't a plugin: it has no .claude-plugin/plugin.json.")
