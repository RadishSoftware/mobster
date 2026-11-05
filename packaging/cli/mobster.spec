# PyInstaller spec for the standalone `mobster` command (run by packaging/cli/build.sh).
#
# One-dir, no app bundle: `mobster/mobster` is the bootloader and `mobster/_internal/`
# the Python runtime beside it, so `mobster` starts in place with no unpacking.
# The CLI-owned entry calls the same main as `python -m mobile_agent`.
# `mobster` with no command still opens the terminal UI.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

# These are reached only through optional imports the agent never exercises: jsonschema's
# remote-$ref fetch tries `requests` (which drags in urllib3, cryptography, chardet,
# charset_normalizer, certifi and zstd); sqlite3's interactive `__main__` pulls
# readline/ncurses; PyInstaller's multiprocessing runtime hook imports multiprocessing
# at every start although nothing uses it.
EXCLUDES = [
    "requests", "urllib3", "idna", "cryptography", "chardet", "charset_normalizer",
    "certifi", "backports.zstd", "_cffi_backend", "readline", "multiprocessing",
    "tkinter", "unittest", "pydoc", "doctest", "lib2to3", "pip", "setuptools",
    # Mobster Remote and Teams are part of Mobster for Mac only, and their packages are not in the open-source tree.
    "mobile_agent.remote", "mobile_agent.teams",
    # A separate program you install (pipx install pymobiledevice3). Mobster runs it as a child process and never
    # imports or bundles it. The build stops if it is in the venv or in the frozen tree
    # (scripts/check_no_pymobiledevice3.py).
    "pymobiledevice3",
]

HIDDEN_IMPORTS = [
    # Pillow registers its format plugins by dynamic import: frames are JPEGs.
    "PIL.Image", "PIL.ImageChops", "PIL.ImageStat", "PIL.JpegImagePlugin",
    "PIL.PngImagePlugin", "PIL.ImageDraw", "PIL.ImageFont",
    # Check files are YAML.
    "yaml",
    # The argument parser and its help: __main__ imports it inside functions, after `--version` and `--help`.
    "mobile_agent.cli_parser",
    # The developer commands are imported inside functions, so name them here.
    "mobile_agent.devtools",
    "mobile_agent.verify.cli",
    "mobile_agent.sim.cli",
    "mobile_agent.mcp_server.cli",
    "mobile_agent.integrations.cli",
    # The terminal UI, which `mobster` with no command opens.
    "mobile_agent.tui.app",
    "mobile_agent.tui.render",
    # The SOTA tracks (seam S1/S9): tracks.load imports each register module inside a function.
    "mobile_agent.tracks",
    "mobile_agent.harness.register", "mobile_agent.threads.register", "mobile_agent.memory.register",
    "mobile_agent.attachments.register", "mobile_agent.wireless.register", "mobile_agent.testkit.register",
    "mobile_agent.threads.cli", "mobile_agent.memory.cli", "mobile_agent.testkit.cli", "mobile_agent.wireless.cli",
    # Loaded by name with importlib, so import analysis never sees them: the engine's hooks
    # (harness_api.HOOKS_MODULE) and the app packs (packs/register.py MODULES).
    "mobile_agent.engine_hooks", "mobile_agent.packs.instagram", "mobile_agent.packs.tiktok",
    "mobile_agent.packs.x", "mobile_agent.packs.youtube", "mobile_agent.packs.threads", "mobile_agent.packs.linkedin",
    "mobile_agent.packs.reddit",
    "mobile_agent.packs.messages", "mobile_agent.packs.settings", "mobile_agent.packs.calendar",
    "mobile_agent.packs.notes", "mobile_agent.packs.maps", "mobile_agent.packs.photos", "mobile_agent.packs.safari",
]

# A module another workstream adds may be missing from the tree being built. PyInstaller
# only warns about a hidden import it can't find, and build.sh's smoke then fails on the
# command that needs it, so a partial build never ships quietly.
# Source files a command reads at run time: `mobster build-ocr` compiles ocr.swift (with Xcode's swiftc).
# `conditions.network` and `stubs` compile verify/netshim.m for the simulator the first time a check uses them.
DATAS = [(os.path.join(ROOT, "mobile_agent", "swift", "ocr.swift"), "mobile_agent/swift"),
         (os.path.join(ROOT, "mobile_agent", "verify", "netshim.m"), "mobile_agent/verify")]

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,  # Keep asserts: the agent's safety checks must not change when frozen.
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mobster",
    console=True,
    strip=False,
    upx=False,  # UPX breaks signed macOS binaries.
    target_arch="arm64",
    # build.sh signs every Mach-O itself afterwards, with the Developer ID or ad hoc.
    codesign_identity=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="mobster")
