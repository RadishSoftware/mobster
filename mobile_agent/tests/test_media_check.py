"""The queue's media check at the Post (content_queue/media_check.py): a screen that holds the post's file is a match, a
screen that holds another photo or none is not, the limited-photos notice ends the task, and the batch approval answers
the Post card only after a match. Synthetic screens and files: no phone, no model. How well it tells real composers
apart is not measured here (docs/content-queue.mdx says so)."""

import os
import random
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from mobile_agent import phone_ready
from mobile_agent.accounts import screen as account_screen
from mobile_agent.content_queue import media_check
from mobile_agent.content_queue.runner import preapproval, preapproval_matches
from mobile_agent.packs import instagram, tiktok
from mobile_agent.state import Element
from mobile_agent.tests.packs_support import el, snap
from mobile_agent.tests.test_accounts import INSTAGRAM
from mobile_agent.tests.test_accounts import screen as account_snapshot

ON = {phone_ready.ENV: "1"}


def photo(seed, size=(600, 800)):
    """A smooth, photo-like picture that is different for each seed: coarse colour fields with a few shapes."""
    rng = random.Random(seed)
    base = Image.new("RGB", (6, 8))
    base.putdata([tuple(rng.randrange(256) for _ in range(3)) for _ in range(48)])
    image = base.resize(size, Image.BICUBIC)
    draw = ImageDraw.Draw(image)
    for _ in range(5):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        r = rng.randrange(30, 140)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=tuple(rng.randrange(256) for _ in range(3)))
    return image


def composer(preview, *, at=(60, 180), width=270, chrome=True, cover=None):
    """A 390 by 844 screen with ``preview`` drawn at ``at``, ``width`` wide, among app chrome."""
    screen = Image.new("RGB", (390, 844), (250, 250, 250))
    draw = ImageDraw.Draw(screen)
    if chrome:
        draw.rectangle((0, 0, 390, 90), fill=(30, 30, 30))
        draw.rectangle((0, 700, 390, 844), fill=(240, 240, 240))
        draw.text((20, 40), "New post", fill=(255, 255, 255))
        draw.text((20, 740), "Write a caption...", fill=(10, 10, 10))
    shown = preview.resize((width, round(width * preview.height / preview.width)))
    screen.paste(shown, at)
    if cover:
        draw.rectangle(cover, fill=(0, 0, 0))
    return screen


def item_with(*frames):
    return {"id": "i" * 12,
            "media": [{"path": f"/x/{n}.jpg", "kind": "photo", "sha256": str(n) * 64, "name": f"{n}.jpg"}
                      for n, _ in enumerate(frames)]}


def check_of(*frames, pack=None):
    return media_check.MediaCheck(item_with(*frames), pack, frames=lambda item: list(frames))


class PreviewTests(unittest.TestCase):
    def test_a_screen_holding_the_file_matches_however_it_is_scaled_placed_or_recompressed(self):
        mine = photo(1)
        for case, screen in (("centred", composer(mine)), ("smaller", composer(mine, at=(30, 400), width=180)),
                             ("full width", composer(mine, at=(0, 120), width=390)),
                             ("with the controls over a corner", composer(mine, cover=(60, 180, 130, 215)))):
            with self.subTest(case=case):
                self.assertTrue(check_of(mine).preview(screen), case)

    def test_the_square_crop_an_app_makes_of_a_tall_photo_still_matches(self):
        mine = photo(2)
        square = media_check.cropped(mine, 1.0)
        self.assertTrue(check_of(mine).preview(composer(square, width=300, at=(45, 200))))

    def test_another_photo_a_blank_composer_or_a_flat_file_is_no_match(self):
        mine = photo(1)
        self.assertFalse(check_of(mine).preview(composer(photo(2))))
        self.assertFalse(check_of(mine).preview(composer(photo(3))))
        self.assertFalse(check_of(mine).preview(composer(Image.new("RGB", (300, 400), (250, 250, 250)))))
        flat = Image.new("RGB", (300, 400), (10, 10, 10))
        self.assertFalse(check_of(flat).preview(composer(flat)))   # nothing to tell it from other flat pictures

    def test_a_carousel_matches_when_the_composer_shows_any_of_its_files(self):
        first, second = photo(4), photo(5)
        self.assertTrue(check_of(first, second).preview(composer(second)))
        self.assertFalse(check_of(first, second).preview(composer(photo(6))))

    def test_what_cant_be_checked_is_no_match_never_an_error(self):
        mine = photo(1)
        screen = composer(mine)
        self.assertFalse(media_check.MediaCheck(item_with(mine), frames=lambda item: []).preview(screen))
        self.assertFalse(media_check.MediaCheck(item_with(mine), frames=mock.Mock(side_effect=OSError)).preview(screen))
        self.assertFalse(check_of(mine).preview(None))

    def test_the_files_first_frame_is_read_from_the_queues_own_stills(self):
        mine = photo(1)
        import io
        data = io.BytesIO()
        mine.save(data, "JPEG")
        with mock.patch("mobile_agent.content_queue.client._still", return_value=(data.getvalue(), .75)) as still:
            frames = media_check.first_frames({"media": [{"path": "/x/a.jpg", "kind": "photo", "sha256": "a" * 64}]})
        self.assertEqual((len(frames), still.call_args.args[1]), (1, 480))
        with mock.patch("mobile_agent.content_queue.client._still", return_value=None):
            self.assertEqual(media_check.first_frames({"media": [{"path": "/x/a.jpg", "sha256": "a" * 64}]}), [])


class LimitedPhotosTests(unittest.TestCase):
    NOTICES = ("Instagram has access to only some of your photos and videos",
               "TikTok can see a limited selection of photos", "Allow access to all photos", "Select more photos")
    ORDINARY = ("Recents", "Next", "Select multiple", "New post", "Write a caption...", "Share",
                "Spring photos, limited run", "Photos")

    def test_each_pack_recognises_its_apps_notice_and_nothing_an_ordinary_composer_says(self):
        for pack in (instagram.PACK, tiktok.PACK):
            for notice in self.NOTICES:
                with self.subTest(pack=pack.name, notice=notice):
                    self.assertTrue(pack.limits_photos(snap(pack.bundle, [el("Next"), el(notice, "StaticText")])))
            for words in self.ORDINARY:
                with self.subTest(pack=pack.name, words=words):
                    self.assertFalse(pack.limits_photos(snap(pack.bundle, [el(words, "StaticText")])))

    def test_another_apps_screen_and_a_caption_field_are_never_the_notice(self):
        self.assertFalse(instagram.PACK.limits_photos(snap("com.example.other", [el(self.NOTICES[0], "StaticText")])))
        caption = snap(instagram.BUNDLE, [el("", "TextView", value=self.NOTICES[0])])
        self.assertFalse(instagram.PACK.limits_photos(caption))
        typed = snap(instagram.BUNDLE, [el(self.NOTICES[0], "TextView")])
        self.assertFalse(instagram.PACK.limits_photos(typed))

    def test_the_notice_ends_the_check_with_a_sentence_that_names_the_app(self):
        check = check_of(photo(1), pack=instagram.PACK)
        stop = check.stop(snap(instagram.BUNDLE, [el(self.NOTICES[0], "StaticText")]))
        self.assertEqual(stop["code"], "photos_limited")
        self.assertIn("Instagram can see only some of your photos", stop["stop"])
        self.assertIn("Nothing was posted", stop["stop"])
        self.assertIsNone(check.stop(snap(instagram.BUNDLE, [el("Next")])))
        # no pack
        self.assertIsNone(check_of(photo(1)).stop(snap(instagram.BUNDLE, [el(self.NOTICES[0], "StaticText")])))


class CommitTests(unittest.TestCase):
    GETMOBSTER = {"id": "a" * 12, "handle": "getmobster", "app": INSTAGRAM, "deviceName": "Studio iPhone"}

    def own_profile(self):
        return account_snapshot(Element("10", "getmobster", "NavigationBar", (0, .05, 1, .06)),
                                Element("11", "Edit profile", "Button", (.1, .4, .4, .05)),
                                bundle=INSTAGRAM["bundleId"])

    def confirmed(self, media):
        check = account_screen.AccountCheck(self.GETMOBSTER, (), "Studio iPhone")
        check.media = media
        check.look(self.own_profile())
        return check

    def test_a_queued_posts_commit_carries_the_preview_check_and_other_acts_do_not(self):
        media = check_of(photo(1), pack=instagram.PACK)
        check = self.confirmed(media)
        verdict = check.commit("post", self.own_profile())
        self.assertEqual((verdict["acting_as"], verdict["preview"]), ("@getmobster on Instagram", media.preview))
        self.assertNotIn("preview", check.commit("send_message", self.own_profile()))
        self.assertNotIn("preview", self.confirmed(None).commit("post", self.own_profile()))

    def test_the_limited_photos_notice_stops_the_post_before_it_asks(self):
        check = self.confirmed(check_of(photo(1), pack=instagram.PACK))
        notice = account_snapshot(Element("10", "getmobster", "NavigationBar", (0, .05, 1, .06)),
                                  Element("11", "Edit profile", "Button", (.1, .4, .4, .05)),
                                  Element("12", "Instagram has access to only some of your photos", "StaticText",
                                          (.1, .2, .8, .05)), bundle=INSTAGRAM["bundleId"])
        verdict = check.commit("post", notice)
        self.assertEqual(verdict["code"], "photos_limited")
        self.assertNotIn("acting_as", verdict)

    def test_the_run_gets_a_check_only_for_a_queued_post_with_the_queue_checks_on(self):
        class Context:
            extras = {"queueItem": {"queue": "q" * 12, "item": "i" * 12}, "account": self.GETMOBSTER}
            runtime = None
        with mock.patch.dict(os.environ, {phone_ready.ENV: ""}):
            self.assertIsNone(media_check.for_run(Context))
        with mock.patch.dict(os.environ, ON):
            self.assertIsNone(media_check.for_run(Context))   # no queue to read the post from: no check
            self.assertIsNone(media_check.for_run(mock.Mock(extras={}, runtime=None)))


class BatchApprovalTests(unittest.TestCase):
    PROFILE = {"id": "p" * 12, "handle": "getmobster", "app": {"name": "Instagram", "bundleId": "com.burbn.instagram"}}
    POST = {"kind": "commit", "act": "post", "operation": "TAP", "acting_as": "@getmobster on Instagram",
            "app_name": "Instagram"}

    def test_with_the_checks_on_a_batch_approval_answers_the_post_only_after_a_match(self):
        with mock.patch.dict(os.environ, ON):
            spec = preapproval({"id": "item"}, self.PROFILE)
        self.assertTrue(spec["preview"])
        self.assertTrue(preapproval_matches(spec, {**self.POST, "media_checked": True}))
        for request in (self.POST, {**self.POST, "media_checked": False}, {**self.POST, "media_checked": None},
                        {**self.POST, "media_checked": "yes"}):
            with self.subTest(request=request):
                self.assertFalse(preapproval_matches(spec, request))   # the Post card waits for the person

    def test_with_the_checks_off_it_answers_as_it_always_did(self):
        with mock.patch.dict(os.environ, {phone_ready.ENV: ""}):
            spec = preapproval({"id": "item"}, self.PROFILE)
        self.assertFalse(spec["preview"])
        self.assertTrue(preapproval_matches(spec, self.POST))


if __name__ == "__main__":
    unittest.main()
