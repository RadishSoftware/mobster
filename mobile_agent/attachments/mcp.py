"""The files track's MCP tools (seam S10): put_file, get_file and list_files, listed only when the server was
started with ``mobster mcp --allow-files``.

- ``put_file {device, path, app | clipboard}`` (ACTS): a file from this Mac into an app's folder on a USB iPhone,
  or a text file onto the device's clipboard. The client's own permission prompt applies to the call.
- ``get_file {device, app, name}``: a file from an app's folder, saved under the server's working folder.
- ``list_files {device, app?}`` (READ_ONLY): the apps that share files, or the files in one.

Every device a tool names passes ``--allow-device`` (ToolSet._record_for), and results pass the ToolSet's mask.
Nothing reaches the camera roll: only apps' Documents folders, over the cable.
"""

import os
from pathlib import Path

from ..mcp_server.protocol import ToolResult
from ..mcp_server.tools import ACTS, READ_ONLY, ToolError, jsonable, sentence

WRITES_HERE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
_DEVICE = {"type": "string", "minLength": 1, "maxLength": 128,
           "description": "a USB iPhone from list_devices (its id or name)"}
_APP = {"type": "string", "minLength": 3, "maxLength": 155, "pattern": "^[A-Za-z0-9][A-Za-z0-9.-]*$",
        "description": "the app's bundle ID, such as com.apple.Pages (list_files lists the apps that share files)"}


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


class FilesTools:
    name = "files"

    def __init__(self, phone_files=None, cwd=None):
        self._phone_files = phone_files
        self._cwd = cwd

    def definitions(self, toolset):
        return [
            {"name": "put_file",
             "description": "Copy a file from this Mac into an app's folder on a USB iPhone (the folder Files shows "
                            "under On My iPhone), or a text file onto its clipboard. A file with the same name is "
                            "kept; the copy gets a new name. At most 100 MB; never the camera roll.",
             "inputSchema": _schema({"device": _DEVICE,
                                     "path": {"type": "string", "minLength": 1, "maxLength": 1024,
                                              "description": "the absolute path of the file on this Mac"},
                                     "app": _APP,
                                     "clipboard": {"type": "boolean",
                                                   "description": "true: put a text file's text on the clipboard "
                                                                  "instead (at most 64 KB)"}},
                                    ("device", "path"))},
            {"name": "get_file",
             "description": "Copy a file from an app's folder on a USB iPhone to this Mac, into the server's working "
                            "folder. Returns its path.",
             "inputSchema": _schema({"device": _DEVICE, "app": _APP,
                                     "name": {"type": "string", "minLength": 1, "maxLength": 255,
                                              "description": "the file's name, from list_files"}},
                                    ("device", "app", "name"))},
            {"name": "list_files",
             "description": "Without app: the iPhone's apps whose folders show in Files. With app: the files in "
                            "that app's folder, with sizes.",
             "inputSchema": _schema({"device": _DEVICE, "app": _APP}, ("device",))},
        ]

    def annotations(self):
        return {"put_file": ACTS, "get_file": WRITES_HERE, "list_files": READ_ONLY}

    def instructions(self):
        """None: the server's instructions don't change (the tools are listed only with --allow-files, and their
        descriptions say what they do)."""
        return None

    def _open(self, record):
        if self._phone_files is not None:
            return self._phone_files(record)
        from ..phone_io.files import PhoneFiles
        return PhoneFiles(record)

    def call(self, name, arguments, call, toolset):
        from ..phone_io import PhoneIOError
        record = toolset._record_for(arguments)
        try:
            if name == "put_file":
                return self._put(record, arguments, call, toolset)
            phone = self._open(record)
            if name == "list_files":
                if not arguments.get("app"):
                    apps = phone.apps()
                    words = ", ".join(f"{a['name']} ({a['bundleId']})" for a in apps) or "none"
                    return ToolResult(f"Apps on {record['name']} whose folders show in Files: {words}.",
                                      jsonable({"device": record["id"], "apps": apps}))
                files = phone.ls(arguments["app"])
                words = ", ".join(f"{f['name']}{'/' if f['folder'] else ''}" for f in files) or "nothing"
                return ToolResult(f"{arguments['app']}'s folder on {record['name']} holds: {words}.",
                                  jsonable({"device": record["id"], "app": arguments["app"], "files": files}))
            folder = Path(self._cwd or os.getcwd())
            saved = phone.get(arguments["app"], arguments["name"], folder)
            return ToolResult(f"Saved {arguments['name']} from {record['name']} to {saved}.",
                              jsonable({"device": record["id"], "path": str(saved), "bytes": saved.stat().st_size}))
        except PhoneIOError as error:
            raise ToolError(sentence(error) + (" " + sentence(error.fix) if error.fix else "")) from None

    def _put(self, record, arguments, call, toolset):
        path = Path(os.path.expanduser(arguments["path"]))
        if not path.is_absolute():
            raise ToolError("Pass the absolute path of the file on this Mac.")
        clipboard, app = bool(arguments.get("clipboard")), arguments.get("app")
        if clipboard == bool(app):
            raise ToolError("Pass app (the bundle ID of the app whose folder takes it) or clipboard: true, not both.")
        if clipboard:
            from ..phone_io.clipboard import read_text_file
            from ..phone_io import PhoneIOError
            try:
                text = read_text_file(path)
            except PhoneIOError as error:
                raise ToolError(sentence(error)) from None
            return toolset.tool_set_clipboard({"device": arguments["device"], "text": text}, call)
        result = self._open(record).put(app, path)
        return ToolResult(f"Put {result['name']} in {app}'s folder on {record['name']}. The Files app shows it under "
                          "On My iPhone.", jsonable({"device": record["id"], **result}))
