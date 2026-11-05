"""Screens a run points at, kept beside the journal."""

import io
import os
import re
import shutil
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..paths import write_private

FRAME_ID = re.compile(r"f[0-9a-f]{10}")
RUN_ID = re.compile(r"[a-f0-9]{12}")


class FrameStore:
    """Screens a run points at (steps, receipts, proof), as JPEGs no more than MAX_SIDE px on their long
    side. A frame is kept in memory at once and downscaled, then written beside the journal, on one worker
    thread: the agent never waits for an image. Without durable history frames live in memory only."""

    MAX_SIDE = 480
    MAX_PER_RUN = 400
    MAX_MEMORY_RUNS = 4

    def __init__(self, root=None):
        self.root = Path(root) if root else None
        self.memory = OrderedDict()  # run id -> {frame id: bytes (raw until processed)}
        self.lock = threading.Lock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mobster-frames")

    def put(self, run_id, data):
        """Keep ``data`` (JPEG bytes) for ``run_id``; returns its frame id, or None when the run is full."""
        frame_id = "f" + uuid.uuid4().hex[:10]
        with self.lock:
            frames = self.memory.setdefault(run_id, {})
            self.memory.move_to_end(run_id)
            if len(frames) >= self.MAX_PER_RUN:
                return None
            frames[frame_id] = bytes(data)
            # Older runs' frames are on disk (or, without durable history, gone with the oldest runs).
            while len(self.memory) > (self.MAX_MEMORY_RUNS if self.root is not None else 20):
                self.memory.popitem(last=False)
        self.worker.submit(self._process, run_id, frame_id)
        return frame_id

    @classmethod
    def downscale(cls, data):
        try:
            from PIL import Image
            image = Image.open(io.BytesIO(data))
            if max(image.size) <= cls.MAX_SIDE and image.format == "JPEG":
                return data
            image = image.convert("RGB")
            image.thumbnail((cls.MAX_SIDE, cls.MAX_SIDE))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=72)
            return out.getvalue()
        except Exception:
            return None

    def _process(self, run_id, frame_id):
        with self.lock:
            raw = self.memory.get(run_id, {}).get(frame_id)
        small = self.downscale(raw) if raw is not None else None
        if small is None:
            with self.lock:
                self.memory.get(run_id, {}).pop(frame_id, None)
            return
        with self.lock:
            if frame_id in self.memory.get(run_id, {}):
                self.memory[run_id][frame_id] = small
        if self.root is not None:
            try:
                # frames/ itself is 0700 too: mkdir's mode covers only the folder it names, not the parents it makes.
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(self.root, 0o700)
                folder = self.root / run_id
                folder.mkdir(exist_ok=True, mode=0o700)
                write_private(folder / f"{frame_id}.jpg", small, binary=True)
            except OSError:
                pass

    def get(self, run_id, frame_id):
        if not FRAME_ID.fullmatch(frame_id or ""):
            return None
        with self.lock:
            data = self.memory.get(run_id, {}).get(frame_id)
        if data is not None:
            small = self.downscale(data)
            return small
        if self.root is not None:
            try:
                return (self.root / run_id / f"{frame_id}.jpg").read_bytes()
            except OSError:
                return None
        return None

    def folder(self, run_id):
        """Where a run's files are kept (its frames, its tape), or None without durable history or for an id that
        is not a run's."""
        return self.root / run_id if self.root is not None and RUN_ID.fullmatch(run_id or "") else None

    def delete(self, run_id):
        with self.lock:
            self.memory.pop(run_id, None)
        folder = self.folder(run_id)
        if folder is not None:
            # An in-flight downscale may still write its frame. Remove the directory after that worker finishes.
            self.worker.submit(shutil.rmtree, folder, ignore_errors=True).result()

    def close(self):
        self.worker.shutdown(wait=True, cancel_futures=False)
