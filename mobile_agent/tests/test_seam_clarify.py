"""Seam S5: clarifying questions through Run.request_approval (kind "clarify"), answered with text or a choice; the
typed answer is never journaled. Offline."""

import json
import threading
import time
import unittest

from mobile_agent import harness_api
from mobile_agent.journal import Journal
from mobile_agent.server import Run
from mobile_agent.tests.test_server_hardening import APP

QUESTION = {"kind": "clarify", "operation": "ASK_USER", "label": "Which Sam?", "question": "Which Sam: Sam Lee or "
            "Sam Ortiz?", "choices": [{"id": "lee", "label": "Sam Lee"}, {"id": "ortiz", "label": "Sam Ortiz"}],
            "allow_text": True}


def make_run():
    db = Journal(None)
    run = Run(dict(APP), "Text Sam", "live", journal=db, engine="smart")
    db.create(run.metadata())
    return run, db


def answer_when_asked(run, **answer):
    def loop():
        for _ in range(500):
            pending = run.public()["approval"]
            if pending:
                run.answer_approval(pending["id"], True, **answer)
                return
            time.sleep(.005)
    thread = threading.Thread(target=loop)
    thread.start()
    return thread


class ClarifyTests(unittest.TestCase):
    def test_a_question_is_published_with_its_shape(self):
        run, db = make_run()
        self.addCleanup(db.close)
        seen = {}

        def look():
            for _ in range(500):
                pending = run.public()["approval"]
                if pending:
                    seen.update(pending)
                    run.answer_approval(pending["id"], True, choice="ortiz")
                    return
                time.sleep(.005)
        thread = threading.Thread(target=look)
        thread.start()
        self.assertEqual(run.request_approval(dict(QUESTION), timeout=5), "choice:ortiz")
        thread.join(5)
        self.assertEqual((seen["kind"], seen["operation"], seen["label"]), ("clarify", "ASK_USER", "Which Sam?"))
        self.assertEqual(seen["question"], QUESTION["question"])
        self.assertEqual([c["id"] for c in seen["choices"]], ["lee", "ortiz"])
        self.assertTrue(seen["allowText"])

    def test_a_typed_answer_reaches_the_agent_and_never_the_journal(self):
        run, db = make_run()
        self.addCleanup(db.close)
        thread = answer_when_asked(run, answer="  the  one from   work ")
        self.assertEqual(run.request_approval(dict(QUESTION), timeout=5), "answer:the one from work")
        thread.join(5)
        resolved = next(e for e in run.events if e["event"] == "approval_resolved")
        self.assertEqual(resolved["decision"], "answered")
        saved = json.dumps(db.load())
        self.assertNotIn("the one from work", saved)
        self.assertNotIn("the one from work", json.dumps(run.events))

    def test_a_question_that_nobody_answers_times_out(self):
        run, db = make_run()
        self.addCleanup(db.close)
        self.assertEqual(run.request_approval(dict(QUESTION), timeout=.05), "timeout")

    def test_answers_only_fit_questions_and_have_limits(self):
        run, db = make_run()
        self.addCleanup(db.close)
        errors = []

        def try_answers():
            for _ in range(500):
                pending = run.public()["approval"]
                if pending:
                    for kwargs in ({"answer": "yes"}, ):
                        try:
                            run.answer_approval(pending["id"], True, **kwargs)
                        except ValueError as error:
                            errors.append(str(error))
                    run.answer_approval(pending["id"], False)
                    return
                time.sleep(.005)
        thread = threading.Thread(target=try_answers)
        thread.start()
        commit = {"kind": "commit", "operation": "TAP", "label": "Send", "text": "On my way", "title": "Send?"}
        self.assertEqual(run.request_approval(commit, timeout=5), "denied")
        thread.join(5)
        self.assertEqual(errors, ["Only a question from Mobster's agent takes a typed answer"])

        thread = threading.Thread(target=lambda: self._bad_answers(run, errors))
        thread.start()
        self.assertEqual(run.request_approval(dict(QUESTION), timeout=5), "denied")
        thread.join(5)
        self.assertEqual(len(errors), 4)

    def _bad_answers(self, run, errors):
        for _ in range(500):
            pending = run.public()["approval"]
            if pending:
                for approve, kwargs in ((False, {"answer": "Lee"}), (True, {"answer": "   "}),
                                        (True, {"answer": "x" * 501})):
                    try:
                        run.answer_approval(pending["id"], approve, **kwargs)
                    except ValueError as error:
                        errors.append(str(error))
                run.answer_approval(pending["id"], False)
                return
            time.sleep(.005)

    def test_clarify_only_asks_clarifying_questions(self):
        asked = []
        clarify = harness_api.clarify_only(lambda request: asked.append(request) or "answer:Lee")
        self.assertEqual(clarify({"kind": "clarify", "question": "Which Sam do you mean, Lee or Ortiz?"}), "answer:Lee")
        self.assertEqual((asked[0]["operation"], asked[0]["label"]),
                         ("ASK_USER", "Which Sam do you mean, Lee or Ortiz?"))
        for kind in ("commit", "action", "use_code", None):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                clarify({"kind": kind, "operation": "TAP", "label": "Send"})
        self.assertEqual(len(asked), 1)


if __name__ == "__main__":
    unittest.main()
