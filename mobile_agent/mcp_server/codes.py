"""The sign-in codes ``use_code`` typed: masked in every result and blacked out of screenshots while they show."""

import copy
import dataclasses
import io
import time
from pathlib import Path

from .session import overlap

SECURE_MASK = "••••"
# How long a code use_code typed stays masked in every result, beyond the session that typed it: a session closed
# by stop or the 90-second idle limit and opened again still reads the field, and a check after it still might.
CODE_MEMORY_SECONDS = 3600


class CodeVault:
    def __init__(self, lock, sessions, quality=70):
        self._lock, self._sessions, self._quality = lock, sessions, quality
        self._codes = {}

    def all_secrets(self):
        with self._lock:
            sessions = self._sessions()
            remembered = [secret for entry in self._live().values() for secret in entry["secrets"]]
        return sorted({secret for session in sessions for secret in getattr(session, "secrets", ())}
                      | set(remembered), key=len, reverse=True)

    @staticmethod
    def _device_key(session):
        run = session.run
        return (getattr(run, "device_id", None) or (getattr(run, "record", None) or {}).get("id")
                or getattr(getattr(run, "target", None), "udid", None) or session.run_id)

    def _live(self):
        now = time.monotonic()
        self._codes = {key: entry for key, entry in self._codes.items() if now - entry["at"] < CODE_MEMORY_SECONDS}
        return self._codes

    def remember(self, session, code, rect):
        session.add_secret(code, rect)
        with self._lock:
            entry = self._live().setdefault(self._device_key(session), {"secrets": [], "rects": []})
            if code not in entry["secrets"]:
                entry["secrets"].append(code)
            if rect is not None and tuple(rect) not in entry["rects"]:
                entry["rects"].append(tuple(rect))
            entry["at"] = time.monotonic()

    def seed(self, session, device):
        with self._lock:
            entry = self._live().get(device)
            if entry is None:
                return
            for secret in entry["secrets"]:
                session.add_secret(secret)
            session.secret_rects.extend(rect for rect in entry["rects"] if rect not in session.secret_rects)

    def forget_rects(self, session):
        with self._lock:
            entry = self._codes.get(self._device_key(session))
            if entry is not None:
                entry["rects"] = []

    def mask(self, result):
        secrets = self.all_secrets()
        if not secrets or result is None:
            return result

        def scrub(value):
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, SECURE_MASK)
                return value
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [scrub(item) for item in value]
            return value
        result.text = scrub(result.text)
        result.structured = scrub(result.structured)
        return result

    def read_tree(self, session):
        tree = session.run.read_tree()
        if not getattr(session, "secret_rects", None):
            return tree
        if not session.secret_shown(tree):
            self.forget_rects(session)
            return tree
        width, height = getattr(tree, "size", (0, 0)) or (0, 0)
        nodes = []
        for node in tree.nodes:
            value, label = getattr(node, "value", "") or "", getattr(node, "label", "") or ""
            for secret in session.secrets:
                value, label = value.replace(secret, SECURE_MASK), label.replace(secret, SECURE_MASK)
            if value and width and height and getattr(node, "role", "") in ("TextField", "SecureTextField"):
                x, y, w, h = node.rect
                box = (x / width, y / height, w / width, h / height)
                if any(overlap(box, rect) for rect in session.secret_rects):
                    value = SECURE_MASK
            if value != (getattr(node, "value", "") or "") or label != (getattr(node, "label", "") or ""):
                node = dataclasses.replace(node, value=value, label=label)
            nodes.append(node)
        masked = copy.copy(tree)
        masked.nodes = tuple(nodes)
        if hasattr(masked, "_shown"):
            masked._shown = None
        return masked

    def redact_bytes(self, session, data, rects=None):
        rects = list(rects if rects is not None else getattr(session, "secret_rects", ()) or ())
        if not rects:
            return data
        try:
            from PIL import Image, ImageDraw
            with Image.open(io.BytesIO(data)) as image:
                image = image.convert("RGB")
                draw = ImageDraw.Draw(image)
                for x, y, w, h in rects:
                    pad = 4
                    draw.rectangle([x * image.width - pad, y * image.height - pad, (x + w) * image.width + pad,
                                    (y + h) * image.height + pad], fill=(0, 0, 0))
                out = io.BytesIO()
                image.save(out, "JPEG", quality=self._quality)
            return out.getvalue()
        except Exception:
            # An image that can't be redacted is not shown at all.
            return b""

    def redact_file(self, session, path, rects=None):
        rects = list(rects if rects is not None else getattr(session, "secret_rects", ()) or ())
        if not rects:
            return True
        try:
            data = Path(path).read_bytes()
            redacted = self.redact_bytes(session, data, rects)
            if redacted:
                Path(path).write_bytes(redacted)
                return True
        except OSError:
            pass
        try:
            Path(path).unlink()
        except OSError:
            pass
        return False
