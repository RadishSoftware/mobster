"""What Mobster's agent gets from attached files: one context block, and the READ_ATTACHMENT and PUT_FILE tools.

**The block** (provider ``attachments``, order 30): every file the task's ``attachmentIds`` names, with its type,
size and pages or rows, and as much of its text as fits in 12,000 characters, shared fairly between the files;
images go to the model as pictures (at most 4, 1,024 px). The block is *untrusted*: the frontier fences it as data
from a file, never instructions (harness_api.fence), and Mobster still asks before anything is sent, bought, posted
or deleted, whatever a file says.

**READ_ATTACHMENT** reads more: a PDF page, or the next 6,000 characters of a text or CSV file; the text comes
back fenced. **PUT_FILE** puts an attached file on the iPhone: in an app's folder (Files › On My iPhone, over USB)
or, for a text file, on the clipboard. Its ``prepare`` returns the question ("Put “menu.pdf” in Pages on Sam's
iPhone?"), so the frontier's own approval path asks first; with no way to ask, it never copies. Both tools are
offered only to a run that has attachments, so every other run's prompt is unchanged.
"""

import re
import threading
import time

from .. import harness_api
from ..agent_hooks import Prepared, SkillResult
from . import sniff
from .extract import pages_of

MAX_CHARS = 12_000
READ_CHARS = 6_000
MIN_SHARE = 800
MAX_IMAGES = 4
TITLE = "Files the user attached to this task"
CLIPBOARD_MAX = 64 * 1024
# The approval title's parts: with "Put “…” in … on …?" they stay within the 160 characters the card shows.
NAME_CHARS, APP_CHARS, DEVICE_CHARS = 48, 40, 40


def _defang(text):
    """File text can't close the fence it sits in."""
    return str(text or "").replace("<<<", "‹‹‹").replace(">>>", "›››")


def _short(text, limit):
    """``text`` at most ``limit`` characters, cut in the middle so a file keeps its extension ("Luigi’s…menu.pdf").
    The approval card shows a title of at most 160 characters: a long file name must never push out where it goes."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    stem, dot, ext = text.rpartition(".")
    tail = f".{ext}" if dot and stem and len(ext) <= 8 else ""
    head = text[:len(text) - len(tail)] if tail else text
    keep = limit - len(tail) - 1
    return head[:max(1, keep)].rstrip() + "…" + tail


def describe(attachment):
    """"menu.pdf: PDF, 2 pages, 412 KB"."""
    data = attachment.data
    parts = [data.get("label") or attachment.kind or "file"]
    if data.get("pages"):
        parts.append(f"{data['pages']} page" + ("" if data["pages"] == 1 else "s"))
    if data.get("rows") is not None:
        parts.append(f"{data['rows']:,} row" + ("" if data["rows"] == 1 else "s"))
    if data.get("width") and data.get("height"):
        parts.append(f"{data['width']} × {data['height']}")
    parts.append(sniff.size_words(data.get("bytes")))
    return f"{attachment.name}: " + ", ".join(parts)


def chunks(attachment, text):
    """The pages READ_ATTACHMENT serves: a PDF's own pages, else 6,000-character parts of the text."""
    if attachment.kind == "pdf":
        return pages_of(text)
    if not text:
        return [""]
    return [text[i:i + READ_CHARS] for i in range(0, len(text), READ_CHARS)]


def find(attachments, wanted):
    """The attachment ``wanted`` names: its exact name (any case), else the one name containing it, else the only
    file there is. None when it names none or several."""
    wanted = " ".join(str(wanted or "").strip().strip("\"'“”‘’").split()).casefold()
    if not attachments:
        return None
    exact = [a for a in attachments if a.name.casefold() == wanted]
    if exact:
        return exact[0]
    partial = [a for a in attachments if wanted and (wanted in a.name.casefold() or a.name.casefold() in wanted)]
    if len(partial) == 1:
        return partial[0]
    if len(attachments) == 1 and not partial:
        return attachments[0]
    return None


# -- the run's files ------------------------------------------------------------------------------------------------

def run_files(ctx, store):
    """The attachments this run may read: the ones its ``attachmentIds`` names that exist and belong to no other
    conversation."""
    ids = list(ctx.extras.get("attachmentIds") or ())
    if store is None or not ids:
        return []
    thread = ctx.thread_id
    return [a for a in store.many(ids) if a.thread_id is None or thread is None or a.thread_id == thread]


class AttachmentsProvider:
    """harness_api.ContextProvider ``attachments``: one stable, untrusted block for the run's files."""

    key = "attachments"
    max_chars = MAX_CHARS

    def __init__(self, store):
        self.store = store

    def blocks(self, ctx):
        files = run_files(ctx, self.store)
        if not files:
            return ()
        try:
            # This task reads them: record it, so they stay while it is in history and join its conversation.
            self.store.attach([a.id for a in files], ctx.run_id, ctx.thread_id)
        except Exception:  # noqa: BLE001 -- the block matters more than the bookkeeping
            harness_api.log.exception("could not record which task read its files")
        texts = {a.id: self.store.text(a) for a in files}
        headers = [f"{index}. {describe(a)}" for index, a in enumerate(files, 1)]
        room = max(MIN_SHARE * len(files), MAX_CHARS - sum(len(h) + 80 for h in headers))
        shares = _shares([len(texts[a.id]) for a in files], room)
        parts, images = [], []
        for index, (attachment, header, share) in enumerate(zip(files, headers, shares), 1):
            body = self._excerpt(attachment, texts[attachment.id], share, images)
            parts.append(header + ("\n" + body if body else ""))
            try:
                ctx.emit({"event": "attachment_read", "id": attachment.id, "name": attachment.name,
                          **({"pages": attachment.data["pages"]} if attachment.data.get("pages") else {})})
            except Exception:  # noqa: BLE001 -- an event never stops the block
                pass
        text = _defang("\n\n".join(parts))
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS - 1] + "…"
        return (harness_api.ContextBlock(self.key, TITLE, text, stable=True, untrusted=True,
                                         images=tuple(images[:MAX_IMAGES])),)

    def turn_blocks(self, ctx, turn):
        return ()

    def _excerpt(self, attachment, text, share, images):
        name = attachment.name
        if attachment.kind == "image":
            lines = []
            model = self.store.path(attachment, "model")
            if model is not None and len(images) < MAX_IMAGES:
                images.append(("image/jpeg", model.read_bytes()))
                lines.append(f"Shown to you as picture {len(images)} above.")
            else:
                lines.append("Not shown to you as a picture." if model is None
                             else f"Not shown: at most {MAX_IMAGES} pictures go with a task.")
            if text.strip():
                lines.append("Text in the picture:\n" + _cut(text.strip(), share))
            return "\n".join(lines)
        if not attachment.data.get("textAvailable") and not text.strip():
            return ("Mobster couldn't read text from this file on this Mac."
                    if attachment.kind == "pdf" else "It holds no text.")
        if attachment.kind == "pdf":
            pages = pages_of(text)
            out, used, shown = [], 0, 0
            for number, page in enumerate(pages, 1):
                page = page.strip()
                block = f"Page {number}:\n{page or '(no text on this page)'}"
                if used + len(block) > share and shown:
                    break
                if used + len(block) > share:
                    block = _cut(block, share - used)
                out.append(block)
                used += len(block) + 2
                shown = number
            if shown < len(pages):
                out.append(f"[Pages {shown + 1}–{len(pages)} not shown: READ_ATTACHMENT \"{name} page {shown + 1}\"]")
            elif out and out[-1].endswith("…"):
                out.append(f"[Page {shown} cut short: READ_ATTACHMENT \"{name} page {shown}\" reads it all]")
            return "\n\n".join(out)
        if len(text) <= share:
            return text.strip()
        parts = chunks(attachment, text)
        shown = _cut(text, share)
        next_page = min(len(parts), len(shown) // READ_CHARS + 1)
        note = (f"[{len(text) - len(shown) + 1:,} more characters: READ_ATTACHMENT \"{name} page {next_page}\" "
                f"(page {next_page} of {len(parts)})]")
        return shown + "\n" + note


def _cut(text, limit):
    limit = max(1, int(limit))
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _shares(lengths, room):
    """Each file's characters: an equal share, with what short files don't need handed to the long ones."""
    if not lengths:
        return []
    shares = [0] * len(lengths)
    left, open_ = room, list(range(len(lengths)))
    while open_ and left > 0:
        fair = left // len(open_)
        done = [i for i in open_ if lengths[i] - shares[i] <= fair]
        if not done:
            for i in open_:
                shares[i] += fair
            break
        for i in done:
            left -= lengths[i] - shares[i]
            shares[i] = lengths[i]
        open_ = [i for i in open_ if i not in done]
    return [max(MIN_SHARE, s) for s in shares]


# -- READ_ATTACHMENT ------------------------------------------------------------------------------------------------

READ_TEXT = re.compile(r"^\s*(?P<name>.+?)(?:\s*(?:,|#|\bp\.?|\bpage|\bpart)\s*(?P<page>\d{1,4}))?\s*$", re.I)


class ReadAttachment:
    """agent_hooks.Skill READ_ATTACHMENT: a page of an attached file, fenced as data."""

    op = "READ_ATTACHMENT"
    prompt = ("- READ_ATTACHMENT text: read more of a file the user attached; text is its name, then \"page N\" for "
              "a PDF's page or a later part of a long text (\"menu.pdf page 2\"). The text comes back next turn.")
    needs_target = False

    def __init__(self, store, ctx):
        self.store, self.ctx = store, ctx

    def available(self, ctx):
        return True

    def prepare(self, ctx, act, snapshot):
        return Prepared(None)

    def perform(self, ctx, prepared, act, snapshot):
        files = run_files(self.ctx, self.store)
        found = READ_TEXT.match(str(act.get("text") or ""))
        attachment = find(files, found.group("name") if found else "")
        if attachment is None:
            names = ", ".join(f"“{a.name}”" for a in files) or "none"
            return SkillResult(feedback=f"READ_ATTACHMENT needs one file's name. The attached files: {names}.")
        parts = chunks(attachment, self.store.text(attachment))
        page = int(found.group("page")) if found and found.group("page") else 1
        if not 1 <= page <= len(parts):
            return SkillResult(feedback=f"“{attachment.name}” has {len(parts)} page"
                                        f"{'' if len(parts) == 1 else 's'}; ask for page 1 to {len(parts)}.")
        text = parts[page - 1].strip() or "(no text on this page)"
        if len(text) > READ_CHARS:
            text = text[:READ_CHARS - 1] + "…"
        try:
            self.ctx.emit({"event": "attachment_read", "id": attachment.id, "name": attachment.name,
                           "page": page, **({"pages": len(parts)} if attachment.kind == "pdf" else {})})
        except Exception:  # noqa: BLE001
            pass
        title = _defang(f"{attachment.name}, page {page} of {len(parts)}")
        more = f"\n(Next: READ_ATTACHMENT \"{attachment.name} page {page + 1}\".)" if page < len(parts) else ""
        return SkillResult(feedback=harness_api.fence(title, _defang(text)) + more, changed=False)


# -- PUT_FILE -------------------------------------------------------------------------------------------------------

PUT_TEXT = re.compile(r"^\s*(?P<name>.+?)\s*(?:>|->|→|\bto\b|\binto\b|\bin\b)\s*(?P<dest>[A-Za-z0-9:._-]+)\s*$", re.I)
_apps_cache = {}
_apps_lock = threading.Lock()
APPS_TTL = 60.0


def file_sharing_apps(phone_files):
    """The phone's apps that share files, cached a minute per iPhone."""
    key = phone_files.udid
    with _apps_lock:
        at, apps = _apps_cache.get(key, (0.0, None))
    if apps is not None and time.monotonic() - at < APPS_TTL:
        return apps
    apps = phone_files.apps()
    with _apps_lock:
        _apps_cache[key] = (time.monotonic(), apps)
    return apps


class _Plan:
    def __init__(self, refusal=None, attachment=None, destination=None, app_name=None, phone=None):
        self.refusal, self.attachment, self.destination = refusal, attachment, destination
        self.app_name, self.phone = app_name, phone


class PutFile:
    """agent_hooks.Skill PUT_FILE: an attached file into an app's folder or onto the clipboard, after the user's
    OK (its ``prepare`` returns the question, so the frontier asks; with no way to ask, the copy never runs)."""

    op = "PUT_FILE"
    prompt = ("- PUT_FILE text: put a file the user attached on the iPhone, only when the task needs it there; text "
              "is the file's name, \" > \", then clipboard (a text file, to paste) or the bundle id of an app whose "
              "folder shows in Files (\"menu.pdf > com.apple.Pages\"). Mobster asks the user first.")
    needs_target = False

    def __init__(self, store, ctx, phone_files=None):
        self.store, self.ctx = store, ctx
        self._phone_files = phone_files  # tests: record -> phone_io.files.PhoneFiles

    def available(self, ctx):
        return True

    def _device_name(self):
        try:
            from .service import device_record
            return _short(device_record(self.ctx.runtime, self.ctx.device_id)["name"], DEVICE_CHARS)
        except Exception:  # noqa: BLE001
            return "your iPhone"

    def prepare(self, ctx, act, snapshot):
        found = PUT_TEXT.match(str(act.get("text") or ""))
        files = run_files(self.ctx, self.store)
        if not found:
            return Prepared(None, _Plan("PUT_FILE needs the file's name, \" > \", then clipboard or an app's bundle "
                                        "id, such as \"menu.pdf > com.apple.Pages\"."))
        attachment = find(files, found.group("name"))
        if attachment is None:
            names = ", ".join(f"“{a.name}”" for a in files) or "none"
            return Prepared(None, _Plan(f"PUT_FILE found no attached file by that name. The attached files: {names}."))
        destination = found.group("dest")
        destination = destination[4:] if destination.lower().startswith("app:") else destination
        device = self._device_name()
        if destination.lower() == "clipboard":
            if attachment.kind not in ("text", "csv"):
                return Prepared(None, _Plan(f"Only a text file goes on the clipboard; “{attachment.name}” is "
                                            f"{attachment.data.get('label') or attachment.kind}. Put it in an app's "
                                            "folder instead, or type what is needed."))
            if (attachment.data.get("bytes") or 0) > CLIPBOARD_MAX:
                return Prepared(None, _Plan(f"“{attachment.name}” is over 64 KB, too long for the clipboard."))
            return Prepared(f"Put the text of “{_short(attachment.name, NAME_CHARS)}” on {device}'s clipboard?",
                            _Plan(attachment=attachment, destination="clipboard"))
        if not self.ctx.interactive and not harness_api.unattended_allowed(destination):
            return Prepared(None, _Plan("Mobster doesn't put files in social or dating apps unless you ask it "
                                        "yourself in the Mobster app."))
        try:
            from .service import device_record
            record = device_record(self.ctx.runtime, self.ctx.device_id)
            phone = self._open(record)
            apps = file_sharing_apps(phone)
        except Exception as error:  # noqa: BLE001 -- a phone that can't take files is a sentence for the model
            return Prepared(None, _Plan(f"PUT_FILE can't reach the iPhone's files: {_sentence(error)}"))
        app = next((a for a in apps if a["bundleId"] == destination), None)
        if app is None:
            names = ", ".join(f"{a['name']} ({a['bundleId']})" for a in apps[:12]) or "none on this iPhone"
            return Prepared(None, _Plan(f"{destination} doesn't keep files you can see in Files. Apps that do: "
                                        f"{names}."))
        title = f"Put “{_short(attachment.name, NAME_CHARS)}” in {_short(app['name'], APP_CHARS)} on {device}?"
        return Prepared(title, _Plan(attachment=attachment, destination=destination, app_name=app["name"], phone=phone))

    def _open(self, record):
        if self._phone_files is not None:
            return self._phone_files(record)
        from .service import phone_files
        return phone_files(record)

    def perform(self, ctx, prepared, act, snapshot):
        plan = prepared.state if isinstance(prepared.state, _Plan) else _Plan("PUT_FILE didn't run.")
        if plan.refusal or plan.attachment is None:
            return SkillResult(feedback=plan.refusal or "PUT_FILE didn't run.")
        attachment = plan.attachment
        size = attachment.data.get("bytes") or 0
        if plan.destination == "clipboard":
            from ..phone_io.clipboard import set_text
            driver = ctx.driver
            text = self.store.whole_text(attachment)
            if text is None:
                return SkillResult(feedback=f"“{attachment.name}” is no longer on this Mac.")
            try:
                set_text(lambda method, path, body, timeout: driver.call(method, path, body, timeout=timeout), text)
            except Exception as error:  # noqa: BLE001
                self._event(attachment, size, "clipboard", "failed")
                return SkillResult(feedback=f"PUT_FILE couldn't set the clipboard: {_sentence(error)}")
            self._event(attachment, size, "clipboard", "copied")
            return SkillResult(feedback=f"The text of “{attachment.name}” is on the clipboard now: paste it with a "
                                        "long press in a field, then Paste.", changed=False)
        original = self.store.path(attachment, "original")
        if original is None:
            return SkillResult(feedback=f"“{attachment.name}” is no longer on this Mac.")
        try:
            result = plan.phone.put(plan.destination, original, name=attachment.name)
        except Exception as error:  # noqa: BLE001
            self._event(attachment, size, f"app:{plan.destination}", "failed")
            return SkillResult(feedback=f"PUT_FILE couldn't copy “{attachment.name}”: {_sentence(error)}")
        self._event(attachment, size, f"app:{plan.destination}", "copied")
        renamed = f" (as “{result['name']}”, since that name was taken)" if result["name"] != attachment.name else ""
        return SkillResult(feedback=f"Put “{attachment.name}” in {plan.app_name}{renamed}. In the Files app it is "
                                    f"under On My iPhone › {plan.app_name}, and {plan.app_name} can open it.",
                           changed=False)

    def _event(self, attachment, size, destination, status):
        try:
            self.ctx.emit({"event": "file_put", "name": attachment.name, "bytes": size, "destination": destination,
                           "status": status})
        except Exception:  # noqa: BLE001
            pass


def _sentence(error):
    text = " ".join(str(error).split())[:240] or type(error).__name__
    fix = getattr(error, "fix", "")
    return (text if text.endswith((".", "?", "!")) else text + ".") + (f" {fix}" if fix else "")
