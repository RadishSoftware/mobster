"""The conversation each terminal used last, per Mobster address: `mobster chat` continues it, completion lists it."""

import json
import os


def path():
    from ..paths import user_data_dir
    return user_data_dir() / "state" / "chat-threads.json"


def last_thread(url):
    try:
        value = json.loads(path().read_text()).get(url)
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and len(value) == 12 else None


def remember_thread(url, thread_id):
    file = path()
    try:
        saved = json.loads(file.read_text()) if file.is_file() else {}
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, ValueError):
        saved = {}
    saved[url] = thread_id
    try:
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w") as stream:
            stream.write(json.dumps(saved))
    except OSError:
        pass
