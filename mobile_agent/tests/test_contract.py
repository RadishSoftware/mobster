import unittest
from types import SimpleNamespace as NS

from mobile_agent import contract as K
from mobile_agent import contract_asks as KA
from mobile_agent import contract_screen as KS
from mobile_agent import drivers
from mobile_agent.frontier import FrontierAgent, prompt_text
from mobile_agent.state import Element, Snapshot


def el(label, role="Button", y=.5, x=.1, w=.3, h=.04, editable=False, value=""):
    return NS(label=label, role=role, rect=(x, y, w, h), editable=editable, value=value)


def shot(*elements, keyboard=""):
    return NS(elements=list(elements), keyboard=keyboard)


def item(kind="COMMIT", app="SplitPay", what="x", act="none", quote="", count="1", condition="", payload=""):
    return {"kind": kind, "app": app, "what": what, "payload": payload, "act": act, "count": count,
            "condition": condition, "quote": quote}


def contract(request, *items):
    return K.Contract.from_items(list(items), request)


PAY = "Review SplitPay balance and pay any pending requests under $50."
PAY_ITEM = item(what="pending requests under $50", act="pay", count="each", condition="pending request is under $50",
                quote="pay any pending requests under $50.")


class ContractValidationTests(unittest.TestCase):
    def test_a_commit_must_quote_the_request_verbatim(self):
        c = contract(PAY, PAY_ITEM, item(what="Maya", act="pay", quote="pay Maya back"),
                     item(what="x", act="teleport", quote="pay any"))
        self.assertEqual([i.what for i in c.items], ["pending requests under $50"])
        self.assertEqual(len(c.dropped), 2)

    def test_quote_check_ignores_case_quotes_and_spacing(self):
        c = contract("Message ‘Brunch Crew’  with the time.", item(app="QuickChat", what="Brunch Crew",
                                                                   act="send_message",
                                                                   quote="message 'brunch crew' with"))
        self.assertEqual(len(c.items), 1)

    def test_forbid_items_are_kept_and_refuse_their_act(self):
        c = contract("Clean up my inbox but do not delete anything.",
                     item(kind="FORBID", app="Mail", what="emails", act="delete", quote="do not delete anything"))
        g = c.check("TAP", el("Delete"), shot(), "Mail")
        self.assertFalse(g.allowed)
        self.assertIn("rules it out", g.reason)


class ForbidScopeTests(unittest.TestCase):
    REQUEST = "In Notes, delete the note Mobster QA loop A. Delete nothing else."
    NAMED = item(app="Notes", what="Mobster QA loop A", act="delete", quote="delete the note Mobster QA loop A")

    def test_the_named_delete_passes_with_its_object_on_screen(self):
        c = contract(self.REQUEST, self.NAMED,
                     item(kind="FORBID", app="Notes", what="other notes", act="delete", quote="Delete nothing else."))
        g = c.check("TAP", el("Delete"), shot(el("Mobster QA loop A", "StaticText"), el("Delete")), "Notes")
        self.assertTrue(g.allowed, g.reason)
        self.assertEqual(g.item.what, "Mobster QA loop A")

    def test_other_deletes_are_still_refused(self):
        c = contract(self.REQUEST, self.NAMED,
                     item(kind="FORBID", app="Notes", what="notes", act="delete", quote="Delete nothing else."))
        g = c.check("TAP", el("Delete"), shot(el("Groceries", "StaticText"), el("Delete")), "Notes")
        self.assertFalse(g.allowed)
        self.assertIn("not on this screen", g.reason)
        c.items[0].status = "closed"
        g = c.check("TAP", el("Delete"), shot(el("Mobster QA loop A", "StaticText"), el("Delete")), "Notes")
        self.assertFalse(g.allowed)
        self.assertIn("rules it out", g.reason)

    def test_a_forbid_with_no_exception_refuses_a_declared_delete_too(self):
        c = contract("Delete the note Mobster QA loop A, but do not delete anything.", self.NAMED | {
            "quote": "Delete the note Mobster QA loop A"},
                     item(kind="FORBID", app="Notes", what="notes", act="delete", quote="do not delete anything"))
        g = c.check("TAP", el("Delete"), shot(el("Mobster QA loop A", "StaticText"), el("Delete")), "Notes")
        self.assertFalse(g.allowed)
        self.assertIn("rules it out", g.reason)
        self.assertFalse(K.excludes_others(c.items[1]))


class VerbTests(unittest.TestCase):
    def test_first_four_words_name_the_act(self):
        self.assertEqual(KS.label_verbs("Pay $42.00"), {"pay"})
        self.assertEqual(KS.label_verbs("Confirm & Place Order"), {"confirm", "place order"})
        self.assertEqual(KS.label_verbs("A film about the Ocean's call"), set())

    def test_known_false_triggers(self):
        self.assertEqual(KS.label_verbs("Dentist, 490 Post St, San Francisco"), set())
        self.assertEqual(KS.label_verbs("pencil.tip"), set())
        self.assertEqual(KS.label_verbs("9:30, Client call, Weekdays", "Switch"), set())
        self.assertEqual(KS.label_verbs("Feels Like"), set())
        self.assertEqual(KS.label_verbs("Your requests"), set())
        self.assertEqual(KS.label_verbs("CityRide One, Save on rides"), set())

    def test_added_words_count_as_the_first_word(self):
        self.assertEqual(KS.label_verbs("Like"), {"like"})
        self.assertEqual(KS.label_verbs("Save post"), {"save", "post"})
        self.assertEqual(KS.label_verbs("Request $15.00"), {"request"})
        self.assertEqual(KS.label_verbs("Cancel Ride"), {"cancel"})
        self.assertEqual(KS.label_verbs("archive"), {"archive"})
        self.assertEqual(KS.label_verbs("Unfollow"), {"unfollow"})
        self.assertEqual(KS.label_verbs("Following"), {"unfollow"})
        self.assertEqual(KS.label_verbs("React"), {"react"})

    def test_identifier_labels_are_split(self):
        self.assertEqual(KS.label_verbs("mybank.checkout.confirmToggle", "Switch"), {"checkout", "confirm"})

    def test_sheet_openers_commit_nothing(self):
        for label in ("Share", "Comment", "Comments", "Reply", "Reply in thread", "Cancel", "Comment Left"):
            self.assertEqual(KS.label_verbs(label), set(), label)


class FamilyTests(unittest.TestCase):
    TABLE = {
        "send_message": ({"Send", "Post", "Publish", "Reply", "Comment on post", "Tweet", "Submit"}, {"Pay"}),
        "pay": ({"Pay", "Send", "Transfer", "Confirm", "Submit"}, {"Place Order", "Request"}),
        "request_money": ({"Request", "Send", "Confirm", "Submit"}, {"Pay"}),
        "transfer": ({"Transfer", "Send", "Confirm", "Submit"}, {"Pay"}),
        "order": ({"Order", "Place Order", "Buy", "Purchase", "Checkout", "Pay", "Confirm", "Submit", "Tip"}, {"Send"}),
        "book": ({"Book", "Reserve", "Confirm", "Submit", "Request CityRideX"}, {"Pay"}),
        "follow": ({"Follow", "Subscribe"}, {"Unfollow", "Following"}),
        "share": ({"Share trip status", "Send"}, {"Post"}),
        "call": ({"Call", "Dial", "FaceTime"}, {"Send"}),
        "delete": ({"Delete", "Erase", "Remove"}, {"Archive"}),
        "react": ({"Like", "React"}, {"Send"}),
    }

    def test_each_act_allows_its_verbs_and_no_others(self):
        request = "do the thing"
        for act, (allowed, refused) in self.TABLE.items():
            c = contract(request, item(app="App", what="the thing", act=act, quote="do the thing"))
            typed = [el("hello there", "TextField", editable=True, value="hello there")]
            c.record_typed("App", typed[0], "hello there", 0)
            for label in allowed:
                self.assertTrue(c.check("TAP", el(label), shot(*typed), "App").allowed, (act, label))
            for label in refused:
                self.assertFalse(c.check("TAP", el(label), shot(*typed), "App").allowed, (act, label))

    def test_other_allows_the_verbs_its_quote_uses(self):
        c = contract("Find the Netflix job and save it.",
                     item(app="LockedIn", what="Platform Engineering Lead posting", act="other", quote="save it"))
        c.items[0].what = ""
        self.assertTrue(c.check("TAP", el("Save"), shot(), "LockedIn").allowed)
        self.assertFalse(c.check("TAP", el("Delete"), shot(), "LockedIn").allowed)

    def test_the_requests_own_word_for_the_act_is_allowed_whatever_family_it_compiled_to(self):
        request = "Request a CityRideX from Home to Work in the CityRide app."
        c = contract(request, item(app="CityRide", what="CityRideX ride from Home to Work", act="order",
                                   quote="Request a CityRideX from Home to Work"))
        self.assertTrue(c.check("TAP", el("Request CityRideX · $12.80"), shot(), "CityRide").allowed)
        self.assertFalse(c.check("TAP", el("Request CityRideX · $12.80"), shot(), "SplitPay").allowed)
        self.assertFalse(c.check("TAP", el("Send"), shot(), "CityRide").allowed)
        self.assertFalse(c.check("TAP", el("Delete ride"), shot(), "CityRide").allowed)

    def test_an_unrequested_request_is_still_refused(self):
        c = contract("Order the Sugarfish Trust Me set for lunch.",
                     item(app="QuickBite", what="Sugarfish Trust Me set", act="order",
                          quote="Order the Sugarfish Trust Me set"))
        self.assertTrue(c.check("TAP", el("Place Order · $40.45"), shot(), "QuickBite").allowed)
        self.assertFalse(c.check("TAP", el("Request $40.45"), shot(), "QuickBite").allowed)

    def test_only_the_quotes_first_commit_word_is_taken(self):
        request = "Book a table for 2 and post a review."
        c = contract(request, item(app="DineSpot", what="table for 2", act="book",
                                   quote="Book a table for 2 and post a review"))
        self.assertTrue(c.check("TAP", el("Book"), shot(), "DineSpot").allowed)
        self.assertFalse(c.check("TAP", el("Post"), shot(), "DineSpot").allowed)

    def test_a_forbid_items_family_is_unchanged(self):
        c = contract("Check my orders but don't cancel anything.",
                     item(kind="FORBID", app="QuickBite", what="orders", act="cancel", quote="don't cancel anything"))
        self.assertEqual(c.items[0].family(), set(KS.FAMILIES["cancel"]))

    def test_the_commit_must_be_in_the_current_app(self):
        c = contract(PAY, PAY_ITEM)
        self.assertTrue(c.check("TAP", el("Pay"), shot(), "SplitPay").allowed)
        self.assertFalse(c.check("TAP", el("Pay"), shot(), "MyBank").allowed)

    def test_save_completes_a_write_in_its_app_only(self):
        c = contract("Set a 6:45 AM alarm labeled 'Gym'.",
                     item(kind="WRITE", app="Clock", what="new alarm", payload="6:45 AM", quote="Set a 6:45 AM alarm"))
        self.assertTrue(c.check("TAP", el("Save"), shot(), "Clock").allowed)
        self.assertFalse(c.check("TAP", el("Save post"), shot(), "LockedIn").allowed)

    def test_an_alarm_switch_passes_only_when_the_request_names_that_alarm(self):
        switch = el("9:30, Client call, Weekdays", "Switch")
        c = contract("Set a 6:45 AM alarm labeled 'Gym'.",
                     item(kind="WRITE", app="Clock", what="new alarm", payload="6:45 AM", quote="Set a 6:45 AM alarm"))
        self.assertFalse(c.check("TAP", switch, shot(switch), "Clock").allowed)
        c = contract("Turn off my Client call alarm.",
                     item(kind="WRITE", app="Clock", what="Client call alarm switch",
                          quote="Turn off my Client call alarm"))
        self.assertTrue(c.check("TAP", switch, shot(switch), "Clock").allowed)

    def test_long_press_is_gated_like_a_tap(self):
        c = contract(PAY, PAY_ITEM)
        self.assertFalse(c.check("LONG_PRESS", el("Post comment"), shot(), "SplitPay").allowed)


class WriteProofTests(unittest.TestCase):

    TIMER = item(kind="WRITE", app="Clock", what="1-hour timer", payload="1 hour", quote="Set a 1-hour timer")

    def clock(self, *rows):
        return shot(el("Clock", "NavigationBar"), *[el(r, "StaticText") for r in rows])

    def test_numbers_are_whole_tokens_and_durations_match_across_units(self):
        self.assertFalse(KS.shows("1 hour", "today, +10:30hrs"))
        for text in ("1:00:00", "59:58", "1 hr", "60 min"):
            self.assertTrue(KS.shows("1 hour", text), text)
        self.assertTrue(KS.shows("7 pm friday", "friday 7:00 pm"))
        self.assertTrue(KS.shows("6:45 am", "6:45, gym on"))
        self.assertFalse(KS.shows("6:45 am", "16:45"))
        self.assertFalse(KS.shows("$40.45", "total $140.45"))
        self.assertTrue(KS.shows("6 people", "6 guests"))
        self.assertFalse(KS.shows("6 people", "16 guests"))

    def test_a_value_on_screen_before_any_write_in_its_app_proves_nothing(self):
        c = contract("Set a 1-hour timer in Clock.", self.TIMER)
        c.observe(self.clock("Today, +1HRS", "1 hour"), "Clock", 0)
        self.assertFalse(c.proof(c.items[0])[0])
        c.items[0].claim = "done"
        self.assertFalse(c.proof(c.items[0])[0])

    def test_after_a_write_in_its_app_the_value_on_screen_proves_it(self):
        c = contract("Set a 1-hour timer in Clock.", self.TIMER)
        c.observe(self.clock("Timer"), "Clock", 0)
        c.record_action("Clock", "SET_TEXT", "Hours")
        c.record_action("Clock", "TAP", "Start")
        c.observe(self.clock("59:58", "Pause"), "Clock", 1)
        self.assertTrue(c.proof(c.items[0])[0])

    def test_a_navigation_tap_is_not_a_write(self):
        c = contract("Set a 1-hour timer in Clock.", self.TIMER)
        c.record_action("Clock", "TAP", "World Clock")
        c.observe(self.clock("1:00:00"), "Clock", 1)
        self.assertFalse(c.proof(c.items[0])[0])

    def test_a_cited_row_proves_a_write_that_was_already_so(self):
        c = contract("Add New York to Weather.", item(kind="WRITE", app="Weather", what="New York as a location",
                                                      payload="New York", quote="Add New York"))
        c.observe(shot(el("New York, 61°, Partly Cloudy", "Cell")), "Weather", 2)
        c.update([{"id": 1, "status": "done", "value": "already: New York, 61°"}], 2)
        self.assertTrue(c.proof(c.items[0])[0])
        c.update([{"id": 1, "status": "done", "value": "already: Boston, 55°"}], 3)
        self.assertFalse(c.proof(c.items[0])[0])

    def test_a_bare_value_counts_as_a_citation_only_as_a_whole_row(self):
        c = contract("Add New York to Weather.", item(kind="WRITE", app="Weather", what="New York as a location",
                                                      payload="New York", quote="Add New York"))
        c.observe(shot(el("New York, 61°, Partly Cloudy", "Cell")), "Weather", 2)
        c.update([{"id": 1, "status": "done", "value": "New York"}], 2)
        self.assertFalse(c.proof(c.items[0])[0])
        c.update([{"id": 1, "status": "done", "value": "New York, 61°, Partly Cloudy"}], 2)
        self.assertTrue(c.proof(c.items[0])[0])


class AlreadyInAListTests(unittest.TestCase):

    NY = item(kind="WRITE", app="Weather", what="New York added as a location", payload="New York",
              quote="add New York")

    def locations(self):
        return shot(el("My Locations", "NavigationBar", y=.07), el("weather_location_list_row_new-york-ny", "Cell",
                                                                   x=0, y=.22, w=1, h=.07),
                    el("New York", "StaticText", x=.05, y=.24, w=.19, h=.02))

    def test_the_value_as_a_list_row_proves_an_already_so_write(self):
        c = contract("Check Weather and add New York.", self.NY)
        c.observe(self.locations(), "Weather", 30)
        c.update([{"id": 1, "status": "done", "value": "already: New York row in My Locations (59° Mostly Cloudy)"}],
                 30)
        self.assertTrue(c.proof(c.items[0])[0])

    def test_a_page_titled_with_the_value_is_no_list_row(self):
        c = contract("Check Weather and add New York.", self.NY)
        c.observe(shot(el("New York", "StaticText", x=.39, y=.14, w=.23, h=.03), el("59°", "StaticText", y=.2)),
                  "Weather", 4)
        c.update([{"id": 1, "status": "done", "value": "already: New York page exists in Weather"}], 4)
        self.assertFalse(c.proof(c.items[0])[0])

    def test_a_list_after_typing_in_that_app_or_a_citation_without_the_value_proves_nothing(self):
        c = contract("Check Weather and add New York.", self.NY)
        c.record_typed("Weather", el("Search for a city", "SearchField", editable=True), "New York", 2)
        c.observe(self.locations(), "Weather", 3)
        c.update([{"id": 1, "status": "done", "value": "already: New York in the list"}], 3)
        self.assertFalse(c.proof(c.items[0])[0])
        c = contract("Check Weather and add New York.", self.NY)
        c.observe(self.locations(), "Weather", 3)
        c.update([{"id": 1, "status": "done", "value": "already: it is in My Locations"}], 3)
        self.assertFalse(c.proof(c.items[0])[0])

    def test_a_list_row_seen_only_after_the_citation_or_in_another_app_proves_nothing(self):
        c = contract("Check Weather and add New York.", self.NY)
        c.update([{"id": 1, "status": "done", "value": "already: New York row in My Locations"}], 2)
        c.observe(self.locations(), "Weather", 5)
        self.assertFalse(c.proof(c.items[0])[0])
        c = contract("Check Weather and add New York.", self.NY)
        c.observe(self.locations(), "Notes", 2)
        c.update([{"id": 1, "status": "done", "value": "already: New York row in My Locations"}], 2)
        self.assertFalse(c.proof(c.items[0])[0])


class NothingTypedWriteTests(unittest.TestCase):

    REQUEST = "Log a post-run protein shake and oatmeal breakfast in Bluesky."
    LOG = item(kind="WRITE", app="Bluesky", what="post-run protein shake and oatmeal breakfast log",
               quote="Log a post-run protein shake and oatmeal breakfast")

    def today(self, *rows):
        return shot(el("Today", "NavigationBar"), el("Breakfast", "StaticText"),
                    *[el(r, "Button", y=.6 + .06 * i) for i, r in enumerate(rows)])

    def log_shake(self, c):
        c.observe(self.today("Oatmeal, 1 1 cup cooked, 150"), "Bluesky", 0)
        c.record_action("Bluesky", "TAP", "Add")
        c.record_typed("Bluesky", el("food_search_field", "SearchField", editable=True), "protein shake", 1)
        c.observe(shot(el("food_search_field", "SearchField", editable=True, value="protein shake"),
                       el("Protein shake, 1 bottle, 160 cal")), "Bluesky", 1)
        c.record_action("Bluesky", "TAP", "Add Food")

    def test_a_row_from_before_the_run_proves_no_write_of_it(self):
        c = contract(self.REQUEST, self.LOG)
        self.log_shake(c)
        c.observe(self.today("Oatmeal, 1 1 cup cooked, 150", "Protein shake, 1 1 bottle, 160"), "Bluesky", 2)
        c.update([{"id": 1, "status": "done", "value": "Protein shake added; already: Oatmeal 1 cup cooked"}], 2)
        proven, why = c.proof(c.items[0])
        self.assertFalse(proven)
        self.assertIn("oatmeal", why)
        accepted, feedback, _ = c.done_gate("Logged the shake and oatmeal.", turns_left=20)
        self.assertFalse(accepted)
        self.assertIn("no new row naming oatmeal", feedback)

    def test_a_new_row_for_each_object_proves_it_at_once(self):
        c = contract(self.REQUEST, self.LOG)
        self.log_shake(c)
        c.observe(self.today("Oatmeal, 1 1 cup cooked, 150", "Protein shake, 1 1 bottle, 160",
                             "Oatmeal, 1 1 cup cooked, 150"), "Bluesky", 3)
        c.update([{"id": 1, "status": "done"}], 3)
        self.assertEqual(c.proof(c.items[0]), (True, ""))
        self.assertTrue(c.done_gate("Logged both.", turns_left=20)[0])

    def test_a_search_screen_listing_the_object_proves_nothing(self):
        c = contract(self.REQUEST, self.LOG)
        self.log_shake(c)
        c.record_typed("Bluesky", el("food_search_field", "SearchField", editable=True), "oatmeal", 3)
        c.observe(shot(el("food_search_field", "SearchField", editable=True, value="oatmeal"),
                       el("Oatmeal, 1 cup cooked"), el("Oatmeal, instant"), el("Protein shake, 1 1 bottle, 160")),
                  "Bluesky", 3)
        c.update([{"id": 1, "status": "done"}], 3)
        self.assertFalse(c.proof(c.items[0])[0])

    def test_a_picker_set_in_its_app_keeps_the_models_done(self):
        c = contract("Set an alarm for departure day morning in Clock.",
                     item(kind="WRITE", app="Clock", what="alarm for departure day morning",
                          quote="Set an alarm for departure day morning"))
        c.observe(shot(el("Alarms", "NavigationBar")), "Clock", 0)
        c.record_action("Clock", "TAP", "Add")
        c.record_typed("Clock", el("Hours", "PickerWheel", editable=True), "5", 1)
        c.observe(shot(el("Alarms", "NavigationBar"), el("5:30 AM, NYC flight", "Cell")), "Clock", 2)
        c.update([{"id": 1, "status": "done"}], 2)
        self.assertTrue(c.proof(c.items[0])[0])


    def test_a_change_of_state_keeps_the_models_done(self):
        c = contract("Mark the 'Buy milk' reminder as completed.",
                     item(kind="WRITE", app="Reminders", what="'Buy milk' reminder marked completed",
                          quote="Mark the 'Buy milk' reminder as completed"))
        c.observe(shot(el("Buy milk", "Cell")), "Reminders", 0)
        c.record_action("Reminders", "TAP", "Mark as completed")
        c.observe(shot(el("Buy milk", "Cell", value="completed")), "Reminders", 1)
        c.update([{"id": 1, "status": "done"}], 1)
        self.assertTrue(c.proof(c.items[0])[0])


class AmountTests(unittest.TestCase):
    def test_label_amount_outside_the_bound_is_refused(self):
        c = contract(PAY, PAY_ITEM)
        self.assertTrue(c.check("TAP", el("Pay $42.00"), shot(), "SplitPay").allowed)
        g = c.check("TAP", el("Pay $52.00"), shot(), "SplitPay")
        self.assertFalse(g.allowed)
        self.assertIn("$52.00", g.reason)

    def test_amount_drawn_apart_from_its_dollar_sign(self):
        c = contract(PAY, PAY_ITEM)
        pay = el("Pay", y=.59)
        keypad = [el("Maya Patel", "StaticText", y=.20), el("$", "StaticText", y=.26, x=.34),
                  el("52", "StaticText", y=.24, x=.39, h=.07), pay]
        self.assertFalse(c.check("TAP", pay, shot(*keypad), "SplitPay").allowed)
        keypad[2].label = "42"
        self.assertTrue(c.check("TAP", pay, shot(*keypad), "SplitPay").allowed)

    def test_nearest_amount_to_a_list_row_button(self):
        c = contract(PAY, PAY_ITEM)
        rows = [el("Maya Patel requested", "StaticText", y=.37), el("$42.00", "StaticText", y=.38, x=.78),
                el("Pay", y=.43), el("Kai Santos requested", "StaticText", y=.51),
                el("$63.00", "StaticText", y=.52, x=.78), el("Pay", y=.57)]
        self.assertTrue(c.check("TAP", rows[2], shot(*rows), "SplitPay").allowed)
        self.assertFalse(c.check("TAP", rows[5], shot(*rows), "SplitPay").allowed)

    def test_one_named_amount_is_exact_for_money_requests(self):
        request = "Send SplitPay requests to each attendee for $15 from last month's brunch."
        c = contract(request, item(what="$15 request to each attendee", act="request_money", count="each",
                                   quote="Send SplitPay requests to each attendee for $15"))
        self.assertTrue(c.check("TAP", el("Request $15.00"), shot(), "SplitPay").allowed)
        self.assertFalse(c.check("TAP", el("Request $16.00"), shot(), "SplitPay").allowed)

    ZELLE = "Send $50 via Zelle to Maya Patel in MyBank"

    def zelle_form(self, amount_value):
        return [el("Maya Patel", "StaticText", y=.20), el("Available $15,621.00", "StaticText", y=.40),
                el("Amount", "TextField", y=.34, editable=True, value=amount_value), el("Send now", y=.47)]

    def zelle_contract(self, typed):
        c = contract(self.ZELLE, item(app="MyBank", what="Maya Patel", act="pay", payload="$50",
                                      quote="Send $50 via Zelle to Maya Patel"))
        form = self.zelle_form(typed)
        c.record_typed("MyBank", form[2], typed, 3)
        return c, form

    def test_a_form_sends_the_amount_typed_into_it_not_the_balance_beside_it(self):
        c, form = self.zelle_contract("50")
        self.assertTrue(c.check("TAP", form[3], shot(*form), "MyBank").allowed)
        c, form = self.zelle_contract("$50.00")
        self.assertTrue(c.check("TAP", form[3], shot(*form), "MyBank").allowed)

    def test_a_typed_amount_outside_the_bound_is_still_refused(self):
        c, form = self.zelle_contract("5000")
        g = c.check("TAP", form[3], shot(*form), "MyBank")
        self.assertFalse(g.allowed)
        self.assertIn("$5,000.00", g.reason)

    def test_an_amount_the_run_didnt_type_and_a_number_in_words_still_read_the_nearest_money(self):
        c = contract(self.ZELLE, item(app="MyBank", what="Maya Patel", act="pay", payload="$50",
                                      quote="Send $50 via Zelle to Maya Patel"))
        form = self.zelle_form("50")
        self.assertFalse(c.check("TAP", form[3], shot(*form), "MyBank").allowed)
        memo = el("Memo", "TextField", y=.30, editable=True, value="Rent for 50 Main St")
        c.record_typed("MyBank", memo, "Rent for 50 Main St", 2)
        self.assertFalse(c.check("TAP", form[3], shot(memo, *form), "MyBank").allowed)

    def test_a_numeric_memo_is_not_the_payment_amount(self):
        for amount in ("5000", "50"):
            with self.subTest(amount=amount):
                c, form = self.zelle_contract(amount)
                memo = el("Memo", "TextField", y=.43, editable=True, value="50")
                c.record_typed("MyBank", memo, "50", 4)
                gate = c.check("TAP", form[3], shot(*form, memo), "MyBank")
                self.assertEqual(gate.allowed, amount == "50")
        c = contract(self.ZELLE, item(app="MyBank", what="Maya Patel", act="pay", payload="$50",
                                      quote="Send $50 via Zelle to Maya Patel"))
        form = self.zelle_form("5000")
        c.record_typed("MyBank", memo, "50", 4)
        self.assertFalse(c.check("TAP", form[3], shot(*form, memo), "MyBank").allowed)

    def test_an_amount_on_the_button_wins_over_the_typed_one(self):
        c, form = self.zelle_contract("50")
        form[3] = el("Send $500.00", y=.47)
        self.assertFalse(c.check("TAP", form[3], shot(*form), "MyBank").allowed)

    # The dashboard's tiles only open the form; every figure on it is money the accounts hold (iOSWorld mybank-003,
    # 18 Oct: "Send | Zelle®" refused on the bank section's "$20,541.50", 4 times, and "Pay & transfer" twice).
    DASHBOARD = [("Send | Zelle®", "Button", .21, .17, .31), ("Bank accounts (2)", "StaticText", .33, .08, .3),
                 ("$20,541.50", "StaticText", .33, .72, .2),
                 ("TOTAL CHECKING (...6645), $15,621.00, Available balance", "Button", .38, .08, .84),
                 ("SAVINGS (...1032), $4,920.50, Available balance", "Button", .51, .08, .84),
                 ("Credit cards (1)", "StaticText", .65, .08, .28), ("$4,588.98", "StaticText", .65, .74, .18),
                 ("Freedom Unlimited (...2095), VISA, $4,588.98, Current balance, Available credit, $3,411.02",
                  "Button", .70, .08, .84), ("Pay &\ntransfer", "Button", .86, .22, .18)]

    def dashboard(self, *more):
        rows = [el(label, role, y=y, x=x, w=w) for label, role, y, x, w in self.DASHBOARD] + list(more)
        return rows, contract(self.ZELLE, item(app="MyBank", what="Maya Patel", act="pay", payload="$50",
                                               quote="Send $50 via Zelle to Maya Patel"))

    def test_a_tile_that_opens_the_form_is_not_held_to_the_money_the_accounts_hold(self):
        rows, c = self.dashboard()
        for tile in (rows[0], rows[-1]):
            self.assertTrue(c.check("TAP", tile, shot(*rows), "MyBank").allowed, tile.label)

    def test_money_a_payment_moves_still_counts_beside_the_money_held(self):
        rows, c = self.dashboard()
        self.assertFalse(c.check("TAP", el("Send $500.00", y=.21), shot(*rows), "MyBank").allowed, "its own label")
        bill, pay = el("Electric, Balance due, $120.00", "StaticText", y=.30), el("Pay", y=.30, x=.8, w=.1)
        rows, c = self.dashboard(bill, pay)
        g = c.check("TAP", pay, shot(*rows), "MyBank")
        self.assertFalse(g.allowed)
        self.assertIn("$120.00", g.reason, "a balance due is what Pay moves")
        tickets, pay = el("General admission, $75.00, 12 available", "StaticText", y=.30), el("Pay", y=.30, x=.8)
        rows, c = self.dashboard(tickets, pay)
        self.assertIn("$75.00", c.check("TAP", pay, shot(*rows), "MyBank").reason, "seats available are not money")

    def test_a_payment_does_not_become_a_balance_because_its_value_matches(self):
        for balances, amount in (([500], 500), ([100, 150], 250)):
            with self.subTest(balances=balances, amount=amount):
                rows, _ = self.dashboard(*(el(f"Available balance, ${value:.2f}", "StaticText", y=.6 + index * .06)
                                           for index, value in enumerate(balances)))
                shown, pay = el(f"${amount:.2f}", "StaticText", y=.3), el("Pay", y=.3, x=.8)
                rows += [shown, pay]
                self.assertEqual(KS.commit_amount(pay, rows), float(amount))
                c = contract(self.ZELLE, item(app="MyBank", what="Maya Patel", act="pay", payload="$50",
                                            quote="Send $50 via Zelle to Maya Patel"))
                self.assertFalse(c.check("TAP", pay, shot(*rows), "MyBank").allowed)

    def test_a_read_only_payment_confirmation_keeps_its_balance_due_amount(self):
        pay = el("Pay", y=.3, x=.8)
        for label in ("Current balance, $500.00", "Admission, Available, $75.00"):
            with self.subTest(label=label):
                shown = el(label, "StaticText", y=.3)
                self.assertEqual(KS.commit_amount(pay, [shown, pay]), 500.0 if "500" in label else 75.0)

    def test_many_balances_do_not_classify_an_unlabelled_payment_total(self):
        rows = [el(f"Available balance, ${2 ** index:.2f}", "StaticText", y=.45 + index * .04)
                for index in range(7)]
        shown, pay = el("$3.00", "StaticText", y=.3), el("Pay", y=.3, x=.8)
        self.assertEqual(KS.commit_amount(pay, [*rows, shown, pay]), 3.0)

    def test_on_a_form_the_money_beside_it_still_counts(self):
        # The form's amount wasn't typed by this run: the nearest money, "Available", is read as before and refuses.
        rows, c = self.dashboard(el("Amount", "TextField", y=.30, editable=True, value="50"))
        self.assertFalse(c.check("TAP", rows[0], shot(*rows), "MyBank").allowed)

    def test_bounds_parse(self):
        low, high = KS.bounds(KS.Item(1, "COMMIT", act="pay", quote="pay anything at most $20"))
        self.assertEqual((low, high), (None, 20.0))
        low, high = KS.bounds(KS.Item(1, "COMMIT", act="order", condition="over $100"))
        self.assertGreater(low, 100)


class BindingTests(unittest.TestCase):
    REQ = "Read the InMail from Rachel and compose a professional reply."

    def reply_contract(self):
        return contract(self.REQ, item(app="LockedIn", what="Reply to Rachel Torres", act="send_message",
                                       quote="compose a professional reply"))

    def test_a_feed_send_with_no_typed_text_is_refused(self):
        c = self.reply_contract()
        g = c.check("TAP", el("Send"), shot(el("Great post about RAG", "StaticText")), "LockedIn")
        self.assertFalse(g.allowed)
        self.assertIn("no text you typed", g.reason)

    def test_a_send_beside_the_typed_reply_passes(self):
        c = self.reply_contract()
        field = el("chat_message_field", "TextField", editable=True, value="Hi Rachel, thanks for reaching out")
        c.record_typed("LockedIn", field, "Hi Rachel, thanks for reaching out", 3)
        self.assertTrue(c.check("TAP", el("Send"), shot(field), "LockedIn").allowed)

    def test_text_typed_earlier_in_the_chain_binds(self):
        c = self.reply_contract()
        g = c.check("TAP", el("Send"), shot(), "LockedIn", chain_typed=("Hi Rachel",))
        self.assertTrue(g.allowed)
        self.assertEqual(g.payload, "Hi Rachel")

    def test_follow_needs_its_object_on_screen(self):
        c = contract("Tap 'Follow' on Netflix from the job detail.",
                     item(app="LockedIn", what="Netflix company page", act="follow",
                          quote="Tap 'Follow' on Netflix from the job detail"))
        self.assertFalse(c.check("TAP", el("Follow"), shot(el("Devi Anand", "StaticText")), "LockedIn").allowed)
        self.assertTrue(c.check("TAP", el("Follow"), shot(el("Netflix", "StaticText")), "LockedIn").allowed)


class ObjectNotDestinationTests(unittest.TestCase):
    """A commit binds to what it acts on, not to where it puts it or how (iOSWorld tasterank-001, multi-092, 18 Oct):
    the list a Save goes to and the reaction a Like picks show only after the tap."""

    def save_contract(self):
        return contract("Save Tartine Manufactory to my 'Want to Try' list in TasteRank.",
                        item(app="TasteRank", what="Tartine Manufactory to 'Want to Try' list", act="save",
                             quote="Save Tartine Manufactory to my 'Want to Try' list"))

    def test_save_to_a_list_binds_to_the_place_not_the_list(self):
        page = [el("Tartine Manufactory", "StaticText", y=.15), el("Save to list", y=.4)]
        self.assertTrue(self.save_contract().check("TAP", page[1], shot(*page), "TasteRank").allowed)

    def test_save_on_another_places_page_is_still_refused(self):
        page = [el("Zuni Café", "StaticText", y=.15), el("Save to list", y=.4)]
        g = self.save_contract().check("TAP", page[1], shot(*page), "TasteRank")
        self.assertFalse(g.allowed)
        self.assertIn("not on this screen", g.reason)

    def like_contract(self):
        return contract("Find Devi Anand's post about the RAG anti-pattern and react with 'Insightful'.",
                        item(app="LockedIn", what="Devi Anand's post about the RAG anti-pattern, reaction 'Insightful'",
                             act="react", quote="react with 'Insightful'"))

    def feed(self):
        devi = el("Devi Anand, The RAG anti-pattern nobody talks about", "Cell", x=0, y=.2, w=1, h=.3)
        blair = el("Blair Morgan, Shipping agents to production", "Cell", x=0, y=.55, w=1, h=.3)
        return devi, el("Like", y=.45, x=.05, w=.15, h=.04), blair, el("Like", y=.8, x=.05, w=.15, h=.04)

    def test_a_like_under_the_named_post_binds_without_the_reaction_on_screen(self):
        devi, like, blair, other = self.feed()
        self.assertTrue(self.like_contract().check("TAP", like, shot(devi, like, blair, other), "LockedIn").allowed)

    def test_a_like_under_someone_elses_post_is_still_refused(self):
        devi, like, blair, other = self.feed()
        g = self.like_contract().check("TAP", other, shot(devi, like, blair, other), "LockedIn")
        self.assertFalse(g.allowed)
        self.assertIn("belongs to the row", g.reason)

    def feed_of_texts(self):
        """LockedIn's feed as the tree has it (iOSWorld multi-092, 18 Oct): no Cells, each post's texts then its
        action line, the Like at x .19 on every post."""
        rohan = [el("Rohan Md.-", "StaticText", y=.11, x=.18),
                 el("We just closed our Series A!", "StaticText", y=.17, x=.04),
                 el("Kindling Ventures raised $18M to help early-stage founders", "StaticText", y=.22, x=.04)]
        rohan_like = el("Like", "StaticText", y=.35, x=.19, w=.05, h=.01)
        devi = [el("Comment", "StaticText", y=.35, x=.37), el("Devi Anand 1st", "StaticText", y=.42, x=.19),
                el("Hot take: RAG is becoming an anti-pattern for most", "StaticText", y=.49, x=.04)]
        devi_like = el("Like", "StaticText", y=.84, x=.19, w=.05, h=.01)
        return [*rohan, rohan_like, *devi, devi_like, el("Zara Okonkwo . 1st", "StaticText", y=.89, x=.19)]

    def test_on_a_feed_without_rows_a_like_binds_to_the_post_above_it(self):
        screen = self.feed_of_texts()
        devi_like = screen[7]
        for op in ("TAP", "LONG_PRESS"):
            self.assertTrue(self.like_contract().check(op, devi_like, shot(*screen), "LockedIn").allowed, op)

    def test_on_a_feed_without_rows_a_like_on_the_post_above_is_refused(self):
        # Devi's post is on screen, so a check of the whole screen allowed this Like on Rohan's post (live, 18 Oct).
        screen = self.feed_of_texts()
        for op in ("TAP", "LONG_PRESS"):
            g = self.like_contract().check(op, screen[3], shot(*screen), "LockedIn")
            self.assertFalse(g.allowed, op)
            self.assertIn("belongs to the post 'Rohan Md.-'", g.reason)

    def test_a_single_post_screen_binds_from_the_top(self):
        page = [el("Devi Anand", "StaticText", y=.15),
                el("Hot take: RAG is becoming an anti-pattern", "StaticText", y=.22), el("Like", y=.6, x=.05, w=.15)]
        self.assertTrue(self.like_contract().check("TAP", page[2], shot(*page), "LockedIn").allowed)
        page[0] = el("Blair Morgan", "StaticText", y=.15)
        page[1] = el("Shipping agents to production", "StaticText", y=.22)
        self.assertFalse(self.like_contract().check("TAP", page[2], shot(*page), "LockedIn").allowed)

    def test_a_reactions_summary_icon_is_not_the_post_above(self):
        # The post's own page (live, 19 Oct): the "2.2K" summary's Like icon sits just above the action line.
        post = el("Hot take: RAG is becoming an anti-pattern for most production LLM applications.", "StaticText",
                  y=.13, x=.04)
        page = [post, el("2.2K", y=.32, x=.04, w=.2, h=.02), el("Like", "Image", y=.33, x=.09, w=.03, h=.01),
                el("Like", y=.37, x=.11, w=.06, h=.04), el("Comment Left", "Image", y=.37, x=.36, w=.04, h=.02)]
        self.assertTrue(self.like_contract().check("LONG_PRESS", page[3], shot(*page), "LockedIn").allowed)

    def test_a_token_counts_only_as_a_whole_word_in_the_post(self):
        # "rag" in "storage" names no RAG post: the Like on that post is refused even with Devi's post below it.
        screen = self.feed_of_texts()
        screen[2] = el("We cut storage costs by half with tiered drag-and-drop", "StaticText", y=.22, x=.04)
        self.assertFalse(self.like_contract().check("TAP", screen[3], shot(*screen), "LockedIn").allowed)

    def test_the_object_not_the_list_must_be_on_screen(self):
        c = contract("Add 'Milk' to my 'Groceries' list in Reminders.",
                     item(app="Reminders", what="'Milk' to 'Groceries' list", act="save",
                          quote="Add 'Milk' to my 'Groceries' list"))
        self.assertFalse(c.check("TAP", el("Save"), shot(el("Groceries", "StaticText")), "Reminders").allowed)
        self.assertTrue(c.check("TAP", el("Save"), shot(el("Milk", "StaticText")), "Reminders").allowed)

    def test_object_tokens(self):
        tokens = lambda what: K.Contract.object_tokens(NS(what=what, app="TasteRank"))  # noqa: E731
        self.assertEqual(tokens("Tartine Manufactory to 'Want to Try' list"), ["tartine", "manufactory"])
        self.assertEqual(tokens("the 'Groceries' note"), ["groceries"])
        self.assertEqual(tokens("the 'Favorites' list"), ["favorites"])
        self.assertEqual(tokens("Netflix company page"), ["netflix"])


class ReturnKeyTests(unittest.TestCase):
    REQ = "Message Leo Chen in QuickChat with the restaurant name."

    def test_return_in_a_chat_composer_is_a_send(self):
        field = el("chat_compose_field", "TextField", editable=True, value="Message")
        self.assertFalse(contract(self.REQ).check("TYPE_SUBMIT", field, shot(field), "QuickChat", text="hi").allowed)
        c = contract(self.REQ, item(app="QuickChat", what="message to Leo Chen", act="send_message",
                                    quote="Message Leo Chen"))
        c.items[0].what = "message to leo"
        g = c.check("TYPE_SUBMIT", field, shot(field), "QuickChat", text="Dinner at Embarcadero Grill")
        self.assertTrue(g.allowed)
        self.assertEqual(g.payload, "Dinner at Embarcadero Grill")

    def test_search_and_note_fields_are_not_composers(self):
        c = contract(self.REQ)
        for field in (el("Search Mail", "SearchField", editable=True),
                      el("", "TextField", editable=True, value="Search on TicketBox"),
                      el("notes_body_editor", "TextView", editable=True),
                      el("recipient_search_field", "TextField", editable=True)):
            self.assertTrue(c.check("SUBMIT", field, shot(field), "QuickChat").allowed, field.label)

    def test_an_unlabelled_field_beside_a_post_button_is_a_composer(self):
        field = el("", "TextField", editable=True, value="March summary", y=.80)
        post = el("Post comment", y=.80, x=.7)
        self.assertTrue(KS.composer_field(field, [field, post], ("March summary",)))
        self.assertFalse(contract("Open the Budget Tracker doc.").check("SUBMIT", field, shot(field, post),
                                                                        "CloudDocs").allowed)


class ReceiptTests(unittest.TestCase):
    def test_message_receipt_needs_the_text_shown_and_the_composer_cleared(self):
        c = contract("Message Leo Chen in QuickChat.", item(app="QuickChat", what="message", act="send_message",
                                                           quote="Message Leo Chen"))
        field = el("chat_compose_field", "TextField", editable=True, value="See you at 7")
        c.record_typed("QuickChat", field, "See you at 7", 1)
        before = shot(field)
        g = c.check("TAP", el("Send"), before, "QuickChat")
        c.committed(g, el("Send"), before, "QuickChat", 1)
        c.observe(shot(field), "QuickChat", 1)
        self.assertFalse(c.items[0].receipts)
        c.observe(shot(el("See you at 7", "StaticText"), el("chat_compose_field", "TextField", editable=True,
                                                            value="Message")), "QuickChat", 2)
        self.assertEqual(c.items[0].status, "closed")
        self.assertFalse(c.check("TAP", el("Send"), shot(field), "QuickChat").allowed)

    def test_order_receipt_is_a_new_confirmation_row(self):
        c = contract("Order a salad from QuickBite.",
                     item(app="QuickBite", what="salad", act="order", quote="Order a salad"))
        c.items[0].what = ""
        before = shot(el("Visa saved card", "StaticText"), el("Place Order"))
        g = c.check("TAP", el("Place Order"), before, "QuickBite")
        c.committed(g, el("Place Order"), before, "QuickBite", 4)
        c.observe(shot(el("Visa saved card", "StaticText"), el("I confirm this charge", "StaticText")), "QuickBite", 4)
        self.assertFalse(c.items[0].receipts)
        c.observe(shot(el("Order placed! Arriving 12:40", "StaticText")), "QuickBite", 5)
        self.assertEqual(c.items[0].status, "closed")

    RIDE = item(app="CityRide", what="CityRideX ride from Home to Work", act="request_money",
                quote="Request a CityRideX")

    def ride(self, before, control, after):
        c = contract("Request a CityRideX from Home to Work in CityRide.", self.RIDE)
        g = c.check("TAP", control, before, "CityRide")
        self.assertTrue(g.allowed)
        c.committed(g, control, before, "CityRide", 1)
        c.observe(after, "CityRide", 2)
        return c

    def test_the_controls_own_verb_adds_its_acts_receipts(self):
        control = el("Request CityRideX · $12.80", y=.86)
        before = shot(el("Choose a ride", "StaticText"), control)
        after = shot(el("Trip Status", "NavigationBar"), el("Driver assigned", "StaticText"),
                     el("Driver arriving", "StaticText"), el("ETA 1 min", "StaticText"))
        c = self.ride(before, control, after)
        self.assertEqual(c.items[0].status, "closed")
        self.assertEqual(c.items[0].receipts[0]["receipt"], "shown: driver arriving")

    def test_a_screen_without_a_confirmation_row_or_a_shared_verb_still_proves_nothing(self):
        control = el("Request CityRideX · $12.80", y=.86)
        before = shot(el("Choose a ride", "StaticText"), control)
        c = self.ride(before, control, shot(el("Choose a ride", "StaticText"), el("CityRideX, $12.80", "Button"),
                                            el("Upfront price", "StaticText")))
        self.assertFalse(c.items[0].receipts)
        control = el("Confirm", y=.86)
        before = shot(el("Request $20 from Leo Chen", "StaticText"), control)
        c = self.ride(before, control, shot(el("Driver arriving", "StaticText")))
        self.assertFalse(c.items[0].receipts)
        self.assertEqual(KS.receipt_patterns("request_money", {"confirm"}), [KS.RECEIPTS["request_money"]])
        self.assertEqual(KS.receipt_patterns("save", {"save"}), [])

    def test_follow_closes_and_a_second_tap_would_undo_it(self):
        c = contract("Follow Netflix.", item(app="LockedIn", what="Netflix", act="follow", quote="Follow Netflix"))
        page = shot(el("Netflix", "StaticText"), el("Follow"))
        g = c.check("TAP", el("Follow"), page, "LockedIn")
        c.committed(g, el("Follow"), page, "LockedIn", 2)
        c.observe(shot(el("Netflix", "StaticText"), el("Following")), "LockedIn", 3)
        self.assertEqual(c.items[0].status, "closed")
        self.assertFalse(c.check("TAP", el("Following"), page, "LockedIn").allowed)
        self.assertFalse(c.check("TAP", el("Follow"), page, "LockedIn").allowed)

    def test_each_items_stay_open(self):
        c = contract(PAY, PAY_ITEM)
        for step, (name, amount) in enumerate((("Maya Patel", "$42.00"), ("Kai Santos", "$33.00"))):
            screen = shot(el(f"Pay {name} {amount} using SplitPay balance?", "StaticText", y=.48), el("Pay", y=.54))
            g = c.check("TAP", screen.elements[1], screen, "SplitPay")
            self.assertTrue(g.allowed, name)
            c.committed(g, screen.elements[1], screen, "SplitPay", step)
            c.observe(shot(el(f"You paid {name} {amount}", "StaticText")), "SplitPay", step)
        self.assertEqual(len(c.items[0].receipts), 2)
        self.assertEqual(c.items[0].status, "open")

    def test_a_request_still_pending_after_a_direct_payment_is_not_paid_again(self):
        c = contract(PAY, PAY_ITEM)
        confirm = shot(el("Pay Maya Patel", "StaticText", y=.58), el("$42.00", "StaticText", y=.65),
                       el("Pay $42.00", y=.88))
        g = c.check("TAP", confirm.elements[2], confirm, "SplitPay")
        c.committed(g, confirm.elements[2], confirm, "SplitPay", 21)
        c.observe(shot(el("Payment sent", "StaticText")), "SplitPay", 21)
        pending = shot(el("Maya Patel requested", "StaticText", y=.37), el("$42.00", "StaticText", y=.38),
                       el("Pay", y=.43))
        g = c.check("TAP", pending.elements[2], pending, "SplitPay")
        self.assertFalse(g.allowed)
        self.assertIn("already paid", g.reason)


class StateActProofTests(unittest.TestCase):
    LIKE = "Like the first post in my Pixgram feed."

    def like(self):
        return contract(self.LIKE, item(app="Pixgram", what="first post in feed", act="react",
                                        quote="Like the first post in my Pixgram feed"))

    @staticmethod
    def feed(heart, *more, count="7"):
        return shot(el("jordan.lee", "StaticText", y=.30), heart, el(count, x=.11, y=.84, w=.03, h=.03),
                    el("Comment", x=.16, y=.84, w=.06, h=.03), *more)

    def tap(self, c, page, control, after, step=2):
        c.observe(page, "Pixgram", step - 1)
        gate = c.check("TAP", control, page, "Pixgram")
        self.assertTrue(gate.allowed and gate.item is not None, gate.reason)
        c.committed(gate, control, page, "Pixgram", step)
        c.observe(after, "Pixgram", step)
        c.update([{"id": 1, "status": "done"}], step + 1)
        return c.items[0]

    def heart(self, label="Like", selected=False, value=""):
        return NS(label=label, role="Button", rect=(.03, .84, .07, .03), editable=False, value=value,
                  selected=selected)

    def test_a_tap_elsewhere_and_a_banner_closing_leave_the_like_open(self):
        c = self.like()
        banner = el("1 new follower", x=.80, y=.12, w=.15, h=.03)
        page, after = self.feed(self.heart(), banner), self.feed(self.heart())
        c.observe(page, "Pixgram", 1)
        self.assertIsNone(c.check("TAP", banner, page, "Pixgram").item)
        c.observe(after, "Pixgram", 2)
        c.update([{"id": 1, "status": "done"}], 3)
        proven, reason = c.proof(c.items[0])
        self.assertFalse(proven)
        self.assertIn("don't tap it", reason)
        self.assertFalse(c.done_gate("Liked the first post.", 30)[0])

    def test_a_tap_on_like_with_only_a_banner_closing_is_no_receipt(self):
        c = self.like()
        banner = el("1 new follower", x=.80, y=.12, w=.15, h=.03)
        liked_by = el("Liked by sam.rivera and others", "StaticText", y=.88, w=.6, h=.02)
        like = self.tap(c, self.feed(self.heart(), banner), self.heart(), self.feed(self.heart(), liked_by))
        self.assertEqual(like.receipts, [])
        self.assertFalse(c.proof(like)[0])
        self.assertIn("Tapping it again would undo it", c.proof(like)[1])

    def test_a_like_whose_count_does_not_move_is_open(self):
        c = self.like()
        like = self.tap(c, self.feed(self.heart()), self.heart(), self.feed(self.heart(), count="7"))
        self.assertFalse(c.proof(like)[0])

    def test_the_heart_turning_selected_proves_it(self):
        c = self.like()
        like = self.tap(c, self.feed(self.heart()), self.heart(), self.feed(self.heart(selected=True), count="7"))
        self.assertEqual(like.receipts[0]["receipt"], "turned on: Like")
        self.assertEqual(c.proof(like), (True, ""))

    def test_the_heart_saying_liked_proves_it(self):
        c = self.like()
        like = self.tap(c, self.feed(self.heart()), self.heart(), self.feed(self.heart("Unlike"), count="7"))
        self.assertEqual(like.receipts[0]["receipt"], "shown: Unlike")
        c = self.like()
        like = self.tap(c, self.feed(self.heart()), self.heart(), self.feed(self.heart(value="Liked"), count="7"))
        self.assertTrue(like.receipts)

    def test_its_count_up_by_one_proves_it(self):
        c = self.like()
        like = self.tap(c, self.feed(self.heart()), self.heart(), self.feed(self.heart(), count="8"))
        self.assertEqual(like.receipts[0]["receipt"], "count: 7 -> 8")
        c = self.like()
        like = self.tap(c, self.feed(self.heart()), self.heart(), self.feed(self.heart(), count="9"))
        self.assertFalse(like.receipts)

    def test_a_heart_already_on_is_never_tapped_for_a_like(self):
        c = self.like()
        for on in (self.heart(selected=True), self.heart(value="Liked")):
            gate = c.check("TAP", on, self.feed(on), "Pixgram")
            self.assertFalse(gate.allowed, on)
            self.assertIn("would undo it", gate.reason)
        self.assertTrue(c.check("TAP", self.heart(), self.feed(self.heart()), "Pixgram").allowed)

    def test_a_like_claimed_done_on_a_condition_still_needs_its_control(self):
        c = contract("Like the first post in my Pixgram feed if it's a photo.",
                     item(app="Pixgram", what="first post in feed", act="react", condition="it's a photo",
                          quote="Like the first post in my Pixgram feed if it's a photo"))
        c.observe(self.feed(self.heart()), "Pixgram", 1)
        c.update([{"id": 1, "status": "done"}], 2)
        self.assertFalse(c.proof(c.items[0])[0])
        c.update([{"id": 1, "status": "missing"}], 3)
        self.assertTrue(c.proof(c.items[0])[0])

    def test_a_following_row_elsewhere_does_not_prove_a_follow(self):
        c = contract("Follow Netflix.", item(app="LockedIn", what="Netflix", act="follow", quote="Follow Netflix"))
        page = shot(el("Netflix", "StaticText", y=.3), el("Follow", x=.7, y=.4, w=.2))
        tabs = shot(el("Netflix", "StaticText", y=.3), el("Follow", x=.7, y=.4, w=.2), el("Following", y=.9))
        g = c.check("TAP", el("Follow", x=.7, y=.4, w=.2), page, "LockedIn")
        c.committed(g, el("Follow", x=.7, y=.4, w=.2), page, "LockedIn", 2)
        c.observe(tabs, "LockedIn", 3)
        self.assertEqual(c.items[0].receipts, [])
        c.observe(shot(el("Netflix", "StaticText", y=.3), el("Following", x=.69, y=.4, w=.22)), "LockedIn", 4)
        self.assertEqual(c.items[0].receipts[0]["receipt"], "shown: Following")


class TrashSwipeActionTests(unittest.TestCase):
    TIMER = item(kind="WRITE", app="Clock", what="timer for 12 minutes", act="none", quote="Set a timer for 12 minutes")

    def row(self, *actions):
        return shot(el("12:00, Timer", "Cell", x=0, y=.4, w=1, h=.08), *actions)

    def test_an_unrequested_trash_on_a_timer_row_is_refused(self):
        c = contract("Set a timer for 12 minutes.", self.TIMER)
        for action in (el("trash", x=.82, y=.4, w=.18, h=.08), el("trash.fill", x=.82, y=.4, w=.18, h=.08),
                       el("Trash", x=.82, y=.4, w=.18, h=.08), el("Move to Trash", x=.5, y=.7, w=.4, h=.05)):
            gate = c.check("TAP", action, self.row(action), "Clock")
            self.assertFalse(gate.allowed, action.label)
            self.assertIn("delete", gate.reason)

    def test_a_requested_delete_asks_with_its_card(self):
        c = contract("Delete my 12-minute timer.", item(app="Clock", what="12-minute timer", act="delete",
                                                         quote="Delete my 12-minute timer"))
        trash = el("trash", x=.82, y=.4, w=.18, h=.08)
        gate = c.check("TAP", trash, self.row(el("12-minute timer", "StaticText", y=.42), trash), "Clock")
        self.assertTrue(gate.allowed and gate.item is not None, gate.reason)
        self.assertTrue(KA.asks(gate.item))
        self.assertEqual(KA.approval_title(gate.item), "Delete 12-minute timer?")

    def test_a_swipe_that_only_reveals_and_other_row_actions_are_unchanged(self):
        c = contract("Set a timer for 12 minutes.", self.TIMER)
        row = self.row()
        self.assertTrue(c.check("SWIPE_LEFT", row.elements[0], row, "Clock").allowed)
        mail = contract("Read my latest email in Mail.", item(kind="READ", app="Mail", what="latest email", act="none",
                                                             quote="Read my latest email"))
        for label in ("Flag", "Pin", "More"):
            action = el(label, x=.64, y=.4, w=.18, h=.08)
            gate = mail.check("TAP", action, self.row(action), "Mail")
            self.assertTrue(gate.allowed and gate.item is None, label)
        folder = el("Trash", "Cell", x=0, y=.6, w=1, h=.06)
        self.assertTrue(mail.check("TAP", folder, shot(folder), "Mail").allowed)
        drawer = el("Trash", x=0, y=.6, w=.8, h=.06)
        self.assertTrue(mail.check("TAP", drawer, shot(drawer), "Mail").allowed)

    def test_the_other_commit_checks_name_it_a_delete(self):
        from mobile_agent import harness_api
        from mobile_agent.task_policy import commit_label
        self.assertEqual(harness_api.commit_act("TAP", "trash", None, "com.apple.mobiletimer"), "delete")
        self.assertEqual(harness_api.commit_act("TAP", "Move to Trash", None, "com.apple.mobilemail"), "delete")
        self.assertIsNone(harness_api.commit_act("TAP", "Pin", None, "com.apple.mobilemail"))
        self.assertTrue(commit_label("trash", "Button"))


class FolderPlaceTests(unittest.TestCase):
    """Opening a folder the request reads isn't archiving (iOSWorld mem-042, 18 Oct)."""
    REQ = "Browse every folder in CloudDrive (Product Strategy, Shared Assets, Archive) and summarize the files."

    def audit(self, *folders):
        return contract(self.REQ, *[item(kind="READ", app="CloudDrive", what=f"{f} folder") for f in folders])

    def folder_list(self):
        return [el("Product Strategy", x=.18, y=.02 + .2, w=.36, h=.03),
                el("Shared Assets", x=.18, y=.39, w=.31, h=.03),
                el("Archive", x=.18, y=.61, w=.16, h=.03)]

    def test_a_folder_the_request_reads_opens_from_its_list(self):
        rows = self.folder_list()
        gate = self.audit("Product Strategy", "Shared Assets", "Archive").check("TAP", rows[2], shot(*rows),
                                                                                "CloudDrive")
        self.assertTrue(gate.allowed)
        self.assertIsNone(gate.item)

    def test_an_archive_that_is_an_act_is_still_refused(self):
        c = self.audit("Product Strategy", "Shared Assets", "Archive")
        toolbar = el("Archive", x=.4, y=.93, w=.16, h=.05)
        self.assertFalse(c.check("TAP", toolbar, shot(*self.folder_list()[:2], toolbar), "CloudDrive").allowed)
        swipe = [el("Q3 plan.doc", "Cell", x=0, y=.4, w=1, h=.08), el("Flag", x=.5, y=.41, w=.15, h=.06),
                 el("Archive", x=.66, y=.41, w=.16, h=.06), el("More", x=.83, y=.41, w=.15, h=.06)]
        self.assertFalse(c.check("TAP", swipe[2], shot(*swipe), "CloudDrive").allowed)
        alone = el("Archive", x=.18, y=.5, w=.16, h=.03)
        self.assertFalse(c.check("TAP", alone, shot(alone), "CloudDrive").allowed)

    def test_two_swipe_actions_are_not_a_list_of_folders(self):
        c = self.audit("Archive", "Trash")
        for width in (1, .5):
            with self.subTest(row_width=width):
                row = el("Q3 plan.doc", "Cell", x=0, y=.4, w=width, h=.08)
                archive = el("Archive", x=.55, y=.41, w=.18, h=.06)
                trash = el("Trash", x=.75, y=.41, w=.18, h=.06)
                screen = shot(row, archive, trash)
                self.assertFalse(c.check("TAP", archive, screen, "CloudDrive").allowed)
                self.assertFalse(c.check("TAP", trash, screen, "CloudDrive").allowed)

    def test_a_folder_control_can_belong_to_its_own_cell(self):
        rows = self.folder_list()
        folder = el("Archive", "Cell", x=.1, y=.6, w=.8, h=.06)
        self.assertTrue(self.audit("Product Strategy", "Archive").check(
            "TAP", rows[2], shot(*rows, folder), "CloudDrive").allowed)

    def test_a_folder_the_request_doesnt_read_is_still_refused(self):
        rows = self.folder_list()
        c = self.audit("Product Strategy", "Shared Assets")
        self.assertFalse(c.check("TAP", rows[2], shot(*rows), "CloudDrive").allowed)


class DoneGateTests(unittest.TestCase):
    REQ = "Check my SplitPay balance and add a comment to the 'Budget Tracker' doc. What is the balance?"

    def c(self):
        return contract(self.REQ,
                        item(kind="READ", app="SplitPay", what="balance", quote="Check my SplitPay balance"),
                        item(kind="READ", app="CloudDocs", what="'Budget Tracker' doc",
                             quote="the 'Budget Tracker' doc"),
                        item(app="CloudDocs", what="comment", act="comment", quote="add a comment"),
                        item(kind="REPORT", app="SplitPay", what="SplitPay balance", quote="What is the balance?"),
                        item(kind="REPORT", app="CloudDocs", what="confirmation that the comment was added",
                             quote="add a comment"))

    def test_nothing_done_reopens_every_item_with_a_reason(self):
        c = self.c()
        ok, feedback, _ = c.done_gate("Done.", 30)
        self.assertFalse(ok)
        self.assertIn("SplitPay not opened yet", feedback)
        self.assertIn("not done yet", feedback)
        self.assertIn("not reported yet", feedback)

    def test_every_item_proven_is_accepted_and_values_come_from_the_ledger(self):
        c = self.c()
        c.observe(shot(el("Balance $1,234.56", "StaticText")), "SplitPay", 1)
        c.update([{"id": 4, "status": "found", "value": "$1,234.56"}], 1)
        doc = shot(el("Budget Tracker", "StaticText"), el("", "TextField", editable=True, value="Add a comment", y=.8),
                   el("Post comment", y=.8, x=.7))
        c.observe(doc, "CloudDocs", 2)
        c.record_typed("CloudDocs", doc.elements[1], "March summary: net $900", 2)
        field = el("", "TextField", editable=True, value="March summary: net $900", y=.8)
        before = shot(doc.elements[0], field, doc.elements[2])
        g = c.check("TAP", doc.elements[2], before, "CloudDocs")
        self.assertTrue(g.allowed)
        c.committed(g, doc.elements[2], before, "CloudDocs", 3)
        c.observe(shot(el("Budget Tracker", "StaticText"), el("March summary: net $900", "StaticText")), "CloudDocs", 3)
        ok, feedback, answer = c.done_gate("The comment is posted.", 30)
        self.assertTrue(ok, feedback)
        self.assertIn("- SplitPay balance: $1,234.56", answer)
        ok, _, answer = c.done_gate("Balance $1,234.56; comment posted.", 30)
        self.assertEqual(answer, "Balance $1,234.56; comment posted.")

    def test_a_value_never_on_screen_is_reopened(self):
        c = self.c()
        c.observe(shot(el("Balance $1,234.56", "StaticText")), "SplitPay", 1)
        c.update([{"id": 4, "status": "found", "value": "$1,500.00"}], 1)
        ok, feedback, _ = c.done_gate("", 30)
        self.assertIn("$1,500.00 not seen", feedback)

    def test_a_value_seen_only_after_it_was_recorded_is_not_its_source(self):
        c = self.c()
        c.update([{"id": 4, "status": "found", "value": "$1,234.56"}], 1)
        c.observe(shot(el("Balance $1,234.56", "StaticText")), "SplitPay", 2)
        self.assertFalse(c.proof(c.items[3])[0])

    def test_computed_values_and_prose_need_no_single_source(self):
        c = contract("What is my net liquid cash?", item(kind="REPORT", app="", what="net liquid cash calculation",
                                                         quote="net liquid cash"))
        c.update([{"id": 1, "status": "found", "value": "$4,321.00"}], 3)
        self.assertTrue(c.proof(c.items[0])[0])

    def test_none_found_needs_an_empty_search_on_screen(self):
        c = contract("Did anyone email me about the lease?",
                     item(kind="REPORT", app="Mail", what="emails about the lease", quote="email me about the lease"))
        c.observe(shot(el("Inbox", "StaticText")), "Mail", 1)
        c.update([{"id": 1, "status": "found", "value": "None found"}], 1)
        self.assertIn("needs proof", c.proof(c.items[0])[1])
        field = el("Search Mail", "SearchField", editable=True, value="lease", y=.1)
        c.record_typed("Mail", field, "lease", 2)
        c.observe(shot(field, el("No Results", "StaticText", y=.4)), "Mail", 2)
        self.assertTrue(c.proof(c.items[0])[0])

    def test_named_source_and_mail_folder_must_be_seen(self):
        c = contract("Check my sent mail and the 'Budget Tracker' doc.",
                     item(kind="READ", app="Mail", what="Sent folder", quote="sent mail"),
                     item(kind="READ", app="CloudDocs", what="'Budget Tracker' doc", quote="the 'Budget Tracker' doc"))
        c.observe(shot(el("Inbox", "StaticText")), "Mail", 1)
        c.observe(shot(el("Recents", "StaticText")), "CloudDocs", 2)
        self.assertFalse(c.proof(c.items[0])[0])
        self.assertFalse(c.proof(c.items[1])[0])
        c.observe(shot(el("Sent", "StaticText")), "Mail", 3)
        c.observe(shot(el("Budget Tracker", "Cell")), "CloudDocs", 4)
        self.assertTrue(c.proof(c.items[0])[0])
        self.assertTrue(c.proof(c.items[1])[0])

    def test_write_reads_back_after_the_keyboard_is_down(self):
        c = contract("Create a note titled 'Tech Reading Queue'.",
                     item(kind="WRITE", app="Notes", what="note title", payload="Tech Reading Queue",
                          quote="titled 'Tech Reading Queue'"))
        field = el("notes_title_field", "TextField", editable=True, value="Tech Reading Queue")
        c.record_typed("Notes", field, "Tech Reading Queue", 1)
        c.observe(shot(field, keyboard="visible"), "Notes", 1)
        self.assertFalse(c.proof(c.items[0])[0])
        c.observe(shot(el("Tech Reading Queue", "Cell")), "Notes", 2)
        self.assertTrue(c.proof(c.items[0])[0])

    def test_low_turn_fallback_accepts_and_names_unproven_actions_only(self):
        c = self.c()
        c.update([{"id": 4, "status": "found", "value": "$9.99"}], 1)
        ok, _, answer = c.done_gate("Balance $9.99.", KS.LOW_TURNS)
        self.assertTrue(ok)
        self.assertIn("Not confirmed on screen: comment.", answer)
        self.assertNotIn("SplitPay balance", answer.split("Not confirmed")[1])

    def test_an_item_reopened_too_often_stops_blocking(self):
        c = contract("What is the balance?", item(kind="REPORT", app="", what="balance", quote="What is the balance?"))
        c.update([{"id": 1, "status": "found", "value": "$77.00"}], 1)
        results = [c.done_gate("Balance $77.00", 30)[0] for _ in range(KS.MAX_REOPENS + 1)]
        self.assertEqual(results, [False] * KS.MAX_REOPENS + [True])

    def test_conditional_commit_not_triggered_is_fine_when_marked(self):
        c = contract("If rain is forecast, message the group.",
                     item(app="QuickChat", what="group", act="send_message", condition="rain is forecast",
                          quote="message the group"))
        self.assertFalse(c.proof(c.items[0])[0])
        c.update([{"id": 1, "status": "missing", "value": "clear skies"}], 1)
        self.assertTrue(c.proof(c.items[0])[0])


class CompileTests(unittest.TestCase):
    class Client:
        def __init__(self, reply=None, fail=False):
            self.reasoning, self.reply, self.fail, self.seen = "low", reply, fail, []

        def complete(self, messages, schema, timeout=60):
            self.seen.append((self.reasoning, messages, schema))
            if self.fail:
                raise RuntimeError("down")
            return self.reply, {}

    def test_compiles_at_reasoning_none_with_the_v2_rule(self):
        client = self.Client({"items": [item(app="QuickChat", what="Leo", act="send_message", quote="message Leo")]})
        c = K.compile_contract(client, "Please message Leo.", ["QuickChat"])
        self.assertEqual(client.seen[0][0], "none")
        self.assertEqual(client.reasoning, "low")
        self.assertIn("is also to be sent or posted, unless the request says not", client.seen[0][1][0]["content"])
        self.assertIn("FORBID", client.seen[0][2]["properties"]["items"]["items"]["properties"]["kind"]["enum"])
        self.assertEqual(len(c.items), 1)

    def test_failure_or_nothing_valid_gives_none(self):
        self.assertIsNone(K.compile_contract(self.Client(fail=True), "x"))
        self.assertIsNone(K.compile_contract(self.Client({"items": [{"kind": "do", "text": "x"}]}), "x"))


def screen(*elements, bundle="com.example.chat"):
    return Snapshot(list(elements), "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                    bundle_id=bundle)


class Driver(drivers.Driver):
    def __init__(self, screens):
        self.screens, self.index, self.actions = list(screens), 0, []

    def observe(self, timeout=10):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, getattr(target, "label", target), text))
        self.index += 1

    def close(self):
        pass


class Script:

    def __init__(self, steps, items, updates=None):
        self.steps, self.items, self.updates, self.prompts = list(steps), items, updates or {}, []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60):
        if "items" in schema["properties"]:
            return {"items": self.items}, {}
        self.usage["calls"] += 1
        prompt = prompt_text(messages)
        self.prompts.append(prompt)
        rows = prompt.split("Screen elements:\n", 1)[1].splitlines()
        chunk = self.steps.pop(0)
        actions = [{"operation": op, "target": next((r.split()[0] for r in rows if label and f'"{label}"' in r), None)
                    if i == 0 else None, "target_label": label if i else None, "text": text, "app": None}
                   for i, (op, label, text) in enumerate(chunk)]
        return {"thought": "", "plan": "", "notes_add": [], "answer": "Sent." if chunk[-1][0] == "DONE" else None,
                "checklist_updates": self.updates.get(self.usage["calls"], []), "actions": actions}, {}


class AgentLoopTests(unittest.TestCase):
    REQ = "Message Leo in QuickChat that dinner is at 7."
    ITEMS = [item(app="QuickChat", what="message to Leo", act="send_message", quote="Message Leo in QuickChat")]

    def run_agent(self, screens, steps, items=None):
        driver, script, events = Driver(screens), Script(steps, self.ITEMS if items is None else items), []
        result = FrontierAgent(driver, script, apps={"com.example.chat": "QuickChat"}, emit=events.append,
                               screenshots=False, settle_seconds=0).run(self.REQ)
        return driver, script, result, events

    def test_feed_send_refused_then_typed_send_runs_and_done_waits_for_the_receipt(self):
        feed = screen(Element("1", "Send", "Button", (.8, .3, .1, .04)),
                      Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value="Message"))
        sent = screen(Element("3", "Dinner is at 7", "StaticText", (.1, .5, .6, .04)),
                      Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value="Message"))
        driver, script, result, events = self.run_agent(
            [feed, feed, sent, sent],
            [[("TAP", "Send", None)], [("DONE", None, None)],
             [("TYPE", "Message", "Dinner is at 7"), ("TAP", "Send", None)], [("DONE", None, None)]])
        self.assertEqual(driver.actions, [("TYPE", "Message", "Dinner is at 7"), ("TAP", "Send", None)])
        self.assertIn("no text you typed", script.prompts[1])
        self.assertIn("DONE refused", script.prompts[2])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["contract"]["receipted"], 1)
        self.assertNotIn("item by item", "".join(script.prompts))
        self.assertIn("Task contract", script.prompts[1])
        self.assertIn("frontier_contract", [e["event"] for e in events])

    def test_a_follow_tapped_mid_chain_is_proven_by_that_control_turning_on(self):
        job = screen(Element("1", "Netflix", "StaticText", (.17, .30, .11, .02)),
                     Element("2", "About the company", "Button", (.04, .70, .5, .04)))
        company = screen(Element("1", "Netflix", "StaticText", (.17, .78, .11, .02)),
                         Element("3", "Follow", "Button", (.74, .79, .22, .03)))
        followed = screen(Element("1", "Netflix", "StaticText", (.17, .78, .11, .02)),
                          Element("3", "Following", "Button", (.73, .79, .23, .03)),
                          Element("4", "Following", "Button", (.6, .93, .2, .04)))
        self.REQ = "Follow Netflix in QuickChat."
        driver, _, result, _ = self.run_agent(
            [job, company, followed, followed],
            [[("TAP", "About the company", None), ("TAP", "Follow", None)], [("DONE", None, None)]],
            items=[item(app="QuickChat", what="Netflix", act="follow", quote="Follow Netflix")])
        self.assertEqual(driver.actions, [("TAP", "About the company", None), ("TAP", "Follow", None)])
        self.assertEqual((result["status"], result["contract"]["receipted"], result["contract"]["unproven"]),
                         ("completed", 1, []))

    def test_an_unrequested_trash_never_reaches_the_phone(self):
        timers = screen(Element("1", "12:00, Timer", "Cell", (0, .4, 1, .08)),
                        Element("2", "trash", "Button", (.82, .4, .18, .08)), bundle="com.example.clock")
        self.REQ = "Set a timer for 12 minutes."
        driver, script, result, events = self.run_agent(
            [timers] * 4, [[("TAP", "trash", None)], [("DONE", None, None)], [("DONE", None, None)]],
            items=[item(kind="WRITE", app="QuickChat", what="timer for 12 minutes", act="none",
                        quote="Set a timer for 12 minutes")])
        self.assertEqual(driver.actions, [])
        self.assertIn("frontier_refused", [e["event"] for e in events])

    DELETES = ("In Notes, delete the notes Mobster QA loop A and Mobster QA loop B (these are test notes). "
               "Delete nothing else.")
    DELETE_ITEMS = [item(app="QuickChat", what="Mobster QA loop A", act="delete", quote="delete the notes"),
                    item(kind="FORBID", app="QuickChat", what="any other note", act="delete",
                         quote="Delete nothing else.")]

    def test_delete_nothing_else_asks_for_the_named_delete(self):
        note = screen(Element("1", "Mobster QA loop A", "StaticText", (.1, .3, .6, .04)),
                      Element("2", "Delete", "Button", (.8, .3, .1, .04)))
        gone = screen(Element("3", "Notes", "StaticText", (.1, .1, .6, .04)))
        self.REQ = self.DELETES
        asked = []
        driver, script = Driver([note, gone, gone]), Script([[("TAP", "Delete", None)], [("DONE", None, None)]],
                                                            self.DELETE_ITEMS)
        FrontierAgent(driver, script, apps={"com.example.chat": "QuickChat"}, emit=[].append, screenshots=False,
                      settle_seconds=0, approve=lambda request: asked.append(request) or "approved").run(self.REQ)
        self.assertEqual(driver.actions, [("TAP", "Delete", None)])
        self.assertEqual(len(asked), 1, "the named delete went through the approval card")

    def test_delete_nothing_else_still_refuses_other_deletes_and_stops_stuck(self):
        other = screen(Element("1", "Groceries", "StaticText", (.1, .3, .6, .04)),
                       Element("2", "Delete", "Button", (.8, .3, .1, .04)))
        self.REQ = self.DELETES
        driver, script, result, events = self.run_agent(
            [other] * 12, [[("TAP", "Delete", None)]] * 10, items=self.DELETE_ITEMS)
        self.assertEqual(driver.actions, [], "a delete of something the request doesn't name never reaches the phone")
        self.assertEqual((result["status"], result["code"]), ("blocked", "stuck"))
        self.assertIn("refused the same step 4 times", result["reason"])
        self.assertEqual(script.usage["calls"], 4, "it stopped at the 4th refusal, not at the turn limit")
        self.assertIn("frontier_stuck", [e["event"] for e in events])

    def test_one_refusal_then_another_way_is_not_stuck(self):
        feed = screen(Element("1", "Delete", "Button", (.8, .3, .1, .04)),
                      Element("2", "Archive", "Button", (.8, .5, .1, .04)))
        self.REQ = "Clean up my inbox but do not delete anything."
        driver, _, result, _ = self.run_agent(
            [feed] * 8, [[("TAP", "Delete", None)]] * 3 + [[("DONE", None, None)]] * 3,
            items=[item(kind="FORBID", app="QuickChat", what="emails", act="delete", quote="do not delete anything")])
        self.assertEqual(driver.actions, [])
        self.assertNotEqual(result.get("code"), "stuck", "three refusals are not yet stuck")

    def test_without_a_contract_the_regex_guard_stays(self):
        feed = screen(Element("1", "Delete", "Button", (.8, .3, .1, .04)))
        driver, _, result, events = self.run_agent([feed] * 3, [[("TAP", "Delete", None)], [("DONE", None, None)],
                                                                [("DONE", None, None)]], items=[])
        self.assertEqual(driver.actions, [])
        self.assertIn("frontier_checklist", [e["event"] for e in events])


import json as _json
from pathlib import Path as _Path


def _store(name):
    data = _json.loads((_Path(__file__).parent / "fixtures" / "frontier" / f"{name}.json").read_text())
    return {key: Snapshot([Element(r["id"], r["label"], r["role"], tuple(r["rect"]), value=r.get("value", ""))
                           for r in rows], "", 400, 800, "synthetic_fixture", bundle_id=data["bundle"])
            for key, rows in data["screens"].items()}


class InstallActTests(unittest.TestCase):
    SCREENS = _store("app_store_page")
    DUOLINGO = item(app="App Store", what="Duolingo", act="install", quote="Install Duolingo")

    def gate(self, label, screen_name="page", items=None, app="App Store"):
        snapshot = self.SCREENS[screen_name]
        element = next(e for e in snapshot.elements if e.label == label and (e.rect[1] < .3 or label != "Get"))
        c = contract("Install Duolingo from the App Store.", *(items or [self.DUOLINGO]))
        return c, c.check("TAP", element, snapshot, app)

    def test_get_beside_the_named_app_is_the_declared_install_and_asks_with_its_name(self):
        _, gate = self.gate("Get")
        self.assertTrue(gate.allowed)
        self.assertEqual(gate.item.act, "install")
        self.assertTrue(KA.asks(gate.item))
        self.assertEqual(KA.approval_title(gate.item, gate.amount), "Install Duolingo from the App Store?")

    def test_the_ads_get_button_is_refused(self):
        snapshot = self.SCREENS["page"]
        ad_get = [e for e in snapshot.elements if e.label == "Get"][1]
        c = contract("Install Duolingo from the App Store.", self.DUOLINGO)
        gate = c.check("TAP", ad_get, snapshot, "App Store")
        self.assertFalse(gate.allowed)
        self.assertIn("not beside 'Duolingo'", gate.reason)

    def test_an_undeclared_get_is_refused_and_get_elsewhere_is_no_commit(self):
        _, gate = self.gate("Get", items=[item(app="App Store", what="Duolingo", act="none", kind="READ")])
        self.assertFalse(gate.allowed)
        maps = Snapshot([Element("6", "Get Directions", "Button", (.1, .8, .8, .05))], "", 400, 800,
                        "synthetic_fixture", bundle_id="com.apple.Maps")
        c = contract("Get directions home.", item(kind="READ", app="Maps", what="directions"))
        self.assertTrue(c.check("TAP", maps.elements[0], maps, "Maps").allowed)
        self.assertIsNone(c.check("TAP", maps.elements[0], maps, "Maps").item)

    def test_a_price_button_buys_and_the_title_says_how_much(self):
        things = item(app="App Store", what="Things 3", act="install", quote="Buy Things 3")
        snapshot = self.SCREENS["paid"]
        c = contract("Buy Things 3 on the App Store.", things)
        gate = c.check("TAP", snapshot.elements[1], snapshot, "App Store")
        self.assertTrue(gate.allowed)
        self.assertEqual(KA.approval_title(gate.item, gate.amount), "Buy Things 3 for $9.99?")
        bounded = contract("Buy Things 3 if it is under $5.", item(app="App Store", what="Things 3", act="install",
                                                                    quote="Buy Things 3", condition="under $5"))
        self.assertFalse(bounded.check("TAP", snapshot.elements[1], snapshot, "App Store").allowed)

    def test_an_install_item_is_never_served_outside_the_app_store(self):
        settings = Snapshot([Element("1", "Install Now", "Button", (.1, .5, .8, .05)),
                             Element("2", "Duolingo", "StaticText", (.1, .45, .8, .04))], "", 400, 800,
                            "synthetic_fixture", bundle_id="com.apple.Preferences")
        c = contract("Install Duolingo from the App Store.", self.DUOLINGO)
        gate = c.check("TAP", settings.elements[0], settings, "")
        self.assertFalse(gate.allowed)

    def test_the_plan_line_names_the_app(self):
        c = contract("Install Duolingo from the App Store.", self.DUOLINGO)
        self.assertEqual(KA.plan_line(c), "This task will install Duolingo.")

    def test_in_a_compact_list_only_the_named_apps_own_get_is_served(self):
        rows = []
        for i, name in enumerate(["Babbel - Language Learning", "Duolingo - Language Lessons", "Memrise"]):
            y = .30 + i * .09
            rows += [Element(str(10 + i), name, "StaticText", (.25, y, .45, .03)),
                     Element(str(20 + i), "Get", "Button", (.75, y, .18, .03))]
        snapshot = Snapshot(rows, "", 400, 800, "synthetic_fixture", bundle_id=KS.APP_STORE)
        c = contract("Install Duolingo from the App Store.", self.DUOLINGO)
        for get, allowed in ((rows[1], False), (rows[3], True), (rows[5], False)):
            with self.subTest(row=get.rect[1]):
                self.assertEqual(c.check("TAP", get, snapshot, "App Store").allowed, allowed)

    def test_apples_confirmation_sheet_is_no_receipt_and_open_is(self):
        page, sheet = self.SCREENS["page"], self.SCREENS["confirm"]
        c, gate = self.gate("Get")
        get = next(e for e in page.elements if e.label == "Get")
        c.observe(page, "App Store", 0)
        c.committed(gate, get, page, "App Store", 0)
        c.observe(sheet, "App Store", 1)
        self.assertEqual(c.items[0].receipts, [])
        opened = Snapshot([page.elements[0], Element("2", "Open", "Button", get.rect)], "", 400, 800,
                          "synthetic_fixture", bundle_id=KS.APP_STORE)
        c.observe(opened, "App Store", 2)
        self.assertEqual([r["receipt"] for r in c.items[0].receipts], ["shown: open"])


class AbsenceProofTests(unittest.TestCase):
    def test_an_empty_state_screen_proves_a_no(self):
        day = _store("calendar_empty_day")["day"]
        c = contract("What's on my calendar tomorrow?", item(kind="REPORT", app="Calendar", what="events tomorrow"))
        c.observe(day, "Calendar", 0)
        c.update([{"id": 1, "status": "found", "value": "No events tomorrow"}], 1)
        self.assertEqual(c.proof(c.items[0]), (True, ""))
        self.assertEqual(c.done_gate("Nothing tomorrow.", 30)[0], True)

    def test_a_no_without_any_empty_screen_is_still_unproven_and_reopens_once(self):
        c = contract("What's on my calendar tomorrow?", item(kind="REPORT", app="Calendar", what="events tomorrow"))
        c.observe(Snapshot([Element("1", "Tuesday, October 6", "StaticText", (.1, .1, .8, .04))], "", 400, 800,
                           "synthetic_fixture"), "Calendar", 0)
        c.update([{"id": 1, "status": "found", "value": "No events tomorrow"}], 1)
        self.assertFalse(c.proof(c.items[0])[0])
        self.assertEqual(KS.MAX_REOPENS, 1)
        self.assertFalse(c.done_gate("Nothing.", 30)[0])
        self.assertTrue(c.done_gate("Nothing.", 30)[0])
        self.assertEqual([i.id for i in c.unproven()], [1])


class MissingNeedsProofTests(unittest.TestCase):
    TERMINAL = item(kind="REPORT", app="SkyTrip", what="terminal", quote="and terminal")

    def test_missing_with_nothing_that_shows_it_absent_is_sent_back_once(self):
        c = contract("Check SkyTrip for my flight's seat and terminal.", self.TERMINAL)
        c.observe(shot(el("Gate", "StaticText"), el("E13", "StaticText")), "SkyTrip", 1)
        c.update([{"id": 1, "status": "missing", "value": "Terminal not shown; gate E13"}], 1)
        proven, reason = c.proof(c.items[0])
        self.assertFalse(proven)
        self.assertIn("marked missing, but no screen showed it absent", reason)
        self.assertFalse(c.done_gate("Gate E13; no terminal shown.", 30)[0])
        self.assertTrue(c.done_gate("Gate E13; no terminal shown.", 30)[0])

    def test_missing_after_an_empty_search_in_its_app_stands(self):
        c = contract("Do I have contacts in Seattle?",
                     item(kind="REPORT", app="QuickChat", what="contacts in Seattle", quote="contacts in Seattle"))
        field = el("Search", "SearchField", editable=True, value="Seattle", y=.1)
        c.record_typed("QuickChat", field, "Seattle", 1)
        c.observe(shot(field, el("No matches", "StaticText", y=.4)), "QuickChat", 1)
        c.update([{"id": 1, "status": "missing"}], 2)
        self.assertEqual(c.proof(c.items[0]), (True, ""))

    def test_missing_where_the_app_says_it_is_empty_stands(self):
        c = contract("What trips do I have this week?",
                     item(kind="REPORT", app="SkyTrip", what="trips this week", quote="trips do I have this week"))
        c.observe(shot(el("No trips", "StaticText", y=.4)), "SkyTrip", 1)
        c.update([{"id": 1, "status": "missing"}], 1)
        self.assertEqual(c.proof(c.items[0]), (True, ""))

    def test_missing_in_an_app_never_opened_still_says_so(self):
        c = contract("Check SkyTrip for my terminal.", self.TERMINAL)
        c.update([{"id": 1, "status": "missing"}], 1)
        self.assertEqual(c.proof(c.items[0]), (False, "marked missing but SkyTrip was never opened"))

    def test_a_found_value_is_unchanged(self):
        c = contract("Check SkyTrip for my terminal.", self.TERMINAL)
        c.observe(shot(el("Terminal 2, Gate E13", "StaticText")), "SkyTrip", 1)
        c.update([{"id": 1, "status": "found", "value": "Terminal 2"}], 1)
        self.assertEqual(c.proof(c.items[0]), (True, ""))


if __name__ == "__main__":
    unittest.main()


class ProvenItemsShowInThePromptTests(unittest.TestCase):

    def test_a_read_proven_by_a_search_row_is_marked_seen(self):
        c = contract("Read the locker code in the note 'MobsterBench Note' in Notes.",
                     item(kind="READ", app="Notes", what="'MobsterBench Note'"))
        self.assertTrue(c.lines()[0].startswith("1. [ ] READ"))
        c.observe(Snapshot([Element("r", "MobsterBench Note, modified September 24, 2026, Locker code: 4817", "Cell",
                                    (0, .3, 1, .06), False)], "", 400, 800, "synthetic_fixture",
                           bundle_id="com.apple.mobilenotes"), "Notes", 1)
        self.assertTrue(c.lines()[0].startswith("1. [seen on screen] READ"))

    def test_a_read_not_yet_seen_stays_open(self):
        c = contract("Read the locker code in the note 'MobsterBench Note' in Notes.",
                     item(kind="READ", app="Notes", what="'MobsterBench Note'"))
        c.observe(Snapshot([Element("r", "Groceries", "Cell", (0, .3, 1, .06), False)], "", 400, 800,
                           "synthetic_fixture", bundle_id="com.apple.mobilenotes"), "Notes", 1)
        self.assertTrue(c.lines()[0].startswith("1. [ ] READ"))
