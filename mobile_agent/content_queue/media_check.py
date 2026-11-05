"""Whether the post about to go out shows the media the queue sent (MOBSTER_QUEUE_PREFLIGHT).

The agent picks a queued post's media in the app's own picker, "the newest N" by eye. This is the code check behind it,
made at the Post, on the Mac:

- ``stop``: the app shows its limited-photos notice (the pack's ``limits_photos``), so the picker could not offer the
  media: the post ends as Didn't post, and the person widens that app's Photos access themselves. Mobster never changes
  an app's access.
- ``preview``: a picture of the screen at the Post holds one of the post's files. The batch approval answers the Post
  card only when it does (runner.preapproval_matches); any other result leaves the card for the person, so a wrong,
  older or personal photo is never posted on a batch approval.

The match looks for a window of the screen that has a file's first frame's proportions (a video's, from Quick Look),
or a square, portrait or tall crop of it: a coarse pass picks the places whose layout of colours is near, and a fine
pass compares a 16 by 16 difference hash there. It needs the screen's picture and a decodable file, and anything else
is no match. It is a similarity test, not proof: it was
measured only on synthetic screens and on the Photos app in a simulator (docs/content-queue.mdx), not yet on Instagram's
or TikTok's composer on a real phone.
"""

import io

POST_ACTS = frozenset({"post", "publish", "share"})   # the acts a queued post's Post card can carry
HASH = 16
# Of 256 bits, how many may differ: a file shown scaled, recompressed or lightly covered by the app's controls stays
# well under it; two different photos sit near 128.
MAX_DISTANCE = 56
# A first frame with fewer set (or unset) bits than this is flat (black, white, a gradient): a window can't tell it from
# another flat one, so it is not checked.
MIN_DETAIL = 40
GRID = 4                                # the coarse layout: GRID by GRID mean colours
MAX_COARSE = 30                         # of 255, the mean difference per colour value a window may have to be tried
SCALES = (.3, .4, .5, .6, .7, .8, .9, 1.0)
CROPS = (None, 1.0, 4 / 5, 9 / 16)      # the media as is, then the square, portrait and tall crops apps make
CANDIDATES = 8                          # places the coarse pass hands to the fine one, per proportion
FINE_SIZES = (.92, 1.0, 1.08)
SCREEN_SIDE = 480


def dhash(image):
    """The difference hash of a PIL image: HASH rows of HASH bits, each set when a pixel is brighter than its right
    neighbour, as one integer."""
    pixels = image.convert("L").resize((HASH + 1, HASH)).tobytes()
    bits = 0
    for row in range(HASH):
        for column in range(HASH):
            bits = bits << 1 | (pixels[row * (HASH + 1) + column] > pixels[row * (HASH + 1) + column + 1])
    return bits


def layout(image):
    """The image as GRID by GRID mean colours: a picture that holds still under a few pixels' shift."""
    return image.convert("RGB").resize((GRID, GRID), 3).tobytes()   # 3: Image.BOX


def layout_distance(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def cropped(image, ratio):
    """``image`` centre-cropped to width / height ``ratio`` (the image as is for None)."""
    if ratio is None:
        return image
    width, height = image.size
    if width / height > ratio:
        keep = round(height * ratio)
        return image.crop(((width - keep) // 2, 0, (width - keep) // 2 + keep, height))
    keep = round(width / ratio)
    return image.crop((0, (height - keep) // 2, width, (height - keep) // 2 + keep))


def first_frames(item):
    """The first frame of each of the item's files as a PIL image (a photo, or a video's first frame); a file that
    can't be read is left out."""
    from PIL import Image

    from . import client
    frames = []
    for media in item.get("media") or ():
        got = client._still(media, 480)
        if got is not None:
            frames.append(Image.open(io.BytesIO(got[0])).convert("RGB"))
    return frames


def references(frames):
    """[(width / height, dhash, layout)] for each frame at each crop that has enough detail to be told apart."""
    out = []
    for frame in frames:
        for ratio in CROPS:
            part = cropped(frame, ratio)
            value = dhash(part)
            if value.bit_count() >= MIN_DETAIL and 256 - value.bit_count() >= MIN_DETAIL:
                out.append((round(part.width / part.height, 3), value, layout(part)))
    return out


def boxes(width, height, ratio):
    """Boxes (left, top, right, bottom) of proportions ``ratio`` at SCALES of the screen's width, a twelfth of a box
    apart: the coarse pass's positions."""
    for scale in SCALES:
        w = width * scale
        h = w / ratio
        if h > height:
            h = height
            w = h * ratio
        step = max(2.0, w / (GRID * 3))
        top = 0.0
        while True:
            left = 0.0
            while True:
                yield round(left), round(top), round(left + w), round(top + h)
                if left + w >= width - .5:
                    break
                left = min(left + step, width - w)
            if top + h >= height - .5:
                break
            top = min(top + step, height - h)


def candidates(screen, ratio, wanted):
    """The best-placed, apart-from-each-other boxes of proportions ``ratio`` whose layout is near one of the ``wanted``
    layouts (CANDIDATES at most), best first."""
    scored = []
    for box in boxes(*screen.size, ratio):
        if box[2] - box[0] < 24 or box[3] - box[1] < 24:
            continue
        near = layout(screen.crop(box))
        best = min(layout_distance(near, one) for one in wanted)
        if best <= MAX_COARSE:
            scored.append((best, box))
    scored.sort()
    kept = []
    for _, box in scored:
        centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        if all(abs(centre[0] - (k[0] + k[2]) / 2) > (box[2] - box[0]) / 4
               or abs(centre[1] - (k[1] + k[3]) / 2) > (box[3] - box[1]) / 4 for k in kept):
            kept.append(box)
        if len(kept) == CANDIDATES:
            break
    return kept


def fine(screen, box, wanted):
    """Whether a window near ``box`` (shifted by up to a coarse step, 8% bigger or smaller) hashes within
    MAX_DISTANCE of one of the ``wanted`` hashes."""
    width, height = screen.size
    w, h = box[2] - box[0], box[3] - box[1]
    step = max(1.0, w / (GRID * 3 * 3))
    reach = round(w / (GRID * 3) / step)
    for size in FINE_SIZES:
        sw, sh = w * size, h * size
        for dy in range(-reach, reach + 1):
            for dx in range(-reach, reach + 1):
                left = box[0] + (w - sw) / 2 + dx * step
                top = box[1] + (h - sh) / 2 + dy * step
                window = (round(left), round(top), round(left + sw), round(top + sh))
                if window[0] < 0 or window[1] < 0 or window[2] > width or window[3] > height:
                    continue
                value = dhash(screen.crop(window))
                if any((value ^ one).bit_count() <= MAX_DISTANCE for one in wanted):
                    return True
    return False


def screen_holds(screen, refs):
    """Whether the PIL image ``screen`` holds one of the references: a coarse pass picks the places whose layout is
    near, a fine pass compares their hashes."""
    if not refs:
        return False
    screen = screen.convert("RGB")
    screen.thumbnail((SCREEN_SIDE, SCREEN_SIDE * 3))
    for ratio in sorted({r for r, _, _ in refs}):
        same = [(value, near) for r, value, near in refs if r == ratio]
        for box in candidates(screen, ratio, [near for _, near in same]):
            if fine(screen, box, [value for value, _ in same]):
                return True
    return False


class MediaCheck:
    """One queued post's check at its Post (accounts.screen.AccountCheck.media). ``pack`` is the profile's app pack
    (None for an app with none)."""

    POST_ACTS = POST_ACTS

    def __init__(self, item, pack=None, frames=first_frames):
        self.item, self.pack, self._frames = item, pack, frames
        self._refs = None

    def stop(self, snapshot):
        """The commit check's stop verdict when the app shows its limited-photos notice, else None."""
        if self.pack is not None and self.pack.limits_photos(snapshot):
            return {"stop": f"{self.pack.name} can see only some of your photos, so Mobster can't be sure it "
                            f"picked this post's media. Nothing was posted. Give {self.pack.name} access to all your "
                            "photos in Settings, then run the post again.", "code": "photos_limited"}
        return None

    def preview(self, screen):
        """Whether the PIL image of the screen at the Post holds one of the post's files. False on any failure."""
        try:
            if self._refs is None:
                self._refs = references(self._frames(self.item))
            return screen_holds(screen, self._refs)
        except Exception:  # noqa: BLE001 - a check that fails is no match, and the person decides
            return False


def for_run(run_context):
    """The run's MediaCheck when it is a queued post's task and the queue checks are on, else None."""
    from .. import phone_ready
    extras = getattr(run_context, "extras", None) or {}
    queued = extras.get("queueItem")
    if not phone_ready.enabled() or not isinstance(queued, dict):
        return None
    try:
        from ..packs.register import all_packs
        from .store import store_for
        queue = store_for(run_context.runtime).get(queued["queue"])
        item = next(i for i in queue["items"] if i["id"] == queued["item"])
        bundle = (extras.get("account") or {}).get("app", {}).get("bundleId")
        return MediaCheck(item, next((p for p in all_packs() if p.bundle == bundle), None))
    except Exception:  # noqa: BLE001 - no item to check against: no check, and the Post card asks the person
        return None
