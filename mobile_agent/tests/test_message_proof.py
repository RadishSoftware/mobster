import unittest

from mobile_agent import contract as K
from mobile_agent import contract_asks as KA
from mobile_agent import contract_screen as KS
from mobile_agent.state import Element, Snapshot

TEXT = "Running 10 minutes late, save me a seat!"


def screen(*elements, bundle="com.apple.MobileSMS"):
    return Snapshot(list(elements), "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                    bundle_id=bundle)


def nav(title):
    return Element("90", title, "NavigationBar", (0, .05, 1, .06))


def composer(value):
    return Element("20", "Message", "TextField", (.2, .56, .6, .05), editable=True, value=value,
                   actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"))


SEND = Element("21", "Send", "Button", (.85, .59, .1, .03))
DELIVERED = Element("6", "Delivered", "StaticText", (.8, .5, .15, .02))


def text_view(value=TEXT):
    return Element("5", "", "TextView", (.32, .455, .6, .04), editable=True, value=value)


def contract():
    items = [{"kind": "COMMIT", "app": "Messages", "what": "Kate Bell", "payload": TEXT, "act": "send_message",
              "count": "1", "condition": "", "quote": "Text Kate Bell"}]
    return K.Contract.from_items(items, "Text Kate Bell: " + TEXT)


def send_and_look(after, before=None, thread="+1 (555) 564-8583"):
    c = contract()
    before = before or screen(nav(thread), composer(TEXT), SEND)
    c.record_typed("Messages", composer(""), TEXT, 1)
    gate = c.check("TAP", SEND, before, "Messages")
    assert gate.allowed and gate.item is not None, gate.reason
    c.committed(gate, SEND, before, "Messages", 2)
    c.observe(after, "Messages", 2)
    return c.items[0].receipts


class MessageProofTests(unittest.TestCase):
    def test_a_bubble_cell_with_a_text_view_inside_is_proof_once_the_composer_is_empty(self):
        after = screen(nav("+1 (555) 564-8583"), Element("4", TEXT, "Cell", (.3, .45, .65, .05)), text_view(),
                       DELIVERED, composer(""))
        self.assertEqual(len(send_and_look(after)), 1)

    def test_a_text_view_bubble_needs_its_delivery_mark(self):
        with_mark = screen(nav("+1 (555) 564-8583"), text_view(), DELIVERED, composer(""))
        self.assertEqual(len(send_and_look(with_mark)), 1)
        without = screen(nav("+1 (555) 564-8583"), text_view(), composer(""))
        self.assertEqual(send_and_look(without), [])

    def test_a_composer_still_holding_the_text_is_never_proof(self):
        held = screen(nav("+1 (555) 564-8583"), Element("3", TEXT, "Cell", (.1, .3, .8, .05)), DELIVERED,
                      composer(TEXT + "\n"), SEND)
        before = screen(nav("+1 (555) 564-8583"), Element("3", TEXT, "Cell", (.1, .3, .8, .05)), composer(TEXT), SEND)
        self.assertEqual(send_and_look(held, before), [])

    def test_the_same_text_already_in_the_thread_is_not_proof(self):
        old = Element("3", "", "TextView", (.1, .3, .8, .05), editable=True, value=TEXT)
        before = screen(nav("+1 (555) 564-8583"), old, DELIVERED, composer(TEXT), SEND)
        after = screen(nav("+1 (555) 564-8583"), old, DELIVERED, composer(""))
        self.assertEqual(send_and_look(after, before), [])

    def test_another_thread_is_not_proof(self):
        after = screen(nav("Sam Rivera"), text_view(), DELIVERED, composer(""))
        self.assertEqual(send_and_look(after), [])

    def test_a_wrong_text_is_not_proof(self):
        after = screen(nav("+1 (555) 564-8583"), Element("4", "Running 15 minutes late, save me a seat!", "Cell",
                                                         (.3, .45, .65, .05)), DELIVERED, composer(""))
        self.assertEqual(send_and_look(after), [])

    def test_the_message_body_write_is_proven_by_the_send(self):
        items = [{"kind": "WRITE", "app": "Messages", "what": "message to Kate Bell", "payload": TEXT, "act": "none",
                  "count": "", "condition": "", "quote": TEXT},
                 {"kind": "COMMIT", "app": "Messages", "what": "Kate Bell", "payload": TEXT, "act": "send_message",
                  "count": "1", "condition": "", "quote": "Text Kate Bell"}]
        c = K.Contract.from_items(items, "Text Kate Bell: " + TEXT)
        before = screen(nav("+1 (555) 564-8583"), composer(TEXT), SEND)
        c.record_typed("Messages", composer(""), TEXT, 1)
        gate = c.check("TAP", SEND, before, "Messages")
        c.committed(gate, SEND, before, "Messages", 2)
        self.assertEqual(c.proof(c.items[0])[0], False)
        after = Snapshot([nav("+1 (555) 564-8583"), text_view(), DELIVERED, composer("")] +
                         [Element("70", "q", "Key", (0, .75, .1, .05))], "", 400, 800, "synthetic_fixture",
                         bundle_id="com.apple.MobileSMS", keyboard="visible")
        c.observe(after, "Messages", 2)
        self.assertEqual(c.proof(c.items[0]), (True, ""))
        self.assertEqual(c.done_gate("Sent.", 30)[0], True)


class ApprovalTitleTests(unittest.TestCase):
    ITEM = contract().items[0]

    def test_the_title_names_who_the_screen_shows(self):
        self.assertEqual(KA.approval_title(self.ITEM, shown="+1 (555) 564-8583"),
                         "Send this message to +1 (555) 564-8583 (Kate Bell in your request)?")
        self.assertEqual(KA.approval_title(self.ITEM, shown="Kate Bell"), "Send this message to Kate Bell?")
        self.assertEqual(KA.approval_title(self.ITEM), "Send this message to the person you named (Kate Bell)?")

    def test_a_list_or_blank_composer_title_names_no_one(self):
        self.assertIsNone(KS.thread_title(screen(nav("Messages")), "Messages"))
        self.assertIsNone(KS.thread_title(screen(nav("New Message")), "Messages"))
        self.assertEqual(KS.thread_title(screen(nav("+1 (555) 564-8583")), "Messages"), "+1 (555) 564-8583")
        self.assertIsNone(KS.thread_title(screen(nav("Re: Dinner plans")), "Gmail"))
        self.assertIsNone(KS.thread_title(screen(nav("Dinner plans")), "Mail"))

    def test_the_same_number_written_two_ways_is_the_same_party(self):
        self.assertTrue(KS.same_party("+1 (555) 564-8583", "555-564-8583"))
        self.assertFalse(KS.same_party("+1 (555) 564-8583", "Kate Bell"))


def compose_sheet(to_value="", token=None, message=TEXT):
    elements = [Element("1", "CKComposeChat", "NavigationBar", (0, .09, 1, .06)),
                Element("2", "New Message", "Button", (.32, .10, .37, .02)),
                Element("3", "Cancel", "Button", (.85, .09, .09, .04)),
                Element("7", "To:", "TextField", (.08, .16, .85, .06), editable=True, value=to_value,
                        actions=("TAP", "TYPE")),
                Element("9", "Add Contact", "Button", (.85, .17, .08, .04)),
                Element("10", "To:", "StaticText", (.08, .18, .05, .02)),
                composer(message), SEND]
    if token:
        elements.append(Element("8", token, "Button", (.13, .18, .27, .03)))
    return screen(*elements)


def jordan():
    items = [{"kind": "COMMIT", "app": "Messages", "what": "text message to Jordan Lee", "payload": TEXT,
              "act": "send_message", "count": "1", "condition": "", "quote": "Text Jordan Lee"}]
    return K.Contract.from_items(items, "Text Jordan Lee: " + TEXT).items[0]


class CardRecipientTests(unittest.TestCase):
    def test_the_compose_sheet_names_the_to_fields_person_not_its_class(self):
        sheet = compose_sheet("To:\ufffcJordan Lee", "Jordan Lee, +15550100000")
        self.assertIsNone(KS.thread_title(sheet, "Messages"))
        self.assertEqual(KS.visible_recipient(sheet, "Messages"), "Jordan Lee")
        self.assertEqual(KA.approval_title(jordan(), shown=KS.visible_recipient(sheet, "Messages")),
                         "Send this message to Jordan Lee?")
        self.assertEqual(KS.visible_recipient(compose_sheet("", "Jordan Lee, +15550100000"), "Messages"), "Jordan Lee")

    def test_another_person_in_the_to_field_is_named_beside_the_request(self):
        sheet = compose_sheet("To:\ufffcSam Rivera", "Sam Rivera, +15550100001")
        self.assertEqual(KA.approval_title(jordan(), shown=KS.visible_recipient(sheet, "Messages")),
                         "Send this message to Sam Rivera (Jordan Lee in your request)?")

    def test_no_one_on_screen_asks_with_the_request_or_a_neutral_phrase(self):
        sheet = compose_sheet()
        self.assertIsNone(KS.visible_recipient(sheet, "Messages"))
        self.assertEqual(KA.approval_title(jordan(), shown=KS.visible_recipient(sheet, "Messages")),
                         "Send this message to the person you named (Jordan Lee)?")
        unnamed = KS.Item(id=1, kind="COMMIT", app="Messages", what="a text message", act="send_message")
        self.assertEqual(KA.approval_title(unnamed, shown=KS.visible_recipient(sheet, "Messages")),
                         "Send this message?")
        self.assertIsNone(KS.visible_recipient(compose_sheet("To:\ufffcNo Name, Searching"), "Messages"))

    def test_a_class_name_never_reaches_the_card(self):
        self.assertEqual(KA.approval_title(jordan(), shown="CKComposeChat"),
                         "Send this message to the person you named (Jordan Lee)?")
        named_by_id = KS.Item(id=1, kind="COMMIT", app="Messages", what="CKComposeChat", act="send_message")
        self.assertEqual(KA.approval_title(named_by_id), "Send this message?")
        sheet = compose_sheet("To:\ufffcJordan Lee", "Jordan Lee, +15550100000")
        self.assertEqual(KS.header_recipient(sheet, SEND), "Jordan Lee")
        self.assertIsNone(KS.header_recipient(compose_sheet(), SEND))

    def test_what_counts_as_a_developers_name(self):
        for name in ("CKComposeChat", "UINavigationBar", "ComposeChatViewController", "composeTextField",
                     "chat_compose_field", "com.example.MobileChat", "_TtC4Chat8ChatView", "ChatView"):
            self.assertTrue(KS.internal_name(name), name)
        for name in ("Jordan Lee", "+1 (555) 010-0000", "jordan.lee@example.com", "McDonald", "DeShawn", "iPhone",
                     "jordan_lee", "jo.bloggs.99", "Netflix", "WhatsApp", "NYTimes", "Mom", "New Message"):
            self.assertFalse(KS.internal_name(name), name)


if __name__ == "__main__":
    unittest.main()
