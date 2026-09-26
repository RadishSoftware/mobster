"""Each frontier turn is recorded with what the model saw and chose, and reads back for replay. Offline."""

import unittest

from mobile_agent.bench.replay import describe, parse, rebuilt, rows_for
from mobile_agent.bench.sweep import action_key
from mobile_agent.frontier import KEYBOARD_ROW, LISTING_HEAD, FrontierAgent, prompt_messages, prompt_text
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import ARCHIVE, Driver, Script, screen


class PromptRecordTests(unittest.TestCase):
    def test_a_turn_records_its_prompt_and_choice_and_resolves_the_chosen_row(self):
        row = screen(Element("1", "Archive", "Button", (.4, .1, .2, .05)))
        events = []
        FrontierAgent(Driver([row] * 3), Script([("TAP", "Archive", None), ("DONE", None, None)]),
                      emit=events.append, screenshots=False, settle_seconds=0).run(ARCHIVE)
        prompts = [e for e in events if e["event"] == "frontier_prompt"]
        self.assertEqual([p["step"] for p in prompts], [0, 1])
        first = prompts[0]
        self.assertIn("Screen elements:", first["text"])
        self.assertIsNone(first["image"])
        self.assertIn("TAP", first["operations"])
        self.assertEqual(first["targets"], ["e1"])
        self.assertTrue(describe(first["out"], first["text"]).startswith('TAP e1 Button "Archive"'))


OLD = ('Request: Add eggs.\n\nTurns used: 3 of 50\n\nApps you may use:\n- Shop: com.shop\n\nCurrent app: Shop\n\n'
       'Plan so far: Search, add.\n\nNotes:\n- eggs are $3\n\nRecent actions (oldest first):\n0.0: TAP \'Search\' -> '
       'screen changed\n\nFeedback on your last action: Refused: loop.\n\nScreen elements:\ne1 Button "Cart" @40,90,20,6\n'
       'e2 Image "shopping cart" @46,91,8,3\ne3 Key "q" @0,70,10,5\ne4 Button "Add" @80,40,10,5')


class LayoutReplayTests(unittest.TestCase):
    def test_a_recorded_prompt_is_rebuilt_in_todays_layout_with_the_same_fields(self):
        fields = parse(OLD)
        self.assertEqual((fields["request"], fields["turns"], fields["plan"]), ("Add eggs.", "3 of 50", "Search, add."))
        self.assertEqual((fields["notes"], fields["feedback"]), (["eggs are $3"], "Refused: loop."))
        messages, back = rebuilt({"text": OLD}, ".", lean=False)
        self.assertEqual(messages[1]["content"][0]["text"], "Request: Add eggs.\n\nApps you may use:\n- Shop: com.shop")
        self.assertEqual(parse(prompt_text(messages)), fields)
        self.assertEqual(sorted(prompt_text(messages)), sorted(OLD))
        self.assertEqual(back, {f"e{i}": f"e{i}" for i in range(1, 5)})

    def test_the_checklist_and_a_read_list_survive_the_round_trip(self):
        messages = prompt_messages("Total receipts.", "- Mail: m", "2 of 50", "Mail", "", [], [], None,
                                   ['e1 Cell "Receipt" @0,20,100,8'],
                                   extra="\n\nChecklist:\n1. [ ] report: total\n\n" + LISTING_HEAD + "Receipt 1\nReceipt 2")
        text = prompt_text(messages)
        again, _ = rebuilt({"text": text}, ".")
        self.assertEqual(prompt_text(again), text)
        self.assertEqual(parse(text)["checklist"], "1. [ ] report: total")

    def test_lean_rows_are_re_aliased_and_mapped_back_to_the_recorded_ids(self):
        lines, back = rows_for(parse(OLD)["rows"], lean=True)
        self.assertEqual(lines, ['e1 Button "Cart" @40,90,20,6', 'e2 Image "shopping cart" @46,91,8,3',
                                 'e3 Button "Add" @80,40,10,5', KEYBOARD_ROW])
        self.assertEqual(back, {"e1": "e1", "e2": "e2", "e3": "e4"})
        out = {"actions": [{"operation": "TAP", "target": "e3", "target_label": None, "text": None, "app": None}]}
        self.assertEqual(action_key(out, OLD, back), ("TAP", 'Button "Add" @80,40,10,5'))
        self.assertEqual(action_key({"actions": [{"operation": "TAP", "target": "e4"}]}, OLD),
                         ("TAP", 'Button "Add" @80,40,10,5'))


if __name__ == "__main__":
    unittest.main()
