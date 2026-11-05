"""Long task outcome receipts use the existing task contract, not plan-step navigation."""

import json
import unittest

from mobile_agent.contract import Contract as RealContract
from mobile_agent.contract import Item
from mobile_agent.longform import receipts, report
from mobile_agent.longform.notebook import Notebook


class Contract:
    def __init__(self, items, proven=()):
        self.items = items
        self.proven = set(proven)

    def proof(self, item):
        return item.id in self.proven, "" if item.id in self.proven else "not shown on screen"


class ReceiptTests(unittest.TestCase):
    def book(self, run_id="r1"):
        return Notebook(run_id, "Send two messages and report the price", "chain",
                        mask=lambda text: text.replace("secret-123", "•••"))

    def test_step_done_or_gate_satisfied_without_screen_evidence_is_unverified(self):
        book = self.book()
        book.steps = [{"id": 1, "title": "Send a message", "app": "Chat", "state": "done", "result": "Sent"}]
        item = Item(id=1, kind="COMMIT", what="send a message", act="send_message", count="1", claim="done")
        entry = receipts.snapshot(book, Contract([item], proven={1}), final=True)
        self.assertEqual(entry["outcome"], "unverified")
        self.assertTrue(entry["items"][0]["gateSatisfied"])
        self.assertEqual(entry["items"][0]["evidence"], [])

    def test_count_two_needs_both_receipts_and_keeps_source_run(self):
        book = self.book()
        item = Item(id=1, kind="COMMIT", what="send two messages", act="send_message", count="2",
                    receipts=[{"step": 3, "receipt": "sent: first secret-123"}])
        one = receipts.snapshot(book, Contract([item], proven={1}))
        self.assertEqual(one["items"][0]["outcome"], "partial")
        self.assertEqual(one["items"][0]["evidence"][0]["text"], "sent: first •••")
        self.assertEqual(one["items"][0]["evidence"][0]["runId"], "r1")
        item.receipts.append({"step": 5, "receipt": "sent: second"})
        two = receipts.snapshot(book, Contract([item], proven={1}), final=True)
        self.assertEqual(two["outcome"], "completed")
        receipts.put(book, two)
        self.assertEqual(Notebook.from_dict(json.loads(book.dumps())).receipts, [two])

    def test_many_confirmations_keep_count_but_bound_excerpts_and_name_omissions(self):
        book = self.book()
        item = Item(id=1, kind="COMMIT", what="send nine notes", act="send_message", count="9",
                    receipts=[{"step": n, "receipt": f"sent: note {n}"} for n in range(9)])
        entry = receipts.snapshot(book, Contract([item], proven={1}), final=True)
        self.assertEqual(entry["outcome"], "completed")
        self.assertEqual(len(entry["items"][0]["evidence"]), receipts.MAX_EVIDENCE)
        self.assertEqual(entry["items"][0]["evidenceOmitted"], 1)

    def test_checkpoint_only_cannot_headline_terminal_completion_and_old_history_is_bounded(self):
        book = self.book()
        item = Item(id=1, kind="COMMIT", what="send", act="send_message", receipts=[{"step": 1, "receipt": "sent"}])
        receipts.put(book, receipts.snapshot(book, Contract([item], proven={1})))
        self.assertEqual(receipts.overall(book.receipts, book.run_id), "unverified")
        for n in range(receipts.MAX_RUNS + 1):
            book.run_id = f"pickup-{n}"
            receipts.put(book, receipts.snapshot(book, Contract([item], proven={1}), final=True))
        self.assertEqual(book.receipt_runs_omitted, 2)
        self.assertEqual(len(book.receipts), receipts.MAX_RUNS)
        finished = report.finish({"sections": [], "ranked": [], "gaps": []}, book)
        self.assertEqual(finished["outcomes"]["historyOmitted"], 2)

    def test_recorded_value_is_distinct_from_screen_evidence_and_requested_payload(self):
        book = self.book()
        item = Item(id=1, kind="REPORT", what="price", payload="requested offer", status="found",
                    value="$18 secret-123", value_step=4)
        unverified = receipts.snapshot(book, Contract([item], proven={1}))
        self.assertEqual(unverified["items"][0]["outcome"], "unverified")
        self.assertEqual(unverified["items"][0]["recordedValue"], "$18 •••")
        self.assertEqual(unverified["items"][0]["requested"], "requested offer")
        self.assertNotIn("frameId", str(unverified))

    def test_no_action_and_write_without_canonical_receipt_cannot_be_reported_complete(self):
        book = self.book()
        forbid = Item(id=1, kind="FORBID", what="do not pay")
        write = Item(id=2, kind="WRITE", what="save a draft", payload="hello")
        entry = receipts.snapshot(book, Contract([forbid, write], proven={1, 2}), final=True)
        self.assertEqual([row["outcome"] for row in entry["items"]], ["unverified", "unverified"])

    def test_generic_screen_change_is_context_not_confirmation_and_constraint_does_not_block_success(self):
        book = self.book()
        weak = Item(id=1, kind="COMMIT", what="save", act="other", receipts=[{"step": 2, "receipt": "screen changed"}])
        strong = Item(id=2, kind="COMMIT", what="send", act="send_message",
                      receipts=[{"step": 3, "receipt": "sent: hello"}])
        avoid = Item(id=3, kind="FORBID", what="do not pay")
        entry = receipts.snapshot(book, Contract([weak, strong, avoid], proven={1, 2, 3}), final=True)
        self.assertEqual(entry["outcome"], "partial")
        self.assertEqual(entry["items"][0]["outcome"], "unverified")
        self.assertEqual(entry["items"][0]["evidence"][0]["kind"], "screen_change")
        self.assertEqual(entry["items"][1]["outcome"], "completed")
        self.assertEqual(receipts.status(entry["items"][1:]), "completed")

    def test_real_contract_gate_with_reported_value_has_no_screen_receipt(self):
        book = self.book()
        contract = RealContract.from_items([{"kind": "REPORT", "what": "calculate total"}], book.request)
        contract.items[0].status = "found"
        contract.items[0].value = "$18"
        entry = receipts.snapshot(book, contract, final=True)
        self.assertEqual(entry["items"][0]["recordedValue"], "$18")
        self.assertEqual(entry["items"][0]["outcome"], "unverified")

    def test_pickup_same_contract_item_id_remains_separate_and_report_survives_synthesis_failure(self):
        book = self.book()
        item = Item(id=1, kind="COMMIT", what="send", act="send_message", receipts=[{"step": 2, "receipt": "sent"}])
        receipts.put(book, receipts.snapshot(book, Contract([item], proven={1}), final=True))
        picked = Notebook.from_dict(json.loads(book.dumps()))
        picked.run_id = "r2"
        receipts.put(picked, receipts.snapshot(picked, Contract([Item(id=1, kind="REPORT", what="price")]),
                                             final=True))
        self.assertEqual([run["runId"] for run in picked.receipts], ["r1", "r2"])
        self.assertEqual(receipts.overall(picked.receipts, "r2"), "unverified")
        fallback = report.finish(report.deterministic(picked), picked, failed=True)
        self.assertTrue(fallback["synthesisFailed"])
        self.assertEqual(fallback["outcomes"]["status"], "unverified")
        self.assertIn("Screen evidence (r1", report.markdown(fallback))


if __name__ == "__main__":
    unittest.main()
