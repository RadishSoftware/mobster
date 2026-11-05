from copy import deepcopy
from dataclasses import replace

from mobile_agent.learned import programs as P
from mobile_agent.learned import taught as PTT
from mobile_agent.learned import teach as T
from mobile_agent.tests.test_teach import CANVAS, Control, Phone, Stored, replay, save, sketch_lesson, tap, taught

REQUEST = "Tap the middle of my sketch"


def canvas_with_undo_at(y):
    undo = CANVAS.elements[1]
    x, _, width, height = undo.rect
    return replace(CANVAS, elements=[CANVAS.elements[0], replace(undo, rect=(x, y, width, height))])


class TaughtPointProof(Stored):
    def assert_replays_point(self, path, snapshot, expected):
        result, driver, _, _ = replay([snapshot] * 3, P.Matched(path, {}), REQUEST)
        self.assertEqual(("TAP_POINT", .5, .6) in driver.actions, expected)
        if not expected:
            self.assertEqual(result["program"]["status"], "stopped")

    def test_teaching_keeps_geometry_from_before_mutating_dispatch(self):
        snapshot = deepcopy(CANVAS)
        before = deepcopy(snapshot)

        class MutatingPhone(Phone):
            def call(self, *args, **kwargs):
                super().call(*args, **kwargs)
                snapshot.elements[:] = canvas_with_undo_at(.8).elements

        phone, lesson = MutatingPhone([snapshot]), T.Lesson("d")
        Control(phone).perform(tap(.5, .6), teach=lesson)
        lesson.stop()
        path = save(self.store, lesson, REQUEST)
        stored = self.store.by_id(P.Programs.id_of(path))
        self.assertEqual(stored["steps"][1]["screen"]["fingerprint"], before.content_fingerprint)
        self.assertNotEqual(snapshot.content_fingerprint, before.content_fingerprint)
        self.assertEqual(len(phone.sent), 1)
        self.assert_replays_point(stored, before, True)
        self.assert_replays_point(stored, snapshot, False)

    def test_a_stored_point_refuses_moved_controls_with_the_same_labels(self):
        path = save(self.store, sketch_lesson(), REQUEST)
        stored = self.store.by_id(P.Programs.id_of(path))
        self.assert_replays_point(stored, CANVAS, True)
        self.assert_replays_point(stored, canvas_with_undo_at(.8), False)

    def test_a_legacy_point_refuses_until_a_new_lesson_records_its_proof(self):
        path = save(self.store, sketch_lesson(), REQUEST)
        path["steps"][1]["screen"].pop("fingerprint")
        self.store.cache.save(path)
        legacy = self.store.by_id(P.Programs.id_of(path))
        self.assert_replays_point(legacy, CANVAS, False)
        save(self.store, sketch_lesson(), REQUEST)
        save(self.store, sketch_lesson(), REQUEST)
        repaired = self.store.by_id(P.Programs.id_of(path))
        self.assertEqual(len(repaired["steps"][1]["alts"]), 1)
        self.assertNotIn("fingerprint", repaired["steps"][1]["screen"])
        self.assert_replays_point(repaired, CANVAS, True)

    def test_reteaching_keeps_versions_for_each_geometry(self):
        path = save(self.store, sketch_lesson(), REQUEST)
        moved = canvas_with_undo_at(.8)
        for _ in range(2):
            save(self.store, taught(Phone([moved, moved]), [tap(.5, .6)]), REQUEST)
        stored = self.store.by_id(P.Programs.id_of(path))
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(len(stored["steps"][1]["alts"]), 1)
        self.assert_replays_point(stored, CANVAS, True)
        self.assert_replays_point(stored, moved, True)
        self.assert_replays_point(stored, canvas_with_undo_at(.7), False)

    def test_a_practice_correction_persists_its_own_pre_action_proof(self):
        path = save(self.store, sketch_lesson(), REQUEST)
        program = P.Matched(path, {})
        moved = canvas_with_undo_at(.8)
        correction, under = program.correction(moved, .5, .6)
        self.assertIsNone(under)
        program.corrections = [{"index": 1, "kind": "tap", "same": True, **correction}]
        PTT.apply_corrections(self.store, program)
        stored = self.store.by_id(P.Programs.id_of(path))
        self.assert_replays_point(stored, moved, True)
        self.assert_replays_point(stored, CANVAS, False)

    def test_practice_alternates_keep_distinct_same_label_geometries(self):
        path = save(self.store, sketch_lesson(), REQUEST)
        program = P.Matched(path, {})
        for y in (.8, .7):
            correction, _ = program.correction(canvas_with_undo_at(y), .5, .6)
            program.corrections.append({"index": 1, "kind": "tap", "same": False, **correction})
        PTT.apply_corrections(self.store, program)
        stored = self.store.by_id(P.Programs.id_of(path))
        self.assertEqual(len(stored["steps"][1]["alts"]), 2)
        for y in (.9, .8, .7):
            self.assert_replays_point(stored, canvas_with_undo_at(y), True)
